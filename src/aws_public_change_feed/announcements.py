"""Normalization, canonical identity, and provenance coalescing.

Chapter 04: accept items with a resolvable canonical HTTPS URL and a nonempty
title; publication time is optional and `observed_at` is required. Normalization
applies Unicode normalization, HTML sanitization to plain text, whitespace
collapse, and bounded truncation with an explicit marker. Items are grouped by
canonical URL with merged sorted provenance, and the normalized announcement
uses `source_type = public_feed` because one announcement can merge provenance
from feeds with different formats.
"""

from __future__ import annotations

import html
import re
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

from .feedparse import ParsedItem
from .identity import announcement_id, canonical_public_url, content_fingerprint, revision_id
from .normalize import normalize_text

__all__ = [
    "MAX_SUMMARY_CHARACTERS",
    "MAX_TITLE_CHARACTERS",
    "NormalizedAnnouncement",
    "Provenance",
    "TRUNCATION_MARKER",
    "coalesce",
    "normalize_item",
]

MAX_TITLE_CHARACTERS = 300
MAX_SUMMARY_CHARACTERS = 2_000
TRUNCATION_MARKER = "…"
SOURCE_TYPE = "public_feed"

_TAG = re.compile(r"<[^>]*>")


@dataclass(frozen=True, slots=True, order=True)
class Provenance:
    """One feed's sighting of an announcement, ordered for deterministic merge."""

    feed_name: str
    item_url: str


@dataclass(frozen=True, slots=True)
class NormalizedAnnouncement:
    canonical_url: str
    title: str
    summary: str
    observed_at: datetime
    published_at: datetime | None
    provenance: tuple[Provenance, ...]

    @property
    def announcement_id(self) -> str:
        return announcement_id(self.canonical_url)

    @property
    def content_fingerprint(self) -> str:
        # The stored title and summary keep their original case for display;
        # `content_fingerprint` folds case internally, which is what the
        # contract fixtures and the validator require.
        return content_fingerprint(self.title, self.summary)

    @property
    def revision_id(self) -> str:
        return revision_id(self.announcement_id, self.content_fingerprint)

    @property
    def source_type(self) -> str:
        return SOURCE_TYPE


def sanitize(raw: str, limit: int) -> str:
    """HTML to plain text, Unicode normalized, whitespace collapsed, bounded."""

    text = normalize_text(html.unescape(_TAG.sub(" ", raw)))
    # Unescaping can reveal markup that was entity-encoded, so strip once more
    # rather than returning tags that arrived as &lt;script&gt;.
    text = normalize_text(_TAG.sub(" ", text))
    if len(text) > limit:
        head = text[:limit].rstrip()
        text = f"{head}{TRUNCATION_MARKER}"
    return text


def parse_published(raw: str | None) -> datetime | None:
    """Parse a feed publication timestamp, tolerating its absence.

    Chapter 04 makes publication time optional, so an unparseable value is
    treated as absent rather than failing the item.
    """

    if not raw:
        return None
    candidate = raw.strip()
    for parser in (_parse_rfc2822, _parse_iso8601):
        parsed = parser(candidate)
        if parsed is not None:
            return parsed.astimezone(UTC)
    return None


def _parse_rfc2822(value: str) -> datetime | None:
    from email.utils import parsedate_to_datetime

    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _parse_iso8601(value: str) -> datetime | None:
    text = f"{value[:-1]}+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def normalize_item(
    item: ParsedItem, feed_name: str, observed_at: datetime, approved_hosts: Collection[str]
) -> NormalizedAnnouncement | None:
    """Normalize one parsed item, or return None when it cannot be accepted.

    Two URLs matter here and both are checked. The canonical URL carries
    identity, so the host allowlist and default-port checks run on it rather
    than on raw feed text. The raw URL is kept as the sighting record in
    provenance and reaches the candidate contract as `source_item_id` and
    `source_item_url`, so anything canonicalization silently repairs has to be
    rejected here instead: credentials in the netloc are dropped by
    canonicalization but would remain in the stored sighting, where the
    contract forbids them. A fragment is the one raw/canonical difference the
    contract accepts, because the provenance rule is that the sighting
    canonicalizes to the announcement URL rather than equalling it.

    A malformed URL (for example a non-numeric port) cannot abort the
    acquisition run, so it is dropped here.
    """

    # Checked on the raw netloc, not on `parsed.username`: an empty user-info
    # component (`https://@host/x`) leaves a falsy username behind and would
    # otherwise be stored verbatim.
    if "@" in urlsplit(item.url).netloc:
        return None

    try:
        canonical = canonical_public_url(item.url)
    except ValueError:
        return None
    if not canonical.startswith("https://"):
        return None

    # The allowlist is folded here rather than trusted from callers: hosts in
    # the canonical URL are always lowercase, so an unfolded list would reject
    # every item the moment one caller hands over a differently cased set.
    approved = {host.casefold() for host in approved_hosts}
    parsed = urlsplit(canonical)
    if (parsed.hostname or "") not in approved:
        return None
    try:
        if parsed.port not in (None, 443):
            return None
    except ValueError:
        # Defensive only, and not for the reason it looks like. Canonicalization
        # raises on a non-numeric port, so that form never arrives. A bracketed
        # IPv6 URL does reach here re-parsed and unbracketed
        # (`https://[::1]/x` canonicalizes to `https://::1/x`, whose port text
        # is `:1`), but the allowlist check above has already dropped it: the
        # mangled hostname cannot be an approved host. Kept so a reordering of
        # these two checks cannot turn a dropped item into a raised exception.
        return None

    title = sanitize(item.title, MAX_TITLE_CHARACTERS)
    if not title:
        return None

    return NormalizedAnnouncement(
        canonical_url=canonical,
        title=title,
        summary=sanitize(item.summary, MAX_SUMMARY_CHARACTERS),
        observed_at=observed_at,
        published_at=parse_published(item.published_raw),
        provenance=(Provenance(feed_name=feed_name, item_url=item.url),),
    )


def _precedence(candidate: NormalizedAnnouncement, current: NormalizedAnnouncement) -> bool:
    """Deterministic precedence when feeds disagree about the same URL.

    The earlier publication time wins; absent times lose to present ones. Ties
    break on the lexically smaller title, so the merge never depends on feed
    ordering or dictionary iteration.
    """

    if (candidate.published_at is None) != (current.published_at is None):
        return current.published_at is None
    if candidate.published_at is not None and current.published_at is not None:
        if candidate.published_at != current.published_at:
            return candidate.published_at < current.published_at
    return (candidate.title, candidate.summary) < (current.title, current.summary)


def coalesce(announcements: Iterable[NormalizedAnnouncement]) -> tuple[NormalizedAnnouncement, ...]:
    """Group by canonical URL, merging sorted provenance.

    One announcement seen through several feeds becomes one record whose
    provenance lists every sighting, sorted, so the result does not depend on
    the order feeds were fetched.
    """

    merged: dict[str, NormalizedAnnouncement] = {}
    provenance: dict[str, set[Provenance]] = {}

    for announcement in announcements:
        key = announcement.canonical_url
        provenance.setdefault(key, set()).update(announcement.provenance)
        current = merged.get(key)
        if current is None or _precedence(announcement, current):
            merged[key] = announcement

    results: list[NormalizedAnnouncement] = []
    for key in sorted(merged):
        chosen = merged[key]
        results.append(
            NormalizedAnnouncement(
                canonical_url=chosen.canonical_url,
                title=chosen.title,
                summary=chosen.summary,
                observed_at=chosen.observed_at,
                published_at=chosen.published_at,
                provenance=tuple(sorted(provenance[key])),
            )
        )
    return tuple(results)


def sorted_by_identity(announcements: Sequence[NormalizedAnnouncement]) -> tuple[NormalizedAnnouncement, ...]:
    """Stable ordering for replay comparisons."""

    return tuple(sorted(announcements, key=lambda item: item.announcement_id))
