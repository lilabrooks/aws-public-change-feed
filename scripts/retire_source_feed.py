#!/usr/bin/env python3
"""Preview and apply exact-feed retirement, compaction, or restoration."""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aws_public_change_feed.loading import load_release_version  # noqa: E402
from aws_public_change_feed.parsing import load_unique_json  # noqa: E402
from aws_public_change_feed.releases import S3ObjectStore  # noqa: E402
from aws_public_change_feed.source_retirement import (  # noqa: E402
    RetirementContext,
    SourceRetirementError,
    apply_plan,
    canonical_json,
    create_plan,
    sha256_bytes,
)

EXIT_INVALID = 2
EXIT_REFUSED = 3
EXIT_AMBIGUOUS = 4
_SHA256 = re.compile(r"[0-9a-f]{64}")
_FEED_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")


def _bounded_string(value: object, field: str, *, status: str = "invalid_input") -> str:
    if not isinstance(value, str) or not value or len(value) > 2048 or any(ord(char) < 32 for char in value):
        raise SourceRetirementError(status, f"{field} must be a bounded nonempty string")
    return value


def _read_mapping(path: Path, kind: str) -> tuple[dict[str, Any], bytes]:
    try:
        body = path.read_bytes()
    except OSError as error:
        raise SourceRetirementError("invalid_input", f"{kind} file cannot be read") from error
    try:
        if path.suffix in {".yaml", ".yml"}:
            document = yaml.safe_load(body)
        else:
            document = load_unique_json(body)
    except Exception as error:
        raise SourceRetirementError("invalid_input", f"{kind} file is malformed") from error
    if not isinstance(document, dict):
        raise SourceRetirementError("invalid_input", f"{kind} file must contain an object")
    return document, body


def _output_value(outputs: Mapping[str, Any], name: str) -> Any:
    output = outputs.get(name)
    if not isinstance(output, Mapping) or set(output) != {"sensitive", "type", "value"}:
        raise SourceRetirementError("invalid_input", f"Terraform output {name} is missing or malformed")
    return output["value"]


def _validate_deployment(document: Mapping[str, Any]) -> None:
    try:
        schema = load_unique_json((ROOT / "schemas" / "deployment.schema.json").read_bytes())
        if not isinstance(schema, Mapping):
            raise ValueError("deployment schema is not an object")
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(document)
    except Exception as error:
        raise SourceRetirementError("invalid_input", "deployment document failed validation") from error


def _read_pointer(s3_client: Any, *, bucket: str, key: str) -> dict[str, str]:
    try:
        response = s3_client.get_object(Bucket=bucket, Key=key)
        body = response["Body"].read()
    except Exception as error:
        raise SourceRetirementError("identity_refused", "active release pointer could not be read") from error
    try:
        document = load_unique_json(body)
    except Exception as error:
        raise SourceRetirementError("identity_refused", "active release pointer is malformed") from error
    if not isinstance(document, Mapping):
        raise SourceRetirementError("identity_refused", "active release pointer is malformed")
    version_id = _bounded_string(response.get("VersionId"), "active pointer version", status="identity_refused")
    etag = _bounded_string(response.get("ETag"), "active pointer ETag", status="identity_refused")
    return {"version_id": version_id, "etag": etag}


def verify_pointer_current(s3_client: Any, context: RetirementContext) -> None:
    try:
        pointer = _read_pointer(s3_client, bucket=context.bucket, key=context.pointer_key)
    except SourceRetirementError as error:
        raise SourceRetirementError("stale_plan", "active release pointer can no longer be proved") from error
    if pointer["version_id"] != context.pointer_version_id or pointer["etag"] != context.pointer_etag:
        raise SourceRetirementError("stale_plan", "active release pointer differs from the preview")


def load_context(
    *,
    deployment_path: Path,
    terraform_output_path: Path,
    expected_account: str,
    feed_name: str,
    sts_client: Any,
    s3_client: Any,
    ddb_client_factory: Any,
) -> tuple[RetirementContext, Any]:
    if not re.fullmatch(r"[0-9]{12}", expected_account):
        raise SourceRetirementError("invalid_input", "expected account must be 12 digits")
    if _FEED_NAME.fullmatch(feed_name) is None:
        raise SourceRetirementError("invalid_input", "feed name must be a bounded lowercase identifier")
    deployment, deployment_body = _read_mapping(deployment_path, "deployment")
    outputs, output_body = _read_mapping(terraform_output_path, "Terraform output")
    _validate_deployment(deployment)
    try:
        identity = sts_client.get_caller_identity()
    except Exception as error:
        raise SourceRetirementError("identity_refused", "caller identity could not be read") from error
    if identity.get("Account") != expected_account:
        raise SourceRetirementError("identity_refused", "caller account differs from the reviewed account")

    deployment_id = _bounded_string(deployment.get("deployment_id"), "deployment ID")
    region = _bounded_string(deployment.get("deployment_region"), "deployment Region")
    bucket = _bounded_string(_output_value(outputs, "config_bucket_name"), "configuration bucket")
    if bucket != deployment.get("config_bucket_name"):
        raise SourceRetirementError("identity_refused", "Terraform configuration bucket differs from deployment")
    table_name = _bounded_string(_output_value(outputs, "source_state_table"), "source-state table")
    if table_name != f"apcf-source-state-{deployment_id}":
        raise SourceRetirementError("identity_refused", "Terraform source-state table differs from deployment")
    roles = _output_value(outputs, "roles")
    if not isinstance(roles, Mapping):
        raise SourceRetirementError("invalid_input", "Terraform roles output is malformed")
    role_arn = _bounded_string(roles.get("source_state_retirement"), "source-state retirement role ARN")
    expected_role = f"arn:aws:iam::{expected_account}:role/apcf-{deployment_id}-source-state-retirement"
    if role_arn != expected_role:
        raise SourceRetirementError("identity_refused", "source-state retirement role differs from deployment")
    application_version = _bounded_string(
        _output_value(outputs, "watcher_application_version"), "watcher application version"
    )
    if not application_version.startswith("sha256:") or _SHA256.fullmatch(application_version[7:]) is None:
        raise SourceRetirementError("identity_refused", "watcher application version is malformed")
    pointer_key = _bounded_string(deployment.get("active_versions_object_key"), "active pointer key")
    pointer = _read_pointer(s3_client, bucket=bucket, key=pointer_key)
    try:
        release = load_release_version(
            S3ObjectStore(s3_client, bucket),
            pointer_key=pointer_key,
            version_id=pointer["version_id"],
            application_version=application_version,
        )
    except Exception as error:
        raise SourceRetirementError("identity_refused", "active release failed exact verification") from error
    retention = release.config.get("state_retention")
    feeds = release.config.get("feeds")
    if not isinstance(retention, Mapping) or not isinstance(feeds, list):
        raise SourceRetirementError("identity_refused", "active release feed policy is malformed")
    feed_days = retention.get("feed_state_ttl_days")
    if isinstance(feed_days, bool) or not isinstance(feed_days, int) or not 1 <= feed_days <= 3650:
        raise SourceRetirementError("identity_refused", "active feed-state retention is malformed")
    configured: dict[str, str] = {}
    for item in feeds:
        if not isinstance(item, Mapping):
            raise SourceRetirementError("identity_refused", "active release feed policy is malformed")
        name = _bounded_string(item.get("name"), "configured feed name", status="identity_refused")
        url = _bounded_string(item.get("url"), "configured feed URL", status="identity_refused")
        if name in configured:
            raise SourceRetirementError("identity_refused", "active release repeats a feed name")
        configured[name] = url
    reference = release.reference.get("config")
    if not isinstance(reference, Mapping):
        raise SourceRetirementError("identity_refused", "active configuration reference is malformed")

    try:
        assumed = sts_client.assume_role(
            RoleArn=role_arn,
            RoleSessionName=f"apcf-source-retirement-{feed_name}"[:64],
            DurationSeconds=3600,
            Tags=[{"Key": "FeedName", "Value": feed_name}],
        )
    except Exception as error:
        raise SourceRetirementError("identity_refused", "source-state retirement role assumption failed") from error
    credentials = assumed.get("Credentials")
    assumed_identity = assumed.get("AssumedRoleUser")
    if not isinstance(credentials, Mapping) or not isinstance(assumed_identity, Mapping):
        raise SourceRetirementError("identity_refused", "retirement role assumption response is malformed")
    role_session_arn = _bounded_string(assumed_identity.get("Arn"), "retirement role session ARN")
    expected_prefix = f"arn:aws:sts::{expected_account}:assumed-role/apcf-{deployment_id}-source-state-retirement/"
    if not role_session_arn.startswith(expected_prefix):
        raise SourceRetirementError("identity_refused", "retirement role session differs from the reviewed role")
    try:
        ddb_client = ddb_client_factory(
            region_name=region,
            aws_access_key_id=credentials["AccessKeyId"],
            aws_secret_access_key=credentials["SecretAccessKey"],
            aws_session_token=credentials["SessionToken"],
        )
    except Exception as error:
        raise SourceRetirementError("identity_refused", "retirement DynamoDB client could not be created") from error
    return (
        RetirementContext(
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
            feed_state_ttl_days=feed_days,
            configured_feeds=configured,
            deployment_sha256=sha256_bytes(deployment_body),
            terraform_output_sha256=sha256_bytes(output_body),
        ),
        ddb_client,
    )


def write_plan(path: Path, plan: Mapping[str, Any]) -> str:
    body = canonical_json(plan) + b"\n"
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_bytes(body)
        temporary.replace(path)
    except OSError as error:
        raise SourceRetirementError("local_write_failed", "source-retirement plan could not be written") from error
    return sha256_bytes(body)


def load_plan(path: Path, expected_sha256: str) -> dict[str, Any]:
    if _SHA256.fullmatch(expected_sha256) is None:
        raise SourceRetirementError("stale_plan", "expected plan SHA-256 is malformed")
    try:
        body = path.read_bytes()
    except OSError as error:
        raise SourceRetirementError("stale_plan", "saved source-retirement plan cannot be read") from error
    if sha256_bytes(body) != expected_sha256:
        raise SourceRetirementError("stale_plan", "saved source-retirement plan differs from its digest")
    try:
        document = load_unique_json(body)
    except Exception as error:
        raise SourceRetirementError("stale_plan", "saved source-retirement plan is malformed") from error
    if not isinstance(document, dict) or canonical_json(document) + b"\n" != body:
        raise SourceRetirementError("stale_plan", "saved source-retirement plan is not canonical JSON")
    return document


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preview or apply exact removed-feed lifecycle work.")
    parser.add_argument("action", choices=("preview", "apply"))
    parser.add_argument("--operation", choices=("retire", "compact", "restore"), required=True)
    parser.add_argument("--deployment", type=Path, required=True)
    parser.add_argument("--terraform-output", type=Path, required=True)
    parser.add_argument("--expected-account", required=True)
    parser.add_argument("--feed-name", required=True)
    parser.add_argument("--decision-id")
    parser.add_argument("--decision-at")
    parser.add_argument("--plan", type=Path, required=True)
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
        expected_plan_sha256 = arguments.expected_plan_sha256
        decision_id = arguments.decision_id
        decision_at_text = arguments.decision_at
        if arguments.action == "preview":
            if expected_plan_sha256 is not None:
                raise SourceRetirementError("invalid_input", "preview does not accept a plan digest")
            if not isinstance(decision_id, str) or not isinstance(decision_at_text, str):
                raise SourceRetirementError("invalid_input", "preview requires decision_id and decision_at")
        else:
            if decision_id is not None or decision_at_text is not None:
                raise SourceRetirementError("stale_plan", "apply takes decision inputs from the saved plan")
            if not isinstance(expected_plan_sha256, str):
                raise SourceRetirementError("stale_plan", "apply requires the expected plan SHA-256")

        if sts_client is None or s3_client is None or ddb_client_factory is None:
            import boto3

            sts_client = sts_client or boto3.client("sts")
            s3_client = s3_client or boto3.client("s3")
            ddb_client_factory = ddb_client_factory or (lambda **kwargs: boto3.client("dynamodb", **kwargs))

        saved: dict[str, Any] | None = None
        if arguments.action == "apply":
            assert isinstance(expected_plan_sha256, str)
            saved = load_plan(arguments.plan, expected_plan_sha256)
            if saved.get("action") != arguments.operation or saved.get("feed_name") != arguments.feed_name:
                raise SourceRetirementError("stale_plan", "operation or feed differs from the saved plan")
        context, ddb_client = load_context(
            deployment_path=arguments.deployment,
            terraform_output_path=arguments.terraform_output,
            expected_account=arguments.expected_account,
            feed_name=arguments.feed_name,
            sts_client=sts_client,
            s3_client=s3_client,
            ddb_client_factory=ddb_client_factory,
        )
        if arguments.action == "preview":
            from datetime import datetime

            assert isinstance(decision_at_text, str)
            assert isinstance(decision_id, str)
            try:
                decision_at = datetime.fromisoformat(decision_at_text.replace("Z", "+00:00"))
            except ValueError as error:
                raise SourceRetirementError("invalid_input", "decision_at is malformed") from error
            plan = create_plan(
                ddb_client,
                context=context,
                action=arguments.operation,
                feed_name=arguments.feed_name,
                decision_id=decision_id,
                decision_at=decision_at,
            )
            digest = write_plan(arguments.plan, plan)
            _write(
                {
                    "status": "previewed",
                    "operation": arguments.operation,
                    "feed_name": arguments.feed_name,
                    "release_id": context.release_id,
                    "plan_sha256": digest,
                    "target_state_version": plan["target"]["state_version"],
                }
            )
            return 0
        assert saved is not None
        assert isinstance(expected_plan_sha256, str)
        result = apply_plan(
            ddb_client,
            context=context,
            plan=saved,
            plan_sha256=expected_plan_sha256,
            context_verifier=lambda: verify_pointer_current(s3_client, context),
        )
        _write(result)
        if result["status"] in {"applied", "applied_after_reread"}:
            return 0
        return EXIT_AMBIGUOUS if result["status"] in {"ambiguous", "applied_unverified"} else EXIT_REFUSED
    except SourceRetirementError as error:
        _write({"status": error.status, "detail": error.detail}, stream=sys.stderr)
        if error.status in {"ambiguous", "applied_unverified"}:
            return EXIT_AMBIGUOUS
        if error.status in {"invalid_input", "local_write_failed"}:
            return EXIT_INVALID
        return EXIT_REFUSED


if __name__ == "__main__":
    raise SystemExit(main())
