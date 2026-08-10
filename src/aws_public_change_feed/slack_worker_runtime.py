"""AWS composition root and FIFO Lambda handler for Slack delivery.

The worker state machine stays in :mod:`aws_public_change_feed.worker`. This
module owns the deployment edges: decoding SQS records, preserving FIFO order,
stopping before the Lambda time reserve is spent, constructing AWS adapters,
and emitting a bounded metric document for one invocation.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from .aws_credentials import credential_reader_for
from .credentials import BOT_TOKEN, WEBHOOK, CredentialReader
from .dispatch import InvalidDeliveryRequest, validate_delivery_request
from .identity import application_artifact_id
from .outbox import DynamoDBDeliveryStore, OutboxStore
from .parsing import load_unique_json
from .releases import ObjectStore, S3ObjectStore
from .slack_transport import SlackHttpSender
from .worker import QueueDelivery, SlackSender, WorkerMetrics, WorkerResult, process_delivery

__all__ = [
    "EmbeddedWorkerMetrics",
    "SlackWorkerRuntime",
    "S3ArtifactCatalog",
    "lambda_handler",
    "process_fifo_batch",
]

_METRIC_NAMES = {
    "delivery_attempted": "DeliveryAttempted",
    "posted": "Posted",
    "retryable": "Retryable",
    "terminal": "TerminalFailure",
    "unknown": "DeliveryUnknown",
    "duplicate_posted": "DuplicatePosted",
    "unprocessed": "Unprocessed",
    "dropped_message": "DroppedMessage",
    "application_version_mismatch": "ApplicationVersionMismatch",
    "artifact_unavailable": "ArtifactUnavailable",
    "artifact_availability_check_failed": "ArtifactAvailabilityCheckFailed",
    "malformed_message": "MalformedMessage",
    "time_reserve_exhausted": "TimeReserveExhausted",
    "batch_stopped": "BatchStopped",
    "worker_fault": "WorkerFault",
}
_METRIC_NAMESPACE_CHARACTERS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./#:-")


class LambdaContext(Protocol):
    def get_remaining_time_in_millis(self) -> int: ...


class RecordProcessor(Protocol):
    def __call__(self, candidate: str, queue_delivery: QueueDelivery, metrics: WorkerMetrics) -> WorkerResult: ...


class ArtifactCatalog(Protocol):
    def available(self, application_version: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class S3ArtifactCatalog:
    client: Any
    bucket: str
    prefix: str

    def available(self, application_version: str) -> bool:
        from botocore.exceptions import ClientError

        digest = application_artifact_id(application_version).removeprefix("sha256:")
        try:
            response = self.client.head_object(
                Bucket=self.bucket,
                Key=f"{self.prefix.rstrip('/')}/{digest}.zip",
            )
        except ClientError as error:
            status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            code = error.response.get("Error", {}).get("Code")
            if status == 404 or code in ("404", "NoSuchKey", "NotFound"):
                return False
            raise RuntimeError("application artifact availability check did not complete") from None
        metadata = response.get("Metadata")
        return isinstance(metadata, Mapping) and metadata.get("sha256") == digest


@dataclass(slots=True)
class EmbeddedWorkerMetrics:
    """Collect fixed-name counts and emit one CloudWatch EMF document.

    The deployment ID is already part of the namespace. No candidate, route,
    destination, response text, or credential-derived value becomes a metric
    dimension, which keeps cardinality and secret exposure bounded.
    """

    namespace: str
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    emit: Callable[[str], None] = print
    _counts: dict[str, int] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.namespace, str)
            or not 1 <= len(self.namespace) <= 255
            or self.namespace.startswith("AWS/")
            or any(character not in _METRIC_NAMESPACE_CHARACTERS for character in self.namespace)
        ):
            raise ValueError("metrics namespace must be 1-255 safe characters and cannot start with AWS/")

    def _increment(self, key: str) -> None:
        name = _METRIC_NAMES[key]
        self._counts[name] = self._counts.get(name, 0) + 1

    def delivery_attempted(self) -> None:
        self._increment("delivery_attempted")

    def posted(self) -> None:
        self._increment("posted")

    def retryable(self) -> None:
        self._increment("retryable")

    def terminal(self) -> None:
        self._increment("terminal")

    def unknown(self) -> None:
        self._increment("unknown")

    def duplicate_posted(self) -> None:
        self._increment("duplicate_posted")

    def unprocessed(self) -> None:
        self._increment("unprocessed")

    def dropped_message(self) -> None:
        self._increment("dropped_message")

    def application_version_mismatch(self) -> None:
        self._increment("application_version_mismatch")

    def artifact_unavailable(self) -> None:
        self._increment("artifact_unavailable")

    def artifact_availability_check_failed(self) -> None:
        self._increment("artifact_availability_check_failed")

    def malformed_message(self) -> None:
        self._increment("malformed_message")

    def time_reserve_exhausted(self) -> None:
        self._increment("time_reserve_exhausted")

    def batch_stopped(self) -> None:
        self._increment("batch_stopped")

    def worker_fault(self) -> None:
        self._increment("worker_fault")

    def flush(self) -> None:
        if not self._counts:
            return
        metric_names = sorted(self._counts)
        document: dict[str, Any] = {
            "_aws": {
                "Timestamp": int(self.clock().timestamp() * 1000),
                "CloudWatchMetrics": [
                    {
                        "Namespace": self.namespace,
                        "Dimensions": [[]],
                        "Metrics": [{"Name": name, "Unit": "Count"} for name in metric_names],
                    }
                ],
            },
        }
        document.update({name: self._counts[name] for name in metric_names})
        self.emit(json.dumps(document, separators=(",", ":"), sort_keys=True))


@dataclass(frozen=True, slots=True)
class SlackWorkerRuntime:
    store: OutboxStore
    release_store: ObjectStore
    credentials: CredentialReader
    sender: SlackSender
    application_version: str
    max_delivery_request_bytes: int
    lease_duration_seconds: int
    artifact_catalog: ArtifactCatalog
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    def __post_init__(self) -> None:
        application_artifact_id(self.application_version)
        for name, value in (
            ("max_delivery_request_bytes", self.max_delivery_request_bytes),
            ("lease_duration_seconds", self.lease_duration_seconds),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")

    def process(self, candidate: str, queue_delivery: QueueDelivery, metrics: WorkerMetrics) -> WorkerResult:
        result = process_delivery(
            self.store,
            self.release_store,
            self.credentials,
            self.sender,
            candidate=candidate,
            application_version=self.application_version,
            clock=self.clock,
            max_delivery_request_bytes=self.max_delivery_request_bytes,
            lease_duration_seconds=self.lease_duration_seconds,
            queue_delivery=queue_delivery,
            metrics=metrics,
        )
        if result.reason_code == "application_version_mismatch":
            required = cast(str, queue_delivery.request["candidate"]["release"]["application_version"])
            try:
                available = self.artifact_catalog.available(required)
            except RuntimeError:
                metrics.artifact_availability_check_failed()
            else:
                if not available:
                    metrics.artifact_unavailable()
        return result


def _record_ids(records: Sequence[object], start: int) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for record in records[start:]:
        if not isinstance(record, Mapping):
            raise ValueError("every SQS record must be an object")
        message_id = record.get("messageId")
        if not isinstance(message_id, str) or not message_id:
            raise ValueError("every SQS record must carry a nonempty messageId")
        failures.append({"itemIdentifier": message_id})
    return failures


def _delivery_from_record(record: object, *, max_delivery_request_bytes: int) -> tuple[str, QueueDelivery]:
    if not isinstance(record, Mapping):
        raise ValueError("every SQS record must be an object")
    body = record.get("body")
    if not isinstance(body, str):
        raise ValueError("every SQS record must carry a text body")
    if len(body.encode("utf-8")) > max_delivery_request_bytes:
        raise InvalidDeliveryRequest("delivery request message body exceeds the byte limit")
    parsed = load_unique_json(body)
    if not isinstance(parsed, Mapping):
        raise InvalidDeliveryRequest("delivery request message body must be an object")
    request = cast(Mapping[str, Any], parsed)
    validate_delivery_request(request, max_bytes=max_delivery_request_bytes)
    message_id = record.get("messageId")
    attributes = record.get("attributes")
    if not isinstance(message_id, str) or not message_id:
        raise ValueError("every SQS record must carry a nonempty messageId")
    if not isinstance(attributes, Mapping):
        raise ValueError("every SQS record must carry an attributes object")
    message_group_id = attributes.get("MessageGroupId")
    if not isinstance(message_group_id, str) or not message_group_id:
        raise ValueError("every FIFO SQS record must carry a nonempty MessageGroupId")
    return cast(str, request["candidate"]["candidate_id"]), QueueDelivery(
        request=request,
        message_id=message_id,
        message_group_id=message_group_id,
    )


def process_fifo_batch(
    event: Mapping[str, Any],
    context: LambdaContext,
    processor: RecordProcessor,
    *,
    max_delivery_request_bytes: int,
    safety_reserve_milliseconds: int,
    metrics: EmbeddedWorkerMetrics,
) -> dict[str, list[dict[str, str]]]:
    """Process an SQS FIFO batch until the first failure or time boundary."""

    records = event.get("Records")
    if not isinstance(records, list):
        raise ValueError("SQS event must carry a Records array")
    if (
        isinstance(safety_reserve_milliseconds, bool)
        or not isinstance(safety_reserve_milliseconds, int)
        or safety_reserve_milliseconds < 0
    ):
        raise ValueError("safety_reserve_milliseconds must be a non-negative integer")

    for index, record in enumerate(records):
        if context.get_remaining_time_in_millis() < safety_reserve_milliseconds:
            metrics.time_reserve_exhausted()
            metrics.batch_stopped()
            return {"batchItemFailures": _record_ids(records, index)}
        try:
            candidate, queue_delivery = _delivery_from_record(
                record, max_delivery_request_bytes=max_delivery_request_bytes
            )
        except (InvalidDeliveryRequest, UnicodeError, ValueError, TypeError):
            metrics.malformed_message()
            metrics.batch_stopped()
            return {"batchItemFailures": _record_ids(records, index)}

        try:
            result = processor(candidate, queue_delivery, metrics)
        except Exception:  # noqa: BLE001 - Lambda retry is safer than acknowledging an unknown worker fault
            metrics.worker_fault()
            metrics.batch_stopped()
            return {"batchItemFailures": _record_ids(records, index)}
        if not result.handled:
            metrics.batch_stopped()
            return {"batchItemFailures": _record_ids(records, index)}

    return {"batchItemFailures": []}


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


def _build_runtime_from_environment() -> SlackWorkerRuntime:
    import boto3

    secret_store = _required_environment("SECRET_STORE")
    delivery_mode = _required_environment("DELIVERY_MODE")
    if delivery_mode not in (WEBHOOK, BOT_TOKEN):
        raise ValueError("DELIVERY_MODE must name an accepted Slack delivery mode")
    credential_service = "secretsmanager" if secret_store == "secrets_manager" else "ssm"
    s3_client = boto3.client("s3")
    config_bucket = _required_environment("CONFIG_BUCKET_NAME")
    return SlackWorkerRuntime(
        store=DynamoDBDeliveryStore(
            boto3.client("dynamodb"),
            _required_environment("DELIVERY_TABLE_NAME"),
            _required_environment("DELIVERY_INDEX_NAME"),
        ),
        release_store=S3ObjectStore(s3_client, config_bucket),
        credentials=credential_reader_for(secret_store, boto3.client(credential_service), kind=delivery_mode),
        sender=SlackHttpSender(),
        application_version=_required_environment("APPLICATION_VERSION"),
        max_delivery_request_bytes=_positive_environment_integer("MAX_DELIVERY_REQUEST_BYTES"),
        lease_duration_seconds=_positive_environment_integer("WORKER_LEASE_DURATION_SECONDS"),
        artifact_catalog=S3ArtifactCatalog(
            s3_client,
            config_bucket,
            _required_environment("APPLICATION_ARTIFACT_PREFIX"),
        ),
    )


_runtime: SlackWorkerRuntime | None = None


def lambda_handler(event: Mapping[str, Any], context: LambdaContext) -> dict[str, list[dict[str, str]]]:
    """Lambda entrypoint. Adapter construction is cached after the first call."""

    global _runtime
    if _runtime is None:
        _runtime = _build_runtime_from_environment()
    metrics = EmbeddedWorkerMetrics(_required_environment("METRICS_NAMESPACE"))
    try:
        return process_fifo_batch(
            event,
            context,
            _runtime.process,
            max_delivery_request_bytes=_runtime.max_delivery_request_bytes,
            safety_reserve_milliseconds=_positive_environment_integer("WORKER_SAFETY_RESERVE_MILLISECONDS"),
            metrics=metrics,
        )
    finally:
        metrics.flush()
