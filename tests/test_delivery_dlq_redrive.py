"""Contract tests for preview-first native delivery-DLQ redrive control."""

import argparse
import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from botocore.exceptions import ClientError, EndpointConnectionError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import redrive_delivery_dlq  # noqa: E402

SOURCE_URL = "https://sqs.us-west-2.amazonaws.com/123456789012/delivery.fifo"
DLQ_URL = "https://sqs.us-west-2.amazonaws.com/123456789012/delivery-dlq.fifo"
SOURCE_ARN = "arn:aws:sqs:us-west-2:123456789012:delivery.fifo"
DLQ_ARN = "arn:aws:sqs:us-west-2:123456789012:delivery-dlq.fifo"
TASK_HANDLE = "task-handle-1"


def source_attributes(**overrides):
    values = {
        "QueueArn": SOURCE_ARN,
        "FifoQueue": "true",
        "RedrivePolicy": json.dumps({"deadLetterTargetArn": DLQ_ARN, "maxReceiveCount": 7}),
        "ApproximateNumberOfMessages": "0",
    }
    values.update(overrides)
    return values


def dlq_attributes(**overrides):
    values = {
        "QueueArn": DLQ_ARN,
        "FifoQueue": "true",
        "RedriveAllowPolicy": json.dumps({"redrivePermission": "byQueue", "sourceQueueArns": [SOURCE_ARN]}),
        "ApproximateNumberOfMessages": "4",
    }
    values.update(overrides)
    return values


def task(status="RUNNING", *, handle=TASK_HANDLE, started=100):
    return {
        "TaskHandle": handle,
        "Status": status,
        "SourceArn": DLQ_ARN,
        "DestinationArn": SOURCE_ARN,
        "MaxNumberOfMessagesPerSecond": 1,
        "ApproximateNumberOfMessagesMoved": 2,
        "ApproximateNumberOfMessagesToMove": 4,
        "StartedTimestamp": started,
    }


class FakeSqsClient:
    def __init__(self, *, source=None, dlq=None, tasks=None, start_error=None, cancel_error=None):
        self.source = source_attributes() if source is None else source
        self.dlq = dlq_attributes() if dlq is None else dlq
        self.tasks = [] if tasks is None else tasks
        self.start_error = start_error
        self.cancel_error = cancel_error
        self.calls = []

    def get_queue_attributes(self, **kwargs):
        self.calls.append(("get_queue_attributes", kwargs))
        return {"Attributes": self.source if kwargs["QueueUrl"] == SOURCE_URL else self.dlq}

    def list_message_move_tasks(self, **kwargs):
        self.calls.append(("list_message_move_tasks", kwargs))
        return {"Results": self.tasks}

    def start_message_move_task(self, **kwargs):
        self.calls.append(("start_message_move_task", kwargs))
        if self.start_error is not None:
            raise self.start_error
        return {"TaskHandle": TASK_HANDLE}

    def cancel_message_move_task(self, **kwargs):
        self.calls.append(("cancel_message_move_task", kwargs))
        if self.cancel_error is not None:
            raise self.cancel_error
        return {"ApproximateNumberOfMessagesMoved": 3}

    def receive_message(self, **kwargs):
        raise AssertionError("native task control must not receive individual messages")

    def send_message(self, **kwargs):
        raise AssertionError("native task control must not send individual messages")

    def delete_message(self, **kwargs):
        raise AssertionError("native task control must not delete individual messages")


def arguments(action, *, apply=False, rate=1, task_handle=TASK_HANDLE):
    return argparse.Namespace(
        action=action,
        source_queue_url=SOURCE_URL,
        dlq_url=DLQ_URL,
        max_messages_per_second=rate,
        apply=apply,
        task_handle=task_handle,
    )


def invoke(client, args):
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        result = redrive_delivery_dlq.run(args, client)
    return result, stdout.getvalue(), stderr.getvalue()


class DeliveryDlqRedriveTests(unittest.TestCase):
    def test_preview_reads_topology_and_tasks_without_mutation(self):
        client = FakeSqsClient(tasks=[task("COMPLETED")])

        exit_code, stdout, stderr = invoke(client, arguments("preview", rate=1))

        document = json.loads(stdout)
        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(document["status"], "preview")
        self.assertEqual(document["source_arn"], SOURCE_ARN)
        self.assertEqual(document["dlq_arn"], DLQ_ARN)
        self.assertEqual(document["approximate_dlq_visible"], 4)
        self.assertTrue(document["counts_are_approximate"])
        self.assertFalse(document["exact_message_count_boundary"])
        self.assertEqual(
            [name for name, _ in client.calls],
            ["get_queue_attributes", "get_queue_attributes", "list_message_move_tasks"],
        )

    def test_start_omits_destination_and_uses_exact_dlq_and_velocity(self):
        client = FakeSqsClient(tasks=[task("COMPLETED")])

        exit_code, stdout, stderr = invoke(client, arguments("start", apply=True, rate=1))

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(json.loads(stdout)["task_handle"], TASK_HANDLE)
        start_calls = [kwargs for name, kwargs in client.calls if name == "start_message_move_task"]
        self.assertEqual(start_calls, [{"SourceArn": DLQ_ARN, "MaxNumberOfMessagesPerSecond": 1}])
        self.assertNotIn("DestinationArn", start_calls[0])

    def test_every_topology_refusal_occurs_before_start(self):
        cases = (
            ("source non-FIFO", source_attributes(FifoQueue="false"), dlq_attributes()),
            ("DLQ non-FIFO", source_attributes(), dlq_attributes(FifoQueue="false")),
            (
                "wrong target",
                source_attributes(
                    RedrivePolicy=json.dumps({"deadLetterTargetArn": "arn:aws:sqs:us-west-2:123456789012:other.fifo"})
                ),
                dlq_attributes(),
            ),
            (
                "allow all",
                source_attributes(),
                dlq_attributes(RedriveAllowPolicy=json.dumps({"redrivePermission": "allowAll"})),
            ),
            (
                "extra source",
                source_attributes(),
                dlq_attributes(
                    RedriveAllowPolicy=json.dumps(
                        {
                            "redrivePermission": "byQueue",
                            "sourceQueueArns": [SOURCE_ARN, "arn:aws:sqs:us-west-2:123456789012:other.fifo"],
                        }
                    )
                ),
            ),
        )
        for name, source, dlq in cases:
            with self.subTest(case=name):
                client = FakeSqsClient(source=source, dlq=dlq)
                exit_code, stdout, stderr = invoke(client, arguments("start", apply=True))
                self.assertEqual(exit_code, redrive_delivery_dlq.EXIT_INVALID)
                self.assertEqual(stdout, "")
                self.assertEqual(json.loads(stderr)["status"], "invalid")
                self.assertNotIn("start_message_move_task", [call for call, _ in client.calls])

    def test_missing_apply_and_active_task_refuse_without_start(self):
        cases = (
            ("missing apply", FakeSqsClient(), arguments("start"), redrive_delivery_dlq.EXIT_INVALID),
            (
                "running",
                FakeSqsClient(tasks=[task("RUNNING")]),
                arguments("start", apply=True),
                redrive_delivery_dlq.EXIT_REFUSED,
            ),
            (
                "cancelling",
                FakeSqsClient(tasks=[task("CANCELLING")]),
                arguments("start", apply=True),
                redrive_delivery_dlq.EXIT_REFUSED,
            ),
        )
        for name, client, args, expected in cases:
            with self.subTest(case=name):
                exit_code, stdout, stderr = invoke(client, args)
                self.assertEqual(exit_code, expected)
                self.assertEqual(stdout, "")
                self.assertNotIn("start_message_move_task", [call for call, _ in client.calls])

    def test_status_bounds_task_fields_and_sorts_newest_first(self):
        tasks = [
            task("COMPLETED", handle="older", started=10),
            {**task("FAILED", handle="newer", started=20), "FailureReason": "provider detail"},
        ]

        exit_code, stdout, stderr = invoke(FakeSqsClient(tasks=tasks), arguments("status"))

        document = json.loads(stdout)
        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual([value["task_handle"] for value in document["tasks"]], ["newer", "older"])
        self.assertTrue(document["tasks"][0]["failure_reason_present"])
        self.assertNotIn("provider detail", stdout)
        self.assertFalse(document["exact_message_count_boundary"])

    def test_status_renders_each_missing_required_task_fixture(self):
        cases: tuple[tuple[str, list[dict[str, object]], str | None, list[str]], ...] = (
            ("no-task", [], None, []),
            ("running", [task("RUNNING")], "RUNNING", ["RUNNING"]),
            ("cancelled", [task("CANCELLED")], "CANCELLED", ["CANCELLED"]),
        )
        self.assertEqual({name for name, *_ in cases}, {"no-task", "running", "cancelled"})

        for name, tasks, latest_status, expected_statuses in cases:
            with self.subTest(case=name):
                exit_code, stdout, stderr = invoke(FakeSqsClient(tasks=tasks), arguments("status"))

                document = json.loads(stdout)
                self.assertEqual(exit_code, 0)
                self.assertEqual(stderr, "")
                self.assertEqual(
                    document["latest_task"]["status"] if document["latest_task"] is not None else None,
                    latest_status,
                )
                self.assertEqual([value["status"] for value in document["tasks"]], expected_statuses)
                self.assertTrue(document["counts_are_approximate"])
                self.assertFalse(document["exact_message_count_boundary"])

    def test_malformed_or_unknown_task_state_fails_closed_before_start(self):
        cases = ({"TaskHandle": "malformed"}, task("FUTURE_STATUS"))
        for value in cases:
            with self.subTest(value=value):
                client = FakeSqsClient(tasks=[value])
                exit_code, stdout, stderr = invoke(client, arguments("start", apply=True))
                self.assertEqual(exit_code, redrive_delivery_dlq.EXIT_INVALID)
                self.assertEqual(stdout, "")
                self.assertEqual(json.loads(stderr)["status"], "invalid")
                self.assertNotIn("start_message_move_task", [call for call, _ in client.calls])

    def test_cancel_requires_apply_and_exact_running_task(self):
        cases = (
            ("missing apply", [task()], TASK_HANDLE, False, redrive_delivery_dlq.EXIT_INVALID),
            ("wrong handle", [task()], "other", True, redrive_delivery_dlq.EXIT_REFUSED),
            ("completed", [task("COMPLETED")], TASK_HANDLE, True, redrive_delivery_dlq.EXIT_REFUSED),
            ("cancelling", [task("CANCELLING")], TASK_HANDLE, True, redrive_delivery_dlq.EXIT_REFUSED),
        )
        for name, tasks, handle, apply, expected in cases:
            with self.subTest(case=name):
                client = FakeSqsClient(tasks=tasks)
                exit_code, stdout, stderr = invoke(client, arguments("cancel", apply=apply, task_handle=handle))
                self.assertEqual(exit_code, expected)
                self.assertEqual(stdout, "")
                self.assertNotIn("cancel_message_move_task", [call for call, _ in client.calls])

        client = FakeSqsClient(tasks=[task()])
        exit_code, stdout, stderr = invoke(client, arguments("cancel", apply=True))
        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(json.loads(stdout)["status"], "cancel_requested")
        self.assertEqual(
            [kwargs for name, kwargs in client.calls if name == "cancel_message_move_task"],
            [{"TaskHandle": TASK_HANDLE}],
        )

    def test_transport_ambiguity_directs_status_before_retry(self):
        cases = (
            (
                "start",
                FakeSqsClient(start_error=EndpointConnectionError(endpoint_url=DLQ_URL)),
                arguments("start", apply=True),
                "start_message_move_task",
            ),
            (
                "cancel",
                FakeSqsClient(tasks=[task()], cancel_error=EndpointConnectionError(endpoint_url=DLQ_URL)),
                arguments("cancel", apply=True),
                "cancel_message_move_task",
            ),
        )
        for name, client, args, mutation in cases:
            with self.subTest(case=name):
                exit_code, stdout, stderr = invoke(client, args)
                document = json.loads(stderr)
                self.assertEqual(exit_code, redrive_delivery_dlq.EXIT_AMBIGUOUS)
                self.assertEqual(stdout, "")
                self.assertEqual(document["status"], "ambiguous")
                self.assertEqual(document["next_action"], "status")
                self.assertEqual([call for call, _ in client.calls].count(mutation), 1)

    def test_service_rejection_is_definitive_and_does_not_disclose_provider_text(self):
        error = ClientError(
            {"Error": {"Code": "UnsupportedOperation", "Message": "provider-secret-detail"}},
            "StartMessageMoveTask",
        )
        client = FakeSqsClient(start_error=error)

        exit_code, stdout, stderr = invoke(client, arguments("start", apply=True))

        self.assertEqual(exit_code, redrive_delivery_dlq.EXIT_REFUSED)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["status"], "refused")
        self.assertNotIn("provider-secret-detail", stderr)

    def test_pre_mutation_read_failure_is_distinct(self):
        class ReadFailureClient(FakeSqsClient):
            def get_queue_attributes(self, **kwargs):
                raise EndpointConnectionError(endpoint_url=SOURCE_URL)

        exit_code, stdout, stderr = invoke(ReadFailureClient(), arguments("preview"))

        self.assertEqual(exit_code, redrive_delivery_dlq.EXIT_AMBIGUOUS)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["status"], "read_failed")

    def test_cli_bounds_the_required_velocity(self):
        for value in ("0", "501", "not-an-integer"):
            with self.subTest(value=value):
                stderr = io.StringIO()
                with redirect_stderr(stderr), self.assertRaises(SystemExit):
                    redrive_delivery_dlq.parse_args(
                        [
                            "preview",
                            "--source-queue-url",
                            SOURCE_URL,
                            "--dlq-url",
                            DLQ_URL,
                            "--max-messages-per-second",
                            value,
                        ]
                    )
        parsed = redrive_delivery_dlq.parse_args(
            [
                "preview",
                "--source-queue-url",
                SOURCE_URL,
                "--dlq-url",
                DLQ_URL,
                "--max-messages-per-second",
                "500",
            ]
        )
        self.assertEqual(parsed.max_messages_per_second, 500)

    def test_documentation_pins_native_task_limits_and_the_later_revisit(self):
        runbook = " ".join((ROOT / "docs" / "runbooks" / "operations.md").read_text(encoding="utf-8").split())
        specification = " ".join(
            (ROOT / "docs" / "architecture" / "specification" / "06-acceptance-and-generation.md")
            .read_text(encoding="utf-8")
            .split()
        )
        self.assertIn("does not impose an exact message-count boundary", runbook)
        self.assertIn("new message IDs and enqueue times", runbook)
        self.assertIn("Revisit an exact finite-message redrive cap", runbook)
        self.assertIn("preview, start, inspect, and cancel", specification)


if __name__ == "__main__":
    unittest.main()
