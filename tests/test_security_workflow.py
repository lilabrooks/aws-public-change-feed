import unittest
from pathlib import Path

import workflow_pins
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "security.yml"
BASELINE_PATH = ROOT / ".github" / "trivy-terraform-baseline.yml"


class SecurityWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))

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
        for root in ("bootstrap", "central", "preflight"):
            self.assertIn(f"--output trivy-terraform-{root}.json infra/{root}", scan["run"])
            self.assertIn(f"--result {root}=trivy-terraform-{root}.json", compare["run"])
        self.assertNotIn("--scanners", scan["run"])
        self.assertEqual(scan["run"].count("--format json"), 3)
        self.assertEqual(scan["run"].count("--exit-code 0"), 3)
        self.assertIn("scripts/compare_trivy_baseline.py", compare["run"])

    def test_terraform_baseline_is_complete_and_keeps_runtime_checks_explicit(self) -> None:
        baseline = yaml.safe_load(BASELINE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(baseline["scanner"]["version"], "0.74.0")
        self.assertEqual(
            baseline["scan_roots"],
            {"bootstrap": "infra/bootstrap", "central": "infra/central", "preflight": "infra/preflight"},
        )
        self.assertEqual(baseline["totals"], {"findings": 33, "severity": {"HIGH": 10, "MEDIUM": 2, "LOW": 21}})
        self.assertEqual(
            {item["class"]: item["finding_count"] for item in baseline["classifications"]},
            {
                "accepted_managed_encryption": 24,
                "isolated_preflight_pitr_default": 2,
                "scoped_test_identity": 1,
                "unresolved_hardening": 6,
            },
        )
        self.assertEqual(sum(item["finding_count"] for item in baseline["classifications"]), 33)
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


if __name__ == "__main__":
    unittest.main()
