from __future__ import annotations

import hashlib
import unittest
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from moto import mock_aws

from aws_public_change_feed.source_retirement import (
    RetirementContext,
    SourceRetirementError,
    apply_plan,
    canonical_json,
    create_plan,
    sha256_bytes,
)
from aws_public_change_feed.source_store import DynamoDBSourceStateStore
from aws_public_change_feed.state import FeedCheckpoint

REGION = "us-east-1"
TABLE = "source-state"
FEED = "removed-feed"
URL = "https://example.com/feed.xml"
AS_OF = datetime(2026, 8, 30, 16, 0, tzinfo=UTC)
SERIALIZER = TypeSerializer()
DESERIALIZER = TypeDeserializer()


def wire(document):
    return {key: SERIALIZER.serialize(value) for key, value in document.items() if value is not None}


def plain(item):
    def normalize(value):
        if hasattr(value, "to_integral_value") and value == value.to_integral_value():
            return int(value)
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if isinstance(value, dict):
            return {key: normalize(item) for key, item in value.items()}
        return value

    return {key: normalize(DESERIALIZER.deserialize(value)) for key, value in item.items()}


def create_table(client):
    client.create_table(
        TableName=TABLE,
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


def feed_item(checkpoint: FeedCheckpoint | None = None):
    checkpoint = checkpoint or FeedCheckpoint(
        feed_name=FEED,
        feed_url=URL,
        etag='"before"',
        first_attempt_at="2026-08-01T00:00:00+00:00",
        last_attempt_at="2026-08-29T00:00:00+00:00",
        last_success_at="2026-08-29T00:00:01+00:00",
        state_version=9,
    )
    return {"PK": f"FEED#{FEED}", "SK": "STATE", "item_type": "feed", **asdict(checkpoint)}


def context(*, configured=None, release_id="a" * 64):
    return RetirementContext(
        account_id="123456789012",
        region=REGION,
        table_name=TABLE,
        role_arn="arn:aws:iam::123456789012:role/apcf-dev-source-state-retirement",
        role_session_arn=("arn:aws:sts::123456789012:assumed-role/apcf-dev-source-state-retirement/session"),
        bucket="config-bucket",
        pointer_key="apcf/active-versions.json",
        pointer_version_id="pointer-version",
        pointer_etag='"pointer-etag"',
        release_id=release_id,
        application_version="sha256:" + "b" * 64,
        config_reference={"key": "releases/config.yaml", "version_id": "config-version", "sha256": "c" * 64},
        feed_state_ttl_days=730,
        configured_feeds={} if configured is None else configured,
        deployment_sha256="d" * 64,
        terraform_output_sha256="e" * 64,
    )


def plan_sha(plan):
    return sha256_bytes(canonical_json(plan) + b"\n")


class LostResponseClient:
    def __init__(self, client):
        self.client = client

    def get_item(self, **kwargs):
        return self.client.get_item(**kwargs)

    def update_item(self, **kwargs):
        self.client.update_item(**kwargs)
        raise RuntimeError("lost response")


class RaceClient:
    def __init__(self, client, mutate):
        self.client = client
        self.mutate = mutate

    def get_item(self, **kwargs):
        return self.client.get_item(**kwargs)

    def update_item(self, **kwargs):
        self.mutate()
        return self.client.update_item(**kwargs)


class UnreadableAfterWriteClient:
    def __init__(self, client):
        self.client = client
        self.wrote = False

    def get_item(self, **kwargs):
        if self.wrote:
            raise RuntimeError("read failed")
        return self.client.get_item(**kwargs)

    def update_item(self, **kwargs):
        self.client.update_item(**kwargs)
        self.wrote = True
        raise RuntimeError("lost response")


class SourceRetirementTests(unittest.TestCase):
    def setUp(self):
        self.aws = mock_aws()
        self.aws.start()
        self.client = boto3.client("dynamodb", region_name=REGION)
        create_table(self.client)
        self.put(feed_item())

    def tearDown(self):
        self.aws.stop()

    def put(self, document):
        self.client.put_item(TableName=TABLE, Item=wire(document))

    def read(self):
        response = self.client.get_item(
            TableName=TABLE,
            Key=wire({"PK": f"FEED#{FEED}", "SK": "STATE"}),
            ConsistentRead=True,
        )
        return plain(response["Item"])

    def retirement_plan(self, client=None):
        return create_plan(
            client or self.client,
            context=context(),
            action="retire",
            feed_name=FEED,
            decision_id="issue-159",
            decision_at=AS_OF,
            now=AS_OF,
        )

    def apply(self, plan, *, client=None, selected_context=None):
        observed_at = datetime.fromisoformat(plan["decision_at"].replace("Z", "+00:00"))
        return apply_plan(
            client or self.client,
            context=selected_context or context(),
            plan=plan,
            plan_sha256=plan_sha(plan),
            now=observed_at,
        )

    def test_retirement_preview_is_read_only_and_binds_the_exact_checkpoint(self):
        before = self.read()

        plan = self.retirement_plan()

        self.assertEqual(self.read(), before)
        self.assertTrue(plan["active_release_feed_absent"])
        self.assertEqual(plan["checkpoint"], before)
        self.assertEqual(plan["checkpoint_content_sha256"], sha256_bytes(canonical_json(before)))
        self.assertEqual(plan["feed_url_sha256"], hashlib.sha256(URL.encode()).hexdigest())
        self.assertEqual(plan["target"]["state_version"], 10)
        self.assertEqual(plan["target"]["retire_after"], int((AS_OF + timedelta(days=730)).timestamp()))

    def test_future_decision_time_is_refused_and_the_observed_time_succeeds(self):
        with self.assertRaisesRegex(SourceRetirementError, "cannot be in the future"):
            create_plan(
                self.client,
                context=context(),
                action="retire",
                feed_name=FEED,
                decision_id="issue-159",
                decision_at=AS_OF + timedelta(seconds=1),
                now=AS_OF,
            )

        self.assertEqual(self.retirement_plan()["action"], "retire")

    def test_configured_feed_refusal_is_inverted_by_removing_it(self):
        with self.assertRaisesRegex(SourceRetirementError, "remains present"):
            create_plan(
                self.client,
                context=context(configured={FEED: URL}),
                action="retire",
                feed_name=FEED,
                decision_id="issue-159",
                decision_at=AS_OF,
                now=AS_OF,
            )

        self.assertEqual(self.retirement_plan()["action"], "retire")

    def test_live_lease_refusal_is_inverted_by_clearing_the_lease(self):
        leased = FeedCheckpoint(
            feed_name=FEED,
            feed_url=URL,
            state_version=9,
            lease_owner="watcher",
            lease_expires_at=int((AS_OF + timedelta(minutes=1)).timestamp()),
        )
        self.put(feed_item(leased))
        with self.assertRaisesRegex(SourceRetirementError, "lease or pending"):
            self.retirement_plan()

        self.put(feed_item())
        self.assertEqual(self.retirement_plan()["action"], "retire")

    def test_pending_page_work_refusal_is_inverted_by_clearing_pending_state(self):
        pending = FeedCheckpoint(
            feed_name=FEED,
            feed_url=URL,
            state_version=9,
            lease_owner="watcher",
            lease_expires_at=int((AS_OF + timedelta(minutes=1)).timestamp()),
            pending_run_id="f" * 64,
        )
        self.put(feed_item(pending))
        with self.assertRaisesRegex(SourceRetirementError, "lease or pending"):
            self.retirement_plan()

        self.put(feed_item())
        self.assertEqual(self.retirement_plan()["action"], "retire")

    def test_apply_records_the_reviewed_retirement_boundary_conditionally(self):
        plan = self.retirement_plan()

        result = self.apply(plan)

        durable = self.read()
        self.assertEqual(result["status"], "applied")
        self.assertEqual(durable["retired_at"], "2026-08-30T16:00:00Z")
        self.assertEqual(durable["retire_after"], int((AS_OF + timedelta(days=730)).timestamp()))
        self.assertEqual(durable["retirement_decision_id"], "issue-159")
        self.assertEqual(durable["state_version"], 10)

    def test_release_change_refuses_apply_and_the_original_context_succeeds(self):
        plan = self.retirement_plan()
        with self.assertRaisesRegex(SourceRetirementError, "context differs"):
            self.apply(plan, selected_context=context(release_id="9" * 64))

        self.assertEqual(self.apply(plan)["status"], "applied")

    def test_state_version_url_and_content_changes_each_make_the_plan_stale(self):
        mutations = (
            {"state_version": 10},
            {"feed_url": "https://example.com/changed.xml"},
            {"etag": '"changed"'},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.put(feed_item())
                plan = self.retirement_plan()
                changed = {**self.read(), **mutation}
                self.put(changed)
                with self.assertRaisesRegex(SourceRetirementError, "differs from the preview"):
                    self.apply(plan)

                self.put(feed_item())
                fresh = self.retirement_plan()
                self.assertEqual(self.apply(fresh)["status"], "applied")

    def test_changed_plan_proof_is_refused_and_exact_plan_succeeds(self):
        plan = self.retirement_plan()
        changed = {**plan, "feed_url_sha256": "0" * 64}
        with self.assertRaisesRegex(SourceRetirementError, "classification differs"):
            self.apply(changed)

        self.assertEqual(self.apply(plan)["status"], "applied")

    def test_conditional_race_changes_content_and_writes_nothing(self):
        plan = self.retirement_plan()

        def mutate():
            self.put({**self.read(), "etag": '"raced"', "state_version": 10})

        result = self.apply(plan, client=RaceClient(self.client, mutate))

        self.assertEqual(result["status"], "conflict")
        self.assertNotIn("retired_at", self.read())

    def test_lost_write_response_is_proved_by_one_strong_reread(self):
        plan = self.retirement_plan()

        result = self.apply(plan, client=LostResponseClient(self.client))

        self.assertEqual(result["status"], "applied_after_reread")
        self.assertEqual(self.read()["state_version"], 10)

    def test_unreadable_post_write_state_is_ambiguous(self):
        plan = self.retirement_plan()

        result = self.apply(plan, client=UnreadableAfterWriteClient(self.client))

        self.assertEqual(result["status"], "ambiguous")

    def retire(self):
        plan = self.retirement_plan()
        self.assertEqual(self.apply(plan)["status"], "applied")
        return plan

    def test_compaction_refuses_one_second_early_and_succeeds_at_the_boundary(self):
        retirement = self.retire()
        boundary = datetime.fromtimestamp(retirement["target"]["retire_after"], tz=UTC)
        with self.assertRaisesRegex(SourceRetirementError, "has not elapsed"):
            create_plan(
                self.client,
                context=context(),
                action="compact",
                feed_name=FEED,
                decision_id="issue-159-compaction",
                decision_at=boundary - timedelta(seconds=1),
                now=boundary - timedelta(seconds=1),
            )

        plan = create_plan(
            self.client,
            context=context(),
            action="compact",
            feed_name=FEED,
            decision_id="issue-159-compaction",
            decision_at=boundary,
            now=boundary,
        )
        result = self.apply(plan)

        durable = self.read()
        self.assertEqual(result["status"], "applied")
        self.assertEqual(durable["item_type"], "feed_tombstone")
        self.assertEqual(durable["feed_url_sha256"], hashlib.sha256(URL.encode()).hexdigest())
        self.assertNotIn("feed_url", durable)
        self.assertNotIn("etag", durable)

    def compact(self):
        retirement = self.retire()
        boundary = datetime.fromtimestamp(retirement["target"]["retire_after"], tz=UTC)
        plan = create_plan(
            self.client,
            context=context(),
            action="compact",
            feed_name=FEED,
            decision_id="issue-159-compaction",
            decision_at=boundary,
            now=boundary,
        )
        self.assertEqual(self.apply(plan)["status"], "applied")

    def test_tombstone_blocks_watcher_claim_until_reviewed_same_url_restoration(self):
        self.compact()
        store = DynamoDBSourceStateStore(self.client, TABLE)

        with self.assertRaisesRegex(ValueError, "wrong item_type"):
            store.claim(
                FEED,
                URL,
                owner="watcher",
                attempted_at=AS_OF,
                lease_expires_at=int((AS_OF + timedelta(minutes=6)).timestamp()),
                now=int(AS_OF.timestamp()),
            )

        with self.assertRaisesRegex(SourceRetirementError, "different URL"):
            create_plan(
                self.client,
                context=context(configured={FEED: "https://example.com/replacement.xml"}),
                action="restore",
                feed_name=FEED,
                decision_id="issue-159-restoration",
                decision_at=AS_OF + timedelta(days=731),
                now=AS_OF + timedelta(days=731),
            )

        restore_context = context(configured={FEED: URL})
        plan = create_plan(
            self.client,
            context=restore_context,
            action="restore",
            feed_name=FEED,
            decision_id="issue-159-restoration",
            decision_at=AS_OF + timedelta(days=731),
            now=AS_OF + timedelta(days=731),
        )
        result = self.apply(plan, selected_context=restore_context)
        restored = store.load_feed(FEED)
        claimed = store.claim(
            FEED,
            URL,
            owner="watcher",
            attempted_at=AS_OF + timedelta(days=731, seconds=1),
            lease_expires_at=int((AS_OF + timedelta(days=731, minutes=6)).timestamp()),
            now=int((AS_OF + timedelta(days=731)).timestamp()),
        )

        self.assertEqual(result["status"], "applied")
        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertEqual(restored.consecutive_failures, 0)
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertEqual(claimed.restoration_decision_id, "issue-159-restoration")
        self.assertEqual(claimed.prior_retirement_decision_id, "issue-159")


if __name__ == "__main__":
    unittest.main()
