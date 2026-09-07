"""
ChannelFlow AI - Telegram Stars (XTR) checkout backend
=========================================================

Implements PRD section 26 (Telegram Stars plan purchase):

    Select plan/duration -> send_invoice(XTR) -> user pays in the
    Telegram client -> Telegram pre_checkout_query (answered OK /
    rejected) -> successful_payment update -> local request finalized
    exactly once -> plan activated -> payment history row.

Design rules honoured here:

* NEVER trust the user. The bot renders the invoice (amount, payload)
  from a DB snapshot row. On the way back we re-derive the expected
  cost from the payment_requests snapshot (base_amount /
  discount_percent / final_amount) and verify it matches Telegram's own
  `total_amount`. A crafted update or an edited price can never mint a
  plan.
* Duplicate protection: the row can only move PENDING_PAYMENT ->
  SUCCESS once (guarded UPDATE ... WHERE status='PENDING_PAYMENT'),
  exactly like approve_crypto_payment. Restarts/delays replaying the
  update are no-ops.
* Expiry: pending Stars invoices expire after
  STARS_INVOICE_LIFETIME_MINUTES (same policy as crypto). A
  successful_payment arriving for an EXPIRED/CANCELLED row is refused;
  Telegram refunds the Stars on its side automatically when the
  invoice is invalid/expired at charge time.
* Stars unit handling: plan_configs.stars_monthly_price is an INTEGER
  count of Stars (Telegram XTR is integer-only). Telegram's
  successful_payment.total_amount is in the currency's minor unit -
  for XTR the minor unit IS 1 Star, so a 249-Star invoice reports
  total_amount == 249.
* Nothing user-secret is stored or logged (only the same
  plan/duration/amount rows UPI/crypto already record).

The pre_checkout_query / successful_payment handlers live in
bot/handlers.py; this module is the pure service layer (DB + business
rules) with no telegram import so it stays unit-testable offline.
"""

import logging
from datetime import datetime, timedelta, timezone

from database.db import get_connection
from services import plan_service, pricing_service, referral_service

logger = logging.getLogger(__name__)

# Stars invoices expire on the same clock as crypto invoices (minutes).
STARS_INVOICE_LIFETIME_MINUTES = 15

# Telegram's currency token for Stars invoices.
STARS_CURRENCY = "XTR"

# Payload namespaces (distinct from the inline upgrade:* chain so a
# stale payload can never collide with a live callback).
PAYLOAD_PLAN = "xtr:plan:"          # xtr:plan:<request_id>  (plan purchase)
PAYLOAD_PKG = "xtr:pkg:"            # xtr:pkg:<request_id>   (extra-credit package)

# Plans purchasable with Stars (same set as UPI/crypto).
PURCHASABLE_PLANS = ("BEGINNER", "PRO", "CREATOR")


def _now():
    return datetime.now(timezone.utc)


def get_stars_price(plan_name) -> int:
    """Monthly Stars price from the DB (admin-configurable)."""
    return int(plan_service.get_plan_stars_price(plan_name) or 0)


def get_stars_options(plan):
    """Duration options labelled in Stars (used by the live Stars
    duration picker). Each option: {months, label, total, full,
    discount_percent}. Rows come from the same pricing tables UPI and
    crypto use - never a hard-coded price."""
    out = []
    for row in pricing_service.get_duration_options(plan):
        months = row["months"]
        try:
            total, discount, full = invoice_details_for(plan, months)
        except ValueError:
            continue  # Stars price unconfigured - omit option
        if discount > 0:
            label = (f"{months} mo — ⭐{total} "
                     f"(was ⭐{full}) · save {discount:.0f}%")
        else:
            label = f"{months} mo — ⭐{total}"
        out.append({
            "months": months,
            "label": label,
            "total": total,
            "full": full,
            "discount_percent": discount,
        })
    return out


def _duration_discount(plan, months):
    durations = {row["months"]: row for row in pricing_service.get_duration_options(plan)}
    row = durations.get(months)
    if row is None:
        raise ValueError(f"Invalid duration: {months} months for plan {plan}")
    return row["discount_percent"] or 0


def invoice_details_for(plan, months):
    """(total_stars, discount_percent, full_stars) used to build the
    invoice AND to verify Telegram's charge when it comes back.

    Telegram requires integer Stars; a discounted total is rounded DOWN
    to a whole Star so the user never pays more than the exact
    discounted amount (same policy as INR floor rounding)."""
    monthly = get_stars_price(plan)
    if monthly <= 0:
        raise ValueError(f"Stars price not configured for plan {plan}")
    full = monthly * months
    discount = _duration_discount(plan, months)
    total = int(full * (1 - discount / 100)) if discount > 0 else full
    return max(1, total), discount, full


def create_plan_invoice_row(user_id, plan, months):
    """Create the local snapshot row for a Stars plan purchase and
    return it. Raises ValueError on invalid plan/duration or when the
    Stars price is unconfigured (caller turns that into user copy)."""
    if plan not in PURCHASABLE_PLANS:
        raise ValueError(f"{plan} is not a purchasable plan")
    total, discount, full = invoice_details_for(plan, months)

    expires_at = (_now() + timedelta(minutes=STARS_INVOICE_LIFETIME_MINUTES)).isoformat()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO payment_requests(
            user_id, plan, months, method, currency,
            base_amount, discount_percent, final_amount,
            amount_inr, amount_usd,
            status, purpose, expires_at
        )
        VALUES (?, ?, ?, 'stars', 'STARS', ?, ?, ?, ?, ?, 'PENDING_PAYMENT', 'plan', ?)
        """,
        (
            user_id, plan, months,
            float(full), discount, float(total),
            0, float(total),   # amount_inr n/a; amount_usd column carries the Stars count
            expires_at,
        ),
    )
    conn.commit()
    request_id = cur.lastrowid
    conn.close()
    logger.info("Stars invoice row %s created: user=%s plan=%s months=%s stars=%s",
                request_id, user_id, plan, months, total)
    return get_request(request_id)


def get_request(request_id):
    """Returns the payment_requests row (sqlite Row) or None."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM payment_requests WHERE id=?", (request_id,))
    row = cur.fetchone()
    conn.close()
    return row


def cancel_request(request_id):
    """Cancel a still-pending Stars invoice (e.g. send_invoice failed
    so the row must not linger as a payable request)."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE payment_requests SET status='CANCELLED' "
        "WHERE id=? AND status='PENDING_PAYMENT'",
        (request_id,),
    )
    conn.commit()
    conn.close()


def resolve_stars_success(request_id, telegram_user_id, paid_stars):
    """Finalize a Telegram Stars successful_payment exactly once.

    Validates, in order:
      1. the request row exists and has method='stars';
      2. it belongs to telegram_user_id;
      3. it is still PENDING_PAYMENT (anything decided earlier is a
         replay -> "already_decided");
      4. it has not expired (flips the row to EXPIRED when it has);
      5. the Telegram-reported paid_stars equals the snapshot
         final_amount (a mismatch means the user paid a different
         amount than quoted - refuse, never partially credit).

    When every check passes, atomically flips the row to 'SUCCESS' and
    activates what was paid for (plan subscription, or extra-credit
    package for purpose='extra_credit' - shared, idempotent logic).

    Returns (row_after, "ok") on success, else (row, reason) where
    reason is one of: not_found / not_stars / not_owner /
    already_decided / expired / amount_mismatch.
    """
    row = get_request(request_id)
    if row is None:
        return None, "not_found"
    if row["method"] != "stars":
        return row, "not_stars"
    if int(row["user_id"]) != int(telegram_user_id):
        return row, "not_owner"
    if row["status"] != "PENDING_PAYMENT":
        return row, "already_decided"

    if row["expires_at"]:
        try:
            exp = datetime.fromisoformat(row["expires_at"])
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if _now() > exp:
                _flip(request_id, "EXPIRED")
                return get_request(request_id), "expired"
        except (TypeError, ValueError):
            pass

    if int(paid_stars or 0) != int(float(row["final_amount"] or 0)):
        logger.warning("Stars amount mismatch request=%s user=%s paid=%s expected=%s",
                       request_id, telegram_user_id, paid_stars, row["final_amount"])
        return row, "amount_mismatch"

    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE payment_requests SET status='SUCCESS' WHERE id=? AND status='PENDING_PAYMENT'",
        (request_id,),
    )
    changed = cur.rowcount > 0
    conn.commit()
    conn.close()

    if not changed:
        return get_request(request_id), "already_decided"

    after = get_request(request_id)
    _activate(after)
    return after, "ok"


def _flip(request_id, status):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE payment_requests SET status=? WHERE id=?", (status, request_id))
    conn.commit()
    conn.close()


def _activate(row):
    """Grant what was paid for (mirror of payment_service activation;
    only ever invoked once per row thanks to the guarded SUCCESS flip).
    Purpose branches: 'plan' -> activate subscription; 'extra_credit'
    -> grant the purchased prepaid forwards package (PRD 23)."""
    user_id = row["user_id"]
    if row["purpose"] == "extra_credit":
        from services import extra_credits_service
        extra_credits_service.activate_payment_row(row)
        return
    plan = row["plan"]
    months = row["months"] or 1
    expiry = (_now() + timedelta(days=30 * months)).isoformat()

    plan_service.set_user_plan(user_id, plan, expiry=expiry)
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET last_purchase_plan=?, last_purchase_months=? WHERE telegram_id=?",
        (plan, months, user_id),
    )
    conn.commit()
    conn.close()
    referral_service.grant_reward_if_pending(user_id, plan)
    logger.info("Plan activated from Stars success: user=%s plan=%s months=%s request=%s",
                user_id, plan, months, row["id"])


def get_user_stars_history(user_id, limit=10):
    """Payment history for Stars purchases (PRD 26: payment history)."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM payment_requests WHERE user_id=? AND method='stars' "
        "ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    )
    rows = cur.fetchall()
    conn.close()
    return rows
