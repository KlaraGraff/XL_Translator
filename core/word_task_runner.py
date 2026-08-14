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
from core.coverage_arbitration import (
    RETRANSLATE_UNCERTAIN,
    apply_arbitration,
    collect_arbitration_candidates,
    review_coverage_pairs,
)
from core.word_coverage import (
    apply_coverage_review_marks,
    build_word_coverage_plan,
    write_untranslated_docx,
)
from core.engine_dispatcher import (
    build_engine,
    get_system_prompt,
    is_local_engine_name,
)
from core.language_registry import (
    build_lang_pair,
    get_default_source_lang,
    get_tm_language_pairs,
    is_auto_source_lang,
)
from core.language_preflight import (
    LANGUAGE_PREFLIGHT_SYSTEM_PROMPT,
    TranslationLanguageResult,
    build_language_preflight_prompt,
    preflight_files,
)
from core.mixed_language import (
    DEFAULT_MIXED_MAX_BATCH_CHARS,
    MIXED_ACTION_EXISTING_BILINGUAL,
    MIXED_ACTION_FOREIGN_NOISE,
    MIXED_ACTION_TRANSLATE,
    MIXED_ACTION_UNCERTAIN,
    MIXED_MARK_FOREIGN_NOISE,
    MIXED_MARK_UNRESOLVED,
    MixedLanguageResult,
    MixedLanguageRunStats,
    split_mixed_language_sources,
    translate_mixed_language_texts,
)
from core.model_roles import ROLE_TRANSLATION, resolve_effective_model_config
from core.model_throughput import get_model_throughput
from core.residual_classifier import (
    CATEGORY_TERM_FRAGMENT,
    check_heading_consistency,
    is_section_heading_source,
    summarize_residuals,
)
from core.residual_pipeline import run_residual_pass
from core.residual_repair import (
    DEFAULT_REPAIR_BREAKER_THRESHOLD,
    DEFAULT_REPAIR_MAX_UNITS,
    REPAIR_METHOD_LABELS,
    build_feedback_note,
    run_repair_ladder,
)
from core.task_logger import TaskLogger
from core.tm_hygiene import sanitize_tm_pairs, tm_hygiene_log_lines
from core.task_runner import (
    DoneMsg,
    ErrorMsg,
    LogMsg,
    ProgressMsg,
    StatusMsg,
    StoppedMsg,
    TaskStopped,
    WordRecoveryStatusMsg,
    user_facing_reason,
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
    WordConversionResult,
    convert_numbering_to_text_with_native_apps,
    convert_doc_to_docx,
    is_legacy_word_doc,
)
from core.word_document import (
    WordFileItem,
    WordFrontMatterBoundary,
    build_word_output_dir,
    count_text_bearing_header_footer_parts,
    detect_hidden_word_content,
    extract_word_header_footer_segments,
    extract_word_segments,
    find_word_front_matter_boundary_for_path,
    normalize_docx_automatic_numbering,
    write_bilingual_docx,
)
from core.word_batching import (
    WordBatchRunStats,
    estimate_api_request_weight,
    translate_word_texts,
)
from engines.base_engine import engine_supports_chat, strip_markdown_json
from settings import AppSettings, provider_key_overrides

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_SEMANTIC_VERDICT_EQUIVALENT = "equivalent"
_SEMANTIC_VERDICT_NOT_EQUIVALENT = "not_equivalent"
_SEMANTIC_VERDICT_UNCERTAIN = "uncertain"
_WORD_RECOVERY_NORMAL_SOFT_RATIO = 0.8
_SEMANTIC_MIN_LENGTH_RATIO = 0.18
_SEMANTIC_RESIDUAL_CJK_RATIO_BLOCK = 0.12
_SEMANTIC_RESIDUAL_CJK_COUNT_BLOCK = 12
# Word 输出文档里的底色只留给真正需要人手处理的两类：译文没出来（残留原文、残留中文）
# 和原文本身可疑。程序自己已经判过并放行的——严格重试恢复、语义仲裁认定等义——一律
# 不上底色，只在质量报告里留记录。满篇底色等于没有底色：用户会挨个点开发现全是"已处理"，
# 下一次就整片跳过，真正的问题跟着一起被跳过。
# 这张表只在一件事上用得着：同一条原文被判进两类时留哪一类（见 _set_review_mark）。
# 全集就是这两个——「语义校验接受」那一类在 9.3.1 整类删除了，它是"已经没事了"的
# 记录，不该占用文档上的底色。
_WORD_REVIEW_MARK_PRIORITY = {
    MIXED_MARK_UNRESOLVED: 1,
    MIXED_MARK_FOREIGN_NOISE: 2,
}
_POST_WRITE_COVERAGE_ISSUE_LIMIT = 50
# 「保护封面和目录」上报到前端的标题文字截断长度——正常标题（如「第一章 工程概况」）
# 远短于这个长度，只有正文标题识别跑偏、把整段正文当标题时才会触顶，截断避免把大段
# 文字塞进任务日志/结果面板。
_FRONT_MATTER_HEADING_DISPLAY_LIMIT = 60

# 残留修复阶梯的护栏值与方法中文名只在 core/residual_repair 维护一份（Excel 共用）
_RESIDUAL_REPAIR_MAX_UNITS = DEFAULT_REPAIR_MAX_UNITS
_RESIDUAL_REPAIR_BREAKER_THRESHOLD = DEFAULT_REPAIR_BREAKER_THRESHOLD
_RESIDUAL_REPAIR_METHOD_LABELS = REPAIR_METHOD_LABELS


def _truncate_front_matter_heading(text: str, limit: int = _FRONT_MATTER_HEADING_DISPLAY_LIMIT) -> str:
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1] + "…"


def _front_matter_report(front_matter: WordFrontMatterBoundary, *, requested: bool) -> dict:
    """把 WordFrontMatterBoundary 转成可以原样塞进 file_results / DoneMsg.files 的展示结构。

    requested 单独保留，是因为前端要能区分"这个文件没开保护"和"开了保护但没找到正文
    标题"——两者都表现为 protected_paragraph_count == 0，但对用户的含义完全不同。
    """
    return {
        "requested": bool(requested),
        "found": bool(front_matter.found),
        "protected_paragraph_count": front_matter.protected_paragraph_count,
        "heading_text": _truncate_front_matter_heading(front_matter.heading_text),
    }


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


@dataclass(frozen=True)
class _PreparedWordSource:
    path: Path
    method: str
    temp_paths: tuple[Path, ...] = ()
    fallback_messages: tuple[str, ...] = ()
    labels_seen: int = 0
    labels_prepended: int = 0
    conversion_method: str = "not_required"
    conversion_fidelity: str = "not_required"
    numbering_method: str = "python_conservative"
    numbering_fallback_messages: tuple[str, ...] = ()


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


def _set_review_mark(review_marks: dict[str, str], source: str, mark: str) -> None:
    cleaned = str(source or "").strip()
    if not cleaned:
        return
    existing = review_marks.get(cleaned)
    if existing is None or _WORD_REVIEW_MARK_PRIORITY.get(mark, 0) > _WORD_REVIEW_MARK_PRIORITY.get(existing, 0):
        review_marks[cleaned] = mark


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
        untranslated_only: bool = False,
        protect_front_matter: bool = False,
        translate_headers_footers: bool = False,
        allow_doc_fallback: bool = False,
    ):
        self._files = file_items
        self._settings = settings
        self._source_root = Path(source_root) if source_root else None
        self._source_lang = str(source_lang or settings.source_lang or "zh").strip() or "zh"
        self._key_overrides = dict(key_overrides or {})
        self._api_scheduler_override = api_scheduler
        self._untranslated_only = bool(untranslated_only)
        self._protect_front_matter = bool(protect_front_matter)
        self._translate_headers_footers = bool(translate_headers_footers)
        self._allow_doc_fallback = bool(allow_doc_fallback)
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

    def _log_front_matter_boundary(
        self, file_name: str, front_matter: WordFrontMatterBoundary
    ) -> None:
        """按文件汇报「保护封面和目录」的判定结果——找不到正文标题绝不能默默生效。"""
        if not self._protect_front_matter:
            return
        if front_matter.found:
            self._log(
                "INFO",
                (
                    f"  → [前置内容保护] {file_name}：跳过开头 "
                    f"{front_matter.protected_paragraph_count} 段，正文从"
                    f"「{_truncate_front_matter_heading(front_matter.heading_text)}」开始。"
                ),
            )
        else:
            self._log(
                "WARNING",
                (
                    f"  → [前置内容保护] {file_name}：未能识别出正文标题，"
                    "前置内容保护未生效，本文件将按未保护方式正常处理。"
                ),
            )

    def _arbitrate_coverage_pairs(
        self,
        coverage_plan,
        *,
        engine,
        api_scheduler,
        target_lang: str,
        source_lang: str,
        lang_pair: str | None,
        concurrency: int,
        file_name: str,
        file_identity: str,
        quality_issues: list[dict],
    ) -> None:
        """复核「这一段中文的下一段真的是它的译文吗」，判错的打回去重新翻译。

        补译模式靠启发式判断某段中文后面那段是不是译文。判成"已有译文"而其实不是，
        那段中文就永远留在文档里，体检也发现不了（用的是同一套启发式）；判反了顶多
        多插一条译文，看得见。所以这里宁可多翻，不可漏翻。
        """
        candidates = collect_arbitration_candidates(coverage_plan.units)
        if not candidates:
            return

        known_translations: dict[str, str] = {}
        if lang_pair:
            try:
                tm_result = tm_manager.lookup_batch(
                    [unit.source_text.strip() for unit in candidates], lang_pair
                )
                known_translations = {
                    text: str(value)
                    for text, value in (tm_result or {}).items()
                    if value
                }
            except Exception as exc:  # noqa: BLE001 - 记忆库只是省一次模型调用
                logger.debug(f"补译复核读取记忆库失败：{exc!r}")

        arbitrate = None
        if engine_supports_chat(engine):
            def arbitrate(source: str, candidate: str) -> str:
                if self._stop_event.is_set():
                    return _SEMANTIC_VERDICT_EQUIVALENT
                return _run_semantic_arbitration(
                    engine,
                    source,
                    candidate,
                    target_lang=target_lang,
                    source_lang=source_lang,
                    api_scheduler=api_scheduler,
                    error_callback=lambda message: self._log("WARNING", message),
                ).verdict

        try:
            outcome = review_coverage_pairs(
                coverage_plan.units,
                known_translations=known_translations,
                arbitrate=arbitrate,
                max_workers=max(1, int(concurrency or 4)),
                notify_model_checks=lambda count: self._log(
                    "INFO",
                    f"  → 补译复核：{count} 对疑似配对送模型判定，请稍候。",
                ),
            )
        except Exception as exc:  # noqa: BLE001
            # 复核只是给启发式加的一道保险。它自己出错（限流、网络、模型异常）不能连累
            # 整个文件——外层的 per-file except 会把这个文件当成"打不开"，直接不出译文，
            # 那比不复核严重得多。出错就退回原判：启发式说已覆盖就已覆盖。
            self._log(
                "WARNING",
                f"  → {file_name}：补译复核未能完成（{exc}），本文件按原判处理。",
            )
            return
        flipped = apply_arbitration(outcome)
        uncertain = sum(
            1
            for review in outcome.retranslated
            if review.reason == RETRANSLATE_UNCERTAIN
        )
        # 把"模型说不是译文"和"没问出结果"分开报：后者成批出现时说明接口在抖，
        # 不是文档里真有那么多配错的段落。
        detail = f"{len(flipped)} 对改为重新翻译"
        if uncertain:
            detail += f"（其中 {uncertain} 对因未取得判定结果而从严处理）"
        # 有候选就报一句，哪怕结论是"全都没问题"。只在有异常时才吭声的检查，用户
        # 无从分辨它是查过了没事，还是压根没跑。
        self._log(
            "INFO",
            (
                f"  → 补译复核：{len(candidates)} 对已有译文，"
                f"其中 {outcome.model_check_count} 对送模型判定，{detail}。"
            ),
        )
        if outcome.skipped_over_cap:
            self._log(
                "WARNING",
                (
                    f"  → 补译复核：可疑对超过单文件上限，"
                    f"{outcome.skipped_over_cap} 对未送模型，按原判保留为已有译文。"
                ),
            )
        for unit in flipped:
            quality_issues.append(
                {
                    "file": file_identity,
                    "kind": unit.kind,
                    "location": unit.location,
                    "location_label": _format_location_label(unit.location),
                    "section_path": unit.section_path or "正文",
                    "snippet": _build_source_excerpt(unit.source_text),
                    "problem": "紧邻段落不是这一段的译文",
                    "status": "复核判定后已改为补译，原有内容保持不变。",
                    "severity": "resolved",
                }
            )
        if flipped:
            self._log(
                "INFO",
                f"  → {file_name}：{len(flipped)} 段原判「已有译文」经复核改为补译。",
            )

    def _run_with_overrides(self) -> None:
        with provider_key_overrides(self._key_overrides):
            self._run()

    def _run(self) -> None:
        start_ts = datetime.now()
        settings = self._settings
        source_lang = self._source_lang
        auto_source_lang = is_auto_source_lang(source_lang)
        target_lang = settings.target_lang
        lang_pair = (
            None
            if auto_source_lang
            else build_lang_pair(target_lang, source_lang=source_lang)
        )
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
                source_lang=source_lang if not auto_source_lang else get_default_source_lang(),
                page_key="word",
            )
            model_config = resolve_effective_model_config(settings, ROLE_TRANSLATION)
            concurrency = get_model_throughput(settings, model_config).concurrency
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
            logger.debug(f"Word 引擎初始化失败原始错误：{exc!r}")
            self._queue.put(
                ErrorMsg(
                    message="引擎初始化失败："
                    + user_facing_reason(
                        exc,
                        fallback="请在设置里检查翻译模型这条连接。",
                    )
                )
            )
            return

        if not self._files:
            self._queue.put(ErrorMsg(message="未选择可翻译的 Word 文件。"))
            return

        root_for_output = self._source_root if self._source_root else self._files[0].path.parent
        word_output = settings.word_output
        custom_output_dir = (
            word_output.custom_output_dir if word_output.use_custom_output_dir else None
        )
        try:
            if word_output.use_custom_output_dir:
                output_error = get_custom_output_dir_error(custom_output_dir)
                if output_error is not None:
                    raise ValueError(output_error)
            output_dir = build_word_output_dir(root_for_output, custom_output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            logger.debug(f"Word 输出目录初始化失败原始错误：{exc!r}")
            self._queue.put(
                ErrorMsg(
                    message="输出目录初始化失败："
                    + user_facing_reason(
                        exc,
                        fallback="请换一个有写入权限的输出目录后重试。",
                    )
                )
            )
            return

        def _raise_if_stopped(message: str = "任务已停止") -> None:
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
        if self._untranslated_only:
            self._log("INFO", "[补译模式] 只补未译内容：已有译文位置将保持不变。")
        if self._protect_front_matter:
            self._log(
                "INFO",
                "[前置内容保护] 已启用：每个文件从开头到第一个正文标题之前的内容"
                "（封面、目录、前言等）默认不翻译，找不到正文标题的文件不受影响。",
            )

        phase_total = 3
        file_texts: list[set[str]] = []
        file_language_preflights = {}
        tm_language_pairs: list[str] = []
        text_source_candidates: dict[str, set[str]] = {}
        coverage_plans: list = []
        global_unique_texts: set[str] = set()
        segment_locations: dict[str, list[dict]] = {}
        quality_issues: list[dict] = []
        unresolved_review_sources: set[str] = set()
        recovery_review_sources: set[str] = set()
        review_marks: dict[str, str] = {}
        process_paths: list[Path] = []
        converted_temp_paths: list[Path] = []
        preprocess_summaries: list[dict] = []
        front_matter_summaries: list[dict] = []
        word_batch_stats = WordBatchRunStats()
        model_source_results: dict[str, list[TranslationLanguageResult]] = {}
        model_source_results_lock = threading.Lock()
        recovery_pool: _WordRecoveryPool | None = None
        recovery_outcome = _WordRecoveryOutcome(
            fixed_sources=[],
            unresolved_sources=[],
            accepted_translations={},
            recovery_review_results={},
            semantic_review_results={},
            unresolved_validation_results={},
        )

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
                    self._queue.put(
                        StatusMsg(
                            phase_desc=(
                                f"状态：[阶段 1/{phase_total}] 正在预处理 Word 文档："
                                f"{file_item.name}"
                            )
                        )
                    )
                    prepared = _prepare_word_source_for_translation(
                        file_item.path,
                        use_native_preprocessing=(
                            settings.word_conversion.use_native_preprocessing
                        ),
                        allow_doc_fallback=self._allow_doc_fallback,
                    )
                    process_path = prepared.path
                    converted_temp_paths.extend(prepared.temp_paths)
                    for fallback_message in prepared.fallback_messages:
                        self._log("INFO", f"{file_item.name}：{fallback_message}，已继续尝试下一处理方式。")
                    self._log(
                        "INFO",
                        (
                            f"Word 预处理完成 {file_item.name}，"
                            f"使用 {prepared.method}，"
                            f"自动编号 {prepared.labels_seen} 段，"
                            f"物化 {prepared.labels_prepended} 段，"
                            f"耗时 {(datetime.now() - t0).total_seconds():.2f}s"
                        ),
                    )
                    process_paths.append(process_path)
                    hidden_content = detect_hidden_word_content(process_path)
                    if hidden_content.found:
                        # 这部分内容 python-docx 根本看不见，本次一定漏译。做不到翻译，
                        # 至少不能让它静悄悄地漏掉。
                        self._log(
                            "WARN",
                            (
                                f"  → [未翻译内容] {file_item.name}：检测到"
                                f"{hidden_content.describe()}，"
                                "这些内容不在本次翻译范围内，输出文档中会保持原文。"
                                "建议在 Word 里接受全部修订、或把内容控件转换为普通文本后重试。"
                            ),
                        )
                        quality_issues.append(
                            {
                                "file": _file_result_identity(file_item, self._source_root),
                                "kind": "hidden_content",
                                "location": "document",
                                "location_label": "整篇文档",
                                "section_path": "正文",
                                "snippet": "",
                                "problem": f"存在无法读取的内容（{hidden_content.describe()}）",
                                "status": (
                                    "这些内容被内容控件或未接受的修订包裹，未参与翻译，"
                                    "输出文档中保持原文。"
                                ),
                                "severity": "needs_review",
                            }
                        )
                    preprocess_summaries.append(
                        {
                            "hidden_content": hidden_content.as_dict(),
                            "method": prepared.method,
                            "labels_seen": prepared.labels_seen,
                            "labels_prepended": prepared.labels_prepended,
                            "conversion_method": prepared.conversion_method,
                            "conversion_fidelity": prepared.conversion_fidelity,
                            "conversion_fallback_messages": list(
                                prepared.fallback_messages
                            ),
                            "numbering_method": prepared.numbering_method,
                            "numbering_fallback_messages": list(
                                prepared.numbering_fallback_messages
                            ),
                        }
                    )
                    if self._untranslated_only:
                        coverage_plan = build_word_coverage_plan(
                            process_path,
                            target_lang=target_lang,
                            source_lang=(
                                source_lang
                                if not auto_source_lang
                                else get_default_source_lang()
                            ),
                            protect_front_matter=self._protect_front_matter,
                        )
                        self._arbitrate_coverage_pairs(
                            coverage_plan,
                            engine=engine,
                            api_scheduler=api_scheduler,
                            target_lang=target_lang,
                            source_lang=(
                                source_lang
                                if not auto_source_lang
                                else get_default_source_lang()
                            ),
                            lang_pair=lang_pair,
                            concurrency=concurrency,
                            file_name=file_item.name,
                            file_identity=_file_result_identity(
                                file_item, self._source_root
                            ),
                            quality_issues=quality_issues,
                        )
                        coverage_plans.append(coverage_plan)
                        _remember_coverage_unit_locations(
                            segment_locations,
                            _file_result_identity(file_item, self._source_root),
                            coverage_plan.source_units,
                        )
                        text_set = set(coverage_plan.source_texts)
                        # 覆盖率计划只看正文和表格，页眉页脚要单独取一次才能一起补译。
                        if self._translate_headers_footers:
                            header_segments = extract_word_header_footer_segments(
                                process_path,
                                target_lang=target_lang,
                                source_lang=(
                                    source_lang
                                    if not auto_source_lang
                                    else get_default_source_lang()
                                ),
                            )
                            _remember_segment_locations(
                                segment_locations,
                                _file_result_identity(file_item, self._source_root),
                                header_segments,
                            )
                            text_set.update(segment.source for segment in header_segments)
                        summary = coverage_plan.summary
                        self._log(
                            "INFO",
                            (
                                "  → 补译识别："
                                f"待补 {summary.get('source_only', 0)}，"
                                f"已覆盖 {summary.get('covered', 0)}，"
                                f"不确定跳过 {summary.get('ambiguous', 0)}"
                            ),
                        )
                        front_matter_summaries.append(
                            _front_matter_report(
                                coverage_plan.front_matter,
                                requested=self._protect_front_matter,
                            )
                        )
                        self._log_front_matter_boundary(file_item.name, coverage_plan.front_matter)
                    else:
                        segments = extract_word_segments(
                            process_path,
                            target_lang=target_lang,
                            source_lang=(
                                source_lang
                                if not auto_source_lang
                                else get_default_source_lang()
                            ),
                            protect_front_matter=self._protect_front_matter,
                            include_headers_footers=self._translate_headers_footers,
                        )
                        coverage_plans.append(None)
                        _remember_segment_locations(
                            segment_locations,
                            _file_result_identity(file_item, self._source_root),
                            segments,
                        )
                        text_set = {segment.source for segment in segments}
                        # extract_word_segments 只返回抽取好的词条，不携带边界信息；
                        # 全文翻译模式下要单独查一次正文边界才能把保护结果上报给前端，
                        # 这与补译模式下 coverage_plan.front_matter 已经"顺手"带出来不同。
                        front_matter = (
                            find_word_front_matter_boundary_for_path(process_path)
                            if self._protect_front_matter
                            else WordFrontMatterBoundary(body_start_index=None)
                        )
                        front_matter_summaries.append(
                            _front_matter_report(
                                front_matter,
                                requested=self._protect_front_matter,
                            )
                        )
                        self._log_front_matter_boundary(file_item.name, front_matter)
                    file_texts.append(text_set)
                    global_unique_texts.update(text_set)
                    elapsed = (datetime.now() - t0).total_seconds()
                    self._log("INFO", f"  → {file_item.name}：{len(text_set)} 处待翻译段落位置（{elapsed:.3f}s）")
                    self._task_logger.file_collected(file_item.name, len(text_set), elapsed)
                except Exception as exc:
                    if len(process_paths) < index + 1:
                        process_paths.append(file_item.path)
                    if len(preprocess_summaries) < index + 1:
                        preprocess_summaries.append({})
                    if len(coverage_plans) < index + 1:
                        coverage_plans.append(None)
                    if len(front_matter_summaries) < index + 1:
                        front_matter_summaries.append({})
                    file_texts.append(set())
                    logger.debug(f"Word 文件读取失败 {file_item.name} 原始错误：{exc!r}")
                    read_reason = user_facing_reason(
                        exc,
                        fallback="这个文件打不开，可能已损坏或设置了打开密码。",
                    )
                    self._log("ERROR", f"Word 文件读取失败 {file_item.name}: {read_reason}")
                    self._task_logger.file_error(
                        file_item.name,
                        f"Word 文件读取失败: {read_reason}",
                    )
                    file_results.append(
                        {
                            "name": file_item.name,
                            "source_path": str(file_item.path),
                            "success": False,
                            "error": f"Word 文件读取失败: {read_reason}",
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
            self._log("OK", f"[阶段 1 完成] 全部文档合计 {len(global_unique_texts)} 处待翻译文本，相同内容只翻一次（{phase1_elapsed:.2f}s）")
            self._task_logger.global_collected(
                total_unique=len(global_unique_texts),
                file_count=len(self._files),
                elapsed=phase1_elapsed,
            )

            if auto_source_lang:
                self._queue.put(
                    StatusMsg(
                        phase_desc="状态：自动识别模式，正在对每个有候选文本的 Word 文件执行一次语言预检..."
                    )
                )

                detector_calls = {"count": 0}

                def _detect_file_language(samples, detected_target):
                    detector_calls["count"] += 1
                    return engine.chat(
                        LANGUAGE_PREFLIGHT_SYSTEM_PROMPT,
                        build_language_preflight_prompt(
                            samples,
                            target_lang=detected_target,
                        ),
                    )

                file_payloads = {
                    str(file_item.path): texts
                    for file_item, texts in zip(self._files, file_texts)
                }
                file_language_preflights = preflight_files(
                    file_payloads,
                    _detect_file_language,
                    target_lang=target_lang,
                )
                detected_sources: list[str] = []
                for result in file_language_preflights.values():
                    for detected in result.source_langs:
                        if detected not in detected_sources:
                            detected_sources.append(detected)
                if detected_sources:
                    source_lang = detected_sources[0]
                    system_prompt = get_system_prompt(
                        settings,
                        target_lang=target_lang,
                        source_lang=source_lang,
                        page_key="word",
                    )
                else:
                    source_lang = get_default_source_lang()
                tm_language_pairs = get_tm_language_pairs(detected_sources, target_lang)
                for file_item, text_set in zip(self._files, file_texts):
                    result = file_language_preflights.get(str(file_item.path))
                    if result is None:
                        continue
                    for text in text_set:
                        text_source_candidates.setdefault(text, set()).update(result.source_langs)
                self._log(
                    "INFO",
                    (
                        f"自动语言预检完成：{detector_calls['count']} 次请求，"
                        f"实际源语言={','.join(detected_sources) or '未确定'}，"
                        f"TM 语言对={','.join(tm_language_pairs) or '无'}"
                    ),
                )
            else:
                tm_language_pairs = [lang_pair] if lang_pair else []

            _raise_if_stopped()

            self._queue.put(StatusMsg(phase_desc=f"状态：[阶段 2/{phase_total}] 正在比对翻译记忆库..."))
            t_phase2 = datetime.now()
            all_texts = list(global_unique_texts)
            normal_texts, mixed_texts = split_mixed_language_sources(
                all_texts,
                target_lang=target_lang,
                source_lang=source_lang,
            )
            if mixed_texts:
                self._log(
                    "INFO",
                    f"混合语言路径命中 {len(mixed_texts)} 处内容，已从记忆库查询中分流。",
                )
            # TM lookups are scoped per file preflight, like the Excel runner:
            # querying the global pool against every detected pair lets a text
            # from a zh file hit an entry stored under another source language
            # (the same string can mean different things per language).
            normal_text_set = set(normal_texts)
            text_tm_pairs: dict[str, set[str]] = {}
            if auto_source_lang:
                for file_item, text_set in zip(self._files, file_texts):
                    result = file_language_preflights.get(str(file_item.path))
                    allowed_pairs = (
                        set(result.tm_lang_pairs(target_lang)) if result is not None else set()
                    )
                    for text in text_set:
                        if text in normal_text_set and allowed_pairs:
                            text_tm_pairs.setdefault(text, set()).update(allowed_pairs)
            elif lang_pair:
                text_tm_pairs = {text: {lang_pair} for text in normal_texts}

            tm_values_by_text: dict[str, list[str]] = {text: [] for text in normal_texts}
            tm_texts_by_pair: dict[str, list[str]] = {}
            for text, pairs in text_tm_pairs.items():
                for pair in pairs:
                    tm_texts_by_pair.setdefault(pair, []).append(text)
            for pair, pair_texts in tm_texts_by_pair.items():
                tm_result = tm_manager.lookup_batch(pair_texts, pair)
                for text, value in tm_result.items():
                    if value is not None and str(value) not in tm_values_by_text.setdefault(text, []):
                        tm_values_by_text[text].append(str(value))
            hits = {
                text: values[0]
                for text, values in tm_values_by_text.items()
                if len(values) == 1
            }
            misses = [text for text in normal_texts if text not in hits]
            tm_hit_count = len(hits)
            api_call_count = len(misses) + len(mixed_texts)
            self._log(
                "INFO",
                f"[阶段 2] TM 命中：{tm_hit_count}  普通待 API：{len(misses)}  混合语言：{len(mixed_texts)}",
            )
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
            if (misses or mixed_texts) and not self._stop_event.is_set():
                self._queue.put(StatusMsg(phase_desc=f"状态：[阶段 2/{phase_total}] 正在请求大模型翻译未命中词汇..."))

                def progress_cb(done, total):
                    self._queue.put(
                        ProgressMsg(
                            phase_index=2,
                            phase_total=phase_total,
                            phase_name="云端翻译",
                            step_done=done,
                            step_total=max(api_call_count, 1),
                        )
                    )

                t0 = datetime.now()
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
                    defer_until_started=True,
                )
                main_drain_gate = _MainTranslationDrainGate(
                    queue_count=int(bool(misses)) + int(bool(mixed_texts)),
                    on_all_drained=recovery_pool.start,
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

                def model_source_result_cb(
                    source: str,
                    result: TranslationLanguageResult,
                ) -> None:
                    if not auto_source_lang:
                        return
                    with model_source_results_lock:
                        model_source_results.setdefault(source, []).append(result)

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
                normal_api_translations: dict[str, str] = {}
                mixed_results: dict[str, MixedLanguageResult] = {}
                mixed_stats = MixedLanguageRunStats()

                def run_normal_main_translation() -> dict[str, str]:
                    if not misses:
                        return {}
                    return translate_word_texts(
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
                        api_scheduler=api_scheduler,
                        request_category=API_REQUEST_CATEGORY_NORMAL,
                        candidate_callback=recovery_candidate_cb,
                        report_source_languages=auto_source_lang,
                        source_result_callback=model_source_result_cb,
                        drained_callback=main_drain_gate.queue_drained,
                    )

                def run_mixed_main_translation() -> dict[str, MixedLanguageResult]:
                    if not mixed_texts:
                        return {}
                    self._queue.put(
                        StatusMsg(phase_desc=f"状态：[阶段 2/{phase_total}] 正在处理混合语言内容...")
                    )

                    def mixed_progress_cb(done, total):
                        self._queue.put(
                            ProgressMsg(
                                phase_index=2,
                                phase_total=phase_total,
                                phase_name="云端翻译",
                                step_done=min(len(misses) + done, max(api_call_count, 1)),
                                step_total=max(api_call_count, 1),
                            )
                        )

                    return translate_mixed_language_texts(
                        mixed_texts,
                        engine=engine,
                        target_lang=target_lang,
                        system_prompt=system_prompt,
                        source_lang=source_lang,
                        concurrency=concurrency,
                        max_items_per_batch=settings.word_batch.max_paragraphs_per_batch,
                        max_chars_per_batch=max(
                            DEFAULT_MIXED_MAX_BATCH_CHARS,
                            settings.word_batch.max_chars_per_batch,
                        ),
                        retry_attempts=settings.word_batch.strict_retry_attempts,
                        progress_callback=mixed_progress_cb,
                        error_callback=lambda msg: self._log("WARN", msg),
                        should_stop=self.stop_requested,
                        api_scheduler=api_scheduler,
                        request_category=API_REQUEST_CATEGORY_NORMAL,
                        stats=mixed_stats,
                        drained_callback=main_drain_gate.queue_drained,
                    )

                if misses and mixed_texts and api_scheduler is not None:
                    with ThreadPoolExecutor(max_workers=2) as main_executor:
                        normal_future = main_executor.submit(run_normal_main_translation)
                        mixed_future = main_executor.submit(run_mixed_main_translation)
                        normal_api_translations = normal_future.result()
                        mixed_results = mixed_future.result()
                else:
                    normal_api_translations = run_normal_main_translation()
                    mixed_results = run_mixed_main_translation()

                api_translations.update(normal_api_translations)
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
                _raise_if_stopped("任务已停止，未写入剩余 Word 翻译结果。")

                if mixed_texts:
                    _apply_mixed_language_word_results(
                        mixed_results=mixed_results,
                        translations=api_translations,
                        quality_issues=quality_issues,
                        segment_locations=segment_locations,
                        review_marks=review_marks,
                    )
                    self._log(
                        "INFO",
                        (
                            "混合语言路径："
                            f"命中 {mixed_stats.input_count} 条，"
                            f"已双语 {mixed_stats.action_counts.get(MIXED_ACTION_EXISTING_BILINGUAL, 0)}，"
                            f"疑似原文错误 {mixed_stats.action_counts.get(MIXED_ACTION_FOREIGN_NOISE, 0)}，"
                            f"不确定 {mixed_stats.action_counts.get(MIXED_ACTION_UNCERTAIN, 0)}，"
                            f"语义校验接受 {mixed_stats.semantic_accepted_count}"
                        ),
                    )

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
                for source in retry_sources:
                    recovery_pool.add_candidate(source, api_translations.get(source, ""))
                recovery_outcome = recovery_pool.wait_for_completion()
                _raise_if_stopped("任务已停止，未写入剩余 Word 翻译结果。")
                api_translations.update(recovery_outcome.accepted_translations)
                for source in recovery_outcome.unresolved_sources:
                    api_translations[source] = source
                # 这两类都通过了复核：恢复规则认可、或语义仲裁判定与原文等义。文档里
                # 不再上底色——底色只留给真正要人工看的东西，见 _WORD_REVIEW_MARK_PRIORITY
                # 上方的说明。
                # 但它们仍然不写记忆库：接受的是"这一段可以用"，不是"这条译文可以复用"。
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
                            "译文主体已通过恢复校验，本段已写入译文，"
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
                            "本段已写入译文，且不会写入翻译记忆库。"
                        ),
                        severity="resolved",
                        validation_results=recovery_outcome.semantic_review_results,
                    )
                if recovery_outcome.unresolved_sources:
                    unresolved_review_sources.update(recovery_outcome.unresolved_sources)
                    for source in recovery_outcome.unresolved_sources:
                        _set_review_mark(review_marks, source, MIXED_MARK_UNRESOLVED)
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

                elapsed = (datetime.now() - t0).total_seconds()
                self._log("OK", f"API 翻译完成，返回 {len(api_translations)} 条（{elapsed:.2f}s）")
                self._task_logger.global_api_done(returned=len(api_translations), elapsed=elapsed)
                # TM 写入挪到了残留修复之后（见下）：在这里写会把「修复前」的
                # 带残留译文存进库，下个文档 TM 命中直接短路，残留就固化了。

            global_translations = {**api_translations, **hits}

            # ── 残留中文体检 + 确定性序号修复（0 API，与 Excel 共用分类器）──
            # 放在写盘前对最终词典做：能确定修的（序号前缀）直接改词典；
            # 改不了的走修复阶梯；仍修不上的标记待复核并逐位置写进报告。
            residual_result = run_residual_pass(
                global_translations.items(), target_lang=target_lang
            )
            if residual_result.fixes:
                global_translations.update(residual_result.fixes)
                self._log(
                    "OK",
                    (
                        f"残留序号确定性修复 {len(residual_result.fixes)} 条"
                        f"（序号惯例：{residual_result.convention}）"
                    ),
                )
                _add_quality_issues(
                    quality_issues,
                    segment_locations,
                    list(residual_result.fixes.keys()),
                    problem="译文残留中文序号",
                    status="已按文档序号惯例自动修复，无需人工处理。",
                    severity="resolved",
                )

            # 修复阶梯：外科修补 → 带反馈重译（验收不过绝不覆盖原译文）。
            # 上限/熔断/进度护栏在 run_repair_ladder 里，与 Excel 主流程共用。
            still_needs_review = list(residual_result.needs_review)
            repair_method_counts: dict[str, int] = {}
            repair_accepted_sources: list[str] = []
            # 拒收理由跟着源文走：残留告警里「为什么没修上」必须逐条可查
            repair_reject_reasons: dict[str, tuple[str, ...]] = {}
            # 超限/熔断要写进结果报告：只在日志里提会被滚走，用户就不知道
            # 「这批单元根本没尝试修复，重跑还有救」
            repair_over_cap_count = 0
            repair_breaker_tripped = False
            if still_needs_review and engine_supports_chat(engine):

                def _on_repair_progress(done: int, total: int) -> None:
                    self._queue.put(StatusMsg(
                        phase_desc=(
                            f"状态：[阶段 2/{phase_total}] 正在修复残留中文"
                            f"（{done}/{total}）..."
                        )
                    ))

                ladder = run_repair_ladder(
                    still_needs_review,
                    target_lang=target_lang,
                    send=engine.chat,
                    convention=residual_result.convention,
                    max_units=_RESIDUAL_REPAIR_MAX_UNITS,
                    breaker_threshold=_RESIDUAL_REPAIR_BREAKER_THRESHOLD,
                    should_stop=self.stop_requested,
                    on_progress=_on_repair_progress,
                )
                if ladder.over_cap_count:
                    self._log(
                        "WARN",
                        (
                            f"残留待修单元 {len(still_needs_review)} 条超出单次上限 "
                            f"{_RESIDUAL_REPAIR_MAX_UNITS}，超出的 "
                            f"{ladder.over_cap_count} 条本次不发请求，直接列入待复核。"
                        ),
                    )
                if ladder.breaker_tripped:
                    self._log(
                        "WARN",
                        (
                            f"残留修复通道连续 {_RESIDUAL_REPAIR_BREAKER_THRESHOLD} 次"
                            "请求失败，已停止后续修复请求，剩余单元直接列入待复核。"
                        ),
                    )
                global_translations.update(ladder.accepted)
                repair_accepted_sources = list(ladder.accepted.keys())
                repair_method_counts = ladder.method_counts
                repair_reject_reasons = ladder.reject_reasons
                repair_over_cap_count = ladder.over_cap_count
                repair_breaker_tripped = ladder.breaker_tripped
                still_needs_review = list(ladder.remaining)
            repaired_total = sum(repair_method_counts.values())
            if repaired_total:
                method_desc = "、".join(
                    f"{_RESIDUAL_REPAIR_METHOD_LABELS.get(method, method)} {count} 条"
                    for method, count in sorted(repair_method_counts.items())
                )
                self._log(
                    "OK", f"残留中文修复阶梯通过 {repaired_total} 条（{method_desc}）"
                )
                _add_quality_issues(
                    quality_issues,
                    segment_locations,
                    repair_accepted_sources,
                    problem="译文残留未翻译的中文片段",
                    status="残留中文已自动修复，修复稿通过机器验收。",
                    severity="resolved",
                )
            if still_needs_review:
                for unit in still_needs_review:
                    _set_review_mark(review_marks, unit.source_text, MIXED_MARK_UNRESOLVED)
                    spans_desc = "、".join(f"«{span}»" for span in unit.spans)
                    reject_desc = (
                        "；自动修复尝试被拒："
                        + "；".join(repair_reject_reasons[unit.source_text])
                        if unit.source_text in repair_reject_reasons
                        else ""
                    )
                    _add_quality_issues(
                        quality_issues,
                        segment_locations,
                        [unit.source_text],
                        problem="译文残留未翻译的中文片段",
                        status=f"残留：{spans_desc}{reject_desc}，需人工复核。",
                        severity="needs_review",
                    )
                if repair_over_cap_count:
                    quality_issues.append(
                        {
                            "file": "",
                            "kind": "document",
                            "location": "residual.repair_cap",
                            "location_label": "整批任务",
                            "section_path": "正文",
                            "snippet": "",
                            "problem": "部分残留中文本次未尝试自动修复",
                            "status": (
                                f"待修单元超出单次自动修复上限"
                                f"（{_RESIDUAL_REPAIR_MAX_UNITS} 条），其中 "
                                f"{repair_over_cap_count} 条本次未尝试修复，"
                                "重新运行任务可继续修复。"
                            ),
                            "severity": "needs_review",
                        }
                    )
                if repair_breaker_tripped:
                    quality_issues.append(
                        {
                            "file": "",
                            "kind": "document",
                            "location": "residual.repair_breaker",
                            "location_label": "整批任务",
                            "section_path": "正文",
                            "snippet": "",
                            "problem": "自动修复通道因连续请求失败已熔断",
                            "status": (
                                "部分单元未尝试修复，确认网络与引擎可用后"
                                "重新运行任务可继续修复。"
                            ),
                            "severity": "needs_review",
                        }
                    )
                self._log(
                    "WARN",
                    f"检出 {len(still_needs_review)} 条译文残留中文，已列入待复核。",
                )
            if residual_result.released_notes:
                # 万/亿等数量单位残留：不拦发布，但报告里留痕
                _add_quality_issues(
                    quality_issues,
                    segment_locations,
                    [unit.source_text for unit in residual_result.released_notes],
                    problem="译文保留了「万/亿」等数量单位",
                    status="通常不影响理解，如需统一可人工调整。",
                    severity="resolved",
                )

            # ── 文档级标题写法巡检（0 API）：逐段各自都对，全篇混两套写法
            # 只有聚在一起才看得出来。多数派可确定性改写时直接改词典。
            heading_observations = [
                (source, target, source)
                for source, target in global_translations.items()
                if is_section_heading_source(source)
            ]
            # 全篇多数派要贯通到 TM 入库口：TM 只拿到 API 未命中子集，
            # 让它自己投票可能与全篇结论相反，把库改成与交付文件相左的写法
            heading_majority: str | None = None
            if heading_observations:
                heading_consistency = check_heading_consistency(
                    heading_observations, target_lang=target_lang
                )
                heading_majority = heading_consistency.majority_form
                if heading_consistency.fixes:
                    global_translations.update(heading_consistency.fixes)
                    self._log(
                        "OK",
                        (
                            f"节标题写法归一 {len(heading_consistency.fixes)} 条"
                            "（全篇多数派为准）。"
                        ),
                    )
                    _add_quality_issues(
                        quality_issues,
                        segment_locations,
                        list(heading_consistency.fixes.keys()),
                        problem="节标题写法与全篇多数派不一致",
                        status="已按全篇多数派自动归一，无需人工处理。",
                        severity="resolved",
                    )
                elif heading_consistency.outliers:
                    # 多数派是序数词写法时归一需要词形知识，只报告不改写
                    _add_quality_issues(
                        quality_issues,
                        segment_locations,
                        [item.unit_key for item in heading_consistency.outliers],
                        problem="节标题写法与全篇多数派不一致",
                        status="因归一需要词形变化知识未自动改写，如需统一可人工调整。",
                        severity="resolved",
                    )

            # ── TM 写入（从 API 返回后挪到这里，与 Excel 同一条规矩）：必须
            # 存「文件最终译文」——序号修复、修复阶梯、标题归一都改完之后的
            # 版本；仍带残留的配对由 sanitize_tm_pairs 在入库口拦下。停止信号
            # 不拦这一步：API 结果已付费拿到，写库是本地操作，跳过只会让
            # 下次运行整批重新付费。
            if api_translations:

                def _final_tm_text(source: str, fallback: str) -> str:
                    value = global_translations.get(source)
                    if isinstance(value, str) and value.strip():
                        return value
                    return fallback

                # 写库失败不许弄死任务（Excel store_api_results_in_tm 的另一半
                # 规矩）：此刻 API 已全部付费返回、双语文件还没写，词库写不进去
                # （典型：词库页长写入持锁）只降级为 WARN + 报告条目，阶段 3
                # 照常写文件。
                written = 0
                try:
                    if auto_source_lang:
                        candidate_entries: list[tuple[str, str, str]] = []
                        for source, translated in api_translations.items():
                            final_text = _final_tm_text(source, translated)
                            candidates = text_source_candidates.get(source, set())
                            reported_codes = {
                                result.source_lang
                                for result in model_source_results.get(source, [])
                                if result.tm_eligible
                            }
                            if (
                                len(candidates) != 1
                                or len(reported_codes) != 1
                                or next(iter(reported_codes)) not in candidates
                                or source in mixed_texts
                                or source in recovery_review_sources
                                or source in unresolved_review_sources
                                or not should_store_translation_in_tm(source, final_text)
                            ):
                                continue
                            pair = build_lang_pair(
                                target_lang,
                                source_lang=next(iter(reported_codes)),
                            )
                            candidate_entries.append((pair, source, final_text))
                        if candidate_entries:
                            hygiene = sanitize_tm_pairs(
                                [(source, text) for _pair, source, text in candidate_entries],
                                target_lang=target_lang,
                                convention=residual_result.convention,
                                heading_majority=heading_majority,
                            )
                            for level, message in tm_hygiene_log_lines(hygiene):
                                self._log(level, message)
                            normalized_targets = dict(hygiene.pairs)
                            rejected_sources = {source for source, _reason in hygiene.rejected}
                            pairs_to_insert: dict[str, list[tuple[str, str]]] = {}
                            for pair, source, text in candidate_entries:
                                if source in rejected_sources:
                                    continue
                                pairs_to_insert.setdefault(pair, []).append(
                                    (source, normalized_targets.get(source, text))
                                )
                            written = sum(
                                tm_manager.insert_batch(
                                    entries,
                                    pair,
                                    max_len,
                                    engine.engine_name,
                                    sync_reverse=False,
                                )
                                for pair, entries in pairs_to_insert.items()
                            )
                    else:
                        new_pairs = [
                            (source, _final_tm_text(source, translated))
                            for source, translated in api_translations.items()
                            if (
                                source not in mixed_texts
                                and source not in recovery_review_sources
                                and source not in unresolved_review_sources
                                and should_store_translation_in_tm(
                                    source, _final_tm_text(source, translated)
                                )
                            )
                        ]
                        hygiene = sanitize_tm_pairs(
                            new_pairs,
                            target_lang=target_lang,
                            convention=residual_result.convention,
                            heading_majority=heading_majority,
                        )
                        for level, message in tm_hygiene_log_lines(hygiene):
                            self._log(level, message)
                        written = tm_manager.insert_batch(
                            list(hygiene.pairs),
                            lang_pair,
                            max_len,
                            engine.engine_name,
                            sync_reverse=False,
                        )
                except Exception as tm_error:  # noqa: BLE001 - never lose finished work
                    tm_write_message = (
                        "翻译记忆库写入失败，本次译文不受影响，"
                        f"但这批词条没有存入词库：{tm_error}"
                    )
                    self._log("WARN", tm_write_message)
                    quality_issues.append(
                        {
                            "file": "",
                            "kind": "document",
                            "location": "tm.write_failed",
                            "location_label": "整批任务",
                            "section_path": "正文",
                            "snippet": "",
                            "problem": "翻译记忆库写入失败",
                            "status": tm_write_message
                            + " 下次翻译相同内容会重新调用 API，可稍后重跑补存。",
                            "severity": "needs_review",
                        }
                    )
                if written:
                    self._log("INFO", f"新增 TM 词条：{written} 条")
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
                    result.get("source_path") == str(file_item.path)
                    and not result.get("success")
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
                    if self._untranslated_only:
                        coverage_plan = coverage_plans[index] if index < len(coverage_plans) else None
                        if coverage_plan is None:
                            raise ValueError("缺少补译识别计划，无法安全按位置写入。")
                        out_path = write_untranslated_docx(
                            source_path=source_path,
                            output_dir=output_dir / rel_subdir,
                            plan=coverage_plan,
                            translations=global_translations,
                            target_lang=target_lang,
                            source_lang=source_lang,
                            output_name=_word_output_source_name(file_item.path),
                            review_marks=(
                                review_marks
                                if settings.word_review.highlight_unresolved
                                else None
                            ),
                            review_mark_colors=settings.word_review.mark_colors,
                            existing_highlight_policy=settings.word_review.existing_highlight_policy,
                            log_callback=lambda msg: self._log(
                                "OK" if msg.startswith("[OK]") else "INFO",
                                msg,
                            ),
                        )
                    else:
                        out_path = write_bilingual_docx(
                            source_path=source_path,
                            output_dir=output_dir / rel_subdir,
                            translations=global_translations,
                            target_lang=target_lang,
                            source_lang=source_lang,
                            output_name=_word_output_source_name(file_item.path),
                            review_marks=(
                                review_marks
                                if settings.word_review.highlight_unresolved
                                else None
                            ),
                            review_mark_colors=settings.word_review.mark_colors,
                            existing_highlight_policy=settings.word_review.existing_highlight_policy,
                            log_callback=lambda msg: self._log(
                                "OK" if msg.startswith("[OK]") else "INFO",
                                msg,
                            ),
                            issue_callback=lambda info: quality_issues.append(
                                _word_cell_line_mismatch_issue(
                                    file_name=_file_result_identity(
                                        file_item, source_root
                                    ),
                                    info=info,
                                )
                            ),
                            protect_front_matter=self._protect_front_matter,
                            translate_headers_footers=self._translate_headers_footers,
                        )
                    residual_count = _append_post_write_coverage_issues(
                        issues=quality_issues,
                        file_name=_file_result_identity(file_item, source_root),
                        output_path=out_path,
                        target_lang=target_lang,
                        source_lang=source_lang,
                        protect_front_matter=self._protect_front_matter,
                        review_mark_colors=settings.word_review.mark_colors,
                        existing_highlight_policy=(
                            settings.word_review.existing_highlight_policy
                            if settings.word_review.highlight_unresolved
                            else None
                        ),
                        mark_log_callback=lambda count, name=file_item.name: self._log(
                            "INFO",
                            f"{name}：已在输出文档标记 {count} 处需复核位置。",
                        ),
                        # 写盘前残留巡检已逐位置报过的段落（带修复拒收理由），
                        # 成品体检不再重复报，防止同一处残留数成两条待办
                        pre_reported_residual_sources={
                            unit.source_text.strip() for unit in still_needs_review
                        },
                    )
                    if residual_count:
                        self._log(
                            "WARN",
                            (
                                f"{file_item.name}：输出文档仍发现 {residual_count} "
                                "处疑似未翻译源文，已写入质量报告。"
                            ),
                        )
                    elapsed = (datetime.now() - t0).total_seconds()
                    this_file_texts = file_texts[index]
                    this_tm = sum(1 for text in this_file_texts if text in hits)
                    this_api = sum(1 for text in this_file_texts if text in misses or text in mixed_texts)
                    self._task_logger.file_done(
                        filename=file_item.name,
                        elapsed=elapsed,
                        tm_hits=this_tm,
                        api_calls=this_api,
                    )
                    file_results.append(
                        {
                            "name": file_item.name,
                            "source_path": str(file_item.path),
                            "output": str(out_path),
                            "success": True,
                            "preprocess": (
                                preprocess_summaries[index]
                                if index < len(preprocess_summaries)
                                else {}
                            ),
                            "front_matter": (
                                front_matter_summaries[index]
                                if index < len(front_matter_summaries)
                                else {}
                            ),
                            "issues": [
                                issue
                                for issue in quality_issues
                                if issue.get("file")
                                == _file_result_identity(file_item, source_root)
                            ],
                        }
                    )
                    self._log("OK", f"文件完成：{file_item.name}（{elapsed:.2f}s）")
                except Exception as exc:
                    logger.debug(f"Word 文件写入失败 {file_item.name} 原始错误：{exc!r}")
                    write_reason = user_facing_reason(
                        exc,
                        fallback="译文文件没能写出，请检查输出目录的剩余空间和写入权限。",
                    )
                    self._log("ERROR", f"Word 文件写入失败 {file_item.name}：{write_reason}")
                    self._task_logger.file_error(file_item.name, write_reason)
                    file_results.append(
                        {
                            "name": file_item.name,
                            "source_path": str(file_item.path),
                            "success": False,
                            "error": write_reason,
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
        except Exception as exc:  # noqa: BLE001 - 兜底：没有它线程会静默死亡
            # 写回阶段任何未预期的异常（doc.save 权限失败、磁盘满、第三方库崩溃）
            # 原来都会直接冲出工作线程：清理不做、终止消息不发，UI 侧的任务永远停在
            # "运行中"。这里把它降级成一次可见的失败。
            logger.exception("Word 翻译任务异常中止")
            fatal_error_message = "Word 翻译任务异常中止：" + user_facing_reason(
                exc,
                fallback="出现了未预期的问题，已生成的文件仍可使用。",
            )
        finally:
            # 无论走哪条路径，线程池与临时 docx 都必须被回收。
            if recovery_pool is not None:
                try:
                    recovery_pool.shutdown(cancel_futures=True)
                except Exception as exc:  # noqa: BLE001 - 清理失败不得掩盖原始结果
                    logger.warning(f"Word 恢复线程池关停失败：{exc}")
            try:
                _cleanup_converted_word_paths(converted_temp_paths, self._log)
            except Exception as exc:  # noqa: BLE001 - 同上
                logger.warning(f"Word 临时文件清理失败：{exc}")

        # 收尾同样要有兜底：结果契约的组装、报告路径、DoneMsg 构造里任何一处抛出，
        # 都不能让队列收不到终止消息——UI 只认终止消息，收不到就是永久"运行中"。
        terminal_sent = False

        def _emit_terminal(message) -> None:
            nonlocal terminal_sent
            terminal_sent = True
            self._queue.put(message)

        try:
            elapsed_sec = (datetime.now() - start_ts).total_seconds()
            self._task_logger.task_end(elapsed_sec=elapsed_sec, file_results=file_results)
            report_path = None
            report_warning = ""
            if any(item.get("success") for item in file_results):
                try:
                    report_path = _write_word_quality_report(
                        output_dir=output_dir,
                        file_results=file_results,
                        issues=quality_issues,
                        elapsed_sec=elapsed_sec,
                        tm_hit_count=tm_hit_count,
                        api_call_count=api_call_count,
                        translate_headers_footers=self._translate_headers_footers,
                    )
                except Exception as exc:  # report output must not fail a usable task
                    logger.debug(f"Word 翻译质量报告写入失败原始错误：{exc!r}")
                    report_reason = user_facing_reason(
                        exc,
                        fallback="报告文件没能写出。",
                    )
                    self._log("WARN", f"Word 翻译质量报告写入失败：{report_reason}")
                    report_warning = (
                        f"Word 翻译质量报告未能写入：{report_reason}"
                        "已生成的翻译文件仍可使用。"
                    )

            result_contract = self._build_result_contract(
                file_results=file_results,
                output_dir=str(output_dir),
                elapsed_sec=elapsed_sec,
                tm_hit_count=tm_hit_count,
                api_text_count=api_call_count,
                source_lang=source_lang,
                target_lang=target_lang,
                preflights=file_language_preflights,
                file_texts=file_texts,
                quality_issues=quality_issues,
                recovery_outcome=recovery_outcome,
                word_batch_stats=word_batch_stats,
                model_source_results=model_source_results,
                stopped=stopped_message is not None,
                error_message=fatal_error_message or "",
                report_warning=report_warning,
            )

            if stopped_message is not None:
                self._log("WARN", stopped_message)
                _emit_terminal(
                    StoppedMsg(
                        message=stopped_message,
                        output_dir=str(output_dir),
                        report_path=str(report_path) if report_path else "",
                        **result_contract,
                    )
                )
                return

            if fatal_error_message is not None:
                self._log("ERROR", fatal_error_message)
                self._task_logger.error(fatal_error_message)
                _emit_terminal(
                    ErrorMsg(
                        message=fatal_error_message,
                        output_dir=str(output_dir),
                        report_path=str(report_path) if report_path else "",
                        **result_contract,
                    )
                )
                return

            _emit_terminal(
                DoneMsg(
                    output_dir=str(output_dir),
                    file_results=file_results,
                    elapsed_sec=elapsed_sec,
                    tm_hit_count=tm_hit_count,
                    api_call_count=api_call_count,
                    issues=quality_issues,
                    report_path=str(report_path) if report_path else "",
                    **result_contract,
                )
            )
        except Exception as exc:  # noqa: BLE001 - 终止消息必须发出去
            logger.exception("Word 翻译任务收尾失败")
            fatal_error_message = fatal_error_message or (
                "Word 翻译任务收尾失败："
                + user_facing_reason(
                    exc,
                    fallback="结果摘要没能生成，已生成的翻译文件仍可使用。",
                )
            )
        finally:
            if not terminal_sent:
                self._queue.put(
                    ErrorMsg(
                        message=(
                            fatal_error_message
                            or "Word 翻译任务异常中止，未能生成结果摘要。"
                        ),
                        output_dir=str(output_dir),
                    )
                )

    def _build_result_contract(
        self,
        *,
        file_results: list[dict],
        output_dir: str,
        elapsed_sec: float,
        tm_hit_count: int,
        api_text_count: int,
        source_lang: str,
        target_lang: str,
        preflights: dict,
        file_texts: list[set[str]],
        quality_issues: list[dict],
        recovery_outcome: _WordRecoveryOutcome,
        word_batch_stats: WordBatchRunStats,
        model_source_results: dict[str, list[TranslationLanguageResult]],
        stopped: bool,
        error_message: str = "",
        report_warning: str = "",
    ) -> dict[str, object]:
        """Build the W5D terminal result without exposing prompts or raw replies."""
        raw_by_source = {
            str(item.get("source_path") or ""): dict(item)
            for item in file_results
            if isinstance(item, dict)
        }
        files: list[dict[str, object]] = []
        for index, item in enumerate(self._files):
            raw = raw_by_source.get(str(item.path), {})
            success = bool(raw.get("success"))
            preprocess = dict(raw.get("preprocess") or {})
            front_matter = dict(raw.get("front_matter") or {})
            if raw:
                status = "succeeded" if success else "failed"
                readable_error = str(raw.get("error") or "")
            elif stopped:
                status = "unstarted"
                readable_error = "任务在开始该文件前已停止。"
            else:
                status = "unstarted"
                readable_error = "该文件未开始处理。"

            original_format = str(
                getattr(item, "format", "") or item.path.suffix.lstrip(".")
            ).lower()
            conversion_required = original_format == "doc"
            files.append(
                {
                    "name": item.name,
                    "source_path": str(item.path),
                    "source_relative_path": _file_result_identity(item, self._source_root),
                    "format": original_format,
                    "status": status,
                    "success": success,
                    "output": str(raw.get("output") or ""),
                    "conversion": {
                        "required": conversion_required,
                        "path": "temporary_docx" if conversion_required else "not_required",
                        "method": str(
                            preprocess.get("conversion_method")
                            or ("not_started" if conversion_required else "not_required")
                        ),
                        "fidelity": str(
                            preprocess.get("conversion_fidelity")
                            or ("not_started" if conversion_required else "not_required")
                        ),
                        "fallback_messages": list(
                            preprocess.get("conversion_fallback_messages") or []
                        ),
                    },
                    "numbering": {
                        "method": str(preprocess.get("numbering_method") or "not_started"),
                        "fallback_messages": list(
                            preprocess.get("numbering_fallback_messages") or []
                        ),
                        "labels_seen": int(preprocess.get("labels_seen") or 0),
                        "labels_materialized": int(
                            preprocess.get("labels_prepended") or 0
                        ),
                    },
                    "front_matter": {
                        "requested": bool(front_matter.get("requested")),
                        "found": bool(front_matter.get("found")),
                        "protected_paragraph_count": int(
                            front_matter.get("protected_paragraph_count") or 0
                        ),
                        "heading_text": str(front_matter.get("heading_text") or ""),
                    },
                    "review_items": list(raw.get("issues") or []),
                    "error": readable_error,
                }
            )

        # One document position can collect several judgments: the batch runner records
        # "重试后仍未获得有效译文", then the post-write coverage pass records "输出文档仍
        # 存在未译源文" for the same paragraph. Counting judgments told the user to look
        # for five places when the document holds three, so the count is per position
        # (CONTEXT.md 位置计数) — every judgment sentence is kept, joined into the row.
        review_items: list[dict[str, object]] = []
        review_counts: dict[str, int] = {}
        merged_by_position: dict[tuple[str, ...], dict[str, object]] = {}
        for issue in quality_issues:
            if not isinstance(issue, dict):
                continue
            severity = str(issue.get("severity") or "needs_review")
            item = {
                "file": str(issue.get("file") or ""),
                "section_path": str(issue.get("section_path") or "正文"),
                "location": str(issue.get("location_label") or "未知位置"),
                "snippet": str(issue.get("snippet") or ""),
                "problem": str(issue.get("problem") or ""),
                "action": str(issue.get("status") or ""),
                "severity": severity,
            }
            key = (
                str(item["file"]),
                str(item["section_path"]),
                str(item["location"]),
                str(item["snippet"]),
                severity,
            )
            existing = merged_by_position.get(key)
            if existing is None:
                merged_by_position[key] = item
                review_items.append(item)
                review_counts[severity] = review_counts.get(severity, 0) + 1
                continue
            existing["problem"] = _merge_sentences(
                str(existing.get("problem") or ""), str(item["problem"])
            )
            existing["action"] = _merge_sentences(
                str(existing.get("action") or ""), str(item["action"])
            )

        language_files: list[dict[str, object]] = []
        for index, item in enumerate(self._files):
            preflight = preflights.get(str(item.path))
            if preflight is not None:
                preflight_payload = preflight.to_dict(target_lang)
            else:
                preflight_payload = {
                    "source_langs": [source_lang] if self._source_lang != "auto" else [],
                    "requested": False,
                    "request_count": 0,
                }
            actual_counts: dict[str, int] = {}
            for text in file_texts[index] if index < len(file_texts) else ():
                codes = {
                    result.source_lang
                    for result in model_source_results.get(text, [])
                    if result.tm_eligible
                }
                if len(codes) == 1:
                    code = next(iter(codes))
                    actual_counts[code] = actual_counts.get(code, 0) + 1
            language_files.append(
                {
                    "source_path": _file_result_identity(item, self._source_root),
                    "preflight": preflight_payload,
                    "candidate_text_count": len(file_texts[index]) if index < len(file_texts) else 0,
                    "actual_source_counts": actual_counts,
                    "model_source_reports": (
                        "per_item" if self._source_lang == "auto" else "manual_authority"
                    ),
                }
            )

        succeeded = sum(1 for item in files if item["status"] == "succeeded")
        failed = sum(1 for item in files if item["status"] == "failed")
        unstarted = sum(1 for item in files if item["status"] == "unstarted")
        recovered_count = _sources_position_count(
            recovery_outcome.fixed_sources,
            None,
        )
        unresolved_count = _sources_position_count(
            recovery_outcome.unresolved_sources,
            None,
        )
        semantic_accepted_count = _sources_position_count(
            recovery_outcome.semantic_review_results,
            None,
        )
        semantic_checked_count = max(
            semantic_accepted_count,
            int(recovery_outcome.semantic_check_count or 0),
        )
        return {
            "files": files,
            "kpi": {
                "selected_file_count": len(self._files),
                "succeeded_file_count": succeeded,
                "failed_file_count": failed,
                "unstarted_file_count": unstarted,
                "output_dir": output_dir,
                "elapsed_sec": elapsed_sec,
                "tm_hit_count": tm_hit_count,
                "model_translation_text_count": api_text_count,
                "review_text_count": len(review_items),
                "auto_recovered_text_count": recovered_count,
            },
            "recovery": {
                "strict_retry_attempts": self._settings.word_batch.strict_retry_attempts,
                "recovered_count": recovered_count,
                "unresolved_count": unresolved_count,
                "semantic_checked_count": semantic_checked_count,
                "semantic_accepted_count": semantic_accepted_count,
                "semantic_unaccepted_count": max(
                    0, semantic_checked_count - semantic_accepted_count
                ),
                "request_batch_count": word_batch_stats.batch_count,
                "request_unit_count": word_batch_stats.unit_count,
                "split_source_count": word_batch_stats.split_source_count,
                "batch_retry_count": word_batch_stats.retry_count,
            },
            "review": {
                "counts": review_counts,
                "total_count": len(review_items),
                "items": review_items,
            },
            "language": {
                "mode": "automatic" if self._source_lang == "auto" else "manual",
                "selected_source_lang": self._source_lang,
                "target_lang": target_lang,
                "files": language_files,
            },
            "error": {"message": error_message} if error_message else {},
            "report_warning": report_warning,
        }


def _prepare_word_source_for_translation(
    source_path: Path,
    *,
    use_native_preprocessing: bool,
    allow_doc_fallback: bool = False,
) -> _PreparedWordSource:
    temp_paths: list[Path] = []
    fallback_messages: list[str] = []
    process_path = source_path
    conversion_method = "not_required"
    conversion_fidelity = "not_required"
    numbering_fallback_messages: list[str] = []

    if is_legacy_word_doc(source_path):
        conversion = convert_doc_to_docx(
            source_path,
            # Numbering pre-processing is independently configurable.  A
            # legacy .doc must still try Microsoft Word first for fidelity.
            prefer_native_word=True,
            allow_compatibility_fallback=allow_doc_fallback,
        )
        process_path = conversion.path
        conversion_method = conversion.method
        conversion_fidelity = conversion.fidelity
        temp_paths.append(conversion.path)
        fallback_messages.extend(conversion.fallback_messages)

    native_result: WordConversionResult | None = None
    if use_native_preprocessing:
        try:
            native_result = convert_numbering_to_text_with_native_apps(
                process_path,
                prefer_native_word=True,
            )
            process_path = native_result.path
            temp_paths.append(native_result.path)
            fallback_messages.extend(native_result.fallback_messages)
        except WordConversionError as exc:
            logger.debug(f"本地 Office 编号预处理不可用原始错误：{exc!r}")
            message = (
                "本地 Office 编号预处理不可用，已改用内置方式处理编号："
                + user_facing_reason(
                    exc,
                    fallback="本机 Office 这次没能配合，编号已由程序自行还原。",
                )
            )
            fallback_messages.append(message)
            numbering_fallback_messages.append(message)

    normalized = normalize_docx_automatic_numbering(process_path)
    process_path = normalized.path
    temp_paths.append(normalized.path)

    method_parts: list[str] = []
    if conversion_method != "not_required":
        method_parts.append(f".doc 转换：{conversion_method}")
    if native_result is not None:
        method_parts.append(f"编号预处理：{native_result.method}")
        if normalized.stats.labels_seen:
            method_parts.append("Python 残余清理")
    else:
        method_parts.append("编号预处理：Python 兜底")

    return _PreparedWordSource(
        path=process_path,
        method="；".join(method_parts),
        temp_paths=tuple(dict.fromkeys(temp_paths)),
        fallback_messages=tuple(fallback_messages),
        labels_seen=normalized.stats.labels_seen,
        labels_prepended=normalized.stats.labels_prepended,
        conversion_method=conversion_method,
        conversion_fidelity=conversion_fidelity,
        numbering_method=(
            native_result.method if native_result is not None else "python_conservative"
        ),
        numbering_fallback_messages=tuple(numbering_fallback_messages),
    )


def _word_output_source_name(path: Path) -> str:
    return f"{path.stem}.docx" if is_legacy_word_doc(path) else path.name


def _file_result_identity(file_item: WordFileItem, source_root: Path | None) -> str:
    if source_root is not None:
        try:
            return str(file_item.path.relative_to(source_root))
        except ValueError:
            pass
    return str(file_item.path)


def _cleanup_converted_word_paths(
    paths: list[Path],
    log_callback: Callable[[str, str], None],
) -> None:
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except Exception as exc:
            log_callback(
                "WARN",
                f"临时 Word 转换文件清理失败 {path.name}: "
                + user_facing_reason(exc, fallback="临时文件删不掉。"),
            )


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
    # 残留中文按共享分类器分级（与 Excel 同一套标准）：阻断级（日期单位/
    # 整句未译）和须外科修补的短语残留才占重试预算；序号前缀、数量单位
    # 放行——它们由写盘前的确定性序号修复与报告通道兜底（0 API）。
    residual_needs_retry = False
    if source_lang == "zh":
        residual = summarize_residuals(translated_text, target_lang=target_lang)
        residual_needs_retry = residual.blocking or (
            CATEGORY_TERM_FRAGMENT in residual.categories
        )
    if strict_validation.is_pass and not residual_needs_retry:
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
    pending_candidates: list[tuple[str, TranslationValidationResult]] = field(default_factory=list)
    accepted_translation: str = ""
    accepted_by: str = ""
    accepted_validation: TranslationValidationResult = field(default_factory=TranslationValidationResult)
    last_validation: TranslationValidationResult = field(default_factory=TranslationValidationResult)
    # 最近一稿未通过的候选译文：重试时据此提取残留片段做结构化反馈
    last_candidate: str = ""
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


class _MainTranslationDrainGate:
    """Start recovery once every main translation queue has no new batches left."""

    def __init__(self, *, queue_count: int, on_all_drained: Callable[[], None]) -> None:
        self._remaining = max(0, int(queue_count or 0))
        self._on_all_drained = on_all_drained
        self._started = False
        self._lock = threading.Lock()
        if self._remaining <= 0:
            self.queue_drained()

    def queue_drained(self) -> None:
        should_start = False
        with self._lock:
            if self._started:
                return
            self._remaining = max(0, self._remaining - 1)
            if self._remaining <= 0:
                self._started = True
                should_start = True
        if should_start:
            self._on_all_drained()


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
        defer_until_started: bool = False,
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
        self._enable_semantic = enable_semantic and engine_supports_chat(engine)
        self._states: dict[str, _WordRecoveryState] = {}
        self._futures = set()
        self._condition = threading.Condition()
        self._executor = ThreadPoolExecutor(max_workers=max(1, int(concurrency or 1)))
        # ThreadPoolExecutor 的 worker 不是守护线程。没人 shutdown 就等于每次泄漏
        # concurrency 个永久阻塞在队列上的线程，进程也退不干净。
        self._shutdown_lock = threading.Lock()
        self._executor_shutdown = False
        self._semantic_check_count = 0
        self._semantic_checked_sources: set[str] = set()
        self._semantic_accepted_sources: set[str] = set()
        self._semantic_uncertain_sources: set[str] = set()
        self._latest_retry_round = 0
        self._fatal_error: BaseException | None = None
        self._started = not defer_until_started

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
            state.last_candidate = candidate_text
            if self._started:
                self._schedule_semantic_locked(state, candidate_text, evaluation.validation)
                self._schedule_retry_locked(state)
            else:
                state.pending_candidates.append((candidate_text, evaluation.validation))
            self._emit_status_locked()
            self._condition.notify_all()

    def start(self) -> None:
        with self._condition:
            if self._started:
                return
            self._started = True
            stopped = bool(self._should_stop and self._should_stop())
            for state in self._states.values():
                if state.accepted:
                    continue
                pending_candidates = list(state.pending_candidates)
                state.pending_candidates.clear()
                if stopped:
                    state.attempts_done = self._max_attempts
                    continue
                for candidate, validation in pending_candidates:
                    self._schedule_semantic_locked(state, candidate, validation)
                self._schedule_retry_locked(state)
            self._emit_status_locked()
            self._condition.notify_all()

    def shutdown(self, *, cancel_futures: bool = False) -> None:
        """幂等地关停线程池。异常路径下也必须走到这里，否则 worker 线程永久泄漏。"""
        with self._shutdown_lock:
            if self._executor_shutdown:
                return
            self._executor_shutdown = True
            self._executor.shutdown(wait=True, cancel_futures=cancel_futures)

    def __enter__(self) -> _WordRecoveryPool:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.shutdown(cancel_futures=True)

    def wait_for_completion(self) -> _WordRecoveryOutcome:
        try:
            self.start()
            with self._condition:
                while self._fatal_error is None and not self._all_complete_locked():
                    self._condition.wait(timeout=0.1)

                fatal_error = self._fatal_error

            if fatal_error is not None:
                self.shutdown(cancel_futures=True)
                raise fatal_error

            self.shutdown()
            return self._build_outcome()
        finally:
            # start()、等待循环、_build_outcome 里任何一处抛出（含 KeyboardInterrupt）
            # 都不能让线程池活下来。已经关停时这里是空操作。
            self.shutdown(cancel_futures=True)

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
        if not self._started:
            return
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
        if not self._started:
            return
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
                logger.debug(f"Word 恢复池任务失败原始错误：{exc!r}")
                self._log_callback(
                    "WARN",
                    "Word 恢复池任务失败："
                    + user_facing_reason(exc, fallback="这一段的补救没能完成，已转人工复核。"),
                )
        finally:
            with self._condition:
                self._futures.discard(future)
                self._condition.notify_all()

    def _build_attempt_retry_prompt(self, source: str) -> str:
        """上一稿残留中文时，把残留片段作为结构化反馈附进重试 prompt。

        反馈话术与修复阶梯的「带反馈重译」共用一份（build_feedback_note，
        Excel 同源）；其余失败原因（缺数字、返回原文等）沿用静态重试规则。
        """
        with self._condition:
            state = self._states.get(source)
            last_candidate = state.last_candidate if state else ""
        if not last_candidate:
            return self._retry_prompt
        residual = summarize_residuals(last_candidate, target_lang=self._target_lang)
        if not residual.spans:
            return self._retry_prompt
        # 模型整段回吐原文时 spans 会覆盖几乎全文——那不是「残留片段」，逐个
        # 列出只会把 prompt 填满原文噪音；这类失败沿用静态重试规则（规则 2
        # 已写明不能返回原文）。
        span_chars = sum(len(span.text) for span in residual.spans)
        non_space_len = max(len(re.sub(r"\s+", "", last_candidate)), 1)
        if span_chars / non_space_len > 0.5:
            return self._retry_prompt
        note = build_feedback_note([span.text for span in residual.spans])
        return f"{self._retry_prompt}\n5. {note}"

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
            self._build_attempt_retry_prompt(source),
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
            state.last_candidate = candidate
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


def _remember_coverage_unit_locations(
    segment_locations: dict[str, list[dict]],
    file_name: str,
    units,
) -> None:
    for unit in units:
        source = str(unit.source_text or "").strip()
        if not source:
            continue
        segment_locations.setdefault(source, []).append(
            {
                "file": file_name,
                "kind": unit.kind,
                "location": unit.location,
                "location_label": _format_location_label(unit.location),
                "section_path": unit.section_path or "正文",
                "snippet": _build_source_excerpt(source),
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


def _word_cell_line_mismatch_issue(*, file_name: str, info: dict) -> dict:
    """替换式译文行数与单元格源文段数不齐时的报告条目。

    写入器已经做了保底（保留全部原文、译文整体追加），这里负责让这件事在报告里
    被看见——以前这种单元格会被静默清空原文，内容丢了也没人知道。
    """
    location = str(info.get("location") or "output.table")
    return {
        "file": file_name,
        "kind": "table_cell",
        "location": location,
        "location_label": _format_output_location_label(location),
        "section_path": "表格",
        "snippet": _build_source_excerpt(str(info.get("source") or "")),
        "problem": "替换译文与原文行数不一致",
        "status": "已保留原文并在单元格末尾追加整段译文，请人工核对分段。",
        "severity": "needs_review",
    }


def _append_post_write_coverage_issues(
    *,
    issues: list[dict],
    file_name: str,
    output_path: Path,
    target_lang: str,
    source_lang: str,
    protect_front_matter: bool = False,
    review_mark_colors: dict[str, str] | None = None,
    existing_highlight_policy: str | None = None,
    mark_log_callback=None,
    pre_reported_residual_sources: set[str] | None = None,
) -> int:
    """成品文档体检：先按体检结果往文档上涂复核标记，再把同一批位置写进报告。

    顺序不能倒过来。以前这里只写报告，报告里几十条「需人工复核」在文件里一个标记
    都没有——用户勾了「标记需复核内容」，拿到的却是一张要自己去几百页里对照的清单。
    体检本来就只能在成品文档上做（要看的正是写完之后的结果），所以标记也放在这一趟。
    传 existing_highlight_policy=None 表示用户没开标记，只写报告。
    """
    plan = build_word_coverage_plan(
        output_path,
        target_lang=target_lang,
        source_lang=source_lang,
        protect_front_matter=protect_front_matter,
    )
    if existing_highlight_policy is not None:
        marked = apply_coverage_review_marks(
            output_path,
            plan=plan,
            review_mark_colors=review_mark_colors,
            existing_highlight_policy=existing_highlight_policy,
        )
        if marked and mark_log_callback:
            mark_log_callback(marked)
    source_units = plan.source_units
    residual_units = plan.residual_units
    if not source_units and not residual_units:
        return 0

    existing_keys = {
        (
            issue.get("file"),
            issue.get("location"),
            issue.get("problem"),
            issue.get("severity"),
        )
        for issue in issues
    }
    for unit in source_units[:_POST_WRITE_COVERAGE_ISSUE_LIMIT]:
        issue = {
            "file": file_name,
            "kind": unit.kind,
            "location": unit.location,
            "location_label": _format_output_location_label(unit.location),
            "section_path": unit.section_path or "正文",
            "snippet": _build_source_excerpt(unit.source_text),
            "problem": "输出文档仍存在未译源文",
            "status": "该位置未识别到目标语言译文。",
            "severity": "needs_review",
        }
        key = (
            issue.get("file"),
            issue.get("location"),
            issue.get("problem"),
            issue.get("severity"),
        )
        if key in existing_keys:
            continue
        existing_keys.add(key)
        issues.append(issue)

    if len(source_units) > _POST_WRITE_COVERAGE_ISSUE_LIMIT:
        issues.append(
            {
                "file": file_name,
                "kind": "document",
                "location": "output.coverage",
                "location_label": "输出文档",
                "section_path": "正文",
                "snippet": f"共发现 {len(source_units)} 处未译源文，已仅列出前 {_POST_WRITE_COVERAGE_ISSUE_LIMIT} 处。",
                "problem": "输出文档仍存在未译源文",
                "status": "其余未译位置过多，已截断展示。",
                "severity": "needs_review",
            }
        )

    _append_residual_cjk_issues(
        issues=issues,
        existing_keys=existing_keys,
        file_name=file_name,
        residual_units=residual_units,
        pre_reported_residual_sources=pre_reported_residual_sources,
    )

    return len(source_units)


def _append_residual_cjk_issues(
    *,
    issues: list[dict],
    existing_keys: set,
    file_name: str,
    residual_units: list,
    pre_reported_residual_sources: set[str] | None = None,
) -> None:
    """译文整体已翻好、只夹带零星中文时，单独给一条更轻的提示。

    实稿里这些残留常常是章节序号（一、二、三）或单个汉字，不是日期编号——别替用户
    先入为主地断定是哪一种，提示里照实列出残留了什么就够了。

    写盘前的残留巡检已经逐位置报过的段落这里跳过：那条带修复拒收理由、信息更全，
    再报一条坐标和措辞都不同的，位置合并键对不上，同一处残留就会数成两条待办。
    """
    skip_sources = pre_reported_residual_sources or set()
    for unit in residual_units[:_POST_WRITE_COVERAGE_ISSUE_LIMIT]:
        if unit.source_text.strip() in skip_sources:
            continue
        fragments = [str(item) for item in unit.data.get("residual_cjk") or []]
        if not fragments:
            continue
        location = str(unit.data.get("residual_location") or unit.location)
        issue = {
            "file": file_name,
            "kind": unit.kind,
            "location": location,
            "location_label": _format_output_location_label(location),
            "section_path": unit.section_path or "正文",
            "snippet": _build_source_excerpt(
                str(unit.data.get("residual_text") or unit.target_text)
            ),
            "problem": "译文中残留少量中文",
            # 只报实际残留了什么。早先这里固定跟一句"多为日期或编号"，可残留的常常
            # 是章节序号（一、二、三）或单个汉字，那句话等于替用户断言了一个没查过的
            # 原因，会把人往错的方向引。
            "status": f"该位置译文已完成，仅残留：{'、'.join(fragments)}，请确认是否需要改写。",
            "severity": "needs_review",
        }
        key = (
            issue.get("file"),
            issue.get("location"),
            issue.get("problem"),
            issue.get("severity"),
        )
        if key in existing_keys:
            continue
        existing_keys.add(key)
        issues.append(issue)

    if len(residual_units) > _POST_WRITE_COVERAGE_ISSUE_LIMIT:
        issues.append(
            {
                "file": file_name,
                "kind": "document",
                "location": "output.residual",
                "location_label": "输出文档",
                "section_path": "正文",
                "snippet": (
                    f"共发现 {len(residual_units)} 处译文残留中文，"
                    f"已仅列出前 {_POST_WRITE_COVERAGE_ISSUE_LIMIT} 处。"
                ),
                "problem": "译文中残留少量中文",
                "status": "其余位置过多，已截断展示。",
                "severity": "needs_review",
            }
        )


def _apply_mixed_language_word_results(
    *,
    mixed_results: dict[str, MixedLanguageResult],
    translations: dict[str, str],
    quality_issues: list[dict],
    segment_locations: dict[str, list[dict]],
    review_marks: dict[str, str],
) -> None:
    existing_bilingual: list[str] = []
    foreign_noise: list[str] = []
    uncertain: list[str] = []
    semantic_translate: list[str] = []

    for source, result in mixed_results.items():
        if result.action == MIXED_ACTION_EXISTING_BILINGUAL:
            existing_bilingual.append(source)
            continue
        if result.action == MIXED_ACTION_FOREIGN_NOISE:
            if result.translation.strip():
                translations[source] = result.translation.strip()
            foreign_noise.append(source)
            _set_review_mark(review_marks, source, MIXED_MARK_FOREIGN_NOISE)
            continue
        if result.action == MIXED_ACTION_TRANSLATE:
            if result.translation.strip():
                translations[source] = result.translation.strip()
            if result.accepted_by == "semantic":
                # 语义仲裁放行的不上底色，只在报告里留一条记录。
                semantic_translate.append(source)
            continue
        uncertain.append(source)
        translations[source] = source
        _set_review_mark(review_marks, source, MIXED_MARK_UNRESOLVED)

    if existing_bilingual:
        _add_quality_issues(
            quality_issues,
            segment_locations,
            existing_bilingual,
            problem="原文疑似已包含目标语言译文",
            status="已保留原内容，未追加译文，未写入翻译记忆库。",
            severity="resolved",
        )
    if foreign_noise:
        _add_quality_issues(
            quality_issues,
            segment_locations,
            foreign_noise,
            problem="原文疑似夹杂错误外文",
            status=(
                "已输出译文，但原文里混着疑似写错的外文，未写入翻译记忆库；"
                "请对照原件确认这段原文本身是否需要更正。"
            ),
            # 程序能做的只是把这段照常翻出来，它没法判断原文里那串外文是不是写错了——
            # 那要对着原件看。这是"原文可能有问题"，不是"程序已经处理好了"。
            severity="needs_review",
        )
    if uncertain:
        _add_quality_issues(
            quality_issues,
            segment_locations,
            uncertain,
            problem="混合语言内容未能确认",
            status="已保留原文，需人工复核。",
            severity="needs_review",
        )
    if semantic_translate:
        _add_quality_issues(
            quality_issues,
            segment_locations,
            semantic_translate,
            problem="混合语言译文经语义校验接受",
            status=(
                "混合语言路径候选未通过程序化规则校验，但专属语义校验判定可接受；"
                "本段已写入译文，且不会写入翻译记忆库。"
            ),
            severity="resolved",
        )


def _build_source_excerpt(text: str, *, head: int = 18, tail: int = 16) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= head + tail + 3:
        return normalized
    return f"{normalized[:head]}……{normalized[-tail:]}"


def _merge_sentences(current: str, addition: str) -> str:
    """Join two judgments about the same position without repeating either."""
    nxt = " ".join(str(addition or "").split())
    cur = " ".join(str(current or "").split())
    if not nxt:
        return cur
    if not cur:
        return nxt
    if nxt in cur:
        return cur
    return f"{cur}；{nxt}"


def _format_location_label(location: str) -> str:
    paragraph_match = re.match(r"body\.paragraph\[(\d+)\]", str(location or ""))
    if paragraph_match:
        return f"正文段落 {int(paragraph_match.group(1)) + 1}"

    cell_match = re.match(r"table\[(\d+)\]\.cell\[(\d+)\]", str(location or ""))
    if cell_match:
        return f"表格 {int(cell_match.group(1)) + 1} / 单元格 {int(cell_match.group(2)) + 1}"

    return location or "未知位置"


def _format_output_location_label(location: str) -> str:
    """成品体检条目的位置标签。输出文档的段落号与源文档不是同一套坐标
    （双语写入后段落数已经变了），加「输出」前缀，免得用户拿着报告去
    源文档里数段落。"""
    label = _format_location_label(location)
    return label if label == "未知位置" else f"输出{label}"


def _build_translation_scope_lines(
    file_results: list[dict],
    *,
    translate_headers_footers: bool = False,
) -> list[str]:
    """报告里明写翻译范围，用户不该翻到那一页才发现某处没翻。"""
    header_footer_files: list[str] = []
    for item in file_results:
        if not item.get("success"):
            continue
        target = item.get("output") or item.get("source_path")
        if not target:
            continue
        if count_text_bearing_header_footer_parts(target) > 0:
            header_footer_files.append(str(item.get("name") or "未知文件"))

    if not header_footer_files:
        return []

    listed = "、".join(header_footer_files[:5])
    if len(header_footer_files) > 5:
        listed += f" 等 {len(header_footer_files)} 个文件"
    if translate_headers_footers:
        header_footer_line = (
            f"- 页眉、页脚已翻译，译文接在同一行原文后面（不另起一行，避免撑高版心）：{listed}。"
        )
    else:
        header_footer_line = (
            f"- 页眉、页脚不参与翻译，输出文档中保持原文：{listed}。"
            "如需翻译，请在 Word 选项里打开「翻译页眉页脚」后重跑。"
        )
    return [
        "## 翻译范围说明",
        "",
        header_footer_line,
        "- 自动生成的目录、索引、页码域同样不翻译：域一刷新就会覆盖译文。",
        "",
    ]


def _write_word_quality_report(
    *,
    output_dir: Path,
    file_results: list[dict],
    issues: list[dict],
    elapsed_sec: float,
    tm_hit_count: int,
    api_call_count: int,
    translate_headers_footers: bool = False,
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

        preprocess_lines = []
        for item in file_results:
            preprocess = item.get("preprocess") or {}
            method = preprocess.get("method")
            if not method:
                continue
            preprocess_lines.append(
                (
                    f"- {item.get('name') or '未知文件'}：{method}"
                    f"（自动编号 {int(preprocess.get('labels_seen') or 0)} 段，"
                    f"物化 {int(preprocess.get('labels_prepended') or 0)} 段）"
                )
            )
        if preprocess_lines:
            lines.extend(["## Word 预处理", "", *preprocess_lines, ""])

        lines.extend(
            _build_translation_scope_lines(
                file_results,
                translate_headers_footers=translate_headers_footers,
            )
        )

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
                    # 这些片段取自**原文**，是规则校验拿去比对、没在译文里按字面对上的部分。
                    # 早先叫「问题片段」，列出来的又都是中文日期编号，读起来像"译文里的
                    # 日期错了"，而实际情况往往是译文把日期正常译成了外文写法。
                    lines.append(
                        f"- 规则校验对不上的原文片段：{'、'.join(str(item) for item in fragments)}"
                    )
                lines.append("")

        report_path.write_text("\n".join(lines), encoding="utf-8")
        return report_path
    except Exception as exc:  # noqa: BLE001 - the caller records a non-fatal warning.
        # Do not swallow the exception here.  ``WordTaskRunner._run`` owns the
        # task-level result contract and converts this into ``report_warning``
        # while preserving usable Word output.  Returning ``None`` here would
        # make a real filesystem failure indistinguishable from no report and
        # hide its reason from the UI.
        logger.warning(f"Word 翻译质量报告写入失败：{exc}")
        raise OSError(f"Word 翻译质量报告写入失败：{exc}") from exc
