"""Durable feed-watcher orchestration over the accepted runtime ports."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urlsplit

from .acquisition import FeedDefinition, load_feeds
from .announcements import NormalizedAnnouncement, coalesce, normalize_item
from .candidates import build_candidates
from .dispatch import validate_alert_candidate
from .feedparse import FeedParseRejected, parse_feed
from .fetching import FeedFetching, FetchRejected
from .identity import digest_parts
from .loading import LoadedRelease
from .matching import Announcement, load_risk_rules, load_services, match_announcement, rule_exclusion_count
from .outbox import OutboxStore, emit, verify_durable
from .profiles import route_audiences
from .semantics import serialized_size, validate_candidate_against_release
from .state import (
    AnnouncementStateStore,
    ConditionalStateConflict,
    FeedCheckpoint,
    FeedCompletion,
    FeedStateStore,
    ResponsePageMarker,
    SnapshotStore,
    observe,
    record_emission,
)
from .urls import FeedUrlRejected, validate_feed_url

__all__ = [
    "FeedRunOutcome",
    "NullWatcherMetrics",
    "WatcherMetrics",
    "WatcherOrchestrator",
    "WatcherReleaseMismatch",
    "WatcherResult",
    "response_page_set_id",
    "response_run_id",
]

_MINIMUM_REMAINING_MILLISECONDS = 60_000
_PAGE_SIZE = 25
_MAX_RETENTION_DAYS = 3650


class WatcherReleaseMismatch(ValueError):
    """The loaded release cannot run under the deployment-owned fetch policy."""


def _retention_days(release: LoadedRelease, name: str) -> int:
    retention = release.config.get("state_retention")
    if not isinstance(retention, Mapping):
        raise WatcherReleaseMismatch("release state_retention is malformed")
    value = retention.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_RETENTION_DAYS:
        raise WatcherReleaseMismatch(f"release {name} is malformed")
    return value


class WatcherMetrics(Protocol):
    def release_verification_failure(self) -> None: ...

    def watcher_fault(self) -> None: ...

    def feed_attempt(self, feed_name: str) -> None: ...

    def feed_success(self, feed_name: str, *, not_modified: bool) -> None: ...

    def feed_failure(self, feed_name: str, error_class: str) -> None: ...

    def feed_contention(self, feed_name: str) -> None: ...

    def response_observed(self, feed_name: str, *, byte_count: int, item_count: int) -> None: ...

    def raw_snapshot_failure(self, feed_name: str) -> None: ...

    def announcements_observed(self, *, normalized: int, coalesced: int) -> None: ...

    def announcement_state(self, *, new_revision: bool, provenance_only: bool) -> None: ...

    def no_match(self) -> None: ...

    def rule_exclusions(self, count: int) -> None: ...

    def candidate(self, *, service: str, risk: str, priority: str, route: str) -> None: ...

    def candidate_validation_failure(self) -> None: ...

    def candidate_size_failure(self) -> None: ...

    def feed_staleness(self, feed_name: str, seconds: int) -> None: ...

    def newest_publication_age(self, feed_name: str, seconds: int) -> None: ...

    def max_feed_staleness(self, seconds: int) -> None: ...

    def incomplete_run(self) -> None: ...


class NullWatcherMetrics:
    def release_verification_failure(self) -> None:
        pass

    def watcher_fault(self) -> None:
        pass

    def feed_attempt(self, feed_name: str) -> None:
        del feed_name

    def feed_success(self, feed_name: str, *, not_modified: bool) -> None:
        del feed_name, not_modified

    def feed_failure(self, feed_name: str, error_class: str) -> None:
        del feed_name, error_class

    def feed_contention(self, feed_name: str) -> None:
        del feed_name

    def response_observed(self, feed_name: str, *, byte_count: int, item_count: int) -> None:
        del feed_name, byte_count, item_count

    def raw_snapshot_failure(self, feed_name: str) -> None:
        del feed_name

    def announcements_observed(self, *, normalized: int, coalesced: int) -> None:
        del normalized, coalesced

    def announcement_state(self, *, new_revision: bool, provenance_only: bool) -> None:
        del new_revision, provenance_only

    def no_match(self) -> None:
        pass

    def rule_exclusions(self, count: int) -> None:
        del count

    def candidate(self, *, service: str, risk: str, priority: str, route: str) -> None:
        del service, risk, priority, route

    def candidate_validation_failure(self) -> None:
        pass

    def candidate_size_failure(self) -> None:
        pass

    def feed_staleness(self, feed_name: str, seconds: int) -> None:
        del feed_name, seconds

    def newest_publication_age(self, feed_name: str, seconds: int) -> None:
        del feed_name, seconds

    def max_feed_staleness(self, seconds: int) -> None:
        del seconds

    def incomplete_run(self) -> None:
        pass


@dataclass(frozen=True, slots=True)
class FeedRunOutcome:
    feed_name: str
    status: str
    error_class: str | None = None
    snapshot_key: str | None = None
    item_count: int = 0
    candidate_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WatcherResult:
    outcomes: tuple[FeedRunOutcome, ...]
    candidate_ids: tuple[str, ...]
    advanced_feeds: tuple[str, ...]
    created_delivery_ids: tuple[str, ...] = ()
    repaired_delivery_ids: tuple[str, ...] = ()
    incomplete: bool = False


@dataclass(frozen=True, slots=True)
class _FetchedFeed:
    feed: FeedDefinition
    claimed: FeedCheckpoint
    pending: FeedCheckpoint | None
    status: str
    announcements: tuple[NormalizedAnnouncement, ...] = ()
    snapshot_key: str | None = None
    run_id: str | None = None
    error_class: str | None = None


def response_run_id(
    feed_name: str,
    body_sha256: str,
    release_id: str,
    application_version: str,
) -> str:
    """Derive the frozen null-framed response-run identity."""

    return digest_parts(
        "feed-response-run:v1",
        feed_name,
        body_sha256,
        release_id,
        application_version,
    )


def response_page_set_id(run_id: str, candidate_ids: Sequence[str]) -> str:
    """Bind one immutable page set to its response run and sorted candidates."""

    return digest_parts("feed-response-pages:v1", run_id, *sorted(candidate_ids))


@dataclass(slots=True)
class WatcherOrchestrator:
    feed_state: FeedStateStore
    announcement_state: AnnouncementStateStore
    snapshots: SnapshotStore
    outbox: OutboxStore
    fetcher: FeedFetching
    approved_hosts: tuple[str, ...]
    application_version: str
    max_items: int
    max_item_characters: int
    max_concurrent_fetches: int
    lease_seconds: int = 360
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    def __post_init__(self) -> None:
        for name in ("max_items", "max_item_characters", "max_concurrent_fetches", "lease_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if not self.approved_hosts:
            raise ValueError("approved_hosts must not be empty")

    def _fail_claim(
        self,
        claimed: FeedCheckpoint,
        error_class: str,
        metrics: WatcherMetrics,
    ) -> _FetchedFeed:
        if not self.feed_state.fail(
            claimed.feed_name,
            owner=claimed.lease_owner or "",
            expected_state_version=claimed.state_version,
            error_class=error_class,
        ):
            raise ConditionalStateConflict("feed failure lost its exact lease condition")
        metrics.feed_failure(claimed.feed_name, error_class)
        return _FetchedFeed(
            feed=FeedDefinition(claimed.feed_name, claimed.feed_url, "public_feed"),
            claimed=claimed,
            pending=None,
            status="failed",
            error_class=error_class,
        )

    def _fetch_one(
        self,
        feed: FeedDefinition,
        claimed: FeedCheckpoint,
        release: LoadedRelease,
        invocation_id: str,
        observed_at: datetime,
        metrics: WatcherMetrics,
    ) -> _FetchedFeed:
        try:
            target = validate_feed_url(feed.url, self.approved_hosts)
        except FeedUrlRejected:
            return self._fail_claim(claimed, "url", metrics)
        try:
            response = self.fetcher.fetch(
                target,
                etag=claimed.etag,
                last_modified=claimed.last_modified,
            )
        except FetchRejected as error:
            return self._fail_claim(claimed, error.reason_class, metrics)

        if response.not_modified:
            if not self.feed_state.complete(
                feed.name,
                owner=claimed.lease_owner or "",
                expected_state_version=claimed.state_version,
                succeeded_at=observed_at,
            ):
                raise ConditionalStateConflict("304 completion lost its exact lease condition")
            metrics.feed_success(feed.name, not_modified=True)
            return _FetchedFeed(feed=feed, claimed=claimed, pending=None, status="not_modified")

        try:
            snapshot_key = self.snapshots.put(
                feed.name,
                observed_at,
                response.body,
                invocation_id=invocation_id,
            )
        except Exception:  # noqa: BLE001 - adapter failures share one bounded feed outcome
            metrics.raw_snapshot_failure(feed.name)
            return self._fail_claim(claimed, "raw_snapshot", metrics)

        try:
            items = parse_feed(
                response.body,
                max_items=self.max_items,
                max_item_characters=self.max_item_characters,
            )
        except FeedParseRejected as error:
            return self._fail_claim(claimed, error.reason_class, metrics)

        announcements = tuple(
            normalized
            for item in items
            if (normalized := normalize_item(item, feed.name, observed_at, self.approved_hosts)) is not None
        )
        newest = max((entry.published_at for entry in announcements if entry.published_at), default=None)
        body_sha256 = hashlib.sha256(response.body).hexdigest()
        run_id = response_run_id(feed.name, body_sha256, release.release_id, self.application_version)
        pending = self.feed_state.mark_fetched(
            feed.name,
            owner=claimed.lease_owner or "",
            expected_state_version=claimed.state_version,
            etag=response.etag,
            last_modified=response.last_modified,
            newest_publication_at=newest,
            run_id=run_id,
        )
        if pending is None:
            raise ConditionalStateConflict("fetched response lost its exact lease condition")
        metrics.response_observed(feed.name, byte_count=len(response.body), item_count=len(announcements))
        return _FetchedFeed(
            feed=feed,
            claimed=claimed,
            pending=pending,
            status="fetched",
            announcements=announcements,
            snapshot_key=snapshot_key,
            run_id=run_id,
        )

    def _emit_staleness(
        self,
        feeds: Sequence[FeedDefinition],
        now: datetime,
        metrics: WatcherMetrics,
    ) -> None:
        values = []
        for feed in feeds:
            checkpoint = self.feed_state.load(feed.name)
            if checkpoint is None or checkpoint.first_attempt_at is None:
                continue
            basis = checkpoint.last_success_at or checkpoint.first_attempt_at
            seconds = max(0, int((now - datetime.fromisoformat(basis)).total_seconds()))
            values.append(seconds)
            metrics.feed_staleness(feed.name, seconds)
            if checkpoint.newest_publication_at is not None:
                publication_age = max(
                    0,
                    int((now - datetime.fromisoformat(checkpoint.newest_publication_at)).total_seconds()),
                )
                metrics.newest_publication_age(feed.name, publication_age)
        metrics.max_feed_staleness(max(values, default=0))

    def _validate_release_fetch_policy(self, release: LoadedRelease) -> None:
        configured_hosts = set()
        for feed in load_feeds(release.config):
            try:
                target = validate_feed_url(feed.url, self.approved_hosts)
            except FeedUrlRejected as error:
                raise WatcherReleaseMismatch("release feed is outside the deployment fetch policy") from error
            configured_hosts.add((urlsplit(feed.url).hostname or "").casefold())
            if target.hostname not in self.approved_hosts:
                raise WatcherReleaseMismatch("release feed is outside the deployment fetch policy")
        if configured_hosts != set(self.approved_hosts):
            raise WatcherReleaseMismatch("release feed hosts differ from the deployment fetch policy")

    def run(
        self,
        release: LoadedRelease,
        *,
        invocation_id: str,
        remaining_time_ms: Callable[[], int],
        metrics: WatcherMetrics | None = None,
    ) -> WatcherResult:
        metrics = metrics or NullWatcherMetrics()
        self._validate_release_fetch_policy(release)
        announcement_retention_days = _retention_days(release, "announcement_state_ttl_days")
        page_retention_days = _retention_days(release, "feed_state_ttl_days")
        observed_at = self.clock()
        feeds = load_feeds(release.config)
        claimed: list[tuple[FeedDefinition, FeedCheckpoint]] = []
        outcomes: dict[str, FeedRunOutcome] = {}
        incomplete = False

        for feed in feeds:
            if remaining_time_ms() < _MINIMUM_REMAINING_MILLISECONDS:
                incomplete = True
                metrics.incomplete_run()
                break
            metrics.feed_attempt(feed.name)
            lease_expires_at = int(observed_at.timestamp()) + self.lease_seconds
            checkpoint = self.feed_state.claim(
                feed.name,
                feed.url,
                owner=invocation_id,
                attempted_at=observed_at,
                lease_expires_at=lease_expires_at,
                now=int(observed_at.timestamp()),
            )
            if checkpoint is None:
                metrics.feed_contention(feed.name)
                outcomes[feed.name] = FeedRunOutcome(feed_name=feed.name, status="contended")
                continue
            claimed.append((feed, checkpoint))

        with ThreadPoolExecutor(max_workers=self.max_concurrent_fetches) as executor:
            futures = [
                executor.submit(
                    self._fetch_one,
                    feed,
                    checkpoint,
                    release,
                    invocation_id,
                    observed_at,
                    metrics,
                )
                for feed, checkpoint in claimed
            ]
            fetched = [future.result() for future in futures]

        successful = [entry for entry in fetched if entry.status == "fetched"]
        for entry in fetched:
            outcomes[entry.feed.name] = FeedRunOutcome(
                feed_name=entry.feed.name,
                status=entry.status,
                error_class=entry.error_class,
                snapshot_key=entry.snapshot_key,
                item_count=len(entry.announcements),
            )

        normalized = [announcement for entry in successful for announcement in entry.announcements]
        announcements = coalesce(normalized)
        metrics.announcements_observed(normalized=len(normalized), coalesced=len(announcements))
        services = load_services(release.config)
        rules = load_risk_rules(release.config)
        built: list[Mapping[str, Any]] = []
        candidate_ids_by_announcement: dict[str, list[str]] = {}
        candidate_ids_by_feed: dict[str, set[str]] = {entry.feed.name: set() for entry in successful}
        created_at = self.clock()

        for announcement in announcements:
            observation = observe(
                self.announcement_state,
                announcement,
                retention_days=announcement_retention_days,
            )
            metrics.announcement_state(
                new_revision=observation.is_new_revision,
                provenance_only=observation.provenance_only,
            )
            matches = match_announcement(
                Announcement(title=announcement.title, summary=announcement.summary),
                services,
                rules,
            )
            metrics.rule_exclusions(
                rule_exclusion_count(
                    Announcement(title=announcement.title, summary=announcement.summary),
                    rules,
                )
            )
            if not matches:
                metrics.no_match()
            for match in matches:
                candidates = build_candidates(
                    announcement=announcement,
                    match=match,
                    audiences=route_audiences(release.config, release.inventory, match.service_id),
                    configuration=release.config,
                    release=release.reference,
                    created_at=created_at,
                    is_update=observation.is_update,
                )
                for candidate in candidates:
                    if serialized_size(candidate) > release.config["message_policy"]["max_candidate_bytes"]:
                        metrics.candidate_size_failure()
                        raise ValueError("produced candidate exceeds the release byte limit")
                    try:
                        validate_alert_candidate(candidate)
                        validate_candidate_against_release(release.config, release.inventory, candidate)
                    except ValueError:
                        metrics.candidate_validation_failure()
                        raise
                    built.append(candidate)
                    candidate_id = str(candidate["candidate_id"])
                    candidate_ids_by_announcement.setdefault(announcement.announcement_id, []).append(candidate_id)
                    for provenance in announcement.provenance:
                        if provenance.feed_name in candidate_ids_by_feed:
                            candidate_ids_by_feed[provenance.feed_name].add(candidate_id)
                    metrics.candidate(
                        service=match.service_id,
                        risk=match.risk_type,
                        priority=match.priority,
                        route=str(candidate["route_id"]),
                    )

        emission = emit(
            self.outbox,
            built,
            inventory=release.inventory,
            message_policy=release.config["message_policy"],
            created_at=created_at,
        )
        if not verify_durable(self.outbox, emission.candidate_ids):
            raise RuntimeError("outbox durability read-back failed")

        for announcement_id, candidate_ids in candidate_ids_by_announcement.items():
            durable = record_emission(
                self.announcement_state,
                announcement_id,
                candidate_ids,
                release.release_id,
            )
            if not set(candidate_ids).issubset(durable.emitted_candidate_ids):
                raise RuntimeError("announcement emission-reference read-back failed")
            if release.release_id not in durable.release_ids:
                raise RuntimeError("announcement release-reference read-back failed")

        for entry in successful:
            assert entry.run_id is not None
            response_candidate_ids = tuple(sorted(candidate_ids_by_feed[entry.feed.name]))
            page_set_id = response_page_set_id(entry.run_id, response_candidate_ids)
            pages: list[tuple[str, ...]] = [
                response_candidate_ids[index : index + _PAGE_SIZE]
                for index in range(0, len(response_candidate_ids), _PAGE_SIZE)
            ]
            if not pages:
                pages = [()]
            page_expires_at = int((created_at + timedelta(days=page_retention_days)).timestamp())
            for page_number, page_ids in enumerate(pages):
                marker = ResponsePageMarker(
                    run_id=entry.run_id,
                    page_set_id=page_set_id,
                    feed_name=entry.feed.name,
                    page=page_number,
                    candidate_ids=page_ids,
                    expires_at=page_expires_at,
                )
                if not self.announcement_state.put_page(marker):
                    raise RuntimeError("response-page marker conflicts with durable state")
                durable_page = self.announcement_state.load_page(entry.run_id, page_set_id, page_number)
                if (
                    durable_page is None
                    or durable_page != marker
                    or durable_page.expires_at is None
                    or durable_page.expires_at < page_expires_at
                ):
                    raise RuntimeError("response-page marker read-back failed")
            if not verify_durable(self.outbox, response_candidate_ids):
                raise RuntimeError("final response durability read-back failed")
            outcomes[entry.feed.name] = FeedRunOutcome(
                feed_name=entry.feed.name,
                status="fetched",
                snapshot_key=entry.snapshot_key,
                item_count=len(entry.announcements),
                candidate_ids=response_candidate_ids,
            )

        completions = tuple(
            FeedCompletion(
                feed_name=entry.feed.name,
                owner=entry.pending.lease_owner or "",
                expected_state_version=entry.pending.state_version,
                succeeded_at=created_at,
            )
            for entry in successful
            if entry.pending is not None
        )
        if not self.feed_state.complete_many(completions):
            raise ConditionalStateConflict("fetched checkpoint transaction lost a condition")
        for entry in successful:
            metrics.feed_success(entry.feed.name, not_modified=False)

        self._emit_staleness(feeds, self.clock(), metrics)
        ordered_outcomes = tuple(outcomes[feed.name] for feed in feeds if feed.name in outcomes)
        return WatcherResult(
            outcomes=ordered_outcomes,
            candidate_ids=tuple(sorted(set(emission.candidate_ids))),
            advanced_feeds=tuple(entry.feed.name for entry in successful),
            created_delivery_ids=tuple(sorted(emission.created_deliveries)),
            repaired_delivery_ids=tuple(sorted(emission.repaired_deliveries)),
            incomplete=incomplete,
        )
