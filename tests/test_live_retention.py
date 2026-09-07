"""Retention uses terminal evidence, exact versions, and explicit operator approval."""

import copy
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import live_retention as retention  # noqa: E402
import live_window as live  # noqa: E402

NOW = datetime(2026, 9, 7, tzinfo=UTC)
OP = "a" * 32
NEXT = "b" * 32
SOURCE = f"bundles/{OP}-{'c' * 64}.zip"
RECEIPT = f"evidence/{OP}/receipt-{'d' * 32}.json"


class VersionStore:
    def __init__(self):
        self.objects = {}
        self.deletions = []
        self.fail_after = None

    def add(self, key, value, version="v1"):
        self.objects[key, version] = value

    def get_paginator(self, name):
        assert name == "list_object_versions"
        return self

    def paginate(self, Bucket, Prefix):
        yield {
            "Versions": [
                {"Key": key, "VersionId": version, "LastModified": NOW - timedelta(minutes=1)}
                for key, version in self.objects
                if key.startswith(Prefix)
            ]
        }

    def get_object(self, Bucket, Key, VersionId):
        return {"Body": io.BytesIO(json.dumps(self.objects[Key, VersionId]).encode())}

    def put_object(self, Bucket, Key, Body, IfNoneMatch, ServerSideEncryption):
        assert IfNoneMatch == "*" and ServerSideEncryption == "AES256"
        assert not any(key == Key for key, _ in self.objects)
        self.add(Key, json.loads(Body))
        return {"VersionId": "v1"}

    def delete_object(self, Bucket, Key, VersionId):
        if self.fail_after == len(self.deletions):
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "DeleteObject")
        self.deletions.append((Key, VersionId))
        self.objects.pop((Key, VersionId), None)

    def head_object(self, Bucket, Key, VersionId):
        if (Key, VersionId) not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {}


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.s3 = VersionStore()
        self.controller = Mock()
        self.controller.config = {
            "bucket": "control",
            "state_machine": "arn:aws:states:us-east-1:667653114001:stateMachine:apcf-dev-live-control",
        }
        execution = self.controller.config["state_machine"].replace(":stateMachine:", ":execution:") + ":" + OP
        payload = {"operation": OP, "source": "control/" + SOURCE, "version": "v1"}
        self.item = {
            "generation": {"S": OP},
            "execution": {"S": execution},
            "input": {"S": json.dumps(payload)},
            "phase": {"S": "parked"},
            "stop": {"BOOL": True},
            "outcome": {"S": "observation_complete"},
            "evidence_incomplete": {"BOOL": False},
        }
        self.controller.ledger.return_value = self.item
        self.states = Mock()
        self.states.describe_execution.return_value = {
            "status": "SUCCEEDED",
            "input": json.dumps(payload),
            "stopDate": NOW,
        }
        self.controller.clients = {"s3": self.s3, "stepfunctions": self.states}
        self.s3.add(SOURCE, "source bytes")
        self.s3.add(
            RECEIPT,
            {
                "terraform_converged": True,
                "unresolved_work": dict.fromkeys(retention.UNRESOLVED, 0),
                "controls": {
                    "work_inventory_complete": True,
                    "queues": {
                        name: dict.fromkeys(
                            [
                                "ApproximateNumberOfMessages",
                                "ApproximateNumberOfMessagesNotVisible",
                                "ApproximateNumberOfMessagesDelayed",
                            ],
                            "0",
                        )
                        for name in [
                            "apcf-delivery-dev.fifo",
                            "apcf-delivery-dlq-dev.fifo",
                            "apcf-runtime-failures-dev",
                        ]
                    },
                },
            },
        )

    def close(self):
        return retention.closeout(self.controller, NOW)

    def advance(self):
        self.close()
        self.controller.ledger.return_value = {"generation": {"S": NEXT}, "input": {"S": "{}"}}
        return retention.preview(self.controller, NOW + timedelta(days=90))

    def test_closeout_binds_exact_source_all_evidence_versions_and_terminal_owner(self):
        self.s3.add(RECEIPT, copy.deepcopy(self.s3.objects[RECEIPT, "v1"]), "older")
        # Ambiguous last-modified ties must refuse rather than select a clean retry.
        with self.assertRaisesRegex(retention.RetentionError, "ambiguous"):
            self.close()
        self.s3.objects.pop((RECEIPT, "older"))
        extra = f"evidence/{OP}/before-park-{'e' * 32}.json"
        self.s3.add(extra, {}, "noncurrent")
        self.s3.add(extra, {}, "current")
        self.assertEqual(self.close()["status"], "closed")
        manifest = self.s3.objects[f"closeouts/{OP}.json", "v1"]
        self.assertEqual(len(manifest["objects"]), 4)
        self.assertEqual(manifest["closed_at"], NOW.isoformat())
        self.assertEqual(self.close()["status"], "already_closed")

    def test_failed_active_incomplete_or_unresolved_session_cannot_close(self):
        for field, value in [
            ("phase", {"S": "live"}),
            ("stop", {"BOOL": False}),
            ("outcome", {"S": "refused"}),
            ("evidence_incomplete", {"BOOL": True}),
        ]:
            with (
                self.subTest(field=field),
                patch.dict(self.item, {field: value}),
                self.assertRaises(retention.RetentionError),
            ):
                self.close()
        for status in ["RUNNING", "FAILED", "TIMED_OUT", "ABORTED"]:
            with (
                self.subTest(status=status),
                patch.dict(self.states.describe_execution.return_value, status=status),
                self.assertRaises(retention.RetentionError),
            ):
                self.close()
        receipt = self.s3.objects[RECEIPT, "v1"]
        for unresolved in [None, {}, {**dict.fromkeys(retention.UNRESOLVED, 0), "delivery_unknown": 1}]:
            with patch.dict(receipt, unresolved_work=unresolved), self.assertRaises(retention.RetentionError):
                self.close()
        with patch.dict(receipt["controls"], queues={}), self.assertRaises(retention.RetentionError):
            self.close()
        self.assertFalse(any(key.startswith("closeouts/") for key, _ in self.s3.objects))

    def test_successfully_recovered_session_can_close_but_other_execution_names_refuse(self):
        primary = self.item["execution"]["S"]
        self.item["execution"]["S"] = primary.rsplit(":", 1)[0] + ":park-" + NEXT
        self.assertEqual(self.close()["status"], "closed")
        manifest = self.s3.objects[f"closeouts/{OP}.json", "v1"]
        for bad in [primary + "suffix", primary.replace(OP, NEXT), primary.rsplit(":", 1)[0] + ":park-bad"]:
            with patch.dict(manifest, execution=bad), self.assertRaises(retention.RetentionError):
                retention.validate_closeout(self.controller, manifest, NOW)

    def test_input_mismatch_changed_owner_and_missing_source_refuse(self):
        with (
            patch.dict(self.states.describe_execution.return_value, input="{}"),
            self.assertRaises(retention.RetentionError),
        ):
            self.close()
        self.controller.ledger.side_effect = [self.item, {"generation": {"S": NEXT}}]
        with self.assertRaisesRegex(retention.RetentionError, "owner changed"):
            self.close()
        self.controller.ledger.side_effect = None
        self.s3.objects.pop((SOURCE, "v1"))
        with self.assertRaisesRegex(retention.RetentionError, "source version"):
            self.close()

    def test_current_owner_protected_even_after_ninety_days(self):
        self.close()
        self.assertEqual(retention.preview(self.controller, NOW + timedelta(days=91))["sessions"], [])
        self.controller.ledger.return_value = {"generation": {"S": NEXT}}
        self.assertEqual(retention.preview(self.controller, NOW + timedelta(days=90, microseconds=-1))["sessions"], [])
        self.assertEqual(len(retention.preview(self.controller, NOW + timedelta(days=90))["sessions"]), 1)

    def test_prune_deletes_only_approved_versions_preserving_orphans_manifests_and_new_versions(self):
        plan = self.advance()
        self.s3.add(SOURCE, "later unexpected source", "v2")
        self.s3.add(f"bundles/{NEXT}-{'f' * 64}.zip", "orphan")
        result = retention.prune(self.controller, plan, retention.digest(plan), NOW + timedelta(days=90))
        self.assertEqual(result["confirmed_absent_versions"], 2)
        self.assertEqual(set(self.s3.deletions), {(SOURCE, "v1"), (RECEIPT, "v1")})
        self.assertIn((SOURCE, "v2"), self.s3.objects)
        self.assertIn((f"closeouts/{OP}.json", "v1"), self.s3.objects)

    def test_hash_mutation_unknown_fields_and_expired_preview_refuse_before_deletion(self):
        plan = self.advance()
        for mutation in [
            {**plan, "extra": True},
            {**plan, "schema": True},
            {**plan, "bucket": "another"},
            {**plan, "sessions": []},
        ]:
            with self.subTest(mutation=mutation), self.assertRaises(retention.RetentionError):
                retention.prune(self.controller, mutation, retention.digest(mutation), NOW + timedelta(days=90))
        for now in [NOW + timedelta(days=89), NOW + timedelta(days=91, seconds=1)]:
            with self.assertRaises(retention.RetentionError):
                retention.prune(self.controller, plan, retention.digest(plan), now)
        with self.assertRaises(retention.RetentionError):
            retention.prune(self.controller, plan, "wrong", NOW + timedelta(days=90))
        self.assertEqual(self.s3.deletions, [])

    def test_cross_session_null_version_and_unknown_closeout_fields_refuse(self):
        self.advance()
        manifest = self.s3.objects[f"closeouts/{OP}.json", "v1"]
        for change in [
            {"unknown": True},
            {"objects": [{"key": "application/retained.zip", "version": "v1"}]},
            {"closed_at": (NOW + timedelta(days=91)).isoformat()},
        ]:
            with patch.dict(manifest, change), self.assertRaises(retention.RetentionError):
                retention.preview(self.controller, NOW + timedelta(days=90))
        for ref in [{"key": SOURCE, "version": "null"}, {"key": SOURCE.replace(OP, NEXT), "version": "v1"}]:
            with self.assertRaises(retention.RetentionError):
                retention.object_ref(ref, OP)

    def test_partial_delete_can_resume_exact_inventory_without_expanding_authority(self):
        plan = self.advance()
        self.s3.fail_after = 1
        with self.assertRaises(ClientError):
            retention.prune(self.controller, plan, retention.digest(plan), NOW + timedelta(days=90))
        self.s3.fail_after = None
        result = retention.prune(self.controller, plan, retention.digest(plan), NOW + timedelta(days=90))
        self.assertEqual(result["status"], "pruned")
        self.assertEqual(set(self.s3.deletions), {(SOURCE, "v1"), (RECEIPT, "v1")})

    def test_owner_rechecked_before_each_delete(self):
        plan = self.advance()
        self.controller.ledger.side_effect = [self.controller.ledger.return_value, self.item]
        with self.assertRaisesRegex(retention.RetentionError, "current owner"):
            retention.prune(self.controller, plan, retention.digest(plan), NOW + timedelta(days=90))
        self.assertEqual(self.s3.deletions, [])

    def test_missing_owner_or_current_exact_source_reference_blocks_deletion(self):
        plan = self.advance()
        self.controller.ledger.return_value = {}
        with self.assertRaises(retention.RetentionError):
            retention.prune(self.controller, plan, retention.digest(plan), NOW + timedelta(days=90))
        self.controller.ledger.return_value = {
            "generation": {"S": NEXT},
            "input": {"S": json.dumps({"source": "control/" + SOURCE, "version": "v1"})},
        }
        with self.assertRaisesRegex(retention.RetentionError, "current owner"):
            retention.prune(self.controller, plan, retention.digest(plan), NOW + timedelta(days=90))
        self.assertEqual(self.s3.deletions, [])

    def test_delete_markers_oversized_inventory_and_post_closure_writes_refuse(self):
        for page in [{"DeleteMarkers": [{"Key": SOURCE}]}, {"Versions": [{}] * (retention.LIMIT + 1)}]:
            with patch.object(self.s3, "paginate", return_value=[page]), self.assertRaises(retention.RetentionError):
                retention.inventory(self.controller, "bundles/")
        paginate = self.s3.paginate

        def late_write(**kwargs):
            for page in paginate(**kwargs):
                for row in page["Versions"]:
                    row["LastModified"] = NOW + timedelta(seconds=1)
                yield page

        with (
            patch.object(self.s3, "paginate", side_effect=late_write),
            self.assertRaisesRegex(retention.RetentionError, "objects changed"),
        ):
            self.close()

    def test_absence_is_verified_not_assumed_from_delete_response(self):
        plan = self.advance()
        with patch.object(self.s3, "delete_object"), self.assertRaisesRegex(retention.RetentionError, "still exists"):
            retention.prune(self.controller, plan, retention.digest(plan), NOW + timedelta(days=90))

    def test_closeout_failure_never_changes_parking_result_or_leaks_private_details(self):
        with patch.object(retention, "closeout", side_effect=ClientError({"Error": {"Code": "private"}}, "PutObject")):
            result = live.try_closeout(self.controller)
        self.assertEqual(result["status"], "retained_without_closeout")
        self.assertNotIn("private", json.dumps(result))

    def test_notifications_are_failure_only_scoped_private_and_retained(self):
        source = (ROOT / "infra/live-control/notifications.tf").read_text()
        self.assertIn('["FAILED", "TIMED_OUT", "ABORTED"]', source)
        self.assertIn('["FAILED", "FAULT", "TIMED_OUT", "STOPPED"]', source)
        self.assertIn('"aws:SourceAccount" = local.account', source)
        self.assertIn('"aws:SourceArn"', source)
        self.assertIn('Action = ["sns:Publish"], Resource = local.operations_topic_arn', source)
        self.assertNotIn('resource "aws_sns_topic_policy"', source)
        for forbidden in [
            "$.detail.input",
            "$.detail.output",
            "$.detail.additional-information",
            "schedule_expression",
            "aws_cloudwatch_metric_alarm",
        ]:
            self.assertNotIn(forbidden, source)
        workflow = (ROOT / "infra/live-control/workflow.tf").read_text()
        self.assertIn('Next = "NotifyCleanupFailure"', workflow)
        self.assertIn('Error = "LiveCleanupFailed"', workflow)
        self.assertIn("TimeoutSeconds = local.timing.cleanup_notification_seconds", workflow)
        main = (ROOT / "infra/live-control/main.tf").read_text()
        self.assertIn("days_after_initiation = 1", main)
        self.assertNotIn("noncurrent_version_expiration", main)
        self.assertNotIn("expiration {", main)

    @unittest.skipUnless(shutil.which("terraform"), "Terraform not installed")
    def test_rendered_workflow_exhaustion_routes_notify_then_fail_without_private_payloads(self):
        source = (ROOT / "infra/live-control/workflow.tf").read_text()
        main = (ROOT / "infra/live-control/main.tf").read_text()
        prefix_match = re.search(r'prefix\s*= "([^"]+)"', main)
        assert prefix_match is not None
        prefix = prefix_match.group(1)
        for ref in ["aws_codebuild_project.control.name", "aws_dynamodb_table.control.name"]:
            source = source.replace(ref, json.dumps(prefix))
        locals_source = source[: source.index('resource "aws_sfn_state_machine"')]
        definition = source[source.index("  definition = ") : source.index("  tags = local.tags")]
        header = (
            'locals {\nregion = "us-east-1"\noperations_topic_arn = "arn:aws:sns:us-east-1:667653114001:apcf-operations"\ntiming = jsondecode(file('
            + json.dumps(str(ROOT / "scripts/live_window_budget.json"))
            + "))\n}\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "main.tf").write_text(header + locals_source + "\nlocals {\n" + definition + "\n}\n")
            environment = {key: value for key, value in os.environ.items() if key != "TF_DATA_DIR"}
            result = subprocess.run(
                ["terraform", "console"],
                input="local.definition",
                text=True,
                capture_output=True,
                check=True,
                timeout=60,
                cwd=directory,
                env=environment,
            )
        rendered = json.loads(json.loads(result.stdout))
        states = rendered["States"]
        for name in ["Park", "ParkAfterFailure"]:
            self.assertEqual(states[name]["Retry"][0]["MaxAttempts"], live.BUDGET["park_retry_attempts"])
            self.assertEqual(states[name]["Catch"][0]["Next"], "NotifyCleanupFailure")
        notification = states["NotifyCleanupFailure"]
        self.assertEqual(notification["TimeoutSeconds"], 30)
        self.assertEqual(notification["Next"], "CleanupFailed")
        self.assertEqual(notification["Catch"][0]["Next"], "CleanupFailed")
        self.assertEqual(states["CleanupFailed"]["Type"], "Fail")
        self.assertEqual(set(notification["Parameters"]), {"TopicArn", "Subject", "Message.$"})
        self.assertIn("$$.Execution.Id", notification["Parameters"]["Message.$"])
        self.assertNotIn("$.activation_error", notification["Parameters"]["Message.$"])


if __name__ == "__main__":
    unittest.main()
