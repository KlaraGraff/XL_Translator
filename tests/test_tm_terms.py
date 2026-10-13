"""记忆库词条：整段复用、句中扫描、提示词、核对与重译。"""

from __future__ import annotations

import threading
import unittest

from core.language_preflight import TranslationLanguageResult
from core.term_engine import TermAwareEngine
from core.tm_terms import (
    TermEntry,
    TermIndex,
    build_prompt_block,
    split_by_budget,
    term_applied,
)
from engines.base_engine import TranslationEngine


def entry(entry_id, source, target, *, usage, origin="manual", note="", pair="zh-fr", scope=""):
    return TermEntry(
        id=entry_id,
        source=source,
        target=target,
        lang_pair=pair,
        note=note,
        usage=usage,
        origin=origin,
        scope=scope,
    )


class WholeSegmentTests(unittest.TestCase):
    def test_always_and_segment_rows_are_applied_directly(self):
        index = TermIndex([
            entry(1, "甲级防火门", "porte coupe-feu de classe A", usage="always"),
            entry(2, "卫生间", "toilettes", usage="segment", origin="auto"),
        ])
        result = index.resolve_whole({"甲级防火门": ["zh-fr"], "卫生间": ["zh-fr"]}, has_profile=True)
        self.assertEqual(result.hits["甲级防火门"], "porte coupe-feu de classe A")
        self.assertEqual(result.hits["卫生间"], "toilettes")
        self.assertEqual(result.usage_by_text["卫生间"], "segment")
        self.assertEqual(result.entry_ids["甲级防火门"], (1,))

    def test_scene_rows_go_to_the_model_when_there_is_evidence(self):
        index = TermIndex([
            entry(1, "甲", "A", usage="scene", note="配电回路编号"),
            entry(2, "甲", "Partie A", usage="scene", note="合同甲方"),
        ])
        result = index.resolve_whole({"甲": ["zh-fr"]}, has_profile=False)
        self.assertNotIn("甲", result.hits)
        self.assertIn("甲", result.scene_texts)

    def test_short_auto_word_without_profile_keeps_the_old_reuse(self):
        index = TermIndex([entry(1, "甲", "classe A", usage="scene", origin="auto")])
        without_profile = index.resolve_whole({"甲": ["zh-fr"]}, has_profile=False)
        self.assertEqual(without_profile.hits, {"甲": "classe A"})
        with_profile = index.resolve_whole({"甲": ["zh-fr"]}, has_profile=True)
        self.assertEqual(with_profile.hits, {})
        self.assertEqual(with_profile.scene_texts, {"甲"})

    def test_lookup_respects_the_allowed_pairs_and_scope(self):
        index = TermIndex(
            [
                entry(1, "门", "door", usage="always", pair="zh-en"),
                entry(2, "卫生间", "sanitaires", usage="always", scope="公建"),
            ],
            file_paths=["/work/住宅/图纸.xlsx"],
        )
        result = index.resolve_whole({"门": ["zh-fr"], "卫生间": ["zh-fr"]}, has_profile=False)
        self.assertEqual(result.hits, {})
        scoped = TermIndex([entry(2, "卫生间", "sanitaires", usage="always", scope="公建")], file_paths=["/work/公建/a.xlsx"])
        self.assertEqual(scoped.resolve_whole({"卫生间": ["zh-fr"]}, has_profile=False).hits, {"卫生间": "sanitaires"})


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.index = TermIndex([
            entry(1, "甲级防火门", "porte coupe-feu de classe A", usage="always"),
            entry(2, "防火门", "porte coupe-feu", usage="always"),
            entry(3, "甲", "A", usage="scene", note="配电回路编号"),
            entry(4, "预埋件", "pièce noyée", usage="always"),
            entry(5, "门窗", "menuiseries", usage="always"),
            entry(6, "卫生间", "toilettes", usage="segment", origin="auto"),
            entry(7, "楼梯", "escalier", usage="scene", origin="auto"),
        ])

    def keys(self, text, **kwargs):
        return {(hit.source, hit.usage) for hit in self.index.scan(text, **kwargs)}

    def test_containment_keeps_the_longest_term(self):
        self.assertEqual(self.keys("防火门均采用甲级防火门"), {("甲级防火门", "always"), ("防火门", "always")})
        self.assertEqual(self.keys("采用甲级防火门"), {("甲级防火门", "always")})

    def test_single_character_needs_boundaries(self):
        self.assertEqual(self.keys("甲级"), set())
        self.assertEqual(self.keys("回路 甲：照明"), {("甲", "scene")})

    def test_partial_overlap_is_downgraded_to_scene(self):
        self.assertEqual(self.keys("防火门窗"), {("防火门", "scene"), ("门窗", "scene")})

    def test_segment_and_unnoted_auto_rows_are_not_scanned_in_sentences(self):
        self.assertEqual(self.keys("卫生间和楼梯间"), set())

    def test_whole_text_hit_and_local_models(self):
        hits = self.index.scan("甲")
        self.assertEqual(len(hits), 1)
        self.assertTrue(hits[0].whole)
        self.assertEqual(self.keys("甲", include_scene=False), set())
        self.assertEqual(self.keys("回路 甲 采用预埋件", include_scene=False), {("预埋件", "always")})

    def test_in_sentence_switch(self):
        index = TermIndex([entry(4, "预埋件", "pièce noyée", usage="always")], apply_in_sentences=False)
        self.assertEqual(index.scan("门框与预埋件连接"), [])

    def test_latin_terms_match_whole_words(self):
        index = TermIndex([entry(1, "door", "porte", usage="always", pair="en-fr")])
        self.assertEqual(len(index.scan("Fire Door type A")), 1)
        self.assertEqual(index.scan("doorway"), [])


class PromptBlockTests(unittest.TestCase):
    def test_block_lists_positions_and_candidates(self):
        index = TermIndex([
            entry(1, "甲级防火门", "porte coupe-feu de classe A", usage="always"),
            entry(2, "甲", "A", usage="scene", note="配电回路编号"),
            entry(3, "甲", "Partie A", usage="scene", note="合同甲方"),
            entry(4, "甲", "甲", usage="scene", note="编号的一部分"),
        ])
        texts = ["说明", "说明", "采用甲级防火门", "甲"]
        block = build_prompt_block([index.scan(text) for text in texts], "法文")
        self.assertIn("必须采用（可按法文语法调整词形）：", block)
        self.assertIn("- [第 3 条] 甲级防火门 → porte coupe-feu de classe A", block)
        self.assertIn("- [第 4 条] 甲：a. A（配电回路编号）；b. Partie A（合同甲方）；c. 原样保留（编号的一部分）", block)

    def test_budget_splits_without_dropping_terms(self):
        index = TermIndex([entry(i, f"词条{i:02d}", f"terme {i}", usage="always") for i in range(1, 6)])
        texts = ["词条01词条02", "词条03", "词条04词条05", "无"]
        hits = [index.scan(text) for text in texts]
        groups = split_by_budget(texts, hits, 2)
        self.assertEqual(groups, [[0], [1], [2, 3]])
        covered = {hit.source for group in groups for i in group for hit in hits[i]}
        self.assertEqual(len(covered), 5)


class VerificationTests(unittest.TestCase):
    def check(self, translation, expected, source="词", lang="fr", whole=False):
        return term_applied(translation, expected, source=source, target_lang=lang, whole=whole)

    def test_inflection_and_accents_are_tolerated(self):
        self.assertTrue(self.check("Les portes coupe-feu de classe A sont posées", "porte coupe-feu de classe A"))
        self.assertTrue(self.check("fixées aux pieces noyees", "pièce noyée"))
        self.assertFalse(self.check("fixées aux pièces à sceller", "pièce noyée"))

    def test_short_uppercase_tokens_are_case_sensitive(self):
        self.assertFalse(self.check("porte coupe-feu de classe B, a", "porte coupe-feu de classe A"))
        self.assertTrue(self.check("tube en PVC", "PVC"))
        self.assertFalse(self.check("tube en pvc", "PVC"))

    def test_single_letter_in_sentence_is_not_checked(self):
        self.assertIsNone(self.check("circuit B", "A"))
        self.assertFalse(self.check("B", "A", whole=True))

    def test_keep_as_is_and_cjk_targets(self):
        self.assertTrue(self.check("Repère FM1021", "FM", source="FM"))
        self.assertTrue(self.check("采用 甲级 防火门", "甲级防火门", lang="zh"))


class FakeEngine(TranslationEngine):
    """按提示词决定译文：提示词里有改正说明才用设定译法。"""

    def __init__(self, *, comply_on_attempt=1, with_sources=False):
        self.prompts: list[str] = []
        self.batches: list[list[str]] = []
        self.comply_on_attempt = comply_on_attempt
        self.calls_by_text: dict[str, int] = {}
        self._lock = threading.Lock()

    def _translate(self, text, prompt):
        with self._lock:
            self.calls_by_text[text] = self.calls_by_text.get(text, 0) + 1
            attempt = self.calls_by_text[text]
        if "预埋件" in text:
            if attempt >= self.comply_on_attempt:
                return "fixé à la pièce noyée"
            return "fixé à la pièce à sceller"
        return f"FR:{text}"

    def translate_batch(self, texts, target_lang, system_prompt, source_lang="zh"):
        self.prompts.append(system_prompt)
        self.batches.append(list(texts))
        return {text: self._translate(text, system_prompt) for text in texts}

    def translate_batch_with_sources(self, texts, target_lang, system_prompt, source_lang="auto"):
        self.prompts.append(system_prompt)
        self.batches.append(list(texts))
        return [
            TranslationLanguageResult(text, self._translate(text, system_prompt), "zh", target_lang, True)
            for text in texts
        ]


class TermAwareEngineTests(unittest.TestCase):
    def setUp(self):
        self.index = TermIndex([
            entry(4, "预埋件", "pièce noyée", usage="always"),
            entry(5, "甲", "classe A", usage="scene", note="耐火等级"),
        ])

    def test_batches_without_terms_pass_through_unchanged(self):
        inner = FakeEngine()
        engine = TermAwareEngine(inner, self.index, target_lang="fr")
        self.assertEqual(engine.translate_batch(["说明"], "fr", "SYS"), {"说明": "FR:说明"})
        self.assertEqual(inner.prompts, ["SYS"])
        self.assertEqual(engine.stats.outcomes(), [])

    def test_retry_until_the_term_is_applied(self):
        inner = FakeEngine(comply_on_attempt=3)
        engine = TermAwareEngine(inner, self.index, target_lang="fr", retry_attempts=2)
        result = engine.translate_batch(["说明", "门框与预埋件连接"], "fr", "SYS")
        self.assertEqual(result["门框与预埋件连接"], "fixé à la pièce noyée")
        self.assertEqual(result["说明"], "FR:说明")
        self.assertIn("- [第 2 条] 预埋件 → pièce noyée", inner.prompts[0])
        self.assertIn("上次译文：fixé à la pièce à sceller", inner.prompts[-1])
        self.assertEqual(len(inner.prompts), 3)
        summary = engine.stats.summary()
        self.assertEqual(summary["applied_after_retry"], 1)
        self.assertEqual(summary["not_applied"], [])
        self.assertEqual(engine.stats.entry_uses(), {4: 1})

    def test_not_applied_after_all_retries_keeps_the_first_translation(self):
        inner = FakeEngine(comply_on_attempt=99)
        engine = TermAwareEngine(inner, self.index, target_lang="fr", retry_attempts=2)
        result = engine.translate_batch(["门框与预埋件连接"], "fr", "SYS")
        self.assertEqual(result["门框与预埋件连接"], "fixé à la pièce à sceller")
        self.assertEqual(engine.stats.not_applied_texts(), {"门框与预埋件连接"})
        summary = engine.stats.summary()
        self.assertEqual(summary["not_applied"][0]["retries"], 2)
        self.assertEqual(summary["not_applied"][0]["terms"], [{"source": "预埋件", "expected": "pièce noyée"}])
        self.assertEqual(engine.stats.entry_uses(), {})

    def test_zero_retries_marks_directly(self):
        inner = FakeEngine(comply_on_attempt=2)
        engine = TermAwareEngine(inner, self.index, target_lang="fr", retry_attempts=0)
        engine.translate_batch(["门框与预埋件连接"], "fr", "SYS")
        self.assertEqual(len(inner.prompts), 1)
        self.assertEqual(engine.stats.not_applied_texts(), {"门框与预埋件连接"})

    def test_with_sources_path_replaces_the_retried_item(self):
        inner = FakeEngine(comply_on_attempt=2)
        engine = TermAwareEngine(inner, self.index, target_lang="fr")
        items = engine.translate_batch_with_sources(["说明", "门框与预埋件连接"], "fr", "SYS")
        self.assertEqual([item.source_text for item in items], ["说明", "门框与预埋件连接"])
        self.assertEqual(items[1].translation, "fixé à la pièce noyée")
        self.assertTrue(items[1].tm_eligible)

    def test_local_models_get_only_always_terms(self):
        inner = FakeEngine()
        engine = TermAwareEngine(inner, self.index, target_lang="fr", include_scene=False)
        engine.translate_batch(["甲"], "fr", "SYS")
        self.assertEqual(inner.prompts, ["SYS"])

    def test_scene_choice_is_reported(self):
        inner = FakeEngine()
        engine = TermAwareEngine(inner, self.index, target_lang="fr")
        engine.translate_batch(["甲"], "fr", "SYS")
        choices = engine.stats.summary()["scene_choices"]
        self.assertEqual(choices[0]["source"], "甲")
        self.assertEqual(choices[0]["chosen"], [{"target": "FR:甲", "count": 1}])


if __name__ == "__main__":
    unittest.main()
