"""Helpers for asserting how workflow steps pin their actions.

The workflow files are the single source of truth for which commit each action
is pinned to. Tests assert the property that matters — a known action, a full
40-character commit SHA, and a readable version comment — so a dependency bump
does not have to be mirrored into a constant here.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIRECTORY = ROOT / ".github/workflows"

PINNED_USES = re.compile(r"(?P<action>[\w.-]+/[\w./-]+)@(?P<sha>[0-9a-f]{40})")
USES_LINE = re.compile(r"^\s*-?\s*uses:\s*(?P<uses>\S+)(?:\s+#\s*(?P<comment>.*?))?\s*$")
VERSION_COMMENT = re.compile(r"^v\d+(?:\.\d+)*$")


def workflow_paths() -> list[Path]:
    """Return every committed workflow file, sorted by name."""
    return sorted(path for path in WORKFLOW_DIRECTORY.iterdir() if path.suffix in {".yml", ".yaml"})


def uses_entries(path: Path) -> list[tuple[int, str, str | None]]:
    """Return the line number, `uses` value, and trailing comment of each step action."""
    entries = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        match = USES_LINE.match(line)
        if match is not None:
            entries.append((number, match["uses"], match["comment"]))
    return entries


def assert_pinned(uses: str, action: str) -> str:
    """Assert that `uses` pins `action` to a full commit SHA and return that SHA."""
    match = PINNED_USES.fullmatch(uses)
    if match is None or match["action"] != action:
        raise AssertionError(f"expected {action} pinned to a full 40-character commit SHA, found {uses!r}")
    return match["sha"]
