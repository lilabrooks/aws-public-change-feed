"""Target-package import gate behavior."""

import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from verify_lambda_package_imports import verify  # noqa: E402


class LambdaPackageImportTests(unittest.TestCase):
    def package(self, root: Path, source: str) -> Path:
        package = root / "package.zip"
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("fixture_handler.py", source)
        return package

    @patch("verify_lambda_package_imports.validate_package")
    @patch("verify_lambda_package_imports.RUNTIME_HANDLERS", ("fixture_handler.lambda_handler",))
    def test_two_argument_sync_handler_passes(self, validate_package):
        with tempfile.TemporaryDirectory() as raw:
            verify(self.package(Path(raw), "def lambda_handler(event, context):\n    return {}\n"))
        validate_package.assert_called_once()

    @patch("verify_lambda_package_imports.validate_package")
    @patch("verify_lambda_package_imports.RUNTIME_HANDLERS", ("fixture_handler.lambda_handler",))
    def test_missing_dependency_fails_import(self, validate_package):
        with tempfile.TemporaryDirectory() as raw:
            package = self.package(
                Path(raw),
                "import dependency_absent_from_package\n\ndef lambda_handler(event, context):\n    return {}\n",
            )
            with self.assertRaises(ModuleNotFoundError):
                verify(package)
        validate_package.assert_called_once()

    @patch("verify_lambda_package_imports.validate_package")
    @patch("verify_lambda_package_imports.RUNTIME_HANDLERS", ("fixture_handler.lambda_handler",))
    def test_non_callable_handler_fails(self, validate_package):
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaisesRegex(ValueError, "not callable"):
                verify(self.package(Path(raw), "lambda_handler = None\n"))
        validate_package.assert_called_once()

    @patch("verify_lambda_package_imports.validate_package")
    @patch("verify_lambda_package_imports.RUNTIME_HANDLERS", ("fixture_handler.lambda_handler",))
    def test_async_handler_fails(self, validate_package):
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaisesRegex(ValueError, "cannot be async"):
                verify(self.package(Path(raw), "async def lambda_handler(event, context):\n    return {}\n"))
        validate_package.assert_called_once()


if __name__ == "__main__":
    unittest.main()
