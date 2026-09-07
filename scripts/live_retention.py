"""Explicit, version-pinned retirement of verified closed live-control sessions.

Only operators write closeouts or delete objects. The cleanup build has neither
permission. Unknown objects remain retained, including losing-start uploads.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError

RETENTION = timedelta(days=90)
LIMIT = 10000
UNRESOLVED = {"pending_queue", "queued", "sending", "failed_retryable", "delivery_unknown"}
OUTCOMES = {"posted", "no_positive_match", "observation_complete"}


class RetentionError(ValueError):
    """A safe-to-display refusal; do not include private object contents."""


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise RetentionError("invalid retention timestamp")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise RetentionError("retention timestamps must be UTC")
    return parsed


def operation_id(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise RetentionError("invalid retention generation")
    return value


def object_ref(value: Any, operation: str) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"key", "version"}:
        raise RetentionError("invalid exact-version reference")
    key, version = value["key"], value["version"]
    if not isinstance(version, str) or not version or version == "null":
        raise RetentionError("unversioned control objects cannot be retired")
    if not isinstance(key, str) or not (
        re.fullmatch(rf"bundles/{operation}-[a-f0-9]{{64}}\.zip", key)
        or re.fullmatch(rf"evidence/{operation}/[a-z-]+-[a-f0-9]{{32}}\.json", key)
    ):
        raise RetentionError("retirement target is outside its session")
    return value


def inventory(controller: Any, prefix: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    paginator = controller.clients["s3"].get_paginator("list_object_versions")
    for page in paginator.paginate(Bucket=controller.config["bucket"], Prefix=prefix):
        if page.get("DeleteMarkers"):
            raise RetentionError("delete markers require manual retention review")
        rows.extend(page.get("Versions", []))
        if len(rows) > LIMIT:
            raise RetentionError("control inventory exceeds the reviewed bound")
    return rows


def read(controller: Any, ref: dict[str, str]) -> Any:
    response = controller.clients["s3"].get_object(
        Bucket=controller.config["bucket"], Key=ref["key"], VersionId=ref["version"]
    )
    body = response["Body"]
    try:
        data = body.read(4 * 1024 * 1024 + 1)
    finally:
        body.close()
    if len(data) > 4 * 1024 * 1024:
        raise RetentionError("control document exceeds the reviewed bound")
    return json.loads(data)


def put_once(controller: Any, key: str, value: Any) -> str:
    response = controller.clients["s3"].put_object(
        Bucket=controller.config["bucket"],
        Key=key,
        Body=canonical(value),
        IfNoneMatch="*",
        ServerSideEncryption="AES256",
    )
    version = response.get("VersionId")
    if not isinstance(version, str) or not version or version == "null":
        raise RetentionError("control bucket versioning is required")
    return version


def validate_closeout(controller: Any, value: Any, now: datetime) -> dict[str, Any]:
    fields = {"schema", "bucket", "operation", "execution", "closed_at", "outcome", "receipt", "objects"}
    if not isinstance(value, dict) or set(value) != fields or type(value["schema"]) is not int or value["schema"] != 1:
        raise RetentionError("unknown closeout schema")
    operation = operation_id(value["operation"])
    execution_prefix = controller.config["state_machine"].replace(":stateMachine:", ":execution:") + ":"
    if (
        value["bucket"] != controller.config["bucket"]
        or not isinstance(value["execution"], str)
        or not re.fullmatch(re.escape(execution_prefix) + rf"(?:{operation}|park-[a-f0-9]{{32}})", value["execution"])
        or not isinstance(value["outcome"], str)
        or value["outcome"] not in OUTCOMES
        or timestamp(value["closed_at"]) > now
    ):
        raise RetentionError("closeout identity, outcome, or clock is invalid")
    objects = value["objects"]
    if not isinstance(objects, list) or not 1 <= len(objects) <= LIMIT:
        raise RetentionError("invalid closeout object inventory")
    for ref in objects:
        object_ref(ref, operation)
    if objects != sorted(objects, key=lambda ref: (ref["key"], ref["version"])) or len(
        {canonical(ref) for ref in objects}
    ) != len(objects):
        raise RetentionError("closeout inventory must be sorted and unique")
    receipt = object_ref(value["receipt"], operation)
    if receipt not in objects or not receipt["key"].startswith(f"evidence/{operation}/receipt-"):
        raise RetentionError("closeout must retain the verified receipt reference")
    if not any(ref["key"].startswith("bundles/") for ref in objects):
        raise RetentionError("closeout must bind its source bundle")
    return value


def closeout(controller: Any, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    item = controller.ledger()
    if (
        item.get("phase", {}).get("S") != "parked"
        or item.get("stop", {}).get("BOOL") is not True
        or item.get("evidence_incomplete", {}).get("BOOL") is not False
        or item.get("outcome", {}).get("S") not in OUTCOMES
    ):
        raise RetentionError("only successfully parked, resolved sessions can close")
    operation = operation_id(item["generation"]["S"])
    key = f"closeouts/{operation}.json"
    existing = inventory(controller, key)
    if existing:
        if len(existing) != 1 or existing[0]["Key"] != key:
            raise RetentionError("ambiguous closeout versions")
        value = read(controller, {"key": key, "version": existing[0]["VersionId"]})
        validate_closeout(controller, value, now)
        if value["operation"] != operation or value["execution"] != item["execution"]["S"]:
            raise RetentionError("closeout belongs to another owner")
        return {"status": "already_closed", "operation": operation}
    execution = controller.clients["stepfunctions"].describe_execution(executionArn=item["execution"]["S"])
    if execution["status"] != "SUCCEEDED" or json.loads(execution["input"]) != json.loads(item["input"]["S"]):
        raise RetentionError("terminal successful owner and exact input are required")
    if execution["stopDate"] > now:
        raise RetentionError("execution closure is in the future")
    controller.verify("parked")
    payload = json.loads(item["input"]["S"])
    bucket_prefix = controller.config["bucket"] + "/"
    if not payload["source"].startswith(bucket_prefix) or payload["operation"] != operation:
        raise RetentionError("source identity does not match the owner")
    source = object_ref({"key": payload["source"][len(bucket_prefix) :], "version": payload["version"]}, operation)
    rows = inventory(controller, f"bundles/{operation}-") + inventory(controller, f"evidence/{operation}/")
    if len(rows) > LIMIT or any(row["LastModified"] > execution["stopDate"] for row in rows):
        raise RetentionError("objects changed after execution closure, or inventory is too large")
    refs = sorted(
        [{"key": row["Key"], "version": row["VersionId"]} for row in rows], key=lambda ref: (ref["key"], ref["version"])
    )
    if source not in refs:
        raise RetentionError("pinned source version is missing")
    receipts = [row for row in rows if row["Key"].startswith(f"evidence/{operation}/receipt-")]
    if not receipts:
        raise RetentionError("parking receipt is missing")
    # Ties are ambiguous: never choose an earlier clean receipt over a failed retry.
    latest_time = max(row["LastModified"] for row in receipts)
    latest = [row for row in receipts if row["LastModified"] == latest_time]
    if len(latest) != 1:
        raise RetentionError("latest parking receipt is ambiguous")
    receipt_ref = {"key": latest[0]["Key"], "version": latest[0]["VersionId"]}
    receipt = read(controller, receipt_ref)
    if not isinstance(receipt, dict):
        raise RetentionError("parking receipt must be an object")
    if receipt.get("terraform_converged") is not True or receipt.get("unresolved_work") != dict.fromkeys(UNRESOLVED, 0):
        raise RetentionError("parking receipt has unresolved or unproved work")
    queues = receipt.get("controls", {}).get("queues")
    if (
        receipt.get("controls", {}).get("work_inventory_complete") is not True
        or not isinstance(queues, dict)
        or set(queues) != {"apcf-delivery-dev.fifo", "apcf-delivery-dlq-dev.fifo", "apcf-runtime-failures-dev"}
        or any(
            not isinstance(counts, dict)
            or set(counts)
            != {
                "ApproximateNumberOfMessages",
                "ApproximateNumberOfMessagesNotVisible",
                "ApproximateNumberOfMessagesDelayed",
            }
            or any(str(count) != "0" for count in counts.values())
            for counts in queues.values()
        )
    ):
        raise RetentionError("retained queue work requires review before closure")
    value = {
        "schema": 1,
        "bucket": controller.config["bucket"],
        "operation": operation,
        "execution": item["execution"]["S"],
        "closed_at": now.isoformat(),
        "outcome": item["outcome"]["S"],
        "receipt": receipt_ref,
        "objects": refs,
    }
    validate_closeout(controller, value, now)
    if controller.ledger() != item:
        raise RetentionError("owner changed during closeout; retaining all objects")
    put_once(controller, key, value)
    return {"status": "closed", "operation": operation, "eligible_after": (now + RETENTION).isoformat()}


def preview(controller: Any, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    current = operation_id(controller.ledger().get("generation", {}).get("S"))
    sessions = []
    protected = 0
    rows = inventory(controller, "closeouts/")
    if len({row["Key"] for row in rows}) != len(rows):
        raise RetentionError("multiple closeout versions require review")
    for row in rows:
        ref = {"key": row["Key"], "version": row["VersionId"]}
        value = validate_closeout(controller, read(controller, ref), now)
        if ref["key"] != f"closeouts/{value['operation']}.json":
            raise RetentionError("closeout key does not match its session")
        if value["operation"] == current or timestamp(value["closed_at"]) + RETENTION > now:
            protected += 1
            continue
        sessions.append(
            {"closeout": ref, "sha256": digest(value), "operation": value["operation"], "objects": value["objects"]}
        )
    return {
        "schema": 1,
        "bucket": controller.config["bucket"],
        "created_at": now.isoformat(),
        "sessions": sorted(sessions, key=lambda row: row["operation"]),
        "protected_closeouts": protected,
        "unclosed_objects": "retained, including failed sessions and unproven orphan uploads",
    }


def prune(controller: Any, plan: Any, approval: str, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    fields = {"schema", "bucket", "created_at", "sessions", "protected_closeouts", "unclosed_objects"}
    if (
        not isinstance(plan, dict)
        or set(plan) != fields
        or type(plan["schema"]) is not int
        or plan["schema"] != 1
        or digest(plan) != approval
    ):
        raise RetentionError("prune requires the exact preview SHA-256")
    created = timestamp(plan["created_at"])
    if not timedelta(0) <= now - created <= timedelta(hours=24):
        raise RetentionError("prune preview expired or has a future clock")
    # Recompute before any delete. A retry may find already absent exact versions;
    # immutable manifests remain, so partial deletion does not expand authority.
    if preview(controller, created) != plan:
        raise RetentionError("retention state changed; create and review a new preview")
    deleted = 0
    for session in plan["sessions"]:
        for ref in session["objects"]:
            item = controller.ledger()
            current = operation_id(item.get("generation", {}).get("S"))
            payload = json.loads(item.get("input", {}).get("S", "{}"))
            if current == session["operation"] or (
                payload.get("source") == controller.config["bucket"] + "/" + ref["key"]
                and payload.get("version") == ref["version"]
            ):
                raise RetentionError("current owner references a prune target; stopped")
            controller.clients["s3"].delete_object(
                Bucket=controller.config["bucket"], Key=ref["key"], VersionId=ref["version"]
            )
            try:
                controller.clients["s3"].head_object(
                    Bucket=controller.config["bucket"], Key=ref["key"], VersionId=ref["version"]
                )
            except ClientError as error:
                if error.response["Error"]["Code"] not in {"404", "NoSuchVersion", "NoSuchKey", "NotFound"}:
                    raise
            else:
                raise RetentionError("exact version still exists after deletion; stopped")
            deleted += 1
    # The immutable closeouts are also the durable retry inventory. Keep them.
    return {"status": "pruned", "confirmed_absent_versions": deleted, "approval": approval}


def command(controller: Any, action: str, path: Path | None = None, approval: str = "") -> dict[str, Any]:
    if action == "close":
        return closeout(controller)
    if path is None:
        raise RetentionError("set PLAN to a private preview file")
    if approval:
        return prune(controller, json.loads(path.read_bytes()), approval)
    plan = preview(controller)
    with path.open("xb") as output:
        output.write(canonical(plan) + b"\n")
    return {"status": "preview", "plan": str(path), "sha256": digest(plan), "sessions": len(plan["sessions"])}
