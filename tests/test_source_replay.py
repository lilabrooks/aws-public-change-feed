"""Retained-source replay planning and missing-state repair."""

from __future__ import annotations

import hashlib
import json
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aws_public_change_feed.loading import LoadedRelease  # noqa: E402
from aws_public_change_feed.outbox import InMemoryOutboxStore  # noqa: E402
from aws_public_change_feed.source_replay import (  # noqa: E402
    ReplayRefused,
    RetainedSnapshot,
    apply_source_replay,
    create_source_replay_plan,
)
from aws_public_change_feed.state import InMemoryAnnouncementStateStore  # noqa: E402

OBSERVED = datetime(2026, 7, 13, 16, 59, tzinfo=UTC)
PLANNED = datetime(2026, 8, 30, 18, 0, tzinfo=UTC)
APPLICATION_VERSION = "sha256:" + "d" * 64
POINTER_KEY = "apcf/active-versions.json"
POINTER_VERSION = "pointer-version-7"
RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><item>
<title>Amazon EKS Kubernetes version 1.34 available</title>
<link>https://aws.amazon.com/about-aws/whats-new/2026/example-eks-update/</link>
<description>Amazon EKS now supports Kubernetes version 1.34 in all configured Regions.</description>
<pubDate>Mon, 13 Jul 2026 12:00:00 GMT</pubDate>
</item></channel></rss>"""


def load_json(name: str):
    return json.loads((ROOT / "examples" / name).read_text(encoding="utf-8"))


def release() -> LoadedRelease:
    config = yaml.safe_load((ROOT / "examples" / "config.yaml").read_text(encoding="utf-8"))
    config["feeds"] = [entry for entry in config["feeds"] if entry["name"] == "aws-whats-new"]
    expected = load_json("alert-candidate.json")
    reference = dict(expected["release"])
    reference["application_version"] = APPLICATION_VERSION
    return LoadedRelease(
        release_id=expected["release"]["release_id"],
        config=config,
        inventory=load_json("inventory.json"),
        reference=reference,
    )


def snapshot(body: bytes = RSS) -> RetainedSnapshot:
    digest = hashlib.sha256(body).hexdigest()
    return RetainedSnapshot(
        key=f"apcf/raw-snapshots/aws-whats-new/20260713T165900.000000Z/request-1/{digest}.bin",
        feed_name="aws-whats-new",
        observed_at=OBSERVED,
        body=body,
        body_sha256=digest,
    )


class SourceReplayTests(unittest.TestCase):
    def setUp(self):
        self.release = release()
        self.announcements = InMemoryAnnouncementStateStore()
        self.outbox = InMemoryOutboxStore()

    def plan(self, *, routes=("shared-alerts",)):
        return create_source_replay_plan(
            snapshot(),
            self.release,
            pointer_key=POINTER_KEY,
            pointer_version_id=POINTER_VERSION,
            application_version=APPLICATION_VERSION,
            max_items=200,
            max_item_characters=50_000,
            planned_at=PLANNED,
            operator="lila",
            purpose="restore one retained watcher response",
            expected_route_ids=routes,
            announcement_state=self.announcements,
            outbox=self.outbox,
            context={"deployment": "dev"},
        )

    def apply(self, plan):
        return apply_source_replay(
            snapshot(),
            self.release,
            plan,
            pointer_key=POINTER_KEY,
            pointer_version_id=POINTER_VERSION,
            application_version=APPLICATION_VERSION,
            max_items=200,
            max_item_characters=50_000,
            announcement_state=self.announcements,
            outbox=self.outbox,
            context={"deployment": "dev"},
        )

    def test_preview_is_read_only_and_names_every_missing_record(self):
        plan = self.plan()

        candidate_id = load_json("alert-candidate.json")["candidate_id"]
        self.assertIsNone(self.announcements.load(plan["result"]["announcement_ids"][0]))
        self.assertIsNone(self.outbox.get_candidate(candidate_id))
        self.assertIsNone(self.outbox.get_delivery(candidate_id))
        self.assertEqual(plan["result"]["missing_candidate_ids"], [candidate_id])
        self.assertEqual(plan["result"]["missing_delivery_ids"], [candidate_id])
        self.assertEqual(plan["result"]["missing_page_count"], 1)

    def test_apply_fills_state_without_a_feed_checkpoint_port(self):
        plan = self.plan()
        result = self.apply(plan)

        candidate_id = plan["result"]["candidate_ids"][0]
        announcement = self.announcements.load(plan["result"]["announcement_ids"][0])
        self.assertEqual(result["created_candidate_ids"], [candidate_id])
        self.assertEqual(result["created_delivery_ids"], [candidate_id])
        self.assertEqual(result["repaired_delivery_ids"], [])
        self.assertIsNotNone(self.outbox.get_candidate(candidate_id))
        self.assertIsNotNone(self.outbox.get_delivery(candidate_id))
        self.assertIsNotNone(announcement)
        assert announcement is not None
        self.assertEqual(announcement.emitted_candidate_ids, (candidate_id,))
        marker = self.announcements.load_page(result["run_id"], result["page_set_id"], 0)
        assert marker is not None
        self.assertEqual(marker.candidate_ids, (candidate_id,))

    def test_existing_candidate_and_delivery_are_suppressed(self):
        self.apply(self.plan())
        second_plan = self.plan()

        candidate_id = second_plan["result"]["candidate_ids"][0]
        self.assertEqual(second_plan["result"]["existing_candidate_ids"], [candidate_id])
        self.assertEqual(second_plan["result"]["existing_delivery_ids"], [candidate_id])
        result = self.apply(second_plan)
        self.assertEqual(result["created_candidate_ids"], [])
        self.assertEqual(result["created_delivery_ids"], [])
        self.assertEqual(result["repaired_delivery_ids"], [])
        self.assertEqual(result["reused_candidate_ids"], [candidate_id])

    def test_missing_delivery_is_repaired_from_the_stored_candidate(self):
        self.apply(self.plan())
        candidate_id = load_json("alert-candidate.json")["candidate_id"]
        stored_candidate = self.outbox.get_candidate(candidate_id)
        del self.outbox._deliveries[candidate_id]

        result = self.apply(self.plan())

        self.assertEqual(result["created_candidate_ids"], [])
        self.assertEqual(result["repaired_delivery_ids"], [candidate_id])
        repaired = self.outbox.get_delivery(candidate_id)
        assert repaired is not None
        self.assertEqual(repaired.request["candidate"], stored_candidate)

    def test_missing_delivery_from_another_release_is_refused(self):
        self.apply(self.plan())
        candidate_id = load_json("alert-candidate.json")["candidate_id"]
        stored = self.outbox.get_candidate(candidate_id)
        assert stored is not None
        changed = json.loads(json.dumps(stored))
        changed["release"]["release_id"] = "f" * 64
        self.outbox._candidates[candidate_id] = json.dumps(changed, sort_keys=True)
        del self.outbox._deliveries[candidate_id]

        with self.assertRaisesRegex(ReplayRefused, "names another release") as raised:
            self.plan()
        self.assertEqual(raised.exception.status, "release_mismatch")

    def test_changed_state_makes_a_saved_plan_stale(self):
        plan = self.plan()
        self.apply(plan)

        with self.assertRaisesRegex(ReplayRefused, "differs from the preview") as raised:
            self.apply(plan)
        self.assertEqual(raised.exception.status, "stale_plan")

    def test_route_scope_must_match_the_runtime_result(self):
        with self.assertRaisesRegex(ReplayRefused, "differ from expected routes") as raised:
            self.plan(routes=("another-route",))
        self.assertEqual(raised.exception.status, "route_scope_mismatch")

    def test_body_digest_is_checked_before_replay(self):
        with self.assertRaisesRegex(ValueError, "digest"):
            RetainedSnapshot("key", "aws-whats-new", OBSERVED, RSS, "0" * 64)


if __name__ == "__main__":
    unittest.main()
