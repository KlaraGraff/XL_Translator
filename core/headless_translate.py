"""
Headless translation helpers for running the Excel pipeline without Streamlit.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from core.file_scanner import scan_path
from core.language_registry import (
    get_default_source_lang,
    get_default_target_lang,
    get_source_lang_display,
    get_supported_languages,
    get_supported_source_languages,
    get_target_lang_display,
    is_supported_source_lang,
    is_supported_target_lang,
)
from core.task_runner import (
    DoneMsg,
    ErrorMsg,
    LogMsg,
    ProgressMsg,
    StatusMsg,
    StoppedMsg,
    TaskRunner,
)
from core.tm_manager import init_db
from settings import AppSettings, load_settings

HeadlessEventHandler = Callable[[dict[str, Any]], None]


@dataclass
class HeadlessTranslationResult:
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
    log_entries: list[dict[str, str]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_runtime_settings(
    *,
    base_settings: AppSettings | None = None,
    target_lang: str | None = None,
    source_lang: str | None = None,
    output_dir: str | Path | None = None,
) -> AppSettings:
    """Build an in-memory settings object for headless runs."""
    settings = (base_settings or load_settings()).model_copy(deep=True)

    settings.target_lang = _resolve_target_lang(
        target_lang,
        settings,
    )
    settings.source_lang = _resolve_source_lang(
        source_lang,
        fallback=settings.source_lang,
    )

    if output_dir is None:
        settings.output.use_custom_output_dir = False
        settings.output.custom_output_dir = ""
    else:
        resolved_output = Path(output_dir).expanduser()
        settings.output.use_custom_output_dir = True
        settings.output.custom_output_dir = str(resolved_output)

    return settings


def run_translation_path(
    source_path: str | Path,
    *,
    settings: AppSettings | None = None,
    allow_xls_fallback: bool = False,
    poll_interval: float = 0.1,
    event_handler: HeadlessEventHandler | None = None,
) -> HeadlessTranslationResult:
    """Run one headless translation task and wait for completion."""
    input_path = Path(source_path).expanduser()
    if not input_path.is_absolute():
        input_path = input_path.resolve()

    if not input_path.exists():
        raise FileNotFoundError(f"路径不存在：{input_path}")

    runtime_settings = (settings or load_settings()).model_copy(deep=True)
    if not runtime_settings.target_lang:
        runtime_settings.target_lang = get_default_target_lang()
    runtime_settings.source_lang = _resolve_source_lang(
        runtime_settings.source_lang,
        fallback=get_default_source_lang(),
    )
    runtime_settings.target_lang = _resolve_target_lang(
        runtime_settings.target_lang,
        runtime_settings,
    )

    file_items = scan_path(input_path)
    if not file_items:
        raise ValueError(f"未在路径中发现可处理的 Excel 文件：{input_path}")

    init_db()

    source_root = input_path if input_path.is_dir() else input_path.parent
    runner = TaskRunner(
        file_items,
        runtime_settings,
        source_root=source_root,
        allow_xls_fallback=allow_xls_fallback,
        source_lang=runtime_settings.source_lang,
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
            _emit_event(
                event_handler,
                {
                    "type": "status",
                    "message": message.phase_desc,
                },
            )
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
            result = HeadlessTranslationResult(
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
                log_entries=log_entries,
            )
            _emit_event(
                event_handler,
                {
                    "type": "done",
                    "output_dir": message.output_dir,
                    "successful_outputs": successful_outputs,
                },
            )
            return result

        if isinstance(message, ErrorMsg):
            _emit_event(
                event_handler,
                {
                    "type": "error",
                    "message": message.message,
                },
            )
            raise RuntimeError(message.message)

        if isinstance(message, StoppedMsg):
            _emit_event(
                event_handler,
                {
                    "type": "stopped",
                    "message": message.message,
                },
            )
            raise RuntimeError(message.message)

    raise RuntimeError("翻译任务异常结束：未收到完成消息。")


def _emit_event(
    event_handler: HeadlessEventHandler | None,
    payload: dict[str, Any],
) -> None:
    if event_handler is None:
        return
    event_handler(payload)


def _resolve_target_lang(
    target_lang: str | None,
    settings: AppSettings,
) -> str:
    fallback = settings.target_lang or get_default_target_lang()
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
    alias_map = _build_alias_map(supported_map)
    resolved = alias_map.get(candidate.casefold())
    if resolved is not None:
        return resolved

    supported_labels = ", ".join(sorted(supported_map))
    raise ValueError(f"不支持的目标语言：{candidate}。可用项：{supported_labels}")


def _resolve_source_lang(
    source_lang: str | None,
    *,
    fallback: str,
) -> str:
    candidate = str(source_lang or fallback or get_default_source_lang()).strip()
    if is_supported_source_lang(candidate):
        return candidate

    supported_map = get_supported_source_languages()
    alias_map = _build_alias_map(supported_map)
    resolved = alias_map.get(candidate.casefold())
    if resolved is not None:
        return resolved

    supported_labels = ", ".join(sorted(supported_map))
    raise ValueError(f"不支持的源语言：{candidate}。可用项：{supported_labels}")


def _build_alias_map(supported_map: dict[str, str]) -> dict[str, str]:
    alias_map: dict[str, str] = {}
    for display_name, lang_code in supported_map.items():
        alias_map[display_name.casefold()] = lang_code
        alias_map[lang_code.casefold()] = lang_code
    return alias_map
