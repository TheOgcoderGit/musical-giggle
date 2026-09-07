"""
ChannelFlow AI - Affiliate Link Replacer Service

Replaces supported shopping/affiliate URLs in forwarded posts with
the user's configured affiliate link.

Supported providers (pattern-based, no external API calls):
    * Amazon
    * Flipkart
    * Meesho
    * Wishlink
    * EarnKaro

Design:
    * Pattern matching only - no external API calls
    * Multiple links in the same message are all processed
    * Unsupported/invalid URLs are left untouched
    * Existing query parameters are preserved where safe
    * Failure always falls back to the original URL
    * Per-project configuration (enabled/disable per provider)
    * Per-project isolated settings
"""

import re
import logging
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from database.db import get_connection

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# URL pattern matching for supported affiliate providers
# Each pattern matches the domain and captures the core product ID / path
# so we can reconstruct the link with the user's affiliate parameters.
# ---------------------------------------------------------------------------

# Amazon: matches amazon.in, amazon.com, and any amazon.* domain
AMAZON_DOMAIN_RE = re.compile(
    r"https?://(?:www\.)?(amazon\.[a-z]{2,})/(?:ref=[^?&]*)?([/?#&].*)?",
    re.IGNORECASE,
)

# Flipkart: matches flipkart.com
FLIPKART_DOMAIN_RE = re.compile(
    r"https?://(?:www\.)?flipkart\.com/[^?&]*(?:\?[^?&]*)?",
    re.IGNORECASE,
)

# Meesho: matches meesho.com
MEESHO_DOMAIN_RE = re.compile(
    r"https?://(?:www\.)?meesho\.com/[^?&]*(?:[/?#][^?&]*)?(?:\?[^?&]*)?",
    re.IGNORECASE,
)

# Wishlink: matches wish.com (Wish's new domain)
WISHLINK_DOMAIN_RE = re.compile(
    r"https?://(?:www\.)?wish\.com/[^?&]*(?:[/?#][^?&]*)?(?:\?[^?&]*)?",
    re.IGNORECASE,
)

# EarnKaro: matches app.earnkaro.com and links.earnkaro.com
EARNKARO_DOMAIN_RE = re.compile(
    r"https?://(?:www\.)?(?:app|links)\.earnkaro\.com/[^?&]*(?:[/?#][^?&]*)?(?:\?[^?&]*)?",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Per-project affiliate settings
# ---------------------------------------------------------------------------

def ensure_affiliate_settings(project_id: int) -> dict:
    """Get or create affiliate settings row for a project."""

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT * FROM project_affiliate_settings WHERE project_id=?", (project_id,))
    row = cur.fetchone()

    if row is None:
        cur.execute(
            "INSERT INTO project_affiliate_settings(project_id) VALUES(?)",
            (project_id,),
        )
        conn.commit()
        cur.execute("SELECT * FROM project_affiliate_settings WHERE project_id=?", (project_id,))
        row = cur.fetchone()

    conn.close()

    return {
        "enabled": bool(row["enabled"]),
        "amazon_enabled": bool(row["amazon_enabled"]),
        "flipkart_enabled": bool(row["flipkart_enabled"]),
        "meesho_enabled": bool(row["meesho_enabled"]),
        "wishlink_enabled": bool(row["wishlink_enabled"]),
        "earnkaro_enabled": bool(row["earnkaro_enabled"]),
        "amazon_associate_tag": row["amazon_associate_tag"] or "",
        "flipkart_publisher_id": row["flipkart_publisher_id"] or "",
        "meesho_partner_id": row["meesho_partner_id"] or "",
        "wishlink_partner_id": row["wishlink_partner_id"] or "",
        "earnkaro_publisher_id": row["earnkaro_publisher_id"] or "",
    }


def update_affiliate_settings(project_id: int, **fields):
    """Update specific affiliate setting fields."""

    allowed = {
        "enabled", "amazon_enabled", "flipkart_enabled", "meesho_enabled",
        "wishlink_enabled", "earnkaro_enabled",
        "amazon_associate_tag", "flipkart_publisher_id",
        "meesho_partner_id", "wishlink_partner_id", "earnkaro_publisher_id",
    }

    filtered = {k: v for k, v in fields.items() if k in allowed}
    if not filtered:
        return

    conn = get_connection()
    cur = conn.cursor()

    ensure_affiliate_settings(project_id)

    sets = ", ".join(f"{k}=?" for k in filtered)
    values = list(filtered.values()) + [project_id]

    cur.execute(
        f"UPDATE project_affiliate_settings SET {sets} WHERE project_id=?",
        values,
    )

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Core replacement logic
# ---------------------------------------------------------------------------

def _build_affiliate_url(base_url: str, provider: str, settings: dict) -> str:
    """Build an affiliate URL by appending the provider's params to the
    base URL's query string, preserving existing parameters where safe."""

    parsed = urlparse(base_url)

    # Extract existing query params (keep original keys/values)
    existing_qs = parse_qs(parsed.query, keep_blank_values=True)

    # Provider-specific affiliate params
    if provider == "amazon":
        associate = settings.get("amazon_associate_tag", "")
        if associate:
            existing_qs["tag"] = associate

    elif provider == "flipkart":
        publisher = settings.get("flipkart_publisher_id", "")
        if publisher:
            existing_qs["aff_id"] = publisher

    elif provider == "meesho":
        partner = settings.get("meesho_partner_id", "")
        if partner:
            existing_qs["partner_id"] = partner

    elif provider == "wishlink":
        partner = settings.get("wishlink_partner_id", "")
        if partner:
            existing_qs["aff_id"] = partner

    elif provider == "earnkaro":
        publisher = settings.get("earnkaro_publisher_id", "")
        if publisher:
            existing_qs["pub"] = publisher

    # Rebuild query string, preserving original order for params we didn't touch
    new_qs = urlencode(existing_qs, doseq=True)

    return urlunparse(parsed._replace(query=new_qs))


def _replace_affiliate_urls(text: str, settings: dict) -> str:
    """Scan text for supported affiliate URLs and replace them.

    Returns the text with all detected affiliate URLs replaced, or the
    original text unchanged on any error.
    """

    if not text or not text.strip():
        return text

    # If affiliate processing is disabled for the project, return early
    if not settings.get("enabled"):
        return text

    result = text

    # --- Amazon ---
    if settings.get("amazon_enabled"):
        for match in AMAZON_DOMAIN_RE.finditer(result):
            orig_url = match.group(0)
            try:
                affiliate_url = _build_affiliate_url(orig_url, "amazon", settings)
                if affiliate_url != orig_url:
                    result = result.replace(orig_url, affiliate_url, 1)
            except Exception:
                logger.exception("Affiliate URL replacement failed for Amazon URL")
                # Leave original URL untouched on error

    # --- Flipkart ---
    if settings.get("flipkart_enabled"):
        for match in FLIPKART_DOMAIN_RE.finditer(result):
            orig_url = match.group(0)
            try:
                affiliate_url = _build_affiliate_url(orig_url, "flipkart", settings)
                if affiliate_url != orig_url:
                    result = result.replace(orig_url, affiliate_url, 1)
            except Exception:
                logger.exception("Affiliate URL replacement failed for Flipkart URL")

    # --- Meesho ---
    if settings.get("meesho_enabled"):
        for match in MEESHO_DOMAIN_RE.finditer(result):
            orig_url = match.group(0)
            try:
                affiliate_url = _build_affiliate_url(orig_url, "meesho", settings)
                if affiliate_url != orig_url:
                    result = result.replace(orig_url, affiliate_url, 1)
            except Exception:
                logger.exception("Affiliate URL replacement failed for Meesho URL")

    # --- Wishlink ---
    if settings.get("wishlink_enabled"):
        for match in WISHLINK_DOMAIN_RE.finditer(result):
            orig_url = match.group(0)
            try:
                affiliate_url = _build_affiliate_url(orig_url, "wishlink", settings)
                if affiliate_url != orig_url:
                    result = result.replace(orig_url, affiliate_url, 1)
            except Exception:
                logger.exception("Affiliate URL replacement failed for Wishlink URL")

    # --- EarnKaro ---
    if settings.get("earnkaro_enabled"):
        for match in EARNKARO_DOMAIN_RE.finditer(result):
            orig_url = match.group(0)
            try:
                affiliate_url = _build_affiliate_url(orig_url, "earnkaro", settings)
                if affiliate_url != orig_url:
                    result = result.replace(orig_url, affiliate_url, 1)
            except Exception:
                logger.exception("Affiliate URL replacement failed for EarnKaro URL")

    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def replace_affiliate_links(text: str, project_id: int, user_id: int = None) -> str:
    """Main entry point. Replace affiliate links in *text* for the given
    project. On failure or if the feature is disabled, returns the
    original text unchanged."""

    try:
        settings = ensure_affiliate_settings(project_id)
    except Exception:
        logger.exception("Failed to load affiliate settings for project %s", project_id)
        return text

    if not settings.get("enabled"):
        return text

    return _replace_affiliate_urls(text, settings)


def is_affiliate_url(url: str) -> bool:
    """Check if a URL matches any supported affiliate provider pattern."""
    return bool(
        AMAZON_DOMAIN_RE.match(url)
        or FLIPKART_DOMAIN_RE.match(url)
        or MEESHO_DOMAIN_RE.match(url)
        or WISHLINK_DOMAIN_RE.match(url)
        or EARNKARO_DOMAIN_RE.match(url)
    )


def get_supported_providers() -> list:
    """Return a list of provider names that have pattern matching implemented."""
    return ["amazon", "flipkart", "meesho", "wishlink", "earnkaro"]