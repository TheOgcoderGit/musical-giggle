import logging

from telethon import utils
from telethon.errors import (
    UsernameNotOccupiedError,
    UsernameInvalidError,
    UserAlreadyParticipantError,
    ChannelPrivateError,
    ChatWriteForbiddenError,
    FloodWaitError,
    RPCError,
)
from telethon.tl.functions.channels import JoinChannelRequest

from core.client import client, ensure_started

logger = logging.getLogger(__name__)


def _resolve_type_label(entity):
    """
    Maps a raw Telethon entity to a human friendly chat type used
    throughout the bot: Channel, Supergroup, Group, Telegram Bot, User.
    """

    cls_name = entity.__class__.__name__

    if cls_name == "Channel":
        if getattr(entity, "megagroup", False):
            return "Supergroup"
        return "Channel"

    if cls_name == "Chat":
        return "Group"

    if cls_name == "User":
        if getattr(entity, "bot", False):
            return "Telegram Bot"
        return "User"

    return cls_name


async def _join_chat(entity):
    """
    Ensures the account is actually a member of ``entity`` before it's
    used as a source or destination.

    This matters because Telegram only pushes live "new message" updates
    for chats the account is a member of - resolving a public username
    with get_entity() does NOT require membership, so without this,
    adding a source "succeeds" but the forward engine never receives a
    single update from it. Returns (joined: bool, note: str | None).
    ``note`` is a short, user-facing explanation when auto-join wasn't
    possible, so the person knows they need to join it manually.
    """

    cls_name = entity.__class__.__name__

    if cls_name != "Channel":
        # Basic groups (Chat) and users/bots aren't self-joinable via
        # username the way channels/supergroups are - nothing to do.
        return True, None

    try:
        await client(JoinChannelRequest(entity))
        return True, None

    except UserAlreadyParticipantError:
        return True, None

    except ChannelPrivateError:
        return False, "This is private/invite-only - add the bot's account to it manually, then forwarding will work."

    except FloodWaitError as e:
        return False, f"Telegram asked to wait {e.seconds}s before joining more chats - try again shortly."

    except RPCError as e:
        # Covers rarer cases (e.g. join-requires-approval, too many
        # channels joined) without depending on Telethon exposing every
        # specific error under an exact class name.
        return False, f"Couldn't join automatically ({e}) - add the bot's account manually if forwarding doesn't start."


async def _check_can_post(entity):
    """
    Best-effort check of whether the ChannelFlow account can actually
    send messages into ``entity`` - used when adding a *destination*,
    so a permission problem is surfaced immediately instead of only
    showing up later as a silent forwarding failure.

    Returns (can_post: bool, note: str | None). If the check itself is
    inconclusive (e.g. Telegram doesn't expose permissions for this
    chat type), this returns True with no note rather than blocking a
    real destination on a failed guess - the 🧪 Test button remains
    the definitive check.
    """

    cls_name = entity.__class__.__name__

    if cls_name not in ("Channel", "Chat"):
        return True, None

    try:
        perms = await client.get_permissions(entity, "me")

        if getattr(entity, "broadcast", False):
            # Broadcast channels: only admins with explicit post rights
            # can send.
            can_post = bool(getattr(perms, "is_admin", False)) and bool(
                getattr(perms, "post_messages", False)
            )
        else:
            # Supergroups/basic groups: any non-restricted member can
            # normally post.
            can_post = bool(
                getattr(perms, "is_admin", False)
                or getattr(perms, "send_messages", True)
            )

    except Exception as e:
        logger.info("Permission check inconclusive for %s: %s", entity, e)
        return True, None

    if can_post:
        return True, None

    return False, (
        "⚠ This account doesn't look able to post here yet. Make it "
        "admin with 'Post Messages' permission, then tap 🧪 Test to confirm."
    )


async def send_test_message(chat_id, project_name="ChannelFlow AI"):
    """
    Sends one real message into ``chat_id`` right now so the user gets
    an immediate, genuine yes/no about whether this destination is
    actually reachable - instead of only finding out when a real
    forwarded post silently fails later. Never returns mock/fake
    results: this is a real Telegram API call.

    Returns (ok: bool, detail: str | None).
    """

    await ensure_started()

    try:
        target = int(chat_id)
    except (TypeError, ValueError):
        target = chat_id

    try:
        await client.send_message(
            target,
            f"✅ {project_name} - test message. If you can see this, "
            "forwarding to this destination is working."
        )
        return True, None

    except ChatWriteForbiddenError:
        return False, "This account can't post here - it needs admin rights with 'Post Messages' permission."

    except ChannelPrivateError:
        return False, "This chat is private/invite-only and the account isn't a member - add it manually first."

    except FloodWaitError as e:
        return False, f"Telegram asked to wait {e.seconds}s before sending again - try again shortly."

    except RPCError as e:
        return False, str(e)

    except Exception as e:
        logger.exception("Unexpected error sending test message to %s", chat_id)
        return False, str(e)


async def get_chat(username, for_destination=False):

    await ensure_started()

    try:

        username = username.strip()

        if not username.startswith("@") and not username.lstrip("-").isdigit():
            username = "@" + username

        entity = await client.get_entity(username)

        chat_id = utils.get_peer_id(entity)

        joined, join_note = await _join_chat(entity)

        note = join_note

        if for_destination and joined:
            can_post, post_note = await _check_can_post(entity)
            if not can_post:
                note = post_note

        return {
            "chat_id": str(chat_id),
            "username": getattr(entity, "username", "") or "",
            "title": getattr(entity, "title", "") or getattr(entity, "first_name", "") or "",
            "type": _resolve_type_label(entity),
            "joined": joined,
            "join_note": note,
        }

    except (UsernameNotOccupiedError, UsernameInvalidError):

        # Genuinely doesn't exist / bad username - not a real error to
        # surface, the "Invalid Channel" message already covers this.
        return None

    except (ValueError, TypeError):

        # Telethon couldn't parse the input as a chat reference at all.
        return None

    except FloodWaitError as e:

        raise RuntimeError(
            f"Telegram asked to wait {e.seconds}s before trying again."
        ) from e

    except RPCError as e:

        logger.warning("get_chat RPC error for %r: %s", username, e)

        raise RuntimeError(f"Telegram rejected this request: {e}") from e


async def get_account_info():
    """Used by the bot's Status/Settings screen to show which Telegram
    account is currently powering the forward engine."""

    await ensure_started()

    me = await client.get_me()

    return {
        "id": me.id,
        "username": me.username,
        "phone": me.phone,
        "first_name": me.first_name,
        "premium": bool(getattr(me, "premium", False)),
        "dc_id": client.session.dc_id,
    }
