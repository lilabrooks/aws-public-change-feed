#!/usr/bin/env python3
"""Publish one content-addressed Lambda package without replacing old bytes."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
from typing import Any


def publish(client: Any, *, bucket: str, prefix: str, package: Path) -> tuple[str, str, str]:
    body = package.read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    key = f"{prefix.rstrip('/')}/{digest}.zip"
    try:
        response = client.put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            IfNoneMatch="*",
            Metadata={"sha256": digest},
        )
        version_id = str(response["VersionId"])
    except Exception as error:  # noqa: BLE001 - the SDK class is imported only in main
        status = getattr(error, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode")
        if status not in (409, 412):
            raise
        existing = client.head_object(Bucket=bucket, Key=key)
        version_id = str(existing["VersionId"])
    stored = client.get_object(Bucket=bucket, Key=key, VersionId=version_id)
    stored_digest = hashlib.sha256(stored["Body"].read()).hexdigest()
    if stored.get("Metadata", {}).get("sha256") != digest or stored_digest != digest:
        raise RuntimeError("content-addressed artifact key does not hold the matching package bytes") from None
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
