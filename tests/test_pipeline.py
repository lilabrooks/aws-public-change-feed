"""The chain from a feed response to an advanced checkpoint.

Each milestone-4 module is tested against the contract on its own. This file
tests the joins between them, which is where a design can read correctly at
every step and still not work: acquisition to matching to profile mapping to
candidate construction to the outbox, and only then the checkpoint.

The feed response here carries the same item as `examples/alert-candidate.json`,
so the whole pipeline is required to reproduce the committed candidate from raw
bytes rather than from a hand-assembled `NormalizedAnnouncement`.
"""

import json
import sys
import unittest
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aws_public_change_feed.acquisition import FeedDefinition, FeedWatcher  # noqa: E402
from aws_public_change_feed.candidates import build_candidates  # noqa: E402
from aws_public_change_feed.fetching import FetchOutcome  # noqa: E402
from aws_public_change_feed.matching import (  # noqa: E402
    Announcement,
    load_risk_rules,
    load_services,
    match_announcement,
)
from aws_public_change_feed.outbox import InMemoryOutboxStore, emit, verify_durable  # noqa: E402
from aws_public_change_feed.profiles import route_audiences  # noqa: E402
from aws_public_change_feed.state import InMemoryFeedStateStore  # noqa: E402

APPROVED = ("aws.amazon.com",)
FEED_NAME = "aws-whats-new"
FEED_URL = "https://aws.amazon.com/about-aws/whats-new/recent/feed/"
OBSERVED = datetime(2026, 7, 13, 16, 59, tzinfo=UTC)
CREATED = datetime(2026, 7, 13, 17, 0, tzinfo=UTC)

RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title>Amazon EKS Kubernetes version 1.34 available</title>
    <link>https://aws.amazon.com/about-aws/whats-new/2026/example-eks-update/</link>
    <description>Amazon EKS now supports Kubernetes version 1.34 in all configured Regions.</description>
    <pubDate>Mon, 13 Jul 2026 12:00:00 GMT</pubDate>
  </item>
</channel></rss>"""


@dataclass
class StubFetcher:
    """Implements `FeedFetching` without the socket layer.

    The transport is exercised in `test_acquisition.py`; what matters here is
    that the watcher receives a real response body and parses it itself.
    """

    outcome: FetchOutcome

    def fetch(self, target, *, etag=None, last_modified=None):
        return self.outcome


def checkpoint(state, feed_name):
    """Load a checkpoint the test knows exists."""

    record = state.load(feed_name)
    assert record is not None
    return record


def delivery(store, key):
    """Load a delivery record the test knows exists."""

    record = store.get_delivery(key)
    assert record is not None
    return record


def load_json(name):
    with (ROOT / "examples" / name).open(encoding="utf-8") as handle:
        return json.load(handle)


def load_config():
    with (ROOT / "examples" / "config.yaml").open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config()
        self.inventory = load_json("inventory.json")
        self.expected = load_json("alert-candidate.json")
        self.state = InMemoryFeedStateStore()
        self.store = InMemoryOutboxStore()
        self.watcher = FeedWatcher(
            approved_hosts=APPROVED,
            state=self.state,
            fetcher=StubFetcher(
                FetchOutcome(
                    status=200,
                    body=RSS,
                    etag='"eks-134"',
                    content_type="application/rss+xml",
                )
            ),
            clock=lambda: OBSERVED,
        )

    def run_feed(self):
        return self.watcher.run([FeedDefinition(name=FEED_NAME, url=FEED_URL, source_type="public_rss")])

    def candidates_for(self, result):
        services = load_services(self.config)
        rules = load_risk_rules(self.config)
        built: list[dict] = []
        for announcement in result.announcements:
            for match in match_announcement(
                Announcement(title=announcement.title, summary=announcement.summary),
                services,
                rules,
            ):
                built.extend(
                    build_candidates(
                        announcement=announcement,
                        match=match,
                        audiences=route_audiences(self.config, self.inventory, match.service_id),
                        configuration=self.config,
                        release=self.expected["release"],
                        created_at=CREATED,
                    )
                )
        return built

    def test_the_pipeline_reproduces_the_committed_candidate_from_raw_bytes(self):
        candidates = self.candidates_for(self.run_feed())
        self.assertEqual(candidates, [self.expected])

    def test_the_checkpoint_is_held_until_the_outbox_is_durable(self):
        result = self.run_feed()
        candidates = self.candidates_for(result)

        # Acquisition recorded the attempt but must not have moved the validator.
        self.assertIsNone(checkpoint(self.state, FEED_NAME).etag)
        self.assertFalse(verify_durable(self.store, [item["candidate_id"] for item in candidates]))

        emission = emit(self.store, candidates, inventory=self.inventory, created_at=CREATED)
        self.assertTrue(verify_durable(self.store, emission.candidate_ids))

        self.assertEqual(self.watcher.commit(result), (FEED_NAME,))
        self.assertEqual(checkpoint(self.state, FEED_NAME).etag, '"eks-134"')

    def test_a_lost_outbox_record_leaves_the_checkpoint_unadvanced(self):
        result = self.run_feed()
        candidates = self.candidates_for(result)
        emission = emit(self.store, candidates, inventory=self.inventory, created_at=CREATED)

        # A crash between the candidate and delivery writes.
        del self.store._deliveries[emission.candidate_ids[0]]

        self.assertFalse(verify_durable(self.store, emission.candidate_ids))
        # The caller must not commit, so the validator still holds and the next
        # run replays the same response.
        self.assertIsNone(checkpoint(self.state, FEED_NAME).etag)

    def test_replay_reconstructs_the_same_candidates_and_repairs_the_outbox(self):
        first = self.candidates_for(self.run_feed())
        emission = emit(self.store, first, inventory=self.inventory, created_at=CREATED)
        del self.store._deliveries[emission.candidate_ids[0]]

        # A second invocation over the same unadvanced feed response.
        replay = self.candidates_for(self.run_feed())
        self.assertEqual(
            [item["candidate_id"] for item in replay],
            list(emission.candidate_ids),
            msg="chapter 02: partial completion cannot create new logical work",
        )

        repaired = emit(self.store, replay, inventory=self.inventory, created_at=CREATED)
        self.assertEqual(repaired.created_candidates, ())
        self.assertEqual(repaired.repaired_deliveries, emission.candidate_ids)
        self.assertTrue(verify_durable(self.store, repaired.candidate_ids))

    def test_the_delivery_request_targets_the_route_destination(self):
        candidates = self.candidates_for(self.run_feed())
        emit(self.store, candidates, inventory=self.inventory, created_at=CREATED)
        record = delivery(self.store, candidates[0]["candidate_id"])
        expected_destination = self.inventory["slack"]["routes"][candidates[0]["route_id"]]["destination_key"]
        self.assertEqual(record.destination_key, expected_destination)
        self.assertEqual(record.request["candidate"], candidates[0])


if __name__ == "__main__":
    unittest.main()
