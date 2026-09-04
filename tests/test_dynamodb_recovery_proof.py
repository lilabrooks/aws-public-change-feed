from __future__ import annotations

import copy
import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("prove_dynamodb_recovery", ROOT / "scripts/prove_dynamodb_recovery.py")
assert SPEC is not None
recovery = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = recovery
SPEC.loader.exec_module(recovery)

STARTED = datetime(2026, 9, 3, 15, 0, tzinfo=UTC)
RESTORE = STARTED - timedelta(minutes=5)
SOURCE_NAMES = {
    "source_state": "apcf-source-state-dev",
    "delivery": "apcf-delivery-dev",
}


def timestamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def source_observation(kind: str) -> dict:
    name = SOURCE_NAMES[kind]
    return {
        "name": name,
        "arn": f"arn:aws:dynamodb:us-east-1:123456789012:table/{name}",
        "table_id": f"table-id-{kind}",
        "status": "ACTIVE",
        "reported_item_count": 1,
        "reported_size_bytes": 100,
        "schema": {"kind": kind},
        "pitr": {
            "status": "ENABLED",
            "period_days": 35,
            "earliest": timestamp(STARTED - timedelta(days=1)),
            "latest": timestamp(RESTORE),
        },
        "ttl": {"status": "ENABLED", "attribute": "expires_at"},
        "tags": [{"Key": "project", "Value": "aws-public-change-feed"}],
        "inventory": {
            "item_count": 1,
            "canonical_bytes": 20,
            "items_sha256": kind[0] * 64,
            "item_types": {"record": 1},
            "delivery_states": {},
            "ttl_cutoff_epoch": int((STARTED + timedelta(hours=4)).timestamp()),
            "protected": {
                "item_count": 1,
                "canonical_bytes": 20,
                "items_sha256": kind[0] * 64,
            },
            "ttl_eligible_by_deadline": {
                "item_count": 0,
                "canonical_bytes": 0,
                "items_sha256": "e" * 64,
                "item_digests": [],
            },
        },
    }


def context() -> dict:
    return {
        "deployment_id": "dev",
        "region": "us-east-1",
        "account_id": "123456789012",
        "recovery_role_arn": "arn:aws:iam::123456789012:role/apcf-dev-dynamodb-recovery",
        "git_sha": "a" * 40,
        "deployment_path": str((ROOT / "infra/central/deployment.yaml").resolve()),
        "deployment_sha256": "b" * 64,
        "terraform_output_path": "/reviewed/terraform-output.json",
        "terraform_output_sha256": "c" * 64,
        "primary_tables": copy.deepcopy(SOURCE_NAMES),
        "function_names": {
            "watcher": "apcf-dev-feed-watcher",
            "shadow": "apcf-dev-shadow-evaluator",
            "dispatcher": "apcf-dev-outbox-dispatcher",
            "worker": "apcf-dev-slack-worker",
            "reconciler": "apcf-dev-recovery-reconciler",
        },
        "queue_name": "apcf-delivery-dev.fifo",
        "queue_arn": "arn:aws:sqs:us-east-1:123456789012:apcf-delivery-dev.fifo",
        "trigger_states": {kind: False for kind in ("watcher", "dispatcher", "worker", "reconciler")},
        "pitr": {
            "cutover_enabled": False,
            "pitr_enabled": True,
            "plan_sha256": None,
            "recovery_period_days": 35,
        },
    }


def plan() -> dict:
    return {
        "plan_version": 1,
        "decision": "ADR-027",
        "exercise_id": "l41-proof",
        "operator": "reviewer",
        "started_at": timestamp(STARTED),
        "deadline_at": timestamp(STARTED + timedelta(hours=4)),
        "restore_at": timestamp(RESTORE),
        "rpo_seconds": 300,
        "rpo_observation": {
            "latest_restorable_times": {
                "delivery": timestamp(RESTORE),
                "source_state": timestamp(RESTORE),
            },
            "observed_seconds": 300,
            "nominal_target_met": True,
            "selection": "shared_provider_latest",
        },
        "rto_seconds": 14400,
        "recovery_period_days": 35,
        "max_inventory_items": 100,
        "max_inventory_bytes": 10000,
        "context": context(),
        "controls": {"quiescent": True},
        "aws_identity": {
            "account_id": "123456789012",
            "arn": "arn:aws:sts::123456789012:assumed-role/apcf-dev-dynamodb-recovery/proof",
            "user_id": "AROATEST:proof",
        },
        "source_tables": {kind: source_observation(kind) for kind in SOURCE_NAMES},
        "target_tables": {kind: f"{name}-restore-l41-proof" for kind, name in SOURCE_NAMES.items()},
    }


class Sts:
    def get_caller_identity(self):
        return {
            "Account": "123456789012",
            "Arn": "arn:aws:sts::123456789012:assumed-role/apcf-dev-dynamodb-recovery/proof",
            "UserId": "AROATEST:proof",
        }


class ScanClient:
    def __init__(self, items):
        self.items = items
        self.calls = []

    def scan(self, **arguments):
        self.calls.append(arguments)
        return {"Items": self.items}


class RepeatingCursorClient:
    def scan(self, **arguments):
        return {"Items": [], "LastEvaluatedKey": {"PK": {"S": "same"}}}


class WrongRoleSts:
    def get_caller_identity(self):
        return {
            "Account": "123456789012",
            "Arn": "arn:aws:sts::123456789012:assumed-role/another-role/proof",
            "UserId": "AROATEST:proof",
        }


class RecoveryProofTests(unittest.TestCase):
    def test_plan_file_is_canonical_and_digest_bound(self):
        document = plan()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            digest = recovery.write_preview(path, document)

            self.assertEqual(recovery.load_plan(path, digest), document)
            path.write_bytes(path.read_bytes() + b" ")
            with self.assertRaisesRegex(recovery.RecoveryProofError, "digest or canonical bytes differ"):
                recovery.load_plan(path, digest)

    def test_saved_plan_deadline_and_derived_names_are_recomputed(self):
        for field, value in (
            ("deadline_at", timestamp(STARTED + timedelta(hours=5))),
            ("restore_at", timestamp(STARTED - timedelta(minutes=4))),
            ("target_tables", {"source_state": "wrong", "delivery": "also-wrong"}),
        ):
            with self.subTest(field=field):
                document = plan()
                document[field] = value
                with self.assertRaisesRegex(recovery.RecoveryProofError, "differs"):
                    recovery._validate_saved_plan(document)

    def test_saved_plan_requires_a_recovery_role_session_name(self):
        document = plan()
        document["aws_identity"]["arn"] = "arn:aws:sts::123456789012:assumed-role/apcf-dev-dynamodb-recovery/"

        with self.assertRaisesRegex(recovery.RecoveryProofError, "caller role differs"):
            recovery._validate_saved_plan(document)

    def test_stale_recovery_start_refuses_before_local_or_aws_reads(self):
        unused = mock.Mock()
        with mock.patch.object(recovery, "_local_context", side_effect=AssertionError("unexpected local read")):
            with self.assertRaisesRegex(recovery.RecoveryProofError, "current recovery start"):
                recovery.create_preview(
                    unused,
                    deployment_path=Path("missing.yaml"),
                    terraform_output_path=Path("missing.json"),
                    exercise_id="l41-proof",
                    operator="reviewer",
                    started_at=timestamp(STARTED - timedelta(seconds=61)),
                    max_inventory_items=10,
                    max_inventory_bytes=1000,
                    now=STARTED,
                )

    def test_inventory_safety_maximum_refuses_before_local_or_aws_reads(self):
        unused = mock.Mock()
        with mock.patch.object(recovery, "_local_context", side_effect=AssertionError("unexpected local read")):
            with self.assertRaisesRegex(recovery.RecoveryProofError, "fixed safety maximum"):
                recovery.create_preview(
                    unused,
                    deployment_path=Path("missing.yaml"),
                    terraform_output_path=Path("missing.json"),
                    exercise_id="l41-proof",
                    operator="reviewer",
                    started_at=timestamp(STARTED),
                    max_inventory_items=recovery.MAX_INVENTORY_ITEMS + 1,
                    max_inventory_bytes=1000,
                    now=STARTED,
                )

    def test_preview_derives_one_shared_restore_time_and_records_provider_lag(self):
        clients = recovery.AwsClients(Sts(), mock.Mock(), mock.Mock(), mock.Mock(), mock.Mock())
        controls = {"quiescent": True}
        observations = {kind: source_observation(kind) for kind in SOURCE_NAMES}
        observations["delivery"]["pitr"]["latest"] = timestamp(STARTED - timedelta(minutes=5, seconds=20))
        with (
            mock.patch.object(recovery, "_local_context", return_value=context()),
            mock.patch.object(recovery, "_runtime_controls", return_value=controls),
            mock.patch.object(
                recovery,
                "_observe_table",
                side_effect=lambda _, name, **__: observations[
                    "source_state" if name == SOURCE_NAMES["source_state"] else "delivery"
                ],
            ),
            mock.patch.object(recovery, "_describe_optional", return_value=None),
        ):
            document = recovery.create_preview(
                clients,
                deployment_path=Path("deployment.yaml"),
                terraform_output_path=Path("terraform-output.json"),
                exercise_id="l41-proof",
                operator="reviewer",
                started_at=timestamp(STARTED),
                max_inventory_items=100,
                max_inventory_bytes=10000,
                now=STARTED,
            )

        self.assertEqual(document["restore_at"], timestamp(STARTED - timedelta(minutes=5, seconds=20)))
        self.assertEqual(document["rpo_observation"]["observed_seconds"], 320)
        self.assertFalse(document["rpo_observation"]["nominal_target_met"])
        self.assertEqual(
            document["target_tables"],
            {kind: f"{name}-restore-l41-proof" for kind, name in SOURCE_NAMES.items()},
        )
        self.assertEqual(document["deadline_at"], timestamp(STARTED + timedelta(hours=4)))

    def test_preview_requires_the_scoped_recovery_role_before_runtime_reads(self):
        clients = recovery.AwsClients(WrongRoleSts(), mock.Mock(), mock.Mock(), mock.Mock(), mock.Mock())
        with (
            mock.patch.object(recovery, "_local_context", return_value=context()),
            mock.patch.object(recovery, "_runtime_controls", side_effect=AssertionError("unexpected runtime read")),
        ):
            with self.assertRaisesRegex(recovery.RecoveryProofError, "reviewed DynamoDB recovery role"):
                recovery.create_preview(
                    clients,
                    deployment_path=Path("deployment.yaml"),
                    terraform_output_path=Path("terraform-output.json"),
                    exercise_id="l41-proof",
                    operator="reviewer",
                    started_at=timestamp(STARTED),
                    max_inventory_items=100,
                    max_inventory_bytes=10000,
                    now=STARTED,
                )

    def test_actionable_delivery_state_refuses_preview(self):
        clients = recovery.AwsClients(Sts(), mock.Mock(), mock.Mock(), mock.Mock(), mock.Mock())
        delivery = source_observation("delivery")
        delivery["inventory"]["delivery_states"] = {"queued": 1}
        observations = {"source_state": source_observation("source_state"), "delivery": delivery}
        with (
            mock.patch.object(recovery, "_local_context", return_value=context()),
            mock.patch.object(recovery, "_runtime_controls", return_value={"quiescent": True}),
            mock.patch.object(
                recovery,
                "_observe_table",
                side_effect=lambda _, name, **__: observations[
                    "source_state" if name == SOURCE_NAMES["source_state"] else "delivery"
                ],
            ),
            mock.patch.object(recovery, "_describe_optional", return_value=None),
        ):
            with self.assertRaisesRegex(recovery.RecoveryProofError, "actionable work"):
                recovery.create_preview(
                    clients,
                    deployment_path=Path("deployment.yaml"),
                    terraform_output_path=Path("terraform-output.json"),
                    exercise_id="l41-proof",
                    operator="reviewer",
                    started_at=timestamp(STARTED),
                    max_inventory_items=100,
                    max_inventory_bytes=10000,
                    now=STARTED,
                )

    def test_runtime_controls_prove_every_quiescence_branch(self):
        current = context()

        def clients_for(mutation=None):
            lambda_client = mock.Mock()

            def configuration(*, FunctionName):
                variables = {"DELIVERY_TABLE_NAME": SOURCE_NAMES["delivery"]}
                kind = next(kind for kind, name in current["function_names"].items() if name == FunctionName)
                if kind == "watcher":
                    variables["SOURCE_STATE_TABLE_NAME"] = SOURCE_NAMES["source_state"]
                if mutation == ("binding", kind):
                    variables["DELIVERY_TABLE_NAME"] = "wrong-table"
                return {"Environment": {"Variables": variables}}

            lambda_client.get_function_configuration.side_effect = configuration
            lambda_client.get_function_concurrency.return_value = {
                "ReservedConcurrentExecutions": 1 if mutation == ("concurrency", "watcher") else 0
            }
            if mutation == ("mapping", "missing"):
                mappings = []
            else:
                mapping_state = "Enabled" if mutation == ("mapping", "enabled") else "Disabled"
                mappings = [{"State": mapping_state}]
            lambda_client.list_event_source_mappings.return_value = {"EventSourceMappings": mappings}

            events = mock.Mock()

            def rule(*, Name):
                kind = next(kind for kind, name in current["function_names"].items() if name == Name)
                return {"State": "ENABLED" if mutation == ("schedule", kind) else "DISABLED"}

            events.describe_rule.side_effect = rule
            sqs = mock.Mock()
            sqs.get_queue_url.return_value = {"QueueUrl": "https://sqs.example/queue"}
            attributes = {name: "0" for name in recovery.QUEUE_COUNT_ATTRIBUTES}
            if mutation is not None and mutation[0] == "queue_nonzero":
                attributes[mutation[1]] = "1"
            if mutation is not None and mutation[0] == "queue_missing":
                del attributes[mutation[1]]
            sqs.get_queue_attributes.return_value = {"Attributes": attributes}
            return recovery.AwsClients(mock.Mock(), mock.Mock(), lambda_client, events, sqs)

        controls = recovery._runtime_controls(clients_for(), current)
        self.assertEqual(controls["watcher_reserved_concurrency"], 0)
        self.assertEqual(controls["worker_mapping_state"], "Disabled")
        self.assertEqual(set(controls["queue_counts"]), set(recovery.QUEUE_COUNT_ATTRIBUTES))

        cases = [
            *((("binding", kind), "table binding") for kind in ("watcher", "dispatcher", "worker", "reconciler")),
            (("concurrency", "watcher"), "reserved concurrency"),
            *(
                (("schedule", kind), "schedules must all be disabled")
                for kind in ("watcher", "dispatcher", "reconciler")
            ),
            (("mapping", "missing"), "exactly one disabled mapping"),
            (("mapping", "enabled"), "exactly one disabled mapping"),
            *((("queue_nonzero", name), "queue must be empty") for name in recovery.QUEUE_COUNT_ATTRIBUTES),
            *((("queue_missing", name), "omitted a requested counter") for name in recovery.QUEUE_COUNT_ATTRIBUTES),
        ]
        for mutation, message in cases:
            with self.subTest(mutation=mutation):
                with self.assertRaisesRegex(recovery.RecoveryProofError, message):
                    recovery._runtime_controls(clients_for(mutation), current)

    def test_inventory_is_order_independent_strongly_consistent_and_capped(self):
        first = {"PK": {"S": "B"}, "item_type": {"S": "record"}}
        second = {"PK": {"S": "A"}, "status": {"S": "sent"}}
        forward = ScanClient([first, second])
        reverse = ScanClient([second, first])

        left = recovery._inventory(forward, "table", max_items=2, max_bytes=1000)
        right = recovery._inventory(reverse, "table", max_items=2, max_bytes=1000)

        self.assertEqual(left, right)
        self.assertTrue(forward.calls[0]["ConsistentRead"])
        with self.assertRaisesRegex(recovery.RecoveryProofError, "inventory exceeded"):
            recovery._inventory(ScanClient([first, second]), "table", max_items=1, max_bytes=1000)
        with self.assertRaisesRegex(recovery.RecoveryProofError, "cursor repeated"):
            recovery._inventory(RepeatingCursorClient(), "table", max_items=1, max_bytes=1000)

    def test_inventory_canonicalizes_sets_without_reordering_lists(self):
        first: dict = {
            "PK": {"S": "set-item"},
            "sets": {
                "M": {
                    "strings": {"SS": ["alpha", "beta"]},
                    "numbers": {"NS": ["1", "2"]},
                    "binary": {"BS": [b"alpha", b"beta"]},
                }
            },
            "ordered": {"L": [{"S": "first"}, {"S": "second"}]},
        }
        reordered_sets = copy.deepcopy(first)
        reordered_sets["sets"]["M"]["strings"]["SS"].reverse()
        reordered_sets["sets"]["M"]["numbers"]["NS"].reverse()
        reordered_sets["sets"]["M"]["binary"]["BS"].reverse()
        reordered_list = copy.deepcopy(first)
        reordered_list["ordered"]["L"].reverse()

        expected = recovery._inventory(ScanClient([first]), "source", max_items=1, max_bytes=5000)
        same_sets = recovery._inventory(ScanClient([reordered_sets]), "target", max_items=1, max_bytes=5000)
        different_list = recovery._inventory(ScanClient([reordered_list]), "target", max_items=1, max_bytes=5000)

        self.assertEqual(same_sets, expected)
        self.assertNotEqual(different_list, expected)

    def test_inventory_allows_only_ttl_eligible_deletions_before_the_deadline(self):
        cutoff = int((STARTED + timedelta(hours=4)).timestamp())
        protected = {"PK": {"S": "protected"}, "expires_at": {"N": str(cutoff + 1)}}
        expiring = {"PK": {"S": "expiring"}, "expires_at": {"N": str(cutoff)}}
        source = recovery._inventory(
            ScanClient([protected, expiring]),
            "source",
            max_items=2,
            max_bytes=1000,
            ttl_cutoff_epoch=cutoff,
        )
        after_ttl = recovery._inventory(
            ScanClient([protected]),
            "target",
            max_items=2,
            max_bytes=1000,
            ttl_cutoff_epoch=cutoff,
        )
        changed_expiring = recovery._inventory(
            ScanClient([protected, {**expiring, "value": {"S": "changed"}}]),
            "target",
            max_items=2,
            max_bytes=1000,
            ttl_cutoff_epoch=cutoff,
        )
        missing_protected = recovery._inventory(
            ScanClient([expiring]),
            "target",
            max_items=2,
            max_bytes=1000,
            ttl_cutoff_epoch=cutoff,
        )

        self.assertTrue(recovery._inventory_matches(after_ttl, source))
        self.assertFalse(recovery._inventory_matches(changed_expiring, source))
        self.assertFalse(recovery._inventory_matches(missing_protected, source))

    def test_restore_is_idempotent_only_for_the_exact_source_and_time(self):
        source = source_observation("source_state")
        exact = {
            "TableStatus": "CREATING",
            "RestoreSummary": {"SourceTableArn": source["arn"], "RestoreDateTime": RESTORE},
        }
        client = mock.Mock()
        with mock.patch.object(recovery, "_describe_optional", return_value=exact):
            result = recovery._start_restore(
                client,
                source=source,
                target_name="apcf-source-state-dev-restore-l41-proof",
                restore_at=timestamp(RESTORE),
            )
        self.assertEqual(result["status"], "observed")
        client.restore_table_to_point_in_time.assert_not_called()

        conflicting = {
            "TableStatus": "CREATING",
            "RestoreSummary": {"SourceTableArn": source["arn"], "RestoreDateTime": STARTED},
        }
        with mock.patch.object(recovery, "_describe_optional", return_value=conflicting):
            with self.assertRaisesRegex(recovery.RecoveryProofError, "different restore identity"):
                recovery._start_restore(
                    client,
                    source=source,
                    target_name="apcf-source-state-dev-restore-l41-proof",
                    restore_at=timestamp(RESTORE),
                )

        conflicting_source = {
            "TableStatus": "CREATING",
            "RestoreSummary": {
                "SourceTableArn": source_observation("delivery")["arn"],
                "RestoreDateTime": RESTORE,
            },
        }
        with mock.patch.object(recovery, "_describe_optional", return_value=conflicting_source):
            with self.assertRaisesRegex(recovery.RecoveryProofError, "different restore identity"):
                recovery._start_restore(
                    client,
                    source=source,
                    target_name="apcf-source-state-dev-restore-l41-proof",
                    restore_at=timestamp(RESTORE),
                )

    def test_failed_restore_response_requires_exact_destination_reread(self):
        source = source_observation("source_state")
        exact = {
            "TableStatus": "CREATING",
            "RestoreSummary": {"SourceTableArn": source["arn"], "RestoreDateTime": RESTORE},
        }
        client = mock.Mock()
        client.restore_table_to_point_in_time.side_effect = RuntimeError("provider details")
        with mock.patch.object(recovery, "_describe_optional", side_effect=[None, exact]):
            result = recovery._start_restore(
                client,
                source=source,
                target_name="apcf-source-state-dev-restore-l41-proof",
                restore_at=timestamp(RESTORE),
            )
        self.assertEqual(result["status"], "accepted_after_reread")

        with mock.patch.object(recovery, "_describe_optional", side_effect=[None, None]):
            with self.assertRaisesRegex(recovery.RecoveryProofError, "without an exact destination") as raised:
                recovery._start_restore(
                    client,
                    source=source,
                    target_name="apcf-source-state-dev-restore-l41-proof",
                    restore_at=timestamp(RESTORE),
                )
        self.assertEqual(raised.exception.status, "ambiguous")

    def test_apply_starts_both_restores_with_the_same_bound_timestamp(self):
        document = plan()
        clients = recovery.AwsClients(mock.Mock(), mock.Mock(), mock.Mock(), mock.Mock(), mock.Mock())
        calls = []

        def start(_, *, source, target_name, restore_at):
            calls.append((source["name"], target_name, restore_at))
            return {"status": "accepted", "table_status": "CREATING"}

        with (
            mock.patch.object(recovery, "_fresh_preconditions"),
            mock.patch.object(recovery, "_start_restore", side_effect=start),
            mock.patch.object(recovery, "_describe_optional", return_value=None),
            mock.patch.object(
                recovery,
                "status_plan",
                return_value={
                    "restore_stage_status": "incomplete",
                    "exercise_status": "incomplete_pending_cutover_rollback_and_trigger_restoration",
                    "cutover_input": None,
                },
            ) as status,
        ):
            result = recovery.apply_plan(clients, document, plan_sha256="d" * 64, now=STARTED)

        self.assertEqual(len(calls), 2)
        self.assertEqual({call[2] for call in calls}, {timestamp(RESTORE)})
        self.assertEqual({call[0] for call in calls}, set(SOURCE_NAMES.values()))
        self.assertEqual(result["restore_stage_status"], "incomplete")
        self.assertIsNone(result["cutover_input"])
        status.assert_called_once_with(clients, document, verify_preconditions=False)

    def test_second_restore_failure_is_reported_as_partial(self):
        document = plan()
        clients = recovery.AwsClients(mock.Mock(), mock.Mock(), mock.Mock(), mock.Mock(), mock.Mock())
        outcomes = [
            {"status": "accepted", "table_status": "CREATING"},
            recovery.RecoveryProofError("ambiguous", "second restore is uncertain"),
        ]
        with (
            mock.patch.object(recovery, "_fresh_preconditions"),
            mock.patch.object(recovery, "_start_restore", side_effect=outcomes),
        ):
            result = recovery.apply_plan(clients, document, plan_sha256="d" * 64, now=STARTED)

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["started"], ["source_state"])
        self.assertEqual(result["detail"], "second restore is uncertain")

    def test_target_configuration_repairs_only_ttl_pitr_and_tags(self):
        client = mock.Mock()
        table = {"TableName": "target", "TableArn": "arn:target"}
        with (
            mock.patch.object(recovery, "_ttl", return_value={"status": "DISABLED", "attribute": None}),
            mock.patch.object(recovery, "_pitr", return_value={"status": "DISABLED", "period_days": None}),
            mock.patch.object(
                recovery,
                "_tags",
                return_value=[{"Key": "old", "Value": "remove"}],
            ),
        ):
            changes = recovery._configure_target(
                client,
                table=table,
                expected_tags=[{"Key": "project", "Value": "aws-public-change-feed"}],
            )

        self.assertEqual(changes, ["pitr", "tags", "ttl"])
        client.update_time_to_live.assert_called_once()
        client.update_continuous_backups.assert_called_once()
        client.tag_resource.assert_called_once()
        client.untag_resource.assert_called_once()
        for forbidden in ("put_item", "update_item", "delete_item", "batch_write_item", "delete_table"):
            getattr(client, forbidden).assert_not_called()

        client.reset_mock()
        expected_tags = [{"Key": "project", "Value": "aws-public-change-feed"}]
        with (
            mock.patch.object(recovery, "_ttl", return_value={"status": "ENABLING", "attribute": "expires_at"}),
            mock.patch.object(recovery, "_pitr", return_value={"status": "ENABLED", "period_days": 35}),
            mock.patch.object(recovery, "_tags", return_value=expected_tags),
        ):
            self.assertEqual(recovery._configure_target(client, table=table, expected_tags=expected_tags), [])
        client.update_time_to_live.assert_not_called()

    def test_expired_rto_refuses_before_any_restore(self):
        document = plan()
        clients = recovery.AwsClients(mock.Mock(), mock.Mock(), mock.Mock(), mock.Mock(), mock.Mock())
        with (
            mock.patch.object(recovery, "_fresh_preconditions"),
            mock.patch.object(recovery, "_start_restore") as start,
        ):
            result = recovery.apply_plan(
                clients,
                document,
                plan_sha256="d" * 64,
                now=STARTED + timedelta(hours=4, seconds=1),
            )

        self.assertEqual(result["status"], "rto_refused")
        start.assert_not_called()

    def test_configuration_exception_after_restore_is_partial(self):
        document = plan()
        clients = recovery.AwsClients(mock.Mock(), mock.Mock(), mock.Mock(), mock.Mock(), mock.Mock())
        target = {"TableStatus": "ACTIVE"}
        with (
            mock.patch.object(recovery, "_fresh_preconditions"),
            mock.patch.object(
                recovery,
                "_start_restore",
                return_value={"status": "accepted", "table_status": "ACTIVE"},
            ),
            mock.patch.object(recovery, "_describe_optional", return_value=target),
            mock.patch.object(recovery, "_configure_target", side_effect=RuntimeError("provider detail")),
        ):
            result = recovery.apply_plan(clients, document, plan_sha256="d" * 64, now=STARTED)

        self.assertEqual(result["status"], "partial")
        self.assertNotIn("provider detail", result["detail"])

    def test_status_rechecks_preconditions_and_enforces_deadline(self):
        document = plan()
        clients = recovery.AwsClients(mock.Mock(), mock.Mock(), mock.Mock(), mock.Mock(), mock.Mock())
        target_inventories = {
            kind: copy.deepcopy(document["source_tables"][kind]["inventory"]) for kind in SOURCE_NAMES
        }

        def target(_, name):
            kind = "source_state" if "source-state" in name else "delivery"
            source = document["source_tables"][kind]
            return {
                "TableStatus": "ACTIVE",
                "TableArn": f"arn:target:{kind}",
                "RestoreSummary": {"SourceTableArn": source["arn"], "RestoreDateTime": RESTORE},
            }

        with (
            mock.patch.object(recovery, "_fresh_preconditions") as preconditions,
            mock.patch.object(recovery, "_describe_optional", side_effect=target),
            mock.patch.object(
                recovery,
                "_schema",
                side_effect=lambda table: {
                    "kind": "source_state" if "source_state" in table["TableArn"] else "delivery"
                },
            ),
            mock.patch.object(recovery, "_ttl", return_value={"status": "ENABLED", "attribute": "expires_at"}),
            mock.patch.object(recovery, "_pitr", return_value={"status": "ENABLED", "period_days": 35}),
            mock.patch.object(
                recovery,
                "_tags",
                return_value=[{"Key": "project", "Value": "aws-public-change-feed"}],
            ),
            mock.patch.object(
                recovery,
                "_inventory",
                side_effect=lambda _, name, **__: target_inventories[
                    "source_state" if "source-state" in name else "delivery"
                ],
            ),
        ):
            completed = recovery.status_plan(clients, document, now=STARTED + timedelta(hours=3))
            late = recovery.status_plan(clients, document, now=STARTED + timedelta(hours=5))
            target_inventories["delivery"]["protected"]["item_count"] = 2
            mismatched = recovery.status_plan(clients, document, now=STARTED + timedelta(hours=3))

        self.assertEqual(completed["restore_stage_status"], "completed")
        self.assertEqual(completed["exercise_status"], "incomplete_pending_cutover_rollback_and_trigger_restoration")
        self.assertNotIn("status", completed)
        self.assertIsNotNone(completed["cutover_input"])
        self.assertEqual(late["restore_stage_status"], "incomplete")
        self.assertFalse(late["deadline_met"])
        self.assertIsNone(late["cutover_input"])
        self.assertEqual(mismatched["restore_stage_status"], "incomplete")
        self.assertIsNone(mismatched["cutover_input"])
        self.assertEqual(preconditions.call_count, 3)

    def test_restore_window_is_inclusive_and_refuses_outside_bounds(self):
        table = source_observation("source_state")
        earliest = STARTED - timedelta(days=1)
        latest = RESTORE

        recovery._assert_restore_window(table, earliest)
        recovery._assert_restore_window(table, latest)
        for outside in (earliest - timedelta(seconds=1), latest + timedelta(seconds=1)):
            with self.subTest(outside=outside):
                with self.assertRaisesRegex(recovery.RecoveryProofError, "outside a source table PITR window"):
                    recovery._assert_restore_window(table, outside)

    def test_main_maps_machine_results_and_bounded_failures_to_exit_codes(self):
        arguments = ["status", "--plan", "plan.json", "--expected-plan-sha256", "d" * 64]
        result_cases = (
            ({"restore_stage_status": "completed"}, 0),
            ({"restore_stage_status": "incomplete"}, recovery.EXIT_REFUSED),
            ({"status": "partial"}, recovery.EXIT_AMBIGUOUS),
            ({"status": "ambiguous"}, recovery.EXIT_AMBIGUOUS),
        )
        for result, expected in result_cases:
            with self.subTest(result=result):
                with (
                    mock.patch.object(recovery, "load_plan", return_value=plan()),
                    mock.patch.object(recovery, "status_plan", return_value=result),
                    mock.patch.object(recovery, "_write"),
                ):
                    self.assertEqual(recovery.main(arguments, clients=mock.Mock()), expected)

        error_cases = (
            (recovery.RecoveryProofError("invalid_input", "invalid"), recovery.EXIT_INVALID),
            (recovery.RecoveryProofError("state_refused", "refused"), recovery.EXIT_REFUSED),
            (recovery.RecoveryProofError("provider_error", "provider"), recovery.EXIT_AMBIGUOUS),
            (RuntimeError("provider detail"), recovery.EXIT_AMBIGUOUS),
        )
        for error, expected in error_cases:
            with self.subTest(error=type(error).__name__, expected=expected):
                with (
                    mock.patch.object(recovery, "load_plan", return_value=plan()),
                    mock.patch.object(recovery, "status_plan", side_effect=error),
                    mock.patch.object(recovery, "_write") as write,
                ):
                    self.assertEqual(recovery.main(arguments, clients=mock.Mock()), expected)
                    write.assert_called_once()

    def test_local_context_rejects_cutover_and_unpaused_watcher(self):
        def output(value):
            return {"sensitive": False, "type": "dynamic", "value": value}

        outputs = {
            "primary_source_state_table": output(SOURCE_NAMES["source_state"]),
            "primary_delivery_table": output(SOURCE_NAMES["delivery"]),
            "source_state_table": output(SOURCE_NAMES["source_state"]),
            "delivery_table": output(SOURCE_NAMES["delivery"]),
            "dynamodb_recovery": output(context()["pitr"]),
            "runtime_trigger_states": output(context()["trigger_states"]),
            "watcher_execution_paused": output(True),
            "function_names": output(context()["function_names"]),
            "delivery_queue": output(context()["queue_name"]),
            "delivery_queue_arn": output(context()["queue_arn"]),
            "roles": output({"dynamodb_recovery": context()["recovery_role_arn"]}),
        }
        deployment = {
            "deployment_id": "dev",
            "deployment_region": "us-east-1",
            "environments": [{"account_id": "123456789012"}],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            deployment_path = ROOT / "infra/central/deployment.yaml"
            output_path = root / "outputs.json"
            output_path.write_text(json.dumps(outputs), encoding="utf-8")
            with (
                mock.patch.object(
                    recovery, "_read_mapping", side_effect=[(deployment, b"deployment"), (outputs, b"outputs")]
                ),
                mock.patch.object(recovery, "_git_sha", return_value="a" * 40),
            ):
                self.assertEqual(recovery._local_context(deployment_path, output_path)["primary_tables"], SOURCE_NAMES)
            outputs["source_state_table"]["value"] = f"{SOURCE_NAMES['source_state']}-restore-l41-proof"
            outputs["delivery_table"]["value"] = f"{SOURCE_NAMES['delivery']}-restore-l41-proof"
            outputs["dynamodb_recovery"]["value"] = {
                **context()["pitr"],
                "cutover_enabled": True,
                "plan_sha256": "a" * 64,
            }
            with (
                mock.patch.object(
                    recovery, "_read_mapping", side_effect=[(deployment, b"deployment"), (outputs, b"outputs")]
                ),
                mock.patch.object(recovery, "_git_sha", return_value="a" * 40),
            ):
                with self.assertRaisesRegex(recovery.RecoveryProofError, "already bound to a recovery table"):
                    recovery._local_context(deployment_path, output_path)
            outputs["source_state_table"]["value"] = SOURCE_NAMES["source_state"]
            outputs["delivery_table"]["value"] = SOURCE_NAMES["delivery"]
            outputs["dynamodb_recovery"]["value"] = context()["pitr"]
            outputs["watcher_execution_paused"]["value"] = False
            with (
                mock.patch.object(
                    recovery, "_read_mapping", side_effect=[(deployment, b"deployment"), (outputs, b"outputs")]
                ),
                mock.patch.object(recovery, "_git_sha", return_value="a" * 40),
            ):
                with self.assertRaisesRegex(recovery.RecoveryProofError, "watcher reserved concurrency"):
                    recovery._local_context(deployment_path, output_path)


if __name__ == "__main__":
    unittest.main()
