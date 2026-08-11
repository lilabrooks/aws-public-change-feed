"""Operator-approved replay of one explicit unknown Slack outcome.

This module owns the decision step only. It never calls SQS or Slack. A
successful conditional mutation returns the record to `pending_queue`, where
the existing dispatcher and worker paths retain their accepted responsibilities.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from .candidates import utc_timestamp
from .outbox import MAX_MANUAL_REPLAY_HISTORY, DeliveryRecord, ManualReplayEntry, OutboxStore

__all__ = [
    "ManualReplayPlan",
    "ManualReplayResult",
    "ReplayRefused",
    "apply_unknown_replay",
    "plan_unknown_replay",
]


class ReplayRefused(RuntimeError):
    """The delivery record changed after preview or cannot accept this replay."""


@dataclass(frozen=True, slots=True)
class ManualReplayPlan:
    candidate_id: str
    expected_state_version: int
    entry: ManualReplayEntry
    next_action_at: int


@dataclass(frozen=True, slots=True)
class ManualReplayResult:
    candidate_id: str
    state_version: int
    new_attempt_id: str
    manual_replay_count: int = 1


def _new_attempt_id() -> str:
    return uuid.uuid4().hex


def plan_unknown_replay(
    record: DeliveryRecord,
    *,
    expected_state_version: int,
    operator: str,
    reason: str,
    evidence: str,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    attempt_id_factory: Callable[[], str] = _new_attempt_id,
) -> ManualReplayPlan:
    """Validate one preview and reserve the ID the next worker must claim."""

    if record.status != "delivery_unknown":
        raise ValueError("manual replay requires delivery_unknown state")
    if record.state_version != expected_state_version:
        raise ValueError("expected_state_version does not match the current delivery record")
    if record.last_attempt_id is None:
        raise ValueError("delivery_unknown record has no prior attempt to link")
    if record.expires_at is not None:
        raise ValueError("delivery_unknown record cannot carry expires_at")
    if record.next_attempt_id is not None:
        raise ValueError("delivery record already reserves a replay attempt")
    if len(record.manual_replay_history) >= MAX_MANUAL_REPLAY_HISTORY:
        raise ValueError(f"manual replay history already has {MAX_MANUAL_REPLAY_HISTORY} entries")

    decided = clock()
    decided_at = utc_timestamp(decided)
    new_attempt_id = attempt_id_factory()
    entry = ManualReplayEntry(
        decided_at=decided_at,
        operator=operator,
        reason=reason,
        evidence=evidence,
        prior_attempt_id=record.last_attempt_id,
        new_attempt_id=new_attempt_id,
    )
    return ManualReplayPlan(
        candidate_id=record.candidate_id,
        expected_state_version=expected_state_version,
        entry=entry,
        next_action_at=int(decided.timestamp()),
    )


def apply_unknown_replay(store: OutboxStore, plan: ManualReplayPlan) -> ManualReplayResult:
    """Attempt one conditional replay mutation, without automatic retry."""

    if not store.replay_unknown(
        plan.candidate_id,
        expected_state_version=plan.expected_state_version,
        expected_prior_attempt_id=plan.entry.prior_attempt_id,
        entry=plan.entry,
        next_action_at=plan.next_action_at,
    ):
        raise ReplayRefused("manual replay was refused because the delivery record changed")
    return ManualReplayResult(
        candidate_id=plan.candidate_id,
        state_version=plan.expected_state_version + 1,
        new_attempt_id=plan.entry.new_attempt_id,
    )
