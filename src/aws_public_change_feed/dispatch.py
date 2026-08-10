"""Dispatch due delivery records to the SQS FIFO queue.

Chapter 02 "Outbox dispatcher" and ADR-007 fix the algorithm: query due
`pending_queue` and `failed_retryable` records through `status-next-action-index`,
validate each stored request, claim a dispatch generation conditionally, send
the exact request to SQS with `MessageGroupId = destination_key` and
`MessageDeduplicationId = dispatch_id`, then move the record to `queued` with
the returned message ID.

`dispatch_generation` and `dispatch_id` together form the active claim while a
dispatchable record carries them. Reuse means an uncertain send or failed
post-send update is redone with the same dispatch ID, so FIFO deduplication
suppresses a duplicate of that queue attempt. When a future retry clears the
claim (the worker's job), the next dispatch increments the generation and
derives a new ID, so a deliberate retry is not itself suppressed by the
five-minute deduplication window.

SQS sits behind the `QueueSender` port so the whole decision loop is testable
without a network, and `SQSQueueSender` translates transport ambiguity into the
`unknown` outcome chapter 06 requires the dispatcher to make recoverable.

The `DeliveryRequest` is validated here against its packaged schema and its
deterministic identity before any claim is written, so a corrupted record
cannot be dispatched and a dispatch cannot construct a payload different from
the one the outbox persisted.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from importlib.resources import files
from typing import Any, Protocol

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from .identity import delivery_request_id
from .outbox import SCHEDULED_STATES, SCHEDULED_STATES_ORDERED, DeliveryRecord, OutboxStore, serialized_size
from .schema_formats import contract_format_checker

__all__ = [
    "DispatchResult",
    "DispatchSendRejected",
    "DispatcherMetrics",
    "InvalidDeliveryRequest",
    "NullDispatcherMetrics",
    "QueueSender",
    "SQSQueueSender",
    "SendResult",
    "SendStatus",
    "dispatch_due_work",
    "serialize_request",
    "validate_delivery_request",
]

# The delivery request embeds the candidate, and its schema references the
# candidate schema by ID, so both packaged copies join the resolution registry.
_ALERT_CANDIDATE_SCHEMA = "alert-candidate.schema.json"
_DELIVERY_REQUEST_SCHEMA = "delivery-request.schema.json"


class InvalidDeliveryRequest(ValueError):
    """A stored request failed schema or identity validation at dispatch time.

    Chapter 02 makes the dispatcher validate before claiming. A record that
    fails here is corrupt in a way that no retry fixes, so the dispatcher
    raises rather than sending it or silently skipping it.
    """


class DispatchSendRejected(Exception):
    """SQS definitively refused the message; nothing was accepted.

    Distinct from an `unknown` outcome: a transport ambiguity may or may not
    have enqueued the message, while a rejection means the queue answered and
    did not accept it. The record keeps its claim either way, so the next
    dispatch reuses the same dispatch ID.
    """


class SendStatus(Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class SendResult:
    """What one SQS send attempt proves about the message."""

    status: SendStatus
    message_id: str | None = None


class QueueSender(Protocol):
    """The narrow SQS surface dispatch needs, with the outcome preserved.

    `request` is the exact validated `DeliveryRequest`; `dispatch_id` is the
    claimed ID, sent as `MessageDeduplicationId`. ACCEPTED results carry the
    returned message ID, REJECTED results mean the queue definitely refused the
    message and are raised by the dispatcher, and UNKNOWN results mean the
    message may or may not have been enqueued.
    """

    def send(self, request: Mapping[str, Any], *, queue_url: str, dispatch_id: str) -> SendResult: ...


class SQSQueueSender:
    """`QueueSender` backed by a boto3 SQS client.

    The client is injected the way `S3ObjectStore` injects its S3 client, so
    tests drive the same adapter against `moto` and production supplies the
    real one. `MessageGroupId` and `MessageDeduplicationId` are the two message
    attributes chapter 02 fixes, so they are set here and nowhere else.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    def send(self, request: Mapping[str, Any], *, queue_url: str, dispatch_id: str) -> SendResult:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            response = self._client.send_message(
                QueueUrl=queue_url,
                MessageBody=serialize_request(request),
                MessageGroupId=request["destination_key"],
                MessageDeduplicationId=dispatch_id,
            )
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code", "Unknown")
            raise DispatchSendRejected(
                f"SQS returned {code} for dispatch ID {dispatch_id} to queue {queue_url}"
            ) from error
        except BotoCoreError:
            # Transport ambiguity: the request may have reached SQS even though
            # the client never saw a response.
            return SendResult(SendStatus.UNKNOWN)
        return SendResult(SendStatus.ACCEPTED, message_id=str(response["MessageId"]))


class DispatcherMetrics(Protocol):
    """Delivery metrics chapter 05 names for the dispatcher.

    "Dispatch attempts, accepted queue messages, and unknown send results."
    The dimensions are deliberately empty: bounded dimensions only, and the
    unknown-outcome alarm fires on the sum across destinations.
    """

    def dispatch_attempted(self) -> None: ...

    def dispatch_accepted(self) -> None: ...

    def dispatch_unknown(self) -> None: ...


class NullDispatcherMetrics:
    """A metrics sink that records nothing, for tests and dry runs."""

    def dispatch_attempted(self) -> None:
        return None

    def dispatch_accepted(self) -> None:
        return None

    def dispatch_unknown(self) -> None:
        return None


@dataclass(frozen=True, slots=True)
class DispatchResult:
    """What one dispatch run did, for the caller's logging and tests."""

    considered: int = 0
    reused_claims: int = 0
    new_claims: int = 0
    accepted: int = 0
    failed_transitions: int = 0
    unknown: int = 0


def serialize_request(request: Mapping[str, Any]) -> str:
    """Canonical serialization of the exact request for the SQS message body.

    The worker reconstructs the `DeliveryRequest` from these bytes, so the
    body is the committed contract, not a reserialization from a newer release.
    Sorted keys keep the bytes deterministic for logs and replay evidence.
    """

    return json.dumps(request, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _build_request_validator() -> Draft202012Validator:
    resources = files("aws_public_change_feed.schemas")
    registry = Registry()
    for name in (_ALERT_CANDIDATE_SCHEMA, _DELIVERY_REQUEST_SCHEMA):
        schema = json.loads(resources.joinpath(name).read_text(encoding="utf-8"))
        registry = registry.with_resource(schema["$id"], Resource.from_contents(schema))
    request_schema = json.loads(resources.joinpath(_DELIVERY_REQUEST_SCHEMA).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(request_schema)
    return Draft202012Validator(request_schema, format_checker=contract_format_checker(), registry=registry)


_request_validator = _build_request_validator()


def validate_delivery_request(
    request: Mapping[str, Any],
    *,
    max_bytes: int,
    candidate_id: str | None = None,
    destination_key: str | None = None,
) -> None:
    """Refuse a request that is not the exact contract its record claims.

    Schema first, then identity and the reviewed byte ceiling. A request that
    passes the schema but whose `request_id` does not derive from its embedded
    candidate would let a dispatcher construct a different payload than the one
    the outbox persisted, which is exactly what chapter 04 embeds the candidate
    to prevent.
    """

    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError("max_bytes must be a positive integer")
    violation = next(_request_validator.iter_errors(request), None)
    if violation is not None:
        location = ".".join(str(part) for part in violation.absolute_path) or "<root>"
        raise InvalidDeliveryRequest(f"delivery request fails its contract at {location}: {violation.message}")

    embedded = request["candidate"]["candidate_id"]
    expected = delivery_request_id(embedded)
    if request["request_id"] != expected:
        raise InvalidDeliveryRequest(
            f"request_id {request['request_id']} does not derive from embedded candidate {embedded}"
        )
    if candidate_id is not None and embedded != candidate_id:
        raise InvalidDeliveryRequest(
            f"request embeds candidate {embedded}, not the {candidate_id} its delivery record names"
        )
    if destination_key is not None and request["destination_key"] != destination_key:
        raise InvalidDeliveryRequest(
            f"request names destination {request['destination_key']}, not the record's {destination_key}"
        )
    actual_bytes = serialized_size(request)
    if actual_bytes > max_bytes:
        raise InvalidDeliveryRequest(f"delivery request is {actual_bytes} UTF-8 JSON bytes; maximum is {max_bytes}")


def _resolve_claim(
    store: OutboxStore,
    record: DeliveryRecord,
    due_before: int,
    max_delivery_request_bytes: int,
) -> tuple[int, str, bool] | None:
    """Return `(generation, dispatch_id, reused)` or `None` to skip this run.

    An active claim is reused unchanged. Otherwise the stored generation (or
    zero for a first dispatch) increments and is claimed conditionally; a lost
    claim race re-reads and reuses the winner's claim, and any other concurrent
    state change skips the record for this run.
    """

    if record.dispatch_id is not None:
        assert record.dispatch_generation is not None
        return record.dispatch_generation, record.dispatch_id, True
    claim = store.claim_dispatch(
        record.candidate_id,
        expected_state_version=record.state_version,
        expected_generation=record.dispatch_generation,
        request_id=record.request["request_id"],
        due_before=due_before,
    )
    if claim is not None:
        generation, dispatch_id = claim
        return generation, dispatch_id, False
    fresh = store.get_delivery(record.candidate_id)
    if fresh is not None and fresh.status in SCHEDULED_STATES and fresh.dispatch_id is not None:
        assert fresh.dispatch_generation is not None
        validate_delivery_request(
            fresh.request,
            max_bytes=max_delivery_request_bytes,
            candidate_id=fresh.candidate_id,
            destination_key=fresh.destination_key,
        )
        return fresh.dispatch_generation, fresh.dispatch_id, True
    return None


def dispatch_due_work(
    store: OutboxStore,
    sender: QueueSender,
    *,
    queue_url: str,
    now: datetime,
    max_delivery_request_bytes: int,
    limit: int = 100,
    metrics: DispatcherMetrics | None = None,
) -> DispatchResult:
    """Dispatch every due `pending_queue` and `failed_retryable` record once.

    Chapter 02's dispatcher loop: query due work through the status index,
    validate the stored request, resolve the dispatch claim, send the exact
    request to SQS with the destination as the message group and the dispatch
    ID as the deduplication ID, and move the record to `queued` with the
    returned message ID.

    An unknown send outcome or a failed `queued` transition leaves the claim in
    place so the next run reuses the same dispatch ID, and neither stops other
    destinations' records in the same run. A definite SQS rejection raises
    `DispatchSendRejected` so the failure is visible. `now` is the invocation
    clock; work due at or before it is dispatched. `limit` caps the merged batch
    across both scheduled states.
    """

    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")
    if (
        isinstance(max_delivery_request_bytes, bool)
        or not isinstance(max_delivery_request_bytes, int)
        or max_delivery_request_bytes < 1
    ):
        raise ValueError("max_delivery_request_bytes must be a positive integer")
    metrics = metrics if metrics is not None else NullDispatcherMetrics()
    due_before = int(now.timestamp())
    result = DispatchResult()

    # Collect due candidate IDs and their next_action_at across both scheduled
    # statuses, then sort them into one oldest-first order. The status index
    # queries each return results ordered by status's sort key, and the merge
    # gives us the cross-status ordering chapter 02 requires.
    due: list[tuple[int, str]] = []
    for status in SCHEDULED_STATES_ORDERED:
        due.extend(store.query_due(status, due_before=due_before, limit=limit))
    due.sort(key=lambda entry: entry[0])
    due = due[:limit]

    for _, candidate in due:
        result = replace(result, considered=result.considered + 1)
        record = store.get_delivery(candidate)
        if record is None:
            continue
        if record.status not in SCHEDULED_STATES or record.next_action_at is None or record.next_action_at > due_before:
            continue
        validate_delivery_request(
            record.request,
            max_bytes=max_delivery_request_bytes,
            candidate_id=candidate,
            destination_key=record.destination_key,
        )
        claim = _resolve_claim(store, record, due_before, max_delivery_request_bytes)
        if claim is None:
            continue
        generation, dispatch_id, reused = claim
        metrics.dispatch_attempted()
        result = replace(
            result,
            reused_claims=result.reused_claims + (1 if reused else 0),
            new_claims=result.new_claims + (0 if reused else 1),
        )
        try:
            outcome = sender.send(record.request, queue_url=queue_url, dispatch_id=dispatch_id)
        except DispatchSendRejected as error:
            raise DispatchSendRejected(f"dispatch of delivery {candidate} rejected: {error}") from error
        if outcome.status is SendStatus.ACCEPTED:
            metrics.dispatch_accepted()
            transitioned = outcome.message_id is not None and store.mark_queued(
                candidate,
                dispatch_id=dispatch_id,
                message_id=outcome.message_id,
                at=due_before,
            )
            result = replace(
                result,
                accepted=result.accepted + (1 if transitioned else 0),
                failed_transitions=result.failed_transitions + (0 if transitioned else 1),
            )
        elif outcome.status is SendStatus.UNKNOWN:
            metrics.dispatch_unknown()
            result = replace(result, unknown=result.unknown + 1)
        elif outcome.status is SendStatus.REJECTED:
            raise DispatchSendRejected(f"dispatch of delivery {candidate} rejected without queue acceptance")
    return result
