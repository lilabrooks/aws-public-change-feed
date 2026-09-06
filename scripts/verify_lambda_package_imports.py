#!/usr/bin/env python3
"""Import and inspect every configured Lambda handler from an extracted package."""

from __future__ import annotations

import argparse
import importlib
import inspect
import sys
import tempfile
import zipfile
from pathlib import Path

from build_lambda_package import RUNTIME_HANDLERS
from publish_lambda_artifact import validate_package


def verify(package: Path) -> None:
    validate_package(package.read_bytes())
    configured_modules = {entrypoint.rpartition(".")[0] for entrypoint in RUNTIME_HANDLERS}
    with tempfile.TemporaryDirectory(prefix="apcf-lambda-import-") as raw:
        root = Path(raw)
        with zipfile.ZipFile(package) as archive:
            archive.extractall(root)
        sys.path.insert(0, str(root))
        try:
            for entrypoint in RUNTIME_HANDLERS:
                module_name, _, attribute = entrypoint.rpartition(".")
                module = importlib.import_module(module_name)
                handler = getattr(module, attribute)
                if not callable(handler):
                    raise ValueError(f"configured Lambda handler is not callable: {entrypoint}")
                if inspect.iscoroutinefunction(handler):
                    raise ValueError(f"configured Lambda handler cannot be async: {entrypoint}")
                try:
                    inspect.signature(handler).bind(object(), object())
                except TypeError as error:
                    raise ValueError(
                        f"configured Lambda handler cannot accept event and context: {entrypoint}"
                    ) from error
        finally:
            sys.path.remove(str(root))
            package_roots = {name.partition(".")[0] for name in configured_modules}
            for module_name in [
                name
                for name in sys.modules
                if any(name == root_name or name.startswith(f"{root_name}.") for root_name in package_roots)
            ]:
                del sys.modules[module_name]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", required=True, type=Path)
    arguments = parser.parse_args()
    verify(arguments.package.resolve())
    print(f"Lambda package import contract passed: {arguments.package}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
