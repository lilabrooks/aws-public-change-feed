"""The `DynamoDBDeliveryStore` against a table shaped like `infra/central`.

`outbox.py` documents the conditional expressions as the contract that makes the
delivery table the system of record, so these tests exercise them through the
real service API (moto) rather than through a hand-rolled fake: a claimed record
refuses a second claim, `mark_queued` refuses a stale dispatch ID, and the GSI
query returns due work oldest-first. The in-memory store tests in
`test_outbox.py` share the same conditions; the two stores must not drift.

moto does not evaluate conditional writes atomically under real concurrency
(see `test_releases.py`), so interleaving races stay covered by the dispatch
tests' injecting fake. Here each assertion is a single call whose outcome the
store's conditional expression determines.
"""

import json
import sys
import unittest
from pathlib import Path

import boto3
from moto import mock_aws

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aws_public_change_feed.identity import queue_dispatch_id  # noqa: E402
from aws_public_change_feed.outbox import DeliveryRecord, DynamoDBDeliveryStore  # noqa: E402

REGION = "us-west-2"
TABLE = "aws-public-change-feed-delivery"


def load_json(name):
    with (ROOT / "examples" / name).open(encoding="utf-8") as handle:
        return json.load(handle)


def delivery(store, key):
    """Load a delivery record the test knows exists, without the None branch."""

    record = store.get_delivery(key)
    assert record is not None
    return record


@mock_aws
class DynamoDeliveryStoreTests(unittest.TestCase):
    """The delivery table contract, one conditional write per assertion."""

    def setUp(self):
        self.client = boto3.client("dynamodb", region_name=REGION)
        self.client.create_table(
            TableName=TABLE,
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
            BillingMode="PAY_PER_REQUEST",
        )
        self.candidate = load_json("alert-candidate.json")
        self.request = load_json("delivery-request.json")
        self.key = self.candidate["candidate_id"]
        self.store = DynamoDBDeliveryStore(self.client, TABLE)

    def record(self, status="pending_queue", next_action_at=1000, **fields):
        return DeliveryRecord(
            candidate_id=self.key,
            destination_key="shared-aws-change-alerts",
            request=self.request,
            next_action_at=next_action_at,
            status=status,
            **fields,
        )

    def test_candidate_and_delivery_roundtrip_under_the_composite_key(self):
        self.assertTrue(self.store.put_candidate_if_absent(self.candidate))
        self.assertFalse(self.store.put_candidate_if_absent(self.candidate))
        self.assertEqual(self.store.get_candidate(self.key), self.candidate)

        self.assertTrue(self.store.put_delivery_if_absent(self.record()))
        self.assertFalse(self.store.put_delivery_if_absent(self.record()))
        record = delivery(self.store, self.key)
        self.assertEqual(record.status, "pending_queue")
        self.assertEqual(record.request, self.request)
        self.assertEqual(record.next_action_at, 1000)

    def test_a_claim_roundtrips_and_a_second_claim_is_conditional(self):
        self.store.put_delivery_if_absent(self.record())
        dispatch_id = queue_dispatch_id(self.request["request_id"], 1)

        self.assertEqual(
            self.store.claim_dispatch(
                self.key,
                expected_state_version=1,
                expected_generation=None,
                request_id=self.request["request_id"],
                due_before=2000,
            ),
            (1, dispatch_id),
        )
        record = delivery(self.store, self.key)
        self.assertEqual(record.dispatch_generation, 1)
        self.assertEqual(record.dispatch_id, dispatch_id)
        self.assertEqual(record.state_version, 2)

        self.assertIsNone(
            self.store.claim_dispatch(
                self.key,
                expected_state_version=record.state_version,
                expected_generation=record.dispatch_generation,
                request_id=self.request["request_id"],
                due_before=2000,
            ),
            msg="an active claim refuses a second concurrent generation",
        )

    def test_claim_refuses_work_that_is_not_due(self):
        self.store.put_delivery_if_absent(self.record(next_action_at=5000))
        self.assertIsNone(
            self.store.claim_dispatch(
                self.key,
                expected_state_version=1,
                expected_generation=None,
                request_id=self.request["request_id"],
                due_before=2000,
            )
        )

    def test_claim_refuses_a_resolved_record(self):
        self.store.put_delivery_if_absent(self.record(status="queued"))
        self.assertIsNone(
            self.store.claim_dispatch(
                self.key,
                expected_state_version=1,
                expected_generation=None,
                request_id=self.request["request_id"],
                due_before=2000,
            )
        )

    def test_claim_refuses_a_stale_state_version_when_the_generation_matches(self):
        self.store.put_delivery_if_absent(
            self.record(status="failed_retryable", dispatch_generation=1, state_version=7)
        )

        stale = self.store.claim_dispatch(
            self.key,
            expected_state_version=4,
            expected_generation=1,
            request_id=self.request["request_id"],
            due_before=2000,
        )

        self.assertIsNone(stale)
        record = delivery(self.store, self.key)
        self.assertEqual(record.dispatch_generation, 1)
        self.assertEqual(record.state_version, 7)

    def test_claim_refuses_a_stale_generation_when_the_state_version_matches(self):
        self.store.put_delivery_if_absent(
            self.record(status="failed_retryable", dispatch_generation=2, state_version=7)
        )

        stale = self.store.claim_dispatch(
            self.key,
            expected_state_version=7,
            expected_generation=1,
            request_id=self.request["request_id"],
            due_before=2000,
        )

        self.assertIsNone(stale)
        record = delivery(self.store, self.key)
        self.assertEqual(record.dispatch_generation, 2)
        self.assertEqual(record.state_version, 7)

    def test_mark_queued_is_bound_to_the_claimed_dispatch_id(self):
        self.store.put_delivery_if_absent(self.record(next_action_at=1000))
        wrong = queue_dispatch_id(self.request["request_id"], 9)
        self.assertFalse(self.store.mark_queued(self.key, dispatch_id=wrong, message_id="m", at=2000))

        claimed = queue_dispatch_id(self.request["request_id"], 1)
        self.assertEqual(
            self.store.claim_dispatch(
                self.key,
                expected_state_version=1,
                expected_generation=None,
                request_id=self.request["request_id"],
                due_before=2000,
            ),
            (1, claimed),
        )
        self.assertTrue(self.store.mark_queued(self.key, dispatch_id=claimed, message_id="m", at=2000))

        record = delivery(self.store, self.key)
        self.assertEqual(record.status, "queued")
        self.assertEqual(record.queue_message_id, "m")
        self.assertEqual(record.next_action_at, 2000)
        self.assertEqual(record.state_version, 3)

    def test_mark_queued_refuses_a_record_that_left_the_scheduled_states(self):
        self.store.put_delivery_if_absent(self.record(status="queued"))
        claimed = queue_dispatch_id(self.request["request_id"], 1)
        self.assertFalse(self.store.mark_queued(self.key, dispatch_id=claimed, message_id="m2", at=2000))

    def test_query_due_reads_the_gsi_oldest_first(self):
        for index, (key, when) in enumerate(
            [
                ("first-candidate", 2000),
                ("second-candidate", 500),
                ("retry-candidate", 1500),
            ]
        ):
            record = DeliveryRecord(
                candidate_id=key,
                destination_key="shared-aws-change-alerts",
                request=self.request,
                next_action_at=when,
                status="pending_queue" if index != 2 else "failed_retryable",
            )
            self.store.put_delivery_if_absent(record)

        self.assertEqual(
            self.store.query_due("pending_queue", due_before=2500, limit=10),
            ((500, "second-candidate"), (2000, "first-candidate")),
        )
        self.assertEqual(
            self.store.query_due("failed_retryable", due_before=2500, limit=10), ((1500, "retry-candidate"),)
        )
        self.assertEqual(self.store.query_due("queued", due_before=2500, limit=10), ())

    def test_query_due_respects_the_limit(self):
        self.store.put_delivery_if_absent(self.record(next_action_at=100))
        self.store.put_delivery_if_absent(
            DeliveryRecord(
                candidate_id="other",
                destination_key="shared-aws-change-alerts",
                request=self.request,
                next_action_at=200,
            )
        )
        self.assertEqual(self.store.query_due("pending_queue", due_before=300, limit=1), ((100, self.key),))

    def test_request_roundtrips_through_the_json_document(self):
        self.store.put_delivery_if_absent(self.record())
        record = delivery(self.store, self.key)
        self.assertEqual(record.request, self.request)
        self.assertEqual(record.request["candidate"], self.candidate)


if __name__ == "__main__":
    unittest.main()
