#!/usr/bin/env python3
"""Preview and apply one exact configuration release."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import validate_config as validator  # noqa: E402

from aws_public_change_feed.identity import application_artifact_id, release_id  # noqa: E402
from aws_public_change_feed.loading import (  # noqa: E402
    LoadedRelease,
    probe_release,
)
from aws_public_change_feed.parsing import load_unique_json  # noqa: E402
from aws_public_change_feed.releases import (  # noqa: E402
    ObjectMissing,
    ObjectStore,
    PointerVanished,
    PreconditionFailed,
    Promotion,
    PromotionSuperseded,
    ReleaseArtifacts,
    S3ObjectStore,
    StoredObject,
    WriteConflict,
    promote_pointer,
    publish_objects,
    release_keys,
)

EXIT_INVALID = 2
EXIT_REFUSED = 3
EXIT_AMBIGUOUS = 4
PLAN_VERSION = 1
INVENTORY_SCHEMA_VERSION = 3
SHA256_RE = re.compile(r"[a-f0-9]{64}")


class ReleaseCommandError(RuntimeError):
    """One bounded operator-facing refusal with no provider response text."""

    def __init__(self, status: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(frozen=True, slots=True)
class LocalReleaseInputs:
    deployment_path: Path
    config_path: Path
    terraform_output_path: Path
    inventory_path: Path
    deployment: Mapping[str, Any]
    config: Mapping[str, Any]
    terraform_outputs: Mapping[str, Any]
    deployment_body: bytes
    config_body: bytes
    terraform_output_body: bytes
    inventory_body: bytes
    generated_at: str


class OutcomeRecordingStore:
    """Record the typed write outcome that the release core consumes."""

    def __init__(self, delegate: ObjectStore) -> None:
        self.delegate = delegate
        self.last_create_outcome: str | None = None
        self.last_replace_outcome: str | None = None

    def reset(self) -> None:
        self.last_create_outcome = None
        self.last_replace_outcome = None

    def create(self, key: str, body: bytes) -> str:
        try:
            result = self.delegate.create(key, body)
        except PreconditionFailed:
            self.last_create_outcome = "precondition_failed"
            raise
        except WriteConflict:
            self.last_create_outcome = "write_conflict"
            raise
        self.last_create_outcome = "created"
        return result

    def read(self, key: str, version_id: str | None = None) -> StoredObject:
        return self.delegate.read(key, version_id)

    def replace(self, key: str, body: bytes, *, if_match: str) -> str:
        try:
            result = self.delegate.replace(key, body, if_match=if_match)
        except PreconditionFailed:
            self.last_replace_outcome = "precondition_failed"
            raise
        except WriteConflict:
            self.last_replace_outcome = "write_conflict"
            raise
        except ObjectMissing:
            self.last_replace_outcome = "object_missing"
            raise
        self.last_replace_outcome = "replaced"
        return result


def canonical_json(document: Mapping[str, Any]) -> bytes:
    return json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _utc_timestamp(value: object, field: str, *, status: str = "invalid_input") -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ReleaseCommandError(status, f"{field} must be an explicit UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ReleaseCommandError(status, f"{field} must be an explicit UTC timestamp ending in Z") from error
    normalized = parsed.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed) or value != normalized:
        raise ReleaseCommandError(status, f"{field} must be an explicit UTC timestamp ending in Z")
    return normalized


def _path(value: object, field: str) -> Path:
    if not isinstance(value, str) or not value or any(character in value for character in "\r\n\x00"):
        raise ReleaseCommandError("stale_plan", f"saved {field} path is malformed")
    return Path(value)


def _atomic_write(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_bytes(body)
        temporary.replace(path)
    except OSError as error:
        raise ReleaseCommandError("local_write_failed", "preview artifact could not be written") from error


def generate_inventory(deployment: Mapping[str, Any], generated_at: str) -> dict[str, Any]:
    """Project only deployment-owned runtime fields into inventory schema v3."""

    generated_at = _utc_timestamp(generated_at, "generated_at")
    try:
        return {
            "schema_version": INVENTORY_SCHEMA_VERSION,
            "deployment_id": deployment["deployment_id"],
            "generated_at": generated_at,
            "deployment_region": deployment["deployment_region"],
            "slack": copy.deepcopy(deployment["slack"]),
            "environments": [
                {key: copy.deepcopy(environment[key]) for key in validator.INVENTORY_ENVIRONMENT_KEYS}
                for environment in deployment["environments"]
            ],
        }
    except (KeyError, TypeError) as error:
        raise ReleaseCommandError("invalid_input", "deployment cannot produce inventory schema version 3") from error


def _load_terraform_outputs(path: Path, deployment: Mapping[str, Any]) -> tuple[Mapping[str, Any], bytes]:
    try:
        body = path.read_bytes()
        document = load_unique_json(body)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        raise ReleaseCommandError("invalid_input", "Terraform output capture is malformed") from error
    if not isinstance(document, Mapping):
        raise ReleaseCommandError("invalid_input", "Terraform output capture must be an object")
    expected = {
        "config_bucket_name": deployment.get("config_bucket_name"),
        "release_prefix": f"{str(deployment.get('release_prefix', '')).rstrip('/')}/",
    }
    for name, expected_value in expected.items():
        output = document.get(name)
        if not isinstance(output, Mapping) or set(output) != {"sensitive", "type", "value"}:
            raise ReleaseCommandError("invalid_input", f"Terraform output {name} is missing or malformed")
        if output["sensitive"] is not False or output["type"] != "string" or output["value"] != expected_value:
            raise ReleaseCommandError("invalid_input", f"Terraform output {name} differs from the reviewed deployment")
    return document, body


def _validate_bundle(
    deployment_path: Path,
    config_path: Path,
    inventory_path: Path,
    deployment: Mapping[str, Any],
    config: Mapping[str, Any],
    inventory: Mapping[str, Any],
) -> None:
    try:
        validator.validate_schema(ROOT / "schemas/deployment.schema.json", deployment_path, deployment)
        validator.validate_schema(ROOT / "schemas/config.schema.json", config_path, config)
        validator.validate_schema(ROOT / "schemas/inventory.schema.json", inventory_path, inventory)
        validator.validate_semantics(deployment, config, inventory)
    except Exception as error:
        raise ReleaseCommandError(
            "invalid_input", "deployment, configuration, and inventory failed validation"
        ) from error


def load_local_inputs(
    *,
    deployment_path: Path,
    config_path: Path,
    terraform_output_path: Path,
    inventory_path: Path,
    generated_at: str,
) -> LocalReleaseInputs:
    generated_at = _utc_timestamp(generated_at, "generated_at")
    try:
        deployment_body = deployment_path.read_bytes()
        config_body = config_path.read_bytes()
        deployment = validator.load_yaml(deployment_path)
        config = validator.load_yaml(config_path)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        raise ReleaseCommandError("invalid_input", "deployment or configuration document is malformed") from error
    if not isinstance(deployment, Mapping) or not isinstance(config, Mapping):
        raise ReleaseCommandError("invalid_input", "deployment and configuration documents must be objects")
    terraform_outputs, terraform_output_body = _load_terraform_outputs(terraform_output_path, deployment)
    inventory = generate_inventory(deployment, generated_at)
    inventory_body = canonical_json(inventory) + b"\n"
    _validate_bundle(deployment_path, config_path, inventory_path, deployment, config, inventory)
    return LocalReleaseInputs(
        deployment_path=deployment_path.resolve(),
        config_path=config_path.resolve(),
        terraform_output_path=terraform_output_path.resolve(),
        inventory_path=inventory_path.resolve(),
        deployment=deployment,
        config=config,
        terraform_outputs=terraform_outputs,
        deployment_body=deployment_body,
        config_body=config_body,
        terraform_output_body=terraform_output_body,
        inventory_body=inventory_body,
        generated_at=generated_at,
    )


def _pointer_document(body: bytes) -> Mapping[str, Any] | None:
    try:
        document = load_unique_json(body)
    except (UnicodeError, ValueError, json.JSONDecodeError):
        return None
    return document if isinstance(document, Mapping) else None


def pointer_identity(observed: StoredObject | None) -> dict[str, Any]:
    if observed is None:
        return {"status": "missing"}
    document = _pointer_document(observed.body)
    named_release = document.get("release_id") if document is not None else None
    return {
        "status": "present",
        "etag": observed.etag,
        "version_id": observed.version_id,
        "sha256": sha256_bytes(observed.body),
        "release_id": named_release if isinstance(named_release, str) else None,
    }


def observe_pointer(store: ObjectStore, pointer_key: str) -> StoredObject | None:
    try:
        return store.read(pointer_key)
    except ObjectMissing:
        return None


def _require_forward_time(promoted_at: str, observed: StoredObject | None, *, status: str = "invalid_input") -> None:
    if observed is None:
        return
    document = _pointer_document(observed.body)
    current_raw = document.get("promoted_at") if document is not None else None
    if not isinstance(current_raw, str):
        return
    try:
        current = datetime.fromisoformat(current_raw.replace("Z", "+00:00"))
    except ValueError:
        return
    proposed = datetime.fromisoformat(promoted_at.replace("Z", "+00:00"))
    if current.tzinfo is not None and proposed <= current:
        raise ReleaseCommandError(status, "promoted_at must follow the pointer being replaced")


def _build_plan(
    local: LocalReleaseInputs,
    *,
    application_version: str,
    promoted_at: str,
    observed: StoredObject | None,
    error_status: str = "invalid_input",
) -> dict[str, Any]:
    try:
        application_version = application_artifact_id(application_version)
    except ValueError as error:
        raise ReleaseCommandError(error_status, "selected application digest is malformed") from error
    promoted_at = _utc_timestamp(promoted_at, "promoted_at", status=error_status)
    _require_forward_time(promoted_at, observed, status=error_status)
    config_sha256 = sha256_bytes(local.config_body)
    inventory_sha256 = sha256_bytes(local.inventory_body)
    identifier = release_id(config_sha256, inventory_sha256)
    release_prefix = str(local.deployment["release_prefix"])
    config_filename = str(local.deployment["config_filename"])
    inventory_filename = str(local.deployment["inventory_filename"])
    config_key, inventory_key = release_keys(release_prefix, identifier, config_filename, inventory_filename)
    return {
        "plan_version": PLAN_VERSION,
        "inputs": {
            "deployment": {"path": str(local.deployment_path), "sha256": sha256_bytes(local.deployment_body)},
            "config": {"path": str(local.config_path), "sha256": config_sha256},
            "terraform_output": {
                "path": str(local.terraform_output_path),
                "sha256": sha256_bytes(local.terraform_output_body),
            },
            "inventory": {"path": str(local.inventory_path), "sha256": inventory_sha256},
        },
        "target": {
            "bucket": local.deployment["config_bucket_name"],
            "region": local.deployment["deployment_region"],
            "release_prefix": release_prefix,
            "pointer_key": local.deployment["active_versions_object_key"],
            "config_filename": config_filename,
            "inventory_filename": inventory_filename,
        },
        "generated_at": local.generated_at,
        "promoted_at": promoted_at,
        "application_version": application_version,
        "release_id": identifier,
        "objects": {
            "config": {
                "key": config_key,
                "sha256": config_sha256,
                "schema_version": int(local.config["version"]),
            },
            "inventory": {
                "key": inventory_key,
                "sha256": inventory_sha256,
                "schema_version": INVENTORY_SCHEMA_VERSION,
            },
        },
        "current_pointer": pointer_identity(observed),
    }


def create_preview(
    store: ObjectStore,
    *,
    deployment_path: Path,
    config_path: Path,
    terraform_output_path: Path,
    inventory_path: Path,
    application_version: str,
    generated_at: str,
    promoted_at: str,
) -> tuple[dict[str, Any], bytes]:
    local = load_local_inputs(
        deployment_path=deployment_path,
        config_path=config_path,
        terraform_output_path=terraform_output_path,
        inventory_path=inventory_path,
        generated_at=generated_at,
    )
    observed = observe_pointer(store, str(local.deployment["active_versions_object_key"]))
    return (
        _build_plan(local, application_version=application_version, promoted_at=promoted_at, observed=observed),
        local.inventory_body,
    )


def write_preview(plan_path: Path, inventory_path: Path, plan: Mapping[str, Any], inventory_body: bytes) -> str:
    plan_body = canonical_json(plan) + b"\n"
    _atomic_write(inventory_path, inventory_body)
    _atomic_write(plan_path, plan_body)
    return sha256_bytes(plan_body)


def load_plan(path: Path, expected_sha256: str) -> dict[str, Any]:
    if SHA256_RE.fullmatch(expected_sha256) is None:
        raise ReleaseCommandError("stale_plan", "expected plan SHA-256 is malformed")
    try:
        body = path.read_bytes()
    except OSError as error:
        raise ReleaseCommandError("stale_plan", "saved plan cannot be read") from error
    if sha256_bytes(body) != expected_sha256:
        raise ReleaseCommandError("stale_plan", "saved plan SHA-256 differs from the expected digest")
    try:
        plan = load_unique_json(body)
    except (UnicodeError, ValueError, json.JSONDecodeError) as error:
        raise ReleaseCommandError("stale_plan", "saved plan is malformed") from error
    if not isinstance(plan, dict) or canonical_json(plan) + b"\n" != body:
        raise ReleaseCommandError("stale_plan", "saved plan is not canonical JSON")
    return plan


def _require_plan_shape(plan: Mapping[str, Any]) -> None:
    required = {
        "plan_version",
        "inputs",
        "target",
        "generated_at",
        "promoted_at",
        "application_version",
        "release_id",
        "objects",
        "current_pointer",
    }
    if set(plan) != required or plan.get("plan_version") != PLAN_VERSION:
        raise ReleaseCommandError("stale_plan", "saved plan shape is not supported")
    inputs = plan.get("inputs")
    target = plan.get("target")
    objects = plan.get("objects")
    if not isinstance(inputs, Mapping) or set(inputs) != {"deployment", "config", "terraform_output", "inventory"}:
        raise ReleaseCommandError("stale_plan", "saved plan inputs are malformed")
    if not isinstance(target, Mapping) or set(target) != {
        "bucket",
        "region",
        "release_prefix",
        "pointer_key",
        "config_filename",
        "inventory_filename",
    }:
        raise ReleaseCommandError("stale_plan", "saved plan target is malformed")
    if not isinstance(objects, Mapping) or set(objects) != {"config", "inventory"}:
        raise ReleaseCommandError("stale_plan", "saved plan objects are malformed")
    for name in ("deployment", "config", "terraform_output", "inventory"):
        value = inputs.get(name)
        if not isinstance(value, Mapping) or set(value) != {"path", "sha256"}:
            raise ReleaseCommandError("stale_plan", f"saved {name} input is malformed")


def load_plan_local_inputs(plan: Mapping[str, Any]) -> LocalReleaseInputs:
    """Validate every saved local identity before constructing an AWS client."""

    _require_plan_shape(plan)
    inputs = plan["inputs"]
    local = load_local_inputs(
        deployment_path=_path(inputs["deployment"]["path"], "deployment"),
        config_path=_path(inputs["config"]["path"], "configuration"),
        terraform_output_path=_path(inputs["terraform_output"]["path"], "Terraform output"),
        inventory_path=_path(inputs["inventory"]["path"], "inventory"),
        generated_at=_utc_timestamp(plan.get("generated_at"), "generated_at", status="stale_plan"),
    )
    try:
        saved_inventory = local.inventory_path.read_bytes()
    except OSError as error:
        raise ReleaseCommandError("stale_plan", "generated inventory cannot be read") from error
    if saved_inventory != local.inventory_body:
        raise ReleaseCommandError("stale_plan", "generated inventory differs from the preview")
    static = _build_plan(
        local,
        application_version=str(plan.get("application_version")),
        promoted_at=_utc_timestamp(plan.get("promoted_at"), "promoted_at", status="stale_plan"),
        observed=None,
        error_status="stale_plan",
    )
    static["current_pointer"] = plan["current_pointer"]
    if canonical_json(static) != canonical_json(plan):
        raise ReleaseCommandError("stale_plan", "saved local release identities differ from the preview")
    return local


def rebuild_plan(
    store: ObjectStore, plan: Mapping[str, Any]
) -> tuple[dict[str, Any], LocalReleaseInputs, StoredObject | None]:
    local = load_plan_local_inputs(plan)
    pointer_key = str(local.deployment["active_versions_object_key"])
    observed = observe_pointer(store, pointer_key)
    try:
        rebuilt = _build_plan(
            local,
            application_version=str(plan.get("application_version")),
            promoted_at=_utc_timestamp(plan.get("promoted_at"), "promoted_at", status="stale_plan"),
            observed=observed,
            error_status="stale_plan",
        )
    except ValueError as error:
        raise ReleaseCommandError("stale_plan", "saved release plan contains malformed values") from error
    if canonical_json(rebuilt) != canonical_json(plan):
        raise ReleaseCommandError("stale_plan", "release inputs or pointer decision differ from the preview")
    return rebuilt, local, observed


def _published_result(artifacts: ReleaseArtifacts) -> dict[str, Any]:
    return {
        "release_id": artifacts.release_id,
        "config": {
            "key": artifacts.config.key,
            "version_id": artifacts.config.version_id,
            "sha256": artifacts.config.sha256,
            "adopted": artifacts.config.adopted,
        },
        "inventory": {
            "key": artifacts.inventory.key,
            "version_id": artifacts.inventory.version_id,
            "sha256": artifacts.inventory.sha256,
            "adopted": artifacts.inventory.adopted,
        },
    }


def _promotion_result(promotion: Promotion) -> dict[str, Any]:
    return {
        "status": "converged" if promotion.converged else "promoted",
        "release_id": promotion.release_id,
        "new_version_id": promotion.new_version_id,
        "prior_version_id": promotion.prior_version_id,
        "prior_release_id": promotion.prior_release_id,
    }


def _partial_result(
    status: str,
    detail: str,
    artifacts: ReleaseArtifacts,
) -> dict[str, Any]:
    return {
        "status": status,
        "detail": detail,
        "release_objects": _published_result(artifacts),
    }


def apply_release(
    store: ObjectStore,
    plan: Mapping[str, Any],
    *,
    probe: Callable[..., LoadedRelease] = probe_release,
) -> dict[str, Any]:
    rebuilt, local, observed = rebuild_plan(store, plan)
    recorder = OutcomeRecordingStore(store)
    try:
        artifacts = publish_objects(
            recorder,
            config_body=local.config_body,
            inventory_body=local.inventory_body,
            config_schema_version=int(local.config["version"]),
            inventory_schema_version=INVENTORY_SCHEMA_VERSION,
            release_prefix=str(local.deployment["release_prefix"]),
            config_filename=str(local.deployment["config_filename"]),
            inventory_filename=str(local.deployment["inventory_filename"]),
        )
    except WriteConflict:
        raise ReleaseCommandError("publication_conflict", "release object creation remained indeterminate") from None
    except (ObjectMissing, PreconditionFailed, ValueError):
        raise ReleaseCommandError(
            "publication_refused", "release objects could not be created or adopted exactly"
        ) from None

    recorder.reset()
    pointer_body = canonical_json(
        artifacts.pointer_document(datetime.fromisoformat(str(rebuilt["promoted_at"]).replace("Z", "+00:00")))
    )
    try:
        promotion = promote_pointer(
            recorder,
            pointer_key=str(rebuilt["target"]["pointer_key"]),
            document=pointer_body,
            observed=observed,
        )
    except PromotionSuperseded as error:
        conflict = recorder.last_replace_outcome == "write_conflict" or recorder.last_create_outcome == "write_conflict"
        result = _partial_result(
            "promotion_conflict" if conflict else "promotion_superseded",
            (
                "promotion conflict resolved to another active release"
                if conflict
                else "pointer changed before the planned promotion"
            ),
            artifacts,
        )
        result["active_release_id"] = error.observed
        return result
    except PointerVanished:
        return _partial_result(
            "pointer_vanished",
            "the planned current pointer vanished before promotion",
            artifacts,
        )
    except (ValueError, ObjectMissing, PreconditionFailed, WriteConflict):
        return _partial_result(
            "promotion_refused",
            "the planned pointer promotion did not produce a bounded active result",
            artifacts,
        )

    promotion_result = _promotion_result(promotion)
    try:
        loaded = probe(
            recorder,
            pointer_key=str(rebuilt["target"]["pointer_key"]),
            application_version=str(rebuilt["application_version"]),
            expected_release_id=artifacts.release_id,
        )
    except Exception:  # noqa: BLE001 - preserve the bounded promoted-but-unproved result
        return {
            "status": "probe_failed",
            "detail": "the active pointer result failed the required compatibility probe",
            "release_objects": _published_result(artifacts),
            "promotion": promotion_result,
        }
    return {
        "status": "completed",
        "detail": "the active release passed the compatibility probe",
        "release_objects": _published_result(artifacts),
        "promotion": promotion_result,
        "probed_release_id": loaded.release_id,
        "application_version": rebuilt["application_version"],
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preview or apply one exact configuration release.")
    subparsers = parser.add_subparsers(dest="action", required=True)
    preview = subparsers.add_parser("preview", help="validate inputs and write a read-only release plan")
    preview.add_argument("--deployment", type=Path, required=True)
    preview.add_argument("--config", type=Path, required=True)
    preview.add_argument("--terraform-output", type=Path, required=True)
    preview.add_argument("--inventory", type=Path, required=True)
    preview.add_argument("--plan", type=Path, required=True)
    preview.add_argument("--application-version", required=True)
    preview.add_argument("--generated-at", required=True)
    preview.add_argument("--promoted-at", required=True)
    apply = subparsers.add_parser("apply", help="apply an unchanged preview plan and probe the active release")
    apply.add_argument("--plan", type=Path, required=True)
    apply.add_argument("--expected-plan-sha256", required=True)
    return parser.parse_args(argv)


def _write(document: Mapping[str, Any], *, stream: Any | None = None) -> None:
    print(
        canonical_json(document).decode("utf-8"),
        file=sys.stdout if stream is None else stream,
    )


def _store(bucket: str, region: str) -> S3ObjectStore:
    import boto3

    return S3ObjectStore(boto3.client("s3", region_name=region), bucket)


def main(argv: list[str] | None = None, *, store: ObjectStore | None = None) -> int:
    arguments = parse_args(argv)
    try:
        if arguments.action == "preview":
            local = load_local_inputs(
                deployment_path=arguments.deployment,
                config_path=arguments.config,
                terraform_output_path=arguments.terraform_output,
                inventory_path=arguments.inventory,
                generated_at=arguments.generated_at,
            )
            active_store = store or _store(
                str(local.deployment["config_bucket_name"]), str(local.deployment["deployment_region"])
            )
            observed = observe_pointer(active_store, str(local.deployment["active_versions_object_key"]))
            plan = _build_plan(
                local,
                application_version=arguments.application_version,
                promoted_at=arguments.promoted_at,
                observed=observed,
            )
            digest = write_preview(arguments.plan, arguments.inventory, plan, local.inventory_body)
            _write(
                {
                    "status": "previewed",
                    "plan_sha256": digest,
                    "release_id": plan["release_id"],
                    "config_key": plan["objects"]["config"]["key"],
                    "inventory_key": plan["objects"]["inventory"]["key"],
                    "current_pointer": plan["current_pointer"],
                }
            )
            return 0
        plan = load_plan(arguments.plan, arguments.expected_plan_sha256)
        local = load_plan_local_inputs(plan)
        active_store = store or _store(
            str(local.deployment["config_bucket_name"]), str(local.deployment["deployment_region"])
        )
        result = apply_release(active_store, plan)
        _write(result)
        return 0 if result["status"] == "completed" else EXIT_REFUSED
    except ReleaseCommandError as error:
        _write({"status": error.status, "detail": error.detail}, stream=sys.stderr)
        return EXIT_INVALID if error.status in {"invalid_input", "local_write_failed"} else EXIT_REFUSED
    except Exception:  # noqa: BLE001 - CLI output must not expose provider or credential details
        _write(
            {"status": "provider_error", "detail": "release operation failed without a safe bounded result"},
            stream=sys.stderr,
        )
        return EXIT_AMBIGUOUS


if __name__ == "__main__":
    raise SystemExit(main())
