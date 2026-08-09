"""The dispatcher path: due record to the FIFO queue.

Chapter 06's "Dispatch and queue acceptance" cases are the contract here, and
`examples/delivery-request.json` is the exact request the dispatcher must send,
so the group and dedupe IDs and the message body are bound to the committed
document rather than to the module's own output.

The claim semantics are the load-bearing part. ADR-007 and chapter 02: an
uncertain send or a failed post-send update leaves the claimed generation on
the record and the next dispatch reuses its dispatch ID, while a future retry
that cleared the claim increments the generation, so the retry is not
suppressed by SQS's five-minute deduplication window.
"""

import copy
import json
import sys
import unittest
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import boto3
import yaml
from botocore.exceptions import ClientError, EndpointConnectionError
from moto import mock_aws

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aws_public_change_feed.dispatch import (  # noqa: E402
    DispatchSendRejected,
    InvalidDeliveryRequest,
    SendResult,
    SendStatus,
    SQSQueueSender,
    dispatch_due_work,
    serialize_request,
)
from aws_public_change_feed.identity import (  # noqa: E402
    audience_fingerprint,
    candidate_id,
    delivery_request_id,
    queue_dispatch_id,
)
from aws_public_change_feed.outbox import DeliveryRecord, InMemoryOutboxStore  # noqa: E402

CLOCK = datetime(2026, 7, 13, 17, 10, tzinfo=UTC)
DUE = int(datetime(2026, 7, 13, 17, 0, tzinfo=UTC).timestamp())
QUEUE_URL = "https://sqs.us-east-1.amazonaws.com/667653114001/apcf-delivery-dev.fifo"


def load_json(name):
    with (ROOT / "examples" / name).open(encoding="utf-8") as handle:
        return json.load(handle)


def load_yaml(name):
    with (ROOT / "examples" / name).open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


MAX_REQUEST_BYTES = load_yaml("config.yaml")["message_policy"]["max_delivery_request_bytes"]


def record(store, key, request, destination_key, status="pending_queue", next_action_at=DUE, **fields):
    delivery = DeliveryRecord(
        candidate_id=key,
        destination_key=destination_key,
        request=request,
        next_action_at=next_action_at,
        status=status,
        **fields,
    )
    store.put_delivery_if_absent(delivery)
    return delivery


def stored(store, key):
    """Load a delivery record the test knows exists, without the None branch."""

    delivery = store.get_delivery(key)
    assert delivery is not None
    return delivery


class RecordingSender:
    """QueueSender that records every send and returns scripted outcomes."""

    def __init__(self, *outcomes):
        self.calls = []
        self._outcomes = list(outcomes)

    def send(self, request, *, queue_url, dispatch_id):
        self.calls.append(
            {
                "queue_url": queue_url,
                "dispatch_id": dispatch_id,
                "destination_key": request["destination_key"],
                "request": copy.deepcopy(dict(request)),
            }
        )
        if self._outcomes:
            return self._outcomes.pop(0)
        return SendResult(SendStatus.ACCEPTED, message_id=f"message-{len(self.calls)}")

    @property
    def last(self):
        return self.calls[-1]


class CountingMetrics:
    def __init__(self):
        self.counts: Counter[str] = Counter()

    def dispatch_attempted(self):
        self.counts["attempted"] += 1

    def dispatch_accepted(self):
        self.counts["accepted"] += 1

    def dispatch_unknown(self):
        self.counts["unknown"] += 1


class DispatchTestCase(unittest.TestCase):
    def setUp(self):
        self.request = load_json("delivery-request.json")
        self.key = self.request["candidate"]["candidate_id"]
        self.destination = self.request["destination_key"]
        self.store = InMemoryOutboxStore()
        self.sender = RecordingSender()
        self.metrics = CountingMetrics()

    def dispatch(self, **kwargs):
        return dispatch_due_work(
            self.store,
            self.sender,
            queue_url=QUEUE_URL,
            now=kwargs.pop("now", CLOCK),
            max_delivery_request_bytes=kwargs.pop("max_delivery_request_bytes", MAX_REQUEST_BYTES),
            metrics=self.metrics,
            **kwargs,
        )

    def seed(self, **fields):
        record(self.store, self.key, self.request, self.destination, **fields)
        return stored(self.store, self.key)

    @staticmethod
    def second_request():
        """A second schema-valid request for an independent destination.

        Built the way `test_outbox.py` builds its second candidate: recompute
        every identity field the route and environment changes touch, so the
        document the dispatcher validates is internally consistent.
        """

        original = load_json("delivery-request.json")
        candidate = copy.deepcopy(original["candidate"])
        candidate["route_id"] = "second-alerts"
        candidate["environment_ids"] = ["acme-prod"]
        candidate["audience_fingerprint"] = audience_fingerprint(candidate["environment_ids"])
        candidate["candidate_id"] = candidate_id(
            candidate["announcement"]["revision_id"],
            candidate["service"]["id"],
            candidate["risk"]["risk_type"],
            candidate["route_id"],
            candidate["audience_fingerprint"],
        )
        return {
            "contract_version": 3,
            "request_id": delivery_request_id(candidate["candidate_id"]),
            "candidate": candidate,
            "destination_key": "second-destination",
            "created_at": original["created_at"],
        }


class DispatchAcceptanceTests(DispatchTestCase):
    def test_due_work_sends_the_exact_request_with_group_and_dedupe_ids(self):
        self.seed()
        result = self.dispatch()

        self.assertEqual(result.considered, 1)
        self.assertEqual(result.new_claims, 1)
        self.assertEqual(result.accepted, 1)
        self.assertEqual(len(self.sender.calls), 1)
        sent = self.sender.last
        self.assertEqual(
            sent["request"],
            self.request,
            msg="chapter 04 embeds the candidate so the dispatcher cannot build a different payload",
        )
        self.assertEqual(sent["destination_key"], self.destination)
        self.assertEqual(sent["queue_url"], QUEUE_URL)
        self.assertEqual(sent["dispatch_id"], queue_dispatch_id(self.request["request_id"], 1))

        delivery = stored(self.store, self.key)
        self.assertEqual(delivery.status, "queued")
        self.assertEqual(delivery.queue_message_id, "message-1")
        self.assertEqual(delivery.dispatch_generation, 1)
        self.assertEqual(delivery.dispatch_id, queue_dispatch_id(self.request["request_id"], 1))
        self.assertEqual(delivery.state_version, 3, msg="the claim and the queued transition each bump the version")
        self.assertEqual(
            delivery.next_action_at,
            int(CLOCK.timestamp()),
            msg="queued work records the queue-entry time for the reconciler",
        )

    def test_the_message_body_is_the_exact_request_and_carries_delivery_evidence(self):
        self.seed()
        self.dispatch()
        body = serialize_request(self.sender.last["request"])
        self.assertIn(self.request["request_id"], body)
        self.assertIn(self.key, body)
        self.assertIn(self.destination, body)
        self.assertEqual(
            json.loads(body),
            self.request,
            msg="the worker reconstructs the exact contract from the SQS body",
        )

    def test_an_uncertain_send_leaves_the_claim_and_redispatch_reuses_it(self):
        self.seed()
        self.sender = RecordingSender(SendResult(SendStatus.UNKNOWN))
        first = self.dispatch()

        self.assertEqual(first.unknown, 1)
        self.assertEqual(self.metrics.counts["unknown"], 1)
        delivery = stored(self.store, self.key)
        self.assertEqual(delivery.status, "pending_queue", msg="unknown outcome keeps the record recoverable")
        self.assertEqual(delivery.dispatch_generation, 1)
        claimed = delivery.dispatch_id

        self.sender = RecordingSender()
        second = self.dispatch()
        self.assertEqual(second.reused_claims, 1)
        self.assertEqual(
            self.sender.last["dispatch_id"],
            claimed,
            msg="redispatch reuses the claimed dispatch ID so FIFO dedupe suppresses the duplicate",
        )
        self.assertEqual(stored(self.store, self.key).dispatch_generation, 1)
        self.assertEqual(stored(self.store, self.key).status, "queued")

    def test_a_failed_post_send_update_reuses_the_claim_on_redispatch(self):
        self.seed()
        self.sender = RecordingSender(SendResult(SendStatus.ACCEPTED, message_id="accepted-1"))
        store = FailMarkQueuedOnce()
        # Copy the seeded record into the failing store so both runs share state.
        store._deliveries = dict(self.store._deliveries)
        store._candidates = dict(self.store._candidates)

        first = dispatch_due_work(
            store,
            self.sender,
            queue_url=QUEUE_URL,
            now=CLOCK,
            max_delivery_request_bytes=MAX_REQUEST_BYTES,
            metrics=self.metrics,
        )
        self.assertEqual(first.failed_transitions, 1)
        self.assertEqual(first.accepted, 0)
        delivery = stored(store, self.key)
        self.assertEqual(delivery.status, "pending_queue")
        self.assertEqual(delivery.dispatch_generation, 1)
        claimed = delivery.dispatch_id

        second = dispatch_due_work(
            store,
            self.sender,
            queue_url=QUEUE_URL,
            now=CLOCK,
            max_delivery_request_bytes=MAX_REQUEST_BYTES,
            metrics=self.metrics,
        )
        self.assertEqual(second.reused_claims, 1)
        self.assertEqual(second.accepted, 1)
        self.assertEqual(
            self.sender.calls[-1]["dispatch_id"],
            claimed,
            msg="SQS acceptance followed by a failed state update is redone with the same dispatch ID",
        )
        self.assertEqual(stored(store, self.key).status, "queued")

    def test_a_future_retry_increments_the_generation_for_a_new_dispatch_id(self):
        # The worker schedules a future retry by clearing the active claim but
        # keeping the generation as the counter, per chapter 02.
        self.seed(status="failed_retryable", dispatch_generation=1, dispatch_id=None)
        first_dispatch_id = queue_dispatch_id(self.request["request_id"], 1)

        result = self.dispatch()

        self.assertEqual(result.new_claims, 1)
        new_id = self.sender.last["dispatch_id"]
        self.assertNotEqual(
            new_id,
            first_dispatch_id,
            msg="a retry must use a new dispatch ID so it is not suppressed inside the dedup window",
        )
        self.assertEqual(new_id, queue_dispatch_id(self.request["request_id"], 2))
        delivery = stored(self.store, self.key)
        self.assertEqual(delivery.dispatch_generation, 2)
        self.assertEqual(delivery.status, "queued")

    def test_an_unknown_outcome_emits_the_alarmable_metric_and_stays_recoverable(self):
        self.seed()
        self.sender = RecordingSender(SendResult(SendStatus.UNKNOWN))
        self.dispatch()

        self.assertEqual(self.metrics.counts["unknown"], 1)
        self.assertEqual(self.metrics.counts["attempted"], 1)
        delivery = stored(self.store, self.key)
        self.assertEqual(delivery.status, "pending_queue")
        self.assertIsNotNone(delivery.dispatch_id)

    def test_one_destinations_unknown_send_does_not_block_another_group(self):
        second = self.second_request()
        second_key = second["candidate"]["candidate_id"]
        self.seed()
        record(self.store, second_key, second, second["destination_key"])

        self.sender = RecordingSender(SendResult(SendStatus.UNKNOWN))
        self.sender._outcomes.append(SendResult(SendStatus.ACCEPTED, message_id="second-ok"))

        result = self.dispatch()

        self.assertEqual(result.unknown, 1)
        self.assertEqual(result.accepted, 1)
        self.assertEqual(len(self.sender.calls), 2)
        self.assertEqual(stored(self.store, self.key).status, "pending_queue")
        self.assertEqual(stored(self.store, second_key).status, "queued")
        self.assertEqual(self.sender.calls[1]["destination_key"], second["destination_key"])

    def test_due_work_is_oldest_first(self):
        older = self.second_request()
        older_key = older["candidate"]["candidate_id"]
        record(self.store, older_key, older, older["destination_key"], next_action_at=DUE - 300)
        self.seed()

        self.dispatch()

        self.assertEqual(
            [call["destination_key"] for call in self.sender.calls],
            [older["destination_key"], self.destination],
            msg="chapter 02 orders due work by next_action_at so the oldest dispatches first",
        )

    def test_limit_caps_the_combined_cross_status_batch(self):
        older = self.second_request()
        older_key = older["candidate"]["candidate_id"]
        record(
            self.store,
            older_key,
            older,
            older["destination_key"],
            status="failed_retryable",
            next_action_at=DUE - 300,
        )
        self.seed()

        result = self.dispatch(limit=1)

        self.assertEqual(result.considered, 1)
        self.assertEqual(len(self.sender.calls), 1)
        self.assertEqual(self.sender.last["destination_key"], older["destination_key"])
        self.assertEqual(stored(self.store, self.key).status, "pending_queue")

    def test_not_yet_due_work_is_not_dispatched(self):
        self.seed(next_action_at=int(CLOCK.timestamp()) + 60)
        result = self.dispatch()
        self.assertEqual(result.considered, 0)
        self.assertEqual(self.sender.calls, [])

    def test_duplicate_sends_of_the_reuse_path_share_one_dispatch_id(self):
        # Two dispatcher invocations both reusing a claim must present the same
        # dedupe ID, which is what makes FIFO deduplication suppress the second.
        self.seed()
        self.sender = RecordingSender(SendResult(SendStatus.UNKNOWN))
        self.dispatch()
        claimed = stored(self.store, self.key).dispatch_id

        self.sender = RecordingSender(SendResult(SendStatus.UNKNOWN))
        self.dispatch()
        self.assertEqual(self.sender.last["dispatch_id"], claimed)


class DispatchValidationTests(DispatchTestCase):
    def test_a_corrupted_request_id_is_rejected_before_any_claim(self):
        self.seed()
        tampered = copy.deepcopy(self.request)
        tampered["request_id"] = "0" * 64
        self.store._deliveries[self.key] = DeliveryRecord(
            candidate_id=self.key,
            destination_key=self.destination,
            request=tampered,
            next_action_at=DUE,
        )
        with self.assertRaisesRegex(InvalidDeliveryRequest, "does not derive"):
            self.dispatch()
        self.assertEqual(self.sender.calls, [])
        self.assertIsNone(stored(self.store, self.key).dispatch_id)

    def test_a_request_for_another_candidate_is_rejected(self):
        other = self.second_request()
        self.seed()
        # The record key is this candidate's, but the request embeds the other.
        self.store._deliveries[self.key] = DeliveryRecord(
            candidate_id=self.key,
            destination_key=self.destination,
            request=other,
            next_action_at=DUE,
        )
        with self.assertRaisesRegex(InvalidDeliveryRequest, "not the .* its delivery record names"):
            self.dispatch()
        self.assertEqual(self.sender.calls, [])

    def test_a_request_with_an_unknown_field_is_rejected(self):
        self.seed()
        tampered = copy.deepcopy(self.request)
        tampered["unreviewed_behavior"] = True
        self.store._deliveries[self.key] = DeliveryRecord(
            candidate_id=self.key,
            destination_key=self.destination,
            request=tampered,
            next_action_at=DUE,
        )
        with self.assertRaisesRegex(InvalidDeliveryRequest, "fails its contract"):
            self.dispatch()
        self.assertEqual(self.sender.calls, [])

    def test_a_request_naming_the_wrong_destination_is_rejected(self):
        self.seed()
        tampered = copy.deepcopy(self.request)
        tampered["destination_key"] = "other-destination"
        self.store._deliveries[self.key] = DeliveryRecord(
            candidate_id=self.key,
            destination_key=self.destination,
            request=tampered,
            next_action_at=DUE,
        )
        with self.assertRaisesRegex(InvalidDeliveryRequest, "destination"):
            self.dispatch()
        self.assertEqual(self.sender.calls, [])

    def test_an_oversize_request_is_rejected_before_any_claim(self):
        self.seed()
        maximum = len(serialize_request(self.request).encode("utf-8")) - 1

        with self.assertRaisesRegex(InvalidDeliveryRequest, rf"maximum is {maximum}"):
            self.dispatch(max_delivery_request_bytes=maximum)

        self.assertEqual(self.sender.calls, [])
        self.assertIsNone(stored(self.store, self.key).dispatch_id)


class SQSQueueSenderTests(unittest.TestCase):
    @mock_aws
    def test_moto_queue_receives_the_exact_body_group_and_dedupe_id(self):
        request = load_json("delivery-request.json")
        client = boto3.client("sqs", region_name="us-west-2")
        queue_url = client.create_queue(
            QueueName="dispatcher-test.fifo",
            Attributes={"FifoQueue": "true", "ContentBasedDeduplication": "false"},
        )["QueueUrl"]
        dispatch_id = queue_dispatch_id(request["request_id"], 1)

        result = SQSQueueSender(client).send(request, queue_url=queue_url, dispatch_id=dispatch_id)

        self.assertEqual(result.status, SendStatus.ACCEPTED)
        self.assertIsNotNone(result.message_id)
        received = client.receive_message(QueueUrl=queue_url, AttributeNames=["All"])["Messages"][0]
        self.assertEqual(json.loads(received["Body"]), request)
        self.assertEqual(received["Attributes"]["MessageGroupId"], request["destination_key"])
        self.assertEqual(received["Attributes"]["MessageDeduplicationId"], dispatch_id)

    def test_a_service_error_is_a_definitive_rejection(self):
        class ServiceErrorClient:
            def send_message(self, **kwargs):
                raise ClientError(
                    {"Error": {"Code": "AWS.SimpleQueueService.NonExistentQueue", "Message": "missing"}},
                    "SendMessage",
                )

        request = load_json("delivery-request.json")
        with self.assertRaisesRegex(DispatchSendRejected, "NonExistentQueue"):
            SQSQueueSender(ServiceErrorClient()).send(
                request,
                queue_url=QUEUE_URL,
                dispatch_id=queue_dispatch_id(request["request_id"], 1),
            )

    def test_a_transport_error_is_unknown(self):
        class TransportErrorClient:
            def send_message(self, **kwargs):
                raise EndpointConnectionError(endpoint_url=QUEUE_URL)

        request = load_json("delivery-request.json")
        result = SQSQueueSender(TransportErrorClient()).send(
            request,
            queue_url=QUEUE_URL,
            dispatch_id=queue_dispatch_id(request["request_id"], 1),
        )
        self.assertEqual(result, SendResult(SendStatus.UNKNOWN))


class DispatchRejectionTests(DispatchTestCase):
    def test_a_definitive_sqs_rejection_raises_and_keeps_the_claim(self):
        self.seed()
        self.sender = RejectingSender()

        with self.assertRaisesRegex(DispatchSendRejected, "rejected"):
            self.dispatch()

        delivery = stored(self.store, self.key)
        self.assertEqual(delivery.status, "pending_queue")
        self.assertEqual(delivery.dispatch_generation, 1)
        self.assertIsNotNone(delivery.dispatch_id)

    def test_a_returned_rejected_status_raises_and_keeps_the_claim(self):
        self.seed()
        self.sender = RecordingSender(SendResult(SendStatus.REJECTED))

        with self.assertRaisesRegex(DispatchSendRejected, "without queue acceptance"):
            self.dispatch()

        delivery = stored(self.store, self.key)
        self.assertEqual(delivery.status, "pending_queue")
        self.assertEqual(delivery.dispatch_generation, 1)
        self.assertIsNotNone(delivery.dispatch_id)


class DispatchClaimRaceTests(DispatchTestCase):
    def test_a_lost_claim_race_reuses_the_winners_dispatch_id(self):
        # Between this dispatcher's query and its claim attempt, a concurrent
        # dispatcher's conditional write won. The re-read must reuse the winner's
        # claim, so both present the same dedupe ID, instead of skipping or
        # sending a different generation.
        self.seed()
        self.store = LostClaimRaceStore(self.store)
        result = self.dispatch()

        self.assertEqual(result.new_claims, 0)
        self.assertEqual(result.reused_claims, 1)
        self.assertEqual(result.accepted, 1)
        self.assertEqual(self.sender.last["dispatch_id"], queue_dispatch_id(self.request["request_id"], 1))
        delivery = stored(self.store, self.key)
        self.assertEqual(delivery.status, "queued")
        self.assertEqual(delivery.dispatch_id, queue_dispatch_id(self.request["request_id"], 1))

    def test_an_aba_race_skips_the_stale_claim_then_uses_the_next_generation(self):
        self.seed(status="failed_retryable", dispatch_generation=1, state_version=4)
        race = AbaClaimRaceStore(self.store)
        self.store = race

        first = self.dispatch()

        self.assertEqual(first.new_claims, 0)
        self.assertEqual(self.sender.calls, [])
        advanced = stored(self.store, self.key)
        self.assertEqual(advanced.dispatch_generation, 2)
        self.assertIsNone(advanced.dispatch_id)

        self.store = race._wrapped
        second = self.dispatch()
        self.assertEqual(second.new_claims, 1)
        self.assertEqual(self.sender.last["dispatch_id"], queue_dispatch_id(self.request["request_id"], 3))

    def test_cross_status_due_work_is_oldest_first(self):
        # A failed_retryable record at DUE-500 is older than a pending_queue
        # record at DUE. The dispatcher merge-sorts the two status queries into
        # one oldest-first order before dispatching.
        older = self.second_request()
        older_key = older["candidate"]["candidate_id"]
        record(
            self.store, older_key, older, older["destination_key"], status="failed_retryable", next_action_at=DUE - 500
        )
        self.seed()

        self.dispatch()

        self.assertEqual(len(self.sender.calls), 2)
        self.assertEqual(
            self.sender.calls[0]["destination_key"],
            older["destination_key"],
            msg="the older failed_retryable record dispatches before the newer pending_queue record",
        )
        self.assertEqual(self.sender.calls[1]["destination_key"], self.destination)
        self.assertEqual(stored(self.store, older_key).status, "queued")
        self.assertEqual(stored(self.store, self.key).status, "queued")

    def test_a_lost_race_to_a_resolved_record_is_skipped(self):
        # Between this dispatcher's query and its claim attempt, a faster
        # dispatcher completed the full transition to queued. The re-read finds
        # a non-SCHEDULED record, so _resolve_claim returns None and the record
        # is skipped without being sent again.
        self.seed()
        self.store = LostClaimRaceToResolvedStore(self.store)
        result = self.dispatch()

        self.assertEqual(result.considered, 1)
        self.assertEqual(result.new_claims, 0)
        self.assertEqual(result.accepted, 0)
        self.assertEqual(self.sender.calls, [])

    def test_a_lost_race_to_a_vanished_record_is_skipped(self):
        # The re-read after a failed claim returns None. _resolve_claim returns
        # None without blowing up, and the record is skipped.
        self.seed()
        self.store = LostClaimRaceToNothingStore(self.store)
        result = self.dispatch()

        self.assertEqual(result.considered, 1)
        self.assertEqual(result.new_claims, 0)
        self.assertEqual(result.accepted, 0)
        self.assertEqual(self.sender.calls, [])


class RejectingSender(RecordingSender):
    """A sender whose queue answer is a definite refusal."""

    def send(self, request, *, queue_url, dispatch_id):
        raise DispatchSendRejected(f"queue does not exist: {queue_url}")


class AbaClaimRaceStore(InMemoryOutboxStore):
    """Advance a claimed record through one queue cycle before the stale write."""

    def __init__(self, wrapped):
        super().__init__()
        self._wrapped = wrapped

    def get_delivery(self, candidate):
        return self._wrapped.get_delivery(candidate)

    def query_due(self, status, *, due_before, limit):
        return self._wrapped.query_due(status, due_before=due_before, limit=limit)

    def claim_dispatch(self, candidate, *, expected_state_version, expected_generation, request_id, due_before):
        claim = self._wrapped.claim_dispatch(
            candidate,
            expected_state_version=expected_state_version,
            expected_generation=expected_generation,
            request_id=request_id,
            due_before=due_before,
        )
        assert claim is not None
        _, dispatch_id = claim
        self._wrapped.mark_queued(candidate, dispatch_id=dispatch_id, message_id="winner", at=due_before)
        queued = self._wrapped.get_delivery(candidate)
        assert queued is not None
        self._wrapped._deliveries[candidate] = replace(
            queued,
            status="failed_retryable",
            dispatch_id=None,
            queue_message_id=None,
            next_action_at=due_before,
            state_version=queued.state_version + 1,
        )
        return None

    def mark_queued(self, candidate, *, dispatch_id, message_id, at):
        return self._wrapped.mark_queued(candidate, dispatch_id=dispatch_id, message_id=message_id, at=at)


class LostClaimRaceStore(InMemoryOutboxStore):
    """A store whose `claim_dispatch` fails after writing the winner's claim.

    The winner's write is what the losing caller observes on re-read, so the
    dispatcher's `_resolve_claim` sees the claim it just lost and reuses it.
    Every dispatch operation delegates to the wrapped store, so the record the
    race writes is the record the dispatcher transitions.
    """

    def __init__(self, wrapped):
        super().__init__()
        self._wrapped = wrapped

    def get_candidate(self, candidate):
        return self._wrapped.get_candidate(candidate)

    def put_candidate_if_absent(self, candidate):
        return self._wrapped.put_candidate_if_absent(candidate)

    def get_delivery(self, candidate):
        return self._wrapped.get_delivery(candidate)

    def query_due(self, status, *, due_before, limit):
        return self._wrapped.query_due(status, due_before=due_before, limit=limit)

    def claim_dispatch(self, candidate, *, expected_state_version, expected_generation, request_id, due_before):
        self._wrapped.claim_dispatch(
            candidate,
            expected_state_version=expected_state_version,
            expected_generation=expected_generation,
            request_id=request_id,
            due_before=due_before,
        )
        return None

    def mark_queued(self, candidate, *, dispatch_id, message_id, at):
        return self._wrapped.mark_queued(candidate, dispatch_id=dispatch_id, message_id=message_id, at=at)


class LostClaimRaceToResolvedStore(InMemoryOutboxStore):
    """A store whose `claim_dispatch` writes the claim and transitions to queued.

    Simulates a lost race where the winner dispatched and completed the full
    `queued` transition before this dispatcher's re-read. The re-read finds a
    non-SCHEDULED record, so `_resolve_claim` returns None and the record is
    skipped.
    """

    def __init__(self, wrapped):
        super().__init__()
        self._wrapped = wrapped

    def get_delivery(self, candidate):
        return self._wrapped.get_delivery(candidate)

    def query_due(self, status, *, due_before, limit):
        return self._wrapped.query_due(status, due_before=due_before, limit=limit)

    def claim_dispatch(self, candidate, *, expected_state_version, expected_generation, request_id, due_before):
        claim = self._wrapped.claim_dispatch(
            candidate,
            expected_state_version=expected_state_version,
            expected_generation=expected_generation,
            request_id=request_id,
            due_before=due_before,
        )
        assert claim is not None
        _, dispatch_id = claim
        self._wrapped.mark_queued(candidate, dispatch_id=dispatch_id, message_id="fast-msg", at=due_before)
        return None

    def mark_queued(self, candidate, *, dispatch_id, message_id, at):
        return self._wrapped.mark_queued(candidate, dispatch_id=dispatch_id, message_id=message_id, at=at)


class LostClaimRaceToNothingStore(InMemoryOutboxStore):
    """A store whose re-read after a lost claim returns None.

    Simulates the extreme edge where the record vanishes between the failed
    claim and the re-read. `_resolve_claim` must return None without blowing up.
    The record is only vanished after `claim_dispatch` writes it, so the
    dispatch loop's SCHEDULED check still sees it before the claim attempt.
    """

    def __init__(self, wrapped):
        super().__init__()
        self._wrapped = wrapped
        self._vanished: set[str] = set()

    def get_delivery(self, candidate):
        if candidate in self._vanished:
            return None
        return self._wrapped.get_delivery(candidate)

    def query_due(self, status, *, due_before, limit):
        return self._wrapped.query_due(status, due_before=due_before, limit=limit)

    def claim_dispatch(self, candidate, *, expected_state_version, expected_generation, request_id, due_before):
        self._wrapped.claim_dispatch(
            candidate,
            expected_state_version=expected_state_version,
            expected_generation=expected_generation,
            request_id=request_id,
            due_before=due_before,
        )
        self._vanished.add(candidate)
        return None

    def mark_queued(self, candidate, *, dispatch_id, message_id, at):
        return self._wrapped.mark_queued(candidate, dispatch_id=dispatch_id, message_id=message_id, at=at)


class FailMarkQueuedOnce(InMemoryOutboxStore):
    """A store whose first `mark_queued` fails, simulating a lost update."""

    def __init__(self):
        super().__init__()
        self.fail_times = 1

    def mark_queued(self, candidate, *, dispatch_id, message_id, at):
        if self.fail_times > 0:
            self.fail_times -= 1
            return False
        return super().mark_queued(candidate, dispatch_id=dispatch_id, message_id=message_id, at=at)


if __name__ == "__main__":
    unittest.main()
