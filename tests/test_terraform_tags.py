"""Static resource coverage and real Terraform evaluation of the shared tags."""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TAG_EXPRESSIONS = {
    "aws_cloudwatch_event_rule": "local.tags",
    "aws_cloudwatch_log_group": "local.monitoring_tags",
    "aws_cloudwatch_metric_alarm": "local.common_alarm_tags",
    "aws_codebuild_project": "local.tags",
    "aws_dynamodb_table": "local.storage_tags",
    "aws_iam_role": "local.tags",
    "aws_iam_user": "local.tags",
    "aws_lambda_event_source_mapping": "local.tags",
    "aws_lambda_function": "local.tags",
    "aws_s3_bucket": "local.storage_tags",
    "aws_secretsmanager_secret": "local.storage_tags",
    "aws_sfn_state_machine": "local.tags",
    "aws_sns_topic": "local.monitoring_tags",
    "aws_sqs_queue": "local.storage_tags",
    "aws_ssm_parameter": "local.storage_tags",
}
# Bound to the locked provider schemas below, not an assertion about all AWS APIs.
UNTAGGABLE = {
    "aws_cloudwatch_dashboard",
    "aws_cloudwatch_event_target",
    "aws_iam_role_policy",
    "aws_iam_user_policy",
    "aws_lambda_function_event_invoke_config",
    "aws_lambda_permission",
    "aws_s3_bucket_lifecycle_configuration",
    "aws_s3_bucket_policy",
    "aws_s3_bucket_public_access_block",
    "aws_s3_bucket_server_side_encryption_configuration",
    "aws_s3_bucket_versioning",
    "aws_sns_topic_policy",
    "aws_sns_topic_subscription",
    "aws_sqs_queue_policy",
    "aws_sqs_queue_redrive_allow_policy",
    "aws_sqs_queue_redrive_policy",
    "terraform_data",
}


def resources(root):
    for path in sorted((ROOT / "infra" / root).glob("*.tf")):
        source = path.read_text()
        blocks = list(re.finditer(r'(?ms)^resource "([^"]+)" "([^"]+)" \{\n(.*?)^}', source))
        declarations = re.findall(r'(?m)^\s*resource\s+"([^"]+)"\s+"([^"]+)"', source)
        if declarations != [(match.group(1), match.group(2)) for match in blocks]:
            raise ValueError(f"unparsed resource declaration in {path}; extend coverage before accepting this format")
        for match in blocks:
            yield match.group(1), match.group(2), match.group(3)


def environment():
    # These fixtures must never load a live backend, private tfvars, or CLI overrides.
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("TF_VAR_", "TF_CLI_ARGS"))
        and key not in {"TF_DATA_DIR", "TF_CLI_CONFIG_FILE", "TF_PLUGIN_CACHE_DIR"}
    } | {"CHECKPOINT_DISABLE": "1", "TF_IN_AUTOMATION": "1"}


class TerraformTagTests(unittest.TestCase):
    def test_coverage_refuses_to_silently_skip_an_unparsed_resource(self):
        with tempfile.TemporaryDirectory() as directory, patch(__name__ + ".ROOT", Path(directory)):
            root = Path(directory) / "infra/central"
            root.mkdir(parents=True)
            path = root / "fixture.tf"
            path.write_text('resource "aws_s3_bucket" "fixture" {\n  tags = local.storage_tags\n}\n')
            self.assertEqual(len(list(resources("central"))), 1)
            path.write_text('resource "aws_s3_bucket" "fixture" { bucket = "fixture" }\n')
            with self.assertRaisesRegex(ValueError, "unparsed resource declaration"):
                list(resources("central"))

    def assert_resource_tags(self, root, entries):
        self.assertTrue(entries, root)
        for kind, name, body in entries:
            label = f"{root}: {kind}.{name}"
            self.assertIn(kind, TAG_EXPRESSIONS.keys() | UNTAGGABLE, label + ": classify each new resource type")
            tags = re.findall(r"(?m)^  tags\s*=\s*(.*)$", body)
            expected = TAG_EXPRESSIONS.get(kind) if root == "central" else "local.tags"
            self.assertEqual(tags, [expected] if kind in TAG_EXPRESSIONS else [], label)

    def test_every_declared_resource_uses_its_component_tags_or_a_schema_bound_exception(self):
        for root in ("bootstrap", "central", "live-control", "preflight"):
            self.assert_resource_tags(root, list(resources(root)))
        preflight = (ROOT / "infra/preflight/main.tf").read_text()
        self.assertRegex(preflight, r'source\s*=\s*"../central"')
        self.assertRegex(preflight, r"tags\s*=\s*var.tags")
        self.assertRegex(preflight, r"deployment_file\s*=\s*local.deployment_path")

    def test_coverage_rejects_missing_tags_wrong_component_and_unclassified_resource(self):
        mapping = next(row for row in resources("central") if row[0] == "aws_lambda_event_source_mapping")
        self.assert_resource_tags("central", [mapping])
        kind, name, body = mapping
        for changed in (
            (kind, name, re.sub(r"(?m)^  tags\s*=.*\n", "", body)),
            (kind, name, body.replace("= local.tags", "= local.storage_tags")),
            ("aws_unclassified_resource", name, body),
        ):
            with self.assertRaises(AssertionError):
                self.assert_resource_tags("central", [changed])

    @unittest.skipUnless(shutil.which("terraform"), "Terraform not installed")
    def test_actual_tag_expressions_preserve_extras_and_fix_identity_and_component(self):
        extras = {"owner": "fixture", "lifecycle": "preflight"}
        supplied = extras | dict.fromkeys(("project", "deployment_id", "managed_by", "component"), "wrong")
        for root, deployment in (
            ("central", "dev"),
            ("central", "preflight"),
            ("bootstrap", "dev"),
            ("live-control", "dev"),
        ):
            with self.subTest(root=root, deployment=deployment), tempfile.TemporaryDirectory() as directory:
                source = (ROOT / "infra" / root / "tags.tf").read_text()
                inputs = {
                    "central": {"var.tags", "local.deployment_id"},
                    "bootstrap": {"var.tags", "var.deployment_id"},
                    "live-control": set(),
                }[root]
                references = set(re.findall(r"\b(?:var|local)\.[a-z_]+", source))
                self.assertEqual(references - {"local.tags", "local.monitoring_tags"}, inputs)
                fixture = 'variable "tags" { type = map(string) }\nvariable "deployment_id" { type = string }\n'
                fixture += "locals { deployment_id = var.deployment_id }\n" + source
                names = ["tags"] + (
                    ["storage_tags", "monitoring_tags", "common_alarm_tags"] if root == "central" else []
                )
                expression = "jsonencode({" + ",".join(f"{name}=local.{name}" for name in names) + "})\n"
                path = Path(directory)
                (path / "main.tf").write_text(fixture)
                (path / "terraform.tfvars.json").write_text(json.dumps({"tags": supplied, "deployment_id": deployment}))
                result = subprocess.run(
                    ["terraform", "console", "-no-color"],
                    cwd=path,
                    env=environment(),
                    input=expression,
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=30,
                )
                actual = json.loads(json.loads(result.stdout))
                base = {"project": "aws-public-change-feed", "deployment_id": deployment, "managed_by": "terraform"}
                if root == "live-control":
                    self.assertEqual(
                        actual, {"tags": base | {"component": "live-control", "purpose": "bounded-live-control"}}
                    )
                else:
                    for name in names:
                        component = "runtime" if root == "central" and name == "tags" else "storage"
                        if name in {"monitoring_tags", "common_alarm_tags"}:
                            component = "monitoring"
                        self.assertEqual(actual[name], extras | base | {"component": component})

    @unittest.skipUnless(shutil.which("terraform"), "Terraform not installed")
    def test_tag_support_matches_each_locked_provider_offline(self):
        makefile = (ROOT / "Makefile").read_text()
        match = re.search(r"(?m)^check: (.*)$", makefile)
        assert match is not None
        targets = match.group(1).split()
        self.assertLess(targets.index("terraform-check"), targets.index("test"))
        for root in ("bootstrap", "central", "live-control", "preflight"):
            with self.subTest(root=root), tempfile.TemporaryDirectory() as directory:
                lock = (ROOT / "infra" / root / ".terraform.lock.hcl").read_text()
                match = re.search(r'provider "registry.terraform.io/hashicorp/aws" \{\s+version\s*= "([^"]+)"', lock)
                assert match is not None
                version = match.group(1)
                candidates = [ROOT / "infra" / root / ".terraform/providers"]
                if os.environ.get("TF_DATA_DIR"):
                    candidates.insert(0, Path(os.environ["TF_DATA_DIR"]) / "providers")
                mirror = next(
                    (
                        path
                        for path in candidates
                        if list(
                            (path / "registry.terraform.io/hashicorp/aws" / version).glob("*/terraform-provider-aws*")
                        )
                    ),
                    None,
                )
                if mirror is None:
                    self.skipTest(f"AWS {version} not cached; run terraform-check before the schema test")
                path = Path(directory)
                (path / "main.tf").write_text(
                    'terraform {\n required_providers {\n aws = {\n source = "hashicorp/aws"\n version = '
                    + json.dumps(version)
                    + "\n }\n }\n}\n"
                )
                (path / ".terraform.lock.hcl").write_text(lock)
                (path / "terraform.rc").write_text(
                    "provider_installation {\n filesystem_mirror {\n path = "
                    + json.dumps(str(mirror.resolve()))
                    + '\n include = ["registry.terraform.io/hashicorp/aws"]\n }\n}\n'
                )
                env = environment() | {"TF_CLI_CONFIG_FILE": str(path / "terraform.rc")}
                subprocess.run(
                    ["terraform", "init", "-backend=false", "-input=false", "-lockfile=readonly"],
                    cwd=path,
                    env=env,
                    capture_output=True,
                    check=True,
                    timeout=60,
                )
                result = subprocess.run(
                    ["terraform", "providers", "schema", "-json"],
                    cwd=path,
                    env=env,
                    capture_output=True,
                    check=True,
                    timeout=60,
                )
                schemas = json.loads(result.stdout)["provider_schemas"]["registry.terraform.io/hashicorp/aws"][
                    "resource_schemas"
                ]
                types = {kind for kind, _, _ in resources(root) if kind.startswith("aws_")}
                for kind in types:
                    self.assertEqual(
                        "tags" in schemas[kind]["block"].get("attributes", {}),
                        kind in TAG_EXPRESSIONS,
                        f"{root}: recheck tag support for {kind} at provider {version}",
                    )
