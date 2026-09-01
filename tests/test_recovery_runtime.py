"""Scheduled recovery Lambda boundary and bounded EMF metrics."""

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

import aws_public_change_feed.recovery_runtime as runtime_module  # noqa: E402
from aws_public_change_feed.dispatch import DispatchResult  # noqa: E402
from aws_public_change_feed.identity import delivery_request_id  # noqa: E402
from aws_public_change_feed.outbox import DeliveryRecord, DynamoDBDeliveryStore  # noqa: E402
from aws_public_change_feed.recovery import RecoveryResult, StateObservation  # noqa: E402
from aws_public_change_feed.recovery_runtime import EmbeddedRecoveryMetrics, lambda_handler  # noqa: E402

NOW = datetime(2026, 8, 10, 19, 0, tzinfo=UTC)
EVENT = {
    "version": "0",
    "id": "scheduled-event-1",
    "detail-type": "Scheduled Event",
    "source": "aws.events",
    "account": "667653114001",
    "time": "2026-08-10T19:00:00Z",
    "region": "us-east-1",
    "resources": ["arn:aws:events:us-east-1:667653114001:rule/apcf-dev-recovery-reconciler"],
    "detail": {},
}
ENVIRONMENT = {
    "METRICS_NAMESPACE": "AWSPublicChangeFeed/dev",
    "AWS_LAMBDA_FUNCTION_NAME": "apcf-dev-recovery-reconciler",
}


def load_json(name):
    with (ROOT / "examples" / name).open(encoding="utf-8") as handle:
        return json.load(handle)


def load_yaml(name):
    with (ROOT / "examples" / name).open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


class FakeRuntime:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def run(self, now, metrics):
        self.calls.append((now, metrics))
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


def complete_result(**fields):
    return RecoveryResult(
        dispatch=fields.pop("dispatch", DispatchResult()),
        observations=fields.pop(
            "observations",
            tuple(StateObservation(state, 0, 0, False) for state in runtime_module._STATE_ALLOWLIST),
        ),
        **fields,
    )


class EmbeddedRecoveryMetricsTests(unittest.TestCase):
    def test_metrics_have_only_fixed_names_and_bounded_dimensions(self):
        documents: list[str] = []
        metrics = EmbeddedRecoveryMetrics(
            "AWSPublicChangeFeed/dev",
            "apcf-dev-recovery-reconciler",
            clock=lambda: NOW,
            emit=documents.append,
        )
        metrics.heartbeat()
        metrics.dispatch_attempted()
        metrics.dispatch_accepted()
        metrics.expired_lease_unknown()
        metrics.stale_queued(2)
        metrics.state_observed("queued", count=3, oldest_age_seconds=601)
        metrics.state_observation_saturated("queued")
        metrics.flush()

        parsed = [json.loads(document) for document in documents]
        self.assertEqual(len(parsed), 6)
        heartbeat = parsed[0]
        self.assertEqual(heartbeat["_aws"]["CloudWatchMetrics"][0]["Dimensions"], [["Function"]])
        self.assertEqual(heartbeat["Function"], "apcf-dev-recovery-reconciler")
        dimensionless = parsed[1]
        self.assertEqual(dimensionless["_aws"]["CloudWatchMetrics"][0]["Dimensions"], [[]])
        self.assertEqual(dimensionless["DeliveryUnknown"], 1)
        self.assertEqual(dimensionless["ExpiredLeaseUnknown"], 1)
        self.assertEqual(dimensionless["StaleQueued"], 2)
        state_documents = {document["State"]: document for document in parsed[2:]}
        self.assertEqual(set(state_documents), set(runtime_module._STATE_ALLOWLIST))
        self.assertEqual(state_documents["queued"]["ObservedDeliveryCount"], 3)
        self.assertEqual(state_documents["queued"]["OldestDeliveryAgeSeconds"], 601)
        self.assertEqual(state_documents["queued"]["StateObservationSaturated"], 1)
        self.assertEqual(state_documents["queued"]["_aws"]["CloudWatchMetrics"][0]["Dimensions"], [["State"]])

    def test_a_state_outside_the_allowlist_is_refused(self):
        metrics = EmbeddedRecoveryMetrics("AWSPublicChangeFeed/dev", "reconciler")
        with self.assertRaisesRegex(ValueError, "not allowlisted"):
            metrics.state_observed("candidate-id", count=1, oldest_age_seconds=1)


class LambdaHandlerTests(unittest.TestCase):
    def tearDown(self):
        runtime_module._runtime = None

    def invoke(self, runtime, event=EVENT):
        runtime_module._runtime = runtime
        output = io.StringIO()
        with patch.dict(os.environ, ENVIRONMENT, clear=False), redirect_stdout(output):
            result = lambda_handler(event, object())
        return result, output.getvalue()

    def test_a_complete_run_returns_only_bounded_counts_and_emits_heartbeat(self):
        runtime = FakeRuntime(
            complete_result(
                dispatch=DispatchResult(accepted=2),
                expired_leases=1,
                stale_queued=4,
                conditional_races=1,
            )
        )

        result, output = self.invoke(runtime)

        self.assertEqual(result, {"repaired": 3, "stale_queued": 4, "conditional_races": 1})
        documents = [json.loads(line) for line in output.splitlines()]
        self.assertTrue(any(document.get("Heartbeat") == 1 for document in documents))
        self.assertEqual(len(runtime.calls), 1)
        self.assertIsNotNone(runtime.calls[0][0].tzinfo)

    def test_a_saturated_run_fails_loudly_after_emitting_metrics(self):
        observations = tuple(
            StateObservation(state, 100, 10, state == "queued") for state in runtime_module._STATE_ALLOWLIST
        )
        runtime = FakeRuntime(complete_result(observations=observations))
        output = io.StringIO()

        with patch.dict(os.environ, ENVIRONMENT, clear=False), redirect_stdout(output):
            runtime_module._runtime = runtime
            with self.assertRaisesRegex(RuntimeError, "recovery invocation incomplete"):
                lambda_handler(EVENT, object())

        self.assertIn("Heartbeat", output.getvalue())
        self.assertNotIn("ReconcilerFault", output.getvalue())

    def test_a_conditional_race_is_a_complete_run_not_a_retry_loop(self):
        runtime = FakeRuntime(complete_result(conditional_races=1))
        result, _ = self.invoke(runtime)
        self.assertEqual(result["conditional_races"], 1)

    def test_a_malformed_schedule_event_is_refused_before_runtime_work(self):
        runtime = FakeRuntime(complete_result())
        for event in ({}, {**EVENT, "source": "attacker"}, {**EVENT, "detail": "not-an-object"}):
            with self.subTest(event=event):
                runtime_module._runtime = runtime
                with self.assertRaisesRegex(ValueError, "invalid scheduled recovery event"):
                    lambda_handler(event, object())
        self.assertEqual(runtime.calls, [])

    def test_an_unexpected_adapter_fault_uses_a_fixed_error_and_does_not_log_its_detail(self):
        secret_detail = "candidate-secret-detail"
        runtime = FakeRuntime(error=RuntimeError(secret_detail))
        output = io.StringIO()

        with patch.dict(os.environ, ENVIRONMENT, clear=False), redirect_stdout(output):
            runtime_module._runtime = runtime
            with self.assertRaisesRegex(RuntimeError, "^recovery invocation failed$") as caught:
                lambda_handler(EVENT, object())

        self.assertNotIn(secret_detail, str(caught.exception))
        self.assertNotIn(secret_detail, output.getvalue())
        self.assertIn("ReconcilerFault", output.getvalue())

    def test_an_adapter_cannot_claim_incompletion_by_reusing_the_old_error_text(self):
        runtime = FakeRuntime(error=RuntimeError("recovery invocation incomplete"))
        output = io.StringIO()

        with patch.dict(os.environ, ENVIRONMENT, clear=False), redirect_stdout(output):
            runtime_module._runtime = runtime
            with self.assertRaisesRegex(RuntimeError, "^recovery invocation failed$"):
                lambda_handler(EVENT, object())

        self.assertIn("ReconcilerFault", output.getvalue())


@mock_aws
class MotoRecoveryCompositionTests(unittest.TestCase):
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
            Attributes={"FifoQueue": "true"},
        )["QueueUrl"]

    def test_the_real_aws_composition_root_dispatches_one_due_record(self):
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
            "DELIVERY_TABLE_NAME": "delivery",
            "DELIVERY_INDEX_NAME": "status-next-action-index",
            "DELIVERY_QUEUE_URL": self.queue_url,
            "MAX_DELIVERY_REQUEST_BYTES": str(load_yaml("config.yaml")["message_policy"]["max_delivery_request_bytes"]),
            "RECOVERY_REPAIR_LIMIT": "100",
            "RECOVERY_OBSERVATION_LIMIT": "101",
            "RECOVERY_STALE_QUEUED_SECONDS": "600",
        }

        with patch.dict(os.environ, environment, clear=False), redirect_stdout(io.StringIO()):
            result = lambda_handler(EVENT, object())

        self.assertEqual(result["repaired"], 1)
        durable = store.get_delivery(candidate_id)
        self.assertIsNotNone(durable)
        assert durable is not None
        self.assertEqual(durable.status, "queued")
        messages = self.sqs.receive_message(QueueUrl=self.queue_url, MaxNumberOfMessages=1)
        self.assertEqual(len(messages.get("Messages", [])), 1)
