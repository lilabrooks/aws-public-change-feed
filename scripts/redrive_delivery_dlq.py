#!/usr/bin/env python3
"""Preview and control native SQS delivery-DLQ redrive tasks."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

EXIT_INVALID = 2
EXIT_REFUSED = 3
EXIT_AMBIGUOUS = 4
MAX_IDENTIFIER_CHARACTERS = 2048
MAX_REDRIVE_RATE = 500
MAX_TASK_RESULTS = 10
MAX_SQS_COUNT = (1 << 63) - 1
ACTIVE_TASK_STATES = frozenset({"RUNNING", "CANCELLING"})
KNOWN_TASK_STATES = frozenset({"RUNNING", "CANCELLING", "CANCELLED", "COMPLETED", "FAILED"})


@dataclass(frozen=True, slots=True)
class QueueTopology:
    source_arn: str
    dlq_arn: str
    approximate_dlq_visible: int | None


def _bounded_identifier(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= MAX_IDENTIFIER_CHARACTERS
        or any(character in value for character in "\r\n\x00")
    ):
        raise ValueError(f"{field} must be a bounded single-line string")
    return value


def _redrive_rate(value: str) -> int:
    try:
        rate = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("redrive velocity must be an integer") from error
    if not 1 <= rate <= MAX_REDRIVE_RATE:
        raise argparse.ArgumentTypeError(f"redrive velocity must be between 1 and {MAX_REDRIVE_RATE}")
    return rate


def _common_queue_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source-queue-url", required=True)
    parser.add_argument("--dlq-url", required=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preview and control native delivery-DLQ redrive.")
    actions = parser.add_subparsers(dest="action", required=True)

    preview = actions.add_parser("preview", help="validate topology and show the proposed start without mutation")
    _common_queue_arguments(preview)
    preview.add_argument("--max-messages-per-second", required=True, type=_redrive_rate)

    start = actions.add_parser("start", help="start one native SQS message movement task")
    _common_queue_arguments(start)
    start.add_argument("--max-messages-per-second", required=True, type=_redrive_rate)
    start.add_argument("--apply", action="store_true", help="perform the start mutation")

    status = actions.add_parser("status", help="show recent native SQS message movement tasks")
    _common_queue_arguments(status)

    cancel = actions.add_parser("cancel", help="cancel one exact running task")
    _common_queue_arguments(cancel)
    cancel.add_argument("--task-handle", required=True)
    cancel.add_argument("--apply", action="store_true", help="perform the cancel mutation")
    return parser.parse_args(argv)


def _write(document: Mapping[str, Any], *, stream: Any | None = None) -> None:
    print(json.dumps(document, separators=(",", ":"), sort_keys=True), file=sys.stdout if stream is None else stream)


def _parse_policy(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} is missing")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError(f"{field} is not valid JSON") from error
    if not isinstance(parsed, dict):
        raise ValueError(f"{field} must be a JSON object")
    return parsed


def _optional_count(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        count = value
    elif isinstance(value, str) and value.isascii() and value.isdecimal():
        count = int(value)
    else:
        return None
    return count if 0 <= count <= MAX_SQS_COUNT else None


def _queue_attributes(client: Any, queue_url: str) -> dict[str, str]:
    response = client.get_queue_attributes(
        QueueUrl=queue_url,
        AttributeNames=[
            "QueueArn",
            "FifoQueue",
            "RedrivePolicy",
            "RedriveAllowPolicy",
            "ApproximateNumberOfMessages",
        ],
    )
    attributes = response.get("Attributes")
    if not isinstance(attributes, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in attributes.items()
    ):
        raise ValueError("queue attributes response is malformed")
    return attributes


def _load_topology(client: Any, source_queue_url: object, dlq_url: object) -> QueueTopology:
    source_url = _bounded_identifier(source_queue_url, "source queue URL")
    dead_letter_url = _bounded_identifier(dlq_url, "DLQ URL")
    if source_url == dead_letter_url:
        raise ValueError("source queue URL and DLQ URL must differ")

    source = _queue_attributes(client, source_url)
    dlq = _queue_attributes(client, dead_letter_url)
    source_arn = _bounded_identifier(source.get("QueueArn"), "source queue ARN")
    dlq_arn = _bounded_identifier(dlq.get("QueueArn"), "DLQ ARN")
    if source.get("FifoQueue") != "true" or dlq.get("FifoQueue") != "true":
        raise ValueError("source queue and DLQ must both be FIFO queues")

    redrive = _parse_policy(source.get("RedrivePolicy"), "source RedrivePolicy")
    if redrive.get("deadLetterTargetArn") != dlq_arn:
        raise ValueError("source RedrivePolicy does not name the supplied DLQ")

    allow = _parse_policy(dlq.get("RedriveAllowPolicy"), "DLQ RedriveAllowPolicy")
    if allow.get("redrivePermission") != "byQueue" or allow.get("sourceQueueArns") != [source_arn]:
        raise ValueError("DLQ RedriveAllowPolicy must name only the supplied source queue")
    return QueueTopology(
        source_arn=source_arn,
        dlq_arn=dlq_arn,
        approximate_dlq_visible=_optional_count(dlq.get("ApproximateNumberOfMessages")),
    )


def _task_document(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("message movement task entry is malformed")
    task_handle = _bounded_identifier(value.get("TaskHandle"), "task handle")
    source_arn = _bounded_identifier(value.get("SourceArn"), "task source ARN")
    status = value.get("Status")
    if not isinstance(status, str) or status not in KNOWN_TASK_STATES:
        raise ValueError("message movement task status is unknown")
    destination = value.get("DestinationArn")
    destination_arn = None
    if destination is not None:
        try:
            destination_arn = _bounded_identifier(destination, "task destination ARN")
        except ValueError:
            destination_arn = None
    rate = _optional_count(value.get("MaxNumberOfMessagesPerSecond"))
    if rate is not None and not 1 <= rate <= MAX_REDRIVE_RATE:
        rate = None
    return {
        "task_handle": task_handle,
        "status": status,
        "source_arn": source_arn,
        "destination_arn": destination_arn,
        "max_messages_per_second": rate,
        "approximate_messages_moved": _optional_count(value.get("ApproximateNumberOfMessagesMoved")),
        "approximate_messages_to_move": _optional_count(value.get("ApproximateNumberOfMessagesToMove")),
        "started_timestamp": _optional_count(value.get("StartedTimestamp")),
        "failure_reason_present": bool(value.get("FailureReason")),
    }


def _list_tasks(client: Any, dlq_arn: str) -> list[dict[str, Any]]:
    response = client.list_message_move_tasks(SourceArn=dlq_arn, MaxResults=MAX_TASK_RESULTS)
    values = response.get("Results", [])
    if not isinstance(values, list):
        raise ValueError("message movement task response is malformed")
    tasks = [_task_document(value) for value in values]
    tasks.sort(key=lambda task: (task["started_timestamp"] or -1, task["task_handle"]), reverse=True)
    return tasks


def _base_document(topology: QueueTopology, tasks: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "action": "delivery_dlq_redrive",
        "source_arn": topology.source_arn,
        "dlq_arn": topology.dlq_arn,
        "approximate_dlq_visible": topology.approximate_dlq_visible,
        "latest_task": tasks[0] if tasks else None,
        "counts_are_approximate": True,
        "exact_message_count_boundary": False,
    }


def _find_task(tasks: list[dict[str, Any]], task_handle: str) -> dict[str, Any] | None:
    return next((task for task in tasks if task["task_handle"] == task_handle), None)


def _is_definitive_client_refusal(error: Any) -> bool:
    response = error.response if isinstance(getattr(error, "response", None), dict) else {}
    metadata = response.get("ResponseMetadata")
    status = metadata.get("HTTPStatusCode") if isinstance(metadata, dict) else None
    return isinstance(status, int) and not isinstance(status, bool) and 400 <= status < 500


def run(arguments: argparse.Namespace, client: Any) -> int:
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        topology = _load_topology(client, arguments.source_queue_url, arguments.dlq_url)
        tasks = _list_tasks(client, topology.dlq_arn)
    except ValueError as error:
        _write({"status": "invalid", "error": str(error)}, stream=sys.stderr)
        return EXIT_INVALID
    except (BotoCoreError, ClientError):
        _write(
            {
                "status": "read_failed",
                "error": "AWS reads failed before any redrive mutation; restore access and preview again",
            },
            stream=sys.stderr,
        )
        return EXIT_AMBIGUOUS

    base = _base_document(topology, tasks)
    if arguments.action == "preview":
        _write({"status": "preview", **base, "max_messages_per_second": arguments.max_messages_per_second})
        return 0
    if arguments.action == "status":
        _write({"status": "observed", **base, "tasks": tasks})
        return 0

    if arguments.action == "start":
        if not arguments.apply:
            _write({"status": "invalid", "error": "start requires --apply"}, stream=sys.stderr)
            return EXIT_INVALID
        if any(task["status"] in ACTIVE_TASK_STATES for task in tasks):
            _write(
                {
                    "status": "refused",
                    "error": "a message movement task is already active; inspect status before retry",
                },
                stream=sys.stderr,
            )
            return EXIT_REFUSED
        try:
            response = client.start_message_move_task(
                SourceArn=topology.dlq_arn,
                MaxNumberOfMessagesPerSecond=arguments.max_messages_per_second,
            )
            task_handle = _bounded_identifier(response.get("TaskHandle"), "task handle")
        except ValueError as error:
            _write({"status": "ambiguous", "error": str(error), "next_action": "status"}, stream=sys.stderr)
            return EXIT_AMBIGUOUS
        except ClientError as error:
            if _is_definitive_client_refusal(error):
                _write(
                    {
                        "status": "refused",
                        "error": "SQS rejected the start; inspect topology and status before retry",
                    },
                    stream=sys.stderr,
                )
                return EXIT_REFUSED
            _write(
                {
                    "status": "ambiguous",
                    "error": "AWS did not prove whether redrive started",
                    "next_action": "status",
                },
                stream=sys.stderr,
            )
            return EXIT_AMBIGUOUS
        except BotoCoreError:
            _write(
                {"status": "ambiguous", "error": "AWS did not prove whether redrive started", "next_action": "status"},
                stream=sys.stderr,
            )
            return EXIT_AMBIGUOUS
        latest_task_before_start = base.pop("latest_task")
        _write(
            {
                "status": "started",
                **base,
                "latest_task_before_start": latest_task_before_start,
                "max_messages_per_second": arguments.max_messages_per_second,
                "task_handle": task_handle,
            }
        )
        return 0

    if arguments.action == "cancel":
        if not arguments.apply:
            _write({"status": "invalid", "error": "cancel requires --apply"}, stream=sys.stderr)
            return EXIT_INVALID
        try:
            requested_handle = _bounded_identifier(arguments.task_handle, "task handle")
        except ValueError as error:
            _write({"status": "invalid", "error": str(error)}, stream=sys.stderr)
            return EXIT_INVALID
        task = _find_task(tasks, requested_handle)
        if task is None or task["source_arn"] != topology.dlq_arn or task["status"] != "RUNNING":
            _write(
                {"status": "refused", "error": "the exact supplied task is not currently running; inspect status"},
                stream=sys.stderr,
            )
            return EXIT_REFUSED
        try:
            response = client.cancel_message_move_task(TaskHandle=requested_handle)
        except ClientError as error:
            if _is_definitive_client_refusal(error):
                _write(
                    {"status": "refused", "error": "SQS rejected cancellation; inspect status before retry"},
                    stream=sys.stderr,
                )
                return EXIT_REFUSED
            _write(
                {
                    "status": "ambiguous",
                    "error": "AWS did not prove whether cancellation completed",
                    "next_action": "status",
                },
                stream=sys.stderr,
            )
            return EXIT_AMBIGUOUS
        except BotoCoreError:
            _write(
                {
                    "status": "ambiguous",
                    "error": "AWS did not prove whether cancellation completed",
                    "next_action": "status",
                },
                stream=sys.stderr,
            )
            return EXIT_AMBIGUOUS
        _write(
            {
                "status": "cancel_requested",
                **base,
                "task_handle": requested_handle,
                "approximate_messages_moved": _optional_count(response.get("ApproximateNumberOfMessagesMoved")),
                "next_action": "status",
            }
        )
        return 0

    _write({"status": "invalid", "error": "unknown action"}, stream=sys.stderr)
    return EXIT_INVALID


def main(argv: list[str] | None = None) -> int:
    import boto3

    arguments = parse_args(argv)
    return run(arguments, boto3.client("sqs"))


if __name__ == "__main__":
    raise SystemExit(main())
