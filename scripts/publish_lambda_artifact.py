#!/usr/bin/env python3
"""Publish one content-addressed Lambda package without replacing old bytes."""

from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import importlib.metadata
import io
import json
import stat
import subprocess
import sys
import zipfile
import zlib
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any

from build_lambda_package import (
    MANIFEST_PATH,
    ROOT,
    RUNTIME_HANDLERS,
    manifest_bytes,
    runtime_entrypoints_sha256,
)

RUNTIME_ENTRYPOINTS_METADATA_KEY = "runtime-entrypoints-sha256"
MANIFEST_METADATA_KEY = "manifest-sha256"


def _handler_path(handler: str) -> str:
    module, separator, function = handler.rpartition(".")
    if not separator or not module or not function:
        raise ValueError(f"invalid Lambda handler: {handler}")
    return f"{module.replace('.', '/')}.py"


def _git_blob(repository_root: Path, path: str) -> bytes:
    return subprocess.run(["git", "show", f"HEAD:{path}"], cwd=repository_root, check=True, capture_output=True).stdout


def _tracked_source(repository_root: Path) -> dict[str, bytes]:
    raw = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "-z", "HEAD", "--", "src/aws_public_change_feed"],
        cwd=repository_root,
        check=True,
        capture_output=True,
    ).stdout
    paths = [value.decode("utf-8") for value in raw.split(b"\0") if value]
    return {path.removeprefix("src/"): _git_blob(repository_root, path) for path in paths}


def _working_source(repository_root: Path) -> dict[str, bytes]:
    source_root = repository_root / "src/aws_public_change_feed"
    return {
        path.relative_to(repository_root / "src").as_posix(): path.read_bytes()
        for path in source_root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }


def publication_attestations(repository_root: Path = ROOT) -> dict[str, str]:
    def revision(value: str) -> str:
        return subprocess.run(
            ["git", "rev-parse", value], cwd=repository_root, check=True, capture_output=True, text=True
        ).stdout.strip()

    return {
        "builder-git-commit": revision("HEAD"),
        "builder-git-tree": revision("HEAD^{tree}"),
        "builder-pip": importlib.metadata.version("pip"),
        "builder-python": sys.version.split()[0],
        "builder-zlib": zlib.ZLIB_VERSION,
    }


class _ModuleBindingVisitor(ast.NodeVisitor):
    """Find bindings visible at module scope without descending into nested scopes."""

    def __init__(self, name: str):
        self.name = name
        self.found = False

    def visit_Name(self, node: ast.Name) -> None:
        if node.id == self.name and isinstance(node.ctx, (ast.Store, ast.Del)):
            self.found = True

    def _visit_function_header(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        if node.name == self.name:
            self.found = True
        for value in (*node.decorator_list, *node.args.defaults, *node.args.kw_defaults):
            if value is not None:
                self.visit(value)
        annotations = [
            argument.annotation for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
        ]
        annotations.extend(
            argument.annotation for argument in (node.args.vararg, node.args.kwarg) if argument is not None
        )
        if node.returns is not None:
            annotations.append(node.returns)
        for annotation in annotations:
            if annotation is not None:
                self.visit(annotation)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function_header(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function_header(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if node.name == self.name:
            self.found = True
        for value in (*node.decorator_list, *node.bases, *(keyword.value for keyword in node.keywords)):
            self.visit(value)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for value in (*node.args.defaults, *node.args.kw_defaults):
            if value is not None:
                self.visit(value)

    def _visit_comprehension(self, value: ast.expr, generators: list[ast.comprehension]) -> None:
        self.visit(value)
        for generator in generators:
            self.visit(generator.iter)
            for condition in generator.ifs:
                self.visit(condition)

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_comprehension(node.elt, node.generators)

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._visit_comprehension(node.elt, node.generators)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._visit_comprehension(node.elt, node.generators)

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._visit_comprehension(node.key, node.generators)
        self.visit(node.value)

    def visit_Import(self, node: ast.Import) -> None:
        if any((alias.asname or alias.name.partition(".")[0]) == self.name for alias in node.names):
            self.found = True

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if any(alias.name == "*" or (alias.asname or alias.name) == self.name for alias in node.names):
            self.found = True

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name == self.name:
            self.found = True
        self.generic_visit(node)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:
        if node.name == self.name:
            self.found = True
        self.generic_visit(node)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:
        if node.name == self.name:
            self.found = True

    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
        if node.rest == self.name:
            self.found = True
        self.generic_visit(node)


def _binds_module_name(statement: ast.stmt, name: str) -> bool:
    visitor = _ModuleBindingVisitor(name)
    visitor.visit(statement)
    return visitor.found


def _validate_handler(source: bytes, handler: str) -> None:
    module, _, function = handler.rpartition(".")
    try:
        tree = ast.parse(source, filename=f"{module}.py", feature_version=(3, 12))
    except (SyntaxError, ValueError) as error:
        raise ValueError(f"configured handler module is not valid Python 3.12: {module}") from error
    definitions = [
        (index, node)
        for index, node in enumerate(tree.body)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function
    ]
    if not definitions:
        raise ValueError(f"configured handler has no supported top-level function declaration: {handler}")
    if len(definitions) != 1:
        raise ValueError(f"configured handler is declared more than once: {handler}")
    index, node = definitions[0]
    if isinstance(node, ast.AsyncFunctionDef):
        raise ValueError(f"configured handler cannot be async: {handler}")
    if node.decorator_list:
        raise ValueError(f"configured handler declaration cannot be decorated: {handler}")
    arguments = node.args
    if arguments.vararg or arguments.kwarg or arguments.kwonlyargs or len(arguments.posonlyargs + arguments.args) != 2:
        raise ValueError(f"configured handler must declare exactly two positional arguments: {handler}")
    for later in tree.body[index + 1 :]:
        if _binds_module_name(later, function):
            raise ValueError(f"configured handler is rebound after declaration: {handler}")


def validate_package(body: bytes, *, repository_root: Path = ROOT) -> dict[str, Any]:
    """Verify archive safety, manifest fields, source identity, and handler declarations."""

    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            members = archive.infolist()
            names = [member.filename for member in members]
            contents = {member.filename: archive.read(member) for member in members if not member.is_dir()}
    except zipfile.BadZipFile:
        raise ValueError("Lambda package must be a valid ZIP archive") from None
    duplicates = sorted(name for name, count in Counter(names).items() if count > 1)
    if duplicates:
        raise ValueError(f"Lambda package contains duplicate members: {duplicates}")
    for member in members:
        archive_path = PurePosixPath(member.filename)
        mode = member.external_attr >> 16
        if (
            "\\" in member.filename
            or archive_path.is_absolute()
            or ".." in archive_path.parts
            or (archive_path.parts and archive_path.parts[0].endswith(":"))
        ):
            raise ValueError(f"Lambda package contains an unsafe member path: {member.filename}")
        if member.is_dir() or not stat.S_ISREG(mode):
            raise ValueError(f"Lambda package contains a non-regular member: {member.filename}")
    if MANIFEST_PATH not in contents:
        raise ValueError("Lambda package is missing its canonical manifest")
    try:
        manifest = json.loads(contents[MANIFEST_PATH])
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("Lambda package manifest is not canonical JSON") from None
    if not isinstance(manifest, dict):
        raise ValueError("Lambda package manifest must be a JSON object")
    source = {name: value for name, value in contents.items() if name.startswith("aws_public_change_feed/")}
    tracked = _tracked_source(repository_root)
    working = _working_source(repository_root)
    if set(working) != set(tracked):
        raise ValueError("working source member set does not match tracked HEAD")
    dirty_working = sorted(name for name in working if working[name] != tracked[name])
    if dirty_working:
        raise ValueError(f"working source bytes do not match tracked HEAD: {dirty_working}")
    for input_path in ("requirements-lambda.txt", "scripts/build_lambda_package.py"):
        if (repository_root / input_path).read_bytes() != _git_blob(repository_root, input_path):
            raise ValueError(f"package input differs from tracked HEAD: {input_path}")
    missing = sorted({_handler_path(handler) for handler in RUNTIME_HANDLERS} - set(names))
    if missing:
        raise ValueError(f"Lambda package is missing configured handler modules: {missing}")
    for handler in RUNTIME_HANDLERS:
        _validate_handler(contents[_handler_path(handler)], handler)
    if set(source) != set(tracked):
        raise ValueError("Lambda package source member set does not match tracked HEAD")
    mismatched = sorted(name for name in source if source[name] != tracked[name])
    if mismatched:
        raise ValueError(f"Lambda package source bytes do not match tracked HEAD: {mismatched}")
    canonical_manifest = manifest_bytes(source)
    expected = json.loads(canonical_manifest)
    if set(manifest) != set(expected):
        raise ValueError("Lambda package manifest has missing or unknown fields")
    if manifest != expected or contents[MANIFEST_PATH] != canonical_manifest:
        raise ValueError("Lambda package manifest does not match the package and tracked inputs")
    return manifest


def validate_runtime_entrypoints(body: bytes) -> None:
    validate_package(body)


def publish(client: Any, *, bucket: str, prefix: str, package: Path) -> tuple[str, str, str]:
    body = package.read_bytes()
    manifest = validate_package(body)
    digest = hashlib.sha256(body).hexdigest()
    checksum = base64.b64encode(bytes.fromhex(digest)).decode()
    entrypoints_digest = runtime_entrypoints_sha256()
    manifest_body = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    metadata = {
        "sha256": digest,
        RUNTIME_ENTRYPOINTS_METADATA_KEY: entrypoints_digest,
        MANIFEST_METADATA_KEY: hashlib.sha256(manifest_body).hexdigest(),
        **publication_attestations(),
    }
    key = f"{prefix.rstrip('/')}/{digest}.zip"
    try:
        response = client.put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            IfNoneMatch="*",
            ChecksumAlgorithm="SHA256",
            ChecksumSHA256=checksum,
            Metadata=metadata,
        )
        version_id = str(response["VersionId"])
    except Exception as error:  # noqa: BLE001 - the SDK class is imported only in main
        status = getattr(error, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode")
        if status not in (409, 412):
            raise
        existing = client.head_object(Bucket=bucket, Key=key, ChecksumMode="ENABLED")
        version_id = str(existing["VersionId"])
    head = client.head_object(Bucket=bucket, Key=key, VersionId=version_id, ChecksumMode="ENABLED")
    stored = client.get_object(Bucket=bucket, Key=key, VersionId=version_id)
    stored_body = stored["Body"].read()
    stored_digest = hashlib.sha256(stored_body).hexdigest()
    stored_metadata = stored.get("Metadata", {})
    if (
        stored_metadata.get("sha256") != digest
        or stored_metadata.get(RUNTIME_ENTRYPOINTS_METADATA_KEY) != entrypoints_digest
        or stored_metadata.get(MANIFEST_METADATA_KEY) != hashlib.sha256(manifest_body).hexdigest()
        or stored_digest != digest
        or head.get("ChecksumSHA256") != checksum
    ):
        raise RuntimeError("content-addressed artifact key does not hold the matching package contract") from None
    validate_package(stored_body)
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
    print(f"worker_artifact_checksum_sha256={base64.b64encode(bytes.fromhex(digest)).decode()}")
    print(f"runtime_entrypoints_sha256={runtime_entrypoints_sha256()}")
    for key, value in sorted(publication_attestations().items()):
        print(f"{key.replace('-', '_')}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
