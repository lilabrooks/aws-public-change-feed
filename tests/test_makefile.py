import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = ROOT / "Makefile"


class WhitespaceGateTests(unittest.TestCase):
    def run_command(self, directory: Path, *command: str):
        return subprocess.run(command, cwd=directory, check=False, capture_output=True, text=True)

    def commit(self, directory: Path, message: str) -> str:
        self.run_command(directory, "git", "add", "fixture.txt").check_returncode()
        self.run_command(directory, "git", "commit", "-qm", message).check_returncode()
        return self.run_command(directory, "git", "rev-parse", "HEAD").stdout.strip()

    def run_whitespace(self, directory: Path, base: str | None = None):
        command = ["make", "--file", str(MAKEFILE), "whitespace"]
        environment = os.environ.copy()
        environment.pop("CHECK_DIFF_BASE", None)
        environment.pop("CHECK_DIFF_HEAD", None)
        if base is not None:
            command.extend((f"CHECK_DIFF_BASE={base}", "CHECK_DIFF_HEAD=HEAD"))
        return subprocess.run(command, cwd=directory, check=False, capture_output=True, text=True, env=environment)

    def test_working_tree_and_committed_range_are_both_checked(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            self.run_command(directory, "git", "init", "-q").check_returncode()
            self.run_command(directory, "git", "config", "user.name", "Whitespace Test").check_returncode()
            self.run_command(directory, "git", "config", "user.email", "whitespace@example.invalid").check_returncode()
            fixture = directory / "fixture.txt"
            fixture.write_text("clean\n", encoding="utf-8")
            clean_base = self.commit(directory, "clean base")

            fixture.write_text("bad trailing space \n", encoding="utf-8")
            self.assertNotEqual(self.run_whitespace(directory).returncode, 0)

            self.commit(directory, "bad committed whitespace")
            with patch.dict(
                os.environ,
                {"CHECK_DIFF_BASE": "outer-ci-base", "CHECK_DIFF_HEAD": "outer-ci-head"},
            ):
                self.assertEqual(self.run_whitespace(directory).returncode, 0)
            committed_result = self.run_whitespace(directory, clean_base)
            self.assertNotEqual(committed_result.returncode, 0)
            self.assertIn("trailing whitespace", committed_result.stdout)

            fixture.write_text("clean again\n", encoding="utf-8")
            fixed_base = self.commit(directory, "remove whitespace error")
            fixture.write_text("still clean\n", encoding="utf-8")
            self.commit(directory, "clean change")
            self.assertEqual(self.run_whitespace(directory, fixed_base).returncode, 0)


if __name__ == "__main__":
    unittest.main()
