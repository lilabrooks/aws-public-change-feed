#!/usr/bin/env python3

import argparse
import hashlib
import re
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "site" / "architecture.drawio"
DEFAULT_EXPORT = ROOT / "site" / "architecture.svg"
HASH_ATTRIBUTE = "data-drawio-source-sha256"
SVG_TAG = re.compile(r"<svg\b[^>]*>", re.DOTALL)
HASH_VALUE = re.compile(rf'\s{HASH_ATTRIBUTE}="[0-9a-f]{{64}}"')
TITLE = "AWS Public Change Alerting processing flow"
DESCRIPTION = (
    "Approved public AWS feeds and exact immutable releases produce route-scoped candidates, "
    "durable delivery state, destination-grouped SQS FIFO work, Slack delivery, and bounded recovery. "
    "AWS service icons identify Lambda, S3, DynamoDB, SQS, Secrets Manager, Systems Manager, CloudWatch, and SNS."
)


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def set_attribute(opening_tag: str, name: str, value: str) -> str:
    pattern = re.compile(rf'\s{name}="[^"]*"')
    replacement = f' {name}="{value}"'
    if pattern.search(opening_tag):
        return pattern.sub(replacement, opening_tag, count=1)
    return opening_tag.replace("<svg", f"<svg{replacement}", 1)


def ensure_accessible_element(svg: str, tag: str, element_id: str, text: str, insert_at: int) -> tuple[str, int]:
    pattern = re.compile(rf"<{tag}\b[^>]*>", re.IGNORECASE)
    match = pattern.search(svg)
    if match is None:
        element = f'\n  <{tag} id="{element_id}">{text}</{tag}>'
        return svg[:insert_at] + element + svg[insert_at:], insert_at + len(element)
    opening_tag = set_attribute(match.group(0), "id", element_id)
    replacement = svg[: match.start()] + opening_tag + svg[match.end() :]
    return replacement, insert_at + len(opening_tag) - len(match.group(0))


def stamp_export(source_path: Path, export_path: Path) -> str:
    source_root = ET.parse(source_path).getroot()
    if local_name(source_root.tag) != "mxfile":
        raise ValueError("draw.io source root must be mxfile")

    export_root = ET.parse(export_path).getroot()
    if local_name(export_root.tag) != "svg":
        raise ValueError("draw.io export root must be svg")

    source_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
    svg = export_path.read_text(encoding="utf-8")
    match = SVG_TAG.search(svg)
    if match is None:
        raise ValueError("SVG opening tag is missing")
    opening_tag = match.group(0)
    if HASH_VALUE.search(opening_tag):
        opening_tag = HASH_VALUE.sub(f' {HASH_ATTRIBUTE}="{source_sha256}"', opening_tag, count=1)
    else:
        opening_tag = set_attribute(opening_tag, HASH_ATTRIBUTE, source_sha256)
    opening_tag = set_attribute(opening_tag, "role", "img")
    opening_tag = set_attribute(opening_tag, "aria-labelledby", "diagram-title diagram-description")
    svg = svg[: match.start()] + opening_tag + svg[match.end() :]
    insert_at = match.start() + len(opening_tag)
    svg, insert_at = ensure_accessible_element(svg, "title", "diagram-title", TITLE, insert_at)
    svg, _ = ensure_accessible_element(svg, "desc", "diagram-description", DESCRIPTION, insert_at)
    export_path.write_text(svg, encoding="utf-8")
    return source_sha256


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bind a draw.io SVG export to its exact editable source.")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="editable .drawio source")
    parser.add_argument("--export", type=Path, default=DEFAULT_EXPORT, help="SVG exported from draw.io")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    digest = stamp_export(args.source.resolve(), args.export.resolve())
    print(f"stamped {args.export} with draw.io source SHA-256 {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
