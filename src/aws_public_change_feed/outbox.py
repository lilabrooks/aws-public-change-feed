"""Durable outbox port for candidates and their delivery records.

Chapter 04 "Durable emission" and chapter 02 "Delivery table" define this
boundary: for each candidate, conditionally write the candidate and its
`pending_queue` delivery record, and never advance a feed checkpoint until that
work is durable. ADR-007 makes DynamoDB the system of record.

The port is defined here with an in-memory implementation, so idempotent
re-emission, request reuse, and the identity-mismatch failure are testable
before any AWS resource exists. The DynamoDB adapter lands with the Terraform
root that creates the table, the same way `state.py` waits for its table.

What lives here is the creation boundary only. Dispatch generations, leases,
Slack outcomes, and terminal TTLs belong to the dispatcher and worker in
milestone 5, and this module deliberately writes none of them: a `pending_queue`
record has no TTL, because chapter 02 gives one only to resolved states.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from .candidates import CONTRACT_VERSION, utc_timestamp
from .identity import audience_fingerprint, candidate_id, delivery_request_id

__all__ = [
    "DELIVERY_STATES",
    "CandidateIdentityError",
    "DeliveryRecord",
    "EmissionResult",
    "InMemoryOutboxStore",
    "OutboxStore",
    "build_delivery_request",
    "emit",
    "verify_durable",
]

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


class CandidateIdentityError(Exception):
    """A stored item's identity fields disagree with its key.

    Chapter 04 calls this a correctness failure that blocks checkpoint
    advancement, so it is deliberately not a `ValueError` the caller might
    handle alongside ordinary validation. Nothing recovers from it
    automatically.
    """


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

    def __post_init__(self) -> None:
        if self.status not in DELIVERY_STATES:
            raise ValueError(f"unknown delivery state: {self.status}")
        if self.status in SCHEDULED_STATES and self.next_action_at is None:
            raise ValueError(f"{self.status} delivery records require next_action_at")
        if self.next_action_at is not None and (
            isinstance(self.next_action_at, bool) or not isinstance(self.next_action_at, int) or self.next_action_at < 0
        ):
            raise ValueError("next_action_at must be a non-negative integer Unix timestamp")


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
    """

    def get_candidate(self, candidate: str) -> Mapping[str, Any] | None: ...

    def put_candidate_if_absent(self, candidate: Mapping[str, Any]) -> bool: ...

    def get_delivery(self, candidate: str) -> DeliveryRecord | None: ...

    def put_delivery_if_absent(self, record: DeliveryRecord) -> bool: ...


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
    created_at: datetime,
) -> EmissionResult:
    """Make each candidate and its `pending_queue` delivery record durable.

    Safe to repeat. A candidate that already exists is loaded and validated
    rather than replaced, and a missing delivery record is repaired from the
    stored candidate's request rather than from the current release.

    Raises `CandidateIdentityError` when a stored item disagrees with its key,
    which chapter 04 requires to block checkpoint advancement.
    """

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

        if store.put_candidate_if_absent(candidate):
            created_candidates.append(key)
            durable = candidate
        else:
            # Chapter 04: load and validate the stored candidate rather than
            # replacing it. Its evidence, release, and creation time are
            # immutable once written.
            stored = store.get_candidate(key)
            if stored is None:
                raise CandidateIdentityError(f"candidate {key} reported present but could not be loaded")
            _assert_identity(stored)
            reused_candidates.append(key)
            durable = stored

        if store.get_delivery(key) is not None:
            continue

        # Repairing a missing record reuses the durable candidate, so a replay
        # under a newer release cannot change the payload already committed.
        destination_key = _destination_key(inventory, durable["route_id"])
        record = DeliveryRecord(
            candidate_id=key,
            destination_key=destination_key,
            request=build_delivery_request(durable, destination_key, created_at),
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
