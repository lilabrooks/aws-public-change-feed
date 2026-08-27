"""Watcher Lambda boundary, EMF, and moto-backed AWS composition."""

import contextlib
import hashlib
import io
import json
import os
import sys
import unittest
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import boto3
import yaml
from moto import mock_aws

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import aws_public_change_feed.watcher_runtime as runtime_module  # noqa: E402
from aws_public_change_feed.fetching import FetchOutcome  # noqa: E402
from aws_public_change_feed.identity import release_id  # noqa: E402
from aws_public_change_feed.outbox import DynamoDBDeliveryStore  # noqa: E402
from aws_public_change_feed.releases import S3ObjectStore  # noqa: E402
from aws_public_change_feed.source_store import (  # noqa: E402
    DynamoDBAnnouncementStateStore,
    DynamoDBFeedStateStore,
    S3SnapshotStore,
)
from aws_public_change_feed.state import ConditionalStateConflict  # noqa: E402
from aws_public_change_feed.watcher import (  # noqa: E402
    FeedRunOutcome,
    NullWatcherMetrics,
    WatcherOrchestrator,
    WatcherReleaseMismatch,
    WatcherResult,
)
from aws_public_change_feed.watcher_runtime import (  # noqa: E402
    EmbeddedWatcherMetrics,
    WatcherRuntime,
    lambda_handler,
)

REGION = "us-east-1"
BUCKET = "watcher-runtime-bucket"
SOURCE_TABLE = "source-state"
DELIVERY_TABLE = "delivery"
POINTER_KEY = "apcf/active-versions.json"
APP_VERSION = "sha256:" + "a" * 64
NOW = datetime(2026, 7, 13, 16, 59, tzinfo=UTC)
RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><item>
<title>Amazon EKS Kubernetes version 1.34 available</title>
<link>https://aws.amazon.com/about-aws/whats-new/2026/example-eks-update/</link>
<description>Amazon EKS now supports Kubernetes version 1.34 in all configured Regions.</description>
<pubDate>Mon, 13 Jul 2026 12:00:00 GMT</pubDate>
</item></channel></rss>"""


def load_json(name):
    with (ROOT / "examples" / name).open(encoding="utf-8") as handle:
        return json.load(handle)


def load_config():
    with (ROOT / "examples" / "config.yaml").open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def create_table(client, name):
    client.create_table(
        TableName=name,
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


@dataclass
class StubFetcher:
    def fetch(self, target, *, etag=None, last_modified=None):
        return FetchOutcome(
            status=200,
            body=RSS,
            etag='"eks-134"',
            content_type="application/rss+xml",
        )


class FixedClock:
    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return NOW if self.calls == 1 else NOW.replace(hour=17, minute=0)


class CountingMetrics(NullWatcherMetrics):
    def __init__(self):
        self.release_failures = 0

    def release_verification_failure(self):
        self.release_failures += 1


class MetricsTests(unittest.TestCase):
    def test_unobserved_staleness_is_not_reported_as_zero(self):
        output: list[str] = []
        metrics = EmbeddedWatcherMetrics(
            "AWSPublicChangeFeed/dev",
            "watcher",
            clock=lambda: NOW,
            emit=output.append,
        )
        metrics.release_verification_failure()
        metrics.flush()
        rendered = json.dumps([json.loads(line) for line in output], sort_keys=True)
        self.assertIn("ReleaseVerificationFailures", rendered)
        self.assertNotIn("MaxFeedStalenessSeconds", rendered)

    def test_alarm_inputs_and_bounded_dimensions_are_emitted(self):
        output: list[str] = []
        metrics = EmbeddedWatcherMetrics(
            "AWSPublicChangeFeed/dev",
            "watcher",
            clock=lambda: NOW,
            emit=output.append,
        )
        metrics.heartbeat()
        metrics.release_verification_failure()
        metrics.raw_snapshot_failure("aws-whats-new")
        metrics.feed_staleness("aws-whats-new", 120)
        metrics.max_feed_staleness(120)
        metrics.candidate(service="eks", risk="api-change", priority="high", route="platform")
        metrics.flush()
        documents = [json.loads(line) for line in output]
        rendered = json.dumps(documents, sort_keys=True)
        for name in (
            "Heartbeat",
            "ReleaseVerificationFailures",
            "RawSnapshotFailures",
            "FeedStalenessSeconds",
            "MaxFeedStalenessSeconds",
        ):
            self.assertIn(name, rendered)
        self.assertNotIn("candidate_id", rendered)
        self.assertNotIn("https://", rendered)

    def test_every_watcher_metric_has_the_frozen_unit_and_dimension_shape(self):
        output: list[str] = []
        metrics = EmbeddedWatcherMetrics(
            "AWSPublicChangeFeed/dev",
            "watcher",
            clock=lambda: NOW,
            emit=output.append,
        )
        metrics.heartbeat()
        metrics.release_verification_failure()
        metrics.watcher_fault()
        metrics.feed_attempt("aws-whats-new")
        metrics.feed_success("aws-whats-new", not_modified=True)
        metrics.feed_failure("aws-whats-new", "dns")
        metrics.feed_contention("aws-whats-new")
        metrics.response_observed("aws-whats-new", byte_count=123, item_count=2)
        metrics.raw_snapshot_failure("aws-whats-new")
        metrics.announcements_observed(normalized=2, coalesced=1)
        metrics.announcement_state(new_revision=True, provenance_only=True)
        metrics.no_match()
        metrics.rule_exclusions(3)
        metrics.candidate(service="eks", risk="security", priority="high", route="platform")
        metrics.candidate_validation_failure()
        metrics.candidate_size_failure()
        metrics.feed_staleness("aws-whats-new", 120)
        metrics.newest_publication_age("aws-whats-new", 60)
        metrics.max_feed_staleness(120)
        metrics.incomplete_run()
        metrics.incomplete_run()
        metrics.flush()

        expected = {
            "CandidateSizeFailures": "Count",
            "CandidateValidationFailures": "Count",
            "Candidates": "Count",
            "CoalescedAnnouncements": "Count",
            "FeedAttempts": "Count",
            "FeedFailures": "Count",
            "FeedItems": "Count",
            "FeedLeaseContention": "Count",
            "FeedNotModified": "Count",
            "FeedStalenessSeconds": "Seconds",
            "FeedSuccesses": "Count",
            "Heartbeat": "Count",
            "IncompleteRuns": "Count",
            "MaxFeedStalenessSeconds": "Seconds",
            "NewRevisions": "Count",
            "NewestPublicationAgeSeconds": "Seconds",
            "NoMatch": "Count",
            "NormalizedAnnouncements": "Count",
            "ProvenanceOnlyUpdates": "Count",
            "RawSnapshotFailures": "Count",
            "Rejected_dns": "Count",
            "ReleaseVerificationFailures": "Count",
            "ResponseBytes": "Bytes",
            "RuleExclusions": "Count",
            "WatcherFaults": "Count",
        }
        observed = {}
        observed_dimensions = {}
        allowed_dimensions = {
            (),
            ("FeedName",),
            ("Function",),
            ("Service", "Risk", "Priority", "Route"),
        }
        for document in map(json.loads, output):
            metadata = document["_aws"]["CloudWatchMetrics"][0]
            self.assertIn(tuple(metadata["Dimensions"][0]), allowed_dimensions)
            for definition in metadata["Metrics"]:
                observed[definition["Name"]] = definition["Unit"]
                observed_dimensions[definition["Name"]] = tuple(metadata["Dimensions"][0])
        self.assertEqual(observed, expected)
        self.assertEqual(observed_dimensions["IncompleteRuns"], ("Function",))
        self.assertEqual(observed_dimensions["WatcherFaults"], ("Function",))
        incomplete_document = next(document for document in map(json.loads, output) if "IncompleteRuns" in document)
        self.assertEqual(incomplete_document["IncompleteRuns"], 1)


class MotoCompositionTests(unittest.TestCase):
    def setUp(self):
        self.aws = mock_aws()
        self.aws.start()
        self.s3 = boto3.client("s3", region_name=REGION)
        self.s3.create_bucket(Bucket=BUCKET)
        self.s3.put_bucket_versioning(Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})
        self.ddb = boto3.client("dynamodb", region_name=REGION)
        create_table(self.ddb, SOURCE_TABLE)
        create_table(self.ddb, DELIVERY_TABLE)

    def tearDown(self):
        self.aws.stop()

    def publish_release(self):
        config = load_config()
        config["feeds"] = [entry for entry in config["feeds"] if entry["name"] == "aws-whats-new"]
        inventory = load_json("inventory.json")
        config_body = yaml.safe_dump(config, sort_keys=False).encode()
        inventory_body = json.dumps(inventory, separators=(",", ":"), sort_keys=True).encode()
        config_key = "apcf/releases/test/config.yaml"
        inventory_key = "apcf/releases/test/inventory.json"
        config_version = self.s3.put_object(Bucket=BUCKET, Key=config_key, Body=config_body)["VersionId"]
        inventory_version = self.s3.put_object(Bucket=BUCKET, Key=inventory_key, Body=inventory_body)["VersionId"]
        config_sha = hashlib.sha256(config_body).hexdigest()
        inventory_sha = hashlib.sha256(inventory_body).hexdigest()
        identifier = release_id(config_sha, inventory_sha)
        pointer = {
            "schema_version": 2,
            "release_id": identifier,
            "promoted_at": "2026-07-13T16:58:00Z",
            "config": {
                "key": config_key,
                "version_id": config_version,
                "sha256": config_sha,
                "schema_version": config["version"],
            },
            "inventory": {
                "key": inventory_key,
                "version_id": inventory_version,
                "sha256": inventory_sha,
                "schema_version": inventory["schema_version"],
            },
        }
        self.s3.put_object(Bucket=BUCKET, Key=POINTER_KEY, Body=json.dumps(pointer).encode())
        return pointer

    def test_aws_root_drives_raw_xml_to_both_tables_snapshot_and_checkpoint(self):
        pointer = self.publish_release()
        runtime = WatcherRuntime(
            release_store=S3ObjectStore(self.s3, BUCKET),
            pointer_key=POINTER_KEY,
            application_version=APP_VERSION,
            orchestrator=WatcherOrchestrator(
                feed_state=DynamoDBFeedStateStore(self.ddb, SOURCE_TABLE),
                announcement_state=DynamoDBAnnouncementStateStore(self.ddb, SOURCE_TABLE),
                snapshots=S3SnapshotStore(
                    self.s3,
                    BUCKET,
                    "apcf/raw-snapshots/",
                    max_bytes=5 * 1024 * 1024,
                ),
                outbox=DynamoDBDeliveryStore(self.ddb, DELIVERY_TABLE),
                fetcher=StubFetcher(),
                approved_hosts=("aws.amazon.com",),
                application_version=APP_VERSION,
                max_items=200,
                max_item_characters=50_000,
                max_concurrent_fetches=5,
                clock=FixedClock(),
            ),
        )
        result = runtime.run(
            invocation_id="request-1",
            remaining_time_ms=lambda: 300_000,
            metrics=NullWatcherMetrics(),
        )
        self.assertEqual(len(result.candidate_ids), 1)
        candidate_id = result.candidate_ids[0]
        candidate = DynamoDBDeliveryStore(self.ddb, DELIVERY_TABLE).get_candidate(candidate_id)
        assert candidate is not None
        self.assertEqual(candidate["release"]["release_id"], pointer["release_id"])
        self.assertEqual(candidate["release"]["application_version"], APP_VERSION)
        self.assertIsNotNone(DynamoDBDeliveryStore(self.ddb, DELIVERY_TABLE).get_delivery(candidate_id))
        checkpoint = DynamoDBFeedStateStore(self.ddb, SOURCE_TABLE).load("aws-whats-new")
        assert checkpoint is not None
        self.assertEqual(checkpoint.etag, '"eks-134"')
        self.assertIsNone(checkpoint.lease_owner)
        snapshots = self.s3.list_objects_v2(Bucket=BUCKET, Prefix="apcf/raw-snapshots/")
        self.assertEqual(snapshots["KeyCount"], 1)

    def test_release_policy_mismatch_emits_verification_failure_before_any_state_write(self):
        self.publish_release()
        metrics = CountingMetrics()
        runtime = WatcherRuntime(
            release_store=S3ObjectStore(self.s3, BUCKET),
            pointer_key=POINTER_KEY,
            application_version=APP_VERSION,
            orchestrator=WatcherOrchestrator(
                feed_state=DynamoDBFeedStateStore(self.ddb, SOURCE_TABLE),
                announcement_state=DynamoDBAnnouncementStateStore(self.ddb, SOURCE_TABLE),
                snapshots=S3SnapshotStore(
                    self.s3,
                    BUCKET,
                    "apcf/raw-snapshots/",
                    max_bytes=5 * 1024 * 1024,
                ),
                outbox=DynamoDBDeliveryStore(self.ddb, DELIVERY_TABLE),
                fetcher=StubFetcher(),
                approved_hosts=("aws.amazon.com", "extra.example"),
                application_version=APP_VERSION,
                max_items=200,
                max_item_characters=50_000,
                max_concurrent_fetches=5,
                clock=FixedClock(),
            ),
        )
        with self.assertRaises(WatcherReleaseMismatch):
            runtime.run(
                invocation_id="request-1",
                remaining_time_ms=lambda: 300_000,
                metrics=metrics,
            )
        self.assertEqual(metrics.release_failures, 1)
        self.assertEqual(self.ddb.scan(TableName=SOURCE_TABLE)["Count"], 0)
        self.assertEqual(self.ddb.scan(TableName=DELIVERY_TABLE)["Count"], 0)


class HandlerTests(unittest.TestCase):
    @dataclass
    class Context:
        aws_request_id: str = "request-1"

        def get_remaining_time_in_millis(self):
            return 300_000

    class Runner:
        def run(self, *, invocation_id, remaining_time_ms, metrics):
            self.invocation_id = invocation_id
            self.remaining = remaining_time_ms()
            return WatcherResult(outcomes=(), candidate_ids=(), advanced_feeds=())

    class MixedCompletionRunner:
        def run(self, *, invocation_id, remaining_time_ms, metrics):
            del invocation_id, remaining_time_ms, metrics
            return WatcherResult(
                outcomes=(
                    FeedRunOutcome(feed_name="fetched", status="fetched"),
                    FeedRunOutcome(feed_name="unchanged", status="not_modified"),
                    FeedRunOutcome(feed_name="contended", status="contended"),
                ),
                candidate_ids=("created", "repaired", "reused"),
                advanced_feeds=("fetched",),
                created_delivery_ids=("created",),
                repaired_delivery_ids=("repaired",),
            )

    class FailingRunner:
        def run(self, *, invocation_id, remaining_time_ms, metrics):
            del invocation_id, remaining_time_ms, metrics
            raise ValueError("https://evil.example/private-source")

    class IncompleteRunner:
        def run(self, *, invocation_id, remaining_time_ms, metrics):
            del invocation_id, remaining_time_ms, metrics
            return WatcherResult(outcomes=(), candidate_ids=(), advanced_feeds=(), incomplete=True)

    class ConditionalConflictRunner:
        def run(self, *, invocation_id, remaining_time_ms, metrics):
            del invocation_id, remaining_time_ms, metrics
            raise ConditionalStateConflict("bounded conditional retry exhausted")

    def event(self):
        return {
            "version": "0",
            "id": "event-1",
            "detail-type": "Scheduled Event",
            "source": "aws.events",
            "account": "123456789012",
            "time": "2026-07-13T16:59:00Z",
            "region": REGION,
            "resources": ["arn:aws:events:us-east-1:123456789012:rule/watcher"],
            "detail": {},
        }

    def environment(self):
        return {
            "CONFIG_BUCKET": BUCKET,
            "ACTIVE_VERSIONS_OBJECT_KEY": POINTER_KEY,
            "SOURCE_STATE_TABLE_NAME": SOURCE_TABLE,
            "DELIVERY_TABLE_NAME": DELIVERY_TABLE,
            "DELIVERY_INDEX_NAME": "status-next-action-index",
            "RAW_SNAPSHOT_PREFIX": "apcf/raw-snapshots/",
            "APPLICATION_VERSION": APP_VERSION,
            "APPROVED_FEED_HOSTS_JSON": '["aws.amazon.com"]',
            "FEED_CONNECT_TIMEOUT_SECONDS": "5",
            "FEED_RESPONSE_TIMEOUT_SECONDS": "15",
            "MAX_FEED_RESPONSE_BYTES": str(5 * 1024 * 1024),
            "MAX_FEED_ITEMS": "200",
            "MAX_FEED_ITEM_CHARACTERS": "50000",
            "MAX_FEED_REDIRECTS": "0",
            "MAX_CONCURRENT_FETCHES": "5",
            "FEED_LEASE_SECONDS": "360",
            "METRICS_NAMESPACE": "AWSPublicChangeFeed/dev",
            "AWS_LAMBDA_FUNCTION_NAME": "watcher",
        }

    def tearDown(self):
        runtime_module._runtime = None

    def test_valid_event_emits_heartbeat_and_returns_bounded_counts(self):
        runner = self.Runner()
        runtime_module._runtime = runner
        output = io.StringIO()
        with patch.dict(os.environ, self.environment(), clear=True), contextlib.redirect_stdout(output):
            result = lambda_handler(self.event(), self.Context())
        self.assertEqual(
            result,
            {
                "feeds": 0,
                "completed": 0,
                "advanced": 0,
                "candidates": 0,
                "created_deliveries": 0,
                "repaired_deliveries": 0,
            },
        )
        self.assertEqual(runner.invocation_id, "request-1")
        rendered = output.getvalue()
        self.assertIn("Heartbeat", rendered)
        self.assertNotIn("IncompleteRuns", rendered)
        self.assertNotIn("WatcherFaults", rendered)

    def test_bounded_counts_distinguish_304_completion_from_contention(self):
        runtime_module._runtime = self.MixedCompletionRunner()
        with patch.dict(os.environ, self.environment(), clear=True):
            result = lambda_handler(self.event(), self.Context())
        self.assertEqual(
            result,
            {
                "feeds": 3,
                "completed": 2,
                "advanced": 1,
                "candidates": 3,
                "created_deliveries": 1,
                "repaired_deliveries": 1,
            },
        )

    def test_malformed_event_and_missing_environment_emit_no_heartbeat(self):
        for event, environment in (({}, self.environment()), (self.event(), {})):
            output = io.StringIO()
            with (
                self.subTest(event=event),
                patch.dict(os.environ, environment, clear=True),
                contextlib.redirect_stdout(output),
            ):
                with self.assertRaises(ValueError):
                    lambda_handler(event, self.Context())
                self.assertNotIn("Heartbeat", output.getvalue())

    def test_nonzero_redirect_policy_is_refused_before_heartbeat(self):
        environment = self.environment()
        environment["MAX_FEED_REDIRECTS"] = "1"
        output = io.StringIO()
        with patch.dict(os.environ, environment, clear=True), contextlib.redirect_stdout(output):
            with self.assertRaisesRegex(ValueError, "MAX_FEED_REDIRECTS must be zero"):
                lambda_handler(self.event(), self.Context())
        self.assertNotIn("Heartbeat", output.getvalue())

    def test_invalid_snapshot_prefix_is_refused_before_heartbeat(self):
        environment = self.environment()
        environment["RAW_SNAPSHOT_PREFIX"] = "/absolute-prefix/"
        output = io.StringIO()
        with patch.dict(os.environ, environment, clear=True), contextlib.redirect_stdout(output):
            with self.assertRaisesRegex(ValueError, "relative S3 prefix"):
                lambda_handler(self.event(), self.Context())
        self.assertNotIn("Heartbeat", output.getvalue())

    def test_internal_fault_is_generic_and_emits_the_bounded_fault_metric(self):
        runtime_module._runtime = self.FailingRunner()
        output = io.StringIO()
        with patch.dict(os.environ, self.environment(), clear=True), contextlib.redirect_stdout(output):
            with self.assertRaisesRegex(RuntimeError, "watcher invocation failed") as caught:
                lambda_handler(self.event(), self.Context())
        rendered = output.getvalue()
        self.assertIn("WatcherFaults", rendered)
        self.assertNotIn("IncompleteRuns", rendered)
        self.assertNotIn("evil.example", rendered)
        self.assertNotIn("evil.example", str(caught.exception))

    def test_remaining_time_incompletion_fails_for_retry_without_a_fault_metric(self):
        runtime_module._runtime = self.IncompleteRunner()
        output = io.StringIO()
        with patch.dict(os.environ, self.environment(), clear=True), contextlib.redirect_stdout(output):
            with self.assertRaisesRegex(RuntimeError, "watcher invocation failed"):
                lambda_handler(self.event(), self.Context())
        rendered = output.getvalue()
        self.assertIn("IncompleteRuns", rendered)
        self.assertNotIn("WatcherFaults", rendered)

    def test_exhausted_conditional_state_conflict_is_incomplete_not_a_fault(self):
        runtime_module._runtime = self.ConditionalConflictRunner()
        output = io.StringIO()
        with patch.dict(os.environ, self.environment(), clear=True), contextlib.redirect_stdout(output):
            with self.assertRaisesRegex(RuntimeError, "watcher invocation failed"):
                lambda_handler(self.event(), self.Context())
        rendered = output.getvalue()
        self.assertIn("IncompleteRuns", rendered)
        self.assertNotIn("WatcherFaults", rendered)


if __name__ == "__main__":
    unittest.main()
