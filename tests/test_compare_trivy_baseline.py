import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import compare_trivy_baseline as comparator  # noqa: E402


class TrivyBaselineComparatorTests(unittest.TestCase):
    def write_fixture(self, directory: Path, expected_count: int = 1, observed_count: int = 1):
        baseline = {
            "scanner": {"version": "0.74.0"},
            "scan_roots": {"central": "infra/central"},
            "totals": {"findings": expected_count, "severity": {"LOW": expected_count}},
            "classifications": [
                {
                    "class": "example",
                    "status": "open",
                    "finding_count": expected_count,
                    "findings": [
                        {
                            "id": "AWS-0001",
                            "severity": "LOW",
                            "occurrences": [
                                {
                                    "scan_root": "central",
                                    "path": "infra/central/main.tf",
                                    "count": expected_count,
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        result = {
            "SchemaVersion": 2,
            "ArtifactName": "infra/central",
            "ArtifactType": "filesystem",
            "Trivy": {"Version": "0.74.0"},
            "Results": [
                {
                    "Target": "main.tf",
                    "Class": "config",
                    "Type": "terraform",
                    "Misconfigurations": [
                        {"ID": "AWS-0001", "Severity": "LOW", "Status": "FAIL"} for _ in range(observed_count)
                    ],
                }
            ],
        }
        baseline_path = directory / "baseline.yml"
        result_path = directory / "result.json"
        baseline_path.write_text(yaml.safe_dump(baseline, sort_keys=False), encoding="utf-8")
        result_path.write_text(json.dumps(result), encoding="utf-8")
        return baseline_path, result_path

    def run_comparator(self, baseline_path: Path, result_path: Path):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = comparator.main(["--baseline", str(baseline_path), "--result", f"central={result_path}"])
        return status, stdout.getvalue(), stderr.getvalue()

    def test_exact_scanner_inventory_matches(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            baseline_path, result_path = self.write_fixture(Path(raw_directory))
            status, stdout, stderr = self.run_comparator(baseline_path, result_path)
        self.assertEqual(status, 0)
        self.assertIn("matches 1 findings across 1 scan roots", stdout)
        self.assertEqual(stderr, "")

    def test_changed_expected_occurrence_count_fails(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            baseline_path, result_path = self.write_fixture(Path(raw_directory), expected_count=2)
            status, _, stderr = self.run_comparator(baseline_path, result_path)
        self.assertEqual(status, 1)
        self.assertIn("missing: 1 x central: AWS-0001 LOW infra/central/main.tf", stderr)

    def test_unexpected_scanner_occurrence_fails(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            baseline_path, result_path = self.write_fixture(Path(raw_directory), observed_count=2)
            status, _, stderr = self.run_comparator(baseline_path, result_path)
        self.assertEqual(status, 1)
        self.assertIn("unexpected: 1 x central: AWS-0001 LOW infra/central/main.tf", stderr)

    def test_internally_inconsistent_baseline_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            baseline_path, result_path = self.write_fixture(directory)
            baseline = yaml.safe_load(baseline_path.read_text(encoding="utf-8"))
            baseline["totals"]["findings"] = 2
            baseline_path.write_text(yaml.safe_dump(baseline, sort_keys=False), encoding="utf-8")
            status, _, stderr = self.run_comparator(baseline_path, result_path)
        self.assertEqual(status, 2)
        self.assertIn("totals.findings declares 2", stderr)


if __name__ == "__main__":
    unittest.main()
