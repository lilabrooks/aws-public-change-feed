"""Bounded qualification for the one retained pre-ADR-029 package."""

import hashlib
import io
import stat
import sys
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from qualify_legacy_lambda_artifact import qualify_legacy_package  # noqa: E402


class LegacyLambdaPackageTests(unittest.TestCase):
    handlers = ("aws_public_change_feed.fixture_handler.lambda_handler",)
    source = {"aws_public_change_feed/fixture_handler.py": b"def lambda_handler(event, context):\n    return {}\n"}

    def package(self, source=None, *, unsafe_name=None) -> bytes:
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            for name, body in (source or self.source).items():
                info = zipfile.ZipInfo(name)
                info.external_attr = stat.S_IFREG << 16
                archive.writestr(info, body)
            if unsafe_name:
                info = zipfile.ZipInfo(unsafe_name)
                info.external_attr = stat.S_IFREG << 16
                archive.writestr(info, b"unsafe")
        return output.getvalue()

    def qualify(self, body: bytes, *, version_id="legacy-version"):
        with (
            patch("qualify_legacy_lambda_artifact.LEGACY_ARTIFACT_SHA256", hashlib.sha256(body).hexdigest()),
            patch("qualify_legacy_lambda_artifact.LEGACY_ARTIFACT_VERSION_ID", "legacy-version"),
            patch("qualify_legacy_lambda_artifact.LEGACY_SOURCE_REVISION", "fixed-revision"),
            patch("qualify_legacy_lambda_artifact.RUNTIME_HANDLERS", self.handlers),
            patch("qualify_legacy_lambda_artifact._source_at_revision", return_value=self.source),
        ):
            return qualify_legacy_package(body, observed_version_id=version_id, repository_root=ROOT)

    def test_exact_identity_source_and_handler_pass(self):
        body = self.package()
        self.assertEqual(
            self.qualify(body),
            {
                "artifact_sha256": hashlib.sha256(body).hexdigest(),
                "artifact_version_id": "legacy-version",
                "source_revision": "fixed-revision",
            },
        )

    def test_wrong_version_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "wrong S3 VersionId"):
            self.qualify(self.package(), version_id="another-version")

    def test_changed_package_bytes_are_rejected(self):
        body = self.package()
        with (
            patch("qualify_legacy_lambda_artifact.LEGACY_ARTIFACT_SHA256", "0" * 64),
            patch("qualify_legacy_lambda_artifact.LEGACY_ARTIFACT_VERSION_ID", "legacy-version"),
        ):
            with self.assertRaisesRegex(ValueError, "wrong SHA-256"):
                qualify_legacy_package(body, observed_version_id="legacy-version", repository_root=ROOT)

    def test_source_member_set_mismatch_is_rejected(self):
        body = self.package({"aws_public_change_feed/other.py": b"value = 1\n"})
        with self.assertRaisesRegex(ValueError, "source member set"):
            self.qualify(body)

    def test_source_byte_mismatch_is_rejected(self):
        body = self.package({"aws_public_change_feed/fixture_handler.py": b"value = 1\n"})
        with self.assertRaisesRegex(ValueError, "source bytes"):
            self.qualify(body)

    def test_unsafe_archive_is_rejected(self):
        body = self.package(unsafe_name="../escape")
        with self.assertRaisesRegex(ValueError, "unsafe member path"):
            self.qualify(body)


if __name__ == "__main__":
    unittest.main()
