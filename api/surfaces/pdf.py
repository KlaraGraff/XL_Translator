"""PDF and image translation (each page redrawn by an image model)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from api.surfaces.base import RunnerRequest, Surface
from api.task_contracts import Runner, TaskInputError, TaskOptions
from core import bilingual_writer
from core.model_roles import (
    ROLE_IMAGE,
    ROLE_PDF_REVIEW,
    provider_supports_capability,
    resolve_effective_model_config,
)
from core.pdf_image_translation import (
    PdfImageTranslationRunner,
    scan_pdf_path,
    scan_pdf_sources,
)
from settings import AppSettings


class PdfSurface(Surface):
    key = "pdf"
    label = "PDF translation"
    page_key = "pdf_translate"
    target_lang_setting = "pdf"
    # 预处理 PDF / 翻译页面 / 生成最终产物 / 写入报告
    phase_weights = (5, 85, 8, 2)
    can_pause = True
    pause_mode = "immediate"
    can_resume = True
    can_end_paused = True
    keeps_runner_after_finish = True

    def scan(self, root: Path, options: TaskOptions, *, selected_paths=None) -> list[Any]:
        return scan_pdf_path(root, include_images=options.include_images)

    def scan_sources(self, root: Path, request: Any) -> Any:
        return scan_pdf_sources(root, include_images=request.include_images)

    def validate_preflight(
        self, *, files: list[Any], settings: AppSettings, options: TaskOptions
    ) -> None:
        """Validate PDF/image-specific settings before allocating a task lease."""
        if not files:
            raise TaskInputError("请至少选择一个 PDF 或图片文件。")
        if not str(settings.pdf.target_lang or "").strip():
            raise TaskInputError("请先选择 PDF/图片目标语言。")
        pdf_output = settings.pdf_output
        if pdf_output.use_custom_output_dir:
            output_error = bilingual_writer.get_custom_output_dir_error(
                pdf_output.custom_output_dir
            )
            if output_error:
                raise TaskInputError(output_error)
        try:
            image_model = resolve_effective_model_config(settings, ROLE_IMAGE)
        except Exception as exc:  # noqa: BLE001 - give UI the resolved contract error.
            raise TaskInputError(f"PDF 翻译模型配置不可用：{exc}") from exc
        if not image_model.model:
            raise TaskInputError("请先填写 PDF 翻译模型名称。")
        if not provider_supports_capability(image_model.provider, "image"):
            raise TaskInputError(
                f"当前 PDF 翻译模型服务商不支持图像生成能力：{image_model.provider}"
            )
        if image_model.mode == "cloud" and not image_model.api_key:
            raise TaskInputError("PDF 翻译模型尚未配置 API Key。")
        if not settings.pdf.review_enabled:
            return
        try:
            review_model = resolve_effective_model_config(settings, ROLE_PDF_REVIEW)
        except Exception as exc:  # noqa: BLE001
            raise TaskInputError(f"PDF翻译（图像翻译）复核模型配置不可用：{exc}") from exc
        if not review_model.model:
            raise TaskInputError("已启用逐页审核，请先填写 PDF翻译（图像翻译）复核模型名称。")
        if not provider_supports_capability(review_model.provider, "vision_text"):
            raise TaskInputError(
                f"当前 PDF翻译（图像翻译）复核模型服务商不支持视觉理解能力：{review_model.provider}"
            )
        if review_model.mode == "cloud" and not review_model.api_key:
            raise TaskInputError("已启用逐页审核，请先配置审核模型 API Key。")
        availability = str(settings.pdf_review_model_role.availability_status or "unknown")
        if availability == "unavailable" and not options.allow_known_review_failure:
            # 这一条是有出路的拦截（allow_known_review_failure 就是那条出路），界面必须
            # 拿它开一个带「仍要继续」按钮的弹窗，而不是弹一句两秒就没的提示。靠比对中文
            # 句子来认这种情况太脆，给它一个稳定的 reason。
            raise TaskInputError(
                "PDF翻译（图像翻译）复核模型当前配置已测试失败；请重新测试、关闭审核，或明确确认继续。",
                reason="pdf_review_model_unavailable",
            )

    def snapshot(
        self, *, settings: AppSettings, files: list[Any], options: TaskOptions
    ) -> dict[str, object]:
        return {
            "pdf": settings.pdf.model_dump(mode="json"),
            "pdf_output": settings.pdf_output.model_dump(mode="json"),
            "selected_file_count": len(files),
            "pdf_file_count": sum(1 for item in files if getattr(item, "source_type", "pdf") == "pdf"),
            "image_file_count": sum(1 for item in files if getattr(item, "source_type", "pdf") == "image"),
            "tm": {"enabled": False},
        }

    def build_runner(self, request: RunnerRequest) -> Runner:
        return PdfImageTranslationRunner(
            request.files,
            request.settings,
            source_root=request.source_root,
            key_overrides=request.key_overrides,
            api_scheduler=request.api_schedulers.get("image"),
            review_api_scheduler=request.api_schedulers.get("pdf_review"),
            resume_output_dir=request.options.resume_output_dir,
        )
