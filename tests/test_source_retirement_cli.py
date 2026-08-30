from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("retire_source_feed", ROOT / "scripts/retire_source_feed.py")
assert SPEC is not None
tool = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = tool
SPEC.loader.exec_module(tool)


class SourceRetirementCliTests(unittest.TestCase):
    def test_plan_file_is_canonical_and_digest_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            document = {"z": 1, "a": {"value": "bound"}}

            digest = tool.write_plan(path, document)

            self.assertEqual(path.read_bytes(), b'{"a":{"value":"bound"},"z":1}\n')
            self.assertEqual(tool.load_plan(path, digest), document)
            path.write_bytes(path.read_bytes() + b" ")
            with self.assertRaisesRegex(tool.SourceRetirementError, "differs from its digest"):
                tool.load_plan(path, digest)

    def test_invalid_feed_name_is_refused_before_file_or_identity_reads(self):
        class Unused:
            def __getattr__(self, name):
                raise AssertionError(f"unexpected call: {name}")

        with self.assertRaisesRegex(tool.SourceRetirementError, "bounded lowercase identifier"):
            tool.load_context(
                deployment_path=Path("missing-deployment.yaml"),
                terraform_output_path=Path("missing-output.json"),
                expected_account="123456789012",
                feed_name="../../another-key",
                sts_client=Unused(),
                s3_client=Unused(),
                ddb_client_factory=Unused(),
            )

    def test_preview_and_apply_arguments_keep_decision_authority_separate(self):
        preview = tool.parse_args(
            [
                "preview",
                "--operation",
                "retire",
                "--deployment",
                "deployment.yaml",
                "--terraform-output",
                "outputs.json",
                "--expected-account",
                "123456789012",
                "--feed-name",
                "removed-feed",
                "--decision-id",
                "issue-159",
                "--decision-at",
                "2026-08-30T16:00:00Z",
                "--plan",
                "plan.json",
            ]
        )
        apply = tool.parse_args(
            [
                "apply",
                "--operation",
                "retire",
                "--deployment",
                "deployment.yaml",
                "--terraform-output",
                "outputs.json",
                "--expected-account",
                "123456789012",
                "--feed-name",
                "removed-feed",
                "--plan",
                "plan.json",
                "--expected-plan-sha256",
                "a" * 64,
            ]
        )

        self.assertEqual(preview.decision_id, "issue-159")
        self.assertIsNone(apply.decision_id)
        self.assertEqual(apply.expected_plan_sha256, "a" * 64)


if __name__ == "__main__":
    unittest.main()
