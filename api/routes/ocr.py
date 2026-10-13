"""OCR routes: the optional text-recognition (Tesseract) plugin."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException

from api.ocr_plugin import OFFICIAL_OCR_URL, connect_tesseract, probe_status, uninstall


def register(app: FastAPI) -> None:
    """Register the ocr routes on ``app``."""

    @app.get("/api/plugins/ocr")
    def ocr_plugin_status() -> dict[str, Any]:
        """Return the text-recognition plugin's local dependency status."""
        return probe_status()

    @app.post("/api/plugins/ocr/install")
    def install_ocr_plugin(payload: dict[str, Any]) -> dict[str, Any]:
        path = str(payload.get("path") or "").strip()
        if not path:
            raise HTTPException(422, "请先选择文字识别组件的安装位置。")
        try:
            return connect_tesseract(path)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/plugins/ocr/probe")
    def probe_ocr_plugin() -> dict[str, Any]:
        return probe_status()

    @app.post("/api/plugins/ocr/uninstall")
    def uninstall_ocr_plugin() -> dict[str, Any]:
        """Forget the saved path; never deletes the user's Tesseract install."""
        return uninstall()

    @app.post("/api/plugins/ocr/official-download")
    def official_ocr_download() -> dict[str, str]:
        """Return the project page without bundling or downloading Tesseract."""
        return {"authorization_url": OFFICIAL_OCR_URL, "url": OFFICIAL_OCR_URL}
