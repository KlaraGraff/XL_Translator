"""扩展页「PDF 组件」大类的接口：段落重排组件（BabelDOC）单项，以及「全部安装」。

PDF 处理组件、文字识别组件两项沿用原来的接口（api/routes/pdf2docx.py、api/routes/ocr.py）。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException

from api import babeldoc_plugin, pdf_components_plugin


def register(app: FastAPI) -> None:
    """Register the PDF component group routes on ``app``."""

    @app.get("/api/plugins/babeldoc")
    def babeldoc_plugin_status() -> dict[str, Any]:
        return babeldoc_plugin.probe_status()

    @app.post("/api/plugins/babeldoc/one-click-install")
    def one_click_install_babeldoc() -> dict[str, Any]:
        """Download and install BabelDOC in the background; poll install-progress."""
        try:
            return babeldoc_plugin.start_install()
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/plugins/babeldoc/install-progress")
    def babeldoc_install_progress() -> dict[str, Any]:
        return babeldoc_plugin.install_progress()

    @app.post("/api/plugins/babeldoc/cancel-install")
    def cancel_babeldoc_install() -> dict[str, Any]:
        return babeldoc_plugin.cancel_install()

    @app.post("/api/plugins/babeldoc/uninstall")
    def uninstall_babeldoc() -> dict[str, Any]:
        try:
            return babeldoc_plugin.uninstall()
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/plugins/babeldoc/official-download")
    def official_babeldoc_download() -> dict[str, str]:
        """Return the project page."""
        url = babeldoc_plugin.OFFICIAL_BABELDOC_URL
        return {"authorization_url": url, "url": url}

    @app.get("/api/plugins/pdf-components")
    def pdf_components_status() -> dict[str, Any]:
        return pdf_components_plugin.status()

    @app.post("/api/plugins/pdf-components/install-all")
    def install_all_pdf_components() -> dict[str, Any]:
        """Install the missing items one after another in the background; poll install-progress."""
        try:
            return pdf_components_plugin.start_install()
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/plugins/pdf-components/install-progress")
    def pdf_components_install_progress() -> dict[str, Any]:
        return pdf_components_plugin.install_progress()

    @app.post("/api/plugins/pdf-components/cancel-install")
    def cancel_pdf_components_install() -> dict[str, Any]:
        return pdf_components_plugin.cancel_install()
