"""本文档统一用词：本机找词、请模型定译法、随批附上、核对后按「自动保存」存进记忆库。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core import tm_manager
from core.document_terms import (
    MIN_DOCUMENT_CHARS,
    DocumentTerm,
    TermCandidate,
    TermPicker,
    build_pick_request,
    cleaner_engine_factory,
    find_recurring_terms,
    parse_pick_response,
)
from core.term_engine import TermAwareEngine
from core.term_session import DocumentTermMemory, TaskTermSession
from core.tm_terms import (
    USAGE_ALWAYS,
    USAGE_DOCUMENT,
    TermEntry,
    TermIndex,
    build_prompt_block,
    term_keys_of,
)
from engines.base_engine import TranslationEngine

TM_SETTINGS = SimpleNamespace(
    apply_in_sentences=True,
    term_retry_attempts=2,
    term_batch_budget=20,
    auto_save_short_scene=True,
    local_scene_candidates=False,
)

_FILLERS = [
    "现场排水沟应提前疏通，雨天停止作业",
    "施工前完成测量放线并报验",
    "边坡按一比零点五放坡，坡面拍实",
    "弃土运至指定堆放点，不得随意倾倒",
    "夜间施工设置照明，并安排专人值守",
    "临近建筑物一侧加密观测频次",
    "开挖过程中如遇地下管线立即停工",
    "坑底预留三十厘米由人工清理",
]


_PHRASES = {
    "基坑开挖": ["基坑开挖分层进行", "完成基坑开挖后验槽", "基坑开挖至设计标高"],
    "挖掘机": ["挖掘机停放在坡顶以外", "两台挖掘机同时作业", "由挖掘机装车外运"],
    "项目部": ["由项目部统一调度", "报项目部审批", "项目部每周检查一次"],
}


def _document() -> list[str]:
    """四十段不同的文字：「基坑开挖」出现在 12 段，「挖掘机」10 段，「项目部」8 段，前后文各不相同。"""
    texts = []
    for index in range(40):
        parts = [f"第{index + 1}条", _FILLERS[index % len(_FILLERS)]]
        if index % 10 < 3:
            parts.append(_PHRASES["基坑开挖"][index % 3])
        if index % 4 == 1:
            parts.append(_PHRASES["挖掘机"][index % 3])
        if index % 5 == 2:
            parts.append(_PHRASES["项目部"][index % 3])
        parts.append(f"本段编号{index * 7}，记录存档备查，确保资料完整可追溯")
        texts.append("，".join(parts) + "。")
    return texts


class MiningTests(unittest.TestCase):
    def test_finds_terms_not_fragments(self) -> None:
        sources = [item.source for item in find_recurring_terms(_document())]
        for term in ("基坑开挖", "挖掘机", "项目部"):
            self.assertIn(term, sources)
        # 只在长词里出现的一截不单列。
        for fragment in ("坑开挖", "基坑开", "掘机", "项目"):
            self.assertNotIn(fragment, sources)
        # 到处都是的两字词不交给模型。
        self.assertNotIn("进行", sources)

    def test_counts_distinct_texts_and_gives_example(self) -> None:
        by_source = {item.source: item for item in find_recurring_terms(_document())}
        self.assertEqual(by_source["基坑开挖"].texts, 12)
        self.assertIn("基坑开挖", by_source["基坑开挖"].example)

    def test_terms_below_three_texts_are_ignored(self) -> None:
        texts = ["盛泰公司负责供货", "盛泰公司负责安装", "其余由总包负责"]
        self.assertNotIn("盛泰公司", [item.source for item in find_recurring_terms(texts)])

    def test_exclude_and_known_callbacks(self) -> None:
        candidates = find_recurring_terms(
            _document(),
            exclude=lambda term: term == "挖掘机",
            known=lambda term: "fouille" if term == "基坑开挖" else "",
        )
        sources = {item.source: item for item in candidates}
        self.assertNotIn("挖掘机", sources)
        self.assertEqual(sources["基坑开挖"].known, "fouille")

    def test_latin_phrases_keep_original_case(self) -> None:
        texts = [
            f"Line {n}: the Shengtai site office keeps the earthwork excavation records, batch {n}."
            for n in range(3)
        ] + [f"Earthwork excavation stops at night ({n})." for n in range(2)]
        sources = [item.source for item in find_recurring_terms(texts)]
        self.assertIn("Shengtai", sources)
        self.assertTrue(any(source.lower() == "earthwork excavation" for source in sources))
        # 汉字和标点不当拉丁词组的一部分。
        self.assertFalse(any("，" in source or "第" in source for source in sources))


class PickResponseTests(unittest.TestCase):
    CANDIDATES = [
        TermCandidate("基坑开挖", 12, "基坑开挖分层进行"),
        TermCandidate("挖掘机", 10, "挖掘机停放在坡顶以外"),
        TermCandidate("项目部", 8, "由项目部统一调度"),
    ]

    def test_parses_fenced_json_and_filters_bad_values(self) -> None:
        raw = "```json\n" + json.dumps(
            {
                "基坑开挖": {"译法": "fouille", "场景": "建筑基坑土方施工"},
                "挖掘机": {"译法": "挖掘机", "场景": "x"},  # 法语译法里还有汉字
                "项目部": "direction de projet",
                "别的词": {"译法": "autre"},  # 不在候选里
            },
            ensure_ascii=False,
        ) + "\n```"
        terms = parse_pick_response(raw, self.CANDIDATES, target_is_cjk=False)
        self.assertEqual(
            terms,
            [
                DocumentTerm("基坑开挖", "fouille", "建筑基坑土方施工", 12),
                DocumentTerm("项目部", "direction de projet", "", 8),
            ],
        )

    def test_request_lists_profile_candidates_and_known(self) -> None:
        system, user = build_pick_request(
            [TermCandidate("基坑开挖", 12, "基坑开挖分层进行", known="fouille")],
            profile="土方开挖施工方案。",
            target_language="法语",
        )
        self.assertIn("法语", system)
        self.assertIn("JSON", system)
        self.assertIn("文档说明：土方开挖施工方案。", user)
        self.assertIn("基坑开挖 ｜ 12 段", user)
        self.assertIn("记忆库已有译法：fouille", user)


class _ChatEngine(TranslationEngine):
    engine_name = ""

    def __init__(self, name: str, reply: str | Exception) -> None:
        self.engine_name = name
        self.reply = reply
        self.calls = 0

    def translate_batch(self, texts, target_lang, system_prompt, source_lang="zh"):
        raise AssertionError("not used")

    def chat(self, system, user):
        self.calls += 1
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class PickerTests(unittest.TestCase):
    CANDIDATES = [TermCandidate("基坑开挖", 12, "基坑开挖分层进行")]
    GOOD = json.dumps({"基坑开挖": {"译法": "fouille", "场景": "基坑土方施工"}}, ensure_ascii=False)

    def _picker(self, primary, fallback):
        logs: list[tuple[str, str]] = []
        picker = TermPicker(
            primary=(lambda: primary) if primary is not ... else None,
            fallback=fallback,
            target_language="法语",
            target_is_cjk=False,
            log=lambda level, message: logs.append((level, message)),
        )
        return picker, logs

    def test_cleaner_model_first(self) -> None:
        cleaner = _ChatEngine("cleaner/model", self.GOOD)
        translator = _ChatEngine("translation/model", self.GOOD)
        picker, logs = self._picker(cleaner, translator)
        terms = picker(self.CANDIDATES, profile="说明")
        self.assertEqual([term.target for term in terms], ["fouille"])
        self.assertEqual((cleaner.calls, translator.calls), (1, 0))
        self.assertEqual(picker.engine_name, "cleaner/model")
        self.assertIn("记忆库清洗模型", logs[-1][1])

    def test_falls_back_to_translation_model(self) -> None:
        cleaner = _ChatEngine("cleaner/model", RuntimeError("401"))
        translator = _ChatEngine("translation/model", self.GOOD)
        picker, logs = self._picker(cleaner, translator)
        self.assertEqual([term.target for term in picker(self.CANDIDATES, profile="")], ["fouille"])
        self.assertEqual(picker.engine_name, "translation/model")
        self.assertTrue(any("改用文档翻译模型" in message for _level, message in logs))

    def test_unconfigured_cleaner_uses_translation_model(self) -> None:
        translator = _ChatEngine("translation/model", self.GOOD)
        picker, _logs = self._picker(None, translator)
        self.assertEqual(len(picker(self.CANDIDATES, profile="")), 1)
        self.assertEqual(translator.calls, 1)

    def test_everything_failing_returns_nothing(self) -> None:
        picker, logs = self._picker(_ChatEngine("c", "not json"), _ChatEngine("t", RuntimeError("down")))
        self.assertEqual(picker(self.CANDIDATES, profile=""), [])
        self.assertEqual(logs[-1][0], "WARN")

    def test_cleaner_without_model_name_is_skipped(self) -> None:
        from settings import AppSettings

        self.assertIsNone(cleaner_engine_factory(AppSettings())())


class IndexTests(unittest.TestCase):
    def _index(self) -> TermIndex:
        manual = TermEntry(1, "基坑", "fosse", "zh-fr", usage=USAGE_ALWAYS, origin="manual")
        return TermIndex([manual], lang_pairs={"zh-fr"})

    def test_document_terms_scanned_and_stored_terms_win(self) -> None:
        index = self._index()
        added = index.add_document_terms(
            [
                DocumentTerm("基坑开挖", "fouille"),  # 和手动维护的「基坑」重叠
                DocumentTerm("挖掘机", "pelle hydraulique"),
                DocumentTerm("基坑", "autre"),  # 记忆库已有
            ]
        )
        self.assertEqual([entry.source for entry in added], ["基坑开挖", "挖掘机"])
        self.assertEqual(index.in_sentence_term_count, 1)
        hits = {hit.source: hit for hit in index.scan("基坑开挖时挖掘机停在坡顶")}
        self.assertEqual(set(hits), {"基坑", "挖掘机"})
        self.assertEqual(hits["基坑"].usage, USAGE_ALWAYS)
        self.assertEqual(hits["挖掘机"].usage, USAGE_DOCUMENT)
        self.assertTrue(index.has_sentence_term("基坑开挖"))
        self.assertFalse(index.has_sentence_term("挖掘机"))

    def test_prompt_block_and_batch_budget(self) -> None:
        index = self._index()
        index.add_document_terms([DocumentTerm("挖掘机", "pelle hydraulique")])
        hits = [index.scan("挖掘机就位"), index.scan("基坑验收")]
        block = build_prompt_block(hits, "法语")
        self.assertIn("本批文字中出现的记忆库词条：", block)
        self.assertIn("本文档统一用词", block)
        self.assertIn("- [第 1 条] 挖掘机 → pelle hydraulique", block)
        self.assertEqual(term_keys_of(hits[0]), set())
        only_document = build_prompt_block([hits[0]], "法语")
        self.assertNotIn("记忆库词条", only_document)


class _DocEngine(TranslationEngine):
    """看到统一用词时照用；``ignore`` 里的段落不照用。"""

    engine_name = "test/mock"

    def __init__(self, ignore: set[int] = frozenset()) -> None:
        self.ignore = ignore
        self.prompts: list[str] = []
        self.calls = 0

    def translate_batch(self, texts, target_lang, system_prompt, source_lang="zh"):
        self.prompts.append(system_prompt)
        self.calls += 1
        out = {}
        for text in texts:
            number = int(text.split("条", 1)[0].lstrip("第"))
            told = "基坑开挖 → excavation de la fouille" in system_prompt
            word = "excavation de la fouille" if told and number not in self.ignore else "creusement du puits"
            out[text] = f"Article {number} : {word}." if "基坑开挖" in text else f"Article {number}."
        return out

    def chat(self, system, user):
        return json.dumps(
            {"基坑开挖": {"译法": "excavation de la fouille", "场景": "建筑基坑土方施工"}},
            ensure_ascii=False,
        )


class SessionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for patcher in (
            patch.object(tm_manager, "DB_PATH", root / "tm.db"),
            patch.object(tm_manager, "BACKUPS_DIR", root / "backups"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        tm_manager.init_db()
        self.texts = _document()
        self.assertGreaterEqual(sum(map(len, self.texts)), MIN_DOCUMENT_CHARS // 2)
        # 文档够长才做：把每段拉长到总量过线。
        self.texts = [text + "说明" * 40 for text in self.texts]

    def _run(self, engine: _DocEngine, *, tm=TM_SETTINGS, smart_mode=True):
        logs: list[tuple[str, str]] = []
        session = TaskTermSession(
            tm, target_lang="fr", smart_mode=smart_mode, log=lambda level, message: logs.append((level, message))
        )
        session.resolve({text: {"zh-fr"} for text in self.texts}, profile="建筑基坑土方开挖施工方案。")
        picker = TermPicker(primary=None, fallback=engine, target_language="法语", target_is_cjk=False)
        added = session.add_document_terms(self.texts, picker)
        wrapped = session.wrap(engine)
        translations = {}
        for start in range(0, len(self.texts), 5):
            translations.update(wrapped.translate_batch(self.texts[start:start + 5], "fr", "SYSTEM"))
        session.auto_save_short_words(engine, translations, pair_for_text=lambda _text: "zh-fr")
        return session, added, logs

    def test_terms_sent_with_every_batch_and_saved(self) -> None:
        engine = _DocEngine()
        session, added, logs = self._run(engine)
        self.assertEqual(added, 1)
        self.assertIsInstance(session.wrap(engine), TermAwareEngine)
        with_term = [prompt for prompt in engine.prompts if "基坑开挖 → excavation de la fouille" in prompt]
        self.assertEqual(len(with_term), 4)  # 12 段分在 4 批里，批批都带上
        # 统一用词不核对重译：一次请求一批。
        self.assertEqual(engine.calls, 8)
        saved = [item for item in session.auto_saved if item.get("kind") == "document"]
        self.assertEqual(len(saved), 1)
        self.assertEqual((saved[0]["term"], saved[0]["target"], saved[0]["note"]),
                         ("基坑开挖", "excavation de la fouille", "建筑基坑土方施工"))
        self.assertEqual(saved[0]["count"], 12)
        rows = [row for row in tm_manager.list_entries_for_target("fr", {"zh-fr"}) if row["source_text"] == "基坑开挖"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["note"], "建筑基坑土方施工")
        self.assertNotEqual(rows[0]["origin"], "manual")
        self.assertTrue(any("统一用词的译法" in message for _level, message in logs))
        result = session.finish(pair_for_text=lambda _text: "zh-fr")
        self.assertEqual(result["auto_saved"], session.auto_saved)
        self.assertEqual(result["scene_choices"], [])
        self.assertEqual(result["hits"]["scene"], 0)

    def test_not_saved_when_most_places_ignored_it(self) -> None:
        engine = _DocEngine(ignore={1, 2, 3, 11, 12, 13})
        session, added, _logs = self._run(engine)
        self.assertEqual(added, 1)
        self.assertEqual(session.auto_saved, [])

    def test_not_saved_when_auto_save_switched_off(self) -> None:
        tm = SimpleNamespace(**{**vars(TM_SETTINGS), "auto_save_short_scene": False})
        session, added, _logs = self._run(_DocEngine(), tm=tm)
        self.assertEqual(added, 1)
        self.assertEqual(session.auto_saved, [])

    def test_off_without_smart_mode_or_for_short_documents(self) -> None:
        engine = _DocEngine()
        _session, added, _logs = self._run(engine, smart_mode=False)
        self.assertEqual(added, 0)
        self.texts = _document()[:6]
        _session, added, _logs = self._run(_DocEngine())
        self.assertEqual(added, 0)

    def test_document_memory_opens_with_picker(self) -> None:
        engine = _DocEngine()
        picker = TermPicker(primary=None, fallback=engine, target_language="法语", target_is_cjk=False)
        memory = DocumentTermMemory(
            TM_SETTINGS,
            target_lang="fr",
            lang_pairs=["zh-fr"],
            smart_mode=True,
            engine_provider=lambda: engine,
            term_picker=picker,
        )
        session = memory.open(self.texts, profile="建筑基坑土方开挖施工方案。", location="plan.dxf")
        self.assertEqual(session.document_terms, 1)
        wrapped = session.wrap(engine)
        translations = {}
        for start in range(0, len(self.texts), 5):
            translations.update(wrapped.translate_batch(self.texts[start:start + 5], "fr", "SYSTEM"))
        memory.close(session, translations)
        result = memory.finish()
        self.assertEqual([item["term"] for item in result["auto_saved"]], ["基坑开挖"])


if __name__ == "__main__":
    unittest.main()
