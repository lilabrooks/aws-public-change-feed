"""DynamoDB source-state and S3 snapshot adapters against moto."""

import hashlib
import sys
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3
from moto import mock_aws

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aws_public_change_feed.announcements import NormalizedAnnouncement, Provenance  # noqa: E402
from aws_public_change_feed.source_store import (  # noqa: E402
    DynamoDBAnnouncementStateStore,
    DynamoDBFeedStateStore,
    DynamoDBSourceStateStore,
    S3SnapshotStore,
)
from aws_public_change_feed.state import (  # noqa: E402
    AnnouncementStateStore,
    FeedCompletion,
    InMemoryFeedStateStore,
    Observation,
    ResponsePageMarker,
    record_emission,
)
from aws_public_change_feed.state import observe as merge_observation  # noqa: E402

REGION = "us-east-1"
TABLE = "source-state"
BUCKET = "snapshot-bucket"
NOW = datetime(2026, 8, 10, 15, 0, tzinfo=UTC)
FEED_URL = "https://aws.amazon.com/about-aws/whats-new/recent/feed/"
RETENTION_DAYS = 730


def observe(store: AnnouncementStateStore, announcement: NormalizedAnnouncement) -> Observation:
    return merge_observation(store, announcement, retention_days=RETENTION_DAYS)


def create_table(client):
    client.create_table(
        TableName=TABLE,
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
    )


class DynamoDBFeedStateTests(unittest.TestCase):
    def setUp(self):
        self.aws = mock_aws()
        self.aws.start()
        self.client = boto3.client("dynamodb", region_name=REGION)
        create_table(self.client)
        self.store = DynamoDBFeedStateStore(self.client, TABLE)

    def tearDown(self):
        self.aws.stop()

    def claim(self, *, owner="invocation-a", now=100, lease=460):
        return self.store.claim(
            "aws-whats-new",
            FEED_URL,
            owner=owner,
            attempted_at=NOW,
            lease_expires_at=lease,
            now=now,
        )

    def test_claim_uses_the_exact_feed_key_and_sets_first_attempt_once(self):
        claimed = self.claim()
        assert claimed is not None

        raw = self.client.get_item(
            TableName=TABLE,
            Key={"PK": {"S": "FEED#aws-whats-new"}, "SK": {"S": "STATE"}},
            ConsistentRead=True,
        )["Item"]
        self.assertEqual(raw["item_type"], {"S": "feed"})
        self.assertEqual(claimed.first_attempt_at, NOW.isoformat())
        self.assertEqual(claimed.state_version, 1)

        takeover = self.claim(owner="invocation-b", now=461, lease=821)
        assert takeover is not None
        self.assertEqual(takeover.first_attempt_at, NOW.isoformat())
        self.assertEqual(takeover.state_version, 2)

    def test_a_live_lease_refuses_a_competing_claim(self):
        self.assertIsNotNone(self.claim())
        self.assertIsNone(self.claim(owner="invocation-b", now=459, lease=819))
        self.assertEqual(self.store.load("aws-whats-new").lease_owner, "invocation-a")  # type: ignore[union-attr]

    def test_a_released_lease_is_claimable_on_the_next_schedule(self):
        claimed = self.claim()
        assert claimed is not None
        self.assertTrue(
            self.store.complete(
                claimed.feed_name,
                owner="invocation-a",
                expected_state_version=claimed.state_version,
                succeeded_at=NOW,
            )
        )
        next_claim = self.claim(owner="invocation-b", now=101, lease=461)
        assert next_claim is not None
        self.assertEqual(next_claim.lease_owner, "invocation-b")
        self.assertEqual(next_claim.state_version, 3)

    def test_a_failed_feed_is_claimable_on_the_next_schedule(self):
        claimed = self.claim()
        assert claimed is not None
        self.assertTrue(
            self.store.fail(
                claimed.feed_name,
                owner="invocation-a",
                expected_state_version=claimed.state_version,
                error_class="dns",
            )
        )
        next_claim = self.claim(owner="invocation-b", now=101, lease=461)
        assert next_claim is not None
        self.assertEqual(next_claim.lease_owner, "invocation-b")
        self.assertEqual(next_claim.consecutive_failures, 1)

    def test_fetched_values_stay_pending_until_the_exact_owner_commits(self):
        claimed = self.claim()
        assert claimed is not None
        run_id = "a" * 64
        self.assertIsNone(
            self.store.mark_fetched(
                claimed.feed_name,
                owner="wrong-owner",
                expected_state_version=claimed.state_version,
                etag='"new"',
                last_modified="Sun, 10 Aug 2026 15:00:00 GMT",
                newest_publication_at=NOW,
                run_id=run_id,
            )
        )
        pending = self.store.mark_fetched(
            claimed.feed_name,
            owner="invocation-a",
            expected_state_version=claimed.state_version,
            etag='"new"',
            last_modified="Sun, 10 Aug 2026 15:00:00 GMT",
            newest_publication_at=NOW,
            run_id=run_id,
        )
        assert pending is not None
        self.assertIsNone(pending.etag)
        self.assertEqual(pending.pending_etag, '"new"')
        self.assertFalse(
            self.store.complete(
                claimed.feed_name,
                owner="invocation-a",
                expected_state_version=claimed.state_version,
                succeeded_at=NOW,
            )
        )
        self.assertTrue(
            self.store.complete(
                claimed.feed_name,
                owner="invocation-a",
                expected_state_version=pending.state_version,
                succeeded_at=NOW,
            )
        )
        complete = self.store.load(claimed.feed_name)
        assert complete is not None
        self.assertEqual(complete.etag, '"new"')
        self.assertIsNone(complete.pending_run_id)
        self.assertIsNone(complete.lease_owner)

    def test_failure_preserves_validators_and_clears_the_lease(self):
        claimed = self.claim()
        assert claimed is not None
        self.assertTrue(
            self.store.fail(
                claimed.feed_name,
                owner="invocation-a",
                expected_state_version=claimed.state_version,
                error_class="parser_failed",
            )
        )
        failed = self.store.load(claimed.feed_name)
        assert failed is not None
        self.assertIsNone(failed.etag)
        self.assertEqual(failed.consecutive_failures, 1)
        self.assertEqual(failed.last_error_class, "parser_failed")
        self.assertIsNone(failed.lease_owner)

    def test_unknown_durable_fields_are_refused_on_read(self):
        claimed = self.claim()
        assert claimed is not None
        self.client.update_item(
            TableName=TABLE,
            Key={"PK": {"S": "FEED#aws-whats-new"}, "SK": {"S": "STATE"}},
            UpdateExpression="SET invented = :value",
            ExpressionAttributeValues={":value": {"S": "corrupt"}},
        )
        with self.assertRaisesRegex(ValueError, "unknown fields: invented"):
            self.store.load("aws-whats-new")

    def test_a_transaction_condition_loss_advances_no_fetched_feed(self):
        first = self.claim(owner="invocation-a")
        assert first is not None
        second = self.store.claim(
            "aws-news-blog",
            "https://aws.amazon.com/blogs/aws/feed/",
            owner="invocation-a",
            attempted_at=NOW,
            lease_expires_at=460,
            now=100,
        )
        assert second is not None
        first_pending = self.store.mark_fetched(
            first.feed_name,
            owner="invocation-a",
            expected_state_version=first.state_version,
            etag='"first"',
            last_modified=None,
            newest_publication_at=None,
            run_id="a" * 64,
        )
        second_pending = self.store.mark_fetched(
            second.feed_name,
            owner="invocation-a",
            expected_state_version=second.state_version,
            etag='"second"',
            last_modified=None,
            newest_publication_at=None,
            run_id="b" * 64,
        )
        assert first_pending is not None and second_pending is not None

        class RacingClient:
            def __init__(self, client):
                self.client = client

            def __getattr__(self, name):
                return getattr(self.client, name)

            def transact_write_items(self, **kwargs):
                self.client.update_item(
                    TableName=TABLE,
                    Key={"PK": {"S": "FEED#aws-news-blog"}, "SK": {"S": "STATE"}},
                    UpdateExpression="SET state_version = state_version + :one",
                    ExpressionAttributeValues={":one": {"N": "1"}},
                )
                return self.client.transact_write_items(**kwargs)

        racing = DynamoDBFeedStateStore(RacingClient(self.client), TABLE)
        self.assertFalse(
            racing.complete_many(
                (
                    FeedCompletion(first.feed_name, "invocation-a", first_pending.state_version, NOW),
                    FeedCompletion(second.feed_name, "invocation-a", second_pending.state_version, NOW),
                )
            )
        )
        self.assertIsNone(self.store.load(first.feed_name).etag)  # type: ignore[union-attr]
        self.assertIsNone(self.store.load(second.feed_name).etag)  # type: ignore[union-attr]

    def test_batch_completion_refuses_a_claim_that_was_never_marked_fetched(self):
        claimed = self.claim()
        assert claimed is not None
        self.assertFalse(
            self.store.complete_many((FeedCompletion(claimed.feed_name, "invocation-a", claimed.state_version, NOW),))
        )
        durable = self.store.load(claimed.feed_name)
        assert durable is not None
        self.assertIsNone(durable.last_success_at)
        self.assertEqual(durable.lease_owner, "invocation-a")


class FeedStateClaimParityTests(unittest.TestCase):
    def setUp(self):
        self.aws = mock_aws()
        self.aws.start()
        self.client = boto3.client("dynamodb", region_name=REGION)
        create_table(self.client)
        self.stores = (
            ("memory", InMemoryFeedStateStore()),
            ("dynamodb", DynamoDBFeedStateStore(self.client, TABLE)),
        )

    def tearDown(self):
        self.aws.stop()

    @staticmethod
    def claim(store, url=FEED_URL, *, owner="invocation-a", now=100, lease=460):
        return store.claim(
            "aws-whats-new",
            url,
            owner=owner,
            attempted_at=NOW,
            lease_expires_at=lease,
            now=now,
        )

    def test_changed_feed_url_is_an_invariant_failure_during_a_live_lease(self):
        changed_url = "https://aws.amazon.com/another-feed/"
        for label, store in self.stores:
            with self.subTest(store=label):
                self.assertIsNotNone(self.claim(store))
                with self.assertRaisesRegex(ValueError, "stored feed_url does not match"):
                    self.claim(store, changed_url, owner="invocation-b")

    def test_changed_feed_url_is_an_invariant_failure_after_release(self):
        changed_url = "https://aws.amazon.com/another-feed/"
        for label, store in self.stores:
            with self.subTest(store=label):
                claimed = self.claim(store)
                assert claimed is not None
                self.assertTrue(
                    store.complete(
                        claimed.feed_name,
                        owner="invocation-a",
                        expected_state_version=claimed.state_version,
                        succeeded_at=NOW,
                    )
                )
                with self.assertRaisesRegex(ValueError, "stored feed_url does not match"):
                    self.claim(store, changed_url, owner="invocation-b", now=101, lease=461)

    def test_same_url_contention_and_released_reclaim_stay_valid(self):
        for label, store in self.stores:
            with self.subTest(store=label):
                claimed = self.claim(store)
                assert claimed is not None
                self.assertIsNone(self.claim(store, owner="invocation-b"))
                self.assertTrue(
                    store.complete(
                        claimed.feed_name,
                        owner="invocation-a",
                        expected_state_version=claimed.state_version,
                        succeeded_at=NOW,
                    )
                )
                reclaimed = self.claim(store, owner="invocation-b", now=101, lease=461)
                assert reclaimed is not None
                self.assertEqual(reclaimed.lease_owner, "invocation-b")


class DynamoDBAnnouncementStateTests(unittest.TestCase):
    def setUp(self):
        self.aws = mock_aws()
        self.aws.start()
        self.client = boto3.client("dynamodb", region_name=REGION)
        create_table(self.client)
        self.store = DynamoDBAnnouncementStateStore(self.client, TABLE)

    def tearDown(self):
        self.aws.stop()

    def announcement(self, title="AWS first title", observed_at=NOW, feed_name="feed-a"):
        return NormalizedAnnouncement(
            canonical_url="https://aws.amazon.com/example/",
            title=title,
            summary="A bounded summary.",
            observed_at=observed_at,
            published_at=NOW - timedelta(hours=1),
            provenance=(Provenance(feed_name=feed_name, item_url="https://aws.amazon.com/example/"),),
        )

    def test_observation_round_trips_the_exact_announcement_key(self):
        result = observe(self.store, self.announcement())
        raw = self.client.get_item(
            TableName=TABLE,
            Key={
                "PK": {"S": f"ANNOUNCEMENT#{result.record.announcement_id}"},
                "SK": {"S": "STATE"},
            },
            ConsistentRead=True,
        )["Item"]
        self.assertEqual(raw["item_type"], {"S": "announcement"})
        self.assertEqual(
            raw["expires_at"],
            {"N": str(int((NOW + timedelta(days=RETENTION_DAYS)).timestamp()))},
        )
        self.assertEqual(self.store.load(result.record.announcement_id), result.record)

    def test_explicit_item_type_disambiguates_a_store_valid_hexadecimal_feed_name(self):
        """The shared store must not infer kind from an identifier's text.

        The configuration contract currently caps feed IDs at 63 characters,
        while the feed-checkpoint store accepts this 64-character key. This is
        therefore a store-boundary collision rather than a release-produced
        fixture. Keeping the explicit kind at the exported store API also
        protects direct callers and any future configuration-bound expansion.
        """

        result = observe(self.store, self.announcement())
        self.assertEqual(len(result.record.announcement_id), 64)
        feed_store = DynamoDBFeedStateStore(self.client, TABLE)
        claimed = feed_store.claim(
            result.record.announcement_id,
            FEED_URL,
            owner="invocation-a",
            attempted_at=NOW,
            lease_expires_at=460,
            now=100,
        )
        assert claimed is not None
        source = DynamoDBSourceStateStore(self.client, TABLE)

        self.assertEqual(source.load(result.record.announcement_id, item_type="feed"), claimed)
        self.assertEqual(source.load(result.record.announcement_id, item_type="announcement"), result.record)
        with self.assertRaisesRegex(ValueError, "item_type must be feed or announcement"):
            source.load(result.record.announcement_id, item_type="response_page")  # type: ignore[arg-type]

    def test_legacy_announcement_without_expiry_remains_readable(self):
        record = replace(observe(self.store, self.announcement()).record, expires_at=None)
        self.store.save(record)
        raw = self.client.get_item(
            TableName=TABLE,
            Key={
                "PK": {"S": f"ANNOUNCEMENT#{record.announcement_id}"},
                "SK": {"S": "STATE"},
            },
            ConsistentRead=True,
        )["Item"]
        self.assertNotIn("expires_at", raw)
        self.assertEqual(self.store.load(record.announcement_id), record)

    def test_forged_durable_announcement_identity_is_refused_on_read(self):
        record = observe(self.store, self.announcement()).record
        self.client.update_item(
            TableName=TABLE,
            Key={
                "PK": {"S": f"ANNOUNCEMENT#{record.announcement_id}"},
                "SK": {"S": "STATE"},
            },
            UpdateExpression="SET title = :title",
            ExpressionAttributeValues={":title": {"S": "forged title"}},
        )
        with self.assertRaisesRegex(ValueError, "content_fingerprint does not derive"):
            self.store.load(record.announcement_id)

    def test_a_stale_compare_and_swap_cannot_drop_a_new_revision(self):
        first = observe(self.store, self.announcement()).record
        newer = observe(self.store, self.announcement(title="AWS newer title", observed_at=NOW + timedelta(minutes=2)))
        stale = replace(first, state_version=first.state_version + 1)
        self.assertFalse(self.store.put(stale, expected_state_version=first.state_version))
        self.assertEqual(self.store.load(first.announcement_id), newer.record)

    def test_out_of_order_content_extends_history_without_rewinding_current(self):
        first = observe(self.store, self.announcement())
        newer = observe(
            self.store,
            self.announcement(title="AWS newest title", observed_at=NOW + timedelta(minutes=5), feed_name="feed-b"),
        )
        replay = observe(
            self.store,
            self.announcement(title="AWS historical title", observed_at=NOW - timedelta(minutes=5), feed_name="feed-c"),
        )
        self.assertEqual(replay.record.title, newer.record.title)
        self.assertEqual(len(replay.record.revision_ids), 3)
        self.assertEqual([entry.feed_name for entry in replay.record.provenance], ["feed-a", "feed-b", "feed-c"])
        self.assertEqual(replay.record.first_observed_at, (NOW - timedelta(minutes=5)).isoformat())
        self.assertEqual(first.record.announcement_id, replay.record.announcement_id)

    def test_emission_references_merge_conditionally(self):
        record = observe(self.store, self.announcement()).record
        updated = record_emission(self.store, record.announcement_id, ["b" * 64, "a" * 64], "c" * 64)
        self.assertEqual(updated.emitted_candidate_ids, ("a" * 64, "b" * 64))
        self.assertEqual(updated.release_ids, ("c" * 64,))

    def test_response_pages_are_immutable_and_exactly_keyed(self):
        marker = ResponsePageMarker(
            run_id="d" * 64,
            page_set_id="f" * 64,
            feed_name="feed-a",
            page=0,
            candidate_ids=("e" * 64,),
        )
        self.assertTrue(self.store.put_page(marker))
        self.assertTrue(self.store.put_page(marker))
        self.assertFalse(self.store.put_page(replace(marker, candidate_ids=("a" * 64,))))
        self.assertEqual(self.store.load_page(marker.run_id, marker.page_set_id, 0), marker)
        raw = self.client.get_item(
            TableName=TABLE,
            Key={
                "PK": {"S": f"RUN#{marker.run_id}"},
                "SK": {"S": f"PAGESET#{marker.page_set_id}#PAGE#000000"},
            },
            ConsistentRead=True,
        )["Item"]
        self.assertEqual(raw["item_type"], {"S": "response_page"})
        self.assertNotIn("expires_at", raw)

    def test_response_page_expiry_round_trips_as_non_proof_metadata(self):
        marker = ResponsePageMarker(
            run_id="d" * 64,
            page_set_id="f" * 64,
            feed_name="feed-a",
            page=0,
            candidate_ids=("e" * 64,),
            expires_at=2_000_000_000,
        )
        self.assertTrue(self.store.put_page(marker))
        loaded = self.store.load_page(marker.run_id, marker.page_set_id, marker.page)
        assert loaded is not None
        self.assertEqual(loaded.expires_at, 2_000_000_000)
        self.assertEqual(loaded, replace(marker, expires_at=2_100_000_000))

    def test_exact_response_page_reobservation_only_extends_expiry(self):
        marker = ResponsePageMarker(
            run_id="d" * 64,
            page_set_id="f" * 64,
            feed_name="feed-a",
            page=0,
            candidate_ids=("e" * 64,),
            expires_at=2_000_000_000,
        )
        self.assertTrue(self.store.put_page(marker))

        self.assertTrue(self.store.put_page(replace(marker, expires_at=2_100_000_000)))
        self.assertTrue(self.store.put_page(replace(marker, expires_at=1_900_000_000)))

        durable = self.store.load_page(marker.run_id, marker.page_set_id, marker.page)
        assert durable is not None
        self.assertEqual(durable.expires_at, 2_100_000_000)
        self.assertEqual(durable.candidate_ids, marker.candidate_ids)

    def test_expired_but_present_exact_page_is_refreshed(self):
        expired = ResponsePageMarker(
            run_id="d" * 64,
            page_set_id="f" * 64,
            feed_name="feed-a",
            page=0,
            candidate_ids=("e" * 64,),
            expires_at=int(NOW.timestamp()) - 1,
        )
        self.assertTrue(self.store.put_page(expired))

        refreshed_expiry = int((NOW + timedelta(days=RETENTION_DAYS)).timestamp())
        self.assertTrue(self.store.put_page(replace(expired, expires_at=refreshed_expiry)))

        durable = self.store.load_page(expired.run_id, expired.page_set_id, expired.page)
        assert durable is not None
        self.assertEqual(durable.expires_at, refreshed_expiry)

    def test_conflicting_page_proof_cannot_change_expiry(self):
        marker = ResponsePageMarker(
            run_id="d" * 64,
            page_set_id="f" * 64,
            feed_name="feed-a",
            page=0,
            candidate_ids=("e" * 64,),
            expires_at=2_000_000_000,
        )
        self.assertTrue(self.store.put_page(marker))

        conflict = replace(marker, candidate_ids=("a" * 64,), expires_at=2_100_000_000)
        self.assertFalse(self.store.put_page(conflict))

        durable = self.store.load_page(marker.run_id, marker.page_set_id, marker.page)
        assert durable is not None
        self.assertEqual(durable.expires_at, marker.expires_at)
        self.assertEqual(durable.candidate_ids, marker.candidate_ids)

    def test_concurrent_later_page_expiry_wins_the_conditional_retry(self):
        marker = ResponsePageMarker(
            run_id="d" * 64,
            page_set_id="f" * 64,
            feed_name="feed-a",
            page=0,
            candidate_ids=("e" * 64,),
            expires_at=2_000_000_000,
        )
        self.assertTrue(self.store.put_page(marker))

        class RacingClient:
            def __init__(self, client):
                self.client = client
                self.raced = False

            def __getattr__(self, name):
                return getattr(self.client, name)

            def update_item(self, **kwargs):
                if not self.raced and kwargs.get("UpdateExpression") == "SET #expires_at = :expires_at":
                    self.raced = True
                    self.client.update_item(
                        TableName=TABLE,
                        Key=kwargs["Key"],
                        UpdateExpression="SET expires_at = :expiry",
                        ExpressionAttributeValues={":expiry": {"N": "2200000000"}},
                    )
                return self.client.update_item(**kwargs)

        racing_store = DynamoDBAnnouncementStateStore(RacingClient(self.client), TABLE)
        self.assertTrue(racing_store.put_page(replace(marker, expires_at=2_100_000_000)))

        durable = self.store.load_page(marker.run_id, marker.page_set_id, marker.page)
        assert durable is not None
        self.assertEqual(durable.expires_at, 2_200_000_000)

    def test_response_page_expiry_rejects_malformed_durable_metadata(self):
        marker = ResponsePageMarker(
            run_id="d" * 64,
            page_set_id="f" * 64,
            feed_name="feed-a",
            page=0,
            candidate_ids=("e" * 64,),
        )
        self.assertTrue(self.store.put_page(marker))
        self.client.update_item(
            TableName=TABLE,
            Key={
                "PK": {"S": f"RUN#{marker.run_id}"},
                "SK": {"S": f"PAGESET#{marker.page_set_id}#PAGE#000000"},
            },
            UpdateExpression="SET expires_at = :expiry",
            ExpressionAttributeValues={":expiry": {"S": "later"}},
        )
        with self.assertRaisesRegex(ValueError, "non-negative integer Unix timestamp"):
            self.store.load_page(marker.run_id, marker.page_set_id, marker.page)

    def test_optional_expiry_does_not_make_page_proof_fields_optional(self):
        marker = ResponsePageMarker(
            run_id="d" * 64,
            page_set_id="f" * 64,
            feed_name="feed-a",
            page=0,
            candidate_ids=("e" * 64,),
        )
        self.assertTrue(self.store.put_page(marker))
        self.client.update_item(
            TableName=TABLE,
            Key={
                "PK": {"S": f"RUN#{marker.run_id}"},
                "SK": {"S": f"PAGESET#{marker.page_set_id}#PAGE#000000"},
            },
            UpdateExpression="REMOVE complete",
        )
        with self.assertRaisesRegex(ValueError, "missing fields: complete"):
            self.store.load_page(marker.run_id, marker.page_set_id, marker.page)


class S3SnapshotStoreTests(unittest.TestCase):
    def setUp(self):
        self.aws = mock_aws()
        self.aws.start()
        self.client = boto3.client("s3", region_name=REGION)
        self.client.create_bucket(Bucket=BUCKET)
        self.store = S3SnapshotStore(
            self.client,
            BUCKET,
            "apcf/raw-snapshots/",
            max_bytes=32,
        )

    def tearDown(self):
        self.aws.stop()

    def test_body_key_prefix_and_safe_metadata_round_trip(self):
        body = b"<rss>safe</rss>"
        key = self.store.put("feed-a", NOW, body, invocation_id="request-1")
        response = self.client.get_object(Bucket=BUCKET, Key=key)
        self.assertEqual(response["Body"].read(), body)
        self.assertTrue(key.startswith("apcf/raw-snapshots/feed-a/20260810T150000.000000Z/request-1/"))
        self.assertTrue(key.endswith(f"/{hashlib.sha256(body).hexdigest()}.bin"))
        self.assertEqual(
            response["Metadata"],
            {
                "body-sha256": hashlib.sha256(body).hexdigest(),
                "feed-name": "feed-a",
                "invocation-id": "request-1",
            },
        )

    def test_two_invocations_cannot_collide(self):
        first = self.store.put("feed-a", NOW, b"same", invocation_id="request-1")
        second = self.store.put("feed-a", NOW, b"same", invocation_id="request-2")
        self.assertNotEqual(first, second)

    def test_oversized_body_is_refused_before_s3(self):
        with self.assertRaisesRegex(ValueError, "exceeds max_bytes"):
            self.store.put("feed-a", NOW, b"x" * 33, invocation_id="request-1")
        self.assertEqual(self.client.list_objects_v2(Bucket=BUCKET).get("KeyCount"), 0)

    def test_invalid_snapshot_scalars_are_refused_before_s3(self):
        cases = (
            ("BAD FEED", NOW, b"safe", "request-1"),
            ("feed-a", NOW.replace(tzinfo=None), b"safe", "request-1"),
            ("feed-a", NOW, "not-bytes", "request-1"),
            ("feed-a", NOW, b"safe", "unsafe/request"),
        )
        for feed_name, observed_at, body, invocation_id in cases:
            with self.subTest(feed_name=feed_name, invocation_id=invocation_id):
                with self.assertRaises(ValueError):
                    self.store.put(feed_name, observed_at, body, invocation_id=invocation_id)  # type: ignore[arg-type]
        self.assertEqual(self.client.list_objects_v2(Bucket=BUCKET).get("KeyCount"), 0)


if __name__ == "__main__":
    unittest.main()
