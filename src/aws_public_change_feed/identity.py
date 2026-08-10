"""Canonical URL processing and null-framed identity digests.

Chapter 04 requires one framing helper for runtime and test vectors. This
module is that helper: `scripts/validate_config.py` imports it rather than
carrying a second copy, so a change to framing or canonicalization cannot move
the runtime and the contract fixtures apart.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable
from urllib.parse import unquote_plus, urlsplit, urlunsplit

__all__ = [
    "TRACKING_QUERY_KEYS",
    "application_artifact_id",
    "announcement_id",
    "audience_fingerprint",
    "candidate_id",
    "canonical_public_url",
    "content_fingerprint",
    "delivery_request_id",
    "digest_parts",
    "evidence_order",
    "identity_text",
    "queue_dispatch_id",
    "release_id",
    "revision_id",
]

_DIGEST = re.compile(r"[a-f0-9]{64}")
_APPLICATION_ARTIFACT_ID = re.compile(r"sha256:[a-f0-9]{64}")

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


def application_artifact_id(value: str) -> str:
    """Validate and return the content address used as application version."""

    if not isinstance(value, str) or _APPLICATION_ARTIFACT_ID.fullmatch(value) is None:
        raise ValueError("application_version must be sha256 followed by a lowercase SHA-256 digest")
    return value


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


def identity_text(value: str) -> str:
    """Normalize a value for identity framing.

    Distinct from display normalization on purpose. Identity folds case so an
    editorial capitalization change does not mint a new revision; the stored
    title keeps its original case because Slack shows it to a human. Callers
    pass display text and this applies the fold, so the two cannot drift.
    """

    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def evidence_order(value: str) -> tuple[str, str]:
    """Sort key for the normalized lexical order chapter 04 fixes.

    `identity_text` alone is not a total order. Two evidence values differing
    only in case or spacing fold to the same key, and `sorted` then falls back
    to input order, which for matched terms is set iteration order and so
    depends on the hash seed. Configuration validation rejects aliases and
    terms that collide once normalized, making the tie unreachable today; the
    raw value is appended anyway so candidate identity does not rest on that
    rule continuing to hold.
    """

    return (identity_text(value), value)


def announcement_id(canonical_url: str) -> str:
    """SHA-256 of the canonical URL bytes."""

    return hashlib.sha256(canonical_url.encode()).hexdigest()


def content_fingerprint(title: str, summary: str) -> str:
    """Fingerprint announcement content. Takes display text, folds internally."""

    return digest_parts("announcement-content:v1", identity_text(title), identity_text(summary))


def revision_id(announcement: str, fingerprint: str) -> str:
    return digest_parts("announcement-revision:v1", announcement, fingerprint)


def release_id(config_sha256: str, inventory_sha256: str) -> str:
    return digest_parts("release:v1", config_sha256, inventory_sha256)


def audience_fingerprint(environment_ids: Iterable[str]) -> str:
    """Fingerprint the sorted environment IDs a candidate addresses."""

    return digest_parts("candidate-audience:v1", *sorted(environment_ids))


def candidate_id(
    revision: str,
    service_id: str,
    risk_type: str,
    route_id: str,
    audience: str,
) -> str:
    """Route-scoped candidate identity, per ADR-002 and chapter 04.

    The rule ID is deliberately absent: renaming a rule that keeps its risk
    type and evidence must not mint a new candidate.
    """

    return digest_parts("candidate:v3", revision, service_id, risk_type, route_id, audience)


def delivery_request_id(candidate: str) -> str:
    return digest_parts("delivery-request:v2", candidate)


def queue_dispatch_id(request_id: str, generation: int) -> str:
    if not _DIGEST.fullmatch(request_id):
        raise ValueError("queue dispatch request_id must be a lowercase SHA-256 digest")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise ValueError("queue dispatch generation must be a positive integer")
    return digest_parts("queue-dispatch:v1", request_id, str(generation))
