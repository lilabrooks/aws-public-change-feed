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
    probe_release,
)
from aws_public_change_feed.releases import (  # noqa: E402
    S3ObjectStore,
    promote_pointer,
    publish_objects,
)

BUCKET = "release-bucket"
REGION = "us-east-1"
POINTER = "aws-public-change-alerting/active-versions.json"
PROMOTED = datetime(2026, 7, 13, 16, 30, tzinfo=UTC)
APPLICATION_VERSION = "0.1.0-design-fixture"


class ReleaseLoadingTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
