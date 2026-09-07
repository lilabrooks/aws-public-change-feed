#!/usr/bin/env python3
"""Bounded dev live sessions. Terraform owns controls; Step Functions owns cleanup.

Never print Terraform plans, private inputs, provider output, or credential data.
The control root and initial parked migration must be reviewed/applied separately.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import boto3
import live_retention
import yaml
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from preflight_delivery import PreflightError

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = "667653114001"
REGION = "us-east-1"
MAX_WINDOW = 364 * 86400
KEY = {"id": {"S": "dev"}}
FUNCTIONS = {
    "watcher": "apcf-dev-feed-watcher",
    "dispatcher": "apcf-dev-outbox-dispatcher",
    "reconciler": "apcf-dev-recovery-reconciler",
    "worker": "apcf-dev-slack-worker",
    "shadow": "apcf-dev-shadow-evaluator",
}
MAPPING = "245de870-e031-4d39-a5fe-bedc3f0c90f2"
# Coupled to ExactQueueTrigger in infra/live-control/iam.tf. Recheck the live
# mapping UUID before migration or after any mapping replacement.
ACTIONABLE = {"pending_queue", "queued", "sending", "failed_retryable"}
CONTROL_OUTPUTS = {"live_mode", "runtime_reserved_concurrency", "runtime_trigger_states", "dashboard_name"}
# Computed AND NOT optional/required in the central AWS 6.58.0 provider schema.
# These may become unknown on an in-place toggle. Configurable unknowns are
# still refused; after_unknown alone is not permission to change code or IAM.
COMPUTED_ONLY = {
    "aws_lambda_function": {
        "arn",
        "invoke_arn",
        "last_modified",
        "qualified_arn",
        "qualified_invoke_arn",
        "response_streaming_invoke_arn",
        "signing_job_arn",
        "signing_profile_version_arn",
        "source_code_size",
        "version",
    },
    "aws_lambda_event_source_mapping": {
        "arn",
        "function_arn",
        "last_modified",
        "last_processing_result",
        "state",
        "state_transition_reason",
        "uuid",
    },
}


class Refused(RuntimeError):
    """Safe operator-facing failure, with no private provider payload."""


def validate_timing_budget(value: Any) -> dict[str, int]:
    fields = {
        "watcher_wait_seconds",
        "final_wait_seconds",
        "drain_seconds",
        "control_work_allowance_seconds",
        "build_timeout_minutes",
        "queued_timeout_minutes",
        "provisioning_allowance_seconds",
        "startup_reserve_seconds",
        "activation_allowance_seconds",
        "observation_seconds",
        "delivery_seconds",
        "park_retry_attempts",
        "park_retry_interval_seconds",
        "park_retry_backoff_rate",
        "cleanup_notification_seconds",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise Refused("live timing budget has missing or unknown fields")
    if any(type(number) is not int or number <= 0 for number in value.values()):
        raise Refused("live timing budget requires positive integer values")
    planned = sum(
        value[key]
        for key in ("watcher_wait_seconds", "final_wait_seconds", "drain_seconds", "control_work_allowance_seconds")
    )
    if value["build_timeout_minutes"] * 60 < planned:
        raise Refused("build timeout cannot contain the planned shutdown work")
    if value["build_timeout_minutes"] * 60 < sum(
        value[key] for key in ("startup_reserve_seconds", "activation_allowance_seconds", "delivery_seconds")
    ):
        raise Refused("build timeout cannot contain the planned activation and delivery work")
    return value


# Both Terraform and the immutable source bundle consume this reviewed file.
# Allowances are planning assumptions, not measured AWS upper bounds.
BUDGET = validate_timing_budget(json.loads((ROOT / "scripts/live_window_budget.json").read_bytes()))
CLEANUP_RESERVE = (BUDGET["build_timeout_minutes"] + BUDGET["queued_timeout_minutes"]) * 60 + BUDGET[
    "provisioning_allowance_seconds"
]
STARTUP_RESERVE = BUDGET["startup_reserve_seconds"] + BUDGET["activation_allowance_seconds"]
MIN_WINDOW = CLEANUP_RESERVE + STARTUP_RESERVE + BUDGET["observation_seconds"]
MIN_DELIVERY_WINDOW = CLEANUP_RESERVE + STARTUP_RESERVE + BUDGET["delivery_seconds"]
PARK_ATTEMPTS_ALLOWANCE = (BUDGET["park_retry_attempts"] + 1) * CLEANUP_RESERVE + sum(
    BUDGET["park_retry_interval_seconds"] * BUDGET["park_retry_backoff_rate"] ** retry
    for retry in range(BUDGET["park_retry_attempts"])
)
TERMINAL_FAILURE_ALLOWANCE = PARK_ATTEMPTS_ALLOWANCE + BUDGET["cleanup_notification_seconds"]


def duration(value: str) -> int:
    parts = re.findall(r"([0-9]+)([smhd])", value)
    if not parts or "".join(number + unit for number, unit in parts) != value:
        raise Refused("WINDOW must be a positive duration such as 90m, 2h, or 1h30m")
    units = [unit for _, unit in parts]
    if len(units) != len(set(units)) or units != sorted(units, key="dhms".index):
        raise Refused("WINDOW units must appear once each in d, h, m, s order")
    seconds = sum(int(number) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit] for number, unit in parts)
    if not MIN_WINDOW <= seconds <= MAX_WINDOW:
        raise Refused(f"WINDOW must be between {MIN_WINDOW}s and {MAX_WINDOW}s, including setup and shutdown reserves")
    return seconds


def utc() -> datetime:
    return datetime.now(UTC)


def stamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def encode(value: Any) -> bytes:
    def serialize(item: Any) -> str:
        if isinstance(item, datetime) and item.tzinfo is not None:
            return stamp(item.astimezone(UTC))
        raise TypeError("unsupported evidence value")

    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=serialize).encode()


def run_command(args: list[str], *, cwd: Path = ROOT, timeout: int = 300) -> bytes:
    # Let Terraform persist state and release its lock on cancellation. A hard
    # kill is only the bounded last resort; an orphan lock then needs review.
    with subprocess.Popen(
        args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
    ) as process:
        try:
            output, _ = process.communicate(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            for sig, grace in ((signal.SIGINT, 60), (signal.SIGTERM, 15), (signal.SIGKILL, 5)):
                if process.poll() is not None:
                    break
                os.killpg(process.pid, sig)
                try:
                    process.communicate(timeout=grace)
                    break
                except subprocess.TimeoutExpired:
                    continue
            raise
    if process.returncode:
        raise Refused(f"{Path(args[0]).name} failed; private subprocess output was withheld")
    return output


def worker_concurrency() -> int:
    deployment = yaml.safe_load((ROOT / "infra/central/deployment.yaml").read_bytes())
    value = deployment["slack"]["rate_control"]["worker_reserved_concurrency"]
    if type(value) is not int or value <= 0:
        raise Refused("deployment worker concurrency must be a positive integer")
    return value


def expected_controls(mode: str, worker: int | None = None) -> tuple[dict[str, bool], dict[str, int]]:
    consumers = mode in {"live", "draining"}
    triggers = dict.fromkeys(("watcher", "dispatcher", "worker", "reconciler"), consumers)
    triggers["watcher"] = mode == "live"
    concurrency = dict.fromkeys(FUNCTIONS, 0)
    if mode in {"live", "draining", "direct"}:
        if worker is None:
            worker = worker_concurrency()
        concurrency.update(dispatcher=1, reconciler=1, worker=worker)
    if mode in {"live", "direct"}:
        concurrency["watcher"] = 1
    if mode == "shadow":
        concurrency["shadow"] = 1
    return triggers, concurrency


def has_unknown(value: Any) -> bool:
    if isinstance(value, dict):
        return any(has_unknown(item) for item in value.values())
    if isinstance(value, list):
        return any(has_unknown(item) for item in value)
    return value is True


def validate_plan(plan: dict[str, Any], mode: str) -> None:
    """Reject every change outside exact lifecycle fields, including replacements."""
    triggers, concurrency = expected_controls(mode)
    rules = {
        f"aws_cloudwatch_event_rule.{name}" + ("[0]" if name != "reconciler" else ""): name
        for name in ("watcher", "dispatcher", "reconciler")
    }
    functions = {
        f"aws_lambda_function.{ ({'shadow': 'shadow_evaluator', 'worker': 'slack_worker'}.get(name, name)) }[0]": name
        for name in FUNCTIONS
    }
    for resource in plan.get("resource_changes", []):
        change = resource["change"]
        actions = change["actions"]
        if actions in (["no-op"], ["read"]):
            continue
        address = resource["address"]
        before, after = change.get("before") or {}, change.get("after") or {}
        changed = {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)}
        unknown = {key for key, value in change.get("after_unknown", {}).items() if has_unknown(value)}
        computed = COMPUTED_ONLY.get(resource["type"], set())
        permitted_unknown = {key for key in computed if change.get("after_unknown", {}).get(key) is True}
        if resource["type"] in COMPUTED_ONLY and unknown - permitted_unknown:
            raise Refused(f"unknown configurable Terraform lifecycle field at {address}")
        changed = (changed | unknown) - permitted_unknown
        if address in functions and actions == ["update"]:
            if (
                changed <= {"reserved_concurrent_executions"}
                and after["reserved_concurrent_executions"] == concurrency[functions[address]]
            ):
                continue
        if address in rules and actions == ["update"]:
            if changed <= {"state"} and after["state"] == ("ENABLED" if triggers[rules[address]] else "DISABLED"):
                continue
        if address == "aws_lambda_event_source_mapping.slack_worker[0]" and actions == ["update"]:
            if changed <= {"enabled"} and after["enabled"] == triggers["worker"] and before.get("uuid") == MAPPING:
                continue
        if resource["type"] == "aws_cloudwatch_metric_alarm" and re.fullmatch(
            r"aws_cloudwatch_metric_alarm\.[a-z_]+\[0\]", address
        ):
            name = (after or before).get("alarm_name", "")
            if name.startswith("apcf-dev-") and actions in (["create"], ["delete"]):
                # Definition changes and replacements are migration work, never a cost toggle.
                if actions == ["create"] and mode != "parked":
                    continue
                if actions == ["delete"] and (
                    mode == "parked"
                    or name
                    in {
                        "apcf-dev-feed-watcher-errors",
                        "apcf-dev-watcher-incomplete-runs",
                        "apcf-dev-watcher-fault",
                        "apcf-dev-feed-watcher-heartbeat",
                        "apcf-dev-outbox-dispatcher-errors",
                        "apcf-dev-outbox-dispatcher-heartbeat",
                        "apcf-dev-recovery-reconciler-heartbeat",
                    }
                ):
                    continue
        if address == "aws_cloudwatch_dashboard.operations[0]":
            if (after or before).get("dashboard_name") == "apcf-dev-operations" and actions == (
                ["delete"] if mode == "parked" else ["create"]
            ):
                continue
        raise Refused(f"unsafe Terraform lifecycle change at {address}")
    for name, change in plan.get("output_changes", {}).items():
        if change["actions"] != ["no-op"] and name not in CONTROL_OUTPUTS:
            raise Refused(f"unexpected Terraform output change: {name}")
    if plan.get("errored") or plan.get("complete") is False:
        raise Refused("Terraform plan is incomplete")


class Controller:
    def __init__(self, config: dict[str, Any], *, session: Any = None):
        self.config = config
        self.session = session or boto3.Session(profile_name=config.get("profile"), region_name=REGION)
        options = Config(connect_timeout=10, read_timeout=30, retries={"max_attempts": 3, "mode": "standard"})
        self.clients = {
            name: self.session.client(name, config=options)
            for name in ("dynamodb", "s3", "stepfunctions", "lambda", "events", "cloudwatch", "sqs", "sts")
        }
        if self.clients["sts"].get_caller_identity()["Account"] != ACCOUNT:
            raise Refused("AWS account mismatch")
        self.evidence_errors: list[str] = []

    def ledger(self) -> dict[str, Any]:
        return (
            self.clients["dynamodb"]
            .get_item(TableName=self.config["table"], Key=KEY, ConsistentRead=True)
            .get("Item", {})
        )

    def update(
        self, operation: str, values: dict[str, Any], *, expected_execution: str | None = None
    ) -> dict[str, Any]:
        condition = "generation = :generation"
        extra = {}
        if expected_execution:
            condition += " AND execution = :execution"
            extra[":execution"] = {"S": expected_execution}
        response = self.clients["dynamodb"].update_item(
            TableName=self.config["table"],
            Key=KEY,
            ReturnValues="ALL_NEW",
            ConditionExpression=condition,
            UpdateExpression="SET " + ", ".join(f"#v{i} = :v{i}" for i in range(len(values))),
            ExpressionAttributeNames={f"#v{i}": key for i, key in enumerate(values)},
            ExpressionAttributeValues={
                ":generation": {"S": operation},
                **extra,
                **{
                    f":v{i}": ({"BOOL": value} if isinstance(value, bool) else {"S": str(value)})
                    for i, value in enumerate(values.values())
                },
            },
        )
        return response["Attributes"]

    def owner(self, operation: str, *, enabling: bool = False) -> dict[str, Any]:
        item = self.ledger()
        if item.get("generation", {}).get("S") != operation:
            raise Refused("stale operation generation")
        execution = item["execution"]["S"]
        if os.environ.get("LIVE_EXECUTION") != execution:
            raise Refused("execution does not own this operation")
        if self.clients["stepfunctions"].describe_execution(executionArn=execution)["status"] != "RUNNING":
            raise Refused("owner execution is no longer running")
        if enabling and (
            item["stop"]["BOOL"]
            or item["phase"]["S"] in {"parking", "parked"}
            or utc() >= datetime.fromisoformat(item["cleanup_at"]["S"])
        ):
            raise Refused("stop requested or usable window expired")
        return item

    def snapshot(self, *, include_work: bool = True) -> dict[str, Any]:
        lc = self.clients["lambda"]
        rules = {
            name: self.clients["events"].describe_rule(Name=FUNCTIONS[name])["State"]
            for name in ("watcher", "dispatcher", "reconciler")
        }
        mapping = lc.get_event_source_mapping(UUID=MAPPING)
        if mapping["FunctionArn"].split(":")[-1] != FUNCTIONS["worker"] or not mapping["EventSourceArn"].endswith(
            ":apcf-delivery-dev.fifo"
        ):
            raise Refused("queue trigger identity changed")
        concurrency = {
            name: lc.get_function_concurrency(FunctionName=function).get("ReservedConcurrentExecutions")
            for name, function in FUNCTIONS.items()
        }
        provisioned = {
            name: lc.list_provisioned_concurrency_configs(FunctionName=function).get(
                "ProvisionedConcurrencyConfigs", []
            )
            for name, function in FUNCTIONS.items()
        }
        alarms = []
        for page in self.clients["cloudwatch"].get_paginator("describe_alarms").paginate(AlarmNamePrefix="apcf-dev-"):
            alarms.extend(page.get("MetricAlarms", []))
            alarms.extend(page.get("CompositeAlarms", []))
        try:
            self.clients["cloudwatch"].get_dashboard(DashboardName="apcf-dev-operations")
            dashboard = True
        except ClientError as error:
            if error.response["Error"]["Code"] != "ResourceNotFound":
                raise
            dashboard = False
        states: Counter[str] = Counter()
        token = None
        scanned = 0
        while include_work:
            args: dict[str, Any] = {
                "TableName": "apcf-delivery-dev",
                "ConsistentRead": True,
                "Limit": 1000,
                "ProjectionExpression": "#s",
                "ExpressionAttributeNames": {"#s": "status"},
            }
            if token:
                args["ExclusiveStartKey"] = token
            response = self.clients["dynamodb"].scan(**args)
            scanned += response["ScannedCount"]
            if scanned > 10000:
                raise Refused("delivery inventory exceeds the bounded 10000-item control scan")
            for item in response.get("Items", []):
                if "status" in item:
                    states[item["status"]["S"]] += 1
            token = response.get("LastEvaluatedKey")
            if not token:
                break
        queues = {}
        for name in (
            ("apcf-delivery-dev.fifo", "apcf-delivery-dlq-dev.fifo", "apcf-runtime-failures-dev")
            if include_work
            else ()
        ):
            url = self.clients["sqs"].get_queue_url(QueueName=name)["QueueUrl"]
            queues[name] = self.clients["sqs"].get_queue_attributes(
                QueueUrl=url,
                AttributeNames=[
                    "ApproximateNumberOfMessages",
                    "ApproximateNumberOfMessagesNotVisible",
                    "ApproximateNumberOfMessagesDelayed",
                ],
            )["Attributes"]
        return {
            "rules": rules,
            "mapping": mapping["State"],
            "mapping_metrics_config": mapping.get("MetricsConfig"),
            "concurrency": concurrency,
            "provisioned": {name: bool(value) for name, value in provisioned.items()},
            "provisioned_pollers": bool(mapping.get("ProvisionedPollerConfig")),
            "alarms": alarms,
            "dashboard": dashboard,
            "delivery_states": dict(states),
            "queues": queues,
            "work_inventory_complete": include_work,
        }

    def verify(self, mode: str) -> dict[str, Any]:
        snap = self.snapshot(include_work=False)
        triggers, concurrency = expected_controls(mode)
        if snap["concurrency"] != concurrency or any(snap["provisioned"].values()) or snap["provisioned_pollers"]:
            raise Refused("execution fences or provisioned capacity do not match the phase")
        if any(snap["rules"][name] != ("ENABLED" if triggers[name] else "DISABLED") for name in snap["rules"]):
            raise Refused("schedule read-back does not match the phase")
        if snap["mapping"] != ("Enabled" if triggers["worker"] else "Disabled"):
            raise Refused("queue mapping read-back does not match the phase")
        if snap["mapping_metrics_config"] not in (None, {}, {"Metrics": []}):
            raise Refused("queue mapping metrics must be disabled; separate drift recovery required")
        expected_alarms = 0 if mode == "parked" else 21 + (7 if mode == "live" else 3 if mode == "draining" else 0)
        if len(snap["alarms"]) != expected_alarms or snap["dashboard"] != (mode != "parked"):
            raise Refused("monitoring read-back does not match the phase")
        return snap

    def evidence(self, operation: str, phase: str, document: Any) -> None:
        try:
            self.clients["s3"].put_object(
                Bucket=self.config["bucket"],
                Key=f"evidence/{operation}/{phase}-{uuid.uuid4().hex}.json",
                Body=encode(document),
                ServerSideEncryption="AES256",
            )
        except (ClientError, BotoCoreError, TypeError, ValueError):
            self.evidence_errors.append(phase)

    def plan(self, mode: str, work: Path) -> tuple[dict[str, Any], Path]:
        plan_file = work / "phase.tfplan"
        args = ["terraform", "-chdir=infra/central"]
        run_command(
            [
                *args,
                "plan",
                "-input=false",
                "-lock-timeout=60s",
                "-var-file=" + str(ROOT / "live-private.tfvars.json"),
                f"-var=live_mode={mode}",
                "-out=" + str(plan_file),
            ]
        )
        plan = json.loads(run_command([*args, "show", "-json", str(plan_file)]))
        validate_plan(plan, mode)
        return plan, plan_file

    def apply(self, operation: str, mode: str, work: Path) -> None:
        enabling = mode in {"prepared", "direct", "live", "shadow"} or (
            mode == "draining" and self.ledger()["phase"]["S"] != "parking"
        )
        self.owner(operation, enabling=enabling)
        _, plan_file = self.plan(mode, work)
        self.owner(operation, enabling=enabling)
        run_command(["terraform", "-chdir=infra/central", "apply", "-input=false", "-lock-timeout=60s", str(plan_file)])
        if mode not in {"stopping", "parked"}:
            self.evidence(operation, mode, self.verify(mode))

    def remote(self, action: str, operation: str) -> None:
        initial = self.owner(operation, enabling=action == "unpark")
        if action == "park" and initial["phase"]["S"] == "parked":
            self.verify("parked")
            if initial.get("evidence_incomplete", {}).get("BOOL"):
                raise Refused("controls already parked; the original evidence remains incomplete")
            return
        run_command(["terraform", "-chdir=infra/central", "init", "-input=false", "-lockfile=readonly"])
        with tempfile.TemporaryDirectory(prefix="apcf-live-") as temporary:
            work = Path(temporary)
            if action == "unpark":
                self.verify("parked")
                self.evidence(operation, "before-unpark", self.snapshot())
                self.apply(operation, "prepared", work)
                item = self.owner(operation, enabling=True)
                case = json.loads(item["input"]["S"])["case"]
                require_usable_window(item, case, activating=True)
                if case == "delivery":
                    self.apply(operation, "direct", work)
                    require_usable_window(self.owner(operation, enabling=True), case)
                    try:
                        result = self.delivery_test(work)
                    except PreflightError as error:
                        result = {"status": error.status, "detail": error.detail}
                    self.evidence(operation, "delivery", result)
                    self.update(
                        operation,
                        {
                            "outcome": result["status"],
                            "stop": True,
                            "phase": "test_finished",
                            "evidence_incomplete": bool(self.evidence_errors),
                        },
                    )
                    if result["status"] not in {"posted", "no_positive_match"}:
                        raise Refused("delivery preflight did not produce a proved terminal result")
                    return
                self.apply(operation, "draining", work)
                self.apply(operation, "live", work)
                require_usable_window(self.owner(operation, enabling=True), case)
                self.update(
                    operation,
                    {
                        "phase": "live",
                        "outcome": "observation_in_progress",
                        "evidence_incomplete": bool(self.evidence_errors),
                    },
                )
                return
            self.update(operation, {"phase": "parking", "stop": True})
            try:
                snap = self.snapshot()
                self.evidence(operation, "before-park", snap)
            except (ClientError, BotoCoreError, Refused):
                self.evidence_errors.append("before-park")
                snap = {"mapping": "unknown"}
            if snap["mapping"] == "Enabled" and all(
                snap["rules"][name] == "ENABLED" for name in ("dispatcher", "reconciler")
            ):
                try:
                    self.apply(operation, "draining", work)
                    # A previously running watcher can produce until its timeout.
                    time.sleep(BUDGET["watcher_wait_seconds"])
                    drain_until = min(
                        utc() + timedelta(seconds=BUDGET["drain_seconds"]),
                        datetime.fromisoformat(self.ledger()["deadline"]["S"])
                        - timedelta(seconds=BUDGET["final_wait_seconds"]),
                    )
                    while utc() < drain_until:
                        snap = self.snapshot()
                        if not any(snap["delivery_states"].get(state, 0) for state in ACTIONABLE):
                            break
                        time.sleep(10)
                except (ClientError, BotoCoreError, Refused, subprocess.TimeoutExpired):
                    self.evidence_errors.append("drain")
                    # A failed optional drain never bypasses the mandatory fence.
            self.apply(operation, "stopping", work)
            # Reserved concurrency zero does not cancel calls already executing.
            time.sleep(BUDGET["final_wait_seconds"])
            self.apply(operation, "parked", work)
            snap = self.verify("parked")
            final_plan, _ = self.plan("parked", work)
            if any(
                resource["change"]["actions"] not in (["no-op"], ["read"])
                for resource in final_plan.get("resource_changes", [])
            ) or any(output["actions"] != ["no-op"] for output in final_plan.get("output_changes", {}).values()):
                raise Refused("controls read back parked but Terraform has not converged")
            try:
                snap = self.snapshot()
            except (ClientError, BotoCoreError, Refused):
                self.evidence_errors.append("retained-work")
            self.evidence(
                operation,
                "receipt",
                {
                    "controls": snap,
                    "test_outcome": self.ledger().get("outcome", {}).get("S", "activation_incomplete"),
                    "terraform_converged": True,
                    "unresolved_work": (
                        {state: snap["delivery_states"].get(state, 0) for state in ACTIONABLE | {"delivery_unknown"}}
                        if snap.get("work_inventory_complete")
                        else None
                    ),
                    "async_backlog": "not observable; retained async events may execute on a later unpark",
                    "deadline_overrun": utc() > datetime.fromisoformat(self.ledger()["deadline"]["S"]),
                },
            )
            previous = self.ledger()
            old_outcome = previous.get("outcome", {}).get("S", "activation_incomplete")
            outcome = "observation_complete" if old_outcome == "observation_in_progress" else old_outcome
            incomplete = bool(self.evidence_errors) or previous.get("evidence_incomplete", {}).get("BOOL", False)
            self.update(
                operation,
                {
                    "phase": "parked",
                    "parked_at": stamp(utc()),
                    "outcome": outcome,
                    "evidence_incomplete": incomplete,
                },
            )
            if incomplete:
                raise Refused("controls parked, but evidence capture incomplete")

    def delivery_test(self, work: Path) -> dict[str, Any]:
        import preflight_delivery

        output = work / "outputs.json"
        output.write_bytes(run_command(["terraform", "-chdir=infra/central", "output", "-json"]))
        inputs = json.loads((ROOT / "live-private.tfvars.json").read_bytes())
        clients = preflight_delivery.AwsClients(
            sts=self.clients["sts"],
            lambda_client=self.session.client(
                "lambda", config=Config(connect_timeout=10, read_timeout=330, retries={"max_attempts": 0})
            ),
            events=self.clients["events"],
            sqs=self.clients["sqs"],
            dynamodb=self.clients["dynamodb"],
            s3=self.clients["s3"],
            secretsmanager=self.session.client("secretsmanager"),
        )
        preview = preflight_delivery.build_preview(
            clients,
            deployment_path=ROOT / "infra/central/deployment.yaml",
            config_path=ROOT / "config/dev.yaml",
            terraform_output_path=output,
            expected_account=ACCOUNT,
            application_digest=inputs["worker_artifact_sha256"],
            candidate_cap=10,
        )
        plan_path = work / "delivery-plan.json"
        digest = preflight_delivery.write_preview(plan_path, preview)
        return preflight_delivery.apply_plan(clients, preflight_delivery.load_plan(plan_path, digest))


def read_config(path: Path) -> dict[str, Any]:
    config = json.loads(path.read_bytes())
    required = {"account", "region", "bucket", "table", "project", "state_machine", "tfvars"}
    if not isinstance(config, dict) or not required <= config.keys() or config.keys() - required - {"profile"}:
        raise Refused("LIVE_CONFIG has missing or unknown fields")
    if any(not isinstance(value, str) or not value.strip() for value in config.values()):
        raise Refused("LIVE_CONFIG values must be nonempty strings")
    if config["account"] != ACCOUNT or config["region"] != REGION:
        raise Refused("LIVE_CONFIG must select the reviewed dev account and region")
    expected = {
        "bucket": f"apcf-dev-live-control-{ACCOUNT}",
        "table": "apcf-dev-live-control",
        "project": "apcf-dev-live-control",
        "state_machine": f"arn:aws:states:{REGION}:{ACCOUNT}:stateMachine:apcf-dev-live-control",
    }
    if any(config[name] != value for name, value in expected.items()):
        raise Refused("LIVE_CONFIG control resource identity mismatch")
    return config


def bundle(config: dict[str, Any]) -> tuple[bytes, str]:
    if run_command(["git", "status", "--porcelain"]).strip():
        raise Refused("publish a reviewed clean commit before bundling the controller")
    revision = run_command(["git", "rev-parse", "HEAD"]).decode().strip()
    baseline = json.loads(Path(config["tfvars"]).expanduser().read_bytes())
    if not isinstance(baseline, dict):
        raise Refused("private tfvars must be a JSON object")
    declared = set(
        re.findall(
            r'^variable "([a-z_][a-z0-9_]*)"',
            "\n".join(path.read_text() for path in (ROOT / "infra/central").glob("*.tf")),
            re.MULTILINE,
        )
    )
    if baseline.keys() - declared:
        raise Refused("private tfvars contains unknown fields")
    if baseline.get("deployment_file", "deployment.yaml") != "deployment.yaml":
        raise Refused("live control requires the tracked central deployment.yaml")
    if baseline.get("live_mode") != "parked":
        raise Refused("complete private tfvars must persist live_mode=parked")
    for runtime in ("watcher", "dispatcher", "worker", "reconciler"):
        if not baseline.get(f"{runtime}_artifact_sha256") or not baseline.get(f"{runtime}_artifact_version_id"):
            raise Refused("complete immutable application inputs are required")
    if not baseline.get("operational_sns_subscription_endpoints"):
        raise Refused("preserve private notification subscription inputs")
    stream = io.BytesIO(
        run_command(
            [
                "git",
                "archive",
                "--format=zip",
                revision,
                "infra/central",
                "infra/live-control/buildspec.yml",
                "scripts",
                "src",
                "schemas",
                "config/dev.yaml",
                "requirements.txt",
            ]
        )
    )
    with zipfile.ZipFile(stream, "a", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("live-private.tfvars.json", encode(baseline))
        archive.writestr(
            "live-control.json",
            encode({key: value for key, value in config.items() if key not in {"profile", "tfvars"}}),
        )
    return stream.getvalue(), revision


def require_usable_window(item: dict[str, Any], case: str, *, activating: bool = False) -> None:
    reserve = BUDGET["delivery_seconds"] if case == "delivery" else BUDGET["observation_seconds"]
    if activating:
        reserve += BUDGET["activation_allowance_seconds"]
    if (datetime.fromisoformat(item["cleanup_at"]["S"]) - utc()).total_seconds() < reserve:
        raise Refused("startup consumed the activation or usable-window reserve")


def try_closeout(controller: Controller) -> dict[str, Any]:
    """Retention failures must never block fencing, parking, or a new window."""
    try:
        return live_retention.closeout(controller)
    except (Refused, ValueError, KeyError, TypeError, ClientError, BotoCoreError, OSError):
        return {
            "status": "retained_without_closeout",
            "notice": "Run live-close before replacing the owner if closure evidence is needed. No artifacts were pruned.",
        }


def start(controller: Controller, window: str, case: str) -> dict[str, Any]:
    seconds = duration(window)
    if case == "delivery" and seconds < MIN_DELIVERY_WINDOW:
        raise Refused(f"delivery WINDOW needs at least {MIN_DELIVERY_WINDOW}s including setup, protocol, and cleanup")
    existing = controller.ledger()
    if existing and existing["phase"]["S"] != "parked":
        # Retrying start uses the same named execution and exact original input.
        payload = json.loads(existing["input"]["S"])
        if payload.get("case") != case:
            raise Refused("another test case owns the active window; finish parking before starting this case")
        if case == "manual" and existing["stop"]["BOOL"]:
            raise Refused("the existing session is stopping; finish parking before unpark")
        try:
            controller.clients["stepfunctions"].start_execution(
                stateMachineArn=controller.config["state_machine"],
                name=existing["execution"]["S"].rsplit(":", 1)[1],
                input=json.dumps(payload, sort_keys=True),
            )
        except ClientError as error:
            if error.response["Error"]["Code"] != "ExecutionAlreadyExists":
                raise
        return {
            "status": "existing_session",
            "execution": existing["execution"]["S"],
            "deadline": existing["deadline"]["S"],
        }
    if (
        existing
        and controller.clients["stepfunctions"].describe_execution(executionArn=existing["execution"]["S"])["status"]
        == "RUNNING"
    ):
        raise Refused("previous owner is finishing; wait for its terminal result")
    controller.verify("parked")
    if existing:
        closure = try_closeout(controller)
        if closure["status"] == "retained_without_closeout":
            print(json.dumps(closure), file=sys.stderr)
    data, revision = bundle(controller.config)
    operation = uuid.uuid4().hex
    now = utc()
    deadline = stamp(now + timedelta(seconds=seconds))
    cleanup = stamp(now + timedelta(seconds=seconds - CLEANUP_RESERVE))
    key = f"bundles/{operation}-{hashlib.sha256(data).hexdigest()}.zip"
    upload = controller.clients["s3"].put_object(
        Bucket=controller.config["bucket"], Key=key, Body=data, ServerSideEncryption="AES256", IfNoneMatch="*"
    )
    if not upload.get("VersionId") or upload["VersionId"] == "null":
        raise Refused("control bucket must have versioning enabled")
    execution = controller.config["state_machine"].replace(":stateMachine:", ":execution:") + ":" + operation
    payload = {
        "operation": operation,
        "source": controller.config["bucket"] + "/" + key,
        "version": upload["VersionId"],
        "cleanup_at": cleanup,
        "action": "unpark",
        "case": case,
    }
    item = {
        **KEY,
        "generation": {"S": operation},
        "execution": {"S": execution},
        "phase": {"S": "starting"},
        "stop": {"BOOL": False},
        "deadline": {"S": deadline},
        "cleanup_at": {"S": cleanup},
        "input": {"S": json.dumps(payload, sort_keys=True)},
        "revision": {"S": revision},
    }
    args: dict[str, Any] = {
        "TableName": controller.config["table"],
        "Item": item,
        "ConditionExpression": "attribute_not_exists(id)",
    }
    if existing:
        args.update(
            ConditionExpression="generation = :old AND #phase = :parked",
            ExpressionAttributeNames={"#phase": "phase"},
            ExpressionAttributeValues={":old": existing["generation"], ":parked": {"S": "parked"}},
        )
    controller.clients["dynamodb"].put_item(**args)
    controller.clients["stepfunctions"].start_execution(
        stateMachineArn=controller.config["state_machine"], name=operation, input=json.dumps(payload, sort_keys=True)
    )
    actual = controller.clients["stepfunctions"].describe_execution(executionArn=execution)
    if actual["status"] != "RUNNING" or json.loads(actual["input"]) != payload:
        raise Refused("durable session ownership could not be verified; inspect live-status")
    return {
        "status": "starting",
        "execution": execution,
        "deadline": deadline,
        "cleanup_at": cleanup,
        "startup_reserve_seconds": STARTUP_RESERVE,
        "cleanup_reserve_seconds": CLEANUP_RESERVE,
        "all_park_attempts_allowance_seconds": PARK_ATTEMPTS_ALLOWANCE,
        "terminal_failure_allowance_seconds": TERMINAL_FAILURE_ALLOWANCE,
        "cleanup_notice": "The window reserves one parking attempt. Emergency retries may overrun it; AWS delays are not bounded by these allowances.",
        "retained_work_notice": "Unpark resumes retained work, including unobservable accepted async invocations. Unknown Slack outcomes are not automatically replayed.",
    }


def request_park(controller: Controller) -> dict[str, Any]:
    item = controller.ledger()
    if not item:
        controller.verify("parked")
        return {"status": "parked"}
    if item["phase"]["S"] == "parked":
        controller.verify("parked")
        return {"status": "parked"}
    item = controller.update(item["generation"]["S"], {"stop": True})
    if token := item.get("callback_token", {}).get("S"):
        try:
            controller.clients["stepfunctions"].send_task_success(taskToken=token, output="{}")
        except ClientError as error:
            if error.response["Error"]["Code"] not in {"TaskDoesNotExist", "TaskTimedOut", "InvalidToken"}:
                raise
    try:
        status = controller.clients["stepfunctions"].describe_execution(executionArn=item["execution"]["S"])["status"]
    except ClientError as error:
        if error.response["Error"]["Code"] != "ExecutionDoesNotExist":
            raise
        controller.clients["stepfunctions"].start_execution(
            stateMachineArn=controller.config["state_machine"],
            name=item["execution"]["S"].rsplit(":", 1)[1],
            input=item["input"]["S"],
        )
        return {
            "status": "parking_requested",
            "detail": "Recovered the reserved cleanup owner with stop already requested",
            "execution": item["execution"]["S"],
        }
    if status != "RUNNING":
        # Do not discard the generation or bundle. A failed owner can resume only
        # parking with the same immutable inputs, never activate again.
        payload = json.loads(item["input"]["S"])
        payload["action"] = "park"
        name = "park-" + uuid.uuid4().hex
        execution = controller.config["state_machine"].replace(":stateMachine:", ":execution:") + ":" + name
        controller.update(
            item["generation"]["S"],
            {"execution": execution, "input": json.dumps(payload, sort_keys=True), "phase": "parking"},
            expected_execution=item["execution"]["S"],
        )
        controller.clients["stepfunctions"].start_execution(
            stateMachineArn=controller.config["state_machine"], name=name, input=json.dumps(payload, sort_keys=True)
        )
        item["execution"] = {"S": execution}
    return {
        "status": "parking_requested",
        "detail": "Durable owner will park after the current bounded stage; cleanup ownership was not cancelled.",
        "execution": item["execution"]["S"],
    }


def wait_for(controller: Controller, *, terminal: bool, expected_execution: str) -> dict[str, Any]:
    started = time.monotonic()
    while True:
        item = controller.ledger()
        if item.get("execution", {}).get("S") != expected_execution:
            raise Refused(f"session ownership changed; inspect the original execution {expected_execution}")
        phase = item["phase"]["S"]
        execution = controller.clients["stepfunctions"].describe_execution(executionArn=item["execution"]["S"])
        if not terminal and phase == "live" and not item["stop"]["BOOL"]:
            controller.verify("live")
            return {"status": "ready", "deadline": item["deadline"]["S"], "cleanup_at": item["cleanup_at"]["S"]}
        if execution["status"] != "RUNNING":
            if (
                phase != "parked"
                or execution["status"] != "SUCCEEDED"
                or item.get("evidence_incomplete", {}).get("BOOL")
            ):
                raise Refused(
                    f"session {execution['status']}; controls phase={phase}; inspect live-status and evidence"
                )
            controller.verify("parked")
            return {
                "status": "parked",
                "outcome": item.get("outcome", {}).get("S", "not_run"),
                "retention": try_closeout(controller),
            }
        if not terminal and time.monotonic() - started > STARTUP_RESERVE:
            raise Refused("startup still pending; cleanup remains armed, use live-status")
        time.sleep(5)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["unpark", "test", "park", "status", "run", "close", "prune"])
    args = parser.parse_args(argv)
    controller: Controller | None = None
    try:
        if args.action == "run":
            config = json.loads((ROOT / "live-control.json").read_bytes())
            controller = Controller(config)
            controller.remote(os.environ["LIVE_ACTION"], os.environ["LIVE_OPERATION"])
            return 0
        config_path = os.environ.get("LIVE_CONFIG")
        if not config_path:
            raise Refused(
                "set LIVE_CONFIG to your private one-time operator configuration; see docs/runbooks/live-window.md"
            )
        controller = Controller(read_config(Path(config_path).expanduser()))
        if args.action in {"close", "prune"}:
            plan_path = os.environ.get("PLAN")
            result = live_retention.command(
                controller,
                args.action,
                Path(plan_path).expanduser() if plan_path else None,
                os.environ.get("APPLY", ""),
            )
        elif args.action in {"unpark", "test"}:
            case = "manual" if args.action == "unpark" else (os.environ.get("CASE") or "observation")
            if case not in {"manual", "observation", "delivery"}:
                raise Refused(
                    "registered cases: observation, delivery; synthetic recovery/load remain isolated preflight procedures"
                )
            result = start(controller, os.environ.get("WINDOW", ""), case)
            print(json.dumps(result), flush=True)
            result = wait_for(controller, terminal=args.action == "test", expected_execution=result["execution"])
        elif args.action == "park":
            result = request_park(controller)
            if result["status"] != "parked":
                print(json.dumps(result), flush=True)
                result = wait_for(controller, terminal=True, expected_execution=result["execution"])
            else:
                result["retention"] = try_closeout(controller)
        else:
            snap = controller.snapshot()
            result = {
                "controls": {**snap, "alarms": [alarm["AlarmName"] for alarm in snap["alarms"]]},
                "session": {key: value for key, value in controller.ledger().items() if key != "callback_token"},
                "residual_costs": [
                    "Secrets Manager",
                    "DynamoDB storage/PITR",
                    "S3",
                    "retained CloudWatch logs",
                    "live-control storage and requests; CodeBuild minutes while operating",
                ],
            }
        print(json.dumps(result, indent=2))
        return 0
    except KeyboardInterrupt:
        if args.action == "test" and controller is not None:
            try:
                request_park(controller)
            except Exception:
                # Best effort only. An exception inside this handler cannot
                # reach the sibling sanitizer; never expose provider details.
                print("Early park request not confirmed; run live-status.", file=sys.stderr)
        print("Local wait ended. The AWS cleanup owner remains responsible for parking.", file=sys.stderr)
        return 130
    except (
        Refused,
        PreflightError,
        ClientError,
        BotoCoreError,
        ValueError,
        KeyError,
        OSError,
        subprocess.TimeoutExpired,
    ) as error:
        print(
            str(error)
            if isinstance(error, (Refused, live_retention.RetentionError))
            else f"{type(error).__name__}: operation not confirmed; run live-status. No cleanup owner was cancelled.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
