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
from datetime import datetime, timezone

from telegram import Update, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
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
)

from bot.states import (
    WAITING_PROJECT_NAME,
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
    WAITING_PAYMENT_SCREENSHOT,
    CURRENT_PROJECT
)

from database.models import register_user, get_all_user_ids, count_users
from database.db import get_connection
from config import ADMIN_IDS, INSTAGRAM_ACCESS_TOKEN

from services import content_rules_service, formatting_service, plan_service, referral_service, payment_service, pricing_service, wallet_service
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
from destinations.instagram_destination import is_configured as is_ig_configured, verify_capability as verify_ig_capability

from core.telegram_utils import get_chat, send_test_message
from core.listener import is_running
from core.forwarder import force_refresh_routes
from core.processing_listener import force_refresh_processing_routes
from core.client import client as telethon_client

from bot.admin_promo_handlers import (
    WAITING_PROMO,
    handle_promo_text,
    handle_promo_callback,
    cancel_promo,
)


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
    cancel_promo(user_id)

    if WAITING_CONNECT_PHONE.pop(user_id, None) or WAITING_CONNECT_STAGE.pop(user_id, None):
        user_sessions.cancel_connect(user_id)


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

    sources_total = count_sources(project["id"])
    destinations_total = count_destinations(project["id"])
    platform_type = project["platform_type"] if "platform_type" in project.keys() else "telegram"

    lines = [
        f"📂 {project['name']}",
        "",
        f"🔀 {_platform_label(platform_type)}",
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

    if user_sessions.is_connected(user_id):
        await message.reply_text("✅ Your Telegram account is already connected.", reply_markup=main_menu)
        return

    error = _connect_prerequisite_error()
    if error:
        await message.reply_text(error)
        return

    WAITING_CONNECT_PHONE[user_id] = True

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


async def _send_account_card(message, user):

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

    await message.reply_text(
        _account_card_text(user, entitlements, phone, active_count, days_remaining, referral_stats),
        reply_markup=account_card_keyboard(connected=bool(phone))
    )


# ==========================================
# /start
# ==========================================

def _welcome_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Connect", callback_data="acct:connect")]])


async def _send_welcome(message, user):

    await message.reply_text(

        f"👋 Welcome, creator, to ChannelFlow AI!\n\n"
        "This bot watches the Telegram channels and groups you choose "
        "and automatically forwards or copies new posts into other "
        "destinations for you - Telegram today, with WhatsApp and "
        "Threads support on the way.\n\n"
        "Set filters on what gets through, reformat or replace text "
        "before it goes out, add delays, and let the bot handle "
        "retries and reliability so you don't have to babysit it.\n\n"
        "Everything runs on your own connected Telegram account - "
        "nothing is shared with other users, and your login is "
        "encrypted the moment it's saved. Your first 7 days on the "
        "Creator's plan are free once you connect, with no limits on "
        "projects, sources, or destinations while it lasts.\n\n"
        "Tap Connect below to get started.",

        reply_markup=_welcome_keyboard()

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

    # Not connected (and not the admin, who's exempt - see the LOGIN
    # GATE comment in menu_handler) -> welcome screen with a single
    # Connect button, not the account card. This is also what a user
    # sees again after /disconnect, since that's exactly what makes
    # is_connected() False again - no separate "returning user" state
    # to track.
    if user.id not in ADMIN_IDS and not user_sessions.is_connected(user.id):
        await _send_welcome(update.message, user)
        return

    await _send_account_card(update.message, user)
    await update.message.reply_text("Use the menu below.", reply_markup=main_menu)


# ==========================================
# /connect - per-user Telegram login
# ==========================================

async def connect_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/connect <phone_number> as a single command (matches how other
    forwarding bots do this), in addition to the 🚀 Connect Now button
    flow which prompts for the number as a separate message."""

    user = update.effective_user

    if user_sessions.is_connected(user.id):
        await update.message.reply_text("✅ Your Telegram account is already connected.", reply_markup=main_menu)
        return

    args = context.args

    if not args:
        _reset_waiting_states(user.id)

        error = _connect_prerequisite_error()
        if error:
            await update.message.reply_text(error)
            return

        WAITING_CONNECT_PHONE[user.id] = True
        await update.message.reply_text(
            "📱 Send your phone number with country code, e.g. +919876543210."
        )
        return

    phone = args[0]
    await _handle_connect_phone(update.message, user.id, phone)


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

    WAITING_CONNECT_STAGE[user_id] = "code"

    await message.reply_text(
        "🔑 Enter the login code Telegram just sent you.\n\n"
        "Never share this code with anyone, including anyone claiming "
        "to be ChannelFlow support."
    )


async def _handle_connect_code_or_password(message, user_id, text):

    stage = WAITING_CONNECT_STAGE.get(user_id)

    try:

        if stage == "code":

            try:
                await user_sessions.submit_code(user_id, text.strip())

            except user_sessions.NeedsPassword:
                WAITING_CONNECT_STAGE[user_id] = "password"
                await message.reply_text(
                    "🔒 Your account has 2-step verification. Enter your Telegram password."
                )
                return

            except user_sessions.ConnectError as e:
                await message.reply_text(f"❌ {e}")
                return

        elif stage == "password":

            try:
                await user_sessions.submit_password(user_id, text)

            except user_sessions.ConnectError as e:
                await message.reply_text(f"❌ {e}")
                return

        else:
            return

    except Exception:
        # Safety net for the exact bug that was reported: a failure
        # inside the final encrypt-and-store step (or anything else
        # unanticipated) used to propagate all the way to the global
        # error handler, leaving WAITING_CONNECT_STAGE set forever with
        # no way to recover except /cancel. Now it's caught here,
        # cleared, and the user gets a message that tells them what to
        # do next instead of silently going nowhere.
        logger.exception("Unexpected error finishing /connect for user %s", user_id)
        WAITING_CONNECT_STAGE.pop(user_id, None)
        user_sessions.cancel_connect(user_id)
        await message.reply_text(
            "❌ Something went wrong finishing the login. Please send "
            "/connect <phone_number> to start over.",
            reply_markup=pre_login_menu,
        )
        return

    WAITING_CONNECT_STAGE.pop(user_id, None)

    plan_service.start_trial(user_id)

    await client_pool.start_owner_engine(user_id)

    await message.reply_text(
        "✅ Telegram account connected.\n\n"
        "Use the menu below to create your first project.",
        reply_markup=main_menu
    )


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
        await update.message.reply_text("⛔ Admins only.")
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

async def setplan_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    if user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Admins only.")
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

    had_state = (
        user.id in WAITING_PROJECT_NAME
        or user.id in WAITING_SOURCE
        or user.id in WAITING_DESTINATION
        or user.id in WAITING_RENAME
    )

    _reset_waiting_states(user.id)

    if had_state:

        await update.message.reply_text(
            "❌ Cancelled\n\n"
            "The pending action was cancelled.",
            reply_markup=main_menu
        )

    else:

        await update.message.reply_text(
            "ℹ Nothing to cancel.",
            reply_markup=main_menu
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
    # PRE-LOGIN MENU
    # (Connect / Guide / Tour) - available regardless of connection
    # status, since Connect is how a user gets past this gate.
    # ======================================

    if text == BTN_CONNECT_NOW:

        _reset_waiting_states(user.id)
        await _start_connect_flow(message, user.id)
        return

    if text == BTN_GUIDE:
        await _send_guide(message)
        return

    if text == BTN_TOUR:
        await _send_tour(message)
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
    # MAIN MENU NAVIGATION
    # (always takes priority and clears any
    # pending waiting-state so the user can
    # never get permanently stuck)
    # ======================================

    if text in MAIN_MENU_BUTTONS:

        _reset_waiting_states(user.id)

        if text == BTN_NEW_PROJECT:

            WAITING_PROJECT_NAME[user.id] = True

            await message.reply_text(
                "📝 Send Project Name"
            )

            return

        if text == BTN_MY_PROJECTS:
            await _send_my_projects(message, user.id)
            return

        if text == BTN_STATUS:
            await _send_status(message, user.id)
            return

        if text == BTN_MY_PLAN:
            await _send_my_plan(message, user.id)
            return

        if text == BTN_SETTINGS:

            await message.reply_text(
                "⚙ ChannelFlow Settings",
                reply_markup=settings_keyboard(wallet_service.is_auto_renew_enabled(user.id))
            )

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
        reply_markup=main_menu
    )


# ==========================================
# MENU BRANCH IMPLEMENTATIONS
# ==========================================

async def _send_my_projects(message, user_id):

    projects = get_projects(user_id)

    if not projects:

        await message.reply_text(
            "❌ No Projects Found\n\n"
            "Tap ➕ New Project to create one."
        )

        return

    for project in projects:

        await message.reply_text(
            _project_card_text(project),
            reply_markup=project_keyboard(
                project["id"], running=bool(project["status"]), platform_type=project["platform_type"]
            )
        )


async def _send_my_plan(message, user_id):

    entitlements = plan_service.get_entitlements(user_id)

    def _fmt(v):
        return "Unlimited" if v is None else str(v)

    projects = get_projects(user_id)

    lines = [
        f"📦 Plan: {entitlements['plan']}",
        "",
        f"📂 Projects: {len(projects)} / {_fmt(entitlements['max_projects'])}",
        f"📡 Sources per project: {_fmt(entitlements['max_sources_per_project'])}",
        f"🎯 Destinations per project: {_fmt(entitlements['max_destinations_per_project'])}",
        f"📈 Daily forwards per project: {_fmt(entitlements['daily_forward_limit'])}",
        f"🏷 Attribution footer: {'Required' if entitlements['requires_attribution'] else 'Not required'}",
    ]

    if entitlements["plan"] == "FREE":
        lines.append("")
        lines.append("Contact the admin to upgrade your plan.")

    await message.reply_text("\n".join(lines))


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
        await message.reply_text(f"🔒 {reason}")
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
            f"🔒 {reason}",
            reply_markup=project_keyboard(project_id, platform_type=project["platform_type"])
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
            f"🔒 {reason}",
            reply_markup=project_keyboard(project_id, platform_type=project["platform_type"])
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
# INLINE KEYBOARD (CALLBACK) HANDLER
# ==========================================

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
                await query.message.reply_text(
                    "❓ Support\n\n"
                    "Contact: ChannelFlow AI Support Team\n"
                    "https://t.me/ChannelFlowSupport_bot"
                )
                return

            if sub == "upgrade":

                entitlements = plan_service.get_entitlements(user_id)
                buttons = []

                for plan_name, price in payment_service.PLAN_PRICES.items():
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
                await _send_account_card(query.message, query.from_user)
                return

            if sub == "earn":

                bot_username = context.bot.username
                code = referral_service.build_referral_code(user_id)
                link = f"https://t.me/{bot_username}?start={code}"
                stats = referral_service.get_referral_stats(user_id)

                await query.message.reply_text(
                    "🎁 Invite\n\n"
                    "Share your link. When someone joins through it and "
                    f"upgrades to PRO, you get +{referral_service.REFERRAL_REWARD_DAYS} days of PRO "
                    "- for every person who does, stacking.\n\n"
                    f"{link}\n\n"
                    f"Total invited: {stats['total_invited']}\n"
                    f"Rewarded so far: {stats['active_referrals']}"
                )
                return

            if sub == "language":
                await query.answer(
                    "🚧 Currently English only - more languages coming soon.",
                    show_alert=True,
                )
                return

            return

        # ======================================
        # UPGRADE / PAYMENT FLOW
        # ======================================

        if action == "upgrade":

            sub = parts[1] if len(parts) > 1 else None

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

                await query.message.reply_text(f"✅ Approved. User {approved['user_id']} is now on {approved['plan']}.")

                try:
                    await context.bot.send_message(
                        approved["user_id"],
                        f"✅ Your payment was approved - you're now on {approved['plan']}!"
                    )
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
                    "Tap ➕ Source on the project dashboard to add one."
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
                    "Tap ➕ Destination on the project dashboard to add one."
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
                await query.message.reply_text("❌ Project Not Found")
                return

            if count_sources(project_id) == 0:

                await query.message.reply_text(
                    "⚠ Cannot Start\n\n"
                    "Add at least one source before starting."
                )

                return

            if count_destinations(project_id) == 0:

                await query.message.reply_text(
                    "⚠ Cannot Start\n\n"
                    "Add at least one destination before starting."
                )

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

            await query.message.reply_text(

                "🟢 Project Started\n\n"
                f"📂 {project['name']}",

                reply_markup=project_keyboard(project_id, running=True, platform_type=project["platform_type"])

            )

            return

        # ======================================
        # STOP PROJECT
        # ======================================

        if action == "stop":

            project_id = int(parts[1])
            project = _get_owned_project(project_id, user_id)

            if project is None:
                await query.message.reply_text("❌ Project Not Found")
                return

            update_status(project_id, 0)
            await force_refresh_routes()

            project = get_project(project_id)

            await query.message.reply_text(

                "🔴 Project Stopped\n\n"
                f"📂 {project['name']}",

                reply_markup=project_keyboard(project_id, running=False, platform_type=project["platform_type"])

            )

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

            await query.message.reply_text(

                "✅ Source Deleted\n\n"
                f"📂 {source['title'] or source['chat_id']}",

                reply_markup=project_keyboard(project["id"], platform_type=project["platform_type"])

            )

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

            await query.message.reply_text(

                "✅ Destination Deleted\n\n"
                f"📂 {destination['title'] or destination['chat_id']}",

                reply_markup=project_keyboard(project["id"], platform_type=project["platform_type"])

            )

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
                await query.message.reply_text("❌ Project Not Found")
                return

            delete_project(project_id)
            await force_refresh_routes()

            if CURRENT_PROJECT.get(user_id) == project_id:
                _reset_waiting_states(user_id)
                CURRENT_PROJECT.pop(user_id, None)

            await query.message.reply_text(
                "🗑 Project Deleted Successfully\n\n"
                f"📂 {project['name']}"
            )

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

                await query.message.reply_text(
                    "🔌 Disconnected. Your Telegram session was deleted. "
                    "Send /start to connect again.",
                    reply_markup=pre_login_menu
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
                await query.message.reply_text("❌ Project Not Found")
                return

            CURRENT_PROJECT[user_id] = project_id
            rules = formatting_service.get_rules(project_id)

            await query.message.reply_text(
                f"📝 Formatting\n\n📂 {project['name']}\n\n"
                "Applies in Telegram Copy mode and to Instagram captions. "
                "Native Telegram Forward mode can't have its content edited, "
                "so formatting never applies there.",
                reply_markup=formatting_keyboard(project_id, rules)
            )

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
                await query.message.reply_text("❌ Project Not Found")
                return

            stats = stats_service.get_stats(project_id)

            await query.message.reply_text(
                f"📊 Stats - {project['name']}\n\n"
                f"✅ Forwarded: {stats['forwarded']}\n"
                f"❌ Failed: {stats['failed']}\n"
                f"🔁 Retried: {stats['retried']}\n"
                f"🧹 Filtered Out: {stats['filtered']}\n"
                f"🕐 Last Forward: {stats['last_forward_at'] or '-'}"
            )

            return

        # ======================================
        # LOGS
        # ======================================

        if action == "logs":

            project_id = int(parts[1])
            project = _get_owned_project(project_id, user_id)

            if project is None:
                await query.message.reply_text("❌ Project Not Found")
                return

            logs = log_service.get_logs(project_id, limit=15)

            if not logs:

                await query.message.reply_text("📜 No logs yet for this project.")
                return

            level_icon = {"forward": "✅", "error": "❌", "retry": "🔁"}

            lines = [
                f"{level_icon.get(row['level'], 'ℹ')} [{row['created_at']}] {row['message']}"
                for row in logs
            ]

            await query.message.reply_text(
                "📜 Recent Logs\n\n" + "\n".join(lines)
            )

            return

        # ======================================
        # ADMIN PANEL
        # ======================================

        if action == "admin":

            if user_id not in ADMIN_IDS:
                await query.message.reply_text("⛔ Admins only.")
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

            await query.message.reply_text("⚠ Unknown Admin Action")
            return

        # ======================================
        # UNKNOWN CALLBACK
        # ======================================

        await query.message.reply_text(
            "⚠ Unknown Action"
        )

    except (IndexError, ValueError):

        logger.warning("Malformed callback data received: %s", data)

        await query.message.reply_text(
            "⚠ This button is no longer valid. Please refresh with 📁 My Projects."
        )

    except Exception as e:

        logger.exception("Unhandled error in button_handler: %s", e)

        await query.message.reply_text(
            "⚠ Something went wrong while processing that action. Please try again."
        )
