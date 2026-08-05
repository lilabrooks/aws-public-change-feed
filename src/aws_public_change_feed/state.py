"""Source-state and raw-snapshot ports.

Chapter 02's source-state table holds two item types, and both live here, the
way `outbox.py` holds the delivery table's candidate and delivery items.

The feed item carries feed URL, ETag, Last-Modified, last attempt, last success,
newest observed publication time, consecutive failures, last error class, lease
owner, lease expiry, and TTL. The announcement item carries the canonical URL,
latest content fingerprint, known revision IDs, normalized title and summary,
first and last observation, optional publication time, merged provenance,
emitted candidate IDs, and release references.

The ports are defined here with in-memory and file-backed implementations, so
conditional requests, checkpoint rules, failure accounting, provenance merging,
and revision tracking are testable before any AWS resource exists. The DynamoDB
and S3 adapters land with the Terraform roots that create the table and bucket,
and TTL is theirs: an in-memory store does not expire.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Protocol

from .announcements import NormalizedAnnouncement, Provenance

__all__ = [
    "AnnouncementRecord",
    "AnnouncementStateStore",
    "FeedCheckpoint",
    "FeedStateStore",
    "FileFeedStateStore",
    "InMemoryAnnouncementStateStore",
    "InMemoryFeedStateStore",
    "InMemorySnapshotStore",
    "Observation",
    "SnapshotStore",
    "observe",
]


@dataclass(frozen=True, slots=True)
class FeedCheckpoint:
    """Durable state for one feed."""

    feed_name: str
    feed_url: str
    etag: str | None = None
    last_modified: str | None = None
    last_attempt_at: str | None = None
    last_success_at: str | None = None
    newest_publication_at: str | None = None
    consecutive_failures: int = 0
    last_error_class: str | None = None

    def with_attempt(self, attempted_at: datetime) -> FeedCheckpoint:
        return replace(self, last_attempt_at=attempted_at.isoformat())

    def with_failure(self, error_class: str) -> FeedCheckpoint:
        """Record a failure without disturbing the validators.

        Chapter 04 requires a failed feed to keep its prior checkpoint, so ETag
        and Last-Modified are deliberately untouched here.
        """

        return replace(
            self,
            consecutive_failures=self.consecutive_failures + 1,
            last_error_class=error_class,
        )

    def with_success(
        self,
        *,
        succeeded_at: datetime,
        etag: str | None,
        last_modified: str | None,
        newest_publication_at: datetime | None,
    ) -> FeedCheckpoint:
        newest = self.newest_publication_at
        if newest_publication_at is not None:
            observed = newest_publication_at.isoformat()
            newest = max(newest, observed) if newest else observed
        return replace(
            self,
            etag=etag if etag is not None else self.etag,
            last_modified=last_modified if last_modified is not None else self.last_modified,
            last_success_at=succeeded_at.isoformat(),
            newest_publication_at=newest,
            consecutive_failures=0,
            last_error_class=None,
        )


class FeedStateStore(Protocol):
    """Durable feed checkpoints."""

    def load(self, feed_name: str) -> FeedCheckpoint | None: ...

    def save(self, checkpoint: FeedCheckpoint) -> None: ...


class SnapshotStore(Protocol):
    """Bounded raw response snapshots retained for replay."""

    def put(self, feed_name: str, observed_at: datetime, body: bytes) -> str: ...


class InMemoryFeedStateStore:
    """Checkpoints held in memory, for tests and dry runs."""

    def __init__(self) -> None:
        self._records: dict[str, FeedCheckpoint] = {}

    def load(self, feed_name: str) -> FeedCheckpoint | None:
        return self._records.get(feed_name)

    def save(self, checkpoint: FeedCheckpoint) -> None:
        self._records[checkpoint.feed_name] = checkpoint


class FileFeedStateStore:
    """Checkpoints in one JSON file, for local replay across runs."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def _read(self) -> dict[str, dict[str, object]]:
        if not self._path.exists():
            return {}
        with self._path.open(encoding="utf-8") as handle:
            document: dict[str, dict[str, object]] = json.load(handle)
        return document

    def load(self, feed_name: str) -> FeedCheckpoint | None:
        record = self._read().get(feed_name)
        if record is None:
            return None
        return FeedCheckpoint(**record)  # type: ignore[arg-type]

    def save(self, checkpoint: FeedCheckpoint) -> None:
        document = self._read()
        document[checkpoint.feed_name] = asdict(checkpoint)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, sort_keys=True)
            handle.write("\n")


class InMemorySnapshotStore:
    """Snapshots held in memory, bounded the same way S3 objects would be."""

    def __init__(self, max_bytes: int = 5 * 1024 * 1024) -> None:
        self.max_bytes = max_bytes
        self.snapshots: dict[str, bytes] = {}

    def put(self, feed_name: str, observed_at: datetime, body: bytes) -> str:
        key = f"{feed_name}/{observed_at.isoformat()}"
        self.snapshots[key] = body[: self.max_bytes]
        return key


@dataclass(frozen=True, slots=True)
class AnnouncementRecord:
    """The announcement item chapter 02 keys as `ANNOUNCEMENT#<id>` / `STATE`.

    `revision_id` is the latest content revision; `revision_ids` is every
    revision ever seen, in first-seen order. Both are needed: identity uses the
    latest, while ADR-013's "append a revision only when absent" needs the set.
    """

    announcement_id: str
    canonical_url: str
    content_fingerprint: str
    revision_id: str
    revision_ids: tuple[str, ...]
    title: str
    summary: str
    first_observed_at: str
    last_observed_at: str
    published_at: str | None = None
    provenance: tuple[Provenance, ...] = ()
    emitted_candidate_ids: tuple[str, ...] = field(default=())
    release_ids: tuple[str, ...] = field(default=())

    def with_emission(
        self,
        candidate_ids: Iterable[str],
        release_id: str | None = None,
    ) -> AnnouncementRecord:
        """Record the candidates emitted for this announcement.

        ADR-013 keeps the release references used for emitted candidates, so a
        replay can tell which immutable release produced which candidate. Both
        sets are sorted and deduplicated, because a repeated invocation must not
        grow them.
        """

        merged = tuple(sorted(set(self.emitted_candidate_ids) | set(candidate_ids)))
        releases = self.release_ids
        if release_id is not None:
            releases = tuple(sorted(set(releases) | {release_id}))
        return replace(self, emitted_candidate_ids=merged, release_ids=releases)


@dataclass(frozen=True, slots=True)
class Observation:
    """What one sighting of an announcement changed.

    `provenance_only` is the flag chapter 04 and ADR-013 hang delivery off: a
    sighting that adds provenance or corrects a publication timestamp updates
    state and emits nothing.
    """

    record: AnnouncementRecord
    is_new_announcement: bool
    is_new_revision: bool
    is_update: bool
    provenance_only: bool


class AnnouncementStateStore(Protocol):
    """Durable announcement records, keyed by `announcement_id`."""

    def load(self, announcement: str) -> AnnouncementRecord | None: ...

    def save(self, record: AnnouncementRecord) -> None: ...


class InMemoryAnnouncementStateStore:
    """Announcement records held in memory, for tests and dry runs."""

    def __init__(self) -> None:
        self._records: dict[str, AnnouncementRecord] = {}

    def load(self, announcement: str) -> AnnouncementRecord | None:
        return self._records.get(announcement)

    def save(self, record: AnnouncementRecord) -> None:
        self._records[record.announcement_id] = record


def _earlier(left: str, right: str) -> str:
    return left if datetime.fromisoformat(left) <= datetime.fromisoformat(right) else right


def _later(left: str, right: str) -> str:
    return left if datetime.fromisoformat(left) >= datetime.fromisoformat(right) else right


def observe(
    store: AnnouncementStateStore,
    announcement: NormalizedAnnouncement,
) -> Observation:
    """Merge one sighting into announcement state and classify what changed.

    Chapter 04 and ADR-013 between them fix the rules: a normalized title or
    summary change makes a revision; publication-timestamp corrections and new
    provenance alone update state without delivery; a revision is appended only
    when absent; and `is_update` is true when state already contains an earlier
    content revision for the same announcement.

    Observation times are compared as timestamps rather than strings, because a
    replay can present sightings out of order and string order is only
    coincidentally correct across offsets.
    """

    key = announcement.announcement_id
    revision = announcement.revision_id
    observed = announcement.observed_at.isoformat()
    published = None if announcement.published_at is None else announcement.published_at.isoformat()
    existing = store.load(key)

    if existing is None:
        record = AnnouncementRecord(
            announcement_id=key,
            canonical_url=announcement.canonical_url,
            content_fingerprint=announcement.content_fingerprint,
            revision_id=revision,
            revision_ids=(revision,),
            title=announcement.title,
            summary=announcement.summary,
            first_observed_at=observed,
            last_observed_at=observed,
            published_at=published,
            provenance=tuple(sorted(announcement.provenance)),
        )
        store.save(record)
        return Observation(
            record=record,
            is_new_announcement=True,
            is_new_revision=True,
            is_update=False,
            provenance_only=False,
        )

    content_changed = revision != existing.revision_id
    is_new_revision = revision not in existing.revision_ids
    # "state already contains an earlier content revision" — any known revision
    # other than this one. A first sighting and a provenance-only repeat both
    # leave this false.
    is_update = any(other != revision for other in existing.revision_ids)

    record = replace(
        existing,
        content_fingerprint=announcement.content_fingerprint if content_changed else existing.content_fingerprint,
        revision_id=revision if content_changed else existing.revision_id,
        revision_ids=existing.revision_ids + (revision,) if is_new_revision else existing.revision_ids,
        title=announcement.title if content_changed else existing.title,
        summary=announcement.summary if content_changed else existing.summary,
        first_observed_at=_earlier(existing.first_observed_at, observed),
        last_observed_at=_later(existing.last_observed_at, observed),
        published_at=published if published is not None else existing.published_at,
        provenance=tuple(sorted(set(existing.provenance) | set(announcement.provenance))),
    )
    store.save(record)
    return Observation(
        record=record,
        is_new_announcement=False,
        is_new_revision=is_new_revision,
        is_update=is_update,
        provenance_only=not content_changed,
    )
