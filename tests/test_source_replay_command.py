"""Source replay command input and saved-plan guards."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("replay_source_snapshot", ROOT / "scripts" / "replay_source_snapshot.py")
assert SPEC is not None and SPEC.loader is not None
COMMAND = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(COMMAND)


class Body:
    def __init__(self, value):
        self.value = value

    def read(self, limit):
        return self.value[:limit]


class S3:
    def __init__(self, body, metadata):
        self.body = body
        self.metadata = metadata

    def get_object(self, **kwargs):
        self.request = kwargs
        return {"Body": Body(self.body), "Metadata": self.metadata}


class SourceReplayCommandTests(unittest.TestCase):
    def test_snapshot_loader_binds_runtime_key_metadata_and_bytes(self):
        body = b"<rss/>"
        digest = hashlib.sha256(body).hexdigest()
        key = f"apcf/raw-snapshots/feed-a/20260830T180000.000000Z/request-1/{digest}.bin"
        client = S3(body, {"body-sha256": digest, "feed-name": "feed-a"})

        snapshot = COMMAND._load_snapshot(
            client,
            bucket="bucket",
            prefix="apcf/raw-snapshots/",
            key=key,
            max_bytes=100,
        )

        self.assertEqual(snapshot.body, body)
        self.assertEqual(snapshot.body_sha256, digest)
        self.assertEqual(snapshot.feed_name, "feed-a")
        self.assertEqual(client.request, {"Bucket": "bucket", "Key": key})

    def test_snapshot_loader_refuses_changed_bytes(self):
        body = b"<rss/>"
        wrong = "0" * 64
        key = f"apcf/raw-snapshots/feed-a/20260830T180000.000000Z/request-1/{wrong}.bin"
        client = S3(body, {"body-sha256": wrong, "feed-name": "feed-a"})

        with self.assertRaisesRegex(COMMAND.ReplayRefused, "disagree") as raised:
            COMMAND._load_snapshot(
                client,
                bucket="bucket",
                prefix="apcf/raw-snapshots/",
                key=key,
                max_bytes=100,
            )
        self.assertEqual(raised.exception.status, "stale_plan")

    def test_saved_plan_requires_exact_canonical_bytes_and_digest(self):
        plan = {"schema_version": 1, "action": "source_replay", "result": {"candidate_ids": []}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            digest = COMMAND._write_plan(path, plan)
            self.assertEqual(COMMAND._load_plan(path, digest), plan)

            path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
            changed_digest = hashlib.sha256(path.read_bytes()).hexdigest()
            with self.assertRaisesRegex(COMMAND.ReplayRefused, "reviewed bytes"):
                COMMAND._load_plan(path, changed_digest)

    def test_preview_requires_an_explicit_route_scope(self):
        arguments = COMMAND.parse_args(
            [
                "preview",
                "--deployment",
                "deployment.yaml",
                "--terraform-output",
                "outputs.json",
                "--expected-account",
                "123456789012",
                "--snapshot-key",
                "snapshot",
                "--release-version-id",
                "version",
                "--plan",
                "plan.json",
                "--expect-no-routes",
            ]
        )
        self.assertTrue(arguments.expect_no_routes)
        self.assertIsNone(arguments.expected_routes)


if __name__ == "__main__":
    unittest.main()
