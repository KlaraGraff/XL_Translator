"""Mixed-language source routing and structured translation helpers."""

from __future__ import annotations

import json
import math
import re
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Callable

from config import (
    REVIEW_MARK_COLOR_FOREIGN_NOISE_DEFAULT,
    REVIEW_MARK_COLOR_SEMANTIC_DEFAULT,
    REVIEW_MARK_COLOR_UNRESOLVED_DEFAULT,
    REVIEW_MARK_FOREIGN_NOISE,
    REVIEW_MARK_SEMANTIC,
    REVIEW_MARK_UNRESOLVED,
)
from core.api_concurrency_control import (
    ApiKeyTemporarilyUnavailableError,
    handle_api_concurrency_limit,
)
from core.api_scheduler import (
    API_REQUEST_CATEGORY_NORMAL,
    API_REQUEST_CATEGORY_RECOVERY,
    WeightedApiScheduler,
)
from core.language_registry import get_source_lang_display, get_target_lang_display
from core.translation_filter import (
    TranslationValidationResult,
    validate_translation,
)
from engines.base_engine import TranslationEngine, strip_markdown_json

MIXED_ACTION_EXISTING_BILINGUAL = "existing_bilingual"
MIXED_ACTION_TRANSLATE = "translate"
MIXED_ACTION_FOREIGN_NOISE = "foreign_noise_suspected"
MIXED_ACTION_UNCERTAIN = "uncertain"
MIXED_LANGUAGE_ACTIONS = {
    MIXED_ACTION_EXISTING_BILINGUAL,
    MIXED_ACTION_TRANSLATE,
    MIXED_ACTION_FOREIGN_NOISE,
    MIXED_ACTION_UNCERTAIN,
}

MIXED_MARK_SEMANTIC = REVIEW_MARK_SEMANTIC
MIXED_MARK_UNRESOLVED = REVIEW_MARK_UNRESOLVED
MIXED_MARK_FOREIGN_NOISE = REVIEW_MARK_FOREIGN_NOISE

MIXED_COLOR_SEMANTIC = REVIEW_MARK_COLOR_SEMANTIC_DEFAULT
MIXED_COLOR_UNRESOLVED = REVIEW_MARK_COLOR_UNRESOLVED_DEFAULT
MIXED_COLOR_FOREIGN_NOISE = REVIEW_MARK_COLOR_FOREIGN_NOISE_DEFAULT

SHORT_LABEL_MAX_CJK = 12
SHORT_LABEL_MAX_LENGTH = 60
LONG_BODY_RESIDUAL_LATIN_THRESHOLD = 4
DEFAULT_MIXED_RETRY_ATTEMPTS = 3
DEFAULT_MIXED_MAX_BATCH_CHARS = 6000
_API_WEIGHT_CHARS_PER_SLOT = 4000
_API_WEIGHT_PROMPT_CHAR_CAP = 5000
_API_WEIGHT_OUTPUT_MULTIPLIER = 1.4

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_LATIN_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ]")
_LATIN_LETTER_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ]")
_WHITESPACE_RE = re.compile(r"\s+")

_STANDARD_RE = re.compile(r"\b(?:GB/T|ISO|EN|ASTM|DIN|IEC|JIS|BS)\b", re.IGNORECASE)
_UNIT_TOKEN_PATTERN = (
    r"(?:mpa|kpa|mm|cm|km|kg|kn|kv|kw|hz|m2|m3|m²|m³|pa|m|g|t|n|v|w)"
)
_UNIT_RE = re.compile(
    rf"(?<![A-Za-zÀ-ÖØ-öø-ÿ]){_UNIT_TOKEN_PATTERN}(?![A-Za-zÀ-ÖØ-öø-ÿ])",
    re.IGNORECASE,
)
_MODEL_RE = re.compile(
    r"\b(?:"
    r"C\d{1,3}|"
    r"DN\d+[A-Z]?|"
    r"PN\d+|"
    r"HRB\d+|"
    r"HPB\d+|"
    r"M\d+|"
    r"A\d+(?:\.\d+)?|"
    r"PDF-\d+|"
    r"[A-Z]{1,4}-\d+[A-Z0-9-]*"
    r")\b",
    re.IGNORECASE,
)
_UPPER_ABBR_RE = re.compile(r"\b[A-Z]{2,8}\b")


@dataclass(frozen=True)
class MixedLanguageDecision:
    is_mixed: bool
    reason: str = ""


@dataclass
class MixedLanguageResult:
    source: str
    action: str = MIXED_ACTION_UNCERTAIN
    translation: str = ""
    note: str = ""
    accepted_by: str = ""
    validation: TranslationValidationResult = field(default_factory=TranslationValidationResult)

    @property
    def has_translation(self) -> bool:
        return self.action in {MIXED_ACTION_TRANSLATE, MIXED_ACTION_FOREIGN_NOISE} and bool(
            self.translation.strip()
        )

    @property
    def mark_kind(self) -> str | None:
        if self.action == MIXED_ACTION_FOREIGN_NOISE:
            return MIXED_MARK_FOREIGN_NOISE
        if self.action == MIXED_ACTION_UNCERTAIN:
            return MIXED_MARK_UNRESOLVED
        if self.action == MIXED_ACTION_TRANSLATE and self.accepted_by == "semantic":
            return MIXED_MARK_SEMANTIC
        return None


@dataclass
class MixedLanguageRunStats:
    input_count: int = 0
    mixed_batch_count: int = 0
    retry_count: int = 0
    failed_count: int = 0
    semantic_check_count: int = 0
    semantic_accepted_count: int = 0
    action_counts: dict[str, int] = field(default_factory=dict)
    adaptive_concurrency_reductions: int = 0
    adaptive_lowest_concurrency: int = 0

    def record_result(self, result: MixedLanguageResult) -> None:
        self.action_counts[result.action] = self.action_counts.get(result.action, 0) + 1

    def record_adaptive_concurrency_decision(self, decision) -> None:
        self.adaptive_concurrency_reductions += 1
        current = int(getattr(decision, "current_capacity", 0) or 0)
        if self.adaptive_lowest_concurrency <= 0:
            self.adaptive_lowest_concurrency = current
        elif current > 0:
            self.adaptive_lowest_concurrency = min(self.adaptive_lowest_concurrency, current)


def classify_mixed_language_source(
    text: str,
    *,
    target_lang: str,
    source_lang: str = "zh",
) -> MixedLanguageDecision:
    """Return whether one source should use the mixed-language path."""
    source = str(text or "").strip()
    if not source or target_lang == "zh":
        return MixedLanguageDecision(False)
    if source_lang != "zh":
        return MixedLanguageDecision(False)
    if not _CJK_RE.search(source) or not _LATIN_RE.search(source):
        return MixedLanguageDecision(False)

    compact = _WHITESPACE_RE.sub(" ", source)
    cjk_count = len(_CJK_RE.findall(source))
    if cjk_count <= SHORT_LABEL_MAX_CJK and len(compact) <= SHORT_LABEL_MAX_LENGTH:
        return MixedLanguageDecision(True, "short_label")

    residual = _strip_long_body_whitelist(source)
    residual_latin_count = len(_LATIN_LETTER_RE.findall(residual))
    if residual_latin_count > LONG_BODY_RESIDUAL_LATIN_THRESHOLD:
        return MixedLanguageDecision(True, "long_body")
    return MixedLanguageDecision(False)


def split_mixed_language_sources(
    texts: list[str],
    *,
    target_lang: str,
    source_lang: str = "zh",
) -> tuple[list[str], list[str]]:
    """Split sources into normal and mixed-language groups while preserving order."""
    normal: list[str] = []
    mixed: list[str] = []
    for text in texts:
        decision = classify_mixed_language_source(
            text,
            target_lang=target_lang,
            source_lang=source_lang,
        )
        if decision.is_mixed:
            mixed.append(text)
        else:
            normal.append(text)
    return normal, mixed


def translate_mixed_language_texts(
    texts: list[str],
    *,
    engine: TranslationEngine,
    target_lang: str,
    system_prompt: str,
    source_lang: str = "zh",
    concurrency: int = 1,
    max_items_per_batch: int = 8,
    max_chars_per_batch: int = DEFAULT_MIXED_MAX_BATCH_CHARS,
    retry_attempts: int = DEFAULT_MIXED_RETRY_ATTEMPTS,
    progress_callback: Callable[[int, int], None] | None = None,
    error_callback: Callable[[str], None] | None = None,
    should_stop=None,
    api_scheduler: WeightedApiScheduler | None = None,
    request_category: str = API_REQUEST_CATEGORY_NORMAL,
    stats: MixedLanguageRunStats | None = None,
    drained_callback: Callable[[], None] | None = None,
) -> dict[str, MixedLanguageResult]:
    """Translate mixed-language sources with structured actions keyed by source text."""
    if not texts:
        return {}

    run_stats = stats or MixedLanguageRunStats()
    run_stats.input_count = len(texts)
    batches = _build_mixed_batches(
        texts,
        max_items=max_items_per_batch,
        max_chars=max_chars_per_batch,
    )
    run_stats.mixed_batch_count = len(batches)
    results: dict[str, MixedLanguageResult] = {}
    done_count = 0
    lock = threading.Lock()
    drained_notified = False

    def _record_progress(completed: int) -> None:
        nonlocal done_count
        with lock:
            done_count += completed
            if progress_callback:
                progress_callback(min(done_count, len(texts)), len(texts))

    def _notify_drained() -> None:
        nonlocal drained_notified
        if drained_notified:
            return
        drained_notified = True
        if drained_callback:
            drained_callback()

    def _translate_one_batch(batch: list[str]) -> dict[str, MixedLanguageResult]:
        partial = _translate_mixed_batch_with_fallback(
            batch,
            engine=engine,
            target_lang=target_lang,
            system_prompt=system_prompt,
            source_lang=source_lang,
            api_scheduler=api_scheduler,
            request_category=request_category,
            retry_attempts=retry_attempts,
            should_stop=should_stop,
            error_callback=error_callback,
            stats=run_stats,
        )
        recovered = _recover_failed_mixed_results(
            partial,
            engine=engine,
            target_lang=target_lang,
            system_prompt=system_prompt,
            source_lang=source_lang,
            api_scheduler=api_scheduler,
            retry_attempts=retry_attempts,
            should_stop=should_stop,
            error_callback=error_callback,
            stats=run_stats,
        )
        _record_progress(len(batch))
        return recovered

    max_workers = max(1, int(concurrency or 1))
    max_workers = min(max_workers, len(batches))

    if max_workers <= 1:
        for batch in batches:
            if should_stop and should_stop():
                break
            results.update(_translate_one_batch(batch))
        _notify_drained()
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map: dict = {}
            batch_iter = iter(batches)

            def _submit_next() -> bool:
                if should_stop and should_stop():
                    return False
                try:
                    batch = next(batch_iter)
                except StopIteration:
                    _notify_drained()
                    return False
                future = executor.submit(_translate_one_batch, batch)
                future_map[future] = batch
                return True

            for _ in range(max_workers):
                if not _submit_next():
                    break

            while future_map:
                done_futures, _ = wait(tuple(future_map.keys()), return_when=FIRST_COMPLETED)
                for future in done_futures:
                    future_map.pop(future, None)
                    results.update(future.result())
                    if not (should_stop and should_stop()):
                        _submit_next()

    for text in texts:
        result = results.setdefault(text, _uncertain_result(text, note="missing_result"))
        run_stats.record_result(result)
    return results


def _strip_long_body_whitelist(text: str) -> str:
    stripped = _STANDARD_RE.sub(" ", text)
    stripped = _UNIT_RE.sub(" ", stripped)
    stripped = _MODEL_RE.sub(" ", stripped)
    stripped = _UPPER_ABBR_RE.sub(" ", stripped)
    return stripped


def _build_mixed_batches(
    texts: list[str],
    *,
    max_items: int,
    max_chars: int,
) -> list[list[str]]:
    batches: list[list[str]] = []
    current: list[str] = []
    current_chars = 0
    item_limit = max(1, int(max_items or 1))
    char_limit = max(1, int(max_chars or 1))

    def flush() -> None:
        nonlocal current, current_chars
        if current:
            batches.append(current)
        current = []
        current_chars = 0

    for text in texts:
        text_chars = len(str(text or ""))
        if text_chars >= char_limit:
            flush()
            batches.append([text])
            continue
        if len(current) >= item_limit or (current and current_chars + text_chars > char_limit):
            flush()
        current.append(text)
        current_chars += text_chars
    flush()
    return batches


def _translate_mixed_batch_with_fallback(
    batch: list[str],
    *,
    engine: TranslationEngine,
    target_lang: str,
    system_prompt: str,
    source_lang: str,
    api_scheduler: WeightedApiScheduler | None,
    request_category: str,
    retry_attempts: int,
    should_stop,
    error_callback,
    stats: MixedLanguageRunStats,
) -> dict[str, MixedLanguageResult]:
    if not batch:
        return {}
    if should_stop and should_stop():
        return {text: _uncertain_result(text, note="stopped") for text in batch}

    request_generation: int | None = None
    try:
        request_weight = _estimate_mixed_request_weight(batch, system_prompt)
        if api_scheduler is None:
            raw_results = _translate_mixed_batch_once(
                batch,
                engine=engine,
                target_lang=target_lang,
                system_prompt=system_prompt,
                source_lang=source_lang,
                retry_hint=False,
            )
        else:
            with api_scheduler.slot(request_weight, category=request_category) as lease:
                request_generation = lease.generation
                raw_results = _translate_mixed_batch_once(
                    batch,
                    engine=engine,
                    target_lang=target_lang,
                    system_prompt=system_prompt,
                    source_lang=source_lang,
                    retry_hint=False,
                )
        return _validate_mixed_results(raw_results, target_lang=target_lang, source_lang=source_lang)
    except Exception as exc:  # noqa: BLE001 - fallback controls degradation
        if isinstance(exc, ApiKeyTemporarilyUnavailableError):
            raise
        if api_scheduler is not None:
            decision = handle_api_concurrency_limit(
                exc,
                scheduler=api_scheduler,
                request_generation=request_generation,
                context_label="混合语言",
                error_callback=error_callback,
            )
            if decision is not None:
                stats.record_adaptive_concurrency_decision(decision)
                if should_stop and should_stop():
                    return {text: _uncertain_result(text, note="stopped") for text in batch}
                return _translate_mixed_batch_with_fallback(
                    batch,
                    engine=engine,
                    target_lang=target_lang,
                    system_prompt=system_prompt,
                    source_lang=source_lang,
                    api_scheduler=api_scheduler,
                    request_category=request_category,
                    retry_attempts=retry_attempts,
                    should_stop=should_stop,
                    error_callback=error_callback,
                    stats=stats,
                )

        if len(batch) > 1 and not (should_stop and should_stop()):
            midpoint = max(1, len(batch) // 2)
            stats.retry_count += 1
            if error_callback:
                error_callback(
                    "混合语言批次响应无法解析，已缩小批次重试"
                    f"（{len(batch)} -> {midpoint}+{len(batch) - midpoint}）：{exc}"
                )
            left = _translate_mixed_batch_with_fallback(
                batch[:midpoint],
                engine=engine,
                target_lang=target_lang,
                system_prompt=system_prompt,
                source_lang=source_lang,
                api_scheduler=api_scheduler,
                request_category=request_category,
                retry_attempts=retry_attempts,
                should_stop=should_stop,
                error_callback=error_callback,
                stats=stats,
            )
            right = _translate_mixed_batch_with_fallback(
                batch[midpoint:],
                engine=engine,
                target_lang=target_lang,
                system_prompt=system_prompt,
                source_lang=source_lang,
                api_scheduler=api_scheduler,
                request_category=request_category,
                retry_attempts=retry_attempts,
                should_stop=should_stop,
                error_callback=error_callback,
                stats=stats,
            )
            return {**left, **right}

        stats.failed_count += len(batch)
        if error_callback:
            error_callback(f"混合语言单条解析失败，已保留原文：{str(batch[0])[:40]}... | {exc}")
        return {text: _uncertain_result(text, note=str(exc)) for text in batch}


def _translate_mixed_batch_once(
    batch: list[str],
    *,
    engine: TranslationEngine,
    target_lang: str,
    system_prompt: str,
    source_lang: str,
    retry_hint: bool,
) -> list[MixedLanguageResult]:
    if not _engine_supports_chat(engine):
        raise NotImplementedError(f"{engine.__class__.__name__} 不支持混合语言结构化调用")

    entries = [
        {"id": f"m{index:04d}", "text": text}
        for index, text in enumerate(batch, 1)
    ]
    id_to_source = {entry["id"]: entry["text"] for entry in entries}
    raw = engine.chat(
        _build_mixed_translation_prompt(
            target_lang=target_lang,
            source_lang=source_lang,
            base_prompt=system_prompt,
            retry_hint=retry_hint,
        ),
        json.dumps(entries, ensure_ascii=False),
    )
    payload = _loads_json(raw)
    if not isinstance(payload, list):
        raise ValueError("混合语言响应不是 JSON 数组")

    seen: set[str] = set()
    results: list[MixedLanguageResult] = []
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("混合语言响应项不是对象")
        item_id = str(item.get("id") or "").strip()
        if item_id not in id_to_source:
            raise ValueError(f"混合语言响应包含未知 id：{item_id}")
        if item_id in seen:
            raise ValueError(f"混合语言响应重复 id：{item_id}")
        seen.add(item_id)
        action = str(item.get("action") or "").strip()
        if action not in MIXED_LANGUAGE_ACTIONS:
            raise ValueError(f"混合语言 action 无效：{action}")
        translation = str(item.get("translation") or "").strip()
        if action in {MIXED_ACTION_TRANSLATE, MIXED_ACTION_FOREIGN_NOISE} and not translation:
            raise ValueError(f"混合语言 action={action} 缺少译文")
        results.append(
            MixedLanguageResult(
                source=id_to_source[item_id],
                action=action,
                translation=translation,
                note=str(item.get("note") or "").strip(),
            )
        )

    missing = [entry_id for entry_id in id_to_source if entry_id not in seen]
    if missing:
        raise ValueError(f"混合语言响应缺少 {len(missing)} 条结果")
    return results


def _validate_mixed_results(
    raw_results: list[MixedLanguageResult],
    *,
    target_lang: str,
    source_lang: str,
) -> dict[str, MixedLanguageResult]:
    results: dict[str, MixedLanguageResult] = {}
    for result in raw_results:
        if result.has_translation:
            validation = validate_translation(
                result.source,
                result.translation,
                target_lang=target_lang,
                source_lang=source_lang,
            )
            result.validation = validation
        results[result.source] = result
    return results


def _recover_failed_mixed_results(
    results: dict[str, MixedLanguageResult],
    *,
    engine: TranslationEngine,
    target_lang: str,
    system_prompt: str,
    source_lang: str,
    api_scheduler: WeightedApiScheduler | None,
    retry_attempts: int,
    should_stop,
    error_callback,
    stats: MixedLanguageRunStats,
) -> dict[str, MixedLanguageResult]:
    recovered: dict[str, MixedLanguageResult] = {}
    for source, result in results.items():
        if not _mixed_result_needs_recovery(result):
            recovered[source] = result
            continue
        recovered[source] = _recover_one_mixed_result(
            result,
            engine=engine,
            target_lang=target_lang,
            system_prompt=system_prompt,
            source_lang=source_lang,
            api_scheduler=api_scheduler,
            retry_attempts=retry_attempts,
            should_stop=should_stop,
            error_callback=error_callback,
            stats=stats,
        )
    return recovered


def _mixed_result_needs_recovery(result: MixedLanguageResult) -> bool:
    return result.has_translation and result.validation.is_fail


def _recover_one_mixed_result(
    result: MixedLanguageResult,
    *,
    engine: TranslationEngine,
    target_lang: str,
    system_prompt: str,
    source_lang: str,
    api_scheduler: WeightedApiScheduler | None,
    retry_attempts: int,
    should_stop,
    error_callback,
    stats: MixedLanguageRunStats,
) -> MixedLanguageResult:
    if should_stop and should_stop():
        return _uncertain_result(result.source, note="stopped")

    futures = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures.append(
            executor.submit(
                _retry_one_mixed_source,
                result.source,
                engine=engine,
                target_lang=target_lang,
                system_prompt=system_prompt,
                source_lang=source_lang,
                api_scheduler=api_scheduler,
                retry_attempts=retry_attempts,
                should_stop=should_stop,
                stats=stats,
            )
        )
        if _engine_supports_chat(engine):
            futures.append(
                executor.submit(
                    _semantic_review_mixed_result,
                    result,
                    engine=engine,
                    target_lang=target_lang,
                    source_lang=source_lang,
                    api_scheduler=api_scheduler,
                    stats=stats,
                )
            )
        pending = set(futures)
        fallback: MixedLanguageResult | None = None
        while pending:
            done, pending = wait(tuple(pending), return_when=FIRST_COMPLETED)
            for future in done:
                try:
                    candidate = future.result()
                except Exception as exc:  # noqa: BLE001 - another recovery branch may still succeed
                    if error_callback:
                        error_callback(f"混合语言恢复分支失败：{exc}")
                    continue
                if candidate is None:
                    continue
                if fallback is None:
                    fallback = candidate
                if not _mixed_result_needs_recovery(candidate) and candidate.action != MIXED_ACTION_UNCERTAIN:
                    return candidate

    stats.failed_count += 1
    return fallback if fallback is not None else _uncertain_result(result.source, note="recovery_failed")


def _retry_one_mixed_source(
    source: str,
    *,
    engine: TranslationEngine,
    target_lang: str,
    system_prompt: str,
    source_lang: str,
    api_scheduler: WeightedApiScheduler | None,
    retry_attempts: int,
    should_stop,
    stats: MixedLanguageRunStats,
) -> MixedLanguageResult:
    attempts = max(1, int(retry_attempts or 1))
    last_result = _uncertain_result(source, note="retry_not_run")
    for _ in range(attempts):
        if should_stop and should_stop():
            return _uncertain_result(source, note="stopped")
        stats.retry_count += 1
        request_generation: int | None = None
        try:
            if api_scheduler is None:
                raw_results = _translate_mixed_batch_once(
                    [source],
                    engine=engine,
                    target_lang=target_lang,
                    system_prompt=system_prompt,
                    source_lang=source_lang,
                    retry_hint=True,
                )
            else:
                with api_scheduler.slot(1, category=API_REQUEST_CATEGORY_RECOVERY) as lease:
                    request_generation = lease.generation
                    raw_results = _translate_mixed_batch_once(
                        [source],
                        engine=engine,
                        target_lang=target_lang,
                        system_prompt=system_prompt,
                        source_lang=source_lang,
                        retry_hint=True,
                    )
            result = _validate_mixed_results(
                raw_results,
                target_lang=target_lang,
                source_lang=source_lang,
            ).get(source, _uncertain_result(source, note="retry_missing_result"))
            result.accepted_by = "retry"
            last_result = result
            if not _mixed_result_needs_recovery(result):
                return result
        except Exception as exc:  # noqa: BLE001 - retry loop degrades safely
            if api_scheduler is not None:
                decision = handle_api_concurrency_limit(
                    exc,
                    scheduler=api_scheduler,
                    request_generation=request_generation,
                    context_label="混合语言重试",
                    error_callback=None,
                )
                if decision is not None:
                    stats.record_adaptive_concurrency_decision(decision)
                    continue
            last_result = _uncertain_result(source, note=str(exc))
    return last_result


def _semantic_review_mixed_result(
    result: MixedLanguageResult,
    *,
    engine: TranslationEngine,
    target_lang: str,
    source_lang: str,
    api_scheduler: WeightedApiScheduler | None,
    stats: MixedLanguageRunStats,
) -> MixedLanguageResult | None:
    if not _engine_supports_chat(engine):
        return None
    stats.semantic_check_count += 1
    prompt = _build_mixed_semantic_prompt(target_lang=target_lang, source_lang=source_lang)
    user_payload = json.dumps(
        {
            "source": result.source,
            "action": result.action,
            "translation": result.translation,
            "note": result.note,
        },
        ensure_ascii=False,
    )
    if api_scheduler is None:
        raw = engine.chat(prompt, user_payload)
    else:
        with api_scheduler.slot(1, category=API_REQUEST_CATEGORY_RECOVERY):
            raw = engine.chat(prompt, user_payload)
    payload = _loads_json(raw)
    if not isinstance(payload, dict):
        return None

    verdict = str(payload.get("verdict") or "").strip()
    if verdict not in {"accept", "reject", "uncertain"}:
        return None
    if verdict != "accept":
        return None

    action = str(payload.get("corrected_action") or result.action).strip()
    translation = str(payload.get("corrected_translation") or result.translation).strip()
    if action not in MIXED_LANGUAGE_ACTIONS:
        return None
    if action in {MIXED_ACTION_TRANSLATE, MIXED_ACTION_FOREIGN_NOISE} and not translation:
        return None
    corrected = MixedLanguageResult(
        source=result.source,
        action=action,
        translation=translation,
        note=str(payload.get("reason") or result.note or "").strip(),
        accepted_by="semantic",
    )
    if corrected.has_translation:
        corrected.validation = validate_translation(
            corrected.source,
            corrected.translation,
            target_lang=target_lang,
            source_lang=source_lang,
        )
        if corrected.validation.is_fail and action == MIXED_ACTION_TRANSLATE:
            return None
    stats.semantic_accepted_count += 1
    return corrected


def _build_mixed_translation_prompt(
    *,
    target_lang: str,
    source_lang: str,
    base_prompt: str,
    retry_hint: bool,
) -> str:
    source_lang_name = get_source_lang_display(source_lang)
    target_lang_name = get_target_lang_display(target_lang, include_optional=True)
    retry_block = (
        "\n上一轮结果未通过质量校验。请重新判断 action，并确保 translation 完整翻译源语言主体、保留数字/单位/编号。"
        if retry_hint
        else ""
    )
    return (
        f"{base_prompt}\n\n"
        "你正在处理疑似混合语言的翻译单元。输入是 JSON 数组，每项包含 id 和 text。\n"
        f"源语言：{source_lang_name}。目标语言：{target_lang_name}。\n"
        "请只输出 JSON 数组，不要输出 markdown 代码块、解释文字或额外字段。\n"
        "每个输出项必须包含：id、action、translation、note。\n"
        "action 只能是 existing_bilingual、translate、foreign_noise_suspected、uncertain。\n"
        "判定规则：\n"
        f"1. 只有 text 已经包含“{source_lang_name} + {target_lang_name}”且目标语言片段准确覆盖源语言主体时，才使用 existing_bilingual；translation 留空。\n"
        "2. 外文片段若只是专名、品牌、机构名、地点名、型号、标准号、单位或缩写，不得判为 existing_bilingual；请使用 translate，并在 translation 中保留这些片段。\n"
        f"3. 外文片段若不是{target_lang_name}，不得判为 existing_bilingual；请使用 translate 或 uncertain。\n"
        "4. 外文片段疑似无意义误输入、键盘误敲或与上下文无关时，使用 foreign_noise_suspected，并返回源语言主体的目标语言译文。\n"
        "5. translate 和 foreign_noise_suspected 必须返回非空 translation；existing_bilingual 和 uncertain 的 translation 必须为空字符串。\n"
        "6. 必须保留原文中的数字、日期、单位、编号、轴线、规格型号和工程专名，不得添加解释或多版本。\n"
        f"{retry_block}"
    ).strip()


def _build_mixed_semantic_prompt(*, target_lang: str, source_lang: str) -> str:
    source_lang_name = get_source_lang_display(source_lang)
    target_lang_name = get_target_lang_display(target_lang, include_optional=True)
    return (
        "你是混合语言翻译结果校验器。输入为 JSON 对象，包含 source、action、translation、note。\n"
        f"源语言：{source_lang_name}。目标语言：{target_lang_name}。\n"
        "请只输出 JSON 对象，不要输出 markdown。\n"
        "字段：verdict（accept/reject/uncertain）、reason、corrected_action、corrected_translation。\n"
        "只判断 action 与 translation 是否合理，不要重新自由发挥。\n"
        "如果 source 已含完整目标语言译文，可接受 existing_bilingual。\n"
        "如果 translation 完整表达源语言主体且保留数字/单位/编号，可接受 translate。\n"
        "如果外文片段明显是错误输入且 translation 已翻译源语言主体，可接受 foreign_noise_suspected。\n"
        "只有高置信时才修正 corrected_action/corrected_translation；拿不准返回 uncertain。"
    )


def _loads_json(raw: str):
    cleaned = strip_markdown_json(str(raw or ""))
    return json.loads(cleaned)


def _estimate_mixed_request_weight(texts: list[str], system_prompt: str = "") -> int:
    input_chars = sum(len(str(text or "")) for text in texts)
    prompt_chars = min(len(str(system_prompt or "")), _API_WEIGHT_PROMPT_CHAR_CAP)
    estimated_output_chars = int(math.ceil(input_chars * _API_WEIGHT_OUTPUT_MULTIPLIER))
    total_chars = max(1, input_chars + prompt_chars + estimated_output_chars)
    return max(1, int(math.ceil(total_chars / _API_WEIGHT_CHARS_PER_SLOT)))


def _engine_supports_chat(engine) -> bool:
    chat = getattr(type(engine), "chat", None)
    return chat is not None and chat is not TranslationEngine.chat


def _uncertain_result(source: str, *, note: str = "") -> MixedLanguageResult:
    return MixedLanguageResult(
        source=source,
        action=MIXED_ACTION_UNCERTAIN,
        translation="",
        note=note,
    )
