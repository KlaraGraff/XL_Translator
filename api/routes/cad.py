"""CAD routes: the drawing-converter plugin and CAD source scanning."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from loguru import logger

from api.cad_plugin import (
    OFFICIAL_DOWNLOAD_URL,
    connect_oda,
    install_builtin,
    install_from_directory,
    probe_status,
    uninstall,
)
from core.cad_cache import default_cad_cache
from core.cad_translation import (
    CadPipelineError,
    CadPipelineOptions,
    SubprocessCadConverter,
    inspect_cad_file,
    parse_lock_patterns,
    scan_cad_paths,
)
from core.task_runner import user_facing_reason
from api.schemas import (
    CadInspectRequest,
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
            raise HTTPException(422, "请先选择图纸转换工具程序。")
        try:
            return connect_oda(path)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/cad/scan")
    def scan_cad(payload: CadScanRequest) -> dict[str, Any]:
        # DWG is listed without conversion (counts are counted by the task).
        status = probe_status()
        converter_path = str(status.get("converter") or "").strip()
        options = CadPipelineOptions(
            source_lang=str(payload.source_language or "auto"),
            target_lang=str(payload.target_language or "zh"),
            include_block_text=bool(payload.include_block_text),
            locked_blocks=parse_lock_patterns(payload.locked_blocks),
            locked_layers=parse_lock_patterns(payload.locked_layers),
        )
        skipped: list[dict[str, str]] = []
        try:
            summaries = scan_cad_paths(
                payload.paths, converter_available=bool(converter_path), options=options, skipped=skipped
            )
        except CadPipelineError as exc:
            raise HTTPException(422, str(exc) or "图纸扫描失败。") from exc
        except Exception as exc:  # noqa: BLE001 - return a user-facing preflight error
            logger.opt(exception=exc).warning(f"[CAD] 图纸扫描失败：{exc!r}")
            raise HTTPException(
                422, user_facing_reason(exc, fallback="图纸扫描失败，出现了未预期的问题。")
            ) from exc
        items = [
            {
                "path": str(item.path),
                "name": item.filename,
                "format": item.format,
                "text_count": item.text_entity_count,
                "candidate_count": item.candidate_count,
                "needs_conversion": item.needs_conversion,
                "counts_pending": item.counts_pending,
            }
            for item in summaries
        ]
        return {"items": items, "skipped": skipped, "capability": status}

    @app.post("/api/cad/inspect")
    def inspect_cad(payload: CadInspectRequest) -> dict[str, Any]:
        """Open one drawing: its counts plus the blocks and layers that hold text.

        扫描页「逐张统计」和「扫描图块名」一张一张地调用它，界面每回来一张就更新一行。
        DWG 在这里转换一次并存进缓存，开始翻译时直接复用。
        """
        status = probe_status()
        converter_path = str(status.get("converter") or "").strip()
        options = CadPipelineOptions(
            source_lang=str(payload.source_language or "auto"),
            target_lang=str(payload.target_language or "zh"),
            include_block_text=bool(payload.include_block_text),
            locked_blocks=parse_lock_patterns(payload.locked_blocks),
            locked_layers=parse_lock_patterns(payload.locked_layers),
        )
        try:
            inspection = inspect_cad_file(
                payload.path,
                converter=SubprocessCadConverter(converter_path) if converter_path else None,
                cache=default_cad_cache(),
                options=options,
            )
        except CadPipelineError as exc:
            raise HTTPException(422, str(exc) or "图纸读取失败。") from exc
        except Exception as exc:  # noqa: BLE001 - return a user-facing error for this one drawing
            logger.opt(exception=exc).warning(f"[CAD] 图纸统计失败：{exc!r}")
            raise HTTPException(
                422, user_facing_reason(exc, fallback="图纸读取失败，出现了未预期的问题。")
            ) from exc
        summary = inspection.summary
        return {
            "path": str(summary.path),
            "name": summary.filename,
            "format": summary.format,
            "text_count": summary.text_entity_count,
            "candidate_count": summary.candidate_count,
            "uncertain_count": summary.uncertain_count,
            "counts_pending": False,
            "converted": inspection.converted,
            "blocks": inspection.blocks,
            "layers": inspection.layers,
        }
