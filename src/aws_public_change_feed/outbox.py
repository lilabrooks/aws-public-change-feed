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
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Protocol

from .candidates import CONTRACT_VERSION, utc_timestamp
from .identity import audience_fingerprint, candidate_id, delivery_request_id, queue_dispatch_id

__all__ = [
    "CANDIDATE_KEY_PREFIX",
    "DESTINATION_KEY_PREFIX",
    "DELIVERY_STATES",
    "CandidateIdentityError",
    "DeliveryPace",
    "DeliveryRecord",
    "DynamoDBDeliveryStore",
    "EmissionResult",
    "InMemoryOutboxStore",
    "OutboxStore",
    "ACKNOWLEDGED_STATES",
    "OversizeDeliveryError",
    "SCHEDULED_STATES",
    "SCHEDULED_STATES_ORDERED",
    "TTL_ELIGIBLE_STATES",
    "build_delivery_request",
    "emit",
    "serialized_size",
    "verify_durable",
]

# A dispatch ID is `queue_dispatch_id` output, a lowercase SHA-256 digest.
_DIGEST = re.compile(r"[a-f0-9]{64}")

# Chapter 02 keys the candidate and delivery items under one partition, and the
# destination pacing item under its own.
CANDIDATE_KEY_PREFIX = "CANDIDATE#"
DELIVERY_SORT_KEY = "DELIVERY"
DESTINATION_KEY_PREFIX = "DESTINATION#"
PACE_SORT_KEY = "PACE"

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

# Two distinct sets, and collapsing them into one is a correctness bug.
#
# `ACKNOWLEDGED_STATES` is "no further delivery work is scheduled": a queue
# message for one of these is finished, whoever recorded it.
#
# `TTL_ELIGIBLE_STATES` is the strictly smaller set chapter 02 permits to
# expire — "`posted` and `failed_terminal` may expire after the configured
# terminal retention". `delivery_unknown` is acknowledged but must never
# expire: ADR-004 requires an operator to inspect Slack and record a manual
# replay decision, and a TTL would delete the evidence that review depends on.
#
# The distinction is load-bearing twice over. `DeliveryRecord` refuses
# `expires_at` outside the TTL-eligible set, and `put_delivery_if_absent`
# replaces an expired item only within it — because an expired TTL is only
# proof that a record may be discarded if a TTL could legitimately be there in
# the first place. Admitting `delivery_unknown` to that set let a replay
# silently overwrite an unknown outcome awaiting review.
ACKNOWLEDGED_STATES = frozenset({"posted", "failed_terminal", "delivery_unknown"})
TTL_ELIGIBLE_STATES = frozenset({"posted", "failed_terminal"})


def validate_delivery_transition(
    *,
    status: str,
    next_action_at: int | None,
    attempt_id: str | None = None,
    lease_expires_at: int | None = None,
    network_attempt_count: int = 0,
    slack_response: Mapping[str, Any] | None = None,
    expires_at: int | None = None,
) -> None:
    """Refuse a delivery transition that would produce an unreadable record.

    `InMemoryOutboxStore` gets this for free: every transition goes through
    `dataclasses.replace`, so `DeliveryRecord.__post_init__` sees the complete
    proposed record. `DynamoDBDeliveryStore` builds update expressions
    attribute by attribute and never constructs one, so it had no validation
    beyond a status/TTL pairing — it would write an unknown status, a
    `failed_retryable` with no `next_action_at`, a `sending` with no lease, or
    a negative attempt count, each of which its own decoder then refused on
    the next read. A store that can write what it cannot read can strand a
    record where no code path repairs it.

    The proposed record is assembled here rather than read back from the
    table, so this costs no extra request. Identity and payload fields get
    placeholders because the transition never touches them and no invariant
    below relates them to the fields it does touch; every rule being checked
    is a relation among exactly these arguments.
    """

    DeliveryRecord(
        candidate_id=_TRANSITION_PROBE_ID,
        destination_key=_TRANSITION_PROBE_ID,
        request={},
        next_action_at=next_action_at,
        status=status,
        attempt_id=attempt_id,
        lease_expires_at=lease_expires_at,
        network_attempt_count=network_attempt_count,
        slack_response=slack_response,
        expires_at=expires_at,
    )


# A placeholder for the fields `validate_delivery_transition` does not check.
# Nonempty so it cannot trip an unrelated emptiness rule, and obviously not a
# real candidate ID so it can never be mistaken for one in a traceback.
_TRANSITION_PROBE_ID = "transition-probe"


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
    # The worker claim chapter 02 names "sending with a lease and attempt ID".
    # `attempt_id` and `lease_expires_at` exist only on `sending` records, and
    # the worker's outcome write removes them. `network_attempt_count` counts
    # Slack network calls, which stays separate from the SQS receive count:
    # redeliveries that never call Slack must not exhaust a Slack retry budget.
    attempt_id: str | None = None
    lease_expires_at: int | None = None
    network_attempt_count: int = 0
    # Bounded Slack response metadata for diagnosis: the worker's derived
    # response class, latency, whether request bytes went out, the HTTP status
    # when one arrived, a bounded `Retry-After`, and in bot mode the returned
    # message timestamp. Never a secret, never a response body, and never the
    # webhook URL.
    slack_response: Mapping[str, Any] | None = None
    # TTL in whole Unix epoch seconds, valid only on a state in
    # `TTL_ELIGIBLE_STATES`. `__post_init__` enforces that, and
    # `put_delivery_if_absent` depends on it: an expired TTL is what proves an
    # item may be replaced. `delivery_unknown` is deliberately excluded — it is
    # acknowledged but awaits operator review, so it never expires.
    expires_at: int | None = None

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
        if self.status == "sending":
            if self.attempt_id is None or self.lease_expires_at is None:
                raise ValueError("sending delivery records require attempt_id and lease_expires_at")
        else:
            if self.attempt_id is not None:
                raise ValueError("attempt_id is only valid on sending delivery records")
            if self.lease_expires_at is not None:
                raise ValueError("lease_expires_at is only valid on sending delivery records")
        if self.attempt_id is not None and (not isinstance(self.attempt_id, str) or not self.attempt_id):
            raise ValueError("attempt_id must be a nonempty string")
        if self.lease_expires_at is not None and (
            isinstance(self.lease_expires_at, bool)
            or not isinstance(self.lease_expires_at, int)
            or self.lease_expires_at < 0
        ):
            raise ValueError("lease_expires_at must be a non-negative integer Unix timestamp")
        if isinstance(self.network_attempt_count, bool) or not isinstance(self.network_attempt_count, int):
            raise ValueError("network_attempt_count must be an integer")
        if self.network_attempt_count < 0:
            raise ValueError("network_attempt_count cannot be negative")
        if self.slack_response is not None and not isinstance(self.slack_response, Mapping):
            raise ValueError("slack_response must be an object")
        if self.expires_at is not None:
            if isinstance(self.expires_at, bool) or not isinstance(self.expires_at, int) or self.expires_at < 0:
                raise ValueError("expires_at must be a non-negative integer Unix timestamp")
            # Chapter 02 lets only `posted` and `failed_terminal` expire, and
            # ADR-004 keeps `delivery_unknown` for operator review.
            # `put_delivery_if_absent` treats an expired TTL as proof a record
            # may be replaced, so a TTL anywhere else makes that unsound.
            if self.status not in TTL_ELIGIBLE_STATES:
                raise ValueError(f"expires_at is only valid on {sorted(TTL_ELIGIBLE_STATES)}, not {self.status}")


@dataclass(frozen=True, slots=True)
class DeliveryPace:
    """The destination pacing item chapter 02 keys as `DESTINATION#<key>` / `PACE`.

    `next_allowed_at` is the whole-epoch second before which the worker must
    not make another Slack call for this destination. `last_response_class` is
    the reviewed bounded class of the most recent Slack response, and `version`
    is a monotonic counter the conditional pacing update writes against, which
    is what serializes two workers updating one destination.
    """

    destination_key: str
    next_allowed_at: int
    last_response_class: str | None = None
    version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.destination_key, str) or not self.destination_key:
            raise ValueError("destination_key must be a nonempty string")
        if isinstance(self.next_allowed_at, bool) or not isinstance(self.next_allowed_at, int):
            raise ValueError("next_allowed_at must be an integer Unix timestamp")
        if self.next_allowed_at < 0:
            raise ValueError("next_allowed_at cannot be negative")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise ValueError("pace version must be a positive integer")


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

    def put_delivery_if_absent(self, record: DeliveryRecord, *, now: int | None = None) -> bool:
        """Write the record unless a live item occupies the key.

        Returns `False` when the item already exists and is not an expired
        terminal item. Chapter 06 lets a new put over an expired terminal item
        prove its TTL is in the past and replace it, because DynamoDB TTL
        deletion is asynchronous. `now` is the epoch clock for that proof;
        callers that do not pass one use the current time.
        """
        ...

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

    def get_pace(self, destination_key: str) -> DeliveryPace | None:
        """Read the destination pacing item, or `None` when no call was made yet."""
        ...

    def update_pace(
        self,
        destination_key: str,
        *,
        expected_version: int | None,
        next_allowed_at: int,
        last_response_class: str | None,
    ) -> bool:
        """Advance the destination's pacing, conditional on its version.

        `expected_version = None` creates the item and succeeds only while it
        does not exist; an integer requires the observed version so a lost
        pacing race cannot overwrite a newer decision. Succeeds atomically or
        not at all, which is what serializes workers updating one destination.
        """
        ...

    def claim_sending(
        self,
        candidate: str,
        *,
        expected_state_version: int,
        attempt_id: str,
        lease_expires_at: int,
    ) -> bool:
        """Move a `queued` record to `sending` with a lease and attempt ID.

        Succeeds only while the record is still `queued` at the observed state
        version, so two workers cannot both believe they hold the same claim.
        `next_action_at` is set to `lease_expires_at` so the status index can
        surface expired leases to the reconciler.
        """
        ...

    def record_outcome(
        self,
        candidate: str,
        *,
        expected_state_version: int,
        attempt_id: str,
        status: str,
        network_attempt_count: int,
        next_action_at: int | None,
        slack_response: Mapping[str, Any] | None,
        expires_at: int | None,
    ) -> bool:
        """Record the documented outcome of a held `sending` claim.

        Succeeds only while the record is `sending`, at the observed state
        version, and still carries `attempt_id`, so a worker whose lease was
        superseded cannot overwrite a newer transition. Clears the lease and the
        active dispatch claim and writes the outcome's `next_action_at`,
        `expires_at`, attempt counter, and bounded response metadata. Clearing
        `dispatch_id` on a recorded retry is what makes the next due dispatch
        increment the generation for a fresh dispatch ID (chapter 02 and
        ADR-007).
        """
        ...

    def schedule_retry(
        self,
        candidate: str,
        *,
        expected_state_version: int,
        next_action_at: int,
        slack_response: Mapping[str, Any] | None,
    ) -> bool:
        """Defer a `queued` record to `failed_retryable` without a network call.

        Used when destination pacing forbids a call now. Clears the active
        dispatch claim so the next due dispatch increments the generation for a
        fresh dispatch ID, exactly as chapter 02 requires for a worker-scheduled
        future retry. Succeeds only while the record is still `queued` at the
        observed version.
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
        self._paces: dict[str, DeliveryPace] = {}

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

    def put_delivery_if_absent(self, record: DeliveryRecord, *, now: int | None = None) -> bool:
        existing = self._deliveries.get(record.candidate_id)
        if existing is not None:
            timestamp = int(time.time()) if now is None else now
            if existing.expires_at is None or existing.expires_at >= timestamp:
                return False
            # Mirrors the DynamoDB condition. `DeliveryRecord` already refuses
            # a TTL outside the eligible states, so this is the second of two
            # independent guards rather than the only one.
            if existing.status not in TTL_ELIGIBLE_STATES:
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

    def get_pace(self, destination_key: str) -> DeliveryPace | None:
        return self._paces.get(destination_key)

    def update_pace(
        self,
        destination_key: str,
        *,
        expected_version: int | None,
        next_allowed_at: int,
        last_response_class: str | None,
    ) -> bool:
        existing = self._paces.get(destination_key)
        if expected_version is None:
            if existing is not None:
                return False
            version = 1
        else:
            if existing is None or existing.version != expected_version:
                return False
            version = expected_version + 1
        self._paces[destination_key] = DeliveryPace(
            destination_key=destination_key,
            next_allowed_at=next_allowed_at,
            last_response_class=last_response_class,
            version=version,
        )
        return True

    def claim_sending(
        self,
        candidate: str,
        *,
        expected_state_version: int,
        attempt_id: str,
        lease_expires_at: int,
    ) -> bool:
        record = self._deliveries.get(candidate)
        if record is None or record.status != "queued" or record.state_version != expected_state_version:
            return False
        self._deliveries[candidate] = replace(
            record,
            status="sending",
            attempt_id=attempt_id,
            lease_expires_at=lease_expires_at,
            next_action_at=lease_expires_at,
            state_version=record.state_version + 1,
        )
        return True

    def record_outcome(
        self,
        candidate: str,
        *,
        expected_state_version: int,
        attempt_id: str,
        status: str,
        network_attempt_count: int,
        next_action_at: int | None,
        slack_response: Mapping[str, Any] | None,
        expires_at: int | None,
    ) -> bool:
        record = self._deliveries.get(candidate)
        if (
            record is None
            or record.status != "sending"
            or record.state_version != expected_state_version
            or record.attempt_id != attempt_id
        ):
            return False
        self._deliveries[candidate] = replace(
            record,
            status=status,
            attempt_id=None,
            lease_expires_at=None,
            network_attempt_count=network_attempt_count,
            next_action_at=next_action_at,
            slack_response=slack_response,
            expires_at=expires_at,
            dispatch_id=None,
            state_version=record.state_version + 1,
        )
        return True

    def schedule_retry(
        self,
        candidate: str,
        *,
        expected_state_version: int,
        next_action_at: int,
        slack_response: Mapping[str, Any] | None,
    ) -> bool:
        record = self._deliveries.get(candidate)
        if record is None or record.status != "queued" or record.state_version != expected_state_version:
            return False
        self._deliveries[candidate] = replace(
            record,
            status="failed_retryable",
            dispatch_id=None,
            next_action_at=next_action_at,
            slack_response=slack_response,
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

    @staticmethod
    def _pace_key(destination_key: str) -> dict[str, dict[str, str]]:
        return {"PK": {"S": f"{DESTINATION_KEY_PREFIX}{destination_key}"}, "SK": {"S": PACE_SORT_KEY}}

    def _encode_delivery(self, record: DeliveryRecord) -> dict[str, dict[str, object]]:
        item: dict[str, dict[str, object]] = {
            "PK": {"S": f"{CANDIDATE_KEY_PREFIX}{record.candidate_id}"},
            "SK": {"S": DELIVERY_SORT_KEY},
            "status": {"S": record.status},
            "state_version": {"N": str(record.state_version)},
            "destination_key": {"S": record.destination_key},
            "request": {"S": json.dumps(record.request, sort_keys=True)},
            "created_at": {"S": record.created_at},
            "network_attempt_count": {"N": str(record.network_attempt_count)},
        }
        if record.next_action_at is not None:
            item["next_action_at"] = {"N": str(record.next_action_at)}
        if record.dispatch_generation is not None:
            item["dispatch_generation"] = {"N": str(record.dispatch_generation)}
        if record.dispatch_id is not None:
            item["dispatch_id"] = {"S": record.dispatch_id}
        if record.queue_message_id is not None:
            item["queue_message_id"] = {"S": record.queue_message_id}
        if record.attempt_id is not None:
            item["attempt_id"] = {"S": record.attempt_id}
        if record.lease_expires_at is not None:
            item["lease_expires_at"] = {"N": str(record.lease_expires_at)}
        if record.slack_response is not None:
            item["slack_response"] = {"S": json.dumps(record.slack_response, sort_keys=True)}
        if record.expires_at is not None:
            item["expires_at"] = {"N": str(record.expires_at)}
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
            attempt_id=item["attempt_id"]["S"] if "attempt_id" in item else None,
            lease_expires_at=int(item["lease_expires_at"]["N"]) if "lease_expires_at" in item else None,
            network_attempt_count=(int(item["network_attempt_count"]["N"]) if "network_attempt_count" in item else 0),
            slack_response=json.loads(item["slack_response"]["S"]) if "slack_response" in item else None,
            expires_at=int(item["expires_at"]["N"]) if "expires_at" in item else None,
        )

    def get_candidate(self, candidate: str) -> Mapping[str, Any] | None:
        # Strongly consistent: `emit` reads this to decide whether a candidate
        # is already durable, and `verify_durable` reads it to gate checkpoint
        # advancement. An eventually consistent miss right after a write would
        # either duplicate the write or hold back a checkpoint that is in fact
        # safe to advance.
        response = self._client.get_item(TableName=self._table, Key=self._candidate_key(candidate), ConsistentRead=True)
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
        # Strongly consistent: every worker and dispatcher decision is taken
        # against this record and then written back conditionally on the state
        # version it reports. A stale read makes the worker reason about
        # superseded state, and a stale miss is worse still — the worker treats
        # an absent record as "no delivery work" and acknowledges the message,
        # discarding work that is durable.
        response = self._client.get_item(TableName=self._table, Key=self._key(candidate), ConsistentRead=True)
        stored = response.get("Item")
        if stored is None:
            return None
        return self._decode_delivery(stored)

    def put_delivery_if_absent(self, record: DeliveryRecord, *, now: int | None = None) -> bool:
        from botocore.exceptions import ClientError

        # Chapter 06: a new put over an expired `posted` or `failed_terminal`
        # item proves the TTL is in the past, because DynamoDB TTL deletion is
        # asynchronous. A live item stays put, and so does any item outside
        # those two states that carries a TTL it should never have had. The
        # status clause is the guard: it keeps a corrupt `expires_at` on live
        # `pending_queue`, `queued`, `sending`, or `failed_retryable` work from
        # satisfying this condition, and it keeps a `delivery_unknown` awaiting
        # operator review from being replaced by a replay.
        try:
            self._client.put_item(
                TableName=self._table,
                Item=self._encode_delivery(record),
                ConditionExpression=(
                    "attribute_not_exists(PK) OR (attribute_exists(expires_at) AND expires_at < :now "
                    "AND #status IN (:posted, :terminal))"
                ),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":now": {"N": str(int(time.time()) if now is None else now)},
                    ":posted": {"S": "posted"},
                    ":terminal": {"S": "failed_terminal"},
                },
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

    def get_pace(self, destination_key: str) -> DeliveryPace | None:
        # Strongly consistent: ADR-015's rate control is only as good as this
        # read. A stale `next_allowed_at` lets a worker call Slack inside an
        # interval another worker already reserved, which is the exact thing
        # destination pacing exists to prevent.
        response = self._client.get_item(
            TableName=self._table, Key=self._pace_key(destination_key), ConsistentRead=True
        )
        stored = response.get("Item")
        if stored is None:
            return None
        return DeliveryPace(
            destination_key=destination_key,
            next_allowed_at=int(stored["next_allowed_at"]["N"]),
            last_response_class=stored["last_response_class"]["S"] if "last_response_class" in stored else None,
            version=int(stored["version"]["N"]),
        )

    def update_pace(
        self,
        destination_key: str,
        *,
        expected_version: int | None,
        next_allowed_at: int,
        last_response_class: str | None,
    ) -> bool:
        from botocore.exceptions import ClientError

        # `DeliveryPace` validates a pacing item the same way `DeliveryRecord`
        # validates a delivery one, and the in-memory store gets that by
        # constructing one. This store writes attributes directly, so it
        # constructs the proposed item purely to have it checked.
        DeliveryPace(
            destination_key=destination_key,
            next_allowed_at=next_allowed_at,
            last_response_class=last_response_class,
            version=1 if expected_version is None else expected_version + 1,
        )

        values: dict[str, dict[str, object]] = {
            ":next": {"N": str(next_allowed_at)},
            ":version": {"N": str(1 if expected_version is None else expected_version + 1)},
        }
        if last_response_class is not None:
            values[":class"] = {"S": last_response_class}
        if expected_version is None:
            condition = "attribute_not_exists(PK)"
        else:
            condition = "version = :expected"
            values[":expected"] = {"N": str(expected_version)}
        update = "SET version = :version, next_allowed_at = :next"
        if last_response_class is not None:
            update += ", last_response_class = :class"
        else:
            update += " REMOVE last_response_class"

        try:
            self._client.update_item(
                TableName=self._table,
                Key=self._pace_key(destination_key),
                UpdateExpression=update,
                ConditionExpression=condition,
                ExpressionAttributeValues=values,
            )
        except ClientError as error:
            if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise
        return True

    def claim_sending(
        self,
        candidate: str,
        *,
        expected_state_version: int,
        attempt_id: str,
        lease_expires_at: int,
    ) -> bool:
        from botocore.exceptions import ClientError

        validate_delivery_transition(
            status="sending",
            next_action_at=lease_expires_at,
            attempt_id=attempt_id,
            lease_expires_at=lease_expires_at,
        )

        try:
            self._client.update_item(
                TableName=self._table,
                Key=self._key(candidate),
                UpdateExpression=(
                    "SET #status = :sending, attempt_id = :attempt, lease_expires_at = :lease, "
                    "next_action_at = :lease, state_version = state_version + :one"
                ),
                ConditionExpression="#status = :queued AND state_version = :version AND attribute_not_exists(attempt_id)",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":queued": {"S": "queued"},
                    ":sending": {"S": "sending"},
                    ":attempt": {"S": attempt_id},
                    ":lease": {"N": str(lease_expires_at)},
                    ":version": {"N": str(expected_state_version)},
                    ":one": {"N": "1"},
                },
            )
        except ClientError as error:
            if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise
        return True

    def record_outcome(
        self,
        candidate: str,
        *,
        expected_state_version: int,
        attempt_id: str,
        status: str,
        network_attempt_count: int,
        next_action_at: int | None,
        slack_response: Mapping[str, Any] | None,
        expires_at: int | None,
    ) -> bool:
        from botocore.exceptions import ClientError

        # Before the write, not after the read. This store builds the item
        # attribute by attribute and never constructs a `DeliveryRecord`, so
        # nothing else here would refuse a shape its own decoder rejects.
        # `record_outcome` always clears the lease, so the probe carries no
        # attempt ID: that is the post-state being validated, not the claim
        # this call is resolving.
        validate_delivery_transition(
            status=status,
            next_action_at=next_action_at,
            network_attempt_count=network_attempt_count,
            slack_response=slack_response,
            expires_at=expires_at,
        )

        set_parts = ["#status = :status", "network_attempt_count = :net", "state_version = state_version + :one"]
        remove_parts = ["attempt_id", "lease_expires_at", "dispatch_id"]
        values: dict[str, dict[str, object]] = {
            ":status": {"S": status},
            ":net": {"N": str(network_attempt_count)},
            ":one": {"N": "1"},
        }
        if next_action_at is not None:
            set_parts.append("next_action_at = :next")
            values[":next"] = {"N": str(next_action_at)}
        else:
            remove_parts.append("next_action_at")
        if slack_response is not None:
            set_parts.append("slack_response = :resp")
            values[":resp"] = {"S": json.dumps(slack_response, sort_keys=True)}
        else:
            remove_parts.append("slack_response")
        if expires_at is not None:
            set_parts.append("expires_at = :expiry")
            values[":expiry"] = {"N": str(expires_at)}
        else:
            remove_parts.append("expires_at")
        update = f"SET {', '.join(set_parts)} REMOVE {', '.join(remove_parts)}"

        try:
            self._client.update_item(
                TableName=self._table,
                Key=self._key(candidate),
                UpdateExpression=update,
                ConditionExpression="#status = :sending AND state_version = :version AND attempt_id = :attempt",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    **values,
                    ":sending": {"S": "sending"},
                    ":version": {"N": str(expected_state_version)},
                    ":attempt": {"S": attempt_id},
                },
            )
        except ClientError as error:
            if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise
        return True

    def schedule_retry(
        self,
        candidate: str,
        *,
        expected_state_version: int,
        next_action_at: int,
        slack_response: Mapping[str, Any] | None,
    ) -> bool:
        from botocore.exceptions import ClientError

        validate_delivery_transition(
            status="failed_retryable",
            next_action_at=next_action_at,
            slack_response=slack_response,
        )

        set_parts = ["#status = :retryable", "next_action_at = :next", "state_version = state_version + :one"]
        remove_parts = ["dispatch_id"]
        values: dict[str, dict[str, object]] = {
            ":retryable": {"S": "failed_retryable"},
            ":next": {"N": str(next_action_at)},
            ":one": {"N": "1"},
        }
        if slack_response is not None:
            set_parts.append("slack_response = :resp")
            values[":resp"] = {"S": json.dumps(slack_response, sort_keys=True)}
        else:
            remove_parts.append("slack_response")
        update = f"SET {', '.join(set_parts)} REMOVE {', '.join(remove_parts)}"

        try:
            self._client.update_item(
                TableName=self._table,
                Key=self._key(candidate),
                UpdateExpression=update,
                ConditionExpression="#status = :queued AND state_version = :version",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    **values,
                    ":queued": {"S": "queued"},
                    ":version": {"N": str(expected_state_version)},
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
