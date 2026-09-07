"""
ChannelFlow AI - Telegram Bot Handlers
========================================

This module contains every user-facing handler for the bot:

    * /start                       -> start()
    * /cancel                      -> cancel()
    * Reply keyboard (main menu)   -> menu_handler()
    * Inline keyboard callbacks    -> button_handler()

Design notes
------------
State is tracked with the plain module-level dictionaries defined in
``bot.states`` (WAITING_PROJECT_NAME, WAITING_SOURCE, WAITING_DESTINATION,
WAITING_RENAME, CURRENT_PROJECT), keyed by the Telegram user id. This keeps
the handlers compatible with the rest of the project's architecture.

Every callback_data string consumed here matches exactly what is produced
by ``bot.keyboards`` (project_keyboard, source_item_keyboard,
destination_item_keyboard, settings_keyboard). No callback name was
invented that isn't already wired into a keyboard.

Ownership of every project / source / destination is verified against the
requesting Telegram user before any read or write happens, closing the
insecure-direct-object-reference gap that existed in the previous
implementation (any user could act on any project by guessing/crafting a
project_id in callback data).
"""

import logging
import json
import time
from datetime import datetime, timezone

from telegram import Update, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, LabeledPrice
from telegram.ext import ContextTypes

from bot.keyboards import (
    main_menu,
    pre_login_menu,
    project_keyboard,
    source_item_keyboard,
    destination_item_keyboard,
    settings_keyboard,
    admin_keyboard,
    project_settings_keyboard,
    project_filters_keyboard,
    media_filter_choice_keyboard,
    platform_selection_keyboard,
    instagram_management_keyboard,
    instagram_destination_type_keyboard,
    approval_queue_item_keyboard,
    formatting_keyboard,
    formatting_replace_list_keyboard,
    formatting_remove_list_keyboard,
    account_card_keyboard,
    # UX-NAV-01/02 account-state menus + task hub (imported here so the
    # full surface is import-auditable for the orphan-callback tests)
    menu_unconnected_keyboard,
    menu_connected_keyboard,
    FIRST_RUN_LANGUAGE_KEYBOARD,
    LANGUAGE_KEYBOARD,
    stars_plan_keyboard,
    stars_duration_keyboard,
    stars_confirm_keyboard,
    task_list_keyboard,
    task_detail_keyboard,
    delete_confirm_keyboard,
    edit_project_keyboard,
    keyword_filter_keyboard,
    domain_filter_keyboard,
    sender_filter_keyboard,
    support_section_keyboard,
    support_ticket_keyboard,
    # reply-menu label constants (single source of truth in keyboards.py)
    MB_CONNECT_ACCOUNT,
    MB_WHY_CONNECT,
    MB_SUBSCRIPTION_PLAN,
    MB_HOW_IT_WORKS,
    MB_SUPPORT,
    MB_PROJECTS,
    MB_SUBSCRIPTION,
    MB_REWARDS,
    MB_ACCOUNT,
    MB_SETTINGS,
    MB_HOME,
)

from services import i18n

from bot.states import (
    WAITING_PROJECT_NAME,
    WAITING_TASK_SEARCH,
    WAITING_SOURCE,
    WAITING_DESTINATION,
    WAITING_RENAME,
    WAITING_DELAY,
    WAITING_WHITELIST,
    WAITING_BLACKLIST,
    WAITING_REGEX,
    WAITING_BROADCAST,
    PENDING_PROJECT_NAME,
    WAITING_PROCESSING_CHANNEL,
    WAITING_INSTAGRAM_DESTINATION,
    WAITING_CONTENT_RULE_FIELD,
    WAITING_FORMATTING_FIELD,
    WAITING_REPLACE_RULE,
    WAITING_REMOVE_PATTERN,
    WAITING_CONNECT_PHONE,
    WAITING_CONNECT_STAGE,
    WAITING_CONNECT_STAGE_STARTED,
    WAITING_PAYMENT_SCREENSHOT,
    CURRENT_PROJECT,
    WAITING_SUPPORT_AI,
    WAITING_TICKET_SUBJECT,
    WAITING_TICKET_MESSAGE,
    WAITING_TICKET_REPLY,
    WAITING_FEEDBACK,
)

from database.models import register_user, get_all_user_ids, count_users
from database.db import get_connection
from config import ADMIN_IDS, INSTAGRAM_ACCESS_TOKEN

from services import content_rules_service, formatting_service, plan_service, referral_service, payment_service, pricing_service, wallet_service
from services import stars_service, extra_credits_service
from config import UPI_ID, UPI_PAYEE_NAME
from core import user_sessions, client_pool
from core import session_crypto

from services.project_service import (
    create_project,
    get_projects,
    get_all_projects,
    get_project,
    rename_project,
    delete_project,
    update_status,
    set_platform_type,
    set_processing_channel,
    count_projects
)

from services.source_service import (
    add_source,
    get_sources,
    get_source,
    delete_source,
    count_sources,
    toggle_source_enabled
)

from services.destination_service import (
    add_destination,
    get_destinations,
    get_destination,
    delete_destination,
    count_destinations,
    toggle_destination_enabled
)

from services import settings_service, log_service, stats_service
from services import instagram_service, processing_service
from services.platform_registry import get_platform, get_platforms
from services import support_service, support_ai_service, knowledge_service

from bot import nav_state
from destinations.instagram_destination import is_configured as is_ig_configured, verify_capability as verify_ig_capability

from core.telegram_utils import get_chat, send_test_message
from core.listener import is_running
from core.forwarder import force_refresh_routes
from core.processing_listener import force_refresh_processing_routes
from core.client import client as telethon_client

from bot.admin_promo_handlers import (
    WAITING_PROMO,
    handle_promo_text,
    handle_promo_media,
    handle_promo_callback,
    cancel_promo,
)


async def media_message_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route incoming photo/video messages to the active workflow.

    Payment proof takes priority over admin promotional media. This keeps
    the media handler in bot.handlers (where main.py imports it) while
    delegating promotional media capture to admin_promo_handlers.
    """
    user = update.effective_user
    message = update.effective_message

    if user is None or message is None:
        return

    user_id = user.id

    # Payment screenshot/proof flow. Only the owning user's active
    # PENDING_PAYMENT request may be updated.
    if user_id in WAITING_PAYMENT_SCREENSHOT:
        request_id = WAITING_PAYMENT_SCREENSHOT.get(user_id)
        request = payment_service.get_payment_request(request_id)

        if request is None or request["user_id"] != user_id or request["status"] != "PENDING_PAYMENT":
            WAITING_PAYMENT_SCREENSHOT.pop(user_id, None)
            await message.reply_text("⚠ This payment request is no longer active. Please start the payment flow again.")
            return

        if not message.photo:
            await message.reply_text("📸 Please send the payment screenshot as a photo.")
            return

        file_id = message.photo[-1].file_id
        if payment_service.submit_screenshot(request_id, file_id):
            WAITING_PAYMENT_SCREENSHOT.pop(user_id, None)
            await message.reply_text("✅ Payment screenshot submitted. An admin will review it shortly.")
        else:
            await message.reply_text("⚠ Could not submit the screenshot. Please try again.")
        return

    # Admin promotional post/broadcast media flow.
    if user_id in WAITING_PROMO and user_id in ADMIN_IDS:
        await handle_promo_media(update, context)
        return

    # Ignore unrelated media rather than generating a misleading error.
    return


logger = logging.getLogger(__name__)


# ==========================================
# CONSTANTS
# ==========================================

BTN_NEW_PROJECT = "➕ New Project"
BTN_MY_PROJECTS = "📁 My Projects"
BTN_STATUS = "📊 Status"
BTN_SETTINGS = "⚙ Settings"
BTN_MY_PLAN = "📦 My Plan"
BTN_CONNECT_NOW = "🚀 Connect Now"
BTN_GUIDE = "📖 Guide"
BTN_TOUR = "🧭 Tour"

MAIN_MENU_BUTTONS = {
    BTN_NEW_PROJECT,
    BTN_MY_PROJECTS,
    BTN_STATUS,
    BTN_SETTINGS,
    BTN_MY_PLAN
}

# Connected-menu labels (UX-NAV-01 92.3) + the legacy labels they
# replace. Pressing any of these clears pending input states so the
# user can never get permanently stuck mid-flow.
CONNECTED_MENU_LABELS = MAIN_MENU_BUTTONS | {
    MB_PROJECTS,
    MB_SUBSCRIPTION,
    MB_REWARDS,
    MB_ACCOUNT,
    MB_SETTINGS,
    MB_HOME,
}

MAX_NAME_LENGTH = 100

PLATFORM_LABELS = {
    "telegram": "Telegram → Telegram",
    "instagram_broadcast": "Telegram → Instagram Broadcast",
    "both": "Telegram → Telegram + Instagram",
}


def _platform_label(platform_type):
    return PLATFORM_LABELS.get(platform_type, platform_type)


# ==========================================
# STATE HELPERS
# ==========================================

def _reset_waiting_states(user_id):
    """
    Clears every pending input flow for a user. Called whenever the user
    navigates away (menu button, cancel, project deletion, errors) so a
    stale waiting flag never swallows an unrelated future message.
    """

    WAITING_PROJECT_NAME.pop(user_id, None)
    WAITING_SOURCE.pop(user_id, None)
    WAITING_DESTINATION.pop(user_id, None)
    WAITING_RENAME.pop(user_id, None)
    WAITING_DELAY.pop(user_id, None)
    WAITING_WHITELIST.pop(user_id, None)
    WAITING_BLACKLIST.pop(user_id, None)
    WAITING_REGEX.pop(user_id, None)
    WAITING_BROADCAST.pop(user_id, None)
    PENDING_PROJECT_NAME.pop(user_id, None)
    WAITING_PROCESSING_CHANNEL.pop(user_id, None)
    WAITING_INSTAGRAM_DESTINATION.pop(user_id, None)
    WAITING_CONTENT_RULE_FIELD.pop(user_id, None)
    WAITING_FORMATTING_FIELD.pop(user_id, None)
    WAITING_REPLACE_RULE.pop(user_id, None)
    WAITING_REMOVE_PATTERN.pop(user_id, None)
    # Support-hub text inputs (UX-NAV companion): leaving the hub or
    # navigating away always clears them so a stale flag never swallows
    # an unrelated future message.
    WAITING_SUPPORT_AI.pop(user_id, None)
    WAITING_TASK_SEARCH.pop(user_id, None)
    WAITING_TICKET_SUBJECT.pop(user_id, None)
    WAITING_TICKET_MESSAGE.pop(user_id, None)
    WAITING_TICKET_REPLY.pop(user_id, None)
    WAITING_FEEDBACK.pop(user_id, None)
    cancel_promo(user_id)

    if WAITING_CONNECT_PHONE.pop(user_id, None) is not None or WAITING_CONNECT_STAGE.pop(user_id, None):
        WAITING_CONNECT_STAGE_STARTED.pop(user_id, None)
        user_sessions.cancel_connect(user_id)


def _reply_menu_for(user_id):
    """Persistent reply menu matching the CURRENT account state
    (UX-NAV-01 92.3): connected users get the authenticated menu,
    everyone else the unconnected menu. Used at every reply site so a
    stale keyboard never stays on screen after a state change."""
    if user_id in ADMIN_IDS or user_sessions.is_connected(user_id):
        return menu_connected_keyboard()
    return menu_unconnected_keyboard()


# ==========================================
# TASK-LIST BROWSING STATE (Batch 4 / UX-NAV-03)
# ==========================================
# Per-user pagination + search term for the task list. Deliberately
# separate from WAITING_* flags: the term survives opening a task and
# coming back, and is cleared only by an explicit Clear, Home, or
# disconnect (not by every navigation reset).

_TASK_PAGE_SIZE = 8
_TASK_PAGE = {}     # user_id -> int page (1-based)
_TASK_TERM = {}     # user_id -> str search term (None/absent = browse all)


def _clear_task_browse(user_id):
    _TASK_PAGE.pop(user_id, None)
    _TASK_TERM.pop(user_id, None)
    WAITING_TASK_SEARCH.pop(user_id, None)


def _today_global_usage(user_id) -> int:
    """Forwards used today across all of the user's projects."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT COALESCE(SUM(du.forward_count), 0) AS total FROM daily_usage du "
        "JOIN projects p ON p.id = du.project_id "
        "WHERE p.user_id = ? AND du.usage_date = date('now')",
        (user_id,),
    )
    row = cur.fetchone()
    conn.close()
    return int(row["total"]) if row else 0


# ==========================================
# CONNECT-STAGE HELPERS (/connect, /mycode)
# ==========================================
# WAITING_CONNECT_STAGE values are "code"/"password" (which prompt the
# bot layer shows); WAITING_CONNECT_STAGE_STARTED tracks monotonic
# timestamps so the core.user_sessions cleanup task can expire stale
# flags in lock-step with the pending Telethon client (PRD 43/44).

def _set_connect_stage(user_id, stage):
    WAITING_CONNECT_STAGE[user_id] = stage
    WAITING_CONNECT_STAGE_STARTED[user_id] = time.monotonic()


def _clear_connect_stage(user_id):
    WAITING_CONNECT_STAGE.pop(user_id, None)
    WAITING_CONNECT_STAGE_STARTED.pop(user_id, None)


async def _finish_connect_success(message, user_id):
    """Shared tail of a successful login (OTP or 2FA path).

    PRD hard rule: a successful Telegram account connection must NOT be
    reported as failed merely because starting the per-owner forwarding
    engine later fails. The DB session is already stored and claimed at
    this point, so engine trouble is degraded to a notice while the
    user still sees ✅ connected (the engine restarts on next boot /
    project activation via core/client_pool.py)."""

    _clear_connect_stage(user_id)

    # Idempotent: only fires for a user who never had a trial (see
    # services/plan_service.start_trial). Trial is normally granted at
    # /start already; this covers users who connected before /start
    # trial logic existed.
    plan_service.start_trial(user_id)

    try:
        await client_pool.start_owner_engine(user_id)
    except Exception:
        # Never turn a completed login into a failure because the
        # forwarding engine could not start. Log for diagnostics; the
        # engine will be retried on project activation / restart.
        logger.exception("Post-connect engine start failed for user %s", user_id)
        await message.reply_text(
            "✅ Telegram account connected.\n\n"
            "⚠ Your forwarding engine couldn't start right now - it will "
            "start automatically when you create/activate a project."
        )
        return

    await message.reply_text(
        "✅ Telegram account connected.\n\n"
        "Use the menu below to create your first project.",
        reply_markup=main_menu
    )


def _get_owned_project(project_id, user_id):
    """
    Returns the project row if it exists AND belongs to user_id,
    otherwise None. Prevents cross-account access via crafted callback
    data.
    """

    project = get_project(project_id)

    if project is None:
        return None

    if project["user_id"] != user_id:
        return None

    return project


def _get_owned_source(source_id, user_id):
    """
    Returns (source, project) if the source exists and its parent
    project belongs to user_id, otherwise (None, None).
    """

    source = get_source(source_id)

    if source is None:
        return None, None

    project = _get_owned_project(source["project_id"], user_id)

    if project is None:
        return None, None

    return source, project


def _get_owned_destination(destination_id, user_id):
    """
    Returns (destination, project) if the destination exists and its
    parent project belongs to user_id, otherwise (None, None).
    """

    destination = get_destination(destination_id)

    if destination is None:
        return None, None

    project = _get_owned_project(destination["project_id"], user_id)

    if project is None:
        return None, None

    return destination, project


# ==========================================
# FORMATTING HELPERS
# ==========================================

def _project_status_text(project):
    return "🟢 Running" if project["status"] else "🔴 Stopped"


def _project_card_text(project):

    from services.plan_service import get_project_daily_usage

    sources_total = count_sources(project["id"])
    destinations_total = count_destinations(project["id"])
    platform_type = project["platform_type"] if "platform_type" in project.keys() else "telegram"

    # Add daily forward usage meter
    used, total_limit = get_project_daily_usage(project["id"])
    if total_limit is not None:
        daily_line = f"📤 Daily Forwards: {used} / {total_limit}"
        remaining = total_limit - used
        daily_line += f"\n⏳ Remaining: {remaining}"
    else:
        daily_line = "📤 Daily Forwards: Unlimited"

    lines = [
        f"📂 {project['name']}",
        "",
        f"🔀 {_platform_label(platform_type)}",
        "",
        daily_line,
        "",
        _project_status_text(project),
        "",
        f"📥 Sources: {sources_total}",
        f"📤 Destinations: {destinations_total}",
    ]

    if platform_type in ("instagram_broadcast", "both"):

        processing_title = project["processing_username"] or project["processing_title"] or "not set"
        lines.append(f"🔗 Converter Output: {processing_title}")

        ig_destinations = instagram_service.get_instagram_destinations(project["id"])
        ig_label = ", ".join(d["username"] or d["ig_user_id"] for d in ig_destinations) or "not set"
        lines.append(f"📸 Instagram: {ig_label}")

        counts = processing_service.get_project_job_counts(project["id"])
        lines.append(
            f"Processed: {counts['processed']} · "
            f"Published: {counts['published']} · "
            f"Failed: {counts['failed']}"
        )

    return "\n".join(lines)


def _source_card_text(source):

    username = source["username"] or "-"
    chat_type = source["chat_type"] or "Unknown"
    state = "🟢 Enabled" if source["enabled"] else "🔴 Disabled"

    return (
        "📥 Source\n\n"
        f"📂 {source['title'] or '-'}\n"
        f"🏷 {chat_type}\n"
        f"👤 @{username}\n"
        f"🆔 {source['chat_id']}\n"
        f"{state}"
    )


def _destination_card_text(destination):

    username = destination["username"] or "-"
    chat_type = destination["chat_type"] or "Unknown"
    state = "🟢 Enabled" if destination["enabled"] else "🔴 Disabled"

    return (
        "📤 Destination\n\n"
        f"📂 {destination['title'] or '-'}\n"
        f"🏷 {chat_type}\n"
        f"👤 @{username}\n"
        f"🆔 {destination['chat_id']}\n"
        f"{state}"
    )


async def _start_connect_flow(message, user_id):
    """Shared by the 🚀 Connect Now reply-keyboard button and the
    inline account-card button (acct:connect) - both need the exact
    same prerequisite check + prompt."""

    if user_sessions.is_connected(user_id) and not user_sessions.needs_reconnect(user_id):
        await message.reply_text(
            "✅ Your Telegram account is already connected.\n\n"
            "Use Connected Accounts to manage it, or disconnect it "
            "before connecting again.",
            reply_markup=main_menu,
        )
        return

    error = _connect_prerequisite_error()
    if error:
        await message.reply_text(error)
        return

    _reset_waiting_states(user_id)
    WAITING_CONNECT_PHONE[user_id] = time.monotonic()

    await message.reply_text(
        "📱 Send your phone number with country code, e.g. +919876543210.\n\n"
        "Telegram will send you a login code. Never share that code with "
        "anyone else, including someone claiming to be ChannelFlow support "
        "- we will never ask you to send it to us in any other way."
    )


def _account_card_text(user, plan_entitlements, phone_number, active_task_count, days_remaining, referral_stats):
    """Mirrors the reference bot's account/status screen (Display name,
    User ID, Connected phone, Service Plan, Forwarding Tasks, Your
    Referrals, Credits Balance) - real numbers only. Credits Balance
    shows 0 because that system (Phase 14 payments/credits ledger)
    isn't built yet - every user genuinely has 0 credits right now, so
    0 is the honest answer rather than an invented placeholder.
    Referrals ARE real, from services/referral_service.py."""

    plan = plan_entitlements["plan"]

    def _fmt_limit(v):
        return "Unlimited" if v is None else str(v)

    time_remaining = f"{days_remaining} days" if days_remaining is not None else "Unlimited" if plan != "FREE" else "—"

    return (
        f"Display name: {user.first_name}\n"
        f"User ID: {user.id}\n"
        f"Connected phone: {phone_number or 'None'}\n\n"

        "💎 SERVICE PLAN\n"
        "――――――――――――――――\n"
        f"Current plan: {plan}\n"
        f"Time remaining: {time_remaining}\n"
        f"Active projects: {active_task_count} / {_fmt_limit(plan_entitlements['max_projects'])}\n\n"

        "📋 FORWARDING TASKS\n"
        "――――――――――――――――\n"
        f"Currently active tasks: {active_task_count}\n\n"

        "👥 YOUR REFERRALS\n"
        "――――――――――――――――\n"
        f"Total people invited: {referral_stats['total_invited']}\n"
        f"Currently active referrals: {referral_stats['active_referrals']}\n\n"

        "💰 CREDITS BALANCE\n"
        "――――――――――――――――\n"
        "Current balance: 0.00"
    )


async def _account_card_payload(user):
    """Reads everything the account card needs in one place."""
    entitlements = plan_service.get_entitlements(user.id)
    referral_stats = referral_service.get_referral_stats(user.id)

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT plan_expiry FROM users WHERE telegram_id=?", (user.id,))
    plan_row = cur.fetchone()
    cur.execute("SELECT phone_number FROM user_telegram_sessions WHERE telegram_id=? AND status='connected'", (user.id,))
    row = cur.fetchone()
    cur.execute("SELECT COUNT(*) FROM projects WHERE user_id=? AND status=1", (user.id,))
    active_count = cur.fetchone()[0]
    conn.close()

    phone = row["phone_number"] if row else None

    days_remaining = None

    if plan_row and plan_row["plan_expiry"]:
        try:
            expiry = datetime.fromisoformat(plan_row["plan_expiry"])
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            remaining = expiry - datetime.now(timezone.utc)
            days_remaining = max(0, remaining.days)
        except (TypeError, ValueError):
            pass

    return (
        _account_card_text(user, entitlements, phone, active_count, days_remaining, referral_stats),
        account_card_keyboard(connected=bool(phone)),
    )


async def _show_account_hub(message, user, edit=False):
    """👤 Account hub (UX-NAV-01 92.3). Live rows only: Plan & Billing,
    Wallet, Referrals, Connected Accounts (when connected), Home."""
    text, markup = await _account_card_payload(user)
    return await nav_state.place(message, user.id, "account", text, reply_markup=markup, edit=edit)


async def _send_account_card(message, user):
    # Legacy entry point (fresh message render).
    return await _show_account_hub(message, user, edit=False)


# ==========================================
# SUPPORT HUB TEXT INPUTS (UX-NAV companion)
# ==========================================

async def _handle_support_ai_message(message, user_id, text):
    """Chat message while WAITING_SUPPORT_AI: forwarded to the Support
    AI assistant. Keeps the flag so the conversation can continue until
    the user navigates Home / leaves the hub."""
    result = await support_ai_service.get_support_ai_response(
        user_id, text, user_context=None
    )
    if result and result.success:
        await message.reply_text(f"🤖 {result.text}", disable_web_page_preview=True)
    else:
        error = (result.error if result else "Service unavailable") or "Service unavailable"
        await message.reply_text(f"⚠️ {error}")
    return


async def _handle_ticket_subject(message, user_id, text):
    state = WAITING_TICKET_SUBJECT.get(user_id)
    if not state:
        return
    subject = (text or "").strip()
    if not subject:
        await message.reply_text("❌ Subject cannot be empty. Send a short subject, or /cancel.")
        return
    if len(subject) > 200:
        await message.reply_text(f"❌ Subject is too long (max 200 characters). Try again.")
        return
    WAITING_TICKET_SUBJECT.pop(user_id, None)
    WAITING_TICKET_MESSAGE[user_id] = {"category": state["category"], "subject": subject}
    await message.reply_text(
        f"🎫 Subject: {subject}\n\n"
        "Now describe your issue in a few lines (you can also send a "
        "screenshot after finishing)."
    )


async def _handle_ticket_message(message, user_id, text):
    state = WAITING_TICKET_MESSAGE.get(user_id)
    if not state:
        return
    body = (text or "").strip()
    if not body:
        await message.reply_text("❌ Please describe the issue - a few words is enough.")
        return
    WAITING_TICKET_MESSAGE.pop(user_id, None)

    ticket_id = support_service.create_ticket(
        user_id, state["category"], state["subject"], body
    )
    await message.reply_text(
        "✅ " + i18n.t(user_id, "support.ticket_created", id=ticket_id),
        reply_markup=_ticket_back_markup(ticket_id),
    )


async def _handle_ticket_reply(message, user_id, text):
    ticket_id = WAITING_TICKET_REPLY.get(user_id)
    if ticket_id is None:
        return
    ticket = support_service.get_ticket(ticket_id)
    if ticket is None or ticket["user_id"] != user_id:
        WAITING_TICKET_REPLY.pop(user_id, None)
        await message.reply_text("⚠ This ticket is no longer active.")
        return
    body = (text or "").strip()
    if not body:
        await message.reply_text("❌ Reply cannot be empty.")
        return
    WAITING_TICKET_REPLY.pop(user_id, None)
    support_service.add_message(ticket_id, user_id, "user", body)
    if ticket["status"] in ("resolved", "closed"):
        support_service.set_status(ticket_id, "open")
    await message.reply_text("✅ Reply added to ticket.", reply_markup=_ticket_back_markup(ticket_id))


def _ticket_back_markup(ticket_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🎫 View Ticket", callback_data=f"support:view:{ticket_id}"),
        InlineKeyboardButton("🏠 Home", callback_data="nav:home"),
    ]])


async def _handle_feedback_message(message, user_id, text):
    """Feature-request text. Works for ANY user who reached the hub
    (including accounts that never connected one) - the request is
    stored as a 'feature' support ticket and never crashes on missing
    account state (UX-NAV companion defect)."""
    WAITING_FEEDBACK.pop(user_id, None)
    body = (text or "").strip()
    if not body:
        await message.reply_text("❌ Please describe the feature you'd like.")
        return
    ticket_id = support_service.create_ticket(user_id, "feature", "Feature Request", body)
    await message.reply_text(
        "✅ Thanks! Your feature request was saved "
        f"(#{ticket_id}). We read every request.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🏠 Home", callback_data="nav:home"),
            InlineKeyboardButton("🆘 Support", callback_data="nav:help"),
        ]]),
    )


# ==========================================
# /start - UX-NAV-01 unified onboarding
# ==========================================
# Order of a /start run:
#   1. register_user (idempotent upsert - never duplicates users)
#   2. one-time trial grant (idempotent - never re-grants)
#   3. referral capture from ?start=<code>
#   4. maintenance gate
#   5. language-first picker when users.language_chosen == 0
#      (92.1) - only en/hi on first run; selection persists and the
#      bot never asks again
#   6. account-state-aware Home (92.2/92.3): connected -> connected
#      menu, unconnected -> unconnected menu. Repeated /start is
#      idempotent: it only ever re-shows the current appropriate state.

def _language_chosen(user_id) -> bool:
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT language_chosen FROM users WHERE telegram_id=?", (user_id,))
        row = cur.fetchone()
        conn.close()
        return bool(row and row["language_chosen"])
    except Exception:
        return True  # never block on DB problems - show the menu


def _trial_note(trial_new, trial_active):
    if trial_new:
        return (
            "🎁 Your 7-day Creator trial is NOW ACTIVE - unlimited "
            "projects, sources and destinations while it lasts.\n\n"
        )
    if trial_active:
        return "🎁 Your 7-day Creator trial is active.\n\n"
    return ""


def _home_text(user, is_connected, trial_new=False, trial_active=False):
    name = (user.first_name or "").strip() or "creator"
    note = _trial_note(trial_new, trial_active)
    if is_connected:
        header = i18n.t(user.id, "nav.home_connected", name=name)
    else:
        header = i18n.t(user.id, "nav.home_unconnected", name=name)
    return f"{header}\n\n{note}".rstrip("\n")


async def _go_home(message, user, edit=False):
    """Account-state-aware Home (UX-NAV-01 92.2/92.3). Always safe:
    never cancels flows or touches configuration - it only re-renders
    the appropriate Main Menu state."""
    _reset_waiting_states(user.id)
    _clear_task_browse(user.id)
    connected = user.id in ADMIN_IDS or user_sessions.is_connected(user.id)
    text = _home_text(user, connected)
    menu = menu_connected_keyboard() if connected else menu_unconnected_keyboard()
    if edit:
        # An inline 🏠 Home on a bot message: refresh this message only;
        # the persistent reply menu was already attached by the last
        # fresh render / state change.
        return await nav_state.place(message, user.id, "home", text, reply_markup=None, edit=True)
    return await nav_state.place(message, user.id, "home", text, reply_markup=menu, edit=False)


async def _send_language_picker(message, user_id):
    return await message.reply_text(
        i18n.t(user_id, "lang.prompt"),
        reply_markup=FIRST_RUN_LANGUAGE_KEYBOARD,
    )


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    register_user(
        user.id,
        user.username,
        user.first_name
    )

    # One-time 7-day Creator trial, granted at /start (PRD 83 / E2E
    # 82.1). start_trial is idempotent + persistent: it only fires for
    # a user who has never received a trial (plan FREE, no expiry, no
    # trial marker) and can never re-fire after expiry/restart.
    trial_new = plan_service.start_trial(user.id)

    if context.args:
        referrer_id = referral_service.parse_referral_code(context.args[0])
        if referrer_id:
            referral_service.capture_referral(referrer_id, user.id)

    _reset_waiting_states(user.id)

    if context.bot_data.get("maintenance_mode") and user.id not in ADMIN_IDS:

        await update.message.reply_text(
            "🔧 ChannelFlow AI is under maintenance right now. "
            "Please try again shortly."
        )

        return

    # UX-NAV-01 92.1: language-first onboarding - a user without a
    # saved language preference sees the picker ONCE. After choosing,
    # lang:* continues straight into the appropriate Home below.
    if not _language_chosen(user.id):
        await _send_language_picker(update.message, user.id)
        return

    await _go_home(update.message, user)


# ==========================================
# /connect - per-user Telegram login
# ==========================================

async def connect_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/connect <phone_number> as a single command (matches how other
    forwarding bots do this), in addition to the 🚀 Connect Now button
    flow which prompts for the number as a separate message."""

    user = update.effective_user

    if user_sessions.is_connected(user.id) and not user_sessions.needs_reconnect(user.id):
        await update.message.reply_text(
            "✅ Your Telegram account is already connected.\n\n"
            "Use Connected Accounts to manage it, or disconnect it "
            "before connecting again.",
            reply_markup=main_menu,
        )
        return

    args = context.args

    if not args:
        _reset_waiting_states(user.id)

        error = _connect_prerequisite_error()
        if error:
            await update.message.reply_text(error)
            return

        WAITING_CONNECT_PHONE[user.id] = time.monotonic()
        await update.message.reply_text(
            "📱 Send your phone number with country code, e.g. +919876543210."
        )
        return

    phone = args[0]
    await _handle_connect_phone(update.message, user.id, phone)


# ==========================================
# /mycode - OTP delivery (PRD section 5.2)
# ==========================================
# Two accepted shapes:
#   /mycode94563     (concatenated - arrives as an unknown command)
#   /mycode 94563    (spaced - matches the CommandHandler below)
# The legacy /myflow prefix is also accepted for old clients.

def _is_otp_command(text: str) -> bool:
    lowered = (text or "").strip().lower()
    return lowered.startswith("/mycode") or lowered.startswith("/myflow")


async def _consume_mycode_text(message, user_id, raw_text):
    """Routes an OTP-carrying message (/mycode... or /myflow...) into
    the pending login attempt. Plain numeric messages are NEVER treated
    as login codes (PRD 5.3)."""

    if user_sessions.is_connected(user_id) and not user_sessions.needs_reconnect(user_id):
        _clear_connect_stage(user_id)
        await message.reply_text(
            "✅ Your Telegram account is already connected.",
            reply_markup=main_menu,
        )
        return

    stage = WAITING_CONNECT_STAGE.get(user_id)

    if stage is None:
        await message.reply_text(
            "No active Telegram login attempt.\n"
            "Please use /connect first."
        )
        return

    if stage != "code":
        # Code stage already passed; a second code is meaningless.
        await message.reply_text(
            "No code needed right now - finish the step shown on screen, "
            "or send /cancel to start over."
        )
        return

    ok, parsed = user_sessions.extract_otp_from_command(raw_text)

    if not ok:
        await message.reply_text(f"❌ {parsed}")
        return

    try:
        await user_sessions.submit_code(user_id, parsed)

    except user_sessions.NeedsPassword:
        _set_connect_stage(user_id, "password")
        await message.reply_text(
            "OTP accepted.\n"
            "Your Telegram account has 2-step verification enabled.\n"
            "Please enter your Telegram password."
        )
        return

    except user_sessions.ConnectConflict as e:
        _clear_connect_stage(user_id)
        user_sessions.cancel_connect(user_id)
        await message.reply_text(str(e), reply_markup=pre_login_menu)
        return

    except user_sessions.ConnectError as e:
        await message.reply_text(f"❌ {e}")
        return

    except Exception:
        logger.exception("Unexpected error finishing /connect for user %s", user_id)
        _clear_connect_stage(user_id)
        user_sessions.cancel_connect(user_id)
        await message.reply_text(
            "❌ Something went wrong finishing the login. Please send "
            "/connect <phone_number> to start over.",
            reply_markup=pre_login_menu,
        )
        return

    await _finish_connect_success(message, user_id)


async def mycode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for the spaced form: /mycode 94563."""

    user = update.effective_user
    raw = update.message.text or "/mycode"

    await _consume_mycode_text(update.message, user.id, raw)


async def unknown_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catches commands no dedicated CommandHandler owns - including
    the concatenated OTP form /mycode94563 (Telegram parses that as an
    unknown command, not as /mycode + args). Anything that is not an
    OTP command gets a short pointer to /start."""

    message = update.effective_message
    if message is None or not message.text:
        return

    text = message.text.strip()

    if _is_otp_command(text):
        await _consume_mycode_text(message, update.effective_user.id, text)
        return

    await message.reply_text(
        "❓ Unknown command.\n\n"
        "Use /start to open the menu, or /connect to link your "
        "Telegram account."
    )


def _connect_prerequisite_error():
    """Checked BEFORE starting the phone/OTP flow, not after - failing
    at the last step (encrypting the finished session) after the user
    already burned a real OTP from Telegram is exactly what caused the
    "stuck after entering code" bug: the exception had nowhere to go,
    the user was left in WAITING_CONNECT_STAGE limbo, and retrying
    requested a fresh code each time until Telegram's own flood
    protection kicked in. Checking the key up front costs nothing and
    means a misconfigured deployment fails with one clear message
    instead of a confusing multi-step dead end."""

    try:
        session_crypto._key_bytes()
    except session_crypto.SessionEncryptionNotConfigured as e:
        return (
            "⚠ Login isn't available right now - the server admin needs to "
            f"finish configuring it.\n\n({e})"
        )

    return None


async def _handle_connect_phone(message, user_id, phone_text):

    phone = phone_text.strip()

    if not phone.startswith("+") or not phone[1:].replace(" ", "").isdigit():
        await message.reply_text(
            "❌ Include the country code with a leading +, e.g. +919876543210. Try again."
        )
        return

    error = _connect_prerequisite_error()
    if error:
        WAITING_CONNECT_PHONE.pop(user_id, None)
        await message.reply_text(error, reply_markup=pre_login_menu)
        return

    WAITING_CONNECT_PHONE.pop(user_id, None)

    await message.reply_text("⏳ Sending you a login code on Telegram...")

    try:
        await user_sessions.start_connect(user_id, phone)

    except user_sessions.ConnectError as e:
        await message.reply_text(f"❌ {e}", reply_markup=pre_login_menu)
        return

    except Exception:
        # Safety net: any unexpected failure here must not leave the
        # user in a stuck waiting-state with no way back except /cancel.
        logger.exception("Unexpected error starting /connect for user %s", user_id)
        await message.reply_text(
            "❌ Something went wrong starting the login. Please try again.",
            reply_markup=pre_login_menu,
        )
        return

    _set_connect_stage(user_id, "code")

    await message.reply_text(
        "📩 Telegram sent your login code.\n\n"
        "Send it as:\n"
        "/mycode<code>\n\n"
        "Example: /mycode94563\n"
        "(or with a space: /mycode 94563)\n\n"
        "Never share this code with anyone, including anyone claiming "
        "to be ChannelFlow support."
    )


async def _handle_connect_code_or_password(message, user_id, text):
    """Text-route entry for the connect flow (menu_handler). Only runs
    while WAITING_CONNECT_STAGE is set for this user.

    code stage     - ONLY /mycode... (or legacy /myflow...) messages are
                     consumed; a bare numeric message is never treated
                     as a login code (PRD 5.3) and gets format help.
    password stage - the 2FA password is free text (it is not an OTP
                     and must never be logged or stored).
    """

    stage = WAITING_CONNECT_STAGE.get(user_id)

    if stage == "code":

        if not _is_otp_command(text):
            await message.reply_text(
                "📩 Send the login code from Telegram in this format:\n\n"
                "/mycode94563\n\n"
                "(or with a space: /mycode 94563)"
            )
            return

        await _consume_mycode_text(message, user_id, text)
        return

    if stage == "password":

        try:
            await user_sessions.submit_password(user_id, text)

        except user_sessions.ConnectConflict as e:
            _clear_connect_stage(user_id)
            user_sessions.cancel_connect(user_id)
            await message.reply_text(str(e), reply_markup=pre_login_menu)
            return

        except user_sessions.ConnectError as e:
            await message.reply_text(f"❌ {e}")
            return

        except Exception:
            logger.exception("Unexpected error finishing /connect for user %s", user_id)
            _clear_connect_stage(user_id)
            user_sessions.cancel_connect(user_id)
            await message.reply_text(
                "❌ Something went wrong finishing the login. Please send "
                "/connect <phone_number> to start over.",
                reply_markup=pre_login_menu,
            )
            return

        await _finish_connect_success(message, user_id)
        return

    return


async def _send_guide(message):

    await message.reply_text(
        "📖 Guide\n\n"
        "1. 🚀 Connect Now — log your Telegram account into the bot. "
        "Needed once; the bot uses this account to read your source "
        "channels and post into your destinations.\n\n"
        "2. ➕ New Project — name it, then choose Telegram → Telegram.\n\n"
        "3. Inside the project: add a 📥 Source (a channel/group you "
        "want to watch) and a 📤 Destination (where posts should go). "
        "Both accept a username, invite link, or numeric chat ID - "
        "private chats work too, as long as your connected account is "
        "already a member.\n\n"
        "4. Tune 🎛 Filters, 📝 Formatting, and ⚙ Forward Settings "
        "(delay, forward vs copy mode) to taste.\n\n"
        "5. Tap ▶ Start on the project. Use 🧪 Test to send a one-off "
        "test message to your destinations before going live.\n\n"
        "6. 📊 Stats and 📜 Logs on each project show what's happened "
        "so far.\n\n"
        "📦 My Plan shows your current limits and how much you're using."
    )


async def _send_tour(message):

    await message.reply_text(
        "🧭 Quick Tour\n\n"
        "ChannelFlow AI watches Telegram channels you choose and "
        "automatically forwards or copies new posts into other "
        "destinations.\n\n"
        "• Multiple sources → multiple destinations, per project\n"
        "• Forward mode (keeps the \"Forwarded from\" tag) or Copy mode "
        "(sends as new messages, so formatting/filters can apply)\n"
        "• Keyword, hashtag, domain, length, and sender filters\n"
        "• Text formatting: prefix/suffix, find-and-replace, remove "
        "patterns - URLs are always protected from accidental edits\n"
        "• Fixed or random delay between detection and forwarding\n"
        "• Automatic retry with backoff on Telegram rate limits\n"
        "• Per-project stats, activity logs, and a one-tap test send\n\n"
        "Tap 🚀 Connect Now when you're ready to set it up."
    )


# ==========================================
# /admin
# ==========================================

def _admin_dashboard_text(context=None):

    total_users = count_users()
    engine_state = "🟢 Online" if is_running() else "🔴 Offline"
    maintenance = False

    if context is not None:
        maintenance = context.bot_data.get("maintenance_mode", False)

    return (
        "🛠 Admin Dashboard\n\n"
        f"👥 Users: {total_users}\n"
        f"🚀 Forward Engine: {engine_state}\n"
        f"🔧 Maintenance Mode: {'On' if maintenance else 'Off'}"
    )


async def admin_panel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    if user.id not in ADMIN_IDS:
        await update.message.reply_text(i18n.t(user.id, "error.generic_forward"), reply_markup=admin_keyboard)
        return

    await update.message.reply_text(
        _admin_dashboard_text(context),
        reply_markup=admin_keyboard
    )


# ==========================================
# /setplan (Phase 8) - admin-only, manual plan assignment.
# No payment processing yet (Phase 14), so this is how a plan actually
# gets changed for now: /setplan <telegram_id> <FREE|BEGINNER|PRO|MAX>
# ==========================================

# ---------------------------------------------------------------------------
# Legacy/admin command compatibility handlers
# These commands are registered by main.py and intentionally use the same
# ADMIN_IDS gate as the current admin panel.
# ---------------------------------------------------------------------------

async def setprice_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Admins only.")
        return
    args = context.args
    if len(args) != 2:
        await update.message.reply_text("Usage: /setprice <PLAN> <price_inr>\nExample: /setprice PRO 399")
        return
    plan, raw = args[0].upper(), args[1]
    if plan not in ("BEGINNER", "PRO", "CREATOR"):
        await update.message.reply_text("❌ Invalid plan. Choose: BEGINNER, PRO, CREATOR")
        return
    try:
        price = float(raw)
    except ValueError:
        await update.message.reply_text("❌ Price must be a number.")
        return
    conn = get_connection(); cur = conn.cursor()
    cur.execute("UPDATE plan_configs SET monthly_price_inr=?, updated_at=CURRENT_TIMESTAMP WHERE plan_name=?", (price, plan))
    conn.commit(); conn.close()
    if hasattr(plan_service, "invalidate_plan_configs_cache"):
        plan_service.invalidate_plan_configs_cache()
    await update.message.reply_text(f"✅ Price updated\n\n📦 {plan}\n💰 ₹{price:.0f}/month")


async def setduration_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Admins only.")
        return
    args = context.args
    if len(args) not in (3, 4):
        await update.message.reply_text("Usage: /setduration <PLAN> <months> <discount%> [fixed_price]")
        return
    plan = args[0].upper()
    if plan not in ("BEGINNER", "PRO", "CREATOR"):
        await update.message.reply_text("❌ Invalid plan. Choose: BEGINNER, PRO, CREATOR")
        return
    try:
        months, discount = int(args[1]), float(args[2])
        fixed = float(args[3]) if len(args) == 4 else None
    except ValueError:
        await update.message.reply_text("❌ Months, discount and fixed price must be numbers.")
        return
    if months <= 0 or not 0 <= discount <= 100:
        await update.message.reply_text("❌ Months must be > 0; discount between 0-100.")
        return
    pricing_service.set_duration(plan, months, discount, fixed)
    await update.message.reply_text(f"✅ Duration saved\n\n📦 {plan} · {months} mo")


async def planconfig_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Admins only.")
        return
    configs = plan_service.get_cached_plan_configs()
    lines = ["🧩 Plan Configuration\n"]
    for plan_name in plan_service.VALID_PLANS:
        cfg = configs.get(plan_name)
        if not cfg:
            continue
        lines.append(f"━━━ {cfg.get('display_name', plan_name)} ({plan_name}) ━━━")
        lines.append(f"💰 Monthly: ₹{cfg['monthly_price_inr']:.0f}")
        lines.append(f"📁 Projects: {cfg['max_projects']}")
        lines.append(f"📡 Sources/project: {cfg['max_sources_per_project']}")
        lines.append(f"📤 Destinations/project: {cfg['max_destinations_per_project']}")
        lines.append(f"⚡ Daily forwards: {cfg['daily_forward_limit']}")
        lines.append("")
    await update.message.reply_text("\n".join(lines))


async def payments_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Admins only.")
        return
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT pr.*, u.username, u.first_name FROM payment_requests pr LEFT JOIN users u ON u.telegram_id = pr.user_id WHERE pr.status = 'SUBMITTED' ORDER BY pr.id ASC LIMIT 10")
    rows = cur.fetchall(); conn.close()
    if not rows:
        await update.message.reply_text("✅ No submitted payments waiting for review.")
        return
    lines = [f"💳 Payments Awaiting Review ({len(rows)})\n"]
    buttons = []
    for r in rows:
        name = r["first_name"] or ""
        uname = f"@{r['username']}" if r["username"] else "no username"
        lines.append(f"#{r['id']} · {name} {uname} (ID {r['user_id']})")
        lines.append(f"   {r['plan']} × {r['months']}mo — ₹{r['amount_inr']:.0f} via {r['method']}")
        buttons.append([InlineKeyboardButton(f"✅ #{r['id']}", callback_data=f"upgrade:approve:{r['id']}"), InlineKeyboardButton(f"❌ #{r['id']}", callback_data=f"upgrade:reject:{r['id']}")])
    await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))


async def walletadjust_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Admins only.")
        return
    args = context.args
    if len(args) < 3 or args[0] not in ("credit", "debit"):
        await update.message.reply_text("Usage: /walletadjust <credit|debit> <telegram_id> <amount_inr>")
        return
    if not args[1].isdigit():
        await update.message.reply_text("❌ telegram_id must be numeric."); return
    try:
        target, amount = int(args[1]), float(args[2])
    except ValueError:
        await update.message.reply_text("❌ Invalid amount."); return
    if amount <= 0:
        await update.message.reply_text("❌ Amount must be positive."); return
    if args[0] == "credit":
        wallet_service.credit(target, amount, reference=f"admin_credit:{user.id}")
        result = "credited"
    else:
        if not wallet_service.debit(target, amount, reference=f"admin_debit:{user.id}"):
            await update.message.reply_text("❌ Debit failed (insufficient balance or invalid)."); return
        result = "debited"
    balance = wallet_service.get_balance_inr(target)
    await update.message.reply_text(f"✅ Wallet {result} for user {target}.\nNew balance: ₹{balance:.2f}")


async def creditsadjust_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin grant/revoke of Extra Credits with reason + audit record
    (PRD 23.3). Usage:
        /creditsadjust credit <telegram_id> <amount> [reason...]
        /creditsadjust debit <telegram_id> <amount> [reason...]
    """
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Admins only.")
        return
    args = context.args or []
    if len(args) < 3 or args[0] not in ("credit", "debit"):
        await update.message.reply_text(
            "Usage: /creditsadjust <credit|debit> <telegram_id> <amount> [reason]")
        return
    if not args[1].isdigit():
        await update.message.reply_text("❌ telegram_id must be numeric.")
        return
    try:
        target = int(args[1])
        amount = int(args[2])
    except ValueError:
        await update.message.reply_text("❌ Amount must be a whole number.")
        return
    if amount <= 0:
        await update.message.reply_text("❌ Amount must be positive.")
        return
    reason = " ".join(args[3:]) or ("admin grant" if args[0] == "credit" else "admin revoke")
    delta = amount if args[0] == "credit" else -amount
    if not extra_credits_service.admin_adjust(target, delta, reason, admin_id=user.id):
        await update.message.reply_text(
            f"❌ {args[0].title()} failed (not enough balance, or user does "
            "not exist). Nothing changed.")
        return
    balance = extra_credits_service.get_balance(target)
    await update.message.reply_text(
        f"✅ {args[0].title()} of {amount} applied to user {target}.\n"
        f"New balance: ⚡ {balance:,} forwards")
    logger.info("Extra credits adjusted: admin=%s target=%s delta=%s reason=%r",
                user.id, target, delta, reason)


async def setplan_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    if user.id not in ADMIN_IDS:
        await update.message.reply_text(i18n.t(user.id, "error.generic_forward"), reply_markup=admin_keyboard)
        return

    args = context.args

    if len(args) != 2:
        await update.message.reply_text(
            "Usage: /setplan <telegram_id> <FREE|BEGINNER|PRO|MAX>"
        )
        return

    target_id_raw, plan = args

    if not target_id_raw.isdigit():
        await update.message.reply_text("❌ telegram_id must be numeric.")
        return

    target_id = int(target_id_raw)
    plan = plan.upper()

    if plan not in plan_service.VALID_PLANS:
        await update.message.reply_text(
            f"❌ Invalid plan. Choose one of: {', '.join(plan_service.VALID_PLANS)}"
        )
        return

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM users WHERE telegram_id=?", (target_id,))
    exists = cur.fetchone() is not None
    conn.close()

    if not exists:
        await update.message.reply_text("❌ No such user (they must have used /start at least once).")
        return

    plan_service.set_user_plan(target_id, plan)

    await update.message.reply_text(
        f"✅ Plan updated\n\n👤 {target_id}\n📦 {plan}"
    )


# ==========================================
# /cancel
# ==========================================

async def cancel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    had_connect_state = (
        user.id in WAITING_CONNECT_PHONE
        or user.id in WAITING_CONNECT_STAGE
    )

    had_state = (
        user.id in WAITING_PROJECT_NAME
        or user.id in WAITING_SOURCE
        or user.id in WAITING_DESTINATION
        or user.id in WAITING_RENAME
    )

    _reset_waiting_states(user.id)

    if had_connect_state:
        # Fully tear down any pending Telethon login client - popping
        # the dict alone would leave an authenticated client connected
        # in memory until the next sweep.
        await user_sessions.cancel_connect_async(user.id)

    if had_state or had_connect_state:

        await update.message.reply_text(
            "❌ Cancelled\n\n"
            "The pending action was cancelled.",
            reply_markup=_reply_menu_for(user.id)
        )

    else:

        await update.message.reply_text(
            "ℹ Nothing to cancel.",
            reply_markup=_reply_menu_for(user.id)
        )


# ==========================================
# MAIN MENU (REPLY KEYBOARD) HANDLER
# ==========================================

async def menu_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user
    message = update.message

    if message is None or not message.text:
        return

    text = message.text.strip()

    register_user(
        user.id,
        user.username,
        user.first_name
    )

    # ======================================
    # UNCONNECTED MENU (UX-NAV-01 92.2) + legacy pre-login menu.
    # Available regardless of connection status (Connect is how a user
    # gets past this gate; How It Works / Why Connect / Subscription /
    # Support make sense before connecting too).
    # ======================================

    if text in (BTN_CONNECT_NOW, MB_CONNECT_ACCOUNT):
        _reset_waiting_states(user.id)
        await _start_connect_flow(message, user.id)
        return

    if text == MB_WHY_CONNECT:
        await message.reply_text(i18n.t(user.id, "nav.why_connect"),
                                 reply_markup=_reply_menu_for(user.id))
        return

    if text == MB_SUBSCRIPTION_PLAN:
        await _send_plan_screen(message, user.id, edit=False)
        return

    if text in (BTN_GUIDE, MB_HOW_IT_WORKS):
        await _send_guide(message)
        return

    if text == BTN_TOUR:
        await _send_tour(message)
        return

    if text == MB_SUPPORT:
        await _send_support_hub(message, user.id, edit=False)
        return

    if text in (MB_HOME,):
        # Legacy "🏠 Home" reply-menu button (old keyboards): safe
        # account-state Home.
        await _go_home(message, user)
        return

    # ======================================
    # MAIN MENU NAVIGATION - handled BEFORE any waiting-state capture
    # so a menu press always wins (UX-NAV-01/02: menus are escape
    # hatches; stale connected-menu presses from an unconnected user
    # hit the login gate below instead of being swallowed).
    # ======================================

    if text in CONNECTED_MENU_LABELS:

        # LOGIN GATE (see the full explanation further down; the
        # authenticated menu is only valid for connected accounts).
        if user.id not in ADMIN_IDS and not user_sessions.is_connected(user.id):
            await message.reply_text(
                "🔒 Connect your Telegram account first.",
                reply_markup=pre_login_menu
            )
            return

        _reset_waiting_states(user.id)

        if text in (BTN_NEW_PROJECT,):
            # Legacy creation entry; the task hub uses the ➕ New Task
            # inline button (callback newproj) which reaches the same
            # name-capture step below.
            WAITING_PROJECT_NAME[user.id] = True
            await message.reply_text("📝 Send Project Name")
            return

        if text in (BTN_MY_PROJECTS, MB_PROJECTS):
            await _show_task_list(message, user.id)
            return

        if text == BTN_STATUS:
            await _send_status(message, user.id)
            return

        if text in (BTN_MY_PLAN, MB_SUBSCRIPTION):
            await _send_plan_screen(message, user.id)
            return

        if text in (BTN_SETTINGS, MB_SETTINGS):
            await _send_settings_screen(message, user.id)
            return

        if text == MB_REWARDS:
            await _show_rewards(message, context, user.id)
            return

        if text == MB_ACCOUNT:
            await _send_account_card(message, user)
            return

        if text == MB_HOME:
            await _go_home(message, user)
            return

    # ======================================
    # CONNECT FLOW (phone / code / password capture)
    # Must be handled before the login gate below, since the whole
    # point is capturing input from a user who ISN'T connected yet.
    # ======================================

    if user.id in WAITING_CONNECT_PHONE:
        await _handle_connect_phone(message, user.id, text)
        return

    if user.id in WAITING_CONNECT_STAGE:
        await _handle_connect_code_or_password(message, user.id, text)
        return

    # ======================================
    # SUPPORT HUB TEXT INPUTS (available pre-login too)
    # ======================================

    if user.id in WAITING_SUPPORT_AI:
        await _handle_support_ai_message(message, user.id, text)
        return

    if user.id in WAITING_TICKET_SUBJECT:
        await _handle_ticket_subject(message, user.id, text)
        return

    if user.id in WAITING_TICKET_MESSAGE:
        await _handle_ticket_message(message, user.id, text)
        return

    if user.id in WAITING_TICKET_REPLY:
        await _handle_ticket_reply(message, user.id, text)
        return

    if user.id in WAITING_FEEDBACK:
        await _handle_feedback_message(message, user.id, text)
        return

    # ======================================
    # LOGIN GATE
    # Everything below this point is project/settings functionality -
    # not reachable until the user has connected their own Telegram
    # account. pre_login_menu doesn't show these buttons at all, but a
    # user could still type them, or have main_menu cached client-side
    # from before a /disconnect - so the gate is enforced here too, not
    # just by which keyboard is shown.
    #
    # ADMIN_IDS are exempt: their account already powers the shared
    # session (core/client.py, authorized once via `python -m
    # core.authorize` on the server) that every not-yet-connected
    # user's projects run on anyway - making them go through /connect
    # too, in-chat, for the same account would be pure friction with no
    # security benefit, and was the "why is it still asking me to
    # connect after I already logged in via the terminal" bug.
    # ======================================

    if user.id not in ADMIN_IDS and not user_sessions.is_connected(user.id):

        await message.reply_text(
            "🔒 Connect your Telegram account first.",
            reply_markup=pre_login_menu
        )

        return

    # ======================================
    # TASK SEARCH TERM (Batch 4 / UX-NAV-03)
    # ======================================

    if user.id in WAITING_TASK_SEARCH:
        WAITING_TASK_SEARCH.pop(user.id, None)
        term = text.strip()
        _TASK_PAGE.pop(user.id, None)
        if term:
            _TASK_TERM[user.id] = term
        else:
            _TASK_TERM.pop(user.id, None)
        await _show_task_list(message, user.id, edit=False)
        return

    # ======================================
    # CREATE PROJECT
    # ======================================

    if user.id in WAITING_PROJECT_NAME:
        await _handle_create_project(message, user.id, text)
        return

    # ======================================
    # RENAME PROJECT
    # ======================================

    if user.id in WAITING_RENAME:
        await _handle_rename_project(message, user.id, text)
        return

    # ======================================
    # ADD SOURCE
    # ======================================

    if user.id in WAITING_SOURCE:
        await _handle_add_source(message, user.id, text)
        return

    # ======================================
    # ADD DESTINATION
    # ======================================

    if user.id in WAITING_DESTINATION:
        await _handle_add_destination(message, user.id, text)
        return

    # ======================================
    # INSTAGRAM: PROCESSING CHANNEL / DESTINATION SETUP
    # ======================================

    if user.id in WAITING_PROCESSING_CHANNEL:
        await _handle_set_processing_channel(message, user.id, text)
        return

    if user.id in WAITING_INSTAGRAM_DESTINATION:
        await _handle_add_instagram_destination(message, user.id, text)
        return

    # ======================================
    # SET DELAY
    # ======================================

    if user.id in WAITING_DELAY:
        await _handle_set_delay(message, user.id, text)
        return

    # ======================================
    # SET WHITELIST
    # ======================================

    if user.id in WAITING_WHITELIST:
        await _handle_set_whitelist(message, user.id, text)
        return

    # ======================================
    # SET BLACKLIST
    # ======================================

    if user.id in WAITING_BLACKLIST:
        await _handle_set_blacklist(message, user.id, text)
        return

    # ======================================
    # SET REGEX
    # ======================================

    if user.id in WAITING_REGEX:
        await _handle_set_regex(message, user.id, text)
        return

    # ======================================
    # CONTENT RULES / FORMATTING (Phase 7)
    # ======================================

    if user.id in WAITING_CONTENT_RULE_FIELD:
        await _handle_content_rule_field(message, user.id, text)
        return

    if user.id in WAITING_FORMATTING_FIELD:
        await _handle_formatting_field(message, user.id, text)
        return

    if user.id in WAITING_REPLACE_RULE:
        await _handle_replace_rule_input(message, user.id, text)
        return

    if user.id in WAITING_REMOVE_PATTERN:
        await _handle_remove_pattern_input(message, user.id, text)
        return

    # ======================================
    # ADMIN BROADCAST (existing: DM announcement to all bot users)
    # ======================================

    if user.id in WAITING_BROADCAST and user.id in ADMIN_IDS:
        await _handle_broadcast(message, context, user.id, text)
        return

    # ======================================
    # ADMIN /post, /broadcast (new: promotional post to project
    # destinations - a different feature from the announcement above,
    # see bot/admin_promo_handlers.py's module docstring)
    # ======================================

    if user.id in WAITING_PROMO and user.id in ADMIN_IDS:
        await handle_promo_text(message, context, user.id, text)
        return

    # ======================================
    # FALLBACK
    # ======================================

    await message.reply_text(
        "❓ I didn't understand that.\n\n"
        "Please use the menu below.",
        reply_markup=_reply_menu_for(user.id)
    )


# ==========================================
# MENU BRANCH IMPLEMENTATIONS
# ==========================================

# ==========================================
# UX-NAV-02: TASK-FIRST SCREENS
# ==========================================

def _task_status_label(project):
    """'🟢 Running' / '⚪ Stopped' - shared by list and detail views."""
    return "🟢 Running" if project["status"] else "⚪ Stopped"


def _task_summary_text(project):
    """Compact Task-Details card (UX-NAV-02 93.2): status + route
    summary. Advanced configuration lives behind ⚙️ Edit Task."""
    sources_total = count_sources(project["id"])
    destinations_total = count_destinations(project["id"])
    if "platform_type" in project.keys():
        platform_type = project["platform_type"] or "telegram"
    else:
        platform_type = "telegram"
    return i18n.t(
        project["user_id"], "task.details",
        name=project["name"],
        status=_task_status_label(project),
        route=_platform_label(platform_type),
        sources=sources_total,
        destinations=destinations_total,
    )


def _task_list_payload(user_id):
    """(text, markup, page_count) for the user's task list honouring
    the per-user browse state: optional search term + pagination
    (UX-NAV-03). Pure helper used by both the initial render and the
    after-delete refresh so both stay consistent."""
    all_projects = get_projects(user_id)
    allowed, _reason = plan_service.can_create_project(user_id)

    term = _TASK_TERM.get(user_id)
    if term:
        needle = term.lower()
        projects = [p for p in all_projects if needle in (p["name"] or "").lower()]
    else:
        projects = all_projects

    total = len(projects)
    page_count = max(1, -(-total // _TASK_PAGE_SIZE))
    page = max(1, min(_TASK_PAGE.get(user_id, 1), page_count))
    start = (page - 1) * _TASK_PAGE_SIZE
    visible = projects[start:start + _TASK_PAGE_SIZE]

    if term and not total:
        text = f"🔍 No tasks match “{term}”.\n\nClear the search to see all {len(all_projects)} task(s)."
        markup = task_list_keyboard([], can_create=allowed, page=page, pages=1,
                                    search_term=term, can_search=False)
    elif not term and not all_projects:
        text = i18n.t(user_id, "nav.tasks_empty")
        markup = task_list_keyboard([], can_create=allowed, can_search=False)
    else:
        header = f"🔍 “{term}” - {total} match(es)\n\n" if term else ""
        if page_count > 1:
            header += f"{i18n.t(user_id, 'nav.your_tasks')} (page {page}/{page_count})\n"
        else:
            header += i18n.t(user_id, "nav.your_tasks")
        text = header
        markup = task_list_keyboard(visible, can_create=allowed, page=page,
                                    pages=page_count, search_term=term,
                                    can_search=bool(all_projects))

    return text, markup, page_count


async def _show_task_list(message, user_id, edit=False):
    """📁 Projects = the task list FIRST (UX-NAV-02 93.1), with an
    empty state (Create Your First Task) when there are zero tasks,
    and pagination + search for longer lists (Batch 4 / UX-NAV-03).
    Rendered through nav_state.place so repeated navigation replaces
    the previous bot screen message instead of stacking duplicates."""
    text, markup, _pages = _task_list_payload(user_id)
    await nav_state.place(message, user_id, "tasks", text, reply_markup=markup, edit=edit)


async def _show_task_detail(message, project_id, user_id, edit=False, notice=None):
    """Task Details card (compact). ``notice`` prefixes an optional
    one-line confirmation (Task started / stopped / deleted)."""
    project = _get_owned_project(project_id, user_id)
    if project is None:
        # Ownership or existence failed (deleted / other user): safe
        # "screen expired" answer with a way Home (UX-NAV-02 93.4).
        text = "⚠️ " + i18n.t(user_id, "nav.screen_expired")
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Home", callback_data="nav:home")]])
        if edit:
            try:
                await message.edit_text(text, reply_markup=markup)
            except Exception:
                await message.reply_text(text, reply_markup=markup)
        else:
            await message.reply_text(text, reply_markup=markup)
        return None

    body = _task_summary_text(project)
    text = f"{notice}\n\n{body}" if notice else body
    running = bool(project["status"])
    platform_type = project["platform_type"] if "platform_type" in project.keys() else "telegram"
    markup = task_detail_keyboard(project_id, running=running, platform_type=platform_type)
    return await nav_state.place(
        message, user_id, f"task_detail:{project_id}", text, reply_markup=markup, edit=edit,
        data={"project_id": project_id},
    )


async def _show_edit_task(message, project_id, user_id, edit=False):
    """⚙️ Edit Task root: the task's configuration grouped by area,
    each with live handlers only (UX-NAV-02 93.2 / 92.5)."""
    project = _get_owned_project(project_id, user_id)
    if project is None:
        await _show_task_detail(message, project_id, user_id, edit=edit)
        return

    sources_total = count_sources(project["id"])
    destinations_total = count_destinations(project["id"])
    platform_type = project["platform_type"] if "platform_type" in project.keys() else "telegram"

    text = (
        f"⚙️ Edit Task — {project['name']}\n\n"
        f"Status: {_task_status_label(project)}\n"
        f"Route: {_platform_label(platform_type)}\n"
        f"Sources: {sources_total}\n"
        f"Destinations: {destinations_total}\n\n"
        "Choose an area to configure:"
    )
    markup = edit_project_keyboard(project_id, platform_type=platform_type)
    return await nav_state.place(
        message, user_id, f"edit_task:{project_id}", text, reply_markup=markup, edit=edit,
        data={"project_id": project_id},
    )


async def _confirm_delete_task(message, project_id, user_id, edit=False):
    """Destructive confirmation before deleting a task (UX-NAV-02
    93.2). Delete always requires this confirmation step."""
    project = _get_owned_project(project_id, user_id)
    if project is None:
        await _show_task_detail(message, project_id, user_id, edit=edit)
        return

    text = i18n.t(user_id, "task.delete_confirm", name=project["name"])
    markup = delete_confirm_keyboard(f"delete:{project_id}", f"projcard:{project_id}")
    return await nav_state.place(
        message, user_id, f"delete_confirm:{project_id}", text, reply_markup=markup, edit=edit,
        data={"project_id": project_id},
    )


# Legacy alias kept for older call sites / tests.
_send_my_projects = _show_task_list


# ==========================================
# PLAN-LOCKED STATE (Batch 4 / UX-NAV-04)
# ==========================================
# A gate that fails (project/source/destination cap, feature not on
# the plan) renders a real screen: what the limit is, why, and a
# working ⬆️ Upgrade Plan CTA that drops into the live upgrade chain -
# instead of a bare "locked" toast with no way forward.

def _locked_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬆️ Upgrade Plan", callback_data="acct:upgrade")],
        [InlineKeyboardButton("🏠 Home", callback_data="nav:home")],
    ])


def _locked_text(reason):
    return f"🔒 Plan limit\n\n{reason}\n\nUpgrade to lift this limit - plans activate instantly."


def _payment_request_line(r):
    """One-line human description of a payment_request row across all
    methods/purposes (plan, wallet top-up, extra-credit package)."""
    purpose = r["purpose"] or "plan"
    if purpose == "wallet_topup":
        what = "💰 Wallet top-up"
    elif purpose == "extra_credit":
        n = int(r["extra_forwards"] or 0)
        what = f"⚡ Extra Credits ({n:,} forwards)" if n else "⚡ Extra Credits"
    else:
        plan = r["plan"] or "?"
        months = r["months"]
        what = f"{plan}" + (f" × {months}mo" if months else "")
    cur = r["currency"] or "INR"
    if cur == "STARS":
        amount = f"⭐{int(float(r['final_amount'] or 0))}"
    elif cur == "USD":
        amount = f"${float(r['final_amount'] or r['amount_usd'] or 0):.2f}"
    else:
        amount = f"₹{float(r['final_amount'] or r['amount_inr'] or 0):.0f}"
    return f"{what} — {amount} via {r['method']}"


_STATUS_ICON = {
    "PENDING_PAYMENT": "⏳", "SUBMITTED": "🕓", "SUCCESS": "✅",
    "APPROVED": "✅", "REJECTED": "❌", "CANCELLED": "🚫",
    "EXPIRED": "⌛", "DETECTING": "🔄", "CONFIRMING": "🔄", "FAILED": "❌",
}


def _payment_status_label(status):
    return f"{_STATUS_ICON.get(status, '•')} {status.replace('_', ' ').title()}"


async def _send_payment_history(message, user_id, edit=False):
    """📜 Payment history for the user across every method (Batch 4 /
    PRD 24 History + 25.2 + 26 payment history). Read-only over
    payment_requests; never shows provider secrets."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM payment_requests WHERE user_id=? ORDER BY id DESC LIMIT 10",
        (user_id,),
    )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        text = "📜 Payment History\n\nNo payments yet. Your plan upgrades and extra-credit purchases will appear here."
    else:
        lines = ["📜 Payment History\n"]
        for r in rows:
            lines.append(f"{_payment_request_line(r)}")
            lines.append(f"   {_payment_status_label(r['status'])} · #{r['id']} · {r['created_at'] or ''}")
        lines.append("\nShowing the latest 10.")
        text = "\n".join(lines)

    markup = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⬅ Account", callback_data="nav:account"),
            InlineKeyboardButton("🛒 Extra Credits", callback_data="credits:home"),
        ],
        [InlineKeyboardButton("🏠 Home", callback_data="nav:home")],
    ])
    return await nav_state.place(message, user_id, "pay_history", text,
                                 reply_markup=markup, edit=edit)


def _plan_limits_text(entitlements):
    def _fmt(v):
        return "Unlimited" if v is None else str(v)

    lines = [
        f"📦 Plan: {entitlements['plan']}",
        "",
        f"📂 Projects: {_fmt(entitlements['max_projects'])}",
        f"📡 Sources per project: {_fmt(entitlements['max_sources_per_project'])}",
        f"🎯 Destinations per project: {_fmt(entitlements['max_destinations_per_project'])}",
        f"📈 Daily forwards per project: {_fmt(entitlements['daily_forward_limit'])}",
        f"🏷 Attribution footer: {'Required' if entitlements['requires_attribution'] else 'Not required'}",
    ]
    return "\n".join(lines)


async def _send_plan_screen(message, user_id, edit=False):
    """💳 Plan & Billing card. Unconnected users see the plan catalog
    with a Connect CTA; connected users see their entitlements plus
    live upgrade/wallet/rewards actions. Telegram Stars is mentioned
    as text only - checkout is not implemented, so no dead Stars
    buttons are ever rendered (UX-NAV-01 92.5 companion defect)."""
    entitlements = plan_service.get_entitlements(user_id)
    connected = user_id in ADMIN_IDS or user_sessions.is_connected(user_id)
    projects = get_projects(user_id)

    if not connected:
        # Plan catalog for prospective users (unconnected state).
        lines = ["💎 Subscription Plans\n"]
        configs = plan_service.get_cached_plan_configs()
        for plan_name in plan_service.VALID_PLANS:
            cfg = configs.get(plan_name)
            if not cfg:
                continue
            inr = cfg.get("monthly_price_inr")
            usd = plan_service.get_plan_crypto_price_usd(plan_name)
            price_line = f"₹{inr:.0f}/mo" if inr else ("$" + f"{usd:.2f}/mo" if usd else "—")
            lines.append(f"• {cfg.get('display_name', plan_name)} — {price_line}")
        lines.append("")
        lines.append("⭐ Telegram Stars, UPI, and crypto are all available - "
                     "connect your Telegram account first, then upgrade "
                     "instantly with Stars from the Plan screen.")
        lines.append("")
        lines.append("Connect your Telegram account first, then upgrade from here.")
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("🚀 Connect Account", callback_data="acct:connect")],
            [InlineKeyboardButton("🏠 Home", callback_data="nav:home")],
        ])
        return await nav_state.place(message, user_id, "plan", "\n".join(lines),
                                     reply_markup=markup, edit=edit)

    used_projects = len(projects)
    daily_cap = entitlements.get("daily_forward_limit")
    credits = extra_credits_service.get_balance(user_id)
    text = (
        f"{_plan_limits_text(entitlements)}\n"
        f"\n"
        f"Using: {used_projects} / {_fmt_limit(entitlements['max_projects'])} projects\n"
        f"📈 Today: {_today_global_usage(user_id)} / {_fmt_limit(daily_cap)} forwards\n"
        f"⚡ Extra Credits: {credits:,} (used only after the daily allowance)"
    )
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬆️ Upgrade Plan", callback_data="acct:upgrade")],
        [
            InlineKeyboardButton("🛒 Extra Credits", callback_data="credits:home"),
            InlineKeyboardButton("📜 History", callback_data="acct:history"),
        ],
        [
            InlineKeyboardButton("💰 Wallet", callback_data="acct:wallet"),
            InlineKeyboardButton("👥 Rewards", callback_data="acct:earn"),
        ],
        [
            InlineKeyboardButton("🏠 Home", callback_data="nav:home"),
            InlineKeyboardButton("⬅ Account", callback_data="nav:account"),
        ],
    ])
    return await nav_state.place(message, user_id, "plan", text, reply_markup=markup, edit=edit)


def _fmt_limit(v):
    return "Unlimited" if v is None else str(v)


async def _send_my_plan(message, user_id):
    return await _send_plan_screen(message, user_id, edit=False)


async def _send_settings_screen(message, user_id, edit=False):
    """⚙️ Settings hub (canonical; live rows only)."""
    text = "⚙ ChannelFlow Settings"
    markup = settings_keyboard(wallet_service.is_auto_renew_enabled(user_id))
    return await nav_state.place(message, user_id, "settings", text, reply_markup=markup, edit=edit)


async def _show_rewards(message, context, user_id, edit=False):
    """🎁 Rewards (referrals) screen - real link, real stats."""
    bot_username = context.bot.username
    code = referral_service.build_referral_code(user_id)
    link = f"https://t.me/{bot_username}?start={code}"
    stats = referral_service.get_referral_stats(user_id)

    text = (
        "🎁 Rewards\n\n"
        "Share your link. When someone joins through it and upgrades "
        f"to PRO, you get +{referral_service.REFERRAL_REWARD_DAYS} days of PRO "
        "- for every person who does, stacking.\n\n"
        f"{link}\n\n"
        f"Total invited: {stats['total_invited']}\n"
        f"Rewarded so far: {stats['active_referrals']}"
    )
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Share Link", url=link)],
        [
            InlineKeyboardButton("🏠 Home", callback_data="nav:home"),
            InlineKeyboardButton("⬅ Account", callback_data="nav:account"),
        ],
    ])
    return await nav_state.place(message, user_id, "rewards", text, reply_markup=markup, edit=edit)


async def _send_support_hub(message, user_id, edit=False, is_creator=False):
    """🆘 Support hub (available pre- and post-login)."""
    text = i18n.t(user_id, "support.why_connect_hint")
    markup = support_section_keyboard(user_id=user_id, is_creator=is_creator)
    return await nav_state.place(message, user_id, "support", text, reply_markup=markup, edit=edit)


async def _send_status(message, user_id):

    projects = get_projects(user_id)

    total = count_projects(user_id)
    running = sum(1 for project in projects if project["status"])

    engine_state = "🟢 Online" if is_running() else "🔴 Offline"

    await message.reply_text(

        "📊 ChannelFlow AI\n\n"
        f"📁 Projects : {total}\n"
        f"▶ Running : {running}\n"
        f"⏹ Stopped : {total - running}\n\n"
        f"⚙ Forward Engine : {engine_state}"

    )


async def _handle_create_project(message, user_id, text):

    if not text:

        await message.reply_text(
            "❌ Project name cannot be empty. Send a valid name."
        )

        return

    if len(text) > MAX_NAME_LENGTH:

        await message.reply_text(
            f"❌ Project name is too long (max {MAX_NAME_LENGTH} characters)."
        )

        return

    allowed, reason = plan_service.can_create_project(user_id)

    if not allowed:
        WAITING_PROJECT_NAME.pop(user_id, None)
        await message.reply_text(_locked_text(reason), reply_markup=_locked_markup())
        return

    WAITING_PROJECT_NAME.pop(user_id, None)
    PENDING_PROJECT_NAME[user_id] = text

    await message.reply_text(
        f"📂 {text}\n\n"
        "Choose what this project connects to:",
        reply_markup=platform_selection_keyboard(),
    )


async def _handle_platform_selected(message, user_id, platform_type):

    name = PENDING_PROJECT_NAME.pop(user_id, None)

    if not name:
        await message.reply_text(
            "⚠️ No project name in progress. Tap ➕ New Project to start again.",
            reply_markup=main_menu,
        )
        return

    project_id = create_project(user_id, name, platform_type=platform_type)

    CURRENT_PROJECT[user_id] = project_id

    project = get_project(project_id)
    platform_meta = get_platform(platform_type) or {}

    await message.reply_text(

        "✅ Project Created\n\n"
        f"📂 {project['name']}\n"
        f"{platform_meta.get('icon', '')} {_platform_label(project['platform_type'])}",

        reply_markup=project_keyboard(project_id, platform_type=project["platform_type"])

    )


async def _handle_rename_project(message, user_id, text):

    project_id = CURRENT_PROJECT.get(user_id)
    project = _get_owned_project(project_id, user_id) if project_id else None

    if project is None:

        WAITING_RENAME.pop(user_id, None)

        await message.reply_text(
            "❌ No project selected. Open a project and tap ✏ Rename again.",
            reply_markup=main_menu
        )

        return

    if not text:

        await message.reply_text(
            "❌ Project name cannot be empty. Send a valid name."
        )

        return

    if len(text) > MAX_NAME_LENGTH:

        await message.reply_text(
            f"❌ Project name is too long (max {MAX_NAME_LENGTH} characters)."
        )

        return

    rename_project(project_id, text)

    WAITING_RENAME.pop(user_id, None)

    project = get_project(project_id)

    await message.reply_text(

        "✅ Project Renamed\n\n"
        f"📂 {project['name']}",

        reply_markup=project_keyboard(project_id, platform_type=project["platform_type"])

    )


async def _handle_add_source(message, user_id, text):

    project_id = CURRENT_PROJECT.get(user_id)
    project = _get_owned_project(project_id, user_id) if project_id else None

    if project is None:

        WAITING_SOURCE.pop(user_id, None)

        await message.reply_text(
            "❌ No project selected. Open a project and tap ➕ Source again.",
            reply_markup=main_menu
        )

        return

    allowed, reason = plan_service.can_add_source(user_id, project_id)

    if not allowed:
        WAITING_SOURCE.pop(user_id, None)
        await message.reply_text(
            _locked_text(reason),
            reply_markup=_locked_markup()
        )
        return

    try:
        chat = await get_chat(text)
    except Exception as e:
        logger.exception("get_chat failed while adding source: %s", e)
        await message.reply_text(f"❌ Couldn't add this source\n\n{e}")
        return

    if chat is None:

        await message.reply_text(
            "❌ Invalid Channel / Group / Bot\n\n"
            "Send a valid public username, e.g. @YourChannel"
        )

        return

    ok = add_source(
        project_id,
        chat["chat_id"],
        chat["username"],
        chat["title"],
        chat["type"]
    )

    WAITING_SOURCE.pop(user_id, None)

    if ok:

        await force_refresh_routes()

        text = (
            "✅ Source Added\n\n"
            f"📂 {chat['title'] or chat['username'] or chat['chat_id']}\n"
            f"🏷 {chat['type']}"
        )

        if not chat.get("joined", True):
            text += f"\n\n⚠ {chat['join_note']}"

        await message.reply_text(
            text,
            reply_markup=project_keyboard(project_id, platform_type=project["platform_type"])
        )

    else:

        await message.reply_text(
            "⚠ Source Already Exists",
            reply_markup=project_keyboard(project_id, platform_type=project["platform_type"])
        )


async def _handle_add_destination(message, user_id, text):

    project_id = CURRENT_PROJECT.get(user_id)
    project = _get_owned_project(project_id, user_id) if project_id else None

    if project is None:

        WAITING_DESTINATION.pop(user_id, None)

        await message.reply_text(
            "❌ No project selected. Open a project and tap ➕ Destination again.",
            reply_markup=main_menu
        )

        return

    allowed, reason = plan_service.can_add_destination(user_id, project_id)

    if not allowed:
        WAITING_DESTINATION.pop(user_id, None)
        await message.reply_text(
            _locked_text(reason),
            reply_markup=_locked_markup()
        )
        return

    try:
        chat = await get_chat(text, for_destination=True)
    except Exception as e:
        logger.exception("get_chat failed while adding destination: %s", e)
        await message.reply_text(f"❌ Couldn't add this destination\n\n{e}")
        return

    if chat is None:

        await message.reply_text(
            "❌ Invalid Channel / Group / Bot\n\n"
            "Send a valid public username, e.g. @YourChannel"
        )

        return

    ok = add_destination(
        project_id,
        chat["chat_id"],
        chat["username"],
        chat["title"],
        chat["type"]
    )

    WAITING_DESTINATION.pop(user_id, None)

    if ok:

        await force_refresh_routes()

        text = (
            "✅ Destination Added\n\n"
            f"📂 {chat['title'] or chat['username'] or chat['chat_id']}\n"
            f"🏷 {chat['type']}"
        )

        if not chat.get("joined", True):
            text += f"\n\n⚠ {chat['join_note']}"

        await message.reply_text(
            text,
            reply_markup=project_keyboard(project_id, platform_type=project["platform_type"])
        )

    else:

        await message.reply_text(
            "⚠ Destination Already Exists",
            reply_markup=project_keyboard(project_id, platform_type=project["platform_type"])
        )


# ==========================================
# INSTAGRAM MANAGEMENT
# ==========================================

def _instagram_screen_text(project):

    lines = [f"📸 Instagram — {project['name']}", ""]

    if project["processing_username"] or project["processing_title"]:
        lines.append(f"🔗 Converter Output: {project['processing_username'] or project['processing_title']}")
    else:
        lines.append("🔗 Converter Output: not set")

    lines.append("")

    destinations = instagram_service.get_instagram_destinations(project["id"])

    if destinations:
        for d in destinations:
            status_label = "🟢 Available" if d["status"] == "available" else "🟡 Setup Required"
            lines.append(f"• {d['username'] or d['ig_user_id']} ({d['target_type']}) — {status_label}")
    else:
        lines.append("No Instagram destinations yet.")

    if not INSTAGRAM_ACCESS_TOKEN:
        lines.append(
            "\nℹ️ No INSTAGRAM_ACCESS_TOKEN configured — Broadcast Channel "
            "targets still work via the approval queue; Feed auto-publish "
            "needs a token to go live."
        )

    lines.append(
        "\n⚠️ Instagram Broadcast has been removed from the platform "
        "roadmap. This project keeps working, but Instagram is no longer "
        "offered for new projects."
    )

    return "\n".join(lines)


async def _show_instagram_screen(message, project_id, user_id, edit=False):

    project = _get_owned_project(project_id, user_id)

    if project is None:
        await message.reply_text("❌ Project Not Found")
        return

    destinations = instagram_service.get_instagram_destinations(project_id)

    kwargs = dict(
        text=_instagram_screen_text(project),
        reply_markup=instagram_management_keyboard(
            project_id,
            has_processing_channel=bool(project["processing_chat_id"]),
            destinations=destinations,
        ),
    )

    if edit:
        await message.edit_text(**kwargs)
    else:
        await message.reply_text(**kwargs)


async def _handle_set_processing_channel(message, user_id, text):

    project_id = WAITING_PROCESSING_CHANNEL.get(user_id)
    project = _get_owned_project(project_id, user_id) if project_id else None

    if project is None:

        WAITING_PROCESSING_CHANNEL.pop(user_id, None)

        await message.reply_text(
            "❌ No project selected. Open the project's Instagram screen again.",
            reply_markup=main_menu
        )

        return

    try:
        chat = await get_chat(text)
    except Exception as e:
        logger.exception("get_chat failed while setting processing channel: %s", e)
        await message.reply_text(f"❌ Couldn't set this channel\n\n{e}")
        return

    if chat is None:
        await message.reply_text(
            "❌ Invalid Channel\n\nSend a valid public username, e.g. @ConverterOutput"
        )
        return

    set_processing_channel(project_id, chat["chat_id"], chat["username"], chat["title"])
    WAITING_PROCESSING_CHANNEL.pop(user_id, None)

    await force_refresh_processing_routes()

    reply_text = f"✅ Converter output channel set to {chat['title'] or chat['username']}"

    if not chat.get("joined", True):
        reply_text += f"\n\n⚠ {chat['join_note']}"

    await message.reply_text(reply_text)
    await _show_instagram_screen(message, project_id, user_id)


async def _handle_add_instagram_destination(message, user_id, text):

    draft = WAITING_INSTAGRAM_DESTINATION.get(user_id)

    if not draft:
        await message.reply_text(
            "❌ No destination setup in progress.", reply_markup=main_menu
        )
        return

    project_id = draft["project_id"]
    project = _get_owned_project(project_id, user_id)

    if project is None:
        WAITING_INSTAGRAM_DESTINATION.pop(user_id, None)
        await message.reply_text("❌ Project Not Found", reply_markup=main_menu)
        return

    ig_user_id = text.strip()

    if not ig_user_id:
        await message.reply_text("❌ Send a valid Instagram Business Account ID.")
        return

    status = "setup_required"
    verified_username = None

    if draft["target_type"] == "feed" and is_ig_configured():

        ok, detail = await verify_ig_capability(ig_user_id)

        if ok:
            status = "available"
        else:
            await message.reply_text(
                f"⚠️ Added, but couldn't verify this account yet: {detail}\n"
                "You can retry verification later; it's saved as Setup Required."
            )

    destination_id = instagram_service.add_instagram_destination(
        project_id, ig_user_id, verified_username, verified_username, target_type=draft["target_type"]
    )
    instagram_service.set_destination_status(destination_id, status)

    WAITING_INSTAGRAM_DESTINATION.pop(user_id, None)

    await message.reply_text("✅ Instagram destination added.")
    await _show_instagram_screen(message, project_id, user_id)


async def _show_approval_queue_item(message, project_id, user_id, offset, edit=False):
    """Shows one Ready-to-Publish job at a time. Fetches a fresh live
    preview (forward) of the actual converter-channel message via
    Telethon on every view - nothing about the post is cached or
    duplicated locally, see core/processing_listener.py's docstring."""

    project = _get_owned_project(project_id, user_id)

    if project is None:
        await message.reply_text("❌ Project Not Found")
        return

    jobs = processing_service.get_approval_queue(project_id, limit=offset + 1)

    if offset >= len(jobs):

        text = "📋 Approval Queue\n\nNo more items - the queue is empty."
        markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅ Back", callback_data=f"instagram:{project_id}")]]
        )

        if edit:
            await message.edit_text(text, reply_markup=markup)
        else:
            await message.reply_text(text, reply_markup=markup)

        return

    job = jobs[offset]

    try:
        detected_urls = json.loads(job["detected_urls"] or "[]")
    except (TypeError, ValueError):
        detected_urls = []

    caption = instagram_service.format_for_instagram(project_id, job["text_content"], detected_urls)

    try:
        await telethon_client.forward_messages(
            user_id, int(job["converter_message_id"]), from_peer=int(job["converter_chat_id"])
        )
    except Exception:
        logger.exception("Couldn't forward approval-queue preview for job %s", job["id"])
        await message.reply_text("⚠️ Couldn't fetch a live preview of this post - showing text only.")

    await message.reply_text(
        f"📋 Approval Queue (item {offset + 1})\n\n"
        f"Suggested Instagram caption:\n\n{caption}",
        reply_markup=approval_queue_item_keyboard(job["id"], project_id, offset),
    )

async def _handle_set_delay(message, user_id, text):

    project_id = CURRENT_PROJECT.get(user_id)
    project = _get_owned_project(project_id, user_id) if project_id else None

    if project is None:

        WAITING_DELAY.pop(user_id, None)

        await message.reply_text(
            "❌ No project selected.",
            reply_markup=main_menu
        )

        return

    WAITING_DELAY.pop(user_id, None)

    parts = [p.strip() for p in text.replace(" ", "").split(",")]

    try:

        if len(parts) == 1:
            delay_min = delay_max = float(parts[0])
        else:
            delay_min, delay_max = float(parts[0]), float(parts[1])

        if delay_min < 0 or delay_max < 0:
            raise ValueError

    except (ValueError, IndexError):

        await message.reply_text(
            "❌ Invalid format. Send a single number (fixed delay) or "
            "two numbers separated by a comma (random range), e.g.\n\n"
            "5\n\nor\n\n2,8"
        )

        return

    settings_service.set_delay(project_id, delay_min, delay_max)
    await force_refresh_routes()

    settings = settings_service.get_settings(project_id)

    await message.reply_text(
        "✅ Delay Updated",
        reply_markup=project_settings_keyboard(project_id, settings)
    )


async def _handle_set_whitelist(message, user_id, text):

    project_id = CURRENT_PROJECT.get(user_id)
    project = _get_owned_project(project_id, user_id) if project_id else None

    if project is None:
        WAITING_WHITELIST.pop(user_id, None)
        await message.reply_text("❌ No project selected.", reply_markup=main_menu)
        return

    WAITING_WHITELIST.pop(user_id, None)

    value = "" if text.strip() == "-" else text.strip()
    settings_service.set_keyword_whitelist(project_id, value)
    await force_refresh_routes()

    settings = settings_service.get_settings(project_id)

    await message.reply_text(
        "✅ Whitelist Updated" if value else "✅ Whitelist Cleared",
        reply_markup=project_filters_keyboard(
            project_id, settings, content_rules_service.get_rules(project_id)
        )
    )


async def _handle_set_blacklist(message, user_id, text):

    project_id = CURRENT_PROJECT.get(user_id)
    project = _get_owned_project(project_id, user_id) if project_id else None

    if project is None:
        WAITING_BLACKLIST.pop(user_id, None)
        await message.reply_text("❌ No project selected.", reply_markup=main_menu)
        return

    WAITING_BLACKLIST.pop(user_id, None)

    value = "" if text.strip() == "-" else text.strip()
    settings_service.set_keyword_blacklist(project_id, value)
    await force_refresh_routes()

    settings = settings_service.get_settings(project_id)

    await message.reply_text(
        "✅ Blacklist Updated" if value else "✅ Blacklist Cleared",
        reply_markup=project_filters_keyboard(
            project_id, settings, content_rules_service.get_rules(project_id)
        )
    )


async def _handle_set_regex(message, user_id, text):

    project_id = CURRENT_PROJECT.get(user_id)
    project = _get_owned_project(project_id, user_id) if project_id else None

    if project is None:
        WAITING_REGEX.pop(user_id, None)
        await message.reply_text("❌ No project selected.", reply_markup=main_menu)
        return

    WAITING_REGEX.pop(user_id, None)

    value = "" if text.strip() == "-" else text.strip()

    if value:

        import re as _re

        try:
            _re.compile(value)
        except _re.error as e:

            await message.reply_text(
                f"❌ Invalid regex: {e}\n\nSend a valid pattern, or `-` to clear."
            )

            return

    settings_service.set_regex_filter(project_id, value)
    await force_refresh_routes()

    settings = settings_service.get_settings(project_id)

    await message.reply_text(
        "✅ Regex Filter Updated" if value else "✅ Regex Filter Cleared",
        reply_markup=project_filters_keyboard(
            project_id, settings, content_rules_service.get_rules(project_id)
        )
    )


# ==========================================
# CONTENT RULES (Phase 7) - text input handlers
# ==========================================

_CONTENT_RULE_LABELS = {
    "required_keywords": "Required Keywords",
    "hashtag_filter": "Hashtag Filter",
    "domain_whitelist": "Domain Whitelist",
    "domain_blacklist": "Domain Blacklist",
    "sender_whitelist": "Sender Whitelist",
    "sender_blacklist": "Sender Blacklist",
}


async def _handle_content_rule_field(message, user_id, text):

    state = WAITING_CONTENT_RULE_FIELD.get(user_id)
    WAITING_CONTENT_RULE_FIELD.pop(user_id, None)

    if not state:
        return

    project_id = state["project_id"]
    field = state["field"]

    project = _get_owned_project(project_id, user_id)

    if project is None:
        await message.reply_text("❌ No project selected.", reply_markup=main_menu)
        return

    if field == "length":

        value = text.strip()

        if value == "-":
            content_rules_service.update_rules(project_id, min_length=None, max_length=None)
        else:
            try:
                min_s, max_s = [p.strip() for p in value.split(",", 1)]
                min_length = int(min_s) if min_s else None
                max_length = int(max_s) if max_s else None
            except (ValueError, IndexError):
                await message.reply_text(
                    "❌ Send as `min,max` (e.g. `10,200`), or `-` to clear."
                )
                WAITING_CONTENT_RULE_FIELD[user_id] = state
                return

            content_rules_service.update_rules(
                project_id, min_length=min_length, max_length=max_length
            )

    else:

        value = "" if text.strip() == "-" else text.strip()
        content_rules_service.update_rules(project_id, **{field: value})

    await force_refresh_routes()

    settings = settings_service.get_settings(project_id)
    content_rules = content_rules_service.get_rules(project_id)

    await message.reply_text(
        f"✅ {_CONTENT_RULE_LABELS.get(field, field)} Updated",
        reply_markup=project_filters_keyboard(project_id, settings, content_rules)
    )


# ==========================================
# FORMATTING (Phase 7) - text input handlers
# ==========================================

async def _handle_formatting_field(message, user_id, text):

    state = WAITING_FORMATTING_FIELD.get(user_id)
    WAITING_FORMATTING_FIELD.pop(user_id, None)

    if not state:
        return

    project_id = state["project_id"]
    field = state["field"]

    project = _get_owned_project(project_id, user_id)

    if project is None:
        await message.reply_text("❌ No project selected.", reply_markup=main_menu)
        return

    value = "" if text.strip() == "-" else text.strip()

    if field == "prefix":
        formatting_service.set_prefix(project_id, value)
    else:
        formatting_service.set_suffix(project_id, value)

    await force_refresh_routes()
    rules = formatting_service.get_rules(project_id)

    await message.reply_text(
        f"✅ {field.title()} Updated",
        reply_markup=formatting_keyboard(project_id, rules)
    )


async def _handle_replace_rule_input(message, user_id, text):

    state = WAITING_REPLACE_RULE.get(user_id)

    if not state:
        return

    project_id = state["project_id"]
    project = _get_owned_project(project_id, user_id)

    if project is None:
        WAITING_REPLACE_RULE.pop(user_id, None)
        await message.reply_text("❌ No project selected.", reply_markup=main_menu)
        return

    if state["stage"] == "find":

        state["find"] = text
        state["stage"] = "replace"

        await message.reply_text(
            f"Find: {text}\n\nNow send the replacement text (send empty `-` to replace with nothing)."
        )
        return

    replace_value = "" if text.strip() == "-" else text

    WAITING_REPLACE_RULE.pop(user_id, None)

    formatting_service.add_replace_rule(project_id, state["find"], replace_value)
    await force_refresh_routes()

    rules = formatting_service.get_rules(project_id)

    await message.reply_text(
        f"✅ Replace Rule Added: {state['find']} → {replace_value or '(nothing)'}",
        reply_markup=formatting_keyboard(project_id, rules)
    )


async def _handle_remove_pattern_input(message, user_id, text):

    state = WAITING_REMOVE_PATTERN.get(user_id)
    WAITING_REMOVE_PATTERN.pop(user_id, None)

    if not state:
        return

    project_id = state["project_id"]
    project = _get_owned_project(project_id, user_id)

    if project is None:
        await message.reply_text("❌ No project selected.", reply_markup=main_menu)
        return

    try:
        import re as _re
        _re.compile(text)
    except _re.error:
        pass  # not a valid regex - formatting_service falls back to a plain substring removal

    formatting_service.add_remove_pattern(project_id, text)
    await force_refresh_routes()

    rules = formatting_service.get_rules(project_id)

    await message.reply_text(
        f"✅ Remove Pattern Added: {text}",
        reply_markup=formatting_keyboard(project_id, rules)
    )


async def _handle_broadcast(message, context, admin_id, text):

    WAITING_BROADCAST.pop(admin_id, None)

    user_ids = get_all_user_ids()

    sent = 0
    failed = 0

    for uid in user_ids:

        try:
            await context.bot.send_message(uid, f"📢 Announcement\n\n{text}")
            sent += 1
        except Exception:
            failed += 1

    await message.reply_text(
        f"✅ Broadcast Complete\n\nDelivered: {sent}\nFailed: {failed}"
    )


# ==========================================
# HELP / KNOWLEDGE + SUPPORT TICKETS (UX-NAV companion)
# ==========================================

def _row_get(row, key, default=None):
    """sqlite3.Row access without KeyError risk."""
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        return default


async def _send_articles(message, user_id, kind="faq", edit=False):
    """❓ FAQ / 📖 Guide content from the knowledge base."""
    rows = knowledge_service.list_articles(kind=kind, active_only=True)
    title = "❓ FAQ" if kind == "faq" else "📖 Guide"

    if not rows:
        text = f"{title}\n\nNo articles published yet - check back soon."
    else:
        lines = [title]
        for art in rows[:8]:
            body = (_row_get(art, "body") or "").strip()
            if len(body) > 420:
                body = body[:417] + "..."
            lines.append(f"\n📌 {_row_get(art, 'title') or '-'}\n{body}")
        if len(rows) > 8:
            lines.append(f"\n… and {len(rows) - 8} more. Ask in Support AI for details.")
        text = "\n".join(lines)

    markup = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⬅ Support", callback_data="nav:help"),
            InlineKeyboardButton("🏠 Home", callback_data="nav:home"),
        ]
    ])
    return await nav_state.place(message, user_id, f"help_{kind}", text,
                                 reply_markup=markup, edit=edit)


async def _render_tickets_list(message, user_id, edit=True):
    tickets = support_service.list_user_tickets(user_id, limit=10)

    if not tickets:
        text = "🎫 My Support Tickets\n\nNo tickets yet."
        rows = [[InlineKeyboardButton("➕ New Ticket", callback_data="support:new")]]
    else:
        text_lines = ["🎫 My Support Tickets", ""]
        rows = []
        for t in tickets:
            icon = support_service.STATUS_ICONS.get(_row_get(t, "status"), "•")
            subject = (_row_get(t, "subject") or "Ticket")[:42]
            text_lines.append(f"{icon} #{_row_get(t, 'id')} · {subject}")
            rows.append([InlineKeyboardButton(
                f"{icon} #{_row_get(t, 'id')} {subject}", callback_data=f"support:view:{_row_get(t, 'id')}"
            )])
        rows.append([InlineKeyboardButton("➕ New Ticket", callback_data="support:new")])
        text = "\n".join(text_lines)

    rows.append([
        InlineKeyboardButton("⬅ Support", callback_data="nav:help"),
        InlineKeyboardButton("🏠 Home", callback_data="nav:home"),
    ])
    return await nav_state.place(message, user_id, "tickets", text,
                                 reply_markup=InlineKeyboardMarkup(rows), edit=edit)


async def _render_ticket_screen(message, ticket_id, user_id, edit=True):
    ticket = support_service.get_ticket(ticket_id)
    if ticket is None or _row_get(ticket, "user_id") != user_id:
        if edit:
            try:
                await message.edit_text(
                    "⚠️ " + i18n.t(user_id, "nav.screen_expired"),
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🏠 Home", callback_data="nav:home")]
                    ]),
                )
            except Exception:
                pass
        else:
            await message.reply_text("🎫 Ticket not found.")
        return None

    icon = support_service.STATUS_ICONS.get(_row_get(ticket, "status"), "•")
    lines = [
        f"🎫 Ticket #{ticket_id} — {_row_get(ticket, 'category')}",
        f"Status: {icon} {_row_get(ticket, 'status')}",
        "",
        f"📌 {_row_get(ticket, 'subject')}",
    ]

    messages = support_service.get_messages(ticket_id) or []
    for msg in messages[-8:]:
        who = "👤 You" if _row_get(msg, "sender_type") == "user" else "🛟 Support"
        when = (_row_get(msg, "created_at") or "")[:16]
        body = (_row_get(msg, "message") or "").strip()
        if len(body) > 500:
            body = body[:497] + "..."
        lines.append(f"\n{who} · {when}\n{body}")

    if not messages:
        lines.append("\n(no messages yet)")

    status = _row_get(ticket, "status")
    markup = support_ticket_keyboard(ticket_id, status)
    return await nav_state.place(message, user_id, f"ticket:{ticket_id}", "\n".join(lines),
                                 reply_markup=markup, edit=edit)


_TICKET_CATEGORY_LABELS = {
    "general": "💬 General",
    "billing": "💳 Billing / Payments",
    "forwarding": "🔁 Forwarding Problem",
    "connection": "🔗 Connection / Login",
    "feature": "💡 Feature Request",
}


async def _handle_support_callback(query, context, user_id, sub, parts):
    """Support hub + ticket flows. All data is read/written strictly
    for the calling user (tickets are ownership-checked)."""

    if sub == "ai":
        _reset_waiting_states(user_id)
        WAITING_SUPPORT_AI[user_id] = True
        text = (
            "🤖 Support AI\n\n"
            "Ask me anything about ChannelFlow - plans, limits, how to "
            "set up sources and destinations...\n\n"
            "Send your question below (or press 🏠 Home / send /cancel "
            "to stop the chat)."
        )
        await nav_state.place(query.message, user_id, "support_ai", text,
                              reply_markup=None, edit=True)
        return

    if sub == "group":
        text = (
            "Support Team\n\n"
            "ChannelFlow AI Support: https://t.me/ChannelFlowSupport_bot\n\n"
            "For tracked help prefer 💬 New Support Ticket - replies come "
            "here in the chat."
        )
        await nav_state.place(query.message, user_id, "support_group", text,
                              reply_markup=InlineKeyboardMarkup([[
                                  InlineKeyboardButton("⬅ Support", callback_data="nav:help"),
                                  InlineKeyboardButton("🏠 Home", callback_data="nav:home"),
                              ]]), edit=True)
        return

    if sub == "owner":
        text = (
            "👑 Owner\n\n"
            "Owner-level help is handled through the /owner panel. "
            "Open /owner in this chat to see owner options."
        )
        await nav_state.place(query.message, user_id, "support_owner", text,
                              reply_markup=InlineKeyboardMarkup([[
                                  InlineKeyboardButton("⬅ Support", callback_data="nav:help"),
                                  InlineKeyboardButton("🏠 Home", callback_data="nav:home"),
                              ]]), edit=True)
        return

    if sub == "list":
        await _render_tickets_list(query.message, user_id, edit=True)
        return

    if sub == "view":
        ticket_id = int(parts[2])
        await _render_ticket_screen(query.message, ticket_id, user_id, edit=True)
        return

    if sub == "reply":
        ticket_id = int(parts[2])
        ticket = support_service.get_ticket(ticket_id)
        if ticket is None or _row_get(ticket, "user_id") != user_id:
            await query.answer("⚠ Ticket not found.", show_alert=True)
            return
        WAITING_TICKET_REPLY[user_id] = ticket_id
        try:
            await query.edit_message_text(
                f"✉️ Reply to ticket #{ticket_id}\n\nSend your reply text:"
            )
        except Exception:
            await query.message.reply_text("✉️ Send your reply text:")
        return

    if sub in ("close", "reopen"):
        ticket_id = int(parts[2])
        ticket = support_service.get_ticket(ticket_id)
        if ticket is None or _row_get(ticket, "user_id") != user_id:
            await query.answer("⚠ Ticket not found.", show_alert=True)
            return
        support_service.set_status(ticket_id, "closed" if sub == "close" else "open")
        await _render_ticket_screen(query.message, ticket_id, user_id, edit=True)
        return

    if sub == "new":
        buttons = []
        for cat in support_service.CATEGORIES:
            label = _TICKET_CATEGORY_LABELS.get(cat, cat.capitalize())
            buttons.append([InlineKeyboardButton(label, callback_data=f"support:newcat:{cat}")])
        buttons.append([
            InlineKeyboardButton("⬅ Support", callback_data="nav:help"),
            InlineKeyboardButton("🏠 Home", callback_data="nav:home"),
        ])
        await nav_state.place(
            query.message, user_id, "ticket_new",
            "🎫 New Support Ticket\n\nWhat is your request about?",
            reply_markup=InlineKeyboardMarkup(buttons), edit=True,
        )
        return

    if sub == "newcat":
        cat = parts[2] if len(parts) > 2 else "general"
        WAITING_TICKET_SUBJECT[user_id] = {"category": cat}
        try:
            await query.edit_message_text(
                "📝 Send a short subject for your ticket "
                "(max 200 characters), or /cancel to abort."
            )
        except Exception:
            await query.message.reply_text("📝 Send a short subject, or /cancel to abort.")
        return

    # Unknown support sub-action -> hub again.
    await _send_support_hub(query.message, user_id, edit=True, is_creator=user_id in ADMIN_IDS)


# ==========================================
# INLINE KEYBOARD (CALLBACK) HANDLER
# ==========================================

async def _reply_stale_callback(query, text):
    """Safe dead-end answer for stale/unhandled callbacks: always gives
    the user a way Home (UX-NAV-02 93.4) instead of stranding them."""
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Home", callback_data="nav:home")]])
    try:
        await query.edit_message_text(text, reply_markup=markup)
    except Exception:
        try:
            await query.message.reply_text(text, reply_markup=markup)
        except Exception:
            pass


async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query: CallbackQuery = update.callback_query

    await query.answer()

    data = query.data or ""
    user_id = query.from_user.id

    parts = data.split(":")
    action = parts[0]

    try:

        # ======================================
        # PROMOTIONAL /post, /broadcast TARGET SELECTION
        # ======================================

        if action == "promo":
            await handle_promo_callback(query, context, user_id, parts)
            return

        # ======================================
        # UX-NAV: CENTRAL NAVIGATION (92.4 / 93.4)
        # ======================================

        if action == "nav":
            sub = parts[1] if len(parts) > 1 else "home"
            _reset_waiting_states(user_id)
            if sub == "home":
                await _go_home(query.message, query.from_user, edit=True)
            elif sub == "projects":
                await _show_task_list(query.message, user_id, edit=True)
            elif sub == "account":
                await _show_account_hub(query.message, query.from_user, edit=True)
            elif sub == "settings":
                await _send_settings_screen(query.message, user_id, edit=True)
            elif sub == "help":
                await _send_support_hub(query.message, user_id, edit=True,
                                        is_creator=user_id in ADMIN_IDS)
            else:
                await _go_home(query.message, query.from_user, edit=True)
            return

        # ======================================
        # LANGUAGE (first-run picker + Settings -> Language)
        # ======================================

        if action == "lang":
            lang = parts[1] if len(parts) > 1 else None
            first_run = not _language_chosen(user_id)

            if not i18n.set_user_language(user_id, lang):
                await query.answer("⚠ Invalid language.", show_alert=True)
                return

            saved = i18n.t(user_id, "lang.saved")

            if first_run:
                # Onboarding continues straight into the account-state
                # Home with the right persistent menu attached.
                try:
                    await query.edit_message_text(saved)
                except Exception:
                    await query.message.reply_text(saved)
                await _go_home(query.message, query.from_user)
            else:
                await query.answer(i18n.t(user_id, "language.updated"))
                try:
                    await query.edit_message_text(
                        saved,
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("⚙️ Settings", callback_data="nav:settings")],
                            [InlineKeyboardButton("🏠 Home", callback_data="nav:home")],
                        ]),
                    )
                except Exception:
                    pass
            return

        # ======================================
        # TASK LIST PAGINATION / SEARCH (Batch 4 / UX-NAV-03)
        # ======================================

        if action == "tasks":
            sub = parts[1] if len(parts) > 1 else None
            if sub == "page":
                try:
                    page = int(parts[2])
                except (IndexError, ValueError):
                    page = 1
                _TASK_PAGE[user_id] = max(1, page)
                await _show_task_list(query.message, user_id, edit=True)
                return
            if sub == "search":
                _reset_waiting_states(user_id)
                WAITING_TASK_SEARCH[user_id] = True
                text = (
                    "🔍 Search tasks\n\n"
                    "Send a task name (or part of one) and I'll filter "
                    "the list to matches.\n\n"
                    "Send /cancel to abort."
                )
                try:
                    await query.edit_message_text(text)
                except Exception:
                    await query.message.reply_text(text)
                return
            if sub == "clear":
                _TASK_TERM.pop(user_id, None)
                _TASK_PAGE.pop(user_id, None)
                await _show_task_list(query.message, user_id, edit=True)
                return
            await _show_task_list(query.message, user_id, edit=True)
            return

        # ======================================
        # NEW TASK (task-list ➕ New Task)
        # ======================================

        if action == "newproj":
            allowed, reason = plan_service.can_create_project(user_id)
            if not allowed:
                # Batch 4 / UX-NAV-04: plan-locked state renders a
                # screen with a working Upgrade CTA, not a bare alert.
                await nav_state.place(
                    query.message, user_id, "locked",
                    _locked_text(reason),
                    reply_markup=_locked_markup(), edit=True,
                )
                return
            _reset_waiting_states(user_id)
            WAITING_PROJECT_NAME[user_id] = True
            try:
                await query.edit_message_text(
                    "📝 Send Task Name\n\n"
                    "(e.g. Deals Channel → Backup Group)\n\n"
                    "Send /cancel to abort."
                )
            except Exception:
                await query.message.reply_text("📝 Send Task Name")
            return

        # ======================================
        # TASK DETAILS (projcard: from the task list)
        # ======================================

        if action == "projcard":
            project_id = int(parts[1])
            _reset_waiting_states(user_id)
            await _show_task_detail(query.message, project_id, user_id, edit=True)
            return

        # ======================================
        # EDIT TASK ROOT
        # ======================================

        if action == "editproj":
            project_id = int(parts[1])
            _reset_waiting_states(user_id)
            await _show_edit_task(query.message, project_id, user_id, edit=True)
            return

        # ======================================
        # DELETE TASK - confirmation required (93.2)
        # ======================================

        if action == "deleteconfirm":
            project_id = int(parts[1])
            _reset_waiting_states(user_id)
            await _confirm_delete_task(query.message, project_id, user_id, edit=True)
            return

        # ======================================
        # DELETE SOURCE / DESTINATION - confirmations
        # ======================================

        if action == "deletesourceconfirm":
            source_id, project_id = int(parts[1]), int(parts[2])
            source, project = _get_owned_source(source_id, user_id)
            if source is None:
                await query.answer("⚠ Source not found.", show_alert=True)
                return
            text = (
                f"⚠️ Disconnect this source?\n\n"
                f"📂 {source['title'] or source['chat_id']}\n"
                f"👤 @{source['username'] or '-'}\n\n"
                "Messages will stop coming from it."
            )
            markup = delete_confirm_keyboard(f"deletesource:{source_id}", f"listsource:{project_id}")
            await nav_state.place(query.message, user_id, f"delete_source:{source_id}",
                                  text, reply_markup=markup, edit=True)
            return

        if action == "deletedestinationconfirm":
            destination_id, project_id = int(parts[1]), int(parts[2])
            destination, project = _get_owned_destination(destination_id, user_id)
            if destination is None:
                await query.answer("⚠ Destination not found.", show_alert=True)
                return
            text = (
                f"⚠️ Disconnect this destination?\n\n"
                f"📂 {destination['title'] or destination['chat_id']}\n"
                f"👤 @{destination['username'] or '-'}\n\n"
                "Messages will stop being sent to it."
            )
            markup = delete_confirm_keyboard(
                f"deletedestination:{destination_id}", f"listdestination:{project_id}"
            )
            await nav_state.place(query.message, user_id, f"delete_destination:{destination_id}",
                                  text, reply_markup=markup, edit=True)
            return

        # ======================================
        # SUPPORT HUB + HELP (available pre-login too)
        # ======================================

        if action == "help":
            sub = parts[1] if len(parts) > 1 else "faq"

            if sub == "faq":
                await _send_articles(query.message, user_id, kind="faq", edit=True)
                return

            if sub == "guide":
                await _send_articles(query.message, user_id, kind="guide", edit=True)
                return

            if sub == "tour":
                await _send_tour(query.message)
                return

            if sub == "feedback":
                WAITING_FEEDBACK[user_id] = True
                try:
                    await query.edit_message_text(
                        "💡 Feature Request\n\n"
                        "Describe the feature you'd like (a few lines is enough)."
                    )
                except Exception:
                    await query.message.reply_text("💡 Describe the feature you'd like.")
                return

            await _send_support_hub(query.message, user_id, edit=True,
                                    is_creator=user_id in ADMIN_IDS)
            return

        if action == "support":
            sub = parts[1] if len(parts) > 1 else "hub"
            await _handle_support_callback(query, context, user_id, sub, parts)
            return

        # ======================================
        # FILTER SUBSCREENS (Keywords / Domains / Senders)
        # ======================================

        if action in ("filterkw", "filterdomains", "filtersenders"):
            project_id = int(parts[1])
            project = _get_owned_project(project_id, user_id)
            if project is None:
                await query.answer("⚠ This task no longer exists.", show_alert=True)
                return

            settings = settings_service.get_settings(project_id)
            content_rules = content_rules_service.get_rules(project_id)
            header = f"🧹 {project['name']}"

            if action == "filterkw":
                text = (
                    f"{header} — Keyword Filters\n\n"
                    "Required: only messages containing at least one of "
                    "these keywords pass.\n"
                    "Blocked: messages containing any are skipped."
                )
                markup = keyword_filter_keyboard(project_id, settings)
                name = f"filters_keywords:{project_id}"
            elif action == "filterdomains":
                text = (
                    f"{header} — Domain Filters\n\n"
                    "Whitelist: only messages linking to these domains "
                    "pass.\nBlacklist: links to these are skipped."
                )
                markup = domain_filter_keyboard(project_id, content_rules)
                name = f"filters_domains:{project_id}"
            else:
                text = (
                    f"{header} — Sender Filters\n\n"
                    "Allow: only these sender IDs pass (groups).\n"
                    "Block: these sender IDs are skipped."
                )
                markup = sender_filter_keyboard(project_id, content_rules)
                name = f"filters_senders:{project_id}"

            await nav_state.place(query.message, user_id, name, text, reply_markup=markup, edit=True)
            return

        # ======================================
        # CLEAR ALL FILTERS - confirmation required
        # ======================================

        if action == "clearfiltersconfirm":
            project_id = int(parts[1])
            project = _get_owned_project(project_id, user_id)
            if project is None:
                await query.answer("⚠ This task no longer exists.", show_alert=True)
                return
            text = (
                f"⚠️ Clear ALL filters for {project['name']}?\n\n"
                "Media type, keywords, domains, senders, regex and "
                "content rules will be reset."
            )
            markup = delete_confirm_keyboard(f"clearfilters:{project_id}", f"projfilters:{project_id}")
            await nav_state.place(query.message, user_id, f"clear_filters:{project_id}",
                                  text, reply_markup=markup, edit=True)
            return

        # ======================================
        # FORMATTING ROOT (Back-to-Formatting target)
        # ======================================

        if action == "fmtroot":
            project_id = int(parts[1])
            project = _get_owned_project(project_id, user_id)
            if project is None:
                await query.answer("⚠ This task no longer exists.", show_alert=True)
                return
            rules = formatting_service.get_rules(project_id)
            text = (
                f"📝 Formatting — {project['name']}\n\n"
                "Applies in Telegram Copy mode and to Instagram captions. "
                "Native Telegram Forward mode can't have its content "
                "edited, so formatting never applies there."
            )
            await nav_state.place(query.message, user_id, f"formatting:{project_id}",
                                  text, reply_markup=formatting_keyboard(project_id, rules), edit=True)
            return

        # ======================================
        # 🛒 EXTRA CREDITS HUB (Batch 4 / PRD 23 + 26)
        # ======================================

        if action == "credits":

            sub = parts[1] if len(parts) > 1 else None

            if sub == "home":
                balance = extra_credits_service.get_balance(user_id)
                lines = [
                    "🛒 Extra Credits\n",
                    f"Balance: ⚡ {balance:,} forward(s)\n",
                    "Priority: your daily plan allowance is used first - "
                    "Extra Credits are consumed only after today's "
                    "allowance is exhausted (visible in the ledger below).",
                ]
                ledger = extra_credits_service.recent_ledger(user_id, limit=5)
                if ledger:
                    lines.append("\nRecent activity:")
                    for row in ledger:
                        sign = "+" if row["delta"] > 0 else ""
                        lines.append(
                            f"   {sign}{row['delta']:,} ({row['kind']}) "
                            f"{row['created_at'] or ''}"
                        )
                else:
                    lines.append("\nNo activity yet - buy a package to get started.")
                lines.append("\n📦 Packages:")

                buttons = []
                for pkg in extra_credits_service.list_packages():
                    if extra_credits_service.available_methods_for(pkg):
                        buttons.append([InlineKeyboardButton(
                            extra_credits_service.package_price_line(pkg),
                            callback_data=f"credits:buy:{pkg['id']}",
                        )])
                buttons.append([
                    InlineKeyboardButton("⬅ Account", callback_data="nav:account"),
                    InlineKeyboardButton("🏠 Home", callback_data="nav:home"),
                ])
                await nav_state.place(query.message, user_id, "credits",
                                      "\n".join(lines),
                                      reply_markup=InlineKeyboardMarkup(buttons),
                                      edit=True)
                return

            if sub == "buy":
                package_id = int(parts[2]) if len(parts) > 2 else 0
                pkg = extra_credits_service.get_package(package_id)
                if pkg is None:
                    await query.answer("⚠ Package not found.", show_alert=True)
                    return
                methods = extra_credits_service.available_methods_for(pkg)
                if not methods:
                    await query.answer(
                        "⚠ This package has no configured price yet. "
                        "Contact the admin.", show_alert=True)
                    return
                lines = [
                    f"🛒 {extra_credits_service.package_price_line(pkg)}\n",
                    f"⚡ {int(pkg['forwards_amount']):,} prepaid forwards, "
                    "never expire, used only after the daily allowance.\n",
                    "Choose how to pay:",
                ]
                rows = []
                if "stars" in methods:
                    rows.append([InlineKeyboardButton(
                        f"⭐ Pay {int(pkg['stars_price'])} Stars (instant)",
                        callback_data=f"credits:pay:{package_id}:stars")])
                if "upi" in methods:
                    rows.append([InlineKeyboardButton(
                        f"🇮🇳 UPI ₹{float(pkg['price_inr']):.0f}",
                        callback_data=f"credits:pay:{package_id}:upi")])
                if "crypto" in methods:
                    rows.append([InlineKeyboardButton(
                        f"₿ Crypto ${float(pkg['price_usd']):.2f}",
                        callback_data=f"credits:pay:{package_id}:crypto")])
                rows.append([InlineKeyboardButton("⬅ Back", callback_data="credits:home")])
                try:
                    await query.edit_message_text("\n".join(lines),
                                                  reply_markup=InlineKeyboardMarkup(rows))
                except Exception:
                    await query.message.reply_text("\n".join(lines),
                                                   reply_markup=InlineKeyboardMarkup(rows))
                return

            if sub == "pay":
                package_id = int(parts[2]) if len(parts) > 2 else 0
                method = parts[3] if len(parts) > 3 else None
                pkg = extra_credits_service.get_package(package_id)
                if pkg is None:
                    await query.answer("⚠ Package not found.", show_alert=True)
                    return

                if method == "stars":
                    try:
                        request = extra_credits_service.create_stars_package_request(
                            user_id, package_id)
                    except ValueError as e:
                        await query.answer(f"⚠ {e}", show_alert=True)
                        return
                    total = int(float(request["final_amount"] or 0))
                    payload = f"{stars_service.PAYLOAD_PKG}{request['id']}"
                    description = (
                        f"ChannelFlow Extra Credits - "
                        f"{int(pkg['forwards_amount']):,} prepaid forwards."
                    )
                    try:
                        await context.bot.send_invoice(
                            chat_id=user_id,
                            title=f"ChannelFlow Extra Credits ({int(pkg['forwards_amount']):,})",
                            description=description,
                            payload=payload,
                            provider_token="",
                            currency=stars_service.STARS_CURRENCY,
                            prices=[LabeledPrice("Extra Credits", total)],
                        )
                    except Exception:
                        stars_service.cancel_request(request["id"])
                        await query.message.reply_text(
                            "❌ Could not open the Stars checkout right now. "
                            "Please try again shortly.")
                        return
                    await query.message.reply_text(
                        "⭐ Payment sheet sent!\n\n"
                        f"{int(pkg['forwards_amount']):,} Extra Credits are "
                        "added the moment the payment completes. This "
                        f"invoice expires in {stars_service.STARS_INVOICE_LIFETIME_MINUTES} minutes."
                    )
                    return

                if method == "upi":
                    try:
                        request = extra_credits_service.create_offline_package_request(
                            user_id, package_id, "upi", payment_reference=UPI_ID)
                    except ValueError as e:
                        await query.answer(f"⚠ {e}", show_alert=True)
                        return
                    amount = float(request["final_amount"] or 0)
                    text = (
                        f"🇮🇳 {int(pkg['forwards_amount']):,} Extra Credits "
                        f"via UPI\n\n"
                        f"Pay ₹{amount:.0f} to: {UPI_ID}\n"
                        f"Name: {UPI_PAYEE_NAME or '-'}\n\n"
                        "After paying, tap Verify and send a screenshot."
                    )
                else:  # crypto
                    try:
                        pay_link, _track = await payment_service.create_oxapay_invoice(
                            float(pkg["price_usd"] or 0),
                            order_id=f"u{user_id}-credits-{package_id}",
                        )
                    except RuntimeError as e:
                        await query.message.reply_text(f"❌ {e}")
                        return
                    try:
                        request = extra_credits_service.create_offline_package_request(
                            user_id, package_id, "crypto", payment_reference=pay_link)
                    except ValueError as e:
                        await query.answer(f"⚠ {e}", show_alert=True)
                        return
                    amount = float(request["final_amount"] or 0)
                    text = (
                        f"₿ {int(pkg['forwards_amount']):,} Extra Credits "
                        f"in crypto\n\n"
                        f"Pay ${amount:.2f} here: {pay_link}\n\n"
                        "After paying, tap Verify and send a screenshot."
                    )

                buttons = InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Verify", callback_data=f"upgrade:verify:{request['id']}"),
                    InlineKeyboardButton("❌ Cancel", callback_data=f"upgrade:cancel:{request['id']}"),
                ]])
                await query.message.reply_text(text, reply_markup=buttons)
                return

            await query.message.reply_text("⚠ Unknown Extra Credits action")
            return

        # ======================================
        # ACCOUNT CARD (/start screen buttons)
        # ======================================

        if action == "acct":

            sub = parts[1] if len(parts) > 1 else None

            if sub == "noop":
                await query.answer("✅ Already connected.")
                return

            if sub == "connect":
                _reset_waiting_states(user_id)
                await _start_connect_flow(query.message, user_id)
                return

            if sub == "guide":
                await _send_guide(query.message)
                return

            if sub == "tour":
                await _send_tour(query.message)
                return

            if sub == "support":
                await _send_support_hub(query.message, user_id, edit=True,
                                        is_creator=user_id in ADMIN_IDS)
                return

            if sub == "plan":
                await _send_plan_screen(query.message, user_id, edit=True)
                return

            if sub == "wallet":
                balance_usd = wallet_service.get_balance(user_id)
                inr = balance_usd * pricing_service.INR_PER_USD
                text = (
                    f"💰 Wallet Balance: ${balance_usd:.2f} (≈ ₹{inr:.0f})\n\n"
                    "Used for Auto-Renew - top up here, an admin approves "
                    "it the same way as a plan payment, then it's available."
                )
                markup = InlineKeyboardMarkup([
                    [InlineKeyboardButton("➕ Add Funds", callback_data="wallet:topup")],
                    [
                        InlineKeyboardButton("⬅ Account", callback_data="nav:account"),
                        InlineKeyboardButton("🏠 Home", callback_data="nav:home"),
                    ],
                ])
                await nav_state.place(query.message, user_id, "wallet", text,
                                      reply_markup=markup, edit=True)
                return

            if sub == "history":
                await _send_payment_history(query.message, user_id, edit=True)
                return

            if sub == "connections":
                conn = get_connection()
                cur = conn.cursor()
                cur.execute(
                    "SELECT phone_number FROM user_telegram_sessions "
                    "WHERE telegram_id=? AND status='connected'",
                    (user_id,),
                )
                row = cur.fetchone()
                conn.close()
                phone = row["phone_number"] if row else None
                text = (
                    "🔗 Connected Accounts\n\n"
                    f"✈️ Telegram: {phone or 'None'}\n\n"
                    "Your Telegram session is encrypted and powers your "
                    "forwarding tasks. WhatsApp/Threads platforms are not "
                    "live yet."
                )
                buttons = []
                if phone:
                    buttons.append([InlineKeyboardButton(
                        "🔌 Disconnect Telegram", callback_data="settings:disconnect"
                    )])
                buttons.append([
                    InlineKeyboardButton("⬅ Account", callback_data="nav:account"),
                    InlineKeyboardButton("🏠 Home", callback_data="nav:home"),
                ])
                await nav_state.place(query.message, user_id, "connections", text,
                                      reply_markup=InlineKeyboardMarkup(buttons), edit=True)
                return

            if sub == "upgrade":

                entitlements = plan_service.get_entitlements(user_id)
                buttons = []

                # ⭐ Telegram Stars (PRD section 26): instant in-app
                # checkout; every price is DB-configured.
                buttons.append([InlineKeyboardButton(
                    "⭐ Telegram Stars (instant)", callback_data="upgrade:splans"
                )])

                # UPI/crypto plan prices come from the DB USD price book
                # (plan_service) - payment_service.PLAN_PRICES never
                # existed and would AttributeError here.
                for plan_name in ("BEGINNER", "PRO", "CREATOR"):
                    price = plan_service.get_plan_crypto_price_usd(plan_name)
                    if not price:
                        continue
                    marker = " (current)" if plan_name == entitlements["plan"] else ""
                    buttons.append([InlineKeyboardButton(
                        f"{plan_name} - ${price}/mo{marker}", callback_data=f"upgrade:plan:{plan_name}"
                    )])

                buttons.append([InlineKeyboardButton("⬅ Back", callback_data="acct:back")])

                await query.message.reply_text(
                    "💎 Upgrade\n\nChoose a plan:",
                    reply_markup=InlineKeyboardMarkup(buttons)
                )
                return

            if sub == "back":
                await _show_account_hub(query.message, query.from_user, edit=True)
                return

            if sub == "earn":
                await _show_rewards(query.message, context, user_id, edit=True)
                return

            if sub == "language":
                try:
                    await query.edit_message_text(
                        "🌐 Language\n\nChoose your language:",
                        reply_markup=LANGUAGE_KEYBOARD,
                    )
                except Exception:
                    await query.message.reply_text("🌐 Language", reply_markup=LANGUAGE_KEYBOARD)
                return

            return

        # ======================================
        # UPGRADE / PAYMENT FLOW
        # ======================================

        if action == "upgrade":

            sub = parts[1] if len(parts) > 1 else None

            # ======================================
            # ⭐ TELEGRAM STARS CHAIN (PRD section 26)
            # upgrade:splans -> splan -> sduration -> scheckout
            # ======================================

            if sub == "splans":
                entitlements = plan_service.get_entitlements(user_id)
                current = entitlements["plan"]
                text = (
                    "⭐ Telegram Stars\n\n"
                    "Pay instantly inside Telegram - no screenshots, no "
                    "manual review. Choose a plan:"
                )
                try:
                    await query.edit_message_text(
                        text, reply_markup=stars_plan_keyboard(current_plan=current)
                    )
                except Exception:
                    await query.message.reply_text(
                        text, reply_markup=stars_plan_keyboard(current_plan=current)
                    )
                return

            if sub == "splan":
                plan = parts[2] if len(parts) > 2 else None
                text = f"⭐ {plan}\n\nChoose a duration:"
                try:
                    await query.edit_message_text(
                        text, reply_markup=stars_duration_keyboard(plan)
                    )
                except Exception:
                    await query.message.reply_text(
                        text, reply_markup=stars_duration_keyboard(plan)
                    )
                return

            if sub == "sduration":
                plan, months = parts[2], int(parts[3])
                try:
                    total, discount, full = stars_service.invoice_details_for(plan, months)
                except ValueError as e:
                    await query.answer(f"⚠ {e}", show_alert=True)
                    return
                price_line = f"⭐{total}"
                if discount > 0:
                    price_line += f"  (was ⭐{full}, save {discount:.0f}%)"
                text = (
                    f"⭐ {plan} · {months} mo\n\n"
                    f"Total: {price_line}\n\n"
                    "Your plan activates instantly after payment. Tap below "
                    "to open the Telegram payment sheet."
                )
                try:
                    await query.edit_message_text(
                        text, reply_markup=stars_confirm_keyboard(plan, months)
                    )
                except Exception:
                    await query.message.reply_text(
                        text, reply_markup=stars_confirm_keyboard(plan, months)
                    )
                return

            if sub == "scheckout":
                plan, months = parts[2], int(parts[3])

                # Local snapshot row first (local row -> provider order
                # reference, same discipline as the crypto flow).
                try:
                    request = stars_service.create_plan_invoice_row(user_id, plan, months)
                except ValueError as e:
                    await query.answer(f"⚠ {e}", show_alert=True)
                    return

                total = int(float(request["final_amount"] or 0))
                payload = f"{stars_service.PAYLOAD_PLAN}{request['id']}"

                description = f"ChannelFlow {plan} subscription - {months} month(s)."
                discount = int(float(request["discount_percent"] or 0))
                if discount > 0:
                    description += f" Includes a {discount:.0f}% duration discount."

                try:
                    await context.bot.send_invoice(
                        chat_id=user_id,
                        title=f"ChannelFlow {plan} - {months} mo",
                        description=description,
                        payload=payload,
                        provider_token="",
                        currency=stars_service.STARS_CURRENCY,
                        prices=[LabeledPrice(f"{plan} {months} mo", total)],
                    )
                except Exception:
                    stars_service.cancel_request(request["id"])
                    await query.message.reply_text(
                        "❌ Could not open the Stars checkout right now. "
                        "Please try again shortly."
                    )
                    return

                await query.message.reply_text(
                    "⭐ Payment sheet sent!\n\n"
                    "Complete the payment inside Telegram and your plan "
                    "activates instantly. This invoice expires in "
                    f"{stars_service.STARS_INVOICE_LIFETIME_MINUTES} minutes."
                )
                return

            # ======================================
            # LEGACY/UPI/CRYPTO CHAIN (method-first, admin-reviewed)
            # ======================================

            if sub == "plan":

                plan = parts[2]

                options = pricing_service.get_duration_options(plan)
                buttons = []

                for row in options:
                    pricing = pricing_service.calculate_price(plan, row["months"], row["discount_percent"])
                    label = pricing_service.format_duration_label(pricing)
                    buttons.append([InlineKeyboardButton(label, callback_data=f"upgrade:duration:{plan}:{row['months']}")])

                buttons.append([InlineKeyboardButton("⬅ Back", callback_data="acct:upgrade")])

                await query.message.reply_text(
                    f"💎 {plan}\n\nChoose a duration:",
                    reply_markup=InlineKeyboardMarkup(buttons)
                )
                return

            if sub == "duration":

                plan, months = parts[2], int(parts[3])

                buttons = []

                if payment_service.is_oxapay_configured():
                    buttons.append([InlineKeyboardButton("🪙 Crypto (OXAPAY)", callback_data=f"upgrade:method:{plan}:{months}:crypto")])

                if payment_service.is_upi_configured():
                    buttons.append([InlineKeyboardButton("🇮🇳 UPI", callback_data=f"upgrade:method:{plan}:{months}:upi")])

                if not buttons:
                    await query.message.reply_text(
                        "⚠ No payment method is configured yet. Contact the admin."
                    )
                    return

                buttons.append([InlineKeyboardButton("⬅ Back", callback_data=f"upgrade:plan:{plan}")])

                options = {row["months"]: row["discount_percent"] for row in pricing_service.get_duration_options(plan)}
                pricing = pricing_service.calculate_price(plan, months, options.get(months, 0))

                await query.message.reply_text(
                    f"💎 {plan} - {pricing_service.format_duration_label(pricing)}\n\n"
                    "Choose a payment method:",
                    reply_markup=InlineKeyboardMarkup(buttons)
                )
                return

            if sub == "method":

                plan, months, method = parts[2], int(parts[3]), parts[4]

                options = {row["months"]: row["discount_percent"] for row in pricing_service.get_duration_options(plan)}
                pricing = pricing_service.calculate_price(plan, months, options.get(months, 0))

                if method == "crypto":

                    try:
                        pay_link, track_id = await payment_service.create_oxapay_invoice(
                            pricing["price_usd"], order_id=f"u{user_id}-{plan}-{months}mo"
                        )
                    except RuntimeError as e:
                        await query.message.reply_text(f"❌ {e}")
                        return

                    request_id = payment_service.create_payment_request(
                        user_id, plan, "crypto", payment_reference=pay_link, months=months
                    )

                    text = (
                        f"🪙 {plan} ({months} mo) - ${pricing['price_usd']} in crypto\n\n"
                        f"Pay here: {pay_link}\n\n"
                        "After paying, tap Verify and send a screenshot."
                    )

                else:

                    request_id = payment_service.create_payment_request(
                        user_id, plan, "upi", payment_reference=UPI_ID, months=months
                    )

                    text = (
                        f"🇮🇳 {plan} ({months} mo) - ₹{pricing['price_inr']:.0f} via UPI\n\n"
                        f"Pay to: {UPI_ID}\n"
                        f"Name: {UPI_PAYEE_NAME or '-'}\n\n"
                        "After paying, tap Verify and send a screenshot."
                    )

                buttons = InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Verify", callback_data=f"upgrade:verify:{request_id}"),
                    InlineKeyboardButton("❌ Cancel", callback_data=f"upgrade:cancel:{request_id}"),
                ]])

                await query.message.reply_text(text, reply_markup=buttons)
                return

            if sub == "verify":

                request_id = int(parts[2])
                request = payment_service.get_payment_request(request_id)

                if request is None or request["user_id"] != user_id or request["status"] != "PENDING_PAYMENT":
                    await query.answer("⚠ This payment request is no longer active.", show_alert=True)
                    return

                WAITING_PAYMENT_SCREENSHOT[user_id] = request_id

                await query.message.reply_text("📸 Send a screenshot of your payment.")
                return

            if sub == "cancel":

                request_id = int(parts[2])
                request = payment_service.get_payment_request(request_id)

                if request and request["user_id"] == user_id:
                    payment_service.cancel_payment_request(request_id)
                    WAITING_PAYMENT_SCREENSHOT.pop(user_id, None)
                    await query.message.reply_text("❌ Cancelled.")

                return

            if sub == "approve":

                if user_id not in ADMIN_IDS:
                    await query.answer("⛔ Admins only.", show_alert=True)
                    return

                request_id = int(parts[2])
                approved = payment_service.approve_payment(request_id, admin_id=user_id)

                if approved is None:
                    await query.message.reply_text("⚠ Already decided or not ready for review.")
                    return

                await query.message.reply_text(
                    f"✅ Approved: {_payment_request_line(approved)}."
                )

                if approved["purpose"] == "extra_credit":
                    n = int(approved["extra_forwards"] or 0)
                    user_notice = (
                        f"✅ Your payment was approved - ⚡ {n:,} Extra "
                        "Credits were added to your balance!"
                    )
                else:
                    user_notice = (
                        f"✅ Your payment was approved - you're now on "
                        f"{approved['plan']}!"
                    )
                try:
                    await context.bot.send_message(approved["user_id"], user_notice)
                except Exception:
                    pass

                return

            if sub == "reject":

                if user_id not in ADMIN_IDS:
                    await query.answer("⛔ Admins only.", show_alert=True)
                    return

                request_id = int(parts[2])
                request = payment_service.get_payment_request(request_id)
                rejected = payment_service.reject_payment(request_id, admin_id=user_id)

                if not rejected:
                    await query.message.reply_text("⚠ Already decided or not ready for review.")
                    return

                await query.message.reply_text("❌ Rejected.")

                if request:
                    try:
                        await context.bot.send_message(
                            request["user_id"],
                            "❌ Your payment couldn't be verified. Contact Support if you believe this is a mistake."
                        )
                    except Exception:
                        pass

                return

            return

        # ======================================
        # PROJECT CREATION: PLATFORM SELECTION
        # ======================================

        if action == "platform":

            platform_type = parts[1]

            if platform_type == "locked":
                await query.answer(
                    "🚧 Not live yet - no verified provider for this platform exists. "
                    "Telegram → Telegram is ready to use now.",
                    show_alert=True,
                )
                return

            if platform_type != "telegram":
                # instagram_broadcast/both removed from the platform
                # roadmap (platform priority migration) - no longer
                # offered by platform_selection_keyboard, but guard here
                # too in case a stale keyboard from before the change is
                # still on someone's screen.
                await query.message.reply_text(
                    "❌ That platform is no longer available. Choose Telegram → Telegram."
                )
                return

            await _handle_platform_selected(query.message, user_id, platform_type)
            return

        # ======================================
        # INSTAGRAM MANAGEMENT
        # ======================================

        if action == "instagram":

            project_id = int(parts[1])
            await _show_instagram_screen(query.message, project_id, user_id, edit=True)
            return

        if action == "igsetprocessing":

            project_id = int(parts[1])
            project = _get_owned_project(project_id, user_id)

            if project is None:
                await query.message.reply_text("❌ Project Not Found")
                return

            WAITING_PROCESSING_CHANNEL[user_id] = project_id

            await query.message.reply_text(
                "📝 Send the converter output channel's username (e.g. @ConvertedDeals).\n\n"
                "This is the channel the external affiliate-converter bot posts "
                "its converted output into - not a source, not a Telegram "
                "destination."
            )
            return

        if action == "igadddest":

            project_id = int(parts[1])
            project = _get_owned_project(project_id, user_id)

            if project is None:
                await query.message.reply_text("❌ Project Not Found")
                return

            await query.message.reply_text(
                "Choose how this Instagram destination should publish:",
                reply_markup=instagram_destination_type_keyboard(project_id),
            )
            return

        if action == "igtargettype":

            project_id = int(parts[1])
            target_type = parts[2]
            project = _get_owned_project(project_id, user_id)

            if project is None:
                await query.message.reply_text("❌ Project Not Found")
                return

            WAITING_INSTAGRAM_DESTINATION[user_id] = {
                "project_id": project_id,
                "target_type": target_type,
            }

            prompt = (
                "📝 Send the Instagram Business Account ID to attach.\n\n"
                if target_type == "feed"
                else
                "📝 Send an identifier for this Broadcast Channel (any label "
                "you'll recognize - there's no API to look it up automatically).\n\n"
            )

            await query.message.reply_text(prompt + "Find your Business Account ID in Meta Business Suite settings.")
            return

        if action == "igdestination":

            destination_id = int(parts[1])
            destination = instagram_service.get_instagram_destination(destination_id)

            if destination is None:
                await query.message.reply_text("❌ Destination Not Found")
                return

            project = _get_owned_project(destination["project_id"], user_id)

            if project is None:
                await query.message.reply_text("❌ Not authorized for this destination")
                return

            status_label = "🟢 Available" if destination["status"] == "available" else "🟡 Setup Required"

            await query.message.reply_text(
                f"📸 {destination['username'] or destination['ig_user_id']}\n\n"
                f"Type: {destination['target_type']}\n"
                f"Status: {status_label}\n\n"
                "Use the buttons below to manage it.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🗑 Remove", callback_data=f"igdeldest:{destination_id}:{destination['project_id']}")],
                    [InlineKeyboardButton("⬅ Back", callback_data=f"instagram:{destination['project_id']}")],
                ]),
            )
            return

        if action == "igdeldest":

            destination_id = int(parts[1])
            project_id = int(parts[2])
            project = _get_owned_project(project_id, user_id)

            if project is None:
                await query.message.reply_text("❌ Project Not Found")
                return

            instagram_service.delete_instagram_destination(destination_id)

            await query.message.reply_text("🗑 Instagram destination removed.")
            await _show_instagram_screen(query.message, project_id, user_id)
            return

        if action == "igqueue":

            project_id = int(parts[1])
            offset = int(parts[2])

            await _show_approval_queue_item(query.message, project_id, user_id, offset, edit=True)
            return

        if action in ("igjobdone", "igjobskip"):

            job_id = int(parts[1])
            project_id = int(parts[2])
            offset = int(parts[3])

            project = _get_owned_project(project_id, user_id)

            if project is None:
                await query.message.reply_text("❌ Project Not Found")
                return

            processing_service.set_status(
                job_id, "PUBLISHED" if action == "igjobdone" else "SKIPPED"
            )

            await _show_approval_queue_item(query.message, project_id, user_id, offset, edit=True)
            return

        # ======================================
        # ADD SOURCE
        # ======================================

        if action == "source":

            project_id = int(parts[1])
            project = _get_owned_project(project_id, user_id)

            if project is None:
                await query.message.reply_text("❌ Project Not Found")
                return

            _reset_waiting_states(user_id)

            # Batch 4 / UX-NAV-04: gate before prompting so a user at
            # the source cap never types a name for nothing.
            allowed, reason = plan_service.can_add_source(user_id, project_id)
            if not allowed:
                await nav_state.place(
                    query.message, user_id, "locked",
                    _locked_text(reason),
                    reply_markup=_locked_markup(), edit=True,
                )
                return

            CURRENT_PROJECT[user_id] = project_id
            WAITING_SOURCE[user_id] = True

            await query.message.reply_text(
                "📥 Send Source Username\n\n"
                "Example:\n"
                "@YourChannel"
            )

            return

        # ======================================
        # ADD DESTINATION
        # ======================================

        if action == "destination":

            project_id = int(parts[1])
            project = _get_owned_project(project_id, user_id)

            if project is None:
                await query.message.reply_text("❌ Project Not Found")
                return

            _reset_waiting_states(user_id)

            # Batch 4 / UX-NAV-04: gate before prompting (destination cap).
            allowed, reason = plan_service.can_add_destination(user_id, project_id)
            if not allowed:
                await nav_state.place(
                    query.message, user_id, "locked",
                    _locked_text(reason),
                    reply_markup=_locked_markup(), edit=True,
                )
                return

            CURRENT_PROJECT[user_id] = project_id
            WAITING_DESTINATION[user_id] = True

            await query.message.reply_text(
                "📤 Send Destination Username\n\n"
                "Example:\n"
                "@YourChannel"
            )

            return

        # ======================================
        # LIST SOURCES
        # ======================================

        if action == "listsource":

            project_id = int(parts[1])
            project = _get_owned_project(project_id, user_id)

            if project is None:
                await query.message.reply_text("❌ Project Not Found")
                return

            sources = get_sources(project_id)

            if not sources:

                await query.message.reply_text(
                    "❌ No Sources Added\n\n"
                    "Tap ➕ Source on the task dashboard to add one.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("⬅ Back to Task", callback_data=f"projcard:{project_id}"),
                        InlineKeyboardButton("🏠 Home", callback_data="nav:home"),
                    ]]),
                )

                return

            for source in sources:

                await query.message.reply_text(
                    _source_card_text(source),
                    reply_markup=source_item_keyboard(
                        source["id"], project_id, bool(source["enabled"])
                    )
                )

            return

        # ======================================
        # LIST DESTINATIONS
        # ======================================

        if action == "listdestination":

            project_id = int(parts[1])
            project = _get_owned_project(project_id, user_id)

            if project is None:
                await query.message.reply_text("❌ Project Not Found")
                return

            destinations = get_destinations(project_id)

            if not destinations:

                await query.message.reply_text(
                    "❌ No Destinations Added\n\n"
                    "Tap ➕ Destination on the task dashboard to add one.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("⬅ Back to Task", callback_data=f"projcard:{project_id}"),
                        InlineKeyboardButton("🏠 Home", callback_data="nav:home"),
                    ]]),
                )

                return

            for destination in destinations:

                await query.message.reply_text(
                    _destination_card_text(destination),
                    reply_markup=destination_item_keyboard(
                        destination["id"], project_id, bool(destination["enabled"])
                    )
                )

            return

        # ======================================
        # START PROJECT
        # ======================================

        if action == "start":

            project_id = int(parts[1])
            project = _get_owned_project(project_id, user_id)

            if project is None:
                await query.answer("⚠ This task no longer exists.", show_alert=True)
                return

            if count_sources(project_id) == 0:
                await query.answer("⚠ Add at least one source before starting.", show_alert=True)
                return

            if count_destinations(project_id) == 0:
                await query.answer("⚠ Add at least one destination before starting.", show_alert=True)
                return

            update_status(project_id, 1)
            await force_refresh_routes()

            # If this owner has connected their own Telegram account,
            # make sure their dedicated engine is running now that they
            # have an active project - start_owner_engine no-ops
            # instantly if it's already running or if they haven't
            # connected at all.
            if user_sessions.is_connected(user_id):
                await client_pool.start_owner_engine(user_id)
            await client_pool.force_refresh_owner_routes(user_id)

            project = get_project(project_id)
            notice = "🟢 " + i18n.t(user_id, "task.started", name=project["name"])
            await _show_task_detail(query.message, project_id, user_id, edit=True, notice=notice)
            return

        # ======================================
        # STOP PROJECT
        # ======================================

        if action == "stop":

            project_id = int(parts[1])
            project = _get_owned_project(project_id, user_id)

            if project is None:
                await query.answer("⚠ This task no longer exists.", show_alert=True)
                return

            update_status(project_id, 0)
            await force_refresh_routes()

            project = get_project(project_id)
            notice = "🔴 " + i18n.t(user_id, "task.stopped", name=project["name"])
            await _show_task_detail(query.message, project_id, user_id, edit=True, notice=notice)
            return

        # ======================================
        # DELETE SOURCE
        # ======================================

        if action == "deletesource":

            source_id = int(parts[1])

            source, project = _get_owned_source(source_id, user_id)

            if source is None:

                await query.message.reply_text("❌ Source Not Found")
                return

            delete_source(source_id)
            await force_refresh_routes()

            text = "✅ Source Removed\n\n" f"📂 {source['title'] or source['chat_id']}"
            markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅ Sources", callback_data=f"listsource:{project['id']}"),
                InlineKeyboardButton("🏠 Home", callback_data="nav:home"),
            ]])
            try:
                await query.edit_message_text(text, reply_markup=markup,
                                              disable_web_page_preview=True)
            except Exception:
                await query.message.reply_text(text, reply_markup=markup)

            return

        # ======================================
        # DELETE DESTINATION
        # ======================================

        if action == "deletedestination":

            destination_id = int(parts[1])

            destination, project = _get_owned_destination(destination_id, user_id)

            if destination is None:

                await query.message.reply_text("❌ Destination Not Found")
                return

            delete_destination(destination_id)
            await force_refresh_routes()

            text = "✅ Destination Removed\n\n" f"📂 {destination['title'] or destination['chat_id']}"
            markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅ Destinations", callback_data=f"listdestination:{project['id']}"),
                InlineKeyboardButton("🏠 Home", callback_data="nav:home"),
            ]])
            try:
                await query.edit_message_text(text, reply_markup=markup,
                                              disable_web_page_preview=True)
            except Exception:
                await query.message.reply_text(text, reply_markup=markup)

            return

        # ======================================
        # TEST DESTINATION
        # ======================================

        if action == "testdestination":

            destination_id = int(parts[1])

            destination, project = _get_owned_destination(destination_id, user_id)

            if destination is None:
                await query.message.reply_text("❌ Destination Not Found")
                return

            await query.message.reply_text("🧪 Sending a real test message...")

            ok, detail = await send_test_message(destination["chat_id"], project["name"])

            label = destination["title"] or destination["username"] or destination["chat_id"]

            if ok:
                await query.message.reply_text(f"✅ Test Passed\n\n📤 {label} - message delivered.")
            else:
                await query.message.reply_text(f"❌ Test Failed\n\n📤 {label}\n\n{detail}")

            return

        # ======================================
        # TEST ALL DESTINATIONS FOR A PROJECT
        # ======================================

        if action == "testproject":

            project_id = int(parts[1])
            project = _get_owned_project(project_id, user_id)

            if project is None:
                await query.message.reply_text("❌ Project Not Found")
                return

            destinations = get_destinations(project_id)

            if not destinations:
                await query.message.reply_text(
                    "❌ No Destinations Added\n\n"
                    "Tap ➕ Destination on the project dashboard to add one."
                )
                return

            await query.message.reply_text(
                f"🧪 Testing {len(destinations)} destination(s)..."
            )

            lines = []

            for destination in destinations:

                ok, detail = await send_test_message(destination["chat_id"], project["name"])
                label = destination["title"] or destination["username"] or destination["chat_id"]

                lines.append(
                    f"✅ {label}" if ok else f"❌ {label} - {detail}"
                )

            await query.message.reply_text(
                "🧪 Test Results\n\n" + "\n".join(lines)
            )

            return

        # ======================================
        # RENAME PROJECT
        # ======================================

        if action == "rename":

            project_id = int(parts[1])
            project = _get_owned_project(project_id, user_id)

            if project is None:
                await query.message.reply_text("❌ Project Not Found")
                return

            _reset_waiting_states(user_id)

            CURRENT_PROJECT[user_id] = project_id
            WAITING_RENAME[user_id] = True

            await query.message.reply_text(
                "✏ Send New Project Name"
            )

            return

        # ======================================
        # DELETE PROJECT
        # ======================================

        if action == "delete":

            project_id = int(parts[1])
            project = _get_owned_project(project_id, user_id)

            if project is None:
                await query.answer("⚠ This task no longer exists.", show_alert=True)
                return

            delete_project(project_id)
            await force_refresh_routes()

            if CURRENT_PROJECT.get(user_id) == project_id:
                _reset_waiting_states(user_id)
                CURRENT_PROJECT.pop(user_id, None)

            nav_state.drop_screens_to(user_id, "tasks")
            deleted_line = i18n.t(user_id, "task.deleted", name=project["name"])

            # Refresh the task list in the same message - empty state
            # when the last task was just deleted (UX-NAV-02 93.2);
            # pagination/search state is re-derived (Batch 4:
            # page clamps when the last item on it disappears).
            text, markup, _pages = _task_list_payload(user_id)
            text = f"{deleted_line}\n\n{text}"

            try:
                await query.edit_message_text(text, reply_markup=markup,
                                              disable_web_page_preview=True)
            except Exception:
                await query.message.reply_text(text, reply_markup=markup)
            return

        # ======================================
        # SETTINGS
        # ======================================

        if action == "settings":

            sub_action = parts[1] if len(parts) > 1 else ""

            if sub_action == "refresh":

                total = count_projects(user_id)
                projects = get_projects(user_id)
                running = sum(1 for project in projects if project["status"])
                engine_state = "🟢 Online" if is_running() else "🔴 Offline"
                maintenance = context.bot_data.get("maintenance_mode", False)

                try:

                    await query.edit_message_text(

                        "⚙ ChannelFlow Settings\n\n"
                        f"📁 Projects : {total}\n"
                        f"▶ Running : {running}\n"
                        f"⏹ Stopped : {total - running}\n\n"
                        f"🚀 Forward Engine : {engine_state}\n"
                        f"🔧 Maintenance Mode : {'On' if maintenance else 'Off'}\n"
                        "📡 Mode : Native Forward",

                        reply_markup=settings_keyboard(wallet_service.is_auto_renew_enabled(user_id))

                    )

                except Exception:
                    # message content/markup unchanged - safe to ignore
                    pass

                return

            if sub_action == "disconnect":

                await client_pool.stop_owner_engine(user_id)
                user_sessions.disconnect_user(user_id)

                # UX-NAV-01 92.3: after disconnect the UI automatically
                # returns to the unconnected state - the connected
                # menu is replaced by the unconnected one and no
                # connected-only screens stay tracked.
                nav_state.clear_screens(user_id)
                CURRENT_PROJECT.pop(user_id, None)
                _clear_task_browse(user_id)

                await query.message.reply_text(
                    "🔌 Disconnected. Your Telegram session was deleted. "
                    "Send /start or tap Connect Account to connect again.",
                    reply_markup=menu_unconnected_keyboard()
                )

                return

            if sub_action == "wallet":

                balance = wallet_service.get_balance(user_id)

                buttons = InlineKeyboardMarkup([
                    [InlineKeyboardButton("➕ Add Funds", callback_data="wallet:topup")],
                    [InlineKeyboardButton("⬅ Back", callback_data="settings:refresh")],
                ])

                await query.message.reply_text(
                    f"💰 Wallet Balance: ${balance:.2f}\n\n"
                    "Used for Auto-Renew - top up here, an admin approves "
                    "it the same way as a plan payment, then it's available.",
                    reply_markup=buttons
                )
                return

            if sub_action == "togglerenew":

                if not user_sessions.is_connected(user_id) and user_id not in ADMIN_IDS:
                    await query.answer("Connect your account first.", show_alert=True)
                    return

                new_state = not wallet_service.is_auto_renew_enabled(user_id)
                wallet_service.set_auto_renew(user_id, new_state)

                if new_state:
                    balance = wallet_service.get_balance(user_id)
                    await query.answer(
                        f"✅ Auto-Renew ON (wallet: ${balance:.2f})",
                        show_alert=True
                    )
                else:
                    await query.answer("Auto-Renew OFF", show_alert=True)

                try:
                    await query.edit_message_reply_markup(
                        reply_markup=settings_keyboard(new_state)
                    )
                except Exception:
                    pass

                return

            if sub_action == "language":
                try:
                    await query.edit_message_text(
                        "🌐 Language\n\nChoose your language:",
                        reply_markup=LANGUAGE_KEYBOARD,
                    )
                except Exception:
                    await query.message.reply_text("🌐 Language", reply_markup=LANGUAGE_KEYBOARD)
                return

            if sub_action == "systatus":
                total = count_projects(user_id)
                running = sum(1 for p in get_projects(user_id) if p["status"])
                engine_state = "🟢 Online" if is_running() else "🔴 Offline"
                maintenance = context.bot_data.get("maintenance_mode", False)
                text = (
                    "📊 System Status\n\n"
                    f"📁 Tasks: {total}\n"
                    f"▶ Running: {running}\n"
                    f"⏹ Stopped: {total - running}\n\n"
                    f"🚀 Forward Engine: {engine_state}\n"
                    f"🔧 Maintenance Mode: {'On' if maintenance else 'Off'}\n"
                    "📡 Mode: Native Forward"
                )
                markup = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("⬅ Settings", callback_data="nav:settings"),
                        InlineKeyboardButton("🏠 Home", callback_data="nav:home"),
                    ]
                ])
                await nav_state.place(query.message, user_id, "settings_systatus", text,
                                      reply_markup=markup, edit=True)
                return

            await query.message.reply_text("⚠ Unknown Settings Action")
            return

        # ======================================
        # WALLET TOP-UP
        # ======================================

        if action == "wallet":

            sub = parts[1] if len(parts) > 1 else None

            if sub == "topup":

                buttons = InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"${amt}", callback_data=f"wallet:amount:{amt}") for amt in (10, 25, 50)],
                    [InlineKeyboardButton("⬅ Back", callback_data="settings:wallet")],
                ])

                await query.message.reply_text(
                    "➕ Add Funds\n\nChoose an amount:",
                    reply_markup=buttons
                )
                return

            if sub == "amount":

                amount = float(parts[2])

                buttons = []

                if payment_service.is_oxapay_configured():
                    buttons.append([InlineKeyboardButton("🪙 Crypto (OXAPAY)", callback_data=f"wallet:method:{amount}:crypto")])

                if payment_service.is_upi_configured():
                    buttons.append([InlineKeyboardButton("🇮🇳 UPI", callback_data=f"wallet:method:{amount}:upi")])

                if not buttons:
                    await query.message.reply_text("⚠ No payment method is configured yet. Contact the admin.")
                    return

                buttons.append([InlineKeyboardButton("⬅ Back", callback_data="settings:wallet")])

                await query.message.reply_text(
                    f"➕ Add ${amount:.2f}\n\nChoose a payment method:",
                    reply_markup=InlineKeyboardMarkup(buttons)
                )
                return

            if sub == "method":

                amount, method = float(parts[2]), parts[3]

                if method == "crypto":

                    try:
                        pay_link, track_id = await payment_service.create_oxapay_invoice(
                            amount, order_id=f"u{user_id}-topup"
                        )
                    except RuntimeError as e:
                        await query.message.reply_text(f"❌ {e}")
                        return

                    request_id = payment_service.create_wallet_topup_request(
                        user_id, amount, "crypto", payment_reference=pay_link
                    )

                    text = f"🪙 Add ${amount:.2f} in crypto\n\nPay here: {pay_link}\n\nAfter paying, tap Verify and send a screenshot."

                else:

                    request_id = payment_service.create_wallet_topup_request(
                        user_id, amount, "upi", payment_reference=UPI_ID
                    )

                    inr_amount = amount * pricing_service.INR_PER_USD

                    text = (
                        f"🇮🇳 Add ₹{inr_amount:.0f} via UPI\n\n"
                        f"Pay to: {UPI_ID}\n"
                        f"Name: {UPI_PAYEE_NAME or '-'}\n\n"
                        "After paying, tap Verify and send a screenshot."
                    )

                buttons = InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Verify", callback_data=f"upgrade:verify:{request_id}"),
                    InlineKeyboardButton("❌ Cancel", callback_data=f"upgrade:cancel:{request_id}"),
                ]])

                await query.message.reply_text(text, reply_markup=buttons)
                return

            return

        # ======================================
        # TOGGLE SOURCE ENABLED
        # ======================================

        if action == "togglesource":

            source_id = int(parts[1])
            source, project = _get_owned_source(source_id, user_id)

            if source is None:
                await query.message.reply_text("❌ Source Not Found")
                return

            new_value = toggle_source_enabled(source_id)
            await force_refresh_routes()
            source = get_source(source_id)

            await query.message.edit_text(
                _source_card_text(source),
                reply_markup=source_item_keyboard(source_id, project["id"], bool(new_value))
            )

            return

        # ======================================
        # TOGGLE DESTINATION ENABLED
        # ======================================

        if action == "toggledestination":

            destination_id = int(parts[1])
            destination, project = _get_owned_destination(destination_id, user_id)

            if destination is None:
                await query.message.reply_text("❌ Destination Not Found")
                return

            new_value = toggle_destination_enabled(destination_id)
            await force_refresh_routes()
            destination = get_destination(destination_id)

            await query.message.edit_text(
                _destination_card_text(destination),
                reply_markup=destination_item_keyboard(
                    destination_id, project["id"], bool(new_value)
                )
            )

            return

        # ======================================
        # BACK TO PROJECT DASHBOARD
        # ======================================

        if action == "backproject":

            project_id = int(parts[1])
            project = _get_owned_project(project_id, user_id)

            if project is None:
                await query.message.reply_text("❌ Project Not Found")
                return

            await query.message.edit_text(
                _project_card_text(project),
                reply_markup=project_keyboard(project_id, platform_type=project["platform_type"])
            )

            return

        # ======================================
        # PROJECT FORWARD SETTINGS
        # ======================================

        if action == "projsettings":

            project_id = int(parts[1])
            project = _get_owned_project(project_id, user_id)

            if project is None:
                await query.message.reply_text("❌ Project Not Found")
                return

            CURRENT_PROJECT[user_id] = project_id
            settings = settings_service.get_settings(project_id)

            await query.message.reply_text(
                f"⚙ Forward Settings\n\n📂 {project['name']}",
                reply_markup=project_settings_keyboard(project_id, settings)
            )

            return

        if action == "togglemode":

            project_id = int(parts[1])
            project = _get_owned_project(project_id, user_id)

            if project is None:
                await query.message.reply_text("❌ Project Not Found")
                return

            settings_service.toggle_mode(project_id)
            await force_refresh_routes()
            settings = settings_service.get_settings(project_id)

            await query.message.edit_reply_markup(
                reply_markup=project_settings_keyboard(project_id, settings)
            )

            return

        if action in ("togglesilent", "toggleprotect", "togglealbum"):

            field_map = {
                "togglesilent": "silent",
                "toggleprotect": "protect_content",
                "togglealbum": "keep_media_groups",
            }

            project_id = int(parts[1])
            project = _get_owned_project(project_id, user_id)

            if project is None:
                await query.message.reply_text("❌ Project Not Found")
                return

            settings_service.toggle_flag(project_id, field_map[action])
            await force_refresh_routes()
            settings = settings_service.get_settings(project_id)

            await query.message.edit_reply_markup(
                reply_markup=project_settings_keyboard(project_id, settings)
            )

            return

        if action == "setdelay":

            project_id = int(parts[1])
            project = _get_owned_project(project_id, user_id)

            if project is None:
                await query.message.reply_text("❌ Project Not Found")
                return

            _reset_waiting_states(user_id)
            CURRENT_PROJECT[user_id] = project_id
            WAITING_DELAY[user_id] = True

            await query.message.reply_text(
                "⏱ Send Delay\n\n"
                "Single number for a fixed delay (seconds), e.g. `5`\n"
                "Two numbers for a random range, e.g. `2,8`"
            )

            return

        # ======================================
        # PROJECT FILTER SETTINGS
        # ======================================

        if action == "projfilters":

            project_id = int(parts[1])
            project = _get_owned_project(project_id, user_id)

            if project is None:
                await query.message.reply_text("❌ Project Not Found")
                return

            CURRENT_PROJECT[user_id] = project_id
            settings = settings_service.get_settings(project_id)
            content_rules = content_rules_service.get_rules(project_id)

            await query.message.reply_text(
                f"🧹 Filters\n\n📂 {project['name']}",
                reply_markup=project_filters_keyboard(project_id, settings, content_rules)
            )

            return

        if action == "mediafilter":

            project_id = int(parts[1])
            project = _get_owned_project(project_id, user_id)

            if project is None:
                await query.message.reply_text("❌ Project Not Found")
                return

            await query.message.edit_reply_markup(
                reply_markup=media_filter_choice_keyboard(project_id)
            )

            return

        if action == "mediafilterset":

            project_id = int(parts[1])
            choice = parts[2]
            project = _get_owned_project(project_id, user_id)

            if project is None:
                await query.message.reply_text("❌ Project Not Found")
                return

            settings_service.set_media_filter(project_id, choice)
            await force_refresh_routes()
            settings = settings_service.get_settings(project_id)
            content_rules = content_rules_service.get_rules(project_id)

            await query.message.edit_reply_markup(
                reply_markup=project_filters_keyboard(project_id, settings, content_rules)
            )

            return

        if action == "setwhitelist":

            project_id = int(parts[1])
            project = _get_owned_project(project_id, user_id)

            if project is None:
                await query.message.reply_text("❌ Project Not Found")
                return

            _reset_waiting_states(user_id)
            CURRENT_PROJECT[user_id] = project_id
            WAITING_WHITELIST[user_id] = True

            await query.message.reply_text(
                "✅ Send Whitelist Keywords\n\n"
                "Comma-separated. Only messages containing at least one "
                "will be forwarded. Send `-` to clear."
            )

            return

        if action == "setblacklist":

            project_id = int(parts[1])
            project = _get_owned_project(project_id, user_id)

            if project is None:
                await query.message.reply_text("❌ Project Not Found")
                return

            _reset_waiting_states(user_id)
            CURRENT_PROJECT[user_id] = project_id
            WAITING_BLACKLIST[user_id] = True

            await query.message.reply_text(
                "🚫 Send Blacklist Keywords\n\n"
                "Comma-separated. Messages containing any of these will "
                "be skipped. Send `-` to clear."
            )

            return

        if action == "setregex":

            project_id = int(parts[1])
            project = _get_owned_project(project_id, user_id)

            if project is None:
                await query.message.reply_text("❌ Project Not Found")
                return

            _reset_waiting_states(user_id)
            CURRENT_PROJECT[user_id] = project_id
            WAITING_REGEX[user_id] = True

            await query.message.reply_text(
                "🔤 Send Regex Pattern\n\n"
                "Only messages whose text matches will be forwarded. "
                "Send `-` to clear."
            )

            return

        # ======================================
        # CONTENT RULES (Phase 7)
        # ======================================

        if action == "crfield":

            project_id = int(parts[1])
            field = parts[2]
            project = _get_owned_project(project_id, user_id)

            if project is None:
                await query.message.reply_text("❌ Project Not Found")
                return

            _reset_waiting_states(user_id)
            CURRENT_PROJECT[user_id] = project_id
            WAITING_CONTENT_RULE_FIELD[user_id] = {"project_id": project_id, "field": field}

            prompts = {
                "required_keywords": "🔑 Send required keywords, comma-separated. "
                                      "Use the ALL/ANY toggle to control whether all or any "
                                      "must be present. Send `-` to clear.",
                "hashtag_filter": "#️⃣ Send required hashtags, comma-separated (e.g. `#deal, #sale`). "
                                   "At least one must be present. Send `-` to clear.",
                "domain_whitelist": "🌐 Send allowed domains, comma-separated (e.g. `amazon.in, flipkart.com`). "
                                     "Only messages linking to these will be forwarded. Send `-` to clear.",
                "domain_blacklist": "🌐 Send blocked domains, comma-separated. "
                                     "Messages linking to these will be skipped. Send `-` to clear.",
                "length": "📏 Send `min,max` text length (e.g. `10,500`). Leave either side blank "
                          "for no bound on that side. Send `-` to clear.",
                "sender_whitelist": "👤 Send allowed sender Telegram user IDs, comma-separated. "
                                     "Only relevant in group sources. Send `-` to clear.",
                "sender_blacklist": "👤 Send blocked sender Telegram user IDs, comma-separated. "
                                     "Send `-` to clear.",
            }

            await query.message.reply_text(prompts.get(field, "Send a value, or `-` to clear."))

            return

        if action == "crlogic":

            project_id = int(parts[1])
            project = _get_owned_project(project_id, user_id)

            if project is None:
                await query.message.reply_text("❌ Project Not Found")
                return

            content_rules = content_rules_service.get_rules(project_id)
            new_logic = "all" if content_rules["filter_logic"] == "any" else "any"
            content_rules_service.update_rules(project_id, filter_logic=new_logic)
            await force_refresh_routes()

            settings = settings_service.get_settings(project_id)
            content_rules = content_rules_service.get_rules(project_id)

            await query.message.edit_reply_markup(
                reply_markup=project_filters_keyboard(project_id, settings, content_rules)
            )

            return

        # ======================================
        # FORMATTING (Phase 7)
        # ======================================

        if action == "formatting":

            project_id = int(parts[1])
            project = _get_owned_project(project_id, user_id)

            if project is None:
                await query.answer("⚠ This task no longer exists.", show_alert=True)
                return

            CURRENT_PROJECT[user_id] = project_id
            rules = formatting_service.get_rules(project_id)

            text = (
                f"📝 Formatting — {project['name']}\n\n"
                "Applies in Telegram Copy mode and to Instagram captions. "
                "Native Telegram Forward mode can't have its content "
                "edited, so formatting never applies there."
            )
            await nav_state.place(query.message, user_id, f"formatting:{project_id}",
                                  text, reply_markup=formatting_keyboard(project_id, rules), edit=True)
            return

        if action == "fmtfield":

            project_id = int(parts[1])
            field = parts[2]
            project = _get_owned_project(project_id, user_id)

            if project is None:
                await query.message.reply_text("❌ Project Not Found")
                return

            _reset_waiting_states(user_id)
            CURRENT_PROJECT[user_id] = project_id
            WAITING_FORMATTING_FIELD[user_id] = {"project_id": project_id, "field": field}

            await query.message.reply_text(
                f"📝 Send the {field} text, or `-` to clear."
            )

            return

        if action == "fmtreplace":

            project_id = int(parts[1])
            project = _get_owned_project(project_id, user_id)

            if project is None:
                await query.message.reply_text("❌ Project Not Found")
                return

            rules_list = formatting_service.get_replace_rules(project_id)

            await query.message.reply_text(
                "🔁 Replace Rules\n\nTap a rule to delete it, or add a new one.",
                reply_markup=formatting_replace_list_keyboard(project_id, rules_list)
            )

            return

        if action == "fmtreplaceadd":

            project_id = int(parts[1])
            project = _get_owned_project(project_id, user_id)

            if project is None:
                await query.message.reply_text("❌ Project Not Found")
                return

            _reset_waiting_states(user_id)
            CURRENT_PROJECT[user_id] = project_id
            WAITING_REPLACE_RULE[user_id] = {"project_id": project_id, "stage": "find"}

            await query.message.reply_text("🔁 Send the text to find (exact match).")

            return

        if action == "fmtreplacedel":

            project_id = int(parts[1])
            index = int(parts[2])
            project = _get_owned_project(project_id, user_id)

            if project is None:
                await query.message.reply_text("❌ Project Not Found")
                return

            formatting_service.remove_replace_rule(project_id, index)
            await force_refresh_routes()

            rules_list = formatting_service.get_replace_rules(project_id)

            await query.message.edit_reply_markup(
                reply_markup=formatting_replace_list_keyboard(project_id, rules_list)
            )

            return

        if action == "fmtremove":

            project_id = int(parts[1])
            project = _get_owned_project(project_id, user_id)

            if project is None:
                await query.message.reply_text("❌ Project Not Found")
                return

            patterns = formatting_service.get_remove_patterns(project_id)

            await query.message.reply_text(
                "✂ Remove Patterns\n\nTap a pattern to delete it, or add a new one.",
                reply_markup=formatting_remove_list_keyboard(project_id, patterns)
            )

            return

        if action == "fmtremoveadd":

            project_id = int(parts[1])
            project = _get_owned_project(project_id, user_id)

            if project is None:
                await query.message.reply_text("❌ Project Not Found")
                return

            _reset_waiting_states(user_id)
            CURRENT_PROJECT[user_id] = project_id
            WAITING_REMOVE_PATTERN[user_id] = {"project_id": project_id}

            await query.message.reply_text(
                "✂ Send a regex pattern (or plain text) to remove from every message."
            )

            return

        if action == "fmtremovedel":

            project_id = int(parts[1])
            index = int(parts[2])
            project = _get_owned_project(project_id, user_id)

            if project is None:
                await query.message.reply_text("❌ Project Not Found")
                return

            formatting_service.remove_remove_pattern(project_id, index)
            await force_refresh_routes()

            patterns = formatting_service.get_remove_patterns(project_id)

            await query.message.edit_reply_markup(
                reply_markup=formatting_remove_list_keyboard(project_id, patterns)
            )

            return

        if action == "fmtclear":

            project_id = int(parts[1])
            project = _get_owned_project(project_id, user_id)

            if project is None:
                await query.message.reply_text("❌ Project Not Found")
                return

            formatting_service.clear_rules(project_id)
            await force_refresh_routes()

            rules = formatting_service.get_rules(project_id)

            await query.message.edit_reply_markup(
                reply_markup=formatting_keyboard(project_id, rules)
            )

            return

        if action == "clearfilters":

            project_id = int(parts[1])
            project = _get_owned_project(project_id, user_id)

            if project is None:
                await query.message.reply_text("❌ Project Not Found")
                return

            settings_service.update_settings(
                project_id,
                media_filter="all",
                keyword_whitelist="",
                keyword_blacklist="",
                regex_filter="",
            )
            content_rules_service.clear_rules(project_id)
            await force_refresh_routes()

            settings = settings_service.get_settings(project_id)
            content_rules = content_rules_service.get_rules(project_id)

            await query.message.edit_reply_markup(
                reply_markup=project_filters_keyboard(project_id, settings, content_rules)
            )

            return

        # ======================================
        # STATS
        # ======================================

        if action == "stats":

            project_id = int(parts[1])
            project = _get_owned_project(project_id, user_id)

            if project is None:
                await query.answer("⚠ This task no longer exists.", show_alert=True)
                return

            stats = stats_service.get_stats(project_id)

            text = (
                f"📊 Stats - {project['name']}\n\n"
                f"✅ Forwarded: {stats['forwarded']}\n"
                f"❌ Failed: {stats['failed']}\n"
                f"🔁 Retried: {stats['retried']}\n"
                f"🧹 Filtered Out: {stats['filtered']}\n"
                f"🕐 Last Forward: {stats['last_forward_at'] or '-'}"
            )
            markup = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("⬅ Back to Task", callback_data=f"projcard:{project_id}"),
                    InlineKeyboardButton("🏠 Home", callback_data="nav:home"),
                ]
            ])
            await nav_state.place(query.message, user_id, f"task_stats:{project_id}", text,
                                  reply_markup=markup, edit=True)
            return

        # ======================================
        # LOGS
        # ======================================

        if action == "logs":

            project_id = int(parts[1])
            project = _get_owned_project(project_id, user_id)

            if project is None:
                await query.answer("⚠ This task no longer exists.", show_alert=True)
                return

            logs = log_service.get_logs(project_id, limit=15)
            markup = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("⬅ Back to Task", callback_data=f"projcard:{project_id}"),
                    InlineKeyboardButton("🏠 Home", callback_data="nav:home"),
                ]
            ])

            if not logs:
                await nav_state.place(query.message, user_id, f"task_logs:{project_id}",
                                      "📜 No logs yet for this project.", reply_markup=markup, edit=True)
                return

            level_icon = {"forward": "✅", "error": "❌", "retry": "🔁"}

            lines = [
                f"{level_icon.get(row['level'], 'ℹ')} [{row['created_at']}] {row['message']}"
                for row in logs
            ]

            await nav_state.place(query.message, user_id, f"task_logs:{project_id}",
                                  "📜 Recent Logs\n\n" + "\n".join(lines),
                                  reply_markup=markup, edit=True)
            return

        # ======================================
        # ADMIN PANEL
        # ======================================

        # ======================================
        # WHATSAPP PAIRING CODE FLOW (Phase 10)
        # ======================================
        if action == "admin":

            if user_id not in ADMIN_IDS:
                await query.message.reply_text(i18n.t(user_id, "error.generic_forward"), reply_markup=admin_keyboard)
                return

            sub_action = parts[1] if len(parts) > 1 else ""

            if sub_action == "refresh":

                await query.message.edit_text(
                    _admin_dashboard_text(context),
                    reply_markup=admin_keyboard
                )

                return

            if sub_action == "broadcast":

                _reset_waiting_states(user_id)
                WAITING_BROADCAST[user_id] = True

                await query.message.reply_text(
                    "📢 Send the message you want to broadcast to every bot user."
                )

                return

            if sub_action == "maintenance":

                context.bot_data["maintenance_mode"] = not context.bot_data.get(
                    "maintenance_mode", False
                )

                await query.message.edit_text(
                    _admin_dashboard_text(context),
                    reply_markup=admin_keyboard
                )

                return

            if sub_action == "payments":

                conn = get_connection()
                cur = conn.cursor()
                cur.execute(
                    "SELECT pr.*, u.username, u.first_name FROM payment_requests pr "
                    "LEFT JOIN users u ON u.telegram_id = pr.user_id "
                    "WHERE pr.status = 'SUBMITTED' ORDER BY pr.id ASC LIMIT 10"
                )
                rows = cur.fetchall()
                conn.close()

                if not rows:
                    try:
                        await query.edit_message_text("✅ No submitted payments waiting for review.",
                                                      reply_markup=admin_keyboard)
                    except Exception:
                        await query.message.reply_text("✅ No submitted payments waiting for review.",
                                                       reply_markup=admin_keyboard)
                    return

                lines = [f"💳 Payments Awaiting Review ({len(rows)})", ""]
                buttons = []
                for r in rows:
                    name = r["first_name"] or ""
                    uname = f"@{r['username']}" if r["username"] else "no username"
                    lines.append(f"#{r['id']} · {name} {uname} (ID {r['user_id']})")
                    lines.append(f"   {_payment_request_line(r)}")
                    buttons.append([
                        InlineKeyboardButton(f"✅ #{r['id']}", callback_data=f"upgrade:approve:{r['id']}"),
                        InlineKeyboardButton(f"❌ #{r['id']}", callback_data=f"upgrade:reject:{r['id']}"),
                    ])
                buttons.append([
                    InlineKeyboardButton("📜 Full history", callback_data="admin:payhistory"),
                    InlineKeyboardButton("🛠 Admin", callback_data="admin:refresh"),
                ])

                try:
                    await query.edit_message_text("\n".join(lines),
                                                  reply_markup=InlineKeyboardMarkup(buttons))
                except Exception:
                    await query.message.reply_text("\n".join(lines),
                                                   reply_markup=InlineKeyboardMarkup(buttons))
                return

            if sub_action == "payhistory":
                conn = get_connection()
                cur = conn.cursor()
                cur.execute(
                    "SELECT pr.*, u.username, u.first_name FROM payment_requests pr "
                    "LEFT JOIN users u ON u.telegram_id = pr.user_id "
                    "ORDER BY pr.id DESC LIMIT 25"
                )
                rows = cur.fetchall()
                conn.close()

                if not rows:
                    await query.message.reply_text("📜 No payments recorded yet.")
                    return
                lines = ["📜 Payment History (latest 25)", ""]
                for r in rows:
                    name = r["first_name"] or ""
                    uname = f"@{r['username']}" if r["username"] else "no username"
                    lines.append(f"#{r['id']} · {name} {uname} (ID {r['user_id']})")
                    lines.append(
                        f"   {_payment_request_line(r)} — "
                        f"{_payment_status_label(r['status'])}"
                    )
                lines.append("")
                lines.append("Stars/instant payments are marked SUCCESS; "
                             "UPI/crypto run through review.")
                buttons = InlineKeyboardMarkup([[
                    InlineKeyboardButton("💳 Review queue", callback_data="admin:payments"),
                    InlineKeyboardButton("🛠 Admin", callback_data="admin:refresh"),
                ]])
                try:
                    await query.edit_message_text("\n".join(lines),
                                                  reply_markup=buttons)
                except Exception:
                    await query.message.reply_text("\n".join(lines),
                                                   reply_markup=buttons)
                return

            await query.message.reply_text("⚠ Unknown Admin Action")
            return

        # ======================================
        # UNKNOWN CALLBACK
        # ======================================

        await _reply_stale_callback(query, "⚠ Unknown Action")

    except (IndexError, ValueError):

        logger.warning("Malformed callback data received: %s", data)

        await _reply_stale_callback(
            query,
            "⚠ This button is no longer valid. Please refresh with 📁 Projects."
        )

    except Exception as e:

        logger.exception("Unhandled error in button_handler: %s", e)

        await _reply_stale_callback(
            query,
            "⚠ Something went wrong while processing that action. Please try again."
        )


# ==========================================
# ⭐ TELEGRAM STARS - PRE-CHECKOUT + SUCCESS
# (PRD section 26; registered in main.py)
# ==========================================

def _parse_stars_payload(payload):
    """'xtr:plan:<id>' / 'xtr:pkg:<id>' -> int request id, else None.
    The row itself (looked up afterwards) is the authority on what was
    paid for - the prefix only selects the payload namespace."""
    try:
        parts = (payload or "").split(":")
        if (len(parts) == 3 and parts[0] == "xtr"
                and parts[1] in ("plan", "pkg")):
            return int(parts[2])
    except (TypeError, ValueError):
        pass
    return None


async def pre_checkout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Answers Telegram's pre_checkout_query for Stars invoices.

    Security checks before we let Telegram charge the user:
      * payload must be our xtr:plan:<id> namespace;
      * the request row must exist, belong to this user, and still be
        PENDING_PAYMENT;
      * the invoice must not have expired on our clock;
      * Telegram's reported total_amount must equal the DB snapshot.

    Any failure answers ok=False and Telegram shows the error message
    without charging (no refund needed at this stage)."""
    q = update.pre_checkout_query
    if q is None:
        return

    user_id = q.from_user.id
    request_id = _parse_stars_payload(q.invoice_payload)

    if request_id is None:
        await q.answer(False, error_message="Unrecognized invoice. Please start a fresh checkout.")
        return

    row = stars_service.get_request(request_id)

    if row is None or row["method"] != "stars" or int(row["user_id"]) != int(user_id):
        await q.answer(False, error_message="This invoice is not valid for your account. Start a fresh checkout.")
        return

    if row["status"] != "PENDING_PAYMENT":
        await q.answer(False, error_message="This invoice was already used or expired. Start a fresh checkout.")
        return

    if q.currency != stars_service.STARS_CURRENCY or \
       int(q.total_amount or 0) != int(float(row["final_amount"] or 0)):
        logger.warning("Stars pre-checkout mismatch: user=%s payload=%s currency=%s total=%s",
                       user_id, q.invoice_payload, q.currency, q.total_amount)
        await q.answer(False, error_message="Amount mismatch. Start a fresh checkout.")
        return

    await q.answer(True)


async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Telegram reported a successful Stars payment.

    The money is only converted into a plan here after
    stars_service.resolve_stars_success() re-validates ownership,
    idempotency, expiry and the exact Stars amount (never trust the
    client; duplicates and stale replays are no-ops)."""
    sp = update.message.successful_payment if update.message else None
    if sp is None:
        return

    request_id = _parse_stars_payload(sp.invoice_payload)
    if request_id is None:
        await update.message.reply_text(
            "⚠ We couldn't match this payment to an invoice. Your Stars "
            "will be refunded automatically by Telegram - contact Support "
            "if that doesn't happen within a few minutes."
        )
        return

    payer_id = sp.from_user.id if sp.from_user else update.effective_user.id
    row, reason = stars_service.resolve_stars_success(
        request_id, payer_id, sp.total_amount
    )

    if reason == "ok":
        if row["purpose"] == "extra_credit":
            n = int(row["extra_forwards"] or 0)
            await update.message.reply_text(
                f"⭐ Payment received - ⚡ {n:,} Extra Credits added!\n\n"
                "They never expire and are used automatically only after "
                "your daily allowance is exhausted. See them under "
                "Subscription → 🛒 Extra Credits.",
                reply_markup=_reply_menu_for(payer_id),
            )
            logger.info("Stars extra-credit success for user=%s request=%s forwards=%s",
                        payer_id, request_id, n)
        else:
            plan = row["plan"]
            display = plan_service.get_plan_display_name(plan)
            await update.message.reply_text(
                f"⭐ Payment received - welcome to {display}!\n\n"
                f"Your {plan} plan is active for {row['months']} month(s). "
                "You can close this receipt and head back to 📁 Projects.",
                reply_markup=_reply_menu_for(payer_id),
            )
            logger.info("Stars success finalized for user=%s request=%s plan=%s",
                        payer_id, request_id, plan)
        return

    if reason == "expired":
        await update.message.reply_text(
            "⚠ Your Stars payment arrived after this invoice expired. "
            "Telegram refunds the Stars to your account automatically - "
            "please start a fresh checkout, or contact Support if the "
            "refund doesn't appear."
        )
        return

    # already_decided / amount_mismatch / not_owner / not_found /
    # not_stars: never grant anything; Telegram refunds invalid charges.
    logger.warning("Stars success refused: user=%s request=%s reason=%s",
                   payer_id, request_id, reason)
    await update.message.reply_text(
        "⚠ We couldn't process this payment (it may be a duplicate or an "
        "expired invoice). No plan was changed. Telegram refunds invalid "
        "Stars automatically - contact Support if you were charged and "
        "nothing changed."
    )
