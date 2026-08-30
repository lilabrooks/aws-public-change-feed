"""Source-state and raw-snapshot ports.

Chapter 02's source-state table holds two item types, and both live here, the
way `outbox.py` holds the delivery table's candidate and delivery items.

The feed item carries conditional lease, attempt, pending-validator, success, and
failure state. The announcement item carries deterministic current content,
revision history, observation precedence, merged provenance, emission IDs, and
release references.

The ports are defined here with in-memory and file-backed implementations; the
production DynamoDB and S3 adapters live in `source_store`. Active feed state
has no cleanup TTL because removed-feed retirement is a separate policy.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from .announcements import NormalizedAnnouncement, Provenance
from .identity import announcement_id as derive_announcement_id
from .identity import content_fingerprint as derive_content_fingerprint
from .identity import revision_id as derive_revision_id

__all__ = [
    "AnnouncementRecord",
    "AnnouncementStateStore",
    "ConditionalStateConflict",
    "FeedCheckpoint",
    "FeedCompletion",
    "FeedStateStore",
    "FileFeedStateStore",
    "InMemoryAnnouncementStateStore",
    "InMemoryFeedStateStore",
    "InMemorySnapshotStore",
    "Observation",
    "ResponsePageMarker",
    "SnapshotStore",
    "observe",
    "record_emission",
]

_DIGEST = re.compile(r"[0-9a-f]{64}")
_FEED_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")
_MAX_STATE_RETRIES = 8


class ConditionalStateConflict(RuntimeError):
    """A bounded conditional-state retry could not make progress."""


def _required_string(name: str, value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    if "\0" in value:
        raise ValueError(f"{name} cannot contain a null character")
    return value


def _digest(name: str, value: object) -> str:
    text = _required_string(name, value)
    if _DIGEST.fullmatch(text) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return text


def _timestamp(name: str, value: str | None) -> datetime | None:
    if value is None:
        return None
    _required_string(name, value)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a UTC offset")
    return parsed


def _positive_integer(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _unix_timestamp(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer Unix timestamp")
    return value


@dataclass(frozen=True, slots=True)
class FeedCheckpoint:
    """Durable state for one feed."""

    feed_name: str
    feed_url: str
    etag: str | None = None
    last_modified: str | None = None
    first_attempt_at: str | None = None
    last_attempt_at: str | None = None
    last_success_at: str | None = None
    newest_publication_at: str | None = None
    consecutive_failures: int = 0
    last_error_class: str | None = None
    pending_etag: str | None = None
    pending_last_modified: str | None = None
    pending_newest_publication_at: str | None = None
    pending_run_id: str | None = None
    state_version: int = 1
    lease_owner: str | None = None
    lease_expires_at: int | None = None

    def __post_init__(self) -> None:
        if _FEED_NAME.fullmatch(_required_string("feed_name", self.feed_name)) is None:
            raise ValueError("feed_name must be a bounded lowercase identifier")
        _required_string("feed_url", self.feed_url)
        for name in (
            "first_attempt_at",
            "last_attempt_at",
            "last_success_at",
            "newest_publication_at",
            "pending_newest_publication_at",
        ):
            _timestamp(name, getattr(self, name))
        if isinstance(self.consecutive_failures, bool) or not isinstance(self.consecutive_failures, int):
            raise ValueError("consecutive_failures must be a non-negative integer")
        if self.consecutive_failures < 0:
            raise ValueError("consecutive_failures must be a non-negative integer")
        _positive_integer("state_version", self.state_version)
        if (self.lease_owner is None) != (self.lease_expires_at is None):
            raise ValueError("lease_owner and lease_expires_at must be present together")
        if self.lease_owner is not None:
            _required_string("lease_owner", self.lease_owner)
            _positive_integer("lease_expires_at", self.lease_expires_at)
        if self.pending_run_id is not None:
            _digest("pending_run_id", self.pending_run_id)
            if self.lease_owner is None:
                raise ValueError("pending response state requires an active lease")
        for name in ("etag", "last_modified", "last_error_class", "pending_etag", "pending_last_modified"):
            value = getattr(self, name)
            if value is not None:
                _required_string(name, value)

    def with_attempt(self, attempted_at: datetime) -> FeedCheckpoint:
        attempted = attempted_at.isoformat()
        return replace(
            self,
            first_attempt_at=self.first_attempt_at or attempted,
            last_attempt_at=attempted,
        )

    def with_failure(self, error_class: str) -> FeedCheckpoint:
        """Record a failure without disturbing the validators.

        Chapter 04 requires a failed feed to keep its prior checkpoint, so ETag
        and Last-Modified are deliberately untouched here.
        """

        return replace(
            self,
            consecutive_failures=self.consecutive_failures + 1,
            last_error_class=error_class,
            pending_etag=None,
            pending_last_modified=None,
            pending_newest_publication_at=None,
            pending_run_id=None,
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
            pending_etag=None,
            pending_last_modified=None,
            pending_newest_publication_at=None,
            pending_run_id=None,
        )

    def completed(self, succeeded_at: datetime) -> FeedCheckpoint:
        """Promote pending validators and release the exact held lease."""

        newest = self.newest_publication_at
        if self.pending_newest_publication_at is not None:
            newest = (
                self.pending_newest_publication_at
                if newest is None
                else _later(newest, self.pending_newest_publication_at)
            )
        return replace(
            self,
            etag=self.pending_etag if self.pending_etag is not None else self.etag,
            last_modified=(
                self.pending_last_modified if self.pending_last_modified is not None else self.last_modified
            ),
            last_success_at=succeeded_at.isoformat(),
            newest_publication_at=newest,
            consecutive_failures=0,
            last_error_class=None,
            pending_etag=None,
            pending_last_modified=None,
            pending_newest_publication_at=None,
            pending_run_id=None,
            lease_owner=None,
            lease_expires_at=None,
            state_version=self.state_version + 1,
        )


@dataclass(frozen=True, slots=True)
class FeedCompletion:
    """The exact leased feed version eligible for final checkpoint commit."""

    feed_name: str
    owner: str
    expected_state_version: int
    succeeded_at: datetime

    def __post_init__(self) -> None:
        if _FEED_NAME.fullmatch(_required_string("feed_name", self.feed_name)) is None:
            raise ValueError("feed_name must be a bounded lowercase identifier")
        _required_string("owner", self.owner)
        _positive_integer("expected_state_version", self.expected_state_version)
        if self.succeeded_at.tzinfo is None or self.succeeded_at.utcoffset() is None:
            raise ValueError("succeeded_at must include a UTC offset")


class FeedStateStore(Protocol):
    """Durable feed checkpoints."""

    def load(self, feed_name: str) -> FeedCheckpoint | None: ...

    def save(self, checkpoint: FeedCheckpoint) -> None: ...

    def claim(
        self,
        feed_name: str,
        feed_url: str,
        *,
        owner: str,
        attempted_at: datetime,
        lease_expires_at: int,
        now: int,
    ) -> FeedCheckpoint | None: ...

    def mark_fetched(
        self,
        feed_name: str,
        *,
        owner: str,
        expected_state_version: int,
        etag: str | None,
        last_modified: str | None,
        newest_publication_at: datetime | None,
        run_id: str,
    ) -> FeedCheckpoint | None: ...

    def complete(
        self,
        feed_name: str,
        *,
        owner: str,
        expected_state_version: int,
        succeeded_at: datetime,
    ) -> bool: ...

    def fail(
        self,
        feed_name: str,
        *,
        owner: str,
        expected_state_version: int,
        error_class: str,
    ) -> bool: ...

    def complete_many(self, completions: Iterable[FeedCompletion]) -> bool: ...


class SnapshotStore(Protocol):
    """Bounded raw response snapshots retained for replay."""

    def put(
        self,
        feed_name: str,
        observed_at: datetime,
        body: bytes,
        *,
        invocation_id: str = "local",
    ) -> str: ...


class InMemoryFeedStateStore:
    """Checkpoints held in memory, for tests and dry runs."""

    def __init__(self) -> None:
        self._records: dict[str, FeedCheckpoint] = {}

    def load(self, feed_name: str) -> FeedCheckpoint | None:
        return self._records.get(feed_name)

    def save(self, checkpoint: FeedCheckpoint) -> None:
        self._records[checkpoint.feed_name] = checkpoint

    def claim(
        self,
        feed_name: str,
        feed_url: str,
        *,
        owner: str,
        attempted_at: datetime,
        lease_expires_at: int,
        now: int,
    ) -> FeedCheckpoint | None:
        existing = self._records.get(feed_name)
        if existing is not None and existing.feed_url != feed_url:
            raise ValueError("stored feed_url does not match the claimed release feed")
        if existing is not None and existing.lease_expires_at is not None and existing.lease_expires_at > now:
            return None
        base = existing or FeedCheckpoint(feed_name=feed_name, feed_url=feed_url)
        attempted = attempted_at.isoformat()
        claimed = replace(
            base,
            first_attempt_at=base.first_attempt_at or attempted,
            last_attempt_at=attempted,
            state_version=base.state_version + (1 if existing is not None else 0),
            lease_owner=owner,
            lease_expires_at=lease_expires_at,
            pending_etag=None,
            pending_last_modified=None,
            pending_newest_publication_at=None,
            pending_run_id=None,
        )
        self._records[feed_name] = claimed
        return claimed

    def mark_fetched(
        self,
        feed_name: str,
        *,
        owner: str,
        expected_state_version: int,
        etag: str | None,
        last_modified: str | None,
        newest_publication_at: datetime | None,
        run_id: str,
    ) -> FeedCheckpoint | None:
        existing = self._records.get(feed_name)
        if existing is None or existing.lease_owner != owner or existing.state_version != expected_state_version:
            return None
        updated = replace(
            existing,
            pending_etag=etag,
            pending_last_modified=last_modified,
            pending_newest_publication_at=(
                None if newest_publication_at is None else newest_publication_at.isoformat()
            ),
            pending_run_id=run_id,
            state_version=existing.state_version + 1,
        )
        self._records[feed_name] = updated
        return updated

    def complete(
        self,
        feed_name: str,
        *,
        owner: str,
        expected_state_version: int,
        succeeded_at: datetime,
    ) -> bool:
        existing = self._records.get(feed_name)
        if existing is None or existing.lease_owner != owner or existing.state_version != expected_state_version:
            return False
        self._records[feed_name] = existing.completed(succeeded_at)
        return True

    def complete_many(self, completions: Iterable[FeedCompletion]) -> bool:
        requested = tuple(completions)
        if len({entry.feed_name for entry in requested}) != len(requested):
            raise ValueError("feed completions must name each feed once")
        records: list[tuple[FeedCompletion, FeedCheckpoint]] = []
        for entry in requested:
            existing = self._records.get(entry.feed_name)
            if (
                existing is None
                or existing.lease_owner != entry.owner
                or existing.state_version != entry.expected_state_version
                or existing.pending_run_id is None
            ):
                return False
            records.append((entry, existing))
        for entry, existing in records:
            self._records[entry.feed_name] = existing.completed(entry.succeeded_at)
        return True

    def fail(
        self,
        feed_name: str,
        *,
        owner: str,
        expected_state_version: int,
        error_class: str,
    ) -> bool:
        existing = self._records.get(feed_name)
        if existing is None or existing.lease_owner != owner or existing.state_version != expected_state_version:
            return False
        self._records[feed_name] = replace(
            existing.with_failure(error_class),
            lease_owner=None,
            lease_expires_at=None,
            state_version=existing.state_version + 1,
        )
        return True


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

    def put(
        self,
        feed_name: str,
        observed_at: datetime,
        body: bytes,
        *,
        invocation_id: str = "local",
    ) -> str:
        if _FEED_NAME.fullmatch(_required_string("feed_name", feed_name)) is None:
            raise ValueError("feed_name must be a bounded lowercase identifier")
        _required_string("invocation_id", invocation_id)
        if not isinstance(body, bytes):
            raise ValueError("snapshot body must be bytes")
        if not isinstance(observed_at, datetime) or observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("snapshot observed_at must include a UTC offset")
        if len(body) > self.max_bytes:
            raise ValueError("snapshot body exceeds max_bytes")
        digest = hashlib.sha256(body).hexdigest()
        instant = observed_at.astimezone(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        key = f"{feed_name}/{instant}/{invocation_id}/{digest}"
        self.snapshots[key] = body
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
    current_observed_at: str | None = None
    published_at: str | None = None
    provenance: tuple[Provenance, ...] = ()
    emitted_candidate_ids: tuple[str, ...] = field(default=())
    release_ids: tuple[str, ...] = field(default=())
    state_version: int = 1
    expires_at: int | None = None

    def __post_init__(self) -> None:
        _digest("announcement_id", self.announcement_id)
        _required_string("canonical_url", self.canonical_url)
        _digest("content_fingerprint", self.content_fingerprint)
        _digest("revision_id", self.revision_id)
        if not isinstance(self.revision_ids, tuple) or not self.revision_ids:
            raise ValueError("revision_ids must be a non-empty tuple")
        for value in self.revision_ids:
            _digest("revision_ids entry", value)
        if len(set(self.revision_ids)) != len(self.revision_ids):
            raise ValueError("revision_ids must not contain duplicates")
        if self.revision_id not in self.revision_ids:
            raise ValueError("revision_id must be present in revision_ids")
        _required_string("title", self.title)
        if not isinstance(self.summary, str):
            raise ValueError("summary must be a string")
        expected_announcement = derive_announcement_id(self.canonical_url)
        expected_fingerprint = derive_content_fingerprint(self.title, self.summary)
        expected_revision = derive_revision_id(expected_announcement, expected_fingerprint)
        if self.announcement_id != expected_announcement:
            raise ValueError("announcement_id does not derive from canonical_url")
        if self.content_fingerprint != expected_fingerprint:
            raise ValueError("content_fingerprint does not derive from title and summary")
        if self.revision_id != expected_revision:
            raise ValueError("revision_id does not derive from the current announcement content")
        first = _timestamp("first_observed_at", self.first_observed_at)
        last = _timestamp("last_observed_at", self.last_observed_at)
        current = _timestamp("current_observed_at", self.current_observed_at)
        _timestamp("published_at", self.published_at)
        assert first is not None and last is not None
        if first > last:
            raise ValueError("first_observed_at cannot follow last_observed_at")
        if current is not None and not first <= current <= last:
            raise ValueError("current_observed_at must be inside the observation window")
        if not isinstance(self.provenance, tuple):
            raise ValueError("provenance must be a tuple")
        for entry in self.provenance:
            if not isinstance(entry, Provenance):
                raise ValueError("provenance entries must be Provenance values")
            _required_string("provenance feed_name", entry.feed_name)
            _required_string("provenance item_url", entry.item_url)
        if tuple(sorted(set(self.provenance))) != self.provenance:
            raise ValueError("provenance must be sorted and deduplicated")
        for name, values in (
            ("emitted_candidate_ids", self.emitted_candidate_ids),
            ("release_ids", self.release_ids),
        ):
            if not isinstance(values, tuple):
                raise ValueError(f"{name} must be a tuple")
            for value in values:
                _digest(f"{name} entry", value)
            if tuple(sorted(set(values))) != values:
                raise ValueError(f"{name} must be sorted and deduplicated")
        _positive_integer("state_version", self.state_version)
        if self.expires_at is not None:
            _unix_timestamp("expires_at", self.expires_at)

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
        if merged == self.emitted_candidate_ids and releases == self.release_ids:
            return self
        return replace(
            self,
            emitted_candidate_ids=merged,
            release_ids=releases,
            state_version=self.state_version + 1,
        )


@dataclass(frozen=True, slots=True)
class ResponsePageMarker:
    """Durable completion proof for one deterministic response-run page."""

    run_id: str
    page_set_id: str
    feed_name: str
    page: int
    candidate_ids: tuple[str, ...]
    complete: bool = True
    expires_at: int | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        _digest("run_id", self.run_id)
        _digest("page_set_id", self.page_set_id)
        if _FEED_NAME.fullmatch(_required_string("feed_name", self.feed_name)) is None:
            raise ValueError("feed_name must be a bounded lowercase identifier")
        if isinstance(self.page, bool) or not isinstance(self.page, int) or self.page < 0:
            raise ValueError("page must be a non-negative integer")
        if not isinstance(self.complete, bool):
            raise ValueError("complete must be a boolean")
        if self.expires_at is not None:
            _unix_timestamp("expires_at", self.expires_at)
        if not isinstance(self.candidate_ids, tuple) or len(self.candidate_ids) > 25:
            raise ValueError("candidate_ids must be a tuple with at most 25 entries")
        for candidate in self.candidate_ids:
            _digest("candidate_ids entry", candidate)
        if tuple(sorted(set(self.candidate_ids))) != self.candidate_ids:
            raise ValueError("candidate_ids must be sorted and deduplicated")


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

    def put(self, record: AnnouncementRecord, *, expected_state_version: int | None) -> bool: ...

    def load_page(self, run_id: str, page_set_id: str, page: int) -> ResponsePageMarker | None: ...

    def put_page(self, marker: ResponsePageMarker) -> bool: ...


class InMemoryAnnouncementStateStore:
    """Announcement records held in memory, for tests and dry runs."""

    def __init__(self) -> None:
        self._records: dict[str, AnnouncementRecord] = {}
        self._pages: dict[tuple[str, str, int], ResponsePageMarker] = {}

    def load(self, announcement: str) -> AnnouncementRecord | None:
        return self._records.get(announcement)

    def save(self, record: AnnouncementRecord) -> None:
        self._records[record.announcement_id] = record

    def put(self, record: AnnouncementRecord, *, expected_state_version: int | None) -> bool:
        existing = self._records.get(record.announcement_id)
        if expected_state_version is None:
            if existing is not None or record.state_version != 1:
                return False
        elif (
            existing is None
            or existing.state_version != expected_state_version
            or record.state_version != expected_state_version + 1
        ):
            return False
        self._records[record.announcement_id] = record
        return True

    def load_page(self, run_id: str, page_set_id: str, page: int) -> ResponsePageMarker | None:
        return self._pages.get((run_id, page_set_id, page))

    def put_page(self, marker: ResponsePageMarker) -> bool:
        key = (marker.run_id, marker.page_set_id, marker.page)
        existing = self._pages.get(key)
        if existing is None:
            self._pages[key] = marker
            return True
        if existing != marker:
            return False
        if marker.expires_at is not None and (existing.expires_at is None or marker.expires_at > existing.expires_at):
            self._pages[key] = replace(existing, expires_at=marker.expires_at)
        return True


def _earlier(left: str, right: str) -> str:
    return left if datetime.fromisoformat(left) <= datetime.fromisoformat(right) else right


def _later(left: str, right: str) -> str:
    return left if datetime.fromisoformat(left) >= datetime.fromisoformat(right) else right


def observe(
    store: AnnouncementStateStore,
    announcement: NormalizedAnnouncement,
    *,
    retention_days: int,
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

    `retention_days` comes from the exact loaded release. Expiry follows the
    durable latest observation and never moves backward.
    """

    retention_days = _positive_integer("retention_days", retention_days)
    key = announcement.announcement_id
    revision = announcement.revision_id
    observed = announcement.observed_at.isoformat()
    published = None if announcement.published_at is None else announcement.published_at.isoformat()
    for _ in range(_MAX_STATE_RETRIES):
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
                current_observed_at=observed,
                published_at=published,
                provenance=tuple(sorted(set(announcement.provenance))),
                expires_at=int((announcement.observed_at + timedelta(days=retention_days)).timestamp()),
            )
            if not store.put(record, expected_state_version=None):
                continue
            return Observation(
                record=record,
                is_new_announcement=True,
                is_new_revision=True,
                is_update=False,
                provenance_only=False,
            )

        is_new_revision = revision not in existing.revision_ids
        is_update = any(other != revision for other in existing.revision_ids)
        current_observed = existing.current_observed_at or existing.last_observed_at
        observed_time = datetime.fromisoformat(observed)
        current_time = datetime.fromisoformat(current_observed)
        input_wins = observed_time > current_time or (observed_time == current_time and revision < existing.revision_id)
        content_changed = input_wins and revision != existing.revision_id
        revisions = existing.revision_ids + (revision,) if is_new_revision else existing.revision_ids
        last_observed_at = _later(existing.last_observed_at, observed)
        calculated_expiry = int((datetime.fromisoformat(last_observed_at) + timedelta(days=retention_days)).timestamp())
        expires_at = max(existing.expires_at or 0, calculated_expiry)
        merged = replace(
            existing,
            content_fingerprint=(announcement.content_fingerprint if input_wins else existing.content_fingerprint),
            revision_id=revision if input_wins else existing.revision_id,
            revision_ids=revisions,
            title=announcement.title if input_wins else existing.title,
            summary=announcement.summary if input_wins else existing.summary,
            current_observed_at=observed if input_wins else current_observed,
            first_observed_at=_earlier(existing.first_observed_at, observed),
            last_observed_at=last_observed_at,
            published_at=published if published is not None else existing.published_at,
            provenance=tuple(sorted(set(existing.provenance) | set(announcement.provenance))),
            expires_at=expires_at,
        )
        if merged == existing:
            return Observation(
                record=existing,
                is_new_announcement=False,
                is_new_revision=False,
                is_update=is_update,
                provenance_only=True,
            )
        record = replace(merged, state_version=existing.state_version + 1)
        if not store.put(record, expected_state_version=existing.state_version):
            continue
        return Observation(
            record=record,
            is_new_announcement=False,
            is_new_revision=is_new_revision,
            is_update=is_update,
            provenance_only=not content_changed,
        )
    raise ConditionalStateConflict("announcement observation lost its conditional write repeatedly")


def record_emission(
    store: AnnouncementStateStore,
    announcement_id: str,
    candidate_ids: Iterable[str],
    release_id: str,
) -> AnnouncementRecord:
    """Conditionally merge durable emission references without losing a writer."""

    candidates = tuple(candidate_ids)
    for _ in range(_MAX_STATE_RETRIES):
        existing = store.load(announcement_id)
        if existing is None:
            raise KeyError(f"announcement state is missing: {announcement_id}")
        updated = existing.with_emission(candidates, release_id)
        if updated == existing:
            return existing
        if store.put(updated, expected_state_version=existing.state_version):
            durable = store.load(announcement_id)
            if durable is None:
                continue
            if set(candidates).issubset(durable.emitted_candidate_ids) and release_id in durable.release_ids:
                return durable
    raise ConditionalStateConflict("announcement emission lost its conditional write repeatedly")
