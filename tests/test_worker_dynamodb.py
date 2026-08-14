"""`process_delivery` end to end against the DynamoDB delivery store.

Every other worker test drives `InMemoryOutboxStore`, which is written to
agree with the worker. `DynamoDBDeliveryStore` implements the same contract as
condition expressions by hand, and passing in-memory tests say nothing about
whether those expressions are even valid: two of them referenced expression
attribute values they never defined, so `record_outcome` and `schedule_retry`
would have raised `ValidationException` at the worker's first invocation in
AWS. The in-memory suite was green throughout.

So this file exercises the worker's decision loop against moto's DynamoDB,
covering one representative outcome per branch that writes state. The store's
own conditional behaviour stays in `test_delivery_store.py`; what is new here
is that `process_delivery` drives it.

moto does not evaluate conditional writes atomically under real concurrency,
so interleaving races remain covered by the in-memory suite's injecting fakes.
Each assertion here is a single pass whose outcome the store's conditions
determine.
"""

import copy
import sys
import unittest
from datetime import timedelta
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from test_worker import (  # noqa: E402
    NOW,
    NOW_TS,
    AcceptedQueueSender,
    FakeSlackSender,
    RecordingCredentials,
    WorkerFixture,
)

from aws_public_change_feed.credentials import WEBHOOK, SlackCredential  # noqa: E402
from aws_public_change_feed.dispatch import dispatch_due_work  # noqa: E402
from aws_public_change_feed.manual_replay import apply_unknown_replay, plan_unknown_replay  # noqa: E402
from aws_public_change_feed.outbox import (  # noqa: E402
    DeliveryRecord,
    DynamoDBDeliveryStore,
    FoundPostEntry,
    build_delivery_request,
)
from aws_public_change_feed.worker import (  # noqa: E402
    DELIVERY_UNKNOWN,
    FAILED_RETRYABLE,
    FAILED_TERMINAL,
    POSTED,
    QueueDelivery,
    SlackResponse,
    TransportError,
)

TABLE = "aws-public-change-feed-delivery"
DDB_REGION = "us-east-1"


class WorkerAgainstDynamoDB(WorkerFixture):
    """The worker fixture with its outbox swapped for the real store.

    `WorkerFixture` already runs inside `mock_aws` for the release bucket, so
    the DynamoDB table is created in that same mock and only the store is
    replaced. Everything else — the published release, the committed
    candidate, the credential reader — is shared, so a difference in outcome
    here is a difference in the store and nothing else.
    """

    def setUp(self):
        super().setUp()
        self.ddb = boto3.client("dynamodb", region_name=DDB_REGION)
        self.ddb.create_table(
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
        self.store = DynamoDBDeliveryStore(self.ddb, TABLE)  # type: ignore[assignment]

    def queued_record(self, **overrides):
        """Put a `queued` record through the real store, not a dict poke."""

        defaults = {
            "candidate_id": self.key,
            "destination_key": self.destination_key,
            "request": self.request,
            "next_action_at": NOW_TS,
            "status": "queued",
            "state_version": 1,
            "created_at": self.request["created_at"],
        }
        defaults.update(overrides)
        record = DeliveryRecord(**defaults)
        self.store.put_delivery_if_absent(record)
        return record


class PostedOutcomeTests(WorkerAgainstDynamoDB):
    def test_a_posted_delivery_resolves_the_record_and_advances_pacing(self):
        self.queued_record()

        result = self.process()

        self.assertEqual(result.state, POSTED)
        self.assertTrue(result.performed_network_call)
        record = self.record()
        self.assertEqual(record.status, POSTED)
        self.assertEqual(record.network_attempt_count, 1)
        self.assertIsNotNone(record.expires_at)
        self.assertIsNone(record.attempt_id)
        self.assertIsNone(record.lease_expires_at)
        self.assertIsNotNone(record.last_attempt_id)
        assert record.slack_response is not None
        self.assertEqual(record.slack_response["response_class"], "http_200")

        pace = self.store.get_pace(self.destination_key)
        assert pace is not None
        min_interval = self.inventory["slack"]["rate_control"]["per_destination_min_interval_seconds"]
        self.assertEqual(pace.next_allowed_at, NOW_TS + min_interval)

    def test_a_duplicate_queue_delivery_makes_no_second_call(self):
        self.queued_record()
        self.process()
        self.assertEqual(len(self.sender.calls), 1)

        again = self.process()

        self.assertTrue(again.handled)
        self.assertEqual(again.state, POSTED)
        self.assertEqual(len(self.sender.calls), 1)

    def test_a_found_post_reconciled_record_makes_no_second_call(self):
        prior_attempt = "prior-attempt"
        self.queued_record(
            status=DELIVERY_UNKNOWN,
            next_action_at=None,
            state_version=7,
            last_attempt_id=prior_attempt,
        )
        self.assertTrue(
            self.store.reconcile_found_post(
                self.key,
                expected_state_version=7,
                expected_prior_attempt_id=prior_attempt,
                entry=FoundPostEntry(
                    decided_at="2026-08-11T14:30:00Z",
                    operator="operator@example.com",
                    reason="Closed after finding the Slack post",
                    evidence="Slack search found the posted message",
                    prior_attempt_id=prior_attempt,
                    slack_reference="operator note 42",
                ),
                expires_at=NOW_TS + 365 * 86400,
            )
        )

        result = self.process()

        self.assertTrue(result.handled)
        self.assertEqual(result.state, POSTED)
        self.assertEqual(self.sender.calls, [])


class RetryOutcomeTests(WorkerAgainstDynamoDB):
    def test_a_429_writes_a_future_action_and_returns_to_the_dispatch_index(self):
        """The `record_outcome` retryable branch, which was the broken one."""

        self.queued_record()
        self.sender = FakeSlackSender(SlackResponse(status_code=429, latency_ms=80, retry_after_seconds=45))

        result = self.process()

        self.assertEqual(result.state, FAILED_RETRYABLE)
        record = self.record()
        self.assertEqual(record.status, FAILED_RETRYABLE)
        self.assertEqual(record.next_action_at, NOW_TS + 45)
        self.assertIsNotNone(record.last_attempt_id)
        self.assertIsNone(record.expires_at)
        self.assertIsNone(record.dispatch_id)
        self.assertEqual(
            self.store.query_due("failed_retryable", due_before=NOW_TS + 100, limit=10),
            ((NOW_TS + 45, self.key),),
        )

    def test_destination_pacing_defers_without_a_call(self):
        """The `schedule_retry` branch, the other invalid expression."""

        self.queued_record()
        self.store.update_pace(
            self.destination_key,
            expected_version=None,
            next_allowed_at=NOW_TS + 60,
            last_response_class="http_200",
        )

        result = self.process()

        self.assertEqual(result.state, FAILED_RETRYABLE)
        self.assertFalse(result.performed_network_call)
        self.assertEqual(self.sender.calls, [])
        record = self.record()
        self.assertEqual(record.status, FAILED_RETRYABLE)
        self.assertEqual(record.next_action_at, NOW_TS + 60)

    def test_an_exhausted_budget_escalates_to_terminal_with_a_ttl(self):
        max_attempts = self.inventory["slack"]["rate_control"]["max_network_attempts"]
        self.queued_record(network_attempt_count=max_attempts - 1)
        self.sender = FakeSlackSender(SlackResponse(status_code=503, latency_ms=90))

        result = self.process()

        self.assertEqual(result.state, FAILED_TERMINAL)
        record = self.record()
        self.assertEqual(record.network_attempt_count, max_attempts)
        self.assertIsNotNone(record.last_attempt_id)
        self.assertIsNotNone(record.expires_at)


class UnknownOutcomeTests(WorkerAgainstDynamoDB):
    def test_a_timeout_records_delivery_unknown_with_no_ttl(self):
        """The TTL omission matters here: ADR-004 keeps this for review."""

        self.queued_record()
        self.sender = FakeSlackSender(
            SlackResponse(error_class=TransportError.TIMEOUT, bytes_sent=True, latency_ms=10000)
        )

        result = self.process()

        self.assertEqual(result.state, DELIVERY_UNKNOWN)
        record = self.record()
        self.assertEqual(record.status, DELIVERY_UNKNOWN)
        self.assertIsNone(record.expires_at)
        self.assertIsNone(record.attempt_id)
        self.assertIsNotNone(record.last_attempt_id)

    def test_an_expired_sending_lease_becomes_delivery_unknown(self):
        self.queued_record(
            status="sending",
            next_action_at=NOW_TS - 10,
            attempt_id="a1",
            lease_expires_at=NOW_TS - 10,
        )

        result = self.process()

        self.assertEqual(result.state, DELIVERY_UNKNOWN)
        self.assertEqual(self.record().status, DELIVERY_UNKNOWN)
        self.assertEqual(self.record().last_attempt_id, "a1")
        self.assertEqual(self.sender.calls, [])

    def test_a_replayed_unknown_consumes_its_reserved_attempt(self):
        reserved_attempt = "1" * 32
        unknown = DeliveryRecord(
            candidate_id=self.key,
            destination_key=self.destination_key,
            request=self.request,
            next_action_at=None,
            status=DELIVERY_UNKNOWN,
            state_version=7,
            created_at=self.request["created_at"],
            dispatch_generation=1,
            last_attempt_id="prior-attempt",
            network_attempt_count=1,
        )
        self.store.put_delivery_if_absent(unknown)
        replay = plan_unknown_replay(
            unknown,
            expected_state_version=7,
            operator="operator@example.com",
            reason="Approved after Slack inspection",
            evidence="No matching post in the destination search window",
            clock=lambda: NOW,
            attempt_id_factory=lambda: reserved_attempt,
        )
        apply_unknown_replay(self.store, replay)

        dispatched = dispatch_due_work(
            self.store,
            AcceptedQueueSender(),
            queue_url="https://sqs.example/manual-replay.fifo",
            now=NOW,
            max_delivery_request_bytes=self.config["message_policy"]["max_delivery_request_bytes"],
        )
        self.assertEqual(dispatched.accepted, 1)
        queued = self.record()
        self.assertEqual(queued.next_attempt_id, reserved_attempt)

        result = self.process(
            queue_delivery=QueueDelivery(
                request=self.request,
                message_id=queued.queue_message_id,
                message_group_id=self.destination_key,
            )
        )

        self.assertEqual(result.state, POSTED)
        record = self.record()
        self.assertEqual(record.last_attempt_id, reserved_attempt)
        self.assertIsNone(record.next_attempt_id)
        self.assertEqual(record.manual_replay_history[-1].new_attempt_id, reserved_attempt)

    def test_a_replayed_unknown_survives_pacing_and_does_not_block_other_due_work(self):
        reserved_attempt = "1" * 32
        unknown = DeliveryRecord(
            candidate_id=self.key,
            destination_key=self.destination_key,
            request=self.request,
            next_action_at=None,
            status=DELIVERY_UNKNOWN,
            state_version=7,
            created_at=self.request["created_at"],
            dispatch_generation=1,
            last_attempt_id="prior-attempt",
            network_attempt_count=1,
        )
        self.store.put_delivery_if_absent(unknown)
        replay = plan_unknown_replay(
            unknown,
            expected_state_version=7,
            operator="operator@example.com",
            reason="Approved after Slack inspection",
            evidence="No matching post in the destination search window",
            clock=lambda: NOW,
            attempt_id_factory=lambda: reserved_attempt,
        )
        apply_unknown_replay(self.store, replay)
        dispatch_due_work(
            self.store,
            AcceptedQueueSender(),
            queue_url="https://sqs.example/manual-replay.fifo",
            now=NOW,
            max_delivery_request_bytes=self.config["message_policy"]["max_delivery_request_bytes"],
        )

        retry_time = NOW + timedelta(seconds=60)
        self.assertTrue(
            self.store.update_pace(
                self.destination_key,
                expected_version=None,
                next_allowed_at=int(retry_time.timestamp()),
                last_response_class="http_200",
            )
        )
        queued = self.record()
        paced = self.process(
            queue_delivery=QueueDelivery(
                request=self.request,
                message_id=queued.queue_message_id,
                message_group_id=self.destination_key,
            )
        )

        self.assertEqual(paced.state, FAILED_RETRYABLE)
        self.assertFalse(paced.performed_network_call)
        self.assertEqual(self.sender.calls, [])
        deferred = self.record()
        self.assertEqual(deferred.status, FAILED_RETRYABLE)
        self.assertEqual(deferred.next_attempt_id, reserved_attempt)

        unrelated_key = "f" * 64
        self.assertNotEqual(unrelated_key, self.key)
        unrelated_candidate = copy.deepcopy(self.candidate)
        unrelated_candidate["candidate_id"] = unrelated_key
        unrelated_destination = "unrelated-destination"
        unrelated_request = build_delivery_request(unrelated_candidate, unrelated_destination, NOW)
        dispatch_time = retry_time + timedelta(seconds=1)
        self.assertTrue(
            self.store.put_delivery_if_absent(
                DeliveryRecord(
                    candidate_id=unrelated_key,
                    destination_key=unrelated_destination,
                    request=unrelated_request,
                    next_action_at=int(dispatch_time.timestamp()),
                    status="pending_queue",
                    created_at=unrelated_request["created_at"],
                )
            )
        )

        queue_sender = AcceptedQueueSender()
        dispatched = dispatch_due_work(
            self.store,
            queue_sender,
            queue_url="https://sqs.example/manual-replay.fifo",
            now=dispatch_time,
            max_delivery_request_bytes=self.config["message_policy"]["max_delivery_request_bytes"],
        )
        self.assertEqual(dispatched.accepted, 2)
        self.assertEqual(len(queue_sender.calls), 2)
        redispatched = self.record()
        self.assertEqual(redispatched.status, "queued")
        self.assertEqual(redispatched.next_attempt_id, reserved_attempt)
        unrelated = self.store.get_delivery(unrelated_key)
        assert unrelated is not None
        self.assertEqual(unrelated.status, "queued")

        outer = self

        class InspectingSender(FakeSlackSender):
            claimed: DeliveryRecord | None = None

            def post(self, payload, *, credential, destination, timeout_seconds):
                self.claimed = outer.record()
                return super().post(
                    payload,
                    credential=credential,
                    destination=destination,
                    timeout_seconds=timeout_seconds,
                )

        sender = InspectingSender(self.posted_response())
        self.sender = sender
        posted = self.process(
            clock=lambda: dispatch_time,
            queue_delivery=QueueDelivery(
                request=self.request,
                message_id=redispatched.queue_message_id,
                message_group_id=self.destination_key,
            ),
        )

        self.assertEqual(posted.state, POSTED)
        assert sender.claimed is not None
        self.assertEqual(sender.claimed.status, "sending")
        self.assertEqual(sender.claimed.attempt_id, reserved_attempt)
        self.assertIsNone(sender.claimed.next_attempt_id)
        self.assertEqual(self.record().last_attempt_id, reserved_attempt)


class RefusedCredentialTests(WorkerAgainstDynamoDB):
    def test_a_hostile_webhook_is_terminal_and_spends_no_attempt(self):
        """The pre-call terminal path, written through the real conditions."""

        self.credentials = RecordingCredentials(
            {self.route["credential_secret_id"]: SlackCredential(WEBHOOK, "http://evil.example/services/T/B/S")}
        )
        self.queued_record()

        result = self.process(credentials=self.credentials)

        self.assertEqual(result.state, FAILED_TERMINAL)
        self.assertFalse(result.performed_network_call)
        self.assertEqual(self.sender.calls, [])
        record = self.record()
        self.assertEqual(record.network_attempt_count, 0)
        self.assertIsNotNone(record.last_attempt_id)
        assert record.slack_response is not None
        self.assertEqual(record.slack_response["response_class"], "webhook_url_rejected")


class EndToEndOrderingTests(WorkerAgainstDynamoDB):
    def test_a_retry_then_a_success_leaves_one_resolved_record(self):
        """Two passes over one record, both through the real store.

        The first attempt is rate limited and returns to the dispatch index;
        the second succeeds. Nothing but the store carries state between them,
        so a condition expression that silently failed would show up as a
        wrong `network_attempt_count` or a record stuck in the index.
        """

        self.queued_record()
        self.sender = FakeSlackSender(SlackResponse(status_code=429, latency_ms=50, retry_after_seconds=30))
        self.assertEqual(self.process().state, FAILED_RETRYABLE)

        # The dispatcher would re-queue it; emulate only that transition.
        deferred = self.record()
        self.ddb.update_item(
            TableName=TABLE,
            Key={"PK": {"S": f"CANDIDATE#{self.key}"}, "SK": {"S": "DELIVERY"}},
            UpdateExpression="SET #status = :queued, state_version = :version",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":queued": {"S": "queued"},
                ":version": {"N": str(deferred.state_version + 1)},
            },
        )

        self.sender = FakeSlackSender(SlackResponse(status_code=200, latency_ms=40))
        second = self.process(clock=lambda: NOW.replace(minute=45))

        self.assertEqual(second.state, POSTED)
        record = self.record()
        self.assertEqual(record.status, POSTED)
        self.assertEqual(record.network_attempt_count, 2)
        self.assertEqual(self.store.query_due("failed_retryable", due_before=NOW_TS + 10000, limit=10), ())


if __name__ == "__main__":
    unittest.main()
