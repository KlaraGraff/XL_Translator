"""Thin API wrapper around ``core.pdf2docx_plugin`` (PDF-to-Word converter discovery).

Same shape as ``api.ocr_plugin``: Translator does not bundle or download
pdf2docx, it only discovers a user-provided install and remembers where it is.
"""
from __future__ import annotations

from typing import Any

from core.pdf2docx_plugin import clear_pdf2docx_path, pdf2docx_status, save_pdf2docx_path

OFFICIAL_PDF2DOCX_URL = "https://github.com/ArtifexSoftware/pdf2docx"


def probe_status() -> dict[str, Any]:
    return pdf2docx_status()


def connect_pdf2docx(path: str) -> dict[str, Any]:
    return save_pdf2docx_path(path)


def uninstall() -> dict[str, Any]:
    """Forget the saved path; never deletes the user's pdf2docx install."""
    clear_pdf2docx_path()
    return pdf2docx_status()
