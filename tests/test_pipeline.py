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
sys.path.insert(0, str(ROOT / "scripts"))

import validate_config as validator  # noqa: E402

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
from aws_public_change_feed.state import (  # noqa: E402
    InMemoryAnnouncementStateStore,
    InMemoryFeedStateStore,
    observe,
)

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
        self.announcements = InMemoryAnnouncementStateStore()
        self.store = InMemoryOutboxStore()
        # Held directly rather than reached through `watcher.fetcher`, which is
        # typed as the protocol and has no response to swap.
        self.fetcher = StubFetcher(
            FetchOutcome(
                status=200,
                body=RSS,
                etag='"eks-134"',
                content_type="application/rss+xml",
            )
        )
        self.watcher = FeedWatcher(
            approved_hosts=APPROVED,
            state=self.state,
            fetcher=self.fetcher,
            clock=lambda: OBSERVED,
        )

    def run_feed(self):
        return self.watcher.run([FeedDefinition(name=FEED_NAME, url=FEED_URL, source_type="public_rss")])

    def candidates_for(self, result):
        """Run the emission half of the pipeline over one acquisition result.

        `is_update` is derived from announcement state rather than passed in,
        which is the only way chapter 04 defines it: true when state already
        holds an earlier content revision for the same announcement.

        A provenance-only sighting is deliberately *not* skipped here. Chapter
        02 requires a repeated invocation to reconstruct the same candidates so
        it can repair missing outbox records, and what prevents a second Slack
        delivery is the unchanged candidate identity meeting an existing
        delivery record, not refusing to build.
        """

        services = load_services(self.config)
        rules = load_risk_rules(self.config)
        built: list[dict] = []
        for announcement in result.announcements:
            observation = observe(self.announcements, announcement)
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
                        is_update=observation.is_update,
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

        emission = emit(
            self.store,
            candidates,
            inventory=self.inventory,
            message_policy=self.config["message_policy"],
            created_at=CREATED,
        )
        self.assertTrue(verify_durable(self.store, emission.candidate_ids))

        self.assertEqual(self.watcher.commit(result), (FEED_NAME,))
        self.assertEqual(checkpoint(self.state, FEED_NAME).etag, '"eks-134"')

    def test_a_lost_outbox_record_leaves_the_checkpoint_unadvanced(self):
        result = self.run_feed()
        candidates = self.candidates_for(result)
        emission = emit(
            self.store,
            candidates,
            inventory=self.inventory,
            message_policy=self.config["message_policy"],
            created_at=CREATED,
        )

        # A crash between the candidate and delivery writes.
        del self.store._deliveries[emission.candidate_ids[0]]

        self.assertFalse(verify_durable(self.store, emission.candidate_ids))
        # The caller must not commit, so the validator still holds and the next
        # run replays the same response.
        self.assertIsNone(checkpoint(self.state, FEED_NAME).etag)

    def test_replay_reconstructs_the_same_candidates_and_repairs_the_outbox(self):
        first = self.candidates_for(self.run_feed())
        emission = emit(
            self.store,
            first,
            inventory=self.inventory,
            message_policy=self.config["message_policy"],
            created_at=CREATED,
        )
        del self.store._deliveries[emission.candidate_ids[0]]

        # A second invocation over the same unadvanced feed response.
        replay = self.candidates_for(self.run_feed())
        self.assertEqual(
            [item["candidate_id"] for item in replay],
            list(emission.candidate_ids),
            msg="chapter 02: partial completion cannot create new logical work",
        )

        repaired = emit(
            self.store,
            replay,
            inventory=self.inventory,
            message_policy=self.config["message_policy"],
            created_at=CREATED,
        )
        self.assertEqual(repaired.created_candidates, ())
        self.assertEqual(repaired.repaired_deliveries, emission.candidate_ids)
        self.assertTrue(verify_durable(self.store, repaired.candidate_ids))

    def serve(self, body):
        """Point the stub at a different response body for the next run."""

        self.fetcher.outcome = FetchOutcome(
            status=200,
            body=body,
            etag='"eks-135"',
            content_type="application/rss+xml",
        )

    def test_the_same_announcement_on_a_second_feed_adds_no_delivery_work(self):
        first = self.candidates_for(self.run_feed())
        emit(
            self.store,
            first,
            inventory=self.inventory,
            message_policy=self.config["message_policy"],
            created_at=CREATED,
        )

        # ADR-013: the same canonical URL seen through another configured feed.
        second_feed = self.watcher.run(
            [
                FeedDefinition(
                    name="aws-news-blog",
                    url="https://aws.amazon.com/blogs/aws/feed/",
                    source_type="public_rss",
                )
            ]
        )
        second = self.candidates_for(second_feed)

        self.assertEqual(
            [item["candidate_id"] for item in second],
            [item["candidate_id"] for item in first],
            msg="ADR-002: an overlapping feed enriches provenance without a second candidate",
        )

        emission = emit(
            self.store,
            second,
            inventory=self.inventory,
            message_policy=self.config["message_policy"],
            created_at=CREATED,
        )
        self.assertEqual(emission.created_deliveries, ())
        self.assertEqual(emission.repaired_deliveries, ())

        record = self.announcements.load(self.expected["announcement"]["announcement_id"])
        assert record is not None
        self.assertEqual(
            sorted(entry.feed_name for entry in record.provenance),
            ["aws-news-blog", "aws-whats-new"],
            msg="announcement state carries the merged provenance the candidate does not",
        )
        self.assertEqual(len(record.revision_ids), 1)

    def test_an_edited_title_emits_a_new_candidate_marked_as_an_update(self):
        first = self.candidates_for(self.run_feed())
        emit(
            self.store,
            first,
            inventory=self.inventory,
            message_policy=self.config["message_policy"],
            created_at=CREATED,
        )
        self.assertFalse(first[0]["announcement"]["is_update"])

        self.serve(RSS.replace(b"version 1.34 available", b"version 1.35 available"))
        second = self.candidates_for(self.run_feed())

        self.assertTrue(
            second[0]["announcement"]["is_update"],
            msg="chapter 04: state already held an earlier content revision",
        )
        self.assertNotEqual(second[0]["candidate_id"], first[0]["candidate_id"])

        emission = emit(
            self.store,
            second,
            inventory=self.inventory,
            message_policy=self.config["message_policy"],
            created_at=CREATED,
        )
        self.assertEqual(emission.created_candidates, (second[0]["candidate_id"],))

        record = self.announcements.load(self.expected["announcement"]["announcement_id"])
        assert record is not None
        self.assertEqual(len(record.revision_ids), 2)

    def test_candidate_title_within_the_schema_bound_passes_both_contracts(self):
        title = "Amazon EKS Kubernetes version " + "x" * 170
        self.assertEqual(len(title), 200)
        self.serve(RSS.replace(b"Amazon EKS Kubernetes version 1.34 available", title.encode()))
        candidates = self.candidates_for(self.run_feed())

        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(len(candidate["announcement"]["title"]), 200)
        validator.validate_schema(
            ROOT / "schemas" / "alert-candidate.schema.json",
            ROOT / "examples" / "alert-candidate.json",
            candidate,
        )
        validator.validate_candidate_semantics(
            self.config,
            self.inventory,
            load_json("active-versions.json"),
            candidate,
        )

    def test_a_fragment_item_link_produces_a_candidate_the_contract_accepts(self):
        """A feed link with a fragment must survive the whole chain.

        The runtime keeps the raw sighting in provenance while identity uses the
        canonical URL, so `source_item_url` legitimately differs from
        `announcement.url`. This is the join that broke: each half was correct
        and the candidate was still outside its own contract, because the
        validator rejected the fragment outright rather than requiring the
        sighting to canonicalize to the announcement URL.
        """

        self.serve(RSS.replace(b"example-eks-update/</link>", b"example-eks-update/#overview</link>"))
        candidates = self.candidates_for(self.run_feed())

        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        provenance = candidate["announcement"]["provenance"][0]
        self.assertEqual(
            provenance["source_item_url"],
            "https://aws.amazon.com/about-aws/whats-new/2026/example-eks-update/#overview",
        )
        self.assertEqual(
            candidate["announcement"]["url"],
            "https://aws.amazon.com/about-aws/whats-new/2026/example-eks-update/",
        )
        validator.validate_schema(
            ROOT / "schemas" / "alert-candidate.schema.json",
            ROOT / "examples" / "alert-candidate.json",
            candidate,
        )
        validator.validate_candidate_semantics(
            self.config,
            self.inventory,
            load_json("active-versions.json"),
            candidate,
        )

    def test_a_user_info_item_link_never_reaches_a_candidate(self):
        """The other half of the same rule: what the contract still forbids.

        Canonicalization would strip the credentials from the announcement URL
        and leave them in the sighting, so the item is dropped during
        normalization and the run produces no candidate at all.
        """

        self.serve(RSS.replace(b"https://aws.amazon.com/about-aws", b"https://user:pw@aws.amazon.com/about-aws"))
        result = self.run_feed()

        self.assertEqual(result.outcomes[0].status, "fetched")
        self.assertEqual(result.announcements, ())
        self.assertEqual(self.candidates_for(result), [])

    def test_the_delivery_request_targets_the_route_destination(self):
        candidates = self.candidates_for(self.run_feed())
        emit(
            self.store,
            candidates,
            inventory=self.inventory,
            message_policy=self.config["message_policy"],
            created_at=CREATED,
        )
        record = delivery(self.store, candidates[0]["candidate_id"])
        expected_destination = self.inventory["slack"]["routes"][candidates[0]["route_id"]]["destination_key"]
        self.assertEqual(record.destination_key, expected_destination)
        self.assertEqual(record.request["candidate"], candidates[0])


if __name__ == "__main__":
    unittest.main()
