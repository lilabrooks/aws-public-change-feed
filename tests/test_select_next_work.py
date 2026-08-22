# ruff: noqa: E402, I001

import copy
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import select_next_work as selector


SEQUENCE: dict[str, Any] = {
    "version": 1,
    "repository": "owner/repository",
    "backlog_generation": 1,
    "stages": [
        {
            "id": "first-demo",
            "github_milestone": "D0 · First live Slack delivery",
            "terminal_item": "L-03",
            "sequence": ["L-01", "L-02", "L-03"],
        },
        {
            "id": "mvp",
            "github_milestone": "M1 · MVP",
            "terminal_item": None,
            "sequence": ["L-04"],
        },
    ],
}

DIGESTS = {
    "content_sha256": "a" * 64,
    "id_set_sha256": "b" * 64,
    "open_count": 4,
}
INVALID_DIGEST_VALUES = (
    ("content_sha256", None),
    ("content_sha256", "A" * 64),
    ("content_sha256", "a" * 63),
    ("content_sha256", "a" * 65),
    ("content_sha256", "zz" + "a" * 64),
    ("id_set_sha256", 7),
    ("id_set_sha256", "B" * 64),
    ("id_set_sha256", "b" * 63),
    ("id_set_sha256", "b" * 65),
    ("id_set_sha256", "zz" + "b" * 64),
    ("open_count", True),
    ("open_count", -1),
)


def item(stable_id, number, *, state="ready", waits=None, relation=None):
    document = {
        "id": stable_id,
        "issue_number": number,
        "state": state,
        "kind": "evidence",
        "relation": relation or {"type": "none"},
    }
    if waits:
        document["wait"] = waits
    return document


def issue(stable_id, number, milestone, *, state="OPEN"):
    return {
        "number": number,
        "title": f"[{stable_id}] Example",
        "state": state,
        "milestone": {"title": milestone},
    }


def fixture():
    d0 = SEQUENCE["stages"][0]["github_milestone"]
    m1 = SEQUENCE["stages"][1]["github_milestone"]
    return (
        {"items": [item("L-01", 1), item("L-02", 2), item("L-03", 3), item("L-04", 4)]},
        [issue("L-01", 1, d0), issue("L-02", 2, d0), issue("L-03", 3, d0), issue("L-04", 4, m1)],
        {"generation": 1, "content_sha256": "a" * 64},
    )


class LiveRunner:
    def __init__(self):
        backlog, issues, _audit = fixture()
        self.audit: Any = {"authority_issue": 54, "digests": copy.deepcopy(DIGESTS)}
        self.export: Any = copy.deepcopy(DIGESTS)
        self.backlog: dict[str, Any] = {
            "authority": {
                "status": "active",
                "generation": 1,
                "locator": "owner/repository",
            },
            **backlog,
        }
        self.issues = issues
        self.commands: list[list[str]] = []

    def __call__(self, command):
        command = list(command)
        self.commands.append(command)
        if command[:2] == ["backlogctl", "github-audit"]:
            return json.dumps(self.audit)
        if command[:2] == ["backlogctl", "github-export"]:
            output_path = Path(command[command.index("-output") + 1])
            output_path.write_text(json.dumps(self.backlog))
            return json.dumps(self.export)
        if command[:2] == ["backlogctl", "validate"]:
            return ""
        if command[:3] == ["gh", "issue", "list"]:
            return json.dumps(self.issues)
        raise AssertionError(f"unexpected command: {command}")


class SequenceValidationTests(unittest.TestCase):
    def test_committed_sequence_passes_schema_and_semantics(self):
        document = selector.load_sequence(ROOT / ".github/work-sequence.yaml")
        self.assertEqual(document["repository"], "lilabrooks/aws-public-change-feed")

    def test_unknown_fields_are_rejected(self):
        document = copy.deepcopy(SEQUENCE)
        document["priority"] = "hidden"
        with self.assertRaisesRegex(selector.SelectionError, "Additional properties"):
            selector.validate_sequence(document, path=Path("sequence.yaml"))

    def test_one_item_cannot_appear_in_two_stages(self):
        document = copy.deepcopy(SEQUENCE)
        document["stages"][1]["sequence"] = ["L-01"]
        with self.assertRaisesRegex(selector.SelectionError, "more than one stage"):
            selector.validate_sequence(document, path=Path("sequence.yaml"))

    def test_terminal_item_must_be_sequenced(self):
        document = copy.deepcopy(SEQUENCE)
        document["stages"][0]["terminal_item"] = "L-99"
        with self.assertRaisesRegex(selector.SelectionError, "terminal_item must appear"):
            selector.validate_sequence(document, path=Path("sequence.yaml"))

    def test_title_parser_is_bound_to_the_schema_stable_id_pattern(self):
        schema = json.loads((ROOT / ".github/work-sequence.schema.json").read_text())
        stable_id_pattern = schema["$defs"]["stable_id"]["pattern"]
        embedded = stable_id_pattern.removeprefix("^").removesuffix("$")
        self.assertEqual(selector.STABLE_ID_TITLE.pattern, rf"^\[(?P<stable_id>{embedded})\](?:\s|$)")

    def test_backlog_generation_is_required(self):
        document = copy.deepcopy(SEQUENCE)
        del document["backlog_generation"]
        with self.assertRaisesRegex(selector.SelectionError, "required property"):
            selector.validate_sequence(document, path=Path("sequence.yaml"))

    def test_backlog_generation_must_be_an_integer(self):
        for value in (True, "1", 1.5):
            with self.subTest(value=value):
                document = copy.deepcopy(SEQUENCE)
                document["backlog_generation"] = value
                with self.assertRaisesRegex(selector.SelectionError, "not of type 'integer'"):
                    selector.validate_sequence(document, path=Path("sequence.yaml"))

    def test_backlog_generation_must_be_positive(self):
        document = copy.deepcopy(SEQUENCE)
        document["backlog_generation"] = 0
        with self.assertRaisesRegex(selector.SelectionError, "less than the minimum of 1"):
            selector.validate_sequence(document, path=Path("sequence.yaml"))


class LiveBacklogTests(unittest.TestCase):
    def test_load_live_accepts_matching_active_authority(self):
        runner = LiveRunner()
        backlog, issues, audit = selector.load_live("owner/repository", ROOT, 1, runner)
        self.assertEqual(backlog["authority"]["status"], "active")
        self.assertEqual(len(issues), 4)
        self.assertEqual(audit, {**DIGESTS, "generation": 1})

    def test_load_live_rejects_malformed_audit(self):
        runner = LiveRunner()
        runner.audit = []
        with self.assertRaisesRegex(selector.SelectionError, "malformed document"):
            selector.load_live("owner/repository", ROOT, 1, runner)

    def test_load_live_requires_audit_digests(self):
        runner = LiveRunner()
        runner.audit = {}
        with self.assertRaisesRegex(selector.SelectionError, "omitted its digests"):
            selector.load_live("owner/repository", ROOT, 1, runner)

    def test_load_live_requires_each_typed_digest_on_audit_and_export(self):
        for source in ("audit", "export"):
            for field, value in INVALID_DIGEST_VALUES:
                with self.subTest(source=source, field=field, value=value):
                    runner = LiveRunner()
                    target = runner.audit["digests"] if source == "audit" else runner.export
                    target[field] = value
                    with self.assertRaisesRegex(
                        selector.SelectionError, rf"backlog {source} has an invalid or missing {field}"
                    ):
                        selector.load_live("owner/repository", ROOT, 1, runner)

    def test_load_live_rejects_the_same_invalid_digest_on_both_sources(self):
        for field, value in INVALID_DIGEST_VALUES:
            with self.subTest(field=field, value=value):
                runner = LiveRunner()
                runner.audit["digests"][field] = value
                runner.export[field] = value
                with self.assertRaisesRegex(
                    selector.SelectionError, rf"backlog audit has an invalid or missing {field}"
                ):
                    selector.load_live("owner/repository", ROOT, 1, runner)

    def test_load_live_rejects_digest_disagreement(self):
        runner = LiveRunner()
        runner.export["content_sha256"] = "c" * 64
        with self.assertRaisesRegex(selector.SelectionError, "changed between audit and export"):
            selector.load_live("owner/repository", ROOT, 1, runner)

    def test_load_live_requires_authority_generation(self):
        for value in (None, True):
            with self.subTest(value=value):
                runner = LiveRunner()
                runner.backlog["authority"]["generation"] = value
                with self.assertRaisesRegex(selector.SelectionError, "authority generation"):
                    selector.load_live("owner/repository", ROOT, 1, runner)

    def test_load_live_rejects_unpinned_generation(self):
        runner = LiveRunner()
        runner.backlog["authority"]["generation"] = 7
        with self.assertRaisesRegex(selector.SelectionError, "expected generation 1, found 7"):
            selector.load_live("owner/repository", ROOT, 1, runner)

    def test_load_live_requires_active_authority(self):
        for status in ("building", "historical"):
            with self.subTest(status=status):
                runner = LiveRunner()
                runner.backlog["authority"]["status"] = status
                with self.assertRaisesRegex(selector.SelectionError, "authority is not active"):
                    selector.load_live("owner/repository", ROOT, 1, runner)

    def test_load_live_requires_matching_authority_locator(self):
        runner = LiveRunner()
        runner.backlog["authority"]["locator"] = "someone/else"
        with self.assertRaisesRegex(selector.SelectionError, "authority locator"):
            selector.load_live("owner/repository", ROOT, 1, runner)

    def test_main_passes_the_sequence_generation_to_load_live(self):
        sequence = copy.deepcopy(SEQUENCE)
        sequence["backlog_generation"] = 2
        live = fixture()
        with (
            patch.object(selector, "load_sequence", return_value=sequence),
            patch.object(selector, "load_live", return_value=live) as load_live,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(selector.main(["--root", str(ROOT)]), 0)
        load_live.assert_called_once_with("owner/repository", ROOT.resolve(), 2)


class SelectionTests(unittest.TestCase):
    def test_first_ready_item_in_earliest_stage_is_selected(self):
        backlog, issues, audit = fixture()
        result = selector.select_next(SEQUENCE, backlog, issues, audit)
        self.assertEqual((result["status"], result["stage"], result["stable_id"]), ("ready", "first-demo", "L-01"))

    def test_closed_work_advances_to_the_next_item(self):
        backlog, issues, audit = fixture()
        issues[0]["state"] = "CLOSED"
        backlog["items"] = [entry for entry in backlog["items"] if entry["id"] != "L-01"]
        result = selector.select_next(SEQUENCE, backlog, issues, audit)
        self.assertEqual(result["stable_id"], "L-02")

    def test_a_waiting_item_blocks_the_stage_without_skipping(self):
        backlog, issues, audit = fixture()
        backlog["items"][0] = item("L-01", 1, state="waiting", waits=["owner"])
        result = selector.select_next(SEQUENCE, backlog, issues, audit)
        self.assertEqual((result["status"], result["stable_id"]), ("blocked", "L-01"))
        self.assertEqual(result["reason"], "waiting for: owner")

    def test_an_open_after_dependency_blocks_the_item(self):
        backlog, issues, audit = fixture()
        backlog["items"][0] = item("L-01", 1, relation={"type": "after", "ids": ["L-04"]})
        result = selector.select_next(SEQUENCE, backlog, issues, audit)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "open dependencies: L-04")

    def test_a_completed_terminal_advances_to_the_next_stage(self):
        backlog, issues, audit = fixture()
        for offset in range(3):
            issues[offset]["state"] = "CLOSED"
        backlog["items"] = [entry for entry in backlog["items"] if entry["id"] == "L-04"]
        result = selector.select_next(SEQUENCE, backlog, issues, audit)
        self.assertEqual((result["stage"], result["stable_id"]), ("mvp", "L-04"))

    def test_milestone_drift_is_refused(self):
        backlog, issues, audit = fixture()
        issues[0]["milestone"] = None
        with self.assertRaisesRegex(selector.SelectionError, "expected milestone"):
            selector.select_next(SEQUENCE, backlog, issues, audit)

    def test_an_unsequenced_item_in_a_governed_milestone_is_refused(self):
        backlog, issues, audit = fixture()
        issues.append(issue("L-99", 99, SEQUENCE["stages"][0]["github_milestone"]))
        with self.assertRaisesRegex(selector.SelectionError, "unsequenced items: L-99"):
            selector.select_next(SEQUENCE, backlog, issues, audit)

    def test_a_closed_terminal_with_open_prior_work_is_refused(self):
        backlog, issues, audit = fixture()
        issues[2]["state"] = "CLOSED"
        backlog["items"] = [entry for entry in backlog["items"] if entry["id"] != "L-03"]
        with self.assertRaisesRegex(selector.SelectionError, "closed terminal item"):
            selector.select_next(SEQUENCE, backlog, issues, audit)


if __name__ == "__main__":
    unittest.main()
