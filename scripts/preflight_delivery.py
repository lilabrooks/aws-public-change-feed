#!/usr/bin/env python3
"""Preview and run one disabled-trigger feed-to-Slack preflight."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import validate_config as validator  # noqa: E402

from aws_public_change_feed.dispatch import validate_delivery_request  # noqa: E402
from aws_public_change_feed.loading import LoadedRelease, load_active_release  # noqa: E402
from aws_public_change_feed.outbox import DynamoDBDeliveryStore  # noqa: E402
from aws_public_change_feed.parsing import load_unique_json  # noqa: E402
from aws_public_change_feed.releases import S3ObjectStore  # noqa: E402
from aws_public_change_feed.semantics import validate_candidate_against_release  # noqa: E402

EXIT_INVALID = 2
EXIT_REFUSED = 3
EXIT_AMBIGUOUS = 4
PLAN_VERSION = 1
SHA256_RE = re.compile(r"[a-f0-9]{64}")
MAX_SAFE_TEXT = 500
ACTIONABLE_STATES = ("pending_queue", "failed_retryable", "queued", "sending")
EXPECTED_HANDLERS = {
    "watcher": "aws_public_change_feed.watcher_runtime.lambda_handler",
    "dispatcher": "aws_public_change_feed.dispatcher_runtime.lambda_handler",
    "worker": "aws_public_change_feed.slack_worker_runtime.lambda_handler",
}
EXPECTED_TIMEOUTS = {"watcher": 300, "dispatcher": 60, "worker": 300}
EXPECTED_SCHEDULES = {
    "watcher": {"expression": "rate(15 minutes)", "maximum_event_age": 900, "maximum_retries": 2},
    "dispatcher": {"expression": "rate(1 minute)", "maximum_event_age": 300, "maximum_retries": 2},
}


class PreflightError(RuntimeError):
    """One bounded operator-facing refusal."""

    def __init__(self, status: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(frozen=True, slots=True)
class AwsClients:
    sts: Any
    lambda_client: Any
    events: Any
    sqs: Any
    dynamodb: Any
    s3: Any
    secretsmanager: Any | None = None
    ssm: Any | None = None


def canonical_json(document: Mapping[str, Any]) -> bytes:
    return json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_text(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_SAFE_TEXT
        or any(character in value for character in "\r\n\x00")
    ):
        raise PreflightError("invalid_input", f"{field} must be a bounded single-line string")
    return value


def _sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise PreflightError("invalid_input", f"{field} must be 64 lowercase hexadecimal characters")
    return value


def _positive_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PreflightError("invalid_input", f"{field} must be a positive integer")
    return value


def _read_mapping(path: Path, kind: str) -> tuple[Mapping[str, Any], bytes]:
    try:
        body = path.read_bytes()
        if path.suffix in (".yaml", ".yml"):
            document = yaml.safe_load(body)
        else:
            document = load_unique_json(body)
    except Exception as error:
        raise PreflightError("invalid_input", f"{kind} document is malformed") from error
    if not isinstance(document, Mapping):
        raise PreflightError("invalid_input", f"{kind} document must be an object")
    return document, body


def _output_value(outputs: Mapping[str, Any], name: str) -> Any:
    output = outputs.get(name)
    if not isinstance(output, Mapping) or set(output) != {"sensitive", "type", "value"}:
        raise PreflightError("invalid_input", f"Terraform output {name} is missing or malformed")
    return output["value"]


def _atomic_write(path: Path, body: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_bytes(body)
        temporary.replace(path)
    except OSError as error:
        raise PreflightError("local_write_failed", "preflight plan could not be written") from error


def _load_local_inputs(
    deployment_path: Path,
    config_path: Path,
    terraform_output_path: Path,
) -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any], dict[str, str]]:
    deployment, deployment_body = _read_mapping(deployment_path, "deployment")
    config, config_body = _read_mapping(config_path, "configuration")
    outputs, outputs_body = _read_mapping(terraform_output_path, "Terraform output")
    try:
        validator.validate_schema(ROOT / "schemas/deployment.schema.json", deployment_path, deployment)
        validator.validate_schema(ROOT / "schemas/config.schema.json", config_path, config)
    except Exception as error:
        raise PreflightError("invalid_input", "deployment or configuration failed validation") from error
    if deployment.get("deployment_id") != "dev":
        raise PreflightError("invalid_input", "D0 preflight requires the reviewed dev deployment")
    if set(config.get("environment_policies", {})) != {"dev"}:
        raise PreflightError("invalid_input", "D0 preflight configuration must enable only dev")
    identities = {
        "deployment_path": str(deployment_path.resolve()),
        "deployment_sha256": sha256_bytes(deployment_body),
        "config_path": str(config_path.resolve()),
        "config_sha256": sha256_bytes(config_body),
        "terraform_output_path": str(terraform_output_path.resolve()),
        "terraform_output_sha256": sha256_bytes(outputs_body),
    }
    return deployment, config, outputs, identities


def _runtime_outputs(
    outputs: Mapping[str, Any], deployment: Mapping[str, Any], application_version: str
) -> dict[str, Any]:
    expected_names = {
        "watcher": f"apcf-{deployment['deployment_id']}-feed-watcher",
        "dispatcher": f"apcf-{deployment['deployment_id']}-outbox-dispatcher",
        "worker": f"apcf-{deployment['deployment_id']}-slack-worker",
    }
    function_names = _output_value(outputs, "function_names")
    roles = _output_value(outputs, "roles")
    trigger_states = _output_value(outputs, "runtime_trigger_states")
    if not isinstance(function_names, Mapping) or any(
        function_names.get(name) != value for name, value in expected_names.items()
    ):
        raise PreflightError("invalid_input", "Terraform function names differ from the reviewed deployment")
    if not isinstance(roles, Mapping):
        raise PreflightError("invalid_input", "Terraform runtime roles are malformed")
    if not isinstance(trigger_states, Mapping) or any(trigger_states.get(name) is not False for name in expected_names):
        raise PreflightError("state_refused", "delivery runtime triggers must all be disabled")
    for output_name in ("worker_application_version", "watcher_application_version", "dispatcher_application_version"):
        if _output_value(outputs, output_name) != application_version:
            raise PreflightError("state_refused", "Terraform runtime package versions differ")
    if _output_value(outputs, "config_bucket_name") != deployment["config_bucket_name"]:
        raise PreflightError("invalid_input", "Terraform configuration bucket differs from deployment")
    if _output_value(outputs, "delivery_table") != f"apcf-delivery-{deployment['deployment_id']}":
        raise PreflightError("invalid_input", "Terraform delivery table differs from deployment")
    if _output_value(outputs, "delivery_index_name") != "status-next-action-index":
        raise PreflightError("invalid_input", "Terraform delivery index differs from the runtime contract")
    return {
        "function_names": {name: str(function_names[name]) for name in expected_names},
        "roles": {
            "watcher": _safe_text(roles.get("feed_watcher"), "watcher role"),
            "dispatcher": _safe_text(roles.get("outbox_dispatcher"), "dispatcher role"),
            "worker": _safe_text(roles.get("slack_worker"), "worker role"),
        },
        "delivery_table": str(_output_value(outputs, "delivery_table")),
        "delivery_index": str(_output_value(outputs, "delivery_index_name")),
        "queue_name": str(_output_value(outputs, "delivery_queue")),
        "queue_arn": str(_output_value(outputs, "delivery_queue_arn")),
        "failure_queue_name": str(_output_value(outputs, "runtime_failure_queue")),
        "config_bucket": str(_output_value(outputs, "config_bucket_name")),
        "trigger_states": {name: False for name in expected_names},
    }


def _code_sha256(digest: str) -> str:
    return base64.b64encode(bytes.fromhex(digest)).decode("ascii")


def _inspect_function(
    clients: AwsClients,
    *,
    name: str,
    function_name: str,
    role: str,
    application_version: str,
    digest: str,
    worker_concurrency: int,
) -> dict[str, Any]:
    configuration = clients.lambda_client.get_function_configuration(FunctionName=function_name)
    if configuration.get("FunctionName") != function_name or configuration.get("FunctionArn") is None:
        raise PreflightError("state_refused", f"{name} function identity differs")
    if configuration.get("State") != "Active" or configuration.get("LastUpdateStatus") != "Successful":
        raise PreflightError("state_refused", f"{name} function is not active and settled")
    if configuration.get("Runtime") != "python3.12" or configuration.get("Handler") != EXPECTED_HANDLERS[name]:
        raise PreflightError("state_refused", f"{name} runtime or handler differs")
    if configuration.get("Role") != role or configuration.get("CodeSha256") != _code_sha256(digest):
        raise PreflightError("state_refused", f"{name} role or package digest differs")
    if configuration.get("Timeout") != EXPECTED_TIMEOUTS[name]:
        raise PreflightError("state_refused", f"{name} timeout differs")
    variables = configuration.get("Environment", {}).get("Variables", {})
    if name in ("watcher", "worker") and variables.get("APPLICATION_VERSION") != application_version:
        raise PreflightError("state_refused", f"{name} injected application version differs")
    expected_concurrency = worker_concurrency if name == "worker" else 1
    concurrency = clients.lambda_client.get_function_concurrency(FunctionName=function_name)
    if concurrency.get("ReservedConcurrentExecutions") != expected_concurrency:
        raise PreflightError("state_refused", f"{name} reserved concurrency differs")
    return {
        "name": function_name,
        "arn": _safe_text(configuration["FunctionArn"], f"{name} function ARN"),
        "role": role,
        "handler": EXPECTED_HANDLERS[name],
        "code_sha256": configuration["CodeSha256"],
        "application_version": application_version,
        "timeout": EXPECTED_TIMEOUTS[name],
        "reserved_concurrency": expected_concurrency,
    }


def _inspect_schedule(
    clients: AwsClients,
    *,
    name: str,
    function: Mapping[str, Any],
    failure_queue_arn: str,
) -> dict[str, Any]:
    rule = clients.events.describe_rule(Name=function["name"])
    expected = EXPECTED_SCHEDULES[name]
    if (
        rule.get("Name") != function["name"]
        or rule.get("State") != "DISABLED"
        or rule.get("ScheduleExpression") != expected["expression"]
    ):
        raise PreflightError("state_refused", f"{name} schedule identity, cadence, or state differs")
    targets = clients.events.list_targets_by_rule(Rule=function["name"], Limit=2).get("Targets", [])
    if not isinstance(targets, list) or len(targets) != 1:
        raise PreflightError("state_refused", f"{name} schedule target differs")
    target = targets[0]
    if (
        target.get("Arn") != function["arn"]
        or target.get("RetryPolicy")
        != {
            "MaximumEventAgeInSeconds": expected["maximum_event_age"],
            "MaximumRetryAttempts": expected["maximum_retries"],
        }
        or target.get("DeadLetterConfig") != {"Arn": failure_queue_arn}
    ):
        raise PreflightError("state_refused", f"{name} schedule target policy differs")
    return {
        "name": str(rule["Name"]),
        "arn": _safe_text(rule.get("Arn"), f"{name} rule ARN"),
        "state": "DISABLED",
        "expression": expected["expression"],
        "maximum_event_age": expected["maximum_event_age"],
        "maximum_retries": expected["maximum_retries"],
        "failure_queue_arn": failure_queue_arn,
    }


def _inspect_worker_mapping(clients: AwsClients, *, function: Mapping[str, Any], queue_arn: str) -> dict[str, Any]:
    response = clients.lambda_client.list_event_source_mappings(
        FunctionName=function["name"], EventSourceArn=queue_arn, MaxItems=2
    )
    mappings = response.get("EventSourceMappings", [])
    if not isinstance(mappings, list) or len(mappings) != 1:
        raise PreflightError("state_refused", "worker event-source mapping is missing or ambiguous")
    mapping = mappings[0]
    if (
        mapping.get("State") != "Disabled"
        or mapping.get("BatchSize") != 10
        or mapping.get("MaximumBatchingWindowInSeconds") != 0
        or mapping.get("ScalingConfig") != {"MaximumConcurrency": function["reserved_concurrency"]}
    ):
        raise PreflightError("state_refused", "worker event-source mapping batching, scaling, or state differs")
    if mapping.get("FunctionResponseTypes") != ["ReportBatchItemFailures"]:
        raise PreflightError("state_refused", "worker event-source response contract differs")
    return {
        "uuid": _safe_text(mapping.get("UUID"), "worker mapping UUID"),
        "state": "Disabled",
        "batch_size": 10,
        "batch_window_seconds": 0,
        "maximum_concurrency": function["reserved_concurrency"],
        "event_source_arn": queue_arn,
    }


def _queue_identity(clients: AwsClients, queue_name: str) -> tuple[str, str, Mapping[str, str]]:
    queue_url = _safe_text(clients.sqs.get_queue_url(QueueName=queue_name).get("QueueUrl"), "queue URL")
    attributes = clients.sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["All"]).get("Attributes", {})
    queue_arn = _safe_text(attributes.get("QueueArn"), "queue ARN")
    return queue_url, queue_arn, attributes


def _queue_state(clients: AwsClients, queue_name: str, queue_arn: str) -> dict[str, Any]:
    queue_url, observed_arn, attributes = _queue_identity(clients, queue_name)
    if (
        observed_arn != queue_arn
        or attributes.get("FifoQueue") != "true"
        or attributes.get("ContentBasedDeduplication") != "false"
        or attributes.get("VisibilityTimeout") != "1800"
    ):
        raise PreflightError("state_refused", "delivery queue identity or runtime settings differ")
    try:
        visible = int(attributes.get("ApproximateNumberOfMessages", "-1"))
        in_flight = int(attributes.get("ApproximateNumberOfMessagesNotVisible", "-1"))
    except (TypeError, ValueError) as error:
        raise PreflightError("state_refused", "delivery queue counts are malformed") from error
    if visible != 0 or in_flight != 0:
        raise PreflightError("state_refused", "delivery queue must be empty before preflight")
    return {"name": queue_name, "url": queue_url, "arn": queue_arn, "visible": 0, "in_flight": 0}


def _require_no_actionable(store: DynamoDBDeliveryStore) -> None:
    for status in ACTIONABLE_STATES:
        if store.query_state(status, limit=1):
            raise PreflightError("state_refused", "actionable delivery state exists before preflight")


def _credential_metadata(clients: AwsClients, deployment: Mapping[str, Any], credential_id: str) -> dict[str, str]:
    store = deployment["secret_store"]
    if store == "secrets_manager":
        if clients.secretsmanager is None:
            raise PreflightError("invalid_input", "Secrets Manager client is unavailable")
        metadata = clients.secretsmanager.describe_secret(SecretId=credential_id)
        if metadata.get("Name") != credential_id:
            raise PreflightError("state_refused", "Slack secret metadata differs")
        return {"store": store, "id": credential_id, "arn": _safe_text(metadata.get("ARN"), "Slack secret ARN")}
    if store == "ssm_parameter_store":
        if clients.ssm is None:
            raise PreflightError("invalid_input", "SSM client is unavailable")
        parameters = clients.ssm.describe_parameters(
            ParameterFilters=[{"Key": "Name", "Option": "Equals", "Values": [credential_id]}], MaxResults=2
        ).get("Parameters", [])
        if not isinstance(parameters, list) or len(parameters) != 1:
            raise PreflightError("state_refused", "Slack parameter metadata is missing or ambiguous")
        parameter = parameters[0]
        if parameter.get("Name") != credential_id or parameter.get("Type") != "SecureString":
            raise PreflightError("state_refused", "Slack parameter metadata differs")
        return {"store": store, "id": credential_id, "arn": f"ssm:{credential_id}"}
    raise PreflightError("invalid_input", "deployment secret store is unsupported")


def _release_route(
    deployment: Mapping[str, Any],
    config: Mapping[str, Any],
    release: LoadedRelease,
) -> tuple[Mapping[str, Any], str, Mapping[str, Any]]:
    feeds = release.config.get("feeds")
    if not isinstance(feeds, list) or len(feeds) != 4:
        raise PreflightError("state_refused", "active release must contain exactly 4 feeds")
    if release.config != config:
        raise PreflightError("state_refused", "active release configuration differs from reviewed local configuration")
    try:
        validator.validate_semantics(deployment, config, release.inventory)
    except Exception as error:
        raise PreflightError(
            "state_refused", "active release inventory differs from the reviewed deployment"
        ) from error
    slack = release.inventory.get("slack", {})
    route_id = slack.get("default_route_id")
    route = slack.get("routes", {}).get(route_id) if isinstance(slack.get("routes"), Mapping) else None
    if not isinstance(route_id, str) or not isinstance(route, Mapping):
        raise PreflightError("state_refused", "active release Slack route is missing")
    return slack, route_id, route


def build_preview(
    clients: AwsClients,
    *,
    deployment_path: Path,
    config_path: Path,
    terraform_output_path: Path,
    expected_account: str,
    application_digest: str,
    candidate_cap: int,
) -> dict[str, Any]:
    expected_account = _safe_text(expected_account, "expected account")
    if not expected_account.isdigit() or len(expected_account) != 12:
        raise PreflightError("invalid_input", "expected account must be 12 digits")
    digest = _sha256(application_digest, "application digest")
    cap = _positive_integer(candidate_cap, "candidate cap")
    if cap != 10:
        raise PreflightError("invalid_input", "D0 candidate cap must equal 10")
    deployment, config, outputs, local_identities = _load_local_inputs(
        deployment_path, config_path, terraform_output_path
    )
    region = str(deployment["deployment_region"])
    if region != "us-east-1":
        raise PreflightError("invalid_input", "D0 preflight requires us-east-1")
    identity = clients.sts.get_caller_identity()
    if identity.get("Account") != expected_account:
        raise PreflightError("identity_refused", "caller account differs from the authorized account")
    caller_arn = _safe_text(identity.get("Arn"), "caller ARN")
    application_version = f"sha256:{digest}"
    runtime = _runtime_outputs(outputs, deployment, application_version)
    worker_concurrency = int(deployment["slack"]["rate_control"]["worker_reserved_concurrency"])
    functions = {
        name: _inspect_function(
            clients,
            name=name,
            function_name=runtime["function_names"][name],
            role=runtime["roles"][name],
            application_version=application_version,
            digest=digest,
            worker_concurrency=worker_concurrency,
        )
        for name in ("watcher", "dispatcher", "worker")
    }
    _, failure_queue_arn, _ = _queue_identity(clients, runtime["failure_queue_name"])
    schedules = {
        name: _inspect_schedule(
            clients,
            name=name,
            function=functions[name],
            failure_queue_arn=failure_queue_arn,
        )
        for name in ("watcher", "dispatcher")
    }
    mapping = _inspect_worker_mapping(clients, function=functions["worker"], queue_arn=runtime["queue_arn"])
    queue = _queue_state(clients, runtime["queue_name"], runtime["queue_arn"])
    store = DynamoDBDeliveryStore(clients.dynamodb, runtime["delivery_table"], runtime["delivery_index"])
    _require_no_actionable(store)
    release = load_active_release(
        S3ObjectStore(clients.s3, runtime["config_bucket"]),
        pointer_key=str(deployment["active_versions_object_key"]),
        application_version=application_version,
    )
    slack, route_id, route = _release_route(deployment, config, release)
    credential_id = _safe_text(route.get("credential_secret_id"), "Slack credential identifier")
    credential = _credential_metadata(clients, deployment, credential_id)
    return {
        "plan_version": PLAN_VERSION,
        "local": local_identities,
        "aws": {"account": expected_account, "caller_arn": caller_arn, "region": region},
        "application": {"digest": digest, "version": application_version},
        "release": {
            "id": release.release_id,
            "reference": release.reference,
            "feed_count": 4,
            "config_bucket": runtime["config_bucket"],
            "pointer_key": str(deployment["active_versions_object_key"]),
        },
        "runtime": {"functions": functions, "schedules": schedules, "worker_mapping": mapping},
        "delivery": {
            "table": runtime["delivery_table"],
            "index": runtime["delivery_index"],
            "queue": queue,
            "route_id": route_id,
            "destination_key": _safe_text(route.get("destination_key"), "Slack destination key"),
            "channel_label": _safe_text(route.get("channel_label"), "Slack channel label"),
            "delivery_mode": _safe_text(slack.get("delivery_mode"), "Slack delivery mode"),
            "credential": credential,
        },
        "bounds": {"candidate_cap": cap, "worker_message_count": 1},
    }


def write_preview(plan_path: Path, plan: Mapping[str, Any]) -> str:
    body = canonical_json(plan) + b"\n"
    _atomic_write(plan_path, body)
    return sha256_bytes(body)


def load_plan(plan_path: Path, expected_digest: str) -> Mapping[str, Any]:
    expected = _sha256(expected_digest, "expected plan digest")
    try:
        body = plan_path.read_bytes()
        plan = load_unique_json(body)
    except Exception as error:
        raise PreflightError("stale_plan", "preflight plan is malformed") from error
    if sha256_bytes(body) != expected or not isinstance(plan, Mapping) or plan.get("plan_version") != PLAN_VERSION:
        raise PreflightError("stale_plan", "preflight plan digest or version differs")
    return plan


def _clients_for_region(region: str) -> AwsClients:
    import boto3

    return AwsClients(
        sts=boto3.client("sts", region_name=region),
        lambda_client=boto3.client("lambda", region_name=region),
        events=boto3.client("events", region_name=region),
        sqs=boto3.client("sqs", region_name=region),
        dynamodb=boto3.client("dynamodb", region_name=region),
        s3=boto3.client("s3", region_name=region),
        secretsmanager=boto3.client("secretsmanager", region_name=region),
        ssm=boto3.client("ssm", region_name=region),
    )


def _payload_bytes(response: Mapping[str, Any]) -> bytes:
    payload = response.get("Payload")
    if payload is not None and hasattr(payload, "read"):
        value = payload.read(1_048_577)
    else:
        value = payload
    if not isinstance(value, bytes) or len(value) > 1_048_576:
        raise PreflightError("provider_error", "Lambda response payload is missing or oversized")
    return value


def _invoke(clients: AwsClients, function_name: str, event: Mapping[str, Any]) -> tuple[Mapping[str, Any] | None, bool]:
    response = clients.lambda_client.invoke(
        FunctionName=function_name,
        InvocationType="RequestResponse",
        Payload=canonical_json(event),
    )
    ambiguous = response.get("StatusCode") != 200 or "FunctionError" in response
    try:
        document = load_unique_json(_payload_bytes(response))
    except Exception:
        return None, True
    return (document if isinstance(document, Mapping) else None), ambiguous


def _schedule_event(plan: Mapping[str, Any], runtime: str, now: datetime) -> dict[str, Any]:
    return {
        "version": "0",
        "id": f"l34-preflight-{runtime}-{int(now.timestamp())}",
        "detail-type": "Scheduled Event",
        "source": "aws.events",
        "account": plan["aws"]["account"],
        "time": now.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "region": plan["aws"]["region"],
        "resources": [plan["runtime"]["schedules"][runtime]["arn"]],
        "detail": {},
    }


def _delivery_ids(store: DynamoDBDeliveryStore, status: str, *, due_before: int, limit: int) -> tuple[str, ...]:
    rows = store.query_due(status, due_before=due_before, limit=limit)
    return tuple(candidate_id for _, candidate_id in rows)


def _await_delivery_ids(
    store: DynamoDBDeliveryStore,
    status: str,
    *,
    due_before: int,
    expected: int,
    attempts: int = 11,
    pause: Any = time.sleep,
) -> tuple[str, ...]:
    for attempt in range(attempts):
        candidate_ids = _delivery_ids(store, status, due_before=due_before, limit=expected + 1)
        if len(candidate_ids) >= expected or attempt == attempts - 1:
            return candidate_ids
        pause(1)
    raise AssertionError("bounded delivery observation did not terminate")


def _receive_one(clients: AwsClients, queue_url: str) -> Mapping[str, Any]:
    response = clients.sqs.receive_message(
        QueueUrl=queue_url,
        MaxNumberOfMessages=1,
        WaitTimeSeconds=10,
        VisibilityTimeout=300,
        MessageSystemAttributeNames=["MessageGroupId", "MessageDeduplicationId"],
        MessageAttributeNames=["All"],
    )
    messages = response.get("Messages", [])
    if not isinstance(messages, list) or len(messages) != 1 or not isinstance(messages[0], Mapping):
        raise PreflightError("queue_not_observed", "exactly one queued delivery message was not observed")
    return messages[0]


def _validate_message(
    message: Mapping[str, Any],
    *,
    plan: Mapping[str, Any],
    release: LoadedRelease,
    candidate_ids: Sequence[str],
) -> tuple[Mapping[str, Any], str, str]:
    body = message.get("Body")
    message_id = _safe_text(message.get("MessageId"), "SQS message ID")
    receipt = _safe_text(message.get("ReceiptHandle"), "SQS receipt handle")
    if not isinstance(body, str):
        raise PreflightError("message_refused", "SQS message body is missing")
    try:
        request = load_unique_json(body)
        if not isinstance(request, Mapping):
            raise ValueError
        validate_delivery_request(
            request,
            max_bytes=int(release.config["message_policy"]["max_delivery_request_bytes"]),
        )
        candidate = request["candidate"]
        validate_candidate_against_release(release.config, release.inventory, candidate)
    except Exception as error:
        raise PreflightError("message_refused", "queued delivery request failed contract validation") from error
    if candidate.get("candidate_id") not in candidate_ids:
        raise PreflightError("message_refused", "queued candidate was not created by this preflight")
    if candidate.get("announcement", {}).get("source_type") != "public_feed":
        raise PreflightError("message_refused", "queued candidate is not a public-feed announcement")
    if candidate.get("release") != plan["release"]["reference"]:
        raise PreflightError("message_refused", "queued candidate release differs from the plan")
    if request.get("destination_key") != plan["delivery"]["destination_key"]:
        raise PreflightError("message_refused", "queued destination differs from the plan")
    return request, message_id, receipt


def _worker_event(message: Mapping[str, Any], queue_arn: str, region: str) -> dict[str, Any]:
    attributes = message.get("Attributes")
    if (
        not isinstance(attributes, Mapping)
        or not isinstance(attributes.get("MessageGroupId"), str)
        or not isinstance(attributes.get("MessageDeduplicationId"), str)
    ):
        raise PreflightError("message_refused", "FIFO message group or deduplication ID is missing")
    return {
        "Records": [
            {
                "messageId": message["MessageId"],
                "receiptHandle": message["ReceiptHandle"],
                "body": message["Body"],
                "attributes": {
                    "MessageGroupId": attributes["MessageGroupId"],
                    "MessageDeduplicationId": attributes["MessageDeduplicationId"],
                },
                "messageAttributes": {},
                "eventSource": "aws:sqs",
                "eventSourceARN": queue_arn,
                "awsRegion": region,
            }
        ]
    }


def apply_plan(
    clients: AwsClients, plan: Mapping[str, Any], *, clock: Any = lambda: datetime.now(UTC)
) -> dict[str, Any]:
    local = plan.get("local", {})
    fresh = build_preview(
        clients,
        deployment_path=Path(local["deployment_path"]),
        config_path=Path(local["config_path"]),
        terraform_output_path=Path(local["terraform_output_path"]),
        expected_account=plan["aws"]["account"],
        application_digest=plan["application"]["digest"],
        candidate_cap=plan["bounds"]["candidate_cap"],
    )
    if fresh != plan:
        raise PreflightError("stale_plan", "live or local preflight inputs changed after preview")
    now = clock()
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise PreflightError("invalid_input", "preflight clock must be timezone-aware")
    table = plan["delivery"]["table"]
    index = plan["delivery"]["index"]
    store = DynamoDBDeliveryStore(clients.dynamodb, table, index)
    release = load_active_release(
        S3ObjectStore(clients.s3, plan["release"]["config_bucket"]),
        pointer_key=plan["release"]["pointer_key"],
        application_version=plan["application"]["version"],
    )
    watcher_result, watcher_ambiguous = _invoke(
        clients,
        plan["runtime"]["functions"]["watcher"]["name"],
        _schedule_event(plan, "watcher", now),
    )
    if watcher_ambiguous or watcher_result is None:
        raise PreflightError("watcher_unknown", "watcher invocation did not return a proved bounded result")
    expected_feeds = plan["release"]["feed_count"]
    if watcher_result.get("feeds") != expected_feeds or watcher_result.get("advanced") != expected_feeds:
        raise PreflightError("watcher_refused", "watcher did not complete every planned feed")
    candidate_count = watcher_result.get("candidates")
    if isinstance(candidate_count, bool) or not isinstance(candidate_count, int) or candidate_count < 0:
        raise PreflightError("watcher_refused", "watcher candidate count is malformed")
    if candidate_count == 0:
        return {"status": "no_positive_match", "feeds": expected_feeds, "candidates": 0}
    if candidate_count > plan["bounds"]["candidate_cap"]:
        return {
            "status": "candidate_cap_exceeded",
            "feeds": expected_feeds,
            "candidates": candidate_count,
            "candidate_cap": plan["bounds"]["candidate_cap"],
        }
    due_before = int(now.timestamp()) + 600
    candidate_ids = _await_delivery_ids(
        store,
        "pending_queue",
        due_before=due_before,
        expected=candidate_count,
    )
    if len(candidate_ids) != candidate_count:
        raise PreflightError("state_refused", "new pending delivery records differ from watcher result")
    dispatcher_result, dispatcher_ambiguous = _invoke(
        clients,
        plan["runtime"]["functions"]["dispatcher"]["name"],
        _schedule_event(plan, "dispatcher", now),
    )
    if dispatcher_ambiguous or dispatcher_result is None:
        raise PreflightError("dispatcher_unknown", "dispatcher invocation did not return a proved bounded result")
    expected_dispatch = {
        "considered": candidate_count,
        "accepted": candidate_count,
        "unknown": 0,
        "failed_transitions": 0,
    }
    if dict(dispatcher_result) != expected_dispatch:
        raise PreflightError("dispatcher_refused", "dispatcher result differs from the bounded candidate cohort")
    queue = plan["delivery"]["queue"]
    message = _receive_one(clients, queue["url"])
    request, message_id, receipt = _validate_message(message, plan=plan, release=release, candidate_ids=candidate_ids)
    candidate_id = str(request["candidate"]["candidate_id"])
    queued = store.get_delivery(candidate_id)
    if queued is None or queued.status != "queued" or queued.queue_message_id != message_id:
        raise PreflightError("state_refused", "durable queued state differs from the received message")
    worker_result, worker_ambiguous = _invoke(
        clients,
        plan["runtime"]["functions"]["worker"]["name"],
        _worker_event(message, queue["arn"], plan["aws"]["region"]),
    )
    durable = store.get_delivery(candidate_id)
    if worker_ambiguous or worker_result is None:
        return {
            "status": "worker_unknown",
            "candidate_id": candidate_id,
            "durable_state": durable.status if durable is not None else "missing",
        }
    if worker_result != {"batchItemFailures": []} or durable is None or durable.status != "posted":
        return {
            "status": "worker_refused",
            "candidate_id": candidate_id,
            "durable_state": durable.status if durable is not None else "missing",
        }
    if durable.queue_message_id != message_id:
        raise PreflightError("state_refused", "posted record queue message differs")
    try:
        clients.sqs.delete_message(QueueUrl=queue["url"], ReceiptHandle=receipt)
    except Exception:
        return {
            "status": "delete_unknown",
            "candidate_id": candidate_id,
            "request_id": request["request_id"],
            "durable_state": "posted",
        }
    return {
        "status": "posted",
        "feeds": expected_feeds,
        "candidates": candidate_count,
        "candidate_id": candidate_id,
        "request_id": request["request_id"],
        "source_url": request["candidate"]["announcement"]["url"],
        "durable_state": "posted",
        "route_id": plan["delivery"]["route_id"],
        "channel_label": plan["delivery"]["channel_label"],
        "operator_slack_confirmation_required": True,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preview or run one disabled-trigger delivery preflight.")
    subparsers = parser.add_subparsers(dest="action", required=True)
    preview = subparsers.add_parser("preview")
    preview.add_argument("--deployment", type=Path, required=True)
    preview.add_argument("--config", type=Path, required=True)
    preview.add_argument("--terraform-output", type=Path, required=True)
    preview.add_argument("--expected-account", required=True)
    preview.add_argument("--application-digest", required=True)
    preview.add_argument("--candidate-cap", type=int, default=10)
    preview.add_argument("--plan", type=Path, required=True)
    apply = subparsers.add_parser("apply")
    apply.add_argument("--plan", type=Path, required=True)
    apply.add_argument("--expected-plan-sha256", required=True)
    return parser.parse_args(argv)


def _write(document: Mapping[str, Any], *, stream: Any | None = None) -> None:
    print(canonical_json(document).decode("utf-8"), file=sys.stdout if stream is None else stream)


def main(argv: list[str] | None = None, *, clients: AwsClients | None = None) -> int:
    arguments = parse_args(argv)
    try:
        if arguments.action == "preview":
            deployment, _ = _read_mapping(arguments.deployment, "deployment")
            try:
                validator.validate_schema(ROOT / "schemas/deployment.schema.json", arguments.deployment, deployment)
            except Exception as error:
                raise PreflightError("invalid_input", "deployment failed validation") from error
            active_clients = clients or _clients_for_region(str(deployment["deployment_region"]))
            plan = build_preview(
                active_clients,
                deployment_path=arguments.deployment,
                config_path=arguments.config,
                terraform_output_path=arguments.terraform_output,
                expected_account=arguments.expected_account,
                application_digest=arguments.application_digest,
                candidate_cap=arguments.candidate_cap,
            )
            digest = write_preview(arguments.plan, plan)
            _write(
                {
                    "status": "previewed",
                    "plan_sha256": digest,
                    "account": plan["aws"]["account"],
                    "release_id": plan["release"]["id"],
                    "application_version": plan["application"]["version"],
                    "candidate_cap": plan["bounds"]["candidate_cap"],
                    "route_id": plan["delivery"]["route_id"],
                    "channel_label": plan["delivery"]["channel_label"],
                }
            )
            return 0
        saved_plan = load_plan(arguments.plan, arguments.expected_plan_sha256)
        active_clients = clients or _clients_for_region(str(saved_plan["aws"]["region"]))
        result = apply_plan(active_clients, saved_plan)
        _write(result)
        if result["status"] in ("posted", "no_positive_match"):
            return 0
        return EXIT_AMBIGUOUS if result["status"] in ("worker_unknown", "delete_unknown") else EXIT_REFUSED
    except PreflightError as error:
        _write({"status": error.status, "detail": error.detail}, stream=sys.stderr)
        if error.status in ("invalid_input", "local_write_failed"):
            return EXIT_INVALID
        if error.status in ("watcher_unknown", "dispatcher_unknown"):
            return EXIT_AMBIGUOUS
        return EXIT_REFUSED
    except Exception:
        _write(
            {"status": "provider_error", "detail": "preflight failed without a safe bounded result"},
            stream=sys.stderr,
        )
        return EXIT_AMBIGUOUS


if __name__ == "__main__":
    raise SystemExit(main())
