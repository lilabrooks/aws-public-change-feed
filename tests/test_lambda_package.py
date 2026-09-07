"""Deterministic package bytes and append-only artifact publication."""

import base64
import hashlib
import io
import json
import re
import struct
import sys
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest.mock import patch

import boto3
import jsonschema
from moto import mock_aws

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from build_lambda_package import (  # noqa: E402
    FIXED_ZIP_TIME,
    MANIFEST_PATH,
    _archive_tree,
    _require_exact_lock,
    build,
    manifest_bytes,
    source_tree_sha256,
)
from publish_lambda_artifact import (  # noqa: E402
    MANIFEST_METADATA_KEY,
    RUNTIME_ENTRYPOINTS_METADATA_KEY,
    RUNTIME_HANDLERS,
    _validate_handler,
    publish,
    runtime_entrypoints_sha256,
)


def runtime_package_bytes(*, omit: str | None = None, replace: tuple[str, bytes] | None = None) -> bytes:
    source = {
        path.relative_to(ROOT / "src").as_posix(): path.read_bytes()
        for path in (ROOT / "src/aws_public_change_feed").rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }
    if omit:
        source.pop(omit, None)
    if replace:
        source[replace[0]] = replace[1]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, body in sorted(source.items()):
            info = zipfile.ZipInfo(path, FIXED_ZIP_TIME)
            info.external_attr = 0o100644 << 16
            archive.writestr(info, body)
        info = zipfile.ZipInfo(MANIFEST_PATH, FIXED_ZIP_TIME)
        info.external_attr = 0o100644 << 16
        archive.writestr(info, manifest_bytes(source))
    return buffer.getvalue()


def rewrite_package(body: bytes, *, omit: str | None = None, replace: tuple[str, bytes] | None = None) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(body)) as source, zipfile.ZipFile(output, "w") as target:
        for member in source.infolist():
            if member.filename == omit:
                continue
            value = replace[1] if replace and member.filename == replace[0] else source.read(member)
            target.writestr(member, value)
    return output.getvalue()


class FakePreconditionError(Exception):
    def __init__(self):
        self.response = {"ResponseMetadata": {"HTTPStatusCode": 412}}


class FakeS3:
    def __init__(
        self,
        existing=False,
        metadata_digest=None,
        metadata_entrypoints=None,
        stored_body=None,
        *,
        include_checksum=True,
    ):
        self.existing = existing
        self.metadata_digest = metadata_digest
        self.metadata_entrypoints = metadata_entrypoints or runtime_entrypoints_sha256()
        self.stored_body = stored_body
        self.include_checksum = include_checksum
        self.puts = []

    def put_object(self, **arguments):
        self.puts.append(arguments)
        if self.existing:
            raise FakePreconditionError()
        return {"VersionId": "new-version"}

    def head_object(self, **arguments):
        body = self.stored_body if self.existing and self.stored_body is not None else self.puts[0]["Body"]
        response = {
            "VersionId": "existing-version",
            "Metadata": {
                "sha256": self.metadata_digest,
                RUNTIME_ENTRYPOINTS_METADATA_KEY: self.metadata_entrypoints,
            },
        }
        if self.include_checksum:
            response["ChecksumSHA256"] = base64.b64encode(hashlib.sha256(body).digest()).decode()
        return response

    def get_object(self, **arguments):
        body = self.stored_body if self.existing and self.stored_body is not None else self.puts[0]["Body"]
        digest = hashlib.sha256(body).hexdigest()
        metadata = self.metadata_digest if self.existing else digest
        entrypoints = (
            self.metadata_entrypoints if self.existing else self.puts[0]["Metadata"][RUNTIME_ENTRYPOINTS_METADATA_KEY]
        )
        manifest_digest = self.puts[0]["Metadata"][MANIFEST_METADATA_KEY]
        return {
            "Body": io.BytesIO(body),
            "Metadata": {
                "sha256": metadata,
                RUNTIME_ENTRYPOINTS_METADATA_KEY: entrypoints,
                MANIFEST_METADATA_KEY: manifest_digest,
            },
        }


class LambdaPackageTests(unittest.TestCase):
    def test_canonical_manifest_example_matches_source_and_schema(self):
        source = {
            path.relative_to(ROOT / "src").as_posix(): path.read_bytes()
            for path in (ROOT / "src/aws_public_change_feed").rglob("*")
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
        }
        example = json.loads((ROOT / "examples/lambda-package-manifest.json").read_text(encoding="utf-8"))
        schema = json.loads((ROOT / "schemas/lambda-package-manifest.schema.json").read_text(encoding="utf-8"))

        jsonschema.Draft202012Validator(schema).validate(example)
        self.assertEqual(example, json.loads(manifest_bytes(source)))

    def test_source_tree_digest_has_an_independent_known_answer(self):
        path = b"aws_public_change_feed/a.py"
        body = b"A\0"
        framed = struct.pack(">Q", len(path)) + path + struct.pack(">Q", len(body)) + body

        self.assertEqual(
            hashlib.sha256(framed).hexdigest(), "0c46f4a268c64da76e91988960e46d15b9b8ccf6f4f626b3cac65c72bec813b2"
        )
        self.assertEqual(source_tree_sha256({path.decode(): body}), hashlib.sha256(framed).hexdigest())

    def test_manifest_has_canonical_known_answer(self):
        body = manifest_bytes({"aws_public_change_feed/a.py": b"A\0"})

        self.assertFalse(body.endswith(b"\n"))
        self.assertEqual(
            hashlib.sha256(body).hexdigest(), "63451de6c1dcdbae8ce1c40c2e3f7b7a6b29254a53ed093f204387c4ccb2a686"
        )

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

    def test_archive_excludes_python_cache_artifacts(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            staging = root / "stage"
            package = staging / "package"
            cache = package / "__pycache__"
            cache.mkdir(parents=True)
            (package / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
            (cache / "generated.txt").write_text("cache data\n", encoding="utf-8")
            (package / "loose.pyc").write_bytes(b"compiled bytecode")
            output = root / "package.zip"

            _archive_tree(staging, output)

            with zipfile.ZipFile(output) as archive:
                self.assertEqual(archive.namelist(), ["package/module.py"])

    def test_dependency_lock_rejects_a_range(self):
        with tempfile.TemporaryDirectory() as raw:
            lock = Path(raw) / "requirements.txt"
            lock.write_text("boto3>=1\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "non-exact"):
                _require_exact_lock(lock)

    def test_the_deployable_package_contains_all_built_runtime_entrypoints(self):
        with tempfile.TemporaryDirectory() as raw, patch("build_lambda_package.subprocess.run") as install:
            package = Path(raw) / "reconciler.zip"

            digest = build(package)

            install.assert_called_once()
            self.assertEqual(digest, hashlib.sha256(package.read_bytes()).hexdigest())
            with zipfile.ZipFile(package) as archive:
                names = set(archive.namelist())
            self.assertIn("aws_public_change_feed/recovery.py", names)
            self.assertIn("aws_public_change_feed/recovery_runtime.py", names)
            self.assertIn("aws_public_change_feed/dispatcher_runtime.py", names)
            self.assertIn("aws_public_change_feed/manual_replay.py", names)
            self.assertIn("aws_public_change_feed/outbox.py", names)
            self.assertIn("aws_public_change_feed/slack_worker_runtime.py", names)
            self.assertIn("aws_public_change_feed/worker.py", names)
            self.assertIn("aws_public_change_feed/watcher.py", names)
            self.assertIn("aws_public_change_feed/watcher_runtime.py", names)
            self.assertIn("aws_public_change_feed/shadow_runtime.py", names)
            self.assertIn("aws_public_change_feed/runtime_environment.py", names)
            self.assertIn("aws_public_change_feed/source_store.py", names)
            for schema in (
                "active-versions.schema.json",
                "alert-candidate.schema.json",
                "config.schema.json",
                "delivery-request.schema.json",
                "inventory.schema.json",
            ):
                with self.subTest(schema=schema):
                    self.assertIn(f"aws_public_change_feed/schemas/{schema}", names)

    def test_publisher_and_terraform_bind_the_same_null_framed_handler_contract(self):
        locals_source = (ROOT / "infra/central/locals.tf").read_text(encoding="utf-8")
        block = locals_source.split("  runtime_entrypoints = [\n", 1)[1].split("\n  ]", 1)[0]
        terraform_handlers = tuple(re.findall(r'^    "([^"]+)",$', block, flags=re.MULTILINE))

        self.assertEqual(terraform_handlers, RUNTIME_HANDLERS)
        self.assertEqual(list(RUNTIME_HANDLERS), sorted(RUNTIME_HANDLERS))
        self.assertIn(
            'runtime_entrypoints_sha256             = sha256(join("\\u0000", local.runtime_entrypoints))',
            locals_source,
        )
        self.assertEqual(
            runtime_entrypoints_sha256(), "8d934863e4305f466c8e4982215f37b48222fee3cb18635509fae7163156cf29"
        )

    def test_nested_local_binding_does_not_count_as_module_rebinding(self):
        _validate_handler(
            b"def lambda_handler(event, context):\n    return {}\n\ndef helper():\n    lambda_handler = None\n    return lambda_handler\n",
            "fixture.lambda_handler",
        )

    def test_conditional_module_binding_counts_as_rebinding(self):
        with self.assertRaisesRegex(ValueError, "rebound after declaration"):
            _validate_handler(
                b"def lambda_handler(event, context):\n    return {}\n\nif enabled:\n    lambda_handler = None\n",
                "fixture.lambda_handler",
            )


class ArtifactPublicationTests(unittest.TestCase):
    def setUp(self):
        checkout_blob = patch(
            "publish_lambda_artifact._git_blob", side_effect=lambda repository_root, path: (ROOT / path).read_bytes()
        )
        self.checkout_blob = checkout_blob.start()
        self.addCleanup(checkout_blob.stop)

    def test_new_package_uses_digest_key_and_if_none_match(self):
        with tempfile.TemporaryDirectory() as raw:
            package = Path(raw) / "worker.zip"
            package.write_bytes(runtime_package_bytes())
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
            self.assertEqual(client.puts[0]["Metadata"]["sha256"], digest)
            self.assertEqual(client.puts[0]["Metadata"][RUNTIME_ENTRYPOINTS_METADATA_KEY], runtime_entrypoints_sha256())
            self.assertRegex(client.puts[0]["Metadata"][MANIFEST_METADATA_KEY], r"^[a-f0-9]{64}$")
            self.assertEqual(client.puts[0]["ChecksumAlgorithm"], "SHA256")
            self.assertEqual(client.puts[0]["ChecksumSHA256"], base64.b64encode(bytes.fromhex(digest)).decode())

    def test_existing_matching_package_is_adopted_without_replacement(self):
        with tempfile.TemporaryDirectory() as raw:
            package = Path(raw) / "worker.zip"
            package.write_bytes(runtime_package_bytes())
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
            package.write_bytes(runtime_package_bytes())
            client = FakeS3(existing=True, metadata_digest="0" * 64)

            with self.assertRaisesRegex(RuntimeError, "matching package contract"):
                publish(
                    client,
                    bucket="artifacts",
                    prefix="application-artifacts",
                    package=package,
                )

    def test_existing_key_with_forged_metadata_but_other_bytes_is_refused(self):
        with tempfile.TemporaryDirectory() as raw:
            package = Path(raw) / "worker.zip"
            package.write_bytes(runtime_package_bytes())
            digest = hashlib.sha256(package.read_bytes()).hexdigest()
            client = FakeS3(existing=True, metadata_digest=digest, stored_body=b"other bytes")

            with self.assertRaisesRegex(RuntimeError, "matching package contract"):
                publish(
                    client,
                    bucket="artifacts",
                    prefix="application-artifacts",
                    package=package,
                )

    def test_existing_package_without_s3_checksum_is_refused(self):
        with tempfile.TemporaryDirectory() as raw:
            package = Path(raw) / "worker.zip"
            package.write_bytes(runtime_package_bytes())
            digest = hashlib.sha256(package.read_bytes()).hexdigest()
            client = FakeS3(existing=True, metadata_digest=digest, include_checksum=False)

            with self.assertRaisesRegex(RuntimeError, "matching package contract"):
                publish(client, bucket="artifacts", prefix="application-artifacts", package=package)

    def test_package_missing_a_configured_handler_is_refused_before_upload(self):
        with tempfile.TemporaryDirectory() as raw:
            package = Path(raw) / "worker.zip"
            package.write_bytes(runtime_package_bytes(omit="aws_public_change_feed/shadow_runtime.py"))
            client = FakeS3()

            with self.assertRaisesRegex(ValueError, "shadow_runtime.py"):
                publish(
                    client,
                    bucket="artifacts",
                    prefix="application-artifacts",
                    package=package,
                )

            self.assertEqual(client.puts, [])

    def test_missing_manifest_is_refused_before_upload(self):
        with tempfile.TemporaryDirectory() as raw:
            package = Path(raw) / "worker.zip"
            package.write_bytes(rewrite_package(runtime_package_bytes(), omit=MANIFEST_PATH))
            client = FakeS3()

            with self.assertRaisesRegex(ValueError, "missing its canonical manifest"):
                publish(client, bucket="artifacts", prefix="application-artifacts", package=package)
            self.assertEqual(client.puts, [])

    def test_non_callable_handler_declaration_is_refused_before_upload(self):
        with tempfile.TemporaryDirectory() as raw:
            package = Path(raw) / "worker.zip"
            package.write_bytes(
                runtime_package_bytes(replace=("aws_public_change_feed/shadow_runtime.py", b"lambda_handler = None\n"))
            )
            client = FakeS3()

            with self.assertRaisesRegex(ValueError, "no supported top-level function declaration"):
                publish(client, bucket="artifacts", prefix="application-artifacts", package=package)
            self.assertEqual(client.puts, [])

    def test_decorated_handler_declaration_is_refused_before_upload(self):
        with tempfile.TemporaryDirectory() as raw:
            package = Path(raw) / "worker.zip"
            package.write_bytes(
                runtime_package_bytes(
                    replace=(
                        "aws_public_change_feed/shadow_runtime.py",
                        b"def decorate(function):\n    return function\n\n@decorate\ndef lambda_handler(event, context):\n    return {}\n",
                    )
                )
            )
            client = FakeS3()

            with self.assertRaisesRegex(ValueError, "cannot be decorated"):
                publish(client, bucket="artifacts", prefix="application-artifacts", package=package)
            self.assertEqual(client.puts, [])

    def test_wrong_arity_handler_declaration_is_refused_before_upload(self):
        with tempfile.TemporaryDirectory() as raw:
            package = Path(raw) / "worker.zip"
            package.write_bytes(
                runtime_package_bytes(
                    replace=("aws_public_change_feed/shadow_runtime.py", b"def lambda_handler(event):\n    return {}\n")
                )
            )
            client = FakeS3()

            with self.assertRaisesRegex(ValueError, "exactly two positional arguments"):
                publish(client, bucket="artifacts", prefix="application-artifacts", package=package)
            self.assertEqual(client.puts, [])

    def test_handler_rebinding_is_refused_before_upload(self):
        with tempfile.TemporaryDirectory() as raw:
            package = Path(raw) / "worker.zip"
            package.write_bytes(
                runtime_package_bytes(
                    replace=(
                        "aws_public_change_feed/shadow_runtime.py",
                        b"def lambda_handler(event, context):\n    return {}\n\nlambda_handler = None\n",
                    )
                )
            )
            client = FakeS3()

            with self.assertRaisesRegex(ValueError, "rebound after declaration"):
                publish(client, bucket="artifacts", prefix="application-artifacts", package=package)
            self.assertEqual(client.puts, [])

    def test_path_traversal_member_is_refused_before_upload(self):
        with tempfile.TemporaryDirectory() as raw:
            package = Path(raw) / "worker.zip"
            body = io.BytesIO(runtime_package_bytes())
            with zipfile.ZipFile(body, "a") as archive:
                info = zipfile.ZipInfo("../../evil.py", FIXED_ZIP_TIME)
                info.external_attr = 0o100644 << 16
                archive.writestr(info, b"pass\n")
            package.write_bytes(body.getvalue())
            client = FakeS3()

            with self.assertRaisesRegex(ValueError, "unsafe member path"):
                publish(client, bucket="artifacts", prefix="application-artifacts", package=package)
            self.assertEqual(client.puts, [])

    def test_malformed_zip_is_refused_before_upload(self):
        with tempfile.TemporaryDirectory() as raw:
            package = Path(raw) / "worker.zip"
            package.write_bytes(b"not a zip")
            client = FakeS3()

            with self.assertRaisesRegex(ValueError, "valid ZIP"):
                publish(client, bucket="artifacts", prefix="application-artifacts", package=package)
            self.assertEqual(client.puts, [])

    def test_symlink_member_is_refused_before_upload(self):
        with tempfile.TemporaryDirectory() as raw:
            package = Path(raw) / "worker.zip"
            body = io.BytesIO(runtime_package_bytes())
            with zipfile.ZipFile(body, "a") as archive:
                info = zipfile.ZipInfo("link", FIXED_ZIP_TIME)
                info.create_system = 3
                info.external_attr = 0o120777 << 16
                archive.writestr(info, b"target")
            package.write_bytes(body.getvalue())
            client = FakeS3()

            with self.assertRaisesRegex(ValueError, "non-regular member"):
                publish(client, bucket="artifacts", prefix="application-artifacts", package=package)
            self.assertEqual(client.puts, [])

    def test_duplicate_member_is_refused_before_upload(self):
        with tempfile.TemporaryDirectory() as raw:
            package = Path(raw) / "worker.zip"
            body = io.BytesIO(runtime_package_bytes())
            with warnings.catch_warnings(), zipfile.ZipFile(body, "a") as archive:
                warnings.simplefilter("ignore", UserWarning)
                archive.writestr(MANIFEST_PATH, b"{}")
            package.write_bytes(body.getvalue())
            client = FakeS3()

            with self.assertRaisesRegex(ValueError, "duplicate members"):
                publish(client, bucket="artifacts", prefix="application-artifacts", package=package)
            self.assertEqual(client.puts, [])

    def test_unknown_manifest_field_is_refused_before_upload(self):
        with tempfile.TemporaryDirectory() as raw:
            package = Path(raw) / "worker.zip"
            body = runtime_package_bytes()
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                manifest = json.loads(archive.read(MANIFEST_PATH))
            manifest["unknown"] = True
            package.write_bytes(
                rewrite_package(
                    body,
                    replace=(
                        MANIFEST_PATH,
                        json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(),
                    ),
                )
            )
            client = FakeS3()

            with self.assertRaisesRegex(ValueError, "missing or unknown fields"):
                publish(client, bucket="artifacts", prefix="application-artifacts", package=package)
            self.assertEqual(client.puts, [])

    def test_changed_valid_source_is_refused_before_upload(self):
        with tempfile.TemporaryDirectory() as raw:
            package = Path(raw) / "worker.zip"
            package.write_bytes(
                runtime_package_bytes(
                    replace=(
                        "aws_public_change_feed/shadow_runtime.py",
                        b"def lambda_handler(event, context):\n    return {'changed': True}\n",
                    )
                )
            )
            client = FakeS3()

            with self.assertRaisesRegex(ValueError, "source bytes do not match"):
                publish(client, bucket="artifacts", prefix="application-artifacts", package=package)
            self.assertEqual(client.puts, [])

    def test_extra_owned_source_member_is_refused_before_upload(self):
        with tempfile.TemporaryDirectory() as raw:
            package = Path(raw) / "worker.zip"
            package.write_bytes(runtime_package_bytes(replace=("aws_public_change_feed/untracked.py", b"VALUE = 1\n")))
            client = FakeS3()

            with self.assertRaisesRegex(ValueError, "source member set does not match"):
                publish(client, bucket="artifacts", prefix="application-artifacts", package=package)
            self.assertEqual(client.puts, [])

    def test_missing_non_handler_source_member_is_refused_before_upload(self):
        with tempfile.TemporaryDirectory() as raw:
            package = Path(raw) / "worker.zip"
            package.write_bytes(runtime_package_bytes(omit="aws_public_change_feed/__init__.py"))
            client = FakeS3()

            with self.assertRaisesRegex(ValueError, "source member set does not match"):
                publish(client, bucket="artifacts", prefix="application-artifacts", package=package)
            self.assertEqual(client.puts, [])

    @patch("publish_lambda_artifact._working_source")
    def test_untracked_working_source_is_refused_before_upload(self, working_source):
        source = {
            path.relative_to(ROOT / "src").as_posix(): path.read_bytes()
            for path in (ROOT / "src/aws_public_change_feed").rglob("*")
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
        }
        working_source.return_value = {**source, "aws_public_change_feed/untracked.py": b"VALUE = 1\n"}
        with tempfile.TemporaryDirectory() as raw:
            package = Path(raw) / "worker.zip"
            package.write_bytes(runtime_package_bytes())
            client = FakeS3()

            with self.assertRaisesRegex(ValueError, "working source member set does not match"):
                publish(client, bucket="artifacts", prefix="application-artifacts", package=package)
            self.assertEqual(client.puts, [])

    def test_stale_tracked_source_is_refused_before_upload(self):
        changed_path = "src/aws_public_change_feed/shadow_runtime.py"
        self.checkout_blob.side_effect = lambda repository_root, path: (
            b"older tracked source\n" if path == changed_path else (ROOT / path).read_bytes()
        )
        with tempfile.TemporaryDirectory() as raw:
            package = Path(raw) / "worker.zip"
            package.write_bytes(runtime_package_bytes())
            client = FakeS3()

            with self.assertRaisesRegex(ValueError, "working source bytes do not match tracked HEAD"):
                publish(client, bucket="artifacts", prefix="application-artifacts", package=package)
            self.assertEqual(client.puts, [])

    def test_manifest_value_or_serialization_mismatch_is_refused_before_upload(self):
        body = runtime_package_bytes()
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            manifest = json.loads(archive.read(MANIFEST_PATH))
        changed_value = {**manifest, "source_tree_sha256": "0" * 64}
        cases = (
            (
                "changed value",
                json.dumps(changed_value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(),
            ),
            (
                "non-canonical serialization",
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode(),
            ),
        )

        for name, manifest_body in cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as raw:
                package = Path(raw) / "worker.zip"
                package.write_bytes(rewrite_package(body, replace=(MANIFEST_PATH, manifest_body)))
                client = FakeS3()

                with self.assertRaisesRegex(ValueError, "manifest does not match the package and tracked inputs"):
                    publish(client, bucket="artifacts", prefix="application-artifacts", package=package)
                self.assertEqual(client.puts, [])

    def test_changed_builder_input_is_refused_before_upload(self):
        with tempfile.TemporaryDirectory() as raw:
            package = Path(raw) / "worker.zip"
            package.write_bytes(runtime_package_bytes())
            client = FakeS3()
            self.checkout_blob.side_effect = lambda repository_root, path: (
                b"older builder" if path == "scripts/build_lambda_package.py" else (ROOT / path).read_bytes()
            )

            with self.assertRaisesRegex(ValueError, "package input differs from tracked HEAD"):
                publish(client, bucket="artifacts", prefix="application-artifacts", package=package)
            self.assertEqual(client.puts, [])

    def test_existing_package_without_the_entrypoint_contract_is_refused(self):
        with tempfile.TemporaryDirectory() as raw:
            package = Path(raw) / "worker.zip"
            package.write_bytes(runtime_package_bytes())
            digest = hashlib.sha256(package.read_bytes()).hexdigest()
            client = FakeS3(existing=True, metadata_digest=digest, metadata_entrypoints="missing")

            with self.assertRaisesRegex(RuntimeError, "matching package contract"):
                publish(
                    client,
                    bucket="artifacts",
                    prefix="application-artifacts",
                    package=package,
                )


class MotoArtifactPublicationTests(unittest.TestCase):
    def setUp(self):
        checkout_blob = patch(
            "publish_lambda_artifact._git_blob", side_effect=lambda repository_root, path: (ROOT / path).read_bytes()
        )
        checkout_blob.start()
        self.addCleanup(checkout_blob.stop)
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
            package.write_bytes(runtime_package_bytes())
            first_attestations = {
                "builder-git-commit": "a" * 40,
                "builder-git-tree": "b" * 40,
                "builder-pip": "first",
                "builder-python": "3.12.0",
                "builder-zlib": "first",
            }
            second_attestations = {**first_attestations, "builder-pip": "second", "builder-zlib": "second"}

            with patch(
                "publish_lambda_artifact.publication_attestations",
                side_effect=(first_attestations, second_attestations),
            ):
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
            for key, value in first_attestations.items():
                self.assertEqual(stored["Metadata"][key], value)


if __name__ == "__main__":
    unittest.main()
