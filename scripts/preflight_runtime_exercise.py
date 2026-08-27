#!/usr/bin/env python3
"""Preview and run the isolated ADR-024 recovery and load exercises.

The command never creates infrastructure. Terraform creation, trigger changes,
and destroy plans remain separate reviewed mutations. This runner binds one
saved exercise plan to the live preflight stack before it seeds any owned
application state.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import boto3
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import validate_config as validator  # noqa: E402

from aws_public_change_feed.announcements import NormalizedAnnouncement, Provenance  # noqa: E402
from aws_public_change_feed.candidates import build_candidate  # noqa: E402
from aws_public_change_feed.loading import LoadedRelease, load_active_release  # noqa: E402
from aws_public_change_feed.matching import (  # noqa: E402
    Announcement,
    load_risk_rules,
    load_services,
    match_announcement,
)
from aws_public_change_feed.outbox import (  # noqa: E402
    DeliveryRecord,
    DynamoDBDeliveryStore,
    build_delivery_request,
)
from aws_public_change_feed.profiles import route_audiences  # noqa: E402
from aws_public_change_feed.releases import S3ObjectStore  # noqa: E402

PLAN_VERSION = 1
DEPLOYMENT_ID = "preflight"
REGION = "us-east-1"
STATE_KEY = "apcf/preflight/terraform.tfstate"
PERSISTENT_CHANNEL = "#aws-change-alerts-dev"
LOAD_PER_MINUTE = 5
LOAD_MINUTES = 10
LOAD_TOTAL = LOAD_PER_MINUTE * LOAD_MINUTES
LOAD_DRAIN_SECONDS = 300
RECOVERY_TOTAL = 2
QUEUE_ATTRIBUTES = (
    "ApproximateNumberOfMessages",
    "ApproximateNumberOfMessagesNotVisible",
    "ApproximateNumberOfMessagesDelayed",
)
PERSISTENT_QUEUE_ATTRIBUTES = (
    "QueueArn",
    "VisibilityTimeout",
    "RedrivePolicy",
    "RedriveAllowPolicy",
    "KmsMasterKeyId",
    "KmsDataKeyReusePeriodSeconds",
    "FifoQueue",
    "ContentBasedDeduplication",
    "Policy",
)


class ExerciseError(RuntimeError):
    """A bounded refusal with a stable machine-readable classification."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class AwsClients:
    sts: Any
    iam: Any
    s3: Any
    lambda_client: Any
    dynamodb: Any
    sqs: Any
    cloudwatch: Any
    logs: Any


def canonical_json(document: Mapping[str, Any]) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _read_mapping(path: Path, kind: str) -> tuple[Mapping[str, Any], bytes]:
    try:
        body = path.read_bytes()
        loaded = json.loads(body) if path.suffix == ".json" else yaml.safe_load(body)
    except (OSError, json.JSONDecodeError, yaml.YAMLError) as error:
        raise ExerciseError("invalid_input", f"{kind} could not be read") from error
    if not isinstance(loaded, Mapping):
        raise ExerciseError("invalid_input", f"{kind} must be an object")
    return loaded, body


def _safe_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or "\n" in value or "\r" in value:
        raise ExerciseError("invalid_input", f"{field} must be nonempty single-line text")
    return value


def _digest(value: object, field: str) -> str:
    text = _safe_text(value, field)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ExerciseError("invalid_input", f"{field} must be a lowercase SHA-256 digest")
    return text


def _output_value(outputs: Mapping[str, Any], name: str) -> Any:
    entry = outputs.get(name)
    if not isinstance(entry, Mapping) or "value" not in entry:
        raise ExerciseError("invalid_input", f"Terraform output {name} is missing")
    return entry["value"]


def _atomic_write(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(json.dumps(document, sort_keys=True, indent=2).encode("utf-8") + b"\n")
    temporary.replace(path)


def _code_sha256(digest: str) -> str:
    return base64.b64encode(bytes.fromhex(digest)).decode("ascii")


def _queue(clients: AwsClients, name: str) -> dict[str, Any]:
    url = _safe_text(clients.sqs.get_queue_url(QueueName=name).get("QueueUrl"), f"{name} queue URL")
    response = clients.sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn", *QUEUE_ATTRIBUTES])
    attributes = response.get("Attributes")
    if not isinstance(attributes, Mapping):
        raise ExerciseError("state_refused", f"{name} queue attributes are missing")
    return {
        "name": name,
        "url": url,
        "arn": _safe_text(attributes.get("QueueArn"), f"{name} queue ARN"),
        "visible": int(attributes.get(QUEUE_ATTRIBUTES[0], -1)),
        "in_flight": int(attributes.get(QUEUE_ATTRIBUTES[1], -1)),
        "delayed": int(attributes.get(QUEUE_ATTRIBUTES[2], -1)),
    }


def _persistent_controls(clients: AwsClients, account: str) -> dict[str, Any]:
    state = clients.s3.head_object(Bucket="apcf-state-dev", Key="apcf/central/terraform.tfstate")
    state_identity = {
        "bucket": "apcf-state-dev",
        "key": "apcf/central/terraform.tfstate",
        "version_id": _safe_text(state.get("VersionId"), "persistent Terraform state VersionId"),
        "etag": _safe_text(state.get("ETag"), "persistent Terraform state ETag"),
        "content_length": state.get("ContentLength"),
    }
    tables = {}
    for table_name in ("apcf-source-state-dev", "apcf-delivery-dev"):
        response = clients.dynamodb.describe_table(TableName=table_name)
        table = response.get("Table")
        if not isinstance(table, Mapping):
            raise ExerciseError("evidence_incomplete", f"persistent table identity is missing for {table_name}")
        indexes = [
            {
                "IndexName": item.get("IndexName"),
                "IndexArn": item.get("IndexArn"),
                "KeySchema": item.get("KeySchema"),
                "Projection": item.get("Projection"),
            }
            for item in table.get("GlobalSecondaryIndexes", [])
        ]
        selected = {
            "TableArn": table.get("TableArn"),
            "TableId": table.get("TableId"),
            "KeySchema": table.get("KeySchema"),
            "AttributeDefinitions": table.get("AttributeDefinitions"),
            "BillingMode": table.get("BillingModeSummary", {}).get("BillingMode"),
            "SSE": {key: table.get("SSEDescription", {}).get(key) for key in ("Status", "SSEType", "KMSMasterKeyArn")},
            "GlobalSecondaryIndexes": indexes,
        }
        tables[table_name] = sha256_bytes(canonical_json(selected))
    queues = {}
    for queue_name in (
        "apcf-delivery-dev.fifo",
        "apcf-delivery-dlq-dev.fifo",
        "apcf-runtime-failures-dev",
    ):
        url = _safe_text(clients.sqs.get_queue_url(QueueName=queue_name).get("QueueUrl"), f"{queue_name} URL")
        response = clients.sqs.get_queue_attributes(
            QueueUrl=url,
            AttributeNames=["All"],
        )
        attributes = response.get("Attributes")
        if not isinstance(attributes, Mapping):
            raise ExerciseError("evidence_incomplete", f"persistent queue configuration is missing for {queue_name}")
        selected = {name: attributes.get(name) for name in PERSISTENT_QUEUE_ATTRIBUTES}
        queues[queue_name] = sha256_bytes(canonical_json(selected))
    alarm_response = clients.cloudwatch.describe_alarms(AlarmNamePrefix="apcf-dev-")
    alarms = alarm_response.get("MetricAlarms")
    if not isinstance(alarms, list) or alarm_response.get("NextToken"):
        raise ExerciseError("evidence_incomplete", "persistent alarm configuration inventory is incomplete")
    dynamic_alarm_fields = {
        "AlarmConfigurationUpdatedTimestamp",
        "StateValue",
        "StateReason",
        "StateReasonData",
        "StateUpdatedTimestamp",
        "StateTransitionedTimestamp",
    }
    alarm_configuration = [
        {key: value for key, value in alarm.items() if key not in dynamic_alarm_fields}
        for alarm in sorted(alarms, key=lambda item: str(item.get("AlarmName")))
    ]
    return {
        "account": account,
        "terraform_state": state_identity,
        "table_configuration_sha256": tables,
        "queue_configuration_sha256": queues,
        "alarm_count": len(alarm_configuration),
        "alarm_configuration_sha256": sha256_bytes(canonical_json({"alarms": alarm_configuration})),
    }


def _persistent_candidate_absence(clients: AwsClients, candidate_ids: Sequence[str]) -> dict[str, Any]:
    store = DynamoDBDeliveryStore(clients.dynamodb, "apcf-delivery-dev", "status-next-action-index")
    found = [
        candidate_id
        for candidate_id in candidate_ids
        if store.get_candidate(candidate_id) is not None or store.get_delivery(candidate_id) is not None
    ]
    return {"checked": len(candidate_ids), "found": found}


def _persistent_boundary_evidence(
    clients: AwsClients,
    plan: Mapping[str, Any],
    candidate_ids: Sequence[str],
) -> dict[str, Any]:
    try:
        controls = _persistent_controls(clients, plan["aws"]["account"])
        candidates = _persistent_candidate_absence(clients, candidate_ids)
    except Exception:
        return {
            "classification": "incomplete",
            "controls_unchanged": None,
            "candidate_absence": None,
        }
    unchanged = controls == plan["persistent_controls"]
    classification = "passed" if unchanged and not candidates["found"] else "failed"
    return {
        "classification": classification,
        "controls_unchanged": unchanged,
        "candidate_absence": candidates,
    }


def _function(clients: AwsClients, *, name: str, role: str, digest: str) -> dict[str, Any]:
    response = clients.lambda_client.get_function(FunctionName=name)
    configuration = response.get("Configuration")
    if not isinstance(configuration, Mapping):
        raise ExerciseError("state_refused", f"Lambda {name} configuration is missing")
    if configuration.get("Role") != role:
        raise ExerciseError("identity_refused", f"Lambda {name} role differs from Terraform output")
    if configuration.get("CodeSha256") != _code_sha256(digest):
        raise ExerciseError("artifact_refused", f"Lambda {name} code digest differs from the reviewed package")
    environment = configuration.get("Environment", {}).get("Variables", {})
    if not isinstance(environment, Mapping):
        raise ExerciseError("state_refused", f"Lambda {name} environment is missing")
    for value in environment.values():
        if isinstance(value, str) and ("apcf-delivery-dev" in value or "apcf-source-state-dev" in value):
            raise ExerciseError("identity_refused", f"Lambda {name} references persistent dev state")
    return {
        "name": name,
        "role": role,
        "code_sha256": str(configuration["CodeSha256"]),
        "state": configuration.get("State"),
        "last_update_status": configuration.get("LastUpdateStatus"),
        "environment_sha256": sha256_bytes(canonical_json(dict(environment))),
    }


def _artifact(
    clients: AwsClients,
    *,
    source_bucket: str,
    catalog_bucket: str,
    prefix: str,
    digest: str,
    version_id: str,
) -> dict[str, Any]:
    key = f"{prefix.rstrip('/')}/{digest}.zip"
    source = clients.s3.head_object(Bucket=source_bucket, Key=key, VersionId=version_id)
    if source.get("VersionId") != version_id or source.get("Metadata", {}).get("sha256") != digest:
        raise ExerciseError("artifact_refused", "persistent application object identity differs")
    catalog = clients.s3.head_object(Bucket=catalog_bucket, Key=key)
    if catalog.get("Metadata", {}).get("sha256") != digest:
        raise ExerciseError("artifact_refused", "isolated catalog mirror lacks the exact digest metadata")
    if source.get("ContentLength") != catalog.get("ContentLength"):
        raise ExerciseError("artifact_refused", "isolated catalog mirror length differs from the persistent object")
    catalog_version_id = _safe_text(catalog.get("VersionId"), "isolated catalog mirror VersionId")
    source_object = clients.s3.get_object(Bucket=source_bucket, Key=key, VersionId=version_id)
    catalog_object = clients.s3.get_object(Bucket=catalog_bucket, Key=key, VersionId=catalog_version_id)
    source_body = source_object.get("Body")
    catalog_body = catalog_object.get("Body")
    if not hasattr(source_body, "read") or not hasattr(catalog_body, "read"):
        raise ExerciseError("artifact_refused", "application package bodies could not be read")
    source_bytes = source_body.read()
    catalog_bytes = catalog_body.read()
    if not isinstance(source_bytes, bytes) or not isinstance(catalog_bytes, bytes):
        raise ExerciseError("artifact_refused", "application package bodies are malformed")
    if sha256_bytes(source_bytes) != digest or sha256_bytes(catalog_bytes) != digest:
        raise ExerciseError("artifact_refused", "application package bytes differ from the reviewed digest")
    return {
        "source_bucket": source_bucket,
        "catalog_bucket": catalog_bucket,
        "key": key,
        "version_id": version_id,
        "catalog_version_id": catalog_version_id,
        "digest": digest,
        "content_length": source.get("ContentLength"),
    }


def _assert_persistent_writes_denied(
    clients: AwsClients,
    *,
    roles: Mapping[str, Any],
    account: str,
    artifact_key: str,
) -> list[dict[str, Any]]:
    actions = [
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:DeleteObjectVersion",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
        "sqs:SendMessage",
    ]
    resources = [
        f"arn:aws:s3:::apcf-config-dev/{artifact_key}",
        f"arn:aws:dynamodb:{REGION}:{account}:table/apcf-source-state-dev",
        f"arn:aws:dynamodb:{REGION}:{account}:table/apcf-delivery-dev",
        f"arn:aws:sqs:{REGION}:{account}:apcf-delivery-dev.fifo",
    ]
    evidence = []
    for role_name, role_value in sorted(roles.items()):
        role_arn = _safe_text(role_value, f"{role_name} role")
        response = clients.iam.simulate_principal_policy(
            PolicySourceArn=role_arn,
            ActionNames=actions,
            ResourceArns=resources,
        )
        results = response.get("EvaluationResults")
        if not isinstance(results, list) or not results:
            raise ExerciseError("iam_refused", f"IAM simulation returned no result for {role_name}")
        allowed = [item for item in results if item.get("EvalDecision") == "allowed"]
        if allowed:
            raise ExerciseError("iam_refused", f"{role_name} can mutate a persistent dev exercise boundary")
        evidence.append(
            {
                "role": role_arn,
                "evaluated_results": len(results),
                "allowed_results": 0,
            }
        )
    return evidence


def _expected_trigger_states(protocol: str) -> dict[str, bool]:
    if protocol == "recovery":
        return {"watcher": False, "dispatcher": False, "worker": False, "reconciler": False}
    if protocol == "load":
        return {"watcher": False, "dispatcher": True, "worker": True, "reconciler": False}
    raise ExerciseError("invalid_input", "protocol must be recovery or load")


def build_preview(
    clients: AwsClients,
    *,
    deployment_path: Path,
    config_path: Path,
    terraform_output_path: Path,
    terraform_plan_path: Path,
    expected_account: str,
    application_digest: str,
    application_version_id: str,
    protocol: str,
) -> dict[str, Any]:
    if not expected_account.isdigit() or len(expected_account) != 12:
        raise ExerciseError("invalid_input", "expected account must be 12 digits")
    digest = _digest(application_digest, "application digest")
    version_id = _safe_text(application_version_id, "application VersionId")
    deployment, deployment_body = _read_mapping(deployment_path, "deployment")
    config, config_body = _read_mapping(config_path, "configuration")
    outputs, outputs_body = _read_mapping(terraform_output_path, "Terraform output")
    try:
        plan_body = terraform_plan_path.read_bytes()
    except OSError as error:
        raise ExerciseError("invalid_input", "Terraform plan could not be read") from error
    if not plan_body:
        raise ExerciseError("invalid_input", "Terraform plan must not be empty")
    try:
        validator.validate_schema(ROOT / "schemas/deployment.schema.json", deployment_path, deployment)
        validator.validate_schema(ROOT / "schemas/config.schema.json", config_path, config)
    except Exception as error:
        raise ExerciseError("invalid_input", "deployment or configuration failed validation") from error
    if deployment.get("deployment_id") != DEPLOYMENT_ID or deployment.get("deployment_region") != REGION:
        raise ExerciseError("identity_refused", "exercise requires the reviewed preflight deployment in us-east-1")
    if deployment.get("config_bucket_name") != f"apcf-config-preflight-{expected_account}":
        raise ExerciseError("identity_refused", "preflight configuration bucket differs from the authorized account")
    if set(config.get("environment_policies", {})) != {"dev"}:
        raise ExerciseError("invalid_input", "first exercise configuration must enable only the reviewed dev policy")
    route = deployment.get("slack", {}).get("routes", {}).get("dev-alerts", {})
    channel = _safe_text(route.get("channel_label"), "private Slack channel")
    destination_key = _safe_text(route.get("destination_key"), "private Slack destination")
    credential_id = _safe_text(route.get("credential_secret_id"), "private Slack credential")
    if channel == PERSISTENT_CHANNEL or not credential_id.startswith("preflight/"):
        raise ExerciseError("identity_refused", "exercise Slack destination is not isolated")
    identity = clients.sts.get_caller_identity()
    if identity.get("Account") != expected_account:
        raise ExerciseError("identity_refused", "caller account differs from the authorized account")
    preflight_identity = _output_value(outputs, "preflight_identity")
    expected_identity = {
        "account_id": expected_account,
        "deployment_id": DEPLOYMENT_ID,
        "region": REGION,
        "config_bucket_name": deployment["config_bucket_name"],
        "state_key": STATE_KEY,
    }
    if preflight_identity != expected_identity:
        raise ExerciseError("identity_refused", "Terraform preflight identity differs from the reviewed boundary")
    application_version = f"sha256:{digest}"
    versions = _output_value(outputs, "application_versions")
    if not isinstance(versions, Mapping) or set(versions.values()) != {application_version}:
        raise ExerciseError("artifact_refused", "runtime application versions are incomplete or inconsistent")
    trigger_states = _output_value(outputs, "runtime_trigger_states")
    if trigger_states != _expected_trigger_states(protocol):
        raise ExerciseError("state_refused", "runtime triggers differ from the selected fixed protocol")
    function_names = _output_value(outputs, "function_names")
    roles = _output_value(outputs, "roles")
    if not isinstance(function_names, Mapping) or not isinstance(roles, Mapping):
        raise ExerciseError("invalid_input", "Terraform function or role outputs are malformed")
    expected_role_keys = {
        "release_publisher",
        "application_artifact_retirement",
        "feed_watcher",
        "outbox_dispatcher",
        "slack_worker",
        "recovery_reconciler",
    }
    if set(roles) != expected_role_keys:
        raise ExerciseError("identity_refused", "Terraform role inventory differs from the isolated boundary")
    role_keys = {
        "watcher": "feed_watcher",
        "dispatcher": "outbox_dispatcher",
        "worker": "slack_worker",
        "reconciler": "recovery_reconciler",
    }
    functions = {}
    for runtime, role_key in role_keys.items():
        expected_name = f"apcf-{DEPLOYMENT_ID}-{'outbox-dispatcher' if runtime == 'dispatcher' else 'slack-worker' if runtime == 'worker' else 'feed-watcher' if runtime == 'watcher' else 'recovery-reconciler'}"
        if function_names.get(runtime) != expected_name:
            raise ExerciseError("identity_refused", f"{runtime} function name differs from the isolated boundary")
        role = _safe_text(roles.get(role_key), f"{runtime} role")
        if f":role/apcf-{DEPLOYMENT_ID}-" not in role:
            raise ExerciseError("identity_refused", f"{runtime} role is not isolated")
        functions[runtime] = _function(clients, name=expected_name, role=role, digest=digest)
    config_bucket = _safe_text(_output_value(outputs, "config_bucket_name"), "configuration bucket")
    source_bucket = _safe_text(_output_value(outputs, "runtime_artifact_bucket_name"), "artifact source bucket")
    if config_bucket != deployment["config_bucket_name"] or source_bucket != "apcf-config-dev":
        raise ExerciseError("identity_refused", "configuration or artifact source bucket differs")
    artifact = _artifact(
        clients,
        source_bucket=source_bucket,
        catalog_bucket=config_bucket,
        prefix=_safe_text(_output_value(outputs, "application_artifact_prefix"), "artifact prefix"),
        digest=digest,
        version_id=version_id,
    )
    iam_denials = _assert_persistent_writes_denied(
        clients,
        roles=roles,
        account=expected_account,
        artifact_key=artifact["key"],
    )
    queues = {
        "delivery": _queue(clients, _safe_text(_output_value(outputs, "delivery_queue"), "delivery queue")),
        "delivery_dlq": _queue(clients, _safe_text(_output_value(outputs, "delivery_dlq"), "delivery DLQ")),
        "runtime_failures": _queue(
            clients, _safe_text(_output_value(outputs, "runtime_failure_queue"), "runtime failure queue")
        ),
    }
    if any(queue[key] != 0 for queue in queues.values() for key in ("visible", "in_flight", "delayed")):
        raise ExerciseError("state_refused", "all isolated queues must be empty at preview")
    table = _safe_text(_output_value(outputs, "delivery_table"), "delivery table")
    source_table = _safe_text(_output_value(outputs, "source_state_table"), "source state table")
    index = _safe_text(_output_value(outputs, "delivery_index_name"), "delivery index")
    if (
        source_table != "apcf-source-state-preflight"
        or table != "apcf-delivery-preflight"
        or index != "status-next-action-index"
    ):
        raise ExerciseError("identity_refused", "runtime stores differ from the isolated boundary")
    store = DynamoDBDeliveryStore(clients.dynamodb, table, index)
    for status in ("pending_queue", "queued", "failed_retryable", "sending"):
        if store.query_state(status, limit=1):
            raise ExerciseError("state_refused", "isolated store contains actionable work")
    release = load_active_release(
        S3ObjectStore(clients.s3, config_bucket),
        pointer_key=str(deployment["active_versions_object_key"]),
        application_version=application_version,
    )
    if release.config != config:
        raise ExerciseError("release_refused", "active isolated release config differs from the reviewed local config")
    persistent_controls = _persistent_controls(clients, expected_account)
    return {
        "plan_version": PLAN_VERSION,
        "protocol": protocol,
        "local": {
            "deployment_path": str(deployment_path.resolve()),
            "deployment_sha256": sha256_bytes(deployment_body),
            "config_path": str(config_path.resolve()),
            "config_sha256": sha256_bytes(config_body),
            "terraform_output_path": str(terraform_output_path.resolve()),
            "terraform_output_sha256": sha256_bytes(outputs_body),
            "terraform_plan_path": str(terraform_plan_path.resolve()),
            "terraform_plan_sha256": sha256_bytes(plan_body),
        },
        "aws": {
            "account": expected_account,
            "caller_arn": _safe_text(identity.get("Arn"), "caller ARN"),
            "region": REGION,
        },
        "identity": expected_identity,
        "persistent_controls": persistent_controls,
        "application": artifact,
        "release": {
            "id": release.release_id,
            "reference": release.reference,
            "config_bucket": config_bucket,
            "pointer_key": str(deployment["active_versions_object_key"]),
        },
        "runtime": {
            "functions": functions,
            "trigger_states": dict(trigger_states),
            "persistent_write_denials": iam_denials,
        },
        "delivery": {
            "source_table": source_table,
            "table": table,
            "index": index,
            "queues": queues,
            "route_id": "dev-alerts",
            "destination_key": destination_key,
            "channel_label": channel,
            "credential_id": credential_id,
        },
        "bounds": {
            "load_per_minute": LOAD_PER_MINUTE,
            "load_minutes": LOAD_MINUTES,
            "load_total": LOAD_TOTAL,
            "load_drain_seconds": LOAD_DRAIN_SECONDS,
            "recovery_total": RECOVERY_TOTAL,
        },
    }


def write_preview(path: Path, plan: Mapping[str, Any]) -> str:
    _atomic_write(path, plan)
    return sha256_bytes(canonical_json(plan))


def load_plan(path: Path, expected_sha256: str) -> Mapping[str, Any]:
    plan, _ = _read_mapping(path, "exercise plan")
    if sha256_bytes(canonical_json(plan)) != _digest(expected_sha256, "expected plan digest"):
        raise ExerciseError("stale_plan", "exercise plan digest differs")
    if plan.get("plan_version") != PLAN_VERSION:
        raise ExerciseError("invalid_input", "unsupported exercise plan version")
    return plan


def _fresh_preview(clients: AwsClients, plan: Mapping[str, Any]) -> Mapping[str, Any]:
    local = plan["local"]
    application = plan["application"]
    return build_preview(
        clients,
        deployment_path=Path(local["deployment_path"]),
        config_path=Path(local["config_path"]),
        terraform_output_path=Path(local["terraform_output_path"]),
        terraform_plan_path=Path(local["terraform_plan_path"]),
        expected_account=plan["aws"]["account"],
        application_digest=application["digest"],
        application_version_id=application["version_id"],
        protocol=plan["protocol"],
    )


def _synthetic_candidate(
    release: LoadedRelease,
    *,
    run_id: str,
    sequence: int,
    created_at: datetime,
    destination_key: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    url = f"https://aws.amazon.com/apcf-preflight-synthetic/{run_id}/{sequence:03d}"
    announcement = NormalizedAnnouncement(
        canonical_url=url,
        title=f"[TEST] Amazon EKS security update exercise {sequence:03d}",
        summary="Synthetic ADR-024 load and recovery evidence. No incident is active and no action is required.",
        observed_at=created_at,
        published_at=created_at,
        provenance=(Provenance(feed_name="aws-security-bulletins", item_url=url),),
    )
    matches = match_announcement(
        Announcement(title=announcement.title, summary=announcement.summary),
        load_services(release.config),
        load_risk_rules(release.config),
    )
    match = next((item for item in matches if item.pair == ("eks", "security")), None)
    if match is None:
        raise ExerciseError("release_refused", "reviewed release does not match the synthetic EKS security fixture")
    audience = next(
        (item for item in route_audiences(release.config, release.inventory, "eks") if item.route_id == "dev-alerts"),
        None,
    )
    if audience is None:
        raise ExerciseError("release_refused", "reviewed release has no isolated dev-alerts audience")
    candidate = build_candidate(
        announcement=announcement,
        match=match,
        audience=audience,
        configuration=release.config,
        release=release.reference,
        created_at=created_at,
    )
    request = build_delivery_request(candidate, destination_key, created_at)
    return candidate, request


def _seed(
    store: DynamoDBDeliveryStore,
    release: LoadedRelease,
    *,
    run_id: str,
    sequence: int,
    created_at: datetime,
    destination_key: str,
    status: str = "pending_queue",
) -> str:
    candidate, request = _synthetic_candidate(
        release,
        run_id=run_id,
        sequence=sequence,
        created_at=created_at,
        destination_key=destination_key,
    )
    due = int(created_at.timestamp())
    fields: dict[str, Any] = {}
    if status == "sending":
        due -= 1
        fields = {"attempt_id": f"preflight-expired-{sequence}", "lease_expires_at": due}
    record = DeliveryRecord(
        candidate_id=candidate["candidate_id"],
        destination_key=destination_key,
        request=request,
        next_action_at=due,
        status=status,
        created_at=candidate["created_at"],
        **fields,
    )
    if not store.put_candidate_if_absent(candidate) or not store.put_delivery_if_absent(record, now=due):
        raise ExerciseError("state_refused", "synthetic candidate identity already exists")
    return candidate["candidate_id"]


def _invoke(clients: AwsClients, function_name: str, event: Mapping[str, Any]) -> Mapping[str, Any]:
    response = clients.lambda_client.invoke(
        FunctionName=function_name,
        InvocationType="RequestResponse",
        Payload=json.dumps(event, sort_keys=True).encode("utf-8"),
    )
    payload = response.get("Payload")
    body = payload.read() if hasattr(payload, "read") else payload
    if response.get("StatusCode") != 200 or response.get("FunctionError") or not isinstance(body, (bytes, bytearray)):
        raise ExerciseError("runtime_unknown", f"Lambda {function_name} invocation was not proved")
    document = json.loads(body)
    if not isinstance(document, Mapping):
        raise ExerciseError("runtime_unknown", f"Lambda {function_name} result is malformed")
    return document


def _schedule_event(plan: Mapping[str, Any], runtime: str, now: datetime) -> dict[str, Any]:
    function_name = plan["runtime"]["functions"][runtime]["name"]
    return {
        "version": "0",
        "id": f"adr024-{runtime}-{int(now.timestamp())}",
        "detail-type": "Scheduled Event",
        "source": "aws.events",
        "account": plan["aws"]["account"],
        "time": now.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "region": plan["aws"]["region"],
        "resources": [f"arn:aws:events:{plan['aws']['region']}:{plan['aws']['account']}:rule/{function_name}"],
        "detail": {},
    }


def _receive_one(clients: AwsClients, queue_url: str) -> Mapping[str, Any]:
    response = clients.sqs.receive_message(
        QueueUrl=queue_url,
        MaxNumberOfMessages=1,
        WaitTimeSeconds=20,
        VisibilityTimeout=300,
        AttributeNames=["All"],
    )
    messages = response.get("Messages", [])
    if not isinstance(messages, list) or len(messages) != 1 or not isinstance(messages[0], Mapping):
        raise ExerciseError("state_refused", "recovery dispatch did not produce exactly one queue message")
    return messages[0]


def _worker_event(message: Mapping[str, Any], queue_arn: str, region: str) -> dict[str, Any]:
    return {
        "Records": [
            {
                "messageId": message["MessageId"],
                "receiptHandle": message["ReceiptHandle"],
                "body": message["Body"],
                "attributes": message.get("Attributes", {}),
                "messageAttributes": {},
                "md5OfBody": message.get("MD5OfBody", ""),
                "eventSource": "aws:sqs",
                "eventSourceARN": queue_arn,
                "awsRegion": region,
            }
        ]
    }


def _recovery_classification(worker_result: Mapping[str, Any], posted: Any, unknown: Any) -> str:
    if (
        worker_result != {"batchItemFailures": []}
        or posted is None
        or posted.status != "posted"
        or posted.network_attempt_count != 1
        or unknown is None
        or unknown.status != "delivery_unknown"
        or unknown.network_attempt_count != 0
    ):
        return "failed"
    return "passed"


def run_recovery(clients: AwsClients, plan: Mapping[str, Any], *, now: datetime) -> dict[str, Any]:
    if _fresh_preview(clients, plan) != plan:
        raise ExerciseError("stale_plan", "live or local inputs changed after preview")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ExerciseError("invalid_input", "exercise clock must be timezone-aware")
    store = DynamoDBDeliveryStore(clients.dynamodb, plan["delivery"]["table"], plan["delivery"]["index"])
    release = load_active_release(
        S3ObjectStore(clients.s3, plan["release"]["config_bucket"]),
        pointer_key=plan["release"]["pointer_key"],
        application_version=f"sha256:{plan['application']['digest']}",
    )
    run_id = f"recovery-{int(now.timestamp())}"
    pending = _seed(
        store,
        release,
        run_id=run_id,
        sequence=1,
        created_at=now,
        destination_key=plan["delivery"]["destination_key"],
    )
    expired = _seed(
        store,
        release,
        run_id=run_id,
        sequence=2,
        created_at=now,
        destination_key=plan["delivery"]["destination_key"],
        status="sending",
    )
    result = _invoke(
        clients,
        plan["runtime"]["functions"]["reconciler"]["name"],
        _schedule_event(plan, "reconciler", now),
    )
    if result.get("repaired") != 2 or result.get("conditional_races") != 0:
        raise ExerciseError("runtime_refused", "reconciler did not repair the exact two-case cohort")
    message = _receive_one(clients, plan["delivery"]["queues"]["delivery"]["url"])
    worker_result = _invoke(
        clients,
        plan["runtime"]["functions"]["worker"]["name"],
        _worker_event(message, plan["delivery"]["queues"]["delivery"]["arn"], plan["aws"]["region"]),
    )
    posted = store.get_delivery(pending)
    unknown = store.get_delivery(expired)
    classification = _recovery_classification(worker_result, posted, unknown)
    if classification == "passed":
        clients.sqs.delete_message(
            QueueUrl=plan["delivery"]["queues"]["delivery"]["url"],
            ReceiptHandle=message["ReceiptHandle"],
        )
    persistent_boundary = _persistent_boundary_evidence(clients, plan, [pending, expired])
    if persistent_boundary["classification"] == "failed":
        classification = "failed"
    elif classification == "passed" and persistent_boundary["classification"] == "incomplete":
        classification = "incomplete"
    return {
        "classification": classification,
        "protocol": "recovery",
        "run_id": run_id,
        "candidate_ids": {"pending_queue": pending, "expired_sending": expired},
        "states": {
            "pending_queue": posted.status if posted else "missing",
            "expired_sending": unknown.status if unknown else "missing",
        },
        "slack_request_counts": {
            "pending_queue": posted.network_attempt_count if posted else None,
            "expired_sending": unknown.network_attempt_count if unknown else None,
        },
        "reconciler": dict(result),
        "persistent_boundary": persistent_boundary,
    }


def _metric_evidence(clients: AwsClients, plan: Mapping[str, Any], start: datetime, end: datetime) -> list[Any]:
    queries = []
    for runtime in ("dispatcher", "worker"):
        function = plan["runtime"]["functions"][runtime]["name"]
        for metric, statistic in (
            ("Invocations", "Sum"),
            ("Duration", "Maximum"),
            ("ConcurrentExecutions", "Maximum"),
            ("Errors", "Sum"),
            ("Throttles", "Sum"),
        ):
            queries.append(
                {
                    "Id": f"{runtime}_{metric.lower()}",
                    "MetricStat": {
                        "Metric": {
                            "Namespace": "AWS/Lambda",
                            "MetricName": metric,
                            "Dimensions": [{"Name": "FunctionName", "Value": function}],
                        },
                        "Period": 60,
                        "Stat": statistic,
                    },
                    "ReturnData": True,
                }
            )
    for table_key in ("source_table", "table"):
        table = plan["delivery"][table_key]
        for metric in ("ReadThrottleEvents", "WriteThrottleEvents"):
            queries.append(
                {
                    "Id": f"{table_key}_{metric.lower()}",
                    "MetricStat": {
                        "Metric": {
                            "Namespace": "AWS/DynamoDB",
                            "MetricName": metric,
                            "Dimensions": [{"Name": "TableName", "Value": table}],
                        },
                        "Period": 60,
                        "Stat": "Sum",
                    },
                    "ReturnData": True,
                }
            )
    for queue_key, queue in plan["delivery"]["queues"].items():
        queries.append(
            {
                "Id": f"{queue_key}_oldest_message_age",
                "MetricStat": {
                    "Metric": {
                        "Namespace": "AWS/SQS",
                        "MetricName": "ApproximateAgeOfOldestMessage",
                        "Dimensions": [{"Name": "QueueName", "Value": queue["name"]}],
                    },
                    "Period": 60,
                    "Stat": "Maximum",
                },
                "ReturnData": True,
            }
        )
    response = clients.cloudwatch.get_metric_data(
        MetricDataQueries=queries,
        StartTime=start,
        EndTime=end,
        ScanBy="TimestampAscending",
    )
    results = response.get("MetricDataResults")
    return _json_safe(list(results)) if isinstance(results, list) else []


def _alarm_evidence(clients: AwsClients, start: datetime, end: datetime) -> list[dict[str, Any]]:
    response = clients.cloudwatch.describe_alarms(AlarmNamePrefix="apcf-preflight-")
    alarms = response.get("MetricAlarms")
    if not isinstance(alarms, list) or not alarms:
        raise ExerciseError("evidence_incomplete", "preflight alarm inventory is missing")
    evidence = []
    for alarm in sorted(alarms, key=lambda item: str(item.get("AlarmName"))):
        name = _safe_text(alarm.get("AlarmName"), "preflight alarm name")
        history_response = clients.cloudwatch.describe_alarm_history(
            AlarmName=name,
            AlarmTypes=["MetricAlarm"],
            HistoryItemType="StateUpdate",
            StartDate=start,
            EndDate=end,
            MaxRecords=100,
            ScanBy="TimestampAscending",
        )
        history = history_response.get("AlarmHistoryItems")
        if not isinstance(history, list) or history_response.get("NextToken"):
            raise ExerciseError("evidence_incomplete", f"alarm history is malformed for {name}")
        evidence.append(
            {
                "name": name,
                "state": alarm.get("StateValue"),
                "state_updated_at": _json_safe(alarm.get("StateUpdatedTimestamp")),
                "transitions": _json_safe(history),
            }
        )
    return evidence


def _log_evidence(clients: AwsClients, plan: Mapping[str, Any], start: datetime, end: datetime) -> dict[str, Any]:
    evidence = {}
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    for runtime in ("dispatcher", "worker"):
        function = plan["runtime"]["functions"][runtime]["name"]
        response = clients.logs.describe_log_streams(
            logGroupName=f"/aws/lambda/{function}",
            orderBy="LastEventTime",
            descending=True,
            limit=50,
        )
        streams = response.get("logStreams")
        if not isinstance(streams, list):
            raise ExerciseError("evidence_incomplete", f"{runtime} log stream inventory is malformed")
        active = [
            {
                "name": stream.get("logStreamName"),
                "first_event_timestamp": stream.get("firstEventTimestamp"),
                "last_event_timestamp": stream.get("lastEventTimestamp"),
            }
            for stream in streams
            if isinstance(stream.get("lastEventTimestamp"), int) and start_ms <= stream["lastEventTimestamp"] <= end_ms
        ]
        evidence[runtime] = {
            "log_group": f"/aws/lambda/{function}",
            "active_streams": active,
            "inventory_truncated": bool(response.get("nextToken")),
        }
    return evidence


def _completion_latencies_ms(records: Sequence[Any], delivery_state_ttl_days: int) -> list[int]:
    latencies = []
    for record in records:
        if record is None or record.status != "posted" or not isinstance(record.expires_at, int):
            continue
        try:
            created_at = datetime.fromisoformat(record.created_at.replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError):
            continue
        completed_at = record.expires_at - delivery_state_ttl_days * 86400
        latency = int((datetime.fromtimestamp(completed_at, UTC) - created_at).total_seconds() * 1000)
        if latency >= 0:
            latencies.append(latency)
    return latencies


def _oldest_unresolved_age_seconds(records: Sequence[Any], observed_at: datetime) -> int:
    ages = []
    for record in records:
        if record is None or record.status in {"posted", "failed_terminal", "delivery_unknown"}:
            continue
        try:
            created_at = datetime.fromisoformat(record.created_at.replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError):
            continue
        ages.append(max(0, int((observed_at - created_at).total_seconds())))
    return max(ages, default=0)


def run_load(
    clients: AwsClients,
    plan: Mapping[str, Any],
    *,
    clock: Callable[[], datetime],
    sleep: Callable[[float], None],
) -> dict[str, Any]:
    if _fresh_preview(clients, plan) != plan:
        raise ExerciseError("stale_plan", "live or local inputs changed after preview")
    store = DynamoDBDeliveryStore(clients.dynamodb, plan["delivery"]["table"], plan["delivery"]["index"])
    release = load_active_release(
        S3ObjectStore(clients.s3, plan["release"]["config_bucket"]),
        pointer_key=plan["release"]["pointer_key"],
        application_version=f"sha256:{plan['application']['digest']}",
    )
    start = clock()
    if start.tzinfo is None or start.utcoffset() is None:
        raise ExerciseError("invalid_input", "exercise clock must be timezone-aware")
    run_id = f"load-{int(start.timestamp())}"
    candidate_ids = []
    for minute in range(LOAD_MINUTES):
        cohort_time = clock()
        for offset in range(LOAD_PER_MINUTE):
            candidate_ids.append(
                _seed(
                    store,
                    release,
                    run_id=run_id,
                    sequence=minute * LOAD_PER_MINUTE + offset + 1,
                    created_at=cohort_time,
                    destination_key=plan["delivery"]["destination_key"],
                )
            )
        if minute < LOAD_MINUTES - 1:
            sleep(60)
    generation_end = start + timedelta(minutes=LOAD_MINUTES)
    remaining_generation = (generation_end - clock()).total_seconds()
    if remaining_generation > 0:
        sleep(remaining_generation)
    deadline = generation_end + timedelta(seconds=LOAD_DRAIN_SECONDS)
    records = [store.get_delivery(candidate) for candidate in candidate_ids]
    outbox_age_samples: list[dict[str, Any]] = [
        {
            "observed_at": clock().astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "oldest_unresolved_seconds": _oldest_unresolved_age_seconds(records, clock()),
        }
    ]
    while any(
        record is None or record.status in {"pending_queue", "queued", "sending", "failed_retryable"}
        for record in records
    ):
        if clock() >= deadline:
            break
        sleep(min(10, max(0, (deadline - clock()).total_seconds())))
        records = [store.get_delivery(candidate) for candidate in candidate_ids]
        observed_at = clock()
        outbox_age_samples.append(
            {
                "observed_at": observed_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                "oldest_unresolved_seconds": _oldest_unresolved_age_seconds(records, observed_at),
            }
        )
    end = clock()
    states = Counter(record.status if record else "missing" for record in records)
    response_classes = Counter(
        str(record.slack_response.get("response_class"))
        for record in records
        if record is not None and isinstance(record.slack_response, Mapping)
    )
    latencies = [
        int(record.slack_response["latency_ms"])
        for record in records
        if record is not None
        and isinstance(record.slack_response, Mapping)
        and isinstance(record.slack_response.get("latency_ms"), int)
    ]
    completion_latencies = _completion_latencies_ms(
        records,
        int(release.config["state_retention"]["delivery_state_ttl_days"]),
    )
    network_attempts = Counter(record.network_attempt_count for record in records if record is not None)
    pace = store.get_pace(plan["delivery"]["destination_key"])
    pacing = (
        {
            "destination_key": pace.destination_key,
            "next_allowed_at": pace.next_allowed_at,
            "last_response_class": pace.last_response_class,
            "version": pace.version,
        }
        if pace is not None
        else None
    )
    terminal_queues = {name: _queue(clients, queue["name"]) for name, queue in plan["delivery"]["queues"].items()}
    metrics = _metric_evidence(clients, plan, start, end)
    alarms = _alarm_evidence(clients, start, end)
    logs = _log_evidence(clients, plan, start, end)
    persistent_boundary = _persistent_boundary_evidence(clients, plan, candidate_ids)
    primary_metric_ids = {
        "dispatcher_invocations",
        "worker_invocations",
        "worker_duration",
        "worker_concurrentexecutions",
        "source_table_readthrottleevents",
        "source_table_writethrottleevents",
        "table_readthrottleevents",
        "table_writethrottleevents",
        "delivery_oldest_message_age",
        "delivery_dlq_oldest_message_age",
        "runtime_failures_oldest_message_age",
    }
    complete_metric_ids = {item.get("Id") for item in metrics if item.get("StatusCode") == "Complete"}
    workload_metric_ids = {
        item.get("Id") for item in metrics if item.get("StatusCode") == "Complete" and item.get("Values")
    }
    throttle_metric_ids = {
        "source_table_readthrottleevents",
        "source_table_writethrottleevents",
        "table_readthrottleevents",
        "table_writethrottleevents",
    }
    throttle_values = [
        value
        for item in metrics
        if item.get("Id") in throttle_metric_ids
        for value in item.get("Values", [])
        if isinstance(value, (int, float))
    ]
    classification = "passed"
    if states != Counter({"posted": LOAD_TOTAL}):
        classification = "failed"
    if any(queue[key] for queue in terminal_queues.values() for key in ("visible", "in_flight", "delayed")):
        classification = "failed"
    if any(value > 0 for value in throttle_values):
        classification = "failed"
    if persistent_boundary["classification"] == "failed":
        classification = "failed"
    if classification == "passed" and (
        not primary_metric_ids.issubset(complete_metric_ids)
        or not {
            "dispatcher_invocations",
            "worker_invocations",
            "worker_duration",
            "worker_concurrentexecutions",
        }.issubset(workload_metric_ids)
        or pace is None
        or len(completion_latencies) != LOAD_TOTAL
        or any(not item["active_streams"] for item in logs.values())
        or persistent_boundary["classification"] == "incomplete"
    ):
        classification = "incomplete"
    return {
        "classification": classification,
        "protocol": "load",
        "run_id": run_id,
        "bounds": dict(plan["bounds"]),
        "created": len(candidate_ids),
        "arrival_rate_per_hour": LOAD_TOTAL * 60 // LOAD_MINUTES,
        "generation_started_at": start.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "observation_ended_at": end.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "states": dict(sorted(states.items())),
        "slack_response_classes": dict(sorted(response_classes.items())),
        "slack_latency_ms": {
            "count": len(latencies),
            "minimum": min(latencies) if latencies else None,
            "maximum": max(latencies) if latencies else None,
        },
        "end_to_end_latency_ms": {
            "count": len(completion_latencies),
            "minimum": min(completion_latencies) if completion_latencies else None,
            "maximum": max(completion_latencies) if completion_latencies else None,
        },
        "network_attempt_counts": {str(key): value for key, value in sorted(network_attempts.items())},
        "destination_pacing": pacing,
        "outbox_age": {
            "samples": outbox_age_samples,
            "maximum_oldest_unresolved_seconds": max(item["oldest_unresolved_seconds"] for item in outbox_age_samples),
        },
        "queues": terminal_queues,
        "metrics": metrics,
        "alarms": alarms,
        "logs": logs,
        "persistent_boundary": persistent_boundary,
        "candidate_ids": candidate_ids,
    }


def build_teardown_preview(
    *,
    terraform_plan_path: Path,
    terraform_plan_json_path: Path,
    expected_account: str,
    config_bucket: str,
) -> dict[str, Any]:
    if config_bucket != f"apcf-config-preflight-{expected_account}":
        raise ExerciseError("identity_refused", "teardown bucket differs from the exact preflight bucket")
    try:
        binary = terraform_plan_path.read_bytes()
    except OSError as error:
        raise ExerciseError("invalid_input", "Terraform destroy plan could not be read") from error
    plan, json_body = _read_mapping(terraform_plan_json_path, "Terraform destroy plan JSON")
    changes = plan.get("resource_changes")
    if not isinstance(changes, list) or not changes:
        raise ExerciseError("invalid_input", "Terraform destroy plan has no resource inventory")
    addresses = []
    for entry in changes:
        if not isinstance(entry, Mapping):
            raise ExerciseError("invalid_input", "Terraform resource change is malformed")
        address = _safe_text(entry.get("address"), "Terraform resource address")
        actions = entry.get("change", {}).get("actions")
        if actions not in (["delete"], ["no-op"]):
            raise ExerciseError("cleanup_refused", f"teardown plan contains non-destroy action for {address}")
        if not (address == "terraform_data.identity_guard" or address.startswith("module.runtime.")):
            raise ExerciseError("cleanup_refused", f"teardown plan escaped the preflight root at {address}")
        addresses.append(address)
    return {
        "plan_version": PLAN_VERSION,
        "kind": "teardown",
        "aws": {"account": expected_account, "region": REGION},
        "identity": {"deployment_id": DEPLOYMENT_ID, "state_key": STATE_KEY, "config_bucket": config_bucket},
        "terraform_plan_path": str(terraform_plan_path.resolve()),
        "terraform_plan_sha256": sha256_bytes(binary),
        "terraform_plan_json_path": str(terraform_plan_json_path.resolve()),
        "terraform_plan_json_sha256": sha256_bytes(json_body),
        "resource_addresses": sorted(addresses),
    }


def _delete_preflight_bucket_versions(s3: Any, bucket: str) -> int:
    objects = []
    key_marker = None
    version_marker = None
    while True:
        arguments: dict[str, Any] = {"Bucket": bucket, "MaxKeys": 1000}
        if key_marker is not None:
            arguments["KeyMarker"] = key_marker
        if version_marker is not None:
            arguments["VersionIdMarker"] = version_marker
        response = s3.list_object_versions(**arguments)
        objects.extend(
            [
                {"Key": item["Key"], "VersionId": item["VersionId"]}
                for group in (response.get("Versions", []), response.get("DeleteMarkers", []))
                for item in group
            ]
        )
        if not response.get("IsTruncated"):
            break
        key_marker = response.get("NextKeyMarker")
        version_marker = response.get("NextVersionIdMarker")
        if not key_marker:
            raise ExerciseError("cleanup_unknown", "preflight bucket pagination marker is missing")
    for start in range(0, len(objects), 1000):
        batch = objects[start : start + 1000]
        result = s3.delete_objects(Bucket=bucket, Delete={"Objects": batch, "Quiet": True})
        if result.get("Errors"):
            raise ExerciseError("cleanup_unknown", "preflight bucket version deletion reported an error")
    remaining = s3.list_object_versions(Bucket=bucket, MaxKeys=1)
    if remaining.get("Versions") or remaining.get("DeleteMarkers") or remaining.get("IsTruncated"):
        raise ExerciseError("cleanup_incomplete", "preflight bucket still has object versions after retirement")
    return len(objects)


def apply_teardown(
    clients: AwsClients,
    manifest: Mapping[str, Any],
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    identity = clients.sts.get_caller_identity()
    if identity.get("Account") != manifest["aws"]["account"]:
        raise ExerciseError("identity_refused", "teardown caller account differs")
    fresh = build_teardown_preview(
        terraform_plan_path=Path(manifest["terraform_plan_path"]),
        terraform_plan_json_path=Path(manifest["terraform_plan_json_path"]),
        expected_account=manifest["aws"]["account"],
        config_bucket=manifest["identity"]["config_bucket"],
    )
    if fresh != manifest:
        raise ExerciseError("stale_plan", "teardown plan changed after preview")
    deleted_versions = _delete_preflight_bucket_versions(clients.s3, manifest["identity"]["config_bucket"])
    completed = run(
        ("terraform", f"-chdir={ROOT / 'infra/preflight'}", "apply", "-input=false", manifest["terraform_plan_path"]),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ExerciseError("cleanup_incomplete", "Terraform destroy apply failed after exact bucket retirement")
    state = run(
        ("terraform", f"-chdir={ROOT / 'infra/preflight'}", "state", "list"),
        capture_output=True,
        text=True,
        check=False,
    )
    if state.returncode != 0 or state.stdout.strip():
        raise ExerciseError("cleanup_incomplete", "preflight Terraform state is not empty after destroy")
    return {"classification": "passed", "deleted_bucket_versions": deleted_versions, "remaining_state": []}


def _clients(region: str) -> AwsClients:
    return AwsClients(
        sts=boto3.client("sts", region_name=region),
        iam=boto3.client("iam", region_name=region),
        s3=boto3.client("s3", region_name=region),
        lambda_client=boto3.client("lambda", region_name=region),
        dynamodb=boto3.client("dynamodb", region_name=region),
        sqs=boto3.client("sqs", region_name=region),
        cloudwatch=boto3.client("cloudwatch", region_name=region),
        logs=boto3.client("logs", region_name=region),
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preview and run isolated ADR-024 runtime exercises.")
    subparsers = parser.add_subparsers(dest="action", required=True)
    preview = subparsers.add_parser("preview")
    preview.add_argument("--protocol", choices=("recovery", "load"), required=True)
    preview.add_argument("--deployment", type=Path, default=ROOT / "infra/preflight/deployment.yaml")
    preview.add_argument("--config", type=Path, default=ROOT / "config/dev.yaml")
    preview.add_argument("--terraform-output", type=Path, required=True)
    preview.add_argument("--terraform-plan", type=Path, required=True)
    preview.add_argument("--expected-account", required=True)
    preview.add_argument("--application-digest", required=True)
    preview.add_argument("--application-version-id", required=True)
    preview.add_argument("--plan", type=Path, required=True)
    apply = subparsers.add_parser("apply")
    apply.add_argument("--plan", type=Path, required=True)
    apply.add_argument("--expected-plan-sha256", required=True)
    apply.add_argument("--evidence", type=Path, required=True)
    teardown = subparsers.add_parser("teardown-preview")
    teardown.add_argument("--terraform-plan", type=Path, required=True)
    teardown.add_argument("--terraform-plan-json", type=Path, required=True)
    teardown.add_argument("--expected-account", required=True)
    teardown.add_argument("--config-bucket", required=True)
    teardown.add_argument("--plan", type=Path, required=True)
    teardown_apply = subparsers.add_parser("teardown-apply")
    teardown_apply.add_argument("--plan", type=Path, required=True)
    teardown_apply.add_argument("--expected-plan-sha256", required=True)
    teardown_apply.add_argument("--evidence", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None, *, clients: AwsClients | None = None) -> int:
    arguments = parse_args(argv)
    active_clients = clients if clients is not None else _clients(REGION)
    try:
        if arguments.action == "preview":
            preview_plan = build_preview(
                active_clients,
                deployment_path=arguments.deployment,
                config_path=arguments.config,
                terraform_output_path=arguments.terraform_output,
                terraform_plan_path=arguments.terraform_plan,
                expected_account=arguments.expected_account,
                application_digest=arguments.application_digest,
                application_version_id=arguments.application_version_id,
                protocol=arguments.protocol,
            )
            digest = write_preview(arguments.plan, preview_plan)
            print(json.dumps({"status": "previewed", "plan_sha256": digest}, sort_keys=True))
            return 0
        if arguments.action == "teardown-preview":
            teardown_plan = build_teardown_preview(
                terraform_plan_path=arguments.terraform_plan,
                terraform_plan_json_path=arguments.terraform_plan_json,
                expected_account=arguments.expected_account,
                config_bucket=arguments.config_bucket,
            )
            digest = write_preview(arguments.plan, teardown_plan)
            print(json.dumps({"status": "previewed", "plan_sha256": digest}, sort_keys=True))
            return 0
        saved_plan = load_plan(arguments.plan, arguments.expected_plan_sha256)
        if arguments.action == "teardown-apply":
            evidence = apply_teardown(active_clients, saved_plan)
        elif saved_plan.get("protocol") == "recovery":
            evidence = run_recovery(active_clients, saved_plan, now=datetime.now(UTC))
        else:
            evidence = run_load(active_clients, saved_plan, clock=lambda: datetime.now(UTC), sleep=time.sleep)
        _atomic_write(arguments.evidence, evidence)
        print(json.dumps({"status": evidence["classification"], "evidence": str(arguments.evidence)}, sort_keys=True))
        return 0 if evidence["classification"] == "passed" else 2
    except ExerciseError as error:
        print(json.dumps({"status": "refused", "code": error.code, "message": str(error)}, sort_keys=True))
        return 2
    except Exception:
        print(json.dumps({"status": "refused", "code": "unexpected_failure"}, sort_keys=True))
        return 2


if __name__ == "__main__":
    sys.exit(main())
