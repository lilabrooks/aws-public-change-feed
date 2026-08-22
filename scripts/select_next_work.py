#!/usr/bin/env python3
"""Select the next repository work item without mutating GitHub."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import jsonschema

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aws_public_change_feed.parsing import load_unique_json, load_unique_yaml  # noqa: E402

DEFAULT_SEQUENCE = Path(".github/work-sequence.yaml")
SEQUENCE_SCHEMA = Path(".github/work-sequence.schema.json")
STABLE_ID_TITLE = re.compile(r"^\[(?P<stable_id>[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+)\](?:\s|$)")


class SelectionError(ValueError):
    """The roadmap, backlog, or GitHub projection cannot select work safely."""


CommandRunner = Callable[[Sequence[str]], str]
DIGEST_NAMES = ("content_sha256", "id_set_sha256", "open_count")


def run_command(command: Sequence[str]) -> str:
    try:
        completed = subprocess.run(command, check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError) as error:
        raise SelectionError(f"read-only dependency failed: {command[0]}") from error
    return completed.stdout


def _validated_digests(document: Mapping[str, Any], *, source: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name in DIGEST_NAMES:
        value = document.get(name)
        if name == "open_count":
            valid = isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else:
            valid = isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None
        if not valid:
            raise SelectionError(f"{source} has an invalid or missing {name}")
        result[name] = value
    return result


def _schema_errors(document: object, schema: Mapping[str, Any] | bool, path: Path) -> list[str]:
    validator = jsonschema.Draft202012Validator(schema)
    return [
        f"{path}: {'.'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message}"
        for error in sorted(validator.iter_errors(document), key=lambda item: list(item.absolute_path))
    ]


def validate_sequence(document: Mapping[str, Any], *, path: Path, schema_path: Path = ROOT / SEQUENCE_SCHEMA) -> None:
    schema = load_unique_json(schema_path.read_bytes())
    if not isinstance(schema, (Mapping, bool)):
        raise SelectionError(f"{schema_path}: expected a schema mapping")
    errors = _schema_errors(document, schema, path)
    if errors:
        raise SelectionError("\n".join(errors))

    stage_ids: set[str] = set()
    milestone_names: set[str] = set()
    sequenced: set[str] = set()
    for stage in document["stages"]:
        stage_id = stage["id"]
        milestone = stage["github_milestone"]
        sequence = stage["sequence"]
        if stage_id in stage_ids:
            errors.append(f"{path}: duplicate stage id: {stage_id}")
        stage_ids.add(stage_id)
        if milestone in milestone_names:
            errors.append(f"{path}: duplicate GitHub milestone: {milestone}")
        milestone_names.add(milestone)
        if len(sequence) != len(set(sequence)):
            errors.append(f"{path}: stage {stage_id} contains a duplicate stable ID")
        for stable_id in sequence:
            if stable_id in sequenced:
                errors.append(f"{path}: stable ID appears in more than one stage: {stable_id}")
            sequenced.add(stable_id)
        terminal = stage["terminal_item"]
        if terminal is not None and terminal not in sequence:
            errors.append(f"{path}: stage {stage_id} terminal_item must appear in its sequence")
    if errors:
        raise SelectionError("\n".join(errors))


def load_sequence(path: Path, *, schema_path: Path = ROOT / SEQUENCE_SCHEMA) -> Mapping[str, Any]:
    document = load_unique_yaml(path.read_bytes())
    if not isinstance(document, Mapping):
        raise SelectionError(f"{path}: expected a mapping")
    validate_sequence(document, path=path, schema_path=schema_path)
    return document


def _stable_id(issue: Mapping[str, Any]) -> str:
    title = issue.get("title")
    if not isinstance(title, str):
        raise SelectionError("GitHub issue title is missing")
    match = STABLE_ID_TITLE.match(title)
    if match is None:
        raise SelectionError(f"sequenced GitHub issue lacks a stable-ID title: #{issue.get('number')}")
    return match["stable_id"]


def _issue_projection(issues: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    projected: dict[str, Mapping[str, Any]] = {}
    for issue in issues:
        title = issue.get("title")
        if not isinstance(title, str):
            continue
        match = STABLE_ID_TITLE.match(title)
        if match is None:
            continue
        stable_id = match["stable_id"]
        if stable_id in projected:
            raise SelectionError(f"GitHub contains duplicate stable ID: {stable_id}")
        projected[stable_id] = issue
    return projected


def _milestone_title(issue: Mapping[str, Any]) -> str | None:
    milestone = issue.get("milestone")
    if milestone is None:
        return None
    if not isinstance(milestone, Mapping) or not isinstance(milestone.get("title"), str):
        raise SelectionError(f"GitHub issue has a malformed milestone: #{issue.get('number')}")
    return milestone["title"]


def _open_items(backlog: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    items = backlog.get("items")
    if not isinstance(items, list):
        raise SelectionError("canonical backlog has no items list")
    result: dict[str, Mapping[str, Any]] = {}
    for item in items:
        if not isinstance(item, Mapping) or not isinstance(item.get("id"), str):
            raise SelectionError("canonical backlog contains a malformed item")
        stable_id = item["id"]
        if stable_id in result:
            raise SelectionError(f"canonical backlog contains duplicate stable ID: {stable_id}")
        result[stable_id] = item
    return result


def _blocked_reason(item: Mapping[str, Any], open_items: Mapping[str, Mapping[str, Any]]) -> str | None:
    relation = item.get("relation", {})
    if isinstance(relation, Mapping) and relation.get("type") == "after":
        dependencies = relation.get("ids", [])
        if not isinstance(dependencies, list):
            raise SelectionError(f"{item['id']}: malformed after relation")
        unresolved = [dependency for dependency in dependencies if dependency in open_items]
        if unresolved:
            return f"open dependencies: {', '.join(unresolved)}"
    waits = item.get("wait", [])
    if waits:
        if not isinstance(waits, list) or any(not isinstance(wait, str) for wait in waits):
            raise SelectionError(f"{item['id']}: malformed wait reasons")
        return f"waiting for: {', '.join(waits)}"
    if item.get("state") != "ready":
        return f"state is {item.get('state', 'missing')}"
    return None


def select_next(
    sequence_document: Mapping[str, Any],
    backlog: Mapping[str, Any],
    issues: Sequence[Mapping[str, Any]],
    audit: Mapping[str, Any],
) -> dict[str, Any]:
    open_items = _open_items(backlog)
    projected = _issue_projection(issues)
    governed_milestones = {stage["github_milestone"]: stage for stage in sequence_document["stages"]}
    sequenced_ids = {stable_id for stage in sequence_document["stages"] for stable_id in stage["sequence"]}

    extras = sorted(
        _stable_id(issue)
        for issue in issues
        if _milestone_title(issue) in governed_milestones and _stable_id(issue) not in sequenced_ids
    )
    if extras:
        raise SelectionError(f"GitHub milestones contain unsequenced items: {', '.join(extras)}")

    for stage in sequence_document["stages"]:
        milestone = stage["github_milestone"]
        stage_issues: list[Mapping[str, Any]] = []
        for stable_id in stage["sequence"]:
            issue = projected.get(stable_id)
            if issue is None:
                raise SelectionError(f"work sequence names an unknown GitHub item: {stable_id}")
            if _milestone_title(issue) != milestone:
                raise SelectionError(
                    f"{stable_id}: expected milestone {milestone!r}, found {_milestone_title(issue)!r}"
                )
            stage_issues.append(issue)

        open_stage_ids = [
            stable_id
            for stable_id, issue in zip(stage["sequence"], stage_issues, strict=True)
            if issue.get("state") == "OPEN"
        ]
        terminal = stage["terminal_item"]
        if terminal is not None:
            terminal_issue = projected[terminal]
            if terminal_issue.get("state") == "CLOSED" and open_stage_ids:
                raise SelectionError(f"stage {stage['id']} has a closed terminal item and open sequenced work")
            stage_complete = terminal_issue.get("state") == "CLOSED"
        else:
            stage_complete = not open_stage_ids
        if stage_complete:
            continue
        if not open_stage_ids:
            raise SelectionError(f"stage {stage['id']} is incomplete but has no open sequenced item")

        selected_id = open_stage_ids[0]
        selected_issue = projected[selected_id]
        item = open_items.get(selected_id)
        if item is None:
            raise SelectionError(f"open GitHub item is absent from the canonical backlog: {selected_id}")
        if item.get("issue_number") != selected_issue.get("number"):
            raise SelectionError(f"{selected_id}: canonical and GitHub issue numbers disagree")
        reason = _blocked_reason(item, open_items)
        status = "blocked" if reason is not None else "ready"
        result = {
            "status": status,
            "stage": stage["id"],
            "milestone": milestone,
            "stable_id": selected_id,
            "issue": selected_issue["number"],
            "backlog_generation": audit["generation"],
            "backlog_content_sha256": audit["content_sha256"],
            "reason": reason or "first open ready item in the earliest unfinished stage",
        }
        return result

    return {
        "status": "complete",
        "backlog_generation": audit["generation"],
        "backlog_content_sha256": audit["content_sha256"],
        "reason": "every governed stage is complete",
    }


def load_live(
    repo: str, root: Path, expected_generation: int, runner: CommandRunner = run_command
) -> tuple[Mapping[str, Any], list[Any], dict[str, Any]]:
    audit_raw = load_unique_json(runner(["backlogctl", "github-audit", "-repo", repo]).encode())
    if not isinstance(audit_raw, Mapping):
        raise SelectionError("backlog audit returned a malformed document")
    digests = audit_raw.get("digests")
    if not isinstance(digests, Mapping):
        raise SelectionError("backlog audit omitted its digests")
    audit = _validated_digests(digests, source="backlog audit")
    with tempfile.TemporaryDirectory(prefix="apcf-next-work-") as temporary:
        path = Path(temporary) / "backlog.yaml"
        export_raw = load_unique_json(
            runner(["backlogctl", "github-export", "-repo", repo, "-output", str(path)]).encode()
        )
        runner(["backlogctl", "validate", "-file", str(path), "-root", str(root)])
        if not isinstance(export_raw, Mapping):
            raise SelectionError("backlog export returned malformed digests")
        export_digests = _validated_digests(export_raw, source="backlog export")
        for name in DIGEST_NAMES:
            if export_digests[name] != audit[name]:
                raise SelectionError(f"backlog changed between audit and export: {name}")
        backlog = load_unique_yaml(path.read_bytes())
    if not isinstance(backlog, Mapping):
        raise SelectionError("live backlog projection is malformed")
    authority = backlog.get("authority")
    if not isinstance(authority, Mapping):
        raise SelectionError("canonical backlog omitted its authority")
    if authority.get("status") != "active":
        raise SelectionError("canonical backlog authority is not active")
    if authority.get("locator") != repo:
        raise SelectionError("canonical backlog authority locator differs from the requested repository")
    generation = authority.get("generation")
    if not isinstance(generation, int) or isinstance(generation, bool):
        raise SelectionError("canonical backlog omitted its authority generation")
    if generation != expected_generation:
        raise SelectionError(f"canonical backlog expected generation {expected_generation}, found {generation}")
    issues = load_unique_json(
        runner(
            [
                "gh",
                "issue",
                "list",
                "--repo",
                repo,
                "--state",
                "all",
                "--limit",
                "1000",
                "--json",
                "number,title,state,milestone",
            ]
        ).encode()
    )
    if not isinstance(issues, list):
        raise SelectionError("live backlog projection is malformed")
    audit["generation"] = generation
    return backlog, issues, audit


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Select the next governed repository work item without mutation.")
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root")
    parser.add_argument(
        "--sequence",
        type=Path,
        default=DEFAULT_SEQUENCE,
        help="work sequence path, relative to --root unless absolute",
    )
    parser.add_argument("--repo", help="GitHub OWNER/NAME; must match the sequence when supplied")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_args(argv)
    root = arguments.root.resolve()
    sequence_path = arguments.sequence if arguments.sequence.is_absolute() else root / arguments.sequence
    try:
        sequence_document = load_sequence(sequence_path, schema_path=root / SEQUENCE_SCHEMA)
        repo = arguments.repo or sequence_document["repository"]
        if repo != sequence_document["repository"]:
            raise SelectionError("--repo must match the repository recorded by the work sequence")
        backlog, issues, audit = load_live(repo, root, sequence_document["backlog_generation"])
        result = select_next(sequence_document, backlog, issues, audit)
    except SelectionError as error:
        print(json.dumps({"status": "invalid", "reason": str(error)}, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 2 if result["status"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
