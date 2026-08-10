#!/usr/bin/env python3

"""Evaluate the matcher against the labeled corpus and the approved thresholds.

Chapter 04 requires every matcher change to be evaluated against a versioned
historical corpus, and chapter 06 requires the recorded per-service and
per-risk figures to meet the approved thresholds. ADR-018 sets those numbers.
"""

import argparse
import json
import sys
from pathlib import Path

import jsonschema
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aws_public_change_feed.corpus import load_corpus, load_thresholds  # noqa: E402
from aws_public_change_feed.evaluation import evaluate_corpus, format_report  # noqa: E402
from aws_public_change_feed.matching import load_risk_rules, load_services  # noqa: E402
from aws_public_change_feed.schema_formats import contract_format_checker  # noqa: E402

CORPUS_PATH = Path("corpus/announcements.json")
THRESHOLDS_PATH = Path("corpus/thresholds.json")
CONFIG_PATH = Path("examples/config.yaml")
CORPUS_SCHEMA_PATH = Path("schemas/corpus.schema.json")
THRESHOLDS_SCHEMA_PATH = Path("schemas/corpus-thresholds.schema.json")

# ADR-018 bounds the committed corpus so repository growth stays a deliberate
# decision rather than a side effect of adding items.
MAX_CORPUS_BYTES = 2 * 1024 * 1024


def validate_document(document_path: Path, schema_path: Path) -> list[str]:
    with document_path.open(encoding="utf-8") as handle:
        document = json.load(handle)
    with schema_path.open(encoding="utf-8") as handle:
        schema = json.load(handle)
    validator = jsonschema.Draft202012Validator(schema, format_checker=contract_format_checker())
    return [
        f"{document_path}: {'/'.join(str(part) for part in error.path) or '<root>'}: {error.message}"
        for error in sorted(validator.iter_errors(document), key=lambda error: list(error.path))
    ]


def duplicate_ids(corpus_path: Path) -> list[str]:
    with corpus_path.open(encoding="utf-8") as handle:
        document = json.load(handle)
    seen: set[str] = set()
    duplicates: list[str] = []
    for entry in document["items"]:
        item_id = entry["id"]
        if item_id in seen:
            duplicates.append(f"{corpus_path}: duplicate item id: {item_id}")
        seen.add(item_id)
    return duplicates


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate matching against the labeled corpus.")
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root")
    parser.add_argument("--json", action="store_true", help="emit the machine-readable summary")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.root.resolve()

    corpus_path = root / CORPUS_PATH
    thresholds_path = root / THRESHOLDS_PATH

    errors = validate_document(corpus_path, root / CORPUS_SCHEMA_PATH)
    errors.extend(validate_document(thresholds_path, root / THRESHOLDS_SCHEMA_PATH))
    errors.extend(duplicate_ids(corpus_path))

    corpus_bytes = corpus_path.stat().st_size
    if corpus_bytes > MAX_CORPUS_BYTES:
        errors.append(f"{CORPUS_PATH}: corpus is {corpus_bytes} bytes, above the {MAX_CORPUS_BYTES} byte ceiling")

    if errors:
        for issue in errors:
            print(issue, file=sys.stderr)
        return 1

    with (root / CONFIG_PATH).open(encoding="utf-8") as handle:
        configuration = yaml.safe_load(handle)

    report = evaluate_corpus(
        items=load_corpus(corpus_path),
        services=load_services(configuration),
        rules=load_risk_rules(configuration),
        thresholds=load_thresholds(thresholds_path),
    )

    if args.json:
        from aws_public_change_feed.evaluation import report_summary

        print(json.dumps(report_summary(report), indent=2, sort_keys=True))
    else:
        print(format_report(report))

    if not report.passed:
        print("", file=sys.stderr)
        for failure in report.failures:
            print(failure, file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
