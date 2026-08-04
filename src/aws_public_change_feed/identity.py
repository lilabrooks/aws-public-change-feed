"""Canonical URL processing and null-framed identity digests.

Chapter 04 requires one framing helper for runtime and test vectors. This
module is that helper: `scripts/validate_config.py` imports it rather than
carrying a second copy, so a change to framing or canonicalization cannot move
the runtime and the contract fixtures apart.
"""

from __future__ import annotations

import hashlib
from urllib.parse import unquote_plus, urlsplit, urlunsplit

__all__ = [
    "TRACKING_QUERY_KEYS",
    "announcement_id",
    "canonical_public_url",
    "content_fingerprint",
    "digest_parts",
    "revision_id",
]

# Reviewed list. Canonicalization removes only these; it must not strip
# arbitrary parameters, because distinct resources can differ by query alone.
TRACKING_QUERY_KEYS = frozenset(
    {
        "sc_channel",
        "trk",
        "trkcampaign",
        "utm_campaign",
        "utm_content",
        "utm_medium",
        "utm_source",
        "utm_term",
    }
)


def digest_parts(*values: str) -> str:
    """SHA-256 over null-framed UTF-8 values."""

    if any("\0" in value for value in values):
        raise ValueError("null-framed identity fields cannot contain null characters")
    return hashlib.sha256("\0".join(values).encode()).hexdigest()


def canonical_public_url(raw_url: str) -> str:
    """Apply the canonical URL rules chapter 04 permits, and nothing more.

    Scheme and host case, default-port removal, fragment removal, and the
    reviewed tracking-parameter list. Path is never normalized, because
    collapsing paths can merge distinct resources.
    """

    parsed = urlsplit(raw_url)
    host = (parsed.hostname or "").casefold()
    port = parsed.port
    netloc = host if port in (None, 443) else f"{host}:{port}"
    path = parsed.path or "/"
    query_parts = []
    for part in parsed.query.split("&") if parsed.query else []:
        encoded_key = part.partition("=")[0]
        if unquote_plus(encoded_key).casefold() not in TRACKING_QUERY_KEYS:
            query_parts.append(part)
    query = "&".join(query_parts)
    return urlunsplit((parsed.scheme.casefold(), netloc, path, query, ""))


def announcement_id(canonical_url: str) -> str:
    """SHA-256 of the canonical URL bytes."""

    return hashlib.sha256(canonical_url.encode()).hexdigest()


def content_fingerprint(normalized_title: str, normalized_summary: str) -> str:
    return digest_parts("announcement-content:v1", normalized_title, normalized_summary)


def revision_id(announcement: str, fingerprint: str) -> str:
    return digest_parts("announcement-revision:v1", announcement, fingerprint)
