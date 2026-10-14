"""一次翻译任务里的记忆库词条：整段查找、批次包装、自动保存和收尾统计。

任务流程只和这里打交道：

1. :meth:`TaskTermSession.resolve` 代替以前的「整段查记忆库 + 短词只认用户定的」，
   返回可以直接套用的整段译文；
2. :meth:`TaskTermSession.add_document_terms`（「自动识别场景」下）请模型为文中反复
   出现的词定下统一译法（:mod:`core.document_terms`），随批附上；
3. :meth:`TaskTermSession.wrap` 把翻译引擎包一层，批次里附上本批出现的词条，
   翻完核对（见 :mod:`core.term_engine`）；
4. :meth:`TaskTermSession.auto_save_short_words` 在「自动识别场景」下把短词在新场景
   里的译法存成带备注的新词条，并把大多数位置都采用了的统一用词按「自动保存」存下；
5. :meth:`TaskTermSession.finish` 记下词条使用次数，返回任务详情里「记忆库」一节。

Excel、Word 的任务流程直接用 :class:`TaskTermSession`；CAD 图纸和 PDF 原位替换
这类一份文件接一份处理的路径用 :class:`DocumentTermMemory`（每份文件一个会话，
收尾合并）；PDF 转 Word 走 Word 流程，几段的结果用 :func:`merge_term_results` 合并。
除图像翻译外，凡是调用文本翻译的路径都走这同一套规则。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections import Counter
from collections.abc import Callable, Iterable, Mapping

from loguru import logger

from core.concurrent_calls import run_in_background, wait_result
from core.document_profile import is_context_sensitive
from core.document_terms import (
    MIN_DOCUMENT_CHARS,
    TermPicker,
    document_char_count,
    find_recurring_terms,
)
from core.term_engine import TermAwareEngine, TermRunStats
from core.tm_terms import (
    USAGE_ALWAYS,
    USAGE_DOCUMENT,
    USAGE_SCENE,
    USAGE_SEGMENT,
    TermIndex,
    WholeSegmentResolution,
    chosen_candidate,
    term_applied,
    whole_key,
)
from engines.base_engine import TranslationEngine, engine_supports_chat, strip_markdown_json

MAX_AUTO_SAVED_PER_TASK = 20
# 本文档统一用词：在至少这么多段、且不少于这个比例的位置被采用，才存进记忆库。
MAX_DOCUMENT_TERMS_SAVED = 20
DOCUMENT_TERM_MIN_APPLIED = 2
DOCUMENT_TERM_MIN_RATIO = 0.6
_NOTE_MAX_CHARS = 40

_SCENE_NOTE_SYSTEM = (
    "你是翻译记忆库的整理助手。下面给出一份文档的说明，以及这份文档里几个短词和它们本次的译法。"
    "请为每个短词写一句不超过 15 个字的适用场景说明，说明这个译法在什么场景下成立"
    "（例如「防火门的耐火等级」「配电回路编号」），供以后翻译其他文档时判断是否沿用。"
    "只输出一个 JSON 对象，键是短词原文，值是说明，不要输出其他内容。"
)


def _has_cjk(text: str) -> bool:
    return any("\u3400" <= ch <= "\u9fff" or "\uf900" <= ch <= "\ufaff" for ch in str(text or ""))


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
        self.document_terms = 0
        self._document_engine_name = ""
        self._document_saved = False
        self._text_pairs: dict[str, set[str]] = {}
        # 过期的整段自动词条 {id: 旧译文}：重译结果入库前删掉（retire_stale_entries）。
        self._stale_entries: dict[int, str] = {}

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
                profile=self.profile,
            )
        except sqlite3.Error as exc:
            logger.warning(f"记忆库词条表读取失败，本次只整段复用：{exc!r}")
            self._log("WARN", "记忆库词条读取失败，本次仅整段复用已有译文，不在句中采用设定译法")
            self.index = TermIndex([])
        self.whole = self.index.resolve_whole(self._text_pairs, has_profile=bool(self.profile))
        self._add_plain_lookup_hits(tm_manager.lookup_batch)
        self._drop_stale_whole_hits()
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

    def _drop_stale_whole_hits(self) -> None:
        """整段复用的旧译文里没用上句中「始终采用」的词条（多半是词条后来改过），就不复用，
        交给模型带着词条重新翻译；否则改了词条，含它的旧句子还会一直照旧贴回去。"""
        assert self.index is not None
        if not self.index.apply_in_sentences:
            return
        stale: list[str] = []
        manual_ids = {entry.id for entry in self.index.entries if entry.origin == "manual"}
        for text, translation in self.whole.hits.items():
            if self.whole.usage_by_text.get(text) == USAGE_ALWAYS:
                continue
            ids = self.whole.entry_ids.get(text, ())
            # 用户自己维护的整段译法照用，不替用户改。
            if manual_ids.intersection(ids):
                continue
            for hit in self.index.scan(text, include_scene=False, sentence_only=True):
                if hit.usage != USAGE_ALWAYS:
                    continue
                if term_applied(translation, hit.expected, source=hit.source, target_lang=self.target_lang) is False:
                    stale.append(text)
                    break
        for text in stale:
            self.index.stale_whole_keys.add(whole_key(text))
            old_translation = self.whole.hits.get(text, "")
            for entry_id in self.whole.entry_ids.get(text, ()):
                self._stale_entries[int(entry_id)] = old_translation
            self.whole.hits.pop(text, None)
            self.whole.entry_ids.pop(text, None)
            self.whole.usage_by_text.pop(text, None)
        if stale:
            self._log("INFO", f"记忆库：{len(stale)} 处整段旧译文未采用当前的设定译法，本次重新翻译")

    def retire_stale_entries(self) -> None:
        """重译结果入库前，删掉那些过期的整段自动词条，让新译文顶替（只做一次）。"""
        if not self._stale_entries:
            return
        entries, self._stale_entries = self._stale_entries, {}
        from core import tm_manager

        try:
            tm_manager.retire_stale_auto_entries(entries)
        except sqlite3.Error as exc:
            logger.warning(f"删除过期的整段词条失败（不影响本次翻译）：{exc!r}")

    # ── 本文档统一用词 ────────────────────────────────────────────────────

    def add_document_terms(
        self,
        texts: Iterable[str],
        picker: TermPicker | None,
        *,
        should_stop: Callable[[], bool] = lambda: False,
        on_request: Callable[[], None] | None = None,
    ) -> int:
        """「自动识别场景」下，为要送模型翻译的文字里反复出现的词定下统一译法。

        文字太少、没有反复出现的词、或模型没给出可用的译法时什么都不加，照常翻译。
        返回加入的词数。``on_request`` 在真要请求模型前调用（任务流程借它更新状态行）。
        """
        index = self.index
        if not self.smart_mode or picker is None or index is None or not index.apply_in_sentences:
            return 0
        try:
            return self._add_document_terms(index, texts, picker, should_stop=should_stop, on_request=on_request)
        except Exception as exc:  # noqa: BLE001 - optional step, translation goes on
            logger.warning(f"统一用词失败（不影响本次翻译）：{exc!r}")
            return 0

    def _add_document_terms(
        self,
        index: TermIndex,
        texts: Iterable[str],
        picker: TermPicker,
        *,
        should_stop: Callable[[], bool],
        on_request: Callable[[], None] | None,
    ) -> int:
        distinct = [text for text in dict.fromkeys(texts) if text]
        if document_char_count(distinct) < MIN_DOCUMENT_CHARS:
            return 0

        def known(term: str) -> str:
            targets = {whole_key(row.target): row.target for row in index.whole_rows(term)}
            return next(iter(targets.values())) if len(targets) == 1 else ""

        candidates = find_recurring_terms(distinct, exclude=index.has_sentence_term, known=known)
        if not candidates:
            return 0
        if on_request is not None:
            on_request()
        future = run_in_background(
            lambda: picker(candidates, profile=self.profile, log=self._log), name="document-terms"
        )
        terms = wait_result(future, should_stop=should_stop, default=None)
        if not terms:
            return 0
        added = index.add_document_terms(terms)
        self.document_terms = len(added)
        self._document_engine_name = picker.engine_name
        return len(added)

    def save_document_terms(self, *, pair_for_text: Callable[[str], str | None]) -> None:
        """大多数位置都采用了的统一用词，按「自动保存」存进记忆库，备注为适用场景（只做一次）。

        与短词自动保存同一个开关；每个任务最多存 20 个，同一原文已有相同译法的不重复存。
        """
        if self._document_saved or not self.document_terms:
            return
        self._document_saved = True
        if not self.smart_mode or not getattr(self._tm, "auto_save_short_scene", True):
            return
        adopted = [
            item
            for item in self.stats.document_usage().values()
            if item.applied >= DOCUMENT_TERM_MIN_APPLIED
            and item.applied >= item.texts * DOCUMENT_TERM_MIN_RATIO
        ]
        if not adopted:
            return
        adopted.sort(key=lambda item: -item.applied)
        fallback = _first_sentence(self.profile)
        from core import tm_manager

        saved = 0
        for item in adopted:
            if saved >= MAX_DOCUMENT_TERMS_SAVED:
                break
            note = item.note or fallback
            pair = pair_for_text(item.example_text)
            if not note or not pair:
                continue
            entry_id = tm_manager.insert_scene_entry(
                item.source,
                item.target,
                pair,
                note,
                engine_name=self._document_engine_name,
                task_id=self._task_id,
            )
            if entry_id:
                saved += 1
                self.auto_saved.append(
                    {
                        "id": entry_id,
                        "term": item.source,
                        "target": item.target,
                        "note": note,
                        "lang_pair": pair,
                        "count": item.applied,
                        "kind": "document",
                    }
                )
        if saved:
            self._log("INFO", f"记忆库：已自动保存 {saved} 个统一用词的译法，并附上适用场景备注")

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

    def will_auto_save(self) -> bool:
        """本次会不会自动保存短词（决定要不要先数出现次数）。"""
        return bool(
            self.smart_mode
            and getattr(self._tm, "auto_save_short_scene", True)
            and self.profile
            and self.index is not None
        )

    def auto_save_short_words(
        self,
        engine: TranslationEngine,
        translations: Mapping[str, str],
        *,
        pair_for_text: Callable[[str], str | None],
        counts: Mapping[str, int] | None = None,
        engine_name: str = "",
    ) -> None:
        """「自动识别场景」下：短词在新场景里的译法存为带备注的新词条（每任务最多 20 条）。

        本文档统一用词也在这时存（:meth:`save_document_terms`）。
        """
        try:
            self.save_document_terms(pair_for_text=pair_for_text)
        except Exception as exc:  # noqa: BLE001 - auto-saving never fails a translated file
            logger.warning(f"自动保存统一用词失败（不影响本次翻译）：{exc!r}")
        if not self.will_auto_save():
            return
        assert self.index is not None
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
        short_saved = sum(1 for item in self.auto_saved if item.get("kind") != "document")
        if short_saved:
            self._log(
                "INFO",
                f"记忆库：已自动保存 {short_saved} 个短词在本文档场景中的译法，备注取自文档说明",
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
        # 「均已采用」数的是真正用上的手动词条：必须采用的命中都算；按场景选用的只算
        # 模型选中的那一种（其余候选本来就不该用）。
        involved: set[int] = {
            entry_id
            for text, ids in self.whole.entry_ids.items()
            if self.whole.usage_by_text.get(text, USAGE_SEGMENT) != USAGE_SCENE
            for entry_id in ids
        }
        for outcome in self.stats.outcomes():
            for hit in outcome.hits:
                if hit.usage == USAGE_DOCUMENT:
                    continue
                if hit.usage == USAGE_ALWAYS:
                    involved.update(hit.entry_ids)
                    continue
                chosen = chosen_candidate(hit, outcome.translation, target_lang=self.target_lang)
                if chosen is not None:
                    involved.update(chosen.entry_ids)
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
            # 同一任务里几份结果合并时按 id 去重（merge_term_results）。
            "manual_hit_ids": sorted(involved & manual_ids),
            "retry_attempts": int(getattr(self._tm, "term_retry_attempts", 2)),
        }

    @property
    def hits(self) -> dict[str, str]:
        """整段直接套用的 {原文: 译文}（:meth:`resolve` 的返回值）。"""
        return dict(self.whole.hits)


# ── 合并与通用入口 ─────────────────────────────────────────────────────────


def merge_term_results(results: Iterable[Mapping[str, object] | None]) -> dict[str, object]:
    """把同一任务里几份「记忆库」结果（每张图纸、每段转 Word 的正文、每页原位替换）
    合成任务详情里的一节；都为空时返回空字典（整节不显示）。"""
    parts = [dict(item) for item in results if item]
    if not parts:
        return {}
    if len(parts) == 1:
        return parts[0]
    hits = {"always": 0, "scene": 0, "segment": 0}
    not_applied: list[object] = []
    auto_saved: list[object] = []
    scenes: dict[str, dict[str, object]] = {}
    manual_ids: set[int] = set()
    manual_hit_fallback = 0
    applied_direct = applied_after_retry = manual_total = retry_attempts = 0
    profile = ""
    marked = {str(part.get("marked_in") or "") for part in parts if part.get("not_applied")}
    for part in parts:
        part_hits = part.get("hits") if isinstance(part.get("hits"), Mapping) else {}
        for key in hits:
            hits[key] += int(part_hits.get(key, 0) or 0)  # type: ignore[union-attr]
        applied_direct += int(part.get("applied_direct", 0) or 0)
        applied_after_retry += int(part.get("applied_after_retry", 0) or 0)
        not_applied.extend(part.get("not_applied") or [])  # type: ignore[arg-type]
        auto_saved.extend(part.get("auto_saved") or [])  # type: ignore[arg-type]
        manual_total = max(manual_total, int(part.get("manual_terms_total", 0) or 0))
        retry_attempts = max(retry_attempts, int(part.get("retry_attempts", 0) or 0))
        ids = part.get("manual_hit_ids")
        if isinstance(ids, list):
            manual_ids.update(int(value) for value in ids)
        else:
            manual_hit_fallback = max(manual_hit_fallback, int(part.get("manual_terms_hit", 0) or 0))
        if not profile:
            profile = str(part.get("profile") or "")
        for item in part.get("scene_choices") or []:  # type: ignore[union-attr]
            if not isinstance(item, Mapping):
                continue
            term = str(item.get("term") or "")
            merged = scenes.setdefault(
                term,
                {"term": term, "candidates": list(item.get("candidates") or []), "chosen": Counter(), "count": 0},
            )
            merged["count"] = int(merged["count"]) + int(item.get("count", 0) or 0)
            for choice in item.get("chosen") or []:
                if isinstance(choice, Mapping):
                    merged["chosen"][str(choice.get("target") or "")] += int(choice.get("count", 0) or 0)  # type: ignore[index]
    scene_choices = []
    for item in scenes.values():
        chosen: Counter[str] = item.pop("chosen")  # type: ignore[assignment]
        item["chosen"] = [{"target": target, "count": count} for target, count in chosen.most_common()]
        scene_choices.append(item)
    return {
        "profile": profile,
        "hits": hits,
        "applied_direct": applied_direct,
        "applied_after_retry": applied_after_retry,
        "not_applied": not_applied,
        "auto_saved": auto_saved,
        "scene_choices": scene_choices,
        "manual_terms_total": manual_total,
        "manual_terms_hit": max(len(manual_ids), manual_hit_fallback),
        "manual_hit_ids": sorted(manual_ids),
        "retry_attempts": retry_attempts,
        # 几处标记的地方不同（PDF：转 Word 的段标在文件里，原位替换的页列在逐页结果）时笼统说「已标记」。
        **({"marked_in": marked.pop() if len(marked) == 1 else "mixed"} if marked - {""} else {}),
    }


class DocumentTermMemory:
    """不经 Excel / Word 流程的文字翻译路径（CAD 图纸、PDF 原位替换）共用的记忆库层。

    规则与 Excel / Word 完全相同（同一个 :class:`TaskTermSession`）：

    1. :meth:`open`：每份文件（或每页）的文字先整段查记忆库，手动维护的词条优先；
       给了 ``term_picker`` 时再为文中反复出现的词定下统一译法（CAD 图纸用，PDF 原位
       替换逐页处理、不做）；
    2. 返回的会话 ``wrap(engine)`` 后再翻译：本批出现的词条随批附上，翻完核对，
       「始终采用」的未采用时重译，仍不符的由调用方标记需复核；
    3. :meth:`close`：「自动识别场景」下把短词的本次译法存成带备注的新词条；
    4. :meth:`finish`：任务结束时记下使用次数，合并成任务详情里的「记忆库」一节。

    这几条路径一份文件接一份地处理，会话按顺序开、按顺序关。
    """

    def __init__(
        self,
        tm_settings,
        *,
        target_lang: str,
        lang_pairs: Iterable[str],
        smart_mode: bool,
        engine_provider: Callable[[], TranslationEngine | None] | None = None,
        log: Callable[[str, str], None] | None = None,
        task_id: str = "",
        marked_in: str = "",
        term_picker: TermPicker | None = None,
    ) -> None:
        self._tm = tm_settings
        self._term_picker = term_picker
        # 未采用设定译法时标记需复核的地方（任务详情据此写说明）：cad_review 为 CAD 译文对照表，
        # pdf_pages 为 PDF 逐页结果；Excel / Word 标在输出文件里，不带这个键。
        self.marked_in = marked_in
        self.target_lang = target_lang
        self.lang_pairs = [pair for pair in dict.fromkeys(str(item) for item in lang_pairs) if pair]
        self.smart_mode = bool(smart_mode)
        self._engine_provider = engine_provider
        self._log = log or (lambda _level, _message: None)
        self._task_id = task_id
        self._sessions: list[tuple[TaskTermSession, list[str], Callable[[str], str | None]]] = []
        # 已收尾的会话结果：使用次数只记一次；收尾后又开的会话（PDF 单页重新生成）下次再记。
        self._finished: list[dict[str, object]] = []
        self._finished_count = 0
        self._lock = threading.Lock()

    def _pair_resolver(self, pairs: list[str]) -> Callable[[str], str | None]:
        def pair_for_text(text: str) -> str | None:
            if len(pairs) == 1:
                return pairs[0]
            # 自动识别源语言时几个语言对都查：含汉字的归中文那一对，其余判断不了就不自动保存。
            if _has_cjk(text):
                return next((pair for pair in pairs if pair.split("-", 1)[0] == "zh"), None)
            return None

        return pair_for_text

    def open(
        self,
        texts: Iterable[str],
        *,
        profile: str = "",
        file_path: object | None = None,
        location: str = "",
        lang_pairs: Iterable[str] | None = None,
        log: Callable[[str, str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> TaskTermSession:
        """一份文件（或一页）开一个会话：整段查记忆库，``session.hits`` 是直接套用的译文。"""
        pairs = [pair for pair in dict.fromkeys(str(item) for item in (lang_pairs or self.lang_pairs)) if pair]
        session = TaskTermSession(
            self._tm,
            target_lang=self.target_lang,
            smart_mode=self.smart_mode,
            file_paths=[file_path] if file_path else (),
            log=log or self._log,
            task_id=self._task_id,
        )
        hits = session.resolve({text: set(pairs) for text in dict.fromkeys(texts) if text}, profile=profile)
        if self._term_picker is not None:
            try:
                session.add_document_terms(
                    [text for text in dict.fromkeys(texts) if text and text not in hits],
                    self._term_picker,
                    should_stop=should_stop or (lambda: False),
                )
            except Exception as exc:  # noqa: BLE001 - optional step, translation goes on
                logger.warning(f"统一用词失败（不影响本次翻译）：{exc!r}")
        with self._lock:
            self._sessions.append((session, [location] if location else [], self._pair_resolver(pairs)))
        return session

    def pair_for(self, session: TaskTermSession) -> Callable[[str], str | None]:
        with self._lock:
            for item, _locations, resolver in self._sessions:
                if item is session:
                    return resolver
        return lambda _text: None

    def close(
        self,
        session: TaskTermSession,
        translations: Mapping[str, str],
        *,
        counts: Mapping[str, int] | None = None,
    ) -> None:
        """翻完一份文件：记一行未采用的日志，「自动识别场景」下自动保存短词和统一用词。"""
        session.log_not_applied()
        try:
            session.save_document_terms(pair_for_text=self.pair_for(session))
        except Exception as exc:  # noqa: BLE001 - auto-saving never fails a translated file
            logger.warning(f"自动保存统一用词失败（不影响本次翻译）：{exc!r}")
        if not session.will_auto_save():
            return
        engine = self._engine_provider() if self._engine_provider is not None else None
        if engine is None:
            return
        try:
            session.auto_save_short_words(
                engine,
                translations,
                pair_for_text=self.pair_for(session),
                counts=counts,
                engine_name=str(getattr(engine, "engine_name", "") or ""),
            )
        except Exception as exc:  # noqa: BLE001 - auto-saving never fails a translated file
            logger.warning(f"自动保存短词失败（不影响本次翻译）：{exc!r}")

    def finish(self) -> dict[str, object]:
        """任务收尾（完成、停止、失败都会走到）：每个会话只收尾一次，返回合并后的结果。"""
        with self._lock:
            for session, locations, resolver in self._sessions[self._finished_count:]:
                result = session.finish(
                    pair_for_text=resolver,
                    locations_for_text=(lambda _text, where=locations: list(where)) if locations else None,
                )
                if self.marked_in:
                    result["marked_in"] = self.marked_in
                self._finished.append(result)
            self._finished_count = len(self._sessions)
            return merge_term_results(self._finished)
