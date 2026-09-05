#!/usr/bin/env python3
"""Preview, start, configure, and verify the ADR-027 DynamoDB restore proof."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import boto3
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aws_public_change_feed.parsing import load_unique_json  # noqa: E402

PLAN_VERSION = 2
EVIDENCE_VERSION = 1
RECOVERY_PERIOD_DAYS = 35
RPO_SECONDS = 300
RTO_SECONDS = 4 * 60 * 60
CLOCK_SKEW_SECONDS = 60
MAX_INVENTORY_ITEMS = 100_000
MAX_INVENTORY_BYTES = 256 * 1024 * 1024
MAX_CLOUDTRAIL_PAGES = 10
MAX_CLOUDTRAIL_EVENTS = 500
CLOUDTRAIL_PAGE_SIZE = 50
EXIT_INVALID = 2
EXIT_REFUSED = 3
EXIT_AMBIGUOUS = 4
SHA256_RE = re.compile(r"[a-f0-9]{64}")
GIT_SHA_RE = re.compile(r"[a-f0-9]{40}")
EXERCISE_ID_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?")
TABLE_NAME_RE = re.compile(r"[A-Za-z0-9_.-]{3,255}")
ACTIONABLE_STATES = ("pending_queue", "queued", "sending", "failed_retryable")
QUEUE_COUNT_ATTRIBUTES = (
    "ApproximateNumberOfMessages",
    "ApproximateNumberOfMessagesNotVisible",
    "ApproximateNumberOfMessagesDelayed",
)


class RecoveryProofError(RuntimeError):
    """One bounded operator-facing refusal without provider response text."""

    def __init__(self, status: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(frozen=True, slots=True)
class AwsClients:
    sts: Any
    dynamodb: Any
    lambda_client: Any
    events: Any
    sqs: Any
    cloudtrail: Any | None = None


def canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return {"base64_sha256": sha256_bytes(value), "byte_length": len(value)}
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _canonical_attribute_value(value: Any) -> Any:
    if not isinstance(value, Mapping) or len(value) != 1:
        return _json_safe(value)
    descriptor, payload = next(iter(value.items()))
    name = str(descriptor)
    if name in {"SS", "NS", "BS"} and isinstance(payload, (list, tuple)):
        members = [_json_safe(member) for member in payload]
        return {name: sorted(members, key=canonical_json)}
    if name == "L" and isinstance(payload, (list, tuple)):
        return {name: [_canonical_attribute_value(member) for member in payload]}
    if name == "M" and isinstance(payload, Mapping):
        return {name: {str(key): _canonical_attribute_value(member) for key, member in payload.items()}}
    return {name: _json_safe(payload)}


def _canonical_item(item: Mapping[str, Any]) -> dict[str, Any]:
    return {str(name): _canonical_attribute_value(value) for name, value in item.items()}


def _safe_text(value: object, field: str, *, status: str = "invalid_input") -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 500
        or any(character in value for character in "\r\n\x00")
    ):
        raise RecoveryProofError(status, f"{field} must be bounded single-line text")
    return value


def _sha256(value: object, field: str, *, status: str = "invalid_input") -> str:
    text = _safe_text(value, field, status=status)
    if SHA256_RE.fullmatch(text) is None:
        raise RecoveryProofError(status, f"{field} must be a lowercase SHA-256 digest")
    return text


def _positive_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RecoveryProofError("invalid_input", f"{field} must be a positive integer")
    return value


def _bounded_positive_integer(value: object, field: str, maximum: int) -> int:
    result = _positive_integer(value, field)
    if result > maximum:
        raise RecoveryProofError("invalid_input", f"{field} exceeds the fixed safety maximum")
    return result


def _timestamp(value: object, field: str, *, status: str = "invalid_input") -> datetime:
    text = _safe_text(value, field, status=status)
    if not text.endswith("Z"):
        raise RecoveryProofError(status, f"{field} must be an explicit UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise RecoveryProofError(status, f"{field} must be an explicit UTC timestamp ending in Z") from error
    normalized = parsed.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    if parsed.utcoffset() != timedelta(0) or normalized != text:
        raise RecoveryProofError(status, f"{field} must be an explicit second-precision UTC timestamp")
    return parsed


def _read_mapping(path: Path, kind: str) -> tuple[Mapping[str, Any], bytes]:
    try:
        body = path.read_bytes()
        value = load_unique_json(body) if path.suffix == ".json" else yaml.safe_load(body)
    except Exception as error:
        raise RecoveryProofError("invalid_input", f"{kind} could not be read") from error
    if not isinstance(value, Mapping):
        raise RecoveryProofError("invalid_input", f"{kind} must be an object")
    return value, body


def _output_value(outputs: Mapping[str, Any], name: str) -> Any:
    entry = outputs.get(name)
    if not isinstance(entry, Mapping) or set(entry) != {"sensitive", "type", "value"}:
        raise RecoveryProofError("invalid_input", f"Terraform output {name} is missing or malformed")
    return entry["value"]


def _atomic_write(path: Path, body: bytes, kind: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        if path.exists() or temporary.exists():
            raise RecoveryProofError("local_write_failed", f"{kind} path or temporary already exists")
        temporary.write_bytes(body)
        temporary.chmod(0o600)
        temporary.replace(path)
    except RecoveryProofError:
        raise
    except OSError as error:
        raise RecoveryProofError("local_write_failed", f"{kind} could not be written") from error


def _git_sha() -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise RecoveryProofError("invalid_input", "Git HEAD could not be read") from error
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise RecoveryProofError("invalid_input", "Git worktree state could not be read") from error
    if status.stdout:
        raise RecoveryProofError("state_refused", "ADR-027 proof requires a clean Git worktree")
    value = _safe_text(completed.stdout.strip(), "Git SHA")
    if GIT_SHA_RE.fullmatch(value) is None:
        raise RecoveryProofError("invalid_input", "Git SHA must be 40 lowercase hexadecimal characters")
    return value


def _account_id(deployment: Mapping[str, Any]) -> str:
    environments = deployment.get("environments")
    if not isinstance(environments, list):
        raise RecoveryProofError("invalid_input", "deployment environments must be a list")
    accounts = {environment.get("account_id") for environment in environments if isinstance(environment, Mapping)}
    if len(accounts) != 1:
        raise RecoveryProofError("invalid_input", "deployment must name exactly one AWS account")
    return _safe_text(next(iter(accounts)), "deployment account ID")


def _local_context(deployment_path: Path, terraform_output_path: Path) -> dict[str, Any]:
    if deployment_path.resolve() != (ROOT / "infra/central/deployment.yaml").resolve():
        raise RecoveryProofError("invalid_input", "ADR-027 proof requires the reviewed central deployment path")
    deployment, deployment_body = _read_mapping(deployment_path, "deployment")
    outputs, outputs_body = _read_mapping(terraform_output_path, "Terraform output")
    if deployment.get("deployment_id") != "dev" or deployment.get("deployment_region") != "us-east-1":
        raise RecoveryProofError("invalid_input", "ADR-027 proof requires the reviewed dev deployment in us-east-1")
    account_id = _account_id(deployment)

    primary_source = _safe_text(_output_value(outputs, "primary_source_state_table"), "primary source table")
    primary_delivery = _safe_text(_output_value(outputs, "primary_delivery_table"), "primary delivery table")
    if primary_source != "apcf-source-state-dev" or primary_delivery != "apcf-delivery-dev":
        raise RecoveryProofError("state_refused", "Terraform primary table names differ from the reviewed deployment")
    if (
        _output_value(outputs, "source_state_table") != primary_source
        or _output_value(outputs, "delivery_table") != primary_delivery
    ):
        raise RecoveryProofError("state_refused", "runtime is already bound to a recovery table")

    recovery = _output_value(outputs, "dynamodb_recovery")
    if not isinstance(recovery, Mapping) or dict(recovery) != {
        "cutover_enabled": False,
        "pitr_enabled": True,
        "plan_sha256": None,
        "recovery_period_days": RECOVERY_PERIOD_DAYS,
    }:
        raise RecoveryProofError("state_refused", "Terraform does not expose the accepted PITR state without a cutover")
    triggers = _output_value(outputs, "runtime_trigger_states")
    if not isinstance(triggers, Mapping) or set(triggers) != {"watcher", "dispatcher", "worker", "reconciler"}:
        raise RecoveryProofError("invalid_input", "Terraform trigger output is malformed")
    if any(value is not False for value in triggers.values()):
        raise RecoveryProofError("state_refused", "all four runtime triggers must be disabled")
    if _output_value(outputs, "watcher_execution_paused") is not True:
        raise RecoveryProofError("state_refused", "watcher reserved concurrency pause is not selected")

    function_names = _output_value(outputs, "function_names")
    if not isinstance(function_names, Mapping) or set(function_names) != {
        "watcher",
        "shadow",
        "dispatcher",
        "worker",
        "reconciler",
    }:
        raise RecoveryProofError("invalid_input", "Terraform function-name output is malformed")
    roles = _output_value(outputs, "roles")
    expected_role_arn = f"arn:aws:iam::{account_id}:role/apcf-dev-dynamodb-recovery"
    expected_evidence_role_arn = f"arn:aws:iam::{account_id}:role/apcf-dev-dynamodb-recovery-evidence"
    if not isinstance(roles, Mapping) or roles.get("dynamodb_recovery") != expected_role_arn:
        raise RecoveryProofError("state_refused", "Terraform recovery role differs from the reviewed deployment")
    if roles.get("dynamodb_recovery_evidence") != expected_evidence_role_arn:
        raise RecoveryProofError(
            "state_refused", "Terraform recovery evidence role differs from the reviewed deployment"
        )

    return {
        "deployment_id": "dev",
        "region": "us-east-1",
        "account_id": account_id,
        "recovery_role_arn": expected_role_arn,
        "recovery_evidence_role_arn": expected_evidence_role_arn,
        "git_sha": _git_sha(),
        "deployment_path": str(deployment_path.resolve()),
        "deployment_sha256": sha256_bytes(deployment_body),
        "terraform_output_path": str(terraform_output_path.resolve()),
        "terraform_output_sha256": sha256_bytes(outputs_body),
        "primary_tables": {"source_state": primary_source, "delivery": primary_delivery},
        "function_names": {name: _safe_text(function_names[name], f"{name} function name") for name in function_names},
        "queue_name": _safe_text(_output_value(outputs, "delivery_queue"), "delivery queue name"),
        "queue_arn": _safe_text(_output_value(outputs, "delivery_queue_arn"), "delivery queue ARN"),
        "trigger_states": {name: False for name in triggers},
        "pitr": dict(recovery),
    }


def _client_error_code(error: Exception) -> str | None:
    response = getattr(error, "response", None)
    if isinstance(response, Mapping):
        details = response.get("Error")
        if isinstance(details, Mapping) and isinstance(details.get("Code"), str):
            return str(details["Code"])
    return None


def _assumed_role_identity(
    client: Any,
    *,
    account_id: str,
    role_arn: str,
    role_description: str,
) -> dict[str, str]:
    try:
        response = client.get_caller_identity()
    except Exception as error:
        raise RecoveryProofError("provider_error", "AWS caller identity could not be read") from error
    if not isinstance(response, Mapping):
        raise RecoveryProofError("provider_error", "AWS caller identity response is malformed")
    account = _safe_text(response.get("Account"), "AWS caller account", status="identity_refused")
    arn = _safe_text(response.get("Arn"), "AWS caller ARN", status="identity_refused")
    user_id = _safe_text(response.get("UserId"), "AWS caller user ID", status="identity_refused")
    expected_prefix = f"arn:aws:sts::{account_id}:assumed-role/{role_arn.rsplit('/', 1)[-1]}/"
    if account != account_id or not arn.startswith(expected_prefix) or len(arn) == len(expected_prefix):
        raise RecoveryProofError("identity_refused", f"AWS caller is not the reviewed {role_description}")
    return {"account_id": account, "arn": arn, "user_id": user_id}


def _caller_identity(client: Any, context: Mapping[str, Any]) -> dict[str, str]:
    return _assumed_role_identity(
        client,
        account_id=context["account_id"],
        role_arn=context["recovery_role_arn"],
        role_description="DynamoDB recovery role",
    )


def _evidence_caller_identity(client: Any, context: Mapping[str, Any]) -> dict[str, str]:
    return _assumed_role_identity(
        client,
        account_id=context["account_id"],
        role_arn=context["recovery_evidence_role_arn"],
        role_description="DynamoDB recovery evidence role",
    )


def _describe_optional(client: Any, table_name: str) -> Mapping[str, Any] | None:
    try:
        response = client.describe_table(TableName=table_name)
    except Exception as error:
        if _client_error_code(error) == "ResourceNotFoundException":
            return None
        raise RecoveryProofError("provider_error", "table identity could not be read") from error
    table = response.get("Table")
    if not isinstance(table, Mapping):
        raise RecoveryProofError("provider_error", "table identity response is malformed")
    return table


def _schema(table: Mapping[str, Any]) -> dict[str, Any]:
    indexes = []
    for index in table.get("GlobalSecondaryIndexes", []):
        if not isinstance(index, Mapping):
            raise RecoveryProofError("state_refused", "table GSI description is malformed")
        indexes.append(
            {
                "IndexName": index.get("IndexName"),
                "KeySchema": sorted(
                    index.get("KeySchema", []),
                    key=lambda item: (item.get("KeyType", ""), item.get("AttributeName", "")),
                ),
                "Projection": index.get("Projection"),
            }
        )
    billing = table.get("BillingModeSummary", {}).get("BillingMode", "PROVISIONED")
    sse = table.get("SSEDescription", {})
    return _json_safe(
        {
            "AttributeDefinitions": sorted(
                table.get("AttributeDefinitions", []), key=lambda item: item.get("AttributeName", "")
            ),
            "BillingMode": billing,
            "GlobalSecondaryIndexes": sorted(indexes, key=lambda item: str(item["IndexName"])),
            "KeySchema": sorted(
                table.get("KeySchema", []), key=lambda item: (item.get("KeyType", ""), item.get("AttributeName", ""))
            ),
            "SSE": {name: sse.get(name) for name in ("SSEType", "KMSMasterKeyArn") if sse.get(name) is not None},
        }
    )


def _pitr(client: Any, table_name: str) -> dict[str, Any]:
    try:
        response = client.describe_continuous_backups(TableName=table_name)
    except Exception as error:
        raise RecoveryProofError("provider_error", "PITR state could not be read") from error
    description = response.get("ContinuousBackupsDescription", {}).get("PointInTimeRecoveryDescription", {})
    if not isinstance(description, Mapping):
        raise RecoveryProofError("provider_error", "PITR state response is malformed")
    return _json_safe(
        {
            "status": description.get("PointInTimeRecoveryStatus"),
            "period_days": description.get("RecoveryPeriodInDays"),
            "earliest": description.get("EarliestRestorableDateTime"),
            "latest": description.get("LatestRestorableDateTime"),
        }
    )


def _ttl(client: Any, table_name: str) -> dict[str, Any]:
    try:
        response = client.describe_time_to_live(TableName=table_name)
    except Exception as error:
        raise RecoveryProofError("provider_error", "TTL state could not be read") from error
    description = response.get("TimeToLiveDescription")
    if not isinstance(description, Mapping):
        raise RecoveryProofError("provider_error", "TTL state response is malformed")
    return {"status": description.get("TimeToLiveStatus"), "attribute": description.get("AttributeName")}


def _tags(client: Any, arn: str) -> list[dict[str, str]]:
    try:
        response = client.list_tags_of_resource(ResourceArn=arn)
    except Exception as error:
        raise RecoveryProofError("provider_error", "table tags could not be read") from error
    values = response.get("Tags")
    if not isinstance(values, list) or any(not isinstance(item, Mapping) for item in values):
        raise RecoveryProofError("provider_error", "table tag response is malformed")
    return sorted(
        [
            {
                "Key": _safe_text(item.get("Key"), "tag key", status="provider_error"),
                "Value": _safe_text(item.get("Value"), "tag value", status="provider_error"),
            }
            for item in values
        ],
        key=lambda item: item["Key"],
    )


def _wire_text(item: Mapping[str, Any], name: str) -> str | None:
    value = item.get(name)
    if isinstance(value, Mapping) and isinstance(value.get("S"), str):
        return str(value["S"])
    return None


def _wire_expiry(item: Mapping[str, Any]) -> int | None:
    value = item.get("expires_at")
    if not isinstance(value, Mapping) or not isinstance(value.get("N"), str):
        return None
    encoded = str(value["N"])
    if re.fullmatch(r"-?[0-9]+", encoded) is None:
        return None
    return int(encoded)


def _digest_summary(digests: Sequence[str], byte_count: int) -> dict[str, Any]:
    digest_body = b"".join(bytes.fromhex(value) for value in sorted(digests))
    return {
        "item_count": len(digests),
        "canonical_bytes": byte_count,
        "items_sha256": sha256_bytes(digest_body),
    }


def _inventory(
    client: Any,
    table_name: str,
    *,
    max_items: int,
    max_bytes: int,
    ttl_cutoff_epoch: int | None = None,
) -> dict[str, Any]:
    digests: list[str] = []
    protected_digests: list[str] = []
    ttl_eligible_digests: list[str] = []
    item_types: Counter[str] = Counter()
    states: Counter[str] = Counter()
    byte_count = 0
    protected_bytes = 0
    ttl_eligible_bytes = 0
    exclusive_start_key: Mapping[str, Any] | None = None
    seen_cursors: set[bytes] = set()
    while True:
        arguments: dict[str, Any] = {"TableName": table_name, "ConsistentRead": True}
        if exclusive_start_key is not None:
            arguments["ExclusiveStartKey"] = exclusive_start_key
        try:
            response = client.scan(**arguments)
        except Exception as error:
            raise RecoveryProofError("provider_error", "table inventory could not be read") from error
        items = response.get("Items")
        if not isinstance(items, list) or any(not isinstance(item, Mapping) for item in items):
            raise RecoveryProofError("provider_error", "table inventory response is malformed")
        for item in items:
            body = canonical_json(_canonical_item(item))
            digest = sha256_bytes(body)
            digests.append(digest)
            byte_count += len(body)
            expiry = _wire_expiry(item)
            if ttl_cutoff_epoch is not None and expiry is not None and expiry <= ttl_cutoff_epoch:
                ttl_eligible_digests.append(digest)
                ttl_eligible_bytes += len(body)
            else:
                protected_digests.append(digest)
                protected_bytes += len(body)
            if len(digests) > max_items or byte_count > max_bytes:
                raise RecoveryProofError("inventory_limit", "table inventory exceeded its reviewed item or byte cap")
            item_type = _wire_text(item, "item_type") or "unknown"
            item_types[item_type] += 1
            state = _wire_text(item, "status")
            if state is not None:
                states[state] += 1
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        if not isinstance(last_key, Mapping):
            raise RecoveryProofError("provider_error", "table inventory cursor is malformed")
        cursor = canonical_json(_json_safe(last_key))
        if cursor in seen_cursors:
            raise RecoveryProofError("provider_error", "table inventory cursor repeated")
        seen_cursors.add(cursor)
        exclusive_start_key = last_key
    result = {
        **_digest_summary(digests, byte_count),
        "item_types": dict(sorted(item_types.items())),
        "delivery_states": dict(sorted(states.items())),
        "ttl_cutoff_epoch": ttl_cutoff_epoch,
        "protected": _digest_summary(protected_digests, protected_bytes),
        "ttl_eligible_by_deadline": {
            **_digest_summary(ttl_eligible_digests, ttl_eligible_bytes),
            "item_digests": sorted(ttl_eligible_digests),
        },
    }
    return result


def _inventory_matches(observed: Mapping[str, Any], bound: Mapping[str, Any]) -> bool:
    if observed.get("ttl_cutoff_epoch") != bound.get("ttl_cutoff_epoch"):
        return False
    if observed.get("protected") != bound.get("protected"):
        return False
    observed_eligible = observed.get("ttl_eligible_by_deadline")
    bound_eligible = bound.get("ttl_eligible_by_deadline")
    if not isinstance(observed_eligible, Mapping) or not isinstance(bound_eligible, Mapping):
        return False
    observed_digests = observed_eligible.get("item_digests")
    bound_digests = bound_eligible.get("item_digests")
    if not isinstance(observed_digests, list) or not isinstance(bound_digests, list):
        return False
    if any(not isinstance(value, str) or SHA256_RE.fullmatch(value) is None for value in observed_digests):
        return False
    if any(not isinstance(value, str) or SHA256_RE.fullmatch(value) is None for value in bound_digests):
        return False
    if len(observed_digests) != len(set(observed_digests)) or len(bound_digests) != len(set(bound_digests)):
        return False
    return set(observed_digests).issubset(set(bound_digests))


def _observe_table(
    client: Any, table_name: str, *, max_items: int, max_bytes: int, ttl_cutoff_epoch: int
) -> dict[str, Any]:
    table = _describe_optional(client, table_name)
    if table is None:
        raise RecoveryProofError("state_refused", "required source table is missing")
    arn = _safe_text(table.get("TableArn"), "table ARN", status="state_refused")
    pitr = _pitr(client, table_name)
    ttl = _ttl(client, table_name)
    if pitr.get("status") != "ENABLED" or pitr.get("period_days") != RECOVERY_PERIOD_DAYS:
        raise RecoveryProofError("state_refused", "source table PITR does not match ADR-027")
    if ttl != {"status": "ENABLED", "attribute": "expires_at"}:
        raise RecoveryProofError("state_refused", "source table TTL does not match the owned contract")
    return {
        "name": table_name,
        "arn": arn,
        "table_id": _safe_text(table.get("TableId"), "table ID", status="state_refused"),
        "status": table.get("TableStatus"),
        "reported_item_count": table.get("ItemCount"),
        "reported_size_bytes": table.get("TableSizeBytes"),
        "schema": _schema(table),
        "pitr": pitr,
        "ttl": ttl,
        "tags": _tags(client, arn),
        "inventory": _inventory(
            client,
            table_name,
            max_items=max_items,
            max_bytes=max_bytes,
            ttl_cutoff_epoch=ttl_cutoff_epoch,
        ),
    }


def _runtime_controls(clients: AwsClients, context: Mapping[str, Any]) -> dict[str, Any]:
    functions: dict[str, Any] = {}
    primary = context["primary_tables"]
    for kind in ("watcher", "dispatcher", "worker", "reconciler"):
        name = context["function_names"][kind]
        try:
            configuration = clients.lambda_client.get_function_configuration(FunctionName=name)
        except Exception as error:
            raise RecoveryProofError("provider_error", "Lambda runtime configuration could not be read") from error
        variables = configuration.get("Environment", {}).get("Variables", {})
        if not isinstance(variables, Mapping):
            raise RecoveryProofError("state_refused", "Lambda runtime environment is malformed")
        expected = {"DELIVERY_TABLE_NAME": primary["delivery"]}
        if kind == "watcher":
            expected["SOURCE_STATE_TABLE_NAME"] = primary["source_state"]
        if any(variables.get(key) != value for key, value in expected.items()):
            raise RecoveryProofError("state_refused", "Lambda runtime table binding differs from the primary pair")
        functions[kind] = {"name": name, "table_bindings": expected}

    try:
        concurrency = clients.lambda_client.get_function_concurrency(FunctionName=context["function_names"]["watcher"])
    except Exception as error:
        raise RecoveryProofError("provider_error", "watcher concurrency could not be read") from error
    if concurrency.get("ReservedConcurrentExecutions") != 0:
        raise RecoveryProofError("state_refused", "watcher reserved concurrency is not zero")

    rules: dict[str, str] = {}
    for kind in ("watcher", "dispatcher", "reconciler"):
        try:
            rule = clients.events.describe_rule(Name=context["function_names"][kind])
        except Exception as error:
            raise RecoveryProofError("provider_error", "runtime schedule state could not be read") from error
        if rule.get("State") != "DISABLED":
            raise RecoveryProofError("state_refused", "runtime schedules must all be disabled")
        rules[kind] = "DISABLED"

    try:
        mappings = clients.lambda_client.list_event_source_mappings(
            FunctionName=context["function_names"]["worker"], EventSourceArn=context["queue_arn"]
        ).get("EventSourceMappings")
    except Exception as error:
        raise RecoveryProofError("provider_error", "worker event source state could not be read") from error
    if not isinstance(mappings, list) or len(mappings) != 1 or mappings[0].get("State") != "Disabled":
        raise RecoveryProofError("state_refused", "worker event source must be exactly one disabled mapping")

    try:
        queue_url = clients.sqs.get_queue_url(QueueName=context["queue_name"]).get("QueueUrl")
        queue_url = _safe_text(queue_url, "delivery queue URL", status="provider_error")
        attributes = clients.sqs.get_queue_attributes(
            QueueUrl=queue_url,
            AttributeNames=list(QUEUE_COUNT_ATTRIBUTES),
        ).get("Attributes")
    except Exception as error:
        raise RecoveryProofError("provider_error", "delivery queue state could not be read") from error
    if not isinstance(attributes, Mapping):
        raise RecoveryProofError("provider_error", "delivery queue response is malformed")
    if set(attributes) != set(QUEUE_COUNT_ATTRIBUTES):
        raise RecoveryProofError("provider_error", "delivery queue response omitted a requested counter")
    try:
        queue_counts = {key: int(attributes[key]) for key in QUEUE_COUNT_ATTRIBUTES}
    except (TypeError, ValueError) as error:
        raise RecoveryProofError("provider_error", "delivery queue response contains a malformed counter") from error
    if any(value != 0 for value in queue_counts.values()):
        raise RecoveryProofError("state_refused", "delivery queue must be empty")
    return {
        "functions": functions,
        "watcher_reserved_concurrency": 0,
        "schedules": rules,
        "worker_mapping_state": "Disabled",
        "queue_counts": queue_counts,
        "queue_counts_are_approximate": True,
    }


def _target_name(source_name: str, exercise_id: str) -> str:
    value = f"{source_name}-restore-{exercise_id}"
    if TABLE_NAME_RE.fullmatch(value) is None:
        raise RecoveryProofError("invalid_input", "derived recovery table name is invalid")
    return value


def _assert_restore_window(table: Mapping[str, Any], restore_at: datetime) -> None:
    pitr = table["pitr"]
    earliest = _timestamp(pitr.get("earliest"), "earliest restorable time", status="state_refused")
    latest = _timestamp(pitr.get("latest"), "latest restorable time", status="state_refused")
    if not earliest <= restore_at <= latest:
        raise RecoveryProofError("state_refused", "restore timestamp is outside a source table PITR window")


def create_preview(
    clients: AwsClients,
    *,
    deployment_path: Path,
    terraform_output_path: Path,
    exercise_id: str,
    operator: str,
    started_at: str,
    max_inventory_items: int,
    max_inventory_bytes: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    if EXERCISE_ID_RE.fullmatch(exercise_id) is None:
        raise RecoveryProofError("invalid_input", "exercise ID must be 1-32 lowercase letters, digits, or hyphens")
    operator = _safe_text(operator, "operator")
    started = _timestamp(started_at, "started_at")
    observed_at = (now or datetime.now(UTC)).astimezone(UTC)
    if started > observed_at or observed_at - started > timedelta(seconds=CLOCK_SKEW_SECONDS):
        raise RecoveryProofError("clock_refused", "started_at must name the current recovery start within 60 seconds")
    deadline = started + timedelta(seconds=RTO_SECONDS)
    max_inventory_items = _bounded_positive_integer(max_inventory_items, "max inventory items", MAX_INVENTORY_ITEMS)
    max_inventory_bytes = _bounded_positive_integer(max_inventory_bytes, "max inventory bytes", MAX_INVENTORY_BYTES)

    context = _local_context(deployment_path, terraform_output_path)
    aws_identity = _caller_identity(clients.sts, context)
    controls = _runtime_controls(clients, context)

    tables: dict[str, Any] = {}
    targets: dict[str, str] = {}
    for kind, name in context["primary_tables"].items():
        observed = _observe_table(
            clients.dynamodb,
            name,
            max_items=max_inventory_items,
            max_bytes=max_inventory_bytes,
            ttl_cutoff_epoch=int(deadline.timestamp()),
        )
        if observed["status"] != "ACTIVE":
            raise RecoveryProofError("state_refused", "source table is not active")
        if kind == "delivery" and any(
            observed["inventory"]["delivery_states"].get(state, 0) for state in ACTIONABLE_STATES
        ):
            raise RecoveryProofError("state_refused", "delivery table still contains actionable work")
        target = _target_name(name, exercise_id)
        if _describe_optional(clients.dynamodb, target) is not None:
            raise RecoveryProofError("target_exists", "derived recovery table already exists")
        tables[kind] = observed
        targets[kind] = target

    latest_by_table = {
        kind: _timestamp(table["pitr"].get("latest"), f"{kind} latest restorable time", status="state_refused")
        for kind, table in tables.items()
    }
    restore = min(started, *latest_by_table.values())
    for table in tables.values():
        _assert_restore_window(table, restore)
    observed_rpo_seconds = int((started - restore).total_seconds())
    restore_at = restore.isoformat(timespec="seconds").replace("+00:00", "Z")

    return {
        "plan_version": PLAN_VERSION,
        "decision": "ADR-027",
        "exercise_id": exercise_id,
        "operator": operator,
        "started_at": started_at,
        "deadline_at": deadline.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "restore_at": restore_at,
        "rpo_seconds": RPO_SECONDS,
        "rpo_observation": {
            "latest_restorable_times": {
                kind: value.isoformat(timespec="seconds").replace("+00:00", "Z")
                for kind, value in sorted(latest_by_table.items())
            },
            "observed_seconds": observed_rpo_seconds,
            "nominal_target_met": observed_rpo_seconds <= RPO_SECONDS,
            "selection": "shared_provider_latest",
        },
        "rto_seconds": RTO_SECONDS,
        "recovery_period_days": RECOVERY_PERIOD_DAYS,
        "max_inventory_items": max_inventory_items,
        "max_inventory_bytes": max_inventory_bytes,
        "context": context,
        "controls": controls,
        "aws_identity": aws_identity,
        "source_tables": tables,
        "target_tables": targets,
    }


def write_preview(path: Path, plan: Mapping[str, Any]) -> str:
    body = canonical_json(plan) + b"\n"
    _atomic_write(path, body, "recovery plan")
    return sha256_bytes(body)


def load_plan(path: Path, expected_sha256: str) -> dict[str, Any]:
    expected = _sha256(expected_sha256, "expected plan SHA-256", status="stale_plan")
    try:
        body = path.read_bytes()
        plan = load_unique_json(body)
    except Exception as error:
        raise RecoveryProofError("stale_plan", "saved recovery plan is malformed") from error
    if sha256_bytes(body) != expected or not isinstance(plan, dict) or canonical_json(plan) + b"\n" != body:
        raise RecoveryProofError("stale_plan", "saved recovery plan digest or canonical bytes differ")
    if plan.get("plan_version") != PLAN_VERSION or plan.get("decision") != "ADR-027":
        raise RecoveryProofError("stale_plan", "saved recovery plan version or decision differs")
    _validate_saved_plan(plan)
    return plan


def _validate_saved_plan(plan: Mapping[str, Any]) -> None:
    required = {
        "plan_version",
        "decision",
        "exercise_id",
        "operator",
        "started_at",
        "deadline_at",
        "restore_at",
        "rpo_seconds",
        "rpo_observation",
        "rto_seconds",
        "recovery_period_days",
        "max_inventory_items",
        "max_inventory_bytes",
        "context",
        "controls",
        "aws_identity",
        "source_tables",
        "target_tables",
    }
    if set(plan) != required:
        raise RecoveryProofError("stale_plan", "saved recovery plan shape differs")
    if plan.get("rpo_seconds") != RPO_SECONDS or plan.get("rto_seconds") != RTO_SECONDS:
        raise RecoveryProofError("stale_plan", "saved recovery objectives differ")
    if plan.get("recovery_period_days") != RECOVERY_PERIOD_DAYS:
        raise RecoveryProofError("stale_plan", "saved recovery period differs")
    _safe_text(plan.get("operator"), "saved operator", status="stale_plan")
    started = _timestamp(plan.get("started_at"), "saved started_at", status="stale_plan")
    deadline = _timestamp(plan.get("deadline_at"), "saved deadline_at", status="stale_plan")
    restore = _timestamp(plan.get("restore_at"), "saved restore_at", status="stale_plan")
    if deadline != started + timedelta(seconds=RTO_SECONDS):
        raise RecoveryProofError("stale_plan", "saved recovery deadline differs")
    if restore > started:
        raise RecoveryProofError("stale_plan", "saved restore timestamp follows the recovery start")
    for name, maximum in (
        ("max_inventory_items", MAX_INVENTORY_ITEMS),
        ("max_inventory_bytes", MAX_INVENTORY_BYTES),
    ):
        try:
            _bounded_positive_integer(plan.get(name), name, maximum)
        except RecoveryProofError as error:
            raise RecoveryProofError("stale_plan", error.detail) from error

    context = plan.get("context")
    controls = plan.get("controls")
    sources = plan.get("source_tables")
    targets = plan.get("target_tables")
    if not isinstance(context, Mapping):
        raise RecoveryProofError("stale_plan", "saved recovery plan contains a malformed object")
    if not isinstance(controls, Mapping):
        raise RecoveryProofError("stale_plan", "saved recovery plan contains a malformed object")
    if not isinstance(sources, Mapping):
        raise RecoveryProofError("stale_plan", "saved recovery plan contains a malformed object")
    if not isinstance(targets, Mapping):
        raise RecoveryProofError("stale_plan", "saved recovery plan contains a malformed object")
    context_keys = {
        "deployment_id",
        "region",
        "account_id",
        "recovery_role_arn",
        "recovery_evidence_role_arn",
        "git_sha",
        "deployment_path",
        "deployment_sha256",
        "terraform_output_path",
        "terraform_output_sha256",
        "primary_tables",
        "function_names",
        "queue_name",
        "queue_arn",
        "trigger_states",
        "pitr",
    }
    if set(context) != context_keys or context.get("deployment_id") != "dev" or context.get("region") != "us-east-1":
        raise RecoveryProofError("stale_plan", "saved deployment context differs")
    if not isinstance(context.get("account_id"), str) or re.fullmatch(r"[0-9]{12}", context["account_id"]) is None:
        raise RecoveryProofError("stale_plan", "saved account ID differs")
    expected_role_arn = f"arn:aws:iam::{context['account_id']}:role/apcf-dev-dynamodb-recovery"
    if context.get("recovery_role_arn") != expected_role_arn:
        raise RecoveryProofError("stale_plan", "saved recovery role differs")
    expected_evidence_role_arn = f"arn:aws:iam::{context['account_id']}:role/apcf-dev-dynamodb-recovery-evidence"
    if context.get("recovery_evidence_role_arn") != expected_evidence_role_arn:
        raise RecoveryProofError("stale_plan", "saved recovery evidence role differs")
    if not isinstance(context.get("git_sha"), str) or GIT_SHA_RE.fullmatch(context["git_sha"]) is None:
        raise RecoveryProofError("stale_plan", "saved Git SHA differs")
    for name in ("deployment_sha256", "terraform_output_sha256"):
        _sha256(context.get(name), f"saved {name}", status="stale_plan")
    for name in ("deployment_path", "terraform_output_path", "queue_name", "queue_arn"):
        _safe_text(context.get(name), f"saved {name}", status="stale_plan")
    if Path(context["deployment_path"]).resolve() != (ROOT / "infra/central/deployment.yaml").resolve():
        raise RecoveryProofError("stale_plan", "saved central deployment path differs")
    if context.get("pitr") != {
        "cutover_enabled": False,
        "pitr_enabled": True,
        "plan_sha256": None,
        "recovery_period_days": RECOVERY_PERIOD_DAYS,
    }:
        raise RecoveryProofError("stale_plan", "saved PITR context differs")
    aws_identity = plan.get("aws_identity")
    if not isinstance(aws_identity, Mapping) or set(aws_identity) != {"account_id", "arn", "user_id"}:
        raise RecoveryProofError("stale_plan", "saved AWS caller identity differs")
    if aws_identity.get("account_id") != context["account_id"]:
        raise RecoveryProofError("stale_plan", "saved AWS caller account differs")
    for name in ("arn", "user_id"):
        _safe_text(aws_identity.get(name), f"saved AWS caller {name}", status="stale_plan")
    caller_prefix = f"arn:aws:sts::{context['account_id']}:assumed-role/apcf-dev-dynamodb-recovery/"
    caller_arn = str(aws_identity["arn"])
    if not caller_arn.startswith(caller_prefix) or len(caller_arn) == len(caller_prefix):
        raise RecoveryProofError("stale_plan", "saved AWS caller role differs")
    triggers = context.get("trigger_states")
    functions = context.get("function_names")
    expected_triggers = {kind: False for kind in ("watcher", "dispatcher", "worker", "reconciler")}
    if triggers != expected_triggers:
        raise RecoveryProofError("stale_plan", "saved trigger context differs")
    if not isinstance(functions, Mapping) or set(functions) != {
        "watcher",
        "shadow",
        "dispatcher",
        "worker",
        "reconciler",
    }:
        raise RecoveryProofError("stale_plan", "saved function context differs")
    if set(sources) != {"source_state", "delivery"} or set(targets) != {"source_state", "delivery"}:
        raise RecoveryProofError("stale_plan", "saved recovery table set differs")
    primary = context.get("primary_tables")
    if not isinstance(primary, Mapping) or set(primary) != {"source_state", "delivery"}:
        raise RecoveryProofError("stale_plan", "saved primary table set differs")
    exercise_id = plan.get("exercise_id")
    if not isinstance(exercise_id, str) or EXERCISE_ID_RE.fullmatch(exercise_id) is None:
        raise RecoveryProofError("stale_plan", "saved exercise ID differs")
    for kind in ("source_state", "delivery"):
        source = sources[kind]
        target = targets[kind]
        if not isinstance(source, Mapping):
            raise RecoveryProofError("stale_plan", "saved source table observation is malformed")
        if set(source) != {
            "name",
            "arn",
            "table_id",
            "status",
            "reported_item_count",
            "reported_size_bytes",
            "schema",
            "pitr",
            "ttl",
            "tags",
            "inventory",
        }:
            raise RecoveryProofError("stale_plan", "saved source table observation shape differs")
        if not all(isinstance(source.get(name), Mapping) for name in ("schema", "pitr", "ttl", "inventory")):
            raise RecoveryProofError("stale_plan", "saved source table observation object differs")
        if not isinstance(source.get("tags"), list):
            raise RecoveryProofError("stale_plan", "saved source table tags differ")
        if source.get("name") != primary[kind]:
            raise RecoveryProofError("stale_plan", "saved source table identity differs")
        if target != _target_name(str(primary[kind]), exercise_id):
            raise RecoveryProofError("stale_plan", "saved recovery table name differs")

    latest_by_table = {
        kind: _timestamp(
            sources[kind]["pitr"].get("latest"), f"saved {kind} latest restorable time", status="stale_plan"
        )
        for kind in ("source_state", "delivery")
    }
    expected_restore = min(started, *latest_by_table.values())
    observed_rpo_seconds = int((started - expected_restore).total_seconds())
    expected_rpo_observation = {
        "latest_restorable_times": {
            kind: value.isoformat(timespec="seconds").replace("+00:00", "Z")
            for kind, value in sorted(latest_by_table.items())
        },
        "observed_seconds": observed_rpo_seconds,
        "nominal_target_met": observed_rpo_seconds <= RPO_SECONDS,
        "selection": "shared_provider_latest",
    }
    if restore != expected_restore or plan.get("rpo_observation") != expected_rpo_observation:
        raise RecoveryProofError("stale_plan", "saved restore timestamp or recovery-point observation differs")


def _fresh_preconditions(clients: AwsClients, plan: Mapping[str, Any]) -> None:
    context = plan["context"]
    fresh_context = _local_context(Path(context["deployment_path"]), Path(context["terraform_output_path"]))
    if fresh_context != context:
        raise RecoveryProofError("stale_plan", "local or Terraform recovery context differs")
    _caller_identity(clients.sts, context)
    if _runtime_controls(clients, context) != plan["controls"]:
        raise RecoveryProofError("stale_plan", "runtime quiescence controls differ from preview")
    restore = _timestamp(plan["restore_at"], "saved restore_at", status="stale_plan")
    for kind, saved in plan["source_tables"].items():
        fresh = _observe_table(
            clients.dynamodb,
            saved["name"],
            max_items=plan["max_inventory_items"],
            max_bytes=plan["max_inventory_bytes"],
            ttl_cutoff_epoch=int(_timestamp(plan["deadline_at"], "saved deadline_at", status="stale_plan").timestamp()),
        )
        _assert_restore_window(fresh, restore)
        for field in ("name", "arn", "table_id", "status", "schema", "ttl", "tags"):
            if fresh[field] != saved[field]:
                raise RecoveryProofError("stale_plan", f"{kind} source table differs from preview")
        if not _inventory_matches(fresh["inventory"], saved["inventory"]):
            raise RecoveryProofError("stale_plan", f"{kind} source protected inventory differs from preview")
        if fresh["pitr"].get("status") != saved["pitr"].get("status") or fresh["pitr"].get("period_days") != saved[
            "pitr"
        ].get("period_days"):
            raise RecoveryProofError("stale_plan", f"{kind} source PITR policy differs from preview")


def _expected_target_arn(source_arn: str, target_name: str) -> str:
    prefix, separator, _ = source_arn.rpartition("/")
    if not separator or not prefix.endswith(":table"):
        raise RecoveryProofError("stale_plan", "saved source table ARN cannot derive a target ARN")
    return f"{prefix}/{target_name}"


def _event_projection(event: Mapping[str, Any], plan: Mapping[str, Any]) -> tuple[str, dict[str, Any]] | None:
    raw_text = event.get("CloudTrailEvent")
    if not isinstance(raw_text, str):
        raise RecoveryProofError("evidence_conflict", "CloudTrail returned a malformed recovery event")
    try:
        raw = load_unique_json(raw_text.encode("utf-8"))
    except Exception as error:
        raise RecoveryProofError("evidence_conflict", "CloudTrail returned a malformed recovery event") from error
    if not isinstance(raw, Mapping):
        raise RecoveryProofError("evidence_conflict", "CloudTrail returned a malformed recovery event")
    request = raw.get("requestParameters")
    if not isinstance(request, Mapping):
        raise RecoveryProofError("evidence_conflict", "CloudTrail returned a malformed recovery event")
    target_name = request.get("targetTableName")
    kinds = {name: kind for kind, name in plan["target_tables"].items()}
    if not isinstance(target_name, str):
        raise RecoveryProofError("evidence_conflict", "CloudTrail returned a malformed recovery event")
    if target_name not in kinds:
        return None
    kind = kinds[target_name]
    context = plan["context"]
    source = plan["source_tables"][kind]
    event_time = _timestamp(raw.get("eventTime"), "CloudTrail event time", status="evidence_conflict")
    started = _timestamp(plan["started_at"], "saved started_at", status="stale_plan")
    deadline = _timestamp(plan["deadline_at"], "saved deadline_at", status="stale_plan")
    identity = raw.get("userIdentity")
    session = identity.get("sessionContext") if isinstance(identity, Mapping) else None
    issuer = session.get("sessionIssuer") if isinstance(session, Mapping) else None
    expected_role = context["recovery_role_arn"]
    expected_session_prefix = f"arn:aws:sts::{context['account_id']}:assumed-role/{expected_role.rsplit('/', 1)[-1]}/"
    session_arn = identity.get("arn") if isinstance(identity, Mapping) else None
    exact = (
        event.get("EventId") == raw.get("eventID")
        and event.get("EventName") == "RestoreTableToPointInTime"
        and _json_safe(event.get("EventTime")) == _json_safe(event_time)
        and raw.get("eventSource") == "dynamodb.amazonaws.com"
        and raw.get("eventName") == "RestoreTableToPointInTime"
        and raw.get("awsRegion") == context["region"]
        and raw.get("recipientAccountId") == context["account_id"]
        and raw.get("readOnly") is False
        and raw.get("managementEvent") is True
        and raw.get("eventType") == "AwsApiCall"
        and started <= event_time <= deadline
        and isinstance(identity, Mapping)
        and identity.get("type") == "AssumedRole"
        and identity.get("accountId") == context["account_id"]
        and isinstance(session_arn, str)
        and session_arn.startswith(expected_session_prefix)
        and len(session_arn) > len(expected_session_prefix)
        and isinstance(issuer, Mapping)
        and issuer.get("type") == "Role"
        and issuer.get("accountId") == context["account_id"]
        and issuer.get("arn") == expected_role
        and request.get("sourceTableArn") == source["arn"]
        and request.get("targetTableName") == plan["target_tables"][kind]
        and request.get("restoreDateTime") == plan["restore_at"]
        and request.get("billingModeOverride") == "PAY_PER_REQUEST"
        and raw.get("errorCode") is None
        and raw.get("errorMessage") is None
    )
    if not exact:
        raise RecoveryProofError("evidence_conflict", "CloudTrail recovery event differs from the saved plan")
    event_id = _safe_text(raw.get("eventID"), "CloudTrail event ID", status="evidence_conflict")
    request_id = _safe_text(raw.get("requestID"), "CloudTrail request ID", status="evidence_conflict")
    return kind, {
        "event_id": event_id,
        "event_time": _json_safe(event_time),
        "request_id": request_id,
        "event_sha256": sha256_bytes(raw_text.encode("utf-8")),
        "recovery_role_session_arn": session_arn,
        "recovery_role_issuer_arn": expected_role,
        "source_table_arn": source["arn"],
        "target_table_name": target_name,
        "expected_target_table_arn": _expected_target_arn(source["arn"], target_name),
        "restore_at": plan["restore_at"],
        "billing_mode_override": "PAY_PER_REQUEST",
    }


def capture_evidence(
    clients: AwsClients,
    plan: Mapping[str, Any],
    *,
    plan_sha256: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    _validate_saved_plan(plan)
    plan_sha256 = _sha256(plan_sha256, "recovery plan SHA-256", status="stale_plan")
    context = plan["context"]
    fresh_context = _local_context(Path(context["deployment_path"]), Path(context["terraform_output_path"]))
    if fresh_context != context:
        raise RecoveryProofError("stale_plan", "local or Terraform recovery context differs")
    identity = _evidence_caller_identity(clients.sts, context)
    if clients.cloudtrail is None:
        raise RecoveryProofError("provider_error", "CloudTrail client is unavailable")
    observed_at = (now or datetime.now(UTC)).astimezone(UTC)
    started = _timestamp(plan["started_at"], "saved started_at", status="stale_plan")
    deadline = _timestamp(plan["deadline_at"], "saved deadline_at", status="stale_plan")
    if observed_at < started:
        raise RecoveryProofError("state_refused", "evidence capture precedes the recovery start")
    end = min(observed_at, deadline)
    matches: dict[str, list[dict[str, Any]]] = {kind: [] for kind in plan["target_tables"]}
    event_count = 0
    token: str | None = None
    seen_tokens: set[str] = set()
    for _ in range(MAX_CLOUDTRAIL_PAGES):
        arguments: dict[str, Any] = {
            "LookupAttributes": [{"AttributeKey": "EventName", "AttributeValue": "RestoreTableToPointInTime"}],
            "StartTime": started,
            "EndTime": end,
            "MaxResults": CLOUDTRAIL_PAGE_SIZE,
        }
        if token is not None:
            arguments["NextToken"] = token
        try:
            response = clients.cloudtrail.lookup_events(**arguments)
        except Exception as error:
            raise RecoveryProofError("provider_error", "CloudTrail recovery evidence could not be read") from error
        events = response.get("Events") if isinstance(response, Mapping) else None
        if not isinstance(events, list):
            raise RecoveryProofError("provider_error", "CloudTrail recovery evidence response is malformed")
        event_count += len(events)
        if event_count > MAX_CLOUDTRAIL_EVENTS:
            raise RecoveryProofError("evidence_incomplete", "CloudTrail recovery evidence exceeded its event cap")
        for event in events:
            if not isinstance(event, Mapping):
                raise RecoveryProofError("evidence_conflict", "CloudTrail returned a malformed recovery event")
            projected = _event_projection(event, plan)
            if projected is not None:
                kind, value = projected
                matches[kind].append(value)
        next_token = response.get("NextToken")
        if next_token is None:
            break
        if not isinstance(next_token, str) or not next_token or next_token in seen_tokens:
            raise RecoveryProofError("provider_error", "CloudTrail recovery evidence cursor is malformed")
        seen_tokens.add(next_token)
        token = next_token
    else:
        raise RecoveryProofError("evidence_incomplete", "CloudTrail recovery evidence exceeded its page cap")

    if any(len(values) > 1 for values in matches.values()):
        raise RecoveryProofError("evidence_conflict", "CloudTrail returned duplicate recovery evidence")
    if any(len(values) != 1 for values in matches.values()):
        raise RecoveryProofError("evidence_incomplete", "CloudTrail evidence is not yet complete for both tables")
    return {
        "evidence_version": EVIDENCE_VERSION,
        "decision": "ADR-028",
        "plan_sha256": plan_sha256,
        "captured_at": _json_safe(observed_at),
        "verifier_git_sha": context["git_sha"],
        "context": {
            "account_id": context["account_id"],
            "region": context["region"],
            "evidence_role_arn": context["recovery_evidence_role_arn"],
        },
        "aws_identity": identity,
        "events": {kind: values[0] for kind, values in sorted(matches.items())},
    }


def write_evidence(path: Path, evidence: Mapping[str, Any]) -> str:
    body = canonical_json(evidence) + b"\n"
    _atomic_write(path, body, "recovery evidence")
    return sha256_bytes(body)


def _validate_evidence(evidence: Mapping[str, Any], plan: Mapping[str, Any], plan_sha256: str) -> None:
    required = {
        "evidence_version",
        "decision",
        "plan_sha256",
        "captured_at",
        "verifier_git_sha",
        "context",
        "aws_identity",
        "events",
    }
    if set(evidence) != required or evidence.get("evidence_version") != EVIDENCE_VERSION:
        raise RecoveryProofError("stale_evidence", "saved recovery evidence shape or version differs")
    if evidence.get("decision") != "ADR-028" or evidence.get("plan_sha256") != plan_sha256:
        raise RecoveryProofError("stale_evidence", "saved recovery evidence plan identity differs")
    captured_at = _timestamp(evidence.get("captured_at"), "saved evidence capture time", status="stale_evidence")
    context = plan["context"]
    if evidence.get("verifier_git_sha") != context["git_sha"]:
        raise RecoveryProofError("stale_evidence", "saved recovery evidence Git identity differs")
    expected_context = {
        "account_id": context["account_id"],
        "region": context["region"],
        "evidence_role_arn": context["recovery_evidence_role_arn"],
    }
    if evidence.get("context") != expected_context:
        raise RecoveryProofError("stale_evidence", "saved recovery evidence context differs")
    identity = evidence.get("aws_identity")
    if not isinstance(identity, Mapping) or set(identity) != {"account_id", "arn", "user_id"}:
        raise RecoveryProofError("stale_evidence", "saved recovery evidence caller differs")
    role_name = context["recovery_evidence_role_arn"].rsplit("/", 1)[-1]
    identity_prefix = f"arn:aws:sts::{context['account_id']}:assumed-role/{role_name}/"
    if (
        identity.get("account_id") != context["account_id"]
        or not isinstance(identity.get("arn"), str)
        or not identity["arn"].startswith(identity_prefix)
        or len(identity["arn"]) == len(identity_prefix)
    ):
        raise RecoveryProofError("stale_evidence", "saved recovery evidence caller role differs")
    _safe_text(identity.get("user_id"), "saved evidence caller user ID", status="stale_evidence")
    events = evidence.get("events")
    if not isinstance(events, Mapping) or set(events) != {"source_state", "delivery"}:
        raise RecoveryProofError("stale_evidence", "saved recovery evidence table set differs")
    started = _timestamp(plan["started_at"], "saved started_at", status="stale_plan")
    deadline = _timestamp(plan["deadline_at"], "saved deadline_at", status="stale_plan")
    if captured_at < started:
        raise RecoveryProofError("stale_evidence", "saved recovery evidence predates the recovery start")
    recovery_role_name = context["recovery_role_arn"].rsplit("/", 1)[-1]
    recovery_prefix = f"arn:aws:sts::{context['account_id']}:assumed-role/{recovery_role_name}/"
    event_keys = {
        "event_id",
        "event_time",
        "request_id",
        "event_sha256",
        "recovery_role_session_arn",
        "recovery_role_issuer_arn",
        "source_table_arn",
        "target_table_name",
        "expected_target_table_arn",
        "restore_at",
        "billing_mode_override",
    }
    for kind, event in events.items():
        if not isinstance(event, Mapping) or set(event) != event_keys:
            raise RecoveryProofError("stale_evidence", "saved recovery event shape differs")
        event_time = _timestamp(event.get("event_time"), "saved recovery event time", status="stale_evidence")
        source_arn = plan["source_tables"][kind]["arn"]
        target_name = plan["target_tables"][kind]
        session_arn = event.get("recovery_role_session_arn")
        if (
            not started <= event_time <= deadline
            or event.get("recovery_role_issuer_arn") != context["recovery_role_arn"]
            or not isinstance(session_arn, str)
            or not session_arn.startswith(recovery_prefix)
            or len(session_arn) == len(recovery_prefix)
            or event.get("source_table_arn") != source_arn
            or event.get("target_table_name") != target_name
            or event.get("expected_target_table_arn") != _expected_target_arn(source_arn, target_name)
            or event.get("restore_at") != plan["restore_at"]
            or event.get("billing_mode_override") != "PAY_PER_REQUEST"
        ):
            raise RecoveryProofError("stale_evidence", "saved recovery event identity differs")
        for name in ("event_id", "request_id"):
            _safe_text(event.get(name), f"saved recovery {name}", status="stale_evidence")
        _sha256(event.get("event_sha256"), "saved recovery event SHA-256", status="stale_evidence")


def load_evidence(
    path: Path,
    expected_sha256: str,
    *,
    plan: Mapping[str, Any],
    plan_sha256: str,
) -> dict[str, Any]:
    expected = _sha256(expected_sha256, "expected evidence SHA-256", status="stale_evidence")
    try:
        body = path.read_bytes()
        evidence = load_unique_json(body)
    except Exception as error:
        raise RecoveryProofError("stale_evidence", "saved recovery evidence is malformed") from error
    if sha256_bytes(body) != expected or not isinstance(evidence, dict) or canonical_json(evidence) + b"\n" != body:
        raise RecoveryProofError("stale_evidence", "saved recovery evidence digest or canonical bytes differ")
    _validate_evidence(evidence, plan, plan_sha256)
    return evidence


def _restore_identity(
    table: Mapping[str, Any],
    source_arn: str,
    restore_at: str,
    target_name: str,
    evidence_event: Mapping[str, Any] | None = None,
) -> bool:
    summary = table.get("RestoreSummary")
    if summary is not None:
        if not isinstance(summary, Mapping) or summary.get("SourceTableArn") != source_arn:
            return False
        return _json_safe(summary.get("RestoreDateTime")) == restore_at
    if evidence_event is None:
        return False
    return (
        evidence_event.get("source_table_arn") == source_arn
        and evidence_event.get("restore_at") == restore_at
        and evidence_event.get("target_table_name") == target_name
        and table.get("TableName") == target_name
        and table.get("TableArn") == evidence_event.get("expected_target_table_arn")
    )


def _start_restore(
    client: Any,
    *,
    source: Mapping[str, Any],
    target_name: str,
    restore_at: str,
    evidence_event: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    existing = _describe_optional(client, target_name)
    if existing is not None:
        if not _restore_identity(existing, source["arn"], restore_at, target_name, evidence_event):
            if existing.get("RestoreSummary") is None and evidence_event is None:
                raise RecoveryProofError("evidence_required", "existing recovery table requires exact ADR-028 evidence")
            raise RecoveryProofError("target_conflict", "existing recovery table has a different restore identity")
        return {"status": "observed", "table_status": existing.get("TableStatus")}
    try:
        response = client.restore_table_to_point_in_time(
            SourceTableArn=source["arn"],
            TargetTableName=target_name,
            RestoreDateTime=_timestamp(restore_at, "saved restore_at", status="stale_plan"),
            BillingModeOverride="PAY_PER_REQUEST",
        )
    except Exception as error:
        reread = _describe_optional(client, target_name)
        if reread is not None and _restore_identity(reread, source["arn"], restore_at, target_name):
            return {"status": "accepted_after_reread", "table_status": reread.get("TableStatus")}
        raise RecoveryProofError(
            "ambiguous", "restore response failed without an exact destination read-back"
        ) from error
    table = response.get("TableDescription")
    if not isinstance(table, Mapping) or not _restore_identity(table, source["arn"], restore_at, target_name):
        raise RecoveryProofError("ambiguous", "restore response omitted the exact destination identity")
    return {"status": "accepted", "table_status": table.get("TableStatus")}


def _configure_target(
    client: Any, *, table: Mapping[str, Any], expected_tags: Sequence[Mapping[str, str]]
) -> list[str]:
    name = table["TableName"]
    arn = table["TableArn"]
    changes: list[str] = []
    ttl = _ttl(client, name)
    if ttl["status"] == "DISABLED":
        client.update_time_to_live(
            TableName=name,
            TimeToLiveSpecification={"Enabled": True, "AttributeName": "expires_at"},
        )
        changes.append("ttl")
    elif ttl == {"status": "ENABLING", "attribute": "expires_at"}:
        pass
    elif ttl != {"status": "ENABLED", "attribute": "expires_at"}:
        raise RecoveryProofError("target_incomplete", "restored table TTL is in a conflicting transition")
    pitr = _pitr(client, name)
    if pitr.get("status") != "ENABLED" or pitr.get("period_days") != RECOVERY_PERIOD_DAYS:
        client.update_continuous_backups(
            TableName=name,
            PointInTimeRecoverySpecification={
                "PointInTimeRecoveryEnabled": True,
                "RecoveryPeriodInDays": RECOVERY_PERIOD_DAYS,
            },
        )
        changes.append("pitr")
    current = {item["Key"]: item["Value"] for item in _tags(client, arn)}
    expected = {item["Key"]: item["Value"] for item in expected_tags}
    missing = [{"Key": key, "Value": value} for key, value in sorted(expected.items()) if current.get(key) != value]
    extra = sorted(set(current) - set(expected))
    if missing:
        client.tag_resource(ResourceArn=arn, Tags=missing)
        changes.append("tags")
    if extra:
        client.untag_resource(ResourceArn=arn, TagKeys=extra)
        changes.append("tags")
    return sorted(set(changes))


def status_plan(
    clients: AwsClients,
    plan: Mapping[str, Any],
    *,
    now: datetime | None = None,
    verify_preconditions: bool = True,
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    _validate_saved_plan(plan)
    if verify_preconditions:
        _fresh_preconditions(clients, plan)
    deadline = _timestamp(plan["deadline_at"], "saved deadline_at", status="stale_plan")
    observed_at = (now or datetime.now(UTC)).astimezone(UTC)
    results: dict[str, Any] = {}
    complete = True
    for kind, source in plan["source_tables"].items():
        target_name = plan["target_tables"][kind]
        target = _describe_optional(clients.dynamodb, target_name)
        if target is None:
            results[kind] = {"status": "missing"}
            complete = False
            continue
        evidence_event = evidence["events"][kind] if evidence is not None else None
        if not _restore_identity(target, source["arn"], plan["restore_at"], target_name, evidence_event):
            if target.get("RestoreSummary") is None and evidence_event is None:
                results[kind] = {
                    "status": "identity_unproved",
                    "table_status": str(target.get("TableStatus")).lower(),
                }
                complete = False
                continue
            raise RecoveryProofError("target_conflict", "recovery table restore identity differs from the plan")
        table_status = target.get("TableStatus")
        result: dict[str, Any] = {"status": str(table_status).lower()}
        if table_status == "ACTIVE":
            arn = _safe_text(target.get("TableArn"), "target table ARN", status="target_incomplete")
            result.update(
                {
                    "schema_matches": _schema(target) == source["schema"],
                    "ttl": _ttl(clients.dynamodb, target_name),
                    "pitr": _pitr(clients.dynamodb, target_name),
                    "tags_match": _tags(clients.dynamodb, arn) == source["tags"],
                    "inventory": _inventory(
                        clients.dynamodb,
                        target_name,
                        max_items=plan["max_inventory_items"],
                        max_bytes=plan["max_inventory_bytes"],
                        ttl_cutoff_epoch=int(deadline.timestamp()),
                    ),
                }
            )
            result["inventory_matches"] = _inventory_matches(result["inventory"], source["inventory"])
            result["ttl_eligible_removed_count"] = max(
                0,
                source["inventory"]["ttl_eligible_by_deadline"]["item_count"]
                - result["inventory"]["ttl_eligible_by_deadline"]["item_count"],
            )
            exact = (
                result["schema_matches"]
                and result["ttl"] == {"status": "ENABLED", "attribute": "expires_at"}
                and result["pitr"].get("status") == "ENABLED"
                and result["pitr"].get("period_days") == RECOVERY_PERIOD_DAYS
                and result["tags_match"]
                and result["inventory_matches"]
            )
            result["status"] = "verified" if exact else "incomplete"
            complete = complete and exact
        else:
            complete = False
        results[kind] = result
    deadline_met = observed_at <= deadline
    classification = "completed" if complete and deadline_met else "incomplete"
    return {
        "restore_stage_status": classification,
        "exercise_status": "incomplete_pending_cutover_rollback_and_trigger_restoration",
        "deadline_met": deadline_met,
        "tables": results,
        "cutover_input": (
            {
                "source_state_table": plan["target_tables"]["source_state"],
                "delivery_table": plan["target_tables"]["delivery"],
            }
            if classification == "completed"
            else None
        ),
    }


def apply_plan(
    clients: AwsClients,
    plan: Mapping[str, Any],
    *,
    plan_sha256: str,
    now: datetime | None = None,
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    _validate_saved_plan(plan)
    _fresh_preconditions(clients, plan)
    if (now or datetime.now(UTC)).astimezone(UTC) > _timestamp(
        plan["deadline_at"], "saved deadline_at", status="stale_plan"
    ):
        return {
            "status": "rto_refused",
            "plan_sha256": plan_sha256,
            "detail": "the accepted 4-hour recovery-time target expired before restore start",
        }
    outcomes: dict[str, Any] = {}
    started: list[str] = []
    try:
        for kind, source in plan["source_tables"].items():
            evidence_event = evidence["events"][kind] if evidence is not None else None
            outcome = _start_restore(
                clients.dynamodb,
                source=source,
                target_name=plan["target_tables"][kind],
                restore_at=plan["restore_at"],
                evidence_event=evidence_event,
            )
            outcomes[kind] = outcome
            if outcome["status"] in {"accepted", "accepted_after_reread"}:
                started.append(kind)
    except RecoveryProofError as error:
        return {
            "status": "partial" if started else error.status,
            "plan_sha256": plan_sha256,
            "tables": outcomes,
            "started": started,
            "detail": error.detail,
        }
    except Exception:  # noqa: BLE001 - a restore may already have started
        return {
            "status": "partial" if started else "ambiguous",
            "plan_sha256": plan_sha256,
            "tables": outcomes,
            "started": started,
            "detail": "restore failed without a safe complete result",
        }

    changes: dict[str, list[str]] = {}
    try:
        for kind, source in plan["source_tables"].items():
            target = _describe_optional(clients.dynamodb, plan["target_tables"][kind])
            if target is not None and target.get("TableStatus") == "ACTIVE":
                changes[kind] = _configure_target(clients.dynamodb, table=target, expected_tags=source["tags"])
    except RecoveryProofError as error:
        return {
            "status": "partial",
            "plan_sha256": plan_sha256,
            "restore_outcomes": outcomes,
            "configuration_changes": changes,
            "detail": error.detail,
        }
    except Exception:  # noqa: BLE001 - a setting write may already have succeeded
        return {
            "status": "partial",
            "plan_sha256": plan_sha256,
            "restore_outcomes": outcomes,
            "configuration_changes": changes,
            "detail": "restored-table configuration failed without a safe complete result",
        }
    status = status_plan(clients, plan, verify_preconditions=False, evidence=evidence)
    return {
        **status,
        "plan_sha256": plan_sha256,
        "restore_outcomes": outcomes,
        "configuration_changes": changes,
        "cutover_input": (
            {**status["cutover_input"], "plan_sha256": plan_sha256} if status["cutover_input"] is not None else None
        ),
    }


def _clients(region: str) -> AwsClients:
    return AwsClients(
        sts=boto3.client("sts", region_name=region),
        dynamodb=boto3.client("dynamodb", region_name=region),
        lambda_client=boto3.client("lambda", region_name=region),
        events=boto3.client("events", region_name=region),
        sqs=boto3.client("sqs", region_name=region),
        cloudtrail=boto3.client("cloudtrail", region_name=region),
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)

    preview = subparsers.add_parser("preview", help="write one canonical read-only recovery plan")
    preview.add_argument("--deployment", type=Path, required=True)
    preview.add_argument("--terraform-output", type=Path, required=True)
    preview.add_argument("--exercise-id", required=True)
    preview.add_argument("--operator", required=True)
    preview.add_argument("--started-at", required=True)
    preview.add_argument("--max-inventory-items", type=int, required=True)
    preview.add_argument("--max-inventory-bytes", type=int, required=True)
    preview.add_argument("--plan", type=Path, required=True)

    evidence = subparsers.add_parser("evidence", help="write exact read-only CloudTrail recovery evidence")
    evidence.add_argument("--plan", type=Path, required=True)
    evidence.add_argument("--expected-plan-sha256", required=True)
    evidence.add_argument("--evidence", type=Path, required=True)

    for action in ("apply", "status"):
        command = subparsers.add_parser(action, help=f"{action} one exact saved recovery plan")
        command.add_argument("--plan", type=Path, required=True)
        command.add_argument("--expected-plan-sha256", required=True)
        command.add_argument("--evidence", type=Path)
        command.add_argument("--expected-evidence-sha256")
    return parser.parse_args(argv)


def _write(value: Mapping[str, Any], *, stream: Any = sys.stdout) -> None:
    stream.write(canonical_json(_json_safe(value)).decode("utf-8") + "\n")


def main(argv: Sequence[str] | None = None, *, clients: AwsClients | None = None) -> int:
    arguments = parse_args(argv)
    try:
        if arguments.action == "preview":
            active_clients = clients or _clients("us-east-1")
            plan = create_preview(
                active_clients,
                deployment_path=arguments.deployment,
                terraform_output_path=arguments.terraform_output,
                exercise_id=arguments.exercise_id,
                operator=arguments.operator,
                started_at=arguments.started_at,
                max_inventory_items=arguments.max_inventory_items,
                max_inventory_bytes=arguments.max_inventory_bytes,
            )
            digest = write_preview(arguments.plan, plan)
            _write({"status": "previewed", "plan_sha256": digest, "targets": plan["target_tables"]})
            return 0
        plan = load_plan(arguments.plan, arguments.expected_plan_sha256)
        active_clients = clients or _clients(plan["context"]["region"])
        if arguments.action == "evidence":
            evidence = capture_evidence(
                active_clients,
                plan,
                plan_sha256=arguments.expected_plan_sha256,
            )
            digest = write_evidence(arguments.evidence, evidence)
            _write({"status": "evidence_captured", "evidence_sha256": digest})
            return 0
        evidence_path = arguments.evidence
        evidence_sha256 = arguments.expected_evidence_sha256
        if (evidence_path is None) != (evidence_sha256 is None):
            raise RecoveryProofError(
                "invalid_input", "evidence path and expected evidence SHA-256 must be supplied together"
            )
        saved_evidence = (
            load_evidence(
                evidence_path,
                evidence_sha256,
                plan=plan,
                plan_sha256=arguments.expected_plan_sha256,
            )
            if evidence_path is not None and evidence_sha256 is not None
            else None
        )
        if arguments.action == "status":
            result = status_plan(active_clients, plan, evidence=saved_evidence)
        else:
            result = apply_plan(
                active_clients,
                plan,
                plan_sha256=arguments.expected_plan_sha256,
                evidence=saved_evidence,
            )
        _write(result)
        result_status = result.get("restore_stage_status", result.get("status"))
        if result_status == "completed":
            return 0
        if result_status in {"ambiguous", "partial"}:
            return EXIT_AMBIGUOUS
        return EXIT_REFUSED
    except RecoveryProofError as error:
        _write({"status": error.status, "detail": error.detail}, stream=sys.stderr)
        if error.status in {"invalid_input", "local_write_failed"}:
            return EXIT_INVALID
        if error.status in {"ambiguous", "provider_error"}:
            return EXIT_AMBIGUOUS
        return EXIT_REFUSED
    except Exception:  # noqa: BLE001 - provider details do not belong in bounded CLI output
        _write(
            {"status": "provider_error", "detail": "recovery proof failed without a safe bounded result"},
            stream=sys.stderr,
        )
        return EXIT_AMBIGUOUS


if __name__ == "__main__":
    raise SystemExit(main())
