"""Audited replay of delivery_unknown, from operator decision to store state."""

import argparse
import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import boto3
from botocore.exceptions import EndpointConnectionError
from moto import mock_aws

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import replay_delivery  # noqa: E402

from aws_public_change_feed.manual_replay import (  # noqa: E402
    ReplayRefused,
    apply_found_post,
    apply_unknown_replay,
    plan_found_post,
    plan_unknown_replay,
)
from aws_public_change_feed.outbox import (  # noqa: E402
    MAX_FOUND_POST_HISTORY,
    MAX_MANUAL_REPLAY_HISTORY,
    DeliveryRecord,
    DynamoDBDeliveryStore,
    FoundPostEntry,
    InMemoryOutboxStore,
    ManualReplayEntry,
)

REGION = "us-west-2"
TABLE = "delivery"
DECIDED = datetime(2026, 8, 11, 14, 30, tzinfo=UTC)
DECIDED_TS = int(DECIDED.timestamp())
PRIOR_ATTEMPT = "prior-attempt"
NEW_ATTEMPT = "1" * 32


def load_json(name):
    with (ROOT / "examples" / name).open(encoding="utf-8") as handle:
        return json.load(handle)


def replay_entry(index=1, *, prior=PRIOR_ATTEMPT, new=NEW_ATTEMPT):
    return ManualReplayEntry(
        decided_at="2026-08-11T14:30:00Z",
        operator="operator@example.com",
        reason=f"Approved replay {index}",
        evidence=f"Slack search found no matching post {index}",
        prior_attempt_id=prior,
        new_attempt_id=new,
    )


def unknown_record(**overrides):
    candidate = load_json("alert-candidate.json")
    defaults = {
        "candidate_id": candidate["candidate_id"],
        "destination_key": "shared-aws-change-alerts",
        "request": load_json("delivery-request.json"),
        "next_action_at": None,
        "status": "delivery_unknown",
        "state_version": 7,
        "created_at": "2026-07-13T17:30:00Z",
        "dispatch_generation": 2,
        "queue_message_id": "old-message",
        "last_attempt_id": PRIOR_ATTEMPT,
        "network_attempt_count": 1,
        "slack_response": {"response_class": "timeout", "bytes_sent": True},
    }
    defaults.update(overrides)
    return DeliveryRecord(**defaults)


def plan(record):
    return plan_unknown_replay(
        record,
        expected_state_version=record.state_version,
        operator="operator@example.com",
        reason="Approved after checking Slack",
        evidence="No matching post in the destination search window",
        clock=lambda: DECIDED,
        attempt_id_factory=lambda: NEW_ATTEMPT,
    )


def found_post_plan(record, *, retention=86400):
    return plan_found_post(
        record,
        expected_state_version=record.state_version,
        operator="operator@example.com",
        reason="Closed after finding the Slack post",
        evidence="Slack search found the posted message",
        terminal_retention_seconds=retention,
        slack_message_ts="1723386600.000100",
        slack_permalink="https://example.slack.com/archives/C0ALERTS/p1723386600000100",
        slack_reference="operator note 42",
        clock=lambda: DECIDED,
    )


def create_table(client):
    client.create_table(
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


class ManualReplayModelTests(unittest.TestCase):
    def test_in_memory_replay_appends_audit_and_preserves_delivery_authority(self):
        store = InMemoryOutboxStore()
        original = unknown_record()
        store._deliveries[original.candidate_id] = original

        result = apply_unknown_replay(store, plan(original))

        current = store.get_delivery(original.candidate_id)
        assert current is not None
        self.assertEqual(result.state_version, 8)
        self.assertEqual(current.status, "pending_queue")
        self.assertEqual(current.next_action_at, DECIDED_TS)
        self.assertEqual(current.next_attempt_id, NEW_ATTEMPT)
        self.assertEqual(current.manual_replay_history[-1].prior_attempt_id, PRIOR_ATTEMPT)
        self.assertEqual(current.manual_replay_history[-1].new_attempt_id, NEW_ATTEMPT)
        self.assertEqual(current.request, original.request)
        self.assertEqual(current.destination_key, original.destination_key)
        self.assertEqual(current.created_at, original.created_at)
        self.assertEqual(current.network_attempt_count, original.network_attempt_count)
        self.assertEqual(current.dispatch_generation, original.dispatch_generation)
        self.assertIsNone(current.queue_message_id)
        self.assertIsNone(current.slack_response)
        self.assertIsNone(current.expires_at)

    def test_a_stale_plan_is_refused_without_changing_the_winner(self):
        store = InMemoryOutboxStore()
        original = unknown_record()
        store._deliveries[original.candidate_id] = original
        replay_plan = plan(original)
        store._deliveries[original.candidate_id] = replace(original, state_version=8)

        with self.assertRaisesRegex(ReplayRefused, "record changed"):
            apply_unknown_replay(store, replay_plan)

        self.assertEqual(store.get_delivery(original.candidate_id), replace(original, state_version=8))

    def test_planning_refuses_wrong_state_missing_attempt_reservation_and_full_history(self):
        cases = (
            ("wrong state", replace(unknown_record(), status="posted", expires_at=DECIDED_TS + 100), "requires"),
            ("missing attempt", replace(unknown_record(), last_attempt_id=None), "no prior attempt"),
            (
                "full history",
                replace(
                    unknown_record(),
                    manual_replay_history=tuple(
                        replay_entry(index, new=f"{index:032x}") for index in range(MAX_MANUAL_REPLAY_HISTORY)
                    ),
                ),
                "already has 25",
            ),
        )
        for name, record, message in cases:
            with self.subTest(case=name), self.assertRaisesRegex(ValueError, message):
                plan(record)

        with self.assertRaisesRegex(ValueError, "only valid"):
            unknown_record(next_attempt_id=NEW_ATTEMPT, manual_replay_history=(replay_entry(),))

    def test_audit_fields_and_history_are_bounded(self):
        base = replay_entry()
        mutations = (
            ("operator blank", {"operator": ""}, "operator"),
            ("reason multiline", {"reason": "one\ntwo"}, "single line"),
            ("evidence oversize", {"evidence": "e" * 2001}, "2000"),
            ("prior oversize", {"prior_attempt_id": "a" * 129}, "128"),
            ("new malformed", {"new_attempt_id": "not-a-uuid"}, "32 lowercase"),
        )
        for name, fields, message in mutations:
            with self.subTest(case=name), self.assertRaisesRegex(ValueError, message):
                replace(base, **fields)

        with self.assertRaisesRegex(ValueError, "cannot exceed"):
            unknown_record(manual_replay_history=tuple(replay_entry(index, new=f"{index:032x}") for index in range(26)))

    def test_found_post_reconciliation_closes_unknown_without_a_new_attempt(self):
        store = InMemoryOutboxStore()
        original = unknown_record()
        store._deliveries[original.candidate_id] = original

        result = apply_found_post(store, found_post_plan(original))

        current = store.get_delivery(original.candidate_id)
        assert current is not None
        self.assertEqual(result.state_version, 8)
        self.assertEqual(result.found_post_count, 1)
        self.assertEqual(current.status, "posted")
        self.assertIsNone(current.next_action_at)
        self.assertIsNone(current.next_attempt_id)
        self.assertEqual(current.manual_replay_history, ())
        self.assertEqual(current.found_post_history[-1].prior_attempt_id, PRIOR_ATTEMPT)
        self.assertEqual(current.request, original.request)
        self.assertEqual(current.destination_key, original.destination_key)
        self.assertEqual(current.created_at, original.created_at)
        self.assertEqual(current.network_attempt_count, original.network_attempt_count)
        self.assertEqual(current.dispatch_generation, original.dispatch_generation)
        self.assertIsNone(current.queue_message_id)
        self.assertEqual(current.slack_response, original.slack_response)
        self.assertEqual(current.expires_at, DECIDED_TS + 86400)

    def test_found_post_conditions_refuse_stale_wrong_state_ttl_reservation_and_cap(self):
        entry = found_post_plan(unknown_record()).entry
        wrong_state = unknown_record(status="queued", next_action_at=DECIDED_TS)
        corrupt_ttl = unknown_record()
        object.__setattr__(corrupt_ttl, "expires_at", 999)
        corrupt_reservation = unknown_record()
        object.__setattr__(corrupt_reservation, "next_attempt_id", NEW_ATTEMPT)
        capped = unknown_record(
            found_post_history=tuple(
                replace(entry, reason=f"Found post {index}") for index in range(MAX_FOUND_POST_HISTORY)
            )
        )
        cases = (
            ("stale version", unknown_record(), 6, PRIOR_ATTEMPT, entry),
            (
                "wrong prior",
                unknown_record(),
                7,
                "another-attempt",
                replace(entry, prior_attempt_id="another-attempt"),
            ),
            ("wrong state", wrong_state, 7, PRIOR_ATTEMPT, entry),
            ("existing ttl", corrupt_ttl, 7, PRIOR_ATTEMPT, entry),
            ("existing reservation", corrupt_reservation, 7, PRIOR_ATTEMPT, entry),
            ("history cap", capped, 7, PRIOR_ATTEMPT, entry),
        )

        for name, original, expected_version, expected_prior, candidate_entry in cases:
            with self.subTest(case=name):
                store = InMemoryOutboxStore()
                store._deliveries[original.candidate_id] = original

                self.assertFalse(
                    store.reconcile_found_post(
                        original.candidate_id,
                        expected_state_version=expected_version,
                        expected_prior_attempt_id=expected_prior,
                        entry=candidate_entry,
                        expires_at=DECIDED_TS + 86400,
                    )
                )
                self.assertIs(store.get_delivery(original.candidate_id), original)

    def test_found_post_fields_are_bounded_and_distinct_from_replay(self):
        entry = found_post_plan(unknown_record()).entry
        mutations = (
            ("operator blank", {"operator": ""}, "operator"),
            ("reason multiline", {"reason": "one\ntwo"}, "single line"),
            ("evidence oversize", {"evidence": "e" * 2001}, "2000"),
            ("prior oversize", {"prior_attempt_id": "a" * 129}, "128"),
            ("message ts multiline", {"slack_message_ts": "1\n2"}, "single line"),
            ("permalink oversize", {"slack_permalink": "h" * 501}, "500"),
        )
        for name, fields, message in mutations:
            with self.subTest(case=name), self.assertRaisesRegex(ValueError, message):
                replace(entry, **fields)

        with self.assertRaisesRegex(ValueError, "fields do not match"):
            FoundPostEntry.from_document({**entry.document(), "message_body": "secret message text"})
        with self.assertRaisesRegex(ValueError, "cannot exceed"):
            unknown_record(
                found_post_history=tuple(
                    replace(entry, reason=f"Found post {index}") for index in range(MAX_FOUND_POST_HISTORY + 1)
                )
            )


@mock_aws
class DynamoManualReplayTests(unittest.TestCase):
    def setUp(self):
        self.client = boto3.client("dynamodb", region_name=REGION)
        create_table(self.client)
        self.store = DynamoDBDeliveryStore(self.client, TABLE)
        self.original = unknown_record()
        self.key = self.original.candidate_id
        self.assertTrue(self.store.put_delivery_if_absent(self.original))

    def test_replay_roundtrips_native_history_and_reserved_attempt(self):
        result = apply_unknown_replay(self.store, plan(self.original))

        current = self.store.get_delivery(self.key)
        assert current is not None
        self.assertEqual(result.new_attempt_id, NEW_ATTEMPT)
        self.assertEqual(current.status, "pending_queue")
        self.assertEqual(current.state_version, 8)
        self.assertEqual(current.next_attempt_id, NEW_ATTEMPT)
        self.assertEqual(current.manual_replay_history, (plan(self.original).entry,))
        raw = self.client.get_item(
            TableName=TABLE,
            Key={"PK": {"S": f"CANDIDATE#{self.key}"}, "SK": {"S": "DELIVERY"}},
            ConsistentRead=True,
        )["Item"]
        self.assertEqual(raw["manual_replay_history"]["L"][0]["M"]["new_attempt_id"]["S"], NEW_ATTEMPT)
        self.assertNotIn("queue_message_id", raw)
        self.assertNotIn("slack_response", raw)

    def test_found_post_roundtrips_native_history_ttl_and_no_reservation(self):
        result = apply_found_post(self.store, found_post_plan(self.original))

        current = self.store.get_delivery(self.key)
        assert current is not None
        self.assertEqual(result.state_version, 8)
        self.assertEqual(current.status, "posted")
        self.assertIsNone(current.next_attempt_id)
        self.assertEqual(current.manual_replay_history, ())
        self.assertEqual(current.found_post_history, (found_post_plan(self.original).entry,))
        self.assertEqual(current.expires_at, DECIDED_TS + 86400)
        raw = self.client.get_item(
            TableName=TABLE,
            Key={"PK": {"S": f"CANDIDATE#{self.key}"}, "SK": {"S": "DELIVERY"}},
            ConsistentRead=True,
        )["Item"]
        self.assertEqual(raw["found_post_history"]["L"][0]["M"]["prior_attempt_id"]["S"], PRIOR_ATTEMPT)
        self.assertNotIn("next_attempt_id", raw)
        self.assertNotIn("queue_message_id", raw)

    def test_found_post_conditions_refuse_stale_wrong_state_ttl_reservation_and_cap(self):
        entry = found_post_plan(self.original).entry
        self.assertFalse(
            self.store.reconcile_found_post(
                self.key,
                expected_state_version=6,
                expected_prior_attempt_id=PRIOR_ATTEMPT,
                entry=entry,
                expires_at=DECIDED_TS + 86400,
            )
        )
        self.assertFalse(
            self.store.reconcile_found_post(
                self.key,
                expected_state_version=7,
                expected_prior_attempt_id="another-attempt",
                entry=replace(entry, prior_attempt_id="another-attempt"),
                expires_at=DECIDED_TS + 86400,
            )
        )

        key = {"PK": {"S": f"CANDIDATE#{self.key}"}, "SK": {"S": "DELIVERY"}}
        corruptions: tuple[tuple[str, str, dict[str, dict[str, str]]], ...] = (
            ("state", "SET #status = :value", {":value": {"S": "posted"}}),
            ("ttl", "SET expires_at = :value", {":value": {"N": "999"}}),
            ("reservation", "SET next_attempt_id = :value", {":value": {"S": "2" * 32}}),
        )
        for name, expression, values in corruptions:
            with self.subTest(case=name):
                self.client.delete_item(TableName=TABLE, Key=key)
                self.assertTrue(self.store.put_delivery_if_absent(self.original))
                kwargs = {
                    "TableName": TABLE,
                    "Key": key,
                    "UpdateExpression": expression,
                    "ExpressionAttributeValues": values,
                }
                if name == "state":
                    kwargs["ExpressionAttributeNames"] = {"#status": "status"}
                self.client.update_item(**kwargs)
                self.assertFalse(
                    self.store.reconcile_found_post(
                        self.key,
                        expected_state_version=7,
                        expected_prior_attempt_id=PRIOR_ATTEMPT,
                        entry=entry,
                        expires_at=DECIDED_TS + 86400,
                    )
                )

        capped = unknown_record(
            candidate_id="found-post-cap",
            found_post_history=tuple(
                replace(entry, reason=f"Found post {index}") for index in range(MAX_FOUND_POST_HISTORY)
            ),
        )
        self.assertTrue(self.store.put_delivery_if_absent(capped))
        self.assertFalse(
            self.store.reconcile_found_post(
                capped.candidate_id,
                expected_state_version=capped.state_version,
                expected_prior_attempt_id=PRIOR_ATTEMPT,
                entry=entry,
                expires_at=DECIDED_TS + 86400,
            )
        )

    def test_state_version_prior_attempt_state_and_history_cap_are_conditions(self):
        entry = plan(self.original).entry
        cases: tuple[tuple[str, ManualReplayEntry, int, str], ...] = (
            ("stale version", entry, 6, PRIOR_ATTEMPT),
            (
                "wrong prior",
                replace(entry, prior_attempt_id="another-attempt"),
                7,
                "another-attempt",
            ),
        )
        for name, candidate_entry, expected_version, expected_prior in cases:
            with self.subTest(case=name):
                self.assertFalse(
                    self.store.replay_unknown(
                        self.key,
                        expected_state_version=expected_version,
                        expected_prior_attempt_id=expected_prior,
                        entry=candidate_entry,
                        next_action_at=DECIDED_TS,
                    )
                )

        self.client.update_item(
            TableName=TABLE,
            Key={"PK": {"S": f"CANDIDATE#{self.key}"}, "SK": {"S": "DELIVERY"}},
            UpdateExpression="SET #status = :posted",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":posted": {"S": "posted"}},
        )
        self.assertFalse(
            self.store.replay_unknown(
                self.key,
                expected_state_version=7,
                expected_prior_attempt_id=PRIOR_ATTEMPT,
                entry=entry,
                next_action_at=DECIDED_TS,
            )
        )

        capped = unknown_record(
            candidate_id="history-cap",
            manual_replay_history=tuple(
                replay_entry(index, new=f"{index:032x}") for index in range(MAX_MANUAL_REPLAY_HISTORY)
            ),
        )
        self.assertTrue(self.store.put_delivery_if_absent(capped))
        self.assertFalse(
            self.store.replay_unknown(
                capped.candidate_id,
                expected_state_version=capped.state_version,
                expected_prior_attempt_id=PRIOR_ATTEMPT,
                entry=entry,
                next_action_at=DECIDED_TS,
            )
        )

    def test_corrupt_ttl_missing_prior_and_existing_reservation_are_refused(self):
        entry = plan(self.original).entry
        key = {"PK": {"S": f"CANDIDATE#{self.key}"}, "SK": {"S": "DELIVERY"}}
        corruptions: tuple[tuple[str, str, dict[str, dict[str, str]]], ...] = (
            ("ttl", "SET expires_at = :value", {":value": {"N": "999"}}),
            ("missing prior", "REMOVE last_attempt_id", {}),
            ("reservation", "SET next_attempt_id = :value", {":value": {"S": "2" * 32}}),
        )
        for name, expression, values in corruptions:
            with self.subTest(case=name):
                self.client.delete_item(TableName=TABLE, Key=key)
                self.assertTrue(self.store.put_delivery_if_absent(self.original))
                if values:
                    self.client.update_item(
                        TableName=TABLE,
                        Key=key,
                        UpdateExpression=expression,
                        ExpressionAttributeValues=values,
                    )
                else:
                    self.client.update_item(TableName=TABLE, Key=key, UpdateExpression=expression)
                self.assertFalse(
                    self.store.replay_unknown(
                        self.key,
                        expected_state_version=7,
                        expected_prior_attempt_id=PRIOR_ATTEMPT,
                        entry=entry,
                        next_action_at=DECIDED_TS,
                    )
                )

    def test_legacy_item_decodes_with_empty_replay_fields(self):
        legacy = unknown_record(candidate_id="legacy")
        self.assertTrue(self.store.put_delivery_if_absent(legacy))
        key = {"PK": {"S": "CANDIDATE#legacy"}, "SK": {"S": "DELIVERY"}}
        self.client.update_item(TableName=TABLE, Key=key, UpdateExpression="REMOVE last_attempt_id")

        decoded = self.store.get_delivery("legacy")

        assert decoded is not None
        self.assertIsNone(decoded.last_attempt_id)
        self.assertIsNone(decoded.next_attempt_id)
        self.assertEqual(decoded.manual_replay_history, ())


@mock_aws
class ReplayCliTests(unittest.TestCase):
    def setUp(self):
        self.client = boto3.client("dynamodb", region_name=REGION)
        create_table(self.client)
        self.store = DynamoDBDeliveryStore(self.client, TABLE)
        self.original = unknown_record()
        self.assertTrue(self.store.put_delivery_if_absent(self.original))

    def arguments(self, *, apply=False, version=7, secret="SIGNINGSECRET"):
        return argparse.Namespace(
            table_name=TABLE,
            candidate_id=self.original.candidate_id,
            expected_state_version=version,
            operator=f"operator-{secret}",
            reason=f"approved-{secret}",
            evidence=f"searched-{secret}",
            found_post=False,
            terminal_retention_seconds=None,
            slack_message_ts=None,
            slack_permalink=None,
            slack_reference=None,
            apply=apply,
        )

    def found_post_arguments(self, *, apply=False, version=7, secret="SIGNINGSECRET"):
        arguments = self.arguments(apply=apply, version=version, secret=secret)
        arguments.found_post = True
        arguments.terminal_retention_seconds = 86400
        arguments.slack_message_ts = f"1723386600.000100-{secret}"
        arguments.slack_permalink = f"https://example.slack.com/archives/C0ALERTS/p1723386600000100-{secret}"
        arguments.slack_reference = f"operator-note-{secret}"
        return arguments

    def invoke(self, arguments, client=None):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = replay_delivery.run(arguments, self.client if client is None else client)
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def test_preview_is_read_only_and_redacts_operator_evidence(self):
        before = self.store.get_delivery(self.original.candidate_id)

        exit_code, stdout, stderr = self.invoke(self.arguments())

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(json.loads(stdout)["status"], "preview")
        self.assertNotIn("SIGNINGSECRET", stdout)
        self.assertEqual(self.store.get_delivery(self.original.candidate_id), before)

    def test_apply_performs_one_conditional_write_and_prints_safe_facts(self):
        exit_code, stdout, stderr = self.invoke(self.arguments(apply=True))

        output = json.loads(stdout)
        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(output["status"], "applied")
        self.assertEqual(output["ManualReplay"], 1)
        self.assertEqual(len(output["new_attempt_id"]), 32)
        self.assertNotIn("SIGNINGSECRET", stdout)
        current = self.store.get_delivery(self.original.candidate_id)
        assert current is not None
        self.assertEqual(current.state_version, 8)

    def test_found_post_preview_and_apply_are_safe_and_create_no_new_attempt(self):
        before = self.store.get_delivery(self.original.candidate_id)

        preview_code, preview_stdout, preview_stderr = self.invoke(self.found_post_arguments())

        self.assertEqual(preview_code, 0)
        self.assertEqual(preview_stderr, "")
        preview = json.loads(preview_stdout)
        self.assertEqual(preview["status"], "preview")
        self.assertEqual(preview["action"], "found_post_reconciliation")
        self.assertIn("expires_at", preview)
        self.assertNotIn("SIGNINGSECRET", preview_stdout)
        self.assertEqual(self.store.get_delivery(self.original.candidate_id), before)

        apply_code, apply_stdout, apply_stderr = self.invoke(self.found_post_arguments(apply=True))

        output = json.loads(apply_stdout)
        self.assertEqual(apply_code, 0)
        self.assertEqual(apply_stderr, "")
        self.assertEqual(output["status"], "applied")
        self.assertEqual(output["new_state"], "posted")
        self.assertEqual(output["FoundPostReconciliation"], 1)
        self.assertNotIn("SIGNINGSECRET", apply_stdout)
        current = self.store.get_delivery(self.original.candidate_id)
        assert current is not None
        self.assertEqual(current.status, "posted")
        self.assertIsNone(current.next_attempt_id)
        self.assertEqual(current.manual_replay_history, ())
        self.assertEqual(len(current.found_post_history), 1)

    def test_found_post_requires_terminal_retention_and_reports_ambiguity_after_write_attempt(self):
        missing_retention = self.found_post_arguments(apply=True)
        missing_retention.terminal_retention_seconds = None

        invalid_code, _, invalid_error = self.invoke(missing_retention)

        self.assertEqual(invalid_code, replay_delivery.EXIT_INVALID)
        self.assertEqual(json.loads(invalid_error)["status"], "invalid")

        class AmbiguousClient:
            def __init__(self, delegate):
                self.delegate = delegate
                self.update_calls = 0

            def get_item(self, **kwargs):
                return self.delegate.get_item(**kwargs)

            def update_item(self, **kwargs):
                self.update_calls += 1
                raise EndpointConnectionError(endpoint_url="https://dynamodb.invalid")

        ambiguous_client = AmbiguousClient(self.client)
        ambiguous_code, _, ambiguous_error = self.invoke(
            self.found_post_arguments(apply=True),
            client=ambiguous_client,
        )
        self.assertEqual(ambiguous_code, replay_delivery.EXIT_AMBIGUOUS)
        self.assertEqual(json.loads(ambiguous_error)["status"], "ambiguous")
        self.assertEqual(ambiguous_client.update_calls, 1)
        self.assertNotIn("dynamodb.invalid", ambiguous_error)
        self.assertNotIn("SIGNINGSECRET", ambiguous_error)

    def test_initial_read_failure_is_distinct_from_write_ambiguity_in_both_modes(self):
        class ReadFailureClient:
            def __init__(self):
                self.update_calls = 0

            def get_item(self, **kwargs):
                raise EndpointConnectionError(endpoint_url="https://dynamodb.invalid")

            def update_item(self, **kwargs):
                self.update_calls += 1
                raise AssertionError("a failed initial read must not attempt an update")

        for apply in (False, True):
            with self.subTest(apply=apply):
                client = ReadFailureClient()

                exit_code, stdout, stderr = self.invoke(self.arguments(apply=apply), client=client)

                self.assertEqual(exit_code, replay_delivery.EXIT_AMBIGUOUS)
                self.assertEqual(stdout, "")
                error = json.loads(stderr)
                self.assertEqual(error["status"], "read_failed")
                self.assertEqual(
                    error["error"],
                    "AWS read failed before any replay write was attempted; retry after restoring read access",
                )
                self.assertEqual(client.update_calls, 0)
                self.assertNotIn("dynamodb.invalid", stderr)
                self.assertNotIn("SIGNINGSECRET", stderr)

    def test_stale_apply_is_distinct_from_an_ambiguous_write(self):
        class RacingClient:
            def __init__(self, delegate):
                self.delegate = delegate
                self.raced = False

            def get_item(self, **kwargs):
                return self.delegate.get_item(**kwargs)

            def update_item(self, **kwargs):
                if not self.raced:
                    self.raced = True
                    self.delegate.update_item(
                        TableName=kwargs["TableName"],
                        Key=kwargs["Key"],
                        UpdateExpression="SET state_version = :winner",
                        ExpressionAttributeValues={":winner": {"N": "8"}},
                    )
                return self.delegate.update_item(**kwargs)

        stale_code, _, stale_error = self.invoke(self.arguments(apply=True), client=RacingClient(self.client))
        self.assertEqual(stale_code, replay_delivery.EXIT_REFUSED)
        self.assertEqual(json.loads(stale_error)["status"], "refused")
        self.assertEqual(json.loads(stale_error)["current_state_version"], 8)

        self.client.update_item(
            TableName=TABLE,
            Key={"PK": {"S": f"CANDIDATE#{self.original.candidate_id}"}, "SK": {"S": "DELIVERY"}},
            UpdateExpression="SET state_version = :version",
            ExpressionAttributeValues={":version": {"N": "7"}},
        )

        class AmbiguousClient:
            def __init__(self, delegate):
                self.delegate = delegate
                self.update_calls = 0

            def get_item(self, **kwargs):
                return self.delegate.get_item(**kwargs)

            def update_item(self, **kwargs):
                self.update_calls += 1
                raise EndpointConnectionError(endpoint_url="https://dynamodb.invalid")

        ambiguous_client = AmbiguousClient(self.client)
        ambiguous_code, _, ambiguous_error = self.invoke(self.arguments(apply=True), client=ambiguous_client)
        self.assertEqual(ambiguous_code, replay_delivery.EXIT_AMBIGUOUS)
        error = json.loads(ambiguous_error)
        self.assertEqual(error["status"], "ambiguous")
        self.assertEqual(error["error"], "AWS did not prove whether the replay write completed; reread before retry")
        self.assertEqual(ambiguous_client.update_calls, 1)
        self.assertNotIn("dynamodb.invalid", ambiguous_error)


if __name__ == "__main__":
    unittest.main()
