"""翻译引擎外层：每批附上本批出现的记忆库词条，翻完核对，未采用的重译。

:class:`TermAwareEngine` 包住真正的引擎，只改两件事：

1. ``translate_batch`` / ``translate_batch_with_sources`` 发出前，扫出这批文字里的
   词条（:class:`core.tm_terms.TermIndex`），把「本批文字中出现的记忆库词条」附在
   system prompt 末尾。涉及的词条超过上限时把这批拆小，词条一条不丢。
2. 收到译文后核对「始终采用」的词条；没采用的那几条带上改正说明重译，最多
   ``retry_attempts`` 次。仍未采用的保留模型译文，记进 :class:`TermRunStats`，
   由任务流程在输出文件里标记需复核、不存进记忆库。

调度、拆批、限流和失败重发仍由 ``engine_dispatcher`` / ``word_batching`` 负责：
包装器只是它们眼里的一个普通引擎。
"""

from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from loguru import logger

from core.language_preflight import TranslationLanguageResult
from core.tm_terms import (
    DEFAULT_BATCH_BUDGET,
    USAGE_ALWAYS,
    USAGE_SCENE,
    TermHit,
    TermIndex,
    build_correction_block,
    build_prompt_block,
    chosen_candidate,
    split_by_budget,
    term_applied,
)
from engines.base_engine import TranslationEngine, get_target_lang_name

DEFAULT_TERM_RETRY_ATTEMPTS = 2
MAX_TERM_RETRY_ATTEMPTS = 5


def excerpt(text: str, limit: int = 80) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _append_block(system_prompt: str, block: str) -> str:
    if not block:
        return system_prompt
    prompt = str(system_prompt or "").rstrip()
    return f"{prompt}\n\n{block}" if prompt else block


@dataclass
class TextTermOutcome:
    """一段文字的词条结果（同一段被调度重发时以最后一次为准）。"""

    text: str
    hits: tuple[TermHit, ...]
    translation: str
    applied_first: tuple[str, ...] = ()
    applied_after_retry: tuple[str, ...] = ()
    missed: tuple[TermHit, ...] = ()
    retries: int = 0


@dataclass
class TermRunStats:
    """一次任务里词条的采用情况，线程安全。"""

    target_lang: str = ""
    _outcomes: dict[str, TextTermOutcome] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, outcome: TextTermOutcome) -> None:
        with self._lock:
            self._outcomes[outcome.text] = outcome

    def outcomes(self) -> list[TextTermOutcome]:
        with self._lock:
            return list(self._outcomes.values())

    def not_applied(self) -> list[TextTermOutcome]:
        return [outcome for outcome in self.outcomes() if outcome.missed]

    def not_applied_texts(self) -> set[str]:
        return {outcome.text for outcome in self.not_applied()}

    def entry_uses(self) -> Counter[int]:
        """采用了的词条各用了几处（未采用、按常规翻译的不算）。"""
        uses: Counter[int] = Counter()
        for outcome in self.outcomes():
            missed = {hit.key for hit in outcome.missed}
            for hit in outcome.hits:
                if hit.usage == USAGE_ALWAYS:
                    if hit.key not in missed:
                        for entry_id in hit.candidates[0].entry_ids if hit.candidates else ():
                            uses[entry_id] += 1
                    continue
                chosen = chosen_candidate(hit, outcome.translation, target_lang=self.target_lang)
                if chosen is not None:
                    for entry_id in chosen.entry_ids:
                        uses[entry_id] += 1
        return uses

    def summary(self) -> dict[str, object]:
        outcomes = self.outcomes()
        always_hits = scene_hits = applied_first = applied_retry = 0
        scene: dict[str, dict[str, object]] = {}
        for outcome in outcomes:
            applied_first += len(outcome.applied_first)
            applied_retry += len(outcome.applied_after_retry)
            for hit in outcome.hits:
                if hit.usage == USAGE_ALWAYS:
                    always_hits += 1
                    continue
                scene_hits += 1
                chosen = chosen_candidate(hit, outcome.translation, target_lang=self.target_lang)
                item = scene.setdefault(
                    hit.source,
                    {
                        "term": hit.source,
                        "candidates": [
                            {"target": cand.target, "note": cand.note} for cand in hit.candidates
                        ],
                        "chosen": Counter(),
                        "count": 0,
                    },
                )
                item["count"] = int(item["count"]) + 1
                label = chosen.target if chosen is not None else (excerpt(outcome.translation) if hit.whole else "")
                item["chosen"][label] += 1  # type: ignore[index]
        scene_choices = []
        for item in scene.values():
            chosen_counter: Counter[str] = item.pop("chosen")  # type: ignore[assignment]
            item["chosen"] = [
                {"target": target, "count": count} for target, count in chosen_counter.most_common()
            ]
            scene_choices.append(item)
        return {
            "injected_texts": len(outcomes),
            "always_hits": always_hits,
            "scene_hits": scene_hits,
            "applied_first": applied_first,
            "applied_after_retry": applied_retry,
            # 键名避开 text / source / translation：任务中心存结果时按这几个键名剔除
            # 文档正文，这里只留摘录（与 Word 复核清单的 snippet 同一口径）。
            "not_applied": [
                {
                    "snippet": excerpt(outcome.text),
                    "output_snippet": excerpt(outcome.translation),
                    "retries": outcome.retries,
                    "terms": [
                        {"term": hit.source, "expected": hit.expected} for hit in outcome.missed
                    ],
                }
                for outcome in outcomes
                if outcome.missed
            ],
            "scene_choices": scene_choices,
        }


class TermAwareEngine(TranslationEngine):
    """在批次层面注入词条、核对并重译的引擎包装器。"""

    # engine_supports_chat 等能力判断看里面那个真正的引擎。
    _delegates_capabilities = True

    def __init__(
        self,
        engine: TranslationEngine,
        index: TermIndex,
        *,
        target_lang: str,
        retry_attempts: int = DEFAULT_TERM_RETRY_ATTEMPTS,
        batch_budget: int = DEFAULT_BATCH_BUDGET,
        include_scene: bool = True,
        stats: TermRunStats | None = None,
        log: Callable[[str, str], None] | None = None,
    ) -> None:
        self._engine = engine
        self._index = index
        self._target_lang = target_lang
        self._target_name = get_target_lang_name(target_lang)
        self._retry_attempts = max(0, min(MAX_TERM_RETRY_ATTEMPTS, int(retry_attempts)))
        self._batch_budget = max(1, int(batch_budget or DEFAULT_BATCH_BUDGET))
        self._include_scene = include_scene
        self.stats = stats if stats is not None else TermRunStats(target_lang=target_lang)
        self._log = log

    @property
    def inner(self) -> TranslationEngine:
        return self.__dict__["_engine"]

    @property
    def engine_name(self) -> str:
        return getattr(self.inner, "engine_name", "")

    def __getattr__(self, item: str):
        return getattr(self.__dict__["_engine"], item)

    def chat(self, system: str, user: str) -> str:
        return self.inner.chat(system, user)

    # ── 批次 ──────────────────────────────────────────────────────────────

    def translate_batch(
        self,
        texts: list[str],
        target_lang: str,
        system_prompt: str,
        source_lang: str = "zh",
    ) -> dict[str, str]:
        hits = self._scan(texts)
        if not any(hits):
            return self.inner.translate_batch(texts, target_lang, system_prompt, source_lang=source_lang)
        # 批次在线程池里并发跑：这里的状态只能放在局部变量里。
        merged: dict[str, str] = {}

        def request(batch: list[str], prompt: str) -> list[str | None]:
            result = self.inner.translate_batch(batch, target_lang, prompt, source_lang=source_lang)
            return [result.get(text) for text in batch]

        for group in split_by_budget(texts, hits, self._batch_budget):
            batch = [texts[i] for i in group]
            first = self.inner.translate_batch(
                batch,
                target_lang,
                _append_block(system_prompt, self._block([hits[i] for i in group])),
                source_lang=source_lang,
            )
            merged.update(first)
            final = self._verify_and_retry(
                batch,
                [hits[i] for i in group],
                [first.get(text) if isinstance(first.get(text), str) else None for text in batch],
                request,
                system_prompt,
            )
            for text, value in zip(batch, final):
                if value is not None:
                    merged[text] = value
        return merged

    def translate_batch_with_sources(
        self,
        texts: list[str],
        target_lang: str,
        system_prompt: str,
        source_lang: str = "auto",
    ) -> list[TranslationLanguageResult]:
        hits = self._scan(texts)
        if not any(hits):
            return self.inner.translate_batch_with_sources(
                texts, target_lang, system_prompt, source_lang=source_lang
            )
        items: list[TranslationLanguageResult | None] = [None] * len(texts)

        def request(batch: list[str], prompt: str) -> list[TranslationLanguageResult]:
            result = self.inner.translate_batch_with_sources(batch, target_lang, prompt, source_lang=source_lang)
            if len(result) != len(batch):
                raise ValueError(f"返回条数不足：输入 {len(batch)}，返回 {len(result)}")
            return list(result)

        for group in split_by_budget(texts, hits, self._batch_budget):
            batch = [texts[i] for i in group]
            first = request(batch, _append_block(system_prompt, self._block([hits[i] for i in group])))
            for local, index in enumerate(group):
                items[index] = first[local]
            retried: dict[str, TranslationLanguageResult] = {}

            def retry_request(retry_batch: list[str], prompt: str) -> list[str | None]:
                result = request(retry_batch, prompt)
                for item in result:
                    retried[item.source_text] = item
                return [item.translation for item in result]

            final = self._verify_and_retry(
                batch,
                [hits[i] for i in group],
                [item.translation for item in first],
                retry_request,
                system_prompt,
            )
            for local, index in enumerate(group):
                current = items[index]
                if current is None or final[local] == current.translation:
                    continue
                replacement = retried.get(batch[local])
                if replacement is not None and replacement.translation == final[local]:
                    items[index] = replacement
        return [item for item in items if item is not None]

    # ── 内部 ──────────────────────────────────────────────────────────────

    def _scan(self, texts: Sequence[str]) -> list[list[TermHit]]:
        return [self._index.scan(text, include_scene=self._include_scene) for text in texts]

    def _block(self, hits_per_text: Sequence[Sequence[TermHit]]) -> str:
        return build_prompt_block(hits_per_text, self._target_name)

    def _missed(self, hits: Sequence[TermHit], translation: str | None) -> list[TermHit]:
        if not translation:
            return []
        return [
            hit
            for hit in hits
            if hit.usage == USAGE_ALWAYS
            and term_applied(
                translation,
                hit.expected,
                source=hit.source,
                target_lang=self._target_lang,
                whole=hit.whole,
            )
            is False
        ]

    def _verify_and_retry(
        self,
        batch: list[str],
        hits: list[list[TermHit]],
        translations: list[str | None],
        request: Callable[[list[str], str], list[str | None]],
        system_prompt: str,
    ) -> list[str | None]:
        final = list(translations)
        always_keys = [
            tuple(hit.key for hit in text_hits if hit.usage == USAGE_ALWAYS) for text_hits in hits
        ]
        pending: dict[int, list[TermHit]] = {}
        for index, text_hits in enumerate(hits):
            missed = self._missed(text_hits, final[index])
            if missed:
                pending[index] = missed
        first_missed = {index: {hit.key for hit in missed} for index, missed in pending.items()}
        retries: dict[int, int] = {}
        attempt = 0
        while pending and attempt < self._retry_attempts:
            attempt += 1
            order = sorted(pending)
            retry_batch = [batch[i] for i in order]
            block = self._block([hits[i] for i in order])
            correction = build_correction_block(
                [(n + 1, final[i] or "", pending[i]) for n, i in enumerate(order)]
            )
            prompt = _append_block(_append_block(system_prompt, block), correction)
            try:
                values = request(retry_batch, prompt)
            except Exception as exc:  # noqa: BLE001 - keep the first translation, mark for review
                logger.warning(f"设定译法重译失败，保留首次译文：{exc!r}")
                break
            for n, index in enumerate(order):
                retries[index] = attempt
                candidate = values[n] if n < len(values) else None
                if not isinstance(candidate, str) or not candidate.strip():
                    continue
                missed = self._missed(hits[index], candidate)
                if not missed:
                    final[index] = candidate
                    del pending[index]
        for index, text in enumerate(batch):
            if not hits[index] or final[index] is None:
                continue
            missed_now = pending.get(index, [])
            missed_keys = {hit.key for hit in missed_now}
            retried_keys = first_missed.get(index, set()) - missed_keys
            first_keys = [key for key in always_keys[index] if key not in first_missed.get(index, set())]
            self.stats.record(
                TextTermOutcome(
                    text=text,
                    hits=tuple(hits[index]),
                    translation=final[index] or "",
                    applied_first=tuple(first_keys),
                    applied_after_retry=tuple(sorted(retried_keys)),
                    missed=tuple(missed_now),
                    retries=retries.get(index, 0),
                )
            )
        return final


def scene_usage_label(usage: str) -> str:
    return "始终采用" if usage == USAGE_ALWAYS else ("按场景选用" if usage == USAGE_SCENE else "仅整段复用")
