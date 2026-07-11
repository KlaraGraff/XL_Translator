"""Headless helpers for running the Word translation pipeline."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from core.language_registry import (
    get_default_source_lang,
    get_default_target_lang,
    get_source_lang_display,
    get_target_lang_display,
    is_supported_source_lang,
    is_supported_target_lang,
)
from core.task_runner import DoneMsg, ErrorMsg, LogMsg, ProgressMsg, StatusMsg, StoppedMsg
from core.tm_manager import init_db
from core.word_document import scan_word_path
from core.word_task_runner import WordTaskRunner
from settings import AppSettings, load_settings

HeadlessWordEventHandler = Callable[[dict[str, Any]], None]


@dataclass
class HeadlessWordTranslationResult:
    source_path: str
    output_dir: str
    file_results: list[dict[str, Any]]
    successful_outputs: list[str]
    elapsed_sec: float
    tm_hit_count: int
    api_call_count: int
    source_lang: str
    source_lang_display: str
    target_lang: str
    target_lang_display: str
    file_count: int
    issues: list[dict[str, Any]]
    report_path: str
    log_entries: list[dict[str, str]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_word_translation_path(
    source_path: str | Path,
    *,
    settings: AppSettings | None = None,
    untranslated_only: bool = False,
    protect_scheme_cover: bool = False,
    poll_interval: float = 0.1,
    event_handler: HeadlessWordEventHandler | None = None,
) -> HeadlessWordTranslationResult:
    """Run one headless Word translation task and wait for completion."""
    input_path = Path(source_path).expanduser()
    if not input_path.is_absolute():
        input_path = input_path.resolve()

    if not input_path.exists():
        raise FileNotFoundError(f"路径不存在：{input_path}")

    runtime_settings = (settings or load_settings()).model_copy(deep=True)
    if not is_supported_target_lang(
        runtime_settings.target_lang,
        runtime_settings.custom_target_langs,
        include_optional=True,
    ):
        runtime_settings.target_lang = get_default_target_lang()
    if not is_supported_source_lang(runtime_settings.source_lang):
        runtime_settings.source_lang = get_default_source_lang()

    file_items = scan_word_path(input_path)
    if not file_items:
        raise ValueError(f"未在路径中发现可处理的 Word 文件：{input_path}")

    init_db()

    source_root = input_path if input_path.is_dir() else input_path.parent
    runner = WordTaskRunner(
        file_items,
        runtime_settings,
        source_root=source_root,
        source_lang=runtime_settings.source_lang,
        untranslated_only=untranslated_only,
        protect_scheme_cover=protect_scheme_cover,
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

        if isinstance(message, DoneMsg):
            successful_outputs = [
                str(item["output"])
                for item in message.file_results
                if item.get("success") and item.get("output")
            ]
            result = HeadlessWordTranslationResult(
                source_path=str(input_path),
                output_dir=message.output_dir,
                file_results=message.file_results,
                successful_outputs=successful_outputs,
                elapsed_sec=message.elapsed_sec,
                tm_hit_count=message.tm_hit_count,
                api_call_count=message.api_call_count,
                source_lang=runtime_settings.source_lang,
                source_lang_display=get_source_lang_display(runtime_settings.source_lang),
                target_lang=runtime_settings.target_lang,
                target_lang_display=get_target_lang_display(
                    runtime_settings.target_lang,
                    runtime_settings.custom_target_langs,
                    include_optional=True,
                ),
                file_count=len(file_items),
                issues=message.issues,
                report_path=message.report_path,
                log_entries=log_entries,
            )
            _emit_event(
                event_handler,
                {
                    "type": "done",
                    "output_dir": message.output_dir,
                    "successful_outputs": successful_outputs,
                    "issues": message.issues,
                    "report_path": message.report_path,
                },
            )
            return result

        if isinstance(message, ErrorMsg):
            _emit_event(event_handler, {"type": "error", "message": message.message})
            raise RuntimeError(message.message)

        if isinstance(message, StoppedMsg):
            _emit_event(event_handler, {"type": "stopped", "message": message.message})
            raise RuntimeError(message.message)

    raise RuntimeError("Word 翻译任务异常结束：未收到完成消息。")


def _emit_event(
    event_handler: HeadlessWordEventHandler | None,
    payload: dict[str, Any],
) -> None:
    if event_handler is None:
        return
    event_handler(payload)
