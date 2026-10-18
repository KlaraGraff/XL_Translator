"""Excel translation (.xlsx / .xlsm / .xls)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from api.surfaces.base import (
    PHASE_READ,
    PHASE_TRANSLATE,
    PHASE_WRITE,
    RunnerRequest,
    Surface,
    file_format,
    require_target_lang,
    require_translation_model,
    suffixes,
)
from api.task_contracts import REASON_NEEDS_CONFIRMATION, Runner, TaskInputError, TaskOptions
from core import bilingual_writer
from core.file_scanner import SUPPORTED_EXCEL_SUFFIXES, scan_excel_sources, scan_path
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
    display_label = {"zh": "Excel 表格", "en": "Excel files", "fr": "Fichiers Excel"}
    file_noun = {"zh": "表格文件", "en": "Excel files", "fr": "fichiers Excel"}
    icon = "excel"
    file_extensions = suffixes(SUPPORTED_EXCEL_SUFFIXES)
    output_bilingual = True
    output_translation_only = True
    # 读取文件 / 翻译 / 生成文件 / 调整行高（没开时只有前三段）。时间几乎都花在等
    # 模型上，所以「翻译」占大头（它包含识别语言、补译、翻译工作表名称）；读取和
    # 生成是本机操作，很快；调整行高要启动 Excel，留得稍多一点。
    phase_weights = (5, 80, 5, 10)
    phase_names = (
        PHASE_READ,
        PHASE_TRANSLATE,
        PHASE_WRITE,
        {"zh": "调整行高", "en": "Adjusting row heights", "fr": "Ajustement des hauteurs de ligne"},
    )

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
            f"检测到 {len(xls_files)} 个 .xls 文件，但无法使用本机 Excel 转换："
            f"{str(reason).rstrip('。')}。请取消任务，安装或授权 Microsoft Excel 后重试，"
            f"或明确确认兼容转换{consequence}",
            reason=REASON_NEEDS_CONFIRMATION,
            accept={"allow_xls_fallback": True},
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
            batch_output_dir=request.options.batch_output_dir,
        )
