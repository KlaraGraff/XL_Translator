"""Headless helpers for running the PDF image-layout translation pipeline."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from core.language_registry import (
    get_target_lang_display,
    is_supported_target_lang,
    resolve_language_code,
    get_supported_languages,
)
from core.model_api_identity import task_api_context_for_page
from core.pdf_image_translation import (
    PDF_MANIFEST_FILENAME,
    PDF_REPORT_FILENAME,
    PdfImageTranslationRunner,
    scan_pdf_path,
)
from core.task_runner import (
    DoneMsg,
    ErrorMsg,
    LogMsg,
    PdfPageRecoveryStatusMsg,
    PdfReviewStatusMsg,
    ProgressMsg,
    StatusMsg,
    StoppedMsg,
)
from settings import AppSettings, load_settings

HeadlessPdfEventHandler = Callable[[dict[str, Any]], None]


@dataclass
class HeadlessPdfTranslationResult:
    source_path: str
    output_dir: str
    file_results: list[dict[str, Any]]
    successful_outputs: list[str]
    elapsed_sec: float
    api_call_count: int
    target_lang: str
    target_lang_display: str
    file_count: int
    issue_count: int
    report_path: str
    manifest_path: str
    log_entries: list[dict[str, str]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_pdf_runtime_settings(
    *,
    base_settings: AppSettings | None = None,
    target_lang: str | None = None,
    output_dir: str | Path | None = None,
    page_concurrency: int | None = None,
    retry_attempts: int | None = None,
    review_enabled: bool | None = None,
    compressed_pdf: bool | None = None,
) -> AppSettings:
    """Build an in-memory settings object for headless PDF runs."""
    settings = (base_settings or load_settings()).model_copy(deep=True)

    settings.pdf.target_lang = _resolve_pdf_target_lang(target_lang, settings)
    settings.target_lang = settings.pdf.target_lang

    if output_dir is None:
        settings.pdf_output.use_custom_output_dir = False
        settings.pdf_output.custom_output_dir = ""
    else:
        resolved_output = Path(output_dir).expanduser()
        settings.pdf_output.use_custom_output_dir = True
        settings.pdf_output.custom_output_dir = str(resolved_output)

    if page_concurrency is not None:
        settings.pdf.page_generation_concurrency = int(page_concurrency)
    if retry_attempts is not None:
        settings.pdf.page_retry_attempts = int(retry_attempts)
    if review_enabled is not None:
        settings.pdf.review_enabled = bool(review_enabled)
    if compressed_pdf is not None:
        settings.pdf.generate_compressed_pdf = bool(compressed_pdf)

    return settings


def run_pdf_translation_path(
    source_path: str | Path,
    *,
    settings: AppSettings | None = None,
    include_images: bool = False,
    poll_interval: float = 0.1,
    event_handler: HeadlessPdfEventHandler | None = None,
) -> HeadlessPdfTranslationResult:
    """Run one headless PDF image-layout translation task and wait for completion."""
    input_path = Path(source_path).expanduser()
    if not input_path.is_absolute():
        input_path = input_path.resolve()

    if not input_path.exists():
        raise FileNotFoundError(f"路径不存在：{input_path}")

    runtime_settings = (settings or load_settings()).model_copy(deep=True)
    runtime_settings.pdf.target_lang = _resolve_pdf_target_lang(
        runtime_settings.pdf.target_lang,
        runtime_settings,
    )
    runtime_settings.target_lang = runtime_settings.pdf.target_lang

    file_items = scan_pdf_path(input_path, include_images=include_images)
    if not file_items:
        raise ValueError(f"未在路径中发现可处理的 PDF 文件：{input_path}")

    source_root = input_path if input_path.is_dir() else input_path.parent
    api_context = task_api_context_for_page(runtime_settings, "pdf_translate")
    runner = PdfImageTranslationRunner(
        file_items,
        runtime_settings,
        source_root=source_root,
        key_overrides=api_context.key_overrides,
    )
    runner.start()

    log_entries: list[dict[str, str]] = []

    while runner.needs_poll():
        message = runner.get_message(timeout=poll_interval)
        if message is None:
            continue

        if isinstance(message, LogMsg):
            log_entry = {
                "type": "log",
                "level": message.level,
                "message": message.message,
                "ts": message.ts,
            }
            log_entries.append(log_entry)
            _emit_event(event_handler, log_entry)
            continue

        if isinstance(message, StatusMsg):
            _emit_event(event_handler, {"type": "status", "message": message.phase_desc})
            continue

        if isinstance(message, ProgressMsg):
            _emit_event(
                event_handler,
                {
                    "type": "progress",
                    "phase_index": message.phase_index,
                    "phase_total": message.phase_total,
                    "phase_name": message.phase_name,
                    "step_done": message.step_done,
                    "step_total": message.step_total,
                },
            )
            continue

        if isinstance(message, PdfPageRecoveryStatusMsg):
            _emit_event(event_handler, {"type": "pdf_page_recovery", **asdict(message)})
            continue

        if isinstance(message, PdfReviewStatusMsg):
            _emit_event(event_handler, {"type": "pdf_review", **asdict(message)})
            continue

        if isinstance(message, DoneMsg):
            successful_outputs = [
                str(item["output"])
                for item in message.file_results
                if item.get("success") and item.get("output")
            ]
            output_dir = str(message.output_dir)
            manifest_path = str(Path(output_dir) / PDF_MANIFEST_FILENAME)
            report_path = str(message.report_path or Path(output_dir) / PDF_REPORT_FILENAME)
            result = HeadlessPdfTranslationResult(
                source_path=str(input_path),
                output_dir=output_dir,
                file_results=message.file_results,
                successful_outputs=successful_outputs,
                elapsed_sec=message.elapsed_sec,
                api_call_count=message.api_call_count,
                target_lang=runtime_settings.pdf.target_lang,
                target_lang_display=get_target_lang_display(
                    runtime_settings.pdf.target_lang,
                    runtime_settings.custom_target_langs,
                    include_optional=True,
                ),
                file_count=len(file_items),
                issue_count=len(message.issues or []),
                report_path=report_path,
                manifest_path=manifest_path,
                log_entries=log_entries,
            )
            _emit_event(
                event_handler,
                {
                    "type": "done",
                    "output_dir": output_dir,
                    "successful_outputs": successful_outputs,
                    "report_path": report_path,
                    "manifest_path": manifest_path,
                },
            )
            return result

        if isinstance(message, ErrorMsg):
            _emit_event(
                event_handler,
                {
                    "type": "error",
                    "message": message.message,
                    "output_dir": message.output_dir,
                    "report_path": message.report_path,
                    "manifest_path": message.manifest_path,
                },
            )
            raise RuntimeError(message.message)

        if isinstance(message, StoppedMsg):
            _emit_event(
                event_handler,
                {
                    "type": "stopped",
                    "message": message.message,
                    "output_dir": message.output_dir,
                    "report_path": message.report_path,
                    "manifest_path": message.manifest_path,
                },
            )
            raise RuntimeError(message.message)

    raise RuntimeError("PDF 翻译任务异常结束：未收到完成消息。")


def _resolve_pdf_target_lang(target_lang: str | None, settings: AppSettings) -> str:
    fallback = settings.pdf.target_lang or "zh"
    candidate = str(target_lang or fallback).strip()
    if is_supported_target_lang(
        candidate,
        settings.custom_target_langs,
        include_optional=True,
    ):
        return candidate

    supported_map = get_supported_languages(
        settings.custom_target_langs,
        include_optional=True,
    )
    resolved = resolve_language_code(candidate, supported_map)
    if resolved is not None:
        return resolved

    supported_labels = ", ".join(sorted(supported_map))
    raise ValueError(f"不支持的 PDF 目标语言：{candidate}。可用项：{supported_labels}")


def _emit_event(
    event_handler: HeadlessPdfEventHandler | None,
    payload: dict[str, Any],
) -> None:
    if event_handler is None:
        return
    event_handler(payload)
