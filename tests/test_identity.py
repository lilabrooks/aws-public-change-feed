"""The runtime must reproduce the canonical fixture's committed identities.

The acquisition tests previously checked only that the runtime agreed with
itself: same input, same ID. That passes even when every ID is wrong. These
tests bind the runtime to the committed contract vectors instead, which is the
check that catches a normalization drift between the runtime and the fixtures.
"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from aws_public_change_feed import identity  # noqa: E402
from aws_public_change_feed.announcements import NormalizedAnnouncement, Provenance  # noqa: E402


def load(name):
    with (ROOT / "examples" / name).open(encoding="utf-8") as handle:
        return json.load(handle)


class CommittedVectorTests(unittest.TestCase):
    """Every identity in the canonical bundle, recomputed by the runtime."""

    def setUp(self):
        self.candidate = load("alert-candidate.json")
        self.request = load("delivery-request.json")
        self.manifest = load("active-versions.json")
        self.announcement = self.candidate["announcement"]

    def test_announcement_id_matches(self):
        self.assertEqual(
            identity.announcement_id(identity.canonical_public_url(self.announcement["url"])),
            self.announcement["announcement_id"],
        )

    def test_content_fingerprint_matches(self):
        self.assertEqual(
            identity.content_fingerprint(self.announcement["title"], self.announcement["summary"]),
            self.announcement["content_fingerprint"],
        )

    def test_revision_id_matches(self):
        self.assertEqual(
            identity.revision_id(self.announcement["announcement_id"], self.announcement["content_fingerprint"]),
            self.announcement["revision_id"],
        )

    def test_release_id_matches(self):
        self.assertEqual(
            identity.release_id(self.manifest["config"]["sha256"], self.manifest["inventory"]["sha256"]),
            self.manifest["release_id"],
        )

    def test_audience_fingerprint_matches(self):
        self.assertEqual(
            identity.audience_fingerprint(self.candidate["environment_ids"]),
            self.candidate["audience_fingerprint"],
        )

    def test_candidate_id_matches(self):
        self.assertEqual(
            identity.candidate_id(
                self.announcement["revision_id"],
                self.candidate["service"]["id"],
                self.candidate["risk"]["risk_type"],
                self.candidate["route_id"],
                self.candidate["audience_fingerprint"],
            ),
            self.candidate["candidate_id"],
        )

    def test_delivery_request_id_matches(self):
        self.assertEqual(
            identity.delivery_request_id(self.candidate["candidate_id"]),
            self.request["request_id"],
        )

    def test_normalized_announcement_reproduces_the_fixture_revision(self):
        # The end-to-end shape: a runtime announcement object carrying the
        # fixture's display text must produce the fixture's revision.
        normalized = NormalizedAnnouncement(
            canonical_url=self.announcement["url"],
            title=self.announcement["title"],
            summary=self.announcement["summary"],
            observed_at=None,  # type: ignore[arg-type]
            published_at=None,
            provenance=(Provenance("aws-whats-new", self.announcement["url"]),),
        )
        self.assertEqual(normalized.announcement_id, self.announcement["announcement_id"])
        self.assertEqual(normalized.content_fingerprint, self.announcement["content_fingerprint"])
        self.assertEqual(normalized.revision_id, self.announcement["revision_id"])


class IdentityTextTests(unittest.TestCase):
    def test_case_is_folded_for_identity(self):
        self.assertEqual(identity.identity_text("Amazon EKS"), "amazon eks")

    def test_capitalisation_change_does_not_mint_a_revision(self):
        first = identity.content_fingerprint("Amazon EKS update", "Body")
        second = identity.content_fingerprint("AMAZON EKS UPDATE", "body")
        self.assertEqual(first, second)

    def test_wording_change_does_mint_a_revision(self):
        first = identity.content_fingerprint("Amazon EKS update", "Body")
        second = identity.content_fingerprint("Amazon EKS update (revised)", "Body")
        self.assertNotEqual(first, second)

    def test_whitespace_and_unicode_are_normalized(self):
        self.assertEqual(identity.identity_text("Amazon EKS   update"), "amazon eks update")


class RuleIdIndependenceTests(unittest.TestCase):
    def test_renaming_a_rule_does_not_change_candidate_identity(self):
        # Chapter 04: a rule ID rename with the same risk type and evidence
        # must leave candidate identity unchanged. The rule ID is not an input.
        first = identity.candidate_id("rev", "eks", "end-of-support", "route", "audience")
        second = identity.candidate_id("rev", "eks", "end-of-support", "route", "audience")
        self.assertEqual(first, second)

    def test_changing_any_identity_field_changes_the_candidate(self):
        base = identity.candidate_id("rev", "eks", "end-of-support", "route", "audience")
        variants = {
            "revision": identity.candidate_id("rev2", "eks", "end-of-support", "route", "audience"),
            "service": identity.candidate_id("rev", "rds", "end-of-support", "route", "audience"),
            "risk type": identity.candidate_id("rev", "eks", "security", "route", "audience"),
            "route": identity.candidate_id("rev", "eks", "end-of-support", "route2", "audience"),
            "audience": identity.candidate_id("rev", "eks", "end-of-support", "route", "audience2"),
        }
        for label, value in variants.items():
            with self.subTest(label=label):
                self.assertNotEqual(base, value)


class DispatchIdentityTests(unittest.TestCase):
    def test_generation_changes_the_dispatch_id(self):
        request = "a" * 64
        self.assertNotEqual(identity.queue_dispatch_id(request, 1), identity.queue_dispatch_id(request, 2))

    def test_malformed_inputs_are_rejected(self):
        with self.assertRaises(ValueError):
            identity.queue_dispatch_id("not-a-digest", 1)
        with self.assertRaises(ValueError):
            identity.queue_dispatch_id("a" * 64, 0)
        with self.assertRaises(ValueError):
            identity.queue_dispatch_id("a" * 64, True)

    def test_null_framed_values_reject_null_characters(self):
        with self.assertRaises(ValueError):
            identity.digest_parts("prefix", "value\0with-null")


class SingleImplementationTests(unittest.TestCase):
    """Chapter 04 requires one framing helper for runtime and test vectors."""

    # Every identity helper the validator needs. `digest_parts` and
    # `queue_dispatch_id` are absent on purpose: the validator no longer
    # derives digests by hand, and nothing in the contract fixtures carries a
    # dispatch ID yet, so importing either would be dead weight.
    SHARED = (
        "announcement_id",
        "audience_fingerprint",
        "candidate_id",
        "canonical_public_url",
        "content_fingerprint",
        "delivery_request_id",
        "identity_text",
        "release_id",
        "revision_id",
    )

    # Where the candidate rules live. `validate_config` delegates the
    # release-relative checks to `semantics`, which the delivery worker also
    # calls, so both modules are inspected rather than only the validator: the
    # invariant is that no module rebinds one of these names to its own
    # implementation, and naming a single module would let a copy appear in
    # whichever one the test does not look at.
    CONSUMERS = ("validate_config", "aws_public_change_feed.semantics")

    def test_every_consumer_uses_the_runtime_helpers(self):
        import importlib

        seen: set[str] = set()
        for module_name in self.CONSUMERS:
            module = importlib.import_module(module_name)
            for name in self.SHARED:
                if not hasattr(module, name):
                    continue
                seen.add(name)
                with self.subTest(module=module_name, helper=name):
                    self.assertIs(getattr(module, name), getattr(identity, name))

        # Every shared helper must be accounted for somewhere, or a rename
        # could quietly drop one out of both modules and still pass.
        self.assertEqual(seen, set(self.SHARED))

    # The null-framed domain prefixes. Each must exist in exactly one place;
    # a second copy is how the runtime and the fixtures drift apart.
    FRAMING_PREFIXES = (
        "announcement-content:v1",
        "announcement-revision:v1",
        "candidate-audience:v1",
        "candidate:v3",
        "delivery-request:v2",
        "queue-dispatch:v1",
        "release:v1",
    )

    # Both files that recompute candidate identities. The candidate rules
    # moved from the validator into `semantics`, and a source-scanning guard
    # that still named only the validator would have stopped covering the code
    # it was written to protect at the moment that code moved.
    CONSUMER_SOURCES = (
        "scripts/validate_config.py",
        "src/aws_public_change_feed/semantics.py",
    )

    def test_framing_prefixes_live_only_in_the_identity_module(self):
        identity_source = (ROOT / "src/aws_public_change_feed/identity.py").read_text(encoding="utf-8")
        for prefix in self.FRAMING_PREFIXES:
            with self.subTest(prefix=prefix):
                self.assertIn(prefix, identity_source)
                for relative in self.CONSUMER_SOURCES:
                    with self.subTest(source=relative):
                        self.assertNotIn(prefix, (ROOT / relative).read_text(encoding="utf-8"))

    def test_no_consumer_frames_a_digest_of_its_own(self):
        # Hashing release artifact bytes stays: that verifies a file against
        # its manifest entry and is not identity framing.
        for relative in self.CONSUMER_SOURCES:
            with self.subTest(source=relative):
                self.assertNotIn("digest_parts(", (ROOT / relative).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
