import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = ROOT / "Makefile"
# An enclosing `make check VAR=value` passes VAR and make's own flags to every
# nested make through MAKEFLAGS; MAKEOVERRIDES and MFLAGS can travel with it.
ENCLOSING_MAKE_VARIABLES = ("MAKEFLAGS", "MAKEOVERRIDES", "MFLAGS")


class WhitespaceGateTests(unittest.TestCase):
    def run_command(self, directory: Path, *command: str):
        return subprocess.run(command, cwd=directory, check=False, capture_output=True, text=True)

    def commit(self, directory: Path, message: str) -> str:
        self.run_command(directory, "git", "add", "fixture.txt").check_returncode()
        self.run_command(directory, "git", "commit", "-qm", message).check_returncode()
        return self.run_command(directory, "git", "rev-parse", "HEAD").stdout.strip()

    def create_repository(self, directory: Path) -> str:
        self.run_command(directory, "git", "init", "-q").check_returncode()
        self.run_command(directory, "git", "config", "user.name", "Whitespace Test").check_returncode()
        self.run_command(directory, "git", "config", "user.email", "whitespace@example.invalid").check_returncode()
        (directory / "fixture.txt").write_text("clean\n", encoding="utf-8")
        return self.commit(directory, "clean base")

    def run_whitespace(self, directory: Path, base: str | None = None, *, inherit_enclosing_make: bool = False):
        command = ["make", "--file", str(MAKEFILE), "whitespace"]
        environment = os.environ.copy()
        environment.pop("CHECK_DIFF_BASE", None)
        environment.pop("CHECK_DIFF_HEAD", None)
        if not inherit_enclosing_make:
            for name in ENCLOSING_MAKE_VARIABLES:
                environment.pop(name, None)
        if base is not None:
            command.extend((f"CHECK_DIFF_BASE={base}", "CHECK_DIFF_HEAD=HEAD"))
        return subprocess.run(command, cwd=directory, check=False, capture_output=True, text=True, env=environment)

    def assert_working_tree_and_committed_range_are_both_checked(self):
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            clean_base = self.create_repository(directory)
            fixture = directory / "fixture.txt"

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

    def test_working_tree_and_committed_range_are_both_checked(self):
        self.assert_working_tree_and_committed_range_are_both_checked()

    def test_enclosing_make_overrides_do_not_reach_the_gate(self):
        # `make check CHECK_DIFF_BASE=<sha>` exports this, naming a commit the fixture
        # repository lacks. The inheriting run proves the running make still honors it.
        with patch.dict(os.environ, {"MAKEFLAGS": " -- CHECK_DIFF_BASE=outer-make-base"}):
            with tempfile.TemporaryDirectory() as raw_directory:
                directory = Path(raw_directory)
                self.create_repository(directory)
                inherited = self.run_whitespace(directory, inherit_enclosing_make=True)
                self.assertNotEqual(inherited.returncode, 0)
                self.assertIn("outer-make-base", inherited.stderr)

            self.assert_working_tree_and_committed_range_are_both_checked()


if __name__ == "__main__":
    unittest.main()
