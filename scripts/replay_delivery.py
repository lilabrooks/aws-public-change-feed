#!/usr/bin/env python3
"""Preview or apply one audited replay of a delivery_unknown record."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aws_public_change_feed.manual_replay import (  # noqa: E402
    ReplayRefused,
    apply_unknown_replay,
    plan_unknown_replay,
)
from aws_public_change_feed.outbox import DynamoDBDeliveryStore  # noqa: E402

EXIT_INVALID = 2
EXIT_REFUSED = 3
EXIT_AMBIGUOUS = 4


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preview or apply one audited unknown-outcome replay.")
    parser.add_argument("--table-name", required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--expected-state-version", required=True, type=int)
    parser.add_argument("--operator", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--apply", action="store_true", help="perform the single conditional mutation")
    return parser.parse_args(argv)


def _redacted_text(value: str) -> dict[str, str | int]:
    return {
        "characters": len(value),
        "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
    }


def _write(document: dict[str, Any], *, stream: Any | None = None) -> None:
    print(json.dumps(document, separators=(",", ":"), sort_keys=True), file=sys.stdout if stream is None else stream)


def run(arguments: argparse.Namespace, client: Any) -> int:
    from botocore.exceptions import BotoCoreError, ClientError

    store = DynamoDBDeliveryStore(client, arguments.table_name)
    write_attempted = False
    try:
        record = store.get_delivery(arguments.candidate_id)
        if record is None:
            raise ValueError("delivery record does not exist")
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

        write_attempted = True
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
