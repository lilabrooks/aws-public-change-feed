#!/usr/bin/env python3
"""Preview and apply bounded retirement of immutable configuration releases."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aws_public_change_feed.identity import release_id as derive_release_id  # noqa: E402
from aws_public_change_feed.parsing import load_unique_json  # noqa: E402

MINIMUM_RETENTION_DAYS = 400
MINIMUM_RETAINED_RELEASES = 10
MAX_IDENTIFIER_CHARACTERS = 2048
MAX_RELEASE_OBJECT_BYTES = 10 * 1024 * 1024
MAX_POINTER_BYTES = 64 * 1024
SHA256_RE = re.compile(r"[a-f0-9]{64}")
EXIT_INVALID = 2
EXIT_REFUSED = 3
EXIT_AMBIGUOUS = 4


class RetirementError(RuntimeError):
    """A bounded operator-facing refusal without provider response text."""

    def __init__(self, status: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def canonical_json(document: Mapping[str, Any]) -> bytes:
    return json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _utc_timestamp(value: datetime, *, status: str = "inventory_refused") -> str:
    if value.tzinfo is None:
        raise RetirementError(status, "inventory timestamp is not timezone-aware")
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


def _bounded_string(value: object, field: str, *, status: str = "inventory_refused") -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= MAX_IDENTIFIER_CHARACTERS
        or any(character in value for character in "\r\n\x00")
    ):
        raise RetirementError(status, f"{field} must be a bounded single-line string")
    return value


def _positive_integer(value: object, field: str, *, status: str = "inventory_refused") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RetirementError(status, f"{field} must be a positive integer")
    return value


def load_deployment(deployment_path: Path, schema_path: Path) -> dict[str, Any]:
    """Load reviewed deployment bytes and enforce the accepted release floors."""

    try:
        deployment_bytes = deployment_path.read_bytes()
        deployment = yaml.safe_load(deployment_bytes)
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(deployment)
    except Exception as error:
        raise RetirementError("inventory_refused", "deployment document failed validation") from error
    if not isinstance(deployment, dict):
        raise RetirementError("inventory_refused", "deployment document must be an object")
    lifecycle = deployment.get("s3_lifecycle")
    if not isinstance(lifecycle, dict):
        raise RetirementError("inventory_refused", "release retirement policy is missing")
    retention_days = lifecycle.get("retired_release_retention_days")
    minimum_releases = lifecycle.get("minimum_retained_releases")
    if (
        isinstance(retention_days, bool)
        or not isinstance(retention_days, int)
        or retention_days < MINIMUM_RETENTION_DAYS
    ):
        raise RetirementError("inventory_refused", f"release retention is below {MINIMUM_RETENTION_DAYS} days")
    if (
        isinstance(minimum_releases, bool)
        or not isinstance(minimum_releases, int)
        or minimum_releases < MINIMUM_RETAINED_RELEASES
    ):
        raise RetirementError("inventory_refused", "release newest-retained floor is below 10")
    return {
        "deployment_sha256": sha256_bytes(deployment_bytes),
        "bucket": _bounded_string(deployment.get("config_bucket_name"), "configuration bucket"),
        "release_prefix": _bounded_string(deployment.get("release_prefix"), "release prefix").rstrip("/"),
        "config_filename": _bounded_string(deployment.get("config_filename"), "configuration filename"),
        "inventory_filename": _bounded_string(deployment.get("inventory_filename"), "inventory filename"),
        "pointer_key": _bounded_string(deployment.get("active_versions_object_key"), "active pointer key"),
        "retention_days": retention_days,
        "minimum_retained_releases": minimum_releases,
    }


def load_pointer_validator(schema_path: Path) -> Draft202012Validator:
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
    except Exception as error:
        raise RetirementError("inventory_refused", "active pointer schema failed validation") from error
    return Draft202012Validator(schema, format_checker=FormatChecker())


def parse_protected(values: Sequence[str] | None, assert_none: bool) -> list[str]:
    if assert_none:
        if values:
            raise RetirementError("inventory_refused", "protected releases conflict with the explicit none assertion")
        return []
    if not values:
        raise RetirementError("inventory_refused", "protected releases or an explicit none assertion are required")
    protected: set[str] = set()
    for value in values:
        if SHA256_RE.fullmatch(value) is None:
            raise RetirementError("inventory_refused", "protected release must be a lowercase SHA-256 release ID")
        if value in protected:
            raise RetirementError("inventory_refused", "protected releases must be unique")
        protected.add(value)
    return sorted(protected)


def _list_all_versions(
    client: Any, *, bucket: str, prefix: str, inventory_limit: int, exact_key: str | None = None
) -> tuple[list[Any], list[Any]]:
    limit = _positive_integer(inventory_limit, "inventory limit")
    versions: list[Any] = []
    delete_markers: list[Any] = []
    pagination_markers: set[tuple[str, str]] = set()
    request: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": min(limit + 1, 1000)}
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
        if len(versions) + len(delete_markers) > limit:
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
    if exact_key is not None:
        entries = [*versions, *delete_markers]
        if any(not isinstance(entry, dict) or entry.get("Key") != exact_key for entry in entries):
            raise RetirementError("inventory_refused", "active pointer inventory returned a prefix sibling")
    return versions, delete_markers


def _read_exact_object(
    client: Any,
    *,
    bucket: str,
    key: str,
    version_id: str,
    etag: str,
    expected_size: int,
    maximum_size: int,
    kind: str,
) -> bytes:
    if expected_size > maximum_size:
        raise RetirementError("inventory_refused", f"{kind} exceeds its retirement inventory byte limit")
    try:
        response = client.get_object(Bucket=bucket, Key=key, VersionId=version_id)
        body = response["Body"].read(maximum_size + 1)
    except Exception as error:
        raise RetirementError("inventory_failed", f"{kind} exact-version read failed") from error
    if not isinstance(body, bytes) or len(body) != expected_size:
        raise RetirementError("inventory_refused", f"{kind} body length differs from its inventory row")
    if response.get("VersionId") not in (None, version_id) or response.get("ETag") not in (None, etag):
        raise RetirementError("inventory_refused", f"{kind} identity changed during inventory")
    return body


def inventory_release_objects(
    client: Any, *, deployment: Mapping[str, Any], inventory_limit: int
) -> list[dict[str, Any]]:
    """Read every exact release object and bind its digest to its release key."""

    prefix = str(deployment["release_prefix"])
    filenames = {
        str(deployment["config_filename"]): "config",
        str(deployment["inventory_filename"]): "inventory",
    }
    versions, delete_markers = _list_all_versions(
        client,
        bucket=str(deployment["bucket"]),
        prefix=f"{prefix}/",
        inventory_limit=inventory_limit,
    )
    if delete_markers:
        raise RetirementError("inventory_refused", "release prefix contains a delete marker")
    rows: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for entry in versions:
        if not isinstance(entry, dict):
            raise RetirementError("inventory_refused", "release version entry is malformed")
        key = _bounded_string(entry.get("Key"), "release object key")
        relative = key.removeprefix(f"{prefix}/")
        parts = relative.split("/")
        if len(parts) != 2 or SHA256_RE.fullmatch(parts[0]) is None or parts[1] not in filenames:
            raise RetirementError("inventory_refused", "release prefix contains a malformed key")
        if key in seen_keys:
            raise RetirementError("inventory_refused", "release object has version history")
        seen_keys.add(key)
        if entry.get("IsLatest") is not True:
            raise RetirementError("inventory_refused", "release object data version is not current")
        version_id = _bounded_string(entry.get("VersionId"), "release object VersionId")
        etag = _bounded_string(entry.get("ETag"), "release object ETag")
        modified = entry.get("LastModified")
        if not isinstance(modified, datetime):
            raise RetirementError("inventory_refused", "release object LastModified is malformed")
        size = entry.get("Size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise RetirementError("inventory_refused", "release object size is malformed")
        body = _read_exact_object(
            client,
            bucket=str(deployment["bucket"]),
            key=key,
            version_id=version_id,
            etag=etag,
            expected_size=size,
            maximum_size=MAX_RELEASE_OBJECT_BYTES,
            kind="release object",
        )
        rows.append(
            {
                "release_id": parts[0],
                "kind": filenames[parts[1]],
                "key": key,
                "version_id": version_id,
                "etag": etag,
                "last_modified": _utc_timestamp(modified),
                "size": size,
                "sha256": sha256_bytes(body),
            }
        )
    return sorted(rows, key=lambda row: (row["release_id"], row["kind"]))


def group_releases(object_rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in object_rows:
        grouped[str(row["release_id"])].append(row)
    releases: list[dict[str, Any]] = []
    for identifier, objects in sorted(grouped.items()):
        by_kind = {str(item["kind"]): item for item in objects}
        if len(objects) != 2 or set(by_kind) != {"config", "inventory"}:
            raise RetirementError("inventory_refused", "release does not contain one complete object pair")
        computed = derive_release_id(str(by_kind["config"]["sha256"]), str(by_kind["inventory"]["sha256"]))
        if computed != identifier:
            raise RetirementError("inventory_refused", "release object bytes do not match their release ID")
        created = max(_parse_timestamp(item["last_modified"], "release object LastModified") for item in objects)
        releases.append(
            {
                "release_id": identifier,
                "created_at": _utc_timestamp(created),
                "objects": [dict(by_kind["config"]), dict(by_kind["inventory"])],
            }
        )
    return releases


def inventory_manifests(
    client: Any,
    *,
    deployment: Mapping[str, Any],
    releases: Sequence[Mapping[str, Any]],
    inventory_limit: int,
    validator: Draft202012Validator,
) -> list[dict[str, Any]]:
    pointer_key = str(deployment["pointer_key"])
    versions, delete_markers = _list_all_versions(
        client,
        bucket=str(deployment["bucket"]),
        prefix=pointer_key,
        exact_key=pointer_key,
        inventory_limit=inventory_limit,
    )
    if delete_markers:
        raise RetirementError("inventory_refused", "active pointer history contains a delete marker")
    if not versions:
        raise RetirementError("inventory_refused", "active pointer history is empty")
    release_map = {str(release["release_id"]): release for release in releases}
    manifests: list[dict[str, Any]] = []
    current_count = 0
    seen_versions: set[str] = set()
    for entry in versions:
        if not isinstance(entry, dict):
            raise RetirementError("inventory_refused", "active pointer version entry is malformed")
        version_id = _bounded_string(entry.get("VersionId"), "active pointer VersionId")
        if version_id in seen_versions:
            raise RetirementError("inventory_refused", "active pointer inventory contains a duplicate version")
        seen_versions.add(version_id)
        etag = _bounded_string(entry.get("ETag"), "active pointer ETag")
        modified = entry.get("LastModified")
        size = entry.get("Size")
        if not isinstance(modified, datetime) or isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise RetirementError("inventory_refused", "active pointer inventory row is malformed")
        is_latest = entry.get("IsLatest")
        if not isinstance(is_latest, bool):
            raise RetirementError("inventory_refused", "active pointer current-version marker is malformed")
        current_count += int(is_latest)
        body = _read_exact_object(
            client,
            bucket=str(deployment["bucket"]),
            key=pointer_key,
            version_id=version_id,
            etag=etag,
            expected_size=size,
            maximum_size=MAX_POINTER_BYTES,
            kind="active pointer",
        )
        try:
            document = load_unique_json(body)
            validator.validate(document)
        except Exception as error:
            raise RetirementError("inventory_refused", "active pointer version failed schema validation") from error
        if not isinstance(document, dict):
            raise RetirementError("inventory_refused", "active pointer version must be an object")
        identifier = str(document["release_id"])
        release = release_map.get(identifier)
        if release is None:
            raise RetirementError("inventory_refused", "retained active pointer references an absent release")
        objects = {str(item["kind"]): item for item in release["objects"]}
        for kind in ("config", "inventory"):
            reference = document[kind]
            item = objects[kind]
            if (
                reference["key"] != item["key"]
                or reference["version_id"] != item["version_id"]
                or reference["sha256"] != item["sha256"]
            ):
                raise RetirementError("inventory_refused", "active pointer reference differs from release inventory")
        manifests.append(
            {
                "version_id": version_id,
                "etag": etag,
                "last_modified": _utc_timestamp(modified),
                "is_latest": is_latest,
                "release_id": identifier,
                "promoted_at": document["promoted_at"],
                "sha256": sha256_bytes(body),
            }
        )
    if current_count != 1:
        raise RetirementError("inventory_refused", "active pointer history does not have one current data version")
    return sorted(manifests, key=lambda row: (not row["is_latest"], row["version_id"]))


def classify_releases(
    releases: Sequence[Mapping[str, Any]],
    *,
    as_of: datetime,
    retention_days: int,
    minimum_retained_releases: int,
    protected_release_ids: Sequence[str],
) -> list[dict[str, Any]]:
    if as_of.tzinfo is None:
        raise RetirementError("inventory_refused", "preview as_of must be timezone-aware")
    ordered = sorted(
        releases,
        key=lambda row: (_parse_timestamp(row.get("created_at"), "release creation time"), row["release_id"]),
        reverse=True,
    )
    floor_time = None
    if len(ordered) >= minimum_retained_releases:
        floor_time = _parse_timestamp(ordered[minimum_retained_releases - 1].get("created_at"), "release creation time")
    cutoff = as_of.astimezone(UTC) - timedelta(days=retention_days)
    protected = set(protected_release_ids)
    candidates: list[dict[str, Any]] = []
    for release in releases:
        created = _parse_timestamp(release.get("created_at"), "release creation time")
        in_newest_floor = floor_time is None or created >= floor_time
        if created <= cutoff and not in_newest_floor and release["release_id"] not in protected:
            candidates.append(dict(release))
    return sorted(candidates, key=lambda row: row["release_id"])


def create_plan(
    client: Any,
    *,
    deployment: Mapping[str, Any],
    inventory_limit: int,
    explicit_protected: Sequence[str],
    as_of: datetime,
    pointer_validator: Draft202012Validator,
) -> dict[str, Any]:
    object_rows = inventory_release_objects(client, deployment=deployment, inventory_limit=inventory_limit)
    releases = group_releases(object_rows)
    manifests = inventory_manifests(
        client,
        deployment=deployment,
        releases=releases,
        inventory_limit=inventory_limit,
        validator=pointer_validator,
    )
    release_ids = {str(release["release_id"]) for release in releases}
    if any(identifier not in release_ids for identifier in explicit_protected):
        raise RetirementError("inventory_refused", "an explicitly protected release is absent from the inventory")
    manifest_protected = sorted({str(manifest["release_id"]) for manifest in manifests})
    protected = sorted(set(manifest_protected) | set(explicit_protected))
    candidates = classify_releases(
        releases,
        as_of=as_of,
        retention_days=int(deployment["retention_days"]),
        minimum_retained_releases=int(deployment["minimum_retained_releases"]),
        protected_release_ids=protected,
    )
    return {
        "plan_version": 1,
        "deployment_sha256": deployment["deployment_sha256"],
        "bucket": deployment["bucket"],
        "release_prefix": deployment["release_prefix"],
        "pointer_key": deployment["pointer_key"],
        "filenames": {
            "config": deployment["config_filename"],
            "inventory": deployment["inventory_filename"],
        },
        "as_of": _utc_timestamp(as_of),
        "policy": {
            "retention_days": deployment["retention_days"],
            "minimum_retained_releases": deployment["minimum_retained_releases"],
        },
        "inventory_limit": inventory_limit,
        "explicit_protected": list(explicit_protected),
        "manifest_protected": manifest_protected,
        "manifests": manifests,
        "release_objects": object_rows,
        "releases": releases,
        "deletion_candidates": candidates,
    }


def write_plan(path: Path, plan: Mapping[str, Any]) -> str:
    body = canonical_json(plan) + b"\n"
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_bytes(body)
        temporary.replace(path)
    except OSError as error:
        raise RetirementError("local_write_failed", "release retirement plan could not be written") from error
    return sha256_bytes(body)


def load_plan(path: Path, expected_sha256: str) -> dict[str, Any]:
    if SHA256_RE.fullmatch(expected_sha256) is None:
        raise RetirementError("stale_plan", "expected plan SHA-256 is malformed")
    try:
        body = path.read_bytes()
    except OSError as error:
        raise RetirementError("stale_plan", "saved release retirement plan cannot be read") from error
    if sha256_bytes(body) != expected_sha256:
        raise RetirementError("stale_plan", "saved plan SHA-256 differs from the expected digest")
    try:
        plan = load_unique_json(body)
    except Exception as error:
        raise RetirementError("stale_plan", "saved release retirement plan is malformed") from error
    if not isinstance(plan, dict) or canonical_json(plan) + b"\n" != body:
        raise RetirementError("stale_plan", "saved release retirement plan is not canonical JSON")
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


def _candidate_objects(candidates: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for release in candidates:
        for item in release["objects"]:
            rows.append(
                {
                    "release_id": release["release_id"],
                    "kind": item["kind"],
                    "key": item["key"],
                    "version_id": item["version_id"],
                }
            )
    return sorted(rows, key=lambda row: (row["release_id"], row["kind"]))


def _result(
    status: str,
    plan_sha256: str,
    candidate_objects: Sequence[Mapping[str, Any]],
    deleted: Sequence[Mapping[str, Any]],
    detail: str,
) -> dict[str, Any]:
    deleted_ids = {(row["key"], row["version_id"]) for row in deleted}
    return {
        "status": status,
        "plan_sha256": plan_sha256,
        "detail": detail,
        "proved_deleted": [dict(row) for row in deleted],
        "untouched": [dict(row) for row in candidate_objects if (row["key"], row["version_id"]) not in deleted_ids],
    }


def _require_plan_shape(plan: Mapping[str, Any]) -> None:
    required = {
        "plan_version",
        "deployment_sha256",
        "bucket",
        "release_prefix",
        "pointer_key",
        "filenames",
        "as_of",
        "policy",
        "inventory_limit",
        "explicit_protected",
        "manifest_protected",
        "manifests",
        "release_objects",
        "releases",
        "deletion_candidates",
    }
    if set(plan) != required or plan.get("plan_version") != 1:
        raise RetirementError("stale_plan", "saved plan shape is not supported")


def apply_plan(
    client: Any,
    *,
    deployment: Mapping[str, Any],
    plan: Mapping[str, Any],
    plan_sha256: str,
    explicit_protected: Sequence[str],
    pointer_validator: Draft202012Validator,
) -> dict[str, Any]:
    _require_plan_shape(plan)
    expected_static = {
        "deployment_sha256": deployment["deployment_sha256"],
        "bucket": deployment["bucket"],
        "release_prefix": deployment["release_prefix"],
        "pointer_key": deployment["pointer_key"],
        "filenames": {"config": deployment["config_filename"], "inventory": deployment["inventory_filename"]},
        "policy": {
            "retention_days": deployment["retention_days"],
            "minimum_retained_releases": deployment["minimum_retained_releases"],
        },
        "explicit_protected": list(explicit_protected),
    }
    if any(plan.get(name) != value for name, value in expected_static.items()):
        raise RetirementError("stale_plan", "deployment policy or protected releases differ from the preview")
    limit = _positive_integer(plan.get("inventory_limit"), "saved inventory limit", status="stale_plan")
    planned_objects = plan.get("release_objects")
    planned_releases = plan.get("releases")
    planned_candidates = plan.get("deletion_candidates")
    if (
        not isinstance(planned_objects, list)
        or not isinstance(planned_releases, list)
        or not isinstance(planned_candidates, list)
    ):
        raise RetirementError("stale_plan", "saved release inventory is malformed")
    candidate_objects = _candidate_objects(planned_candidates)
    candidate_ids = {(row["key"], row["version_id"]) for row in candidate_objects}
    fresh_objects = inventory_release_objects(client, deployment=deployment, inventory_limit=limit)
    planned_by_id = {(row["key"], row["version_id"]): row for row in planned_objects}
    fresh_by_id = {(row["key"], row["version_id"]): row for row in fresh_objects}
    missing = set(planned_by_id) - set(fresh_by_id)
    if set(fresh_by_id) - set(planned_by_id) or any(fresh_by_id[key] != planned_by_id[key] for key in fresh_by_id):
        raise RetirementError("stale_plan", "release object inventory differs from the preview")
    if any(identity not in candidate_ids for identity in missing):
        raise RetirementError("stale_plan", "a retained release object disappeared after preview")
    manifests = inventory_manifests(
        client,
        deployment=deployment,
        releases=planned_releases,
        inventory_limit=limit,
        validator=pointer_validator,
    )
    if manifests != plan.get("manifests"):
        raise RetirementError("stale_plan", "active pointer history differs from the preview")
    manifest_protected = sorted({str(item["release_id"]) for item in manifests})
    if manifest_protected != plan.get("manifest_protected"):
        raise RetirementError("stale_plan", "manifest protection differs from the preview")
    expected_candidates = classify_releases(
        planned_releases,
        as_of=_parse_timestamp(plan.get("as_of"), "as_of"),
        retention_days=int(deployment["retention_days"]),
        minimum_retained_releases=int(deployment["minimum_retained_releases"]),
        protected_release_ids=sorted(set(manifest_protected) | set(explicit_protected)),
    )
    if expected_candidates != planned_candidates:
        raise RetirementError("stale_plan", "saved deletion classification differs from the preview inputs")
    deleted = [row for row in candidate_objects if (row["key"], row["version_id"]) in missing]
    for candidate in candidate_objects:
        identity = (candidate["key"], candidate["version_id"])
        if identity in missing:
            continue
        row = planned_by_id[identity]
        delete_error: Exception | None = None
        refused = False
        try:
            client.delete_object(
                Bucket=plan["bucket"], Key=row["key"], VersionId=row["version_id"], IfMatch=row["etag"]
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
            return _result("ambiguous", plan_sha256, candidate_objects, deleted, "exact-version status is unreadable")
        if refused:
            return _result(
                "delete_refused",
                plan_sha256,
                candidate_objects,
                deleted,
                "conditional exact-version delete was refused",
            )
        if delete_error is not None:
            return _result(
                "delete_failed",
                plan_sha256,
                candidate_objects,
                deleted,
                "provider failure left the exact version present",
            )
        return _result(
            "delete_failed", plan_sha256, candidate_objects, deleted, "exact version remained after the delete response"
        )
    try:
        final_objects = inventory_release_objects(client, deployment=deployment, inventory_limit=limit)
    except RetirementError:
        return _result(
            "applied_unverified",
            plan_sha256,
            candidate_objects,
            deleted,
            "final complete inventory could not be proved",
        )
    deleted_ids = {(row["key"], row["version_id"]) for row in deleted}
    expected_remaining = [row for row in planned_objects if (row["key"], row["version_id"]) not in deleted_ids]
    if final_objects != expected_remaining:
        return _result(
            "applied_unverified",
            plan_sha256,
            candidate_objects,
            deleted,
            "final inventory differs from the proved retained set",
        )
    return _result("applied", plan_sha256, candidate_objects, deleted, "all planned release deletions were proved")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preview or apply exact immutable-release retirement.")
    parser.add_argument("action", choices=("preview", "apply"))
    parser.add_argument("--deployment", type=Path, required=True)
    parser.add_argument("--schema", type=Path, default=ROOT / "schemas/deployment.schema.json")
    parser.add_argument("--pointer-schema", type=Path, default=ROOT / "schemas/active-versions.schema.json")
    parser.add_argument("--plan", type=Path, required=True)
    protection = parser.add_mutually_exclusive_group(required=True)
    protection.add_argument("--protected-release", action="append", metavar="RELEASE_ID")
    protection.add_argument("--no-additional-protected-releases", action="store_true")
    parser.add_argument("--inventory-limit", type=int, required=True)
    parser.add_argument("--expected-plan-sha256")
    return parser.parse_args(argv)


def _write(document: Mapping[str, Any], *, stream: Any = sys.stdout) -> None:
    print(canonical_json(document).decode("utf-8"), file=stream)


def main(argv: list[str] | None = None, *, client: Any | None = None, now: datetime | None = None) -> int:
    arguments = parse_args(argv)
    try:
        deployment = load_deployment(arguments.deployment, arguments.schema)
        pointer_validator = load_pointer_validator(arguments.pointer_schema)
        protected = parse_protected(arguments.protected_release, arguments.no_additional_protected_releases)
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
                explicit_protected=protected,
                as_of=now or datetime.now(UTC),
                pointer_validator=pointer_validator,
            )
            digest = write_plan(arguments.plan, plan)
            _write(
                {
                    "status": "previewed",
                    "plan_sha256": digest,
                    "release_count": len(plan["releases"]),
                    "manifest_count": len(plan["manifests"]),
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
            explicit_protected=protected,
            pointer_validator=pointer_validator,
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
            else (EXIT_INVALID if error.status in {"inventory_refused", "inventory_limit_exceeded"} else EXIT_REFUSED)
        )


if __name__ == "__main__":
    raise SystemExit(main())
