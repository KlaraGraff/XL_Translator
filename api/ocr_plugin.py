"""Thin API wrapper around ``core.ocr_engine`` (Tesseract OCR discovery).

Mirrors ``api.cad_plugin``'s shape for the second optional plugin: there is no
install lifecycle (nothing is bundled or downloaded by Translator), only
discovery of a user-provided Tesseract install and remembering where it is.
"""
from __future__ import annotations

from typing import Any

from core.ocr_engine import clear_tesseract_path, ocr_status, save_tesseract_path

OFFICIAL_OCR_URL = "https://github.com/tesseract-ocr/tesseract"


def probe_status() -> dict[str, Any]:
    return ocr_status()


def connect_tesseract(path: str) -> dict[str, Any]:
    return save_tesseract_path(path)


def uninstall() -> dict[str, Any]:
    """Forget the saved path; never deletes the user's Tesseract install."""
    clear_tesseract_path()
    return ocr_status()
