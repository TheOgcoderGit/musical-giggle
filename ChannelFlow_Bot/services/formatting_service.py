"""
ChannelFlow AI - Formatting Service
======================================

ONE shared prefix/suffix/remove/replace engine, used by:
    * core/forwarder.py, in Telegram copy mode only (never in forward
      mode - Telegram's native forward can't have its content edited,
      and the spec requires preserving native forward behaviour rather
      than faking a transformation that didn't really happen).
    * services/instagram_service.format_for_instagram(), which calls
      apply_formatting() for the base prefix/suffix/replace/remove step
      and then layers its own Instagram-specific CTA/hashtags/link
      placement on top.

This used to be two separate implementations (a Telegram-side one and
an Instagram-side one with its own prefix/suffix). Consolidated into
this module so a formatting rule is defined once per project and
applies everywhere, per the "don't duplicate the same feature across
routes" principle - see services/instagram_service.py's module
docstring for the specific dedup this replaced.

URL safety: replace_rules and remove_patterns never touch anything
that looks like a URL. This is a hard rule, not a suggestion - link
rules / affiliate URLs must never be silently mangled by an unrelated
text-replace rule (see destinations/instagram_destination.py's
docstring on the same constraint from the other direction).
"""

import json
import re

from database.db import get_connection

URL_RE = re.compile(r"https?://\S+")

DEFAULTS = {
    "prefix": "",
    "suffix": "",
    "remove_patterns": "[]",
    "replace_rules": "[]",
}


def get_rules(project_id):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT * FROM formatting_rules WHERE project_id=?", (project_id,))
    row = cur.fetchone()

    if row is None:

        cur.execute("INSERT INTO formatting_rules(project_id) VALUES(?)", (project_id,))
        conn.commit()

        cur.execute("SELECT * FROM formatting_rules WHERE project_id=?", (project_id,))
        row = cur.fetchone()

    conn.close()

    return row


def set_prefix(project_id, prefix):
    _update(project_id, prefix=prefix or "")


def set_suffix(project_id, suffix):
    _update(project_id, suffix=suffix or "")


def add_replace_rule(project_id, find, replace):

    if not find:
        raise ValueError("find text cannot be empty")

    rules = get_replace_rules(project_id)
    rules.append({"find": find, "replace": replace or ""})
    _update(project_id, replace_rules=json.dumps(rules))


def remove_replace_rule(project_id, index):

    rules = get_replace_rules(project_id)

    if 0 <= index < len(rules):
        rules.pop(index)
        _update(project_id, replace_rules=json.dumps(rules))


def get_replace_rules(project_id):

    row = get_rules(project_id)

    try:
        return json.loads(row["replace_rules"] or "[]")
    except (TypeError, ValueError):
        return []


def add_remove_pattern(project_id, pattern):

    if not pattern:
        raise ValueError("pattern cannot be empty")

    patterns = get_remove_patterns(project_id)
    patterns.append(pattern)
    _update(project_id, remove_patterns=json.dumps(patterns))


def remove_remove_pattern(project_id, index):

    patterns = get_remove_patterns(project_id)

    if 0 <= index < len(patterns):
        patterns.pop(index)
        _update(project_id, remove_patterns=json.dumps(patterns))


def get_remove_patterns(project_id):

    row = get_rules(project_id)

    try:
        return json.loads(row["remove_patterns"] or "[]")
    except (TypeError, ValueError):
        return []


def clear_rules(project_id):
    _update(project_id, **DEFAULTS)


def _update(project_id, **fields):

    get_rules(project_id)

    columns = [f"{k}=?" for k in fields]
    values = list(fields.values()) + [project_id]

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        f"UPDATE formatting_rules SET {', '.join(columns)} WHERE project_id=?",
        values,
    )

    conn.commit()
    conn.close()


def _protect_urls(text):
    """Swaps every URL for a short placeholder token before any
    remove/replace runs, then swaps them back afterward - so a rule
    like replace('9' -> '') can never accidentally eat a digit out of
    an affiliate link's query string."""

    urls = URL_RE.findall(text)
    protected = text

    for i, url in enumerate(urls):
        protected = protected.replace(url, f"\x00URL{i}\x00", 1)

    return protected, urls


def _restore_urls(text, urls):

    for i, url in enumerate(urls):
        text = text.replace(f"\x00URL{i}\x00", url)

    return text


def apply_formatting(project_id, text) -> str:
    """The whole transform: prefix -> body (remove -> replace, URL-safe)
    -> suffix. A project with no rules configured returns `text`
    unchanged (byte-for-byte), so this is a true no-op for every
    existing project until someone opts in."""

    text = text or ""
    rules = get_rules(project_id)

    protected, urls = _protect_urls(text)

    for pattern in json.loads(rules["remove_patterns"] or "[]"):
        try:
            protected = re.sub(pattern, "", protected)
        except re.error:
            protected = protected.replace(pattern, "")

    for rule in json.loads(rules["replace_rules"] or "[]"):
        protected = protected.replace(rule["find"], rule["replace"])

    body = _restore_urls(protected, urls)

    if not rules["prefix"] and not rules["suffix"]:
        return body

    parts = [p for p in (rules["prefix"], body, rules["suffix"]) if p and p.strip()]

    return "\n\n".join(parts)
