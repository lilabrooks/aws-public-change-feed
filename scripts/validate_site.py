#!/usr/bin/env python3

import argparse
import subprocess
import sys
from collections.abc import Iterable
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
PAGE_PATH = Path("site/index.html")
MERMAID_PATH = Path("site/architecture.mmd")
MERMAID_RUNTIME = "https://cdn.jsdelivr.net/npm/mermaid@11.16.0/dist/mermaid.esm.min.mjs"
REQUIRED_SITE_FILES = {
    PAGE_PATH,
    MERMAID_PATH,
    Path("site/.nojekyll"),
    Path("site/compact-theme.css"),
    Path("site/compact-theme.js"),
    Path("site/compact-theme-LICENSE.txt"),
    Path("site/compact-theme-COPYRIGHT.txt"),
    Path("site/site.js"),
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
REQUIRED_PAGE_IDS = {"content", "value", "ai", "flow", "decisions", "evidence", "source"}


class PublicPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tag_counts: dict[str, int] = {}
        self.ids: list[str] = []
        self.references: list[tuple[str, str, str]] = []
        self.title_parts: list[str] = []
        self.h1_parts: list[str] = []
        self.diagram_parts: list[str] = []
        self.html_language: str | None = None
        self._in_title = False
        self._in_h1 = False
        self._in_diagram = False

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
        if tag == "pre" and "data-architecture-diagram" in attributes:
            self._in_diagram = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag == "h1":
            self._in_h1 = False
        if tag == "pre" and self._in_diagram:
            self._in_diagram = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        if self._in_h1:
            self.h1_parts.append(data)
        if self._in_diagram:
            self.diagram_parts.append(data)


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


def validate_repository(root: Path) -> list[str]:
    errors: list[str] = []
    for relative_path in sorted(REQUIRED_SITE_FILES):
        path = root / relative_path
        if not path.exists():
            errors.append(f"missing required public-site file: {relative_path}")
        elif relative_path != Path("site/.nojekyll") and path.stat().st_size == 0:
            errors.append(f"public-site file is empty: {relative_path}")

    page = root / PAGE_PATH
    mermaid_source_path = root / MERMAID_PATH
    readme = root / "README.md"
    site_script = root / "site/site.js"
    if not page.is_file() or not mermaid_source_path.is_file() or not readme.is_file() or not site_script.is_file():
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

    source = mermaid_source_path.read_text(encoding="utf-8").strip()
    embedded_source = "".join(parser.diagram_parts).strip()
    if not embedded_source:
        errors.append(f"{PAGE_PATH}: missing embedded Mermaid architecture source")
    elif embedded_source != source:
        errors.append(f"{PAGE_PATH}: embedded Mermaid source differs from {MERMAID_PATH}")

    script_text = site_script.read_text(encoding="utf-8")
    if MERMAID_RUNTIME not in script_text:
        errors.append(f"site/site.js: Mermaid runtime must be pinned to {MERMAID_RUNTIME}")
    if "@latest" in script_text:
        errors.append("site/site.js: unpinned @latest dependency is not allowed")

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
        errors.append("README.md: Mermaid architecture belongs on the public page")

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

    print("public architecture page passed structure, asset, Mermaid, and content-sync validation")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
