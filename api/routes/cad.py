"""CAD routes: the drawing-converter plugin and CAD source scanning."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException

from api.cad_plugin import (
    OFFICIAL_DOWNLOAD_URL,
    connect_oda,
    install_builtin,
    install_from_directory,
    probe_status,
    uninstall,
)
from core.cad_translation import CadPipelineOptions, SubprocessCadConverter, scan_cad_paths
from api.schemas import (
    CadScanRequest,
)


def register(app: FastAPI) -> None:
    """Register the cad routes on ``app``."""

    @app.get("/api/plugins/cad")
    def cad_plugin_status() -> dict[str, Any]:
        """Return the drawing-converter plugin and its local dependency status."""
        return probe_status()

    @app.post("/api/plugins/cad/install")
    def install_cad_plugin(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Keep the legacy plugin-install route for older clients."""
        payload = payload or {}
        source = str(payload.get("source_dir") or "").strip()
        try:
            return install_from_directory(source) if source else install_builtin()
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/plugins/cad/probe")
    def probe_cad_plugin() -> dict[str, Any]:
        return probe_status()

    @app.post("/api/plugins/cad/official-download")
    def official_cad_download() -> dict[str, str]:
        """Return the vendor page without bundling or downloading ODA."""
        return {"authorization_url": OFFICIAL_DOWNLOAD_URL, "url": OFFICIAL_DOWNLOAD_URL}

    @app.post("/api/plugins/cad/uninstall")
    def uninstall_cad_plugin() -> dict[str, Any]:
        """Keep the legacy route without removing built-in support or ODA config."""
        return uninstall()

    @app.post("/api/plugins/cad/oda")
    def connect_cad_oda(payload: dict[str, Any]) -> dict[str, Any]:
        path = str(payload.get("path") or "").strip()
        if not path:
            raise HTTPException(422, "path is required.")
        try:
            return connect_oda(path)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/cad/scan")
    def scan_cad(payload: CadScanRequest) -> dict[str, Any]:
        status = probe_status()
        converter_path = str(status.get("converter") or "").strip()
        converter = SubprocessCadConverter(converter_path) if converter_path else None
        options = CadPipelineOptions(
            source_lang=str(payload.source_language or "auto"),
            target_lang=str(payload.target_language or "zh"),
            include_block_text=bool(payload.include_block_text),
        )
        skipped: list[dict[str, str]] = []
        try:
            summaries = scan_cad_paths(payload.paths, converter=converter, options=options, skipped=skipped)
        except Exception as exc:  # noqa: BLE001 - return a user-facing preflight error
            raise HTTPException(422, str(exc) or "CAD 文件扫描失败。") from exc
        items = [
            {
                "path": str(item.path),
                "name": item.filename,
                "format": item.format,
                "text_count": item.text_entity_count,
                "candidate_count": item.candidate_count,
                "needs_conversion": item.needs_conversion,
            }
            for item in summaries
        ]
        return {"items": items, "skipped": skipped, "capability": status}
