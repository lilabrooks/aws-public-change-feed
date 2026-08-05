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
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aws_public_change_feed.identity import audience_fingerprint, candidate_id  # noqa: E402
from aws_public_change_feed.outbox import (  # noqa: E402
    DELIVERY_STATES,
    CandidateIdentityError,
    DeliveryRecord,
    InMemoryOutboxStore,
    build_delivery_request,
    emit,
    verify_durable,
)


def load_json(name):
    with (ROOT / "examples" / name).open(encoding="utf-8") as handle:
        return json.load(handle)


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
        self.assertIsNone(
            record.next_action_at,
            msg="chapter 02 gives scheduling attributes only to work that needs them",
        )

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
        newer["release"]["application_version"] = "9.9.9-newer-release"
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
        newer["release"]["application_version"] = "9.9.9-newer-release"
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
                status="invented",
            )

    def test_every_documented_state_is_accepted(self):
        for status in DELIVERY_STATES:
            with self.subTest(status=status):
                record = DeliveryRecord(
                    candidate_id=self.key,
                    destination_key="shared-aws-change-alerts",
                    request=self.request,
                    status=status,
                )
                self.assertEqual(record.status, status)


if __name__ == "__main__":
    unittest.main()
