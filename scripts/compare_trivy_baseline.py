#!/usr/bin/env python3

"""Compare root-scoped Trivy Terraform JSON with the reviewed baseline."""

import argparse
import json
import posixpath
import sys
from collections import Counter
from pathlib import Path

import yaml

FindingKey = tuple[str, str, str, str]


class BaselineError(ValueError):
    """The baseline or scanner result cannot support an exact comparison."""


def require_mapping(value, context: str) -> dict:
    if not isinstance(value, dict):
        raise BaselineError(f"{context} must be a mapping")
    return value


def require_list(value, context: str) -> list:
    if not isinstance(value, list):
        raise BaselineError(f"{context} must be a list")
    return value


def require_string(value, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise BaselineError(f"{context} must be a nonempty string")
    return value


def require_positive_integer(value, context: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise BaselineError(f"{context} must be a positive integer")
    return value


def normalized_repository_path(path: str, context: str) -> str:
    if path.startswith("/") or "\\" in path:
        raise BaselineError(f"{context} must be a repository-relative POSIX path: {path!r}")
    normalized = posixpath.normpath(path)
    if normalized in {"", ".", ".."} or normalized.startswith("../"):
        raise BaselineError(f"{context} escapes or omits the repository path: {path!r}")
    return normalized


def normalize_result_target(root_path: str, target: str, context: str) -> str:
    if target.startswith("/") or "\\" in target:
        raise BaselineError(f"{context} must be relative to its scan root: {target!r}")
    return normalized_repository_path(posixpath.join(root_path, target), context)


def load_baseline(path: Path) -> tuple[str, dict[str, str], Counter[FindingKey], int]:
    with path.open(encoding="utf-8") as handle:
        document = require_mapping(yaml.safe_load(handle), str(path))

    scanner = require_mapping(document.get("scanner"), f"{path}: scanner")
    scanner_version = require_string(scanner.get("version"), f"{path}: scanner.version")

    roots_document = require_mapping(document.get("scan_roots"), f"{path}: scan_roots")
    root_paths: dict[str, str] = {}
    for name, raw_root_path in roots_document.items():
        root_name = require_string(name, f"{path}: scan root name")
        root_path = require_string(raw_root_path, f"{path}: scan_roots.{root_name}")
        root_paths[root_name] = normalized_repository_path(root_path, f"{path}: scan_roots.{root_name}")
    if not root_paths:
        raise BaselineError(f"{path}: scan_roots must not be empty")

    expected: Counter[FindingKey] = Counter()
    classified_total = 0
    classifications = require_list(document.get("classifications"), f"{path}: classifications")
    for classification_index, raw_classification in enumerate(classifications):
        classification = require_mapping(raw_classification, f"{path}: classifications[{classification_index}]")
        class_name = require_string(
            classification.get("class"), f"{path}: classifications[{classification_index}].class"
        )
        declared_class_count = require_positive_integer(
            classification.get("finding_count"), f"{path}: classification {class_name}.finding_count"
        )
        class_count = 0
        findings = require_list(classification.get("findings"), f"{path}: classification {class_name}.findings")
        for finding_index, raw_finding in enumerate(findings):
            finding = require_mapping(raw_finding, f"{path}: classification {class_name}.findings[{finding_index}]")
            finding_id = require_string(
                finding.get("id"), f"{path}: classification {class_name}.findings[{finding_index}].id"
            )
            severity = require_string(
                finding.get("severity"), f"{path}: classification {class_name}.{finding_id}.severity"
            )
            occurrences = require_list(
                finding.get("occurrences"), f"{path}: classification {class_name}.{finding_id}.occurrences"
            )
            for occurrence_index, raw_occurrence in enumerate(occurrences):
                occurrence = require_mapping(
                    raw_occurrence,
                    f"{path}: classification {class_name}.{finding_id}.occurrences[{occurrence_index}]",
                )
                scan_root = require_string(
                    occurrence.get("scan_root"),
                    f"{path}: classification {class_name}.{finding_id}.occurrences[{occurrence_index}].scan_root",
                )
                if scan_root not in root_paths:
                    raise BaselineError(f"{path}: unknown scan root in baseline occurrence: {scan_root}")
                finding_path = normalized_repository_path(
                    require_string(
                        occurrence.get("path"),
                        f"{path}: classification {class_name}.{finding_id}.occurrences[{occurrence_index}].path",
                    ),
                    f"{path}: classification {class_name}.{finding_id} occurrence path",
                )
                count = require_positive_integer(
                    occurrence.get("count"),
                    f"{path}: classification {class_name}.{finding_id}.occurrences[{occurrence_index}].count",
                )
                key = (scan_root, finding_id, severity, finding_path)
                if key in expected:
                    raise BaselineError(f"{path}: finding occurrence is classified more than once: {key}")
                expected[key] = count
                class_count += count
        if class_count != declared_class_count:
            raise BaselineError(
                f"{path}: classification {class_name} declares {declared_class_count} findings but contains {class_count}"
            )
        classified_total += class_count

    totals = require_mapping(document.get("totals"), f"{path}: totals")
    declared_total = require_positive_integer(totals.get("findings"), f"{path}: totals.findings")
    severity_document = require_mapping(totals.get("severity"), f"{path}: totals.severity")
    declared_severity = {
        require_string(severity, f"{path}: totals.severity key"): require_positive_integer(
            count, f"{path}: totals.severity.{severity}"
        )
        for severity, count in severity_document.items()
    }
    actual_severity: Counter[str] = Counter()
    for (_, _, severity, _), count in expected.items():
        actual_severity[severity] += count
    if dict(sorted(actual_severity.items())) != dict(sorted(declared_severity.items())):
        raise BaselineError(
            f"{path}: declared severity totals {dict(sorted(declared_severity.items()))} "
            f"do not match classified findings {dict(sorted(actual_severity.items()))}"
        )
    if sum(expected.values()) != classified_total or classified_total != declared_total:
        raise BaselineError(
            f"{path}: totals.findings declares {declared_total} but classifications contain {classified_total}"
        )
    return scanner_version, root_paths, expected, declared_total


def load_result(path: Path, scan_root: str, root_path: str, scanner_version: str) -> Counter[FindingKey]:
    with path.open(encoding="utf-8") as handle:
        document = require_mapping(json.load(handle), str(path))
    if document.get("SchemaVersion") != 2:
        raise BaselineError(f"{path}: unsupported Trivy JSON SchemaVersion: {document.get('SchemaVersion')!r}")
    trivy = require_mapping(document.get("Trivy"), f"{path}: Trivy")
    result_version = require_string(trivy.get("Version"), f"{path}: Trivy.Version")
    if result_version != scanner_version:
        raise BaselineError(
            f"{path}: Trivy version {result_version!r} does not match baseline version {scanner_version!r}"
        )
    artifact_name = normalized_repository_path(
        require_string(document.get("ArtifactName"), f"{path}: ArtifactName"), f"{path}: ArtifactName"
    )
    if artifact_name != root_path:
        raise BaselineError(f"{path}: artifact {artifact_name!r} does not match scan root path {root_path!r}")

    findings: Counter[FindingKey] = Counter()
    for result_index, raw_result in enumerate(require_list(document.get("Results"), f"{path}: Results")):
        result = require_mapping(raw_result, f"{path}: Results[{result_index}]")
        target = require_string(result.get("Target"), f"{path}: Results[{result_index}].Target")
        misconfigurations = result.get("Misconfigurations") or []
        for finding_index, raw_finding in enumerate(
            require_list(misconfigurations, f"{path}: Results[{result_index}].Misconfigurations")
        ):
            finding = require_mapping(
                raw_finding, f"{path}: Results[{result_index}].Misconfigurations[{finding_index}]"
            )
            if finding.get("Status") != "FAIL":
                continue
            finding_id = require_string(
                finding.get("ID"), f"{path}: Results[{result_index}].Misconfigurations[{finding_index}].ID"
            )
            severity = require_string(
                finding.get("Severity"),
                f"{path}: Results[{result_index}].Misconfigurations[{finding_index}].Severity",
            )
            finding_path = normalize_result_target(root_path, target, f"{path}: Results[{result_index}].Target")
            findings[(scan_root, finding_id, severity, finding_path)] += 1
    return findings


def parse_result(value: str) -> tuple[str, Path]:
    name, separator, raw_path = value.partition("=")
    if not separator or not name or not raw_path:
        raise argparse.ArgumentTypeError("result must use NAME=PATH")
    return name, Path(raw_path)


def format_finding(key: FindingKey, count: int) -> str:
    scan_root, finding_id, severity, path = key
    return f"{count} x {scan_root}: {finding_id} {severity} {path}"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare Trivy Terraform JSON with the reviewed baseline.")
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--result", required=True, action="append", type=parse_result, metavar="NAME=PATH")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        scanner_version, root_paths, expected, declared_total = load_baseline(args.baseline)
        result_paths = dict(args.result)
        if len(result_paths) != len(args.result):
            raise BaselineError("each --result scan root name must be unique")
        if set(result_paths) != set(root_paths):
            raise BaselineError(f"result roots {sorted(result_paths)} do not match baseline roots {sorted(root_paths)}")
        observed: Counter[FindingKey] = Counter()
        for scan_root in sorted(root_paths):
            observed.update(load_result(result_paths[scan_root], scan_root, root_paths[scan_root], scanner_version))
    except (BaselineError, OSError, json.JSONDecodeError, yaml.YAMLError) as error:
        print(f"Trivy baseline comparison could not run: {error}", file=sys.stderr)
        return 2

    missing = expected - observed
    unexpected = observed - expected
    if missing or unexpected:
        print("Trivy Terraform findings differ from the reviewed baseline.", file=sys.stderr)
        for key, count in sorted(missing.items()):
            print(f"missing: {format_finding(key, count)}", file=sys.stderr)
        for key, count in sorted(unexpected.items()):
            print(f"unexpected: {format_finding(key, count)}", file=sys.stderr)
        return 1

    print(f"Trivy Terraform baseline matches {declared_total} findings across {len(root_paths)} scan roots.")
    for key, count in sorted(observed.items()):
        print(format_finding(key, count))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
