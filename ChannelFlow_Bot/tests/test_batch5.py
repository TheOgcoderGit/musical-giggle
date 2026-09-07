"""
Batch 5 tests (PRD 94 UX-NAV-05 / UX-NAV-06).

UX-NAV-05 — consistent progress / success / error / empty states:
  * listsource / listdestination render ONE consolidated tracked screen
    (rows drill into an item card) instead of a stack of per-source
    messages; empty lists render a first-use screen, not an error.
  * sourceitem / destitem drill-down callbacks are wired and own the
    item card (toggle stays in place, Back returns to the list).
  * deletesource / deletedestination turn the confirm screen into the
    refreshed list with a removal notice (task-delete pattern).
  * testdestination / testproject send ONE progress message that is
    edited into the result card (no orphan "sending..." text).
  * data-entry prompts (whitelist/blacklist) no longer wear a ✅/🚫
    success glyph.

UX-NAV-06 — first-use onboarding hints:
  * getting-started hint on an unconfigured task detail (auto-hidden
    once a source or destination exists)
  * 💡 hint copy + guide chip on empty source/destination screens
  * empty-ticket, zero-referral, and idle-stats hint lines

Live Telegram E2E stays BLOCKED in the sandbox; handlers are exercised
through the real code with duck-typed telegram objects.
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.conftest_shared import reset_db  # noqa: E402

import pytest  # noqa: E402

# ---- environment imports (must happen after conftest_shared) ----------
from database import models as db_models  # noqa: E402
from services import plan_service  # noqa: E402
from services import stats_service  # noqa: E402

import bot.handlers as h  # noqa: E402
import bot.nav_state as nav_state  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    reset_db()
    nav_state.clear_screens(-1)
    h._TASK_PAGE.clear()
    h._TASK_TERM.clear()
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


_ID = [7000]


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
        self.reply_markup = None

    async def reply_text(self, text, reply_markup=None, **kwargs):
        m = FakeMsg(chat_id=self.chat_id, text=text)
        m.reply_markup = reply_markup
        self.replies.append(m)
        return m

    async def edit_text(self, text, reply_markup=None, **kwargs):
        self.edits.append((self.text, text))  # (before, after) snapshot
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
        self.from_user = user or FakeUser(7001)

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
    def __init__(self, user, message=None, callback_query=None):
        self.effective_user = user
        self.message = message
        self.effective_message = message
        self.callback_query = callback_query


class FakeContext:
    def __init__(self):
        self.args = []
        self.bot_data = {}
        self.bot = type("B", (), {"username": "channelflow_bot"})()


def _run(coro):
    return asyncio.run(coro)


def _flat_callbacks(markup):
    out = []
    for row in markup.inline_keyboard:
        for btn in row:
            if btn.callback_data:
                out.append(btn.callback_data)
    return out


def _register(uid, name="User"):
    db_models.register_user(uid, f"u{uid}", name)
    return uid


def _future_expiry(days=30):
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def _admin(uid):
    h.ADMIN_IDS = {uid}


def _tap(user, data, message, ctx=None):
    nq = FakeQuery(message, user=user)
    nq.data = data
    _run(h.button_handler(FakeUpdate(user, message, callback_query=nq),
                          ctx or FakeContext()))
    return nq


def _creator(uid):
    _register(uid, "Creator")
    _admin(uid)
    plan_service.set_user_plan(uid, "CREATOR", expiry=_future_expiry())
    return h.create_project(uid, "Deals Hub", platform_type="telegram")


def _add_sources(pid, n=3):
    for i in range(n):
        h.add_source(pid, -1000 - i, f"src{i}", f"Source {i}", "channel")


def _add_destinations(pid, n=2):
    for i in range(n):
        h.add_destination(pid, -2000 - i, f"dst{i}", f"Dest {i}", "channel")


def _source_ids(pid):
    return sorted(r["id"] for r in h.get_sources(pid))


def _source_id(pid, title):
    return next(r["id"] for r in h.get_sources(pid) if r["title"] == title)


def _dest_ids(pid):
    return sorted(r["id"] for r in h.get_destinations(pid))


def _dest_id(pid, title):
    return next(r["id"] for r in h.get_destinations(pid) if r["title"] == title)


# ==========================================================
# UX-NAV-05 — consolidated source/destination list screens
# ==========================================================

def test_listsource_renders_one_consolidated_screen():
    uid = _register(7002)
    _admin(uid)
    pid = _creator(uid)
    _add_sources(pid, 3)
    screen = FakeMsg(chat_id=3, text="x")

    _tap(FakeUser(uid), f"listsource:{pid}", screen)
    # one screen edit, no per-source message spam
    assert screen.replies == []
    assert "📥 Sources — Deals Hub" in screen.text
    assert "1." in screen.text and "2." in screen.text and "3." in screen.text
    cbs = _flat_callbacks(screen.reply_markup)
    ids = _source_ids(pid)
    assert len(ids) == 3
    assert all(f"sourceitem:{sid}:{pid}" in cbs for sid in ids)
    assert f"source:{pid}" in cbs                  # ➕ Add Source
    assert f"projcard:{pid}" in cbs and "nav:home" in cbs


def test_listsource_empty_state_is_a_first_use_screen():
    uid = _register(7003)
    _admin(uid)
    pid = _creator(uid)
    screen = FakeMsg(chat_id=3, text="x")

    _tap(FakeUser(uid), f"listsource:{pid}", screen)
    assert "No sources yet" in screen.text
    assert "💡" in screen.text
    cbs = _flat_callbacks(screen.reply_markup)
    assert f"source:{pid}" in cbs
    assert "acct:guide" in cbs   # guide chip
    assert f"projcard:{pid}" in cbs and "nav:home" in cbs


def test_sourceitem_drilldown_toggle_and_back():
    uid = _register(7004)
    _admin(uid)
    pid = _creator(uid)
    _add_sources(pid, 2)
    sid = _source_id(pid, "Source 0")
    screen = FakeMsg(chat_id=3, text="x")

    # list -> item card
    _tap(FakeUser(uid), f"listsource:{pid}", screen)
    _tap(FakeUser(uid), f"sourceitem:{sid}:{pid}", screen)
    assert screen.text.startswith("📥 Source")
    assert "Source 0" in screen.text
    cbs = _flat_callbacks(screen.reply_markup)
    assert f"togglesource:{sid}:{pid}" in cbs
    assert f"deletesourceconfirm:{sid}:{pid}" in cbs
    assert f"listsource:{pid}" in cbs  # Back to list

    # toggle in place: enabled -> disabled label
    _tap(FakeUser(uid), f"togglesource:{sid}:{pid}", screen)
    assert "🔴 Disabled" in screen.text

    # back to the consolidated list
    _tap(FakeUser(uid), f"listsource:{pid}", screen)
    assert "📥 Sources — Deals Hub" in screen.text


def test_delete_source_confirm_refreshes_into_remaining_list_then_empty():
    uid = _register(7005)
    _admin(uid)
    pid = _creator(uid)
    _add_sources(pid, 2)
    sid0 = _source_id(pid, "Source 0")
    sid1 = _source_id(pid, "Source 1")
    screen = FakeMsg(chat_id=3, text="x")

    # confirm screen
    _tap(FakeUser(uid), f"deletesourceconfirm:{sid0}:{pid}", screen)
    assert "Disconnect this source" in screen.text

    # confirm delete -> refreshed list with notice (1 source left)
    _tap(FakeUser(uid), f"deletesource:{sid0}", screen)
    assert "✅ Source removed: Source 0" in screen.text
    assert "📥 Sources — Deals Hub" in screen.text
    assert "1. 🟢 Source 1" in screen.text and "2." not in screen.text

    # delete the last one -> empty first-use screen with hint
    _tap(FakeUser(uid), f"deletesourceconfirm:{sid1}:{pid}", screen)
    _tap(FakeUser(uid), f"deletesource:{sid1}", screen)
    assert "No sources yet" in screen.text
    assert "acct:guide" in _flat_callbacks(screen.reply_markup)
    conn = h.get_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM sources WHERE project_id=?", (pid,))
    assert cur.fetchone()["c"] == 0
    conn.close()


def test_listdestination_drilldown_and_delete():
    uid = _register(7006)
    _admin(uid)
    pid = _creator(uid)
    _add_destinations(pid, 2)
    dst1 = _dest_id(pid, "Dest 1")

    screen = FakeMsg(chat_id=3, text="x")
    user = FakeUser(uid)
    _tap(user, f"listdestination:{pid}", screen)
    assert "📤 Destinations — Deals Hub" in screen.text
    cbs = _flat_callbacks(screen.reply_markup)
    assert all(f"destitem:{did}:{pid}" in cbs for did in _dest_ids(pid))
    assert f"destination:{pid}" in cbs

    _tap(user, f"destitem:{dst1}:{pid}", screen)
    assert screen.text.startswith("📤 Destination")
    assert "Dest 1" in screen.text
    assert f"toggledestination:{dst1}:{pid}" in _flat_callbacks(screen.reply_markup)
    assert f"testdestination:{dst1}:{pid}" in _flat_callbacks(screen.reply_markup)

    # delete from its confirm screen -> list refresh notice
    _tap(user, f"deletedestinationconfirm:{dst1}:{pid}", screen)
    _tap(user, f"deletedestination:{dst1}", screen)
    assert "✅ Destination removed: Dest 1" in screen.text
    assert "1. 🟢 Dest 0" in screen.text and "2." not in screen.text


def test_destination_empty_state_guides_to_add():
    uid = _register(7007)
    _admin(uid)
    pid = _creator(uid)
    screen = FakeMsg(chat_id=3, text="x")
    _tap(FakeUser(uid), f"listdestination:{pid}", screen)
    assert "No destinations yet" in screen.text
    assert "💡 Destinations are where your task forwards TO" in screen.text


# ==========================================================
# UX-NAV-05 — progress -> result messages
# ==========================================================

def test_testdestination_progress_message_edited_to_result(monkeypatch):
    async def fake_send(chat_id, project_name):
        return (True, "delivered")

    monkeypatch.setattr(h, "send_test_message", fake_send)
    uid = _register(7008)
    _admin(uid)
    pid = _creator(uid)
    _add_destinations(pid, 1)
    screen = FakeMsg(chat_id=3, text="x")
    user = FakeUser(uid)

    did = _dest_id(pid, "Dest 0")
    _tap(user, f"testdestination:{did}:{pid}", screen)
    # exactly one progress message was sent...
    assert len(screen.replies) == 1
    progress = screen.replies[0]
    # ...and that same message was edited into the result card
    assert progress.edits, "progress message was not replaced by the result"
    before, after = progress.edits[0]
    assert "🧪 Sending a real test message" in before
    assert "✅ Test Passed" in after
    assert "Dest 0" in progress.text
    cbs = _flat_callbacks(progress.reply_markup)
    assert f"listdestination:{pid}" in cbs and "nav:home" in cbs


def test_testdestination_failure_result(monkeypatch):
    async def fake_send(chat_id, project_name):
        return (False, "peer not found")

    monkeypatch.setattr(h, "send_test_message", fake_send)
    uid = _register(7009)
    _admin(uid)
    pid = _creator(uid)
    _add_destinations(pid, 1)
    screen = FakeMsg(chat_id=3, text="x")
    did = _dest_id(pid, "Dest 0")
    _tap(FakeUser(uid), f"testdestination:{did}:{pid}", screen)
    progress = screen.replies[0]
    before, after = progress.edits[0]
    assert "Sending a real test message" in before
    assert "❌ Test Failed" in after
    assert "peer not found" in progress.text


def test_testproject_progress_edited_to_mixed_results(monkeypatch):
    async def fake_send(chat_id, project_name):
        # chat_id may be stored as int or text in the row
        return (True, "ok") if str(chat_id) == "-2000" else (False, "rejected")

    monkeypatch.setattr(h, "send_test_message", fake_send)
    uid = _register(7010)
    _admin(uid)
    pid = _creator(uid)
    _add_destinations(pid, 2)
    screen = FakeMsg(chat_id=3, text="x")
    user = FakeUser(uid)

    _tap(user, f"testproject:{pid}", screen)
    assert len(screen.replies) == 1
    progress = screen.replies[0]
    assert progress.edits
    before, after = progress.edits[0]
    assert "🧪 Testing 2 destination(s)" in before
    assert "✅ Dest 0" in after and "❌ Dest 1" in after
    cbs = _flat_callbacks(progress.reply_markup)
    assert f"projcard:{pid}" in cbs and "nav:home" in cbs


# ==========================================================
# UX-NAV-05 — data-entry prompt copy
# ==========================================================

def test_data_entry_prompts_are_not_success_glyphs():
    uid = _register(7011)
    _admin(uid)
    pid = _creator(uid)
    screen = FakeMsg(chat_id=3, text="x")
    user = FakeUser(uid)

    _tap(user, f"setwhitelist:{pid}", screen)
    assert uid in h.WAITING_WHITELIST
    assert screen.replies and screen.replies[-1].text.startswith("📝 Whitelist Keywords")
    assert "✅" not in screen.replies[-1].text

    _tap(user, f"setblacklist:{pid}", screen)
    assert uid in h.WAITING_BLACKLIST
    assert screen.replies and screen.replies[-1].text.startswith("📝 Blacklist Keywords")


# ==========================================================
# UX-NAV-06 — first-use hints
# ==========================================================

def test_task_detail_hint_only_when_fully_unconfigured():
    uid = _register(7012)
    _admin(uid)
    pid = _creator(uid)
    msg = FakeMsg(chat_id=3, text="x")

    _run(h._show_task_detail(msg, pid, uid, edit=False))
    hint = msg.replies[0]
    assert "💡 Getting started" in hint.text

    # one source added -> hint disappears (no longer first-use)
    _add_sources(pid, 1)
    msg2 = FakeMsg(chat_id=3, text="x")
    _run(h._show_task_detail(msg2, pid, uid, edit=False))
    assert "💡 Getting started" not in msg2.replies[0].text


def test_tickets_empty_state_has_hint():
    uid = _register(7013)
    msg = FakeMsg(chat_id=3, text="x")
    _run(h._render_tickets_list(msg, uid, edit=True))
    assert "No tickets yet" in msg.text
    assert "💡" in msg.text


def test_rewards_empty_state_has_share_hint():
    uid = _register(7014)
    msg = FakeMsg(chat_id=3, text="x")
    _run(h._show_rewards(msg, FakeContext(), uid, edit=True))
    assert "💡 Share your link" in msg.text


def test_stats_idle_state_has_hint(monkeypatch):
    uid = _register(7015)
    _admin(uid)
    pid = _creator(uid)
    monkeypatch.setattr(stats_service, "get_stats",
                        lambda pid: {"forwarded": 0, "failed": 0,
                                     "retried": 0, "filtered": 0,
                                     "last_forward_at": None})
    screen = FakeMsg(chat_id=3, text="x")
    _tap(FakeUser(uid), f"stats:{pid}", screen)
    assert "📊 Stats" in screen.text
    assert "No activity yet" in screen.text

    # active stats hide the hint
    monkeypatch.setattr(stats_service, "get_stats",
                        lambda pid: {"forwarded": 5, "failed": 1,
                                     "retried": 0, "filtered": 2,
                                     "last_forward_at": "now"})
    screen2 = FakeMsg(chat_id=3, text="x")
    _tap(FakeUser(uid), f"stats:{pid}", screen2)
    assert "No activity yet" not in screen2.text


# ==========================================================
# Integrity: every callback on the new screens is routed
# ==========================================================

def test_new_screens_emit_only_handled_prefixes():
    uid = _register(7016)
    _admin(uid)
    pid = _creator(uid)
    _add_sources(pid, 2)
    _add_destinations(pid, 1)
    handled = {
        "nav", "projcard", "acct", "source", "sourceitem", "togglesource",
        "deletesourceconfirm", "deletesource", "destination", "destitem",
        "toggledestination", "deletedestinationconfirm", "deletedestination",
        "testdestination", "listdestination", "listsource",
    }
    user = FakeUser(uid)
    screen = FakeMsg(chat_id=3, text="x")

    _tap(user, f"listsource:{pid}", screen)
    _tap(user, f"sourceitem:{-1000}:{pid}", screen)
    _tap(user, f"listsource:{pid}", screen)
    _tap(user, f"listdestination:{pid}", screen)
    _tap(user, f"destitem:{-2000}:{pid}", screen)
    for markup in (screen.reply_markup,) + tuple(
            m.reply_markup for m in screen.replies if m.reply_markup):
        for cb in _flat_callbacks(markup):
            prefix = cb.split(":", 1)[0]
            assert prefix in handled, f"unhandled prefix {prefix!r} from {cb!r}"
