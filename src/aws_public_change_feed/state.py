"""Feed state and raw-snapshot ports.

Chapter 02 lists the source-state fields: feed URL, ETag, Last-Modified, last
attempt, last success, newest observed publication time, consecutive failures,
last error class, lease owner, lease expiry, TTL. DynamoDB owns this in the
deployed system and S3 holds bounded raw snapshots.

The ports are defined here with in-memory and file-backed implementations, so
conditional requests, checkpoint rules, and failure accounting are testable
before any AWS resource exists. The DynamoDB and S3 adapters land with the
Terraform roots that create the table and bucket.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Protocol

__all__ = [
    "FeedCheckpoint",
    "FeedStateStore",
    "FileFeedStateStore",
    "InMemoryFeedStateStore",
    "InMemorySnapshotStore",
    "SnapshotStore",
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
