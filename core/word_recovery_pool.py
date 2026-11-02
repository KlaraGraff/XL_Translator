"""Word 翻译的恢复与补译池：主翻译之后，重试没过关的段落、做语义仲裁。
WordTaskRunner 调用它。
"""

from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

from loguru import logger

from core.api_concurrency_control import ApiKeyTemporarilyUnavailableError
from core.api_scheduler import (
    API_REQUEST_CATEGORY_RECOVERY,
    WeightedApiScheduler,
)
from core.prompt_cache import carry_meter
from core.residual_classifier import CATEGORY_TERM_FRAGMENT, summarize_residuals
from core.residual_repair import build_feedback_note
from core.task_runner import WordRecoveryStatusMsg, user_facing_reason
from core.translation_filter import (
    VALIDATION_PROFILE_STRICT,
    VALIDATION_PROFILE_WORD_RECOVERY,
    VALIDATION_STATUS_FAIL,
    VALIDATION_STATUS_SOFT_PASS_REVIEW,
    TranslationValidationIssue,
    TranslationValidationResult,
    validate_translation,
)
from core.translation_protocol import extract_replace_translation, is_replace_translation
from core.translation_review import ReviewItem, build_review_prompt, review_translations
from core.word_batching import WordBatchRunStats, translate_word_texts
from core.zh_hant import lang_base
from engines.base_engine import engine_supports_chat

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


_SEMANTIC_VERDICT_EQUIVALENT = "equivalent"


_SEMANTIC_VERDICT_NOT_EQUIVALENT = "not_equivalent"


_SEMANTIC_VERDICT_UNCERTAIN = "uncertain"


# 复查结论给用户看的说法；verdict 本身是协议代码，不进运行日志。
_SEMANTIC_VERDICT_LABELS = {
    _SEMANTIC_VERDICT_NOT_EQUIVALENT: "意思与原文不一致",
    _SEMANTIC_VERDICT_UNCERTAIN: "无法确定意思是否一致",
}


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


_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")


# 译文里不该出现的文字：原文没有、目标语言也不用的（法译中的译文里冒出一个俄文词
# 「предусмотр」）。拉丁字母和汉字不在这里查（专名、型号、单位常用拉丁字母；汉字由
# _translation_in_unexpected_cjk 和残留中文检查管）。
_FOREIGN_SCRIPTS = {
    "cyrillic": "\u0400-\u052f",
    "greek": "\u0370-\u03ff\u1f00-\u1fff",
    "arabic": "\u0600-\u06ff\u0750-\u077f",
    "hebrew": "\u0590-\u05ff",
    "thai": "\u0e00-\u0e7f",
    "devanagari": "\u0900-\u097f",
    "hangul": "\uac00-\ud7af\u1100-\u11ff",
    "kana": "\u3040-\u309f\u30a0-\u30fa",
    "armenian": "\u0530-\u058f",
    "georgian": "\u10a0-\u10ff",
}


# 目标语言自己用的文字。不在表里的（自定义目标语言）不查。
_TARGET_SCRIPTS = {
    **{lang: () for lang in (
        "zh", "en", "fr", "de", "es", "it", "pt", "nl", "pl", "cs", "sk", "ro", "hu", "tr", "vi", "id", "ms",
        "sv", "da", "no", "nb", "fi", "et", "lv", "lt", "sl", "hr", "bs", "sq", "ca", "eu", "gl", "ga", "is",
        "tl", "sw", "az", "uz",
    )},
    **{lang: ("cyrillic",) for lang in ("ru", "uk", "be", "bg", "mk", "kk", "ky", "mn", "tg")},
    "sr": ("cyrillic",),
    "el": ("greek",),
    **{lang: ("arabic",) for lang in ("ar", "fa", "ur", "ps")},
    "he": ("hebrew",),
    "th": ("thai",),
    **{lang: ("devanagari",) for lang in ("hi", "mr", "ne")},
    "ko": ("hangul",),
    "ja": ("kana",),
    "hy": ("armenian",),
    "ka": ("georgian",),
}


_FOREIGN_SCRIPT_RES = {name: re.compile(f"[{chars}]") for name, chars in _FOREIGN_SCRIPTS.items()}


# 连着两个字以上才算一个词：单个 μ、Ω 是单位符号
_FOREIGN_RUN_RES = {
    name: re.compile(f"[{chars}][{chars}\u0300-\u036f'’-]*[{chars}]") for name, chars in _FOREIGN_SCRIPTS.items()
}


def _unexpected_script_fragments(source: str, translation: str | None, target_lang: str) -> list[str]:
    """译文里原文没有、目标语言也不用的那种文字的词（如法译中译文里的俄文词）。"""
    target = str(target_lang or "").strip().lower().replace("_", "-")
    allowed = _TARGET_SCRIPTS.get(target, _TARGET_SCRIPTS.get(target.split("-")[0]))
    text = str(translation or "")
    if allowed is None or not text:
        return []
    source_text = str(source or "")
    fragments: list[str] = []
    for name, run_re in _FOREIGN_RUN_RES.items():
        if name in allowed or _FOREIGN_SCRIPT_RES[name].search(source_text):
            continue
        fragments.extend(match.group(0) for match in run_re.finditer(text))
    return list(dict.fromkeys(fragments))


def _unexpected_script_issue(fragments: list[str]) -> TranslationValidationIssue:
    return TranslationValidationIssue(
        code="unexpected_script",
        message="译文夹杂了原文和目标语言都不用的文字。",
        fragments=tuple(fragments),
    )


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
    # 夹杂了别的文字（法译中冒出俄文词）：首次译文一律重译；重译后仍有的照用，但标出来请人看。
    foreign = _unexpected_script_fragments(source_text, translated_text, target_lang)
    if strict_validation.is_pass and not residual_needs_retry and not foreign:
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
            if foreign:
                recovery_validation = TranslationValidationResult(
                    status=VALIDATION_STATUS_SOFT_PASS_REVIEW,
                    issues=(*recovery_validation.issues, _unexpected_script_issue(foreign)),
                )
            return _WordRetryEvaluation(True, recovery_validation)
        return _WordRetryEvaluation(False, recovery_validation)

    if foreign and strict_validation.is_pass:
        return _WordRetryEvaluation(
            False,
            TranslationValidationResult(
                status=VALIDATION_STATUS_FAIL, issues=(_unexpected_script_issue(foreign),)
            ),
        )
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


@dataclass
class _WordRecoveryTicket:
    """一次恢复池线程任务的记账凭据。

    worker 正常跑完会自己把 inflight 标志记回去，并置 settled；只要它没走到
    那一步（抛异常、被取消、线程池已关停），就由 `_future_done` 按这张凭据兜底
    复位——否则 `complete()` 永假，`wait_for_completion` 的等待循环空转到天荒地老。
    """

    kind: str  # "retry" | "semantic"
    source: str
    attempt_index: int = 0
    settled: bool = False


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
    """Parallel Word recovery pool for retry and semantic arbitration.

    锁序（改这个类之前先读完这一段）：

    ``pool._condition``  →  **绝不再获取任何会阻塞的锁**，尤其是
    ``ThreadPoolExecutor`` 内部那把 ``_shutdown_lock``。

    原因在 CPython 的 ``Executor.shutdown(cancel_futures=True)``：它**持着**
    ``executor._shutdown_lock`` 逐个 ``work_item.future.cancel()``，而 ``cancel()``
    会**同线程**回调 done-callback（我们的 ``_future_done``），后者要拿
    ``_condition``。于是：

    * 若 done-callback 里再去 ``executor.submit``，就是同一把非重入锁在同一个
      线程上二次获取 —— 直接自锁（审查 WT1-M1，已实测复现）。
    * 若别的线程「持着 ``_condition`` 去 submit」，它等 ``_shutdown_lock``、
      关停线程等 ``_condition``，两把锁构成 ABBA 环（审查 WT1-M2，已实测复现）。

    两条都表现为任务线程永不返回：UI 停在「运行中」、临时文件不清理、soffice
    不回收，和审计高-8 的原始症状一模一样。

    所以本类的排队一律两段走：持锁期间只用 ``_submit_locked`` 把任务记进
    ``_pending_submits``，出锁之后再由 ``_flush_pending_submits`` 统一
    ``executor.submit``。``pool._shutdown_lock`` 同理只在不持 ``_condition`` 时获取。
    """

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
        progress_callback: Callable[[int, int], None] | None = None,
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
        self._progress_callback = progress_callback
        self._source_locations = source_locations or {}
        self._enable_semantic = enable_semantic and engine_supports_chat(engine)
        self._states: dict[str, _WordRecoveryState] = {}
        self._futures: dict[object, _WordRecoveryTicket] = {}
        # 持 _condition 期间攒下的待排队任务，出锁后统一 submit（见类文档的锁序）
        self._pending_submits: list[tuple[Callable, _WordRecoveryTicket, tuple]] = []
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

        try:
            with self._condition:
                state = self._states.setdefault(
                    source_text, _WordRecoveryState(source=source_text)
                )
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
        finally:
            self._flush_pending_submits()

    def start(self) -> None:
        try:
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
        finally:
            self._flush_pending_submits()

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
            stopped = False
            with self._condition:
                while self._fatal_error is None and not self._all_complete_locked():
                    # 只看「全部完成」会漏掉停止：停止后 _schedule_retry_locked
                    # 不再排下一轮，剩余轮次永远补不齐，等待循环就成了死循环
                    # （任务线程空转、UI 永远「运行中」）。停止是第二个出口。
                    if self._stop_requested():
                        stopped = True
                        break
                    self._report_progress_locked()
                    self._condition.wait(timeout=0.1)

                fatal_error = self._fatal_error

            if fatal_error is not None:
                self.shutdown(cancel_futures=True)
                raise fatal_error

            # 停止时丢掉还在排队的任务，但仍要等在跑的 worker 收尾；已经恢复的
            # 译文照常交回——停止不等于丢结果。
            self.shutdown(cancel_futures=stopped)
            return self._build_outcome()
        finally:
            # start()、等待循环、_build_outcome 里任何一处抛出（含 KeyboardInterrupt）
            # 都不能让线程池活下来。已经关停时这里是空操作。
            self.shutdown(cancel_futures=True)

    def _report_progress_locked(self) -> None:
        """恢复池在等的这段也是等模型：按已了结的段数推进度条。"""
        if not self._progress_callback or not self._states:
            return
        done = sum(1 for state in self._states.values() if state.complete(self._max_attempts))
        self._progress_callback(done, len(self._states))

    def _stop_requested(self) -> bool:
        return bool(self._should_stop and self._should_stop())

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
        if self._stop_requested():
            # 停止后不再排下一轮。剩余预算必须就地记为用尽，否则这一段的
            # complete() 永假——等待循环没有别的东西会来补齐它。
            state.attempts_done = self._max_attempts
            return
        state.retry_inflight = True
        attempt_index = state.attempts_done + 1
        self._latest_retry_round = max(self._latest_retry_round, attempt_index)
        ticket = _WordRecoveryTicket(
            kind="retry",
            source=state.source,
            attempt_index=attempt_index,
        )
        # 只记账，不 submit：真正排队由出锁后的 _flush_pending_submits 完成。
        # retry_inflight 已经置上，等待循环在这期间不会误判「全部完成」。
        self._submit_locked(self._run_retry_attempt, ticket)
        self._emit_status_locked()

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
        if self._stop_requested():
            # 停止后不该再花钱发仲裁请求（与 _schedule_retry_locked 同口径）。
            # 少了这道闸，停止收尾期间还在跑的 retry worker 会继续往线程池排
            # 仲裁任务，白烧一轮 API 额度。
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
        ticket = _WordRecoveryTicket(kind="semantic", source=state.source)
        self._submit_locked(self._run_semantic_check, ticket, candidate, validation)
        self._emit_status_locked()

    def _submit_locked(self, fn, ticket: _WordRecoveryTicket, *args) -> None:
        """持 `_condition` 时的排队入口：只记账，不碰线程池。

        调用方必须已经把对应的 inflight 标志置上（`retry_inflight` /
        `semantic_inflight`），这样在「已记账、尚未 submit」这段窗口里，等待循环
        看到的仍是「未完成」，不会提前判定全部完成。真正的 `executor.submit`
        由出锁后的 `_flush_pending_submits` 执行 —— 原因见类文档的锁序说明。
        """
        self._pending_submits.append((fn, ticket, args))

    def _flush_pending_submits(self) -> None:
        """出锁后把攒下的任务排进线程池；排不下去的就地退回记账。

        调用时**不得**持有 `_condition`。线程池已经关停（或正在关停）时一律不再
        碰 executor —— `shutdown(cancel_futures=True)` 的取消回调正走在这条线上，
        再 submit 就是同一把 `_shutdown_lock` 的二次获取（审查 WT1-M1）。
        """
        while True:
            with self._condition:
                if not self._pending_submits:
                    return
                batch = self._pending_submits
                self._pending_submits = []
                shutting_down = self._executor_shutdown
            for fn, ticket, args in batch:
                if shutting_down or not self._submit(fn, ticket, *args):
                    with self._condition:
                        self._abandon_unsubmitted_ticket_locked(ticket)
                        self._emit_status_locked()
                        self._condition.notify_all()

    def _abandon_unsubmitted_ticket_locked(self, ticket: _WordRecoveryTicket) -> None:
        """任务压根没排进线程池时的退账：inflight 标志绝不能挂着不还。

        留在 True 上就等于这一段 `complete()` 永假，等待循环空转到天荒地老。
        """
        ticket.settled = True
        state = self._states.get(ticket.source)
        if state is None:
            return
        if ticket.kind == "semantic":
            state.semantic_inflight = max(0, state.semantic_inflight - 1)
            return
        # 重试排不下去 = 线程池已关停，剩下的轮次也没机会跑，按预算用尽处理。
        state.retry_inflight = False
        state.attempts_done = self._max_attempts

    def _submit(self, fn, ticket: _WordRecoveryTicket, *args) -> bool:
        """真正把任务排进线程池。调用方**不得**持有 `_condition`（见类文档锁序）。"""
        try:
            future = self._executor.submit(carry_meter(fn), ticket, *args)
        except RuntimeError as exc:
            # 线程池已经关停（停止收尾或异常路径）。调用方负责把 inflight
            # 标志退回去，返回 False 就是在通知它。
            logger.debug(f"Word 恢复池线程池已关停，放弃排队：{exc!r}")
            return False
        # 先登记凭据再挂回调：任务可能瞬间跑完，回调会在本线程内联执行，
        # 登记晚了 `_future_done` 就 pop 不到凭据，兜底复位形同虚设。
        with self._condition:
            self._futures[future] = ticket
        future.add_done_callback(self._future_done)
        return True

    def _settle_abandoned_ticket_locked(self, ticket: _WordRecoveryTicket) -> None:
        """任务没能自己完成记账（抛异常、被取消）时的兜底复位。

        inflight 标志留在 True 上就等于这一段永远不会 complete()，等待循环
        再也退不出来——审计里的第二条挂死路径。
        """
        ticket.settled = True
        state = self._states.get(ticket.source)
        if state is None:
            return
        if ticket.kind == "semantic":
            state.semantic_inflight = max(0, state.semantic_inflight - 1)
            self._emit_status_locked()
            return
        state.retry_inflight = False
        # 崩掉的这一轮照样计入预算（否则轮次号原地打转）。
        state.attempts_done = max(state.attempts_done, ticket.attempt_index)
        if self._executor_shutdown or self._stop_requested():
            # 线程池已关停/已按停止：剩余轮次没机会跑了，就地记为预算用尽。
            # 这里绝不能再调 _schedule_retry_locked —— 本函数最常见的调用点就是
            # shutdown(cancel_futures=True) 的取消回调，排队会连锁到 executor
            # 的 _shutdown_lock 上（审查 WT1-M1）。
            state.attempts_done = self._max_attempts
        else:
            # 一轮崩掉不吞掉剩余轮次：继续排下一轮（仅记账，出锁后才 submit）。
            self._schedule_retry_locked(state)
        self._emit_status_locked()

    def _future_done(self, future) -> None:
        try:
            if not future.cancelled():
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
                    "有一个段落的自动重译未完成，已标记为需复核："
                    + user_facing_reason(exc, fallback="重译过程中出错。"),
                )
        finally:
            try:
                with self._condition:
                    ticket = self._futures.pop(future, None)
                    try:
                        if ticket is not None and not ticket.settled:
                            self._settle_abandoned_ticket_locked(ticket)
                    finally:
                        # 兜底复位本身抛出也不能吃掉唤醒，否则等待线程要空转到超时。
                        self._condition.notify_all()
            finally:
                # 出锁后才排队。关停期间 flush 只做退账、不碰 executor，所以取消
                # 回调走到这里是安全的。
                self._flush_pending_submits()

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
        foreign = _unexpected_script_fragments(source, last_candidate, self._target_lang)
        if foreign:
            fragments = "、".join(f"«{text}»" for text in foreign)
            return (
                f"{self._retry_prompt}\n5. 上一稿译文夹杂了原文和目标语言都不用的文字：{fragments}。"
                "本稿必须把整段完整翻译成目标语言，不得出现这类文字。"
            )
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

    def _run_retry_attempt(self, ticket: _WordRecoveryTicket) -> None:
        source = ticket.source
        attempt_index = ticket.attempt_index
        self._log_source_locations(
            "INFO",
            source,
            f"正在单独重译这一段（第 {attempt_index}/{self._max_attempts} 轮）",
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
                (lambda msg: self._log_callback("INFO", msg))
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
        self._handle_retry_result(ticket, candidate)

    def _handle_retry_result(self, ticket: _WordRecoveryTicket, candidate: str) -> None:
        source = ticket.source
        evaluation = _evaluate_word_translation(
            source,
            candidate,
            source_lang=self._source_lang,
            target_lang=self._target_lang,
            allow_recovery=True,
        )
        try:
            self._handle_retry_result_locked(ticket, candidate, evaluation)
        finally:
            # 出锁后才排队：持 _condition 去 executor.submit 会和
            # shutdown(cancel_futures=True) 的取消回调构成 ABBA 环（审查 WT1-M2）。
            self._flush_pending_submits()

    def _handle_retry_result_locked(
        self,
        ticket: _WordRecoveryTicket,
        candidate: str,
        evaluation: _WordRetryEvaluation,
    ) -> None:
        source = ticket.source
        attempt_index = ticket.attempt_index
        with self._condition:
            # 记账已经由本函数接手，_future_done 不再兜底复位
            ticket.settled = True
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
                self._log_source_locations("OK", source, "重译成功")
                return
            state.last_validation = evaluation.validation
            state.last_candidate = candidate
            if attempt_index >= self._max_attempts:
                self._log_source_locations("WARN", source, "重译未成功")
            else:
                self._log_source_locations(
                    "WARN",
                    source,
                    f"重译未成功，继续重译（已完成 {attempt_index}/{self._max_attempts} 轮）",
                )
            self._schedule_semantic_locked(state, candidate, evaluation.validation)
            self._schedule_retry_locked(state)
            self._emit_status_locked()
            self._condition.notify_all()

    def _run_semantic_check(
        self,
        ticket: _WordRecoveryTicket,
        candidate: str,
        validation: TranslationValidationResult,
    ) -> None:
        source = ticket.source
        self._log_source_locations("INFO", source, "正在复查译文意思")
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
            should_stop=self._should_stop,
        )
        with self._condition:
            # 记账已经由本函数接手，_future_done 不再兜底复位
            ticket.settled = True
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
                self._log_source_locations("OK", source, "复查通过，采用译文")
            elif not state.accepted and not result.equivalent:
                self._semantic_uncertain_sources.add(source)
                logger.info(f"语义仲裁未接受：verdict={result.verdict} reason={result.reason}")
                self._log_source_locations(
                    "WARN",
                    source,
                    "复查未通过（"
                    + _SEMANTIC_VERDICT_LABELS.get(result.verdict, "无法确定意思是否一致")
                    + "）",
                )
            self._emit_status_locked()
            self._condition.notify_all()
        # 本函数目前不排新任务，但仍统一走一次出锁 flush：将来这里若加了排队，
        # 不至于把任务永远留在 _pending_submits 里。
        self._flush_pending_submits()


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

    if source_lang == "zh" and lang_base(target_lang) not in {"zh", "ja"}:
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
    should_stop: Callable[[], bool] | None = None,
    problem: str = "",
) -> _SemanticArbitrationResult:
    """一条译文的「意思是否一致」复查：请求、提示词和解析都在 core/translation_review，
    与 Excel、CAD、PDF 原位替换、扩展格式共用。拿不准、出错一律 uncertain。"""
    # 停止之后不再排队、不再发付费请求、不再限流重试：拿不准就按拿不准处理。
    if should_stop is not None and should_stop():
        return _SemanticArbitrationResult(_SEMANTIC_VERDICT_UNCERTAIN, "任务已停止")
    candidate_text = _candidate_validation_text(candidate)
    if not candidate_text:
        return _SemanticArbitrationResult(_SEMANTIC_VERDICT_UNCERTAIN, "候选译文为空")
    verdict = review_translations(
        engine,
        [ReviewItem("1", source, candidate_text, problem)],
        source_lang=source_lang,
        target_lang=target_lang,
        api_scheduler=api_scheduler,
        should_stop=should_stop,
        error_callback=error_callback,
        context_label="Word 复查",
    ).get("1")
    if verdict is None:
        return _SemanticArbitrationResult(_SEMANTIC_VERDICT_UNCERTAIN, "")
    return _SemanticArbitrationResult(verdict.verdict, verdict.reason)


def _build_semantic_arbitration_prompt() -> str:
    return build_review_prompt()


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
