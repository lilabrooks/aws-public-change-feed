"""Announcement state: provenance merging and revision tracking.

Chapter 04 and ADR-013 fix the rules this file checks. A normalized title or
summary change makes a revision; publication-timestamp corrections and new
provenance alone update state without delivery; a revision is appended only when
absent; and `is_update` is true when state already holds an earlier content
revision for the same announcement.

The fixture case rebuilds the announcement committed in
`examples/alert-candidate.json`, so the record's identity fields are bound to the
contract rather than to the runtime's own output.
"""

import json
import sys
import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aws_public_change_feed.announcements import (  # noqa: E402
    NormalizedAnnouncement,
    Provenance,
)
from aws_public_change_feed.state import (  # noqa: E402
    AnnouncementRecord,
    InMemoryAnnouncementStateStore,
    observe,
)


def load_json(name):
    with (ROOT / "examples" / name).open(encoding="utf-8") as handle:
        return json.load(handle)


def parse_timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def stored(store, key):
    """Load a record the test knows exists."""

    record = store.load(key)
    assert record is not None
    return record


class AnnouncementStateTestCase(unittest.TestCase):
    def setUp(self):
        self.candidate = load_json("alert-candidate.json")
        self.source = self.candidate["announcement"]
        self.store = InMemoryAnnouncementStateStore()
        self.observed_at = parse_timestamp(self.source["observed_at"])

    def announcement(self, *, title=None, summary=None, published_at=..., provenance=None, observed_at=None):
        """The committed announcement, optionally varied for one condition."""

        return NormalizedAnnouncement(
            canonical_url=self.source["url"],
            title=self.source["title"] if title is None else title,
            summary=self.source["summary"] if summary is None else summary,
            observed_at=self.observed_at if observed_at is None else observed_at,
            published_at=parse_timestamp(self.source["published_at"]) if published_at is ... else published_at,
            provenance=(
                tuple(
                    Provenance(feed_name=item["feed_name"], item_url=item["source_item_url"])
                    for item in self.source["provenance"]
                )
                if provenance is None
                else provenance
            ),
        )


class FirstSightingTests(AnnouncementStateTestCase):
    def test_reproduces_the_committed_identity(self):
        result = observe(self.store, self.announcement())
        record = result.record

        self.assertEqual(record.announcement_id, self.source["announcement_id"])
        self.assertEqual(record.revision_id, self.source["revision_id"])
        self.assertEqual(record.content_fingerprint, self.source["content_fingerprint"])
        self.assertEqual(record.revision_ids, (self.source["revision_id"],))
        self.assertEqual(record.canonical_url, self.source["url"])

    def test_is_update_matches_the_committed_flag(self):
        result = observe(self.store, self.announcement())
        self.assertEqual(result.is_update, self.source["is_update"])
        self.assertFalse(result.is_update, msg="a first sighting has no earlier revision")

    def test_classifies_as_new(self):
        result = observe(self.store, self.announcement())
        self.assertTrue(result.is_new_announcement)
        self.assertTrue(result.is_new_revision)
        self.assertFalse(result.provenance_only)

    def test_first_and_last_observation_start_equal(self):
        record = observe(self.store, self.announcement()).record
        self.assertEqual(record.first_observed_at, record.last_observed_at)


class ProvenanceOnlyTests(AnnouncementStateTestCase):
    def test_a_second_feed_merges_provenance_without_a_revision(self):
        observe(self.store, self.announcement())
        second = self.announcement(
            provenance=(Provenance(feed_name="aws-news-blog", item_url=self.source["url"]),),
            observed_at=self.observed_at + timedelta(minutes=5),
        )

        result = observe(self.store, second)

        self.assertTrue(result.provenance_only, msg="ADR-013: new provenance alone must not deliver")
        self.assertFalse(result.is_new_revision)
        self.assertEqual(result.record.revision_ids, (self.source["revision_id"],))
        self.assertEqual(
            [entry.feed_name for entry in result.record.provenance],
            ["aws-news-blog", "aws-whats-new"],
            msg="provenance is merged and sorted, not replaced",
        )

    def test_a_publication_timestamp_correction_does_not_deliver(self):
        observe(self.store, self.announcement())
        corrected = parse_timestamp(self.source["published_at"]) - timedelta(hours=2)

        result = observe(self.store, self.announcement(published_at=corrected))

        self.assertTrue(result.provenance_only, msg="chapter 04: a timestamp correction is not delivery work")
        self.assertEqual(result.record.published_at, corrected.isoformat())
        self.assertEqual(result.record.revision_ids, (self.source["revision_id"],))

    def test_repeating_an_identical_sighting_changes_nothing_material(self):
        first = observe(self.store, self.announcement()).record
        again = observe(self.store, self.announcement()).record
        self.assertEqual(first, again)


class RevisionTests(AnnouncementStateTestCase):
    def test_a_title_change_makes_a_revision(self):
        observe(self.store, self.announcement())
        result = observe(self.store, self.announcement(title="Amazon EKS Kubernetes version 1.35 available"))

        self.assertTrue(result.is_new_revision)
        self.assertTrue(result.is_update, msg="state already held an earlier revision")
        self.assertFalse(result.provenance_only)
        self.assertEqual(len(result.record.revision_ids), 2)
        self.assertEqual(result.record.revision_ids[0], self.source["revision_id"])
        self.assertEqual(result.record.revision_id, result.record.revision_ids[-1])

    def test_a_summary_change_makes_a_revision(self):
        observe(self.store, self.announcement())
        result = observe(self.store, self.announcement(summary="Amazon EKS now supports a different thing."))
        self.assertTrue(result.is_new_revision)
        self.assertTrue(result.is_update)

    def test_the_stored_content_follows_the_latest_revision(self):
        observe(self.store, self.announcement())
        edited = "Amazon EKS Kubernetes version 1.35 available"
        observe(self.store, self.announcement(title=edited))

        record = stored(self.store, self.source["announcement_id"])
        self.assertEqual(record.title, edited)
        self.assertNotEqual(record.content_fingerprint, self.source["content_fingerprint"])

    def test_a_revision_is_appended_only_when_absent(self):
        original = self.announcement()
        edited = self.announcement(title="Amazon EKS Kubernetes version 1.35 available")

        observe(self.store, original)
        observe(self.store, edited)
        reverted = observe(self.store, original)

        self.assertEqual(
            len(reverted.record.revision_ids),
            2,
            msg="ADR-013 appends a revision only when absent, so a revert adds nothing",
        )
        self.assertFalse(reverted.is_new_revision)
        self.assertFalse(
            reverted.provenance_only,
            msg="the content did change from the latest revision, so this is not provenance-only",
        )
        self.assertEqual(reverted.record.revision_id, self.source["revision_id"])

    def test_provenance_only_repeats_never_set_is_update(self):
        observe(self.store, self.announcement())
        result = observe(self.store, self.announcement())
        self.assertFalse(
            result.is_update,
            msg="one known revision, seen twice, is not an earlier revision",
        )


class ObservationWindowTests(AnnouncementStateTestCase):
    def test_out_of_order_replay_keeps_the_true_window(self):
        later = self.observed_at + timedelta(hours=3)
        earlier = self.observed_at - timedelta(hours=3)

        observe(self.store, self.announcement(observed_at=self.observed_at))
        observe(self.store, self.announcement(observed_at=later))
        record = observe(self.store, self.announcement(observed_at=earlier)).record

        self.assertEqual(record.first_observed_at, earlier.isoformat())
        self.assertEqual(
            record.last_observed_at,
            later.isoformat(),
            msg="a late-arriving earlier sighting must not rewind the window",
        )


class EmissionRecordTests(AnnouncementStateTestCase):
    def test_emitted_candidates_and_releases_are_sorted_and_deduplicated(self):
        record = observe(self.store, self.announcement()).record
        release = self.candidate["release"]["release_id"]

        once = record.with_emission([self.candidate["candidate_id"]], release)
        twice = once.with_emission([self.candidate["candidate_id"]], release)

        self.assertEqual(once.emitted_candidate_ids, (self.candidate["candidate_id"],))
        self.assertEqual(once.release_ids, (release,))
        self.assertEqual(
            twice,
            once,
            msg="a repeated invocation must not grow the emitted sets",
        )

    def test_emission_does_not_disturb_identity(self):
        record = observe(self.store, self.announcement()).record
        emitted = record.with_emission(["a" * 64])
        self.assertEqual(emitted.revision_id, record.revision_id)
        self.assertEqual(emitted.revision_ids, record.revision_ids)
        self.assertEqual(emitted.content_fingerprint, record.content_fingerprint)


class RecordShapeTests(AnnouncementStateTestCase):
    def test_the_record_carries_the_chapter_02_fields(self):
        record = observe(self.store, self.announcement()).record
        for name in (
            "canonical_url",
            "content_fingerprint",
            "revision_ids",
            "title",
            "summary",
            "first_observed_at",
            "last_observed_at",
            "published_at",
            "provenance",
            "emitted_candidate_ids",
            "release_ids",
        ):
            with self.subTest(field=name):
                self.assertTrue(hasattr(record, name))

    def test_records_are_immutable(self):
        record = observe(self.store, self.announcement()).record
        with self.assertRaises(FrozenInstanceError):
            record.title = "mutated"  # type: ignore[misc]

    def test_an_absent_publication_time_is_kept_absent(self):
        record = observe(self.store, self.announcement(published_at=None)).record
        self.assertIsNone(record.published_at)

    def test_the_record_type_is_constructible_directly(self):
        record = AnnouncementRecord(
            announcement_id="a" * 64,
            canonical_url="https://aws.amazon.com/x/",
            content_fingerprint="b" * 64,
            revision_id="c" * 64,
            revision_ids=("c" * 64,),
            title="t",
            summary="s",
            first_observed_at="2026-07-13T16:59:00+00:00",
            last_observed_at="2026-07-13T16:59:00+00:00",
        )
        self.assertEqual(record.provenance, ())
        self.assertEqual(record.emitted_candidate_ids, ())


if __name__ == "__main__":
    unittest.main()
