"""Word-specific batching and retry helpers."""

from __future__ import annotations

import re
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from math import ceil
from typing import Callable

from loguru import logger

from core.api_concurrency_control import (
    ApiKeyTemporarilyUnavailableError,
    handle_api_concurrency_limit,
)
from core.api_scheduler import (
    API_CONCURRENCY_ACTION_REDUCED,
    API_REQUEST_CATEGORY_NORMAL,
    ApiConcurrencyLimitDecision,
    WeightedApiScheduler,
)
from core.translation_filter import (
    VALIDATION_PROFILE_STRICT,
    validate_translation,
)
from core.engine_dispatcher import is_local_engine_name
from core.translation_protocol import should_apply_quality_filter
from engines.base_engine import TranslationEngine
from settings import WordBatchSettings

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_SPLIT_AFTER_CHARS = set("\n。！？；;")
_SOFT_SPLIT_CHARS = set("，,、")

ProgressCallback = Callable[[int, int], None]
ErrorCallback = Callable[[str], None]
CandidateCallback = Callable[[str, str], None]
DrainedCallback = Callable[[], None]

_API_WEIGHT_CHARS_PER_SLOT = 4000
_API_WEIGHT_PROMPT_CHAR_CAP = 900
_API_WEIGHT_OUTPUT_MULTIPLIER = 1.15


@dataclass(frozen=True)
class WordTranslationUnit:
    source: str
    text: str
    part_index: int = 0
    part_total: int = 1

    @property
    def is_split_part(self) -> bool:
        return self.part_total > 1


@dataclass
class WordBatchRunStats:
    original_count: int = 0
    unit_count: int = 0
    batch_count: int = 0
    split_source_count: int = 0
    retry_count: int = 0
    failed_unit_count: int = 0
    adaptive_concurrency_reductions: int = 0
    adaptive_lowest_concurrency: int = 0

    def record_adaptive_concurrency_decision(
        self,
        decision: ApiConcurrencyLimitDecision,
    ) -> None:
        if decision.action != API_CONCURRENCY_ACTION_REDUCED:
            return
        self.adaptive_concurrency_reductions += 1
        if self.adaptive_lowest_concurrency <= 0:
            self.adaptive_lowest_concurrency = decision.current_capacity
        else:
            self.adaptive_lowest_concurrency = min(
                self.adaptive_lowest_concurrency,
                decision.current_capacity,
            )


class WordBatchIntegrityError(RuntimeError):
    """Raised when a Word batch response cannot be mapped back to every input."""


def translate_word_texts(
    texts: list[str],
    engine: TranslationEngine,
    target_lang: str,
    system_prompt: str,
    batch_settings: WordBatchSettings,
    concurrency: int,
    progress_callback: ProgressCallback | None = None,
    error_callback: ErrorCallback | None = None,
    should_stop=None,
    source_lang: str = "zh",
    stats: WordBatchRunStats | None = None,
    quality_profile: str | None = VALIDATION_PROFILE_STRICT,
    api_scheduler: WeightedApiScheduler | None = None,
    request_category: str = API_REQUEST_CATEGORY_NORMAL,
    candidate_callback: CandidateCallback | None = None,
    drained_callback: DrainedCallback | None = None,
) -> dict[str, str]:
    """Translate Word paragraphs using character-budgeted batches with fallback retries."""
    if not texts:
        return {}

    run_stats = stats or WordBatchRunStats()
    run_stats.original_count = len(texts)

    units = _build_translation_units(texts, batch_settings)
    run_stats.unit_count = len(units)
    run_stats.split_source_count = len({unit.source for unit in units if unit.is_split_part})

    batches = _build_word_batches(units, batch_settings)
    run_stats.batch_count = len(batches)
    total_units = max(len(units), 1)
    done_units = 0
    done_lock = threading.Lock()
    unit_results: dict[tuple[str, int], str] = {}
    drained_notified = False

    def _record_progress(completed: int) -> None:
        nonlocal done_units
        with done_lock:
            done_units += completed
            if progress_callback:
                progress_callback(min(done_units, total_units), total_units)

    def _notify_drained() -> None:
        nonlocal drained_notified
        if drained_notified:
            return
        drained_notified = True
        if drained_callback:
            drained_callback()

    def _translate_one_batch(batch: list[WordTranslationUnit]) -> dict[tuple[str, int], str]:
        partial = _translate_units_with_fallback(
            batch,
            engine=engine,
            target_lang=target_lang,
            system_prompt=system_prompt,
            source_lang=source_lang,
            quality_profile=quality_profile,
            api_scheduler=api_scheduler,
            request_category=request_category,
            candidate_callback=candidate_callback,
            should_stop=should_stop,
            error_callback=error_callback,
            stats=run_stats,
        )
        _record_progress(len(batch))
        return partial

    max_workers = 1 if is_local_engine_name(engine.engine_name) else max(1, int(concurrency))
    max_workers = min(max_workers, len(batches))

    if max_workers <= 1:
        for batch in batches:
            if should_stop and should_stop():
                logger.info("Word 翻译任务收到停止信号，停止提交后续批次。")
                break
            unit_results.update(_translate_one_batch(batch))
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
                    unit_results.update(future.result())
                    if not (should_stop and should_stop()):
                        _submit_next()

    return _merge_unit_results(texts, units, unit_results, target_lang=target_lang)


def _build_translation_units(
    texts: list[str],
    batch_settings: WordBatchSettings,
) -> list[WordTranslationUnit]:
    units: list[WordTranslationUnit] = []
    for source in texts:
        parts = _split_long_word_text(
            source,
            split_threshold=batch_settings.split_paragraph_chars,
            part_char_budget=batch_settings.max_chars_per_batch,
        )
        if len(parts) <= 1:
            units.append(WordTranslationUnit(source=source, text=source))
            continue
        for index, part in enumerate(parts):
            units.append(
                WordTranslationUnit(
                    source=source,
                    text=part,
                    part_index=index,
                    part_total=len(parts),
                )
            )
    return units


def _build_word_batches(
    units: list[WordTranslationUnit],
    batch_settings: WordBatchSettings,
) -> list[list[WordTranslationUnit]]:
    batches: list[list[WordTranslationUnit]] = []
    current: list[WordTranslationUnit] = []
    current_chars = 0
    max_items = max(1, batch_settings.max_paragraphs_per_batch)
    max_chars = max(1, batch_settings.max_chars_per_batch)
    single_threshold = max(max_chars // 2, min(max_chars, 1200))

    def flush() -> None:
        nonlocal current, current_chars
        if current:
            batches.append(current)
        current = []
        current_chars = 0

    for unit in units:
        unit_chars = len(unit.text)
        if unit_chars >= single_threshold:
            flush()
            batches.append([unit])
            continue

        would_exceed_items = len(current) >= max_items
        would_exceed_chars = current and (current_chars + unit_chars > max_chars)
        if would_exceed_items or would_exceed_chars:
            flush()

        current.append(unit)
        current_chars += unit_chars

    flush()
    return batches


def _translate_units_with_fallback(
    units: list[WordTranslationUnit],
    *,
    engine: TranslationEngine,
    target_lang: str,
    system_prompt: str,
    source_lang: str,
    quality_profile: str | None,
    api_scheduler: WeightedApiScheduler | None,
    request_category: str,
    candidate_callback: CandidateCallback | None,
    should_stop,
    error_callback: ErrorCallback | None,
    stats: WordBatchRunStats,
) -> dict[tuple[str, int], str]:
    if not units:
        return {}
    if should_stop and should_stop():
        return {}

    request_generation: int | None = None
    try:
        payloads = _unique_unit_texts(units)
        request_weight = estimate_api_request_weight(payloads, system_prompt)
        if api_scheduler is None:
            raw_results = engine.translate_batch(
                payloads,
                target_lang,
                system_prompt,
                source_lang=source_lang,
            )
        else:
            with api_scheduler.slot(request_weight, category=request_category) as lease:
                request_generation = lease.generation
                raw_results = engine.translate_batch(
                    payloads,
                    target_lang,
                    system_prompt,
                    source_lang=source_lang,
                )
        _validate_batch_integrity(payloads, raw_results)
        _notify_whole_paragraph_candidates(units, raw_results, candidate_callback)
        _apply_word_quality_filter(
            raw_results,
            target_lang,
            source_lang=source_lang,
            quality_profile=quality_profile,
        )
        return {
            (unit.source, unit.part_index): raw_results.get(unit.text, unit.text)
            for unit in units
        }
    except Exception as exc:  # noqa: BLE001 - fallback decides how to recover the batch
        if isinstance(exc, ApiKeyTemporarilyUnavailableError):
            raise
        if api_scheduler is not None and not is_local_engine_name(engine.engine_name):
            decision = handle_api_concurrency_limit(
                exc,
                scheduler=api_scheduler,
                request_generation=request_generation,
                context_label="Word",
                error_callback=error_callback,
            )
            if decision is not None:
                stats.record_adaptive_concurrency_decision(decision)
                if should_stop and should_stop():
                    return {}
                return _translate_units_with_fallback(
                    units,
                    engine=engine,
                    target_lang=target_lang,
                    system_prompt=system_prompt,
                    source_lang=source_lang,
                    quality_profile=quality_profile,
                    api_scheduler=api_scheduler,
                    request_category=request_category,
                    candidate_callback=candidate_callback,
                    should_stop=should_stop,
                    error_callback=error_callback,
                    stats=stats,
                )

        if len(units) > 1:
            stats.retry_count += 1
            midpoint = max(1, len(units) // 2)
            if error_callback:
                error_callback(
                    f"Word 批次响应不完整，已缩小批次重试（{len(units)} -> {midpoint}+{len(units) - midpoint}）：{exc}"
                )
            left = _translate_units_with_fallback(
                units[:midpoint],
                engine=engine,
                target_lang=target_lang,
                system_prompt=system_prompt,
                source_lang=source_lang,
                quality_profile=quality_profile,
                api_scheduler=api_scheduler,
                request_category=request_category,
                candidate_callback=candidate_callback,
                should_stop=should_stop,
                error_callback=error_callback,
                stats=stats,
            )
            right = _translate_units_with_fallback(
                units[midpoint:],
                engine=engine,
                target_lang=target_lang,
                system_prompt=system_prompt,
                source_lang=source_lang,
                quality_profile=quality_profile,
                api_scheduler=api_scheduler,
                request_category=request_category,
                candidate_callback=candidate_callback,
                should_stop=should_stop,
                error_callback=error_callback,
                stats=stats,
            )
            return {**left, **right}

        stats.failed_unit_count += len(units)
        unit = units[0]
        if error_callback:
            error_callback(f"Word 单段翻译仍失败，已暂时保留原文：{unit.text[:40]}... | {exc}")
        return {(unit.source, unit.part_index): unit.text}


def _unique_unit_texts(units: list[WordTranslationUnit]) -> list[str]:
    seen: set[str] = set()
    payloads: list[str] = []
    for unit in units:
        if unit.text in seen:
            continue
        seen.add(unit.text)
        payloads.append(unit.text)
    return payloads


def estimate_api_request_weight(
    texts: list[str],
    system_prompt: str = "",
    *,
    chars_per_slot: int = _API_WEIGHT_CHARS_PER_SLOT,
) -> int:
    """Estimate API request weight for the shared weighted scheduler."""
    input_chars = sum(len(str(text or "")) for text in texts)
    prompt_chars = min(len(str(system_prompt or "")), _API_WEIGHT_PROMPT_CHAR_CAP)
    estimated_output_chars = int(ceil(input_chars * _API_WEIGHT_OUTPUT_MULTIPLIER))
    total_chars = max(1, input_chars + prompt_chars + estimated_output_chars)
    return max(1, int(ceil(total_chars / max(1, int(chars_per_slot)))))


def _notify_whole_paragraph_candidates(
    units: list[WordTranslationUnit],
    raw_results: dict[str, str],
    candidate_callback: CandidateCallback | None,
) -> None:
    if candidate_callback is None:
        return
    for unit in units:
        if unit.is_split_part:
            continue
        if unit.text not in raw_results:
            continue
        try:
            candidate_callback(unit.source, raw_results[unit.text])
        except Exception as exc:  # noqa: BLE001 - callbacks must not break translation
            logger.warning(f"Word 候选译文回调失败：{exc}")


def _validate_batch_integrity(payloads: list[str], results: dict[str, str]) -> None:
    missing = [text for text in payloads if text not in results]
    if missing:
        raise WordBatchIntegrityError(f"缺少 {len(missing)} 条译文")
    if len(results) < len(payloads):
        raise WordBatchIntegrityError(
            f"返回条数不足：输入 {len(payloads)} 条，返回 {len(results)} 条"
        )


def _apply_word_quality_filter(
    results: dict[str, str],
    target_lang: str,
    *,
    source_lang: str,
    quality_profile: str | None = VALIDATION_PROFILE_STRICT,
) -> None:
    if not quality_profile:
        return
    reset_count = 0
    for source in list(results):
        translated = results[source]
        if not should_apply_quality_filter(translated):
            continue
        validation = validate_translation(
            source,
            translated,
            target_lang=target_lang,
            source_lang=source_lang,
            profile=quality_profile,
        )
        if validation.is_fail:
            results[source] = source
            reset_count += 1
    if reset_count:
        logger.warning(f"因 Word 质量校验未通过，已强制保留 {reset_count} 条原文")


def _merge_unit_results(
    originals: list[str],
    units: list[WordTranslationUnit],
    unit_results: dict[tuple[str, int], str],
    *,
    target_lang: str,
) -> dict[str, str]:
    units_by_source: dict[str, list[WordTranslationUnit]] = {}
    for unit in units:
        units_by_source.setdefault(unit.source, []).append(unit)

    merged: dict[str, str] = {}
    for source in originals:
        source_units = sorted(
            units_by_source.get(source, [WordTranslationUnit(source=source, text=source)]),
            key=lambda unit: unit.part_index,
        )
        translated_parts = [
            str(unit_results.get((unit.source, unit.part_index), unit.text)).strip()
            for unit in source_units
        ]
        if len(source_units) == 1:
            merged[source] = translated_parts[0] if translated_parts else source
        else:
            merged[source] = _join_translated_parts(translated_parts, target_lang=target_lang)
    return merged


def _join_translated_parts(parts: list[str], *, target_lang: str) -> str:
    non_empty = [part for part in parts if part]
    if target_lang == "zh" or any(_CJK_RE.search(part) for part in non_empty):
        return "".join(non_empty)
    return " ".join(non_empty)


def _split_long_word_text(
    text: str,
    *,
    split_threshold: int,
    part_char_budget: int,
) -> list[str]:
    normalized = str(text or "").strip()
    if len(normalized) <= split_threshold:
        return [normalized]

    sentences = _split_into_safe_sentences(normalized)
    return _pack_sentences(sentences, max(1, part_char_budget))


def _split_into_safe_sentences(text: str) -> list[str]:
    sentences: list[str] = []
    buffer: list[str] = []
    length = len(text)

    for index, char in enumerate(text):
        buffer.append(char)
        should_split = char in _SPLIT_AFTER_CHARS
        if char in ".!?":
            previous_char = text[index - 1] if index > 0 else ""
            next_char = text[index + 1] if index + 1 < length else ""
            is_decimal_dot = (
                char == "."
                and previous_char.isdigit()
                and next_char.isdigit()
            )
            should_split = not is_decimal_dot and (not next_char or next_char.isspace())

        if should_split:
            _append_sentence(sentences, buffer)

    _append_sentence(sentences, buffer)
    return sentences or [text]


def _pack_sentences(sentences: list[str], part_char_budget: int) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    current_len = 0

    def flush() -> None:
        nonlocal current, current_len
        if current:
            parts.append("".join(current).strip())
        current = []
        current_len = 0

    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(sentence) > part_char_budget:
            flush()
            parts.extend(_split_oversized_sentence(sentence, part_char_budget))
            continue
        if current and current_len + len(sentence) > part_char_budget:
            flush()
        current.append(sentence)
        current_len += len(sentence)

    flush()
    return [part for part in parts if part]


def _split_oversized_sentence(sentence: str, part_char_budget: int) -> list[str]:
    chunks = _split_by_delimiters(sentence, part_char_budget, _SOFT_SPLIT_CHARS)
    if chunks:
        return chunks
    chunks = _split_by_delimiters(sentence, part_char_budget, {" "})
    if chunks:
        return chunks
    return [
        sentence[index: index + part_char_budget].strip()
        for index in range(0, len(sentence), part_char_budget)
        if sentence[index: index + part_char_budget].strip()
    ]


def _split_by_delimiters(
    text: str,
    part_char_budget: int,
    delimiters: set[str],
) -> list[str]:
    chunks: list[str] = []
    buffer: list[str] = []
    for char in text:
        buffer.append(char)
        if char in delimiters and len(buffer) >= part_char_budget // 2:
            _append_sentence(chunks, buffer)
    _append_sentence(chunks, buffer)
    if len(chunks) <= 1 or any(len(chunk) > part_char_budget for chunk in chunks):
        return []
    return chunks


def _append_sentence(target: list[str], buffer: list[str]) -> None:
    if not buffer:
        return
    value = "".join(buffer).strip()
    buffer.clear()
    if value:
        target.append(value)
