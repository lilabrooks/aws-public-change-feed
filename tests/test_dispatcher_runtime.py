"""Scheduled dispatcher Lambda boundary, metrics, and AWS composition."""

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import boto3
import yaml
from moto import mock_aws

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import aws_public_change_feed.dispatcher_runtime as runtime_module  # noqa: E402
from aws_public_change_feed.dispatch import DispatchResult, DispatchSendRejected  # noqa: E402
from aws_public_change_feed.dispatcher_runtime import EmbeddedDispatcherMetrics, lambda_handler  # noqa: E402
from aws_public_change_feed.identity import delivery_request_id  # noqa: E402
from aws_public_change_feed.outbox import DeliveryRecord, DynamoDBDeliveryStore  # noqa: E402

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
EVENT = {
    "version": "0",
    "id": "scheduled-event-1",
    "detail-type": "Scheduled Event",
    "source": "aws.events",
    "account": "667653114001",
    "time": "2026-08-11T12:00:00Z",
    "region": "us-east-1",
    "resources": ["arn:aws:events:us-east-1:667653114001:rule/apcf-dev-outbox-dispatcher"],
    "detail": {},
}
ENVIRONMENT = {
    "DELIVERY_TABLE_NAME": "delivery",
    "DELIVERY_INDEX_NAME": "status-next-action-index",
    "DELIVERY_QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/667653114001/delivery.fifo",
    "MAX_DELIVERY_REQUEST_BYTES": "245760",
    "METRICS_NAMESPACE": "AWSPublicChangeFeed/dev",
    "AWS_LAMBDA_FUNCTION_NAME": "apcf-dev-outbox-dispatcher",
}


def load_json(name):
    with (ROOT / "examples" / name).open(encoding="utf-8") as handle:
        return json.load(handle)


def load_yaml(name):
    with (ROOT / "examples" / name).open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


class FakeRuntime:
    def __init__(self, result=None, error=None, metric=None):
        self.result = result
        self.error = error
        self.metric = metric
        self.calls = []

    def run(self, now, metrics):
        self.calls.append((now, metrics))
        if self.metric is not None:
            getattr(metrics, self.metric)()
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


class EmbeddedDispatcherMetricsTests(unittest.TestCase):
    def test_metrics_have_fixed_names_and_bounded_dimensions(self):
        documents: list[str] = []
        metrics = EmbeddedDispatcherMetrics(
            "AWSPublicChangeFeed/dev",
            "apcf-dev-outbox-dispatcher",
            clock=lambda: NOW,
            emit=documents.append,
        )
        metrics.heartbeat()
        metrics.dispatch_attempted()
        metrics.dispatch_accepted()
        metrics.dispatch_unknown()
        metrics.flush()

        parsed = [json.loads(document) for document in documents]
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]["_aws"]["CloudWatchMetrics"][0]["Dimensions"], [["Function"]])
        self.assertEqual(parsed[0]["Function"], "apcf-dev-outbox-dispatcher")
        self.assertEqual(parsed[1]["_aws"]["CloudWatchMetrics"][0]["Dimensions"], [[]])
        definitions = parsed[1]["_aws"]["CloudWatchMetrics"][0]["Metrics"]
        self.assertEqual(
            {definition["Name"] for definition in definitions},
            {"DispatchAccepted", "DispatchAttempted", "DispatchUnknownOutcome"},
        )


class LambdaHandlerTests(unittest.TestCase):
    def tearDown(self):
        runtime_module._runtime = None

    def invoke(self, runtime, *, event=EVENT, environment=ENVIRONMENT):
        runtime_module._runtime = runtime
        output = io.StringIO()
        with patch.dict(os.environ, environment, clear=True), redirect_stdout(output):
            result = lambda_handler(event, object())
        return result, output.getvalue()

    def test_empty_run_returns_bounded_zero_counts_and_heartbeat(self):
        runtime = FakeRuntime(DispatchResult())

        result, output = self.invoke(runtime)

        self.assertEqual(
            result,
            {"considered": 0, "accepted": 0, "unknown": 0, "failed_transitions": 0},
        )
        self.assertIn("Heartbeat", output)
        self.assertEqual(len(runtime.calls), 1)
        self.assertIsNotNone(runtime.calls[0][0].tzinfo)

    def test_unknown_send_is_a_successful_bounded_fact(self):
        runtime = FakeRuntime(
            DispatchResult(considered=1, new_claims=1, unknown=1),
            metric="dispatch_unknown",
        )

        result, output = self.invoke(runtime)

        self.assertEqual(result["unknown"], 1)
        self.assertEqual(result["accepted"], 0)
        self.assertIn("DispatchUnknownOutcome", output)

    def test_malformed_event_and_missing_environment_fail_before_heartbeat_or_runtime(self):
        runtime = FakeRuntime(DispatchResult())
        for event, environment in (({}, ENVIRONMENT), (EVENT, {})):
            output = io.StringIO()
            runtime_module._runtime = runtime
            with (
                self.subTest(event=event, environment=environment),
                patch.dict(os.environ, environment, clear=True),
                redirect_stdout(output),
            ):
                with self.assertRaises(ValueError):
                    lambda_handler(event, object())
                self.assertNotIn("Heartbeat", output.getvalue())
        self.assertEqual(runtime.calls, [])

    def test_definite_rejection_uses_a_fixed_error_without_leaking_detail(self):
        secret_detail = "candidate-secret-detail"
        runtime = FakeRuntime(error=DispatchSendRejected(secret_detail))
        output = io.StringIO()

        with patch.dict(os.environ, ENVIRONMENT, clear=True), redirect_stdout(output):
            runtime_module._runtime = runtime
            with self.assertRaisesRegex(RuntimeError, "^dispatcher invocation failed$") as caught:
                lambda_handler(EVENT, object())

        self.assertNotIn(secret_detail, str(caught.exception))
        self.assertNotIn(secret_detail, output.getvalue())
        self.assertIn("Heartbeat", output.getvalue())

    def test_unexpected_adapter_fault_uses_the_same_fixed_error(self):
        runtime = FakeRuntime(error=ValueError("private adapter detail"))

        with self.assertRaisesRegex(RuntimeError, "^dispatcher invocation failed$"):
            self.invoke(runtime)


@mock_aws
class MotoDispatcherCompositionTests(unittest.TestCase):
    def setUp(self):
        runtime_module._runtime = None
        self.addCleanup(setattr, runtime_module, "_runtime", None)
        self.dynamodb = boto3.client("dynamodb", region_name="us-east-1")
        self.dynamodb.create_table(
            TableName="delivery",
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
                {"AttributeName": "status", "AttributeType": "S"},
                {"AttributeName": "next_action_at", "AttributeType": "N"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "status-next-action-index",
                    "KeySchema": [
                        {"AttributeName": "status", "KeyType": "HASH"},
                        {"AttributeName": "next_action_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
        )
        self.sqs = boto3.client("sqs", region_name="us-east-1")
        self.queue_url = self.sqs.create_queue(
            QueueName="delivery.fifo",
            Attributes={"FifoQueue": "true", "ContentBasedDeduplication": "false"},
        )["QueueUrl"]

    def test_real_aws_root_dispatches_one_due_record_and_marks_it_queued(self):
        request = load_json("delivery-request.json")
        candidate_id = "c" * 64
        request["candidate"]["candidate_id"] = candidate_id
        request["request_id"] = delivery_request_id(candidate_id)
        store = DynamoDBDeliveryStore(self.dynamodb, "delivery", "status-next-action-index")
        self.assertTrue(
            store.put_delivery_if_absent(
                DeliveryRecord(
                    candidate_id=candidate_id,
                    destination_key=request["destination_key"],
                    request=request,
                    status="pending_queue",
                    next_action_at=0,
                )
            )
        )
        environment = {
            **ENVIRONMENT,
            "AWS_DEFAULT_REGION": "us-east-1",
            "DELIVERY_QUEUE_URL": self.queue_url,
            "MAX_DELIVERY_REQUEST_BYTES": str(load_yaml("config.yaml")["message_policy"]["max_delivery_request_bytes"]),
        }

        with patch.dict(os.environ, environment, clear=False), redirect_stdout(io.StringIO()):
            result = lambda_handler(EVENT, object())

        self.assertEqual(result, {"considered": 1, "accepted": 1, "unknown": 0, "failed_transitions": 0})
        durable = store.get_delivery(candidate_id)
        self.assertIsNotNone(durable)
        assert durable is not None
        self.assertEqual(durable.status, "queued")
        messages = self.sqs.receive_message(QueueUrl=self.queue_url, MaxNumberOfMessages=1)
        self.assertEqual(len(messages.get("Messages", [])), 1)


if __name__ == "__main__":
    unittest.main()
