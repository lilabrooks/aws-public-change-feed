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
from typing import Any

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


@mock_aws
class DynamoWorkerTransitionTests(DynamoDeliveryStoreTests):
    """The worker's own conditional writes, against the same real table.

    The dispatcher's operations were covered here from the start and the
    worker's were not, so `claim_sending`, `record_outcome`, `schedule_retry`,
    and the two pacing operations ran only against `InMemoryOutboxStore`. Two
    stores implementing one contract by hand is exactly where they drift, and
    the in-memory version is the one written to agree with the worker.
    """

    def queued(self, **fields):
        """A record already dispatched to the queue, ready for a worker."""

        self.store.put_delivery_if_absent(self.record(status="queued", next_action_at=1000, **fields))
        return delivery(self.store, self.key)

    def test_claim_sending_writes_the_lease_and_bumps_the_state_version(self):
        record = self.queued()

        self.assertTrue(
            self.store.claim_sending(
                self.key,
                expected_state_version=record.state_version,
                attempt_id="attempt-1",
                lease_expires_at=1030,
            )
        )

        claimed = delivery(self.store, self.key)
        self.assertEqual(claimed.status, "sending")
        self.assertEqual(claimed.attempt_id, "attempt-1")
        self.assertEqual(claimed.lease_expires_at, 1030)
        # The index range key follows the lease so the reconciler can find it.
        self.assertEqual(claimed.next_action_at, 1030)
        self.assertEqual(claimed.state_version, record.state_version + 1)

    def test_a_second_claim_on_the_same_state_version_is_refused(self):
        record = self.queued()
        self.assertTrue(
            self.store.claim_sending(
                self.key, expected_state_version=record.state_version, attempt_id="a1", lease_expires_at=1030
            )
        )

        self.assertFalse(
            self.store.claim_sending(
                self.key, expected_state_version=record.state_version, attempt_id="a2", lease_expires_at=1030
            )
        )
        self.assertEqual(delivery(self.store, self.key).attempt_id, "a1")

    def test_claim_sending_refuses_a_record_that_is_not_queued(self):
        self.store.put_delivery_if_absent(self.record(status="pending_queue"))
        record = delivery(self.store, self.key)

        self.assertFalse(
            self.store.claim_sending(
                self.key, expected_state_version=record.state_version, attempt_id="a1", lease_expires_at=1030
            )
        )

    def test_record_outcome_clears_the_lease_and_the_dispatch_claim(self):
        record = self.queued()
        self.store.claim_sending(
            self.key, expected_state_version=record.state_version, attempt_id="a1", lease_expires_at=1030
        )
        claimed = delivery(self.store, self.key)

        self.assertTrue(
            self.store.record_outcome(
                self.key,
                expected_state_version=claimed.state_version,
                attempt_id="a1",
                status="posted",
                network_attempt_count=1,
                next_action_at=None,
                slack_response={"response_class": "http_200", "latency_ms": 12, "bytes_sent": True},
                expires_at=99999,
            )
        )

        resolved = delivery(self.store, self.key)
        self.assertEqual(resolved.status, "posted")
        self.assertIsNone(resolved.attempt_id)
        self.assertIsNone(resolved.lease_expires_at)
        self.assertIsNone(resolved.next_action_at)
        self.assertIsNone(resolved.dispatch_id)
        self.assertEqual(resolved.network_attempt_count, 1)
        self.assertEqual(resolved.expires_at, 99999)
        assert resolved.slack_response is not None
        self.assertEqual(resolved.slack_response["response_class"], "http_200")

    def test_record_outcome_refuses_a_superseded_attempt_id(self):
        """The lost-write branch the worker now rereads the record for."""

        record = self.queued()
        self.store.claim_sending(
            self.key, expected_state_version=record.state_version, attempt_id="a1", lease_expires_at=1030
        )
        claimed = delivery(self.store, self.key)

        self.assertFalse(
            self.store.record_outcome(
                self.key,
                expected_state_version=claimed.state_version,
                attempt_id="somebody-elses-attempt",
                status="posted",
                network_attempt_count=1,
                next_action_at=None,
                slack_response=None,
                expires_at=99999,
            )
        )
        self.assertEqual(delivery(self.store, self.key).status, "sending")

    def test_record_outcome_refuses_a_stale_state_version(self):
        record = self.queued()
        self.store.claim_sending(
            self.key, expected_state_version=record.state_version, attempt_id="a1", lease_expires_at=1030
        )

        self.assertFalse(
            self.store.record_outcome(
                self.key,
                expected_state_version=record.state_version,
                attempt_id="a1",
                status="posted",
                network_attempt_count=1,
                next_action_at=None,
                slack_response=None,
                expires_at=99999,
            )
        )

    def test_a_retryable_outcome_carries_a_next_action_and_no_ttl(self):
        record = self.queued()
        self.store.claim_sending(
            self.key, expected_state_version=record.state_version, attempt_id="a1", lease_expires_at=1030
        )
        claimed = delivery(self.store, self.key)

        self.assertTrue(
            self.store.record_outcome(
                self.key,
                expected_state_version=claimed.state_version,
                attempt_id="a1",
                status="failed_retryable",
                network_attempt_count=2,
                next_action_at=1200,
                slack_response={"response_class": "http_503"},
                expires_at=None,
            )
        )

        resolved = delivery(self.store, self.key)
        self.assertEqual(resolved.status, "failed_retryable")
        self.assertEqual(resolved.next_action_at, 1200)
        self.assertIsNone(resolved.expires_at)
        # Back in the index, so the dispatcher queues it again when due.
        self.assertEqual(self.store.query_due("failed_retryable", due_before=1300, limit=10), ((1200, self.key),))

    def test_schedule_retry_defers_a_queued_record_and_clears_the_claim(self):
        record = self.queued()
        self.assertTrue(
            self.store.schedule_retry(
                self.key,
                expected_state_version=record.state_version,
                next_action_at=1500,
                slack_response=None,
            )
        )

        deferred = delivery(self.store, self.key)
        self.assertEqual(deferred.status, "failed_retryable")
        self.assertEqual(deferred.next_action_at, 1500)
        self.assertIsNone(deferred.dispatch_id)

    def test_schedule_retry_refuses_a_stale_state_version(self):
        record = self.queued()

        self.assertFalse(
            self.store.schedule_retry(
                self.key,
                expected_state_version=record.state_version + 5,
                next_action_at=1500,
                slack_response=None,
            )
        )

    def test_pacing_is_created_once_and_then_versioned(self):
        destination = "shared-aws-change-alerts"
        self.assertIsNone(self.store.get_pace(destination))

        self.assertTrue(
            self.store.update_pace(
                destination, expected_version=None, next_allowed_at=1001, last_response_class="http_200"
            )
        )
        # A second create loses: the item now exists.
        self.assertFalse(
            self.store.update_pace(
                destination, expected_version=None, next_allowed_at=2000, last_response_class="http_429"
            )
        )

        pace = self.store.get_pace(destination)
        assert pace is not None
        self.assertEqual(pace.version, 1)
        self.assertEqual(pace.next_allowed_at, 1001)
        self.assertEqual(pace.last_response_class, "http_200")

    def test_a_pacing_update_on_a_stale_version_is_refused(self):
        destination = "shared-aws-change-alerts"
        self.store.update_pace(destination, expected_version=None, next_allowed_at=1001, last_response_class="http_200")

        self.assertTrue(
            self.store.update_pace(
                destination, expected_version=1, next_allowed_at=1002, last_response_class="http_200"
            )
        )
        self.assertFalse(
            self.store.update_pace(
                destination, expected_version=1, next_allowed_at=9999, last_response_class="http_500"
            )
        )

        pace = self.store.get_pace(destination)
        assert pace is not None
        self.assertEqual(pace.version, 2)
        self.assertEqual(pace.next_allowed_at, 1002)

    def test_a_pacing_update_without_a_response_class_removes_the_stale_one(self):
        destination = "shared-aws-change-alerts"
        self.store.update_pace(destination, expected_version=None, next_allowed_at=1001, last_response_class="http_429")

        self.assertTrue(
            self.store.update_pace(destination, expected_version=1, next_allowed_at=1002, last_response_class=None)
        )

        pace = self.store.get_pace(destination)
        assert pace is not None
        self.assertIsNone(pace.last_response_class)

    def test_an_expired_resolved_item_is_replaced_but_an_unresolved_one_is_not(self):
        """The TTL replace condition, both branches, against the real table.

        DynamoDB deletes expired items asynchronously, so a replay can meet an
        item whose TTL has passed. Replacing it is safe only because a TTL is
        proof the record was resolved — which is why the status clause has to
        be in the condition and not merely in the calling code.
        """

        self.assertTrue(
            self.store.put_delivery_if_absent(
                self.record(status="posted", next_action_at=None, expires_at=500), now=400
            )
        )
        # Same item, now past its TTL: a replay may take the key back.
        self.assertTrue(
            self.store.put_delivery_if_absent(
                self.record(status="pending_queue", next_action_at=1000),
                now=600,
            )
        )
        self.assertEqual(delivery(self.store, self.key).status, "pending_queue")

    def test_an_unresolved_item_with_a_corrupt_ttl_is_not_replaced(self):
        """`DeliveryRecord` refuses this shape, so the item is written raw.

        The record type is the first guard and this condition is the second.
        Writing the malformed item directly is the only way to reach the
        second one, and reaching it is the point: an operator or an older
        writer could leave a TTL on live work, and a replay must not then
        overwrite a `sending` record that a worker is holding.
        """

        self.client.put_item(
            TableName=TABLE,
            Item={
                "PK": {"S": f"CANDIDATE#{self.key}"},
                "SK": {"S": "DELIVERY"},
                "destination_key": {"S": "shared-aws-change-alerts"},
                "request": {"S": json.dumps(self.request, sort_keys=True)},
                "status": {"S": "sending"},
                "state_version": {"N": "3"},
                "created_at": {"S": "2026-07-13T17:30:00Z"},
                "attempt_id": {"S": "a1"},
                "lease_expires_at": {"N": "700"},
                "next_action_at": {"N": "700"},
                "network_attempt_count": {"N": "1"},
                "expires_at": {"N": "500"},
            },
        )

        self.assertFalse(
            self.store.put_delivery_if_absent(self.record(status="pending_queue", next_action_at=1000), now=600)
        )

        # Read raw rather than through `get_delivery`: decoding this item
        # raises, because `DeliveryRecord` refuses the very shape the item was
        # written in. That is the first guard doing its job, and it is why the
        # surviving state has to be inspected at the item level here.
        stored = self.client.get_item(
            TableName=TABLE,
            Key={"PK": {"S": f"CANDIDATE#{self.key}"}, "SK": {"S": "DELIVERY"}},
            ConsistentRead=True,
        )["Item"]
        self.assertEqual(stored["status"]["S"], "sending")
        self.assertEqual(stored["attempt_id"]["S"], "a1")

    def test_an_expired_delivery_unknown_is_never_replaced_by_a_replay(self):
        """ADR-004 keeps unknown outcomes for operator review.

        `delivery_unknown` is acknowledged — no delivery work is scheduled —
        but chapter 02 permits only `posted` and `failed_terminal` to expire.
        Treating the two sets as one let a replay overwrite the record an
        operator needs in order to check Slack and decide on a manual replay,
        which is the evidence the whole unknown state exists to preserve.

        The item is written raw because `DeliveryRecord` now refuses this
        shape, and refusing it is the first of the two guards; this test
        exercises the second.
        """

        self.client.put_item(
            TableName=TABLE,
            Item={
                "PK": {"S": f"CANDIDATE#{self.key}"},
                "SK": {"S": "DELIVERY"},
                "destination_key": {"S": "shared-aws-change-alerts"},
                "request": {"S": json.dumps(self.request, sort_keys=True)},
                "status": {"S": "delivery_unknown"},
                "state_version": {"N": "4"},
                "created_at": {"S": "2026-07-13T17:30:00Z"},
                "network_attempt_count": {"N": "1"},
                "expires_at": {"N": "500"},
            },
        )

        self.assertFalse(
            self.store.put_delivery_if_absent(self.record(status="pending_queue", next_action_at=1000), now=600)
        )

        stored = self.client.get_item(
            TableName=TABLE,
            Key={"PK": {"S": f"CANDIDATE#{self.key}"}, "SK": {"S": "DELIVERY"}},
            ConsistentRead=True,
        )["Item"]
        self.assertEqual(stored["status"]["S"], "delivery_unknown")

    def test_a_delivery_unknown_record_cannot_carry_a_ttl_at_all(self):
        """The first guard, so the raw write above is the only way in."""

        with self.assertRaises(ValueError) as caught:
            self.record(status="delivery_unknown", next_action_at=None, expires_at=500)

        self.assertIn("delivery_unknown", str(caught.exception))

    def test_record_outcome_refuses_a_ttl_on_an_unresolved_state(self):
        """The stores must refuse the same operation, not just one of them.

        `InMemoryOutboxStore` goes through `dataclasses.replace` and so
        inherits `DeliveryRecord`'s validation for free. This store builds the
        item attribute by attribute, and without an explicit check it wrote a
        `failed_retryable` carrying a TTL that its own decoder then refused to
        read back — a write the store could make and not undo.
        """

        self.queued()
        record = delivery(self.store, self.key)
        self.store.claim_sending(
            self.key, expected_state_version=record.state_version, attempt_id="a1", lease_expires_at=1030
        )
        claimed = delivery(self.store, self.key)

        with self.assertRaises(ValueError):
            self.store.record_outcome(
                self.key,
                expected_state_version=claimed.state_version,
                attempt_id="a1",
                status="failed_retryable",
                network_attempt_count=1,
                next_action_at=1200,
                slack_response=None,
                expires_at=99999,
            )

        # Nothing was written, so the claim survives for a correct retry.
        self.assertEqual(delivery(self.store, self.key).status, "sending")

    def test_record_outcome_refuses_a_ttl_on_delivery_unknown(self):
        self.queued()
        record = delivery(self.store, self.key)
        self.store.claim_sending(
            self.key, expected_state_version=record.state_version, attempt_id="a1", lease_expires_at=1030
        )
        claimed = delivery(self.store, self.key)

        with self.assertRaises(ValueError):
            self.store.record_outcome(
                self.key,
                expected_state_version=claimed.state_version,
                attempt_id="a1",
                status="delivery_unknown",
                network_attempt_count=1,
                next_action_at=None,
                slack_response=None,
                expires_at=99999,
            )

    def test_no_transition_can_write_a_record_this_store_cannot_read(self):
        """Store parity, as one table rather than one case per invariant.

        `InMemoryOutboxStore` routes every transition through
        `dataclasses.replace`, so `DeliveryRecord.__post_init__` judges the
        whole proposed record. This store writes update expressions attribute
        by attribute and constructs nothing, so each of these once wrote
        successfully and then raised from `get_delivery` on the very next
        read — a record the store could create and never load again.

        The assertion is deliberately "refused before the write" rather than
        "refused somewhere": a write that lands and is then unreadable has
        already stranded the record.
        """

        outcomes: dict[str, dict[str, Any]] = {
            "unknown status": {"status": "bogus"},
            "retryable without a next action": {"status": "failed_retryable", "next_action_at": None},
            "sending without a lease": {"status": "sending"},
            "negative attempt count": {"network_attempt_count": -5},
            "ttl on an unresolved state": {"status": "failed_retryable", "next_action_at": 1200, "expires_at": 9},
            "ttl on delivery_unknown": {"status": "delivery_unknown", "expires_at": 9},
        }
        for label, changes in outcomes.items():
            with self.subTest(transition=label):
                self.queued()
                record = delivery(self.store, self.key)
                self.store.claim_sending(
                    self.key, expected_state_version=record.state_version, attempt_id="a1", lease_expires_at=1030
                )
                claimed = delivery(self.store, self.key)
                arguments = {
                    "expected_state_version": claimed.state_version,
                    "attempt_id": "a1",
                    "status": "posted",
                    "network_attempt_count": 1,
                    "next_action_at": None,
                    "slack_response": None,
                    "expires_at": None,
                    **changes,
                }
                with self.assertRaises(ValueError):
                    self.store.record_outcome(self.key, **arguments)
                # Refused before the write, so the claim is intact and a
                # correct call can still resolve it.
                self.assertEqual(delivery(self.store, self.key).status, "sending")
                self.client.delete_item(
                    TableName=TABLE,
                    Key={"PK": {"S": f"CANDIDATE#{self.key}"}, "SK": {"S": "DELIVERY"}},
                )

    def test_claim_sending_refuses_a_negative_lease(self):
        self.queued()
        record = delivery(self.store, self.key)

        with self.assertRaises(ValueError):
            self.store.claim_sending(
                self.key, expected_state_version=record.state_version, attempt_id="a1", lease_expires_at=-99
            )

        self.assertEqual(delivery(self.store, self.key).status, "queued")

    def test_schedule_retry_refuses_a_negative_next_action(self):
        self.queued()
        record = delivery(self.store, self.key)

        with self.assertRaises(ValueError):
            self.store.schedule_retry(
                self.key, expected_state_version=record.state_version, next_action_at=-5, slack_response=None
            )

        self.assertEqual(delivery(self.store, self.key).status, "queued")

    def test_update_pace_refuses_a_negative_next_allowed_time(self):
        with self.assertRaises(ValueError):
            self.store.update_pace(
                "shared-aws-change-alerts",
                expected_version=None,
                next_allowed_at=-10,
                last_response_class="http_200",
            )

        self.assertIsNone(self.store.get_pace("shared-aws-change-alerts"))

    def test_the_valid_equivalents_of_all_of_those_still_succeed(self):
        """The paired case, so the refusals above are not vacuous."""

        self.queued()
        record = delivery(self.store, self.key)
        self.assertTrue(
            self.store.claim_sending(
                self.key, expected_state_version=record.state_version, attempt_id="a1", lease_expires_at=1030
            )
        )
        claimed = delivery(self.store, self.key)
        self.assertTrue(
            self.store.record_outcome(
                self.key,
                expected_state_version=claimed.state_version,
                attempt_id="a1",
                status="failed_retryable",
                network_attempt_count=1,
                next_action_at=1200,
                slack_response=None,
                expires_at=None,
            )
        )
        self.assertTrue(
            self.store.update_pace(
                "shared-aws-change-alerts",
                expected_version=None,
                next_allowed_at=1200,
                last_response_class="http_200",
            )
        )

    def test_decoding_an_item_that_violates_the_ttl_invariant_fails_closed(self):
        """A corrupt item raises rather than becoming a usable record."""

        self.client.put_item(
            TableName=TABLE,
            Item={
                "PK": {"S": f"CANDIDATE#{self.key}"},
                "SK": {"S": "DELIVERY"},
                "destination_key": {"S": "shared-aws-change-alerts"},
                "request": {"S": json.dumps(self.request, sort_keys=True)},
                "status": {"S": "queued"},
                "state_version": {"N": "1"},
                "created_at": {"S": "2026-07-13T17:30:00Z"},
                "next_action_at": {"N": "1000"},
                "network_attempt_count": {"N": "0"},
                "expires_at": {"N": "500"},
            },
        )

        with self.assertRaises(ValueError):
            self.store.get_delivery(self.key)


if __name__ == "__main__":
    unittest.main()
