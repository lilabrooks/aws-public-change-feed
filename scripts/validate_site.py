#!/usr/bin/env python3

import argparse
import hashlib
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

import generate_slack_sample

ROOT = Path(__file__).resolve().parents[1]
PAGE_PATH = Path("site/index.html")
DRAWIO_PATH = Path("site/architecture.drawio")
DRAWIO_EXPORT_PATH = Path("site/architecture.svg")
DRAWIO_HASH_ATTRIBUTE = "data-drawio-source-sha256"
EXPECTED_DIAGRAM_NODES = {
    "feeds",
    "release",
    "package",
    "watcher",
    "normalizer",
    "matcher",
    "routes",
    "source_state",
    "raw_snapshots",
    "outbox",
    "dispatcher",
    "queue",
    "worker",
    "slack",
    "dlq",
    "credentials",
    "reconciler",
    "operations",
}
EXPECTED_DIAGRAM_EDGES = {
    "e_feeds_watcher": ("feeds", "watcher"),
    "e_release_watcher": ("release", "watcher"),
    "e_release_matcher": ("release", "matcher"),
    "e_release_worker": ("release", "worker"),
    "e_package_watcher": ("package", "watcher"),
    "e_package_dispatcher": ("package", "dispatcher"),
    "e_package_worker": ("package", "worker"),
    "e_watcher_normalizer": ("watcher", "normalizer"),
    "e_normalizer_matcher": ("normalizer", "matcher"),
    "e_matcher_routes": ("matcher", "routes"),
    "e_routes_outbox": ("routes", "outbox"),
    "e_watcher_source_state": ("watcher", "source_state"),
    "e_normalizer_source_state": ("normalizer", "source_state"),
    "e_watcher_snapshots": ("watcher", "raw_snapshots"),
    "e_outbox_dispatcher": ("outbox", "dispatcher"),
    "e_dispatcher_queue": ("dispatcher", "queue"),
    "e_queue_worker": ("queue", "worker"),
    "e_worker_slack": ("worker", "slack"),
    "e_worker_outbox": ("worker", "outbox"),
    "e_credentials_worker": ("credentials", "worker"),
    "e_queue_dlq": ("queue", "dlq"),
    "e_dlq_queue": ("dlq", "queue"),
    "e_reconciler_outbox": ("reconciler", "outbox"),
    "e_outbox_reconciler": ("outbox", "reconciler"),
}
EXPECTED_DIAGRAM_ICONS = {
    "icon_release_s3": "mxgraph.aws4.s3",
    "icon_package_s3": "mxgraph.aws4.s3",
    "icon_watcher_lambda": "mxgraph.aws4.lambda",
    "icon_source_state_dynamodb": "mxgraph.aws4.dynamodb",
    "icon_raw_snapshots_s3": "mxgraph.aws4.s3",
    "icon_outbox_dynamodb": "mxgraph.aws4.dynamodb",
    "icon_dispatcher_lambda": "mxgraph.aws4.lambda",
    "icon_queue_sqs": "mxgraph.aws4.sqs",
    "icon_worker_lambda": "mxgraph.aws4.lambda",
    "icon_dlq_sqs": "mxgraph.aws4.sqs",
    "icon_credentials_secrets_manager": "mxgraph.aws4.secrets_manager",
    "icon_credentials_systems_manager": "mxgraph.aws4.systems_manager",
    "icon_reconciler_lambda": "mxgraph.aws4.lambda",
    "icon_operations_cloudwatch": "mxgraph.aws4.cloudwatch",
    "icon_operations_sns": "mxgraph.aws4.sns",
}
REQUIRED_SITE_FILES = {
    PAGE_PATH,
    DRAWIO_PATH,
    DRAWIO_EXPORT_PATH,
    Path("site/.nojekyll"),
    Path("site/compact-theme.css"),
    Path("site/compact-theme.js"),
    Path("site/compact-theme-LICENSE.txt"),
    Path("site/compact-theme-COPYRIGHT.txt"),
    Path("site/fonts/IBMPlexMono-Regular.woff2"),
    Path("site/fonts/IBMPlexMono-SemiBold.woff2"),
    Path("site/fonts/IBMPlexSans-Regular.woff2"),
    Path("site/fonts/IBMPlexSans-SemiBold.woff2"),
    Path("site/fonts/LICENSE.txt"),
}
PUBLIC_NARRATIVE_PREFIXES = (
    "docs/architecture/",
    "docs/adr/",
    "schemas/",
    "examples/",
)
PUBLIC_NARRATIVE_FILES = {"docs/GOAL.md"}
REQUIRED_PAGE_IDS = {"content", "value", "contracts", "slack-sample", "flow", "decisions", "evidence", "source"}


class PublicPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tag_counts: dict[str, int] = {}
        self.ids: list[str] = []
        self.references: list[tuple[str, str, str]] = []
        self.title_parts: list[str] = []
        self.h1_parts: list[str] = []
        self.diagram_images: list[dict[str, str | None]] = []
        self.html_language: str | None = None
        self._in_title = False
        self._in_h1 = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        self.tag_counts[tag] = self.tag_counts.get(tag, 0) + 1
        if tag == "html":
            self.html_language = attributes.get("lang")
        element_id = attributes.get("id")
        if element_id:
            self.ids.append(element_id)
        for attribute in ("href", "src"):
            value = attributes.get(attribute)
            if value:
                self.references.append((tag, attribute, value))
        if tag == "title":
            self._in_title = True
        if tag == "h1":
            self._in_h1 = True
        if tag == "img" and "data-architecture-diagram" in attributes:
            self.diagram_images.append(attributes)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag == "h1":
            self._in_h1 = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        if self._in_h1:
            self.h1_parts.append(data)


def normalized_text(parts: Iterable[str]) -> str:
    return " ".join("".join(parts).split())


def resolve_local_reference(page: Path, raw_reference: str) -> Path | None:
    parsed = urlsplit(raw_reference)
    if parsed.scheme or parsed.netloc or raw_reference.startswith("#"):
        return None
    target_text = unquote(parsed.path)
    if not target_text:
        return page
    target = (page.parent / target_text).resolve()
    if target.is_dir():
        target = target / "index.html"
    return target


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def validate_drawio_artifacts(root: Path) -> list[str]:
    errors: list[str] = []
    source_path = root / DRAWIO_PATH
    export_path = root / DRAWIO_EXPORT_PATH
    if not source_path.is_file() or not export_path.is_file():
        return errors

    try:
        drawio_root = ET.parse(source_path).getroot()
    except (ET.ParseError, OSError) as error:
        return [f"cannot parse {DRAWIO_PATH}: {error}"]
    if _local_name(drawio_root.tag) != "mxfile":
        errors.append(f"{DRAWIO_PATH}: root element must be mxfile")
        return errors

    diagrams = [child for child in drawio_root if _local_name(child.tag) == "diagram"]
    if len(diagrams) != 1:
        errors.append(f"{DRAWIO_PATH}: expected exactly one diagram")
        return errors
    graph_models = [child for child in diagrams[0] if _local_name(child.tag) == "mxGraphModel"]
    if len(graph_models) != 1:
        errors.append(f"{DRAWIO_PATH}: expected one uncompressed mxGraphModel")
        return errors

    cells = {
        cell.get("id"): cell for cell in graph_models[0].iter() if _local_name(cell.tag) == "mxCell" and cell.get("id")
    }
    for node_id in sorted(EXPECTED_DIAGRAM_NODES):
        cell = cells.get(node_id)
        if cell is None or cell.get("vertex") != "1" or not (cell.get("value") or "").strip():
            errors.append(f"{DRAWIO_PATH}: missing required labeled node {node_id}")
    for edge_id, (source, target) in sorted(EXPECTED_DIAGRAM_EDGES.items()):
        cell = cells.get(edge_id)
        if cell is None or cell.get("edge") != "1" or cell.get("source") != source or cell.get("target") != target:
            errors.append(f"{DRAWIO_PATH}: edge {edge_id} must connect {source} to {target}")
    for icon_id, resource_icon in sorted(EXPECTED_DIAGRAM_ICONS.items()):
        cell = cells.get(icon_id)
        style = cell.get("style", "") if cell is not None else ""
        if (
            cell is None
            or cell.get("vertex") != "1"
            or "shape=mxgraph.aws4.resourceIcon" not in style
            or f"resIcon={resource_icon}" not in style
        ):
            errors.append(f"{DRAWIO_PATH}: icon {icon_id} must use {resource_icon}")

    try:
        svg_root = ET.parse(export_path).getroot()
    except (ET.ParseError, OSError) as error:
        errors.append(f"cannot parse {DRAWIO_EXPORT_PATH}: {error}")
        return errors
    if _local_name(svg_root.tag) != "svg":
        errors.append(f"{DRAWIO_EXPORT_PATH}: root element must be svg")
        return errors

    source_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
    if svg_root.get(DRAWIO_HASH_ATTRIBUTE) != source_sha256:
        errors.append(
            f"{DRAWIO_EXPORT_PATH}: export is stale for {DRAWIO_PATH}; "
            "regenerate the SVG and stamp its exact source hash"
        )
    child_names = {_local_name(child.tag) for child in svg_root}
    if not {"title", "desc"}.issubset(child_names):
        errors.append(f"{DRAWIO_EXPORT_PATH}: accessible title and description are required")
    if svg_root.get("role") != "img" or not svg_root.get("aria-labelledby"):
        errors.append(f"{DRAWIO_EXPORT_PATH}: role=img and aria-labelledby are required")
    return errors


def validate_repository(root: Path) -> list[str]:
    errors: list[str] = []
    for relative_path in sorted(REQUIRED_SITE_FILES):
        path = root / relative_path
        if not path.exists():
            errors.append(f"missing required public-site file: {relative_path}")
        elif relative_path != Path("site/.nojekyll") and path.stat().st_size == 0:
            errors.append(f"public-site file is empty: {relative_path}")

    page = root / PAGE_PATH
    drawio_source_path = root / DRAWIO_PATH
    drawio_export_path = root / DRAWIO_EXPORT_PATH
    readme = root / "README.md"
    if (
        not page.is_file()
        or not drawio_source_path.is_file()
        or not drawio_export_path.is_file()
        or not readme.is_file()
    ):
        return errors

    parser = PublicPageParser()
    try:
        parser.feed(page.read_text(encoding="utf-8"))
        parser.close()
    except (OSError, UnicodeError, ValueError) as error:
        errors.append(f"cannot parse {PAGE_PATH}: {error}")
        return errors

    if parser.html_language != "en":
        errors.append(f"{PAGE_PATH}: html language must be en")
    for tag in ("main", "nav", "footer"):
        if parser.tag_counts.get(tag) != 1:
            errors.append(f"{PAGE_PATH}: expected exactly one {tag} element")
    if parser.tag_counts.get("h1") != 1:
        errors.append(f"{PAGE_PATH}: expected exactly one h1 element")
    if normalized_text(parser.title_parts) != "AWS Public Change Alerting":
        errors.append(f"{PAGE_PATH}: page title must be AWS Public Change Alerting")
    if not normalized_text(parser.h1_parts):
        errors.append(f"{PAGE_PATH}: h1 must contain text")

    duplicate_ids = sorted(element_id for element_id in set(parser.ids) if parser.ids.count(element_id) > 1)
    for element_id in duplicate_ids:
        errors.append(f"{PAGE_PATH}: duplicate id: {element_id}")
    missing_ids = sorted(REQUIRED_PAGE_IDS - set(parser.ids))
    for element_id in missing_ids:
        errors.append(f"{PAGE_PATH}: missing required section id: {element_id}")

    repository_root = root.resolve()
    for tag, attribute, reference in parser.references:
        parsed = urlsplit(reference)
        if reference.startswith("#"):
            fragment = unquote(parsed.fragment)
            if fragment and fragment not in parser.ids:
                errors.append(f"{PAGE_PATH}: {tag} {attribute} points to missing fragment: {reference}")
            continue
        target = resolve_local_reference(page, reference)
        if target is None:
            continue
        try:
            target.relative_to(repository_root)
        except ValueError:
            errors.append(f"{PAGE_PATH}: local reference escapes the repository: {reference}")
            continue
        if not target.exists():
            errors.append(f"{PAGE_PATH}: {tag} {attribute} target does not exist: {reference}")

    if len(parser.diagram_images) != 1:
        errors.append(f"{PAGE_PATH}: expected exactly one draw.io architecture image")
    else:
        image = parser.diagram_images[0]
        if image.get("src") != "./architecture.svg":
            errors.append(f"{PAGE_PATH}: draw.io architecture image must load ./architecture.svg")
        if not (image.get("alt") or "").strip():
            errors.append(f"{PAGE_PATH}: draw.io architecture image requires alt text")
        if image.get("width") != "1800" or image.get("height") != "780":
            errors.append(f"{PAGE_PATH}: architecture image dimensions must match the SVG viewBox")

    page_references = {reference for _, _, reference in parser.references}
    for expected_reference in ("./architecture.drawio", "./architecture.svg"):
        if expected_reference not in page_references:
            errors.append(f"{PAGE_PATH}: missing architecture artifact link: {expected_reference}")
    if "mermaid" in page.read_text(encoding="utf-8").casefold():
        errors.append(f"{PAGE_PATH}: Mermaid source or runtime references are no longer allowed")

    errors.extend(validate_drawio_artifacts(root))

    theme_css = (root / "site/compact-theme.css").read_text(encoding="utf-8")
    theme_js = (root / "site/compact-theme.js").read_text(encoding="utf-8")
    for theme_path, text in (("site/compact-theme.css", theme_css), ("site/compact-theme.js", theme_js)):
        if "SPDX-License-Identifier: BSD-2-Clause" not in text:
            errors.append(f"{theme_path}: Compact Theme SPDX notice is missing")

    readme_text = readme.read_text(encoding="utf-8")
    page_url = "https://lilabrooks.github.io/aws-public-change-feed/"
    if page_url not in readme_text:
        errors.append(f"README.md: public architecture page link is missing: {page_url}")
    if "```mermaid" in readme_text:
        errors.append("README.md: architecture diagrams use the committed draw.io source on the public page")

    errors.extend(generate_slack_sample.validation_errors(root))

    return errors


def page_update_required(changed_files: Iterable[str]) -> bool:
    for raw_path in changed_files:
        path = raw_path.strip().replace("\\", "/")
        if not path or path == PAGE_PATH.as_posix():
            continue
        if path.startswith("site/"):
            return True
        if path in PUBLIC_NARRATIVE_FILES or path.startswith(PUBLIC_NARRATIVE_PREFIXES):
            return True
    return False


def validate_site_sync(changed_files: Iterable[str]) -> list[str]:
    normalized = {path.strip().replace("\\", "/") for path in changed_files if path.strip()}
    if page_update_required(normalized) and PAGE_PATH.as_posix() not in normalized:
        return [
            "public architecture content is stale: architecture, contract, example, or site files changed "
            f"without updating {PAGE_PATH}"
        ]
    return []


def git_changed_files(root: Path, base: str, head: str) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...{head}", "--"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.splitlines()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate the GitHub Pages site and its architecture-content sync.")
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root")
    parser.add_argument("--base", help="base Git revision for content-sync validation")
    parser.add_argument("--head", default="HEAD", help="head Git revision for content-sync validation")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    errors = validate_repository(root)
    if args.base:
        try:
            errors.extend(validate_site_sync(git_changed_files(root, args.base, args.head)))
        except subprocess.CalledProcessError as failure:
            errors.append(f"cannot inspect public-site changes: {failure}")

    if errors:
        for issue in errors:
            print(issue, file=sys.stderr)
        return 1

    print("public architecture page passed structure, assets, draw.io/SVG, Slack sample, and content-sync validation")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
