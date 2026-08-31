"""AWS composition root, metrics, and scheduled handler for the feed watcher."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Lock
from typing import Any, Protocol

from .fetching import FeedFetcher
from .identity import application_artifact_id
from .loading import IncompatibleRelease, ReleaseIntegrityError, load_active_release
from .outbox import DynamoDBDeliveryStore
from .releases import ObjectStore, S3ObjectStore
from .source_store import DynamoDBAnnouncementStateStore, DynamoDBFeedStateStore, S3SnapshotStore
from .state import ConditionalStateConflict
from .watcher import WatcherMetrics, WatcherOrchestrator, WatcherReleaseMismatch, WatcherResult

__all__ = ["EmbeddedWatcherMetrics", "WatcherRuntime", "lambda_handler"]

_SAFE_METRIC_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}")
_INVOCATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,127}")
_SAFE_NAMESPACE_CHARACTERS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./#:-")
_ERROR_CLASSES = frozenset(
    {
        "content_encoding",
        "content_type",
        "dns",
        "network",
        "parser",
        "raw_snapshot",
        "redirect",
        "response_limit",
        "status",
        "timeout",
        "tls",
        "url",
    }
)


def _metric_document(
    namespace: str,
    timestamp_ms: int,
    dimensions: list[list[str]],
    metrics: list[tuple[str, str]],
    values: Mapping[str, object],
) -> str:
    document: dict[str, object] = {
        "_aws": {
            "Timestamp": timestamp_ms,
            "CloudWatchMetrics": [
                {
                    "Namespace": namespace,
                    "Dimensions": dimensions,
                    "Metrics": [{"Name": name, "Unit": unit} for name, unit in metrics],
                }
            ],
        }
    }
    document.update(values)
    return json.dumps(document, separators=(",", ":"), sort_keys=True)


def _safe_dimension(name: str, value: str) -> str:
    if not isinstance(value, str) or _SAFE_METRIC_VALUE.fullmatch(value) is None:
        raise ValueError(f"{name} is not a bounded metric dimension")
    return value


@dataclass(slots=True)
class EmbeddedWatcherMetrics:
    """Thread-safe bounded watcher facts emitted as CloudWatch EMF."""

    namespace: str
    function_name: str
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    emit: Callable[[str], None] = print
    _heartbeat: bool = field(default=False, init=False)
    _function: dict[str, int] = field(default_factory=dict, init=False)
    _dimensionless: dict[str, int] = field(default_factory=dict, init=False)
    _feeds: dict[str, dict[str, int]] = field(default_factory=dict, init=False)
    _candidates: dict[tuple[str, str, str, str], int] = field(default_factory=dict, init=False)
    _lock: Lock = field(default_factory=Lock, init=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.namespace, str)
            or not 1 <= len(self.namespace) <= 255
            or self.namespace.startswith("AWS/")
            or any(character not in _SAFE_NAMESPACE_CHARACTERS for character in self.namespace)
        ):
            raise ValueError("metrics namespace must be 1-255 safe characters and cannot start with AWS/")
        _safe_dimension("function_name", self.function_name)

    def _increment(self, name: str, value: int = 1) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("metric increments must be non-negative integers")
        with self._lock:
            self._dimensionless[name] = self._dimensionless.get(name, 0) + value

    def _function_increment(self, name: str, value: int = 1) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("metric increments must be non-negative integers")
        with self._lock:
            self._function[name] = self._function.get(name, 0) + value

    def _feed_increment(self, feed_name: str, name: str, value: int = 1) -> None:
        feed_name = _safe_dimension("feed_name", feed_name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("metric values must be non-negative integers")
        with self._lock:
            metrics = self._feeds.setdefault(feed_name, {})
            metrics[name] = metrics.get(name, 0) + value

    def heartbeat(self) -> None:
        self._heartbeat = True

    def release_verification_failure(self) -> None:
        self._increment("ReleaseVerificationFailures")

    def watcher_fault(self) -> None:
        with self._lock:
            self._function.pop("IncompleteRuns", None)
            self._function["WatcherFaults"] = 1

    def feed_attempt(self, feed_name: str) -> None:
        self._feed_increment(feed_name, "FeedAttempts")

    def feed_success(self, feed_name: str, *, not_modified: bool) -> None:
        self._feed_increment(feed_name, "FeedSuccesses")
        if not_modified:
            self._feed_increment(feed_name, "FeedNotModified")

    def feed_failure(self, feed_name: str, error_class: str) -> None:
        if error_class not in _ERROR_CLASSES:
            raise ValueError(f"error class is not allowlisted: {error_class}")
        self._feed_increment(feed_name, "FeedFailures")
        self._feed_increment(feed_name, f"Rejected_{error_class}")

    def feed_contention(self, feed_name: str) -> None:
        self._feed_increment(feed_name, "FeedLeaseContention")

    def response_observed(self, feed_name: str, *, byte_count: int, item_count: int) -> None:
        self._feed_increment(feed_name, "ResponseBytes", byte_count)
        self._feed_increment(feed_name, "FeedItems", item_count)

    def raw_snapshot_failure(self, feed_name: str) -> None:
        self._feed_increment(feed_name, "RawSnapshotFailures")
        self._increment("RawSnapshotFailures")

    def announcements_observed(self, *, normalized: int, coalesced: int) -> None:
        self._increment("NormalizedAnnouncements", normalized)
        self._increment("CoalescedAnnouncements", coalesced)

    def announcement_state(self, *, new_revision: bool, provenance_only: bool) -> None:
        if new_revision:
            self._increment("NewRevisions")
        if provenance_only:
            self._increment("ProvenanceOnlyUpdates")

    def no_match(self) -> None:
        self._increment("NoMatch")

    def rule_exclusions(self, count: int) -> None:
        self._increment("RuleExclusions", count)

    def candidate(self, *, service: str, risk: str, priority: str, route: str) -> None:
        key = (
            _safe_dimension("service", service),
            _safe_dimension("risk", risk),
            _safe_dimension("priority", priority),
            _safe_dimension("route", route),
        )
        with self._lock:
            self._candidates[key] = self._candidates.get(key, 0) + 1

    def candidate_validation_failure(self) -> None:
        self._increment("CandidateValidationFailures")

    def candidate_size_failure(self) -> None:
        self._increment("CandidateSizeFailures")

    def feed_staleness(self, feed_name: str, seconds: int) -> None:
        feed_name = _safe_dimension("feed_name", feed_name)
        if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds < 0:
            raise ValueError("feed staleness must be non-negative seconds")
        with self._lock:
            self._feeds.setdefault(feed_name, {})["FeedStalenessSeconds"] = seconds

    def newest_publication_age(self, feed_name: str, seconds: int) -> None:
        feed_name = _safe_dimension("feed_name", feed_name)
        if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds < 0:
            raise ValueError("publication age must be non-negative seconds")
        with self._lock:
            self._feeds.setdefault(feed_name, {})["NewestPublicationAgeSeconds"] = seconds

    def max_feed_staleness(self, seconds: int) -> None:
        if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds < 0:
            raise ValueError("maximum feed staleness must be non-negative seconds")
        with self._lock:
            self._dimensionless["MaxFeedStalenessSeconds"] = seconds

    def incomplete_run(self) -> None:
        with self._lock:
            self._function.pop("WatcherFaults", None)
            self._function["IncompleteRuns"] = 1

    def flush(self) -> None:
        timestamp_ms = int(self.clock().timestamp() * 1000)
        function = dict(self._function)
        if self._heartbeat:
            function["Heartbeat"] = 1
        if function:
            self.emit(
                _metric_document(
                    self.namespace,
                    timestamp_ms,
                    [["Function"]],
                    [(name, "Count") for name in sorted(function)],
                    {"Function": self.function_name, **function},
                )
            )

        dimensionless = dict(self._dimensionless)
        dimensionless.setdefault("ReleaseVerificationFailures", 0)
        dimensionless.setdefault("RawSnapshotFailures", 0)
        units = {name: "Seconds" if name == "MaxFeedStalenessSeconds" else "Count" for name in dimensionless}
        self.emit(
            _metric_document(
                self.namespace,
                timestamp_ms,
                [[]],
                [(name, units[name]) for name in sorted(dimensionless)],
                dimensionless,
            )
        )

        for feed_name, values in sorted(self._feeds.items()):
            definitions = [
                (
                    name,
                    "Seconds"
                    if name in {"FeedStalenessSeconds", "NewestPublicationAgeSeconds"}
                    else "Bytes"
                    if name == "ResponseBytes"
                    else "Count",
                )
                for name in sorted(values)
            ]
            self.emit(
                _metric_document(
                    self.namespace,
                    timestamp_ms,
                    [["FeedName"]],
                    definitions,
                    {"FeedName": feed_name, **values},
                )
            )

        for (service, risk, priority, route), count in sorted(self._candidates.items()):
            self.emit(
                _metric_document(
                    self.namespace,
                    timestamp_ms,
                    [["Service", "Risk", "Priority", "Route"]],
                    [("Candidates", "Count")],
                    {
                        "Service": service,
                        "Risk": risk,
                        "Priority": priority,
                        "Route": route,
                        "Candidates": count,
                    },
                )
            )


@dataclass(frozen=True, slots=True)
class WatcherRuntime:
    release_store: ObjectStore
    pointer_key: str
    application_version: str
    orchestrator: WatcherOrchestrator

    def run(
        self,
        *,
        invocation_id: str,
        remaining_time_ms: Callable[[], int],
        metrics: WatcherMetrics,
    ) -> WatcherResult:
        try:
            release = load_active_release(
                self.release_store,
                pointer_key=self.pointer_key,
                application_version=self.application_version,
            )
        except (IncompatibleRelease, ReleaseIntegrityError):
            metrics.release_verification_failure()
            raise
        try:
            return self.orchestrator.run(
                release,
                invocation_id=invocation_id,
                remaining_time_ms=remaining_time_ms,
                metrics=metrics,
            )
        except WatcherReleaseMismatch:
            metrics.release_verification_failure()
            raise


class _WatcherRunner(Protocol):
    def run(
        self,
        *,
        invocation_id: str,
        remaining_time_ms: Callable[[], int],
        metrics: WatcherMetrics,
    ) -> WatcherResult: ...


@dataclass(frozen=True, slots=True)
class _RuntimeConfiguration:
    config_bucket: str
    pointer_key: str
    source_state_table: str
    delivery_table: str
    delivery_index: str
    raw_snapshot_prefix: str
    application_version: str
    approved_hosts: tuple[str, ...]
    connect_timeout_seconds: int
    response_timeout_seconds: int
    max_response_bytes: int
    max_redirects: int
    max_items: int
    max_item_characters: int
    max_concurrent_fetches: int
    lease_seconds: int
    metrics_namespace: str
    function_name: str


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value:
        raise ValueError(f"missing required environment variable {name}")
    return value


def _snapshot_prefix_from_environment() -> str:
    value = _required_environment("RAW_SNAPSHOT_PREFIX")
    if value.startswith("/"):
        raise ValueError("RAW_SNAPSHOT_PREFIX must be a relative S3 prefix")
    return value


def _positive_environment_integer(name: str) -> int:
    raw = _required_environment(name)
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a positive integer") from None
    if value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _zero_redirect_policy() -> int:
    raw = _required_environment("MAX_FEED_REDIRECTS")
    try:
        value = int(raw)
    except ValueError:
        raise ValueError("MAX_FEED_REDIRECTS must be zero") from None
    if value != 0:
        raise ValueError("MAX_FEED_REDIRECTS must be zero")
    return value


def _approved_hosts_from_environment() -> tuple[str, ...]:
    raw = _required_environment("APPROVED_FEED_HOSTS_JSON")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError("APPROVED_FEED_HOSTS_JSON must be a JSON array") from None
    if not isinstance(parsed, list) or not parsed:
        raise ValueError("APPROVED_FEED_HOSTS_JSON must be a non-empty JSON array")
    hosts = tuple(value.casefold() for value in parsed if isinstance(value, str))
    if len(hosts) != len(parsed) or len(set(hosts)) != len(hosts):
        raise ValueError("APPROVED_FEED_HOSTS_JSON must contain unique strings")
    for host in hosts:
        _safe_dimension("approved feed host", host)
    return hosts


def _configuration_from_environment() -> _RuntimeConfiguration:
    return _RuntimeConfiguration(
        config_bucket=_required_environment("CONFIG_BUCKET"),
        pointer_key=_required_environment("ACTIVE_VERSIONS_OBJECT_KEY"),
        source_state_table=_required_environment("SOURCE_STATE_TABLE_NAME"),
        delivery_table=_required_environment("DELIVERY_TABLE_NAME"),
        delivery_index=_required_environment("DELIVERY_INDEX_NAME"),
        raw_snapshot_prefix=_snapshot_prefix_from_environment(),
        application_version=application_artifact_id(_required_environment("APPLICATION_VERSION")),
        approved_hosts=_approved_hosts_from_environment(),
        connect_timeout_seconds=_positive_environment_integer("FEED_CONNECT_TIMEOUT_SECONDS"),
        response_timeout_seconds=_positive_environment_integer("FEED_RESPONSE_TIMEOUT_SECONDS"),
        max_response_bytes=_positive_environment_integer("MAX_FEED_RESPONSE_BYTES"),
        max_redirects=_zero_redirect_policy(),
        max_items=_positive_environment_integer("MAX_FEED_ITEMS"),
        max_item_characters=_positive_environment_integer("MAX_FEED_ITEM_CHARACTERS"),
        max_concurrent_fetches=_positive_environment_integer("MAX_CONCURRENT_FETCHES"),
        lease_seconds=_positive_environment_integer("FEED_LEASE_SECONDS"),
        metrics_namespace=_required_environment("METRICS_NAMESPACE"),
        function_name=_required_environment("AWS_LAMBDA_FUNCTION_NAME"),
    )


def _validate_schedule_event(event: Mapping[str, Any]) -> None:
    if not isinstance(event, Mapping):
        raise ValueError("invalid scheduled watcher event")
    if event.get("version") != "0" or event.get("source") != "aws.events":
        raise ValueError("invalid scheduled watcher event")
    if event.get("detail-type") != "Scheduled Event" or event.get("detail") != {}:
        raise ValueError("invalid scheduled watcher event")
    for name in ("id", "account", "time", "region"):
        if not isinstance(event.get(name), str) or not event[name]:
            raise ValueError("invalid scheduled watcher event")
    resources = event.get("resources")
    if (
        not isinstance(resources, list)
        or not resources
        or not all(isinstance(value, str) and value for value in resources)
    ):
        raise ValueError("invalid scheduled watcher event")


def _context_values(context: object) -> tuple[str, Callable[[], int]]:
    request_id = getattr(context, "aws_request_id", None)
    remaining = getattr(context, "get_remaining_time_in_millis", None)
    if not isinstance(request_id, str) or _INVOCATION_ID.fullmatch(request_id) is None or not callable(remaining):
        raise ValueError("invalid Lambda watcher context")
    return request_id, remaining


def _build_runtime(configuration: _RuntimeConfiguration) -> WatcherRuntime:
    import boto3

    s3 = boto3.client("s3")
    dynamodb = boto3.client("dynamodb")
    feed_state = DynamoDBFeedStateStore(dynamodb, configuration.source_state_table)
    announcement_state = DynamoDBAnnouncementStateStore(dynamodb, configuration.source_state_table)
    orchestrator = WatcherOrchestrator(
        feed_state=feed_state,
        announcement_state=announcement_state,
        snapshots=S3SnapshotStore(
            s3,
            configuration.config_bucket,
            configuration.raw_snapshot_prefix,
            max_bytes=configuration.max_response_bytes,
        ),
        outbox=DynamoDBDeliveryStore(
            dynamodb,
            configuration.delivery_table,
            configuration.delivery_index,
        ),
        fetcher=FeedFetcher(
            connect_timeout_seconds=configuration.connect_timeout_seconds,
            response_timeout_seconds=configuration.response_timeout_seconds,
            max_response_bytes=configuration.max_response_bytes,
        ),
        approved_hosts=configuration.approved_hosts,
        application_version=configuration.application_version,
        max_items=configuration.max_items,
        max_item_characters=configuration.max_item_characters,
        max_concurrent_fetches=configuration.max_concurrent_fetches,
        lease_seconds=configuration.lease_seconds,
    )
    return WatcherRuntime(
        release_store=S3ObjectStore(s3, configuration.config_bucket),
        pointer_key=configuration.pointer_key,
        application_version=configuration.application_version,
        orchestrator=orchestrator,
    )


_runtime: _WatcherRunner | None = None


class _WatcherIncomplete(RuntimeError):
    """A scheduled watcher pass stopped on a known bounded condition."""


def lambda_handler(event: Mapping[str, Any], context: object) -> dict[str, object]:
    """Run one scheduled watcher pass and expose only bounded diagnostics."""

    _validate_schedule_event(event)
    configuration = _configuration_from_environment()
    invocation_id, remaining_time_ms = _context_values(context)
    metrics = EmbeddedWatcherMetrics(configuration.metrics_namespace, configuration.function_name)
    metrics.heartbeat()
    global _runtime
    try:
        if _runtime is None:
            _runtime = _build_runtime(configuration)
        result = _runtime.run(
            invocation_id=invocation_id,
            remaining_time_ms=remaining_time_ms,
            metrics=metrics,
        )
        if result.incomplete:
            metrics.incomplete_run()
            raise _WatcherIncomplete
        return {
            "feeds": len(result.outcomes),
            "completed": sum(outcome.status in ("fetched", "not_modified") for outcome in result.outcomes),
            "advanced": len(result.advanced_feeds),
            "candidates": len(result.candidate_ids),
            "created_deliveries": len(result.created_delivery_ids),
            "repaired_deliveries": len(result.repaired_delivery_ids),
        }
    except _WatcherIncomplete:
        raise RuntimeError("watcher invocation failed") from None
    except ConditionalStateConflict:
        metrics.incomplete_run()
        raise RuntimeError("watcher invocation failed") from None
    except Exception:  # noqa: BLE001 - EventBridge must retry any unexpected watcher fault
        metrics.watcher_fault()
        raise RuntimeError("watcher invocation failed") from None
    finally:
        metrics.flush()
