"""「PDF 处理组件」routes: one-click install, or connect a user-installed pdf2docx."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException

from api.pdf2docx_plugin import (
    OFFICIAL_PDF2DOCX_URL,
    cancel_install,
    connect_pdf2docx,
    install_progress,
    probe_status,
    start_install,
    uninstall,
)


def register(app: FastAPI) -> None:
    """Register the PDF component routes on ``app``."""

    @app.get("/api/plugins/pdf2docx")
    def pdf2docx_plugin_status() -> dict[str, Any]:
        """Return the PDF component's local status."""
        return probe_status()

    @app.post("/api/plugins/pdf2docx/install")
    def install_pdf2docx_plugin(payload: dict[str, Any]) -> dict[str, Any]:
        """Connect a pdf2docx the user installed themselves (「选择安装位置」)."""
        path = str(payload.get("path") or "").strip()
        if not path:
            raise HTTPException(422, "请先选择 PDF 处理组件的安装位置。")
        try:
            return connect_pdf2docx(path)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/plugins/pdf2docx/one-click-install")
    def one_click_install_pdf2docx_plugin() -> dict[str, Any]:
        """Start downloading and installing the component in the background."""
        try:
            return start_install()
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/plugins/pdf2docx/install-progress")
    def pdf2docx_install_progress() -> dict[str, Any]:
        return install_progress()

    @app.post("/api/plugins/pdf2docx/cancel-install")
    def cancel_pdf2docx_install() -> dict[str, Any]:
        return cancel_install()

    @app.post("/api/plugins/pdf2docx/probe")
    def probe_pdf2docx_plugin() -> dict[str, Any]:
        return probe_status()

    @app.post("/api/plugins/pdf2docx/uninstall")
    def uninstall_pdf2docx_plugin(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Remove the one-click install, or forget a user-installed pdf2docx (never deletes it)."""
        target = str((payload or {}).get("target") or "").strip()
        try:
            return uninstall(target)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/plugins/pdf2docx/official-download")
    def official_pdf2docx_download() -> dict[str, str]:
        """Return the project page (「项目主页」) without downloading anything."""
        return {"authorization_url": OFFICIAL_PDF2DOCX_URL, "url": OFFICIAL_PDF2DOCX_URL}
