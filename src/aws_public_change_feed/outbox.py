"""Durable outbox port for candidates and their delivery records.

Chapter 04 "Durable emission" and chapter 02 "Delivery table" define this
boundary: for each candidate, conditionally write the candidate and its
`pending_queue` delivery record, and never advance a feed checkpoint until that
work is durable. ADR-007 makes DynamoDB the system of record.

The port is defined here with in-memory and DynamoDB implementations, so
idempotent re-emission, request reuse, the identity-mismatch failure, and the
dispatcher's conditional claims are testable against the same boundary that
runs in production. `DynamoDBDeliveryStore` targets the table `infra/central`
provisions and encodes every state transition as a condition expression,
because the condition is what serializes concurrent dispatchers and workers.

What lives here is the creation boundary and the delivery-item state machine
the dispatcher drives. Dispatch generations, queue message IDs, Slack outcomes,
and terminal TTLs belong to the dispatcher and worker in milestone 5, and this
module writes only `pending_queue`, `queued`, and the dispatch claim fields
those transitions need: a `pending_queue` record has no TTL, because chapter 02
gives one only to resolved states.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Protocol

from .candidates import CONTRACT_VERSION, utc_timestamp
from .identity import audience_fingerprint, candidate_id, delivery_request_id, queue_dispatch_id

__all__ = [
    "CANDIDATE_KEY_PREFIX",
    "DELIVERY_STATES",
    "CandidateIdentityError",
    "DeliveryRecord",
    "DynamoDBDeliveryStore",
    "EmissionResult",
    "InMemoryOutboxStore",
    "OutboxStore",
    "OversizeDeliveryError",
    "SCHEDULED_STATES",
    "SCHEDULED_STATES_ORDERED",
    "build_delivery_request",
    "emit",
    "serialized_size",
    "verify_durable",
]

# A dispatch ID is `queue_dispatch_id` output, a lowercase SHA-256 digest.
_DIGEST = re.compile(r"[a-f0-9]{64}")

# Chapter 02 keys the candidate and delivery items under one partition.
CANDIDATE_KEY_PREFIX = "CANDIDATE#"
DELIVERY_SORT_KEY = "DELIVERY"

# Chapter 02. Only `pending_queue` is written here; the rest are listed so the
# dispatcher and worker share one definition rather than three string literals.
DELIVERY_STATES = (
    "pending_queue",
    "queued",
    "sending",
    "posted",
    "failed_retryable",
    "failed_terminal",
    "delivery_unknown",
)

INITIAL_STATE = "pending_queue"
SCHEDULED_STATES = frozenset({"pending_queue", "failed_retryable"})
SCHEDULED_STATES_ORDERED = ("pending_queue", "failed_retryable")


class CandidateIdentityError(Exception):
    """A stored item's identity fields disagree with its key.

    Chapter 04 calls this a correctness failure that blocks checkpoint
    advancement, so it is deliberately not a `ValueError` the caller might
    handle alongside ordinary validation. Nothing recovers from it
    automatically.
    """


class OversizeDeliveryError(ValueError):
    """A candidate or request exceeded its reviewed UTF-8 JSON byte limit."""


@dataclass(frozen=True, slots=True)
class DeliveryRecord:
    """The delivery item chapter 02 keys as `CANDIDATE#<id>` / `DELIVERY`.

    `request` is the exact `DeliveryRequest` the dispatcher will send. Chapter
    04 requires it to be reused verbatim when a missing record is repaired, so
    the worker cannot be handed a payload built from a newer release.
    """

    candidate_id: str
    destination_key: str
    request: Mapping[str, Any]
    # Required even when the state omits the index key. A default of None made
    # the default pending state construct an invalid record unless callers knew
    # about an otherwise hidden invariant.
    next_action_at: int | None
    status: str = INITIAL_STATE
    state_version: int = 1
    created_at: str = ""
    # DynamoDB indexes this field as a Number. Whole Unix epoch seconds keep
    # due-work comparisons numeric and avoid an item silently disappearing
    # from status-next-action-index because its sort key is absent.
    #
    # `dispatch_generation` and `dispatch_id` together are the active dispatch
    # claim while a dispatchable record carries them. ADR-007: an uncertain
    # send or failed post-send update leaves the claim in place and the next
    # dispatch reuses it, so FIFO deduplication suppresses a duplicate of that
    # queue attempt. Clearing `dispatch_id` (a future worker retry) keeps the
    # generation as the counter the next claim increments from, so a deliberate
    # retry inside the deduplication window gets a fresh dispatch ID.
    dispatch_generation: int | None = None
    dispatch_id: str | None = None
    queue_message_id: str | None = None

    def __post_init__(self) -> None:
        if self.status not in DELIVERY_STATES:
            raise ValueError(f"unknown delivery state: {self.status}")
        if isinstance(self.state_version, bool) or not isinstance(self.state_version, int) or self.state_version < 1:
            raise ValueError("state_version must be a positive integer")
        if self.status in SCHEDULED_STATES and self.next_action_at is None:
            raise ValueError(f"{self.status} delivery records require next_action_at")
        if self.next_action_at is not None and (
            isinstance(self.next_action_at, bool) or not isinstance(self.next_action_at, int) or self.next_action_at < 0
        ):
            raise ValueError("next_action_at must be a non-negative integer Unix timestamp")
        if self.dispatch_generation is not None and (
            isinstance(self.dispatch_generation, bool)
            or not isinstance(self.dispatch_generation, int)
            or self.dispatch_generation < 1
        ):
            raise ValueError("dispatch_generation must be a positive integer")
        if self.dispatch_id is not None and not _DIGEST.fullmatch(self.dispatch_id):
            raise ValueError("dispatch_id must be a lowercase SHA-256 digest")
        if self.dispatch_id is not None and self.dispatch_generation is None:
            raise ValueError("dispatch_id requires a dispatch_generation")
        if self.dispatch_id is not None:
            assert self.dispatch_generation is not None
            request_id = self.request.get("request_id")
            if not isinstance(request_id, str):
                raise ValueError("dispatch_id requires a valid request_id")
            try:
                expected_dispatch_id = queue_dispatch_id(request_id, self.dispatch_generation)
            except ValueError:
                raise ValueError("dispatch_id requires a valid request_id") from None
            if self.dispatch_id != expected_dispatch_id:
                raise ValueError("dispatch_id does not derive from request_id and dispatch_generation")


@dataclass(frozen=True, slots=True)
class EmissionResult:
    """What one emission run wrote, for the caller's checkpoint decision."""

    candidate_ids: tuple[str, ...] = ()
    created_candidates: tuple[str, ...] = ()
    reused_candidates: tuple[str, ...] = ()
    created_deliveries: tuple[str, ...] = ()
    repaired_deliveries: tuple[str, ...] = field(default=())


class OutboxStore(Protocol):
    """Conditional writes for the candidate and delivery items.

    `put_*_if_absent` returns `False` when the item already exists, which is how
    a repeated watcher invocation stays idempotent instead of overwriting the
    original evidence, release, and creation time.

    The dispatch operations are the same boundary the watcher uses, because
    chapter 02 makes the delivery table one system of record: `query_due` reads
    `status-next-action-index`, and `claim_dispatch` / `mark_queued` are the
    conditional transitions that serialize concurrent dispatchers and workers.
    The conditions each store implements are the contract, not an optimization.
    """

    def get_candidate(self, candidate: str) -> Mapping[str, Any] | None: ...

    def put_candidate_if_absent(self, candidate: Mapping[str, Any]) -> bool: ...

    def get_delivery(self, candidate: str) -> DeliveryRecord | None: ...

    def put_delivery_if_absent(self, record: DeliveryRecord) -> bool: ...

    def query_due(self, status: str, *, due_before: int, limit: int) -> Sequence[tuple[int, str]]:
        """Return `(next_action_at, candidate_id)` for records due at or before `due_before`.

        Ordered by `next_action_at` ascending, matching the GSI range key, so
        the oldest scheduled work dispatches first. The dispatcher merge-sorts
        results from both scheduled statuses to produce one oldest-first order.
        """
        ...

    def claim_dispatch(
        self,
        candidate: str,
        *,
        expected_state_version: int,
        expected_generation: int | None,
        request_id: str,
        due_before: int,
    ) -> tuple[int, str] | None:
        """Claim the next generation for the exact due record the caller read.

        Succeeds only when the record is still dispatchable, still due, and has
        no active claim, and its state version and prior generation still equal
        the caller's observation. The store derives the next generation and ID,
        writes the claim plus a state-version bump, and returns both values.
        """
        ...

    def mark_queued(self, candidate: str, *, dispatch_id: str, message_id: str, at: int) -> bool:
        """Move a claimed record to `queued` with the SQS message ID.

        Succeeds only while the record is dispatchable, due, and carries exactly
        `dispatch_id`, so a stale dispatcher cannot overwrite a transition a
        newer claim already made.
        """
        ...


class InMemoryOutboxStore:
    """Candidate and delivery items held in memory, for tests and dry runs.

    Stored documents are deep-copied on the way in and out. A store that handed
    back a live reference would let a caller mutate a "durable" candidate, which
    is exactly the immutability the conditional put exists to provide.
    """

    def __init__(self) -> None:
        self._candidates: dict[str, str] = {}
        self._deliveries: dict[str, DeliveryRecord] = {}

    def get_candidate(self, candidate: str) -> Mapping[str, Any] | None:
        stored = self._candidates.get(candidate)
        return None if stored is None else json.loads(stored)

    def put_candidate_if_absent(self, candidate: Mapping[str, Any]) -> bool:
        key = candidate["candidate_id"]
        if key in self._candidates:
            return False
        self._candidates[key] = json.dumps(candidate, sort_keys=True)
        return True

    def get_delivery(self, candidate: str) -> DeliveryRecord | None:
        return self._deliveries.get(candidate)

    def put_delivery_if_absent(self, record: DeliveryRecord) -> bool:
        if record.candidate_id in self._deliveries:
            return False
        self._deliveries[record.candidate_id] = record
        return True

    def query_due(self, status: str, *, due_before: int, limit: int) -> tuple[tuple[int, str], ...]:
        due = [
            (record.next_action_at, record.candidate_id)
            for record in self._deliveries.values()
            if record.status == status and record.next_action_at is not None and record.next_action_at <= due_before
        ]
        due.sort(key=lambda entry: entry[0])
        return tuple(due[:limit])

    def claim_dispatch(
        self,
        candidate: str,
        *,
        expected_state_version: int,
        expected_generation: int | None,
        request_id: str,
        due_before: int,
    ) -> tuple[int, str] | None:
        record = self._deliveries.get(candidate)
        if record is None or record.status not in SCHEDULED_STATES or record.dispatch_id is not None:
            return None
        if record.next_action_at is None or record.next_action_at > due_before:
            return None
        if record.state_version != expected_state_version or record.dispatch_generation != expected_generation:
            return None
        if record.request.get("request_id") != request_id:
            return None
        generation = (expected_generation or 0) + 1
        dispatch_id = queue_dispatch_id(request_id, generation)
        self._deliveries[candidate] = replace(
            record,
            dispatch_generation=generation,
            dispatch_id=dispatch_id,
            state_version=record.state_version + 1,
        )
        return generation, dispatch_id

    def mark_queued(self, candidate: str, *, dispatch_id: str, message_id: str, at: int) -> bool:
        record = self._deliveries.get(candidate)
        if record is None or record.status not in SCHEDULED_STATES or record.dispatch_id != dispatch_id:
            return False
        if record.next_action_at is None or record.next_action_at > at:
            return False
        self._deliveries[candidate] = replace(
            record,
            status="queued",
            queue_message_id=message_id,
            next_action_at=at,
            state_version=record.state_version + 1,
        )
        return True


class DynamoDBDeliveryStore:
    """`OutboxStore` backed by the delivery table chapter 02 defines.

    Items use the `CANDIDATE#<id>` / `DELIVERY` keys and carry the GSI's
    `status` and numeric `next_action_at` attributes. `request` and candidate
    documents are stored as JSON strings rather than nested maps, because
    DynamoDB rejects empty string values anywhere in an item and announcement
    summaries can legitimately be empty.

    The state transitions are conditional expressions, not application reads
    plus writes. That is what makes the delivery table the system of record
    under concurrency: `claim_dispatch` and `mark_queued` either mutate one
    item atomically or fail, and two dispatchers or a dispatcher and a worker
    cannot both believe they hold the same transition.
    """

    def __init__(self, client: Any, table_name: str, index_name: str = "status-next-action-index") -> None:
        self._client = client
        self._table = table_name
        self._index = index_name

    def _key(self, candidate: str) -> dict[str, dict[str, str]]:
        return {"PK": {"S": f"{CANDIDATE_KEY_PREFIX}{candidate}"}, "SK": {"S": DELIVERY_SORT_KEY}}

    @staticmethod
    def _candidate_key(candidate: str) -> dict[str, dict[str, str]]:
        return {"PK": {"S": f"{CANDIDATE_KEY_PREFIX}{candidate}"}, "SK": {"S": "CANDIDATE"}}

    def _encode_delivery(self, record: DeliveryRecord) -> dict[str, dict[str, object]]:
        item: dict[str, dict[str, object]] = {
            "PK": {"S": f"{CANDIDATE_KEY_PREFIX}{record.candidate_id}"},
            "SK": {"S": DELIVERY_SORT_KEY},
            "status": {"S": record.status},
            "state_version": {"N": str(record.state_version)},
            "destination_key": {"S": record.destination_key},
            "request": {"S": json.dumps(record.request, sort_keys=True)},
            "created_at": {"S": record.created_at},
        }
        if record.next_action_at is not None:
            item["next_action_at"] = {"N": str(record.next_action_at)}
        if record.dispatch_generation is not None:
            item["dispatch_generation"] = {"N": str(record.dispatch_generation)}
        if record.dispatch_id is not None:
            item["dispatch_id"] = {"S": record.dispatch_id}
        if record.queue_message_id is not None:
            item["queue_message_id"] = {"S": record.queue_message_id}
        return item

    def _decode_delivery(self, item: Mapping[str, Any]) -> DeliveryRecord:
        return DeliveryRecord(
            candidate_id=item["PK"]["S"][len(CANDIDATE_KEY_PREFIX) :],
            destination_key=item["destination_key"]["S"],
            request=json.loads(item["request"]["S"]),
            next_action_at=int(item["next_action_at"]["N"]) if "next_action_at" in item else None,
            status=item["status"]["S"],
            state_version=int(item["state_version"]["N"]),
            created_at=item["created_at"]["S"],
            dispatch_generation=int(item["dispatch_generation"]["N"]) if "dispatch_generation" in item else None,
            dispatch_id=item["dispatch_id"]["S"] if "dispatch_id" in item else None,
            queue_message_id=item["queue_message_id"]["S"] if "queue_message_id" in item else None,
        )

    def get_candidate(self, candidate: str) -> Mapping[str, Any] | None:
        response = self._client.get_item(TableName=self._table, Key=self._candidate_key(candidate))
        stored = response.get("Item")
        if stored is None:
            return None
        document = json.loads(stored["document"]["S"])
        if not isinstance(document, Mapping):
            raise CandidateIdentityError(f"stored candidate {candidate} is not an object")
        return document

    def put_candidate_if_absent(self, candidate: Mapping[str, Any]) -> bool:
        from botocore.exceptions import ClientError

        item = {
            **self._candidate_key(candidate["candidate_id"]),
            "document": {"S": json.dumps(candidate, sort_keys=True)},
        }
        try:
            self._client.put_item(TableName=self._table, Item=item, ConditionExpression="attribute_not_exists(PK)")
        except ClientError as error:
            if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise
        return True

    def get_delivery(self, candidate: str) -> DeliveryRecord | None:
        response = self._client.get_item(TableName=self._table, Key=self._key(candidate))
        stored = response.get("Item")
        if stored is None:
            return None
        return self._decode_delivery(stored)

    def put_delivery_if_absent(self, record: DeliveryRecord) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._client.put_item(
                TableName=self._table,
                Item=self._encode_delivery(record),
                ConditionExpression="attribute_not_exists(PK)",
            )
        except ClientError as error:
            if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise
        return True

    def query_due(self, status: str, *, due_before: int, limit: int) -> tuple[tuple[int, str], ...]:
        response = self._client.query(
            TableName=self._table,
            IndexName=self._index,
            KeyConditionExpression="#status = :status AND next_action_at <= :due",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":status": {"S": status},
                ":due": {"N": str(due_before)},
            },
            Limit=limit,
            ProjectionExpression="PK, next_action_at",
        )
        prefix = len(CANDIDATE_KEY_PREFIX)
        return tuple((int(item["next_action_at"]["N"]), item["PK"]["S"][prefix:]) for item in response["Items"])

    def claim_dispatch(
        self,
        candidate: str,
        *,
        expected_state_version: int,
        expected_generation: int | None,
        request_id: str,
        due_before: int,
    ) -> tuple[int, str] | None:
        from botocore.exceptions import ClientError

        generation = (expected_generation or 0) + 1
        dispatch_id = queue_dispatch_id(request_id, generation)
        condition = (
            "#status IN (:pending, :retryable) AND next_action_at <= :due "
            "AND attribute_not_exists(dispatch_id) AND state_version = :expected_state_version"
        )
        values: dict[str, dict[str, str]] = {
            ":pending": {"S": "pending_queue"},
            ":retryable": {"S": "failed_retryable"},
            ":due": {"N": str(due_before)},
            ":generation": {"N": str(generation)},
            ":dispatch_id": {"S": dispatch_id},
            ":expected_state_version": {"N": str(expected_state_version)},
            ":one": {"N": "1"},
        }
        if expected_generation is None:
            condition += " AND attribute_not_exists(dispatch_generation)"
        else:
            condition += " AND dispatch_generation = :expected_generation"
            values[":expected_generation"] = {"N": str(expected_generation)}

        try:
            self._client.update_item(
                TableName=self._table,
                Key=self._key(candidate),
                UpdateExpression=(
                    "SET dispatch_generation = :generation, dispatch_id = :dispatch_id, "
                    "state_version = state_version + :one"
                ),
                ConditionExpression=condition,
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues=values,
            )
        except ClientError as error:
            if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return None
            raise
        return generation, dispatch_id

    def mark_queued(self, candidate: str, *, dispatch_id: str, message_id: str, at: int) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._client.update_item(
                TableName=self._table,
                Key=self._key(candidate),
                UpdateExpression=(
                    "SET #status = :queued, queue_message_id = :message_id, "
                    "next_action_at = :at, state_version = state_version + :one"
                ),
                ConditionExpression=(
                    "#status IN (:pending, :retryable) AND next_action_at <= :at AND dispatch_id = :dispatch_id"
                ),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":pending": {"S": "pending_queue"},
                    ":retryable": {"S": "failed_retryable"},
                    ":queued": {"S": "queued"},
                    ":at": {"N": str(at)},
                    ":dispatch_id": {"S": dispatch_id},
                    ":message_id": {"S": message_id},
                    ":one": {"N": "1"},
                },
            )
        except ClientError as error:
            if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise
        return True


def _assert_identity(candidate: Mapping[str, Any]) -> None:
    """Recompute the candidate's identity from its own fields.

    Chapter 04 treats an item whose identity fields do not match its key as a
    correctness failure. Recomputing here rather than trusting the stored digest
    is what makes that check real: a document whose route or environment set was
    edited in place keeps its old key and would otherwise pass unnoticed.
    """

    expected_audience = audience_fingerprint(candidate["environment_ids"])
    if candidate["audience_fingerprint"] != expected_audience:
        raise CandidateIdentityError(
            f"stored candidate {candidate['candidate_id']} has an audience fingerprint "
            "that disagrees with its environment IDs"
        )
    expected = candidate_id(
        candidate["announcement"]["revision_id"],
        candidate["service"]["id"],
        candidate["risk"]["risk_type"],
        candidate["route_id"],
        candidate["audience_fingerprint"],
    )
    if candidate["candidate_id"] != expected:
        raise CandidateIdentityError(f"stored candidate {candidate['candidate_id']} does not match its identity fields")


def serialized_size(value: Mapping[str, Any]) -> int:
    """Return the contract's compact UTF-8 JSON byte count."""

    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _byte_limit(message_policy: Mapping[str, Any], field: str) -> int:
    value = message_policy.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"message policy {field} must be a positive integer")
    return value


def _require_within_limit(label: str, value: Mapping[str, Any], maximum: int) -> None:
    actual = serialized_size(value)
    if actual > maximum:
        raise OversizeDeliveryError(f"{label} is {actual} UTF-8 JSON bytes; maximum is {maximum}")


def build_delivery_request(
    candidate: Mapping[str, Any],
    destination_key: str,
    created_at: datetime,
) -> dict[str, Any]:
    """Build the `DeliveryRequest` that embeds this exact candidate.

    Chapter 04 embeds the candidate so the dispatcher cannot construct a
    different payload than the one persisted.
    """

    return {
        "contract_version": CONTRACT_VERSION,
        "request_id": delivery_request_id(candidate["candidate_id"]),
        "candidate": json.loads(json.dumps(candidate)),
        "destination_key": destination_key,
        "created_at": utc_timestamp(created_at),
    }


def _destination_key(inventory: Mapping[str, Any], route_id: str) -> str:
    routes = inventory["slack"]["routes"]
    route = routes.get(route_id)
    if route is None:
        raise ValueError(f"candidate names unknown route {route_id}")
    destination_key: str = route["destination_key"]
    return destination_key


def emit(
    store: OutboxStore,
    candidates: Sequence[Mapping[str, Any]],
    *,
    inventory: Mapping[str, Any],
    message_policy: Mapping[str, Any],
    created_at: datetime,
) -> EmissionResult:
    """Make each candidate and its `pending_queue` delivery record durable.

    Safe to repeat. Candidate and request byte ceilings are checked before a
    new candidate write. A candidate that already exists is loaded and validated
    rather than replaced, and a missing delivery record is repaired from the
    stored candidate's request rather than from the current release.

    Raises `CandidateIdentityError` when a stored item disagrees with its key,
    which chapter 04 requires to block checkpoint advancement.
    """

    max_candidate_bytes = _byte_limit(message_policy, "max_candidate_bytes")
    max_delivery_request_bytes = _byte_limit(message_policy, "max_delivery_request_bytes")
    created_timestamp = utc_timestamp(created_at)
    initial_action_at = int(created_at.timestamp())

    candidate_ids: list[str] = []
    created_candidates: list[str] = []
    reused_candidates: list[str] = []
    created_deliveries: list[str] = []
    repaired_deliveries: list[str] = []

    for candidate in candidates:
        key = candidate["candidate_id"]
        _assert_identity(candidate)
        candidate_ids.append(key)
        created_new = False
        destination_key: str | None = None
        request: Mapping[str, Any] | None = None

        stored = store.get_candidate(key)
        if stored is None:
            # A request is inseparable from a newly durable candidate. Check
            # both limits before the conditional candidate write so an item
            # that can never be queued is not left behind for repair.
            _require_within_limit("alert candidate", candidate, max_candidate_bytes)
            destination_key = _destination_key(inventory, candidate["route_id"])
            request = build_delivery_request(candidate, destination_key, created_at)
            _require_within_limit("delivery request", request, max_delivery_request_bytes)
            if store.put_candidate_if_absent(candidate):
                created_candidates.append(key)
                durable = candidate
                created_new = True
            else:
                # Another writer won after the read. Load its immutable value
                # and follow the same replay path as an item found initially.
                stored = store.get_candidate(key)
                if stored is None:
                    raise CandidateIdentityError(f"candidate {key} reported present but could not be loaded")
                _assert_identity(stored)
                _require_within_limit("stored alert candidate", stored, max_candidate_bytes)
                reused_candidates.append(key)
                durable = stored
        else:
            # Chapter 04: load and validate the stored candidate rather than
            # replacing it. Its evidence, release, and creation time are
            # immutable once written.
            _assert_identity(stored)
            _require_within_limit("stored alert candidate", stored, max_candidate_bytes)
            reused_candidates.append(key)
            durable = stored

        if store.get_delivery(key) is not None:
            continue

        # Repairing a missing record reuses the durable candidate, so a replay
        # under a newer release cannot change the payload already committed.
        if not created_new:
            destination_key = _destination_key(inventory, durable["route_id"])
            request = build_delivery_request(durable, destination_key, created_at)
            _require_within_limit("stored delivery request", request, max_delivery_request_bytes)
        assert destination_key is not None
        assert request is not None
        record = DeliveryRecord(
            candidate_id=key,
            destination_key=destination_key,
            request=request,
            created_at=created_timestamp,
            next_action_at=initial_action_at,
        )
        if store.put_delivery_if_absent(record):
            if key in reused_candidates:
                repaired_deliveries.append(key)
            else:
                created_deliveries.append(key)

    return EmissionResult(
        candidate_ids=tuple(candidate_ids),
        created_candidates=tuple(created_candidates),
        reused_candidates=tuple(reused_candidates),
        created_deliveries=tuple(created_deliveries),
        repaired_deliveries=tuple(repaired_deliveries),
    )


def verify_durable(store: OutboxStore, candidate_ids: Iterable[str]) -> bool:
    """Whether every candidate ID has both records, read back from the store.

    Chapter 04 gates checkpoint advancement on this. It reads rather than
    trusting an `EmissionResult`, because the point of the gate is to catch a
    write that did not survive.
    """

    for key in candidate_ids:
        if store.get_candidate(key) is None:
            return False
        record = store.get_delivery(key)
        if record is None or record.status not in DELIVERY_STATES:
            return False
    return True
