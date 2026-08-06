"""Release publication and promotion against the committed contract.

Two kinds of test live here, and the split is the milestone-2 decision recorded
in ADR-019.

Against `moto`: everything a single request expresses. The adapter's error
translation, the create-and-verify sequence, adoption of identical bytes,
promotion with a matching ETag, and the refusal of a stale one.

Against an injecting fake: the outcomes no backend produces on demand. 409 is a
genuine race, and moto does not evaluate conditional writes atomically, so
neither the indeterminate outcome nor concurrent promotion can be provoked.
Those branches carry the publisher's logic, so the fake raises the outcome and
the test measures the response.

The strongest test here is neither: `test_publishing_the_committed_bundle`
rebuilds `examples/active-versions.json` from the bytes of `examples/config.yaml`
and `examples/inventory.json`. The publisher is bound to the committed contract
rather than to itself, the way `test_pipeline.py` rebuilds the candidate from
raw feed bytes.
"""

import hashlib
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
sys.path.insert(0, str(ROOT / "scripts"))

import validate_config as validator  # noqa: E402

from aws_public_change_feed.releases import (  # noqa: E402
    CREATE_CONFLICT_ATTEMPTS,
    ObjectMissing,
    PointerVanished,
    PreconditionFailed,
    PromotionSuperseded,
    S3ObjectStore,
    StoredObject,
    WriteConflict,
    promote_pointer,
    publish_objects,
    release_keys,
)

BUCKET = "release-bucket"
REGION = "us-east-1"
PREFIX = "aws-public-change-alerting/releases"
POINTER = "aws-public-change-alerting/active-versions.json"
PROMOTED = datetime(2026, 7, 13, 16, 30, tzinfo=UTC)


def load_deployment():
    with (ROOT / "examples" / "deployment.yaml").open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


class RecordingStore:
    """An `ObjectStore` that raises scripted outcomes.

    Used for the outcomes a backend cannot be made to produce. Each entry in
    `raises` is consumed by one call, so a bounded retry can be observed
    exhausting its attempts.
    """

    def __init__(self, raises=None, bodies=None):
        self.raises = list(raises or [])
        self.bodies = dict(bodies or {})
        self.calls = []

    def _next(self, label):
        self.calls.append(label)
        if self.raises:
            outcome = self.raises.pop(0)
            if outcome is not None:
                raise outcome

    def create(self, key, body):
        self._next(f"create:{key}")
        self.bodies[key] = body
        return "version-created"

    def read(self, key, version_id=None):
        self.calls.append(f"read:{key}:{version_id or 'current'}")
        if key not in self.bodies:
            raise ObjectMissing(key)
        return StoredObject(body=self.bodies[key], etag='"etag"', version_id=version_id or "version-current")

    def replace(self, key, body, *, if_match):
        self._next(f"replace:{key}")
        self.bodies[key] = body
        return "version-replaced"


class PublicationAgainstS3Tests(unittest.TestCase):
    """Chapter 03 steps 3 to 5, against the mock the decision approved."""

    def setUp(self):
        self.mock = mock_aws()
        self.mock.start()
        self.addCleanup(self.mock.stop)
        self.client = boto3.client("s3", region_name=REGION)
        self.client.create_bucket(Bucket=BUCKET)
        self.client.put_bucket_versioning(Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})
        self.store = S3ObjectStore(self.client, BUCKET)

    def publish(self, config=b"config: one\n", inventory=b'{"schema_version": 3}'):
        return publish_objects(
            self.store,
            config_body=config,
            inventory_body=inventory,
            config_schema_version=4,
            inventory_schema_version=3,
            release_prefix=PREFIX,
            config_filename="config.yaml",
            inventory_filename="inventory.json",
        )

    def test_publication_writes_both_objects_under_the_release_id(self):
        artifacts = self.publish()

        config_key, inventory_key = release_keys(PREFIX, artifacts.release_id, "config.yaml", "inventory.json")
        self.assertEqual(artifacts.config.key, config_key)
        self.assertEqual(artifacts.inventory.key, inventory_key)
        self.assertIn(artifacts.release_id, config_key)
        self.assertFalse(artifacts.config.adopted)
        self.assertEqual(
            self.client.get_object(Bucket=BUCKET, Key=config_key)["Body"].read(),
            b"config: one\n",
        )

    def test_republishing_identical_bytes_adopts_the_existing_objects(self):
        first = self.publish()
        second = self.publish()

        self.assertEqual(first.release_id, second.release_id)
        self.assertTrue(second.config.adopted)
        self.assertTrue(second.inventory.adopted)
        self.assertEqual(first.config.version_id, second.config.version_id)
        # Adoption reuses the version rather than writing a second one.
        versions = self.client.list_object_versions(Bucket=BUCKET, Prefix=first.config.key)["Versions"]
        self.assertEqual(len(versions), 1)

    def test_an_out_of_band_write_at_a_release_key_fails_publication(self):
        artifacts = self.publish()
        self.client.put_object(Bucket=BUCKET, Key=artifacts.config.key, Body=b"tampered")

        with self.assertRaisesRegex(ValueError, "already exists with different content"):
            self.publish()

    def test_promotion_writes_the_pointer_and_records_both_versions(self):
        artifacts = self.publish()
        document = json.dumps(artifacts.pointer_document(PROMOTED)).encode()

        first = promote_pointer(
            self.store,
            pointer_key=POINTER,
            document=document,
            release_identifier=artifacts.release_id,
            observed=None,
        )
        self.assertIsNone(first.prior_version_id)
        self.assertTrue(first.new_version_id)

        observed = self.store.read(POINTER)
        second = promote_pointer(
            self.store,
            pointer_key=POINTER,
            document=json.dumps({**artifacts.pointer_document(PROMOTED), "release_id": "b" * 64}).encode(),
            release_identifier="b" * 64,
            observed=observed,
        )
        self.assertEqual(second.prior_version_id, observed.version_id)
        self.assertEqual(second.prior_release_id, artifacts.release_id)
        self.assertNotEqual(second.new_version_id, observed.version_id)

    def test_a_stale_observation_is_refused_and_names_both_releases(self):
        artifacts = self.publish()
        document = json.dumps(artifacts.pointer_document(PROMOTED)).encode()
        promote_pointer(
            self.store,
            pointer_key=POINTER,
            document=document,
            release_identifier=artifacts.release_id,
            observed=None,
        )
        stale = self.store.read(POINTER)

        winner = json.dumps({**artifacts.pointer_document(PROMOTED), "release_id": "c" * 64}).encode()
        promote_pointer(
            self.store,
            pointer_key=POINTER,
            document=winner,
            release_identifier="c" * 64,
            observed=stale,
        )

        with self.assertRaises(PromotionSuperseded) as raised:
            promote_pointer(
                self.store,
                pointer_key=POINTER,
                document=document,
                release_identifier=artifacts.release_id,
                observed=stale,
            )
        self.assertEqual(raised.exception.promoting, artifacts.release_id)
        self.assertEqual(raised.exception.observed, "c" * 64)

    def test_a_first_promotion_that_races_another_is_refused(self):
        artifacts = self.publish()
        document = json.dumps(artifacts.pointer_document(PROMOTED)).encode()
        self.client.put_object(Bucket=BUCKET, Key=POINTER, Body=json.dumps({"release_id": "d" * 64}).encode())

        # `observed=None` says this publisher read no pointer. One exists, so
        # the create's 412 means restart from a fresh read, never fall back.
        with self.assertRaises(PromotionSuperseded) as raised:
            promote_pointer(
                self.store,
                pointer_key=POINTER,
                document=document,
                release_identifier=artifacts.release_id,
                observed=None,
            )
        self.assertEqual(raised.exception.observed, "d" * 64)

    def test_a_deleted_pointer_raises_rather_than_being_recreated(self):
        artifacts = self.publish()
        document = json.dumps(artifacts.pointer_document(PROMOTED)).encode()
        promote_pointer(
            self.store,
            pointer_key=POINTER,
            document=document,
            release_identifier=artifacts.release_id,
            observed=None,
        )
        observed = self.store.read(POINTER)
        self.client.delete_object(Bucket=BUCKET, Key=POINTER)

        with self.assertRaises(PointerVanished):
            promote_pointer(
                self.store,
                pointer_key=POINTER,
                document=document,
                release_identifier=artifacts.release_id,
                observed=observed,
            )

    def test_the_adapter_translates_a_missing_object(self):
        with self.assertRaises(ObjectMissing):
            self.store.read("never-written.json")


class InjectedOutcomeTests(unittest.TestCase):
    """The outcomes no backend produces on demand.

    ADR-019 gives 409 its own handling on both paths. It is a genuine race, so
    the fake raises it and these tests measure the publisher's response, which
    is the part carrying the decision.
    """

    def test_a_transient_conflict_on_create_is_retried(self):
        store = RecordingStore(raises=[WriteConflict("409"), None])

        published = publish_objects(
            store,
            config_body=b"c",
            inventory_body=b"i",
            config_schema_version=4,
            inventory_schema_version=3,
            release_prefix=PREFIX,
            config_filename="config.yaml",
            inventory_filename="inventory.json",
        )

        self.assertEqual(published.config.version_id, "version-created")
        # The conflict hits the config create, so that key is attempted twice
        # and the inventory key once. Counting per key rather than in total
        # keeps this from passing on the wrong object's retry.
        creates = [call for call in store.calls if call.startswith("create:")]
        self.assertEqual(creates.count(f"create:{published.config.key}"), 2)
        self.assertEqual(creates.count(f"create:{published.inventory.key}"), 1)

    def test_a_persistent_conflict_on_create_gives_up_bounded(self):
        store = RecordingStore(raises=[WriteConflict("409")] * (CREATE_CONFLICT_ATTEMPTS + 2))

        with self.assertRaises(WriteConflict):
            publish_objects(
                store,
                config_body=b"c",
                inventory_body=b"i",
                config_schema_version=4,
                inventory_schema_version=3,
                release_prefix=PREFIX,
                config_filename="config.yaml",
                inventory_filename="inventory.json",
            )

        self.assertEqual(len(store.calls), CREATE_CONFLICT_ATTEMPTS)

    def test_an_indeterminate_promotion_that_converged_is_recorded_as_convergence(self):
        identifier = "e" * 64
        document = json.dumps({"release_id": identifier}).encode()
        # The pointer already names this release, which is what a competing
        # publisher promoting the same release would leave behind.
        store = RecordingStore(raises=[WriteConflict("409")], bodies={POINTER: document})
        observed = StoredObject(body=b'{"release_id": "old"}', etag='"e"', version_id="v-observed")

        promotion = promote_pointer(
            store,
            pointer_key=POINTER,
            document=document,
            release_identifier=identifier,
            observed=observed,
        )

        self.assertTrue(promotion.converged)
        # Convergence, not attribution: no version ID is claimed for a write
        # this publisher cannot prove it made.
        self.assertIsNone(promotion.new_version_id)
        self.assertEqual(promotion.prior_version_id, "v-observed")

    def test_an_indeterminate_promotion_that_lost_is_refused(self):
        identifier = "f" * 64
        store = RecordingStore(
            raises=[WriteConflict("409")],
            bodies={POINTER: json.dumps({"release_id": "0" * 64}).encode()},
        )
        observed = StoredObject(body=b'{"release_id": "old"}', etag='"e"', version_id="v-observed")

        with self.assertRaises(PromotionSuperseded) as raised:
            promote_pointer(
                store,
                pointer_key=POINTER,
                document=b"{}",
                release_identifier=identifier,
                observed=observed,
            )
        self.assertEqual(raised.exception.observed, "0" * 64)

    def test_a_read_back_that_disagrees_with_the_written_bytes_fails(self):
        """Chapter 03 step 5 verifies the hash of the exact version read back.

        No backend returns bytes it was not given, so the check is unreachable
        against `moto` and would sit untested while looking covered. The fake
        returns different content than it stored, which is the only way to
        exercise the branch that stops a corrupt release from being pointed at.
        """

        class TamperingStore(RecordingStore):
            def read(self, key, version_id=None):
                super().read(key, version_id)
                return StoredObject(body=b"different", etag='"e"', version_id=version_id or "v")

        with self.assertRaisesRegex(ValueError, "read back with a different hash"):
            publish_objects(
                TamperingStore(),
                config_body=b"c",
                inventory_body=b"i",
                config_schema_version=4,
                inventory_schema_version=3,
                release_prefix=PREFIX,
                config_filename="config.yaml",
                inventory_filename="inventory.json",
            )

    def test_an_unreadable_pointer_is_reported_rather_than_guessed(self):
        store = RecordingStore(raises=[PreconditionFailed("412")], bodies={POINTER: b"not json"})
        observed = StoredObject(body=b"{}", etag='"e"', version_id="v")

        with self.assertRaises(PromotionSuperseded) as raised:
            promote_pointer(
                store,
                pointer_key=POINTER,
                document=b"{}",
                release_identifier="a" * 64,
                observed=observed,
            )
        self.assertIsNone(raised.exception.observed)
        self.assertIn("<unreadable>", str(raised.exception))


class CommittedBundleTests(unittest.TestCase):
    """The publisher must reproduce the committed release, not its own idea of one."""

    def setUp(self):
        self.mock = mock_aws()
        self.mock.start()
        self.addCleanup(self.mock.stop)
        self.client = boto3.client("s3", region_name=REGION)
        self.client.create_bucket(Bucket=BUCKET)
        self.client.put_bucket_versioning(Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})
        self.store = S3ObjectStore(self.client, BUCKET)
        self.deployment = load_deployment()
        self.committed = json.loads((ROOT / "examples" / "active-versions.json").read_text(encoding="utf-8"))

    def test_publishing_the_committed_bundle_reproduces_the_committed_pointer(self):
        config_body = (ROOT / "examples" / "config.yaml").read_bytes()
        inventory_body = (ROOT / "examples" / "inventory.json").read_bytes()

        artifacts = publish_objects(
            self.store,
            config_body=config_body,
            inventory_body=inventory_body,
            config_schema_version=yaml.safe_load(config_body)["version"],
            inventory_schema_version=json.loads(inventory_body)["schema_version"],
            release_prefix=self.deployment["release_prefix"],
            config_filename=self.deployment["config_filename"],
            inventory_filename=self.deployment["inventory_filename"],
        )
        document = artifacts.pointer_document(PROMOTED)

        self.assertEqual(artifacts.release_id, self.committed["release_id"])
        for name in ("config", "inventory"):
            for field in ("key", "sha256", "schema_version"):
                self.assertEqual(document[name][field], self.committed[name][field], f"{name}.{field}")
        self.assertEqual(document["promoted_at"], self.committed["promoted_at"])
        self.assertEqual(document["schema_version"], self.committed["schema_version"])

        # Version IDs are the one field the committed example cannot pin: they
        # are assigned by the store. Every other field matches, so the shapes
        # are compared with the committed placeholders substituted out.
        rebuilt = {**document, "config": dict(document["config"]), "inventory": dict(document["inventory"])}
        for name in ("config", "inventory"):
            rebuilt[name]["version_id"] = self.committed[name]["version_id"]
        self.assertEqual(rebuilt, self.committed)

    def test_the_published_pointer_passes_its_own_schema(self):
        artifacts = publish_objects(
            self.store,
            config_body=(ROOT / "examples" / "config.yaml").read_bytes(),
            inventory_body=(ROOT / "examples" / "inventory.json").read_bytes(),
            config_schema_version=4,
            inventory_schema_version=3,
            release_prefix=self.deployment["release_prefix"],
            config_filename=self.deployment["config_filename"],
            inventory_filename=self.deployment["inventory_filename"],
        )

        validator.validate_schema(
            ROOT / "schemas" / "active-versions.schema.json",
            ROOT / "examples" / "active-versions.json",
            artifacts.pointer_document(PROMOTED),
        )

    def test_the_read_back_hash_is_the_published_hash(self):
        config_body = (ROOT / "examples" / "config.yaml").read_bytes()
        artifacts = publish_objects(
            self.store,
            config_body=config_body,
            inventory_body=(ROOT / "examples" / "inventory.json").read_bytes(),
            config_schema_version=4,
            inventory_schema_version=3,
            release_prefix=self.deployment["release_prefix"],
            config_filename=self.deployment["config_filename"],
            inventory_filename=self.deployment["inventory_filename"],
        )

        stored = self.store.read(artifacts.config.key, artifacts.config.version_id)
        self.assertEqual(hashlib.sha256(stored.body).hexdigest(), artifacts.config.sha256)
        self.assertEqual(artifacts.config.sha256, self.committed["config"]["sha256"])


if __name__ == "__main__":
    unittest.main()
