#!/usr/bin/env python3
"""Qualify the one retained pre-ADR-029 Lambda package for rollback only."""

from __future__ import annotations

import argparse
import hashlib
import subprocess
from pathlib import Path

from build_lambda_package import ROOT, RUNTIME_HANDLERS
from publish_lambda_artifact import _handler_path, _validate_handler, safe_archive_contents

LEGACY_ARTIFACT_SHA256 = "c88b49c8f070f1cb808ac005cbe28b484c14be7b29f50a34e21ef3a7ca85ccbd"
LEGACY_ARTIFACT_VERSION_ID = "QXNwt_NBIqp0pNKVFalwbZ72587h.GCc"
LEGACY_SOURCE_REVISION = "bb6add6e881249d75b4b7243e4def9528be23a47"


def _source_at_revision(repository_root: Path, revision: str) -> dict[str, bytes]:
    raw = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "-z", revision, "--", "src/aws_public_change_feed"],
        cwd=repository_root,
        check=True,
        capture_output=True,
    ).stdout
    paths = [value.decode("utf-8") for value in raw.split(b"\0") if value]
    return {
        path.removeprefix("src/"): subprocess.run(
            ["git", "show", f"{revision}:{path}"],
            cwd=repository_root,
            check=True,
            capture_output=True,
        ).stdout
        for path in paths
    }


def qualify_legacy_package(
    body: bytes,
    *,
    observed_version_id: str,
    repository_root: Path = ROOT,
) -> dict[str, str]:
    """Bind exact legacy bytes to their S3 version, source revision, and handlers."""

    if observed_version_id != LEGACY_ARTIFACT_VERSION_ID:
        raise ValueError("legacy Lambda package has the wrong S3 VersionId")
    digest = hashlib.sha256(body).hexdigest()
    if digest != LEGACY_ARTIFACT_SHA256:
        raise ValueError("legacy Lambda package bytes have the wrong SHA-256 digest")
    contents = safe_archive_contents(body)
    packaged_source = {name: value for name, value in contents.items() if name.startswith("aws_public_change_feed/")}
    revision_source = _source_at_revision(repository_root, LEGACY_SOURCE_REVISION)
    if set(packaged_source) != set(revision_source):
        raise ValueError("legacy Lambda package source member set does not match the fixed source revision")
    mismatched = sorted(name for name in packaged_source if packaged_source[name] != revision_source[name])
    if mismatched:
        raise ValueError(f"legacy Lambda package source bytes do not match the fixed source revision: {mismatched}")
    missing = sorted({_handler_path(handler) for handler in RUNTIME_HANDLERS} - set(contents))
    if missing:
        raise ValueError(f"legacy Lambda package is missing configured handler modules: {missing}")
    for handler in RUNTIME_HANDLERS:
        _validate_handler(contents[_handler_path(handler)], handler)
    return {
        "artifact_sha256": digest,
        "artifact_version_id": observed_version_id,
        "source_revision": LEGACY_SOURCE_REVISION,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", required=True, type=Path)
    parser.add_argument("--observed-version-id", required=True)
    arguments = parser.parse_args()
    result = qualify_legacy_package(
        arguments.package.resolve().read_bytes(),
        observed_version_id=arguments.observed_version_id,
    )
    for key, value in result.items():
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
