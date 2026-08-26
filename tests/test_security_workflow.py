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
        self.assertEqual(set(triggers), {"push", "pull_request", "schedule"})
        self.assertEqual(triggers["push"], {"branches": ["main"]})
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

    def test_terraform_scan_reports_all_baseline_severities_without_blocking_on_findings(self) -> None:
        job = self.workflow["jobs"]["terraform-report"]
        checkout, scan = job["steps"]
        workflow_pins.assert_pinned(checkout["uses"], "actions/checkout")
        self.assertEqual(
            workflow_pins.assert_pinned(scan["uses"], "aquasecurity/trivy-action"),
            "ed142fd0673e97e23eac54620cfb913e5ce36c25",
        )
        self.assertEqual(scan["with"]["scan-type"], "config")
        self.assertEqual(scan["with"]["scan-ref"], "infra")
        self.assertEqual(scan["with"]["scanners"], "misconfig")
        self.assertEqual(scan["with"]["severity"], "LOW,MEDIUM,HIGH,CRITICAL")
        self.assertEqual(scan["with"]["exit-code"], "0")
        self.assertEqual(scan["with"]["version"], "v0.74.0")

    def test_terraform_baseline_is_complete_and_keeps_runtime_checks_explicit(self) -> None:
        baseline = yaml.safe_load(BASELINE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(baseline["scanner"]["version"], "0.74.0")
        self.assertEqual(baseline["totals"], {"findings": 19, "severity": {"HIGH": 6, "MEDIUM": 2, "LOW": 11}})
        self.assertEqual(
            {item["class"]: item["finding_count"] for item in baseline["classifications"]},
            {
                "accepted_managed_encryption": 12,
                "preproduction_backup_decision": 2,
                "scoped_test_identity": 1,
                "unresolved_hardening": 4,
            },
        )
        self.assertEqual(sum(item["finding_count"] for item in baseline["classifications"]), 19)
        for classification in baseline["classifications"]:
            self.assertEqual(
                sum(finding["count"] for finding in classification["findings"]),
                classification["finding_count"],
            )

        runbook = (ROOT / "docs" / "runbooks" / "operations.md").read_text(encoding="utf-8")
        self.assertIn("aws iam simulate-principal-policy", runbook)
        self.assertIn("aws s3api get-bucket-lifecycle-configuration", runbook)
        limitations = "\n".join(baseline["limitations"])
        self.assertIn("simulate-principal-policy", limitations)
        self.assertIn("get-bucket-lifecycle-configuration", limitations)


if __name__ == "__main__":
    unittest.main()
