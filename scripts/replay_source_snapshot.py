#!/usr/bin/env python3
"""Preview or apply one exact retained-source replay."""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aws_public_change_feed.loading import load_release_version  # noqa: E402
from aws_public_change_feed.outbox import DynamoDBDeliveryStore  # noqa: E402
from aws_public_change_feed.parsing import load_unique_json  # noqa: E402
from aws_public_change_feed.releases import S3ObjectStore  # noqa: E402
from aws_public_change_feed.source_replay import (  # noqa: E402
    ReplayRefused,
    RetainedSnapshot,
    apply_source_replay,
    canonical_json,
    create_source_replay_plan,
    sha256_bytes,
)
from aws_public_change_feed.source_store import DynamoDBAnnouncementStateStore  # noqa: E402

EXIT_INVALID = 2
EXIT_REFUSED = 3
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SNAPSHOT_SUFFIX = re.compile(
    r"(?P<feed>[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?)/"
    r"(?P<observed>[0-9]{8}T[0-9]{6}\.[0-9]{6}Z)/"
    r"[A-Za-z0-9][A-Za-z0-9-]{0,127}/(?P<digest>[0-9a-f]{64})\.bin"
)


def _bounded(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048 or any(ord(char) < 32 for char in value):
        raise ReplayRefused("invalid_input", f"{name} must be a bounded nonempty string")
    return value


def _read_mapping(path: Path, name: str) -> tuple[dict[str, Any], bytes]:
    try:
        body = path.read_bytes()
        document = yaml.safe_load(body) if path.suffix in {".yaml", ".yml"} else load_unique_json(body)
    except Exception as error:
        raise ReplayRefused("invalid_input", f"{name} cannot be read or parsed") from error
    if not isinstance(document, dict):
        raise ReplayRefused("invalid_input", f"{name} must contain an object")
    return document, body


def _output(outputs: Mapping[str, Any], name: str) -> Any:
    value = outputs.get(name)
    if not isinstance(value, Mapping) or set(value) != {"sensitive", "type", "value"}:
        raise ReplayRefused("invalid_input", f"Terraform output {name} is missing or malformed")
    return value["value"]


def _validate_deployment(document: Mapping[str, Any]) -> None:
    try:
        schema = load_unique_json((ROOT / "schemas" / "deployment.schema.json").read_bytes())
        if not isinstance(schema, Mapping):
            raise ValueError("deployment schema is not an object")
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(document)
    except Exception as error:
        raise ReplayRefused("invalid_input", "deployment document failed validation") from error


def _positive(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ReplayRefused("invalid_input", f"{name} must be a positive integer")
    return value


def _assumed_clients(
    *,
    sts: Any,
    session_factory: Any,
    role_arn: str,
    expected_account: str,
    deployment_id: str,
    region: str,
) -> tuple[Any, Any]:
    try:
        assumed = sts.assume_role(
            RoleArn=role_arn,
            RoleSessionName="apcf-source-replay",
            DurationSeconds=3600,
        )
        credentials = assumed["Credentials"]
        session_arn = assumed["AssumedRoleUser"]["Arn"]
    except Exception as error:
        raise ReplayRefused("identity_refused", "source replay role assumption failed") from error
    expected = f"arn:aws:sts::{expected_account}:assumed-role/apcf-{deployment_id}-source-replay/"
    if not isinstance(session_arn, str) or not session_arn.startswith(expected):
        raise ReplayRefused("identity_refused", "source replay role session differs from the reviewed role")
    try:
        session = session_factory(
            region_name=region,
            aws_access_key_id=credentials["AccessKeyId"],
            aws_secret_access_key=credentials["SecretAccessKey"],
            aws_session_token=credentials["SessionToken"],
        )
        return session.client("s3"), session.client("dynamodb")
    except Exception as error:
        raise ReplayRefused("identity_refused", "source replay clients could not be created") from error


def _load_snapshot(s3: Any, *, bucket: str, prefix: str, key: str, max_bytes: int) -> RetainedSnapshot:
    if not key.startswith(prefix):
        raise ReplayRefused("invalid_input", "snapshot key is outside the reviewed raw-snapshot prefix")
    match = _SNAPSHOT_SUFFIX.fullmatch(key.removeprefix(prefix))
    if match is None:
        raise ReplayRefused("invalid_input", "snapshot key does not match the runtime key contract")
    try:
        response = s3.get_object(Bucket=bucket, Key=key)
        body = response["Body"].read(max_bytes + 1)
    except Exception as error:
        raise ReplayRefused("stale_plan", "retained snapshot cannot be read") from error
    if len(body) > max_bytes:
        raise ReplayRefused("invalid_input", "retained snapshot exceeds the deployment response limit")
    digest = hashlib.sha256(body).hexdigest()
    metadata = response.get("Metadata")
    if (
        digest != match["digest"]
        or not isinstance(metadata, Mapping)
        or metadata.get("body-sha256") != digest
        or metadata.get("feed-name") != match["feed"]
    ):
        raise ReplayRefused("stale_plan", "retained snapshot bytes or metadata disagree with its key")
    observed = datetime.strptime(match["observed"], "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=UTC)
    return RetainedSnapshot(key, match["feed"], observed, body, digest)


def _write_plan(path: Path, plan: Mapping[str, Any]) -> str:
    body = canonical_json(plan) + b"\n"
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_bytes(body)
        temporary.replace(path)
    except OSError as error:
        raise ReplayRefused("local_write_failed", "source replay plan could not be written") from error
    return sha256_bytes(body)


def _load_plan(path: Path, expected_sha256: str) -> dict[str, Any]:
    if _SHA256.fullmatch(expected_sha256) is None:
        raise ReplayRefused("stale_plan", "expected plan SHA-256 is malformed")
    try:
        body = path.read_bytes()
        document = load_unique_json(body)
    except Exception as error:
        raise ReplayRefused("stale_plan", "saved source replay plan cannot be read or parsed") from error
    if (
        sha256_bytes(body) != expected_sha256
        or not isinstance(document, dict)
        or canonical_json(document) + b"\n" != body
    ):
        raise ReplayRefused("stale_plan", "saved source replay plan differs from its reviewed bytes")
    return document


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preview or apply one exact retained-source replay.")
    parser.add_argument("action", choices=("preview", "apply"))
    parser.add_argument("--deployment", type=Path, required=True)
    parser.add_argument("--terraform-output", type=Path, required=True)
    parser.add_argument("--expected-account", required=True)
    parser.add_argument("--snapshot-key", required=True)
    parser.add_argument("--release-version-id", required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--operator")
    parser.add_argument("--purpose")
    parser.add_argument("--planned-at")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--expected-route", action="append", dest="expected_routes")
    scope.add_argument("--expect-no-routes", action="store_true")
    parser.add_argument("--expected-plan-sha256")
    return parser.parse_args(argv)


def _write(document: Mapping[str, Any], *, stream: Any = sys.stdout) -> None:
    print(canonical_json(document).decode("utf-8"), file=stream)


def main(
    argv: list[str] | None = None,
    *,
    sts_client: Any | None = None,
    session_factory: Any | None = None,
) -> int:
    arguments = parse_args(argv)
    try:
        if arguments.action == "preview":
            if not arguments.operator or not arguments.purpose or not arguments.planned_at:
                raise ReplayRefused("invalid_input", "preview requires operator, purpose, and planned_at")
            if arguments.expected_routes is None and not arguments.expect_no_routes:
                raise ReplayRefused("invalid_input", "preview requires an explicit expected route scope")
            if arguments.expected_plan_sha256 is not None:
                raise ReplayRefused("invalid_input", "preview does not accept a plan digest")
            saved = None
        else:
            if any(
                (
                    arguments.operator,
                    arguments.purpose,
                    arguments.planned_at,
                    arguments.expected_routes,
                    arguments.expect_no_routes,
                )
            ):
                raise ReplayRefused("stale_plan", "apply takes operator, purpose, time, and routes from the saved plan")
            if not arguments.expected_plan_sha256:
                raise ReplayRefused("stale_plan", "apply requires the expected plan SHA-256")
            saved = _load_plan(arguments.plan, arguments.expected_plan_sha256)

        deployment, deployment_body = _read_mapping(arguments.deployment, "deployment")
        outputs, output_body = _read_mapping(arguments.terraform_output, "Terraform output")
        _validate_deployment(deployment)
        if not re.fullmatch(r"[0-9]{12}", arguments.expected_account):
            raise ReplayRefused("invalid_input", "expected account must be 12 digits")
        if sts_client is None or session_factory is None:
            import boto3

            sts_client = sts_client or boto3.client("sts")
            session_factory = session_factory or boto3.Session
        try:
            identity = sts_client.get_caller_identity()
        except Exception as error:
            raise ReplayRefused("identity_refused", "caller identity could not be read") from error
        if identity.get("Account") != arguments.expected_account:
            raise ReplayRefused("identity_refused", "caller account differs from the reviewed account")

        deployment_id = _bounded(deployment.get("deployment_id"), "deployment ID")
        region = _bounded(deployment.get("deployment_region"), "deployment Region")
        bucket = _bounded(_output(outputs, "config_bucket_name"), "configuration bucket")
        if bucket != deployment.get("config_bucket_name"):
            raise ReplayRefused("identity_refused", "Terraform bucket differs from deployment")
        source_table = _bounded(_output(outputs, "source_state_table"), "source-state table")
        delivery_table = _bounded(_output(outputs, "delivery_table"), "delivery table")
        delivery_index = _bounded(_output(outputs, "delivery_index_name"), "delivery index")
        if source_table != f"apcf-source-state-{deployment_id}":
            raise ReplayRefused("identity_refused", "source-state table differs from deployment")
        if delivery_table != f"apcf-delivery-{deployment_id}":
            raise ReplayRefused("identity_refused", "delivery table differs from deployment")
        if delivery_index != "status-next-action-index":
            raise ReplayRefused("identity_refused", "delivery index differs from the runtime contract")
        roles = _output(outputs, "roles")
        if not isinstance(roles, Mapping):
            raise ReplayRefused("invalid_input", "Terraform roles output is malformed")
        role_arn = _bounded(roles.get("source_replay"), "source replay role ARN")
        expected_role = f"arn:aws:iam::{arguments.expected_account}:role/apcf-{deployment_id}-source-replay"
        if role_arn != expected_role:
            raise ReplayRefused("identity_refused", "source replay role differs from deployment")
        application_version = _bounded(_output(outputs, "watcher_application_version"), "watcher application version")
        if not application_version.startswith("sha256:") or _SHA256.fullmatch(application_version[7:]) is None:
            raise ReplayRefused("identity_refused", "watcher application version is malformed")
        policy = deployment.get("feed_fetch_policy")
        if not isinstance(policy, Mapping):
            raise ReplayRefused("invalid_input", "deployment feed policy is malformed")
        max_bytes = _positive(policy.get("max_response_bytes"), "max_response_bytes")
        max_items = _positive(policy.get("max_items_per_feed"), "max_items_per_feed")
        max_characters = _positive(policy.get("max_item_characters"), "max_item_characters")
        prefix = _bounded(_output(outputs, "raw_snapshot_prefix"), "raw snapshot prefix")
        pointer_key = _bounded(deployment.get("active_versions_object_key"), "active pointer key")
        pointer_directory = pointer_key.rpartition("/")[0]
        expected_prefix = f"{pointer_directory}/raw-snapshots/" if pointer_directory else "raw-snapshots/"
        if prefix != expected_prefix:
            raise ReplayRefused("identity_refused", "raw snapshot prefix differs from the active pointer directory")

        s3, ddb = _assumed_clients(
            sts=sts_client,
            session_factory=session_factory,
            role_arn=role_arn,
            expected_account=arguments.expected_account,
            deployment_id=deployment_id,
            region=region,
        )
        snapshot = _load_snapshot(
            s3,
            bucket=bucket,
            prefix=prefix,
            key=arguments.snapshot_key,
            max_bytes=max_bytes,
        )
        try:
            release = load_release_version(
                S3ObjectStore(s3, bucket),
                pointer_key=pointer_key,
                version_id=arguments.release_version_id,
                application_version=application_version,
            )
        except Exception as error:
            raise ReplayRefused("stale_plan", "exact retained release cannot be loaded and verified") from error
        announcement_store = DynamoDBAnnouncementStateStore(ddb, source_table)
        outbox = DynamoDBDeliveryStore(ddb, delivery_table, delivery_index)
        context = {
            "account_id": arguments.expected_account,
            "region": region,
            "bucket": bucket,
            "source_state_table": source_table,
            "delivery_table": delivery_table,
            "role_arn": role_arn,
            "deployment_sha256": sha256_bytes(deployment_body),
            "terraform_output_sha256": sha256_bytes(output_body),
        }

        if arguments.action == "preview":
            try:
                planned_at = datetime.fromisoformat(arguments.planned_at.replace("Z", "+00:00"))
            except ValueError as error:
                raise ReplayRefused("invalid_input", "planned_at is malformed") from error
            plan = create_source_replay_plan(
                snapshot,
                release,
                pointer_key=pointer_key,
                pointer_version_id=arguments.release_version_id,
                application_version=application_version,
                max_items=max_items,
                max_item_characters=max_characters,
                planned_at=planned_at,
                operator=arguments.operator,
                purpose=arguments.purpose,
                expected_route_ids=arguments.expected_routes or (),
                announcement_state=announcement_store,
                outbox=outbox,
                context=context,
            )
            digest = _write_plan(arguments.plan, plan)
            _write({"status": "previewed", "plan_sha256": digest, **plan["result"]})
            return 0
        assert saved is not None
        result = apply_source_replay(
            snapshot,
            release,
            saved,
            pointer_key=pointer_key,
            pointer_version_id=arguments.release_version_id,
            application_version=application_version,
            max_items=max_items,
            max_item_characters=max_characters,
            announcement_state=announcement_store,
            outbox=outbox,
            context=context,
        )
        _write(result)
        return 0
    except ReplayRefused as error:
        _write({"status": error.status, "detail": error.detail}, stream=sys.stderr)
        return EXIT_INVALID if error.status in {"invalid_input", "local_write_failed"} else EXIT_REFUSED
    except ValueError as error:
        _write({"status": "invalid_input", "detail": str(error)}, stream=sys.stderr)
        return EXIT_INVALID


if __name__ == "__main__":
    raise SystemExit(main())
