import contextlib
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import boto3
import yaml
from moto import mock_aws

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import publish_release as publisher  # noqa: E402

from aws_public_change_feed.releases import (  # noqa: E402
    ObjectMissing,
    PreconditionFailed,
    S3ObjectStore,
    StoredObject,
    WriteConflict,
)

APPLICATION_VERSION = f"sha256:{'a' * 64}"
DEV_GENERATED_AT = "2026-08-15T12:00:00Z"
GENERATED_AT = "2026-08-15T16:00:00Z"
PROMOTED_AT = "2026-08-15T16:01:00Z"
LATER_PROMOTION = "2026-08-15T16:02:00Z"


def pointer_bytes(release: str, promoted_at: str = "2026-08-15T15:59:00Z") -> bytes:
    return publisher.canonical_json({"release_id": release, "promoted_at": promoted_at})


class MemoryStore:
    def __init__(self) -> None:
        self.current: dict[str, StoredObject] = {}
        self.versions: dict[tuple[str, str], StoredObject] = {}
        self.calls: list[str] = []
        self.sequence = 0
        self.replace_outcome: str | None = None
        self.pointer_create_outcome: str | None = None
        self.pointer_key = "apcf/active-versions.json"

    def _stored(self, body: bytes) -> StoredObject:
        self.sequence += 1
        digest = hashlib.sha256(body).hexdigest()
        return StoredObject(body=body, etag=f'"{digest}"', version_id=f"version-{self.sequence}")

    def put_current(self, key: str, body: bytes) -> StoredObject:
        stored = self._stored(body)
        self.current[key] = stored
        self.versions[(key, stored.version_id)] = stored
        return stored

    def create(self, key: str, body: bytes) -> str:
        self.calls.append(f"create:{key}")
        if key == self.pointer_key and self.pointer_create_outcome == "precondition":
            self.pointer_create_outcome = None
            self.put_current(key, pointer_bytes("competing"))
            raise PreconditionFailed(key)
        if key in self.current:
            raise PreconditionFailed(key)
        return self.put_current(key, body).version_id

    def read(self, key: str, version_id: str | None = None) -> StoredObject:
        self.calls.append(f"read:{key}:{version_id or 'current'}")
        stored = self.versions.get((key, version_id)) if version_id is not None else self.current.get(key)
        if stored is None:
            raise ObjectMissing(key)
        return stored

    def replace(self, key: str, body: bytes, *, if_match: str) -> str:
        self.calls.append(f"replace:{key}")
        outcome = self.replace_outcome
        self.replace_outcome = None
        if outcome == "precondition":
            self.put_current(key, pointer_bytes("competing"))
            raise PreconditionFailed(key)
        if outcome == "conflict_converged":
            self.put_current(key, body)
            raise WriteConflict(key)
        if outcome == "conflict_other":
            self.put_current(key, pointer_bytes("competing"))
            raise WriteConflict(key)
        if outcome == "missing":
            self.current.pop(key, None)
            raise ObjectMissing(key)
        current = self.current.get(key)
        if current is None:
            raise ObjectMissing(key)
        if current.etag != if_match:
            raise PreconditionFailed(key)
        return self.put_current(key, body).version_id

    def mutation_calls(self) -> list[str]:
        return [call for call in self.calls if call.startswith(("create:", "replace:"))]


class ExplodingStore(MemoryStore):
    def read(self, key: str, version_id: str | None = None) -> StoredObject:
        raise RuntimeError("provider response contains secret-value and https://private.example")


class PublishReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.deployment_path = self.root / "deployment.yaml"
        self.config_path = self.root / "config.yaml"
        self.terraform_output_path = self.root / "terraform-output.json"
        self.inventory_path = self.root / "inventory.json"
        self.plan_path = self.root / "release-plan.json"
        self.deployment = yaml.safe_load((ROOT / "infra/central/deployment.yaml").read_text(encoding="utf-8"))
        self.config = yaml.safe_load((ROOT / "config/dev.yaml").read_text(encoding="utf-8"))
        self._write_inputs()

    def _write_inputs(self) -> None:
        self.deployment_path.write_text(yaml.safe_dump(self.deployment, sort_keys=False), encoding="utf-8")
        self.config_path.write_text(yaml.safe_dump(self.config, sort_keys=False), encoding="utf-8")
        self.terraform_output_path.write_bytes((ROOT / "tests/fixtures/terraform-output.dev.json").read_bytes())

    def preview(self, store: MemoryStore, *, promoted_at: str = PROMOTED_AT) -> tuple[dict, str]:
        plan, inventory_body = publisher.create_preview(
            store,
            deployment_path=self.deployment_path,
            config_path=self.config_path,
            terraform_output_path=self.terraform_output_path,
            inventory_path=self.inventory_path,
            application_version=APPLICATION_VERSION,
            generated_at=GENERATED_AT,
            promoted_at=promoted_at,
        )
        digest = publisher.write_preview(self.plan_path, self.inventory_path, plan, inventory_body)
        return plan, digest

    def _load_committed_dev_bundle(self, inventory_path: Path) -> publisher.LocalReleaseInputs:
        return publisher.load_local_inputs(
            deployment_path=ROOT / "infra/central/deployment.yaml",
            config_path=ROOT / "config/dev.yaml",
            terraform_output_path=ROOT / "tests/fixtures/terraform-output.dev.json",
            inventory_path=inventory_path,
            generated_at=DEV_GENERATED_AT,
        )

    def test_committed_dev_bundle_loads_with_exact_inventory_projection(self) -> None:
        local = self._load_committed_dev_bundle(self.inventory_path)
        deployment = yaml.safe_load((ROOT / "infra/central/deployment.yaml").read_text(encoding="utf-8"))
        expected_inventory = {
            "schema_version": 3,
            "deployment_id": deployment["deployment_id"],
            "generated_at": DEV_GENERATED_AT,
            "deployment_region": deployment["deployment_region"],
            "slack": deployment["slack"],
            "environments": [
                {
                    "id": environment["id"],
                    "customer": environment["customer"],
                    "account_id": environment["account_id"],
                    "regions": environment["regions"],
                    "route_id": environment["route_id"],
                }
                for environment in deployment["environments"]
            ],
        }
        expected_bytes = publisher.canonical_json(expected_inventory) + b"\n"
        loaded_inventory = json.loads(local.inventory_body)

        self.assertEqual(local.inventory_body, expected_bytes)
        self.assertEqual(
            hashlib.sha256(local.inventory_body).hexdigest(),
            "72f2618adfb62aef51c89046d9951257b39db074cf1dfa35b7709294d8686c6a",
        )
        self.assertEqual(local.deployment_path, (ROOT / "infra/central/deployment.yaml").resolve())
        self.assertEqual(local.config_path, (ROOT / "config/dev.yaml").resolve())
        self.assertEqual(
            local.terraform_output_path,
            (ROOT / "tests/fixtures/terraform-output.dev.json").resolve(),
        )
        self.assertEqual(local.inventory_path, self.inventory_path.resolve())
        self.assertEqual([environment["id"] for environment in loaded_inventory["environments"]], ["dev"])
        self.assertEqual(list(loaded_inventory["slack"]["routes"]), ["dev-alerts"])
        self.assertEqual(set(local.config["environment_policies"]), {"dev"})

    def test_example_config_substitution_is_refused_while_examples_remain_valid(self) -> None:
        self._load_committed_dev_bundle(self.inventory_path)

        with self.assertRaises(publisher.ReleaseCommandError) as raised:
            publisher.load_local_inputs(
                deployment_path=ROOT / "infra/central/deployment.yaml",
                config_path=ROOT / "examples/config.yaml",
                terraform_output_path=ROOT / "tests/fixtures/terraform-output.dev.json",
                inventory_path=self.inventory_path,
                generated_at=DEV_GENERATED_AT,
            )
        self.assertEqual(raised.exception.status, "invalid_input")

        validation = subprocess.run(
            [sys.executable, str(ROOT / "scripts/validate_config.py")],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(validation.returncode, 0, validation.stdout + validation.stderr)

    def test_inventory_is_the_exact_canonical_deployment_projection(self) -> None:
        inventory = publisher.generate_inventory(self.deployment, GENERATED_AT)

        self.assertEqual(
            inventory,
            {
                "schema_version": 3,
                "deployment_id": self.deployment["deployment_id"],
                "generated_at": GENERATED_AT,
                "deployment_region": self.deployment["deployment_region"],
                "slack": self.deployment["slack"],
                "environments": [
                    {
                        "id": "dev",
                        "customer": "Dev",
                        "account_id": "667653114001",
                        "regions": ["us-east-1"],
                        "route_id": "dev-alerts",
                    }
                ],
            },
        )
        self.assertEqual(
            publisher.canonical_json(inventory) + b"\n",
            publisher.canonical_json(json.loads((publisher.canonical_json(inventory) + b"\n").decode())) + b"\n",
        )

    def test_missing_or_extra_projection_fields_are_refused_before_store_access(self) -> None:
        for mutation in ("missing_environment_field", "extra_deployment_field", "unknown_config_field"):
            with self.subTest(mutation=mutation):
                deployment = yaml.safe_load((ROOT / "infra/central/deployment.yaml").read_text(encoding="utf-8"))
                config = yaml.safe_load((ROOT / "config/dev.yaml").read_text(encoding="utf-8"))
                if mutation == "missing_environment_field":
                    del deployment["environments"][0]["route_id"]
                elif mutation == "extra_deployment_field":
                    deployment["unexpected"] = True
                else:
                    config["unexpected"] = True
                self.deployment = deployment
                self.config = config
                self._write_inputs()
                store = MemoryStore()
                with self.assertRaises(publisher.ReleaseCommandError) as raised:
                    self.preview(store)
                self.assertEqual(raised.exception.status, "invalid_input")
                self.assertEqual(store.calls, [])

    def test_wrong_missing_and_malformed_terraform_outputs_are_refused_before_store_access(self) -> None:
        variants = {
            "wrong": {
                "config_bucket_name": {"sensitive": False, "type": "string", "value": "wrong-bucket"},
                "release_prefix": {
                    "sensitive": False,
                    "type": "string",
                    "value": f"{self.deployment['release_prefix']}/",
                },
            },
            "missing": {
                "config_bucket_name": {
                    "sensitive": False,
                    "type": "string",
                    "value": self.deployment["config_bucket_name"],
                }
            },
            "malformed": {"config_bucket_name": "not-an-output", "release_prefix": {}},
        }
        for name, outputs in variants.items():
            with self.subTest(name=name):
                self.terraform_output_path.write_text(json.dumps(outputs), encoding="utf-8")
                store = MemoryStore()
                with self.assertRaises(publisher.ReleaseCommandError):
                    self.preview(store)
                self.assertEqual(store.calls, [])

    def test_preview_is_s3_read_only_deterministic_and_secret_safe(self) -> None:
        store = MemoryStore()
        first, first_digest = self.preview(store)
        first_bytes = self.plan_path.read_bytes()
        second, second_digest = self.preview(store)

        self.assertEqual(first, second)
        self.assertEqual(first_digest, second_digest)
        self.assertEqual(first_bytes, self.plan_path.read_bytes())
        self.assertEqual(store.mutation_calls(), [])
        self.assertEqual(first["current_pointer"], {"status": "missing"})
        serialized = json.dumps(first)
        for forbidden in ("credential_secret_id", "hooks.slack.com", "dev/slack/dev-alerts-webhook"):
            self.assertNotIn(forbidden, serialized)

    def test_first_apply_publishes_promotes_and_probes_the_exact_release(self) -> None:
        store = MemoryStore()
        plan, _ = self.preview(store)
        store.calls.clear()

        result = publisher.apply_release(store, plan)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["promotion"]["status"], "promoted")
        self.assertEqual(result["probed_release_id"], plan["release_id"])
        self.assertEqual(result["application_version"], APPLICATION_VERSION)
        self.assertEqual(
            store.read(store.pointer_key).body, publisher.canonical_json(json.loads(store.read(store.pointer_key).body))
        )

    def test_matching_release_objects_are_adopted_on_a_later_exact_plan(self) -> None:
        store = MemoryStore()
        first, _ = self.preview(store)
        self.assertEqual(publisher.apply_release(store, first)["status"], "completed")
        second, _ = self.preview(store, promoted_at=LATER_PROMOTION)

        result = publisher.apply_release(store, second)

        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["release_objects"]["config"]["adopted"])
        self.assertTrue(result["release_objects"]["inventory"]["adopted"])

    def test_apply_refuses_each_changed_local_input_before_release_writes(self) -> None:
        mutations = ("deployment", "config", "terraform_output", "inventory")
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self._write_inputs()
                store = MemoryStore()
                plan, _ = self.preview(store)
                if mutation == "deployment":
                    changed = self.deployment_path.read_text(encoding="utf-8").replace(
                        "log_retention_days: 30", "log_retention_days: 31"
                    )
                    self.deployment_path.write_text(changed, encoding="utf-8")
                elif mutation == "config":
                    changed = self.config_path.read_text(encoding="utf-8").replace(
                        "Review supported Kubernetes versions", "Review supported Kubernetes releases"
                    )
                    self.config_path.write_text(changed, encoding="utf-8")
                elif mutation == "terraform_output":
                    document = json.loads(self.terraform_output_path.read_text(encoding="utf-8"))
                    document["source_state_table"]["value"] = "changed"
                    self.terraform_output_path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
                else:
                    self.inventory_path.write_bytes(self.inventory_path.read_bytes() + b" ")
                store.calls.clear()
                with self.assertRaises(publisher.ReleaseCommandError) as raised:
                    publisher.apply_release(store, plan)
                self.assertEqual(raised.exception.status, "stale_plan")
                self.assertEqual(store.mutation_calls(), [])

    def test_apply_refuses_pointer_change_before_release_writes(self) -> None:
        store = MemoryStore()
        plan, _ = self.preview(store)
        store.put_current(store.pointer_key, pointer_bytes("competing"))
        store.calls.clear()

        with self.assertRaisesRegex(publisher.ReleaseCommandError, "pointer decision"):
            publisher.apply_release(store, plan)

        self.assertEqual(store.mutation_calls(), [])

    def test_plan_digest_binds_application_digest_and_times(self) -> None:
        store = MemoryStore()
        _, digest = self.preview(store)
        for field, replacement in (
            ("application_version", f"sha256:{'b' * 64}"),
            ("generated_at", "2026-08-15T16:00:01Z"),
            ("promoted_at", "2026-08-15T16:01:01Z"),
        ):
            with self.subTest(field=field):
                plan = json.loads(self.plan_path.read_text(encoding="utf-8"))
                plan[field] = replacement
                self.plan_path.write_bytes(publisher.canonical_json(plan) + b"\n")
                with self.assertRaisesRegex(publisher.ReleaseCommandError, "differs from the expected digest"):
                    publisher.load_plan(self.plan_path, digest)
                self.preview(store)

    def test_saved_aws_target_is_checked_against_local_inputs_before_store_creation(self) -> None:
        store = MemoryStore()
        plan, _ = self.preview(store)
        plan["target"]["bucket"] = "different-bucket"

        with self.assertRaisesRegex(publisher.ReleaseCommandError, "local release identities"):
            publisher.load_plan_local_inputs(plan)

    def test_preview_refuses_a_malformed_application_digest(self) -> None:
        store = MemoryStore()
        with self.assertRaises(publisher.ReleaseCommandError) as raised:
            publisher.create_preview(
                store,
                deployment_path=self.deployment_path,
                config_path=self.config_path,
                terraform_output_path=self.terraform_output_path,
                inventory_path=self.inventory_path,
                application_version="sha256:WRONG",
                generated_at=GENERATED_AT,
                promoted_at=PROMOTED_AT,
            )
        self.assertEqual(raised.exception.status, "invalid_input")
        self.assertEqual(store.mutation_calls(), [])

    def _preview_against_existing_pointer(self) -> tuple[MemoryStore, dict]:
        store = MemoryStore()
        store.put_current(store.pointer_key, pointer_bytes("old"))
        plan, _ = self.preview(store)
        return store, plan

    def test_promotion_412_reports_supersession_and_keeps_release_objects(self) -> None:
        store, plan = self._preview_against_existing_pointer()
        store.replace_outcome = "precondition"

        result = publisher.apply_release(store, plan)

        self.assertEqual(result["status"], "promotion_superseded")
        self.assertEqual(result["active_release_id"], "competing")
        self.assertEqual(result["release_objects"]["release_id"], plan["release_id"])

    def test_first_promotion_412_reports_supersession(self) -> None:
        store = MemoryStore()
        plan, _ = self.preview(store)
        store.pointer_create_outcome = "precondition"

        result = publisher.apply_release(store, plan)

        self.assertEqual(result["status"], "promotion_superseded")
        self.assertEqual(result["active_release_id"], "competing")

    def test_promotion_409_convergence_completes_without_write_attribution(self) -> None:
        store, plan = self._preview_against_existing_pointer()
        store.replace_outcome = "conflict_converged"

        result = publisher.apply_release(store, plan)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["promotion"]["status"], "converged")
        self.assertIsNone(result["promotion"]["new_version_id"])

    def test_promotion_409_to_another_release_is_distinct(self) -> None:
        store, plan = self._preview_against_existing_pointer()
        store.replace_outcome = "conflict_other"

        result = publisher.apply_release(store, plan)

        self.assertEqual(result["status"], "promotion_conflict")
        self.assertEqual(result["active_release_id"], "competing")

    def test_promotion_404_reports_pointer_loss_without_create_fallback(self) -> None:
        store, plan = self._preview_against_existing_pointer()
        store.replace_outcome = "missing"

        result = publisher.apply_release(store, plan)

        self.assertEqual(result["status"], "pointer_vanished")
        pointer_creates = [call for call in store.calls if call == f"create:{store.pointer_key}"]
        self.assertEqual(pointer_creates, [])

    def test_probe_failure_reports_the_pointer_result_without_completion(self) -> None:
        store = MemoryStore()
        plan, _ = self.preview(store)

        def fail_probe(*args, **kwargs):
            raise RuntimeError("provider detail must stay bounded")

        result = publisher.apply_release(store, plan, probe=fail_probe)

        self.assertEqual(result["status"], "probe_failed")
        self.assertEqual(result["promotion"]["status"], "promoted")
        self.assertNotIn("probed_release_id", result)

    def test_cli_masks_provider_exception_text(self) -> None:
        stderr = io.StringIO()
        arguments = [
            "preview",
            "--deployment",
            str(self.deployment_path),
            "--config",
            str(self.config_path),
            "--terraform-output",
            str(self.terraform_output_path),
            "--inventory",
            str(self.inventory_path),
            "--plan",
            str(self.plan_path),
            "--application-version",
            APPLICATION_VERSION,
            "--generated-at",
            GENERATED_AT,
            "--promoted-at",
            PROMOTED_AT,
        ]
        with contextlib.redirect_stderr(stderr):
            exit_code = publisher.main(arguments, store=ExplodingStore())

        self.assertEqual(exit_code, publisher.EXIT_AMBIGUOUS)
        self.assertEqual(
            json.loads(stderr.getvalue()),
            {"status": "provider_error", "detail": "release operation failed without a safe bounded result"},
        )
        self.assertNotIn("secret-value", stderr.getvalue())
        self.assertNotIn("private.example", stderr.getvalue())

    def test_cli_preview_and_apply_complete_against_one_exact_plan(self) -> None:
        store = MemoryStore()
        preview_stdout = io.StringIO()
        preview_arguments = [
            "preview",
            "--deployment",
            str(self.deployment_path),
            "--config",
            str(self.config_path),
            "--terraform-output",
            str(self.terraform_output_path),
            "--inventory",
            str(self.inventory_path),
            "--plan",
            str(self.plan_path),
            "--application-version",
            APPLICATION_VERSION,
            "--generated-at",
            GENERATED_AT,
            "--promoted-at",
            PROMOTED_AT,
        ]
        with contextlib.redirect_stdout(preview_stdout):
            self.assertEqual(publisher.main(preview_arguments, store=store), 0)
        preview_result = json.loads(preview_stdout.getvalue())

        apply_stdout = io.StringIO()
        with contextlib.redirect_stdout(apply_stdout):
            self.assertEqual(
                publisher.main(
                    [
                        "apply",
                        "--plan",
                        str(self.plan_path),
                        "--expected-plan-sha256",
                        preview_result["plan_sha256"],
                    ],
                    store=store,
                ),
                0,
            )
        apply_result = json.loads(apply_stdout.getvalue())
        self.assertEqual(preview_result["release_id"], apply_result["probed_release_id"])
        self.assertEqual(apply_result["status"], "completed")

    def test_command_help_names_the_clean_checkout_preview_and_apply_inputs(self) -> None:
        preview = subprocess.run(
            [sys.executable, str(ROOT / "scripts/publish_release.py"), "preview", "--help"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        apply = subprocess.run(
            [sys.executable, str(ROOT / "scripts/publish_release.py"), "apply", "--help"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        for option in (
            "--deployment",
            "--config",
            "--terraform-output",
            "--inventory",
            "--plan",
            "--application-version",
            "--generated-at",
            "--promoted-at",
        ):
            self.assertIn(option, preview)
        self.assertIn("--plan", apply)
        self.assertIn("--expected-plan-sha256", apply)

    def test_runbook_initializes_the_backend_before_release_preview(self) -> None:
        runbook = (ROOT / "docs/runbooks/operations.md").read_text(encoding="utf-8")
        section = runbook.split("## Configuration release publication\n", 1)[1].split("\n## ", 1)[0]
        init_command = "terraform -chdir=infra/central init -input=false"
        output_command = "terraform -chdir=infra/central output -json > build/central-outputs.json"
        preview_command = "python3 scripts/publish_release.py preview"

        self.assertIn("Terraform backend principal", section)
        self.assertIn("release-publisher role", section)
        self.assertLess(section.index(init_command), section.index(output_command))
        self.assertLess(section.index(output_command), section.index(preview_command))
        self.assertIn("--config config/dev.yaml", section)
        self.assertIn("tests/fixtures/terraform-output.dev.json", section)


class PublishReleaseMotoTests(unittest.TestCase):
    @mock_aws
    def test_moto_preview_and_apply_reaches_the_runtime_probe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            deployment = yaml.safe_load((ROOT / "infra/central/deployment.yaml").read_text(encoding="utf-8"))
            config = yaml.safe_load((ROOT / "config/dev.yaml").read_text(encoding="utf-8"))
            deployment_path = root / "deployment.yaml"
            config_path = root / "config.yaml"
            outputs_path = root / "terraform-output.json"
            inventory_path = root / "inventory.json"
            deployment_path.write_text(yaml.safe_dump(deployment, sort_keys=False), encoding="utf-8")
            config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
            outputs_path.write_bytes((ROOT / "tests/fixtures/terraform-output.dev.json").read_bytes())
            client = boto3.client("s3", region_name=deployment["deployment_region"])
            client.create_bucket(Bucket=deployment["config_bucket_name"])
            client.put_bucket_versioning(
                Bucket=deployment["config_bucket_name"], VersioningConfiguration={"Status": "Enabled"}
            )
            store = S3ObjectStore(client, deployment["config_bucket_name"])

            plan, inventory_body = publisher.create_preview(
                store,
                deployment_path=deployment_path,
                config_path=config_path,
                terraform_output_path=outputs_path,
                inventory_path=inventory_path,
                application_version=APPLICATION_VERSION,
                generated_at=GENERATED_AT,
                promoted_at=PROMOTED_AT,
            )
            inventory_path.write_bytes(inventory_body)
            result = publisher.apply_release(store, plan)

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["probed_release_id"], plan["release_id"])
            self.assertEqual(
                client.get_object(
                    Bucket=deployment["config_bucket_name"], Key=deployment["active_versions_object_key"]
                )["VersionId"],
                result["promotion"]["new_version_id"],
            )


if __name__ == "__main__":
    unittest.main()
