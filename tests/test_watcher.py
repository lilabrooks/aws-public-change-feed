"""Durable watcher orchestration from raw feed bytes to final checkpoint."""

import copy
import hashlib
import json
import sys
import unittest
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aws_public_change_feed.acquisition import FeedDefinition  # noqa: E402
from aws_public_change_feed.fetching import FetchOutcome, FetchRejected  # noqa: E402
from aws_public_change_feed.loading import LoadedRelease  # noqa: E402
from aws_public_change_feed.outbox import InMemoryOutboxStore  # noqa: E402
from aws_public_change_feed.state import (  # noqa: E402
    FeedCheckpoint,
    InMemoryAnnouncementStateStore,
    InMemoryFeedStateStore,
    InMemorySnapshotStore,
)
from aws_public_change_feed.watcher import (  # noqa: E402
    NullWatcherMetrics,
    WatcherOrchestrator,
    WatcherReleaseMismatch,
    response_page_set_id,
    response_run_id,
)

OBSERVED = datetime(2026, 7, 13, 16, 59, tzinfo=UTC)
CREATED = datetime(2026, 7, 13, 17, 0, tzinfo=UTC)
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


@dataclass
class StubFetcher:
    outcome: FetchOutcome

    def fetch(self, target, *, etag=None, last_modified=None):
        return self.outcome


class SequenceClock:
    def __init__(self, *values):
        self.values = list(values)

    def __call__(self):
        if len(self.values) > 1:
            return self.values.pop(0)
        return self.values[0]


class FailingSnapshotStore(InMemorySnapshotStore):
    def __init__(self, failing_feed):
        super().__init__()
        self.failing_feed = failing_feed

    def put(self, feed_name, observed_at, body, *, invocation_id="local"):
        if feed_name == self.failing_feed:
            raise OSError("simulated S3 refusal")
        return super().put(feed_name, observed_at, body, invocation_id=invocation_id)


class MissingDeliveryStore(InMemoryOutboxStore):
    def get_delivery(self, candidate):
        del candidate
        return None


class MissingEmissionStore(InMemoryAnnouncementStateStore):
    def put(self, record, *, expected_state_version):
        if record.emitted_candidate_ids:
            return True
        return super().put(record, expected_state_version=expected_state_version)


class FirstBatchCompletionFails(InMemoryFeedStateStore):
    def __init__(self):
        super().__init__()
        self.fail_next_batch = True

    def complete_many(self, completions):
        if self.fail_next_batch:
            self.fail_next_batch = False
            return False
        return super().complete_many(completions)


class OverlapFetcher:
    def __init__(self, overlapping_body):
        self.overlapping_body = overlapping_body
        self.fail_overlap = False

    def fetch(self, target, *, etag=None, last_modified=None):
        del etag, last_modified
        if target.path_with_query == "/blogs/aws/feed/":
            if self.fail_overlap:
                raise FetchRejected("network", "bounded test failure")
            return FetchOutcome(
                status=200,
                body=self.overlapping_body,
                etag='"overlap"',
                content_type="application/rss+xml",
            )
        return FetchOutcome(
            status=200,
            body=RSS,
            etag='"eks-134"',
            content_type="application/rss+xml",
        )


class CapturingStalenessMetrics(NullWatcherMetrics):
    def __init__(self):
        self.feed_values = {}
        self.maximum = None
        self.incomplete = 0
        self.snapshot_failures = []

    def feed_staleness(self, feed_name, seconds):
        self.feed_values[feed_name] = seconds

    def max_feed_staleness(self, seconds):
        self.maximum = seconds

    def incomplete_run(self):
        self.incomplete += 1

    def raw_snapshot_failure(self, feed_name):
        self.snapshot_failures.append(feed_name)


class WatcherOrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.expected = load_json("alert-candidate.json")
        config = load_config()
        config["feeds"] = [entry for entry in config["feeds"] if entry["name"] == "aws-whats-new"]
        self.release = LoadedRelease(
            release_id=self.expected["release"]["release_id"],
            config=config,
            inventory=load_json("inventory.json"),
            reference=self.expected["release"],
        )
        self.feed_state = InMemoryFeedStateStore()
        self.announcement_state = InMemoryAnnouncementStateStore()
        self.snapshots = InMemorySnapshotStore()
        self.outbox = InMemoryOutboxStore()

    def orchestrator(
        self,
        *,
        snapshots=None,
        outbox=None,
        fetcher=None,
        release=None,
        announcement_state=None,
        clock=None,
    ):
        selected = release or self.release
        return WatcherOrchestrator(
            feed_state=self.feed_state,
            announcement_state=announcement_state or self.announcement_state,
            snapshots=snapshots or self.snapshots,
            outbox=outbox or self.outbox,
            fetcher=fetcher
            or StubFetcher(
                FetchOutcome(
                    status=200,
                    body=RSS,
                    etag='"eks-134"',
                    content_type="application/rss+xml",
                )
            ),
            approved_hosts=("aws.amazon.com",),
            application_version=selected.reference["application_version"],
            max_items=200,
            max_item_characters=50_000,
            max_concurrent_fetches=5,
            clock=clock or SequenceClock(OBSERVED, CREATED, CREATED),
        )

    def test_raw_bytes_reproduce_the_committed_candidate_and_advance_last(self):
        result = self.orchestrator().run(
            self.release,
            invocation_id="request-1",
            remaining_time_ms=lambda: 300_000,
        )
        candidate_id = self.expected["candidate_id"]
        self.assertEqual(self.outbox.get_candidate(candidate_id), self.expected)
        self.assertEqual(result.candidate_ids, (candidate_id,))
        checkpoint = self.feed_state.load("aws-whats-new")
        assert checkpoint is not None
        self.assertEqual(checkpoint.etag, '"eks-134"')
        self.assertIsNone(checkpoint.lease_owner)
        announcement = self.announcement_state.load(self.expected["announcement"]["announcement_id"])
        assert announcement is not None
        self.assertEqual(announcement.emitted_candidate_ids, (candidate_id,))
        self.assertEqual(announcement.release_ids, (self.release.release_id,))

    def test_response_page_set_identity_normalizes_candidate_order(self):
        run_id = "a" * 64
        self.assertEqual(
            response_page_set_id(run_id, ("c" * 64, "b" * 64)),
            response_page_set_id(run_id, ("b" * 64, "c" * 64)),
        )

    def test_outbox_readback_failure_leaves_the_validator_pending(self):
        broken = MissingDeliveryStore()
        with self.assertRaisesRegex(RuntimeError, "outbox durability"):
            self.orchestrator(outbox=broken).run(
                self.release,
                invocation_id="request-1",
                remaining_time_ms=lambda: 300_000,
            )
        checkpoint = self.feed_state.load("aws-whats-new")
        assert checkpoint is not None
        self.assertIsNone(checkpoint.etag)
        self.assertEqual(checkpoint.pending_etag, '"eks-134"')
        self.assertEqual(checkpoint.lease_owner, "request-1")

    def test_emission_reference_readback_failure_leaves_the_validator_pending(self):
        broken = MissingEmissionStore()
        with self.assertRaisesRegex(RuntimeError, "announcement emission"):
            self.orchestrator(announcement_state=broken).run(
                self.release,
                invocation_id="request-1",
                remaining_time_ms=lambda: 300_000,
            )
        checkpoint = self.feed_state.load("aws-whats-new")
        assert checkpoint is not None
        self.assertIsNone(checkpoint.etag)
        self.assertEqual(checkpoint.pending_etag, '"eks-134"')

    def test_release_host_policy_mismatch_happens_before_any_claim_or_outbox_write(self):
        config = copy.deepcopy(dict(self.release.config))
        config["feeds"][0]["url"] = "https://evil.example/feed/"
        release = LoadedRelease(
            release_id=self.release.release_id,
            config=config,
            inventory=self.release.inventory,
            reference=self.release.reference,
        )
        with self.assertRaises(WatcherReleaseMismatch):
            self.orchestrator(release=release).run(
                release,
                invocation_id="request-1",
                remaining_time_ms=lambda: 300_000,
            )
        self.assertIsNone(self.feed_state.load("aws-whats-new"))
        self.assertIsNone(self.outbox.get_candidate(self.expected["candidate_id"]))

    def test_snapshot_failure_blocks_only_that_response(self):
        config = copy.deepcopy(dict(self.release.config))
        config["feeds"].append(
            {
                "name": "aws-news-blog",
                "url": "https://aws.amazon.com/blogs/aws/feed/",
                "source_type": "public_rss",
            }
        )
        release = LoadedRelease(
            release_id=self.release.release_id,
            config=config,
            inventory=self.release.inventory,
            reference=self.release.reference,
        )
        snapshots = FailingSnapshotStore("aws-whats-new")
        metrics = CapturingStalenessMetrics()
        result = self.orchestrator(snapshots=snapshots, release=release).run(
            release,
            invocation_id="request-1",
            remaining_time_ms=lambda: 300_000,
            metrics=metrics,
        )
        by_feed = {outcome.feed_name: outcome for outcome in result.outcomes}
        self.assertEqual(by_feed["aws-whats-new"].error_class, "raw_snapshot")
        self.assertEqual(by_feed["aws-news-blog"].status, "fetched")
        failed = self.feed_state.load("aws-whats-new")
        succeeded = self.feed_state.load("aws-news-blog")
        assert failed is not None and succeeded is not None
        self.assertIsNone(failed.etag)
        self.assertEqual(failed.consecutive_failures, 1)
        self.assertEqual(succeeded.etag, '"eks-134"')
        self.assertEqual(metrics.snapshot_failures, ["aws-whats-new"])

    def test_304_advances_success_without_creating_work(self):
        result = self.orchestrator(fetcher=StubFetcher(FetchOutcome(status=304))).run(
            self.release,
            invocation_id="request-1",
            remaining_time_ms=lambda: 300_000,
        )
        self.assertEqual(result.candidate_ids, ())
        self.assertEqual(result.outcomes[0].status, "not_modified")
        checkpoint = self.feed_state.load("aws-whats-new")
        assert checkpoint is not None
        self.assertEqual(checkpoint.last_success_at, OBSERVED.isoformat())
        self.assertIsNone(checkpoint.lease_owner)

    def test_no_match_response_has_one_empty_durable_page(self):
        body = RSS.replace(b"Amazon EKS Kubernetes version 1.34 available", b"AWS weekly roundup")
        body = body.replace(
            b"Amazon EKS now supports Kubernetes version 1.34 in all configured Regions.",
            b"A general collection of public announcements.",
        )
        result = self.orchestrator(
            fetcher=StubFetcher(FetchOutcome(status=200, body=body, etag='"quiet"', content_type="application/rss+xml"))
        ).run(
            self.release,
            invocation_id="request-1",
            remaining_time_ms=lambda: 300_000,
        )
        self.assertEqual(result.candidate_ids, ())
        run_id = response_run_id(
            "aws-whats-new",
            hashlib.sha256(body).hexdigest(),
            self.release.release_id,
            self.release.reference["application_version"],
        )
        page_set_id = response_page_set_id(run_id, ())
        marker = self.announcement_state.load_page(run_id, page_set_id, 0)
        assert marker is not None
        self.assertEqual(marker.candidate_ids, ())

    def test_first_success_staleness_uses_the_durable_first_attempt(self):
        self.feed_state.save(
            FeedCheckpoint(
                feed_name="aws-whats-new",
                feed_url="https://aws.amazon.com/about-aws/whats-new/recent/feed/",
                first_attempt_at=OBSERVED.isoformat(),
                last_attempt_at=CREATED.isoformat(),
            )
        )
        metrics = CapturingStalenessMetrics()
        self.orchestrator()._emit_staleness(
            (
                FeedDefinition(
                    "aws-whats-new",
                    "https://aws.amazon.com/about-aws/whats-new/recent/feed/",
                    "public_rss",
                ),
            ),
            CREATED,
            metrics,
        )
        self.assertEqual(metrics.feed_values, {"aws-whats-new": 60})
        self.assertEqual(metrics.maximum, 60)

    def test_remaining_time_exhaustion_claims_no_new_feed(self):
        metrics = CapturingStalenessMetrics()
        result = self.orchestrator().run(
            self.release,
            invocation_id="request-1",
            remaining_time_ms=lambda: 59_999,
            metrics=metrics,
        )
        self.assertTrue(result.incomplete)
        self.assertEqual(result.outcomes, ())
        self.assertIsNone(self.feed_state.load("aws-whats-new"))
        self.assertEqual(metrics.incomplete, 1)

    def test_remaining_time_exhaustion_completes_claimed_feeds_and_leaves_later_feeds_unstarted(self):
        config = copy.deepcopy(dict(self.release.config))
        config["feeds"].append(
            {
                "name": "aws-news-blog",
                "url": "https://aws.amazon.com/blogs/aws/feed/",
                "source_type": "public_rss",
            }
        )
        release = LoadedRelease(
            release_id=self.release.release_id,
            config=config,
            inventory=self.release.inventory,
            reference=self.release.reference,
        )
        remaining = iter((300_000, 59_999))
        result = self.orchestrator(release=release).run(
            release,
            invocation_id="request-1",
            remaining_time_ms=lambda: next(remaining),
        )
        self.assertTrue(result.incomplete)
        self.assertEqual(result.advanced_feeds, ("aws-whats-new",))
        self.assertEqual(self.feed_state.load("aws-whats-new").etag, '"eks-134"')  # type: ignore[union-attr]
        self.assertIsNone(self.feed_state.load("aws-news-blog"))

    def test_replay_of_the_same_feed_body_survives_a_changed_overlap_set(self):
        config = copy.deepcopy(dict(self.release.config))
        config["feeds"].append(
            {
                "name": "aws-news-blog",
                "url": "https://aws.amazon.com/blogs/aws/feed/",
                "source_type": "public_rss",
            }
        )
        release = LoadedRelease(
            release_id=self.release.release_id,
            config=config,
            inventory=self.release.inventory,
            reference=self.release.reference,
        )
        overlapping_body = RSS.replace(
            b"Amazon EKS Kubernetes version 1.34 available",
            b"Amazon EKS Kubernetes version 1.34 generally available",
        ).replace(
            b"Mon, 13 Jul 2026 12:00:00 GMT",
            b"Mon, 13 Jul 2026 11:00:00 GMT",
        )
        fetcher = OverlapFetcher(overlapping_body)
        self.feed_state = FirstBatchCompletionFails()

        with self.assertRaisesRegex(Exception, "checkpoint transaction"):
            self.orchestrator(
                release=release,
                fetcher=fetcher,
                clock=SequenceClock(OBSERVED, CREATED, CREATED),
            ).run(
                release,
                invocation_id="request-1",
                remaining_time_ms=lambda: 300_000,
            )

        fetcher.fail_overlap = True
        replay_time = OBSERVED + timedelta(minutes=10)
        result = self.orchestrator(
            release=release,
            fetcher=fetcher,
            clock=SequenceClock(replay_time, replay_time, replay_time),
        ).run(
            release,
            invocation_id="request-2",
            remaining_time_ms=lambda: 300_000,
        )

        self.assertEqual(result.advanced_feeds, ("aws-whats-new",))
        checkpoint = self.feed_state.load("aws-whats-new")
        assert checkpoint is not None
        self.assertEqual(checkpoint.etag, '"eks-134"')


if __name__ == "__main__":
    unittest.main()
