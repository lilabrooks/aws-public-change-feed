#!/usr/bin/env python3
"""Preview and apply bounded retirement of exact Lambda package versions."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from jsonschema import Draft202012Validator, FormatChecker

MINIMUM_RETENTION_DAYS = 400
MINIMUM_RETAINED_PACKAGES = 10
MAX_IDENTIFIER_CHARACTERS = 2048
PACKAGE_RE = re.compile(r"^(?P<prefix>.+)/(?P<digest>[0-9a-f]{64})\.zip$")
EXIT_INVALID = 2
EXIT_REFUSED = 3
EXIT_AMBIGUOUS = 4


class RetirementError(RuntimeError):
    """A bounded operator-facing refusal without provider response text."""

    def __init__(self, status: str, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def canonical_json(document: Mapping[str, Any]) -> bytes:
    return json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _utc_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise RetirementError("inventory_refused", "LastModified is not timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise RetirementError("stale_plan", f"{field} is malformed")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise RetirementError("stale_plan", f"{field} is malformed") from error
    if parsed.tzinfo is None:
        raise RetirementError("stale_plan", f"{field} is not timezone-aware")
    return parsed.astimezone(UTC)


def _bounded_string(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= MAX_IDENTIFIER_CHARACTERS
        or any(character in value for character in "\r\n\x00")
    ):
        raise RetirementError("inventory_refused", f"{field} must be a bounded single-line string")
    return value


def load_deployment(deployment_path: Path, schema_path: Path) -> dict[str, Any]:
    """Load reviewed bytes, validate their schema, and enforce hard safety floors."""

    try:
        deployment_bytes = deployment_path.read_bytes()
        deployment = yaml.safe_load(deployment_bytes)
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(deployment)
    except Exception as error:  # schema/YAML/filesystem details are not safe CLI output
        raise RetirementError("inventory_refused", "deployment document failed validation") from error
    if not isinstance(deployment, dict):
        raise RetirementError("inventory_refused", "deployment document must be an object")
    policy = deployment.get("application_package_retirement")
    if not isinstance(policy, dict):
        raise RetirementError("inventory_refused", "application package retirement policy is missing")
    retention_days = policy.get("retention_days")
    minimum_packages = policy.get("minimum_retained_packages")
    if (
        isinstance(retention_days, bool)
        or not isinstance(retention_days, int)
        or retention_days < MINIMUM_RETENTION_DAYS
    ):
        raise RetirementError("inventory_refused", "application package retention is below 400 days")
    if (
        isinstance(minimum_packages, bool)
        or not isinstance(minimum_packages, int)
        or minimum_packages < MINIMUM_RETAINED_PACKAGES
    ):
        raise RetirementError("inventory_refused", "application package newest-retained floor is below 10")
    active_key = _bounded_string(deployment.get("active_versions_object_key"), "active versions object key")
    top_prefix = str(PurePosixPath(active_key).parent)
    if top_prefix in ("", ".", "/"):
        raise RetirementError("inventory_refused", "active versions object key has no bounded top prefix")
    return {
        "deployment_sha256": sha256_bytes(deployment_bytes),
        "bucket": _bounded_string(deployment.get("config_bucket_name"), "configuration bucket"),
        "prefix": f"{top_prefix}/application-artifacts",
        "retention_days": retention_days,
        "minimum_retained_packages": minimum_packages,
    }


def parse_protected(values: Sequence[str] | None, assert_none: bool) -> list[dict[str, str]]:
    if assert_none:
        if values:
            raise RetirementError("inventory_refused", "protected references conflict with the explicit none assertion")
        return []
    if not values:
        raise RetirementError(
            "inventory_refused", "protected package references or an explicit none assertion are required"
        )
    protected: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for value in values:
        digest, separator, version_id = value.partition(":")
        if not separator or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise RetirementError("inventory_refused", "protected reference must be lowercase-digest:VersionId")
        version_id = _bounded_string(version_id, "protected VersionId")
        identity = (digest, version_id)
        if identity in seen:
            raise RetirementError("inventory_refused", "protected references must be unique")
        seen.add(identity)
        protected.append({"digest": digest, "version_id": version_id})
    return sorted(protected, key=lambda item: (item["digest"], item["version_id"]))


def _list_all_versions(client: Any, *, bucket: str, prefix: str, inventory_limit: int) -> tuple[list[Any], list[Any]]:
    if isinstance(inventory_limit, bool) or not isinstance(inventory_limit, int) or inventory_limit < 1:
        raise RetirementError("inventory_refused", "inventory limit must be a positive integer")
    versions: list[Any] = []
    delete_markers: list[Any] = []
    pagination_markers: set[tuple[str, str]] = set()
    request: dict[str, Any] = {"Bucket": bucket, "Prefix": f"{prefix}/"}
    while True:
        try:
            response = client.list_object_versions(**request)
        except Exception as error:
            raise RetirementError("inventory_failed", "S3 version inventory failed") from error
        page_versions = response.get("Versions", [])
        page_markers = response.get("DeleteMarkers", [])
        if not isinstance(page_versions, list) or not isinstance(page_markers, list):
            raise RetirementError("inventory_failed", "S3 version inventory response is malformed")
        versions.extend(page_versions)
        delete_markers.extend(page_markers)
        if len(versions) + len(delete_markers) > inventory_limit:
            raise RetirementError("inventory_limit_exceeded", "S3 version inventory exceeded the approved limit")
        truncated = response.get("IsTruncated", False)
        if truncated is False:
            break
        if truncated is not True:
            raise RetirementError("inventory_failed", "S3 version inventory truncation state is malformed")
        key_marker = response.get("NextKeyMarker")
        version_marker = response.get("NextVersionIdMarker")
        if (
            not isinstance(key_marker, str)
            or not key_marker
            or not isinstance(version_marker, str)
            or not version_marker
        ):
            raise RetirementError("inventory_failed", "S3 version inventory pagination markers are missing")
        markers = (key_marker, version_marker)
        if markers in pagination_markers:
            raise RetirementError("inventory_failed", "S3 version inventory pagination markers repeated")
        pagination_markers.add(markers)
        request["KeyMarker"] = key_marker
        request["VersionIdMarker"] = version_marker
    return versions, delete_markers


def inventory_packages(client: Any, *, bucket: str, prefix: str, inventory_limit: int) -> list[dict[str, Any]]:
    versions, delete_markers = _list_all_versions(client, bucket=bucket, prefix=prefix, inventory_limit=inventory_limit)
    if delete_markers:
        raise RetirementError("inventory_refused", "application package prefix contains a delete marker")
    by_key: dict[str, list[Any]] = defaultdict(list)
    for entry in versions:
        if not isinstance(entry, dict):
            raise RetirementError("inventory_refused", "application package version entry is malformed")
        key = entry.get("Key")
        if not isinstance(key, str):
            raise RetirementError("inventory_refused", "application package key is malformed")
        by_key[key].append(entry)
    rows: list[dict[str, Any]] = []
    for key in sorted(by_key):
        entries = by_key[key]
        match = PACKAGE_RE.fullmatch(key)
        if match is None or match.group("prefix") != prefix:
            raise RetirementError("inventory_refused", "application package prefix contains a malformed key")
        if len(entries) != 1:
            raise RetirementError("inventory_refused", "application package has version history")
        entry = entries[0]
        if entry.get("IsLatest") is not True:
            raise RetirementError("inventory_refused", "application package data version is not current")
        version_id = _bounded_string(entry.get("VersionId"), "application package VersionId")
        etag = _bounded_string(entry.get("ETag"), "application package ETag")
        last_modified = entry.get("LastModified")
        if not isinstance(last_modified, datetime):
            raise RetirementError("inventory_refused", "application package LastModified is malformed")
        size = entry.get("Size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise RetirementError("inventory_refused", "application package size is malformed")
        try:
            head = client.head_object(Bucket=bucket, Key=key, VersionId=version_id)
        except Exception as error:
            raise RetirementError("inventory_failed", "application package metadata read failed") from error
        metadata = head.get("Metadata") if isinstance(head, dict) else None
        digest = match.group("digest")
        if not isinstance(metadata, dict) or metadata.get("sha256") != digest:
            raise RetirementError("inventory_refused", "application package digest metadata does not match its key")
        if head.get("VersionId") not in (None, version_id) or head.get("ETag") not in (None, etag):
            raise RetirementError("inventory_refused", "application package metadata identity changed during inventory")
        rows.append(
            {
                "digest": digest,
                "etag": etag,
                "key": key,
                "last_modified": _utc_timestamp(last_modified),
                "size": size,
                "version_id": version_id,
            }
        )
    return rows


def classify_packages(
    rows: Sequence[Mapping[str, Any]],
    *,
    as_of: datetime,
    retention_days: int,
    minimum_retained_packages: int,
    protected: Sequence[Mapping[str, str]],
) -> list[dict[str, str]]:
    if as_of.tzinfo is None:
        raise RetirementError("inventory_refused", "preview as_of must be timezone-aware")
    ordered = sorted(
        rows, key=lambda row: (_parse_timestamp(row.get("last_modified"), "LastModified"), row["key"]), reverse=True
    )
    floor_time = None
    if len(ordered) >= minimum_retained_packages:
        floor_time = _parse_timestamp(ordered[minimum_retained_packages - 1].get("last_modified"), "LastModified")
    cutoff = as_of.astimezone(UTC) - timedelta(days=retention_days)
    protected_set = {(item["digest"], item["version_id"]) for item in protected}
    candidates: list[dict[str, str]] = []
    for row in rows:
        modified = _parse_timestamp(row.get("last_modified"), "LastModified")
        in_newest_floor = floor_time is None or modified >= floor_time
        identity = (str(row["digest"]), str(row["version_id"]))
        if modified <= cutoff and not in_newest_floor and identity not in protected_set:
            candidates.append({"digest": identity[0], "key": str(row["key"]), "version_id": identity[1]})
    return sorted(candidates, key=lambda item: (item["key"], item["version_id"]))


def _validate_protected(rows: Sequence[Mapping[str, Any]], protected: Sequence[Mapping[str, str]]) -> None:
    identities = {(row["digest"], row["version_id"]) for row in rows}
    if any((item["digest"], item["version_id"]) not in identities for item in protected):
        raise RetirementError("inventory_refused", "a protected package reference is absent from the exact inventory")


def create_plan(
    client: Any,
    *,
    deployment: Mapping[str, Any],
    inventory_limit: int,
    protected: Sequence[Mapping[str, str]],
    as_of: datetime,
) -> dict[str, Any]:
    rows = inventory_packages(
        client,
        bucket=str(deployment["bucket"]),
        prefix=str(deployment["prefix"]),
        inventory_limit=inventory_limit,
    )
    _validate_protected(rows, protected)
    candidates = classify_packages(
        rows,
        as_of=as_of,
        retention_days=int(deployment["retention_days"]),
        minimum_retained_packages=int(deployment["minimum_retained_packages"]),
        protected=protected,
    )
    return {
        "plan_version": 1,
        "deployment_sha256": deployment["deployment_sha256"],
        "bucket": deployment["bucket"],
        "prefix": deployment["prefix"],
        "as_of": _utc_timestamp(as_of),
        "policy": {
            "retention_days": deployment["retention_days"],
            "minimum_retained_packages": deployment["minimum_retained_packages"],
        },
        "inventory_limit": inventory_limit,
        "protected": list(protected),
        "inventory": rows,
        "deletion_candidates": candidates,
    }


def write_plan(path: Path, plan: Mapping[str, Any]) -> str:
    data = canonical_json(plan) + b"\n"
    path.write_bytes(data)
    return sha256_bytes(data)


def load_plan(path: Path, expected_sha256: str) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise RetirementError("stale_plan", "expected plan SHA-256 is malformed")
    try:
        data = path.read_bytes()
    except OSError as error:
        raise RetirementError("stale_plan", "saved plan cannot be read") from error
    if sha256_bytes(data) != expected_sha256:
        raise RetirementError("stale_plan", "saved plan SHA-256 differs from the expected digest")
    try:
        plan = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise RetirementError("stale_plan", "saved plan is malformed") from error
    if not isinstance(plan, dict) or canonical_json(plan) + b"\n" != data:
        raise RetirementError("stale_plan", "saved plan is not canonical JSON")
    return plan


def _is_absent_error(error: Exception) -> bool:
    response = getattr(error, "response", {})
    code = response.get("Error", {}).get("Code") if isinstance(response, dict) else None
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode") if isinstance(response, dict) else None
    return code in {"NoSuchKey", "NoSuchVersion", "404"} or status == 404


def _exact_version_absent(client: Any, *, bucket: str, row: Mapping[str, Any]) -> bool | None:
    try:
        client.head_object(Bucket=bucket, Key=row["key"], VersionId=row["version_id"])
    except Exception as error:
        return True if _is_absent_error(error) else None
    return False


def _result(
    status: str,
    plan_sha256: str,
    candidates: Sequence[Mapping[str, Any]],
    deleted: Sequence[Mapping[str, Any]],
    detail: str,
) -> dict[str, Any]:
    deleted_ids = {(row["key"], row["version_id"]) for row in deleted}
    return {
        "status": status,
        "plan_sha256": plan_sha256,
        "detail": detail,
        "proved_deleted": [dict(row) for row in deleted],
        "untouched": [dict(row) for row in candidates if (row["key"], row["version_id"]) not in deleted_ids],
    }


def apply_plan(
    client: Any,
    *,
    deployment: Mapping[str, Any],
    plan: Mapping[str, Any],
    plan_sha256: str,
    protected: Sequence[Mapping[str, str]],
) -> dict[str, Any]:
    required = {
        "deployment_sha256",
        "bucket",
        "prefix",
        "policy",
        "inventory_limit",
        "protected",
        "inventory",
        "deletion_candidates",
        "as_of",
        "plan_version",
    }
    if set(plan) != required or plan.get("plan_version") != 1:
        raise RetirementError("stale_plan", "saved plan shape is not supported")
    if (
        plan.get("deployment_sha256") != deployment["deployment_sha256"]
        or plan.get("bucket") != deployment["bucket"]
        or plan.get("prefix") != deployment["prefix"]
        or plan.get("policy")
        != {
            "retention_days": deployment["retention_days"],
            "minimum_retained_packages": deployment["minimum_retained_packages"],
        }
        or plan.get("protected") != list(protected)
    ):
        raise RetirementError("stale_plan", "deployment policy or protected references differ from the preview")
    limit = plan.get("inventory_limit")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise RetirementError("stale_plan", "saved inventory limit is malformed")
    try:
        fresh = inventory_packages(
            client,
            bucket=str(plan["bucket"]),
            prefix=str(plan["prefix"]),
            inventory_limit=limit,
        )
    except RetirementError as error:
        if error.status == "inventory_refused":
            raise RetirementError("stale_plan", "application package inventory is no longer eligible") from error
        raise
    if fresh != plan.get("inventory"):
        raise RetirementError("stale_plan", "application package inventory differs from the preview")
    expected_candidates = classify_packages(
        fresh,
        as_of=_parse_timestamp(plan.get("as_of"), "as_of"),
        retention_days=int(deployment["retention_days"]),
        minimum_retained_packages=int(deployment["minimum_retained_packages"]),
        protected=protected,
    )
    if expected_candidates != plan.get("deletion_candidates"):
        raise RetirementError("stale_plan", "saved deletion classification differs from the preview inputs")
    candidates = expected_candidates
    by_identity = {(row["key"], row["version_id"]): row for row in fresh}
    deleted: list[dict[str, Any]] = []
    for candidate in candidates:
        row = by_identity[(candidate["key"], candidate["version_id"])]
        delete_error: Exception | None = None
        refused = False
        try:
            client.delete_object(
                Bucket=plan["bucket"],
                Key=row["key"],
                VersionId=row["version_id"],
                IfMatch=row["etag"],
            )
        except Exception as error:
            delete_error = error
            response = getattr(error, "response", {})
            refused = isinstance(response, dict) and response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 412
        absence = _exact_version_absent(client, bucket=str(plan["bucket"]), row=row)
        if absence is True:
            deleted.append(dict(candidate))
            continue
        if absence is None:
            return _result("ambiguous", plan_sha256, candidates, deleted, "exact-version status is unreadable")
        if refused:
            return _result(
                "delete_refused", plan_sha256, candidates, deleted, "conditional exact-version delete was refused"
            )
        if delete_error is not None:
            return _result(
                "delete_failed", plan_sha256, candidates, deleted, "provider failure left the exact version present"
            )
        return _result(
            "delete_failed", plan_sha256, candidates, deleted, "exact version remained after the delete response"
        )
    try:
        final_rows = inventory_packages(
            client, bucket=str(plan["bucket"]), prefix=str(plan["prefix"]), inventory_limit=limit
        )
    except RetirementError:
        return _result(
            "applied_unverified", plan_sha256, candidates, deleted, "final complete inventory could not be proved"
        )
    deleted_ids = {(row["key"], row["version_id"]) for row in deleted}
    expected_remaining = [row for row in fresh if (row["key"], row["version_id"]) not in deleted_ids]
    if final_rows != expected_remaining:
        return _result(
            "applied_unverified",
            plan_sha256,
            candidates,
            deleted,
            "final inventory differs from the proved retained set",
        )
    return _result(
        "applied", plan_sha256, candidates, deleted, "all planned deletions and retained versions were proved"
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preview or apply exact application-package retirement.")
    parser.add_argument("action", choices=("preview", "apply"))
    parser.add_argument("--deployment", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    protection = parser.add_mutually_exclusive_group(required=True)
    protection.add_argument("--protected", action="append", metavar="DIGEST:VERSION_ID")
    protection.add_argument("--no-protected-packages", action="store_true")
    parser.add_argument("--inventory-limit", type=int, required=True)
    parser.add_argument("--expected-plan-sha256")
    return parser.parse_args(argv)


def _write(document: Mapping[str, Any], *, stream: Any = sys.stdout) -> None:
    print(canonical_json(document).decode("utf-8"), file=stream)


def main(argv: list[str] | None = None, *, client: Any | None = None, now: datetime | None = None) -> int:
    arguments = parse_args(argv)
    try:
        deployment = load_deployment(arguments.deployment, arguments.schema)
        protected = parse_protected(arguments.protected, arguments.no_protected_packages)
        if client is None:
            import boto3

            client = boto3.client("s3")
        if arguments.action == "preview":
            if arguments.expected_plan_sha256 is not None:
                raise RetirementError("inventory_refused", "preview does not accept an expected plan digest")
            plan = create_plan(
                client,
                deployment=deployment,
                inventory_limit=arguments.inventory_limit,
                protected=protected,
                as_of=now or datetime.now(UTC),
            )
            digest = write_plan(arguments.plan, plan)
            _write(
                {
                    "status": "previewed",
                    "plan_sha256": digest,
                    "inventory_count": len(plan["inventory"]),
                    "candidate_count": len(plan["deletion_candidates"]),
                }
            )
            return 0
        if arguments.expected_plan_sha256 is None:
            raise RetirementError("stale_plan", "apply requires the expected plan SHA-256")
        plan = load_plan(arguments.plan, arguments.expected_plan_sha256)
        if plan.get("inventory_limit") != arguments.inventory_limit:
            raise RetirementError("stale_plan", "inventory limit differs from the preview")
        result = apply_plan(
            client,
            deployment=deployment,
            plan=plan,
            plan_sha256=arguments.expected_plan_sha256,
            protected=protected,
        )
        _write(result)
        return (
            0
            if result["status"] == "applied"
            else (EXIT_AMBIGUOUS if result["status"] == "ambiguous" else EXIT_REFUSED)
        )
    except RetirementError as error:
        _write({"status": error.status, "detail": error.detail}, stream=sys.stderr)
        return (
            EXIT_AMBIGUOUS
            if error.status == "ambiguous"
            else (EXIT_INVALID if error.status == "inventory_refused" else EXIT_REFUSED)
        )


if __name__ == "__main__":
    raise SystemExit(main())
