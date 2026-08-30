"""Bounded plans for removed-feed retirement, compaction, and restoration."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

from .state import FeedCheckpoint

__all__ = [
    "RetirementContext",
    "SourceRetirementError",
    "apply_plan",
    "canonical_json",
    "create_plan",
    "sha256_bytes",
]

PLAN_VERSION = 1
FEED_SK = "STATE"
TOMBSTONE_TYPE = "feed_tombstone"
_FEED_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")
_DECISION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SERIALIZER = TypeSerializer()
_DESERIALIZER = TypeDeserializer()
_FEED_FIELDS = {field.name for field in fields(FeedCheckpoint)}
_TOMBSTONE_FIELDS = {
    "PK",
    "SK",
    "item_type",
    "feed_name",
    "feed_url_sha256",
    "retired_at",
    "retirement_decision_id",
    "compacted_at",
    "state_version",
}


class SourceRetirementError(RuntimeError):
    """One bounded operator-facing refusal without provider response text."""

    def __init__(self, status: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(frozen=True, slots=True)
class RetirementContext:
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
    feed_state_ttl_days: int
    configured_feeds: Mapping[str, str]
    deployment_sha256: str
    terraform_output_sha256: str

    def plan_document(self) -> dict[str, Any]:
        feeds = dict(sorted(self.configured_feeds.items()))
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
            "feed_state_ttl_days": self.feed_state_ttl_days,
            "configured_feed_count": len(feeds),
            "configured_feeds_sha256": sha256_bytes(canonical_json(feeds)),
            "deployment_sha256": self.deployment_sha256,
            "terraform_output_sha256": self.terraform_output_sha256,
        }


def canonical_json(document: Mapping[str, Any]) -> bytes:
    return json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _plain(value: object) -> object:
    if isinstance(value, Decimal):
        if value != value.to_integral_value():
            raise SourceRetirementError("checkpoint_refused", "source-state numbers must be integers")
        return int(value)
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    return value


def _document(item: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    return {name: _plain(_DESERIALIZER.deserialize(value)) for name, value in item.items()}


def _wire(document: Mapping[str, Any]) -> dict[str, Any]:
    return {name: _SERIALIZER.serialize(value) for name, value in document.items()}


def _timestamp(value: object, field: str, *, status: str = "checkpoint_refused") -> datetime:
    if not isinstance(value, str):
        raise SourceRetirementError(status, f"{field} is malformed")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise SourceRetirementError(status, f"{field} is malformed") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SourceRetirementError(status, f"{field} must include a UTC offset")
    return parsed.astimezone(UTC)


def _utc_timestamp(value: datetime, field: str) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SourceRetirementError("invalid_input", f"{field} must include a UTC offset")
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _decision_id(value: object, field: str = "decision_id") -> str:
    if not isinstance(value, str) or _DECISION_ID.fullmatch(value) is None:
        raise SourceRetirementError("invalid_input", f"{field} must be a bounded decision identifier")
    return value


def _feed_name(value: object) -> str:
    if not isinstance(value, str) or _FEED_NAME.fullmatch(value) is None:
        raise SourceRetirementError("invalid_input", "feed_name must be a bounded lowercase identifier")
    return value


def _positive_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SourceRetirementError("checkpoint_refused", f"{field} must be a positive integer")
    return value


def _feed_url_sha256(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _content_sha256(document: Mapping[str, Any]) -> str:
    return sha256_bytes(canonical_json(dict(document)))


def _strong_read(client: Any, *, table_name: str, feed_name: str) -> dict[str, Any] | None:
    try:
        response = client.get_item(
            TableName=table_name,
            Key=_wire({"PK": f"FEED#{feed_name}", "SK": FEED_SK}),
            ConsistentRead=True,
        )
    except Exception as error:
        raise SourceRetirementError("read_failed", "feed checkpoint could not be strongly read") from error
    item = response.get("Item")
    if item is None:
        return None
    if not isinstance(item, Mapping):
        raise SourceRetirementError("checkpoint_refused", "feed checkpoint read returned a malformed item")
    return _document(item)


def _checkpoint(document: Mapping[str, Any], feed_name: str) -> FeedCheckpoint:
    if document.get("PK") != f"FEED#{feed_name}" or document.get("SK") != FEED_SK:
        raise SourceRetirementError("checkpoint_refused", "feed checkpoint key differs from the selected feed")
    if document.get("item_type") != "feed" or document.get("feed_name") != feed_name:
        raise SourceRetirementError("checkpoint_refused", "selected source-state item is not a feed checkpoint")
    payload = {name: value for name, value in document.items() if name not in {"PK", "SK", "item_type"}}
    unknown = set(payload) - _FEED_FIELDS
    if unknown:
        raise SourceRetirementError("checkpoint_refused", "feed checkpoint contains unknown fields")
    try:
        return FeedCheckpoint(**payload)
    except (TypeError, ValueError) as error:
        raise SourceRetirementError("checkpoint_refused", "feed checkpoint failed its owned model") from error


def _tombstone(document: Mapping[str, Any], feed_name: str) -> dict[str, Any]:
    if set(document) != _TOMBSTONE_FIELDS:
        raise SourceRetirementError("checkpoint_refused", "retired-feed tombstone has an unexpected shape")
    if (
        document.get("PK") != f"FEED#{feed_name}"
        or document.get("SK") != FEED_SK
        or document.get("item_type") != TOMBSTONE_TYPE
        or document.get("feed_name") != feed_name
    ):
        raise SourceRetirementError("checkpoint_refused", "retired-feed tombstone identity is malformed")
    if not isinstance(document.get("feed_url_sha256"), str) or _SHA256.fullmatch(document["feed_url_sha256"]) is None:
        raise SourceRetirementError("checkpoint_refused", "retired-feed tombstone URL hash is malformed")
    _timestamp(document.get("retired_at"), "retired_at")
    _timestamp(document.get("compacted_at"), "compacted_at")
    _decision_id(document.get("retirement_decision_id"), "retirement_decision_id")
    _positive_integer(document.get("state_version"), "state_version")
    return dict(document)


def _pending_or_leased(checkpoint: FeedCheckpoint) -> bool:
    return any(
        value is not None
        for value in (
            checkpoint.lease_owner,
            checkpoint.lease_expires_at,
            checkpoint.pending_etag,
            checkpoint.pending_last_modified,
            checkpoint.pending_newest_publication_at,
            checkpoint.pending_run_id,
        )
    )


def _assert_context(context: RetirementContext) -> None:
    if not re.fullmatch(r"[0-9]{12}", context.account_id):
        raise SourceRetirementError("invalid_input", "account_id must be 12 digits")
    _positive_integer(context.feed_state_ttl_days, "feed_state_ttl_days")
    if (
        _SHA256.fullmatch(context.deployment_sha256) is None
        or _SHA256.fullmatch(context.terraform_output_sha256) is None
    ):
        raise SourceRetirementError("invalid_input", "local input digest is malformed")
    for name, url in context.configured_feeds.items():
        _feed_name(name)
        if not isinstance(url, str) or not url:
            raise SourceRetirementError("invalid_input", "configured feed URL is malformed")


def create_plan(
    client: Any,
    *,
    context: RetirementContext,
    action: str,
    feed_name: str,
    decision_id: str,
    decision_at: datetime,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Create one read-only exact-feed plan."""

    _assert_context(context)
    selected = _feed_name(feed_name)
    decision = _decision_id(decision_id)
    when = _utc_timestamp(decision_at, "decision_at")
    observed_at = now or datetime.now(UTC)
    observed = _utc_timestamp(observed_at, "current time")
    if _timestamp(when, "decision_at") > _timestamp(observed, "current time"):
        raise SourceRetirementError("invalid_input", "decision_at cannot be in the future")
    document = _strong_read(client, table_name=context.table_name, feed_name=selected)
    if document is None:
        raise SourceRetirementError("checkpoint_refused", "selected feed state does not exist")
    configured_url = context.configured_feeds.get(selected)
    base = {
        "plan_version": PLAN_VERSION,
        "action": action,
        "decision_id": decision,
        "decision_at": when,
        "context": context.plan_document(),
        "feed_name": selected,
    }

    if action == "retire":
        if configured_url is not None:
            raise SourceRetirementError("configured_feed", "selected feed remains present in the active release")
        checkpoint = _checkpoint(document, selected)
        if checkpoint.retired_at is not None:
            raise SourceRetirementError("checkpoint_refused", "selected feed checkpoint is already retired")
        if _pending_or_leased(checkpoint):
            raise SourceRetirementError("checkpoint_busy", "selected feed has a lease or pending page-set work")
        retired_at = _timestamp(when, "decision_at")
        retire_after = int((retired_at + timedelta(days=context.feed_state_ttl_days)).timestamp())
        return {
            **base,
            "active_release_feed_absent": True,
            "checkpoint": dict(document),
            "checkpoint_content_sha256": _content_sha256(document),
            "feed_url_sha256": _feed_url_sha256(checkpoint.feed_url),
            "target": {
                "retired_at": when,
                "retire_after": retire_after,
                "retirement_decision_id": decision,
                "state_version": checkpoint.state_version + 1,
            },
        }

    if action == "compact":
        if configured_url is not None:
            raise SourceRetirementError("configured_feed", "selected feed remains present in the active release")
        checkpoint = _checkpoint(document, selected)
        if checkpoint.retired_at is None or checkpoint.retire_after is None:
            raise SourceRetirementError("checkpoint_refused", "selected feed checkpoint is not retired")
        if _pending_or_leased(checkpoint):
            raise SourceRetirementError("checkpoint_busy", "selected feed has a lease or pending page-set work")
        compacted_at = _timestamp(when, "decision_at")
        if int(compacted_at.timestamp()) < checkpoint.retire_after:
            raise SourceRetirementError("retention_incomplete", "full checkpoint retention has not elapsed")
        return {
            **base,
            "active_release_feed_absent": True,
            "checkpoint": dict(document),
            "checkpoint_content_sha256": _content_sha256(document),
            "feed_url_sha256": _feed_url_sha256(checkpoint.feed_url),
            "target": {
                "item_type": TOMBSTONE_TYPE,
                "feed_name": selected,
                "feed_url_sha256": _feed_url_sha256(checkpoint.feed_url),
                "retired_at": checkpoint.retired_at,
                "retirement_decision_id": checkpoint.retirement_decision_id,
                "compacted_at": when,
                "state_version": checkpoint.state_version + 1,
            },
        }

    if action == "restore":
        if configured_url is None:
            raise SourceRetirementError("configured_feed", "selected feed is absent from the active release")
        tombstone = _tombstone(document, selected)
        configured_hash = _feed_url_sha256(configured_url)
        if configured_hash != tombstone["feed_url_sha256"]:
            raise SourceRetirementError(
                "feed_url_mismatch", "active release reuses the tombstoned feed name with a different URL"
            )
        return {
            **base,
            "active_release_feed": {"name": selected, "url_sha256": configured_hash},
            "checkpoint": tombstone,
            "checkpoint_content_sha256": _content_sha256(tombstone),
            "feed_url_sha256": configured_hash,
            "target": {
                "item_type": "feed",
                "feed_name": selected,
                "feed_url": configured_url,
                "consecutive_failures": 0,
                "state_version": int(tombstone["state_version"]) + 1,
                "restored_at": when,
                "restoration_decision_id": decision,
                "prior_retirement_decision_id": tombstone["retirement_decision_id"],
                "restored_feed_url_sha256": configured_hash,
            },
        }

    raise SourceRetirementError("invalid_input", "action must be retire, compact, or restore")


def _conditional_failure(error: Exception) -> bool:
    response = getattr(error, "response", {})
    return isinstance(response, Mapping) and response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def _condition_for_document(document: Mapping[str, Any]) -> tuple[str, dict[str, str], dict[str, Any]]:
    names: dict[str, str] = {}
    values: dict[str, Any] = {}
    clauses: list[str] = []
    for index, (name, value) in enumerate(sorted(document.items())):
        alias = f"#c{index}"
        names[alias] = name
        if value is None:
            clauses.append(f"attribute_not_exists({alias})")
        else:
            token = f":c{index}"
            values[token] = value
            clauses.append(f"{alias} = {token}")
    return " AND ".join(clauses), names, values


def _update_arguments(table_name: str, plan: Mapping[str, Any]) -> dict[str, Any]:
    checkpoint = plan["checkpoint"]
    target = plan["target"]
    condition, names, values = _condition_for_document(checkpoint)
    set_parts: list[str] = []
    remove_parts: list[str] = []
    target_names = set(target)
    for index, name in enumerate(sorted(target_names - set(checkpoint))):
        alias = f"#a{index}"
        names[alias] = name
        condition += f" AND attribute_not_exists({alias})"
    for index, (name, value) in enumerate(sorted(target.items())):
        alias = f"#t{index}"
        token = f":t{index}"
        names[alias] = name
        values[token] = value
        set_parts.append(f"{alias} = {token}")
    if plan["action"] != "retire":
        for index, name in enumerate(sorted(set(checkpoint) - {"PK", "SK"} - target_names)):
            alias = f"#r{index}"
            names[alias] = name
            remove_parts.append(alias)
    expression = "SET " + ", ".join(set_parts)
    if remove_parts:
        expression += " REMOVE " + ", ".join(remove_parts)
    return {
        "TableName": table_name,
        "Key": _wire({"PK": checkpoint["PK"], "SK": checkpoint["SK"]}),
        "UpdateExpression": expression,
        "ConditionExpression": condition,
        "ExpressionAttributeNames": names,
        "ExpressionAttributeValues": _wire(values),
    }


def _target_satisfied(document: Mapping[str, Any] | None, plan: Mapping[str, Any]) -> bool:
    if document is None:
        return False
    if plan["action"] == "retire":
        expected = {**plan["checkpoint"], **plan["target"]}
    else:
        expected = {"PK": plan["checkpoint"]["PK"], "SK": plan["checkpoint"]["SK"], **plan["target"]}
    return dict(document) == expected


def _validate_plan(plan: Mapping[str, Any], context: RetirementContext) -> None:
    required = {
        "plan_version",
        "action",
        "decision_id",
        "decision_at",
        "context",
        "feed_name",
        "checkpoint",
        "checkpoint_content_sha256",
        "feed_url_sha256",
        "target",
    }
    if not required.issubset(plan) or plan.get("plan_version") != PLAN_VERSION:
        raise SourceRetirementError("stale_plan", "saved source-retirement plan has an unexpected shape")
    if plan.get("context") != context.plan_document():
        raise SourceRetirementError("stale_plan", "source-retirement context differs from the preview")
    if _content_sha256(plan["checkpoint"]) != plan.get("checkpoint_content_sha256"):
        raise SourceRetirementError("stale_plan", "saved checkpoint content digest is invalid")
    _decision_id(plan.get("decision_id"))
    _timestamp(plan.get("decision_at"), "decision_at", status="stale_plan")


def apply_plan(
    client: Any,
    *,
    context: RetirementContext,
    plan: Mapping[str, Any],
    plan_sha256: str,
    context_verifier: Callable[[], None] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Apply one unchanged plan and prove its exact durable target."""

    _assert_context(context)
    _validate_plan(plan, context)
    if _SHA256.fullmatch(plan_sha256) is None:
        raise SourceRetirementError("stale_plan", "plan SHA-256 is malformed")
    feed_name = _feed_name(plan["feed_name"])
    fresh = _strong_read(client, table_name=context.table_name, feed_name=feed_name)
    if fresh != plan["checkpoint"] or _content_sha256(fresh or {}) != plan["checkpoint_content_sha256"]:
        raise SourceRetirementError("stale_plan", "feed checkpoint differs from the preview")
    rebuilt = create_plan(
        client,
        context=context,
        action=plan["action"],
        feed_name=feed_name,
        decision_id=plan["decision_id"],
        decision_at=_timestamp(plan["decision_at"], "decision_at", status="stale_plan"),
        now=now,
    )
    if rebuilt != plan:
        raise SourceRetirementError("stale_plan", "source-retirement classification differs from the preview")
    if context_verifier is not None:
        context_verifier()

    write_error: Exception | None = None
    try:
        client.update_item(**_update_arguments(context.table_name, plan))
    except Exception as error:
        write_error = error
    try:
        durable = _strong_read(client, table_name=context.table_name, feed_name=feed_name)
    except SourceRetirementError:
        return {
            "status": "ambiguous",
            "plan_sha256": plan_sha256,
            "action": plan["action"],
            "feed_name": feed_name,
            "detail": "conditional write outcome could not be strongly reread",
        }
    if _target_satisfied(durable, plan):
        return {
            "status": "applied" if write_error is None else "applied_after_reread",
            "plan_sha256": plan_sha256,
            "action": plan["action"],
            "feed_name": feed_name,
            "state_version": plan["target"]["state_version"],
        }
    if write_error is not None and _conditional_failure(write_error):
        status = "conflict"
        detail = "conditional source-retirement write was refused"
    elif write_error is not None:
        status = "ambiguous"
        detail = "write response failed and the exact target is absent"
    else:
        status = "applied_unverified"
        detail = "write returned without the exact durable target"
    return {
        "status": status,
        "plan_sha256": plan_sha256,
        "action": plan["action"],
        "feed_name": feed_name,
        "detail": detail,
    }
