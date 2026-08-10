"""Bounded recovery for durable Slack delivery state.

The reconciler is deliberately narrower than a replay engine. It sends only
scheduled work through the existing dispatcher, resolves only expired sending
claims whose exact durable lease it still owns, and reports old queued work
without guessing that SQS lost it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from .dispatch import DispatcherMetrics, DispatchResult, QueueSender, dispatch_due_work
from .outbox import ACKNOWLEDGED_STATES, SCHEDULED_STATES, OutboxStore

__all__ = [
    "NullRecoveryMetrics",
    "RecoveryMetrics",
    "RecoveryResult",
    "StateObservation",
    "reconcile_deliveries",
]

_OBSERVED_STATES = ("pending_queue", "failed_retryable", "queued", "sending")
_SENDING = "sending"
_QUEUED = "queued"
_UNKNOWN = "delivery_unknown"


class RecoveryMetrics(DispatcherMetrics, Protocol):
    """Fixed-cardinality facts emitted by one reconciler invocation."""

    def heartbeat(self) -> None: ...

    def state_observed(self, state: str, *, count: int, oldest_age_seconds: int) -> None: ...

    def state_observation_saturated(self, state: str) -> None: ...

    def repair_limit_reached(self) -> None: ...

    def expired_lease_unknown(self) -> None: ...

    def stale_queued(self, count: int) -> None: ...

    def conditional_race(self) -> None: ...


class NullRecoveryMetrics:
    def heartbeat(self) -> None:
        return None

    def state_observed(self, state: str, *, count: int, oldest_age_seconds: int) -> None:
        return None

    def state_observation_saturated(self, state: str) -> None:
        return None

    def repair_limit_reached(self) -> None:
        return None

    def expired_lease_unknown(self) -> None:
        return None

    def stale_queued(self, count: int) -> None:
        return None

    def conditional_race(self) -> None:
        return None

    def dispatch_attempted(self) -> None:
        return None

    def dispatch_accepted(self) -> None:
        return None

    def dispatch_unknown(self) -> None:
        return None


@dataclass(frozen=True, slots=True)
class StateObservation:
    state: str
    count: int
    oldest_age_seconds: int
    saturated: bool

    def __post_init__(self) -> None:
        if self.state not in _OBSERVED_STATES:
            raise ValueError(f"unobserved recovery state: {self.state}")
        if isinstance(self.count, bool) or not isinstance(self.count, int) or self.count < 0:
            raise ValueError("count must be a non-negative integer")
        if (
            isinstance(self.oldest_age_seconds, bool)
            or not isinstance(self.oldest_age_seconds, int)
            or self.oldest_age_seconds < 0
        ):
            raise ValueError("oldest_age_seconds must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    dispatch: DispatchResult
    observations: tuple[StateObservation, ...]
    expired_leases: int = 0
    stale_queued: int = 0
    conditional_races: int = 0
    repair_limit_reached: bool = False

    @property
    def observation_saturated(self) -> bool:
        return any(observation.saturated for observation in self.observations)

    @property
    def incomplete(self) -> bool:
        return (
            self.repair_limit_reached
            or self.observation_saturated
            or self.dispatch.unknown > 0
            or self.dispatch.failed_transitions > 0
        )


def _positive_integer(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _non_negative_integer(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def reconcile_deliveries(
    store: OutboxStore,
    sender: QueueSender,
    *,
    queue_url: str,
    now: datetime,
    max_delivery_request_bytes: int,
    repair_limit: int = 100,
    observation_limit: int = 101,
    stale_queued_after_seconds: int = 600,
    metrics: RecoveryMetrics | None = None,
) -> RecoveryResult:
    """Run one bounded reconciliation pass at one fixed invocation time."""

    repair_limit = _positive_integer("repair_limit", repair_limit)
    observation_limit = _positive_integer("observation_limit", observation_limit)
    stale_queued_after_seconds = _non_negative_integer("stale_queued_after_seconds", stale_queued_after_seconds)
    if observation_limit <= repair_limit:
        raise ValueError("observation_limit must exceed repair_limit so saturation is observable")
    if not isinstance(queue_url, str) or not queue_url:
        raise ValueError("queue_url must be a nonempty string")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    now_ts = int(now.timestamp())
    metrics = metrics if metrics is not None else NullRecoveryMetrics()

    indexed: dict[str, tuple[tuple[int, str], ...]] = {}
    observations: list[StateObservation] = []
    for state in _OBSERVED_STATES:
        entries = tuple(store.query_state(state, limit=observation_limit))
        indexed[state] = entries
        saturated = len(entries) >= observation_limit
        visible = entries[:repair_limit]
        oldest_age = max(0, now_ts - visible[0][0]) if visible else 0
        observation = StateObservation(
            state=state,
            count=len(visible),
            oldest_age_seconds=oldest_age,
            saturated=saturated,
        )
        observations.append(observation)
        metrics.state_observed(state, count=observation.count, oldest_age_seconds=oldest_age)
        if saturated:
            metrics.state_observation_saturated(state)

    repairable = [
        (timestamp, state, candidate)
        for state in (*sorted(SCHEDULED_STATES), _SENDING)
        for timestamp, candidate in indexed[state]
        if timestamp <= now_ts
    ]
    repairable.sort()
    selected = repairable[:repair_limit]
    repair_limit_reached = len(repairable) > repair_limit
    if repair_limit_reached:
        metrics.repair_limit_reached()

    scheduled_count = sum(1 for _, state, _ in selected if state in SCHEDULED_STATES)
    dispatch = (
        dispatch_due_work(
            store,
            sender,
            queue_url=queue_url,
            now=now,
            max_delivery_request_bytes=max_delivery_request_bytes,
            limit=scheduled_count,
            metrics=metrics,
        )
        if scheduled_count
        else DispatchResult()
    )

    expired_leases = 0
    conditional_races = 0
    for _, state, candidate in selected:
        if state != _SENDING:
            continue
        record = store.get_delivery(candidate)
        if record is None or record.status in ACKNOWLEDGED_STATES:
            conditional_races += 1
            metrics.conditional_race()
            continue
        if record.status != _SENDING or record.lease_expires_at is None or record.attempt_id is None:
            conditional_races += 1
            metrics.conditional_race()
            continue
        if record.lease_expires_at > now_ts:
            continue
        transitioned = store.mark_expired_sending_unknown(
            candidate,
            expected_state_version=record.state_version,
            attempt_id=record.attempt_id,
            lease_expires_at=record.lease_expires_at,
            due_before=now_ts,
            network_attempt_count=record.network_attempt_count,
            slack_response=record.slack_response,
        )
        if transitioned:
            expired_leases += 1
            metrics.expired_lease_unknown()
        else:
            conditional_races += 1
            metrics.conditional_race()

    stale_cutoff = now_ts - stale_queued_after_seconds
    stale_queued = sum(1 for timestamp, _ in indexed[_QUEUED][:repair_limit] if timestamp <= stale_cutoff)
    if stale_queued:
        metrics.stale_queued(stale_queued)

    return RecoveryResult(
        dispatch=dispatch,
        observations=tuple(observations),
        expired_leases=expired_leases,
        stale_queued=stale_queued,
        conditional_races=conditional_races,
        repair_limit_reached=repair_limit_reached,
    )
