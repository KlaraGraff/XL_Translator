"""一次 Excel / Word 任务里的记忆库词条：整段查找、批次包装、自动保存和收尾统计。

任务流程只和这里打交道：

1. :meth:`TaskTermSession.resolve` 代替以前的「整段查记忆库 + 短词只认用户定的」，
   返回可以直接套用的整段译文；
2. :meth:`TaskTermSession.wrap` 把翻译引擎包一层，批次里附上本批出现的词条，
   翻完核对（见 :mod:`core.term_engine`）；
3. :meth:`TaskTermSession.auto_save_short_words` 在「自动识别场景」下把短词在新场景
   里的译法存成带备注的新词条；
4. :meth:`TaskTermSession.finish` 记下词条使用次数，返回任务详情里「记忆库」一节。
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from collections.abc import Callable, Iterable, Mapping

from loguru import logger

from core.document_profile import is_context_sensitive
from core.term_engine import TermAwareEngine, TermRunStats
from core.tm_terms import (
    USAGE_ALWAYS,
    USAGE_SCENE,
    USAGE_SEGMENT,
    TermIndex,
    WholeSegmentResolution,
    whole_key,
)
from engines.base_engine import TranslationEngine, engine_supports_chat, strip_markdown_json

MAX_AUTO_SAVED_PER_TASK = 20
_NOTE_MAX_CHARS = 40

_SCENE_NOTE_SYSTEM = (
    "你是翻译记忆库的整理助手。下面给出一份文档的说明，以及这份文档里几个短词和它们本次的译法。"
    "请为每个短词写一句不超过 15 个字的适用场景说明，说明这个译法在什么场景下成立"
    "（例如「防火门的耐火等级」「配电回路编号」），供以后翻译其他文档时判断是否沿用。"
    "只输出一个 JSON 对象，键是短词原文，值是说明，不要输出其他内容。"
)


def _first_sentence(text: str) -> str:
    value = " ".join(str(text or "").split())
    for mark in ("。", "；", ";", "\n", ". "):
        if mark in value:
            value = value.split(mark, 1)[0]
            break
    return value[:_NOTE_MAX_CHARS].strip()


class TaskTermSession:
    def __init__(
        self,
        tm_settings,
        *,
        target_lang: str,
        smart_mode: bool,
        file_paths: Iterable[object] = (),
        log: Callable[[str, str], None] | None = None,
        task_id: str = "",
    ) -> None:
        self._tm = tm_settings
        self.target_lang = target_lang
        self.smart_mode = bool(smart_mode)
        self._file_paths = list(file_paths)
        self._log = log or (lambda _level, _message: None)
        self._task_id = task_id
        self.index: TermIndex | None = None
        self.whole = WholeSegmentResolution()
        self.stats = TermRunStats(target_lang=target_lang)
        self.profile = ""
        self.auto_saved: list[dict[str, object]] = []
        self._text_pairs: dict[str, set[str]] = {}

    # ── 整段 ──────────────────────────────────────────────────────────────

    def resolve(self, text_pairs: Mapping[str, Iterable[str]], *, profile: str = "") -> dict[str, str]:
        """整段查记忆库：返回直接套用的 {原文: 译文}，其余交给模型。"""
        self._text_pairs = {text: set(pairs) for text, pairs in text_pairs.items()}
        self.profile = str(profile or "").strip()
        all_pairs: set[str] = set()
        for pairs in self._text_pairs.values():
            all_pairs |= pairs
        from core import tm_manager

        try:
            self.index = TermIndex.load(
                self.target_lang,
                lang_pairs=all_pairs,
                file_paths=self._file_paths,
                apply_in_sentences=bool(getattr(self._tm, "apply_in_sentences", True)),
            )
        except sqlite3.Error as exc:
            logger.warning(f"记忆库词条表读取失败，本次只整段复用：{exc!r}")
            self._log("WARN", "记忆库词条读取失败，本次仅整段复用已有译文，不在句中采用设定译法")
            self.index = TermIndex([])
        self.whole = self.index.resolve_whole(self._text_pairs, has_profile=bool(self.profile))
        self._add_plain_lookup_hits(tm_manager.lookup_batch)
        if self.whole.scene_texts:
            self._log(
                "INFO",
                f"记忆库中有 {len(self.whole.scene_texts)} 处文字对应按场景选用的词条，"
                "将随批附上候选译法，由模型结合文档说明选用",
            )
        if self.index.in_sentence_term_count and self.index.apply_in_sentences:
            self._log(
                "INFO",
                f"记忆库：{self.index.in_sentence_term_count} 个词条将在句中出现时采用，翻译完成后核对",
            )
        return dict(self.whole.hits)

    def _add_plain_lookup_hits(self, lookup: Callable[[list[str], str], Mapping[str, str | None]]) -> None:
        """词条表里没有的文字，再按以前的整段查询查一遍（与 CAD、PDF 同一条路）。

        正常情况下词条表已经包含全部整段词条，这一步查不到新东西；它兜住的是
        历史上未经标准化入库、只有原始写法能命中的旧行，以及词条表读取失败的情况。
        「自动识别场景」下的短词仍不直接套用（以前的规则）。生效范围不含本次文件的
        词条不能从这里查回来。
        """
        assert self.index is not None
        texts_by_pair: dict[str, list[str]] = {}
        for text, pairs in self._text_pairs.items():
            if text in self.whole.hits or text in self.whole.scene_texts:
                continue
            if self.index.whole_rows(text, pairs) or whole_key(text) in self.index.out_of_scope_keys:
                continue
            if self.smart_mode and is_context_sensitive(text):
                continue
            for pair in pairs:
                texts_by_pair.setdefault(pair, []).append(text)
        values: dict[str, list[str]] = {}
        for pair, texts in texts_by_pair.items():
            for text, value in (lookup(texts, pair) or {}).items():
                if value is not None and str(value) not in values.setdefault(text, []):
                    values[text].append(str(value))
        for text, found in values.items():
            # 两个语言对说法不一：仍交给模型，不替它猜。
            if len(found) == 1:
                self.whole.hits[text] = found[0]
                self.whole.usage_by_text[text] = USAGE_SEGMENT

    # ── 批次 ──────────────────────────────────────────────────────────────

    def wrap(self, engine: TranslationEngine) -> TranslationEngine:
        if self.index is None or not self.index.entries:
            return engine
        from core.engine_dispatcher import is_local_engine_name

        include_scene = not is_local_engine_name(getattr(engine, "engine_name", "")) or bool(
            getattr(self._tm, "local_scene_candidates", False)
        )
        return TermAwareEngine(
            engine,
            self.index,
            target_lang=self.target_lang,
            retry_attempts=int(getattr(self._tm, "term_retry_attempts", 2)),
            batch_budget=int(getattr(self._tm, "term_batch_budget", 20)),
            include_scene=include_scene,
            stats=self.stats,
        )

    def not_applied_texts(self) -> set[str]:
        return self.stats.not_applied_texts()

    def not_applied_detail(self, text: str) -> str:
        """复核清单里「处理」一句：设定译法和重译次数。"""
        for outcome in self.stats.not_applied():
            if outcome.text == text:
                wanted = "；".join(f"{hit.source} → {hit.expected}" for hit in outcome.missed)
                if outcome.retries:
                    return f"设定译法：{wanted}；重译 {outcome.retries} 次后仍未采用，需人工复核。"
                return f"设定译法：{wanted}；未重译，需人工复核。"
        return "需人工复核。"

    def log_not_applied(self) -> None:
        missed = self.stats.not_applied()
        if missed:
            self._log(
                "WARN",
                f"有 {len(missed)} 处译文未采用记忆库中的设定译法，已在输出文件中标记需复核，且不保存到记忆库",
            )

    # ── 自动保存短词 ──────────────────────────────────────────────────────

    def auto_save_short_words(
        self,
        engine: TranslationEngine,
        translations: Mapping[str, str],
        *,
        pair_for_text: Callable[[str], str | None],
        counts: Mapping[str, int] | None = None,
        engine_name: str = "",
    ) -> None:
        """「自动识别场景」下：短词在新场景里的译法存为带备注的新词条（每任务最多 20 条）。"""
        if not (self.smart_mode and getattr(self._tm, "auto_save_short_scene", True) and self.profile):
            return
        if self.index is None:
            return
        skipped = self.not_applied_texts()
        picked: list[tuple[str, str, str]] = []
        for text, translation in translations.items():
            if text in skipped or not is_context_sensitive(text):
                continue
            translation = str(translation or "").strip()
            if not translation or whole_key(translation) == whole_key(text):
                continue
            known = {whole_key(row.target) for row in self.index.whole_rows(text)}
            if whole_key(translation) in known:
                continue
            pair = pair_for_text(text)
            if not pair:
                continue
            picked.append((text, translation, pair))
        if not picked:
            return
        counts = counts or {}
        picked.sort(key=lambda item: -int(counts.get(item[0], 0) or 0))
        picked = picked[:MAX_AUTO_SAVED_PER_TASK]
        notes = self._scene_notes(engine, [(text, translation) for text, translation, _pair in picked])
        fallback = _first_sentence(self.profile)
        from core import tm_manager

        for text, translation, pair in picked:
            note = notes.get(text) or fallback
            if not note:
                continue
            entry_id = tm_manager.insert_scene_entry(
                text,
                translation,
                pair,
                note,
                engine_name=engine_name,
                task_id=self._task_id,
            )
            if entry_id:
                self.auto_saved.append(
                    {
                        "id": entry_id,
                        "term": text,
                        "target": translation,
                        "note": note,
                        "lang_pair": pair,
                        "count": int(counts.get(text, 0) or 0),
                    }
                )
        if self.auto_saved:
            self._log(
                "INFO",
                f"记忆库：已自动保存 {len(self.auto_saved)} 个短词在本文档场景中的译法，备注取自文档说明",
            )

    def _scene_notes(self, engine: TranslationEngine, items: list[tuple[str, str]]) -> dict[str, str]:
        if not items or not engine_supports_chat(engine):
            return {}
        user = "文档说明：" + self.profile + "\n\n短词与本次译法：\n" + "\n".join(
            f"{text} → {translation}" for text, translation in items
        )
        try:
            raw = engine.chat(_SCENE_NOTE_SYSTEM, user)
            payload = json.loads(strip_markdown_json(str(raw or "")))
        except Exception as exc:  # noqa: BLE001 - fall back to the document profile
            logger.info(f"短词场景说明未生成，改用文档说明：{exc!r}")
            return {}
        if not isinstance(payload, dict):
            return {}
        return {
            str(key): " ".join(str(value).split())[:_NOTE_MAX_CHARS]
            for key, value in payload.items()
            if str(value or "").strip()
        }

    # ── 收尾 ──────────────────────────────────────────────────────────────

    def finish(
        self,
        *,
        pair_for_text: Callable[[str], str | None],
        locations_for_text: Callable[[str], list[str]] | None = None,
    ) -> dict[str, object]:
        """记下使用次数，返回任务详情里的「记忆库」一节（结果 ``terms``）。"""
        uses: Counter[int] = Counter(self.stats.entry_uses())
        whole_counts = {USAGE_ALWAYS: 0, USAGE_SCENE: 0, USAGE_SEGMENT: 0}
        for text, ids in self.whole.entry_ids.items():
            for entry_id in set(ids):
                uses[entry_id] += 1
            usage = self.whole.usage_by_text.get(text, USAGE_SEGMENT)
            whole_counts[usage] = whole_counts.get(usage, 0) + 1
        not_applied_by_pair: Counter[str] = Counter()
        for text in self.not_applied_texts():
            pair = pair_for_text(text)
            if pair:
                not_applied_by_pair[pair] += 1
        try:
            from core import tm_manager

            tm_manager.record_task_usage(dict(uses), not_applied_by_pair=dict(not_applied_by_pair))
        except Exception as exc:  # noqa: BLE001 - statistics never fail a finished task
            logger.warning(f"记录记忆库使用次数失败：{exc!r}")
        summary = self.stats.summary()
        # 「均已采用」只说必须采用的词条：按场景选用的候选本来就可能一条都不用。
        involved: set[int] = {
            entry_id
            for text, ids in self.whole.entry_ids.items()
            if self.whole.usage_by_text.get(text, USAGE_SEGMENT) != USAGE_SCENE
            for entry_id in ids
        }
        for outcome in self.stats.outcomes():
            for hit in outcome.hits:
                if hit.usage == USAGE_ALWAYS:
                    involved.update(hit.entry_ids)
        manual_ids = (
            {entry.id for entry in self.index.entries if entry.origin == "manual"}
            if self.index is not None
            else set()
        )
        if locations_for_text is not None:
            by_snippet = {outcome.text: outcome for outcome in self.stats.not_applied()}
            for item, outcome in zip(summary["not_applied"], by_snippet.values()):
                item["locations"] = locations_for_text(outcome.text)[:20]
        direct = sum(whole_counts.values())
        return {
            "profile": self.profile,
            "hits": {
                "always": whole_counts[USAGE_ALWAYS] + int(summary["always_hits"]),
                "scene": whole_counts[USAGE_SCENE] + int(summary["scene_hits"]),
                "segment": whole_counts[USAGE_SEGMENT],
            },
            "applied_direct": direct + int(summary["applied_first"]),
            "applied_after_retry": int(summary["applied_after_retry"]),
            "not_applied": summary["not_applied"],
            "auto_saved": list(self.auto_saved),
            "scene_choices": summary["scene_choices"],
            "manual_terms_total": self.index.manual_entry_count if self.index is not None else 0,
            # 本次涉及（整段或句中出现过）的手动维护词条数，任务详情「均已采用」那一行用。
            "manual_terms_hit": len(involved & manual_ids),
            "retry_attempts": int(getattr(self._tm, "term_retry_attempts", 2)),
        }
