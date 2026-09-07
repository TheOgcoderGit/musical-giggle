"""
ChannelFlow AI - Shared Telethon Client
========================================

A single Telethon ``TelegramClient`` instance shared by the forward
engine (``core.forwarder``) and by on-demand chat lookups
(``core.telegram_utils``).

Why this exists
----------------
The previous implementation created two separate ``TelegramClient``
objects that both pointed at the *same* ``.session`` sqlite file (one in
``core/forwarder.py``, one in ``core/telegram_utils.py``). Two independent
sqlite connections against the same session file can hit "database is
locked" errors under concurrent access, and each client would try to
negotiate its own auth key exchange, which Telethon does not support
happening twice for one session file. Sharing a single client removes
the failure class entirely.

This module owns:

    * The client instance itself.
    * ``ensure_started`` - idempotent connect/start, safe to call from
      any coroutine.
    * A ``threading.Lock``-free, ``asyncio.Lock``-guarded startup so
      concurrent callers can't race to start the client twice.
"""

import asyncio
import logging

from telethon import TelegramClient

from config import API_ID, API_HASH, SESSION_NAME

logger = logging.getLogger(__name__)

# connection_retries=None -> retry forever instead of giving up after a
# handful of attempts. retry_delay backs off between reconnect attempts.
# auto_reconnect=True (the default) keeps the socket self-healing after
# a network blip without the rest of the app noticing.
client = TelegramClient(
    SESSION_NAME,
    API_ID,
    API_HASH,
    connection_retries=None,
    retry_delay=5,
    auto_reconnect=True,
)

_start_lock = asyncio.Lock()
_started = False


async def ensure_started():
    """
    Idempotently connects + authorizes the shared client. Safe to call
    from multiple coroutines/tasks concurrently - only the first caller
    actually performs the handshake.
    """

    global _started

    if _started and client.is_connected():
        return client

    async with _start_lock:

        if _started and client.is_connected():
            return client

        await client.start()

        if not await client.is_user_authorized():

            logger.error(
                "The Telethon session is not authorized. Run "
                "`python -m core.authorize` once to log the account in "
                "interactively before starting the bot."
            )

            raise RuntimeError(
                "Telethon session not authorized. See README for the "
                "one-time login step."
            )

        _started = True

        me = await client.get_me()

        logger.info(
            "Telethon client authorized as %s (id=%s)",
            getattr(me, "username", None) or me.first_name,
            me.id,
        )

    return client
