"""
ChannelFlow AI - Forward Engine
=================================

Listens for new messages on every enabled source of every active
project (through the single shared Telethon client - see
``core.client``) and forwards/copies them into that project's enabled
destinations, applying:

    * Media type / keyword / regex filters
    * Fixed or random delay
    * Media-group ("album") batching
    * Forward vs Copy mode, with optional silent + protect-content
    * Automatic retry with FloodWait handling
    * Per-project logging and stats

Performance notes
------------------
The previous implementation ran a fresh SQL query per *project* for
every single incoming message (``get_active_projects`` then, per
project, ``get_sources``). On an account with many projects that's
O(projects) queries per message. This version keeps a small in-memory
routing cache (chat_id -> project routing info) that is rebuilt from
the database on a timer, so the hot path for each incoming message is
a dict lookup, not a database round-trip.
"""

import asyncio
import logging
import random
import re
import time

from telethon import events
from telethon.errors import FloodWaitError, RPCError

from core.client import client, ensure_started
from database.db import get_connection
from services import log_service, stats_service, project_service, content_rules_service, formatting_service, plan_service
from services import dedup_service, ai_service, watermark_service
from bot import notifier

logger = logging.getLogger(__name__)

CACHE_REFRESH_SECONDS = 5
ALBUM_DEBOUNCE_SECONDS = 1.2
MAX_SEND_RETRIES = 3
RETRY_BACKOFF_BASE = 2  # seconds, doubles each attempt

URL_RE = re.compile(r"https?://\S+")


# ==========================================
# ROUTING CACHE
# ==========================================
#
# _ROUTES maps a source chat_id (str) -> list of routing dicts, one per
# active project that listens to it:
#   {
#     "project_id": int,
#     "owner_id": int,
#     "destinations": [int, ...],
#     "settings": dict (project_settings row),
#     "content_rules": dict (content_rules row),
#     "has_formatting": bool,
#   }

_ROUTES = {}
_routes_lock = asyncio.Lock()


def _build_route_for_project(conn, cur, project_id, owner_id):
    """Shared per-project route-building logic used by both
    _load_routes_sync (the shared/legacy client's scope) and
    _load_owner_routes_sync (a connected user's own dedicated client -
    see core/client_pool.py). Returns (sources, route_dict) or
    (None, None) if the project has no enabled source or destination.
    """

    cur.execute(
        "SELECT chat_id FROM sources WHERE project_id=? AND enabled=1",
        (project_id,)
    )
    sources = [str(row["chat_id"]) for row in cur.fetchall()]

    if not sources:
        return None, None

    cur.execute(
        "SELECT id, chat_id, platform_account_id, chat_type FROM destinations WHERE project_id=? AND enabled=1",
        (project_id,)
    )
    dest_rows = cur.fetchall()

    # BUG FIX: non-Telegram destinations (wa:<id>, th:<id>) must NOT be
    # int()-cast - that raised ValueError on EVERY route rebuild once a
    # WhatsApp/Threads destination existed, silently killing routing for
    # the whole project. Split by platform:
    #   - Telegram destinations -> live Telethon dispatch list
    #   - WA/Threads destinations -> enqueued as publish jobs for the
    #     queue worker (services/job_queue.py) with their platform account.
    destinations = []
    external_destinations = []

    for row in dest_rows:
        chat_id = str(row["chat_id"])
        if chat_id.startswith(("wa:", "th:")):
            external_destinations.append({
                "destination_row_id": row["id"],
                "platform": "whatsapp_channel" if chat_id.startswith("wa:") else "threads",
                "account_identifier": chat_id.split(":", 1)[1],
                "platform_account_id": row["platform_account_id"],
            })
        else:
            try:
                destinations.append(int(chat_id))
            except (TypeError, ValueError):
                logger.warning(
                    "Skipping unparseable destination %r in project %s",
                    chat_id, project_id,
                )

    if not destinations and not external_destinations:
        return None, None

    cur.execute(
        "SELECT * FROM project_settings WHERE project_id=?",
        (project_id,)
    )
    settings = cur.fetchone()

    if settings is None:
        cur.execute(
            "INSERT INTO project_settings(project_id) VALUES(?)",
            (project_id,)
        )
        conn.commit()
        cur.execute(
            "SELECT * FROM project_settings WHERE project_id=?",
            (project_id,)
        )
        settings = cur.fetchone()

    # Phase 7: additional filter dimensions (content_rules) and
    # content transformation (formatting_rules), loaded here so the
    # hot path doesn't need extra per-message queries - same lazy-
    # create pattern as project_settings above.
    cur.execute("SELECT * FROM content_rules WHERE project_id=?", (project_id,))
    content_rules = cur.fetchone()

    if content_rules is None:
        cur.execute("INSERT INTO content_rules(project_id) VALUES(?)", (project_id,))
        conn.commit()
        cur.execute("SELECT * FROM content_rules WHERE project_id=?", (project_id,))
        content_rules = cur.fetchone()

    cur.execute("SELECT * FROM formatting_rules WHERE project_id=?", (project_id,))
    formatting_rules = cur.fetchone()

    if formatting_rules is None:
        cur.execute("INSERT INTO formatting_rules(project_id) VALUES(?)", (project_id,))
        conn.commit()
        cur.execute("SELECT * FROM formatting_rules WHERE project_id=?", (project_id,))
        formatting_rules = cur.fetchone()

    route = {
        "project_id": project_id,
        "owner_id": owner_id,
        "destinations": destinations,
        "external_destinations": external_destinations,
        "settings": dict(settings),
        "content_rules": dict(content_rules),
        "has_formatting": bool(
            formatting_rules["prefix"] or formatting_rules["suffix"]
            or formatting_rules["remove_patterns"] not in ("[]", "", None)
            or formatting_rules["replace_rules"] not in ("[]", "", None)
        ),
    }

    return sources, route


def _load_routes_sync():
    """
    Synchronous DB read building the full routing table for the
    SHARED/legacy client - i.e. every active project whose owner has
    NOT connected their own Telegram account via /connect. Once an
    owner connects, their active projects move to a dedicated client
    (see _load_owner_routes_sync + core/client_pool.py) and disappear
    from this scope on the next refresh, rather than being served
    twice.

    Runs off the event loop via asyncio.to_thread so a slow disk never
    stalls message dispatch.
    """

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT id, user_id FROM projects
        WHERE status=1
          AND user_id NOT IN (
              SELECT telegram_id FROM user_telegram_sessions WHERE status='connected'
          )
    """)
    active_projects = [(row["id"], row["user_id"]) for row in cur.fetchall()]

    routes = {}

    for project_id, owner_id in active_projects:

        sources, route = _build_route_for_project(conn, cur, project_id, owner_id)

        if route is None:
            continue

        for chat_id in sources:
            routes.setdefault(chat_id, []).append(route)

    conn.close()

    return routes


def _load_owner_routes_sync(owner_id):
    """Same shape as _load_routes_sync, but scoped to ONE connected
    owner's active projects - used by the per-owner engine that runs
    on that owner's own dedicated Telethon client (core/client_pool.py)
    instead of the shared one. Reuses _build_route_for_project so the
    per-project logic (settings/content_rules/formatting lazy-create,
    has_formatting flag) is defined exactly once."""

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT id FROM projects WHERE status=1 AND user_id=?", (owner_id,))
    project_ids = [row["id"] for row in cur.fetchall()]

    routes = {}

    for project_id in project_ids:

        sources, route = _build_route_for_project(conn, cur, project_id, owner_id)

        if route is None:
            continue

        for chat_id in sources:
            routes.setdefault(chat_id, []).append(route)

    conn.close()

    return routes


async def _refresh_routes():

    global _ROUTES

    try:
        routes = await asyncio.to_thread(_load_routes_sync)
    except Exception:
        logger.exception("Failed to refresh routing cache")
        return

    async with _routes_lock:
        _ROUTES = routes


async def _routes_refresh_loop():

    while True:

        await _refresh_routes()
        await asyncio.sleep(CACHE_REFRESH_SECONDS)


async def force_refresh_routes():
    """Called by the bot after a create/start/stop/edit action so the
    engine doesn't wait up to CACHE_REFRESH_SECONDS to notice."""

    await _refresh_routes()


async def load_owner_routes(owner_id):
    """Public wrapper around _load_owner_routes_sync for
    core/client_pool.py - keeps the underscore-prefixed sync loader an
    internal implementation detail while giving the per-owner engine a
    clean async entry point, same asyncio.to_thread pattern as the
    shared engine's own _refresh_routes."""

    return await asyncio.to_thread(_load_owner_routes_sync, owner_id)


# ==========================================
# FILTER ENGINE
# ==========================================

def _media_type_of(message):

    if message.photo:
        return "photo"

    if message.voice:
        return "voice"

    if message.video_note:
        return "video_note"

    if message.gif:
        return "animation"

    if message.sticker:
        return "sticker"

    if message.video:
        return "video"

    if message.audio:
        return "audio"

    if message.document:
        return "document"

    if message.poll:
        return "poll"

    if message.text:
        return "text"

    return "other"


def _passes_media_filter(message, settings):

    media_filter = settings.get("media_filter") or "all"

    if media_filter == "all":
        return True

    return _media_type_of(message) == media_filter


def _passes_keyword_filters(message, settings):

    text = (message.raw_text or "").lower()

    whitelist = [
        w.strip().lower()
        for w in (settings.get("keyword_whitelist") or "").split(",")
        if w.strip()
    ]

    if whitelist and not any(w in text for w in whitelist):
        return False

    blacklist = [
        w.strip().lower()
        for w in (settings.get("keyword_blacklist") or "").split(",")
        if w.strip()
    ]

    if blacklist and any(w in text for w in blacklist):
        return False

    return True


def _passes_regex_filter(message, settings):

    pattern = (settings.get("regex_filter") or "").strip()

    if not pattern:
        return True

    try:
        return re.search(pattern, message.raw_text or "") is not None
    except re.error:
        logger.warning("Invalid regex filter %r - treating as pass", pattern)
        return True


def _passes_all_filters(message, settings, content_rules=None, sender_id=None):

    if not (
        _passes_media_filter(message, settings)
        and _passes_keyword_filters(message, settings)
        and _passes_regex_filter(message, settings)
    ):
        return False

    if content_rules is not None:

        text = message.raw_text or ""
        urls = URL_RE.findall(text)

        if not content_rules_service.passes_content_rules(text, urls, sender_id, content_rules):
            return False

    return True


# ==========================================
# OWNER ALERTS
# ==========================================

async def _alert_owner(project_id, destination, text):
    """Pushes a real Telegram DM to whoever owns this project, so a
    forwarding problem is seen immediately instead of only living in
    the 📜 Logs screen. Throttled per (project, destination) so a
    chat failing on every message doesn't spam the owner."""

    try:
        project = project_service.get_project(project_id)
    except Exception:
        logger.exception("Could not look up owner for project %s", project_id)
        return

    if not project:
        return

    await notifier.notify_user(
        project["user_id"],
        text,
        throttle_key=f"forward_fail:{project_id}:{destination}",
    )


# ==========================================
# SEND WITH RETRY
# ==========================================

async def _send_with_retry(coro_factory, project_id, destination):
    """
    coro_factory: zero-arg callable returning a fresh awaitable each
    call (needed because a coroutine object can only be awaited once,
    and retries need a new one).
    """

    attempt = 0

    while True:

        try:
            return await coro_factory()

        except FloodWaitError as e:

            wait_for = e.seconds + 1

            log_service.add_log(
                project_id, "retry",
                f"FloodWait on {destination}: sleeping {wait_for}s"
            )
            stats_service.increment(project_id, "retried")

            await asyncio.sleep(wait_for)
            # FloodWait doesn't count against MAX_SEND_RETRIES - it's
            # not a failure, just Telegram enforcing pacing.
            continue

        except RPCError as e:

            attempt += 1

            if attempt > MAX_SEND_RETRIES:

                log_service.add_log(
                    project_id, "error",
                    f"Send to {destination} failed after {MAX_SEND_RETRIES} retries: {e}"
                )
                stats_service.increment(project_id, "failed")

                await _alert_owner(
                    project_id, destination,
                    "⚠ ChannelFlow AI\n\n"
                    f"Forwarding to {destination} keeps failing:\n{e}\n\n"
                    "This usually means the account isn't admin there "
                    "(needs 'Post Messages' permission), or it isn't a "
                    "member of the chat. Open the project → 📤 Destinations "
                    "→ 🧪 Test to confirm, or check 📜 Logs for details."
                )

                return None

            backoff = RETRY_BACKOFF_BASE ** attempt

            log_service.add_log(
                project_id, "retry",
                f"Send to {destination} failed ({e}); retrying in {backoff}s"
            )
            stats_service.increment(project_id, "retried")

            await asyncio.sleep(backoff)

        except Exception as e:

            logger.exception("Unexpected error sending to %s", destination)

            log_service.add_log(
                project_id, "error",
                f"Unexpected error sending to {destination}: {e}"
            )
            stats_service.increment(project_id, "failed")

            await _alert_owner(
                project_id, destination,
                "⚠ ChannelFlow AI\n\n"
                f"An unexpected error is blocking forwarding to {destination}:\n{e}\n\n"
                "Check 📜 Logs on the project for details."
            )

            return None


# ==========================================
# DISPATCH
# ==========================================

async def _dispatch(messages, route, client):

    project_id = route["project_id"]
    settings = route["settings"]

    representative = messages[0]

    sender_id = getattr(representative, "sender_id", None)

    owner_id = route.get("owner_id")

    if not _passes_all_filters(representative, settings, route.get("content_rules"), sender_id):
        stats_service.increment(project_id, "filtered")
        return

    if owner_id is not None:
        # Atomically check and reserve daily forward quota.
        # Uses BEGIN IMMEDIATE transaction so N concurrent forwards at
        # the limit can never all pass the check (prevents race overage).
        # Pairs with release_daily_forward() on forward failure.
        if not plan_service.reserve_daily_forward(owner_id, project_id):
            # Daily limit reached - counted as filtered
            stats_service.increment(project_id, "filtered")
            return

    delay_min = float(settings.get("delay_min") or 0)
    delay_max = float(settings.get("delay_max") or 0)

    if delay_max > 0:
        await asyncio.sleep(random.uniform(delay_min, delay_max))

    mode = settings.get("mode") or "forward"
    silent = bool(settings.get("silent"))
    protect_content = bool(settings.get("protect_content"))

    # ---- Text Replacement Engine (deterministic find/replace) ----
    # Applies per-project find/replace rules. Fast, cheap, no AI.
    # Run in copy mode only when text is available.
    replaced_text = None
    if mode == "copy" and (representative.raw_text or ""):
        from services import text_replacement_service
        replaced_text = text_replacement_service.apply_text_replacement(
            representative.raw_text, project_id,
        )

    # ---- Affiliate Link Replacer ----
    # Replaces supported shopping/affiliate URLs with the user's
    # configured affiliate links. Runs after text replacement so
    # rewritten text is not corrupted, and before AI rewriting so
    # the AI works on cleaned content.
    affiliate_text = None
    if mode == "copy" and (representative.raw_text or ""):
        from services import affiliate_service
        affiliate_text = affiliate_service.replace_affiliate_links(
            representative.raw_text if replaced_text is None else replaced_text,
            project_id, user_id=route.get("owner_id"),
        )

    # Use the text after affiliate replacement (or original if no
    # replacement was applied) as the base for subsequent steps.
    working_text = affiliate_text if affiliate_text is not None \
        else (replaced_text if replaced_text is not None else representative.raw_text)

# ---- AI rewriting (BUG-007 fix) ----
    # Copy-mode only (native forwards can't be edited). Computed ONCE
    # per dispatch, never per destination. Fail-safe: any error leaves
    # ai_text None and forwarding continues with original content -
    # an OpenRouter outage can never take the forward engine down.
    # Plan-based access control: only users with AI rewrite feature enabled
    # can have AI processing applied.
    ai_text = None
    if mode == "copy" and (working_text or ""):
        if not plan_service.has_feature(route.get("owner_id"), "ai_rewrite"):
            # User's plan does not include AI rewriting - skip AI
            log_service.add_log(project_id, "ai", "AI rewriting not enabled for plan")
        else:
            try:
                ai_cfg = ai_service.ensure_ai_settings(project_id)
                if ai_cfg["enabled"]:

                    platform_hint = "telegram"

                    result = await ai_service.rewrite_content(
                        working_text,
                        ai_cfg,
                        user_id=route.get("owner_id"),
                        project_id=project_id,
                        platform=platform_hint,
                    )

                    if result.success and result.text:
                        ai_text = result.text
                        log_service.add_log(
                            f"Rewritten via {result.model_used}"
                            f"{' (fallback)' if result.fallback_used else ''} "
                            f"in {result.latency_ms}ms"
                        )

            except Exception:
                logger.exception("AI processing failed for project %s", project_id)
                log_service.add_log(project_id, "ai", "AI processing error - used original")

    # Phase 7: formatting (prefix/suffix/remove/replace) only ever
    # applies in copy mode - a native Telegram forward can't have its
    # content edited, and faking that would misrepresent what actually
    # happened. Computed once per dispatch (not per destination) since
    # it's the same transform everywhere; skipped entirely (no DB read)
    # for the common case of a project with no formatting rules set, so
    # this is a true no-op for every project that hasn't opted in.
    formatted_text = None

    if mode == "copy" and route.get("has_formatting") and (working_text or ""):
        formatted_text = formatting_service.apply_formatting(
            project_id, working_text
        )

    # AI output wins over pure formatting when both are configured -
    # the AI prompt already preserves URLs/hashtags that formatting
    # would want preserved.
    final_text = ai_text if ai_text is not None else formatted_text

    # If neither AI nor formatting was applied, but text replacement
    # or affiliate link replacement was performed, use the transformed
    # text as the final text so it isn't lost. Fall back to original
    # only when truly no processing was configured.
    if final_text is None and working_text is not None \
            and working_text != representative.raw_text:
        final_text = working_text

    # ---- Watermark decision (BUG-007 fix) ----
    # Only photo media in copy mode gets watermarked. Applied ONCE per
    # dispatch; the resulting bytes are reused across destinations so a
    # 10-destination project re-encodes exactly one time.
    wm_bytes = None
    if mode == "copy" and _media_type_of(representative) == "photo":
        try:
            wm_cfg = watermark_service.ensure_watermark_settings(project_id)
            if wm_cfg["enabled"]:
                photo = representative.media.photo
                if photo is not None:
                    import io as _io
                    buf = _io.BytesIO()
                    await client.download_media(representative, file=buf)
                    data = buf.getvalue()
                    if data:
                        wm_bytes, applied = watermark_service.apply_watermark(data, project_id)
                        if not applied:
                            wm_bytes = None  # disabled/no-op - send original media object
        except Exception:
            logger.exception("Watermark failed for project %s - sending original", project_id)
            wm_bytes = None

    # The daily allowance unit for this source message was ALREADY
    # reserved atomically near the top of _dispatch (immediately after
    # the filters, before any transformation work). This is the single
    # reservation per incoming message - releasing it only happens here
    # or at the end of the destination loop, so a message that passes
    # the filters can never be double-reserved (PRD 22.1) and a failed
    # forward still consumes nothing.
    daily_reserved = owner_id is not None

    published_any = False

    for destination in route["destinations"]:

        # ---- Deduplication gate ----
        dedup_claim_id = dedup_service.claim(
            source_platform="telegram",
            source_account_id=owner_id,
            source_channel_id=representative.chat_id,
            source_message_id=representative.id,
            project_id=project_id,
            destination_id=destination,
        )

        if not dedup_claim_id:
            stats_service.increment(project_id, "filtered")
            continue

        async def _send(destination=destination):
            if mode == "forward":
                try:
                    return await client.forward_messages(
                        destination,
                        messages,
                        silent=silent,
                        noforwards=protect_content,
                    )
                except TypeError:
                    return await client.forward_messages(
                        destination,
                        messages,
                        silent=silent,
                    )

            if len(messages) > 1:
                files = [m.media for m in messages if m.media]
                caption = final_text if final_text is not None else next(
                    (m.raw_text for m in messages if m.raw_text), None
                )
                if files:
                    return await client.send_file(
                        destination,
                        files,
                        caption=caption,
                        silent=silent,
                        noforwards=protect_content,
                    )

            if wm_bytes is not None:
                import io as _io
                return await client.send_file(
                    destination,
                    _io.BytesIO(wm_bytes),
                    caption=final_text or "",
                    silent=silent,
                    noforwards=protect_content,
                    force_document=False,
                )

            if final_text is not None:
                if representative.media:
                    return await client.send_file(
                        destination,
                        representative.media,
                        caption=final_text,
                        silent=silent,
                        noforwards=protect_content,
                    )
                return await client.send_message(
                    destination,
                    final_text,
                    silent=silent,
                    noforwards=protect_content,
                )

            return await client.send_message(
                destination,
                representative,
                silent=silent,
                noforwards=protect_content,
            )

        result = await _send_with_retry(_send, project_id, destination)

        if result is not None:
            dedup_service.confirm(dedup_claim_id)
            published_any = True
            # The daily unit was already atomically reserved above.
            stats_service.increment(project_id, "forwarded", bump_daily=False)
            log_service.add_log(
                project_id, "forward",
                f"{representative.chat_id} -> {destination} "
                f"({len(messages)} message{'s' if len(messages) > 1 else ''})"
            )
        else:
            logger.warning("Forward failed permanently for claim %s", dedup_claim_id)
            dedup_service.release(dedup_claim_id)

    # ---- External platforms (WhatsApp / Threads) via persistent queue ----
    # The same daily reservation covers this dispatch; there is no
    # per-forward wallet deduction.
    for ext in route.get("external_destinations", []):

        ext_claim = dedup_service.claim(
            source_platform="telegram",
            source_account_id=owner_id,
            source_channel_id=representative.chat_id,
            source_message_id=representative.id,
            project_id=project_id,
            destination_id=ext["destination_row_id"],
        )

        if not ext_claim:
            continue

        raw_text = final_text if final_text is not None else (representative.raw_text or "")

        media_info = []
        if representative.media:
            if representative.photo:
                media_info.append({
                    "type": "photo",
                    "media": representative.photo,
                    "caption": representative.raw_text or "",
                })
            elif representative.video:
                media_info.append({
                    "type": "video",
                    "media": representative.video,
                    "caption": representative.raw_text or "",
                })
            elif representative.document:
                media_info.append({
                    "type": "document",
                    "media": representative.document,
                    "caption": representative.raw_text or "",
                })
            elif representative.gif:
                media_info.append({
                    "type": "animation",
                    "media": representative.gif,
                    "caption": representative.raw_text or "",
                })

        payload = {
            "project_id": project_id,
            "owner_id": owner_id,
            "platform": ext["platform"],
            "account_identifier": ext["account_identifier"],
            "platform_account_id": ext["platform_account_id"],
            "destination_row_id": ext["destination_row_id"],
            "text": raw_text,
            "source_chat": str(representative.chat_id),
            "source_msg_id": representative.id,
            "dedup_claim_id": ext_claim,
            "media_info": media_info,
            "ai_settings": route.get("ai_settings"),
            "formatting_rules": route.get("has_formatting"),
        }

        try:
            from services import job_queue as _jq
            _jq.enqueue("publish", payload)
            published_any = True
            log_service.add_log(
                project_id, "queue", f"Queued {ext['platform']} publish"
            )
        except Exception:
            logger.exception("Failed to enqueue %s publish", ext["platform"])
            dedup_service.release(ext_claim)

    # If every destination was a duplicate or failed before publication,
    # return the reserved daily unit. This is quota-only; there is no
    # wallet/per-forward charge.
    if daily_reserved and not published_any:
        try:
            plan_service.release_daily_forward(project_id)
        except Exception:
            logger.exception(
                "Failed to release daily quota for project %s", project_id
            )


# ==========================================
# ALBUM BUFFERING
# ==========================================

_album_buffers = {}  # (chat_id, grouped_id, project_id) -> {"messages": [...], "route": ..., "client": ..., "task": Task}


async def _flush_album(key):
    await asyncio.sleep(ALBUM_DEBOUNCE_SECONDS)

    buffered = _album_buffers.pop(key, None)
    if not buffered:
        return

    messages = sorted(buffered["messages"], key=lambda m: m.id)
    await _dispatch(messages, buffered["route"], buffered["client"])


async def _handle_album_message(message, route, client):
    key = (message.chat_id, message.grouped_id, route["project_id"])

    if key not in _album_buffers:
        _album_buffers[key] = {
            "messages": [message],
            "route": route,
            "client": client,
            "task": asyncio.create_task(_flush_album(key)),
        }
    else:
        _album_buffers[key]["messages"].append(message)


# ==========================================
# EVENT HANDLER
# ==========================================

async def forward_message(event, client, routes, routes_lock):
    source_chat = str(event.chat_id)

    async with routes_lock:
        matched_routes = routes.get(source_chat)

    if not matched_routes:
        return

    message = event.message

    for route in matched_routes:
        keep_media_groups = bool(route["settings"].get("keep_media_groups"))

        if message.grouped_id and keep_media_groups:
            await _handle_album_message(message, route, client)
        else:
            asyncio.create_task(_dispatch([message], route, client))


# ==========================================
# START / RUN
# ==========================================

async def start_forwarder():
    await ensure_started()
    await _refresh_routes()

    refresh_task = asyncio.create_task(_routes_refresh_loop())

    async def _shared_handler(event):
        await forward_message(event, client, _ROUTES, _routes_lock)

    client.add_event_handler(_shared_handler, events.NewMessage)
    logger.info("Forward Engine running")

    try:
        await client.run_until_disconnected()
    finally:
        refresh_task.cancel()


def run_forwarder():
    """Run the shared forwarder in a background thread with reconnect backoff."""
    delay = 5

    while True:
        try:
            asyncio.run(start_forwarder())
            delay = 5
        except KeyboardInterrupt:
            raise
        except Exception:
            logger.exception("Forward engine stopped; restarting in %ss", delay)
            time.sleep(delay)
            delay = min(delay * 2, 60)

