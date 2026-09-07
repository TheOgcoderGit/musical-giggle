"""
ChannelFlow AI - Per-User Telegram Login (/connect + /mycode)
================================================================

Lets a customer log their own Telegram account into the bot, so their
projects can eventually forward using their own account rather than
the service-operator's shared session (core/client.py). This is
SEPARATE infrastructure from core/client.py/core/authorize.py on
purpose - see core/authorize.py's module docstring for why phone/OTP/
2FA collection through bot chat was originally kept out of the chat UI,
and bot/handlers.py's connect-flow messaging for the consent/safety
copy shown to the user before they type anything.

Flow
----
1. start_connect(user_id, phone_number) - opens a temporary Telethon
   client bound to a StringSession (never touches disk on its own),
   requests an OTP, and holds the client in memory keyed by user_id
   while waiting for the code.
2. submit_code(user_id, code) - completes sign-in, or raises
   NeedsPassword if the account has 2FA enabled.
3. submit_password(user_id, password) - completes 2FA sign-in.
4. On success in either step:
   a. client.get_me() returns the REAL external Telegram account id
      (PRD section 6: the external identity, never confused with the
      ChannelFlow user id).
   b. the resulting session string is encrypted
      (core/session_crypto.py) and upserted into user_telegram_sessions
      together with the external account id.
   c. Ownership is claimed ATOMICALLY in the database: the partial
      unique index idx_uts_one_active_owner guarantees one active
      ChannelFlow owner per external Telegram account, and the claim
      transaction re-checks before writing so a second user claiming
      the same account is rejected with a safe, non-identifying error
      (ConnectConflict).
   The temporary client disconnects immediately after - nothing about
   this module keeps a live connection open per connected user; that's
   a separate concern for whatever eventually runs per-user forwarding.

Input format
------------
The login code is delivered to the bot as /mycode<digits> or
/mycode <digits> (legacy /myflow<digits> is still accepted). A bare
numeric message is never treated as a login code (PRD section 5.3).

Safety
------
* Every pending attempt expires after PENDING_TTL_SECONDS and is
  disconnected + dropped - a user who starts /connect and never
  finishes can't leave a dangling authenticated-but-unclaimed client
  sitting in memory indefinitely.
* MAX_ATTEMPTS caps wrong-code/wrong-password tries per pending attempt
  before it's dropped and the user has to restart with /connect.
* Nothing in this module logs a phone number, code, password, session
  string, or the external account id - only user ids and outcome
  (success/failure/reason).
"""

import asyncio
import logging
import re
import sqlite3
import time

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PhoneNumberInvalidError,
    PasswordHashInvalidError,
    FloodWaitError,
)

from config import API_ID, API_HASH
from database.db import get_connection
from core.session_crypto import encrypt_session
from bot.states import (
    WAITING_CONNECT_PHONE,
    WAITING_CONNECT_STAGE,
    WAITING_CONNECT_STAGE_STARTED,
)

logger = logging.getLogger(__name__)

PENDING_TTL_SECONDS = 600  # 10 minutes to finish a /connect attempt
MAX_ATTEMPTS = 3

# ---- OTP input-format handling (/mycode) -----------------------------
# Telegram's anti-abuse systems are NOT touched: the OTP digits sent to
# Telethon are exactly what Telegram delivered. The /mycode prefix is a
# BOT-SIDE INPUT FORMAT ONLY - it exists so a login code arriving in an
# otherwise-normal chat can't be confused with other messages, and so a
# stray paste can't be consumed by the wrong handler.
#
# PRD section 5.2: the required command is /mycode. Both of these must
# be accepted:
#     /mycode94563
#     /mycode 94563
# The old /myflow login-code convention stays accepted for backwards
# compatibility with keyboards/users that learned the old format, but
# every user-facing prompt now documents /mycode.
OTP_COMMAND_PREFIX = "/mycode"
OTP_LEGACY_PREFIX = "/myflow"
_OTP_RE = re.compile(r"^/(?:mycode|myflow)[ \t]*(\d{1,8})$", re.IGNORECASE)


def extract_otp_from_command(text: str):
    """Parses '/mycode94563' or '/mycode 94563' (and the legacy
    '/myflow...' form) -> '94563'.

    Returns (ok, code_or_error_message). Validates:
      * message starts with /mycode or /myflow
      * remainder is 1-8 digits (Telegram OTPs are typically 5)
    No logging of the code itself - only pass/fail."""

    if not text:
        return False, "Empty message."

    stripped = text.strip()

    if not stripped.startswith("/mycode") and not stripped.startswith("/myflow"):
        return False, (
            f"Send the code in this format: {OTP_COMMAND_PREFIX}<code>\n"
            f"Example: {OTP_COMMAND_PREFIX}94563"
        )

    m = _OTP_RE.match(stripped)

    if not m:
        return False, (
            "That doesn't look like a valid code. Digits only - "
            f"example: {OTP_COMMAND_PREFIX}94563"
        )

    return True, m.group(1)


# -----------------------------------------------------------------------

# user_id -> {"client": TelegramClient, "phone": str, "phone_code_hash": str,
#             "stage": "code"|"password", "attempts": int, "started_at": float}
_pending = {}
_pending_lock = asyncio.Lock()

# Codes already submitted for a given pending attempt (per-user set).
# Prevents reuse of an already-tried OTP within the same attempt window
# (a retry with the SAME code would burn another wrong-attempt slot at
# Telegram anyway; blocking it early is cheaper and clearer).
_submitted_codes = {}  # user_id -> set of codes tried this attempt


class ConnectError(Exception):
    """User-facing failure - message is safe to show as-is."""


class NeedsPassword(Exception):
    """Sign-in succeeded on the code but this account has 2FA enabled."""


class ConnectConflict(ConnectError):
    """The external Telegram account the user just logged into is
    already actively owned by another ChannelFlow user (or a race lost
    the claim). The message must never reveal the other user's id,
    phone, username or any private detail."""

    DEFAULT_MESSAGE = (
        "❌ This Telegram account is already connected to another "
        "ChannelFlow account.\n\n"
        "Disconnect it from that account first."
    )

    def __init__(self, message: str = None):
        super().__init__(message or self.DEFAULT_MESSAGE)


async def _drop_pending(user_id):

    state = _pending.pop(user_id, None)
    # Clear any record of codes tried in this attempt (never logged,
    # just freed).
    _submitted_codes.pop(user_id, None)

    if state is not None:
        try:
            await state["client"].disconnect()
        except Exception:
            pass


async def _sweep_expired():

    now = time.monotonic()
    expired = [
        uid for uid, s in _pending.items()
        if now - s["started_at"] > PENDING_TTL_SECONDS
    ]

    for uid in expired:
        await _drop_pending(uid)

    # Bot-side waiting-state flags belong to the same login attempt and
    # must expire in lock-step, otherwise a stale flag swallows an
    # unrelated future message (PRD section 44 - stale flow cleanup).
    stale_stage = [
        uid for uid, ts in list(WAITING_CONNECT_STAGE_STARTED.items())
        if now - ts > PENDING_TTL_SECONDS
    ]
    for uid in stale_stage:
        WAITING_CONNECT_STAGE.pop(uid, None)
        WAITING_CONNECT_STAGE_STARTED.pop(uid, None)
        await _drop_pending(uid)
        logger.info("Auto-cleaned expired connect stage for user %s", uid)

    # WAITING_CONNECT_PHONE values are monotonic timestamps (set by
    # bot/handlers.py) so the same sweep covers the phone-collection
    # step.
    stale_phone = [
        uid for uid, ts in list(WAITING_CONNECT_PHONE.items())
        if not isinstance(ts, (int, float)) or now - ts > PENDING_TTL_SECONDS
    ]
    for uid in stale_phone:
        WAITING_CONNECT_PHONE.pop(uid, None)
        logger.info("Auto-cleaned expired phone prompt for user %s", uid)


async def start_cleanup_task():
    """Background task to periodically clean up expired pending
    connections AND the bot-side WAITING_CONNECT_* flags that belong to
    them (PRD 44/45). One task owns all connect-flow expiry so a stale
    stage can never outlive its Telethon client or vice versa."""
    while True:
        await asyncio.sleep(60)  # Run every minute
        try:
            await _sweep_expired()
        except Exception:
            logger.exception("Error in connect-state cleanup task")


async def start_connect(user_id: int, phone_number: str):
    """Sends the OTP. Raises ConnectError with a user-safe message on
    any failure (invalid number, flood wait, etc.)."""

    async with _pending_lock:

        await _sweep_expired()
        await _drop_pending(user_id)  # restart cleanly if one was already in progress

        client = TelegramClient(StringSession(), API_ID, API_HASH)

        try:
            await client.connect()
            sent = await client.send_code_request(phone_number)

        except PhoneNumberInvalidError:
            await client.disconnect()
            raise ConnectError("That phone number doesn't look valid. Include the country code, e.g. +919876543210.")

        except FloodWaitError as e:
            await client.disconnect()
            raise ConnectError(f"Telegram asked us to wait {e.seconds}s before trying again. Please retry shortly.")

        except Exception:
            await client.disconnect()
            logger.exception("start_connect failed for user %s", user_id)
            raise ConnectError("Couldn't start the login. Please try again in a moment.")

        _pending[user_id] = {
            "client": client,
            "phone": phone_number,
            "phone_code_hash": sent.phone_code_hash,
            "stage": "code",
            "attempts": 0,
            "started_at": time.monotonic(),
        }

        _submitted_codes[user_id] = set()


async def submit_code(user_id: int, code: str):
    """Returns True on full success. Raises NeedsPassword if 2FA is
    required (call submit_password next), ConnectConflict if the
    freshly-logged-in external account is owned by another ChannelFlow
    user, or ConnectError on any other failure - the caller should let
    the user retry within MAX_ATTEMPTS.

    `code` here must be the EXTRACTED digits only (see
    extract_otp_from_command) - the /mycode prefix never reaches this
    function and never goes to Telegram."""

    state = _pending.get(user_id)

    if state is None or state["stage"] != "code":
        raise ConnectError("No active Telegram login attempt. Send /connect <phone_number> to start.")

    # Check for timeout before processing
    if time.monotonic() - state["started_at"] > PENDING_TTL_SECONDS:
        await _drop_pending(user_id)
        raise ConnectError("Login session expired. Send /connect <phone_number> to start over.")

    # Prevent reuse of an already-submitted OTP within this attempt.
    if code in _submitted_codes.get(user_id, set()):
        raise ConnectError(
            "You've already tried that code. Check for a NEWER code from "
            "Telegram - the latest message counts."
        )

    client = state["client"]

    try:
        await client.sign_in(
            phone=state["phone"], code=code, phone_code_hash=state["phone_code_hash"]
        )

    except SessionPasswordNeededError:
        # Code accepted; account has 2FA. Mark code consumed and move on.
        _submitted_codes.setdefault(user_id, set()).add(code)
        state["stage"] = "password"
        raise NeedsPassword()

    except FloodWaitError as e:
        await _drop_pending(user_id)
        raise ConnectError(
            f"Telegram asked us to wait {e.seconds}s before continuing. "
            "Please start again with /connect after that."
        )

    except (PhoneCodeInvalidError, PhoneCodeExpiredError):

        _submitted_codes.setdefault(user_id, set()).add(code)
        state["attempts"] += 1

        if state["attempts"] >= MAX_ATTEMPTS:
            await _drop_pending(user_id)
            raise ConnectError("Too many incorrect attempts. Send /connect <phone_number> to start over.")

        raise ConnectError(
            "That code was incorrect or expired. "
            f"Try the newest code: {OTP_COMMAND_PREFIX}<code>"
        )

    except ConnectConflict:
        await _drop_pending(user_id)
        raise

    except Exception:
        await _drop_pending(user_id)
        logger.exception("submit_code failed for user %s", user_id)
        raise ConnectError("Something went wrong during login. Send /connect <phone_number> to start over.")

    # Success - capture the REAL external Telegram account identity and
    # claim ownership atomically. Pending state is wiped in _finalize.
    await _finalize(user_id, client)
    return True


async def submit_password(user_id: int, password: str):

    state = _pending.get(user_id)

    if state is None or state["stage"] != "password":
        raise ConnectError("No active Telegram login attempt. Send /connect <phone_number> to start.")

    # Check for timeout before processing
    if time.monotonic() - state["started_at"] > PENDING_TTL_SECONDS:
        await _drop_pending(user_id)
        raise ConnectError("Login session expired. Send /connect <phone_number> to start over.")

    client = state["client"]

    try:
        await client.sign_in(password=password)

    except PasswordHashInvalidError:

        state["attempts"] += 1

        if state["attempts"] >= MAX_ATTEMPTS:
            await _drop_pending(user_id)
            raise ConnectError("Too many incorrect attempts. Send /connect <phone_number> to start over.")

        raise ConnectError("Incorrect password. Try again.")

    except FloodWaitError as e:
        await _drop_pending(user_id)
        raise ConnectError(
            f"Telegram asked us to wait {e.seconds}s before continuing. "
            "Please start again with /connect after that."
        )

    except ConnectConflict:
        await _drop_pending(user_id)
        raise

    except Exception:
        await _drop_pending(user_id)
        logger.exception("submit_password failed for user %s", user_id)
        raise ConnectError("Something went wrong during login. Send /connect <phone_number> to start over.")

    await _finalize(user_id, client)
    return True


def _claim_session_sync(user_id, external_id, encrypted, phone_number,
                        external_username=None):
    """Synchronous, transactional ownership claim used by _finalize.

    * Rejects the claim when another ACTIVE ChannelFlow user already
      owns the same external Telegram account (PRD 6.3/6.4).
    * The partial unique index idx_uts_one_active_owner is the
      database-level backstop: even if two processes pass this check
      simultaneously, exactly one insert/update wins and the other
      hits an IntegrityError (PRD 6.5 - atomic claim).
    * A user may reconnect/re-claim their OWN account at any time
      (upsert on telegram_id).
    * Returns True on success. Raises ConnectConflict with a safe,
      non-identifying message otherwise.

    `external_id` may be None only for legacy pre-migration rows; in
    that case the uniqueness constraint is not applied (nothing to
    deduplicate against).
    """

    conn = get_connection()
    try:
        cur = conn.cursor()
        conn.execute("BEGIN IMMEDIATE")

        if external_id is not None:
            cur.execute(
                "SELECT telegram_id FROM user_telegram_sessions "
                "WHERE external_telegram_id=? AND status='connected' AND telegram_id<>?",
                (external_id, user_id),
            )
            if cur.fetchone():
                conn.rollback()
                raise ConnectConflict()

        cur.execute(
            """
            INSERT INTO user_telegram_sessions(
                telegram_id, encrypted_session, phone_number, status,
                external_telegram_id, external_username, needs_reconnect
            )
            VALUES (?, ?, ?, 'connected', ?, ?, 0)
            ON CONFLICT(telegram_id) DO UPDATE SET
                encrypted_session=excluded.encrypted_session,
                phone_number=excluded.phone_number,
                status='connected',
                external_telegram_id=excluded.external_telegram_id,
                external_username=excluded.external_username,
                needs_reconnect=0,
                connected_at=CURRENT_TIMESTAMP,
                last_seen_at=CURRENT_TIMESTAMP
            """,
            (user_id, encrypted, phone_number, external_id, external_username),
        )

        conn.commit()
        return True

    except ConnectConflict:
        conn.rollback()
        raise

    except sqlite3.IntegrityError:
        # Unique-index backstop: another user won the race for this
        # external account between our check and our write.
        conn.rollback()
        raise ConnectConflict()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


def _external_username_of(me):
    """Best-effort public username of the external account (may be
    None). Never returns sensitive fields."""
    try:
        return getattr(me, "username", None)
    except Exception:
        return None


async def _finalize(user_id, client):
    """Runs inside the success path of submit_code / submit_password,
    AFTER Telegram sign-in already succeeded.

    * get_me() -> the real external Telegram account id (PRD 6.2).
    * Encrypts the session string.
    * Atomically claims ownership (see _claim_session_sync).

    Raises ConnectError/ConnectConflict on failure - never logs the
    session, phone or external id."""

    try:
        me = await client.get_me()
        external_id = getattr(me, "id", None)
        external_username = _external_username_of(me)
    except Exception:
        await _drop_pending(user_id)
        logger.exception("get_me failed after sign-in for user %s", user_id)
        raise ConnectError(
            "Telegram accepted your code but we couldn't confirm the "
            "account. Please start /connect again."
        )

    state = _pending.pop(user_id, None)
    _submitted_codes.pop(user_id, None)

    if state is None:
        # Already finalized or cancelled concurrently - nothing to do.
        try:
            await client.disconnect()
        except Exception:
            pass
        return

    session_string = client.session.save()
    phone_number = state["phone"]

    await client.disconnect()

    encrypted = encrypt_session(session_string)

    _claim_session_sync(
        user_id, external_id, encrypted, phone_number,
        external_username=external_username,
    )

    logger.info("User %s connected their Telegram account", user_id)


async def cancel_connect_async(user_id):
    """Async /cancel: removes the pending entry AND disconnects the
    temporary Telethon client right away (no dangling authenticated
    client until the next sweep)."""
    await _drop_pending(user_id)


def cancel_connect(user_id):
    """Sync-safe best-effort cancel for code paths that cannot await -
    removes the dict entry immediately so it stops being usable; the
    actual client disconnect happens in the next async sweep or via
    cancel_connect_async."""

    _pending.pop(user_id, None)
    _submitted_codes.pop(user_id, None)


def is_connected(telegram_id) -> bool:

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        "SELECT 1 FROM user_telegram_sessions WHERE telegram_id=? AND status='connected'",
        (telegram_id,),
    )
    row = cur.fetchone()
    conn.close()

    return row is not None


def get_connection_row(user_id):
    """Full user_telegram_sessions row (or None) for a ChannelFlow user.
    Used by account screens; never log or expose encrypted_session."""

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        "SELECT telegram_id, phone_number, external_telegram_id, status, "
        "needs_reconnect, connected_at, last_seen_at "
        "FROM user_telegram_sessions WHERE telegram_id=? AND status='connected'",
        (user_id,),
    )
    row = cur.fetchone()
    conn.close()

    return dict(row) if row else None


def needs_reconnect(user_id) -> bool:
    """True when the stored session failed validation after a restart
    and the user must /connect again (PRD 7.3)."""

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        "SELECT needs_reconnect FROM user_telegram_sessions "
        "WHERE telegram_id=? AND status='connected'",
        (user_id,),
    )
    row = cur.fetchone()
    conn.close()

    return bool(row and row["needs_reconnect"])


def mark_needs_reconnect(user_id, value: bool = True):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        "UPDATE user_telegram_sessions SET needs_reconnect=? WHERE telegram_id=?",
        (1 if value else 0, user_id),
    )

    conn.commit()
    conn.close()


def disconnect_user(telegram_id):
    """User-initiated revoke - deletes the stored session outright
    rather than just flipping a status flag, so there's nothing left to
    leak even if the row were somehow read elsewhere. Deleting the row
    releases the external Telegram account for a future owner (PRD
    6.7)."""

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("DELETE FROM user_telegram_sessions WHERE telegram_id=?", (telegram_id,))

    conn.commit()
    conn.close()
