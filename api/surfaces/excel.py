"""Excel translation (.xlsx / .xlsm / .xls)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from api.surfaces.base import (
    RunnerRequest,
    Surface,
    file_format,
    require_target_lang,
    require_translation_model,
)
from api.task_contracts import Runner, TaskInputError, TaskOptions
from core import bilingual_writer
from core.file_scanner import scan_excel_sources, scan_path
from core.task_runner import TaskRunner
from core.xls_converter import (
    describe_xls_compatibility_consequence,
    get_local_excel_availability,
    libreoffice_xls_conversion_available,
)
from settings import AppSettings


class ExcelSurface(Surface):
    key = "excel"
    label = "Excel translation"
    page_key = "excel_translate"
    uses_translation_memory = True
    target_lang_setting = "per_surface"
    domain_preset_in_snapshot = True
    # 全局扫描 / 云端翻译 / 生成文件 / Excel 精调（没开精调时只有前三段）
    phase_weights = (10, 60, 15, 15)

    def scan(self, root: Path, options: TaskOptions, *, selected_paths=None) -> list[Any]:
        return scan_path(root)

    def scan_sources(self, root: Path, request: Any) -> Any:
        return scan_excel_sources(root)

    def validate_preflight(
        self, *, files: list[Any], settings: AppSettings, options: TaskOptions
    ) -> None:
        """Fail before task creation for deterministic Excel input/settings issues."""
        require_target_lang(settings.target_lang, "Excel")
        require_translation_model(settings)
        excel_output = settings.excel_output
        if excel_output.use_custom_output_dir:
            output_error = bilingual_writer.get_custom_output_dir_error(
                excel_output.custom_output_dir
            )
            if output_error:
                raise TaskInputError(output_error)

        xls_files = [item for item in files if file_format(item) == "xls"]
        if not xls_files or options.allow_xls_fallback:
            return
        available, reason = get_local_excel_availability()
        if available:
            return
        consequence = describe_xls_compatibility_consequence(
            has_libreoffice=libreoffice_xls_conversion_available()
        )
        raise TaskInputError(
            "检测到 "
            f"{len(xls_files)} 个 .xls 文件，但没法用本机 Excel 转换：{str(reason).rstrip('。')}。"
            f"请取消任务，安装或授权 Microsoft Excel 后重试，或明确确认兼容转换{consequence}"
        )

    def snapshot(
        self, *, settings: AppSettings, files: list[Any], options: TaskOptions
    ) -> dict[str, object]:
        return {
            "excel_output": settings.excel_output.model_dump(mode="json"),
            "excel_review": settings.excel_review.model_dump(mode="json"),
            "tm": {"max_len": settings.tm.max_len},
            "xls_conversion_mode": options.xls_conversion_mode,
            "selected_file_count": len(files),
            "xls_file_count": sum(1 for item in files if file_format(item) == "xls"),
        }

    def build_runner(self, request: RunnerRequest) -> Runner:
        return TaskRunner(
            request.files,
            request.settings,
            source_root=request.source_root,
            allow_xls_fallback=request.options.allow_xls_fallback,
            source_lang=request.source_lang,
            key_overrides=request.key_overrides,
            api_scheduler=request.api_schedulers.get("translation"),
            untranslated_only=request.options.untranslated_only,
            connection_chain=tuple(request.connection_chains.get("translation") or ()),
            resume_output_dir=request.options.resume_output_dir,
        )
