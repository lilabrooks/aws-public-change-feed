from __future__ import annotations

import copy
import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("retire_lambda_artifacts", ROOT / "scripts/retire_lambda_artifacts.py")
assert SPEC is not None
retirement = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(retirement)

AS_OF = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
PREFIX = "apcf/application-artifacts"
BUCKET = "apcf-config-dev"


class FakeError(Exception):
    def __init__(self, status: int, code: str = "Failure"):
        super().__init__("provider detail must never be emitted")
        self.response = {"ResponseMetadata": {"HTTPStatusCode": status}, "Error": {"Code": code}}


def package(digit: str, days_old: int, version: str | None = None, *, timestamp: datetime | None = None):
    digest = digit * 64
    return {
        "digest": digest,
        "key": f"{PREFIX}/{digest}.zip",
        "version_id": version or f"version-{digit}",
        "etag": f'"etag-{digit}"',
        "last_modified": timestamp or AS_OF - timedelta(days=days_old),
        "size": 100,
        "metadata": {"sha256": digest},
    }


class FakeS3:
    def __init__(self, packages, *, page_size=100):
        self.packages = [copy.deepcopy(item) for item in packages]
        self.page_size = page_size
        self.delete_markers = []
        self.delete_calls = []
        self.list_calls = []
        self.fail_list = False
        self.delete_error = None
        self.head_error_after_delete = None
        self.keep_after_delete = False
        self.final_inventory_mutation = None
        self.mutate_after_delete_calls = None
        self.final_inventory_mutated = False

    def list_object_versions(self, **kwargs):
        self.list_calls.append(kwargs)
        if self.fail_list:
            raise FakeError(500)
        if (
            self.final_inventory_mutation is not None
            and not self.final_inventory_mutated
            and len(self.delete_calls) == self.mutate_after_delete_calls
        ):
            self.final_inventory_mutation(self)
            self.final_inventory_mutated = True
        start = int(kwargs.get("KeyMarker", "0"))
        all_entries = [
            {
                "Key": item["key"],
                "VersionId": item["version_id"],
                "ETag": item["etag"],
                "LastModified": item["last_modified"],
                "Size": item["size"],
                "IsLatest": item.get("is_latest", True),
            }
            for item in self.packages
        ]
        end = min(start + self.page_size, len(all_entries))
        response = {
            "Versions": all_entries[start:end],
            "DeleteMarkers": copy.deepcopy(self.delete_markers) if start == 0 else [],
            "IsTruncated": end < len(all_entries),
        }
        if response["IsTruncated"]:
            response["NextKeyMarker"] = str(end)
            response["NextVersionIdMarker"] = f"page-{end}"
        return response

    def head_object(self, **kwargs):
        if self.head_error_after_delete is not None and self.delete_calls:
            raise self.head_error_after_delete
        for item in self.packages:
            if item["key"] == kwargs["Key"] and item["version_id"] == kwargs["VersionId"]:
                return {"Metadata": item["metadata"], "VersionId": item["version_id"], "ETag": item["etag"]}
        raise FakeError(404, "NoSuchVersion")

    def delete_object(self, **kwargs):
        self.delete_calls.append(kwargs)
        if self.delete_error is not None:
            raise self.delete_error
        if not self.keep_after_delete:
            self.packages = [
                item
                for item in self.packages
                if not (item["key"] == kwargs["Key"] and item["version_id"] == kwargs["VersionId"])
            ]
        return {"DeleteMarker": False}


def deployment():
    return {
        "deployment_sha256": "d" * 64,
        "bucket": BUCKET,
        "prefix": PREFIX,
        "retention_days": 400,
        "minimum_retained_packages": 10,
    }


def plan_for(client, packages_protected=()):
    return retirement.create_plan(
        client,
        deployment=deployment(),
        inventory_limit=100,
        protected=list(packages_protected),
        as_of=AS_OF,
    )


class DeploymentLoadingTests(unittest.TestCase):
    def test_loads_package_policy_and_derives_exact_prefix(self):
        loaded = retirement.load_deployment(
            ROOT / "infra/central/deployment.yaml", ROOT / "schemas/deployment.schema.json"
        )
        self.assertEqual(loaded["prefix"], "apcf/application-artifacts")
        self.assertEqual(loaded["retention_days"], 400)
        self.assertEqual(loaded["minimum_retained_packages"], 10)

    def test_hard_retention_floor_survives_an_older_weaker_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = yaml.safe_load((ROOT / "infra/central/deployment.yaml").read_text())
            document["application_package_retirement"]["retention_days"] = 399
            schema = json.loads((ROOT / "schemas/deployment.schema.json").read_text())
            schema["$defs"]["application_package_retirement"]["properties"]["retention_days"]["minimum"] = 1
            deployment_path = root / "deployment.yaml"
            schema_path = root / "schema.json"
            deployment_path.write_text(yaml.safe_dump(document))
            schema_path.write_text(json.dumps(schema))
            with self.assertRaisesRegex(retirement.RetirementError, "below 400"):
                retirement.load_deployment(deployment_path, schema_path)

    def test_hard_count_floor_survives_an_older_weaker_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = yaml.safe_load((ROOT / "infra/central/deployment.yaml").read_text())
            document["application_package_retirement"]["minimum_retained_packages"] = 9
            schema = json.loads((ROOT / "schemas/deployment.schema.json").read_text())
            field = schema["$defs"]["application_package_retirement"]["properties"]["minimum_retained_packages"]
            field["minimum"] = 1
            deployment_path = root / "deployment.yaml"
            schema_path = root / "schema.json"
            deployment_path.write_text(yaml.safe_dump(document))
            schema_path.write_text(json.dumps(schema))
            with self.assertRaisesRegex(retirement.RetirementError, "below 10"):
                retirement.load_deployment(deployment_path, schema_path)


class InventoryTests(unittest.TestCase):
    def test_complete_pagination_and_metadata_are_bound(self):
        client = FakeS3([package("a", 1), package("b", 2)], page_size=1)
        rows = retirement.inventory_packages(client, bucket=BUCKET, prefix=PREFIX, inventory_limit=2)
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(client.list_calls), 2)
        self.assertEqual(rows[0]["digest"], "a" * 64)

    def test_repeated_pagination_markers_are_refused(self):
        class RepeatingPaginationClient(FakeS3):
            def list_object_versions(self, **kwargs):
                self.list_calls.append(kwargs)
                if len(self.list_calls) > 2:
                    raise FakeError(500)
                return {
                    "Versions": [],
                    "DeleteMarkers": [],
                    "IsTruncated": True,
                    "NextKeyMarker": "same-key",
                    "NextVersionIdMarker": "same-version",
                }

        client = RepeatingPaginationClient([])
        with self.assertRaisesRegex(retirement.RetirementError, "pagination markers repeated"):
            retirement.inventory_packages(client, bucket=BUCKET, prefix=PREFIX, inventory_limit=1)
        self.assertEqual(len(client.list_calls), 2)

    def test_inventory_limit_stops_before_a_plan(self):
        client = FakeS3([package("a", 1), package("b", 2)])
        with self.assertRaisesRegex(retirement.RetirementError, "approved limit") as raised:
            retirement.inventory_packages(client, bucket=BUCKET, prefix=PREFIX, inventory_limit=1)
        self.assertEqual(raised.exception.status, "inventory_limit_exceeded")

    def test_delete_marker_refuses_the_complete_inventory(self):
        client = FakeS3([package("a", 1)])
        client.delete_markers = [{"Key": f"{PREFIX}/{'b' * 64}.zip", "VersionId": "marker"}]
        with self.assertRaisesRegex(retirement.RetirementError, "delete marker"):
            retirement.inventory_packages(client, bucket=BUCKET, prefix=PREFIX, inventory_limit=2)

    def test_malformed_key_is_refused(self):
        item = package("a", 1)
        item["key"] = f"{PREFIX}/UPPER.zip"
        with self.assertRaisesRegex(retirement.RetirementError, "malformed key"):
            retirement.inventory_packages(FakeS3([item]), bucket=BUCKET, prefix=PREFIX, inventory_limit=1)

    def test_metadata_mismatch_is_refused(self):
        item = package("a", 1)
        item["metadata"]["sha256"] = "b" * 64
        with self.assertRaisesRegex(retirement.RetirementError, "metadata"):
            retirement.inventory_packages(FakeS3([item]), bucket=BUCKET, prefix=PREFIX, inventory_limit=1)

    def test_noncurrent_version_is_refused(self):
        item = package("a", 1)
        item["is_latest"] = False
        with self.assertRaisesRegex(retirement.RetirementError, "not current"):
            retirement.inventory_packages(FakeS3([item]), bucket=BUCKET, prefix=PREFIX, inventory_limit=1)

    def test_required_inventory_identity_fields_cannot_be_empty(self):
        mutations = {
            "VersionId": ("version_id", ""),
            "ETag": ("etag", ""),
            "LastModified": ("last_modified", None),
            "size": ("size", -1),
        }
        for label, (field, value) in mutations.items():
            with self.subTest(label=label):
                item = package("a", 1)
                item[field] = value
                with self.assertRaises(retirement.RetirementError):
                    retirement.inventory_packages(FakeS3([item]), bucket=BUCKET, prefix=PREFIX, inventory_limit=1)

    def test_extra_version_history_is_refused(self):
        first = package("a", 1, "v1")
        second = package("a", 2, "v2")
        with self.assertRaisesRegex(retirement.RetirementError, "version history"):
            retirement.inventory_packages(FakeS3([first, second]), bucket=BUCKET, prefix=PREFIX, inventory_limit=2)

    def test_failed_listing_has_bounded_classification(self):
        client = FakeS3([])
        client.fail_list = True
        with self.assertRaises(retirement.RetirementError) as raised:
            retirement.inventory_packages(client, bucket=BUCKET, prefix=PREFIX, inventory_limit=1)
        self.assertEqual(raised.exception.status, "inventory_failed")
        self.assertNotIn("provider detail", str(raised.exception))


class ClassificationTests(unittest.TestCase):
    def rows(self, count=12):
        return retirement.inventory_packages(
            FakeS3([package(format(index, "x")[-1], 500 + index, f"v-{index}") for index in range(count)]),
            bucket=BUCKET,
            prefix=PREFIX,
            inventory_limit=100,
        )

    def classify(self, rows, protected=()):
        return retirement.classify_packages(
            rows,
            as_of=AS_OF,
            retention_days=400,
            minimum_retained_packages=10,
            protected=list(protected),
        )

    def test_fewer_than_floor_keeps_every_package(self):
        self.assertEqual(self.classify(self.rows(9)), [])

    def test_exact_floor_keeps_every_package(self):
        self.assertEqual(self.classify(self.rows(10)), [])

    def test_more_than_floor_retires_only_older_packages(self):
        self.assertEqual(len(self.classify(self.rows(12))), 2)

    def test_timestamp_tie_at_floor_keeps_all_tied_packages(self):
        items = [package(format(index, "x")[-1], index) for index in range(11)]
        items.append(package("b", 500, "oldest"))
        tied = AS_OF - timedelta(days=401)
        for index in (8, 9, 10):
            items[index]["last_modified"] = tied
        rows = retirement.inventory_packages(FakeS3(items), bucket=BUCKET, prefix=PREFIX, inventory_limit=20)
        candidates = self.classify(rows)
        self.assertEqual(len(candidates), 1)

    def test_age_cutoff_is_inclusive(self):
        items = [package(format(index, "x")[-1], index, f"recent-{index}") for index in range(10)]
        items.append(package("a", 400, "target"))
        rows = retirement.inventory_packages(FakeS3(items), bucket=BUCKET, prefix=PREFIX, inventory_limit=20)
        self.assertEqual(len(self.classify(rows)), 1)

    def test_one_second_after_cutoff_is_not_age_eligible(self):
        items = [package(format(index, "x")[-1], index, f"recent-{index}") for index in range(10)]
        items.append(package("a", 400, "target", timestamp=AS_OF - timedelta(days=400) + timedelta(seconds=1)))
        rows = retirement.inventory_packages(FakeS3(items), bucket=BUCKET, prefix=PREFIX, inventory_limit=20)
        self.assertEqual(self.classify(rows), [])

    def test_one_second_before_cutoff_is_age_eligible(self):
        items = [package(format(index, "x")[-1], index, f"recent-{index}") for index in range(10)]
        items.append(package("a", 400, "target", timestamp=AS_OF - timedelta(days=400) - timedelta(seconds=1)))
        rows = retirement.inventory_packages(FakeS3(items), bucket=BUCKET, prefix=PREFIX, inventory_limit=20)
        self.assertEqual(len(self.classify(rows)), 1)

    def test_protected_exact_pair_is_retained(self):
        rows = self.rows(11)
        oldest = min(rows, key=lambda row: row["last_modified"])
        protected = [{"digest": oldest["digest"], "version_id": oldest["version_id"]}]
        self.assertEqual(self.classify(rows, protected), [])


class PlanAndApplyTests(unittest.TestCase):
    def old_packages(self):
        return [package(format(index, "x")[-1], 500 + index, f"v-{index}") for index in range(12)]

    def test_preview_is_read_only_and_canonical_plan_digest_is_stable(self):
        client = FakeS3(self.old_packages())
        plan = plan_for(client)
        self.assertEqual(client.delete_calls, [])
        with tempfile.TemporaryDirectory() as directory:
            first = retirement.write_plan(Path(directory) / "first.json", plan)
            second = retirement.write_plan(Path(directory) / "second.json", plan)
        self.assertEqual(first, second)

    def test_absent_protected_reference_refuses_preview(self):
        client = FakeS3(self.old_packages())
        with self.assertRaisesRegex(retirement.RetirementError, "protected package"):
            plan_for(client, [{"digest": "f" * 64, "version_id": "missing"}])

    def test_new_inventory_row_stales_apply_before_delete(self):
        client = FakeS3(self.old_packages())
        plan = plan_for(client)
        client.packages.append(package("f", 1, "new"))
        with self.assertRaisesRegex(retirement.RetirementError, "differs from the preview"):
            retirement.apply_plan(client, deployment=deployment(), plan=plan, plan_sha256="p" * 64, protected=[])
        self.assertEqual(client.delete_calls, [])

    def test_removed_or_changed_inventory_row_stales_apply_before_delete(self):
        mutations = {
            "removed": lambda client: client.packages.pop(),
            "key": lambda client: client.packages[0].update(
                key=f"{PREFIX}/{'f' * 64}.zip", metadata={"sha256": "f" * 64}
            ),
            "etag": lambda client: client.packages[0].update(etag='"changed"'),
            "version": lambda client: client.packages[0].update(version_id="changed"),
            "timestamp": lambda client: client.packages[0].update(
                last_modified=client.packages[0]["last_modified"] + timedelta(seconds=1)
            ),
            "size": lambda client: client.packages[0].update(size=101),
            "metadata": lambda client: client.packages[0]["metadata"].update(sha256="f" * 64),
            "version history": lambda client: client.packages.append(package("0", 600, "extra-version")),
            "delete marker": lambda client: client.delete_markers.append(
                {"Key": client.packages[0]["key"], "VersionId": "marker"}
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                client = FakeS3(self.old_packages())
                plan = plan_for(client)
                mutate(client)
                with self.assertRaises(retirement.RetirementError) as raised:
                    retirement.apply_plan(
                        client,
                        deployment=deployment(),
                        plan=plan,
                        plan_sha256="p" * 64,
                        protected=[],
                    )
                self.assertEqual(raised.exception.status, "stale_plan")
                self.assertEqual(client.delete_calls, [])

    def test_changed_policy_member_stales_apply_before_inventory(self):
        mutations = {
            "retention_days": lambda current: current.update(retention_days=401),
            "minimum_retained_packages": lambda current: current.update(minimum_retained_packages=11),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                client = FakeS3(self.old_packages())
                plan = plan_for(client)
                changed = deployment()
                mutate(changed)
                with self.assertRaisesRegex(retirement.RetirementError, "policy") as raised:
                    retirement.apply_plan(
                        client,
                        deployment=changed,
                        plan=plan,
                        plan_sha256="p" * 64,
                        protected=[],
                    )
                self.assertEqual(raised.exception.status, "stale_plan")
                self.assertEqual(len(client.list_calls), 1)
                self.assertEqual(client.delete_calls, [])

    def test_changed_protected_set_stales_apply_before_inventory(self):
        client = FakeS3(self.old_packages())
        plan = plan_for(client)
        protected = [{"digest": plan["inventory"][0]["digest"], "version_id": plan["inventory"][0]["version_id"]}]
        with self.assertRaisesRegex(retirement.RetirementError, "protected references"):
            retirement.apply_plan(client, deployment=deployment(), plan=plan, plan_sha256="p" * 64, protected=protected)
        self.assertEqual(client.delete_calls, [])

    def test_apply_deletes_each_exact_version_with_etag_precondition(self):
        client = FakeS3(self.old_packages())
        plan = plan_for(client)
        result = retirement.apply_plan(client, deployment=deployment(), plan=plan, plan_sha256="p" * 64, protected=[])
        self.assertEqual(result["status"], "applied")
        self.assertEqual(len(client.delete_calls), 2)
        for call in client.delete_calls:
            self.assertEqual(set(call), {"Bucket", "Key", "VersionId", "IfMatch"})
            self.assertTrue(call["VersionId"])
            self.assertTrue(call["IfMatch"])
        self.assertFalse(hasattr(client, "delete_objects"))

    def test_conditional_refusal_stops_with_version_present(self):
        client = FakeS3(self.old_packages())
        plan = plan_for(client)
        client.delete_error = FakeError(412, "PreconditionFailed")
        result = retirement.apply_plan(client, deployment=deployment(), plan=plan, plan_sha256="p" * 64, protected=[])
        self.assertEqual(result["status"], "delete_refused")
        self.assertEqual(len(client.delete_calls), 1)
        self.assertEqual(len(result["untouched"]), 2)

    def test_lost_response_with_proved_absence_counts_as_deleted(self):
        client = FakeS3(self.old_packages())
        plan = plan_for(client)
        original_delete = client.delete_object

        def delete_then_fail(**kwargs):
            original_delete(**kwargs)
            raise FakeError(500)

        client.delete_object = delete_then_fail  # type: ignore[method-assign]
        result = retirement.apply_plan(client, deployment=deployment(), plan=plan, plan_sha256="p" * 64, protected=[])
        self.assertEqual(result["status"], "applied")
        self.assertEqual(len(result["proved_deleted"]), 2)

    def test_provider_failure_with_presence_is_delete_failed(self):
        client = FakeS3(self.old_packages())
        plan = plan_for(client)
        client.delete_error = FakeError(500)
        result = retirement.apply_plan(client, deployment=deployment(), plan=plan, plan_sha256="p" * 64, protected=[])
        self.assertEqual(result["status"], "delete_failed")

    def test_unreadable_exact_version_is_ambiguous_without_retry(self):
        client = FakeS3(self.old_packages())
        plan = plan_for(client)
        client.delete_error = FakeError(500)
        client.head_error_after_delete = FakeError(503)
        result = retirement.apply_plan(client, deployment=deployment(), plan=plan, plan_sha256="p" * 64, protected=[])
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(len(client.delete_calls), 1)

    def test_partial_failure_separates_proved_and_untouched_candidates(self):
        client = FakeS3(self.old_packages())
        plan = plan_for(client)
        original_delete = client.delete_object

        def fail_second(**kwargs):
            if len(client.delete_calls) == 1:
                client.delete_calls.append(kwargs)
                raise FakeError(500)
            return original_delete(**kwargs)

        client.delete_object = fail_second  # type: ignore[method-assign]
        result = retirement.apply_plan(client, deployment=deployment(), plan=plan, plan_sha256="p" * 64, protected=[])
        self.assertEqual(result["status"], "delete_failed")
        self.assertEqual(len(result["proved_deleted"]), 1)
        self.assertEqual(len(result["untouched"]), 1)
        self.assertEqual(len(client.delete_calls), 2)

    def test_failed_final_inventory_is_applied_unverified(self):
        client = FakeS3(self.old_packages())
        plan = plan_for(client)
        original_delete = client.delete_object

        def delete_and_fail_final_list(**kwargs):
            response = original_delete(**kwargs)
            if len(client.delete_calls) == len(plan["deletion_candidates"]):
                client.fail_list = True
            return response

        client.delete_object = delete_and_fail_final_list  # type: ignore[method-assign]
        result = retirement.apply_plan(client, deployment=deployment(), plan=plan, plan_sha256="p" * 64, protected=[])
        self.assertEqual(result["status"], "applied_unverified")
        self.assertEqual(len(result["proved_deleted"]), 2)

    def test_changed_final_inventory_is_applied_unverified(self):
        def remove_retained(client):
            client.packages.pop(0)

        def add_unexpected(client):
            client.packages.append(package("f", 1, "unexpected"))

        for label, mutate in {
            "retained disappearance": remove_retained,
            "unexpected version": add_unexpected,
        }.items():
            with self.subTest(label=label):
                client = FakeS3(self.old_packages())
                plan = plan_for(client)
                client.final_inventory_mutation = mutate
                client.mutate_after_delete_calls = len(plan["deletion_candidates"])
                result = retirement.apply_plan(
                    client,
                    deployment=deployment(),
                    plan=plan,
                    plan_sha256="p" * 64,
                    protected=[],
                )
                self.assertEqual(result["status"], "applied_unverified")
                self.assertEqual(result["detail"], "final inventory differs from the proved retained set")
                self.assertEqual(len(result["proved_deleted"]), 2)

    def test_protected_disappearance_from_final_inventory_is_applied_unverified(self):
        client = FakeS3(self.old_packages())
        protected_package = client.packages[0]
        protected = [{"digest": protected_package["digest"], "version_id": protected_package["version_id"]}]
        plan = plan_for(client, protected)

        def remove_protected(current):
            current.packages = [
                item
                for item in current.packages
                if (item["digest"], item["version_id"])
                != (protected_package["digest"], protected_package["version_id"])
            ]

        client.final_inventory_mutation = remove_protected
        client.mutate_after_delete_calls = len(plan["deletion_candidates"])
        result = retirement.apply_plan(
            client,
            deployment=deployment(),
            plan=plan,
            plan_sha256="p" * 64,
            protected=protected,
        )
        self.assertEqual(result["status"], "applied_unverified")
        self.assertEqual(len(result["proved_deleted"]), 2)

    def test_old_plan_is_stale_after_one_proved_delete(self):
        client = FakeS3(self.old_packages())
        plan = plan_for(client)
        original_delete = client.delete_object

        def fail_second(**kwargs):
            if len(client.delete_calls) == 1:
                client.delete_calls.append(kwargs)
                raise FakeError(500)
            return original_delete(**kwargs)

        client.delete_object = fail_second  # type: ignore[method-assign]
        first = retirement.apply_plan(
            client,
            deployment=deployment(),
            plan=plan,
            plan_sha256="p" * 64,
            protected=[],
        )
        self.assertEqual(first["status"], "delete_failed")
        self.assertEqual(len(first["proved_deleted"]), 1)
        delete_count = len(client.delete_calls)

        with self.assertRaisesRegex(retirement.RetirementError, "differs from the preview") as raised:
            retirement.apply_plan(
                client,
                deployment=deployment(),
                plan=plan,
                plan_sha256="p" * 64,
                protected=[],
            )
        self.assertEqual(raised.exception.status, "stale_plan")
        self.assertEqual(len(client.delete_calls), delete_count)

    def test_success_response_with_version_present_is_failure(self):
        client = FakeS3(self.old_packages())
        plan = plan_for(client)
        client.keep_after_delete = True
        result = retirement.apply_plan(client, deployment=deployment(), plan=plan, plan_sha256="p" * 64, protected=[])
        self.assertEqual(result["status"], "delete_failed")

    def test_canonical_plan_file_rejects_byte_change(self):
        client = FakeS3(self.old_packages())
        plan = plan_for(client)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            digest = retirement.write_plan(path, plan)
            path.write_bytes(path.read_bytes() + b" ")
            with self.assertRaisesRegex(retirement.RetirementError, "differs"):
                retirement.load_plan(path, digest)


class CliTests(unittest.TestCase):
    def test_success_output_defaults_to_process_stdout(self):
        defaults = retirement._write.__kwdefaults__
        self.assertIsNotNone(defaults)
        assert defaults is not None
        self.assertIs(defaults["stream"], retirement.sys.stdout)

    def arguments(
        self,
        action: str,
        plan_path: Path,
        *,
        inventory_limit: int = 100,
        expected_digest: str | None = None,
    ) -> list[str]:
        arguments = [
            action,
            "--deployment",
            str(ROOT / "infra/central/deployment.yaml"),
            "--schema",
            str(ROOT / "schemas/deployment.schema.json"),
            "--plan",
            str(plan_path),
            "--no-protected-packages",
            "--inventory-limit",
            str(inventory_limit),
        ]
        if expected_digest is not None:
            arguments.extend(("--expected-plan-sha256", expected_digest))
        return arguments

    def run_cli(self, client, arguments):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.object(retirement._write, "__kwdefaults__", {"stream": stdout}), redirect_stderr(stderr):
            exit_code = retirement.main(arguments, client=client, now=AS_OF)
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def preview(self, client, plan_path: Path):
        exit_code, stdout, stderr = self.run_cli(client, self.arguments("preview", plan_path))
        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        document = json.loads(stdout)
        self.assertEqual(stdout, retirement.canonical_json(document).decode("utf-8") + "\n")
        self.assertEqual(set(document), {"candidate_count", "inventory_count", "plan_sha256", "status"})
        self.assertEqual(document["status"], "previewed")
        self.assertEqual(client.delete_calls, [])
        self.assertLessEqual(len(stdout.encode("utf-8")), 512)
        return document["plan_sha256"]

    def assert_bounded_result(self, document, *, inventory_limit=100):
        self.assertEqual(
            set(document),
            {"detail", "plan_sha256", "proved_deleted", "status", "untouched"},
        )
        rows = document["proved_deleted"] + document["untouched"]
        self.assertLessEqual(len(rows), inventory_limit)
        self.assertLessEqual(len(document["detail"]), retirement.MAX_IDENTIFIER_CHARACTERS)
        for row in rows:
            self.assertEqual(set(row), {"digest", "key", "version_id"})
            self.assertEqual(len(row["digest"]), 64)
            self.assertLessEqual(len(row["key"]), retirement.MAX_IDENTIFIER_CHARACTERS)
            self.assertLessEqual(len(row["version_id"]), retirement.MAX_IDENTIFIER_CHARACTERS)

    def test_preview_and_apply_have_canonical_bounded_redacted_output(self):
        client = FakeS3(PlanAndApplyTests().old_packages())
        with tempfile.TemporaryDirectory() as directory:
            plan_path = Path(directory) / "plan.json"
            digest = self.preview(client, plan_path)
            exit_code, stdout, stderr = self.run_cli(
                client,
                self.arguments("apply", plan_path, expected_digest=digest),
            )
        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        document = json.loads(stdout)
        self.assertEqual(stdout, retirement.canonical_json(document).decode("utf-8") + "\n")
        self.assert_bounded_result(document)
        self.assertEqual(document["status"], "applied")
        self.assertLessEqual(len(stdout.encode("utf-8")), 4096)
        for forbidden in ("etag", "last_modified", "metadata", "provider detail", "size"):
            self.assertNotIn(forbidden, stdout)

    def test_preview_rejects_an_expected_plan_digest_with_invalid_exit(self):
        client = FakeS3(PlanAndApplyTests().old_packages())
        with tempfile.TemporaryDirectory() as directory:
            plan_path = Path(directory) / "plan.json"
            exit_code, stdout, stderr = self.run_cli(
                client,
                self.arguments("preview", plan_path, expected_digest="a" * 64),
            )
            self.assertFalse(plan_path.exists())
        self.assertEqual(exit_code, retirement.EXIT_INVALID)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["status"], "inventory_refused")

    def test_apply_requires_the_expected_plan_digest_with_refused_exit(self):
        client = FakeS3(PlanAndApplyTests().old_packages())
        with tempfile.TemporaryDirectory() as directory:
            plan_path = Path(directory) / "plan.json"
            exit_code, stdout, stderr = self.run_cli(client, self.arguments("apply", plan_path))
        self.assertEqual(exit_code, retirement.EXIT_REFUSED)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["status"], "stale_plan")
        self.assertEqual(client.list_calls, [])

    def test_apply_refuses_a_changed_inventory_limit_before_delete(self):
        client = FakeS3(PlanAndApplyTests().old_packages())
        with tempfile.TemporaryDirectory() as directory:
            plan_path = Path(directory) / "plan.json"
            digest = self.preview(client, plan_path)
            exit_code, stdout, stderr = self.run_cli(
                client,
                self.arguments("apply", plan_path, inventory_limit=99, expected_digest=digest),
            )
        self.assertEqual(exit_code, retirement.EXIT_REFUSED)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["status"], "stale_plan")
        self.assertEqual(client.delete_calls, [])

    def test_ambiguous_apply_uses_the_distinct_exit_code(self):
        client = FakeS3(PlanAndApplyTests().old_packages())
        with tempfile.TemporaryDirectory() as directory:
            plan_path = Path(directory) / "plan.json"
            digest = self.preview(client, plan_path)
            client.delete_error = FakeError(500)
            client.head_error_after_delete = FakeError(503)
            exit_code, stdout, stderr = self.run_cli(
                client,
                self.arguments("apply", plan_path, expected_digest=digest),
            )
        self.assertEqual(exit_code, retirement.EXIT_AMBIGUOUS)
        self.assertEqual(stderr, "")
        self.assertEqual(json.loads(stdout)["status"], "ambiguous")

    def test_final_inventory_mismatch_is_nonzero_and_bounded(self):
        client = FakeS3(PlanAndApplyTests().old_packages())
        with tempfile.TemporaryDirectory() as directory:
            plan_path = Path(directory) / "plan.json"
            digest = self.preview(client, plan_path)
            client.final_inventory_mutation = lambda current: current.packages.append(package("f", 1, "unexpected"))
            client.mutate_after_delete_calls = 2
            exit_code, stdout, stderr = self.run_cli(
                client,
                self.arguments("apply", plan_path, expected_digest=digest),
            )
        self.assertEqual(exit_code, retirement.EXIT_REFUSED)
        self.assertEqual(stderr, "")
        document = json.loads(stdout)
        self.assertEqual(document["status"], "applied_unverified")
        self.assert_bounded_result(document)
        self.assertLessEqual(len(stdout.encode("utf-8")), 4096)

    def test_provider_failure_is_redacted_and_refused(self):
        client = FakeS3(PlanAndApplyTests().old_packages())
        client.fail_list = True
        with tempfile.TemporaryDirectory() as directory:
            exit_code, stdout, stderr = self.run_cli(
                client,
                self.arguments("preview", Path(directory) / "plan.json"),
            )
        self.assertEqual(exit_code, retirement.EXIT_REFUSED)
        self.assertEqual(stdout, "")
        document = json.loads(stderr)
        self.assertEqual(set(document), {"detail", "status"})
        self.assertEqual(document["status"], "inventory_failed")
        self.assertLessEqual(len(stderr.encode("utf-8")), 512)
        self.assertNotIn("provider detail", stderr)


if __name__ == "__main__":
    unittest.main()
