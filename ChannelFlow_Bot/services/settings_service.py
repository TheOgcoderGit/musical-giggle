"""
Per-project forward/filter/delay settings.

A project_settings row is created lazily the first time it's read, so
every existing project automatically gets sane defaults without a
migration step.
"""

from database.db import get_connection

VALID_MODES = ("forward", "copy")

# Media type labels the filter engine understands. "all" disables
# filtering entirely.
VALID_MEDIA_FILTERS = (
    "all", "text", "photo", "video", "audio", "document",
    "voice", "sticker", "poll", "animation", "video_note",
)

DEFAULTS = {
    "mode": "forward",
    "silent": 0,
    "protect_content": 0,
    "keep_media_groups": 1,
    "delay_min": 0.0,
    "delay_max": 0.0,
    "media_filter": "all",
    "keyword_whitelist": "",
    "keyword_blacklist": "",
    "regex_filter": "",
}


def get_settings(project_id):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        "SELECT * FROM project_settings WHERE project_id=?",
        (project_id,)
    )

    row = cur.fetchone()

    if row is None:

        cur.execute(
            "INSERT INTO project_settings(project_id) VALUES(?)",
            (project_id,)
        )

        conn.commit()

        cur.execute(
            "SELECT * FROM project_settings WHERE project_id=?",
            (project_id,)
        )

        row = cur.fetchone()

    conn.close()

    return row


def update_settings(project_id, **fields):
    """
    Updates any subset of columns on project_settings. Ensures the row
    exists first (get_settings creates it lazily).

    Example:
        update_settings(5, mode="copy", delay_min=1.0, delay_max=3.0)
    """

    if not fields:
        return

    get_settings(project_id)

    allowed = set(DEFAULTS.keys())
    columns = []
    values = []

    for key, value in fields.items():

        if key not in allowed:
            raise ValueError(f"Unknown setting: {key}")

        columns.append(f"{key}=?")
        values.append(value)

    values.append(project_id)

    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        f"UPDATE project_settings SET {', '.join(columns)} WHERE project_id=?",
        values
    )

    conn.commit()
    conn.close()


def toggle_mode(project_id):

    settings = get_settings(project_id)
    new_mode = "copy" if settings["mode"] == "forward" else "forward"
    update_settings(project_id, mode=new_mode)
    return new_mode


def toggle_flag(project_id, field):
    """Flips a 0/1 boolean-style column (silent, protect_content, keep_media_groups)."""

    if field not in ("silent", "protect_content", "keep_media_groups"):
        raise ValueError(f"{field} is not a toggleable flag")

    settings = get_settings(project_id)
    new_value = 0 if settings[field] else 1
    update_settings(project_id, **{field: new_value})
    return new_value


def set_delay(project_id, delay_min, delay_max):

    delay_min = max(0.0, float(delay_min))
    delay_max = max(delay_min, float(delay_max))

    update_settings(project_id, delay_min=delay_min, delay_max=delay_max)


def set_media_filter(project_id, media_filter):

    if media_filter not in VALID_MEDIA_FILTERS:
        raise ValueError(f"Invalid media filter: {media_filter}")

    update_settings(project_id, media_filter=media_filter)


def set_keyword_whitelist(project_id, text):
    update_settings(project_id, keyword_whitelist=(text or "").strip())


def set_keyword_blacklist(project_id, text):
    update_settings(project_id, keyword_blacklist=(text or "").strip())


def set_regex_filter(project_id, pattern):
    update_settings(project_id, regex_filter=(pattern or "").strip())
