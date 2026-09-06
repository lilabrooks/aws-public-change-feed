#!/usr/bin/env python3
"""Publish one content-addressed Lambda package without replacing old bytes."""

from __future__ import annotations

import argparse
import hashlib
import io
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any

RUNTIME_HANDLERS = (
    "aws_public_change_feed.dispatcher_runtime.lambda_handler",
    "aws_public_change_feed.recovery_runtime.lambda_handler",
    "aws_public_change_feed.shadow_runtime.lambda_handler",
    "aws_public_change_feed.slack_worker_runtime.lambda_handler",
    "aws_public_change_feed.watcher_runtime.lambda_handler",
)
RUNTIME_ENTRYPOINTS_METADATA_KEY = "runtime-entrypoints-sha256"


def runtime_entrypoints_sha256() -> str:
    """Bind one package to every Lambda handler configured by Terraform."""

    return hashlib.sha256("\0".join(RUNTIME_HANDLERS).encode()).hexdigest()


def _handler_path(handler: str) -> str:
    module, separator, function = handler.rpartition(".")
    if not separator or not module or not function:
        raise ValueError(f"invalid Lambda handler: {handler}")
    return f"{module.replace('.', '/')}.py"


def validate_runtime_entrypoints(body: bytes) -> None:
    """Reject package bytes that cannot supply every configured handler module."""

    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            names = archive.namelist()
    except zipfile.BadZipFile:
        raise ValueError("Lambda package must be a valid ZIP archive") from None
    duplicates = sorted(name for name, count in Counter(names).items() if count > 1)
    if duplicates:
        raise ValueError(f"Lambda package contains duplicate members: {duplicates}")
    missing = sorted({_handler_path(handler) for handler in RUNTIME_HANDLERS} - set(names))
    if missing:
        raise ValueError(f"Lambda package is missing configured handler modules: {missing}")


def publish(client: Any, *, bucket: str, prefix: str, package: Path) -> tuple[str, str, str]:
    body = package.read_bytes()
    validate_runtime_entrypoints(body)
    digest = hashlib.sha256(body).hexdigest()
    entrypoints_digest = runtime_entrypoints_sha256()
    key = f"{prefix.rstrip('/')}/{digest}.zip"
    try:
        response = client.put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            IfNoneMatch="*",
            Metadata={
                "sha256": digest,
                RUNTIME_ENTRYPOINTS_METADATA_KEY: entrypoints_digest,
            },
        )
        version_id = str(response["VersionId"])
    except Exception as error:  # noqa: BLE001 - the SDK class is imported only in main
        status = getattr(error, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode")
        if status not in (409, 412):
            raise
        existing = client.head_object(Bucket=bucket, Key=key)
        version_id = str(existing["VersionId"])
    stored = client.get_object(Bucket=bucket, Key=key, VersionId=version_id)
    stored_body = stored["Body"].read()
    stored_digest = hashlib.sha256(stored_body).hexdigest()
    metadata = stored.get("Metadata", {})
    if (
        metadata.get("sha256") != digest
        or metadata.get(RUNTIME_ENTRYPOINTS_METADATA_KEY) != entrypoints_digest
        or stored_digest != digest
    ):
        raise RuntimeError("content-addressed artifact key does not hold the matching package contract") from None
    validate_runtime_entrypoints(stored_body)
    return digest, key, version_id


def main() -> int:
    import boto3

    parser = argparse.ArgumentParser()
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--package", required=True, type=Path)
    arguments = parser.parse_args()
    digest, key, version_id = publish(
        boto3.client("s3"),
        bucket=arguments.bucket,
        prefix=arguments.prefix,
        package=arguments.package.resolve(),
    )
    print(f"worker_artifact_sha256={digest}")
    print(f"worker_artifact_key={key}")
    print(f"worker_artifact_version_id={version_id}")
    print(f"runtime_entrypoints_sha256={runtime_entrypoints_sha256()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
