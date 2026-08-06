"""Feed acquisition across all configured feeds.

Chapter 04: fetch all enabled feeds before emission, group items by canonical
URL, and merge sorted provenance. One failed feed does not block successful
feeds from the same run; its validators remain unchanged, its failure count
rises, and feed-specific alarms apply.

Chapter 02 adds the checkpoint rule: a feed's new ETag or Last-Modified may be
saved only after downstream work is durable. That is enforced here by requiring
the caller to commit explicitly, so a crash between emission and commit replays
rather than skipping.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .announcements import NormalizedAnnouncement, coalesce, normalize_item
from .feedparse import FeedParseRejected, parse_feed
from .fetching import FeedFetcher, FeedFetching, FetchRejected
from .state import FeedCheckpoint, FeedStateStore, SnapshotStore
from .urls import FeedUrlRejected, validate_feed_url

__all__ = ["AcquisitionResult", "FeedOutcome", "FeedWatcher", "load_feeds"]


@dataclass(frozen=True, slots=True)
class FeedDefinition:
    name: str
    url: str
    source_type: str


@dataclass(frozen=True, slots=True)
class FeedOutcome:
    """What happened to one feed in one run."""

    feed_name: str
    status: str  # fetched | not_modified | failed
    error_class: str | None = None
    detail: str | None = None
    item_count: int = 0
    snapshot_key: str | None = None
    pending_checkpoint: FeedCheckpoint | None = None

    @property
    def succeeded(self) -> bool:
        return self.status in ("fetched", "not_modified")


@dataclass(frozen=True, slots=True)
class AcquisitionResult:
    """One run across every configured feed."""

    announcements: tuple[NormalizedAnnouncement, ...]
    outcomes: tuple[FeedOutcome, ...]

    @property
    def failed_feeds(self) -> tuple[str, ...]:
        return tuple(outcome.feed_name for outcome in self.outcomes if not outcome.succeeded)


def load_feeds(configuration: Mapping[str, Any]) -> tuple[FeedDefinition, ...]:
    return tuple(
        FeedDefinition(name=str(entry["name"]), url=str(entry["url"]), source_type=str(entry["source_type"]))
        for entry in configuration.get("feeds", ())
    )


@dataclass
class FeedWatcher:
    """Runs one acquisition pass over the configured feeds."""

    approved_hosts: tuple[str, ...]
    state: FeedStateStore
    fetcher: FeedFetching = field(default_factory=FeedFetcher)
    snapshots: SnapshotStore | None = None
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    def run(self, feeds: Sequence[FeedDefinition]) -> AcquisitionResult:
        observed_at = self.clock()
        collected: list[NormalizedAnnouncement] = []
        outcomes: list[FeedOutcome] = []

        for feed in feeds:
            outcome, announcements = self._run_one(feed, observed_at)
            outcomes.append(outcome)
            collected.extend(announcements)

        return AcquisitionResult(announcements=coalesce(collected), outcomes=tuple(outcomes))

    def _run_one(
        self, feed: FeedDefinition, observed_at: datetime
    ) -> tuple[FeedOutcome, Sequence[NormalizedAnnouncement]]:
        checkpoint = self.state.load(feed.name) or FeedCheckpoint(feed_name=feed.name, feed_url=feed.url)
        # The attempt is persisted immediately. Chapter 05 alarms on attempts
        # and time since last success, so an attempt that never reaches commit
        # must still be visible. Recording it advances no validator, which is
        # what chapter 02 actually forbids before durable downstream work.
        attempted = checkpoint.with_attempt(observed_at)
        self.state.save(attempted)

        try:
            target = validate_feed_url(feed.url, self.approved_hosts)
        except FeedUrlRejected as error:
            return self._failure(feed, attempted, "url", str(error)), ()

        try:
            response = self.fetcher.fetch(target, etag=checkpoint.etag, last_modified=checkpoint.last_modified)
        except FetchRejected as error:
            return self._failure(feed, attempted, error.reason_class, error.detail), ()

        if response.not_modified:
            # A validated 304 advances last success without creating work.
            advanced = attempted.with_success(
                succeeded_at=observed_at,
                etag=checkpoint.etag,
                last_modified=checkpoint.last_modified,
                newest_publication_at=None,
            )
            self.state.save(advanced)
            return FeedOutcome(feed_name=feed.name, status="not_modified", pending_checkpoint=None), ()

        try:
            items = parse_feed(response.body)
        except FeedParseRejected as error:
            return self._failure(feed, attempted, error.reason_class, error.detail), ()

        snapshot_key = None
        if self.snapshots is not None:
            snapshot_key = self.snapshots.put(feed.name, observed_at, response.body)

        announcements = [
            normalized
            for item in items
            if (normalized := normalize_item(item, feed.name, observed_at, self.approved_hosts)) is not None
        ]
        newest = max((entry.published_at for entry in announcements if entry.published_at), default=None)

        # Held, not saved. Chapter 02 forbids advancing the validators before
        # candidate and outbox work is durable, so the caller commits.
        pending = attempted.with_success(
            succeeded_at=observed_at,
            etag=response.etag,
            last_modified=response.last_modified,
            newest_publication_at=newest,
        )
        return (
            FeedOutcome(
                feed_name=feed.name,
                status="fetched",
                item_count=len(announcements),
                snapshot_key=snapshot_key,
                pending_checkpoint=pending,
            ),
            announcements,
        )

    def _failure(self, feed: FeedDefinition, attempted: FeedCheckpoint, error_class: str, detail: str) -> FeedOutcome:
        failed = attempted.with_failure(error_class)
        self.state.save(failed)
        return FeedOutcome(feed_name=feed.name, status="failed", error_class=error_class, detail=detail)

    def commit(self, result: AcquisitionResult) -> tuple[str, ...]:
        """Advance validators for feeds whose downstream work is durable.

        Call only after candidate and outbox records are written. Returns the
        feed names advanced.
        """

        advanced: list[str] = []
        for outcome in result.outcomes:
            if outcome.pending_checkpoint is not None:
                self.state.save(outcome.pending_checkpoint)
                advanced.append(outcome.feed_name)
        return tuple(advanced)
