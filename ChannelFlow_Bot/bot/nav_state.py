"""
ChannelFlow AI - Centralized per-user navigation state (UX-NAV-01/02)
======================================================================

UX-NAV-01 section 6 / UX-NAV-02 section 4 require ONE consistent
navigation approach with per-user state (never global/cross-user):

    * ``_screens`` - a per-user stack of the bot messages that currently
      render an interactive "hub" screen (Home, Tasks, Task Details,
      Edit Task, Settings, Account, Support, ...). Each entry stores
      exactly one bot message id per screen so the message lifecycle
      (93.3) can edit/replace obsolete bot UI messages in place.
      Only messages the BOT sent are ever tracked or deleted - user
      messages are never touched.

    * ``place()`` - the single choke point every hub screen render goes
      through:
        - from an inline button on a bot message -> edit that message
          in place (no duplicate message);
        - from a reply-menu button / command (a user message) ->
          best-effort delete the previous tracked BOT screen message,
          then send a fresh bot message (never delete the user's own
          message);
        - on any Telegram edit/delete error -> fall back safely.

    * Pure helpers (account-state home target, screen-expiry checks,
      stack manipulation) are deliberately free of I/O so the pytest
      suite can exercise them with plain fakes.

Callbacks validate user + project ownership inside the handlers; stale
callbacks are answered with the i18n "screen expired" text plus a way
Home (UX-NAV-01 92.6 / UX-NAV-02 93.4).
"""

import logging

logger = logging.getLogger(__name__)

# Per-user navigation stack. Structure:
#   user_id -> [ {"name": str, "chat_id": int|None, "msg_id": int|None,
#                 "data": dict|None}, ... ]   (index 0 = oldest)
_screens = {}


# ==========================================================
# PURE STATE HELPERS (unit-testable, no Telegram I/O)
# ==========================================================

def screen_names(user_id):
    """Names of every currently tracked screen for a user, oldest
    first. Every element is owned by that one user - there is no
    global navigation state shared across users."""
    return [entry["name"] for entry in _screens.get(user_id, [])]


def top_screen(user_id):
    """The most recent bot-screen entry for this user, or None."""
    stack = _screens.get(user_id)
    return stack[-1] if stack else None


def is_on_screen(user_id, name):
    """True when the user's top (current) screen is ``name``."""
    top = top_screen(user_id)
    return bool(top and top["name"] == name)


def push_screen(user_id, name, chat_id=None, msg_id=None, data=None):
    """Record that a bot screen ``name`` is now displayed to this user
    at (chat_id, msg_id). Re-entering the same top screen refreshes
    that entry instead of duplicating it."""
    stack = _screens.setdefault(user_id, [])
    if stack and stack[-1]["name"] == name:
        stack[-1].update(chat_id=chat_id, msg_id=msg_id, data=data)
        return stack[-1]
    entry = {"name": name, "chat_id": chat_id, "msg_id": msg_id, "data": data}
    stack.append(entry)
    return entry


def pop_screen(user_id, name=None):
    """Pop the top entry (optionally only when it matches ``name``).
    Returns the popped entry or None."""
    stack = _screens.get(user_id)
    if not stack:
        return None
    if name is not None and stack[-1]["name"] != name:
        return None
    return stack.pop()


def drop_screens_to(user_id, name):
    """Pop entries until the top is ``name`` (used after returning to
    an earlier screen through contextual Back buttons)."""
    stack = _screens.get(user_id)
    if not stack:
        return
    while stack and stack[-1]["name"] != name:
        stack.pop()


def clear_screens(user_id):
    """Drop every tracked screen for the user (disconnect, home reset,
    session cleanup). Does NOT touch any chat message."""
    _screens.pop(user_id, None)


def home_variant(is_connected: bool) -> str:
    """Account-state aware Home target (UX-NAV-01 92.3): the unconnected
    Home when the account is not connected, the connected Home when it
    is. Returns 'connected' | 'unconnected'."""
    return "connected" if is_connected else "unconnected"


def expired_for(user_id, name) -> bool:
    """UX-NAV-02 93.4: a callback for hub screen ``name`` is stale when
    the user's current tracked screen is a different hub screen.
    Returns True for stale (expired) callbacks so the caller can answer
    with the safe "screen expired" response instead of acting."""
    top = top_screen(user_id)
    return bool(top and top["name"] != name)


# ==========================================================
# MESSAGE LIFECYCLE (UX-NAV-02 93.3)
# ==========================================================
#
# Category A (interactive UI screens) goes through place() which
# edits/replaces the previous bot message instead of stacking a new
# permanent duplicate every navigation step. Category B confirmations
# stay as normal replies. Category C (OTP/2FA login UI) is cleaned up
# in the connect flow, never logs values and never fails the login on
# cleanup errors.

async def _try_delete(message):
    try:
        await message.delete()
    except Exception:
        # Best-effort only (e.g. the message is too old to delete).
        logger.debug("Could not delete obsolete bot screen message", exc_info=True)


async def _try_edit(message, text, reply_markup):
    try:
        sent = await message.edit_text(text, reply_markup=reply_markup,
                                       disable_web_page_preview=True)
        return sent
    except Exception:
        logger.info("In-place edit failed; falling back to delete-and-resend "
                    "of the bot message only", exc_info=True)
        return None


async def _delete_tracked_previous(anchor, user_id, trigger_msg_id, chat_id):
    """Delete the previously tracked bot screen message (never a user
    message). The only live handle we have from a fresh user-message
    trigger is the trigger's chat, so deletion goes through
    Chat.delete_message when the client exposes it; otherwise the
    obsolete message is simply left in place (safe degradation)."""
    top = top_screen(user_id)
    if not top or top.get("msg_id") is None:
        return
    if top.get("chat_id") not in (None, chat_id):
        return
    if top.get("msg_id") == trigger_msg_id:
        return  # never delete the user's own trigger message

    chat = getattr(anchor, "chat", None)
    delete_fn = getattr(chat, "delete_message", None)
    if delete_fn is None:
        return  # no live handle (unit-test fakes may still provide one)
    try:
        await delete_fn(top["msg_id"])
    except Exception:
        logger.debug("Could not delete tracked bot screen message", exc_info=True)


async def place(anchor, user_id, name, text, reply_markup=None, data=None, edit=False):
    """Render hub screen ``name`` through the single lifecycle choke
    point.

    anchor    - telegram Message of a CallbackQuery (edit=True) or the
                user Message that triggered navigation (edit=False).
    edit      - True when the trigger was an inline button on a bot
                message: edit that bot message in place, and if Telegram
                refuses, delete the obsolete bot message and resend.
                False when the trigger was a user text message: delete
                the previously tracked bot screen message (best-effort,
                never a user message) and send a fresh bot message.

    Returns the message object that now carries the screen (or None).
    """
    chat_id = getattr(anchor, "chat_id", None)
    if chat_id is None:
        chat = getattr(anchor, "chat", None)
        chat_id = getattr(chat, "id", None)

    trigger_msg_id = getattr(anchor, "message_id", None)

    if edit:
        sent = await _try_edit(anchor, text, reply_markup)
        if sent is not None:
            msg_id = getattr(sent, "message_id", None) or trigger_msg_id
            push_screen(user_id, name, chat_id=chat_id, msg_id=msg_id, data=data)
            return sent
        # Fallback path: this message is a bot message (the inline
        # button lives on it) so deleting it is allowed.
        await _try_delete(anchor)

    else:
        # User-message trigger: retire the previously tracked BOT
        # screen message.
        await _delete_tracked_previous(anchor, user_id, trigger_msg_id, chat_id)

    sent = await anchor.reply_text(text, reply_markup=reply_markup,
                                   disable_web_page_preview=True)
    msg_id = getattr(sent, "message_id", None)
    push_screen(user_id, name, chat_id=chat_id, msg_id=msg_id, data=data)
    return sent
