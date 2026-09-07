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
#
# Bug-f fix (Batch 4): construction is LAZY. TelegramClient(...) opens
# the .session sqlite file at import time, which meant every `import
# bot.handlers` (tests, tools, anything) silently created an empty
# ChannelFlow.session file. The module-level `client` is a proxy that
# only constructs the real client on first attribute use, so imports
# are side-effect free.

_client_instance = None

# Decorator registrations made through the proxy before the real
# client exists (e.g. processing_listener's module-level
# `@client.on(events.NewMessage)`) are queued here and replayed onto
# the real client the moment it is constructed, so importing modules
# stays completely side-effect free while registration order is
# preserved.
_pending_event_handlers = []


def _get_client():
    global _client_instance
    if _client_instance is None:
        _client_instance = TelegramClient(
            SESSION_NAME,
            API_ID,
            API_HASH,
            connection_retries=None,
            retry_delay=5,
            auto_reconnect=True,
        )
        for event, callback in _pending_event_handlers:
            try:
                _client_instance.on(event)(callback)
            except Exception:
                logger.exception("Failed to replay deferred event handler")
        _pending_event_handlers.clear()
    return _client_instance


class _LazyClientProxy:
    """Delegates every attribute to the lazily-built TelegramClient.

    `.on(...)` is special-cased so module-level ``@client.on(...)``
    decorators only touch the real client once it actually exists
    (bug-f: imports must never construct the client / open the session
    file)."""

    def on(self, event):
        def decorator(callback):
            if _client_instance is not None:
                _client_instance.on(event)(callback)
            else:
                _pending_event_handlers.append((event, callback))
            return callback
        return decorator

    def __getattr__(self, name):
        return getattr(_get_client(), name)

    def __repr__(self):
        return repr(_get_client())


client = _LazyClientProxy()

_start_lock = asyncio.Lock()
_started = False


async def ensure_started():
    """
    Idempotently connects + authorizes the shared client. Safe to call
    from multiple coroutines/tasks concurrently - only the first caller
    actually performs the handshake.
    """

    global _started

    real = _get_client()

    if _started and real.is_connected():
        return real

    async with _start_lock:

        if _started and real.is_connected():
            return real

        await real.start()

        if not await real.is_user_authorized():

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

        me = await real.get_me()

        logger.info(
            "Telethon client authorized as %s (id=%s)",
            getattr(me, "username", None) or me.first_name,
            me.id,
        )

    return real
