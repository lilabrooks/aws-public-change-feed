"""AWS composition root and scheduled Lambda handler for delivery recovery."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from .dispatch import QueueSender, SQSQueueSender
from .outbox import DynamoDBDeliveryStore, OutboxStore
from .recovery import RecoveryMetrics, RecoveryResult, reconcile_deliveries

__all__ = ["EmbeddedRecoveryMetrics", "RecoveryRuntime", "lambda_handler"]

_STATE_ALLOWLIST = ("pending_queue", "failed_retryable", "queued", "sending")
_METRIC_NAMESPACE_CHARACTERS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./#:-")


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


@dataclass(slots=True)
class EmbeddedRecoveryMetrics:
    """Collect bounded recovery facts and emit CloudWatch EMF documents."""

    namespace: str
    function_name: str
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    emit: Callable[[str], None] = print
    _counts: dict[str, int] = field(default_factory=dict, init=False)
    _states: dict[str, tuple[int, int, bool]] = field(default_factory=dict, init=False)
    _heartbeat: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.namespace, str)
            or not 1 <= len(self.namespace) <= 255
            or self.namespace.startswith("AWS/")
            or any(character not in _METRIC_NAMESPACE_CHARACTERS for character in self.namespace)
        ):
            raise ValueError("metrics namespace must be 1-255 safe characters and cannot start with AWS/")
        if not isinstance(self.function_name, str) or not self.function_name:
            raise ValueError("function_name must be a nonempty string")

    def _increment(self, name: str, value: int = 1) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("metric increments must be non-negative integers")
        self._counts[name] = self._counts.get(name, 0) + value

    def heartbeat(self) -> None:
        self._heartbeat = True

    def state_observed(self, state: str, *, count: int, oldest_age_seconds: int) -> None:
        if state not in _STATE_ALLOWLIST:
            raise ValueError(f"state metric is not allowlisted: {state}")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("observed state count must be a non-negative integer")
        if isinstance(oldest_age_seconds, bool) or not isinstance(oldest_age_seconds, int) or oldest_age_seconds < 0:
            raise ValueError("oldest state age must be a non-negative integer")
        saturated = self._states.get(state, (0, 0, False))[2]
        self._states[state] = (count, oldest_age_seconds, saturated)

    def state_observation_saturated(self, state: str) -> None:
        if state not in _STATE_ALLOWLIST:
            raise ValueError(f"state metric is not allowlisted: {state}")
        count, age, _ = self._states.get(state, (0, 0, False))
        self._states[state] = (count, age, True)
        self._increment("StateObservationSaturated")

    def repair_limit_reached(self) -> None:
        self._increment("RecoveryRepairLimitReached")

    def expired_lease_unknown(self) -> None:
        self._increment("ExpiredLeaseUnknown")
        self._increment("DeliveryUnknown")

    def stale_queued(self, count: int) -> None:
        self._increment("StaleQueued", count)

    def conditional_race(self) -> None:
        self._increment("RecoveryConditionalRace")

    def dispatch_attempted(self) -> None:
        self._increment("DispatchAttempted")

    def dispatch_accepted(self) -> None:
        self._increment("DispatchAccepted")

    def dispatch_unknown(self) -> None:
        self._increment("DispatchUnknownOutcome")

    def reconciler_fault(self) -> None:
        self._increment("ReconcilerFault")

    def flush(self) -> None:
        timestamp_ms = int(self.clock().timestamp() * 1000)
        if self._heartbeat:
            self.emit(
                _metric_document(
                    self.namespace,
                    timestamp_ms,
                    [["Function"]],
                    [("Heartbeat", "Count")],
                    {"Function": self.function_name, "Heartbeat": 1},
                )
            )

        dimensionless = dict(self._counts)
        dimensionless["OldestPendingDeliveryAgeSeconds"] = max((age for _, age, _ in self._states.values()), default=0)
        count_names = sorted(name for name in dimensionless if name != "OldestPendingDeliveryAgeSeconds")
        metric_definitions = [(name, "Count") for name in count_names]
        metric_definitions.append(("OldestPendingDeliveryAgeSeconds", "Seconds"))
        self.emit(
            _metric_document(
                self.namespace,
                timestamp_ms,
                [[]],
                metric_definitions,
                dimensionless,
            )
        )

        for state in _STATE_ALLOWLIST:
            count, age, saturated = self._states.get(state, (0, 0, False))
            definitions = [
                ("ObservedDeliveryCount", "Count"),
                ("OldestDeliveryAgeSeconds", "Seconds"),
            ]
            values: dict[str, object] = {
                "State": state,
                "ObservedDeliveryCount": count,
                "OldestDeliveryAgeSeconds": age,
            }
            if saturated:
                definitions.append(("StateObservationSaturated", "Count"))
                values["StateObservationSaturated"] = 1
            self.emit(
                _metric_document(
                    self.namespace,
                    timestamp_ms,
                    [["State"]],
                    definitions,
                    values,
                )
            )


@dataclass(frozen=True, slots=True)
class RecoveryRuntime:
    store: OutboxStore
    sender: QueueSender
    queue_url: str
    max_delivery_request_bytes: int
    repair_limit: int
    observation_limit: int
    stale_queued_after_seconds: int

    def run(self, now: datetime, metrics: RecoveryMetrics) -> RecoveryResult:
        return reconcile_deliveries(
            self.store,
            self.sender,
            queue_url=self.queue_url,
            now=now,
            max_delivery_request_bytes=self.max_delivery_request_bytes,
            repair_limit=self.repair_limit,
            observation_limit=self.observation_limit,
            stale_queued_after_seconds=self.stale_queued_after_seconds,
            metrics=metrics,
        )


class _RecoveryRunner(Protocol):
    def run(self, now: datetime, metrics: RecoveryMetrics) -> RecoveryResult: ...


class _RecoveryIncomplete(Exception):
    """One bounded recovery pass stopped with durable work remaining."""


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value:
        raise ValueError(f"missing required environment variable {name}")
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


def _non_negative_environment_integer(name: str) -> int:
    raw = _required_environment(name)
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a non-negative integer") from None
    if value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _validate_schedule_event(event: Mapping[str, Any]) -> None:
    if not isinstance(event, Mapping):
        raise ValueError("invalid scheduled recovery event")
    if event.get("source") != "aws.events" or event.get("detail-type") != "Scheduled Event":
        raise ValueError("invalid scheduled recovery event")
    if not isinstance(event.get("id"), str) or not event["id"]:
        raise ValueError("invalid scheduled recovery event")
    if not isinstance(event.get("time"), str) or not event["time"]:
        raise ValueError("invalid scheduled recovery event")
    if not isinstance(event.get("detail"), Mapping):
        raise ValueError("invalid scheduled recovery event")


def _build_runtime_from_environment() -> RecoveryRuntime:
    import boto3

    return RecoveryRuntime(
        store=DynamoDBDeliveryStore(
            boto3.client("dynamodb"),
            _required_environment("DELIVERY_TABLE_NAME"),
            _required_environment("DELIVERY_INDEX_NAME"),
        ),
        sender=SQSQueueSender(boto3.client("sqs")),
        queue_url=_required_environment("DELIVERY_QUEUE_URL"),
        max_delivery_request_bytes=_positive_environment_integer("MAX_DELIVERY_REQUEST_BYTES"),
        repair_limit=_positive_environment_integer("RECOVERY_REPAIR_LIMIT"),
        observation_limit=_positive_environment_integer("RECOVERY_OBSERVATION_LIMIT"),
        stale_queued_after_seconds=_non_negative_environment_integer("RECOVERY_STALE_QUEUED_SECONDS"),
    )


_runtime: _RecoveryRunner | None = None


def lambda_handler(event: Mapping[str, Any], context: object) -> dict[str, object]:
    """Run one scheduled recovery pass and fail safely when it is incomplete."""

    del context
    _validate_schedule_event(event)
    global _runtime
    if _runtime is None:
        _runtime = _build_runtime_from_environment()
    metrics = EmbeddedRecoveryMetrics(
        _required_environment("METRICS_NAMESPACE"),
        _required_environment("AWS_LAMBDA_FUNCTION_NAME"),
    )
    metrics.heartbeat()
    try:
        result = _runtime.run(datetime.now(UTC), metrics)
        if result.incomplete:
            raise _RecoveryIncomplete
        return {
            "repaired": result.dispatch.accepted + result.expired_leases,
            "stale_queued": result.stale_queued,
            "conditional_races": result.conditional_races,
        }
    except _RecoveryIncomplete:
        raise RuntimeError("recovery invocation incomplete") from None
    except Exception:  # noqa: BLE001 - scheduled retry is safer than hiding a recovery fault
        metrics.reconciler_fault()
        raise RuntimeError("recovery invocation failed") from None
    finally:
        metrics.flush()
