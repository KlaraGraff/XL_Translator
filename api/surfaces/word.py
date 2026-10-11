"""Word translation (.docx / .doc)."""

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
from core.word_converter import get_local_word_automation_availability
from core.word_document import scan_word_path, scan_word_sources
from core.word_task_runner import WordTaskRunner
from settings import AppSettings


class WordSurface(Surface):
    key = "word"
    label = "Word translation"
    page_key = "word_translate"
    uses_translation_memory = True
    target_lang_setting = "per_surface"
    domain_preset_in_snapshot = True
    # 读取文件 / 翻译 / 生成文件。时间几乎都花在等模型上，所以「翻译」占大头（它
    # 包含识别语言、单段重试和复查、补译）；生成文件可能要调 Word / LibreOffice
    # 处理编号，比读取多留一些。
    phase_weights = (5, 80, 15)

    def scan(self, root: Path, options: TaskOptions, *, selected_paths=None) -> list[Any]:
        return scan_word_path(root)

    def scan_sources(self, root: Path, request: Any) -> Any:
        return scan_word_sources(root)

    def validate_preflight(
        self, *, files: list[Any], settings: AppSettings, options: TaskOptions
    ) -> None:
        """Fail Word startup before allocating a task for known bad input.

        A selected legacy document is never silently routed through a
        compatibility converter.  The task either has a native Word path or a
        task-level, explicit ``allow_doc_fallback`` confirmation captured in
        its frozen snapshot.
        """
        require_target_lang(settings.target_lang, "Word")
        require_translation_model(settings)
        word_output = settings.word_output
        if word_output.use_custom_output_dir:
            output_error = bilingual_writer.get_custom_output_dir_error(
                word_output.custom_output_dir
            )
            if output_error:
                raise TaskInputError(output_error)

        doc_files = [item for item in files if file_format(item) == "doc"]
        if not doc_files or options.allow_doc_fallback:
            return
        available, reason = get_local_word_automation_availability()
        if available:
            return
        raise TaskInputError(
            "检测到 "
            f"{len(doc_files)} 个 .doc 文件，但没法用本机 Word 转换：{str(reason).rstrip('。')}。"
            "请取消任务，安装或授权 Microsoft Word 后重试，或明确确认兼容转换；"
            "兼容转换可能改变版式、域、图文和宏。"
        )

    def snapshot(
        self, *, settings: AppSettings, files: list[Any], options: TaskOptions
    ) -> dict[str, object]:
        return {
            "word_output": settings.word_output.model_dump(mode="json"),
            "word_batch": settings.word_batch.model_dump(mode="json"),
            "word_review": settings.word_review.model_dump(mode="json"),
            "word_conversion": settings.word_conversion.model_dump(mode="json"),
            "tm": {"max_len": settings.tm.max_len},
            "doc_conversion_mode": options.doc_conversion_mode,
            "selected_file_count": len(files),
            "doc_file_count": sum(1 for item in files if file_format(item) == "doc"),
        }

    def build_runner(self, request: RunnerRequest) -> Runner:
        options = request.options
        return WordTaskRunner(
            request.files,
            request.settings,
            source_root=request.source_root,
            source_lang=request.source_lang,
            key_overrides=request.key_overrides,
            untranslated_only=options.untranslated_only,
            protect_front_matter=options.protect_front_matter,
            translate_headers_footers=options.translate_headers_footers,
            allow_doc_fallback=options.allow_doc_fallback,
            api_scheduler=request.api_schedulers.get("translation"),
            resume_output_dir=options.resume_output_dir,
        )
