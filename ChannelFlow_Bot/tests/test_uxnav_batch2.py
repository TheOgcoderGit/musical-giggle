"""
UX-NAV-01 / UX-NAV-02 batch tests (Batch 2).

Covers (unit/simulation level; live Telegram E2E stays BLOCKED in the
sandbox - no real bot credentials):

* language-first /start + persistence via users.language_chosen
* account-state Home routing (connected vs unconnected menus)
* per-user navigation-state isolation + screen-expiry helpers
* message-lifecycle rules (edit in place; delete only obsolete BOT
  messages, never user messages; safe fallbacks)
* UI-integrity audit: every callback_data statically produced by a
  DISPLAYED keyboard is dispatched by a handler (no dead buttons,
  no Stars/pay: entries)
* task hub structure: task list first, contextual Back/Home targets
* anonymous feature-request + ticket flows usable pre-login
"""
import asyncio
import os
import sys
import ast
import textwrap

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.conftest_shared import reset_db  # noqa: E402

import pytest  # noqa: E402

# ---- environment imports (must happen after conftest_shared) ----------
from database import models as db_models  # noqa: E402
from database.db import get_connection, init_db  # noqa: E402
from services import i18n_service  # noqa: E402
from services import support_service  # noqa: E402

import bot.nav_state as nav_state  # noqa: E402
import bot.keyboards as kb  # noqa: E402
import bot.handlers as h  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_db_and_session():
    reset_db()
    yield
    nav_state.clear_screens(-1)
    # importing bot.handlers pulls core.client which (known side
    # effect, tracker bug f) creates an empty ChannelFlow.session file.
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


_ID = [1000]


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
        self.deleted = []

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
        self.from_user = user or FakeUser(7, "Tester")

    async def answer(self, text=None, show_alert=False, **kwargs):
        self.alerts.append((text, show_alert))

    async def edit_message_text(self, text, reply_markup=None, **kwargs):
        return await self.message.edit_text(text, reply_markup=reply_markup)

    async def edit_message_reply_markup(self, reply_markup=None, **kwargs):
        self.message.reply_markup = reply_markup
        return self.message


class FakeUser:
    def __init__(self, uid, first="Tester", username=None):
        self.id = uid
        self.first_name = first
        self.username = username


class FakeUpdate:
    def __init__(self, user, message, callback_query=None):
        self.effective_user = user
        self.message = message
        self.effective_message = message
        self.callback_query = callback_query


class FakeContext:
    def __init__(self):
        self.args = []
        self.bot_data = {}
        self.bot = FakeBot()


class FakeBot:
    def __init__(self):
        self.username = "ChannelFlowTestBot"

    async def send_message(self, chat_id, text, **kwargs):
        return FakeMsg(chat_id=chat_id, text=text)

    async def delete_message(self, chat_id, message_id):
        return True


def _run(coro):
    return asyncio.run(coro)


# ==========================================================
# 92.1 language picker + idempotent /start
# ==========================================================

def test_language_first_run_flag_lifecycle():
    """New users have language_chosen=0; choosing a language persists
    the flag so the bot never asks again."""
    uid = 7001
    db_models.register_user(uid, "u1", "User One")
    assert h._language_chosen(uid) is False
    assert i18n_service.set_user_language(uid, "hi") is True
    assert h._language_chosen(uid) is True
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT language FROM users WHERE telegram_id=?", (uid,))
    assert cur.fetchone()["language"] == "hi"
    conn.close()
    # invalid code is refused and does not flip the flag
    uid2 = 7002
    db_models.register_user(uid2, "u2", "User Two")
    assert i18n_service.set_user_language(uid2, "xx") is False
    assert h._language_chosen(uid2) is False


def test_start_shows_picker_once_then_home_and_never_duplicates_user():
    uid = 7003
    user = FakeUser(uid, "Alia", "alia")

    async def scenario():
        msg = FakeMsg(chat_id=9, text="/start")
        upd = FakeUpdate(user, msg)
        ctx = FakeContext()

        await h.start(upd, ctx)
        # first /start -> the ONLY reply is the language picker inline
        # (sent by _send_language_picker via msg.reply_text)
        assert len(msg.replies) == 1
        assert msg.replies[0].reply_markup is not None

        # second /start while still unchosen -> picker again, no dup user
        msg2 = FakeMsg(chat_id=9, text="/start")
        upd2 = FakeUpdate(user, msg2)
        await h.start(upd2, ctx)
        assert msg2.replies and msg2.replies[0].reply_markup is not None

        # user chooses Hindi through the lang:* callback
        i18n_service.set_user_language(uid, "hi")

        # third /start -> straight to Home (unconnected), no picker
        msg3 = FakeMsg(chat_id=9, text="/start")
        upd3 = FakeUpdate(user, msg3)
        await h.start(upd3, ctx)
        assert msg3.replies, "home must be sent"
        home = msg3.replies[0]
        assert "अकाउंट" in home.text or "Account" in home.text
        assert home.reply_markup is not None  # persistent menu attached

        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM users WHERE telegram_id=?", (uid,))
        assert cur.fetchone()["c"] == 1
        conn.close()

    _run(scenario())


def test_start_skips_picker_for_users_who_chose_language_before():
    """Pre-existing users (migration default language_chosen=0) see the
    picker exactly once; users already flagged skip it (92.1/92.7)."""
    uid = 7004
    db_models.register_user(uid, "u4", "Old User")
    conn = get_connection()
    conn.cursor().execute("UPDATE users SET language_chosen=1 WHERE telegram_id=?", (uid,))
    conn.commit()
    conn.close()

    msg = FakeMsg(chat_id=9, text="/start")
    upd = FakeUpdate(FakeUser(uid), msg)
    _run(h.start(upd, FakeContext()))
    assert msg.replies, "expected home reply"
    assert "Your Tasks" not in msg.replies[0].text  # unconnected home header


# ==========================================================
# 92.2 / 92.3 account-state home routing
# ==========================================================

def test_home_variant_matches_account_state():
    assert nav_state.home_variant(True) == "connected"
    assert nav_state.home_variant(False) == "unconnected"


def test_home_text_reflects_connection_state():
    user = FakeUser(7010, "Ravi")
    assert "Connected" in h._home_text(user, True)
    assert "Not connected" in h._home_text(user, False)
    # account-state aware menus (92.3): no connected-only rows on the
    # unconnected menu and vice versa
    unconnected_rows = [btn.text for row in kb.menu_unconnected_keyboard().keyboard for btn in row]
    connected_rows = [btn.text for row in kb.menu_connected_keyboard().keyboard for btn in row]
    assert "📁 Projects" not in unconnected_rows
    assert "📁 Projects" in connected_rows
    assert "🔗 Connect Account" in unconnected_rows
    assert "🔗 Connect Account" not in connected_rows
    assert "🆘 Support" in connected_rows and "🆘 Support" in unconnected_rows


def test_go_home_renders_unconnected_home_for_new_user():
    uid = 7011
    db_models.register_user(uid, "u11", "N")
    msg = FakeMsg(chat_id=9, text=kb.MB_HOME)
    _run(h._go_home(msg, FakeUser(uid)))
    assert msg.replies and "Not connected" in msg.replies[0].text


# ==========================================================
# 93.4 per-user navigation state
# ==========================================================

def test_nav_state_is_isolated_per_user():
    nav_state.clear_screens(9001)
    nav_state.clear_screens(9002)
    nav_state.push_screen(9001, "tasks", chat_id=1, msg_id=11)
    nav_state.push_screen(9001, "task_detail:5", chat_id=1, msg_id=12)
    nav_state.push_screen(9002, "home", chat_id=1, msg_id=13)

    assert nav_state.screen_names(9001) == ["tasks", "task_detail:5"]
    assert nav_state.screen_names(9002) == ["home"]
    # user 2's state is untouched by user 1's navigation
    nav_state.pop_screen(9001)
    assert nav_state.screen_names(9002) == ["home"]
    assert nav_state.is_on_screen(9001, "tasks")


def test_screen_expiry_detection():
    nav_state.clear_screens(9010)
    nav_state.push_screen(9010, "tasks", chat_id=1, msg_id=21)
    assert nav_state.expired_for(9010, "tasks") is False
    nav_state.push_screen(9010, "task_detail:9", chat_id=1, msg_id=22)
    # a callback for the OLD screen is stale now
    assert nav_state.expired_for(9010, "tasks") is True
    assert nav_state.expired_for(9010, "task_detail:9") is False
    # re-opening the same screen refreshes instead of duplicating
    nav_state.push_screen(9010, "task_detail:9", chat_id=1, msg_id=23)
    assert nav_state.screen_names(9010) == ["tasks", "task_detail:9"]


# ==========================================================
# 93.3 message lifecycle
# ==========================================================

def test_place_edits_in_place_for_inline_navigation():
    msg = FakeMsg(chat_id=5, text="old screen")
    _run(nav_state.place(msg, 9020, "tasks", "Your Tasks", reply_markup=None, edit=True))
    assert len(msg.edits) == 1
    assert msg.text == "Your Tasks"
    # no duplicate messages were sent
    assert not msg.replies


def test_place_falls_back_to_resend_when_edit_fails():
    class BoomMsg(FakeMsg):
        async def edit_text(self, *a, **kw):
            raise RuntimeError("edit refused")

    msg = BoomMsg(chat_id=5, text="old")
    _run(nav_state.place(msg, 9021, "tasks", "Your Tasks", edit=True))
    # obsolete BOT message deleted, replacement sent, state updated
    assert msg.deleted == [msg.message_id]
    assert len(msg.replies) == 1
    assert msg.replies[0].text == "Your Tasks"


def test_fresh_navigation_deletes_previous_bot_screen_not_user_message():
    nav_state.clear_screens(9022)
    # tracked bot screen from before
    nav_state.push_screen(9022, "tasks", chat_id=5, msg_id=555)
    # user presses 📁 Projects -> their own message id 556
    user_msg = FakeMsg(chat_id=5, text=kb.MB_PROJECTS, message_id=556)
    _run(nav_state.place(user_msg, 9022, "tasks", "Your Tasks"))
    # the obsolete bot screen (555) was deleted through the chat...
    assert user_msg.chat.deleted == [555]
    # ...and the user message itself was NEVER deleted
    assert 556 not in user_msg.chat.deleted
    assert len(user_msg.replies) == 1


def test_lifecycle_never_deletes_the_trigger_message():
    nav_state.clear_screens(9023)
    # edge: tracked id happens to equal the trigger message id
    nav_state.push_screen(9023, "tasks", chat_id=5, msg_id=777)
    user_msg = FakeMsg(chat_id=5, text="📁 Projects", message_id=777)
    _run(nav_state.place(user_msg, 9023, "tasks", "Your Tasks"))
    assert user_msg.chat.deleted == []


# ==========================================================
# Task hub structure (93.1 / 93.2)
# ==========================================================

def _flat_callbacks(markup):
    out = []
    for row in markup.inline_keyboard:
        for btn in row:
            if btn.callback_data:
                out.append(btn.callback_data)
            elif btn.url:
                out.append(f"url:{btn.url}")
    return out


def test_task_list_has_tasks_then_create_and_home():
    projects = [
        {"id": 1, "name": "Deals", "status": 1},
        {"id": 2, "name": "News", "status": 0},
    ]
    cbs = _flat_callbacks(kb.task_list_keyboard(projects, can_create=True))
    assert "projcard:1" in cbs and "projcard:2" in cbs
    assert "newproj" in cbs
    assert cbs[-1] == "nav:home"


def test_task_detail_contextual_buttons_and_back_home():
    cbs = _flat_callbacks(kb.task_detail_keyboard(7, running=False))
    assert "start:7" in cbs and "stop:7" not in cbs
    cbs_paused = _flat_callbacks(kb.task_detail_keyboard(7, running=True))
    assert "stop:7" in cbs_paused and "start:7" not in cbs_paused
    for expected in ("listsource:7", "listdestination:7", "projfilters:7",
                     "testproject:7", "stats:7", "editproj:7",
                     "deleteconfirm:7", "nav:projects", "nav:home"):
        assert expected in cbs, f"missing {expected}"


def test_edit_root_back_targets_task_details():
    cbs = _flat_callbacks(kb.edit_project_keyboard(7))
    assert "projcard:7" in cbs  # Back -> Task Details
    assert "nav:home" in cbs
    # no unimplemented AI / watermark / affiliate / clone entries
    for dead in ("aisettings", "wmsettings", "affiliatesettings",
                 "cloneproj", "analytics", "fmtprefixsuffix"):
        assert not any(c.startswith(dead) for c in cbs), dead


def test_confirm_delete_cancel_returns_to_task():
    markup = kb.delete_confirm_keyboard("delete:7", "projcard:7")
    cbs = _flat_callbacks(markup)
    assert cbs == ["delete:7", "projcard:7"]


def test_show_task_list_empty_state_has_create_but_no_actions():
    projects = []
    cbs = _flat_callbacks(kb.task_list_keyboard(projects, can_create=True))
    assert "newproj" in cbs
    assert not any(c.startswith(("delete", "start", "editproj")) for c in cbs)


# ==========================================================
# UI-integrity audit (companion defect: dead buttons / Stars)
# ==========================================================

# Keyboard builders actually rendered by live handlers (alias chain
# resolved: project_keyboard -> project_actions_keyboard etc.).
DISPLAYED_KEYBOARD_FUNCS = [
    "project_actions_keyboard",
    "edit_project_keyboard",
    "account_section_keyboard",
    "settings_section_keyboard",
    "source_item_keyboard",
    "destination_item_keyboard",
    "project_settings_keyboard",
    "project_filters_keyboard",
    "keyword_filter_keyboard",
    "domain_filter_keyboard",
    "sender_filter_keyboard",
    "media_filter_choice_keyboard",
    "formatting_keyboard",
    "formatting_replace_list_keyboard",
    "formatting_remove_list_keyboard",
    "platform_selection_keyboard",
    "instagram_management_keyboard",
    "instagram_destination_type_keyboard",
    "approval_queue_item_keyboard",
    "support_section_keyboard",
    "support_ticket_keyboard",
    "task_list_keyboard",
    "task_detail_keyboard",
    # legacy aliases used by old call sites
    "project_keyboard",
    "account_card_keyboard",
    "settings_keyboard",
    # ⭐ Telegram Stars upgrade chain (PRD section 26; live since
    # Batch 3 - rows emit only the handled "upgrade" prefix)
    "stars_plan_keyboard",
    "stars_duration_keyboard",
    "stars_confirm_keyboard",
]

DISPLAYED_KEYBOARD_CONSTS = [
    "FIRST_RUN_LANGUAGE_KEYBOARD",
    "LANGUAGE_KEYBOARD",
    "admin_keyboard",
]

# every callback prefix a displayed keyboard may emit must be handled
# by button_handler / promo / admin routers
HANDLED_PREFIXES = {
    "nav", "lang", "newproj", "projcard", "editproj", "deleteconfirm",
    "deletesourceconfirm", "deletedestinationconfirm",
    "support", "help", "acct", "upgrade", "platform", "instagram",
    "igsetprocessing", "igadddest", "igtargettype", "igdestination",
    "igdeldest", "igqueue", "igjobdone", "igjobskip",
    "source", "destination", "listsource", "listdestination",
    "start", "stop", "deletesource", "deletedestination",
    "testdestination", "testproject", "rename", "delete",
    "settings", "wallet", "togglesource", "toggledestination",
    "backproject", "projsettings", "togglemode", "togglesilent",
    "toggleprotect", "togglealbum", "setdelay", "projfilters",
    "mediafilter", "mediafilterset", "setwhitelist", "setblacklist",
    "setregex", "crfield", "crlogic", "formatting", "fmtfield",
    "fmtreplace", "fmtreplaceadd", "fmtreplacedel", "fmtremove",
    "fmtremoveadd", "fmtremovedel", "fmtclear", "clearfilters",
    "clearfiltersconfirm", "filterkw", "filterdomains", "filtersenders",
    "fmtroot", "stats", "logs", "admin", "promo", "noop",
    # Batch 4: task-list pagination/search rows + extra-credits hub
    "tasks", "credits",
}

# callbacks that must NEVER be produced by displayed keyboards
FORBIDDEN_PREFIXES = {"pay", "pacct", "wacode", "analytics", "ai",
                      "aff", "wm", "cloneproj", "editproj_old"}


def _literal_prefixes_in(node):
    """All static callback prefixes inside one keyboard function."""
    prefixes = set()

    class _V(ast.NodeVisitor):
        def visit_Call(self, call):  # noqa: N802
            if isinstance(call.func, ast.Name) and call.func.id == "InlineKeyboardButton":
                for kw in call.keywords:
                    if kw.arg == "callback_data":
                        value = kw.value
                        if isinstance(value, ast.Constant) and isinstance(value.value, str):
                            data = value.value
                            prefixes.add(data.split(":", 1)[0])
                        elif isinstance(value, ast.JoinedStr):
                            first = value.values[0]
                            if isinstance(first, ast.Constant) and first.value:
                                prefixes.add(str(first.value).split(":", 1)[0])
            self.generic_visit(call)

    _V().visit(node)
    return prefixes


def _audit_prefixes():
    src = open(os.path.join(os.path.dirname(kb.__file__), "keyboards.py")).read()
    tree = ast.parse(src)
    produced = set()
    in_funcs = set(DISPLAYED_KEYBOARD_FUNCS)
    current = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            current = [node.name]
        elif isinstance(node, ast.Assign):
            current = [t.id for t in node.targets if isinstance(t, ast.Name)]
        else:
            current = []
        if any(c in in_funcs or c in DISPLAYED_KEYBOARD_CONSTS for c in current):
            produced |= _literal_prefixes_in(node)
    return produced


def test_no_dead_buttons_on_displayed_keyboards():
    produced = _audit_prefixes()
    orphans = sorted(produced - HANDLED_PREFIXES)
    assert not orphans, f"unhandled callback prefixes on displayed keyboards: {orphans}"
    forbidden = sorted(produced & FORBIDDEN_PREFIXES)
    assert not forbidden, f"forbidden (dead/Stars-era) prefixes on displayed keyboards: {forbidden}"
    assert "pay" not in produced, "Stars/pay buttons must never be displayed"


def test_reply_menu_labels_are_handled_by_menu_handler():
    """Every reply-keyboard label has a matching menu_handler branch."""
    handled_labels = {
        kb.MB_CONNECT_ACCOUNT, kb.MB_WHY_CONNECT, kb.MB_SUBSCRIPTION_PLAN,
        kb.MB_HOW_IT_WORKS, kb.MB_SUPPORT, kb.MB_PROJECTS, kb.MB_SUBSCRIPTION,
        kb.MB_REWARDS, kb.MB_ACCOUNT, kb.MB_SETTINGS, kb.MB_HOME,
    }
    handler_src = open(os.path.join(os.path.dirname(h.__file__), "handlers.py")).read()
    handler_ast = ast.parse(handler_src)
    # collect MB_* constants referenced inside handlers.py
    used = set()
    for node in ast.walk(handler_ast):
        if isinstance(node, ast.Name) and node.id.startswith("MB_"):
            used.add(node.id)
    for const_name in ("MB_CONNECT_ACCOUNT", "MB_WHY_CONNECT", "MB_SUBSCRIPTION_PLAN",
                       "MB_HOW_IT_WORKS", "MB_SUPPORT", "MB_PROJECTS", "MB_SUBSCRIPTION",
                       "MB_REWARDS", "MB_ACCOUNT", "MB_SETTINGS", "MB_HOME"):
        assert const_name in used, f"{const_name} unused in handlers (label would be dead)"
    assert len(handled_labels) == 11


# ==========================================================
# Anonymous (pre-login) support flows
# ==========================================================

def test_feature_request_works_for_unconnected_anonymous_user():
    """Companion defect: feature requests must not require a connected
    account and must never crash on missing account state."""
    uid = 7030  # never /start'ed, never connected
    msg = FakeMsg(chat_id=9, text="I want a dark theme please")
    upd = FakeUpdate(FakeUser(uid, "Ghost"), msg)
    _run(h.menu_handler(upd, FakeContext()))

    # no waiting state was set, so the pre-login text hits the login
    # gate: a clear pointer, never a crash and never a stray ticket
    assert msg.replies and "Connect your Telegram account first" in msg.replies[0].text

    # now the real flow: hub -> feature request -> text
    from bot.states import WAITING_FEEDBACK
    WAITING_FEEDBACK[uid] = True
    msg2 = FakeMsg(chat_id=9, text="Support multiple Telegram destinations per task")
    upd2 = FakeUpdate(FakeUser(uid, "Ghost"), msg2)
    _run(h.menu_handler(upd2, FakeContext()))

    tickets = [dict(t) for t in support_service.list_user_tickets(uid)]
    matched = False
    for t in tickets:
        body = " ".join(
            str(dict(m).get("message") or "") for m in (support_service.get_messages(t["id"]) or [])
        )
        if t["category"] == "feature" and ("multiple Telegram" in body or
                                           "multiple Telegram" in str(t.get("subject") or "")):
            matched = True
    assert matched, "feature request ticket with the message text was not stored"
    assert msg2.replies and "Thanks" in msg2.replies[0].text
    assert uid not in WAITING_FEEDBACK


def test_ticket_creation_and_reply_pre_login():
    uid = 7031
    db_models.register_user(uid, "t1", "T")

    from bot.states import WAITING_TICKET_SUBJECT, WAITING_TICKET_MESSAGE, WAITING_TICKET_REPLY

    # category -> subject -> body through menu_handler text routing
    WAITING_TICKET_SUBJECT[uid] = {"category": "billing"}
    m1 = FakeMsg(chat_id=9, text="Invoice question")
    _run(h.menu_handler(FakeUpdate(FakeUser(uid), m1), FakeContext()))
    assert uid not in WAITING_TICKET_SUBJECT
    assert uid in WAITING_TICKET_MESSAGE

    m2 = FakeMsg(chat_id=9, text="Where do I find my invoices?")
    _run(h.menu_handler(FakeUpdate(FakeUser(uid), m2), FakeContext()))
    tickets = [dict(t) for t in support_service.list_user_tickets(uid)]
    assert tickets and tickets[0]["category"] == "billing"
    ticket_id = tickets[0]["id"]
    msgs = [dict(m) for m in (support_service.get_messages(ticket_id) or [])]
    assert any("invoices" in str(m.get("message") or "") for m in msgs)

    # reply on the ticket
    WAITING_TICKET_REPLY[uid] = ticket_id
    m3 = FakeMsg(chat_id=9, text="Found them, thanks")
    _run(h.menu_handler(FakeUpdate(FakeUser(uid), m3), FakeContext()))
    msgs = [dict(m) for m in (support_service.get_messages(ticket_id) or [])]
    assert any("Found them" in str(m.get("message") or "") for m in msgs)
    assert uid not in WAITING_TICKET_REPLY


def test_ticket_view_is_ownership_checked():
    """Another user's crafted support:view:<id> must not leak data."""
    owner, stranger = 7040, 7041
    db_models.register_user(owner, "o", "Owner")
    db_models.register_user(stranger, "s", "Stranger")
    ticket_id = support_service.create_ticket(owner, "general", "Secret subject", "secret body")

    msg = FakeMsg(chat_id=9, text="x")
    q = FakeQuery(msg)
    q.from_user = FakeUser(stranger)
    q.data = f"support:view:{ticket_id}"

    async def _call():
        await h._handle_support_callback(q, FakeContext(), stranger, "view", ["support", "view", str(ticket_id)])

    _run(_call())
    # stranger sees an expired/not-found screen, never the subject
    assert any("expired" in e or "not found" in e for e in msg.edits)
    assert "Secret subject" not in "".join(msg.edits)
    assert "secret body" not in "".join(msg.edits)


# ==========================================================
# Stats/delete flows integrate with the task hub
# ==========================================================

def _make_project(uid, name="Deals"):
    pid = h.create_project(uid, name, platform_type="telegram")
    return pid


def test_show_task_list_and_detail_chain_on_one_user():
    uid = 7050
    db_models.register_user(uid, "owner", "Owner")
    pid = _make_project(uid)
    h.add_source(pid, -1001, "src", "Source Ch", "channel")
    h.add_destination(pid, -1002, "dst", "Dest Ch", "channel")

    msg = FakeMsg(chat_id=3, text=kb.MB_PROJECTS)
    _run(h._show_task_list(msg, uid))
    assert msg.replies and "Your Tasks" in msg.replies[0].text
    list_msg = msg.replies[0]
    cbs = _flat_callbacks(list_msg.reply_markup)
    assert f"projcard:{pid}" in cbs

    # open the task -> edit in place, same message id
    _run(h._show_task_detail(list_msg, pid, uid, edit=True))
    assert list_msg.text and "Deals" in list_msg.text
    assert list_msg.text.startswith("📌")
    cbs = _flat_callbacks(list_msg.reply_markup)
    assert f"editproj:{pid}" in cbs and "nav:projects" in cbs


def test_delete_flow_confirms_then_refreshes_to_empty_state():
    uid = 7051
    db_models.register_user(uid, "owner2", "Owner2")
    pid = _make_project(uid, "OnlyTask")

    msg = FakeMsg(chat_id=3, text=kb.MB_PROJECTS)
    _run(h._show_task_list(msg, uid))
    list_msg = msg.replies[0]

    # task details -> delete-confirm screen (both edits in place)
    _run(h._show_task_detail(list_msg, pid, uid, edit=True))
    assert list_msg.text.startswith("📌")
    _run(h._confirm_delete_task(list_msg, pid, uid, edit=True))
    assert "Delete Task" in list_msg.text or "डिलीट" in list_msg.text

    # user taps "Yes, delete" -> full button_handler runs the delete branch
    query = FakeQuery(list_msg, user=FakeUser(uid))
    query.data = f"delete:{pid}"
    upd = FakeUpdate(FakeUser(uid), list_msg, callback_query=query)
    _run(h.button_handler(upd, FakeContext()))

    # the confirmation message was replaced by the empty-state refresh
    assert "haven't created any tasks" in list_msg.text or "कोई टास्क" in list_msg.text
    cbs = _flat_callbacks(list_msg.reply_markup)
    assert "newproj" in cbs and cbs[-1] == "nav:home"
    # project really is gone
    assert h.get_project(pid) is None
