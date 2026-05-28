"""Background runner for Word translation tasks."""

from __future__ import annotations

import json
import queue
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from loguru import logger

from core import tm_manager
from core.api_concurrency_control import (
    ApiKeyTemporarilyUnavailableError,
    handle_api_concurrency_limit,
)
from core.api_config_check import check_translation_api_config
from core.api_scheduler import (
    API_REQUEST_CATEGORY_NORMAL,
    API_REQUEST_CATEGORY_RECOVERY,
    WeightedApiScheduler,
)
from core.bilingual_writer import get_custom_output_dir_error
from core.engine_dispatcher import (
    build_engine,
    get_system_prompt,
    is_local_engine_name,
)
from core.language_registry import build_lang_pair
from core.task_logger import TaskLogger
from core.task_runner import (
    DoneMsg,
    ErrorMsg,
    LogMsg,
    ProgressMsg,
    StatusMsg,
    StoppedMsg,
    TaskStopped,
    WordRecoveryStatusMsg,
)
from core.translation_filter import (
    VALIDATION_STATUS_SOFT_PASS_REVIEW,
    VALIDATION_PROFILE_STRICT,
    VALIDATION_PROFILE_WORD_RECOVERY,
    TranslationValidationIssue,
    TranslationValidationResult,
    validate_translation,
)
from core.translation_protocol import (
    extract_replace_translation,
    is_replace_translation,
    should_store_translation_in_tm,
)
from core.word_converter import (
    WordConversionError,
    convert_doc_to_docx,
    is_legacy_word_doc,
)
from core.word_document import (
    WordFileItem,
    build_word_output_dir,
    extract_word_segments,
    write_bilingual_docx,
)
from core.word_batching import (
    WordBatchRunStats,
    estimate_api_request_weight,
    translate_word_texts,
)
from engines.base_engine import TranslationEngine, strip_markdown_json
from settings import AppSettings, provider_key_overrides

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_SEMANTIC_VERDICT_EQUIVALENT = "equivalent"
_SEMANTIC_VERDICT_NOT_EQUIVALENT = "not_equivalent"
_SEMANTIC_VERDICT_UNCERTAIN = "uncertain"
_WORD_RECOVERY_NORMAL_SOFT_RATIO = 0.8
_SEMANTIC_MIN_LENGTH_RATIO = 0.18
_SEMANTIC_RESIDUAL_CJK_RATIO_BLOCK = 0.12
_SEMANTIC_RESIDUAL_CJK_COUNT_BLOCK = 12


@dataclass(frozen=True)
class _WordRetryEvaluation:
    accepted: bool
    validation: TranslationValidationResult


@dataclass(frozen=True)
class _SemanticArbitrationResult:
    verdict: str
    reason: str = ""

    @property
    def equivalent(self) -> bool:
        return self.verdict == _SEMANTIC_VERDICT_EQUIVALENT


@dataclass
class _WordRecoveryOutcome:
    fixed_sources: list[str]
    unresolved_sources: list[str]
    accepted_translations: dict[str, str]
    recovery_review_results: dict[str, TranslationValidationResult]
    semantic_review_results: dict[str, TranslationValidationResult]
    unresolved_validation_results: dict[str, TranslationValidationResult]
    semantic_check_count: int = 0


def _source_position_count(
    source: str,
    source_locations: dict[str, list[dict]] | None,
) -> int:
    return max(1, len((source_locations or {}).get(source) or []))


def _sources_position_count(
    sources: list[str] | set[str],
    source_locations: dict[str, list[dict]] | None,
) -> int:
    return sum(_source_position_count(source, source_locations) for source in sources)


def _iter_source_location_labels(
    source: str,
    source_locations: dict[str, list[dict]] | None,
) -> list[str]:
    locations = (source_locations or {}).get(source) or []
    if not locations:
        return ["未知文件 · 正文 · 未知位置"]

    labels: list[str] = []
    for location in locations:
        file_name = str(location.get("file") or "未知文件")
        section_path = str(location.get("section_path") or "正文")
        location_label = str(location.get("location_label") or "未知位置")
        labels.append(f"{file_name} · {section_path} · {location_label}")
    return labels


class WordTaskRunner:
    """Run the Word translation pipeline on a background thread."""

    def __init__(
        self,
        file_items: list[WordFileItem],
        settings: AppSettings,
        source_root: Path | str | None = None,
        source_lang: str | None = None,
        key_overrides: dict[str, str] | None = None,
        api_scheduler: WeightedApiScheduler | None = None,
    ):
        self._files = file_items
        self._settings = settings
        self._source_root = Path(source_root) if source_root else None
        self._source_lang = str(source_lang or settings.source_lang or "zh").strip() or "zh"
        self._key_overrides = dict(key_overrides or {})
        self._api_scheduler_override = api_scheduler
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._task_logger = TaskLogger(enabled=True)

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_with_overrides, daemon=True)
        self._thread.start()

    @property
    def task_id(self) -> str:
        return self._task_logger.task_id

    def stop(self) -> None:
        self._stop_event.set()

    def stop_requested(self) -> bool:
        return self._stop_event.is_set()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def needs_poll(self) -> bool:
        return self.is_running() or not self._queue.empty()

    def get_message(self, timeout: float = 0.05):
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def _log(self, level: str, message: str) -> None:
        self._queue.put(LogMsg(level=level, message=message))
        logger.info(f"[{level}] {message}")

    def _run_with_overrides(self) -> None:
        with provider_key_overrides(self._key_overrides):
            self._run()

    def _run(self) -> None:
        start_ts = datetime.now()
        settings = self._settings
        source_lang = self._source_lang
        target_lang = settings.target_lang
        lang_pair = build_lang_pair(target_lang, source_lang=source_lang)
        max_len = settings.tm.max_len
        tm_hit_count = 0
        api_call_count = 0
        file_results: list[dict] = []
        stopped_message: str | None = None
        fatal_error_message: str | None = None

        try:
            config_check = check_translation_api_config(settings)
            if not config_check.ok:
                detail = f"（{config_check.detail}）" if config_check.detail else ""
                self._queue.put(ErrorMsg(message=f"{config_check.message}{detail}"))
                return
            engine = build_engine(settings)
            system_prompt = get_system_prompt(
                settings,
                target_lang=target_lang,
                source_lang=source_lang,
            )
            concurrency = (
                settings.engine.ollama_concurrency
                if settings.engine.mode == "local"
                else settings.engine.concurrency
            )
            api_scheduler = (
                self._api_scheduler_override
                if settings.engine.mode != "local"
                else None
            )
            if api_scheduler is None and settings.engine.mode != "local":
                api_scheduler = WeightedApiScheduler(
                    concurrency,
                    normal_soft_ratio=_WORD_RECOVERY_NORMAL_SOFT_RATIO,
                )
        except Exception as exc:
            self._queue.put(ErrorMsg(message=f"引擎初始化失败：{exc}"))
            return

        if not self._files:
            self._queue.put(ErrorMsg(message="未选择可翻译的 Word 文件。"))
            return

        root_for_output = self._source_root if self._source_root else self._files[0].path.parent
        custom_output_dir = settings.output.custom_output_dir if settings.output.use_custom_output_dir else None
        try:
            if settings.output.use_custom_output_dir:
                output_error = get_custom_output_dir_error(custom_output_dir)
                if output_error is not None:
                    raise ValueError(output_error)
            output_dir = build_word_output_dir(root_for_output, custom_output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            self._queue.put(ErrorMsg(message=f"输出目录初始化失败：{exc}"))
            return

        def _raise_if_stopped(message: str = "任务已中止") -> None:
            if self._stop_event.is_set():
                raise TaskStopped(message)

        self._task_logger.task_start(
            files=self._files,
            engine_name=engine.engine_name,
            target_lang=target_lang,
            keep_original_sheets=False,
            formula_display_value_backfill=False,
            enable_excel_autofit=False,
            lock_row_height=False,
        )
        self._log("INFO", f"[诊断] source_root={self._source_root} | custom_output_dir={custom_output_dir} | output_dir={output_dir}")
        self._log("INFO", f"扫描到 {len(self._files)} 个 Word 文件")

        phase_total = 3
        file_texts: list[set[str]] = []
        global_unique_texts: set[str] = set()
        segment_locations: dict[str, list[dict]] = {}
        quality_issues: list[dict] = []
        unresolved_review_sources: set[str] = set()
        recovery_review_sources: set[str] = set()
        process_paths: list[Path] = []
        converted_temp_paths: list[Path] = []

        try:
            _raise_if_stopped()

            self._queue.put(StatusMsg(phase_desc=f"状态：[阶段 1/{phase_total}] 正在提取 Word 文本..."))
            t_phase1 = datetime.now()
            for index, file_item in enumerate(self._files):
                _raise_if_stopped()
                self._queue.put(
                    ProgressMsg(
                        phase_index=1,
                        phase_total=phase_total,
                        phase_name="Word 提取",
                        step_done=index,
                        step_total=len(self._files),
                    )
                )
                self._log("INFO", f"[阶段 1] 提取文本：{file_item.name}（{index + 1}/{len(self._files)}）")
                try:
                    t0 = datetime.now()
                    process_path = file_item.path
                    if is_legacy_word_doc(file_item.path):
                        self._queue.put(
                            StatusMsg(
                                phase_desc=(
                                    f"状态：[阶段 1/{phase_total}] 正在转换 .doc 文件："
                                    f"{file_item.name}"
                                )
                            )
                        )
                        try:
                            conversion = convert_doc_to_docx(
                                file_item.path,
                                prefer_native_word=settings.word_conversion.prefer_native_word,
                            )
                            process_path = conversion.path
                            converted_temp_paths.append(conversion.path)
                            for fallback_message in conversion.fallback_messages:
                                self._log("INFO", f"{file_item.name}：{fallback_message}，已继续尝试下一转换方式。")
                            self._log(
                                "INFO",
                                (
                                    f".doc 转换完成 {file_item.name}，"
                                    f"使用 {conversion.method}，"
                                    f"耗时 {(datetime.now() - t0).total_seconds():.2f}s"
                                ),
                            )
                        except WordConversionError as exc:
                            process_paths.append(process_path)
                            file_texts.append(set())
                            self._log("ERROR", f"Word 源文件转换失败 {file_item.name}: {exc}")
                            self._task_logger.file_error(file_item.name, f"Word 源文件转换失败: {exc}")
                            file_results.append(
                                {
                                    "name": file_item.name,
                                    "success": False,
                                    "error": f"Word 源文件转换失败: {exc}",
                                }
                            )
                            continue
                    process_paths.append(process_path)
                    segments = extract_word_segments(
                        process_path,
                        target_lang=target_lang,
                        source_lang=source_lang,
                    )
                    _remember_segment_locations(segment_locations, file_item.name, segments)
                    text_set = {segment.source for segment in segments}
                    file_texts.append(text_set)
                    global_unique_texts.update(text_set)
                    elapsed = (datetime.now() - t0).total_seconds()
                    self._log("INFO", f"  → {file_item.name}：{len(text_set)} 个词条（{elapsed:.3f}s）")
                    self._task_logger.file_collected(file_item.name, len(text_set), elapsed)
                except Exception as exc:
                    if len(process_paths) < index + 1:
                        process_paths.append(file_item.path)
                    file_texts.append(set())
                    self._log("ERROR", f"Word 文件读取失败 {file_item.name}: {exc}")
                    self._task_logger.file_error(file_item.name, f"Word 文件读取失败: {exc}")
                    file_results.append(
                        {
                            "name": file_item.name,
                            "success": False,
                            "error": f"Word 文件读取失败: {exc}",
                        }
                    )

            self._queue.put(
                ProgressMsg(
                    phase_index=1,
                    phase_total=phase_total,
                    phase_name="Word 提取",
                    step_done=len(self._files),
                    step_total=len(self._files),
                )
            )
            phase1_elapsed = (datetime.now() - t_phase1).total_seconds()
            self._log("OK", f"[阶段 1 完成] Word 去重词汇池：{len(global_unique_texts)} 个唯一词条（{phase1_elapsed:.2f}s）")
            self._task_logger.global_collected(
                total_unique=len(global_unique_texts),
                file_count=len(self._files),
                elapsed=phase1_elapsed,
            )

            _raise_if_stopped()

            self._queue.put(StatusMsg(phase_desc=f"状态：[阶段 2/{phase_total}] 正在比对翻译记忆库..."))
            t_phase2 = datetime.now()
            all_texts = list(global_unique_texts)
            tm_result = tm_manager.lookup_batch(all_texts, lang_pair)
            hits = {text: value for text, value in tm_result.items() if value is not None}
            misses = [text for text, value in tm_result.items() if value is None]
            tm_hit_count = len(hits)
            api_call_count = len(misses)
            self._log("INFO", f"[阶段 2] TM 命中：{tm_hit_count}  待 API：{api_call_count}")
            self._task_logger.global_tm_result(hits=tm_hit_count, misses=api_call_count)
            self._queue.put(
                ProgressMsg(
                    phase_index=2,
                    phase_total=phase_total,
                    phase_name="云端翻译",
                    step_done=0 if api_call_count else 1,
                    step_total=max(api_call_count, 1),
                )
            )

            api_translations: dict[str, str] = {}
            if misses and not self._stop_event.is_set():
                self._queue.put(StatusMsg(phase_desc=f"状态：[阶段 2/{phase_total}] 正在请求大模型翻译未命中词汇..."))

                def progress_cb(done, total):
                    self._queue.put(
                        ProgressMsg(
                            phase_index=2,
                            phase_total=phase_total,
                            phase_name="云端翻译",
                            step_done=done,
                            step_total=total,
                        )
                    )

                t0 = datetime.now()
                word_batch_stats = WordBatchRunStats()
                word_prompt = _build_word_batch_prompt(system_prompt)
                retry_prompt = _build_word_retry_prompt(system_prompt)
                retry_batch_settings = settings.word_batch.model_copy(
                    update={"max_paragraphs_per_batch": 1}
                )
                recovery_pool = _WordRecoveryPool(
                    engine=engine,
                    target_lang=target_lang,
                    retry_prompt=retry_prompt,
                    retry_batch_settings=retry_batch_settings,
                    retry_attempts=settings.word_batch.strict_retry_attempts,
                    source_lang=source_lang,
                    api_scheduler=api_scheduler,
                    concurrency=concurrency,
                    should_stop=self.stop_requested,
                    log_callback=self._log,
                    status_callback=lambda msg: self._queue.put(msg),
                    source_locations=segment_locations,
                )

                def recovery_candidate_cb(source: str, candidate: str) -> None:
                    if self.stop_requested():
                        return
                    if _needs_word_translation_retry(
                        source,
                        candidate,
                        source_lang=source_lang,
                        target_lang=target_lang,
                    ):
                        recovery_pool.add_candidate(source, candidate)

                self._log(
                    "INFO",
                    (
                        "Word 批次策略："
                        f"每批最多 {settings.word_batch.max_paragraphs_per_batch} 段，"
                        f"字符预算 {settings.word_batch.max_chars_per_batch}，"
                        f"长段拆分阈值 {settings.word_batch.split_paragraph_chars}，"
                        f"失败严格重试 {settings.word_batch.strict_retry_attempts} 轮"
                    ),
                )
                api_translations = translate_word_texts(
                    misses,
                    engine,
                    target_lang,
                    word_prompt,
                    settings.word_batch,
                    concurrency,
                    progress_callback=progress_cb,
                    error_callback=lambda msg: self._log("WARN", msg),
                    should_stop=self.stop_requested,
                    source_lang=source_lang,
                    stats=word_batch_stats,
                    quality_profile=None,
                    api_scheduler=api_scheduler,
                    request_category=API_REQUEST_CATEGORY_NORMAL,
                    candidate_callback=recovery_candidate_cb,
                )
                self._log(
                    "INFO",
                    (
                        "Word 实际请求："
                        f"{word_batch_stats.batch_count} 批，"
                        f"{word_batch_stats.unit_count} 个请求片段，"
                        f"长段拆分 {word_batch_stats.split_source_count} 段，"
                        f"缩小重试 {word_batch_stats.retry_count} 次"
                        + (
                            f"，自适应降并发 {word_batch_stats.adaptive_concurrency_reductions} 次，"
                            f"最低并发 {word_batch_stats.adaptive_lowest_concurrency}"
                            if word_batch_stats.adaptive_concurrency_reductions
                            else ""
                        )
                    ),
                )
                _raise_if_stopped("任务已中止，未写入剩余 Word 翻译结果。")

                retry_sources = [
                    source
                    for source in misses
                    if _needs_word_translation_retry(
                        source,
                        api_translations.get(source),
                        source_lang=source_lang,
                        target_lang=target_lang,
                    )
                ]
                if retry_sources:
                    for source in retry_sources:
                        recovery_pool.add_candidate(source, api_translations.get(source, ""))
                    recovery_outcome = recovery_pool.wait_for_completion()
                    _raise_if_stopped("任务已中止，未写入剩余 Word 翻译结果。")
                    api_translations.update(recovery_outcome.accepted_translations)
                    for source in recovery_outcome.unresolved_sources:
                        api_translations[source] = source
                    recovery_review_sources.update(recovery_outcome.recovery_review_results)
                    recovery_review_sources.update(recovery_outcome.semantic_review_results)
                    standard_fixed_sources = [
                        source
                        for source in recovery_outcome.fixed_sources
                        if (
                            source not in recovery_outcome.recovery_review_results
                            and source not in recovery_outcome.semantic_review_results
                        )
                    ]
                    _add_quality_issues(
                        quality_issues,
                        segment_locations,
                        standard_fixed_sources,
                        problem="初次翻译未获得有效译文",
                        status="已自动单段严格重试并恢复译文。",
                        severity="resolved",
                    )
                    if recovery_outcome.recovery_review_results:
                        _add_quality_issues(
                            quality_issues,
                            segment_locations,
                            list(recovery_outcome.recovery_review_results.keys()),
                            problem="重试译文按 Word 恢复规则自动接受",
                            status=(
                                "译文主体已通过恢复校验，本段已写入译文并高亮原文，"
                                "建议复核提示片段。"
                            ),
                            severity="resolved",
                            validation_results=recovery_outcome.recovery_review_results,
                        )
                    if recovery_outcome.semantic_review_results:
                        _add_quality_issues(
                            quality_issues,
                            segment_locations,
                            list(recovery_outcome.semantic_review_results.keys()),
                            problem="规则校验未通过，语义仲裁自动接受",
                            status=(
                                "候选译文未通过程序化规则校验，但语义仲裁判定与原文完整等义；"
                                "本段已写入译文并高亮原文，且不会写入翻译记忆库。"
                            ),
                            severity="resolved",
                            validation_results=recovery_outcome.semantic_review_results,
                        )
                    if recovery_outcome.unresolved_sources:
                        unresolved_review_sources.update(recovery_outcome.unresolved_sources)
                        _add_quality_issues(
                            quality_issues,
                            segment_locations,
                            recovery_outcome.unresolved_sources,
                            problem="重试后仍未获得有效译文",
                            status=(
                                f"已进行 {settings.word_batch.strict_retry_attempts} "
                                "轮并行单段重试与语义仲裁，仍保留原文，需人工复核。"
                            ),
                            severity="needs_review",
                            validation_results=recovery_outcome.unresolved_validation_results,
                        )
                        for source in recovery_outcome.unresolved_sources:
                            for label in _iter_source_location_labels(source, segment_locations):
                                self._log("WARN", f"{label} 保留原文，需复核")
                else:
                    recovery_pool.wait_for_completion()

                elapsed = (datetime.now() - t0).total_seconds()
                self._log("OK", f"API 翻译完成，返回 {len(api_translations)} 条（{elapsed:.2f}s）")
                self._task_logger.global_api_done(returned=len(api_translations), elapsed=elapsed)

                new_pairs = [
                    (source, translated)
                    for source, translated in api_translations.items()
                    if (
                        source not in recovery_review_sources
                        and source not in unresolved_review_sources
                        and should_store_translation_in_tm(source, translated)
                    )
                ]
                written = tm_manager.insert_batch(new_pairs, lang_pair, max_len, engine.engine_name)
                if written:
                    self._log("INFO", f"新增 TM 词条：{written} 条")

            global_translations = {**api_translations, **hits}
            phase2_elapsed = (datetime.now() - t_phase2).total_seconds()
            self._log("OK", f"[阶段 2 完成] 翻译数据就绪（{phase2_elapsed:.2f}s）")

            _raise_if_stopped()

            self._queue.put(StatusMsg(phase_desc=f"状态：[阶段 3/{phase_total}] 正在生成双语 Word..."))
            self._queue.put(
                ProgressMsg(
                    phase_index=3,
                    phase_total=phase_total,
                    phase_name="生成文件",
                    step_done=0,
                    step_total=max(len(self._files), 1),
                )
            )

            t_phase3 = datetime.now()
            source_root = self._source_root if self._source_root else self._files[0].path.parent
            for index, file_item in enumerate(self._files):
                _raise_if_stopped()
                already_failed = any(
                    result["name"] == file_item.name and not result.get("success")
                    for result in file_results
                )
                if already_failed:
                    continue

                self._queue.put(
                    ProgressMsg(
                        phase_index=3,
                        phase_total=phase_total,
                        phase_name="生成文件",
                        step_done=index,
                        step_total=len(self._files),
                    )
                )
                self._log("INFO", f"[阶段 3] 写入 Word：{file_item.name}（{index + 1}/{len(self._files)}）")
                try:
                    rel_subdir = file_item.path.parent.relative_to(source_root)
                except ValueError:
                    rel_subdir = Path()

                try:
                    t0 = datetime.now()
                    source_path = process_paths[index] if index < len(process_paths) else file_item.path
                    out_path = write_bilingual_docx(
                        source_path=source_path,
                        output_dir=output_dir / rel_subdir,
                        translations=global_translations,
                        target_lang=target_lang,
                        source_lang=source_lang,
                        output_name=_word_output_source_name(file_item.path),
                        review_highlight_sources=(
                            unresolved_review_sources | recovery_review_sources
                            if settings.word_review.highlight_unresolved
                            else None
                        ),
                        review_highlight_color=settings.word_review.highlight_color,
                        log_callback=lambda msg: self._log(
                            "OK" if msg.startswith("[OK]") else "INFO",
                            msg,
                        ),
                    )
                    elapsed = (datetime.now() - t0).total_seconds()
                    this_file_texts = file_texts[index]
                    this_tm = sum(1 for text in this_file_texts if text in hits)
                    this_api = sum(1 for text in this_file_texts if text in misses)
                    self._task_logger.file_done(
                        filename=file_item.name,
                        elapsed=elapsed,
                        tm_hits=this_tm,
                        api_calls=this_api,
                    )
                    file_results.append(
                        {
                            "name": file_item.name,
                            "output": str(out_path),
                            "success": True,
                            "issues": [
                                issue
                                for issue in quality_issues
                                if issue.get("file") == file_item.name
                            ],
                        }
                    )
                    self._log("OK", f"文件完成：{file_item.name}（{elapsed:.2f}s）")
                except Exception as exc:
                    self._log("ERROR", f"Word 文件写入失败 {file_item.name}：{exc}")
                    self._task_logger.file_error(file_item.name, str(exc))
                    file_results.append(
                        {
                            "name": file_item.name,
                            "success": False,
                            "error": str(exc),
                        }
                    )

            self._queue.put(
                ProgressMsg(
                    phase_index=3,
                    phase_total=phase_total,
                    phase_name="生成文件",
                    step_done=len(self._files),
                    step_total=len(self._files),
                )
            )
            phase3_elapsed = (datetime.now() - t_phase3).total_seconds()
            self._log("OK", f"[阶段 3 完成] Word 文件写入完毕（{phase3_elapsed:.2f}s）")
        except TaskStopped as exc:
            stopped_message = str(exc)
        except ApiKeyTemporarilyUnavailableError as exc:
            fatal_error_message = str(exc)

        elapsed_sec = (datetime.now() - start_ts).total_seconds()
        self._task_logger.task_end(elapsed_sec=elapsed_sec, file_results=file_results)
        report_path = _write_word_quality_report(
            output_dir=output_dir,
            file_results=file_results,
            issues=quality_issues,
            elapsed_sec=elapsed_sec,
            tm_hit_count=tm_hit_count,
            api_call_count=api_call_count,
        )
        _cleanup_converted_word_paths(converted_temp_paths, self._log)

        if stopped_message is not None:
            self._log("WARN", stopped_message)
            self._queue.put(StoppedMsg(message=stopped_message))
            return

        if fatal_error_message is not None:
            self._log("ERROR", fatal_error_message)
            self._task_logger.error(fatal_error_message)
            self._queue.put(ErrorMsg(message=fatal_error_message))
            return

        self._queue.put(
            DoneMsg(
                output_dir=str(output_dir),
                file_results=file_results,
                elapsed_sec=elapsed_sec,
                tm_hit_count=tm_hit_count,
                api_call_count=api_call_count,
                issues=quality_issues,
                report_path=str(report_path) if report_path else "",
            )
        )


def _word_output_source_name(path: Path) -> str:
    return f"{path.stem}.docx" if is_legacy_word_doc(path) else path.name


def _cleanup_converted_word_paths(
    paths: list[Path],
    log_callback: Callable[[str, str], None],
) -> None:
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except Exception as exc:
            log_callback("WARN", f"临时 Word 转换文件清理失败 {path.name}: {exc}")


def _needs_word_translation_retry(
    source: str,
    translated: str | None,
    *,
    source_lang: str,
    target_lang: str = "",
) -> bool:
    """Whether a Word source paragraph should be retried before writing."""
    return not _evaluate_word_translation(
        source,
        translated,
        source_lang=source_lang,
        target_lang=target_lang,
        allow_recovery=False,
    ).accepted


def _evaluate_word_translation(
    source: str,
    translated: str | None,
    *,
    source_lang: str,
    target_lang: str,
    allow_recovery: bool,
) -> _WordRetryEvaluation:
    source_text = str(source or "").strip()
    if not source_text:
        return _WordRetryEvaluation(True, TranslationValidationResult())
    if source_lang == "zh" and not _CJK_RE.search(source_text):
        return _WordRetryEvaluation(True, TranslationValidationResult())

    translated_text = _candidate_validation_text(translated)
    strict_validation = validate_translation(
        source_text,
        translated_text,
        target_lang=target_lang,
        source_lang=source_lang,
        profile=VALIDATION_PROFILE_STRICT,
    )
    has_residual_cjk = (
        source_lang == "zh"
        and target_lang not in {"zh", "ja"}
        and bool(_CJK_RE.search(translated_text))
    )
    if strict_validation.is_pass and not has_residual_cjk:
        return _WordRetryEvaluation(True, strict_validation)

    if allow_recovery:
        recovery_validation = validate_translation(
            source_text,
            translated_text,
            target_lang=target_lang,
            source_lang=source_lang,
            profile=VALIDATION_PROFILE_WORD_RECOVERY,
        )
        if not recovery_validation.is_fail:
            return _WordRetryEvaluation(True, recovery_validation)
        return _WordRetryEvaluation(False, recovery_validation)

    return _WordRetryEvaluation(False, strict_validation)


@dataclass
class _WordRecoveryState:
    source: str
    attempts_done: int = 0
    retry_inflight: bool = False
    semantic_inflight: int = 0
    accepted_translation: str = ""
    accepted_by: str = ""
    accepted_validation: TranslationValidationResult = field(default_factory=TranslationValidationResult)
    last_validation: TranslationValidationResult = field(default_factory=TranslationValidationResult)
    seen_semantic_candidates: set[str] = field(default_factory=set)

    @property
    def accepted(self) -> bool:
        return bool(self.accepted_by)

    def complete(self, max_attempts: int) -> bool:
        return (
            self.accepted
            or (
                self.attempts_done >= max_attempts
                and not self.retry_inflight
                and self.semantic_inflight <= 0
            )
        )


class _WordRecoveryPool:
    """Parallel Word recovery pool for retry and semantic arbitration."""

    def __init__(
        self,
        *,
        engine,
        target_lang: str,
        retry_prompt: str,
        retry_batch_settings,
        retry_attempts: int,
        source_lang: str,
        api_scheduler: WeightedApiScheduler | None,
        concurrency: int,
        should_stop,
        log_callback: Callable[[str, str], None] | None = None,
        status_callback: Callable[[WordRecoveryStatusMsg], None] | None = None,
        source_locations: dict[str, list[dict]] | None = None,
        enable_semantic: bool = True,
    ) -> None:
        try:
            self._max_attempts = max(1, int(retry_attempts))
        except (TypeError, ValueError):
            self._max_attempts = 1
        self._engine = engine
        self._target_lang = target_lang
        self._retry_prompt = retry_prompt
        self._retry_batch_settings = retry_batch_settings
        self._source_lang = source_lang
        self._api_scheduler = api_scheduler
        self._should_stop = should_stop
        self._log_callback = log_callback
        self._status_callback = status_callback
        self._source_locations = source_locations or {}
        self._enable_semantic = enable_semantic and _engine_supports_chat(engine)
        self._states: dict[str, _WordRecoveryState] = {}
        self._futures = set()
        self._condition = threading.Condition()
        self._executor = ThreadPoolExecutor(max_workers=max(1, int(concurrency or 1)))
        self._semantic_check_count = 0
        self._semantic_checked_sources: set[str] = set()
        self._semantic_accepted_sources: set[str] = set()
        self._semantic_uncertain_sources: set[str] = set()
        self._latest_retry_round = 0
        self._fatal_error: BaseException | None = None

    def _position_count(self, source: str) -> int:
        return _source_position_count(source, self._source_locations)

    def _log_source_locations(self, level: str, source: str, message: str) -> None:
        if not self._log_callback:
            return
        for label in _iter_source_location_labels(source, self._source_locations):
            self._log_callback(level, f"{label} {message}")

    def _emit_status_locked(self) -> None:
        if not self._status_callback:
            return
        retry_processing_count = sum(
            self._position_count(source)
            for source, state in self._states.items()
            if state.retry_inflight
        )
        retry_recovered_count = sum(
            self._position_count(source)
            for source, state in self._states.items()
            if state.accepted_by in {"strict_retry", "word_recovery"}
        )
        retry_unresolved_count = sum(
            self._position_count(source)
            for source, state in self._states.items()
            if not state.accepted
        )
        semantic_processing_count = sum(
            self._position_count(source)
            for source, state in self._states.items()
            if state.semantic_inflight > 0
        )
        msg = WordRecoveryStatusMsg(
            retry_round=self._latest_retry_round,
            retry_total=self._max_attempts,
            retry_processing_count=retry_processing_count,
            retry_recovered_count=retry_recovered_count,
            retry_unresolved_count=retry_unresolved_count,
            semantic_processing_count=semantic_processing_count,
            semantic_checked_count=_sources_position_count(
                self._semantic_checked_sources,
                self._source_locations,
            ),
            semantic_accepted_count=_sources_position_count(
                self._semantic_accepted_sources,
                self._source_locations,
            ),
            semantic_uncertain_count=_sources_position_count(
                self._semantic_uncertain_sources,
                self._source_locations,
            ),
        )
        self._status_callback(msg)

    def add_candidate(
        self,
        source: str,
        candidate: str | None,
        *,
        allow_recovery: bool = False,
    ) -> None:
        source_text = str(source or "").strip()
        if not source_text or (self._should_stop and self._should_stop()):
            return

        candidate_text = str(candidate or "").strip()
        evaluation = _evaluate_word_translation(
            source_text,
            candidate_text,
            source_lang=self._source_lang,
            target_lang=self._target_lang,
            allow_recovery=allow_recovery,
        )

        with self._condition:
            state = self._states.setdefault(source_text, _WordRecoveryState(source=source_text))
            if state.accepted:
                return
            if evaluation.accepted:
                accepted_by = (
                    "word_recovery"
                    if evaluation.validation.needs_review
                    else "strict_retry"
                )
                self._accept_locked(state, candidate_text, accepted_by, evaluation.validation)
                return

            state.last_validation = evaluation.validation
            self._schedule_semantic_locked(state, candidate_text, evaluation.validation)
            self._schedule_retry_locked(state)
            self._emit_status_locked()
            self._condition.notify_all()

    def wait_for_completion(self) -> _WordRecoveryOutcome:
        with self._condition:
            while self._fatal_error is None and not self._all_complete_locked():
                self._condition.wait(timeout=0.1)

            fatal_error = self._fatal_error

        if fatal_error is not None:
            self._executor.shutdown(wait=True, cancel_futures=True)
            raise fatal_error

        self._executor.shutdown(wait=True)
        return self._build_outcome()

    def _all_complete_locked(self) -> bool:
        return all(
            state.complete(self._max_attempts)
            for state in self._states.values()
        )

    def _build_outcome(self) -> _WordRecoveryOutcome:
        with self._condition:
            fixed_sources: list[str] = []
            unresolved_sources: list[str] = []
            accepted_translations: dict[str, str] = {}
            recovery_review_results: dict[str, TranslationValidationResult] = {}
            semantic_review_results: dict[str, TranslationValidationResult] = {}
            unresolved_validation_results: dict[str, TranslationValidationResult] = {}

            for source, state in self._states.items():
                if state.accepted:
                    fixed_sources.append(source)
                    accepted_translations[source] = state.accepted_translation
                    if state.accepted_by == "word_recovery":
                        recovery_review_results[source] = state.accepted_validation
                    elif state.accepted_by == "semantic":
                        semantic_review_results[source] = state.accepted_validation
                    continue
                unresolved_sources.append(source)
                unresolved_validation_results[source] = state.last_validation

            return _WordRecoveryOutcome(
                fixed_sources=fixed_sources,
                unresolved_sources=unresolved_sources,
                accepted_translations=accepted_translations,
                recovery_review_results=recovery_review_results,
                semantic_review_results=semantic_review_results,
                unresolved_validation_results=unresolved_validation_results,
                semantic_check_count=self._semantic_check_count,
            )

    def _accept_locked(
        self,
        state: _WordRecoveryState,
        candidate: str,
        accepted_by: str,
        validation: TranslationValidationResult,
    ) -> None:
        if state.accepted:
            return
        state.accepted_translation = str(candidate or "").strip()
        state.accepted_by = accepted_by
        state.accepted_validation = validation
        self._emit_status_locked()
        self._condition.notify_all()

    def _schedule_retry_locked(self, state: _WordRecoveryState) -> None:
        if state.accepted or state.retry_inflight:
            return
        if state.attempts_done >= self._max_attempts:
            return
        if self._should_stop and self._should_stop():
            return
        state.retry_inflight = True
        attempt_index = state.attempts_done + 1
        self._latest_retry_round = max(self._latest_retry_round, attempt_index)
        self._emit_status_locked()
        self._submit(self._run_retry_attempt, state.source, attempt_index)

    def _schedule_semantic_locked(
        self,
        state: _WordRecoveryState,
        candidate: str,
        validation: TranslationValidationResult,
    ) -> None:
        if not self._enable_semantic or state.accepted:
            return
        candidate_key = _candidate_validation_text(candidate)
        if candidate_key in state.seen_semantic_candidates:
            return
        if not _semantic_candidate_is_eligible(
            state.source,
            candidate,
            target_lang=self._target_lang,
            source_lang=self._source_lang,
            validation=validation,
        ):
            return
        state.seen_semantic_candidates.add(candidate_key)
        state.semantic_inflight += 1
        self._emit_status_locked()
        self._submit(self._run_semantic_check, state.source, candidate, validation)

    def _submit(self, fn, *args) -> None:
        future = self._executor.submit(fn, *args)
        self._futures.add(future)
        future.add_done_callback(self._future_done)

    def _future_done(self, future) -> None:
        try:
            future.result()
        except ApiKeyTemporarilyUnavailableError as exc:
            with self._condition:
                self._fatal_error = exc
                self._condition.notify_all()
        except Exception as exc:  # noqa: BLE001 - recovery must degrade to review
            if self._log_callback:
                self._log_callback("WARN", f"Word 恢复池任务失败：{exc}")
        finally:
            with self._condition:
                self._futures.discard(future)
                self._condition.notify_all()

    def _run_retry_attempt(self, source: str, attempt_index: int) -> None:
        self._log_source_locations(
            "INFO",
            source,
            f"正在单段重试（第 {attempt_index}/{self._max_attempts} 轮）",
        )
        retry_stats = WordBatchRunStats()
        retry_translations = translate_word_texts(
            [source],
            self._engine,
            self._target_lang,
            self._retry_prompt,
            self._retry_batch_settings,
            concurrency=1,
            progress_callback=None,
            error_callback=(
                (lambda msg: self._log_callback("WARN", msg))
                if self._log_callback
                else None
            ),
            should_stop=self._should_stop,
            source_lang=self._source_lang,
            stats=retry_stats,
            quality_profile=None,
            api_scheduler=self._api_scheduler,
            request_category=API_REQUEST_CATEGORY_RECOVERY,
        )
        candidate = retry_translations.get(source, "")
        self._handle_retry_result(source, candidate, attempt_index)

    def _handle_retry_result(self, source: str, candidate: str, attempt_index: int) -> None:
        evaluation = _evaluate_word_translation(
            source,
            candidate,
            source_lang=self._source_lang,
            target_lang=self._target_lang,
            allow_recovery=True,
        )
        with self._condition:
            state = self._states.get(source)
            if state is None:
                return
            state.retry_inflight = False
            state.attempts_done = max(state.attempts_done, attempt_index)
            if state.accepted:
                self._emit_status_locked()
                self._condition.notify_all()
                return
            if evaluation.accepted:
                accepted_by = (
                    "word_recovery"
                    if evaluation.validation.needs_review
                    else "strict_retry"
                )
                self._accept_locked(state, candidate, accepted_by, evaluation.validation)
                self._log_source_locations("OK", source, "单段重试恢复")
                return
            state.last_validation = evaluation.validation
            if attempt_index >= self._max_attempts:
                self._log_source_locations("WARN", source, "单段重试未恢复")
            else:
                self._log_source_locations(
                    "WARN",
                    source,
                    f"单段重试未恢复，将继续重试（已完成 {attempt_index}/{self._max_attempts} 轮）",
                )
            self._schedule_semantic_locked(state, candidate, evaluation.validation)
            self._schedule_retry_locked(state)
            self._emit_status_locked()
            self._condition.notify_all()

    def _run_semantic_check(
        self,
        source: str,
        candidate: str,
        validation: TranslationValidationResult,
    ) -> None:
        self._log_source_locations("INFO", source, "正在语义仲裁")
        result = _run_semantic_arbitration(
            self._engine,
            source,
            candidate,
            target_lang=self._target_lang,
            source_lang=self._source_lang,
            api_scheduler=self._api_scheduler,
            error_callback=(
                (lambda msg: self._log_callback("WARN", msg))
                if self._log_callback
                else None
            ),
        )
        with self._condition:
            state = self._states.get(source)
            self._semantic_check_count += 1
            if state is None:
                return
            state.semantic_inflight = max(0, state.semantic_inflight - 1)
            self._semantic_checked_sources.add(source)
            if not state.accepted and result.equivalent:
                self._semantic_uncertain_sources.discard(source)
                self._semantic_accepted_sources.add(source)
                self._accept_locked(
                    state,
                    candidate,
                    "semantic",
                    _semantic_review_validation(validation, result),
                )
                self._log_source_locations("OK", source, "语义仲裁接受")
            elif not state.accepted and not result.equivalent:
                self._semantic_uncertain_sources.add(source)
                self._log_source_locations("WARN", source, f"语义仲裁未接受（{result.verdict}）")
            self._emit_status_locked()
            self._condition.notify_all()


def _candidate_validation_text(candidate: str | None) -> str:
    value = str(candidate or "").strip()
    if is_replace_translation(value):
        return extract_replace_translation(value).strip()
    return value


def _engine_supports_chat(engine) -> bool:
    chat = getattr(type(engine), "chat", None)
    return chat is not None and chat is not TranslationEngine.chat


def _semantic_candidate_is_eligible(
    source: str,
    candidate: str | None,
    *,
    target_lang: str,
    source_lang: str,
    validation: TranslationValidationResult,
) -> bool:
    candidate_text = _candidate_validation_text(candidate)
    source_text = str(source or "").strip()
    if not source_text or not candidate_text:
        return False
    if source_text.casefold() == candidate_text.casefold():
        return False

    hard_codes = {
        "empty_translation",
        "same_as_source",
        "source_non_chinese_only",
        "missing_target_chinese",
    }
    if any(issue.code in hard_codes for issue in validation.issues):
        return False

    source_len = len(re.sub(r"\s+", "", source_text))
    candidate_len = len(re.sub(r"\s+", "", candidate_text))
    if source_len >= 40 and candidate_len < max(8, int(source_len * _SEMANTIC_MIN_LENGTH_RATIO)):
        return False

    if source_lang == "zh" and target_lang not in {"zh", "ja"}:
        cjk_count = len(_CJK_RE.findall(candidate_text))
        if cjk_count:
            candidate_non_space_len = max(len(re.sub(r"\s+", "", candidate_text)), 1)
            if (
                cjk_count >= _SEMANTIC_RESIDUAL_CJK_COUNT_BLOCK
                or (cjk_count / candidate_non_space_len) > _SEMANTIC_RESIDUAL_CJK_RATIO_BLOCK
            ):
                return False

    return True


def _run_semantic_arbitration(
    engine,
    source: str,
    candidate: str,
    *,
    target_lang: str,
    source_lang: str,
    api_scheduler: WeightedApiScheduler | None,
    error_callback: Callable[[str], None] | None = None,
) -> _SemanticArbitrationResult:
    candidate_text = _candidate_validation_text(candidate)
    if not candidate_text:
        return _SemanticArbitrationResult(_SEMANTIC_VERDICT_UNCERTAIN, "候选译文为空")

    system_prompt = _build_semantic_arbitration_prompt()
    user_payload = json.dumps(
        {
            "source_language": source_lang,
            "target_language": target_lang,
            "source_text": source,
            "candidate_translation": candidate_text,
        },
        ensure_ascii=False,
    )
    weight = estimate_api_request_weight([source, candidate_text], system_prompt)

    request_generation: int | None = None
    try:
        if api_scheduler is None:
            raw = engine.chat(system_prompt, user_payload)
        else:
            with api_scheduler.slot(weight, category=API_REQUEST_CATEGORY_RECOVERY) as lease:
                request_generation = lease.generation
                raw = engine.chat(system_prompt, user_payload)
        payload = json.loads(strip_markdown_json(raw))
    except Exception as exc:  # noqa: BLE001 - uncertain keeps original review path
        if isinstance(exc, ApiKeyTemporarilyUnavailableError):
            raise
        if api_scheduler is not None and not is_local_engine_name(engine.engine_name):
            decision = handle_api_concurrency_limit(
                exc,
                scheduler=api_scheduler,
                request_generation=request_generation,
                context_label="Word 语义仲裁",
                error_callback=error_callback,
            )
            if decision is not None:
                return _run_semantic_arbitration(
                    engine,
                    source,
                    candidate,
                    target_lang=target_lang,
                    source_lang=source_lang,
                    api_scheduler=api_scheduler,
                    error_callback=error_callback,
                )
        return _SemanticArbitrationResult(_SEMANTIC_VERDICT_UNCERTAIN, str(exc))

    if not isinstance(payload, dict):
        return _SemanticArbitrationResult(_SEMANTIC_VERDICT_UNCERTAIN, "仲裁结果不是 JSON 对象")

    verdict = str(payload.get("verdict") or "").strip().lower()
    if verdict not in {
        _SEMANTIC_VERDICT_EQUIVALENT,
        _SEMANTIC_VERDICT_NOT_EQUIVALENT,
        _SEMANTIC_VERDICT_UNCERTAIN,
    }:
        verdict = _SEMANTIC_VERDICT_UNCERTAIN
    reason = str(payload.get("reason") or "").strip()
    return _SemanticArbitrationResult(verdict, reason)


def _build_semantic_arbitration_prompt() -> str:
    return (
        "你是一个严谨的合同与工程文本翻译质量仲裁器。\n"
        "任务：只判断候选译文是否完整、准确传达源文的全部实质信息。\n"
        "判定规则：\n"
        "1. 日期、金额、单位、编号、公司名或专有名词可以用目标语言习惯表达，只要事实等价即可。\n"
        "2. 如果遗漏主体、义务、条件、范围、日期、金额、比例、处罚或关键限制，必须判定为 not_equivalent。\n"
        "3. 如果候选译文只是摘要、只翻译局部、照抄原文、包含明显大量未翻译源语言内容，必须判定为 not_equivalent。\n"
        "4. 无法确定时判定为 uncertain。\n"
        "只输出一个 JSON 对象，不要输出 markdown 或解释文字。格式："
        '{"verdict":"equivalent|not_equivalent|uncertain","reason":"简短原因"}'
    )


def _semantic_review_validation(
    validation: TranslationValidationResult,
    arbitration: _SemanticArbitrationResult,
) -> TranslationValidationResult:
    issues = list(validation.issues)
    issues.append(
        TranslationValidationIssue(
            code="semantic_equivalence",
            message=(
                "程序化规则校验未通过，但语义仲裁判定候选译文与原文完整等义。"
                + (f"原因：{arbitration.reason}" if arbitration.reason else "")
            ),
            fragments=validation.review_fragments,
        )
    )
    return TranslationValidationResult(
        status=VALIDATION_STATUS_SOFT_PASS_REVIEW,
        issues=tuple(issues),
    )


def _run_word_strict_retries(
    *,
    retry_sources: list[str],
    api_translations: dict[str, str],
    engine,
    target_lang: str,
    retry_prompt: str,
    retry_batch_settings,
    retry_attempts: int,
    source_lang: str,
    should_stop,
    log_callback: Callable[[str, str], None] | None = None,
) -> tuple[list[str], list[str], dict[str, TranslationValidationResult]]:
    """Retry unresolved Word paragraphs as single-item requests."""
    try:
        max_attempts = max(1, int(retry_attempts))
    except (TypeError, ValueError):
        max_attempts = 1

    pending_sources = list(retry_sources)
    recovery_review_results: dict[str, TranslationValidationResult] = {}
    for attempt_index in range(1, max_attempts + 1):
        if not pending_sources or (should_stop and should_stop()):
            break

        if log_callback:
            log_callback(
                "INFO",
                (
                    f"Word 单段严格重试第 {attempt_index}/{max_attempts} 轮："
                    f"{len(pending_sources)} 条"
                ),
            )

        retry_stats = WordBatchRunStats()
        retry_translations = translate_word_texts(
            pending_sources,
            engine,
            target_lang,
            retry_prompt,
            retry_batch_settings,
            concurrency=1,
            progress_callback=None,
            error_callback=(lambda msg: log_callback("WARN", msg)) if log_callback else None,
            should_stop=should_stop,
            source_lang=source_lang,
            stats=retry_stats,
            quality_profile=None,
        )
        api_translations.update(retry_translations)

        next_pending_sources: list[str] = []
        for source in pending_sources:
            evaluation = _evaluate_word_translation(
                source,
                api_translations.get(source),
                source_lang=source_lang,
                target_lang=target_lang,
                allow_recovery=True,
            )
            if evaluation.accepted:
                if evaluation.validation.needs_review:
                    recovery_review_results[source] = evaluation.validation
                continue
            next_pending_sources.append(source)

        fixed_count = len(pending_sources) - len(next_pending_sources)
        if fixed_count and log_callback:
            log_callback(
                "OK",
                (
                    f"Word 单段严格重试第 {attempt_index}/{max_attempts} 轮"
                    f"恢复 {fixed_count} 条"
                ),
            )
        if next_pending_sources and attempt_index < max_attempts and log_callback:
            log_callback("WARN", f"仍有 {len(next_pending_sources)} 条未恢复，将继续重试。")

        pending_sources = next_pending_sources

    for source in pending_sources:
        api_translations[source] = source

    unresolved_source_set = set(pending_sources)
    retry_fixed_sources = [
        source
        for source in retry_sources
        if source not in unresolved_source_set
    ]
    return retry_fixed_sources, pending_sources, recovery_review_results


def _build_word_batch_prompt(system_prompt: str) -> str:
    return (
        f"{system_prompt}\n\n"
        "Word 文档段落翻译规则：\n"
        "1. 当前输入来自 Word 正文段落或表格单元格，通常比 Excel 单元格更长。\n"
        "2. 必须完整翻译每个数组项，不能跳过、合并、摘要或只翻译前半段。\n"
        "3. 输出数组长度必须与输入完全一致，并按原顺序一一对应。\n"
        "4. 必须完整保留原文中的数字、负号、小数、单位、钢筋规格、强度等级和轴线编号。"
    ).strip()


def _build_word_retry_prompt(system_prompt: str) -> str:
    return (
        f"{system_prompt}\n\n"
        "Word 文档段落重试规则：\n"
        "1. 当前输入是 Word 正文中的完整段落，不是 Excel 短单元格。\n"
        "2. 只要原文含中文，就必须返回目标语言译文，不能返回空字符串，也不能返回原文。\n"
        "3. 必须完整保留原文中的所有数字、负号、小数、单位、钢筋规格、强度等级和轴线编号。\n"
        "4. 不要省略任何参数；若句子很长，也要完整翻译整段。"
    ).strip()


def _remember_segment_locations(
    segment_locations: dict[str, list[dict]],
    file_name: str,
    segments,
) -> None:
    for segment in segments:
        segment_locations.setdefault(segment.source, []).append(
            {
                "file": file_name,
                "kind": segment.kind,
                "location": segment.location,
                "location_label": _format_location_label(segment.location),
                "section_path": segment.section_path or "正文",
                "snippet": _build_source_excerpt(segment.source),
            }
        )


def _add_quality_issues(
    issues: list[dict],
    segment_locations: dict[str, list[dict]],
    sources: list[str],
    *,
    problem: str,
    status: str,
    severity: str,
    validation_results: dict[str, TranslationValidationResult] | None = None,
) -> None:
    seen_keys = {
        (
            issue.get("file"),
            issue.get("location"),
            issue.get("problem"),
            issue.get("severity"),
        )
        for issue in issues
    }
    for source in sources:
        locations = segment_locations.get(source) or [
            {
                "file": "",
                "kind": "",
                "location": "",
                "location_label": "未知位置",
                "section_path": "正文",
                "snippet": _build_source_excerpt(source),
            }
        ]
        for location in locations:
            validation_result = (validation_results or {}).get(source)
            issue = {
                **location,
                "problem": problem,
                "status": status,
                "severity": severity,
            }
            if validation_result and validation_result.review_fragments:
                issue["review_fragments"] = list(validation_result.review_fragments)
            key = (
                issue.get("file"),
                issue.get("location"),
                issue.get("problem"),
                issue.get("severity"),
            )
            if key in seen_keys:
                continue
            seen_keys.add(key)
            issues.append(issue)


def _build_source_excerpt(text: str, *, head: int = 18, tail: int = 16) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= head + tail + 3:
        return normalized
    return f"{normalized[:head]}……{normalized[-tail:]}"


def _format_location_label(location: str) -> str:
    paragraph_match = re.match(r"body\.paragraph\[(\d+)\]", str(location or ""))
    if paragraph_match:
        return f"正文段落 {int(paragraph_match.group(1)) + 1}"

    cell_match = re.match(r"table\[(\d+)\]\.cell\[(\d+)\]", str(location or ""))
    if cell_match:
        return f"表格 {int(cell_match.group(1)) + 1} / 单元格 {int(cell_match.group(2)) + 1}"

    return location or "未知位置"


def _write_word_quality_report(
    *,
    output_dir: Path,
    file_results: list[dict],
    issues: list[dict],
    elapsed_sec: float,
    tm_hit_count: int,
    api_call_count: int,
) -> Path | None:
    try:
        report_path = output_dir / "word_translation_report.md"
        successful = sum(1 for item in file_results if item.get("success"))
        failed = len(file_results) - successful
        resolved_count = sum(1 for issue in issues if issue.get("severity") == "resolved")
        review_count = len(issues) - resolved_count

        lines = [
            "# Word 翻译质量报告",
            "",
            "## 任务概览",
            "",
            f"- 文件数：{len(file_results)}",
            f"- 成功文件：{successful}",
            f"- 失败文件：{failed}",
            f"- 耗时：{elapsed_sec:.2f} 秒",
            f"- TM 命中：{tm_hit_count}",
            f"- API 翻译：{api_call_count}",
            f"- 已自动处理事项：{resolved_count}",
            f"- 需人工复核事项：{review_count}",
            "",
        ]

        if not issues:
            lines.extend(["## 质量提示", "", "未发现需要提示的问题。", ""])
        else:
            lines.extend(["## 需复核内容", ""])
            for idx, issue in enumerate(issues, 1):
                label = "已自动处理" if issue.get("severity") == "resolved" else "需人工复核"
                lines.extend(
                    [
                        f"### {idx}. {label}",
                        "",
                        f"- 文件：{issue.get('file') or '未知文件'}",
                        f"- 章节路径：{issue.get('section_path') or '正文'}",
                        f"- 位置：{issue.get('location_label') or '未知位置'}",
                        f"- 段落：{issue.get('snippet') or ''}",
                        f"- 问题：{issue.get('problem') or ''}",
                        f"- 处理结果：{issue.get('status') or ''}",
                    ]
                )
                fragments = issue.get("review_fragments") or []
                if fragments:
                    lines.append(f"- 问题片段：{'、'.join(str(item) for item in fragments)}")
                lines.append("")

        report_path.write_text("\n".join(lines), encoding="utf-8")
        return report_path
    except Exception as exc:  # noqa: BLE001 - report generation must not fail the translation task
        logger.warning(f"Word 翻译质量报告写入失败：{exc}")
        return None
