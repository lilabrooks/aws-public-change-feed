#!/usr/bin/env python3

import argparse
import html
import re
import sys
import tomllib
from datetime import date
from pathlib import Path
from urllib.parse import unquote, urlsplit

WARNING_AGE_DAYS = 180
MAXIMUM_AGE_DAYS = 365
LYCHEE_CONFIGURATION_POLICY: dict[str, object] = {
    "cache": True,
    "max_cache_age": "1d",
    "cache_exclude_status": "400..",
    "max_redirects": 10,
    "max_retries": 5,
    "max_concurrency": 16,
    "timeout": 30,
    "retry_wait_time": 3,
    "method": "get",
    "require_https": True,
    "include_fragments": "anchor-only",
    "include_verbatim": True,
    "host_concurrency": 2,
    "host_request_interval": "250ms",
    "extensions": ["md"],
    "scheme": ["http", "https"],
    "exclude_all_private": True,
    "include_mail": False,
    "no_progress": True,
}
SKIPPED_DIRECTORIES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".terraform",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "venv",
}

HTTP_URL_RE = re.compile(r"https?://[^\s<>\]`]+")
REFERENCE_PREFIX_RE = re.compile(r"\bReferences? (?:verified|checked):")
REFERENCE_DATE_RE = re.compile(r"\bReferences? (?:verified|checked):\s*(\d{4}-\d{2}-\d{2})(?!\d)")
MARKDOWN_LINK_RE = re.compile(
    r"!?\[[^\]\n]*\]\(\s*"
    r"(?:<(?P<angle>[^>\n]+)>|(?P<plain>[^)\s]+))"
    r"(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^)]*\)))?\s*\)"
)
REFERENCE_DEFINITION_RE = re.compile(
    r"^\s{0,3}\[(?P<label>[^\]\n]+)\]:\s*"
    r"(?:<(?P<angle>[^>\n]+)>|(?P<plain>[^\s\n]+))"
    r"(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^)]*\)))?\s*$",
    re.MULTILINE,
)
REFERENCE_LINK_RE = re.compile(r"!?\[(?P<text>[^\]\n]*)\]\[(?P<label>[^\]\n]*)\]")
SHORTCUT_REFERENCE_RE = re.compile(r"!?\[(?P<label>[^\]\n]+)\](?![\[(])")
FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*$")
SETEXT_HEADING_RE = re.compile(r"^\s{0,3}(?:=+|-+)\s*$")


def parse_iso_date(raw_value: str, context: str) -> date:
    try:
        parsed = date.fromisoformat(raw_value)
    except ValueError as error:
        raise ValueError(f"{context}: invalid ISO date: {raw_value}") from error
    if parsed.isoformat() != raw_value:
        raise ValueError(f"{context}: date must use YYYY-MM-DD: {raw_value}")
    return parsed


def markdown_files(root: Path) -> list[Path]:
    return sorted(
        path for path in root.rglob("*.md") if not SKIPPED_DIRECTORIES.intersection(path.relative_to(root).parts)
    )


def mask_markdown_verbatim(text: str) -> str:
    masked_lines = []
    fence_character = None
    for line in text.splitlines(keepends=True):
        fence_match = FENCE_RE.match(line)
        if fence_match:
            character = fence_match.group(1)[0]
            if fence_character is None:
                fence_character = character
            elif fence_character == character:
                fence_character = None
            masked_lines.append(re.sub(r"[^\r\n]", " ", line))
            continue
        if fence_character is not None:
            masked_lines.append(re.sub(r"[^\r\n]", " ", line))
            continue
        masked_lines.append(re.sub(r"`[^`\n]*`", lambda match: " " * len(match.group(0)), line))
    return "".join(masked_lines)


def github_heading_slug(raw_heading: str) -> str:
    heading = re.sub(r"\[([^\]]+)]\([^)]+\)", r"\1", raw_heading)
    heading = re.sub(r"<[^>]+>", "", heading)
    heading = html.unescape(heading).replace("`", "").lower()
    characters = []
    for character in heading:
        if character.isspace():
            characters.append("-")
        elif character.isalnum() or character in {"-", "_"}:
            characters.append(character)
    return "".join(characters)


def markdown_anchors(path: Path) -> set[str]:
    anchors = set()
    slug_counts: dict[str, int] = {}
    fence_character = None
    lines = path.read_text(encoding="utf-8").splitlines()
    frontmatter_end = None
    if lines and lines[0].strip() == "---":
        frontmatter_end = next(
            (line_number for line_number, line in enumerate(lines[1:], start=1) if line.strip() == "---"),
            None,
        )
    previous_line = None

    def add_heading(raw_heading: str) -> None:
        base_slug = github_heading_slug(raw_heading)
        duplicate_number = slug_counts.get(base_slug, 0)
        slug_counts[base_slug] = duplicate_number + 1
        slug = base_slug if duplicate_number == 0 else f"{base_slug}-{duplicate_number}"
        anchors.add(slug)

    for line_number, line in enumerate(lines):
        if frontmatter_end is not None and line_number <= frontmatter_end:
            previous_line = None
            continue
        fence_match = FENCE_RE.match(line)
        if fence_match:
            character = fence_match.group(1)[0]
            if fence_character is None:
                fence_character = character
            elif fence_character == character:
                fence_character = None
            previous_line = None
            continue
        if fence_character is not None:
            previous_line = None
            continue
        heading_match = HEADING_RE.match(line)
        if heading_match:
            raw_heading = re.sub(r"\s+#+\s*$", "", heading_match.group(1))
            add_heading(raw_heading)
        elif (
            SETEXT_HEADING_RE.match(line)
            and previous_line
            and previous_line.strip()
            and not HEADING_RE.match(previous_line)
        ):
            add_heading(previous_line.strip())
        previous_line = line
    return anchors


def normalized_reference_label(raw_label: str) -> str:
    return " ".join(raw_label.split()).casefold()


def markdown_reference_definitions(text: str) -> dict[str, tuple[str, int]]:
    definitions: dict[str, tuple[str, int]] = {}
    for match in REFERENCE_DEFINITION_RE.finditer(text):
        label = normalized_reference_label(match.group("label"))
        destination = match.group("angle") or match.group("plain")
        definitions.setdefault(label, (destination, match.start()))
    return definitions


def spans_overlap(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(start < span_end and end > span_start for span_start, span_end in spans)


def validate_reference_dates(
    path: Path,
    text: str,
    as_of: date,
    warning_age_days: int,
    maximum_age_days: int,
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if not HTTP_URL_RE.search(text):
        return errors, warnings

    marker_found = False
    scannable_text = mask_markdown_verbatim(text)
    for line_number, line in enumerate(scannable_text.splitlines(), start=1):
        if not REFERENCE_PREFIX_RE.search(line):
            continue
        marker_found = True
        date_match = REFERENCE_DATE_RE.search(line)
        context = f"{path}:{line_number}"
        if not date_match:
            errors.append(f"{context}: reference marker must contain a YYYY-MM-DD date")
            continue
        try:
            verified_on = parse_iso_date(date_match.group(1), context)
        except ValueError as error:
            errors.append(str(error))
            continue
        age_days = (as_of - verified_on).days
        if age_days < 0:
            errors.append(f"{context}: reference verification date is in the future: {verified_on}")
        elif age_days > maximum_age_days:
            errors.append(f"{context}: references were last verified {age_days} days ago (maximum {maximum_age_days})")
        elif age_days > warning_age_days:
            warnings.append(
                f"{context}: references were last verified {age_days} days ago "
                f"(review warning after {warning_age_days})"
            )

    if not marker_found:
        errors.append(f"{path}: external URLs require a 'References verified: YYYY-MM-DD' marker")
    return errors, warnings


def resolve_local_target(root: Path, source: Path, raw_path: str) -> Path:
    decoded_path = unquote(raw_path)
    if decoded_path.startswith("/"):
        target = root / decoded_path.lstrip("/")
    elif decoded_path:
        target = source.parent / decoded_path
    else:
        target = source
    return target.resolve()


def validate_local_destination(root: Path, source: Path, destination: str, line_number: int) -> list[str]:
    parsed = urlsplit(destination)
    if parsed.scheme or parsed.netloc:
        return []

    errors: list[str] = []
    context = f"{source}:{line_number}"
    target = resolve_local_target(root, source, parsed.path)
    try:
        target.relative_to(root)
    except ValueError:
        return [f"{context}: local link escapes the repository: {destination}"]
    if not target.exists():
        return [f"{context}: local link target does not exist: {destination}"]
    if not parsed.fragment:
        return errors
    if target.suffix.lower() != ".md":
        return [f"{context}: fragment target is not a Markdown file: {destination}"]
    fragment = unquote(parsed.fragment)
    if fragment not in markdown_anchors(target):
        errors.append(f"{context}: Markdown anchor does not exist: {destination}")
    return errors


def validate_local_links(root: Path, path: Path, text: str) -> list[str]:
    errors = []
    root = root.resolve()
    scannable_text = mask_markdown_verbatim(text)
    for match in MARKDOWN_LINK_RE.finditer(scannable_text):
        destination = match.group("angle") or match.group("plain")
        line_number = scannable_text.count("\n", 0, match.start()) + 1
        errors.extend(validate_local_destination(root, path, destination, line_number))

    definitions = markdown_reference_definitions(scannable_text)
    definition_spans = [match.span() for match in REFERENCE_DEFINITION_RE.finditer(scannable_text)]
    full_reference_spans = []
    references = []
    for match in REFERENCE_LINK_RE.finditer(scannable_text):
        full_reference_spans.append(match.span())
        label = match.group("label") or match.group("text")
        references.append((normalized_reference_label(label), match.start()))

    ignored_spans = definition_spans + full_reference_spans
    for match in SHORTCUT_REFERENCE_RE.finditer(scannable_text):
        if spans_overlap(match.start(), match.end(), ignored_spans):
            continue
        label = normalized_reference_label(match.group("label"))
        if label in definitions:
            references.append((label, match.start()))

    for label, position in references:
        definition = definitions.get(label)
        if definition is None:
            continue
        destination, _ = definition
        line_number = scannable_text.count("\n", 0, position) + 1
        errors.extend(validate_local_destination(root, path, destination, line_number))
    return errors


def validate_lychee_exclusions(root: Path, as_of: date) -> list[str]:
    path = root / ".lycheeignore"
    if not path.exists():
        return [f"{path}: missing documented Lychee exclusion list"]

    errors = []
    lines = path.read_text(encoding="utf-8").splitlines()
    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        context = f"{path}:{line_number}"
        reason_line = lines[line_number - 3].strip() if line_number >= 3 else ""
        expiry_line = lines[line_number - 2].strip() if line_number >= 2 else ""

        if not reason_line.startswith("# Reason:") or not reason_line.removeprefix("# Reason:").strip():
            errors.append(f"{context}: exclusion requires '# Reason: ...' exactly two lines above")

        if not expiry_line.startswith("# Expires:"):
            errors.append(f"{context}: exclusion requires '# Expires: YYYY-MM-DD' directly above")
            continue
        raw_expiry = expiry_line.removeprefix("# Expires:").strip()
        try:
            expiry = parse_iso_date(raw_expiry, f"{path}:{line_number - 1}")
        except ValueError as error:
            errors.append(str(error))
            continue
        if expiry < as_of:
            errors.append(f"{context}: exclusion expired on {expiry}")
    return errors


def validate_lychee_configuration(root: Path) -> list[str]:
    path = root / "lychee.toml"
    if not path.exists():
        return [f"{path}: missing Lychee configuration"]
    try:
        with path.open("rb") as handle:
            configuration = tomllib.load(handle)
    except tomllib.TOMLDecodeError as error:
        return [f"{path}: invalid TOML: {error}"]

    errors = []
    for key, expected in LYCHEE_CONFIGURATION_POLICY.items():
        if key not in configuration:
            errors.append(f"{path}: required Lychee setting is missing: {key}")
        elif configuration[key] != expected:
            errors.append(f"{path}: Lychee setting {key} must be {expected!r}; found {configuration[key]!r}")
    for key in sorted(set(configuration).difference(LYCHEE_CONFIGURATION_POLICY)):
        errors.append(f"{path}: unreviewed Lychee setting is not allowed: {key}")
    return errors


def validate_repository(
    root: Path,
    as_of: date,
    warning_age_days: int = WARNING_AGE_DAYS,
    maximum_age_days: int = MAXIMUM_AGE_DAYS,
) -> tuple[list[str], list[str], int, int]:
    if warning_age_days < 0 or maximum_age_days <= warning_age_days:
        raise ValueError("reference age policy requires 0 <= warning days < maximum days")

    errors = validate_lychee_configuration(root)
    errors.extend(validate_lychee_exclusions(root, as_of))
    warnings = []
    files = markdown_files(root)
    external_urls = set()
    for path in files:
        text = path.read_text(encoding="utf-8")
        relative_path = path.relative_to(root)
        external_urls.update(HTTP_URL_RE.findall(text))
        date_errors, date_warnings = validate_reference_dates(
            relative_path,
            text,
            as_of,
            warning_age_days,
            maximum_age_days,
        )
        errors.extend(date_errors)
        warnings.extend(date_warnings)
        errors.extend(validate_local_links(root, path, text))
    return errors, warnings, len(files), len(external_urls)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    parser.add_argument("--warning-age-days", type=int, default=WARNING_AGE_DAYS)
    parser.add_argument("--maximum-age-days", type=int, default=MAXIMUM_AGE_DAYS)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    try:
        errors, warnings, file_count, url_count = validate_repository(
            root,
            args.as_of,
            args.warning_age_days,
            args.maximum_age_days,
        )
    except (OSError, UnicodeError, ValueError) as exception:
        print(exception, file=sys.stderr)
        return 1
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if errors:
        for finding in errors:
            print(f"error: {finding}", file=sys.stderr)
        return 1
    print(f"reference validation passed for {file_count} Markdown files and {url_count} external URLs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
