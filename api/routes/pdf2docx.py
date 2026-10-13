"""PDF-to-Word routes: the optional pdf2docx plugin."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException

from api.pdf2docx_plugin import OFFICIAL_PDF2DOCX_URL, connect_pdf2docx, probe_status, uninstall


def register(app: FastAPI) -> None:
    """Register the pdf2docx routes on ``app``."""

    @app.get("/api/plugins/pdf2docx")
    def pdf2docx_plugin_status() -> dict[str, Any]:
        """Return the PDF-to-Word plugin's local dependency status."""
        return probe_status()

    @app.post("/api/plugins/pdf2docx/install")
    def install_pdf2docx_plugin(payload: dict[str, Any]) -> dict[str, Any]:
        path = str(payload.get("path") or "").strip()
        if not path:
            raise HTTPException(422, "请先选择 PDF 转 Word 组件的安装位置。")
        try:
            return connect_pdf2docx(path)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/plugins/pdf2docx/probe")
    def probe_pdf2docx_plugin() -> dict[str, Any]:
        return probe_status()

    @app.post("/api/plugins/pdf2docx/uninstall")
    def uninstall_pdf2docx_plugin() -> dict[str, Any]:
        """Forget the saved path; never deletes the user's pdf2docx install."""
        return uninstall()

    @app.post("/api/plugins/pdf2docx/official-download")
    def official_pdf2docx_download() -> dict[str, str]:
        """Return the project page without bundling or downloading pdf2docx."""
        return {"authorization_url": OFFICIAL_PDF2DOCX_URL, "url": OFFICIAL_PDF2DOCX_URL}
