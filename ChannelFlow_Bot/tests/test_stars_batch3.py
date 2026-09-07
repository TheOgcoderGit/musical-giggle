"""
⭐ Telegram Stars checkout batch tests (Batch 3, PRD section 26).

Covers the full lifecycle at service + handler level:

* DB-driven pricing: invoice details honour plan_configs.
  stars_monthly_price and plan_durations discount_percent
* local snapshot row creation (PENDING_PAYMENT, method=stars,
  currency=STARS, purpose=plan) with expiry
* resolve_stars_success: price-match + ownership + idempotent SUCCESS
  flip -> plan activation exactly once; every refusal reason
* payment history (only Stars rows, newest first)
* the real button_handler chain acct:upgrade -> upgrade:splans ->
  splan -> sduration -> scheckout (send_invoice captured offline)
* pre_checkout_query handler: accept/refuse matrix
* successful_payment handler: grant + receipt, expired-refund copy,
  duplicate replay no-op, unparseable payload

Live Telegram E2E stays BLOCKED in the sandbox (no real credentials);
these tests exercise everything up to the Telegram API boundary.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.conftest_shared import reset_db  # noqa: E402

import pytest  # noqa: E402

# ---- environment imports (must happen after conftest_shared) ----------
from database import models as db_models  # noqa: E402
from database.db import get_connection  # noqa: E402
from services import payment_service, plan_service, pricing_service  # noqa: E402
from services import stars_service  # noqa: E402

import bot.handlers as h  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_db():
    reset_db()
    yield
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove("ChannelFlow.session" + suffix)
        except FileNotFoundError:
            pass


# ==========================================================
# Minimal fakes (duck-typed, no network) - Stars variants
# ==========================================================

class _Chat:
    def __init__(self, chat_id):
        self.id = chat_id

    async def delete_message(self, message_id):
        pass


_ID = [3000]


class FakeMsg:
    def __init__(self, chat_id=7, text="", message_id=None):
        self.chat_id = chat_id
        self.chat = _Chat(chat_id)
        self.text = text
        self.message_id = message_id if message_id is not None else _ID[0]
        _ID[0] += 1
        self.replies = []
        self.edits = []
        self.successful_payment = None

    async def reply_text(self, text, reply_markup=None, **kwargs):
        m = FakeMsg(chat_id=self.chat_id, text=text)
        m.reply_markup = reply_markup
        self.replies.append(m)
        return m

    async def edit_text(self, text, reply_markup=None, **kwargs):
        self.edits.append(text)
        self.text = text
        self.reply_markup = reply_markup
        return self

    async def delete(self):
        pass


class FakeQuery:
    def __init__(self, message, user=None):
        self.message = message
        self.data = ""
        self.alerts = []
        self.from_user = user or FakeUser(7)

    async def answer(self, text=None, show_alert=False, **kwargs):
        self.alerts.append((text, show_alert))

    async def edit_message_text(self, text, reply_markup=None, **kwargs):
        return await self.message.edit_text(text, reply_markup=reply_markup)


class FakeUser:
    def __init__(self, uid, first="Tester", username=None):
        self.id = uid
        self.first_name = first
        self.username = username


class FakeUpdate:
    def __init__(self, user, message=None, pre_checkout_query=None,
                 successful_payment=None, callback_query=None):
        self.effective_user = user
        self.message = message
        self.effective_message = message
        self.pre_checkout_query = pre_checkout_query
        self.callback_query = callback_query
        if successful_payment is not None:
            message.successful_payment = successful_payment


class FakePreCheckout:
    def __init__(self, user, payload, total_amount, currency="XTR",
                 query_id="q1"):
        self.from_user = user
        self.invoice_payload = payload
        self.total_amount = total_amount
        self.currency = currency
        self.id = query_id
        self.answered = []

    async def answer(self, ok, error_message=None, **kwargs):
        self.answered.append((ok, error_message))


class FakeBot:
    def __init__(self):
        self.invoices = []

    async def send_message(self, chat_id, text, **kwargs):
        return FakeMsg(chat_id=chat_id, text=text)

    async def send_invoice(self, **kwargs):
        self.invoices.append(dict(kwargs))
        return FakeMsg(chat_id=kwargs.get("chat_id", 0), text="invoice")

    async def answer_pre_checkout_query(self, query_id, ok, error_message=None, **kwargs):
        return True


class FakeContext:
    def __init__(self):
        self.args = []
        self.bot_data = {}
        self.bot = FakeBot()


def _run(coro):
    return asyncio.run(coro)


def _flat_callbacks(markup):
    out = []
    for row in markup.inline_keyboard:
        for btn in row:
            if btn.callback_data:
                out.append(btn.callback_data)
            elif btn.url:
                out.append(f"url:{btn.url}")
    return out


def _register(uid, name="User"):
    db_models.register_user(uid, f"u{uid}", name)
    return uid


# ==========================================================
# Pricing (DB-configured Stars amounts + duration discounts)
# ==========================================================

def test_invoice_details_are_db_driven():
    """1-month totals equal plan_configs.stars_monthly_price exactly;
    discounted durations floor-round to a whole Star (never round up)."""
    monthly = stars_service.get_stars_price("BEGINNER")
    assert monthly > 0  # seeded 99 in the DB
    total, discount, full = stars_service.invoice_details_for("BEGINNER", 1)
    assert total == monthly and full == monthly and discount == 0

    # PRO: 6-month row carries the 10% duration discount
    durations = {r["months"]: r for r in pricing_service.get_duration_options("PRO")}
    disc = durations[6]["discount_percent"]
    total6, disc6, full6 = stars_service.invoice_details_for("PRO", 6)
    monthly_pro = stars_service.get_stars_price("PRO")
    assert full6 == monthly_pro * 6
    assert disc6 == disc
    assert total6 == int(full6 * (1 - disc / 100))


def test_get_stars_options_lists_db_durations():
    opts = stars_service.get_stars_options("CREATOR")
    months = [o["months"] for o in opts]
    assert 1 in months and 12 in months
    for o in opts:
        assert o["total"] > 0 and o["label"].startswith(f"{o['months']} mo")
        assert "⭐" in o["label"]


# ==========================================================
# Local snapshot row creation
# ==========================================================

def test_create_plan_invoice_row_snapshot():
    uid = _register(4001)
    total, discount, full = stars_service.invoice_details_for("PRO", 3)
    row = stars_service.create_plan_invoice_row(uid, "PRO", 3)
    assert row["status"] == "PENDING_PAYMENT"
    assert row["method"] == "stars"
    assert row["currency"] == "STARS"
    assert row["purpose"] == "plan"
    assert row["plan"] == "PRO" and row["months"] == 3
    assert int(float(row["final_amount"])) == total
    assert int(float(row["amount_usd"])) == total  # column carries Stars
    assert float(row["amount_inr"]) == 0
    assert float(row["base_amount"]) == full
    assert row["expires_at"] is not None


def test_create_rejects_invalid_plan_and_unconfigured_price():
    uid = _register(4002)
    with pytest.raises(ValueError):
        stars_service.create_plan_invoice_row(uid, "FREE", 1)
    with pytest.raises(ValueError):
        stars_service.create_plan_invoice_row(uid, "PRO", 17)  # no such duration
    # admin zeroes the Stars price -> checkout must refuse, not mint a row
    conn = get_connection()
    conn.cursor().execute(
        "UPDATE plan_configs SET stars_monthly_price=0 WHERE plan_name='CREATOR'")
    conn.commit()
    conn.close()
    plan_service.invalidate_plan_configs_cache()
    with pytest.raises(ValueError):
        stars_service.create_plan_invoice_row(uid, "CREATOR", 1)
    # re-invalidate: the refused call re-populated the cache with the
    # zeroed price, which must not leak into later tests
    plan_service.invalidate_plan_configs_cache()


# ==========================================================
# resolve_stars_success - the money-to-plan boundary
# ==========================================================

def _plan_expiry(uid):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT plan, plan_expiry FROM users WHERE telegram_id=?", (uid,))
    row = cur.fetchone()
    conn.close()
    return row["plan"], row["plan_expiry"]


def test_resolve_success_activates_plan_exactly_once():
    uid = _register(4003)
    row = stars_service.create_plan_invoice_row(uid, "BEGINNER", 3)
    paid = int(float(row["final_amount"]))

    after, reason = stars_service.resolve_stars_success(row["id"], uid, paid)
    assert reason == "ok"
    assert after["status"] == "SUCCESS"
    assert _plan_expiry(uid)[0] == "BEGINNER"
    # last_purchase markers for the auto-renew scheduler
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT last_purchase_plan, last_purchase_months FROM users WHERE telegram_id=?", (uid,))
    lp = cur.fetchone()
    conn.close()
    assert lp["last_purchase_plan"] == "BEGINNER" and lp["last_purchase_months"] == 3

    # replay (restart/delivery duplication) must be a no-op
    _, plan_before, = _plan_expiry(uid)
    after2, reason2 = stars_service.resolve_stars_success(row["id"], uid, paid)
    assert reason2 == "already_decided"
    assert _plan_expiry(uid)[1] == plan_before


def test_resolve_refuses_wrong_owner():
    owner = _register(4004)
    stranger = _register(4005)
    row = stars_service.create_plan_invoice_row(owner, "PRO", 1)
    after, reason = stars_service.resolve_stars_success(row["id"], stranger,
                                                        int(float(row["final_amount"])))
    assert reason == "not_owner"
    assert after["status"] == "PENDING_PAYMENT"
    assert plan_service.get_user_plan(stranger) == "FREE"


def test_resolve_refuses_amount_mismatch():
    uid = _register(4006)
    row = stars_service.create_plan_invoice_row(uid, "PRO", 1)
    after, reason = stars_service.resolve_stars_success(row["id"], uid,
                                                        int(float(row["final_amount"])) - 1)
    assert reason == "amount_mismatch"
    assert after["status"] == "PENDING_PAYMENT"  # nothing granted, still pending
    assert plan_service.get_user_plan(uid) in ("FREE",)


def test_resolve_expires_stale_invoice():
    uid = _register(4007)
    row = stars_service.create_plan_invoice_row(uid, "PRO", 1)
    conn = get_connection()
    conn.cursor().execute(
        "UPDATE payment_requests SET expires_at=? WHERE id=?",
        ("2020-01-01T00:00:00+00:00", row["id"]),
    )
    conn.commit()
    conn.close()
    after, reason = stars_service.resolve_stars_success(row["id"], uid,
                                                        int(float(row["final_amount"])))
    assert reason == "expired"
    assert after["status"] == "EXPIRED"
    assert plan_service.get_user_plan(uid) == "FREE"


def test_resolve_not_found_and_not_stars():
    _, reason = stars_service.resolve_stars_success(999999, 1, 1)
    assert reason == "not_found"
    uid = _register(4008)
    rid = payment_service.create_payment_request(uid, "BEGINNER", 1, "upi",
                                                 payment_reference="ref-x")
    row, reason = stars_service.resolve_stars_success(rid, uid, 99)
    assert reason == "not_stars"


def test_cancel_request_only_pending():
    uid = _register(4009)
    row = stars_service.create_plan_invoice_row(uid, "BEGINNER", 1)
    stars_service.cancel_request(row["id"])
    assert stars_service.get_request(row["id"])["status"] == "CANCELLED"
    # cancelling again / after SUCCESS is a no-op
    stars_service.cancel_request(row["id"])
    assert stars_service.get_request(row["id"])["status"] == "CANCELLED"
    ok = stars_service.create_plan_invoice_row(uid, "BEGINNER", 1)
    stars_service.resolve_stars_success(ok["id"], uid, int(float(ok["final_amount"])))
    stars_service.cancel_request(ok["id"])
    assert stars_service.get_request(ok["id"])["status"] == "SUCCESS"


def test_history_lists_stars_rows_only_newest_first():
    uid = _register(4010)
    r1 = stars_service.create_plan_invoice_row(uid, "BEGINNER", 1)
    payment_service.create_payment_request(uid, "BEGINNER", 1, "upi",
                                           payment_reference="upi-1")
    r2 = stars_service.create_plan_invoice_row(uid, "PRO", 1)
    stars_service.resolve_stars_success(r1["id"], uid, int(float(r1["final_amount"])))
    history = [dict(x) for x in stars_service.get_user_stars_history(uid)]
    assert len(history) == 2
    assert all(x["method"] == "stars" for x in history)
    assert history[0]["id"] == r2["id"] and history[1]["id"] == r1["id"]


# ==========================================================
# button_handler chain: acct:upgrade -> splans -> splan ->
# sduration -> scheckout (real code path, offline bot)
# ==========================================================

def test_stars_checkout_chain_sends_xtr_invoice():
    uid = _register(4011)
    user = FakeUser(uid)
    ctx = FakeContext()
    inbox = FakeMsg(chat_id=9, text="x")

    async def tap(data, message):
        nq = FakeQuery(message, user=user)
        nq.data = data
        await h.button_handler(FakeUpdate(user, message, callback_query=nq), ctx)
        return nq

    _run(tap("acct:upgrade", inbox))
    # acct:upgrade replies a FRESH picker message; Stars is the top row
    screen = inbox.replies[-1]
    assert "upgrade:splans" in _flat_callbacks(screen.reply_markup)
    assert any("Stars" in b.text for row in screen.reply_markup.inline_keyboard for b in row)

    # every later Stars step EDITS that screen in place
    _run(tap("upgrade:splans", screen))
    cbs = _flat_callbacks(screen.reply_markup)
    assert "upgrade:splan:BEGINNER" in cbs
    assert any("⭐" in b.text for row in screen.reply_markup.inline_keyboard for b in row)

    _run(tap("upgrade:splan:BEGINNER", screen))
    assert "upgrade:sduration:BEGINNER:3" in _flat_callbacks(screen.reply_markup)

    _run(tap("upgrade:sduration:BEGINNER:3", screen))
    assert "upgrade:scheckout:BEGINNER:3" in _flat_callbacks(screen.reply_markup)
    assert "⭐" in screen.text

    _run(tap("upgrade:scheckout:BEGINNER:3", screen))
    # send_invoice captured by the fake bot with the XTR invoice
    assert len(ctx.bot.invoices) == 1
    inv = ctx.bot.invoices[0]
    assert inv["currency"] == "XTR"
    assert inv["provider_token"] == ""
    assert inv["payload"].startswith("xtr:plan:")
    request_id = int(inv["payload"].split(":")[2])
    row = stars_service.get_request(request_id)
    assert row["status"] == "PENDING_PAYMENT"
    assert inv["prices"][0].amount == int(float(row["final_amount"]))
    assert inv["title"] == "ChannelFlow BEGINNER - 3 mo"
    # user-facing confirmation was sent as a fresh reply
    assert any("Payment sheet sent" in m.text for m in screen.replies)


def test_scheckout_failure_cancels_row_and_informs():
    uid = _register(4012)
    user = FakeUser(uid)

    class BoomBot(FakeBot):
        async def send_invoice(self, **kwargs):
            raise RuntimeError("provider refused")

    ctx = FakeContext()
    ctx.bot = BoomBot()
    msg = FakeMsg(chat_id=9, text="x")
    q = FakeQuery(msg, user=user)
    q.data = "upgrade:scheckout:BEGINNER:1"
    _run(h.button_handler(FakeUpdate(user, msg, callback_query=q), ctx))
    # no invoice, no pending row left behind
    assert ctx.bot.invoices == []
    assert any("Could not open the Stars checkout" in m.text for m in msg.replies)
    rows = [dict(x) for x in stars_service.get_user_stars_history(uid)]
    assert rows and rows[0]["status"] == "CANCELLED"


# ==========================================================
# pre_checkout_query handler
# ==========================================================

def test_pre_checkout_accepts_valid_invoice():
    uid = _register(4013)
    row = stars_service.create_plan_invoice_row(uid, "BEGINNER", 1)
    pcq = FakePreCheckout(FakeUser(uid), f"xtr:plan:{row['id']}",
                          int(float(row["final_amount"])))
    _run(h.pre_checkout_handler(FakeUpdate(FakeUser(uid), pre_checkout_query=pcq),
                                FakeContext()))
    assert pcq.answered == [(True, None)]


@pytest.mark.parametrize("mangle,expected_error", [
    ("payload", "Unrecognized invoice"),
    ("owner", "not valid for your account"),
    ("amount", "Amount mismatch"),
    ("currency", "Amount mismatch"),
    ("decided", "already used or expired"),
])
def test_pre_checkout_refusal_matrix(mangle, expected_error):
    owner = _register(4014)
    stranger = _register(4015)
    row = stars_service.create_plan_invoice_row(owner, "PRO", 1)
    total = int(float(row["final_amount"]))

    payload = f"xtr:plan:{row['id']}"
    payer = FakeUser(owner)
    amount = total
    currency = "XTR"

    if mangle == "payload":
        payload = "xtr:plan:garbage"
    elif mangle == "owner":
        payer = FakeUser(stranger)
    elif mangle == "amount":
        amount = total - 1
    elif mangle == "currency":
        currency = "USD"
    elif mangle == "decided":
        stars_service.cancel_request(row["id"])

    pcq = FakePreCheckout(payer, payload, amount, currency=currency)
    _run(h.pre_checkout_handler(FakeUpdate(payer, pre_checkout_query=pcq),
                                FakeContext()))
    assert len(pcq.answered) == 1
    ok, err = pcq.answered[0]
    assert ok is False
    assert err is not None and expected_error in err


# ==========================================================
# successful_payment handler
# ==========================================================

def test_successful_payment_grants_plan_and_replies():
    uid = _register(4016)
    row = stars_service.create_plan_invoice_row(uid, "CREATOR", 6)
    total = int(float(row["final_amount"]))
    user = FakeUser(uid)
    msg = FakeMsg(chat_id=uid, text="invoice")
    sp = type("SP", (), {
        "invoice_payload": f"xtr:plan:{row['id']}",
        "from_user": user,
        "total_amount": total,
        "currency": "XTR",
    })()
    upd = FakeUpdate(user, message=msg, successful_payment=sp)
    _run(h.successful_payment_handler(upd, FakeContext()))
    assert stars_service.get_request(row["id"])["status"] == "SUCCESS"
    assert _plan_expiry(uid)[0] == "CREATOR"
    assert msg.replies and "welcome" in msg.replies[0].text.lower()
    assert "CREATOR" in msg.replies[0].text


def test_successful_payment_expired_gives_refund_copy():
    uid = _register(4017)
    row = stars_service.create_plan_invoice_row(uid, "PRO", 1)
    conn = get_connection()
    conn.cursor().execute(
        "UPDATE payment_requests SET expires_at=? WHERE id=?",
        ("2020-01-01T00:00:00+00:00", row["id"]),
    )
    conn.commit()
    conn.close()
    user = FakeUser(uid)
    msg = FakeMsg(chat_id=uid, text="invoice")
    sp = type("SP", (), {
        "invoice_payload": f"xtr:plan:{row['id']}",
        "from_user": user,
        "total_amount": int(float(row["final_amount"])),
        "currency": "XTR",
    })()
    _run(h.successful_payment_handler(FakeUpdate(user, message=msg, successful_payment=sp),
                                      FakeContext()))
    assert stars_service.get_request(row["id"])["status"] == "EXPIRED"
    joined = " ".join(m.text for m in msg.replies)
    assert "refund" in joined.lower()
    assert _plan_expiry(uid)[0] == "FREE"


def test_successful_payment_replay_is_noop():
    uid = _register(4018)
    row = stars_service.create_plan_invoice_row(uid, "BEGINNER", 3)
    total = int(float(row["final_amount"]))
    user = FakeUser(uid)

    async def fire():
        msg = FakeMsg(chat_id=uid, text="invoice")
        sp = type("SP", (), {
            "invoice_payload": f"xtr:plan:{row['id']}",
            "from_user": user, "total_amount": total, "currency": "XTR",
        })()
        await h.successful_payment_handler(
            FakeUpdate(user, message=msg, successful_payment=sp), FakeContext())
        return msg

    msg1 = _run(fire())
    assert "welcome" in msg1.replies[0].text.lower()
    expiry_after_first = _plan_expiry(uid)[1]
    msg2 = _run(fire())  # Telegram redelivery / restart replay
    joined = " ".join(m.text for m in msg2.replies)
    assert "couldn't process" in joined or "refund" in joined.lower()
    assert _plan_expiry(uid)[1] == expiry_after_first  # not extended twice


def test_successful_payment_bad_payload_no_crash():
    uid = _register(4019)
    user = FakeUser(uid)
    msg = FakeMsg(chat_id=uid, text="invoice")
    sp = type("SP", (), {
        "invoice_payload": "something:else:entirely",
        "from_user": user, "total_amount": 99, "currency": "XTR",
    })()
    _run(h.successful_payment_handler(FakeUpdate(user, message=msg, successful_payment=sp),
                                      FakeContext()))
    assert msg.replies and "refund" in msg.replies[0].text.lower()
    # no plan was granted (register_user alone never starts a trial)
    assert plan_service.get_user_plan(uid) == "FREE"
