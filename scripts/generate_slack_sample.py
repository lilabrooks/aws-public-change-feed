#!/usr/bin/env python3
"""Generate the public Slack sample from the canonical delivery renderer."""

from __future__ import annotations

import argparse
import html
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aws_public_change_feed.parsing import load_unique_json, load_unique_yaml  # noqa: E402
from aws_public_change_feed.worker import render_message  # noqa: E402

PAGE_PATH = Path("site/index.html")
START_MARKER = "        <!-- GENERATED_SLACK_SAMPLE_START -->"
END_MARKER = "        <!-- GENERATED_SLACK_SAMPLE_END -->"
_BLOCK_TYPES = {"context", "section"}
_TEXT_TYPES = {"mrkdwn", "plain_text"}


class SampleGenerationError(ValueError):
    """The canonical sample cannot be generated without dropping information."""


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise SampleGenerationError(f"{label} must be an object with string keys")
    return cast(Mapping[str, Any], value)


def _sequence(value: object, label: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise SampleGenerationError(f"{label} must be an array")
    return cast(Sequence[object], value)


def _text_object(value: object, label: str) -> tuple[str, str, bool | None]:
    document = _mapping(value, label)
    text_type = document.get("type")
    text = document.get("text")
    if text_type not in _TEXT_TYPES or not isinstance(text, str):
        raise SampleGenerationError(f"{label} has an unsupported text shape")
    expected_fields = {"emoji", "text", "type"} if text_type == "plain_text" else {"text", "type"}
    if set(document) != expected_fields:
        raise SampleGenerationError(f"{label} does not have the exact supported {text_type} shape")
    emoji = document.get("emoji")
    if emoji is not None and not isinstance(emoji, bool):
        raise SampleGenerationError(f"{label}.emoji must be boolean")
    return text_type, text, emoji


def _html_text(value: str) -> str:
    return html.escape(value, quote=True).replace("\n", "<br>\n")


def project_payload(payload: Mapping[str, Any]) -> str:
    """Project every current Slack block into escaped, ordered static HTML."""

    if set(payload) != {"blocks", "mrkdwn", "text"}:
        raise SampleGenerationError("renderer payload has unsupported top-level fields")
    if payload["mrkdwn"] is not False or not isinstance(payload["text"], str):
        raise SampleGenerationError("renderer fallback must be a plain string with mrkdwn disabled")

    lines = ['        <article class="evidence-panel" data-rendered-slack-sample>']
    lines.append("          <h3>Canonical renderer output</h3>")
    blocks = _sequence(payload["blocks"], "renderer payload blocks")
    if not blocks:
        raise SampleGenerationError("renderer payload must contain at least one block")

    for index, raw_block in enumerate(blocks):
        label = f"renderer payload block {index}"
        block = _mapping(raw_block, label)
        block_type = block.get("type")
        if block_type not in _BLOCK_TYPES:
            raise SampleGenerationError(f"{label} has unsupported type: {block_type!r}")
        expected_fields = {"type", "text"} if block_type == "section" else {"elements", "type"}
        if set(block) != expected_fields:
            raise SampleGenerationError(f"{label} does not have the exact supported {block_type} shape")

        text_objects: list[tuple[str, str, bool | None]] = []
        if block_type == "section":
            text_objects.append(_text_object(block["text"], f"{label}.text"))
        else:
            elements = _sequence(block["elements"], f"{label}.elements")
            if not elements:
                raise SampleGenerationError(f"{label}.elements must not be empty")
            for element_index, element in enumerate(elements):
                text_objects.append(_text_object(element, f"{label}.elements[{element_index}]"))

        rendered = "<br>\n".join(_html_text(text) for _, text, _ in text_objects)
        text_types = " ".join(text_type for text_type, _, _ in text_objects)
        emoji_values = " ".join("absent" if emoji is None else str(emoji).lower() for _, _, emoji in text_objects)
        lines.append(
            f'          <p data-slack-block="{block_type}" data-slack-text-type="{text_types}" '
            f'data-slack-emoji="{emoji_values}">{rendered}</p>'
        )

    fallback = _html_text(payload["text"])
    lines.extend(
        [
            "          <details>",
            "            <summary>Accessible fallback text</summary>",
            f"            <p data-slack-fallback>{fallback}</p>",
            "          </details>",
            "        </article>",
        ]
    )
    return "\n".join(lines)


def canonical_payload(root: Path) -> Mapping[str, Any]:
    examples = root / "examples"
    candidate = _mapping(load_unique_json((examples / "alert-candidate.json").read_bytes()), "candidate")
    config = _mapping(load_unique_yaml((examples / "config.yaml").read_bytes()), "configuration")
    deployment = _mapping(load_unique_yaml((examples / "deployment.yaml").read_bytes()), "deployment")
    inventory = _mapping(load_unique_json((examples / "inventory.json").read_bytes()), "inventory")

    slack = _mapping(inventory.get("slack"), "inventory.slack")
    routes = _mapping(slack.get("routes"), "inventory.slack.routes")
    route_id = candidate.get("route_id")
    if not isinstance(route_id, str) or route_id not in routes:
        raise SampleGenerationError("candidate route_id does not resolve through inventory.slack.routes")
    route = _mapping(routes[route_id], f"inventory.slack.routes.{route_id}")
    usergroup_id = route.get("high_priority_usergroup_id")
    if usergroup_id is not None and not isinstance(usergroup_id, str):
        raise SampleGenerationError("high_priority_usergroup_id must be a string when present")

    message_policy = _mapping(config.get("message_policy"), "configuration.message_policy")
    fetch_policy = _mapping(deployment.get("feed_fetch_policy"), "deployment.feed_fetch_policy")
    approved_hosts = _sequence(fetch_policy.get("allowed_feed_hosts"), "allowed_feed_hosts")
    if not all(isinstance(host, str) for host in approved_hosts):
        raise SampleGenerationError("allowed_feed_hosts entries must be strings")

    return render_message(
        candidate,
        inventory,
        message_policy=message_policy,
        approved_source_hosts=cast(Sequence[str], approved_hosts),
        usergroup_id=usergroup_id,
    )


def generated_region(root: Path) -> str:
    return f"{START_MARKER}\n{project_payload(canonical_payload(root))}\n{END_MARKER}"


def _page_parts(root: Path) -> tuple[Path, str, str, str]:
    page_path = root / PAGE_PATH
    source = page_path.read_text(encoding="utf-8")
    if source.count(START_MARKER) != 1 or source.count(END_MARKER) != 1:
        raise SampleGenerationError("public page must contain one complete generated Slack sample marker pair")
    before, remainder = source.split(START_MARKER, 1)
    _, after = remainder.split(END_MARKER, 1)
    return page_path, source, before, after


def validation_errors(root: Path) -> list[str]:
    try:
        _, source, before, after = _page_parts(root)
        expected = f"{before}{generated_region(root)}{after}"
    except (OSError, UnicodeError, ValueError) as error:
        return [f"renderer-generated Slack sample cannot be verified: {error}"]
    if source != expected:
        return ["renderer-generated Slack sample is stale: run scripts/generate_slack_sample.py --write"]
    return []


def write_page(root: Path) -> bool:
    page_path, source, before, after = _page_parts(root)
    updated = f"{before}{generated_region(root)}{after}"
    if updated == source:
        return False
    page_path.write_text(updated, encoding="utf-8")
    return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate or check the canonical Slack sample on the public page.")
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root")
    parser.add_argument("--write", action="store_true", help="replace the marked sample region")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    if args.write:
        changed = write_page(root)
        print("updated renderer-generated Slack sample" if changed else "renderer-generated Slack sample is current")
        return 0
    errors = validation_errors(root)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print("renderer-generated Slack sample matches the canonical renderer")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
