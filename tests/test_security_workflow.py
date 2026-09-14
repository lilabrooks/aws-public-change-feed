import re
import shlex
import sys
import unittest
from pathlib import Path

import workflow_pins
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import compare_trivy_baseline as comparator  # noqa: E402

WORKFLOW_PATH = ROOT / ".github" / "workflows" / "security.yml"
BASELINE_PATH = ROOT / ".github" / "trivy-terraform-baseline.yml"


class SecurityWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        root_inventories = re.findall(r"^TERRAFORM_ROOTS := (.+)$", makefile, re.MULTILINE)
        self.assertEqual(len(root_inventories), 1)
        assignments = re.findall(
            r"^[ \t]*(?:(?:export|override|private)[ \t]+)*TERRAFORM_ROOTS[ \t]*[:+?!]*=.*$",
            makefile,
            re.MULTILINE,
        )
        self.assertEqual(assignments, [f"TERRAFORM_ROOTS := {root_inventories[0]}"])
        root_paths = shlex.split(root_inventories[0])
        self.assertTrue(root_paths)
        self.terraform_roots = {Path(path).name: path for path in root_paths}
        self.assertEqual(len(self.terraform_roots), len(root_paths))

    def test_terraform_inventory_covers_source_roots(self) -> None:
        source_roots = {path.parent.relative_to(ROOT).as_posix() for path in ROOT.glob("infra/*/*.tf")}
        self.assertEqual(set(self.terraform_roots.values()), source_roots)

    def test_terraform_security_gate_is_required(self) -> None:
        job = self.workflow["jobs"]["terraform-report"]
        for scope in [job, *job["steps"]]:
            with self.subTest(scope=scope.get("name", "terraform-report")):
                self.assertNotIn("if", scope)
                self.assertIs(scope.get("continue-on-error", False), False)

    def test_security_workflow_has_only_the_bounded_triggers(self) -> None:
        triggers = self.workflow["on"]
        self.assertEqual(set(triggers), {"pull_request", "schedule"})
        self.assertEqual(triggers["pull_request"], None)
        self.assertEqual(triggers["schedule"], [{"cron": "17 8 * * 1"}])
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})

    def test_dependency_and_plaintext_scan_blocks_on_high_and_critical(self) -> None:
        job = self.workflow["jobs"]["dependency-and-secret-scan"]
        checkout, scan = job["steps"]
        workflow_pins.assert_pinned(checkout["uses"], "actions/checkout")
        self.assertFalse(checkout["with"]["persist-credentials"])
        self.assertEqual(
            workflow_pins.assert_pinned(scan["uses"], "aquasecurity/trivy-action"),
            "ed142fd0673e97e23eac54620cfb913e5ce36c25",
        )
        self.assertEqual(
            scan["with"],
            {
                "scan-type": "fs",
                "scan-ref": ".",
                "trivy-config": "trivy.yaml",
                "scanners": "vuln,secret",
                "severity": "HIGH,CRITICAL",
                "exit-code": "1",
                "version": "v0.74.0",
                "cache-dir": "${{ runner.temp }}/trivy-cache",
            },
        )

        config = yaml.safe_load((ROOT / "trivy.yaml").read_text(encoding="utf-8"))
        self.assertEqual(config["scan"]["file-patterns"], [r"pip:requirements-.*\.txt"])
        self.assertEqual(
            sorted(path.name for path in ROOT.glob("requirements*.txt")),
            ["requirements-dev.txt", "requirements-lambda.txt", "requirements.txt"],
        )

    def test_terraform_scan_compares_each_root_with_the_reviewed_baseline(self) -> None:
        job = self.workflow["jobs"]["terraform-report"]
        steps = {step["name"]: step for step in job["steps"]}
        checkout = steps["Check out repository"]
        setup = steps["Set up Trivy"]
        python_setup = steps["Set up Python"]
        dependency_install = steps["Install baseline comparator dependency"]
        scan = steps["Scan Terraform roots as separate entry points"]
        compare = steps["Compare Terraform findings with the reviewed baseline"]
        workflow_pins.assert_pinned(checkout["uses"], "actions/checkout")
        self.assertEqual(
            workflow_pins.assert_pinned(setup["uses"], "aquasecurity/setup-trivy"),
            "3fb12ec12f41e471780db15c232d5dd185dcb514",
        )
        self.assertEqual(setup["with"], {"version": "v0.74.0", "cache": True})
        workflow_pins.assert_pinned(python_setup["uses"], "actions/setup-python")
        self.assertEqual(
            python_setup["with"],
            {"python-version": "3.12", "cache": "pip", "cache-dependency-path": "requirements.txt"},
        )
        self.assertEqual(
            dependency_install["run"],
            "python -m pip install --no-deps --constraint requirements.txt PyYAML",
        )
        scan_commands = [shlex.split(line) for line in scan["run"].replace("\\\n", "").splitlines() if line.strip()]
        self.assertCountEqual(
            scan_commands,
            [
                shlex.split(
                    "trivy config --config trivy.yaml --severity LOW,MEDIUM,HIGH,CRITICAL "
                    f"--exit-code 0 --format json --output trivy-terraform-{name}.json {path}"
                )
                for name, path in self.terraform_roots.items()
            ],
        )
        compare_command = shlex.split(compare["run"].replace("\\\n", ""))
        self.assertEqual(
            compare_command[:4],
            ["python", "scripts/compare_trivy_baseline.py", "--baseline", ".github/trivy-terraform-baseline.yml"],
        )
        self.assertEqual(compare_command[4::2], ["--result"] * len(self.terraform_roots))
        self.assertCountEqual(
            compare_command[5::2],
            [f"{name}=trivy-terraform-{name}.json" for name in self.terraform_roots],
        )

    def test_terraform_baseline_is_complete_and_keeps_runtime_checks_explicit(self) -> None:
        baseline = yaml.safe_load(BASELINE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(baseline["scanner"]["version"], "0.74.0")
        self.assertEqual(baseline["scan_roots"], self.terraform_roots)
        self.assertEqual(baseline["totals"], {"findings": 38, "severity": {"HIGH": 11, "MEDIUM": 3, "LOW": 24}})
        self.assertEqual(
            {item["class"]: item["finding_count"] for item in baseline["classifications"]},
            {
                "accepted_managed_encryption": 27,
                "isolated_preflight_pitr_default": 2,
                "scoped_test_identity": 1,
                "unresolved_hardening": 8,
            },
        )
        self.assertEqual(sum(item["finding_count"] for item in baseline["classifications"]), 38)
        for classification in baseline["classifications"]:
            self.assertEqual(
                sum(
                    occurrence["count"]
                    for finding in classification["findings"]
                    for occurrence in finding["occurrences"]
                ),
                classification["finding_count"],
            )

        runbook = (ROOT / "docs" / "runbooks" / "operations.md").read_text(encoding="utf-8")
        self.assertIn("aws iam simulate-principal-policy", runbook)
        self.assertIn("aws s3api get-bucket-lifecycle-configuration", runbook)
        limitations = "\n".join(baseline["limitations"])
        self.assertIn("preflight scan evaluates infra/central as a module", limitations)
        self.assertIn("simulate-principal-policy", limitations)
        self.assertIn("get-bucket-lifecycle-configuration", limitations)

    def test_terraform_baseline_classifications_preserve_reviewed_dispositions(self) -> None:
        baseline = yaml.safe_load(BASELINE_PATH.read_text(encoding="utf-8"))
        expected = {
            "accepted_managed_encryption": (
                "accepted_by_architecture",
                {"AWS-0132", "AWS-0025", "AWS-0017", "AWS-0098", "AWS-0096"},
            ),
            "isolated_preflight_pitr_default": ("accepted_by_architecture", {"AWS-0024"}),
            "scoped_test_identity": ("accepted_test_boundary", {"AWS-0143"}),
            "unresolved_hardening": ("open", {"AWS-0024", "AWS-0089", "AWS-0095"}),
        }
        for classification in baseline["classifications"]:
            name = classification["class"]
            with self.subTest(classification=name):
                status, rule_ids = expected[name]
                self.assertEqual(classification["status"], status)
                self.assertEqual({finding["id"] for finding in classification["findings"]}, rule_ids)
                if name == "isolated_preflight_pitr_default":
                    for finding in classification["findings"]:
                        self.assertEqual({item["scan_root"] for item in finding["occurrences"]}, {"preflight"})

    def test_terraform_baseline_occurrences_use_scanned_sources(self) -> None:
        _, root_paths, findings, _ = comparator.load_baseline(BASELINE_PATH)
        scanned_sources = {
            name: {path.relative_to(ROOT).as_posix() for path in (ROOT / root_path).glob("*.tf")}
            for name, root_path in root_paths.items()
        }
        # Preflight evaluates central as its local runtime module (ADR-024).
        scanned_sources["preflight"].update(scanned_sources["central"])
        for scan_root, rule_id, severity, path in findings:
            with self.subTest(scan_root=scan_root, rule_id=rule_id, severity=severity, path=path):
                self.assertIn(path, scanned_sources[scan_root])


if __name__ == "__main__":
    unittest.main()
