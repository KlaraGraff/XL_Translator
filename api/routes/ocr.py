"""OCR routes: the optional text-recognition (Tesseract) plugin."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException

from api.ocr_plugin import (
    OFFICIAL_OCR_URL,
    cancel_install,
    connect_tesseract,
    install_progress,
    probe_status,
    start_install,
    uninstall,
)


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

    @app.post("/api/plugins/ocr/one-click-install")
    def one_click_install_ocr_plugin() -> dict[str, Any]:
        """Download and install the component in the background; poll install-progress."""
        try:
            return start_install()
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/plugins/ocr/install-progress")
    def ocr_install_progress() -> dict[str, Any]:
        return install_progress()

    @app.post("/api/plugins/ocr/cancel-install")
    def cancel_ocr_install() -> dict[str, Any]:
        return cancel_install()

    @app.post("/api/plugins/ocr/probe")
    def probe_ocr_plugin() -> dict[str, Any]:
        return probe_status()

    @app.post("/api/plugins/ocr/uninstall")
    def uninstall_ocr_plugin(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Remove the one-click install, or forget a chosen path (never deletes the user's own install)."""
        try:
            return uninstall(str((payload or {}).get("target") or ""))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/plugins/ocr/official-download")
    def official_ocr_download() -> dict[str, str]:
        """Return the project page."""
        return {"authorization_url": OFFICIAL_OCR_URL, "url": OFFICIAL_OCR_URL}
