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
from typing import Any
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("retire_config_releases", ROOT / "scripts/retire_config_releases.py")
assert SPEC is not None
retirement = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(retirement)

AS_OF = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
BUCKET = "apcf-config-dev"
PREFIX = "apcf/releases"
POINTER_KEY = "apcf/active-versions.json"


class FakeError(Exception):
    def __init__(self, status: int, code: str = "Failure") -> None:
        super().__init__("provider detail must never be emitted")
        self.response = {"ResponseMetadata": {"HTTPStatusCode": status}, "Error": {"Code": code}}


def stored_release(index: int, days_old: int, *, timestamp: datetime | None = None) -> dict[str, Any]:
    config_body = f"version: 4\nindex: {index}\n".encode()
    inventory_body = json.dumps({"schema_version": 3, "index": index}, separators=(",", ":")).encode()
    config_sha = retirement.sha256_bytes(config_body)
    inventory_sha = retirement.sha256_bytes(inventory_body)
    identifier = retirement.derive_release_id(config_sha, inventory_sha)
    modified = timestamp or AS_OF - timedelta(days=days_old)
    return {
        "release_id": identifier,
        "objects": [
            {
                "kind": "config",
                "key": f"{PREFIX}/{identifier}/config.yaml",
                "version_id": f"config-v-{index}",
                "etag": f'"config-etag-{index}"',
                "last_modified": modified,
                "body": config_body,
                "is_latest": True,
            },
            {
                "kind": "inventory",
                "key": f"{PREFIX}/{identifier}/inventory.json",
                "version_id": f"inventory-v-{index}",
                "etag": f'"inventory-etag-{index}"',
                "last_modified": modified + timedelta(seconds=1),
                "body": inventory_body,
                "is_latest": True,
            },
        ],
    }


def manifest(release: dict[str, Any], index: int, *, current: bool) -> dict[str, Any]:
    objects = {str(item["kind"]): item for item in release["objects"]}
    document = {
        "schema_version": 2,
        "release_id": release["release_id"],
        "promoted_at": retirement._utc_timestamp(AS_OF - timedelta(days=index)),
        "config": {
            "key": objects["config"]["key"],
            "version_id": objects["config"]["version_id"],
            "sha256": retirement.sha256_bytes(objects["config"]["body"]),
            "schema_version": 4,
        },
        "inventory": {
            "key": objects["inventory"]["key"],
            "version_id": objects["inventory"]["version_id"],
            "sha256": retirement.sha256_bytes(objects["inventory"]["body"]),
            "schema_version": 3,
        },
    }
    body = retirement.canonical_json(document)
    return {
        "key": POINTER_KEY,
        "version_id": f"pointer-v-{index}",
        "etag": f'"pointer-etag-{index}"',
        "last_modified": AS_OF - timedelta(days=index),
        "body": body,
        "is_latest": current,
    }


class FakeS3:
    def __init__(self, releases: list[dict[str, Any]], manifests: list[dict[str, Any]], *, page_size: int = 100):
        self.release_objects = [copy.deepcopy(item) for release in releases for item in release["objects"]]
        self.manifests = copy.deepcopy(manifests)
        self.page_size = page_size
        self.release_delete_markers: list[dict[str, Any]] = []
        self.pointer_delete_markers: list[dict[str, Any]] = []
        self.list_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []
        self.delete_calls: list[dict[str, Any]] = []
        self.fail_list = False
        self.delete_error: Exception | None = None
        self.fail_delete_number: int | None = None
        self.head_error_after_delete: Exception | None = None
        self.keep_after_delete = False

    def _selected(self, prefix: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if prefix == POINTER_KEY:
            return self.manifests, self.pointer_delete_markers
        return self.release_objects, self.release_delete_markers

    def list_object_versions(self, **kwargs):
        self.list_calls.append(kwargs)
        if self.fail_list:
            raise FakeError(500)
        selected, markers = self._selected(kwargs["Prefix"])
        start = int(kwargs.get("KeyMarker", "0"))
        entries = [
            {
                "Key": item["key"],
                "VersionId": item["version_id"],
                "ETag": item["etag"],
                "LastModified": item["last_modified"],
                "Size": len(item["body"]),
                "IsLatest": item.get("is_latest", True),
            }
            for item in selected
        ]
        end = min(start + self.page_size, len(entries))
        response = {
            "Versions": entries[start:end],
            "DeleteMarkers": copy.deepcopy(markers) if start == 0 else [],
            "IsTruncated": end < len(entries),
        }
        if response["IsTruncated"]:
            response["NextKeyMarker"] = str(end)
            response["NextVersionIdMarker"] = f"page-{end}"
        return response

    def get_object(self, **kwargs):
        self.get_calls.append(kwargs)
        for item in [*self.release_objects, *self.manifests]:
            if item["key"] == kwargs["Key"] and item["version_id"] == kwargs["VersionId"]:
                return {
                    "Body": io.BytesIO(item["body"]),
                    "VersionId": item["version_id"],
                    "ETag": item["etag"],
                }
        raise FakeError(404, "NoSuchVersion")

    def head_object(self, **kwargs):
        if self.head_error_after_delete is not None and self.delete_calls:
            raise self.head_error_after_delete
        for item in self.release_objects:
            if item["key"] == kwargs["Key"] and item["version_id"] == kwargs["VersionId"]:
                return {"VersionId": item["version_id"], "ETag": item["etag"]}
        raise FakeError(404, "NoSuchVersion")

    def delete_object(self, **kwargs):
        self.delete_calls.append(kwargs)
        if self.fail_delete_number == len(self.delete_calls):
            raise FakeError(500)
        if self.delete_error is not None:
            raise self.delete_error
        if not self.keep_after_delete:
            self.release_objects = [
                item
                for item in self.release_objects
                if not (item["key"] == kwargs["Key"] and item["version_id"] == kwargs["VersionId"])
            ]
        return {"DeleteMarker": False}


def deployment() -> dict[str, object]:
    return {
        "deployment_sha256": "d" * 64,
        "bucket": BUCKET,
        "release_prefix": PREFIX,
        "config_filename": "config.yaml",
        "inventory_filename": "inventory.json",
        "pointer_key": POINTER_KEY,
        "retention_days": 400,
        "minimum_retained_releases": 10,
    }


def validator():
    return retirement.load_pointer_validator(ROOT / "schemas/active-versions.schema.json")


def release_set() -> list[dict[str, Any]]:
    return [stored_release(index, index if index < 10 else 500 + index) for index in range(12)]


def client_for(releases: list[dict[str, Any]] | None = None, *, page_size: int = 100) -> FakeS3:
    rows = releases or release_set()
    pointers = [manifest(release, index, current=index == 0) for index, release in enumerate(rows[:10])]
    return FakeS3(rows, pointers, page_size=page_size)


def plan_for(client: FakeS3, protected: list[str] | None = None):
    return retirement.create_plan(
        client,
        deployment=deployment(),
        inventory_limit=100,
        explicit_protected=protected or [],
        as_of=AS_OF,
        pointer_validator=validator(),
    )


class DeploymentTests(unittest.TestCase):
    def test_loads_release_policy_and_keys(self):
        loaded = retirement.load_deployment(
            ROOT / "infra/central/deployment.yaml", ROOT / "schemas/deployment.schema.json"
        )
        self.assertEqual(loaded["release_prefix"], PREFIX)
        self.assertEqual(loaded["retention_days"], 400)
        self.assertEqual(loaded["minimum_retained_releases"], 10)

    def test_hard_floors_survive_a_weaker_schema(self):
        for field, value, message in (
            ("retired_release_retention_days", 399, "below 400"),
            ("minimum_retained_releases", 9, "below 10"),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                document = yaml.safe_load((ROOT / "infra/central/deployment.yaml").read_text())
                document["s3_lifecycle"][field] = value
                schema = json.loads((ROOT / "schemas/deployment.schema.json").read_text())
                schema["$defs"]["s3_lifecycle"]["properties"][field]["minimum"] = 1
                deployment_path = root / "deployment.yaml"
                schema_path = root / "schema.json"
                deployment_path.write_text(yaml.safe_dump(document))
                schema_path.write_text(json.dumps(schema))
                with self.assertRaisesRegex(retirement.RetirementError, message):
                    retirement.load_deployment(deployment_path, schema_path)

    def test_protection_requires_unique_release_ids_or_explicit_none(self):
        identifier = "a" * 64
        self.assertEqual(retirement.parse_protected([identifier], False), [identifier])
        self.assertEqual(retirement.parse_protected(None, True), [])
        for values in (None, ["BAD"], [identifier, identifier]):
            with self.subTest(values=values), self.assertRaises(retirement.RetirementError):
                retirement.parse_protected(values, False)


class InventoryTests(unittest.TestCase):
    def test_complete_paginated_inventory_binds_exact_bodies(self):
        client = client_for(page_size=3)
        objects = retirement.inventory_release_objects(client, deployment=deployment(), inventory_limit=30)
        releases = retirement.group_releases(objects)
        manifests = retirement.inventory_manifests(
            client,
            deployment=deployment(),
            releases=releases,
            inventory_limit=20,
            validator=validator(),
        )
        self.assertEqual(len(objects), 24)
        self.assertEqual(len(releases), 12)
        self.assertEqual(len(manifests), 10)
        self.assertGreater(len(client.list_calls), 2)
        self.assertEqual(len(client.get_calls), 34)

    def test_release_delete_marker_refuses_inventory(self):
        client = client_for()
        client.release_delete_markers.append({"Key": client.release_objects[0]["key"], "VersionId": "marker"})
        with self.assertRaisesRegex(retirement.RetirementError, "delete marker"):
            retirement.inventory_release_objects(client, deployment=deployment(), inventory_limit=100)

    def test_malformed_history_incomplete_pair_and_hash_mismatch_are_refused(self):
        mutations = {
            "version history": lambda client: client.release_objects.append(copy.deepcopy(client.release_objects[0])),
            "complete object pair": lambda client: client.release_objects.pop(),
            "release ID": lambda client: client.release_objects[0].update(body=b"changed"),
        }
        for message, mutate in mutations.items():
            with self.subTest(message=message):
                client = client_for()
                mutate(client)
                with self.assertRaisesRegex(retirement.RetirementError, message):
                    objects = retirement.inventory_release_objects(client, deployment=deployment(), inventory_limit=100)
                    retirement.group_releases(objects)

    def test_pointer_sibling_delete_marker_and_invalid_schema_are_refused(self):
        cases = {
            "prefix sibling": lambda client: client.manifests[1].update(key=f"{POINTER_KEY}.bak"),
            "delete marker": lambda client: client.pointer_delete_markers.append(
                {"Key": POINTER_KEY, "VersionId": "marker"}
            ),
            "schema": lambda client: client.manifests[1].update(body=b"{}"),
        }
        for message, mutate in cases.items():
            with self.subTest(message=message):
                client = client_for()
                releases = retirement.group_releases(
                    retirement.inventory_release_objects(client, deployment=deployment(), inventory_limit=100)
                )
                mutate(client)
                with self.assertRaisesRegex(retirement.RetirementError, message):
                    retirement.inventory_manifests(
                        client,
                        deployment=deployment(),
                        releases=releases,
                        inventory_limit=100,
                        validator=validator(),
                    )

    def test_pointer_with_duplicate_json_key_is_refused(self):
        client = client_for()
        releases = retirement.group_releases(
            retirement.inventory_release_objects(client, deployment=deployment(), inventory_limit=100)
        )
        original = client.manifests[1]["body"]
        client.manifests[1]["body"] = b'{"release_id":"' + b"f" * 64 + b'",' + original[1:]
        with self.assertRaisesRegex(retirement.RetirementError, "schema validation"):
            retirement.inventory_manifests(
                client,
                deployment=deployment(),
                releases=releases,
                inventory_limit=100,
                validator=validator(),
            )

    def test_pointer_reference_must_resolve_to_exact_release_versions(self):
        client = client_for()
        releases = retirement.group_releases(
            retirement.inventory_release_objects(client, deployment=deployment(), inventory_limit=100)
        )
        document = json.loads(client.manifests[0]["body"])
        document["config"]["version_id"] = "changed"
        client.manifests[0]["body"] = retirement.canonical_json(document)
        with self.assertRaisesRegex(retirement.RetirementError, "reference differs"):
            retirement.inventory_manifests(
                client,
                deployment=deployment(),
                releases=releases,
                inventory_limit=100,
                validator=validator(),
            )

    def test_inventory_failure_and_limit_have_bounded_statuses(self):
        client = client_for()
        client.fail_list = True
        with self.assertRaises(retirement.RetirementError) as raised:
            retirement.inventory_release_objects(client, deployment=deployment(), inventory_limit=100)
        self.assertEqual(raised.exception.status, "inventory_failed")
        self.assertNotIn("provider detail", str(raised.exception))
        with self.assertRaises(retirement.RetirementError) as raised:
            retirement.inventory_release_objects(client_for(), deployment=deployment(), inventory_limit=1)
        self.assertEqual(raised.exception.status, "inventory_limit_exceeded")


class ClassificationTests(unittest.TestCase):
    def test_manifest_history_and_newest_floor_protect_releases(self):
        client = client_for()
        plan = plan_for(client)
        self.assertEqual(len(plan["manifest_protected"]), 10)
        self.assertEqual(len(plan["deletion_candidates"]), 2)
        self.assertNotIn(plan["manifests"][0]["release_id"], {row["release_id"] for row in plan["deletion_candidates"]})

    def test_explicit_reference_protects_an_old_release_and_must_exist(self):
        client = client_for()
        baseline = plan_for(client)
        old = baseline["deletion_candidates"][0]["release_id"]
        protected = plan_for(client, [old])
        self.assertEqual(len(protected["deletion_candidates"]), 1)
        with self.assertRaisesRegex(retirement.RetirementError, "explicitly protected"):
            plan_for(client, ["f" * 64])

    def test_age_cutoff_is_inclusive_and_one_second_newer_is_retained(self):
        releases = [
            {"release_id": f"{index:064x}", "created_at": retirement._utc_timestamp(AS_OF - timedelta(days=index))}
            for index in range(10)
        ]
        releases.append({"release_id": "a" * 64, "created_at": retirement._utc_timestamp(AS_OF - timedelta(days=400))})
        candidates = retirement.classify_releases(
            releases,
            as_of=AS_OF,
            retention_days=400,
            minimum_retained_releases=10,
            protected_release_ids=[],
        )
        self.assertEqual([row["release_id"] for row in candidates], ["a" * 64])
        releases[-1]["created_at"] = retirement._utc_timestamp(AS_OF - timedelta(days=400) + timedelta(seconds=1))
        self.assertEqual(
            retirement.classify_releases(
                releases,
                as_of=AS_OF,
                retention_days=400,
                minimum_retained_releases=10,
                protected_release_ids=[],
            ),
            [],
        )

    def test_timestamp_tie_at_newest_floor_is_retained(self):
        boundary = AS_OF - timedelta(days=500)
        releases = [
            {"release_id": f"{index:064x}", "created_at": retirement._utc_timestamp(AS_OF - timedelta(days=index))}
            for index in range(9)
        ]
        releases.extend(
            {"release_id": digit * 64, "created_at": retirement._utc_timestamp(boundary)} for digit in ("a", "b")
        )
        self.assertEqual(
            retirement.classify_releases(
                releases,
                as_of=AS_OF,
                retention_days=400,
                minimum_retained_releases=10,
                protected_release_ids=[],
            ),
            [],
        )


class PlanApplyTests(unittest.TestCase):
    def test_preview_is_read_only_and_plan_digest_is_stable(self):
        client = client_for()
        plan = plan_for(client)
        self.assertEqual(client.delete_calls, [])
        with tempfile.TemporaryDirectory() as directory:
            first = retirement.write_plan(Path(directory) / "first.json", plan)
            second = retirement.write_plan(Path(directory) / "second.json", plan)
        self.assertEqual(first, second)

    def test_apply_deletes_each_exact_version_with_etag_precondition(self):
        client = client_for()
        plan = plan_for(client)
        result = retirement.apply_plan(
            client,
            deployment=deployment(),
            plan=plan,
            plan_sha256="p" * 64,
            explicit_protected=[],
            pointer_validator=validator(),
        )
        self.assertEqual(result["status"], "applied")
        self.assertEqual(len(client.delete_calls), 4)
        self.assertEqual(len(result["proved_deleted"]), 4)
        for call in client.delete_calls:
            self.assertEqual(set(call), {"Bucket", "Key", "VersionId", "IfMatch"})

    def test_changed_release_or_pointer_inventory_stales_before_delete(self):
        mutations = {
            "release object": lambda client: client.release_objects[0].update(etag='"changed"'),
            "active pointer history": lambda client: client.manifests[1].update(etag='"changed"'),
        }
        for message, mutate in mutations.items():
            with self.subTest(message=message):
                client = client_for()
                plan = plan_for(client)
                mutate(client)
                with self.assertRaisesRegex(retirement.RetirementError, message):
                    retirement.apply_plan(
                        client,
                        deployment=deployment(),
                        plan=plan,
                        plan_sha256="p" * 64,
                        explicit_protected=[],
                        pointer_validator=validator(),
                    )
                self.assertEqual(client.delete_calls, [])

    def test_changed_policy_or_explicit_protection_stales_before_inventory(self):
        client = client_for()
        plan = plan_for(client)
        changed = deployment()
        changed["retention_days"] = 401
        with self.assertRaisesRegex(retirement.RetirementError, "policy"):
            retirement.apply_plan(
                client,
                deployment=changed,
                plan=plan,
                plan_sha256="p" * 64,
                explicit_protected=[],
                pointer_validator=validator(),
            )
        self.assertEqual(client.delete_calls, [])
        with self.assertRaisesRegex(retirement.RetirementError, "protected"):
            retirement.apply_plan(
                client,
                deployment=deployment(),
                plan=plan,
                plan_sha256="p" * 64,
                explicit_protected=[plan["deletion_candidates"][0]["release_id"]],
                pointer_validator=validator(),
            )

    def test_partial_apply_can_resume_only_the_same_exact_plan(self):
        client = client_for()
        plan = plan_for(client)
        client.fail_delete_number = 2
        first = retirement.apply_plan(
            client,
            deployment=deployment(),
            plan=plan,
            plan_sha256="p" * 64,
            explicit_protected=[],
            pointer_validator=validator(),
        )
        self.assertEqual(first["status"], "delete_failed")
        self.assertEqual(len(first["proved_deleted"]), 1)
        client.fail_delete_number = None
        second = retirement.apply_plan(
            client,
            deployment=deployment(),
            plan=plan,
            plan_sha256="p" * 64,
            explicit_protected=[],
            pointer_validator=validator(),
        )
        self.assertEqual(second["status"], "applied")
        self.assertEqual(len(second["proved_deleted"]), 4)

    def test_missing_retained_object_is_stale(self):
        client = client_for()
        plan = plan_for(client)
        client.release_objects.pop(0)
        with self.assertRaisesRegex(retirement.RetirementError, "retained release object"):
            retirement.apply_plan(
                client,
                deployment=deployment(),
                plan=plan,
                plan_sha256="p" * 64,
                explicit_protected=[],
                pointer_validator=validator(),
            )

    def test_conditional_refusal_and_unreadable_status_are_distinct(self):
        client = client_for()
        plan = plan_for(client)
        client.delete_error = FakeError(412, "PreconditionFailed")
        refused = retirement.apply_plan(
            client,
            deployment=deployment(),
            plan=plan,
            plan_sha256="p" * 64,
            explicit_protected=[],
            pointer_validator=validator(),
        )
        self.assertEqual(refused["status"], "delete_refused")

        client = client_for()
        plan = plan_for(client)
        client.delete_error = FakeError(500)
        client.head_error_after_delete = FakeError(503)
        ambiguous = retirement.apply_plan(
            client,
            deployment=deployment(),
            plan=plan,
            plan_sha256="p" * 64,
            explicit_protected=[],
            pointer_validator=validator(),
        )
        self.assertEqual(ambiguous["status"], "ambiguous")

    def test_lost_delete_response_with_proved_absence_counts_as_deleted(self):
        client = client_for()
        plan = plan_for(client)
        original_delete = client.delete_object

        def delete_then_fail(**kwargs):
            original_delete(**kwargs)
            raise FakeError(500)

        client.delete_object = delete_then_fail  # type: ignore[method-assign]
        result = retirement.apply_plan(
            client,
            deployment=deployment(),
            plan=plan,
            plan_sha256="p" * 64,
            explicit_protected=[],
            pointer_validator=validator(),
        )
        self.assertEqual(result["status"], "applied")

    def test_plan_file_rejects_byte_change(self):
        plan = plan_for(client_for())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            digest = retirement.write_plan(path, plan)
            path.write_bytes(path.read_bytes() + b" ")
            with self.assertRaisesRegex(retirement.RetirementError, "differs"):
                retirement.load_plan(path, digest)


class CliTests(unittest.TestCase):
    def arguments(self, action: str, plan_path: Path, *, digest: str | None = None) -> list[str]:
        arguments = [
            action,
            "--deployment",
            str(ROOT / "infra/central/deployment.yaml"),
            "--plan",
            str(plan_path),
            "--no-additional-protected-releases",
            "--inventory-limit",
            "100",
        ]
        if digest is not None:
            arguments.extend(("--expected-plan-sha256", digest))
        return arguments

    def run_cli(self, client: FakeS3, arguments: list[str]):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.object(retirement._write, "__kwdefaults__", {"stream": stdout}), redirect_stderr(stderr):
            exit_code = retirement.main(arguments, client=client, now=AS_OF)
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def test_preview_and_apply_emit_canonical_bounded_results(self):
        client = client_for()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            exit_code, stdout, stderr = self.run_cli(client, self.arguments("preview", path))
            self.assertEqual((exit_code, stderr), (0, ""))
            preview = json.loads(stdout)
            self.assertEqual(preview["status"], "previewed")
            self.assertEqual(preview["candidate_count"], 2)
            digest = preview["plan_sha256"]
            exit_code, stdout, stderr = self.run_cli(client, self.arguments("apply", path, digest=digest))
        self.assertEqual((exit_code, stderr), (0, ""))
        result = json.loads(stdout)
        self.assertEqual(result["status"], "applied")
        self.assertLessEqual(len(stdout.encode()), 8192)
        for forbidden in ("etag", "last_modified", '"sha256":', "provider detail", "size"):
            self.assertNotIn(forbidden, stdout)

    def test_apply_requires_the_exact_plan_digest(self):
        client = client_for()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            exit_code, stdout, stderr = self.run_cli(client, self.arguments("apply", path))
        self.assertEqual(exit_code, retirement.EXIT_REFUSED)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["status"], "stale_plan")
        self.assertEqual(client.list_calls, [])


if __name__ == "__main__":
    unittest.main()
