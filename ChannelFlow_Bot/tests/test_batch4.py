"""
Batch 4 tests (PRD 23 / 24 / 25.2 / 26 + UX-NAV-03/04 + housekeeping).

F1  Extra Forward Credits: atomic idempotent ledger, consumption after
    the daily allowance, admin grant/revoke with audit identity.
F2  Configurable credit packages + purchase through every method:
    Stars (payload xtr:pkg:) resolves to a credit grant exactly once;
    UPI/crypto requests flow through admin approval and grant on
    approve; double-approval never double-grants.
F3  Payment history: user screen lists their own rows across every
    method/purpose; admin history + review screens use one label.
F4  UX-NAV-03: task-list pagination + search (page math, per-user
    state, clear, delete-clamp).
F5  UX-NAV-04: plan-locked gates render an Upgrade CTA screen instead
    of a bare toast (new-task, add-source, add-destination, text
    paths), and allowed users flow through untouched.
Housekeeping: no whatsapp_pairing_codes sample seeding; importing
core.listener/bot.handlers creates no ChannelFlow.session file.

Live Telegram E2E stays BLOCKED in the sandbox; every test exercises
the real handlers/services with duck-typed telegram objects.
"""
import asyncio
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.conftest_shared import reset_db  # noqa: E402

import pytest  # noqa: E402

# ---- environment imports (must happen after conftest_shared) ----------
from database import models as db_models  # noqa: E402
from database.db import get_connection  # noqa: E402
from services import extra_credits_service as ec  # noqa: E402
from services import plan_service, payment_service  # noqa: E402
from services import stars_service  # noqa: E402

import bot.handlers as h  # noqa: E402
import bot.keyboards as kb  # noqa: E402
import bot.nav_state as nav_state  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    reset_db()
    nav_state.clear_screens(-1)
    h._TASK_PAGE.clear()
    h._TASK_TERM.clear()
    for uid in list(h.WAITING_TASK_SEARCH):
        h.WAITING_TASK_SEARCH.pop(uid, None)
    yield
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove("ChannelFlow.session" + suffix)
        except FileNotFoundError:
            pass


# ==========================================================
# Fakes (duck-typed telegram objects, no network)
# ==========================================================

class _Chat:
    def __init__(self, chat_id):
        self.id = chat_id
        self.deleted = []

    async def delete_message(self, message_id):
        self.deleted.append(message_id)


_ID = [5000]


class FakeMsg:
    def __init__(self, chat_id=7, text="", message_id=None, is_bot=True):
        self.chat_id = chat_id
        self.chat = _Chat(chat_id)
        self.text = text
        self.is_bot = is_bot
        self.message_id = message_id if message_id is not None else _ID[0]
        _ID[0] += 1
        self.replies = []
        self.edits = []
        self.successful_payment = None
        self.reply_markup = None

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
        self.deleted.append(self.message_id)


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
    def __init__(self, user, message=None, callback_query=None,
                 successful_payment=None):
        self.effective_user = user
        self.message = message
        self.effective_message = message
        self.callback_query = callback_query
        self.pre_checkout_query = None
        if successful_payment is not None:
            message.successful_payment = successful_payment


class FakeBot:
    def __init__(self):
        self.invoices = []

    async def send_message(self, chat_id, text, **kwargs):
        return FakeMsg(chat_id=chat_id, text=text)

    async def send_invoice(self, **kwargs):
        self.invoices.append(dict(kwargs))
        return FakeMsg(chat_id=kwargs.get("chat_id", 0), text="invoice")


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


def _buttons(markup):
    """[(text, callback_data)] for every inline button."""
    return [(b.text, b.callback_data) for row in markup.inline_keyboard
            for b in row if b.callback_data]


def _register(uid, name="User"):
    db_models.register_user(uid, f"u{uid}", name)
    return uid


def _future_expiry(days=30):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def _admin(uid):
    h.ADMIN_IDS = {uid}


def _tap(user, data, message, ctx=None):
    ctx = ctx or FakeContext()
    nq = FakeQuery(message, user=user)
    nq.data = data
    _run(h.button_handler(FakeUpdate(user, message, callback_query=nq), ctx))
    return nq


# ==========================================================
# F1 - ledger core
# ==========================================================

def test_apply_consume_refund_roundtrip_and_idempotency():
    uid = _register(6001)
    # grant via purchase event (reference = idempotency identifier)
    assert ec.apply(uid, 100, "purchase", "purchase:req:1",
                    reason="test") is True
    assert ec.get_balance(uid) == 100
    # replay of the same event is a no-op
    assert ec.apply(uid, 100, "purchase", "purchase:req:1") is False
    assert ec.get_balance(uid) == 100

    # consume
    assert ec.consume(uid, "fwd:1:2:3") is True
    assert ec.get_balance(uid) == 99
    assert ec.consume(uid, "fwd:1:2:3") is False  # duplicate reference
    assert ec.get_balance(uid) == 99

    # refund after a failed forward
    assert ec.refund(uid, "fwd:1:2:3") is True
    assert ec.get_balance(uid) == 100
    assert ec.refund(uid, "fwd:1:2:3") is False  # refund idempotent
    assert ec.get_balance(uid) == 100

    ledger = ec.recent_ledger(uid)
    assert len(ledger) == 3
    assert [r["kind"] for r in ledger] == ["refund", "consumption", "purchase"]


def test_consume_never_goes_below_zero():
    uid = _register(6002)
    assert ec.consume(uid, "fwd:empty:1") is False
    assert ec.get_balance(uid) == 0
    ec.admin_adjust(uid, -5, "revoke", admin_id=99)
    assert ec.get_balance(uid) == 0
    ec.apply(uid, 3, "grant", "g1")
    assert ec.consume(uid, "c1") and ec.consume(uid, "c2") and ec.consume(uid, "c3")
    assert ec.consume(uid, "c4") is False
    assert ec.get_balance(uid) == 0


def test_concurrent_consumption_is_atomic():
    """5 credits, 30 concurrent consumers -> exactly 5 succeed."""
    uid = _register(6003)
    ec.apply(uid, 5, "grant", "seed")
    results = []

    def worker(i):
        ok = ec.consume(uid, f"conc:{i}")
        results.append(ok)

    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(worker, range(30)))
    assert sum(1 for r in results if r) == 5
    assert ec.get_balance(uid) == 0


def test_admin_adjust_records_identity_and_reason():
    uid = _register(6004)
    assert ec.admin_adjust(uid, 250, "compensation for outage", admin_id=9001) is True
    assert ec.admin_adjust(uid, -50, "mistaken grant", admin_id=9001) is True
    ledger = ec.recent_ledger(uid)
    assert all(r["admin_id"] == 9001 for r in ledger)
    assert ledger[0]["reason"] == "mistaken grant"
    assert ledger[1]["reason"] == "compensation for outage"
    assert ec.get_balance(uid) == 200


def test_refund_without_prior_consume_is_still_unique():
    uid = _register(6005)
    assert ec.refund(uid, "never-consumed") is True
    assert ec.refund(uid, "never-consumed") is False  # duplicate reference
    assert ec.get_balance(uid) == 1


# ==========================================================
# F2 - packages + purchases
# ==========================================================

def test_package_seeds_and_listing():
    pkgs = ec.list_packages()
    assert [p["forwards_amount"] for p in pkgs] == [100, 500, 1000, 5000, 10000]
    assert all(p["stars_price"] > 0 for p in pkgs)
    assert all(p["price_inr"] == 0 and p["price_usd"] == 0 for p in pkgs)
    # price line shows the configured books
    assert "⭐15" in ec.package_price_line(pkgs[0])
    # stars-only availability by default (INR/USD unset -> hidden)
    assert ec.available_methods_for(pkgs[0]) == ["stars"]


def test_offline_request_snapshot_and_activation(monkeypatch):
    uid = _register(6006)
    conn = get_connection()
    conn.cursor().execute(
        "UPDATE credit_packages SET price_inr=99, price_usd=2.99 WHERE forwards_amount=500")
    conn.commit()
    conn.close()
    monkeypatch.setattr(payment_service, "is_upi_configured", lambda: True)
    monkeypatch.setattr(payment_service, "is_oxapay_configured", lambda: True)

    pkg = next(p for p in ec.list_packages() if p["forwards_amount"] == 500)
    row = ec.create_offline_package_request(uid, pkg["id"], "upi",
                                            payment_reference="upi@bank")
    assert row["purpose"] == "extra_credit"
    assert row["method"] == "upi" and row["currency"] == "INR"
    assert row["status"] == "PENDING_PAYMENT"
    assert row["package_id"] == pkg["id"] and row["extra_forwards"] == 500
    assert float(row["final_amount"]) == 99

    # screenshot -> SUBMITTED -> admin approval grants exactly once
    assert payment_service.submit_screenshot(row["id"], "file-x") is True
    approved = payment_service.approve_payment(row["id"], admin_id=77)
    assert approved["status"] == "APPROVED"
    assert ec.get_balance(uid) == 500
    # double approval is refused by the status guard
    assert payment_service.approve_payment(row["id"], admin_id=77) is None
    assert ec.get_balance(uid) == 500

    # crypto row + provider-verified approval
    uid2 = _register(6007)
    row2 = ec.create_offline_package_request(uid2, pkg["id"], "crypto",
                                             payment_reference="https://pay")
    assert row2["currency"] == "USD" and float(row2["final_amount"]) == 2.99
    ret = payment_service.approve_crypto_payment(row2["id"])
    assert ret is not None and ec.get_balance(uid2) == 500


def test_package_request_validation():
    uid = _register(6008)
    pkg = ec.list_packages()[0]
    with pytest.raises(ValueError):
        ec.create_offline_package_request(uid, pkg["id"], "upi")  # INR unset
    with pytest.raises(ValueError):
        ec.create_offline_package_request(uid, 999999, "stars")
    with pytest.raises(ValueError):
        ec.create_stars_package_request(uid, 999999)


def test_stars_package_resolves_credits_exactly_once():
    uid = _register(6009)
    pkg = next(p for p in ec.list_packages() if p["forwards_amount"] == 1000)
    row = ec.create_stars_package_request(uid, pkg["id"])
    assert row["purpose"] == "extra_credit"
    assert row["method"] == "stars" and row["currency"] == "STARS"
    assert int(float(row["final_amount"])) == 120  # seeded ⭐120

    after, reason = stars_service.resolve_stars_success(
        row["id"], uid, int(float(row["final_amount"])))
    assert reason == "ok" and after["status"] == "SUCCESS"
    assert ec.get_balance(uid) == 1000
    # replay -> no double grant
    _, reason2 = stars_service.resolve_stars_success(
        row["id"], uid, int(float(row["final_amount"])))
    assert reason2 == "already_decided"
    assert ec.get_balance(uid) == 1000
    # amount mismatch on a fresh row refuses
    row2 = ec.create_stars_package_request(uid, pkg["id"])
    _, reason3 = stars_service.resolve_stars_success(row2["id"], uid, 1)
    assert reason3 == "amount_mismatch"
    assert ec.get_balance(uid) == 1000


def test_credits_hub_and_stars_purchase_ui_chain():
    uid = _register(6010)
    user = FakeUser(uid)
    _admin(uid)
    screen = FakeMsg(chat_id=9, text="x")

    # hub shows balance, empty-ledger hint, buyable package rows + nav
    _tap(user, "credits:home", screen)
    assert "Extra Credits" in screen.text and "Balance: ⚡ 0" in screen.text
    cbs = _flat_callbacks(screen.reply_markup)
    assert any(c.startswith("credits:buy:") for c in cbs)
    assert "nav:account" in cbs and "nav:home" in cbs

    # package picker lists only configured methods (stars default)
    pkg = ec.list_packages()[0]
    _tap(user, f"credits:buy:{pkg['id']}", screen)
    btns = _buttons(screen.reply_markup)
    assert any("Pay 15 Stars (instant)" in t for t, _c in btns)
    assert any(c == f"credits:pay:{pkg['id']}:stars" for _t, c in btns)
    assert all("UPI" not in t and "Crypto" not in t for t, _c in btns)

    # checkout fires a real XTR invoice through the fake bot
    ctx = FakeContext()
    _tap(user, f"credits:pay:{pkg['id']}:stars", screen, ctx)
    assert len(ctx.bot.invoices) == 1
    inv = ctx.bot.invoices[0]
    assert inv["payload"].startswith("xtr:pkg:")
    assert inv["currency"] == "XTR" and inv["prices"][0].amount == 15
    request_id = int(inv["payload"].split(":")[2])
    row = stars_service.get_request(request_id)
    assert row["purpose"] == "extra_credit" and row["extra_forwards"] == 100
    assert any("Payment sheet sent" in m.text for m in screen.replies)

    # Telegram payment-success receipt is credit-flavoured + grants once
    total = int(float(row["final_amount"]))
    user = FakeUser(uid)
    msg = FakeMsg(chat_id=uid, text="invoice")
    sp = type("SP", (), {
        "invoice_payload": f"xtr:pkg:{request_id}",
        "from_user": user, "total_amount": total, "currency": "XTR",
    })()
    _run(h.successful_payment_handler(
        FakeUpdate(user, message=msg, successful_payment=sp), FakeContext()))
    assert stars_service.get_request(request_id)["status"] == "SUCCESS"
    assert any("Extra Credits added" in m.text for m in msg.replies)
    assert ec.get_balance(uid) == 100


def test_upi_package_ui_chain(monkeypatch):
    uid = _register(6011)
    user = FakeUser(uid)
    _admin(uid)
    monkeypatch.setattr(payment_service, "is_upi_configured", lambda: True)
    conn = get_connection()
    conn.cursor().execute(
        "UPDATE credit_packages SET price_inr=49 WHERE forwards_amount=100")
    conn.commit()
    conn.close()

    pkg = ec.list_packages()[0]
    screen = FakeMsg(chat_id=9, text="x")
    _tap(user, f"credits:buy:{pkg['id']}", screen)
    btns = _buttons(screen.reply_markup)
    assert any("UPI ₹49" in t for t, _c in btns)
    assert any(c == f"credits:pay:{pkg['id']}:upi" for _t, c in btns)
    _tap(user, f"credits:pay:{pkg['id']}:upi", screen)
    joined = " ".join(m.text for m in screen.replies)
    assert "Extra Credits via UPI" in joined and "Verify" in joined
    buttons = _flat_callbacks(screen.replies[0].reply_markup)
    assert any(b.startswith("upgrade:verify:") for b in buttons)
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM payment_requests WHERE user_id=? AND purpose='extra_credit'",
                (uid,))
    req = cur.fetchone()
    conn.close()
    assert req is not None and req["method"] == "upi"
    assert req["status"] == "PENDING_PAYMENT"


# ==========================================================
# F3 - payment history
# ==========================================================

def _seed_mixed_rows(uid, other_uid=None):
    payment_service.create_payment_request(uid, "BEGINNER", 1, "upi",
                                           payment_reference="r-upi")
    stars_service.create_plan_invoice_row(uid, "PRO", 3)
    ec.create_stars_package_request(uid, ec.list_packages()[0]["id"])
    if other_uid:
        payment_service.create_payment_request(other_uid, "CREATOR", 12, "upi",
                                               payment_reference="r-other")


def test_user_payment_history_screen_lists_own_rows_only():
    uid1, uid2 = _register(6012), _register(6013)
    _seed_mixed_rows(uid1, other_uid=uid2)
    msg = FakeMsg(chat_id=9, text="x")
    _run(h._send_payment_history(msg, uid1))
    assert msg.replies and "Payment History" in msg.replies[0].text
    text = msg.replies[0].text
    assert "BEGINNER" in text and "PRO" in text and "⭐" in text
    assert "Extra Credits" in text and "upi" in text and "stars" in text
    assert "CREATOR" not in text  # other user's row never appears
    cbs = _flat_callbacks(msg.replies[0].reply_markup)
    assert "nav:account" in cbs and "credits:home" in cbs and "nav:home" in cbs


def test_acct_history_callback_route():
    uid = _register(6014)
    user = FakeUser(uid)
    _admin(uid)
    _seed_mixed_rows(uid)
    screen = FakeMsg(chat_id=9, text="x")
    _tap(user, "acct:history", screen)
    assert "Payment History" in screen.text
    assert "nav:account" in _flat_callbacks(screen.reply_markup)


def test_admin_payments_and_history_screens():
    admin = _register(6015)
    _admin(admin)
    payer = _register(6016)
    _seed_mixed_rows(payer)
    # mark one UPI row as screenshot-submitted so it sits in review
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE payment_requests SET status='SUBMITTED' WHERE user_id=? "
                "AND method='upi' LIMIT 1", (payer,))
    # resolve the stars plan row so history shows a SUCCESS entry too
    cur.execute("SELECT id, final_amount FROM payment_requests WHERE user_id=? "
                "AND method='stars' AND purpose='plan' LIMIT 1", (payer,))
    srow = cur.fetchone()
    conn.commit()
    conn.close()
    stars_service.resolve_stars_success(srow["id"], payer,
                                        int(float(srow["final_amount"])))
    user = FakeUser(admin)
    screen = FakeMsg(chat_id=9, text="x")
    _tap(user, "admin:payments", screen)
    assert "Awaiting Review" in screen.text
    assert "upi" in screen.text
    assert "admin:payhistory" in " ".join(_flat_callbacks(screen.reply_markup))

    _tap(user, "admin:payhistory", screen)
    assert "Payment History (latest 25)" in screen.text
    assert "SUCCESS" in screen.text


def test_plan_screen_shows_credits_and_history_rows():
    uid = _register(6017)
    _admin(uid)
    ec.apply(uid, 42, "grant", "gift")
    msg = FakeMsg(chat_id=9, text="x")
    _run(h._send_plan_screen(msg, uid))
    assert msg.replies
    text = msg.replies[0].text
    assert "Extra Credits: 42" in text and "Today:" in text
    cbs = _flat_callbacks(msg.replies[0].reply_markup)
    assert "credits:home" in cbs and "acct:history" in cbs and "acct:upgrade" in cbs


# ==========================================================
# F4 - task list pagination + search
# ==========================================================

def _creator(uid, tasks=12):
    _register(uid, "Creator")
    plan_service.set_user_plan(uid, "CREATOR", expiry=_future_expiry())
    pids = []
    for i in range(1, tasks + 1):
        pids.append(h.create_project(uid, f"Task {i:02d}", platform_type="telegram"))
    return pids


def test_payload_page_math_and_markup():
    """Lists are newest-first (ids 1..12 -> page 1 = 12..5)."""
    uid = _register(6018)
    pids = _creator(uid, tasks=12)
    text, markup, pages = h._task_list_payload(uid)
    assert pages == 2
    assert "page 1/2" in text
    cbs = _flat_callbacks(markup)
    assert f"projcard:{pids[11]}" in cbs and f"projcard:{pids[4]}" in cbs
    assert f"projcard:{pids[3]}" not in cbs and f"projcard:{pids[0]}" not in cbs
    assert "tasks:page:2" in cbs and "tasks:search" in cbs
    assert cbs[-1] == "nav:home"

    h._TASK_PAGE[uid] = 2
    text2, markup2, pages2 = h._task_list_payload(uid)
    assert pages2 == 2 and "page 2/2" in text2
    cbs2 = _flat_callbacks(markup2)
    assert f"projcard:{pids[3]}" in cbs2 and f"projcard:{pids[0]}" in cbs2
    assert f"projcard:{pids[11]}" not in cbs2
    assert "tasks:page:1" in cbs2 and "tasks:page:3" not in cbs2

    # page clamps: page 99 on 2 pages -> 2
    h._TASK_PAGE[uid] = 99
    _, _, pages3 = h._task_list_payload(uid)
    assert pages3 == 2


def test_pagination_buttons_drive_real_handler():
    uid = _register(6019)
    pids = _creator(uid, tasks=12)
    user = FakeUser(uid)
    _admin(uid)
    msg = FakeMsg(chat_id=3, text=kb.MB_PROJECTS)
    _run(h._show_task_list(msg, uid))
    list_msg = msg.replies[0]
    assert "page 1/2" in list_msg.text

    _tap(user, "tasks:page:2", list_msg)
    assert "page 2/2" in list_msg.text
    assert f"projcard:{pids[0]}" in _flat_callbacks(list_msg.reply_markup)

    _tap(user, "tasks:page:1", list_msg)
    assert "page 1/2" in list_msg.text
    assert f"projcard:{pids[11]}" in _flat_callbacks(list_msg.reply_markup)


def test_search_roundtrip_and_clear():
    uid = _register(6020)
    pids = _creator(uid, tasks=12)
    user = FakeUser(uid)
    _admin(uid)
    screen = FakeMsg(chat_id=3, text="x")

    # 🔍 tap -> prompt, waiting flag set
    _tap(user, "tasks:search", screen)
    assert uid in h.WAITING_TASK_SEARCH
    assert "Search tasks" in screen.text

    # typed term -> filtered list ("Task 0" matches Task 01..09, shown
    # newest-first on page 1/2)
    term_msg = FakeMsg(chat_id=3, text="Task 0")
    _run(h.menu_handler(FakeUpdate(user, term_msg), FakeContext()))
    assert uid not in h.WAITING_TASK_SEARCH
    assert h._TASK_TERM.get(uid) == "Task 0"
    filtered = term_msg.replies[0]
    assert "Task 0" in filtered.text and "9 match(es)" in filtered.text
    cbs = _flat_callbacks(filtered.reply_markup)
    assert f"projcard:{pids[8]}" in cbs            # Task 09 matches (page 1)
    assert f"projcard:{pids[0]}" not in cbs        # Task 01 is on page 2
    assert f"projcard:{pids[11]}" not in cbs       # Task 12 does not match
    assert "tasks:clear" in cbs

    # ✖ clear -> full list restored in place
    _tap(user, "tasks:clear", filtered)
    assert h._TASK_TERM.get(uid) is None
    assert "page 1/2" in filtered.text
    assert f"projcard:{pids[11]}" in _flat_callbacks(filtered.reply_markup)

    # no-match search state keeps a Clear escape
    h._TASK_TERM[uid] = "zzz-none"
    no_msg = FakeMsg(chat_id=3, text="x")
    _run(h._show_task_list(no_msg, uid))
    assert no_msg.replies and "No tasks match" in no_msg.replies[0].text
    assert "tasks:clear" in _flat_callbacks(no_msg.replies[0].reply_markup)


def test_delete_on_last_page_clamps_back():
    uid = _register(6021)
    pids = _creator(uid, tasks=9)
    user = FakeUser(uid)
    _admin(uid)
    h._TASK_PAGE[uid] = 2

    msg = FakeMsg(chat_id=3, text=kb.MB_PROJECTS)
    _run(h._show_task_list(msg, uid))
    list_msg = msg.replies[0]
    assert "page 2/2" in list_msg.text

    # open the last task (9th) -> confirm -> delete
    _run(h._show_task_detail(list_msg, pids[8], uid, edit=True))
    _run(h._confirm_delete_task(list_msg, pids[8], uid, edit=True))
    _tap(user, f"delete:{pids[8]}", list_msg)
    assert h.get_project(pids[8]) is None
    cbs = _flat_callbacks(list_msg.reply_markup)
    assert f"projcard:{pids[0]}" in cbs      # page clamped back to 1
    assert f"projcard:{pids[8]}" not in cbs  # deleted task gone


def test_task_list_keyboard_never_exceeds_row_budget():
    """The keyboard renders exactly the slice it is given (the payload
    does the slicing): at most 8 tasks + page row + search + new + home."""
    projects = [{"id": i, "name": f"T{i}", "status": 1} for i in range(1, 30)]
    visible = projects[8:16]  # page 2 slice of a 29-task list
    markup = kb.task_list_keyboard(visible, can_create=True, page=2, pages=4)
    cbs = _flat_callbacks(markup)
    assert len(cbs) == 8 + 3 + 1 + 1 + 1


# ==========================================================
# F5 - locked states with Upgrade CTA
# ==========================================================

def test_newproj_at_cap_renders_locked_screen_then_allows_after_upgrade():
    uid = _register(6022)
    user = FakeUser(uid)
    _admin(uid)
    plan_service.set_user_plan(uid, "FREE", expiry=None)
    h.create_project(uid, "Only One", platform_type="telegram")

    screen = FakeMsg(chat_id=3, text="tasks")
    _tap(user, "newproj", screen)
    assert "Plan limit" in screen.text
    cbs = _flat_callbacks(screen.reply_markup)
    assert "acct:upgrade" in cbs and "nav:home" in cbs
    assert uid not in h.WAITING_PROJECT_NAME  # never prompted at the cap

    # upgrade -> the same tap now flows into name capture
    plan_service.set_user_plan(uid, "CREATOR", expiry=_future_expiry())
    _tap(user, "newproj", screen)
    assert uid in h.WAITING_PROJECT_NAME
    assert "Send Task Name" in screen.text


def test_add_source_and_destination_taps_gate_at_cap():
    uid = _register(6023)
    user = FakeUser(uid)
    _admin(uid)
    limits = plan_service.get_entitlements(uid)
    pid = h.create_project(uid, "Capped", platform_type="telegram")
    for i in range(limits["max_sources_per_project"]):
        h.add_source(pid, -1000 - i, f"src{i}", f"Src {i}", "channel")
    for i in range(limits["max_destinations_per_project"]):
        h.add_destination(pid, -2000 - i, f"dst{i}", f"Dst {i}", "channel")

    screen = FakeMsg(chat_id=3, text="x")
    _tap(user, f"source:{pid}", screen)
    assert "Plan limit" in screen.text
    assert uid not in h.WAITING_SOURCE
    _tap(user, f"destination:{pid}", screen)
    assert "Plan limit" in screen.text
    assert uid not in h.WAITING_DESTINATION


def test_text_gates_reply_with_cta():
    uid = _register(6024)
    _admin(uid)
    pid = h.create_project(uid, "CapOne", platform_type="telegram")
    limits = plan_service.get_entitlements(uid)
    for i in range(limits["max_sources_per_project"]):
        h.add_source(pid, -3000 - i, f"s{i}", f"S{i}", "channel")

    h.CURRENT_PROJECT[uid] = pid
    h.WAITING_SOURCE[uid] = True
    msg = FakeMsg(chat_id=3, text="@anotherchannel")
    _run(h._handle_add_source(msg, uid, "@anotherchannel"))
    assert msg.replies and "Plan limit" in msg.replies[0].text
    cbs = _flat_callbacks(msg.replies[0].reply_markup)
    assert "acct:upgrade" in cbs and "nav:home" in cbs
    assert uid not in h.WAITING_SOURCE


def test_under_limit_user_flows_through_unchanged():
    uid = _register(6025)
    _admin(uid)
    pid = h.create_project(uid, "Room", platform_type="telegram")
    user = FakeUser(uid)
    screen = FakeMsg(chat_id=3, text="x")
    _tap(user, f"source:{pid}", screen)
    assert "Plan limit" not in screen.text
    assert uid in h.WAITING_SOURCE  # prompt proceeded


# ==========================================================
# Housekeeping d/f
# ==========================================================

def test_no_sample_pairing_codes_seeded():
    """Bug d: creating the table (even with users present) never seeds
    sample codes; codes exist only when the pairing flow creates them."""
    _register(6026)
    conn = get_connection()
    conn.execute("DROP TABLE IF EXISTS whatsapp_pairing_codes")
    conn.commit()
    conn.close()
    from database.db import init_db
    init_db()  # table recreated; users table already has 6026
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM whatsapp_pairing_codes")
    assert cur.fetchone()["c"] == 0
    conn.close()


def test_imports_do_not_create_session_file():
    """Bug f: importing the full app chain (core.listener, handlers)
    must not construct the Telethon client / create ChannelFlow.session."""
    project = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ)
    script = (
        "import core.processing_listener, core.listener, bot.handlers, os; "
        "print('EXISTS' if os.path.exists('ChannelFlow.session') else 'CLEAN')"
    )
    out = subprocess.run(
        [sys.executable, "-c", script], cwd=project, env=env,
        capture_output=True, text=True, timeout=120,
    )
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(os.path.join(project, "ChannelFlow.session" + suffix))
        except FileNotFoundError:
            pass
    assert out.returncode == 0, out.stderr
    assert "CLEAN" in out.stdout
