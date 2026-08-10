#!/usr/bin/env python3
"""Build a deterministic Python 3.12 x86_64 Lambda package."""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_PACKAGE = ROOT / "src/aws_public_change_feed"
LOCK_FILE = ROOT / "requirements-lambda.txt"
FIXED_ZIP_TIME = (2026, 1, 1, 0, 0, 0)


def _require_exact_lock(path: Path) -> None:
    entries = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    unpinned = [line for line in entries if line and not line.startswith("#") and "==" not in line]
    if unpinned:
        raise ValueError(f"Lambda dependency lock has non-exact entries: {unpinned}")


def _archive_tree(staging: Path, output: Path) -> str:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(f"{output.suffix}.tmp")
    if temporary.exists():
        temporary.unlink()
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(staging.rglob("*"), key=lambda item: item.relative_to(staging).as_posix()):
            if path.is_dir() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            if path.is_symlink():
                raise ValueError(f"Lambda package cannot contain a symlink: {path}")
            relative = path.relative_to(staging).as_posix()
            info = zipfile.ZipInfo(relative, FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            with path.open("rb") as source:
                archive.writestr(info, source.read(), compresslevel=9)
    temporary.replace(output)
    return hashlib.sha256(output.read_bytes()).hexdigest()


def build(output: Path) -> str:
    _require_exact_lock(LOCK_FILE)
    with tempfile.TemporaryDirectory(prefix="apcf-lambda-") as raw_staging:
        staging = Path(raw_staging)
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--requirement",
                str(LOCK_FILE),
                "--target",
                str(staging),
                "--platform",
                "manylinux2014_x86_64",
                "--python-version",
                "3.12",
                "--implementation",
                "cp",
                "--only-binary=:all:",
                "--no-compile",
                "--no-deps",
                "--disable-pip-version-check",
            ],
            check=True,
        )
        shutil.copytree(SOURCE_PACKAGE, staging / SOURCE_PACKAGE.name)
        return _archive_tree(staging, output)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "build/slack-worker.zip")
    arguments = parser.parse_args()
    digest = build(arguments.output.resolve())
    print(f"sha256:{digest}  {arguments.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
