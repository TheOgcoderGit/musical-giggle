import logging
import os

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    PreCheckoutQueryHandler,
    filters
)

from config import BOT_TOKEN

from database.db import init_db

from bot.handlers import (
    start,
    cancel,
    admin_panel,
    setplan_command,
    setprice_command,
    setduration_command,
    planconfig_command,
    payments_command,
    connect_command,
    mycode_command,
    unknown_command_handler,
    walletadjust_command,
    menu_handler,
    button_handler,
    pre_checkout_handler,
    successful_payment_handler
)
from bot.admin_promo_handlers import post_command, broadcast_command, handle_promo_media
from bot.admin_panel import admin_dashboard, admin_callback, _render_user_profile
from bot.owner_panel import owner_command, owner_auth_text_handler

from core.listener import start_listener, stop_listener
from core.client import client as telethon_client
from bot import notifier


# ==========================================
# LOGGING
# ==========================================

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)

# python-telegram-bot's own HTTP client is chatty at INFO; keep it at
# WARNING so real application logs aren't drowned out.
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger("channelflow")


# ==========================================
# DATABASE
# ==========================================

init_db()

logger.info("Database initialized")


# ==========================================
# GLOBAL ERROR HANDLER
# ==========================================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):

    logger.error("Unhandled exception while processing an update", exc_info=context.error)

    if isinstance(update, Update) and update.effective_message:

        try:
            await update.effective_message.reply_text(
                "⚠ Something went wrong. Please try again."
            )
        except Exception:
            pass


# ==========================================
# START FORWARD ENGINE
# (scheduled on the bot's own event loop via
# post_init - see core/listener.py for why this
# replaced the old separate-thread approach)
# ==========================================

async def _post_init(app: Application) -> None:
    notifier.set_bot(app.bot)
    start_listener(app.bot)

    # Start WhatsApp pairing HTTP server
    from core.pairing_endpoint import setup_aiohttp_routes
    from aiohttp import web

    pairing_app = web.Application()
    setup_aiohttp_routes(pairing_app)

    runner = web.AppRunner(pairing_app)
    await runner.setup()

    host = os.getenv("PAIRING_SERVER_HOST", "0.0.0.0")
    port = int(os.getenv("PAIRING_SERVER_PORT", "8081"))
    site = web.TCPSite(runner, host, int(os.getenv("PAIRING_SERVER_PORT", "8081")))
    await site.start()

    logger.info(f"WhatsApp pairing server started on {host}:{int(os.getenv('PAIRING_SERVER_PORT', '8081'))}")

    # Store runner for cleanup
    app.bot_data["pairing_runner"] = runner


async def _post_shutdown(app: Application) -> None:
    await stop_listener()

    if telethon_client.is_connected():
        await telethon_client.disconnect()

    # Cleanup pairing server
    runner = app.bot_data.get("pairing_runner")
    if runner:
        await runner.cleanup()
        logger.info("WhatsApp pairing server stopped")


# ==========================================
# BOT
# ==========================================

app = (
    Application.builder()
    .token(BOT_TOKEN)
    .post_init(_post_init)
    .post_shutdown(_post_shutdown)
    .build()
)

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("cancel", cancel))
app.add_handler(CommandHandler("admin", admin_dashboard))
app.add_handler(CommandHandler("setplan", setplan_command))
app.add_handler(CommandHandler("setprice", setprice_command))
app.add_handler(CommandHandler("setduration", setduration_command))
app.add_handler(CommandHandler("planconfig", planconfig_command))
app.add_handler(CommandHandler("payments", payments_command))
app.add_handler(CommandHandler("walletadjust", walletadjust_command))
app.add_handler(CommandHandler("connect", connect_command))

# PRD section 5.2: /mycode is the mandatory OTP command.
#   /mycode 94563    -> handled here (CommandHandler)
#   /mycode94563     -> Telegram parses it as an unknown command, so
#                       unknown_command_handler below routes it
#                       (and the legacy /myflow... form) into the same
#                       login attempt.
app.add_handler(CommandHandler("mycode", mycode_command))

app.add_handler(CommandHandler("owner", owner_command))
app.add_handler(CommandHandler("post", post_command))
app.add_handler(CommandHandler("broadcast", broadcast_command))

# Catch-all for commands without a dedicated handler: routes the
# concatenated OTP form /mycode<digits> and answers unknown commands
# with a pointer to /start instead of silence. Registered after every
# CommandHandler above so real commands always win.
app.add_handler(
    MessageHandler(
        filters.COMMAND,
        unknown_command_handler
    )
)

app.add_handler(
    MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        menu_handler
    )
)

# BUG-002 fix: photo/video messages previously went ONLY to the promo
# handler, so a user verifying a UPI payment could never submit their
# screenshot. One unified media router dispatches by active state:
# payment screenshot > promo draft. (The promo-only registration was
# itself the original fix for "photos had no handler at all".)
from bot.handlers import media_message_router

app.add_handler(
    MessageHandler(
        (filters.PHOTO | filters.VIDEO) & ~filters.COMMAND,
        media_message_router
    )
)

# Categorized admin panel callbacks (adm:*)
app.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^adm:"))

app.add_handler(CallbackQueryHandler(button_handler))

# ⭐ Telegram Stars checkout (PRD section 26): pre_checkout_query is
# answered defensively (ok=False unless the snapshot row + amount both
# verify); successful_payment finalizes the plan exactly once through
# services/stars_service (ownership, expiry, price-match, replay guard).
app.add_handler(PreCheckoutQueryHandler(pre_checkout_handler))
app.add_handler(
    MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler)
)

app.add_error_handler(error_handler)

logger.info("ChannelFlow AI bot starting...")

app.run_polling(allowed_updates=Update.ALL_TYPES)
