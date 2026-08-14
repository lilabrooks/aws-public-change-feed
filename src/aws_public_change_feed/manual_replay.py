"""Operator-approved replay or closure of one explicit Slack outcome.

This module owns the decision step only. It never calls SQS or Slack. A
successful conditional mutation returns the record to `pending_queue`, where
the existing dispatcher and worker paths retain their accepted responsibilities.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .candidates import utc_timestamp
from .outbox import (
    MAX_FOUND_POST_HISTORY,
    MAX_MANUAL_REPLAY_HISTORY,
    MAX_TERMINAL_REPLAY_HISTORY,
    DeliveryRecord,
    FoundPostEntry,
    ManualReplayEntry,
    OutboxStore,
    TerminalReplayEntry,
    terminal_replay_outcome,
)

__all__ = [
    "ManualReplayPlan",
    "ManualReplayResult",
    "ReplayRefused",
    "TerminalReplayPlan",
    "TerminalReplayResult",
    "FoundPostPlan",
    "FoundPostResult",
    "apply_unknown_replay",
    "apply_found_post",
    "apply_terminal_replay",
    "plan_found_post",
    "plan_terminal_replay",
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


@dataclass(frozen=True, slots=True)
class TerminalReplayPlan:
    candidate_id: str
    expected_state_version: int
    expected_expires_at: int
    expected_slack_response: Mapping[str, Any]
    entry: TerminalReplayEntry
    next_action_at: int


@dataclass(frozen=True, slots=True)
class TerminalReplayResult:
    candidate_id: str
    state_version: int
    new_attempt_id: str
    terminal_replay_count: int = 1


@dataclass(frozen=True, slots=True)
class FoundPostPlan:
    candidate_id: str
    expected_state_version: int
    entry: FoundPostEntry
    expires_at: int


@dataclass(frozen=True, slots=True)
class FoundPostResult:
    candidate_id: str
    state_version: int
    found_post_count: int = 1


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


def plan_terminal_replay(
    record: DeliveryRecord,
    *,
    expected_state_version: int,
    operator: str,
    reason: str,
    evidence: str,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    attempt_id_factory: Callable[[], str] = _new_attempt_id,
) -> TerminalReplayPlan:
    """Validate one live exact-request terminal replay and reserve its attempt."""

    if record.status != "failed_terminal":
        raise ValueError("terminal replay requires failed_terminal state")
    if record.state_version != expected_state_version:
        raise ValueError("expected_state_version does not match the current delivery record")
    if record.last_attempt_id is None:
        raise ValueError("failed_terminal record has no prior attempt to link")
    if record.expires_at is None:
        raise ValueError("failed_terminal record has no terminal expiry")
    if record.next_attempt_id is not None:
        raise ValueError("delivery record already reserves a replay attempt")
    if len(record.terminal_replay_history) >= MAX_TERMINAL_REPLAY_HISTORY:
        raise ValueError(f"terminal replay history already has {MAX_TERMINAL_REPLAY_HISTORY} entries")

    response_class, attempts_exhausted = terminal_replay_outcome(record.slack_response)
    decided = clock()
    next_action_at = int(decided.timestamp())
    if record.expires_at <= next_action_at:
        raise ValueError("failed_terminal record has expired and cannot be replayed")
    new_attempt_id = attempt_id_factory()
    entry = TerminalReplayEntry(
        decided_at=utc_timestamp(decided),
        operator=operator,
        reason=reason,
        evidence=evidence,
        prior_attempt_id=record.last_attempt_id,
        new_attempt_id=new_attempt_id,
        prior_response_class=response_class,
        prior_attempts_exhausted=attempts_exhausted,
        prior_expires_at=record.expires_at,
    )
    assert record.slack_response is not None
    return TerminalReplayPlan(
        candidate_id=record.candidate_id,
        expected_state_version=expected_state_version,
        expected_expires_at=record.expires_at,
        expected_slack_response=dict(record.slack_response),
        entry=entry,
        next_action_at=next_action_at,
    )


def apply_terminal_replay(store: OutboxStore, plan: TerminalReplayPlan) -> TerminalReplayResult:
    """Attempt one conditional terminal replay mutation, without automatic retry."""

    if not store.replay_terminal(
        plan.candidate_id,
        expected_state_version=plan.expected_state_version,
        expected_prior_attempt_id=plan.entry.prior_attempt_id,
        expected_expires_at=plan.expected_expires_at,
        expected_slack_response=plan.expected_slack_response,
        entry=plan.entry,
        next_action_at=plan.next_action_at,
    ):
        raise ReplayRefused("terminal replay was refused because the delivery record changed")
    return TerminalReplayResult(
        candidate_id=plan.candidate_id,
        state_version=plan.expected_state_version + 1,
        new_attempt_id=plan.entry.new_attempt_id,
    )


def plan_found_post(
    record: DeliveryRecord,
    *,
    expected_state_version: int,
    operator: str,
    reason: str,
    evidence: str,
    terminal_retention_seconds: int,
    slack_message_ts: str | None = None,
    slack_permalink: str | None = None,
    slack_reference: str | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> FoundPostPlan:
    """Validate one reviewed found-post closure without scheduling another call."""

    if record.status != "delivery_unknown":
        raise ValueError("found-post reconciliation requires delivery_unknown state")
    if record.state_version != expected_state_version:
        raise ValueError("expected_state_version does not match the current delivery record")
    if record.last_attempt_id is None:
        raise ValueError("delivery_unknown record has no prior attempt to close")
    if record.expires_at is not None:
        raise ValueError("delivery_unknown record cannot carry expires_at")
    if record.next_attempt_id is not None:
        raise ValueError("delivery record already reserves a replay attempt")
    if len(record.found_post_history) >= MAX_FOUND_POST_HISTORY:
        raise ValueError(f"found-post history already has {MAX_FOUND_POST_HISTORY} entries")
    if (
        isinstance(terminal_retention_seconds, bool)
        or not isinstance(terminal_retention_seconds, int)
        or terminal_retention_seconds <= 0
    ):
        raise ValueError("terminal_retention_seconds must be a positive integer")

    decided = clock()
    entry = FoundPostEntry(
        decided_at=utc_timestamp(decided),
        operator=operator,
        reason=reason,
        evidence=evidence,
        prior_attempt_id=record.last_attempt_id,
        slack_message_ts=slack_message_ts,
        slack_permalink=slack_permalink,
        slack_reference=slack_reference,
    )
    return FoundPostPlan(
        candidate_id=record.candidate_id,
        expected_state_version=expected_state_version,
        entry=entry,
        expires_at=int(decided.timestamp()) + terminal_retention_seconds,
    )


def apply_found_post(store: OutboxStore, plan: FoundPostPlan) -> FoundPostResult:
    """Attempt one conditional found-post closure, without automatic retry."""

    if not store.reconcile_found_post(
        plan.candidate_id,
        expected_state_version=plan.expected_state_version,
        expected_prior_attempt_id=plan.entry.prior_attempt_id,
        entry=plan.entry,
        expires_at=plan.expires_at,
    ):
        raise ReplayRefused("found-post reconciliation was refused because the delivery record changed")
    return FoundPostResult(
        candidate_id=plan.candidate_id,
        state_version=plan.expected_state_version + 1,
    )
