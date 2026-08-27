from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aws_public_change_feed.loading import LoadedRelease  # noqa: E402
from aws_public_change_feed.outbox import InMemoryOutboxStore  # noqa: E402

SPEC = importlib.util.spec_from_file_location(
    "preflight_runtime_exercise", ROOT / "scripts/preflight_runtime_exercise.py"
)
assert SPEC is not None
exercise = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = exercise
assert SPEC.loader is not None
SPEC.loader.exec_module(exercise)

AS_OF = datetime(2026, 8, 26, 20, 0, tzinfo=UTC)


def loaded_release() -> LoadedRelease:
    config = json.loads(json.dumps(yaml.safe_load((ROOT / "config/dev.yaml").read_text(encoding="utf-8"))))
    inventory = {
        "schema_version": 3,
        "deployment_id": "preflight",
        "generated_at": "2026-08-26T19:59:00Z",
        "deployment_region": "us-east-1",
        "slack": {
            "delivery_mode": "incoming_webhook",
            "default_route_id": "dev-alerts",
            "approved_webhook_hosts": ["hooks.slack.com"],
            "rate_control": {
                "per_destination_min_interval_seconds": 1,
                "slack_request_timeout_seconds": 10,
                "max_retry_after_seconds": 900,
                "max_network_attempts": 5,
                "queue_max_receive_count": 100,
                "worker_reserved_concurrency": 2,
            },
            "routes": {
                "dev-alerts": {
                    "channel_label": "#aws-change-alerts-preflight",
                    "destination_key": "preflight-private-alerts",
                    "credential_secret_id": "preflight/slack/private-test-webhook",
                }
            },
        },
        "environments": [
            {
                "id": "dev",
                "customer": "Preflight",
                "account_id": "667653114001",
                "regions": ["us-east-1"],
                "route_id": "dev-alerts",
            }
        ],
    }
    reference = {
        "release_id": "a" * 64,
        "config": {
            "key": "apcf/releases/a/config.yaml",
            "version_id": "config-version",
            "sha256": "b" * 64,
            "schema_version": 4,
        },
        "inventory": {
            "key": "apcf/releases/a/inventory.json",
            "version_id": "inventory-version",
            "sha256": "c" * 64,
            "schema_version": 3,
        },
        "application_version": f"sha256:{'d' * 64}",
    }
    return LoadedRelease(release_id="a" * 64, config=config, inventory=inventory, reference=reference)


class SyntheticStateTests(unittest.TestCase):
    def test_synthetic_candidate_uses_owned_builders_and_is_unique(self):
        release = loaded_release()
        first, first_request = exercise._synthetic_candidate(
            release,
            run_id="load-1",
            sequence=1,
            created_at=AS_OF,
            destination_key="preflight-private-alerts",
        )
        second, _ = exercise._synthetic_candidate(
            release,
            run_id="load-1",
            sequence=2,
            created_at=AS_OF,
            destination_key="preflight-private-alerts",
        )

        self.assertNotEqual(first["candidate_id"], second["candidate_id"])
        self.assertTrue(first["announcement"]["title"].startswith("[TEST]"))
        self.assertEqual(first["route_id"], "dev-alerts")
        self.assertEqual(first_request["candidate"], first)
        self.assertEqual(first_request["destination_key"], "preflight-private-alerts")

    def test_seed_uses_the_owned_store_for_both_recovery_states(self):
        store = InMemoryOutboxStore()
        release = loaded_release()
        pending = exercise._seed(
            store,
            release,
            run_id="recovery-1",
            sequence=1,
            created_at=AS_OF,
            destination_key="preflight-private-alerts",
        )
        sending = exercise._seed(
            store,
            release,
            run_id="recovery-1",
            sequence=2,
            created_at=AS_OF,
            destination_key="preflight-private-alerts",
            status="sending",
        )

        pending_record = store.get_delivery(pending)
        self.assertIsNotNone(pending_record)
        assert pending_record is not None
        self.assertEqual(pending_record.status, "pending_queue")
        sending_record = store.get_delivery(sending)
        self.assertIsNotNone(sending_record)
        assert sending_record is not None
        self.assertEqual(sending_record.status, "sending")
        self.assertEqual(sending_record.lease_expires_at, int(AS_OF.timestamp()) - 1)
        self.assertEqual(sending_record.network_attempt_count, 0)

    def test_recovery_requires_exact_slack_attempt_counts(self):
        posted = SimpleNamespace(status="posted", network_attempt_count=1)
        unknown = SimpleNamespace(status="delivery_unknown", network_attempt_count=0)

        self.assertEqual(
            exercise._recovery_classification({"batchItemFailures": []}, posted, unknown),
            "passed",
        )
        posted.network_attempt_count = 2
        self.assertEqual(
            exercise._recovery_classification({"batchItemFailures": []}, posted, unknown),
            "failed",
        )


class PreviewRefusalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.deployment = yaml.safe_load((ROOT / "infra/preflight/deployment.yaml").read_text(encoding="utf-8"))
        self.deployment_path = self.root / "deployment.yaml"
        self.outputs = self.root / "outputs.json"
        self.saved_plan = self.root / "apply.tfplan"
        self.outputs.write_text("{}\n", encoding="utf-8")
        self.saved_plan.write_bytes(b"saved plan")

    def tearDown(self):
        self.temporary.cleanup()

    def preview(self):
        self.deployment_path.write_text(yaml.safe_dump(self.deployment, sort_keys=False), encoding="utf-8")
        return exercise.build_preview(
            Mock(),
            deployment_path=self.deployment_path,
            config_path=ROOT / "config/dev.yaml",
            terraform_output_path=self.outputs,
            terraform_plan_path=self.saved_plan,
            expected_account="667653114001",
            application_digest="d" * 64,
            application_version_id="version-1",
            protocol="recovery",
        )

    def test_persistent_deployment_identity_is_refused_before_aws(self):
        self.deployment["deployment_id"] = "dev"
        with self.assertRaisesRegex(exercise.ExerciseError, "reviewed preflight deployment"):
            self.preview()

    def test_operational_slack_channel_is_refused_before_aws(self):
        self.deployment["slack"]["routes"]["dev-alerts"]["channel_label"] = "#aws-change-alerts-dev"
        with self.assertRaisesRegex(exercise.ExerciseError, "not isolated"):
            self.preview()


class FixedLoadProtocolTests(unittest.TestCase):
    def plan(self):
        return {
            "protocol": "load",
            "aws": {"account": "667653114001"},
            "persistent_controls": {"terraform_state": {"version_id": "state-version"}},
            "application": {"digest": "d" * 64},
            "release": {"config_bucket": "bucket", "pointer_key": "apcf/active-versions.json"},
            "runtime": {
                "functions": {
                    "dispatcher": {"name": "apcf-preflight-outbox-dispatcher"},
                    "worker": {"name": "apcf-preflight-slack-worker"},
                }
            },
            "delivery": {
                "source_table": "apcf-source-state-preflight",
                "table": "apcf-delivery-preflight",
                "index": "status-next-action-index",
                "destination_key": "preflight-private-alerts",
                "queues": {
                    "delivery": {"name": "apcf-delivery-preflight.fifo"},
                    "delivery_dlq": {"name": "apcf-delivery-dlq-preflight.fifo"},
                    "runtime_failures": {"name": "apcf-runtime-failures-preflight"},
                },
            },
            "bounds": {
                "load_per_minute": 5,
                "load_minutes": 10,
                "load_total": 50,
                "load_drain_seconds": 300,
                "recovery_total": 2,
            },
        }

    def execute_run(self, metrics):
        current = [AS_OF]

        def clock():
            return current[0]

        def sleep(seconds):
            current[0] += timedelta(seconds=seconds)

        records = {
            f"candidate-{index}": SimpleNamespace(
                status="posted",
                slack_response={"response_class": "http_200", "latency_ms": 10},
                created_at=AS_OF.isoformat().replace("+00:00", "Z"),
                expires_at=int(AS_OF.timestamp()) + 365 * 86400,
                network_attempt_count=1,
            )
            for index in range(1, 51)
        }
        store = SimpleNamespace(
            get_delivery=lambda candidate: records[candidate],
            get_pace=lambda destination: SimpleNamespace(
                destination_key=destination,
                next_allowed_at=int(AS_OF.timestamp()),
                last_response_class="http_200",
                version=50,
            ),
        )
        seeded = []

        def seed(*args, sequence, **kwargs):
            del args, kwargs
            candidate = f"candidate-{sequence}"
            seeded.append((candidate, current[0]))
            return candidate

        queue = {
            "url": "url",
            "arn": "arn",
            "visible": 0,
            "in_flight": 0,
            "delayed": 0,
        }
        with (
            patch.object(exercise, "_fresh_preview", return_value=self.plan()),
            patch.object(exercise, "DynamoDBDeliveryStore", return_value=store),
            patch.object(exercise, "load_active_release", return_value=loaded_release()),
            patch.object(exercise, "_seed", side_effect=seed),
            patch.object(exercise, "_queue", side_effect=lambda clients, name: {"name": name, **queue}),
            patch.object(exercise, "_metric_evidence", return_value=metrics),
            patch.object(
                exercise,
                "_alarm_evidence",
                return_value=[{"name": "apcf-preflight-delivery-queue-age", "state": "OK", "transitions": []}],
            ),
            patch.object(
                exercise,
                "_log_evidence",
                return_value={
                    "dispatcher": {"active_streams": [{"name": "dispatcher-stream"}]},
                    "worker": {"active_streams": [{"name": "worker-stream"}]},
                },
            ),
            patch.object(
                exercise,
                "_persistent_controls",
                return_value=self.plan()["persistent_controls"],
            ),
            patch.object(
                exercise,
                "_persistent_candidate_absence",
                return_value={"checked": 50, "found": []},
            ),
        ):
            result = exercise.run_load(Mock(), self.plan(), clock=clock, sleep=sleep)
        return result, seeded, current[0]

    @staticmethod
    def required_metrics():
        return [
            {
                "Id": metric_id,
                "StatusCode": "Complete",
                "Values": [
                    1
                    if metric_id
                    in {
                        "dispatcher_invocations",
                        "worker_invocations",
                        "worker_duration",
                        "worker_concurrentexecutions",
                    }
                    else 0
                ],
            }
            for metric_id in (
                "dispatcher_invocations",
                "worker_invocations",
                "worker_duration",
                "worker_concurrentexecutions",
                "source_table_readthrottleevents",
                "source_table_writethrottleevents",
                "table_readthrottleevents",
                "table_writethrottleevents",
                "delivery_oldest_message_age",
                "delivery_dlq_oldest_message_age",
                "runtime_failures_oldest_message_age",
            )
        ]

    def test_exactly_five_per_minute_for_ten_minutes_is_not_extended(self):
        result, seeded, ended = self.execute_run(self.required_metrics())

        self.assertEqual(result["classification"], "passed")
        self.assertEqual(result["created"], 50)
        self.assertEqual(result["arrival_rate_per_hour"], 300)
        self.assertEqual(ended, AS_OF + timedelta(minutes=10))
        self.assertEqual(
            [sum(1 for _, time in seeded if time == AS_OF + timedelta(minutes=i)) for i in range(10)], [5] * 10
        )

    def test_missing_primary_metric_makes_an_otherwise_passing_run_incomplete(self):
        result, _, _ = self.execute_run(self.required_metrics()[:-1])
        self.assertEqual(result["classification"], "incomplete")


class TeardownGuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.binary = self.root / "destroy.tfplan"
        self.document = self.root / "destroy.json"
        self.binary.write_bytes(b"saved destroy plan")

    def tearDown(self):
        self.temporary.cleanup()

    def write_plan(self, address="module.runtime.aws_dynamodb_table.delivery", actions=None):
        self.document.write_text(
            json.dumps(
                {
                    "resource_changes": [
                        {
                            "address": address,
                            "change": {"actions": ["delete"] if actions is None else actions},
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

    def preview(self):
        return exercise.build_teardown_preview(
            terraform_plan_path=self.binary,
            terraform_plan_json_path=self.document,
            expected_account="667653114001",
            config_bucket="apcf-config-preflight-667653114001",
        )

    def test_exact_module_destroy_inventory_is_accepted(self):
        self.write_plan()
        preview = self.preview()
        self.assertEqual(preview["identity"]["state_key"], "apcf/preflight/terraform.tfstate")
        self.assertEqual(preview["resource_addresses"], ["module.runtime.aws_dynamodb_table.delivery"])

    def test_create_or_update_action_is_refused(self):
        self.write_plan(actions=["update"])
        with self.assertRaisesRegex(exercise.ExerciseError, "non-destroy action"):
            self.preview()

    def test_address_outside_the_preflight_module_is_refused(self):
        self.write_plan(address="aws_dynamodb_table.delivery")
        with self.assertRaisesRegex(exercise.ExerciseError, "escaped the preflight root"):
            self.preview()

    def test_persistent_config_bucket_is_refused(self):
        self.write_plan()
        with self.assertRaisesRegex(exercise.ExerciseError, "exact preflight bucket"):
            exercise.build_teardown_preview(
                terraform_plan_path=self.binary,
                terraform_plan_json_path=self.document,
                expected_account="667653114001",
                config_bucket="apcf-config-dev",
            )


class BucketRetirementTests(unittest.TestCase):
    def test_every_version_and_delete_marker_is_deleted_with_exact_ids(self):
        client = Mock()
        client.list_object_versions.side_effect = [
            {
                "Versions": [{"Key": "a", "VersionId": "1"}],
                "DeleteMarkers": [{"Key": "b", "VersionId": "2"}],
                "IsTruncated": False,
            },
            {"Versions": [], "DeleteMarkers": [], "IsTruncated": False},
        ]
        client.delete_objects.return_value = {}

        deleted = exercise._delete_preflight_bucket_versions(client, "apcf-config-preflight-667653114001")

        self.assertEqual(deleted, 2)
        self.assertEqual(
            client.delete_objects.call_args.kwargs["Delete"]["Objects"],
            [{"Key": "a", "VersionId": "1"}, {"Key": "b", "VersionId": "2"}],
        )


class ArtifactAndIamBoundaryTests(unittest.TestCase):
    def test_artifact_requires_exact_bytes_in_both_buckets(self):
        body = b"exact immutable package"
        digest = exercise.sha256_bytes(body)
        clients = Mock()
        clients.s3.head_object.side_effect = [
            {"VersionId": "source-version", "Metadata": {"sha256": digest}, "ContentLength": len(body)},
            {"VersionId": "catalog-version", "Metadata": {"sha256": digest}, "ContentLength": len(body)},
        ]
        clients.s3.get_object.side_effect = [
            {"Body": SimpleNamespace(read=lambda: body)},
            {"Body": SimpleNamespace(read=lambda: body)},
        ]

        artifact = exercise._artifact(
            clients,
            source_bucket="apcf-config-dev",
            catalog_bucket="apcf-config-preflight-667653114001",
            prefix="apcf/application-artifacts",
            digest=digest,
            version_id="source-version",
        )

        self.assertEqual(artifact["catalog_version_id"], "catalog-version")
        self.assertEqual(artifact["digest"], digest)

    def test_any_allowed_persistent_write_refuses_preview_boundary(self):
        clients = Mock()
        clients.iam.simulate_principal_policy.return_value = {"EvaluationResults": [{"EvalDecision": "allowed"}]}

        with self.assertRaisesRegex(exercise.ExerciseError, "can mutate a persistent dev"):
            exercise._assert_persistent_writes_denied(
                clients,
                roles={"feed_watcher": "arn:aws:iam::667653114001:role/apcf-preflight-feed-watcher"},
                account="667653114001",
                artifact_key="apcf/application-artifacts/" + "d" * 64 + ".zip",
            )

    def test_missing_persistent_post_run_evidence_is_incomplete(self):
        with patch.object(exercise, "_persistent_controls", side_effect=RuntimeError("unavailable")):
            evidence = exercise._persistent_boundary_evidence(
                Mock(),
                {"aws": {"account": "667653114001"}, "persistent_controls": {}},
                ["candidate-1"],
            )

        self.assertEqual(evidence["classification"], "incomplete")
        self.assertIsNone(evidence["controls_unchanged"])


if __name__ == "__main__":
    unittest.main()
