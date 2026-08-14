#!/usr/bin/env python3
"""Preview or apply one audited delivery replay or found-post closure."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aws_public_change_feed.manual_replay import (  # noqa: E402
    ReplayRefused,
    apply_found_post,
    apply_terminal_replay,
    apply_unknown_replay,
    plan_found_post,
    plan_terminal_replay,
    plan_unknown_replay,
)
from aws_public_change_feed.outbox import DynamoDBDeliveryStore  # noqa: E402

EXIT_INVALID = 2
EXIT_REFUSED = 3
EXIT_AMBIGUOUS = 4


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preview or apply one audited delivery recovery action.")
    parser.add_argument("--table-name", required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--expected-state-version", required=True, type=int)
    parser.add_argument("--operator", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--evidence", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--found-post", action="store_true", help="close an unknown record after finding the Slack post")
    mode.add_argument(
        "--terminal-replay",
        action="store_true",
        help="replay one live failed_terminal record after an exact-request-compatible correction",
    )
    parser.add_argument("--terminal-retention-seconds", type=int, help="TTL horizon for a found posted record")
    parser.add_argument("--slack-message-ts", help="bounded Slack timestamp for the found message")
    parser.add_argument("--slack-permalink", help="bounded Slack permalink for the found message")
    parser.add_argument("--slack-reference", help="bounded operator reference for the found message")
    parser.add_argument("--apply", action="store_true", help="perform the single conditional mutation")
    return parser.parse_args(argv)


def _redacted_text(value: str) -> dict[str, str | int]:
    return {
        "characters": len(value),
        "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
    }


def _write(document: dict[str, Any], *, stream: Any | None = None) -> None:
    print(json.dumps(document, separators=(",", ":"), sort_keys=True), file=sys.stdout if stream is None else stream)


def _run_found_post(
    arguments: argparse.Namespace,
    store: DynamoDBDeliveryStore,
    record: Any,
    before_write: Callable[[dict[str, Any]], None],
) -> int:
    if arguments.terminal_retention_seconds is None:
        raise ValueError("found-post reconciliation requires --terminal-retention-seconds")
    plan = plan_found_post(
        record,
        expected_state_version=arguments.expected_state_version,
        operator=arguments.operator,
        reason=arguments.reason,
        evidence=arguments.evidence,
        terminal_retention_seconds=arguments.terminal_retention_seconds,
        slack_message_ts=arguments.slack_message_ts,
        slack_permalink=arguments.slack_permalink,
        slack_reference=arguments.slack_reference,
    )
    preview = {
        "action": "found_post_reconciliation",
        "candidate_id": plan.candidate_id,
        "current_state": record.status,
        "expected_state_version": plan.expected_state_version,
        "prior_attempt_id": plan.entry.prior_attempt_id,
        "operator": _redacted_text(plan.entry.operator),
        "reason": _redacted_text(plan.entry.reason),
        "evidence": _redacted_text(plan.entry.evidence),
        "expires_at": plan.expires_at,
    }
    if plan.entry.slack_message_ts is not None:
        preview["slack_message_ts"] = _redacted_text(plan.entry.slack_message_ts)
    if plan.entry.slack_permalink is not None:
        preview["slack_permalink"] = _redacted_text(plan.entry.slack_permalink)
    if plan.entry.slack_reference is not None:
        preview["slack_reference"] = _redacted_text(plan.entry.slack_reference)
    if not arguments.apply:
        _write({"status": "preview", **preview})
        return 0

    before_write(
        {
            "action": "found_post_reconciliation",
            "state": "posted",
            "state_version": plan.expected_state_version + 1,
            "prior_attempt_id": plan.entry.prior_attempt_id,
            "decided_at": plan.entry.decided_at,
        }
    )
    result = apply_found_post(store, plan)
    _write(
        {
            "status": "applied",
            **preview,
            "new_state": "posted",
            "new_state_version": result.state_version,
            "FoundPostReconciliation": result.found_post_count,
        }
    )
    return 0


def _run_unknown_replay(
    arguments: argparse.Namespace,
    store: DynamoDBDeliveryStore,
    record: Any,
    before_write: Callable[[dict[str, Any]], None],
) -> int:
    plan = plan_unknown_replay(
        record,
        expected_state_version=arguments.expected_state_version,
        operator=arguments.operator,
        reason=arguments.reason,
        evidence=arguments.evidence,
    )
    preview = {
        "action": "manual_replay",
        "candidate_id": plan.candidate_id,
        "current_state": record.status,
        "expected_state_version": plan.expected_state_version,
        "prior_attempt_id": plan.entry.prior_attempt_id,
        "operator": _redacted_text(plan.entry.operator),
        "reason": _redacted_text(plan.entry.reason),
        "evidence": _redacted_text(plan.entry.evidence),
    }
    if not arguments.apply:
        _write({"status": "preview", **preview})
        return 0

    before_write(
        {
            "action": "manual_replay",
            "state": "pending_queue",
            "state_version": plan.expected_state_version + 1,
            "new_attempt_id": plan.entry.new_attempt_id,
        }
    )
    result = apply_unknown_replay(store, plan)
    _write(
        {
            "status": "applied",
            **preview,
            "new_state": "pending_queue",
            "new_state_version": result.state_version,
            "new_attempt_id": result.new_attempt_id,
            "ManualReplay": result.manual_replay_count,
        }
    )
    return 0


def _run_terminal_replay(
    arguments: argparse.Namespace,
    store: DynamoDBDeliveryStore,
    record: Any,
    before_write: Callable[[dict[str, Any]], None],
) -> int:
    plan = plan_terminal_replay(
        record,
        expected_state_version=arguments.expected_state_version,
        operator=arguments.operator,
        reason=arguments.reason,
        evidence=arguments.evidence,
    )
    preview = {
        "action": "terminal_replay",
        "candidate_id": plan.candidate_id,
        "current_state": record.status,
        "expected_state_version": plan.expected_state_version,
        "prior_attempt_id": plan.entry.prior_attempt_id,
        "prior_response_class": plan.entry.prior_response_class,
        "prior_attempts_exhausted": plan.entry.prior_attempts_exhausted,
        "prior_expires_at": plan.entry.prior_expires_at,
        "operator": _redacted_text(plan.entry.operator),
        "reason": _redacted_text(plan.entry.reason),
        "evidence": _redacted_text(plan.entry.evidence),
    }
    if not arguments.apply:
        _write({"status": "preview", **preview})
        return 0

    before_write(
        {
            "action": "terminal_replay",
            "state": "pending_queue",
            "state_version": plan.expected_state_version + 1,
            "new_attempt_id": plan.entry.new_attempt_id,
        }
    )
    result = apply_terminal_replay(store, plan)
    _write(
        {
            "status": "applied",
            **preview,
            "new_state": "pending_queue",
            "new_state_version": result.state_version,
            "new_attempt_id": result.new_attempt_id,
            "TerminalReplay": result.terminal_replay_count,
        }
    )
    return 0


def _proof_matches(record: Any, proof: dict[str, Any]) -> bool:
    if record is None or record.status != proof["state"] or record.state_version != proof["state_version"]:
        return False
    if proof["action"] == "manual_replay":
        return bool(
            record.next_attempt_id == proof["new_attempt_id"]
            and record.manual_replay_history
            and record.manual_replay_history[-1].new_attempt_id == proof["new_attempt_id"]
        )
    if proof["action"] == "terminal_replay":
        return bool(
            record.next_attempt_id == proof["new_attempt_id"]
            and record.terminal_replay_history
            and record.terminal_replay_history[-1].new_attempt_id == proof["new_attempt_id"]
        )
    return bool(
        record.found_post_history
        and record.found_post_history[-1].prior_attempt_id == proof["prior_attempt_id"]
        and record.found_post_history[-1].decided_at == proof["decided_at"]
    )


def run(arguments: argparse.Namespace, client: Any) -> int:
    from botocore.exceptions import BotoCoreError, ClientError

    store = DynamoDBDeliveryStore(client, arguments.table_name)
    write_attempted = False
    write_proof: dict[str, Any] | None = None

    def before_write(proof: dict[str, Any]) -> None:
        nonlocal write_attempted, write_proof
        write_proof = proof
        write_attempted = True

    try:
        record = store.get_delivery(arguments.candidate_id)
        if record is None:
            raise ValueError("delivery record does not exist")
        found_post = bool(getattr(arguments, "found_post", False))
        terminal_replay = bool(getattr(arguments, "terminal_replay", False))
        if found_post:
            return _run_found_post(arguments, store, record, before_write)
        if terminal_replay:
            return _run_terminal_replay(arguments, store, record, before_write)
        return _run_unknown_replay(arguments, store, record, before_write)
    except ReplayRefused:
        try:
            current = store.get_delivery(arguments.candidate_id)
        except (BotoCoreError, ClientError):
            _write(
                {
                    "status": "ambiguous",
                    "error": "manual replay refusal was followed by an unreadable delivery record; reread before retry",
                },
                stream=sys.stderr,
            )
            return EXIT_AMBIGUOUS
        _write(
            {
                "status": "refused",
                "error": "delivery record changed; inspect the current state before another command",
                "candidate_id": arguments.candidate_id,
                "current_state": current.status if current is not None else None,
                "current_state_version": current.state_version if current is not None else None,
                "prior_attempt_id": current.last_attempt_id if current is not None else None,
                "next_attempt_reserved": current.next_attempt_id is not None if current is not None else None,
            },
            stream=sys.stderr,
        )
        return EXIT_REFUSED
    except ValueError as error:
        _write({"status": "invalid", "error": str(error)}, stream=sys.stderr)
        return EXIT_INVALID
    except (BotoCoreError, ClientError):
        if not write_attempted:
            _write(
                {
                    "status": "read_failed",
                    "error": "AWS read failed before any replay write was attempted; retry after restoring read access",
                },
                stream=sys.stderr,
            )
            return EXIT_AMBIGUOUS
        try:
            current = store.get_delivery(arguments.candidate_id)
        except (BotoCoreError, ClientError):
            current = None
        if write_proof is not None and _proof_matches(current, write_proof):
            applied = {
                "status": "applied_after_reread",
                "action": write_proof["action"],
                "candidate_id": arguments.candidate_id,
                "new_state": write_proof["state"],
                "new_state_version": write_proof["state_version"],
            }
            if "new_attempt_id" in write_proof:
                applied["new_attempt_id"] = write_proof["new_attempt_id"]
            _write(applied)
            return 0
        _write(
            {
                "status": "ambiguous",
                "error": "AWS did not prove whether the replay write completed; reread before retry",
            },
            stream=sys.stderr,
        )
        return EXIT_AMBIGUOUS


def main(argv: list[str] | None = None) -> int:
    import boto3

    arguments = parse_args(argv)
    return run(arguments, boto3.client("dynamodb"))


if __name__ == "__main__":
    raise SystemExit(main())
