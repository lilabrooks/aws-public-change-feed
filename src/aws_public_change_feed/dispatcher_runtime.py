"""Scheduled outbox-dispatcher Lambda boundary and bounded EMF metrics."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from .dispatch import DispatcherMetrics, DispatchResult, QueueSender, SQSQueueSender, dispatch_due_work
from .outbox import DynamoDBDeliveryStore, OutboxStore

__all__ = ["DispatcherRuntime", "EmbeddedDispatcherMetrics", "lambda_handler"]

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
class EmbeddedDispatcherMetrics:
    """Collect bounded dispatcher facts and emit CloudWatch EMF documents."""

    namespace: str
    function_name: str
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    emit: Callable[[str], None] = print
    _counts: dict[str, int] = field(default_factory=dict, init=False)
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

    def _increment(self, name: str) -> None:
        self._counts[name] = self._counts.get(name, 0) + 1

    def heartbeat(self) -> None:
        self._heartbeat = True

    def dispatch_attempted(self) -> None:
        self._increment("DispatchAttempted")

    def dispatch_accepted(self) -> None:
        self._increment("DispatchAccepted")

    def dispatch_unknown(self) -> None:
        self._increment("DispatchUnknownOutcome")

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
        if self._counts:
            self.emit(
                _metric_document(
                    self.namespace,
                    timestamp_ms,
                    [[]],
                    [(name, "Count") for name in sorted(self._counts)],
                    self._counts,
                )
            )


@dataclass(frozen=True, slots=True)
class DispatcherRuntime:
    store: OutboxStore
    sender: QueueSender
    queue_url: str
    max_delivery_request_bytes: int

    def run(self, now: datetime, metrics: DispatcherMetrics) -> DispatchResult:
        return dispatch_due_work(
            self.store,
            self.sender,
            queue_url=self.queue_url,
            now=now,
            max_delivery_request_bytes=self.max_delivery_request_bytes,
            limit=100,
            metrics=metrics,
        )


class _DispatcherRunner(Protocol):
    def run(self, now: datetime, metrics: DispatcherMetrics) -> DispatchResult: ...


@dataclass(frozen=True, slots=True)
class _Configuration:
    delivery_table_name: str
    delivery_index_name: str
    delivery_queue_url: str
    max_delivery_request_bytes: int
    metrics_namespace: str
    function_name: str


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


def _configuration_from_environment() -> _Configuration:
    return _Configuration(
        delivery_table_name=_required_environment("DELIVERY_TABLE_NAME"),
        delivery_index_name=_required_environment("DELIVERY_INDEX_NAME"),
        delivery_queue_url=_required_environment("DELIVERY_QUEUE_URL"),
        max_delivery_request_bytes=_positive_environment_integer("MAX_DELIVERY_REQUEST_BYTES"),
        metrics_namespace=_required_environment("METRICS_NAMESPACE"),
        function_name=_required_environment("AWS_LAMBDA_FUNCTION_NAME"),
    )


def _validate_schedule_event(event: Mapping[str, Any]) -> None:
    if not isinstance(event, Mapping):
        raise ValueError("invalid scheduled dispatcher event")
    if event.get("source") != "aws.events" or event.get("detail-type") != "Scheduled Event":
        raise ValueError("invalid scheduled dispatcher event")
    if not isinstance(event.get("id"), str) or not event["id"]:
        raise ValueError("invalid scheduled dispatcher event")
    if not isinstance(event.get("time"), str) or not event["time"]:
        raise ValueError("invalid scheduled dispatcher event")
    if not isinstance(event.get("detail"), Mapping):
        raise ValueError("invalid scheduled dispatcher event")


def _build_runtime(configuration: _Configuration) -> DispatcherRuntime:
    import boto3

    return DispatcherRuntime(
        store=DynamoDBDeliveryStore(
            boto3.client("dynamodb"),
            configuration.delivery_table_name,
            configuration.delivery_index_name,
        ),
        sender=SQSQueueSender(boto3.client("sqs")),
        queue_url=configuration.delivery_queue_url,
        max_delivery_request_bytes=configuration.max_delivery_request_bytes,
    )


_runtime: _DispatcherRunner | None = None


def lambda_handler(event: Mapping[str, Any], context: object) -> dict[str, int]:
    """Run one scheduled dispatch pass and expose only bounded facts."""

    del context
    _validate_schedule_event(event)
    configuration = _configuration_from_environment()
    metrics = EmbeddedDispatcherMetrics(configuration.metrics_namespace, configuration.function_name)
    metrics.heartbeat()
    global _runtime
    try:
        if _runtime is None:
            _runtime = _build_runtime(configuration)
        result = _runtime.run(datetime.now(UTC), metrics)
        return {
            "considered": result.considered,
            "accepted": result.accepted,
            "unknown": result.unknown,
            "failed_transitions": result.failed_transitions,
        }
    except Exception:  # noqa: BLE001 - EventBridge retry is safer than hiding a dispatch fault
        raise RuntimeError("dispatcher invocation failed") from None
    finally:
        metrics.flush()
