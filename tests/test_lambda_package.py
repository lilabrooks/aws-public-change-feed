"""Deterministic package bytes and append-only artifact publication."""

import hashlib
import io
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

import boto3
from moto import mock_aws

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from build_lambda_package import FIXED_ZIP_TIME, _archive_tree, _require_exact_lock  # noqa: E402
from publish_lambda_artifact import publish  # noqa: E402


class FakePreconditionError(Exception):
    def __init__(self):
        self.response = {"ResponseMetadata": {"HTTPStatusCode": 412}}


class FakeS3:
    def __init__(self, existing=False, metadata_digest=None, stored_body=None):
        self.existing = existing
        self.metadata_digest = metadata_digest
        self.stored_body = stored_body
        self.puts = []

    def put_object(self, **arguments):
        self.puts.append(arguments)
        if self.existing:
            raise FakePreconditionError()
        return {"VersionId": "new-version"}

    def head_object(self, **arguments):
        return {"VersionId": "existing-version", "Metadata": {"sha256": self.metadata_digest}}

    def get_object(self, **arguments):
        body = self.stored_body if self.existing and self.stored_body is not None else self.puts[0]["Body"]
        digest = hashlib.sha256(body).hexdigest()
        metadata = self.metadata_digest if self.existing else digest
        return {"Body": io.BytesIO(body), "Metadata": {"sha256": metadata}}


class LambdaPackageTests(unittest.TestCase):
    def test_archive_bytes_are_reproducible_and_have_fixed_metadata(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            staging = root / "stage"
            staging.mkdir()
            (staging / "b.py").write_text("B = 2\n", encoding="utf-8")
            nested = staging / "package"
            nested.mkdir()
            (nested / "a.py").write_text("A = 1\n", encoding="utf-8")
            first = root / "first.zip"
            second = root / "second.zip"

            first_digest = _archive_tree(staging, first)
            second_digest = _archive_tree(staging, second)

            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(first_digest, hashlib.sha256(first.read_bytes()).hexdigest())
            self.assertEqual(first_digest, second_digest)
            with zipfile.ZipFile(first) as archive:
                self.assertEqual(archive.namelist(), ["b.py", "package/a.py"])
                self.assertTrue(all(member.date_time == FIXED_ZIP_TIME for member in archive.infolist()))

    def test_dependency_lock_rejects_a_range(self):
        with tempfile.TemporaryDirectory() as raw:
            lock = Path(raw) / "requirements.txt"
            lock.write_text("boto3>=1\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "non-exact"):
                _require_exact_lock(lock)


class ArtifactPublicationTests(unittest.TestCase):
    def test_new_package_uses_digest_key_and_if_none_match(self):
        with tempfile.TemporaryDirectory() as raw:
            package = Path(raw) / "worker.zip"
            package.write_bytes(b"exact deployable bytes")
            client = FakeS3()

            digest, key, version = publish(
                client,
                bucket="artifacts",
                prefix="apcf/application-artifacts/",
                package=package,
            )

            self.assertEqual(digest, hashlib.sha256(package.read_bytes()).hexdigest())
            self.assertEqual(key, f"apcf/application-artifacts/{digest}.zip")
            self.assertEqual(version, "new-version")
            self.assertEqual(client.puts[0]["IfNoneMatch"], "*")
            self.assertEqual(client.puts[0]["Metadata"], {"sha256": digest})

    def test_existing_matching_package_is_adopted_without_replacement(self):
        with tempfile.TemporaryDirectory() as raw:
            package = Path(raw) / "worker.zip"
            package.write_bytes(b"same bytes")
            digest = hashlib.sha256(package.read_bytes()).hexdigest()
            client = FakeS3(existing=True, metadata_digest=digest)

            observed, _, version = publish(
                client,
                bucket="artifacts",
                prefix="application-artifacts",
                package=package,
            )

            self.assertEqual(observed, digest)
            self.assertEqual(version, "existing-version")

    def test_existing_key_without_matching_digest_metadata_is_refused(self):
        with tempfile.TemporaryDirectory() as raw:
            package = Path(raw) / "worker.zip"
            package.write_bytes(b"same bytes")
            client = FakeS3(existing=True, metadata_digest="0" * 64)

            with self.assertRaisesRegex(RuntimeError, "matching package bytes"):
                publish(
                    client,
                    bucket="artifacts",
                    prefix="application-artifacts",
                    package=package,
                )

    def test_existing_key_with_forged_metadata_but_other_bytes_is_refused(self):
        with tempfile.TemporaryDirectory() as raw:
            package = Path(raw) / "worker.zip"
            package.write_bytes(b"expected bytes")
            digest = hashlib.sha256(package.read_bytes()).hexdigest()
            client = FakeS3(existing=True, metadata_digest=digest, stored_body=b"other bytes")

            with self.assertRaisesRegex(RuntimeError, "matching package bytes"):
                publish(
                    client,
                    bucket="artifacts",
                    prefix="application-artifacts",
                    package=package,
                )


class MotoArtifactPublicationTests(unittest.TestCase):
    def setUp(self):
        self.mock = mock_aws()
        self.mock.start()
        self.addCleanup(self.mock.stop)
        self.client = boto3.client("s3", region_name="us-east-1")
        self.bucket = "application-artifacts"
        self.client.create_bucket(Bucket=self.bucket)
        self.client.put_bucket_versioning(
            Bucket=self.bucket,
            VersioningConfiguration={"Status": "Enabled"},
        )

    def test_real_request_shape_creates_then_adopts_one_exact_version(self):
        with tempfile.TemporaryDirectory() as raw:
            package = Path(raw) / "worker.zip"
            package.write_bytes(b"deployable package")

            first = publish(
                self.client,
                bucket=self.bucket,
                prefix="apcf/application-artifacts",
                package=package,
            )
            second = publish(
                self.client,
                bucket=self.bucket,
                prefix="apcf/application-artifacts",
                package=package,
            )

            self.assertEqual(second, first)
            versions = self.client.list_object_versions(Bucket=self.bucket, Prefix=first[1]).get("Versions", [])
            self.assertEqual(len(versions), 1)
            stored = self.client.get_object(Bucket=self.bucket, Key=first[1], VersionId=first[2])
            self.assertEqual(stored["Body"].read(), package.read_bytes())


if __name__ == "__main__":
    unittest.main()
