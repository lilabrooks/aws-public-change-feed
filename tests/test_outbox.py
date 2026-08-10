"""The durable outbox creation boundary.

`examples/delivery-request.json` commits a request that its candidate and
destination fully determine, so `build_delivery_request` is bound to that
document rather than to its own output.

The emission tests cover what chapter 04 requires of a repeated watcher
invocation: a candidate that already exists is loaded and validated rather than
replaced, a missing delivery record is repaired from the stored candidate, and a
stored item disagreeing with its key is a correctness failure.
"""

import copy
import json
import sys
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aws_public_change_feed.identity import audience_fingerprint, candidate_id, queue_dispatch_id  # noqa: E402
from aws_public_change_feed.outbox import (  # noqa: E402
    DELIVERY_STATES,
    CandidateIdentityError,
    DeliveryRecord,
    InMemoryOutboxStore,
    OversizeDeliveryError,
    build_delivery_request,
    emit,
    serialized_size,
    verify_durable,
)


def load_json(name):
    with (ROOT / "examples" / name).open(encoding="utf-8") as handle:
        return json.load(handle)


def load_yaml(name):
    with (ROOT / "examples" / name).open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def parse_timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def delivery(store, key):
    """Load a delivery record the test knows exists."""

    record = store.get_delivery(key)
    assert record is not None
    return record


def stored_candidate(store, key):
    """Load a candidate the test knows exists, without copying it."""

    loaded = store.get_candidate(key)
    assert loaded is not None
    return cast(dict, loaded)


class OutboxTestCase(unittest.TestCase):
    def setUp(self):
        self.inventory = load_json("inventory.json")
        self.config = load_yaml("config.yaml")
        self.candidate = load_json("alert-candidate.json")
        self.request = load_json("delivery-request.json")
        self.key = self.candidate["candidate_id"]
        self.created_at = parse_timestamp(self.request["created_at"])
        self.store = InMemoryOutboxStore()

    def emit_once(self, candidates=None):
        return emit(
            self.store,
            candidates if candidates is not None else [self.candidate],
            inventory=self.inventory,
            message_policy=self.config["message_policy"],
            created_at=self.created_at,
        )


class DeliveryRequestTests(OutboxTestCase):
    def test_rebuilds_the_committed_request(self):
        self.assertEqual(
            build_delivery_request(
                self.candidate,
                self.request["destination_key"],
                self.created_at,
            ),
            self.request,
        )

    def test_the_embedded_candidate_is_a_copy(self):
        built = build_delivery_request(self.candidate, self.request["destination_key"], self.created_at)
        built["candidate"]["route_id"] = "mutated"
        self.assertNotEqual(self.candidate["route_id"], "mutated")


class EmissionTests(OutboxTestCase):
    def candidate_over_candidate_limit(self):
        oversized = copy.deepcopy(self.candidate)
        oversized["explainability"]["matched_aliases"] = [f"alias-{index:03d}-" + "a" * 490 for index in range(100)]
        oversized["explainability"]["matched_terms"] = [f"term-{index:03d}-" + "t" * 491 for index in range(100)]
        oversized["announcement"]["provenance"] = [
            {
                "feed_name": f"feed-{index:02d}",
                "feed_url": "https://example.com/" + "f" * 1900 + str(index),
                "source_item_id": "i" * 2045 + f"{index:03d}",
            }
            for index in range(25)
        ]
        return oversized

    def test_writes_the_candidate_and_a_pending_queue_record(self):
        result = self.emit_once()
        self.assertEqual(result.created_candidates, (self.key,))
        self.assertEqual(result.created_deliveries, (self.key,))
        self.assertEqual(result.reused_candidates, ())

        self.assertEqual(self.store.get_candidate(self.key), self.candidate)
        record = delivery(self.store, self.key)
        self.assertEqual(record.status, "pending_queue")
        self.assertEqual(record.destination_key, self.request["destination_key"])
        self.assertEqual(record.request, self.request)
        self.assertEqual(
            record.next_action_at,
            int(self.created_at.timestamp()),
            msg="new pending work must be immediately queryable through the numeric due-work index",
        )
        self.assertIsInstance(record.next_action_at, int)

    def test_repeated_emission_writes_nothing_new(self):
        self.emit_once()
        again = self.emit_once()
        self.assertEqual(again.reused_candidates, (self.key,))
        self.assertEqual(again.created_candidates, ())
        self.assertEqual(again.created_deliveries, ())
        self.assertEqual(again.repaired_deliveries, ())

    def test_a_stored_candidate_is_never_replaced(self):
        self.emit_once()
        newer = copy.deepcopy(self.candidate)
        newer["release"]["application_version"] = "sha256:" + "0" * 64
        newer["created_at"] = "2027-01-01T00:00:00Z"

        self.emit_once([newer])

        stored = stored_candidate(self.store, self.key)
        self.assertEqual(stored, self.candidate)
        self.assertEqual(
            stored["release"]["application_version"],
            self.candidate["release"]["application_version"],
            msg="chapter 04 keeps the original evidence, release, and creation time",
        )

    def test_a_missing_delivery_record_is_repaired_from_the_stored_candidate(self):
        self.emit_once()
        # Simulate a crash between the two conditional writes.
        del self.store._deliveries[self.key]

        newer = copy.deepcopy(self.candidate)
        newer["release"]["application_version"] = "sha256:" + "0" * 64
        result = self.emit_once([newer])

        self.assertEqual(result.repaired_deliveries, (self.key,))
        self.assertEqual(result.created_deliveries, ())
        repaired = delivery(self.store, self.key)
        self.assertEqual(
            repaired.request,
            self.request,
            msg="the repaired request must come from the stored candidate, not the newer release",
        )

    def test_emits_one_record_per_candidate(self):
        second = copy.deepcopy(self.candidate)
        second["route_id"] = "shared-alerts"
        second["environment_ids"] = ["acme-prod"]
        # Recompute identity so the second document is internally consistent.
        second["audience_fingerprint"] = audience_fingerprint(second["environment_ids"])
        second["candidate_id"] = candidate_id(
            second["announcement"]["revision_id"],
            second["service"]["id"],
            second["risk"]["risk_type"],
            second["route_id"],
            second["audience_fingerprint"],
        )

        result = self.emit_once([self.candidate, second])
        self.assertEqual(len(result.created_candidates), 2)
        self.assertEqual(len(result.created_deliveries), 2)
        self.assertNotEqual(second["candidate_id"], self.key)

    def test_unknown_route_is_rejected(self):
        stray = copy.deepcopy(self.candidate)
        stray["route_id"] = "absent-route"
        stray["candidate_id"] = candidate_id(
            stray["announcement"]["revision_id"],
            stray["service"]["id"],
            stray["risk"]["risk_type"],
            stray["route_id"],
            stray["audience_fingerprint"],
        )
        with self.assertRaises(ValueError):
            self.emit_once([stray])

    def test_an_oversize_candidate_creates_no_durable_or_dispatchable_record(self):
        oversized = self.candidate_over_candidate_limit()
        maximum = self.config["message_policy"]["max_candidate_bytes"]
        self.assertGreater(serialized_size(oversized), maximum)
        oversized_request = build_delivery_request(
            oversized,
            self.request["destination_key"],
            self.created_at,
        )
        self.assertLessEqual(
            serialized_size(oversized_request),
            self.config["message_policy"]["max_delivery_request_bytes"],
            msg="the candidate ceiling, rather than the larger request ceiling, must reject this fixture",
        )

        with self.assertRaisesRegex(OversizeDeliveryError, rf"maximum is {maximum}"):
            self.emit_once([oversized])

        self.assertIsNone(self.store.get_candidate(self.key))
        self.assertIsNone(self.store.get_delivery(self.key))

    def test_an_oversize_request_is_rejected_before_the_candidate_write(self):
        policy = copy.deepcopy(self.config["message_policy"])
        policy["max_delivery_request_bytes"] = serialized_size(self.request) - 1

        with self.assertRaisesRegex(OversizeDeliveryError, "delivery request"):
            emit(
                self.store,
                [self.candidate],
                inventory=self.inventory,
                message_policy=policy,
                created_at=self.created_at,
            )

        self.assertIsNone(self.store.get_candidate(self.key))
        self.assertIsNone(self.store.get_delivery(self.key))

    def test_an_oversize_incoming_duplicate_reuses_the_valid_durable_candidate(self):
        first = self.emit_once()
        incoming = self.candidate_over_candidate_limit()
        self.assertGreater(serialized_size(incoming), self.config["message_policy"]["max_candidate_bytes"])

        replay = self.emit_once([incoming])

        self.assertEqual(replay.reused_candidates, first.candidate_ids)
        self.assertEqual(stored_candidate(self.store, self.key), self.candidate)


class IdentityFailureTests(OutboxTestCase):
    def test_edited_environment_ids_are_a_correctness_failure(self):
        tampered = copy.deepcopy(self.candidate)
        tampered["environment_ids"] = ["acme-prod"]
        with self.assertRaises(CandidateIdentityError):
            self.emit_once([tampered])

    def test_edited_route_is_a_correctness_failure(self):
        tampered = copy.deepcopy(self.candidate)
        tampered["route_id"] = "shared-alerts-2"
        with self.assertRaises(CandidateIdentityError):
            self.emit_once([tampered])

    def test_a_tampered_stored_candidate_is_caught_on_reemission(self):
        self.emit_once()
        stored = json.loads(self.store._candidates[self.key])
        stored["environment_ids"] = ["acme-prod"]
        self.store._candidates[self.key] = json.dumps(stored, sort_keys=True)

        with self.assertRaises(CandidateIdentityError):
            self.emit_once()


class DurabilityGateTests(OutboxTestCase):
    def test_false_until_both_records_exist(self):
        self.assertFalse(verify_durable(self.store, [self.key]))
        self.emit_once()
        self.assertTrue(verify_durable(self.store, [self.key]))

    def test_false_when_the_delivery_record_is_lost(self):
        self.emit_once()
        del self.store._deliveries[self.key]
        self.assertFalse(
            verify_durable(self.store, [self.key]),
            msg="chapter 04 blocks checkpoint advancement on a missing outbox record",
        )

    def test_reads_the_store_rather_than_trusting_a_result(self):
        result = self.emit_once()
        del self.store._candidates[self.key]
        self.assertFalse(verify_durable(self.store, result.candidate_ids))


class StoreTests(OutboxTestCase):
    def test_stored_candidates_are_not_live_references(self):
        self.emit_once()
        loaded = stored_candidate(self.store, self.key)
        loaded["route_id"] = "mutated"
        self.assertEqual(stored_candidate(self.store, self.key)["route_id"], "shared-alerts")

    def test_unknown_delivery_state_is_rejected(self):
        with self.assertRaises(ValueError):
            DeliveryRecord(
                candidate_id=self.key,
                destination_key="shared-aws-change-alerts",
                request=self.request,
                next_action_at=None,
                status="invented",
            )

    def test_state_version_is_a_positive_integer(self):
        for bad in (0, -1, True, "1"):
            with self.subTest(state_version=bad), self.assertRaisesRegex(ValueError, "state_version"):
                DeliveryRecord(
                    candidate_id=self.key,
                    destination_key="shared-aws-change-alerts",
                    request=self.request,
                    next_action_at=0,
                    state_version=cast(int, bad),
                )

    def test_non_numeric_next_action_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "non-negative integer Unix timestamp"):
            DeliveryRecord(
                candidate_id=self.key,
                destination_key="shared-aws-change-alerts",
                request=self.request,
                next_action_at=cast(int, "2026-07-13T16:30:00Z"),
            )

    def test_dispatchable_state_requires_next_action(self):
        for status in ("pending_queue", "failed_retryable"):
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, "require next_action_at"):
                DeliveryRecord(
                    candidate_id=self.key,
                    destination_key="shared-aws-change-alerts",
                    request=self.request,
                    next_action_at=None,
                    status=status,
                )

    def test_next_action_is_an_explicit_constructor_argument(self):
        with self.assertRaisesRegex(TypeError, "next_action_at"):
            # Deliberately bypass the static guard to exercise the runtime API.
            DeliveryRecord(  # type: ignore[call-arg]
                candidate_id=self.key,
                destination_key="shared-aws-change-alerts",
                request=self.request,
            )

    def test_every_documented_state_is_accepted(self):
        for status in DELIVERY_STATES:
            with self.subTest(status=status):
                record = DeliveryRecord(
                    candidate_id=self.key,
                    destination_key="shared-aws-change-alerts",
                    request=self.request,
                    status=status,
                    next_action_at=0 if status in ("pending_queue", "failed_retryable") else None,
                    attempt_id="a1" if status == "sending" else None,
                    lease_expires_at=999 if status == "sending" else None,
                )
                self.assertEqual(record.status, status)


class DispatchFieldTests(OutboxTestCase):
    """The delivery item's claim fields, validated the way chapter 02 reads them."""

    def base(self, **fields):
        return dict(
            candidate_id=self.key,
            destination_key="shared-aws-change-alerts",
            request=self.request,
            next_action_at=0,
            **fields,
        )

    def test_a_claim_carries_both_generation_and_dispatch_id(self):
        dispatch_id = queue_dispatch_id(self.request["request_id"], 1)
        record = DeliveryRecord(**self.base(dispatch_generation=1, dispatch_id=dispatch_id))
        self.assertEqual(record.dispatch_generation, 1)
        self.assertEqual(record.dispatch_id, dispatch_id)

    def test_a_dispatch_id_requires_its_generation(self):
        with self.assertRaisesRegex(ValueError, "dispatch_id requires a dispatch_generation"):
            DeliveryRecord(**self.base(dispatch_id=queue_dispatch_id(self.request["request_id"], 1)))

    def test_a_generation_is_a_positive_integer(self):
        for bad in (0, -1, True, "1"):
            with self.subTest(generation=bad):
                with self.assertRaisesRegex(ValueError, "dispatch_generation must be a positive integer"):
                    DeliveryRecord(**self.base(dispatch_generation=cast(int, bad)))

    def test_a_dispatch_id_is_a_digest(self):
        with self.assertRaisesRegex(ValueError, "dispatch_id must be a lowercase SHA-256 digest"):
            DeliveryRecord(**self.base(dispatch_generation=1, dispatch_id="not-a-digest"))

    def test_a_dispatch_id_must_derive_from_the_request_and_generation(self):
        with self.assertRaisesRegex(ValueError, "does not derive"):
            DeliveryRecord(**self.base(dispatch_generation=1, dispatch_id="f" * 64))


class DispatchStoreTests(OutboxTestCase):
    """The conditional claim and transition operations the dispatcher drives."""

    def seed(self, status="pending_queue", next_action_at=1000, **fields):
        record = DeliveryRecord(
            candidate_id=self.key,
            destination_key="shared-aws-change-alerts",
            request=self.request,
            next_action_at=next_action_at,
            status=status,
            **fields,
        )
        self.store.put_delivery_if_absent(record)
        return record

    def test_query_due_returns_only_due_work_oldest_first(self):
        pending_key = self.key
        due_key = "due-key"
        retry_key = "retry-key"
        posted_key = "posted-key"
        self.seed(next_action_at=2000)
        self.store.put_delivery_if_absent(
            DeliveryRecord(
                candidate_id=due_key,
                destination_key="shared-aws-change-alerts",
                request=self.request,
                next_action_at=500,
            )
        )
        self.store.put_delivery_if_absent(
            DeliveryRecord(
                candidate_id=retry_key,
                destination_key="shared-aws-change-alerts",
                request=self.request,
                status="failed_retryable",
                next_action_at=1500,
            )
        )
        self.store.put_delivery_if_absent(
            DeliveryRecord(
                candidate_id=posted_key,
                destination_key="shared-aws-change-alerts",
                request=self.request,
                status="posted",
                next_action_at=3000,
            )
        )

        self.assertEqual(
            self.store.query_due("pending_queue", due_before=2500, limit=10),
            ((500, due_key), (2000, pending_key)),
            msg="oldest first, matching the GSI range key",
        )
        self.assertEqual(self.store.query_due("failed_retryable", due_before=2500, limit=10), ((1500, retry_key),))
        self.assertEqual(self.store.query_due("posted", due_before=2500, limit=10), ())
        self.assertEqual(
            self.store.query_due("pending_queue", due_before=900, limit=10),
            ((500, due_key),),
            msg="future pending work is not due",
        )

    def test_query_due_respects_the_limit(self):
        self.seed(next_action_at=100)
        self.store.put_delivery_if_absent(
            DeliveryRecord(
                candidate_id="other",
                destination_key="shared-aws-change-alerts",
                request=self.request,
                next_action_at=200,
            )
        )
        self.assertEqual(self.store.query_due("pending_queue", due_before=300, limit=1), ((100, self.key),))

    def test_query_state_returns_future_and_due_records_oldest_first_with_a_hard_limit(self):
        self.seed(next_action_at=200)
        self.store.put_delivery_if_absent(
            DeliveryRecord(
                candidate_id="older",
                destination_key="shared-aws-change-alerts",
                request=self.request,
                next_action_at=100,
            )
        )
        self.assertEqual(self.store.query_state("pending_queue", limit=1), ((100, "older"),))
        self.assertEqual(
            self.store.query_state("pending_queue", limit=10),
            ((100, "older"), (200, self.key)),
        )

    def test_expired_sending_recovery_preserves_evidence_and_refuses_every_stale_guard(self):
        response = {"response_class": "connect_failed", "bytes_sent": False}

        for label, changes in (
            ("state version", {"expected_state_version": 99}),
            ("attempt", {"attempt_id": "wrong"}),
            ("lease identity", {"lease_expires_at": 999}),
            ("not due", {"due_before": 1029}),
        ):
            with self.subTest(label=label):
                self.store = InMemoryOutboxStore()
                self.seed(
                    status="sending",
                    next_action_at=1030,
                    state_version=4,
                    attempt_id="attempt-4",
                    lease_expires_at=1030,
                    network_attempt_count=2,
                    slack_response=response,
                )
                arguments: dict[str, Any] = {
                    "expected_state_version": 4,
                    "attempt_id": "attempt-4",
                    "lease_expires_at": 1030,
                    "due_before": 1030,
                    "network_attempt_count": 2,
                    "slack_response": response,
                    **changes,
                }
                self.assertFalse(self.store.mark_expired_sending_unknown(self.key, **arguments))
                self.assertEqual(delivery(self.store, self.key).status, "sending")

        self.store = InMemoryOutboxStore()
        self.seed(
            status="sending",
            next_action_at=1030,
            state_version=4,
            attempt_id="attempt-4",
            lease_expires_at=1030,
            network_attempt_count=2,
            slack_response=response,
        )
        self.assertTrue(
            self.store.mark_expired_sending_unknown(
                self.key,
                expected_state_version=4,
                attempt_id="attempt-4",
                lease_expires_at=1030,
                due_before=1030,
                network_attempt_count=2,
                slack_response=response,
            )
        )
        recovered = delivery(self.store, self.key)
        self.assertEqual(recovered.status, "delivery_unknown")
        self.assertEqual(recovered.network_attempt_count, 2)
        self.assertEqual(recovered.slack_response, response)
        self.assertIsNone(recovered.expires_at)

    def test_claim_succeeds_once_and_refuses_a_second_claim(self):
        initial = self.seed()
        claimed = self.store.claim_dispatch(
            self.key,
            expected_state_version=initial.state_version,
            expected_generation=initial.dispatch_generation,
            request_id=self.request["request_id"],
            due_before=2000,
        )
        self.assertEqual(claimed, (1, queue_dispatch_id(self.request["request_id"], 1)))
        record = delivery(self.store, self.key)
        self.assertEqual(record.dispatch_generation, 1)
        self.assertEqual(record.state_version, 2)

        again = self.store.claim_dispatch(
            self.key,
            expected_state_version=record.state_version,
            expected_generation=record.dispatch_generation,
            request_id=self.request["request_id"],
            due_before=2000,
        )
        self.assertIsNone(again, msg="an active claim prevents a second concurrent generation")
        self.assertEqual(delivery(self.store, self.key).dispatch_generation, 1)

    def test_claim_refuses_work_that_is_not_yet_due(self):
        initial = self.seed(next_action_at=5000)
        self.assertIsNone(
            self.store.claim_dispatch(
                self.key,
                expected_state_version=initial.state_version,
                expected_generation=initial.dispatch_generation,
                request_id=self.request["request_id"],
                due_before=2000,
            )
        )

    def test_claim_refuses_a_resolved_record(self):
        initial = self.seed(status="queued", next_action_at=1000)
        self.assertIsNone(
            self.store.claim_dispatch(
                self.key,
                expected_state_version=initial.state_version,
                expected_generation=initial.dispatch_generation,
                request_id=self.request["request_id"],
                due_before=2000,
            )
        )

    def test_claim_refuses_a_stale_state_version_when_the_generation_matches(self):
        initial = self.seed(status="failed_retryable", dispatch_generation=1, state_version=4)
        self.store._deliveries[self.key] = DeliveryRecord(
            candidate_id=self.key,
            destination_key="shared-aws-change-alerts",
            request=self.request,
            status="failed_retryable",
            next_action_at=1000,
            dispatch_generation=1,
            state_version=7,
        )

        stale = self.store.claim_dispatch(
            self.key,
            expected_state_version=initial.state_version,
            expected_generation=initial.dispatch_generation,
            request_id=self.request["request_id"],
            due_before=2000,
        )

        self.assertIsNone(stale)
        current = delivery(self.store, self.key)
        self.assertEqual(current.dispatch_generation, 1)
        self.assertEqual(current.state_version, 7)

    def test_claim_refuses_a_stale_generation_when_the_state_version_matches(self):
        self.seed(status="failed_retryable", dispatch_generation=2, state_version=7)

        stale = self.store.claim_dispatch(
            self.key,
            expected_state_version=7,
            expected_generation=1,
            request_id=self.request["request_id"],
            due_before=2000,
        )

        self.assertIsNone(stale)
        current = delivery(self.store, self.key)
        self.assertEqual(current.dispatch_generation, 2)
        self.assertEqual(current.state_version, 7)

    def test_mark_queued_requires_the_claimed_dispatch_id(self):
        self.seed(dispatch_generation=1, dispatch_id=queue_dispatch_id(self.request["request_id"], 1))
        self.assertFalse(
            self.store.mark_queued(
                self.key, dispatch_id=queue_dispatch_id(self.request["request_id"], 9), message_id="m", at=2000
            )
        )
        self.assertTrue(
            self.store.mark_queued(
                self.key,
                dispatch_id=queue_dispatch_id(self.request["request_id"], 1),
                message_id="m",
                at=2000,
            )
        )
        record = delivery(self.store, self.key)
        self.assertEqual(record.status, "queued")
        self.assertEqual(record.queue_message_id, "m")
        self.assertEqual(record.state_version, 2, msg="the queued transition bumps the version once")
        self.assertEqual(record.next_action_at, 2000, msg="queued work records its queue-entry time")

    def test_mark_queued_refuses_an_already_queued_record(self):
        self.seed(
            status="queued",
            dispatch_generation=1,
            dispatch_id=queue_dispatch_id(self.request["request_id"], 1),
            queue_message_id="m",
            next_action_at=1000,
        )
        self.assertFalse(
            self.store.mark_queued(
                self.key, dispatch_id=queue_dispatch_id(self.request["request_id"], 1), message_id="m2", at=2000
            )
        )


if __name__ == "__main__":
    unittest.main()
