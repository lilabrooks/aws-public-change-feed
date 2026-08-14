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
from unittest.mock import patch

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
    apply_terminal_replay,
    apply_unknown_replay,
    plan_found_post,
    plan_terminal_replay,
    plan_unknown_replay,
)
from aws_public_change_feed.outbox import (  # noqa: E402
    MAX_FOUND_POST_HISTORY,
    MAX_MANUAL_REPLAY_HISTORY,
    MAX_TERMINAL_REPLAY_HISTORY,
    TERMINAL_REPLAY_EXHAUSTIBLE_RESPONSE_CLASSES,
    TERMINAL_REPLAY_RESPONSE_CLASSES,
    DeliveryRecord,
    DynamoDBDeliveryStore,
    FoundPostEntry,
    InMemoryOutboxStore,
    ManualReplayEntry,
    TerminalReplayEntry,
)

REGION = "us-west-2"
TABLE = "delivery"
DECIDED = datetime(2026, 8, 11, 14, 30, tzinfo=UTC)
DECIDED_TS = int(DECIDED.timestamp())
PRIOR_ATTEMPT = "prior-attempt"
NEW_ATTEMPT = "1" * 32
EXPECTED_TERMINAL_REPLAY_RESPONSE_CLASSES = frozenset(
    {
        "credential_kind_mismatch",
        "credential_read_error",
        "bot_token_rejected",
        "http_403",
        "http_404",
        "http_410",
        "slack_access_denied",
        "slack_account_inactive",
        "slack_app_access_restricted",
        "slack_channel_not_found",
        "slack_ekm_access_denied",
        "slack_invalid_auth",
        "slack_is_archived",
        "slack_missing_scope",
        "slack_no_permission",
        "slack_not_allowed_token_type",
        "slack_not_authed",
        "slack_not_in_channel",
        "slack_restricted_action",
        "slack_restricted_action_non_threadable_channel",
        "slack_restricted_action_read_only_channel",
        "slack_restricted_action_thread_locked",
        "slack_restricted_action_thread_only_channel",
        "slack_team_access_not_granted",
        "slack_token_expired",
        "slack_token_revoked",
        "webhook_url_rejected",
    }
)
EXPECTED_TERMINAL_REPLAY_EXHAUSTIBLE_RESPONSE_CLASSES = frozenset(
    {
        "http_408",
        "http_429",
        "http_500",
        "http_502",
        "http_503",
        "http_504",
        "slack_internal_error",
        "slack_ratelimited",
        "slack_service_unavailable",
        "transport_connect_failed",
        "transport_tls_failed",
    }
)


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


def terminal_record(**overrides):
    defaults = {
        "status": "failed_terminal",
        "expires_at": DECIDED_TS + 86400,
        "slack_response": {
            "response_class": "http_403",
            "status_code": 403,
            "bytes_sent": True,
            "latency_ms": 12,
        },
    }
    defaults.update(overrides)
    return unknown_record(**defaults)


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


def terminal_plan(record):
    return plan_terminal_replay(
        record,
        expected_state_version=record.state_version,
        operator="operator@example.com",
        reason="Approved after correcting Slack access",
        evidence="Synthetic destination preflight succeeded",
        clock=lambda: DECIDED,
        attempt_id_factory=lambda: NEW_ATTEMPT,
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

    def test_terminal_replay_appends_distinct_audit_and_preserves_exact_authority(self):
        store = InMemoryOutboxStore()
        original = terminal_record(network_attempt_count=5)
        store._deliveries[original.candidate_id] = original

        result = apply_terminal_replay(store, terminal_plan(original))

        current = store.get_delivery(original.candidate_id)
        assert current is not None
        self.assertEqual(result.state_version, 8)
        self.assertEqual(result.terminal_replay_count, 1)
        self.assertEqual(current.status, "pending_queue")
        self.assertEqual(current.next_action_at, DECIDED_TS)
        self.assertEqual(current.next_attempt_id, NEW_ATTEMPT)
        self.assertEqual(current.manual_replay_history, ())
        self.assertEqual(len(current.terminal_replay_history), 1)
        entry = current.terminal_replay_history[0]
        self.assertEqual(entry.prior_attempt_id, PRIOR_ATTEMPT)
        self.assertEqual(entry.new_attempt_id, NEW_ATTEMPT)
        self.assertEqual(entry.prior_response_class, "http_403")
        self.assertIs(entry.prior_attempts_exhausted, False)
        self.assertEqual(entry.prior_expires_at, original.expires_at)
        self.assertEqual(current.request, original.request)
        self.assertEqual(current.destination_key, original.destination_key)
        self.assertEqual(current.created_at, original.created_at)
        self.assertEqual(current.dispatch_generation, original.dispatch_generation)
        self.assertEqual(current.network_attempt_count, 5)
        self.assertIsNone(current.queue_message_id)
        self.assertIsNone(current.slack_response)
        self.assertIsNone(current.expires_at)

    def test_runtime_terminal_replay_sets_equal_the_frozen_contract(self):
        cases = (
            (
                "correctable terminal classes",
                EXPECTED_TERMINAL_REPLAY_RESPONSE_CLASSES,
                TERMINAL_REPLAY_RESPONSE_CLASSES,
            ),
            (
                "exhaustible terminal classes",
                EXPECTED_TERMINAL_REPLAY_EXHAUSTIBLE_RESPONSE_CLASSES,
                TERMINAL_REPLAY_EXHAUSTIBLE_RESPONSE_CLASSES,
            ),
        )
        for name, expected, actual in cases:
            missing = sorted(expected - actual)
            unexpected = sorted(actual - expected)
            with self.subTest(contract=name):
                self.assertFalse(
                    missing or unexpected,
                    f"{name} differ from frozen contract: missing={missing!r}, unexpected={unexpected!r}",
                )

    def test_every_frozen_terminal_class_and_exhausted_budget_plan(self):
        for response_class in sorted(EXPECTED_TERMINAL_REPLAY_RESPONSE_CLASSES):
            metadata: dict[str, object] = {"response_class": response_class}
            if response_class.startswith("http_"):
                metadata.update(status_code=int(response_class[5:]), bytes_sent=True, latency_ms=1)
            elif response_class.startswith("slack_"):
                metadata.update(status_code=200, bytes_sent=True, latency_ms=1)
            with self.subTest(response_class=response_class):
                planned = terminal_plan(terminal_record(slack_response=metadata))
                self.assertEqual(planned.entry.prior_response_class, response_class)
                self.assertIs(planned.entry.prior_attempts_exhausted, False)

        for response_class in sorted(EXPECTED_TERMINAL_REPLAY_EXHAUSTIBLE_RESPONSE_CLASSES):
            exhausted_metadata: dict[str, object] = {
                "response_class": response_class,
                "attempts_exhausted": True,
            }
            if response_class.startswith("http_"):
                exhausted_metadata.update(status_code=int(response_class[5:]), bytes_sent=True, latency_ms=3)
            elif response_class.startswith("slack_"):
                exhausted_metadata.update(status_code=200, bytes_sent=True, latency_ms=3)
            else:
                exhausted_metadata["bytes_sent"] = False
            with self.subTest(exhausted=response_class):
                exhausted = terminal_plan(terminal_record(network_attempt_count=5, slack_response=exhausted_metadata))
                self.assertEqual(exhausted.entry.prior_response_class, response_class)
                self.assertIs(exhausted.entry.prior_attempts_exhausted, True)

    def test_accepted_adr_and_delivery_spec_name_every_frozen_terminal_class(self):
        documents = (
            ROOT / "docs" / "adr" / "021-audited-terminal-record-replay.md",
            ROOT / "docs" / "architecture" / "specification" / "04-alert-processing.md",
        )
        for path in documents:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                for response_class in EXPECTED_TERMINAL_REPLAY_RESPONSE_CLASSES:
                    self.assertIn(f"`{response_class}`", text)
                for response_class in EXPECTED_TERMINAL_REPLAY_EXHAUSTIBLE_RESPONSE_CLASSES:
                    self.assertIn(f"`{response_class}`", text)
                self.assertIn("`attempts_exhausted = true`", text)
                self.assertIn("candidate/release/application", text)
                self.assertIn("potentially recoverable", text)

    def test_terminal_planning_refuses_ineligible_expired_and_malformed_records(self):
        full_entry = terminal_plan(terminal_record()).entry
        cases = (
            ("wrong state", unknown_record(), "requires failed_terminal"),
            ("missing attempt", terminal_record(last_attempt_id=None), "no prior attempt"),
            ("missing expiry", terminal_record(expires_at=None), "no terminal expiry"),
            ("expired", terminal_record(expires_at=DECIDED_TS), "has expired"),
            (
                "ineligible immutable class",
                terminal_record(slack_response={"response_class": "candidate_disagrees_with_release"}),
                "not eligible",
            ),
            (
                "immutable class cannot claim exhaustion",
                terminal_record(
                    slack_response={
                        "response_class": "candidate_disagrees_with_release",
                        "attempts_exhausted": True,
                    }
                ),
                "cannot truthfully carry",
            ),
            (
                "HTTP facts disagree",
                terminal_record(slack_response={"response_class": "http_403", "status_code": 404, "bytes_sent": True}),
                "facts disagree",
            ),
            (
                "unknown field",
                terminal_record(slack_response={"response_class": "credential_read_error", "detail": "secret"}),
                "fields do not match",
            ),
            (
                "full history",
                terminal_record(
                    terminal_replay_history=tuple(
                        replace(
                            full_entry,
                            reason=f"Approved replay {index}",
                            new_attempt_id=f"{index:032x}",
                        )
                        for index in range(MAX_TERMINAL_REPLAY_HISTORY)
                    )
                ),
                "already has 25",
            ),
        )
        for name, record, message in cases:
            with self.subTest(case=name), self.assertRaisesRegex(ValueError, message):
                terminal_plan(record)

        reserved = terminal_record()
        object.__setattr__(reserved, "next_attempt_id", NEW_ATTEMPT)
        with self.assertRaisesRegex(ValueError, "already reserves"):
            terminal_plan(reserved)

    def test_terminal_entry_fields_and_history_are_bounded(self):
        entry = terminal_plan(terminal_record()).entry
        mutations = (
            ("operator blank", {"operator": ""}, "operator"),
            ("reason multiline", {"reason": "one\ntwo"}, "single line"),
            ("evidence oversize", {"evidence": "e" * 2001}, "2000"),
            ("prior oversize", {"prior_attempt_id": "a" * 129}, "128"),
            ("new malformed", {"new_attempt_id": "not-a-uuid"}, "32 lowercase"),
            ("class malformed", {"prior_response_class": "HTTP 403"}, "bounded diagnostic"),
            ("exhausted malformed", {"prior_attempts_exhausted": 1}, "boolean"),
            ("expiry malformed", {"prior_expires_at": -1}, "non-negative"),
        )
        for name, fields, message in mutations:
            with self.subTest(case=name), self.assertRaisesRegex(ValueError, message):
                replace(entry, **fields)

        with self.assertRaisesRegex(ValueError, "fields do not match"):
            TerminalReplayEntry.from_document({**entry.document(), "message_body": "secret message text"})
        with self.assertRaisesRegex(ValueError, "cannot exceed"):
            terminal_record(
                terminal_replay_history=tuple(
                    replace(entry, reason=f"Approved replay {index}", new_attempt_id=f"{index:032x}")
                    for index in range(MAX_TERMINAL_REPLAY_HISTORY + 1)
                )
            )

    def test_terminal_store_refuses_every_stale_or_changed_variant(self):
        original = terminal_record()
        replay = terminal_plan(original)
        full_history = tuple(
            replace(replay.entry, reason=f"Approved replay {index}", new_attempt_id=f"{index:032x}")
            for index in range(MAX_TERMINAL_REPLAY_HISTORY)
        )
        cases = (
            ("stale version", original, 6, PRIOR_ATTEMPT, original.expires_at, original.slack_response),
            (
                "wrong state",
                replace(original, status="posted"),
                7,
                PRIOR_ATTEMPT,
                original.expires_at,
                original.slack_response,
            ),
            ("wrong prior", original, 7, "another-attempt", original.expires_at, original.slack_response),
            ("changed expiry", original, 7, PRIOR_ATTEMPT, original.expires_at + 1, original.slack_response),
            (
                "changed response",
                original,
                7,
                PRIOR_ATTEMPT,
                original.expires_at,
                {"response_class": "credential_read_error"},
            ),
            (
                "history cap",
                replace(original, terminal_replay_history=full_history),
                7,
                PRIOR_ATTEMPT,
                original.expires_at,
                original.slack_response,
            ),
        )
        for name, stored, version, prior, expiry, response in cases:
            with self.subTest(case=name):
                store = InMemoryOutboxStore()
                store._deliveries[stored.candidate_id] = stored
                self.assertFalse(
                    store.replay_terminal(
                        stored.candidate_id,
                        expected_state_version=version,
                        expected_prior_attempt_id=prior,
                        expected_expires_at=expiry,
                        expected_slack_response=response,
                        entry=replace(
                            replay.entry,
                            prior_attempt_id=prior,
                            prior_expires_at=expiry,
                            prior_response_class=response["response_class"],
                        ),
                        next_action_at=DECIDED_TS,
                    )
                )
                self.assertIs(store.get_delivery(stored.candidate_id), stored)

        reserved = terminal_record()
        object.__setattr__(reserved, "next_attempt_id", NEW_ATTEMPT)
        store = InMemoryOutboxStore()
        store._deliveries[reserved.candidate_id] = reserved
        self.assertFalse(
            store.replay_terminal(
                reserved.candidate_id,
                expected_state_version=7,
                expected_prior_attempt_id=PRIOR_ATTEMPT,
                expected_expires_at=reserved.expires_at,
                expected_slack_response=reserved.slack_response,
                entry=replay.entry,
                next_action_at=DECIDED_TS,
            )
        )

    def test_terminal_store_refuses_equal_or_expired_ttl_without_mutation(self):
        eligible_entry = terminal_plan(terminal_record()).entry
        for expiry in (DECIDED_TS, DECIDED_TS - 1):
            with self.subTest(expires_at=expiry):
                stored = terminal_record(expires_at=expiry)
                store = InMemoryOutboxStore()
                store._deliveries[stored.candidate_id] = stored

                self.assertFalse(
                    store.replay_terminal(
                        stored.candidate_id,
                        expected_state_version=stored.state_version,
                        expected_prior_attempt_id=PRIOR_ATTEMPT,
                        expected_expires_at=expiry,
                        expected_slack_response=stored.slack_response,
                        entry=replace(eligible_entry, prior_expires_at=expiry),
                        next_action_at=DECIDED_TS,
                    )
                )
                self.assertIs(store.get_delivery(stored.candidate_id), stored)

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

    def replace_with_terminal(self, **overrides):
        key = {"PK": {"S": f"CANDIDATE#{self.key}"}, "SK": {"S": "DELIVERY"}}
        self.client.delete_item(TableName=TABLE, Key=key)
        original = terminal_record(**overrides)
        self.assertTrue(self.store.put_delivery_if_absent(original))
        return original

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

    def test_terminal_replay_roundtrips_typed_history_and_removes_terminal_fields(self):
        original = self.replace_with_terminal(network_attempt_count=5)
        replay = terminal_plan(original)

        result = apply_terminal_replay(self.store, replay)

        current = self.store.get_delivery(self.key)
        assert current is not None
        self.assertEqual(result.state_version, 8)
        self.assertEqual(current.status, "pending_queue")
        self.assertEqual(current.next_attempt_id, NEW_ATTEMPT)
        self.assertEqual(current.network_attempt_count, 5)
        self.assertEqual(current.request, original.request)
        self.assertEqual(current.terminal_replay_history, (replay.entry,))
        raw = self.client.get_item(
            TableName=TABLE,
            Key={"PK": {"S": f"CANDIDATE#{self.key}"}, "SK": {"S": "DELIVERY"}},
            ConsistentRead=True,
        )["Item"]
        encoded = raw["terminal_replay_history"]["L"][0]["M"]
        self.assertEqual(encoded["new_attempt_id"]["S"], NEW_ATTEMPT)
        self.assertEqual(encoded["prior_attempts_exhausted"]["BOOL"], False)
        self.assertEqual(encoded["prior_expires_at"]["N"], str(original.expires_at))
        self.assertNotIn("expires_at", raw)
        self.assertNotIn("queue_message_id", raw)
        self.assertNotIn("slack_response", raw)

    def test_terminal_replay_conditions_refuse_changed_expired_reserved_and_capped_records(self):
        original = self.replace_with_terminal()
        replay = terminal_plan(original)
        self.assertFalse(
            self.store.replay_terminal(
                self.key,
                expected_state_version=6,
                expected_prior_attempt_id=PRIOR_ATTEMPT,
                expected_expires_at=original.expires_at,
                expected_slack_response=original.slack_response,
                entry=replay.entry,
                next_action_at=DECIDED_TS,
            )
        )

        key = {"PK": {"S": f"CANDIDATE#{self.key}"}, "SK": {"S": "DELIVERY"}}
        corruptions = (
            ("state", "SET #status = :value", {":value": {"S": "posted"}}),
            ("prior", "SET last_attempt_id = :value", {":value": {"S": "other-attempt"}}),
            ("expiry", "SET expires_at = :value", {":value": {"N": str(original.expires_at + 1)}}),
            (
                "response",
                "SET slack_response = :value",
                {":value": {"S": json.dumps({"response_class": "credential_read_error"}, sort_keys=True)}},
            ),
            ("reservation", "SET next_attempt_id = :value", {":value": {"S": "2" * 32}}),
        )
        for name, expression, values in corruptions:
            with self.subTest(case=name):
                original = self.replace_with_terminal()
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
                    self.store.replay_terminal(
                        self.key,
                        expected_state_version=7,
                        expected_prior_attempt_id=PRIOR_ATTEMPT,
                        expected_expires_at=original.expires_at,
                        expected_slack_response=original.slack_response,
                        entry=terminal_plan(original).entry,
                        next_action_at=DECIDED_TS,
                    )
                )

        expired = self.replace_with_terminal(expires_at=DECIDED_TS)
        with self.assertRaisesRegex(ValueError, "has expired"):
            terminal_plan(expired)

        entry = terminal_plan(self.replace_with_terminal()).entry
        capped = terminal_record(
            candidate_id="terminal-history-cap",
            terminal_replay_history=tuple(
                replace(entry, reason=f"Approved replay {index}", new_attempt_id=f"{index:032x}")
                for index in range(MAX_TERMINAL_REPLAY_HISTORY)
            ),
        )
        self.assertTrue(self.store.put_delivery_if_absent(capped))
        self.assertFalse(
            self.store.replay_terminal(
                capped.candidate_id,
                expected_state_version=capped.state_version,
                expected_prior_attempt_id=PRIOR_ATTEMPT,
                expected_expires_at=capped.expires_at,
                expected_slack_response=capped.slack_response,
                entry=entry,
                next_action_at=DECIDED_TS,
            )
        )

    def test_terminal_replay_refuses_equal_or_expired_ttl_without_mutation(self):
        eligible_entry = terminal_plan(terminal_record()).entry
        for expiry in (DECIDED_TS, DECIDED_TS - 1):
            with self.subTest(expires_at=expiry):
                stored = self.replace_with_terminal(expires_at=expiry)

                self.assertFalse(
                    self.store.replay_terminal(
                        self.key,
                        expected_state_version=stored.state_version,
                        expected_prior_attempt_id=PRIOR_ATTEMPT,
                        expected_expires_at=expiry,
                        expected_slack_response=stored.slack_response,
                        entry=replace(eligible_entry, prior_expires_at=expiry),
                        next_action_at=DECIDED_TS,
                    )
                )
                self.assertEqual(self.store.get_delivery(self.key), stored)

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
        self.assertEqual(decoded.terminal_replay_history, ())


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
            terminal_replay=False,
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

    def terminal_arguments(self, *, apply=False, version=7, secret="SIGNINGSECRET"):
        arguments = self.arguments(apply=apply, version=version, secret=secret)
        arguments.terminal_replay = True
        return arguments

    def replace_with_terminal(self, **overrides):
        key = {"PK": {"S": f"CANDIDATE#{self.original.candidate_id}"}, "SK": {"S": "DELIVERY"}}
        self.client.delete_item(TableName=TABLE, Key=key)
        overrides.setdefault("expires_at", int(datetime.now(UTC).timestamp()) + 86400)
        self.original = terminal_record(**overrides)
        self.assertTrue(self.store.put_delivery_if_absent(self.original))

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

    def test_terminal_preview_and_apply_are_safe_and_reserve_one_attempt(self):
        self.replace_with_terminal(network_attempt_count=5)
        before = self.store.get_delivery(self.original.candidate_id)

        preview_code, preview_stdout, preview_stderr = self.invoke(self.terminal_arguments())

        self.assertEqual(preview_code, 0)
        self.assertEqual(preview_stderr, "")
        preview = json.loads(preview_stdout)
        self.assertEqual(preview["status"], "preview")
        self.assertEqual(preview["action"], "terminal_replay")
        self.assertEqual(preview["prior_response_class"], "http_403")
        self.assertNotIn("SIGNINGSECRET", preview_stdout)
        self.assertEqual(self.store.get_delivery(self.original.candidate_id), before)

        apply_code, apply_stdout, apply_stderr = self.invoke(self.terminal_arguments(apply=True))

        output = json.loads(apply_stdout)
        self.assertEqual(apply_code, 0)
        self.assertEqual(apply_stderr, "")
        self.assertEqual(output["status"], "applied")
        self.assertEqual(output["new_state"], "pending_queue")
        self.assertEqual(output["TerminalReplay"], 1)
        self.assertNotIn("SIGNINGSECRET", apply_stdout)
        current = self.store.get_delivery(self.original.candidate_id)
        assert current is not None
        self.assertEqual(current.status, "pending_queue")
        self.assertEqual(current.network_attempt_count, 5)
        self.assertIsNotNone(current.next_attempt_id)
        self.assertEqual(len(current.terminal_replay_history), 1)

    def test_replay_modes_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit):
            replay_delivery.parse_args(
                [
                    "--table-name",
                    TABLE,
                    "--candidate-id",
                    self.original.candidate_id,
                    "--expected-state-version",
                    "7",
                    "--operator",
                    "operator",
                    "--reason",
                    "reason",
                    "--evidence",
                    "evidence",
                    "--found-post",
                    "--terminal-replay",
                ]
            )

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

    def test_each_mode_marks_the_write_phase_only_at_its_apply_helper(self):
        class PlanningFailure(EndpointConnectionError):
            pass

        modes = (
            ("unknown", self.arguments(apply=True), "plan_unknown_replay"),
            ("found", self.found_post_arguments(apply=True), "plan_found_post"),
            ("terminal", self.terminal_arguments(apply=True), "plan_terminal_replay"),
        )
        for name, arguments, planner in modes:
            with (
                self.subTest(mode=name),
                patch.object(
                    replay_delivery,
                    planner,
                    side_effect=PlanningFailure(endpoint_url="https://dynamodb.invalid"),
                ),
            ):
                if name == "terminal":
                    self.replace_with_terminal()
                elif self.original.status != "delivery_unknown":
                    key = {
                        "PK": {"S": f"CANDIDATE#{self.original.candidate_id}"},
                        "SK": {"S": "DELIVERY"},
                    }
                    self.client.delete_item(TableName=TABLE, Key=key)
                    self.original = unknown_record()
                    self.assertTrue(self.store.put_delivery_if_absent(self.original))
                code, _, error = self.invoke(arguments)
                self.assertEqual(code, replay_delivery.EXIT_AMBIGUOUS)
                self.assertEqual(json.loads(error)["status"], "read_failed")

    def test_each_mode_can_prove_an_applied_write_after_the_client_loses_its_response(self):
        class AppliedThenFailedClient:
            def __init__(self, delegate):
                self.delegate = delegate

            def get_item(self, **kwargs):
                return self.delegate.get_item(**kwargs)

            def update_item(self, **kwargs):
                self.delegate.update_item(**kwargs)
                raise EndpointConnectionError(endpoint_url="https://dynamodb.invalid")

        modes = ("unknown", "found", "terminal")
        for name in modes:
            with self.subTest(mode=name):
                key = {
                    "PK": {"S": f"CANDIDATE#{self.original.candidate_id}"},
                    "SK": {"S": "DELIVERY"},
                }
                self.client.delete_item(TableName=TABLE, Key=key)
                self.original = (
                    terminal_record(expires_at=int(datetime.now(UTC).timestamp()) + 86400)
                    if name == "terminal"
                    else unknown_record()
                )
                self.assertTrue(self.store.put_delivery_if_absent(self.original))
                arguments = {
                    "unknown": self.arguments(apply=True),
                    "found": self.found_post_arguments(apply=True),
                    "terminal": self.terminal_arguments(apply=True),
                }[name]

                code, stdout, stderr = self.invoke(arguments, client=AppliedThenFailedClient(self.client))

                self.assertEqual(code, 0)
                self.assertEqual(stderr, "")
                output = json.loads(stdout)
                self.assertEqual(output["status"], "applied_after_reread")
                self.assertEqual(
                    output["action"],
                    {"unknown": "manual_replay", "found": "found_post_reconciliation", "terminal": "terminal_replay"}[
                        name
                    ],
                )

    def test_terminal_write_failure_without_reread_proof_remains_ambiguous(self):
        class AmbiguousClient:
            def __init__(self, delegate):
                self.delegate = delegate
                self.update_calls = 0

            def get_item(self, **kwargs):
                return self.delegate.get_item(**kwargs)

            def update_item(self, **kwargs):
                self.update_calls += 1
                raise EndpointConnectionError(endpoint_url="https://dynamodb.invalid")

        self.replace_with_terminal()
        client = AmbiguousClient(self.client)

        code, stdout, stderr = self.invoke(self.terminal_arguments(apply=True), client=client)

        self.assertEqual(code, replay_delivery.EXIT_AMBIGUOUS)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["status"], "ambiguous")
        self.assertEqual(client.update_calls, 1)
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
