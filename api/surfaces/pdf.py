"""PDF and image translation (each page redrawn by an image model)."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from loguru import logger

from api.surfaces.base import PHASE_READ, PHASE_TRANSLATE, PHASE_WRITE, RunnerRequest, Surface, suffixes
from api.task_contracts import (
    REASON_IMAGE_MODEL_MISSING,
    REASON_PDF_REVIEW_MODEL_MISSING,
    Runner,
    TaskInputError,
    TaskOptions,
)
from core.local_endpoint import settings_runs_locally
from core.language_registry import get_target_lang_display
from core.model_roles import (
    ROLE_IMAGE,
    ROLE_PDF_REVIEW,
    ROLE_TRANSLATION,
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
from core.pdf_models import SUPPORTED_IMAGE_SUFFIXES, SUPPORTED_PDF_SUFFIXES
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
    display_label = {"zh": "PDF 与图片", "en": "PDF & images", "fr": "PDF et images"}
    file_noun = {"zh": "PDF / 图片文件", "en": "PDF / image files", "fr": "fichiers PDF / images"}
    icon = "pdf"
    # 图片要在页面上勾选「包含图片」才扫描。
    file_extensions = suffixes((*SUPPORTED_PDF_SUFFIXES, *SUPPORTED_IMAGE_SUFFIXES))
    output_translation_only = True
    # 读取文件 / 翻译 / 生成文件 / 写入报告
    phase_weights = (5, 85, 8, 2)
    phase_names = (
        PHASE_READ,
        PHASE_TRANSLATE,
        PHASE_WRITE,
        {"zh": "写入报告", "en": "Writing report", "fr": "Écriture du rapport"},
    )
    can_pause = True
    pause_mode = "immediate"
    can_resume = True
    can_end_paused = True
    keeps_runner_after_finish = True

    def saved_request_defaults(self, settings: AppSettings, *, target_lang: str) -> dict[str, Any]:
        # 「也翻译图片文件」存在 pdf.include_images。
        return {"include_images": bool(settings.pdf.include_images)}

    def saved_scan_defaults(
        self, settings: AppSettings, *, source_lang: str, target_lang: str
    ) -> dict[str, Any]:
        return {"include_images": bool(settings.pdf.include_images)}

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
        if _inplace_enabled(settings):
            try:
                text_model = resolve_effective_model_config(settings, ROLE_TRANSLATION)
            except Exception as exc:  # noqa: BLE001
                raise TaskInputError(
                    f"所选的 PDF 翻译方式要用文本翻译模型，它的设置不可用：{_model_config_reason(exc)}"
                ) from exc
            if not text_model.model:
                raise TaskInputError("所选的 PDF 翻译方式要用文本翻译模型，请先在模型设置里填好它。")
        try:
            _validate_image_model(settings)
        except TaskInputError:
            # 原位替换、转 Word、自动组合不一定用到图像模型：没设置也能开始，
            # 需要整页图像翻译的页保留原样，完成后列出来。
            if _inplace_enabled(settings):
                return
            raise
        if not settings.pdf.review_enabled:
            return
        try:
            review_model = resolve_effective_model_config(settings, ROLE_PDF_REVIEW)
        except Exception as exc:  # noqa: BLE001
            raise TaskInputError(
                f"图像翻译审核模型的设置不可用：{_model_config_reason(exc)}",
                reason=REASON_PDF_REVIEW_MODEL_MISSING,
            ) from exc
        if not review_model.model:
            raise TaskInputError(
                "已开启逐页审核，请先填写图像翻译审核模型的名称。",
                reason=REASON_PDF_REVIEW_MODEL_MISSING,
            )
        if not provider_supports_capability(review_model.provider, "vision_text"):
            raise TaskInputError(
                unsupported_capability_message(
                    "图像翻译审核模型", review_model.provider, "vision_text"
                ),
                reason=REASON_PDF_REVIEW_MODEL_MISSING,
            )
        if review_model.mode == "cloud" and not review_model.api_key:
            raise TaskInputError(
                "已开启逐页审核，请先给图像翻译审核模型填写 API 密钥。",
                reason=REASON_PDF_REVIEW_MODEL_MISSING,
            )
        availability = str(settings.pdf_review_model_role.availability_status or "unknown")
        if availability == "unavailable" and not options.allow_known_review_failure:
            # 这一条是有出路的拦截（allow_known_review_failure 就是那条出路），界面必须
            # 拿它开一个带「仍要继续」按钮的弹窗，而不是弹一句两秒就没的提示。靠比对中文
            # 句子来认这种情况太脆，给它一个稳定的 reason。
            raise TaskInputError(
                "图像翻译审核模型上次测试未通过；请重新测试、关闭审核，或确认继续。",
                # 界面认这个 reason 开弹窗，所以不改成通用的 needs_confirmation；
                # 命令行和 MCP 看 accept。
                reason="pdf_review_model_unavailable",
                accept={"allow_known_review_failure": True},
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
        profile_concurrency = 1
        if getattr(settings.pdf, "smart_context", False):
            profile_requester = _document_profile_requester(
                settings,
                tuple(request.connection_chains.get("translation") or ()),
            )
            profile_concurrency = _document_profile_concurrency(settings)
        inplace_hooks = None
        if _inplace_enabled(settings):
            inplace_hooks = _inplace_hooks(
                settings, tuple(request.connection_chains.get("translation") or ())
            )
        return PdfImageTranslationRunner(
            request.files,
            settings,
            source_root=request.source_root,
            key_overrides=request.key_overrides,
            api_scheduler=request.api_schedulers.get("image"),
            review_api_scheduler=request.api_schedulers.get("pdf_review"),
            resume_output_dir=request.options.resume_output_dir,
            batch_output_dir=request.options.batch_output_dir,
            profile_requester=profile_requester,
            profile_concurrency=profile_concurrency,
            inplace_hooks=inplace_hooks,
        )


def _inplace_enabled(settings: AppSettings) -> bool:
    """翻译方式不是整页图像翻译（原位替换、转 Word、自动组合都要用文本翻译模型）。"""
    return getattr(settings.pdf, "translation_route", "image") != "image"


def _validate_image_model(settings: AppSettings) -> None:
    try:
        image_model = resolve_effective_model_config(settings, ROLE_IMAGE)
    except Exception as exc:  # noqa: BLE001 - give UI the resolved contract error.
        raise TaskInputError(
            f"图像翻译模型的设置不可用：{_model_config_reason(exc)}",
            reason=REASON_IMAGE_MODEL_MISSING,
        ) from exc
    if not image_model.model:
        raise TaskInputError(
            "请先填写图像翻译模型的名称。", reason=REASON_IMAGE_MODEL_MISSING
        )
    if not provider_supports_capability(image_model.provider, "image"):
        raise TaskInputError(
            unsupported_capability_message(
                "图像翻译模型", image_model.provider, "image"
            ),
            reason=REASON_IMAGE_MODEL_MISSING,
        )
    if image_model.mode == "cloud" and not image_model.api_key:
        raise TaskInputError(
            "图像翻译模型还没有填写 API 密钥。", reason=REASON_IMAGE_MODEL_MISSING
        )


def _inplace_hooks(settings: AppSettings, connection_ids: tuple[str, ...]):
    """「原位替换文字（Beta）」：文本翻译模型 + 可选的文字识别组件。

    引擎在第一次用到时才建（在任务线程里，拿任务开始时的密钥快照）。引擎、分批请求
    和记忆库的接线在 core/text_translation_service.py，与 CAD 共用。
    """
    from core.ocr_engine import resolve_tesseract, tesseract_languages
    from core.pdf_vector import InplaceHooks
    from core.ppocr_component import make_reader
    from core.pdf_vector.classify import target_language_name
    from core.text_translation_service import TextTranslationService
    from engines.base_engine import engine_supports_chat

    smart_mode = bool(getattr(settings.pdf, "smart_context", False))
    target_lang = str(settings.pdf.target_lang or settings.target_lang or "zh")
    service = TextTranslationService(
        settings,
        surface_key="pdf",
        # 速率与重试按「翻译参数 → PDF → 文字翻译」（core/surface_rate.py）。
        rate_surface="pdf_text",
        # 原位替换译成 PDF 页选的目标语言（提示词见 core/pdf_vector/classify.py）。
        target_lang=target_lang,
        source_lang="auto",
        connection_ids=connection_ids,
        marked_in="pdf_pages",
        smart_mode=smart_mode,
    )

    def translate(texts: list[str], prompt: str, should_stop=None, term_session=None,
                  request_callback=None, error_callback=None, resend_unchanged=None, stats=None,
                  source_lang=None) -> dict:
        # 页面自己拼提示词。接口放弃、停止时没发出去的条目不在结果里：页面再试一次，仍
        # 没有就记为「未获得译文」、列进这一页的复核清单，与 CAD 一致；原样返回的才可能是
        # 模型有意保留原文（core/pdf_vector/classify.py _translate_with_retry）。
        # request_callback 收逐批的发出与返回：PDF 任务据此写逐批日志、按返回条数推进进度。
        # resend_unchanged：哪一批先回来就先补问那一批原样退回的词，不等其它批次。
        # stats：这一页的批次重试和降速次数，PDF 报告据此计数。
        # source_lang：这一页认出的原文语种，引擎的任务说明里写「从中文译成法文」而不是「从 auto」。
        return service.translate(texts, prompt=prompt, should_stop=should_stop, term_session=term_session,
                                 request_callback=request_callback, error_callback=error_callback,
                                 resend_unchanged=resend_unchanged, stats=stats, source_lang=source_lang)

    profile = None
    if smart_mode:
        def profile(texts: list[str]) -> str:
            if not engine_supports_chat(service.engine()):
                return ""
            return service.document_profile(texts, target_language=target_language_name(target_lang))

    # 记忆库与 Excel / Word 同一套规则（core.term_session）；语言对按每页识别的原文语种定。
    memory = service.term_memory(target_lang=target_lang, lang_pairs=())

    exe = resolve_tesseract()
    return InplaceHooks(
        translate=translate,
        profile=profile,
        tesseract=str(exe) if exe else None,
        tesseract_languages=tesseract_languages(exe) if exe else None,
        # 装了中文识别模型时中文、日文、西文的线条字都用它认（core.ppocr_component）。
        ocr_reader=make_reader(),
        source_lang="auto",
        ocr_wanted=True,
        memory=memory,
    )


def _document_profile_concurrency(settings: AppSettings) -> int:
    """几份 PDF 的文档说明同时请求几个：沿用 PDF 文字翻译的同时请求数；本地模型一次一个。"""
    if settings_runs_locally(settings):
        return 1
    try:
        from core.surface_rate import resolve_surface_rate

        return max(1, int(resolve_surface_rate(settings, "pdf_text").concurrency))
    except Exception as exc:  # noqa: BLE001 - fall back to one at a time.
        logger.debug(f"[PDF] 读取文本翻译模型并发设置失败：{exc!r}")
        return 1


def _document_profile_requester(settings: AppSettings, connection_ids: tuple[str, ...]):
    """「自动识别场景」：用文本翻译模型（不是生图模型）读 PDF 文字层、写文档说明。

    引擎在第一次用到时才建，并在任务线程里建，这样拿到的是任务开始时的密钥快照；
    建不起来（没配文本翻译模型）就记一行日志、不写说明，PDF 照常翻译。
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
    # 几份 PDF 同时请求说明时，引擎只建一次。
    engine_lock = threading.Lock()

    def requester(texts: list[str], log) -> str:
        with engine_lock:
            if "engine" not in state:
                try:
                    engine = build_role_engine(settings, "translation", connection_ids=connection_ids)
                except Exception as exc:  # noqa: BLE001 - the description is optional.
                    logger.debug(f"[PDF] 自动识别场景：建文档翻译引擎失败：{exc!r}")
                    engine = None
                state["engine"] = engine if engine is not None and engine_supports_chat(engine) else None
                if state["engine"] is None:
                    log("WARN", "自动识别场景：文本翻译模型不可用，不生成文档说明，PDF 照常翻译")
            engine = state["engine"]
        if engine is None:
            return ""
        return request_document_profile(engine.chat, texts, target_language=target_language, log=log)

    return requester
