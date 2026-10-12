"""PDF and image translation (each page redrawn by an image model)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger

from api.surfaces.base import RunnerRequest, Surface
from api.task_contracts import (
    REASON_IMAGE_MODEL_MISSING,
    REASON_PDF_REVIEW_MODEL_MISSING,
    Runner,
    TaskInputError,
    TaskOptions,
)
from core import bilingual_writer
from core.language_registry import get_target_lang_display
from core.model_roles import (
    ROLE_IMAGE,
    ROLE_PDF_REVIEW,
    ModelRoleConfigError,
    provider_supports_capability,
    resolve_effective_model_config,
    unsupported_capability_message,
)
from core.pdf_image_translation import (
    PdfImageTranslationRunner,
    scan_pdf_path,
    scan_pdf_sources,
)
from settings import AppSettings


def _model_config_reason(exc: Exception) -> str:
    """模型设置解析失败的原因：自己写的中文报错原样给，其它异常换成固定一句。"""
    if isinstance(exc, ModelRoleConfigError) and str(exc).strip():
        return str(exc).strip()
    logger.debug(f"[PDF] 解析模型设置失败原始错误：{exc!r}")
    return "请在设置里检查这条连接。"


class PdfSurface(Surface):
    key = "pdf"
    label = "PDF translation"
    page_key = "pdf_translate"
    target_lang_setting = "pdf"
    # 读取文件 / 翻译 / 生成文件 / 写入报告
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
            raise TaskInputError("请先选择 PDF 与图片翻译的目标语言。")
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
            raise TaskInputError(
                f"PDF 与图片翻译模型的设置不可用：{_model_config_reason(exc)}",
                reason=REASON_IMAGE_MODEL_MISSING,
            ) from exc
        if not image_model.model:
            raise TaskInputError(
                "请先填写 PDF 与图片翻译模型的名称。", reason=REASON_IMAGE_MODEL_MISSING
            )
        if not provider_supports_capability(image_model.provider, "image"):
            raise TaskInputError(
                unsupported_capability_message(
                    "PDF 与图片翻译模型", image_model.provider, "image"
                ),
                reason=REASON_IMAGE_MODEL_MISSING,
            )
        if image_model.mode == "cloud" and not image_model.api_key:
            raise TaskInputError(
                "PDF 与图片翻译模型还没有填写 API 密钥。", reason=REASON_IMAGE_MODEL_MISSING
            )
        if not settings.pdf.review_enabled:
            return
        try:
            review_model = resolve_effective_model_config(settings, ROLE_PDF_REVIEW)
        except Exception as exc:  # noqa: BLE001
            raise TaskInputError(
                f"PDF 与图片审核模型的设置不可用：{_model_config_reason(exc)}",
                reason=REASON_PDF_REVIEW_MODEL_MISSING,
            ) from exc
        if not review_model.model:
            raise TaskInputError(
                "已开启逐页审核，请先填写 PDF 与图片审核模型的名称。",
                reason=REASON_PDF_REVIEW_MODEL_MISSING,
            )
        if not provider_supports_capability(review_model.provider, "vision_text"):
            raise TaskInputError(
                unsupported_capability_message(
                    "PDF 与图片审核模型", review_model.provider, "vision_text"
                ),
                reason=REASON_PDF_REVIEW_MODEL_MISSING,
            )
        if review_model.mode == "cloud" and not review_model.api_key:
            raise TaskInputError(
                "已开启逐页审核，请先给 PDF 与图片审核模型填写 API 密钥。",
                reason=REASON_PDF_REVIEW_MODEL_MISSING,
            )
        availability = str(settings.pdf_review_model_role.availability_status or "unknown")
        if availability == "unavailable" and not options.allow_known_review_failure:
            # 这一条是有出路的拦截（allow_known_review_failure 就是那条出路），界面必须
            # 拿它开一个带「仍要继续」按钮的弹窗，而不是弹一句两秒就没的提示。靠比对中文
            # 句子来认这种情况太脆，给它一个稳定的 reason。
            raise TaskInputError(
                "PDF 与图片审核模型上次测试未通过；请重新测试、关闭审核，或确认继续。",
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
        settings = request.settings
        profile_requester = None
        if getattr(settings.pdf, "smart_context", False):
            profile_requester = _document_profile_requester(
                settings,
                tuple(request.connection_chains.get("translation") or ()),
            )
        return PdfImageTranslationRunner(
            request.files,
            settings,
            source_root=request.source_root,
            key_overrides=request.key_overrides,
            api_scheduler=request.api_schedulers.get("image"),
            review_api_scheduler=request.api_schedulers.get("pdf_review"),
            resume_output_dir=request.options.resume_output_dir,
            profile_requester=profile_requester,
        )


def _document_profile_requester(settings: AppSettings, connection_ids: tuple[str, ...]):
    """「智能」：用文档翻译模型（不是生图模型）读 PDF 文字层、写文档说明。

    引擎在第一次用到时才建，并在任务线程里建，这样拿到的是任务开始时的密钥快照；
    建不起来（没配文档翻译模型）就记一行日志、不写说明，PDF 照常翻译。
    """
    from core.document_profile import request_document_profile
    from core.engine_dispatcher import build_role_engine
    from engines.base_engine import engine_supports_chat

    target_language = get_target_lang_display(
        settings.pdf.target_lang,
        settings.custom_target_langs,
        include_optional=True,
    )
    state: dict[str, Any] = {}

    def requester(texts: list[str], log) -> str:
        if "engine" not in state:
            try:
                engine = build_role_engine(settings, "translation", connection_ids=connection_ids)
            except Exception as exc:  # noqa: BLE001 - the description is optional.
                logger.debug(f"[PDF] 智能模式建文档翻译引擎失败：{exc!r}")
                engine = None
            state["engine"] = engine if engine is not None and engine_supports_chat(engine) else None
            if state["engine"] is None:
                log("WARN", "智能模式：文档翻译模型不可用，不生成文档说明，PDF 照常翻译")
        engine = state["engine"]
        if engine is None:
            return ""
        return request_document_profile(engine.chat, texts, target_language=target_language, log=log)

    return requester
