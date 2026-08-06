"""Loading the active release, and refusing the releases this build cannot use.

The binding test is `test_the_loaded_reference_is_the_committed_candidate_release`:
publishing the committed bundle and loading it back must produce the exact
`release` block `examples/alert-candidate.json` carries. That block is what a
candidate embeds, so binding it to the committed fixture is what stops the
loader and the candidate contract from drifting apart.

The refusals get their own tests because each one is a different failure. An
unsupported schema version is intact bytes this build would misread; a hash
mismatch is bytes that are not what was published. Collapsing them would make
an operator read "release failed" and learn nothing about which.
"""

import json
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path

import boto3
import yaml
from moto import mock_aws

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aws_public_change_feed.loading import (  # noqa: E402
    SUPPORTED_CONFIG_SCHEMA_VERSIONS,
    IncompatibleRelease,
    ReleaseIntegrityError,
    load_active_release,
    load_release_version,
    probe_release,
)
from aws_public_change_feed.releases import (  # noqa: E402
    PromotionSuperseded,
    S3ObjectStore,
    promote_pointer,
    publish_objects,
)

BUCKET = "release-bucket"
REGION = "us-east-1"
POINTER = "aws-public-change-alerting/active-versions.json"
PROMOTED = datetime(2026, 7, 13, 16, 30, tzinfo=UTC)
APPLICATION_VERSION = "0.1.0-design-fixture"


class ReleaseFixture(unittest.TestCase):
    """Shared setup: a versioned bucket and the committed bundle's bytes.

    Held apart from the tests so `RollbackTests` can reuse it without
    inheriting and re-running every loading test.
    """

    def setUp(self):
        self.mock = mock_aws()
        self.mock.start()
        self.addCleanup(self.mock.stop)
        self.client = boto3.client("s3", region_name=REGION)
        self.client.create_bucket(Bucket=BUCKET)
        self.client.put_bucket_versioning(Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})
        self.store = S3ObjectStore(self.client, BUCKET)
        with (ROOT / "examples" / "deployment.yaml").open(encoding="utf-8") as handle:
            self.deployment = yaml.safe_load(handle)
        self.config_body = (ROOT / "examples" / "config.yaml").read_bytes()
        self.inventory_body = (ROOT / "examples" / "inventory.json").read_bytes()

    def publish_and_promote(self, config_body=None, inventory_body=None, pointer_edit=None):
        """Publish the committed bundle and promote it, as the publisher does."""

        artifacts = publish_objects(
            self.store,
            config_body=self.config_body if config_body is None else config_body,
            inventory_body=self.inventory_body if inventory_body is None else inventory_body,
            config_schema_version=yaml.safe_load(self.config_body)["version"],
            inventory_schema_version=json.loads(self.inventory_body)["schema_version"],
            release_prefix=self.deployment["release_prefix"],
            config_filename=self.deployment["config_filename"],
            inventory_filename=self.deployment["inventory_filename"],
        )
        document = artifacts.pointer_document(PROMOTED)
        if pointer_edit is not None:
            document = pointer_edit(document)
        promote_pointer(
            self.store,
            pointer_key=POINTER,
            document=json.dumps(document).encode(),
            observed=None,
        )
        return artifacts

    def load(self):
        return load_active_release(
            self.store,
            pointer_key=POINTER,
            application_version=APPLICATION_VERSION,
        )


class ReleaseLoadingTests(ReleaseFixture):
    # --- The round trip --------------------------------------------------

    def test_the_loaded_reference_is_the_committed_candidate_release(self):
        self.publish_and_promote()
        committed = json.loads((ROOT / "examples" / "alert-candidate.json").read_text(encoding="utf-8"))["release"]

        loaded = self.load()

        # Version IDs are assigned by the store, so the committed placeholders
        # are substituted; every other field must match exactly.
        rebuilt = json.loads(json.dumps(loaded.reference))
        for name in ("config", "inventory"):
            rebuilt[name]["version_id"] = committed[name]["version_id"]
        self.assertEqual(rebuilt, committed)

    def test_the_loaded_documents_are_the_committed_documents(self):
        self.publish_and_promote()

        loaded = self.load()

        self.assertEqual(loaded.config, yaml.safe_load(self.config_body))
        self.assertEqual(loaded.inventory, json.loads(self.inventory_body))
        self.assertEqual(loaded.release_id, json.loads(json.dumps(loaded.reference))["release_id"])

    def test_loading_reads_the_pinned_version_not_the_current_object(self):
        """A later write at the same key must not change what the pointer names.

        Release keys are immutable by policy, not by the store, so this is the
        property that makes the pointer meaningful: the runtime reads the exact
        version, and an object appearing at that key afterwards is not it.
        """

        artifacts = self.publish_and_promote()
        self.client.put_object(Bucket=BUCKET, Key=artifacts.config.key, Body=b"version: 4\nreplaced: true\n")

        loaded = self.load()

        self.assertEqual(loaded.config, yaml.safe_load(self.config_body))

    # --- Refusals --------------------------------------------------------

    def test_an_unsupported_config_schema_version_is_refused(self):
        unsupported = max(SUPPORTED_CONFIG_SCHEMA_VERSIONS) + 1

        def bump(document):
            return {**document, "config": {**document["config"], "schema_version": unsupported}}

        self.publish_and_promote(pointer_edit=bump)

        with self.assertRaisesRegex(IncompatibleRelease, "config schema_version"):
            self.load()

    def test_an_unsupported_pointer_schema_version_is_refused(self):
        def bump(document):
            return {**document, "schema_version": 99}

        self.publish_and_promote(pointer_edit=bump)

        with self.assertRaisesRegex(IncompatibleRelease, "active pointer schema_version"):
            self.load()

    def test_compatibility_is_checked_before_any_release_object_is_read(self):
        """An unsupported release costs one read, and fails on its version.

        If the objects were fetched first, an incompatible release with a
        missing object would fail as an integrity error, which points an
        operator at the wrong thing.
        """

        def bump(document):
            return {**document, "config": {**document["config"], "schema_version": 99}}

        artifacts = self.publish_and_promote(pointer_edit=bump)
        # The exact version has to go, not just the current object: deleting
        # without a version ID leaves a delete marker, and a read by version
        # still succeeds behind it. An earlier draft of this test did that and
        # passed with the checks in either order, proving nothing.
        self.client.delete_object(
            Bucket=BUCKET,
            Key=artifacts.config.key,
            VersionId=artifacts.config.version_id,
        )

        # Both failures are now reachable. Which one arrives is the ordering.
        with self.assertRaises(IncompatibleRelease):
            self.load()

    def test_a_tampered_release_object_is_refused(self):
        """The pointer records what should be at the version; the object is wrong."""

        artifacts = self.publish_and_promote()
        # Overwrite the exact version the pointer pins, which S3 versioning
        # would not permit; the fake write is the only way to reach the branch.
        original = self.store.read(artifacts.config.key, artifacts.config.version_id)

        class TamperedRead(S3ObjectStore):
            def read(self, key, version_id=None):
                stored = super().read(key, version_id)
                if key == artifacts.config.key and version_id is not None:
                    return type(stored)(body=b"tampered", etag=stored.etag, version_id=stored.version_id)
                return stored

        with self.assertRaisesRegex(ReleaseIntegrityError, "config at the pinned version hashes to"):
            load_active_release(
                TamperedRead(self.client, BUCKET),
                pointer_key=POINTER,
                application_version=APPLICATION_VERSION,
            )
        self.assertEqual(original.body, self.config_body)

    def test_a_pointer_naming_a_release_its_objects_do_not_derive_is_refused(self):
        """The release ID is a digest of the two hashes, so it is checkable.

        Verifying the objects against the pointer is only half the check. The
        pointer can be internally inconsistent, and the ID is what every
        candidate embeds, so a renamed release would put an ID in the contract
        that belongs to nothing. `validate_config.validate_manifest` makes the
        same comparison; trusting the stored value here diverged from it.
        """

        def rename(document):
            return {**document, "release_id": "f" * 64}

        self.publish_and_promote(pointer_edit=rename)

        with self.assertRaisesRegex(ReleaseIntegrityError, "but the objects it pins derive"):
            self.load()

    def test_a_pointer_missing_a_reference_field_is_refused_by_name(self):
        """A corrupt pointer must not surface as a `KeyError` naming one key.

        Reaching into an unvalidated document field by field reports the first
        thing that happened to be absent and says nothing about the release
        being unusable.
        """

        def strip(document):
            reference = {key: value for key, value in document["config"].items() if key != "sha256"}
            return {**document, "config": reference}

        self.publish_and_promote(pointer_edit=strip)

        with self.assertRaisesRegex(IncompatibleRelease, "config reference sha256 is missing"):
            self.load()

    def test_a_pinned_document_that_does_not_parse_is_refused(self):
        """Hash-valid bytes that are not YAML are reachable.

        `publish_objects` never parses what it writes, so a publisher can put
        unparseable content at a valid hash. Left unwrapped this raised a raw
        `yaml.ParserError`, a fourth untyped refusal beside the documented ones.
        """

        self.publish_and_promote(config_body=b"key: [unclosed\n")

        with self.assertRaisesRegex(IncompatibleRelease, "config at the pinned version does not parse"):
            self.load()

    def test_a_missing_pointer_is_refused(self):
        with self.assertRaisesRegex(ReleaseIntegrityError, "no active release pointer"):
            self.load()

    def test_a_pointer_that_is_not_json_is_refused(self):
        self.client.put_object(Bucket=BUCKET, Key=POINTER, Body=b"not json")

        with self.assertRaisesRegex(IncompatibleRelease, "not JSON"):
            self.load()

    def test_a_pinned_object_that_vanished_is_an_integrity_failure(self):
        artifacts = self.publish_and_promote()
        self.client.delete_object(
            Bucket=BUCKET,
            Key=artifacts.inventory.key,
            VersionId=artifacts.inventory.version_id,
        )

        with self.assertRaisesRegex(ReleaseIntegrityError, "inventory version pinned by the pointer is missing"):
            self.load()

    # --- Step 8 ----------------------------------------------------------

    def test_the_probe_accepts_the_release_it_just_promoted(self):
        artifacts = self.publish_and_promote()

        loaded = probe_release(
            self.store,
            pointer_key=POINTER,
            application_version=APPLICATION_VERSION,
            expected_release_id=artifacts.release_id,
        )

        self.assertEqual(loaded.release_id, artifacts.release_id)

    def test_the_probe_refuses_a_pointer_that_moved_on(self):
        """A probe reporting on someone else's release proves nothing."""

        self.publish_and_promote()

        with self.assertRaisesRegex(ReleaseIntegrityError, "probe expected the pointer to name"):
            probe_release(
                self.store,
                pointer_key=POINTER,
                application_version=APPLICATION_VERSION,
                expected_release_id="a" * 64,
            )


class RollbackTests(ReleaseFixture):
    """ADR-019's rollback promotion, and the runbook procedure around it.

    Rollback is not a separate write path. It reads a retained pointer version,
    verifies everything it pins, and writes those references forward through
    the same `If-Match` promotion the publisher uses. The tests follow the
    runbook: verify the release being restored, then promote it.
    """

    ROLLED_BACK = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)

    def publish_second_release(self):
        """Promote a second release over the first, leaving one to roll back to."""

        first = self.publish_and_promote()
        historical = self.store.read(POINTER)

        edited = self.config_body.replace(b"max_title_characters: 150", b"max_title_characters: 160")
        self.assertNotEqual(edited, self.config_body)
        second = publish_objects(
            self.store,
            config_body=edited,
            inventory_body=self.inventory_body,
            config_schema_version=4,
            inventory_schema_version=3,
            release_prefix=self.deployment["release_prefix"],
            config_filename=self.deployment["config_filename"],
            inventory_filename=self.deployment["inventory_filename"],
        )
        promote_pointer(
            self.store,
            pointer_key=POINTER,
            document=json.dumps(second.pointer_document(PROMOTED)).encode(),
            observed=historical,
        )
        return first, second, historical

    def test_a_retained_pointer_version_still_loads_after_being_replaced(self):
        first, second, historical = self.publish_second_release()

        self.assertEqual(self.load().release_id, second.release_id)
        restored = load_release_version(
            self.store,
            pointer_key=POINTER,
            version_id=historical.version_id,
            application_version=APPLICATION_VERSION,
        )
        self.assertEqual(restored.release_id, first.release_id)

    def test_rollback_restores_the_prior_release_through_the_conditional_path(self):
        first, second, historical = self.publish_second_release()
        restored = load_release_version(
            self.store,
            pointer_key=POINTER,
            version_id=historical.version_id,
            application_version=APPLICATION_VERSION,
        )

        observed = self.store.read(POINTER)
        promotion = promote_pointer(
            self.store,
            pointer_key=POINTER,
            document=json.dumps(restored.forward_document(self.ROLLED_BACK)).encode(),
            observed=observed,
        )

        self.assertEqual(promotion.release_id, first.release_id)
        self.assertEqual(promotion.prior_release_id, second.release_id)
        # A new version, never a reused one: the restored pointer is a third
        # version, and the one it restores is still retained.
        self.assertNotIn(promotion.new_version_id, {historical.version_id, observed.version_id})
        versions = self.client.list_object_versions(Bucket=BUCKET, Prefix=POINTER)["Versions"]
        self.assertEqual(len(versions), 3)
        self.assertEqual(self.load().release_id, first.release_id)

    def test_the_restored_pointer_is_not_the_historical_bytes(self):
        """ "never republishes historical bytes unchanged" is checkable."""

        _, _, historical = self.publish_second_release()
        restored = load_release_version(
            self.store,
            pointer_key=POINTER,
            version_id=historical.version_id,
            application_version=APPLICATION_VERSION,
        )

        forward = json.dumps(restored.forward_document(self.ROLLED_BACK)).encode()

        self.assertNotEqual(forward, historical.body)
        self.assertEqual(json.loads(forward)["release_id"], json.loads(historical.body)["release_id"])
        self.assertNotEqual(json.loads(forward)["promoted_at"], json.loads(historical.body)["promoted_at"])

    def test_reusing_the_restored_promotion_time_is_refused(self):
        """The fresh `promoted_at` is load-bearing, so reusing it is refused.

        ADR-019: identical bytes reproduce the historical ETag, and a
        concurrent publisher still holding it would find its precondition
        satisfied against a pointer that had moved away and come back.
        """

        _, _, historical = self.publish_second_release()
        restored = load_release_version(
            self.store,
            pointer_key=POINTER,
            version_id=historical.version_id,
            application_version=APPLICATION_VERSION,
        )

        with self.assertRaisesRegex(ValueError, "must record a fresh promoted_at"):
            restored.forward_document(PROMOTED)

    def test_a_rollback_against_a_stale_observation_is_refused(self):
        """Rollback is a promotion, so it loses the same race a promotion does."""

        _, _, historical = self.publish_second_release()
        restored = load_release_version(
            self.store,
            pointer_key=POINTER,
            version_id=historical.version_id,
            application_version=APPLICATION_VERSION,
        )
        stale = self.store.read(POINTER)

        third = publish_objects(
            self.store,
            config_body=self.config_body.replace(b"max_title_characters: 150", b"max_title_characters: 170"),
            inventory_body=self.inventory_body,
            config_schema_version=4,
            inventory_schema_version=3,
            release_prefix=self.deployment["release_prefix"],
            config_filename=self.deployment["config_filename"],
            inventory_filename=self.deployment["inventory_filename"],
        )
        promote_pointer(
            self.store,
            pointer_key=POINTER,
            document=json.dumps(third.pointer_document(PROMOTED)).encode(),
            observed=stale,
        )

        with self.assertRaises(PromotionSuperseded):
            promote_pointer(
                self.store,
                pointer_key=POINTER,
                document=json.dumps(restored.forward_document(self.ROLLED_BACK)).encode(),
                observed=stale,
            )

    def test_a_release_whose_objects_were_deleted_cannot_be_rolled_back_to(self):
        """Runbook step 2 exists because a retained pointer is not a usable release."""

        first, _, historical = self.publish_second_release()
        self.client.delete_object(
            Bucket=BUCKET,
            Key=first.config.key,
            VersionId=first.config.version_id,
        )

        with self.assertRaisesRegex(ReleaseIntegrityError, "config version pinned by the pointer is missing"):
            load_release_version(
                self.store,
                pointer_key=POINTER,
                version_id=historical.version_id,
                application_version=APPLICATION_VERSION,
            )

    def test_a_missing_retained_version_is_reported_as_such(self):
        self.publish_and_promote()

        with self.assertRaisesRegex(ReleaseIntegrityError, "retained pointer version .* is missing"):
            load_release_version(
                self.store,
                pointer_key=POINTER,
                version_id="no-such-version",
                application_version=APPLICATION_VERSION,
            )


if __name__ == "__main__":
    unittest.main()
