"""
ChannelFlow AI - Extra Forward Credits service (PRD section 23)
=================================================================

Extra credits are PREPAID forwarding units (not per-forward wallet
billing, PRD 23). A user whose daily plan allowance is exhausted may
keep forwarding by consuming one extra credit per forwarded message
(core/forwarder.py enforces the PRD 23.2 priority: daily allowance
first, extra credits only after it is exhausted).

Ledger + idempotency (PRD 23.1): every balance change is journaled in
extra_credit_ledger with a UNIQUE `reference` acting as the
idempotency identifier. Kinds: purchase / grant / consumption /
adjustment / refund. Duplicate application of the same event is
impossible - the UNIQUE reference rejects it inside the same
transaction, so replays (double webhook, double approval, restart
redelivery of a consumed message) never double-apply.

Atomicity: balance changes run inside BEGIN IMMEDIATE so N concurrent
forwards at zero balance can never all pass the balance>0 check
(same discipline as plan_service.reserve_daily_forward).

Packages (PRD 23 / 26): configurable credit_packages rows with three
independent price books (INR / USD / Stars - never converted). Plan
purchases and admin approvals activate through activate_payment_row()
which is only ever called AFTER the payment_request row has been
flipped to its final status exactly once by the payment layer.
"""

import logging
import uuid

from database.db import get_connection

logger = logging.getLogger(__name__)


# ==========================================================
# BALANCE + LEDGER CORE
# ==========================================================

def get_balance(user_id) -> int:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT balance FROM extra_credits WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return int(row["balance"]) if row else 0


def _ensure_row(cur, user_id):
    cur.execute(
        "INSERT INTO extra_credits(user_id, balance) VALUES (?, 0) "
        "ON CONFLICT(user_id) DO NOTHING",
        (user_id,),
    )


def apply(user_id, delta, kind, reference, reason=None, admin_id=None) -> bool:
    """Apply one signed ledger entry atomically and idempotently.

    Returns True when applied. Returns False when the reference is a
    duplicate (already applied) or when the change would push the
    balance below zero. Raises on DB errors.
    """
    delta = int(delta)
    conn = get_connection()
    try:
        cur = conn.cursor()
        conn.execute("BEGIN IMMEDIATE")
        _ensure_row(cur, user_id)
        cur.execute("SELECT balance FROM extra_credits WHERE user_id=?", (user_id,))
        current = int(cur.fetchone()["balance"])

        if current + delta < 0:
            conn.rollback()
            return False

        new_balance = current + delta
        cur.execute(
            "UPDATE extra_credits SET balance=?, updated_at=CURRENT_TIMESTAMP "
            "WHERE user_id=?",
            (new_balance, user_id),
        )
        try:
            cur.execute(
                "INSERT INTO extra_credit_ledger"
                "(user_id, delta, balance_after, kind, reference, reason, admin_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, delta, new_balance, kind, reference, reason, admin_id),
            )
        except Exception:
            # UNIQUE(reference) violation -> duplicate event, roll back.
            conn.rollback()
            return False
        conn.commit()
        return True
    finally:
        conn.close()


def consume(user_id, reference, reason=None) -> bool:
    """Consume exactly one credit for one forwarded message.
    Returns False when the user has no credits left (caller drops the
    message exactly like a daily-quota rejection)."""
    return apply(user_id, -1, "consumption", reference,
                 reason=reason or "forwarded message")


def refund(user_id, consume_reference, reason=None) -> bool:
    """Give back one credit after a consumed forward failed to publish
    (mirror of plan_service.release_daily_forward). Idempotent: the
    reference 'refund:<consume_reference>' can only ever apply once."""
    return apply(user_id, +1, "refund", f"refund:{consume_reference}",
                 reason=reason or "refund of failed forward")


def admin_adjust(user_id, delta, reason, admin_id) -> bool:
    """PRD 23.3: admin grant (+)/revoke (-) with reason + identity."""
    return apply(user_id, delta, "adjustment",
                 reference=f"adjust:{admin_id}:{uuid.uuid4().hex[:20]}",
                 reason=reason, admin_id=admin_id)


def recent_ledger(user_id, limit=8):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM extra_credit_ledger WHERE user_id=? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


# ==========================================================
# PACKAGES (configurable; PRD 23/26)
# ==========================================================

def list_packages(active_only=True):
    conn = get_connection()
    cur = conn.cursor()
    if active_only:
        cur.execute("SELECT * FROM credit_packages WHERE active=1 ORDER BY forwards_amount ASC")
    else:
        cur.execute("SELECT * FROM credit_packages ORDER BY forwards_amount ASC")
    rows = cur.fetchall()
    conn.close()
    return rows


def get_package(package_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM credit_packages WHERE id=?", (package_id,))
    row = cur.fetchone()
    conn.close()
    return row


# ==========================================================
# PAYMENT-SNAPSHOT ROW CREATION (UPI / crypto / Stars)
# ==========================================================
# One row per purchase attempt in payment_requests, purpose
# 'extra_credit', currency = the method's price book, amount columns
# frozen at creation time (same snapshot discipline as plan rows).

def create_offline_package_request(user_id, package_id, method,
                                   payment_reference=None):
    """Create a UPI ('upi') or crypto ('crypto') payment_request for a
    credit package. Returns the row. Raises ValueError when the package
    is missing/inactive or has no price in that book."""
    pkg = get_package(package_id)
    if pkg is None or not pkg["active"]:
        raise ValueError("This credit package is no longer available.")

    if method == "upi":
        amount_inr = float(pkg["price_inr"] or 0)
        if amount_inr <= 0:
            raise ValueError("UPI price is not configured for this package.")
        currency = "INR"
        final = amount_inr
        amount_usd = round(amount_inr / 85, 2)
    elif method == "crypto":
        amount_usd = float(pkg["price_usd"] or 0)
        if amount_usd <= 0:
            raise ValueError("Crypto price is not configured for this package.")
        currency = "USD"
        final = amount_usd
        amount_inr = round(amount_usd * 85, 2)
    else:
        raise ValueError(f"Unsupported offline method: {method}")

    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO payment_requests(
            user_id, plan, months, method, currency,
            base_amount, discount_percent, final_amount,
            amount_inr, amount_usd,
            payment_reference, status, purpose, package_id, extra_forwards
        )
        VALUES (?, NULL, NULL, ?, ?, ?, 0, ?, ?, ?, ?, 'PENDING_PAYMENT', 'extra_credit', ?, ?)
        """,
        (
            user_id, method, currency,
            final, final,
            amount_inr, amount_usd,
            payment_reference,
            pkg["id"], pkg["forwards_amount"],
        ),
    )
    conn.commit()
    request_id = cur.lastrowid
    conn.close()
    from services.payment_service import get_payment_request
    return get_payment_request(request_id)


def create_stars_package_request(user_id, package_id):
    """Create the Stars (XTR) payment_request snapshot for a credit
    package. Returns the row. Raises ValueError when unconfigured."""
    pkg = get_package(package_id)
    if pkg is None or not pkg["active"]:
        raise ValueError("This credit package is no longer available.")
    stars = int(pkg["stars_price"] or 0)
    if stars <= 0:
        raise ValueError("Stars price is not configured for this package.")

    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO payment_requests(
            user_id, plan, months, method, currency,
            base_amount, discount_percent, final_amount,
            amount_inr, amount_usd,
            status, purpose, package_id, extra_forwards
        )
        VALUES (?, NULL, NULL, 'stars', 'STARS', ?, 0, ?, 0, ?, 'PENDING_PAYMENT', 'extra_credit', ?, ?)
        """,
        (
            user_id,
            float(stars), float(stars), float(stars),
            pkg["id"], pkg["forwards_amount"],
        ),
    )
    conn.commit()
    request_id = cur.lastrowid
    conn.close()
    from services.payment_service import get_payment_request
    return get_payment_request(request_id)


def activate_payment_row(row):
    """Grant the purchased package. Called by the payment layer ONLY
    after the payment_request was flipped to its final approved status
    exactly once (admin approval or Telegram-success), so this function
    itself is guarded by that single flip plus the ledger UNIQUE
    reference - double approval can never double-grant."""
    if row["purpose"] != "extra_credit":
        return False
    user_id = row["user_id"]
    forwards = int(row["extra_forwards"] or 0)
    if forwards <= 0:
        logger.error("extra_credit payment %s has no forwards amount", row["id"])
        return False
    method = row["method"]
    applied = apply(
        user_id, forwards, "purchase",
        reference=f"purchase:payment_request:{row['id']}",
        reason=f"Credit package purchase via {method} (request #{row['id']})",
    )
    if applied:
        logger.info("Extra credits granted: user=%s forwards=%s request=%s",
                    user_id, forwards, row["id"])
    return applied


# ==========================================================
# UI HELPERS
# ==========================================================

def package_price_line(pkg):
    """'5,000 forwards — ⭐550' style label with whatever price books
    the package actually carries."""
    parts = []
    if pkg["stars_price"]:
        parts.append(f"⭐{int(pkg['stars_price'])}")
    if pkg["price_inr"]:
        parts.append(f"₹{float(pkg['price_inr']):.0f}")
    if pkg["price_usd"]:
        parts.append(f"${float(pkg['price_usd']):.2f}")
    price = " · ".join(parts) if parts else "not configured"
    return f"{int(pkg['forwards_amount']):,} forwards — {price}"


def available_methods_for(pkg):
    """Methods a package can actually be bought with right now."""
    from services import payment_service
    methods = []
    if int(pkg["stars_price"] or 0) > 0:
        methods.append("stars")
    if float(pkg["price_inr"] or 0) > 0 and payment_service.is_upi_configured():
        methods.append("upi")
    if float(pkg["price_usd"] or 0) > 0 and payment_service.is_oxapay_configured():
        methods.append("crypto")
    return methods
