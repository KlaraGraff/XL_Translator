"""Read-only startup checks for a source tree or frozen executable."""

from __future__ import annotations

import importlib
import sys
from importlib.metadata import PackageNotFoundError, version


_CRITICAL_IMPORTS = (
    "app_meta",
    "PIL.Image",
    "fastapi",
    "openpyxl",
    "docx",
    "pypdfium2",
    "httpx",
    "api.launcher",
    "core.task_runner",
    "core.word_task_runner",
    "core.pdf_image_translation",
)

_CRITICAL_DISTRIBUTIONS = (
    "Pillow",
    "fastapi",
    "openpyxl",
    "python-docx",
    "pypdfium2",
)


def _emit(message: str, *, error: bool = False) -> None:
    stream = sys.stderr if error else sys.stdout
    if stream is not None:
        print(message, file=stream)


def run_smoke_test() -> int:
    """Validate bundled imports without creating a QApplication or reading settings."""
    try:
        for module_name in _CRITICAL_IMPORTS:
            importlib.import_module(module_name)

        from app_meta import APP_BUNDLE_IDENTIFIER, APP_NAME, APP_VERSION

        if not APP_NAME.strip() or not APP_VERSION.strip() or not APP_BUNDLE_IDENTIFIER.strip():
            raise RuntimeError("application metadata is incomplete")

        for distribution_name in _CRITICAL_DISTRIBUTIONS:
            try:
                installed_version = version(distribution_name)
            except PackageNotFoundError as exc:
                raise RuntimeError(
                    f"required distribution metadata is missing: {distribution_name}"
                ) from exc
            if not installed_version.strip():
                raise RuntimeError(
                    f"required distribution has no version: {distribution_name}"
                )
    except Exception as exc:
        _emit(f"[ERROR] Frozen application smoke test failed: {exc}", error=True)
        return 1

    _emit(f"[INFO] Frozen application smoke test passed: {APP_NAME} {APP_VERSION}")
    return 0
