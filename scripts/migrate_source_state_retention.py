#!/usr/bin/env python3
"""Preview and apply the one-time ADR-025 source-state TTL migration."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aws_public_change_feed.loading import load_release_version  # noqa: E402
from aws_public_change_feed.parsing import load_unique_json  # noqa: E402
from aws_public_change_feed.releases import S3ObjectStore  # noqa: E402

PLAN_VERSION = 1
EXIT_INVALID = 2
EXIT_REFUSED = 3
EXIT_AMBIGUOUS = 4
MAX_RETENTION_DAYS = 3650
MAX_INVENTORY_LIMIT = 1_000
MAX_IDENTIFIER_CHARACTERS = 2048
SHA256_RE = re.compile(r"[a-f0-9]{64}")
ANNOUNCEMENT_PK_RE = re.compile(r"ANNOUNCEMENT#[a-f0-9]{64}")
RUN_PK_RE = re.compile(r"RUN#[a-f0-9]{64}")
PAGE_SK_RE = re.compile(r"PAGESET#[a-f0-9]{64}#PAGE#[0-9]{6}")

PROJECTION_NAMES = {
    "#pk": "PK",
    "#sk": "SK",
    "#type": "item_type",
    "#expires": "expires_at",
    "#observed": "last_observed_at",
    "#version": "state_version",
    "#run": "run_id",
    "#page_set": "page_set_id",
    "#feed": "feed_name",
    "#page": "page",
    "#candidates": "candidate_ids",
    "#complete": "complete",
}
PROJECTION_EXPRESSION = ",".join(PROJECTION_NAMES)


class MigrationError(RuntimeError):
    """One bounded operator-facing refusal without provider response text."""

    def __init__(self, status: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(frozen=True, slots=True)
class MigrationContext:
    account_id: str
    region: str
    table_name: str
    role_arn: str
    role_session_arn: str
    bucket: str
    pointer_key: str
    pointer_version_id: str
    pointer_etag: str
    release_id: str
    application_version: str
    config_reference: Mapping[str, Any]
    announcement_ttl_days: int
    feed_state_ttl_days: int
    deployment_sha256: str
    terraform_output_sha256: str

    def plan_document(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "region": self.region,
            "table_name": self.table_name,
            "role_arn": self.role_arn,
            "role_session_arn": self.role_session_arn,
            "bucket": self.bucket,
            "pointer_key": self.pointer_key,
            "pointer_version_id": self.pointer_version_id,
            "pointer_etag": self.pointer_etag,
            "release_id": self.release_id,
            "application_version": self.application_version,
            "config_reference": dict(self.config_reference),
            "retention": {
                "announcement_state_ttl_days": self.announcement_ttl_days,
                "feed_state_ttl_days": self.feed_state_ttl_days,
            },
            "deployment_sha256": self.deployment_sha256,
            "terraform_output_sha256": self.terraform_output_sha256,
        }


def canonical_json(document: Mapping[str, Any]) -> bytes:
    return json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _utc_timestamp(value: datetime, *, status: str = "inventory_refused") -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise MigrationError(status, "migration time must include a UTC offset")
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_timestamp(value: object, field: str, *, status: str = "inventory_refused") -> datetime:
    if not isinstance(value, str):
        raise MigrationError(status, f"{field} is malformed")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise MigrationError(status, f"{field} is malformed") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MigrationError(status, f"{field} must include a UTC offset")
    return parsed.astimezone(UTC)


def _bounded_string(value: object, field: str, *, status: str = "inventory_refused") -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= MAX_IDENTIFIER_CHARACTERS
        or any(character in value for character in "\r\n\x00")
    ):
        raise MigrationError(status, f"{field} must be a bounded single-line string")
    return value


def _positive_integer(value: object, field: str, *, maximum: int, status: str = "inventory_refused") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise MigrationError(status, f"{field} must be an integer from 1 through {maximum}")
    return value


def _wire(value: object) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"BOOL": value}
    if isinstance(value, int):
        return {"N": str(value)}
    if isinstance(value, str):
        return {"S": value}
    if isinstance(value, list):
        return {"L": [_wire(entry) for entry in value]}
    raise TypeError(f"unsupported DynamoDB value type: {type(value).__name__}")


def _unwire(value: object, field: str) -> object:
    if not isinstance(value, Mapping) or len(value) != 1:
        raise MigrationError("inventory_refused", f"{field} has a malformed DynamoDB value")
    if "S" in value and isinstance(value["S"], str):
        return value["S"]
    if "N" in value and isinstance(value["N"], str) and re.fullmatch(r"0|[1-9][0-9]*", value["N"]):
        return int(value["N"])
    if "BOOL" in value and isinstance(value["BOOL"], bool):
        return value["BOOL"]
    if "L" in value and isinstance(value["L"], Sequence):
        return [_unwire(entry, f"{field} entry") for entry in value["L"]]
    raise MigrationError("inventory_refused", f"{field} has an unsupported DynamoDB value")


def _document(item: object) -> dict[str, object]:
    if not isinstance(item, Mapping):
        raise MigrationError("inventory_refused", "inventory item is not an object")
    document: dict[str, object] = {}
    for name, value in item.items():
        if not isinstance(name, str):
            raise MigrationError("inventory_refused", "inventory item has a malformed attribute name")
        document[name] = _unwire(value, name)
    return document


def _optional_expiry(document: Mapping[str, object]) -> int | None:
    value = document.get("expires_at")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MigrationError("inventory_refused", "expires_at must be a non-negative whole Unix timestamp")
    return value


def _announcement_row(document: Mapping[str, object]) -> dict[str, Any]:
    pk = document.get("PK")
    sk = document.get("SK")
    if not isinstance(pk, str) or ANNOUNCEMENT_PK_RE.fullmatch(pk) is None or sk != "STATE":
        raise MigrationError("inventory_refused", "announcement key is malformed")
    if document.get("item_type") != "announcement":
        raise MigrationError("inventory_refused", "announcement item_type is malformed")
    observed = _bounded_string(document.get("last_observed_at"), "announcement last_observed_at")
    _parse_timestamp(observed, "announcement last_observed_at")
    version = _positive_integer(document.get("state_version"), "announcement state_version", maximum=2**63 - 1)
    return {
        "kind": "announcement",
        "PK": pk,
        "SK": sk,
        "last_observed_at": observed,
        "state_version": version,
        "expires_at": _optional_expiry(document),
    }


def _response_page_row(document: Mapping[str, object]) -> dict[str, Any]:
    pk = document.get("PK")
    sk = document.get("SK")
    run_id = document.get("run_id")
    page_set_id = document.get("page_set_id")
    page = document.get("page")
    if not isinstance(pk, str) or RUN_PK_RE.fullmatch(pk) is None:
        raise MigrationError("inventory_refused", "response-page partition key is malformed")
    if not isinstance(sk, str) or PAGE_SK_RE.fullmatch(sk) is None:
        raise MigrationError("inventory_refused", "response-page sort key is malformed")
    if document.get("item_type") != "response_page":
        raise MigrationError("inventory_refused", "response-page item_type is malformed")
    if not isinstance(run_id, str) or not SHA256_RE.fullmatch(run_id) or pk != f"RUN#{run_id}":
        raise MigrationError("inventory_refused", "response-page run identity is malformed")
    if not isinstance(page_set_id, str) or not SHA256_RE.fullmatch(page_set_id):
        raise MigrationError("inventory_refused", "response-page page-set identity is malformed")
    if isinstance(page, bool) or not isinstance(page, int) or not 0 <= page <= 999_999:
        raise MigrationError("inventory_refused", "response-page number is malformed")
    if sk != f"PAGESET#{page_set_id}#PAGE#{page:06d}":
        raise MigrationError("inventory_refused", "response-page key disagrees with its proof")
    feed_name = _bounded_string(document.get("feed_name"), "response-page feed_name")
    candidate_ids = document.get("candidate_ids")
    if not isinstance(candidate_ids, list) or len(candidate_ids) > 25:
        raise MigrationError("inventory_refused", "response-page candidate_ids are malformed")
    if any(not isinstance(value, str) or SHA256_RE.fullmatch(value) is None for value in candidate_ids):
        raise MigrationError("inventory_refused", "response-page candidate_ids are malformed")
    if candidate_ids != sorted(set(candidate_ids)):
        raise MigrationError("inventory_refused", "response-page candidate_ids are not sorted and unique")
    complete = document.get("complete")
    if not isinstance(complete, bool) or not complete:
        raise MigrationError("inventory_refused", "response-page completion proof is malformed")
    return {
        "kind": "response_page",
        "PK": pk,
        "SK": sk,
        "run_id": run_id,
        "page_set_id": page_set_id,
        "feed_name": feed_name,
        "page": page,
        "candidate_ids": candidate_ids,
        "complete": complete,
        "expires_at": _optional_expiry(document),
    }


def _inventory_row(item: object) -> dict[str, Any]:
    document = _document(item)
    kind = document.get("item_type")
    if kind == "announcement":
        return _announcement_row(document)
    if kind == "response_page":
        return _response_page_row(document)
    raise MigrationError("inventory_refused", "projected inventory returned an unexpected item type")


def inventory_source_state(client: Any, *, table_name: str, inventory_limit: int) -> tuple[list[dict[str, Any]], int]:
    """Read one complete, projected, strongly consistent, bounded inventory."""

    _bounded_string(table_name, "source-state table")
    limit = _positive_integer(inventory_limit, "inventory limit", maximum=MAX_INVENTORY_LIMIT)
    rows: list[dict[str, Any]] = []
    scanned = 0
    start_key: Mapping[str, Any] | None = None
    seen_keys: set[str] = set()
    while True:
        remaining = limit - scanned
        if remaining < 1:
            raise MigrationError("inventory_refused", "source-state inventory reached its limit before completion")
        arguments: dict[str, Any] = {
            "TableName": table_name,
            "ConsistentRead": True,
            "Select": "SPECIFIC_ATTRIBUTES",
            "ProjectionExpression": PROJECTION_EXPRESSION,
            "ExpressionAttributeNames": PROJECTION_NAMES,
            "FilterExpression": "begins_with(#pk, :announcement) OR begins_with(#pk, :run)",
            "ExpressionAttributeValues": {
                ":announcement": {"S": "ANNOUNCEMENT#"},
                ":run": {"S": "RUN#"},
            },
            "Limit": remaining + 1,
        }
        if start_key is not None:
            arguments["ExclusiveStartKey"] = start_key
        try:
            response = client.scan(**arguments)
        except Exception as error:
            raise MigrationError("inventory_refused", "source-state inventory scan failed") from error
        count = response.get("ScannedCount")
        items = response.get("Items")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0 or not isinstance(items, list):
            raise MigrationError("inventory_refused", "source-state inventory response is malformed")
        scanned += count
        if scanned > limit:
            raise MigrationError("inventory_refused", "source-state inventory exceeds its reviewed limit")
        rows.extend(_inventory_row(item) for item in items)
        next_key = response.get("LastEvaluatedKey")
        if not next_key:
            break
        marker = canonical_json(next_key).decode("utf-8") if isinstance(next_key, Mapping) else ""
        if not marker or marker in seen_keys:
            raise MigrationError("inventory_refused", "source-state inventory pagination is malformed")
        if scanned >= limit:
            raise MigrationError("inventory_refused", "source-state inventory reached its limit before completion")
        seen_keys.add(marker)
        start_key = next_key
    rows.sort(key=lambda row: (row["PK"], row["SK"]))
    identities = [(row["PK"], row["SK"]) for row in rows]
    if len(set(identities)) != len(identities):
        raise MigrationError("inventory_refused", "source-state inventory contains duplicate keys")
    return rows, scanned


def classify_updates(
    inventory: Sequence[Mapping[str, Any]],
    *,
    migration_as_of: datetime,
    announcement_ttl_days: int,
    feed_state_ttl_days: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    announcement_days = _positive_integer(announcement_ttl_days, "announcement retention", maximum=MAX_RETENTION_DAYS)
    page_days = _positive_integer(feed_state_ttl_days, "feed-state retention", maximum=MAX_RETENTION_DAYS)
    migration_time = _parse_timestamp(_utc_timestamp(migration_as_of), "migration_as_of")
    migration_epoch = int(migration_time.timestamp())
    page_expiry = int((migration_time + timedelta(days=page_days)).timestamp())
    updates: list[dict[str, Any]] = []
    eligible: list[dict[str, Any]] = []
    for row in inventory:
        if row.get("expires_at") is not None:
            continue
        if row.get("kind") == "announcement":
            observed = _parse_timestamp(row.get("last_observed_at"), "announcement last_observed_at")
            target = int((observed + timedelta(days=announcement_days)).timestamp())
            update = {
                "kind": "announcement",
                "PK": row["PK"],
                "SK": row["SK"],
                "expected_last_observed_at": row["last_observed_at"],
                "expected_state_version": row["state_version"],
                "target_state_version": int(row["state_version"]) + 1,
                "target_expires_at": target,
            }
            updates.append(update)
            if target <= migration_epoch:
                eligible.append({"PK": row["PK"], "SK": row["SK"], "target_expires_at": target})
        elif row.get("kind") == "response_page":
            updates.append(
                {
                    "kind": "response_page",
                    "PK": row["PK"],
                    "SK": row["SK"],
                    "run_id": row["run_id"],
                    "page_set_id": row["page_set_id"],
                    "feed_name": row["feed_name"],
                    "page": row["page"],
                    "candidate_ids": list(row["candidate_ids"]),
                    "complete": row["complete"],
                    "target_expires_at": page_expiry,
                }
            )
        else:
            raise MigrationError("inventory_refused", "source-state inventory contains an unsupported item class")
    updates.sort(key=lambda row: (row["PK"], row["SK"]))
    eligible.sort(key=lambda row: (row["PK"], row["SK"]))
    return updates, eligible


def create_plan(
    client: Any,
    *,
    context: MigrationContext,
    inventory_limit: int,
    migration_as_of: datetime,
) -> dict[str, Any]:
    inventory, scanned = inventory_source_state(client, table_name=context.table_name, inventory_limit=inventory_limit)
    updates, eligible = classify_updates(
        inventory,
        migration_as_of=migration_as_of,
        announcement_ttl_days=context.announcement_ttl_days,
        feed_state_ttl_days=context.feed_state_ttl_days,
    )
    announcement_count = sum(row["kind"] == "announcement" for row in inventory)
    page_count = sum(row["kind"] == "response_page" for row in inventory)
    return {
        "plan_version": PLAN_VERSION,
        "migration_as_of": _utc_timestamp(migration_as_of),
        "context": context.plan_document(),
        "inventory_limit": inventory_limit,
        "inventory_scanned_count": scanned,
        "inventory": inventory,
        "updates": updates,
        "already_eligible_announcements": eligible,
        "counts": {
            "announcements": announcement_count,
            "response_pages": page_count,
            "legacy_announcements": sum(
                row["kind"] == "announcement" and row["expires_at"] is None for row in inventory
            ),
            "legacy_response_pages": sum(
                row["kind"] == "response_page" and row["expires_at"] is None for row in inventory
            ),
            "already_eligible_announcements": len(eligible),
        },
    }


def write_plan(path: Path, plan: Mapping[str, Any]) -> str:
    body = canonical_json(plan) + b"\n"
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_bytes(body)
        temporary.replace(path)
    except OSError as error:
        raise MigrationError("local_write_failed", "migration plan could not be written") from error
    return sha256_bytes(body)


def load_plan(path: Path, expected_sha256: str) -> dict[str, Any]:
    if SHA256_RE.fullmatch(expected_sha256) is None:
        raise MigrationError("stale_plan", "expected plan SHA-256 is malformed")
    try:
        body = path.read_bytes()
    except OSError as error:
        raise MigrationError("stale_plan", "saved migration plan cannot be read") from error
    if sha256_bytes(body) != expected_sha256:
        raise MigrationError("stale_plan", "saved migration plan SHA-256 differs from the expected digest")
    try:
        plan = load_unique_json(body)
    except Exception as error:
        raise MigrationError("stale_plan", "saved migration plan is malformed") from error
    if not isinstance(plan, dict) or canonical_json(plan) + b"\n" != body:
        raise MigrationError("stale_plan", "saved migration plan is not canonical JSON")
    return plan


def _conditional_failure(error: Exception) -> bool:
    response = getattr(error, "response", {})
    return isinstance(response, Mapping) and response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def _update_arguments(table_name: str, update: Mapping[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {
        ":type": _wire(update["kind"]),
        ":expires": _wire(update["target_expires_at"]),
    }
    if update["kind"] == "announcement":
        names = {
            "#type": "item_type",
            "#observed": "last_observed_at",
            "#version": "state_version",
            "#expires": "expires_at",
        }
        values.update(
            {
                ":observed": _wire(update["expected_last_observed_at"]),
                ":version": _wire(update["expected_state_version"]),
                ":next_version": _wire(update["target_state_version"]),
            }
        )
        condition = "#type = :type AND #observed = :observed AND #version = :version AND attribute_not_exists(#expires)"
        expression = "SET #expires = :expires, #version = :next_version"
    elif update["kind"] == "response_page":
        names = {
            "#type": "item_type",
            "#run": "run_id",
            "#page_set": "page_set_id",
            "#feed": "feed_name",
            "#page": "page",
            "#candidates": "candidate_ids",
            "#complete": "complete",
            "#expires": "expires_at",
        }
        values.update(
            {
                ":run": _wire(update["run_id"]),
                ":page_set": _wire(update["page_set_id"]),
                ":feed": _wire(update["feed_name"]),
                ":page": _wire(update["page"]),
                ":candidates": _wire(update["candidate_ids"]),
                ":complete": _wire(update["complete"]),
            }
        )
        condition = (
            "#type = :type AND #run = :run AND #page_set = :page_set AND #feed = :feed "
            "AND #page = :page AND #candidates = :candidates AND #complete = :complete "
            "AND attribute_not_exists(#expires)"
        )
        expression = "SET #expires = :expires"
    else:
        raise MigrationError("stale_plan", "saved update has an unsupported item class")
    return {
        "TableName": table_name,
        "Key": {"PK": _wire(update["PK"]), "SK": _wire(update["SK"])},
        "UpdateExpression": expression,
        "ConditionExpression": condition,
        "ExpressionAttributeNames": names,
        "ExpressionAttributeValues": values,
        "ReturnValues": "NONE",
    }


def _strong_read(client: Any, *, table_name: str, update: Mapping[str, Any]) -> dict[str, Any] | None:
    try:
        response = client.get_item(
            TableName=table_name,
            Key={"PK": _wire(update["PK"]), "SK": _wire(update["SK"])},
            ConsistentRead=True,
            ProjectionExpression=PROJECTION_EXPRESSION,
            ExpressionAttributeNames=PROJECTION_NAMES,
        )
    except Exception as error:
        raise MigrationError("ambiguous", "strong read after the conditional write failed") from error
    item = response.get("Item")
    if item is None:
        return None
    return _inventory_row(item)


def _target_satisfied(row: Mapping[str, Any] | None, update: Mapping[str, Any]) -> bool:
    if row is None or row.get("kind") != update.get("kind"):
        return False
    if row.get("PK") != update.get("PK") or row.get("SK") != update.get("SK"):
        return False
    expiry = row.get("expires_at")
    if isinstance(expiry, bool) or not isinstance(expiry, int) or expiry < int(update["target_expires_at"]):
        return False
    if update["kind"] == "announcement":
        version = row.get("state_version")
        return (
            isinstance(version, int)
            and not isinstance(version, bool)
            and version >= int(update["target_state_version"])
            and _parse_timestamp(row.get("last_observed_at"), "announcement last_observed_at")
            >= _parse_timestamp(update.get("expected_last_observed_at"), "expected announcement last_observed_at")
        )
    return all(
        row.get(name) == update.get(name)
        for name in (
            "run_id",
            "page_set_id",
            "feed_name",
            "page",
            "candidate_ids",
            "complete",
        )
    )


def _identity(update: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "kind": update["kind"],
        "PK": update["PK"],
        "SK": update["SK"],
        "target_expires_at": update["target_expires_at"],
    }


def _partial_result(
    status: str,
    *,
    plan_sha256: str,
    updates: Sequence[Mapping[str, Any]],
    completed: Sequence[Mapping[str, Any]],
    detail: str,
    stopped_at: Mapping[str, Any] | None = None,
    ttl_deletion_observed: Sequence[Mapping[str, Any]] = (),
    eligible_count: int,
) -> dict[str, Any]:
    completed_keys = {(row["PK"], row["SK"]) for row in completed}
    return {
        "status": status,
        "plan_sha256": plan_sha256,
        "detail": detail,
        "completed": [dict(row) for row in completed],
        "untouched": [_identity(row) for row in updates if (row["PK"], row["SK"]) not in completed_keys],
        "stopped_at": None if stopped_at is None else _identity(stopped_at),
        "ttl_eligible_at_migration_count": eligible_count,
        "ttl_deletion_observed": [dict(row) for row in ttl_deletion_observed],
    }


def _validate_plan_shape(plan: Mapping[str, Any], context: MigrationContext) -> None:
    required = {
        "plan_version",
        "migration_as_of",
        "context",
        "inventory_limit",
        "inventory_scanned_count",
        "inventory",
        "updates",
        "already_eligible_announcements",
        "counts",
    }
    if set(plan) != required or plan.get("plan_version") != PLAN_VERSION:
        raise MigrationError("stale_plan", "saved migration plan shape is not supported")
    if plan.get("context") != context.plan_document():
        raise MigrationError("stale_plan", "deployment, role, or active release differs from the preview")
    _positive_integer(
        plan.get("inventory_limit"), "saved inventory limit", maximum=MAX_INVENTORY_LIMIT, status="stale_plan"
    )
    scanned = plan.get("inventory_scanned_count")
    if isinstance(scanned, bool) or not isinstance(scanned, int) or scanned < 0:
        raise MigrationError("stale_plan", "saved inventory count is malformed")
    if not isinstance(plan.get("inventory"), list) or not isinstance(plan.get("updates"), list):
        raise MigrationError("stale_plan", "saved migration inventory is malformed")
    if not isinstance(plan.get("already_eligible_announcements"), list) or not isinstance(plan.get("counts"), Mapping):
        raise MigrationError("stale_plan", "saved migration classification is malformed")


def apply_plan(
    client: Any,
    *,
    context: MigrationContext,
    plan: Mapping[str, Any],
    plan_sha256: str,
    context_verifier: Callable[[], None] | None = None,
) -> dict[str, Any]:
    _validate_plan_shape(plan, context)
    migration_as_of = _parse_timestamp(plan["migration_as_of"], "migration_as_of", status="stale_plan")
    limit = int(plan["inventory_limit"])
    try:
        fresh, scanned = inventory_source_state(client, table_name=context.table_name, inventory_limit=limit)
    except MigrationError as error:
        raise MigrationError("stale_plan", "source-state inventory can no longer be proved") from error
    if fresh != plan["inventory"] or scanned != plan["inventory_scanned_count"]:
        raise MigrationError("stale_plan", "source-state inventory differs from the preview")
    updates, eligible = classify_updates(
        fresh,
        migration_as_of=migration_as_of,
        announcement_ttl_days=context.announcement_ttl_days,
        feed_state_ttl_days=context.feed_state_ttl_days,
    )
    if updates != plan["updates"] or eligible != plan["already_eligible_announcements"]:
        raise MigrationError("stale_plan", "saved migration classification differs from the preview")
    if context_verifier is not None:
        context_verifier()

    completed: list[dict[str, Any]] = []
    deletion_observed: list[dict[str, Any]] = []
    migration_epoch = int(migration_as_of.timestamp())
    for update in updates:
        write_error: Exception | None = None
        try:
            client.update_item(**_update_arguments(context.table_name, update))
        except Exception as error:
            write_error = error
        try:
            durable = _strong_read(client, table_name=context.table_name, update=update)
        except MigrationError:
            return _partial_result(
                "ambiguous",
                plan_sha256=plan_sha256,
                updates=updates,
                completed=completed,
                detail="the conditional write outcome could not be strongly reread",
                stopped_at=update,
                ttl_deletion_observed=deletion_observed,
                eligible_count=len(eligible),
            )
        if durable is None:
            if (
                write_error is None
                and update["kind"] == "announcement"
                and update["target_expires_at"] <= migration_epoch
            ):
                identity = _identity(update)
                completed.append(identity)
                deletion_observed.append(identity)
                continue
            return _partial_result(
                "ambiguous",
                plan_sha256=plan_sha256,
                updates=updates,
                completed=completed,
                detail="the updated item is absent and the write outcome is not provable",
                stopped_at=update,
                ttl_deletion_observed=deletion_observed,
                eligible_count=len(eligible),
            )
        if write_error is None and _target_satisfied(durable, update):
            completed.append(_identity(update))
            continue
        if write_error is not None and _conditional_failure(write_error):
            return _partial_result(
                "conflict",
                plan_sha256=plan_sha256,
                updates=updates,
                completed=completed,
                detail="a conditional migration write was refused",
                stopped_at=update,
                ttl_deletion_observed=deletion_observed,
                eligible_count=len(eligible),
            )
        if write_error is not None and _target_satisfied(durable, update):
            completed.append(_identity(update))
            continue
        return _partial_result(
            "write_failed",
            plan_sha256=plan_sha256,
            updates=updates,
            completed=completed,
            detail="the migration target was not present after the write attempt",
            stopped_at=update,
            ttl_deletion_observed=deletion_observed,
            eligible_count=len(eligible),
        )

    try:
        final_inventory, final_scanned = inventory_source_state(
            client, table_name=context.table_name, inventory_limit=limit
        )
    except MigrationError:
        return _partial_result(
            "applied_unverified",
            plan_sha256=plan_sha256,
            updates=updates,
            completed=completed,
            detail="the final bounded inventory could not be proved",
            ttl_deletion_observed=deletion_observed,
            eligible_count=len(eligible),
        )
    final_by_key = {(row["PK"], row["SK"]): row for row in final_inventory}
    for update in updates:
        row = final_by_key.get((update["PK"], update["SK"]))
        if row is None and update["kind"] == "announcement" and update["target_expires_at"] <= migration_epoch:
            identity = _identity(update)
            if identity not in deletion_observed:
                deletion_observed.append(identity)
            continue
        if not _target_satisfied(row, update):
            return _partial_result(
                "applied_unverified",
                plan_sha256=plan_sha256,
                updates=updates,
                completed=completed,
                detail="the final inventory does not prove every planned retention write",
                stopped_at=update,
                ttl_deletion_observed=deletion_observed,
                eligible_count=len(eligible),
            )
    legacy_remainder = [row for row in final_inventory if row.get("expires_at") is None]
    if legacy_remainder:
        return _partial_result(
            "applied_unverified",
            plan_sha256=plan_sha256,
            updates=updates,
            completed=completed,
            detail="the final inventory still contains legacy retention metadata",
            ttl_deletion_observed=deletion_observed,
            eligible_count=len(eligible),
        )
    expired_present = sum(
        isinstance(row.get("expires_at"), int) and int(row["expires_at"]) <= migration_epoch for row in final_inventory
    )
    return {
        "status": "applied",
        "plan_sha256": plan_sha256,
        "detail": "all planned retention writes and the final bounded inventory were proved",
        "completed": completed,
        "untouched": [],
        "stopped_at": None,
        "ttl_eligible_at_migration_count": len(eligible),
        "ttl_deletion_observed": deletion_observed,
        "post_apply": {
            "inventory_scanned_count": final_scanned,
            "announcement_count": sum(row["kind"] == "announcement" for row in final_inventory),
            "response_page_count": sum(row["kind"] == "response_page" for row in final_inventory),
            "expired_items_still_present": expired_present,
            "legacy_remainder_count": 0,
        },
    }


def _read_mapping(path: Path, kind: str) -> tuple[Mapping[str, Any], bytes]:
    try:
        body = path.read_bytes()
        document = yaml.safe_load(body) if path.suffix in {".yaml", ".yml"} else load_unique_json(body)
    except Exception as error:
        raise MigrationError("invalid_input", f"{kind} document is malformed") from error
    if not isinstance(document, Mapping):
        raise MigrationError("invalid_input", f"{kind} document must be an object")
    return document, body


def _validate_deployment(path: Path, document: Mapping[str, Any]) -> None:
    schema_path = ROOT / "schemas/deployment.schema.json"
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(document)
    except Exception as error:
        raise MigrationError("invalid_input", f"deployment document failed validation: {path.name}") from error


def _output_value(outputs: Mapping[str, Any], name: str) -> Any:
    output = outputs.get(name)
    if not isinstance(output, Mapping) or set(output) != {"sensitive", "type", "value"}:
        raise MigrationError("invalid_input", f"Terraform output {name} is missing or malformed")
    return output["value"]


def _read_pointer(s3_client: Any, *, bucket: str, key: str) -> dict[str, Any]:
    try:
        response = s3_client.get_object(Bucket=bucket, Key=key)
        body = response["Body"].read()
        pointer = load_unique_json(body)
    except Exception as error:
        raise MigrationError("identity_refused", "active release pointer could not be read") from error
    version_id = response.get("VersionId")
    etag = response.get("ETag")
    if not isinstance(pointer, Mapping) or not isinstance(version_id, str) or not isinstance(etag, str):
        raise MigrationError("identity_refused", "active release pointer response is malformed")
    return {"document": pointer, "version_id": version_id, "etag": etag}


def verify_pointer_current(s3_client: Any, context: MigrationContext) -> None:
    try:
        pointer = _read_pointer(s3_client, bucket=context.bucket, key=context.pointer_key)
    except MigrationError as error:
        raise MigrationError("stale_plan", "active release pointer can no longer be proved") from error
    if pointer["version_id"] != context.pointer_version_id or pointer["etag"] != context.pointer_etag:
        raise MigrationError("stale_plan", "active release pointer differs from the preview")


def load_context(
    *,
    deployment_path: Path,
    terraform_output_path: Path,
    expected_account: str,
    sts_client: Any,
    s3_client: Any,
    ddb_client_factory: Any,
) -> tuple[MigrationContext, Any]:
    if not re.fullmatch(r"[0-9]{12}", expected_account):
        raise MigrationError("invalid_input", "expected account must be 12 digits")
    deployment, deployment_body = _read_mapping(deployment_path, "deployment")
    outputs, output_body = _read_mapping(terraform_output_path, "Terraform output")
    _validate_deployment(deployment_path, deployment)
    try:
        identity = sts_client.get_caller_identity()
    except Exception as error:
        raise MigrationError("identity_refused", "caller identity could not be read") from error
    if identity.get("Account") != expected_account:
        raise MigrationError("identity_refused", "caller account differs from the reviewed account")
    deployment_id = _bounded_string(deployment.get("deployment_id"), "deployment ID", status="invalid_input")
    region = _bounded_string(deployment.get("deployment_region"), "deployment Region", status="invalid_input")
    bucket = _bounded_string(
        _output_value(outputs, "config_bucket_name"), "configuration bucket", status="invalid_input"
    )
    if bucket != deployment.get("config_bucket_name"):
        raise MigrationError("identity_refused", "Terraform configuration bucket differs from deployment input")
    table_name = _bounded_string(
        _output_value(outputs, "source_state_table"), "source-state table", status="invalid_input"
    )
    if table_name != f"apcf-source-state-{deployment_id}":
        raise MigrationError("identity_refused", "Terraform source-state table differs from deployment identity")
    roles = _output_value(outputs, "roles")
    if not isinstance(roles, Mapping):
        raise MigrationError("invalid_input", "Terraform roles output is malformed")
    role_arn = _bounded_string(
        roles.get("source_state_retention_migration"), "migration role ARN", status="invalid_input"
    )
    expected_role = f"arn:aws:iam::{expected_account}:role/apcf-{deployment_id}-source-state-retention-migration"
    if role_arn != expected_role:
        raise MigrationError(
            "identity_refused", "Terraform migration role differs from the reviewed account and deployment"
        )
    application_version = _bounded_string(
        _output_value(outputs, "watcher_application_version"), "watcher application version", status="invalid_input"
    )
    pointer_key = _bounded_string(
        deployment.get("active_versions_object_key"), "active pointer key", status="invalid_input"
    )
    pointer = _read_pointer(s3_client, bucket=bucket, key=pointer_key)
    try:
        release = load_release_version(
            S3ObjectStore(s3_client, bucket),
            pointer_key=pointer_key,
            version_id=pointer["version_id"],
            application_version=application_version,
        )
    except Exception as error:
        raise MigrationError("identity_refused", "active release failed exact compatibility verification") from error
    retention = release.config.get("state_retention")
    if not isinstance(retention, Mapping):
        raise MigrationError("identity_refused", "active release state retention is malformed")
    announcement_days = _positive_integer(
        retention.get("announcement_state_ttl_days"),
        "announcement retention",
        maximum=MAX_RETENTION_DAYS,
        status="identity_refused",
    )
    feed_days = _positive_integer(
        retention.get("feed_state_ttl_days"),
        "feed-state retention",
        maximum=MAX_RETENTION_DAYS,
        status="identity_refused",
    )
    reference = release.reference.get("config")
    if not isinstance(reference, Mapping):
        raise MigrationError("identity_refused", "active configuration reference is malformed")
    try:
        assumed = sts_client.assume_role(
            RoleArn=role_arn,
            RoleSessionName="apcf-source-state-retention-migration",
            DurationSeconds=3600,
        )
    except Exception as error:
        raise MigrationError("identity_refused", "migration role assumption failed") from error
    credentials = assumed.get("Credentials")
    assumed_identity = assumed.get("AssumedRoleUser")
    if not isinstance(credentials, Mapping) or not isinstance(assumed_identity, Mapping):
        raise MigrationError("identity_refused", "migration role assumption response is malformed")
    role_session_arn = _bounded_string(assumed_identity.get("Arn"), "migration role session ARN")
    if not role_session_arn.startswith(
        f"arn:aws:sts::{expected_account}:assumed-role/apcf-{deployment_id}-source-state-retention-migration/"
    ):
        raise MigrationError("identity_refused", "migration role session differs from the reviewed role")
    try:
        ddb_client = ddb_client_factory(
            region_name=region,
            aws_access_key_id=credentials["AccessKeyId"],
            aws_secret_access_key=credentials["SecretAccessKey"],
            aws_session_token=credentials["SessionToken"],
        )
    except Exception as error:
        raise MigrationError("identity_refused", "migration DynamoDB client could not be created") from error
    return (
        MigrationContext(
            account_id=expected_account,
            region=region,
            table_name=table_name,
            role_arn=role_arn,
            role_session_arn=role_session_arn,
            bucket=bucket,
            pointer_key=pointer_key,
            pointer_version_id=pointer["version_id"],
            pointer_etag=pointer["etag"],
            release_id=release.release_id,
            application_version=application_version,
            config_reference=dict(reference),
            announcement_ttl_days=announcement_days,
            feed_state_ttl_days=feed_days,
            deployment_sha256=sha256_bytes(deployment_body),
            terraform_output_sha256=sha256_bytes(output_body),
        ),
        ddb_client,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preview or apply the one-time ADR-025 source-state TTL migration.")
    parser.add_argument("action", choices=("preview", "apply"))
    parser.add_argument("--deployment", type=Path, required=True)
    parser.add_argument("--terraform-output", type=Path, required=True)
    parser.add_argument("--expected-account", required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--inventory-limit", type=int, required=True)
    parser.add_argument("--migration-as-of")
    parser.add_argument("--expected-plan-sha256")
    return parser.parse_args(argv)


def _write(document: Mapping[str, Any], *, stream: Any = sys.stdout) -> None:
    print(canonical_json(document).decode("utf-8"), file=stream)


def main(
    argv: list[str] | None = None,
    *,
    sts_client: Any | None = None,
    s3_client: Any | None = None,
    ddb_client_factory: Any | None = None,
) -> int:
    arguments = parse_args(argv)
    try:
        if sts_client is None or s3_client is None or ddb_client_factory is None:
            import boto3

            sts_client = sts_client or boto3.client("sts")
            s3_client = s3_client or boto3.client("s3")
            ddb_client_factory = ddb_client_factory or (lambda **kwargs: boto3.client("dynamodb", **kwargs))
        context, ddb_client = load_context(
            deployment_path=arguments.deployment,
            terraform_output_path=arguments.terraform_output,
            expected_account=arguments.expected_account,
            sts_client=sts_client,
            s3_client=s3_client,
            ddb_client_factory=ddb_client_factory,
        )
        if arguments.action == "preview":
            if arguments.expected_plan_sha256 is not None:
                raise MigrationError("inventory_refused", "preview does not accept an expected plan digest")
            if arguments.migration_as_of is None:
                raise MigrationError("inventory_refused", "preview requires an exact migration_as_of")
            migration_as_of = _parse_timestamp(arguments.migration_as_of, "migration_as_of")
            plan = create_plan(
                ddb_client,
                context=context,
                inventory_limit=arguments.inventory_limit,
                migration_as_of=migration_as_of,
            )
            digest = write_plan(arguments.plan, plan)
            _write(
                {
                    "status": "previewed",
                    "plan_sha256": digest,
                    "migration_as_of": plan["migration_as_of"],
                    "release_id": context.release_id,
                    "inventory_scanned_count": plan["inventory_scanned_count"],
                    "counts": plan["counts"],
                }
            )
            return 0
        if arguments.migration_as_of is not None:
            raise MigrationError("stale_plan", "apply takes migration_as_of only from the saved plan")
        if arguments.expected_plan_sha256 is None:
            raise MigrationError("stale_plan", "apply requires the expected plan SHA-256")
        plan = load_plan(arguments.plan, arguments.expected_plan_sha256)
        if plan.get("inventory_limit") != arguments.inventory_limit:
            raise MigrationError("stale_plan", "inventory limit differs from the preview")
        result = apply_plan(
            ddb_client,
            context=context,
            plan=plan,
            plan_sha256=arguments.expected_plan_sha256,
            context_verifier=lambda: verify_pointer_current(s3_client, context),
        )
        _write(result)
        return (
            0
            if result["status"] == "applied"
            else (EXIT_AMBIGUOUS if result["status"] in {"ambiguous", "applied_unverified"} else EXIT_REFUSED)
        )
    except MigrationError as error:
        _write({"status": error.status, "detail": error.detail}, stream=sys.stderr)
        if error.status in {"ambiguous", "applied_unverified"}:
            return EXIT_AMBIGUOUS
        return (
            EXIT_INVALID
            if error.status in {"invalid_input", "inventory_refused", "local_write_failed"}
            else EXIT_REFUSED
        )


if __name__ == "__main__":
    raise SystemExit(main())
