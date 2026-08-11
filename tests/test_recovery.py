"""The bounded recovery state machine and its no-guessing boundaries."""

import copy
import json
import sys
import unittest
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aws_public_change_feed.dispatch import SendResult, SendStatus  # noqa: E402
from aws_public_change_feed.identity import delivery_request_id, queue_dispatch_id  # noqa: E402
from aws_public_change_feed.outbox import DeliveryRecord, InMemoryOutboxStore  # noqa: E402
from aws_public_change_feed.recovery import reconcile_deliveries  # noqa: E402

NOW = datetime(2026, 8, 10, 18, 0, tzinfo=UTC)
NOW_TS = int(NOW.timestamp())
QUEUE_URL = "https://sqs.us-east-1.amazonaws.com/667653114001/apcf-delivery-dev.fifo"


def load_json(name):
    with (ROOT / "examples" / name).open(encoding="utf-8") as handle:
        return json.load(handle)


def load_yaml(name):
    with (ROOT / "examples" / name).open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


MAX_REQUEST_BYTES = load_yaml("config.yaml")["message_policy"]["max_delivery_request_bytes"]


class RecordingSender:
    def __init__(self, outcome=None):
        self.calls = []
        self.outcome = outcome

    def send(self, request, *, queue_url, dispatch_id):
        self.calls.append((copy.deepcopy(request), queue_url, dispatch_id))
        return self.outcome or SendResult(SendStatus.ACCEPTED, message_id=f"message-{len(self.calls)}")


class CountingMetrics:
    def __init__(self):
        self.counts: Counter[str] = Counter()
        self.observations: dict[str, tuple[int, int]] = {}
        self.saturated_states: list[str] = []

    def heartbeat(self):
        self.counts["heartbeat"] += 1

    def state_observed(self, state, *, count, oldest_age_seconds):
        self.observations[state] = (count, oldest_age_seconds)

    def state_observation_saturated(self, state):
        self.saturated_states.append(state)

    def repair_limit_reached(self):
        self.counts["repair_limit"] += 1

    def expired_lease_unknown(self):
        self.counts["expired"] += 1

    def stale_queued(self, count):
        self.counts["stale_queued"] += count

    def conditional_race(self):
        self.counts["race"] += 1

    def dispatch_attempted(self):
        self.counts["attempted"] += 1

    def dispatch_accepted(self):
        self.counts["accepted"] += 1

    def dispatch_unknown(self):
        self.counts["dispatch_unknown"] += 1


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.base_request = load_json("delivery-request.json")
        self.store = InMemoryOutboxStore()
        self.sender = RecordingSender()
        self.metrics = CountingMetrics()

    def request(self, key, destination="shared-aws-change-alerts"):
        request = copy.deepcopy(self.base_request)
        request["candidate"]["candidate_id"] = key
        request["request_id"] = delivery_request_id(key)
        request["destination_key"] = destination
        return request

    def seed(self, key, status, next_action_at, **fields):
        record = DeliveryRecord(
            candidate_id=key,
            destination_key="shared-aws-change-alerts",
            request=self.request(key),
            status=status,
            next_action_at=next_action_at,
            created_at="2026-08-10T17:00:00Z",
            **fields,
        )
        self.assertTrue(self.store.put_delivery_if_absent(record))
        return record

    def reconcile(self, **kwargs):
        return reconcile_deliveries(
            self.store,
            self.sender,
            queue_url=QUEUE_URL,
            now=NOW,
            max_delivery_request_bytes=MAX_REQUEST_BYTES,
            metrics=self.metrics,
            **kwargs,
        )

    def record(self, key):
        record = self.store.get_delivery(key)
        assert record is not None
        return record

    def test_due_pending_and_retryable_work_use_the_existing_dispatcher(self):
        pending = "a" * 64
        retry = "b" * 64
        self.seed(pending, "pending_queue", NOW_TS - 20)
        self.seed(retry, "failed_retryable", NOW_TS - 10)

        result = self.reconcile()

        self.assertEqual(result.dispatch.accepted, 2)
        self.assertEqual([self.record(key).status for key in (pending, retry)], ["queued", "queued"])
        self.assertEqual(len(self.sender.calls), 2)
        self.assertEqual(self.metrics.counts["accepted"], 2)
        for request, queue_url, _ in self.sender.calls:
            self.assertEqual(queue_url, QUEUE_URL)
            self.assertEqual(request["destination_key"], "shared-aws-change-alerts")

    def test_a_due_record_reuses_its_durable_dispatch_claim(self):
        candidate = "d" * 64
        dispatch_id = queue_dispatch_id(delivery_request_id(candidate), 3)
        self.seed(
            candidate,
            "pending_queue",
            NOW_TS - 1,
            state_version=8,
            dispatch_generation=3,
            dispatch_id=dispatch_id,
        )

        result = self.reconcile()

        self.assertEqual(result.dispatch.reused_claims, 1)
        self.assertEqual(result.dispatch.new_claims, 0)
        self.assertEqual(self.sender.calls[0][2], dispatch_id)
        self.assertEqual(self.record(candidate).dispatch_generation, 3)

    def test_future_scheduled_work_is_observed_but_not_dispatched(self):
        self.seed("future", "pending_queue", NOW_TS + 1)

        result = self.reconcile()

        self.assertEqual(result.dispatch.considered, 0)
        self.assertEqual(self.sender.calls, [])
        self.assertEqual(self.record("future").status, "pending_queue")

    def test_the_exact_durable_lease_expiry_becomes_unknown_and_preserves_evidence(self):
        response = {"response_class": "connect_failed", "bytes_sent": False}
        self.seed(
            "expired",
            "sending",
            NOW_TS,
            state_version=7,
            attempt_id="attempt-7",
            lease_expires_at=NOW_TS,
            network_attempt_count=3,
            slack_response=response,
        )

        result = self.reconcile()

        self.assertEqual(result.expired_leases, 1)
        record = self.record("expired")
        self.assertEqual(record.status, "delivery_unknown")
        self.assertEqual(record.last_attempt_id, "attempt-7")
        self.assertEqual(record.network_attempt_count, 3)
        self.assertEqual(record.slack_response, response)
        self.assertIsNone(record.next_action_at)
        self.assertIsNone(record.expires_at)
        self.assertEqual(record.state_version, 8)
        self.assertEqual(self.metrics.counts["expired"], 1)

    def test_a_lease_that_outlives_the_simulated_invocation_is_not_resolved_early(self):
        self.seed(
            "later",
            "sending",
            NOW_TS + 30,
            attempt_id="attempt-later",
            lease_expires_at=NOW_TS + 30,
            network_attempt_count=1,
        )

        early = self.reconcile()
        self.assertEqual(early.expired_leases, 0)
        self.assertEqual(self.record("later").status, "sending")

        at_expiry = reconcile_deliveries(
            self.store,
            self.sender,
            queue_url=QUEUE_URL,
            now=datetime.fromtimestamp(NOW_TS + 30, UTC),
            max_delivery_request_bytes=MAX_REQUEST_BYTES,
            metrics=self.metrics,
        )
        self.assertEqual(at_expiry.expired_leases, 1)
        self.assertEqual(self.record("later").status, "delivery_unknown")

    def test_old_queued_work_is_signalled_without_another_queue_send(self):
        self.seed("old-queued", "queued", NOW_TS - 600, queue_message_id="message-old")

        result = self.reconcile()

        self.assertEqual(result.stale_queued, 1)
        self.assertEqual(self.sender.calls, [])
        self.assertEqual(self.record("old-queued").status, "queued")
        self.assertEqual(self.record("old-queued").queue_message_id, "message-old")

    def test_acknowledged_states_receive_no_automatic_transition(self):
        for state in ("posted", "failed_terminal", "delivery_unknown"):
            with self.subTest(state=state):
                self.seed(state, state, None, expires_at=NOW_TS + 100 if state != "delivery_unknown" else None)

        result = self.reconcile()

        self.assertEqual(result.expired_leases, 0)
        self.assertEqual(result.dispatch.considered, 0)
        self.assertEqual(self.sender.calls, [])
        for state in ("posted", "failed_terminal", "delivery_unknown"):
            self.assertEqual(self.record(state).status, state)

    def test_a_real_concurrent_winner_is_reported_without_being_overwritten(self):
        outer = self

        class WinningStore(InMemoryOutboxStore):
            def mark_expired_sending_unknown(self, candidate, **kwargs):
                current = self.get_delivery(candidate)
                assert current is not None and current.attempt_id is not None
                self.record_outcome(
                    candidate,
                    expected_state_version=current.state_version,
                    attempt_id=current.attempt_id,
                    status="posted",
                    network_attempt_count=current.network_attempt_count,
                    next_action_at=None,
                    slack_response={"response_class": "http_200"},
                    expires_at=NOW_TS + 86400,
                )
                return False

        self.store = WinningStore()
        self.seed(
            "race",
            "sending",
            NOW_TS,
            attempt_id="attempt-race",
            lease_expires_at=NOW_TS,
            network_attempt_count=1,
        )

        result = self.reconcile()

        self.assertEqual(result.conditional_races, 1)
        self.assertEqual(outer.record("race").status, "posted")

    def test_observation_and_repair_caps_are_explicit(self):
        for index in range(101):
            self.seed(f"queued-{index:03d}", "queued", NOW_TS - 700 - index)

        result = self.reconcile()

        self.assertTrue(result.observation_saturated)
        self.assertTrue(result.incomplete)
        self.assertEqual(result.stale_queued, 100)
        self.assertEqual(self.metrics.saturated_states, ["queued"])

    def test_oldest_repairable_work_wins_the_shared_limit(self):
        self.seed("older-lease", "sending", NOW_TS - 20, attempt_id="a", lease_expires_at=NOW_TS - 20)
        self.seed("newer-pending", "pending_queue", NOW_TS - 10)

        result = self.reconcile(repair_limit=1, observation_limit=2)

        self.assertEqual(result.expired_leases, 1)
        self.assertEqual(result.dispatch.considered, 0)
        self.assertTrue(result.repair_limit_reached)
        self.assertEqual(self.record("newer-pending").status, "pending_queue")

    def test_limits_and_clock_are_validated_before_store_reads(self):
        for kwargs, message in (
            ({"repair_limit": 0}, "repair_limit"),
            ({"repair_limit": 2, "observation_limit": 2}, "must exceed"),
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaisesRegex(ValueError, message):
                    self.reconcile(**kwargs)

        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            reconcile_deliveries(
                self.store,
                self.sender,
                queue_url=QUEUE_URL,
                now=datetime(2026, 8, 10, 18, 0),
                max_delivery_request_bytes=MAX_REQUEST_BYTES,
            )
