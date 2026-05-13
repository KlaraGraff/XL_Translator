from __future__ import annotations

import unittest

from core.word_batching import WordBatchRunStats, translate_word_texts
from engines.base_engine import TranslationEngine
from settings import WordBatchSettings


class FakeWordEngine(TranslationEngine):
    def __init__(self, *, omit_last_for_multi: bool = False) -> None:
        self.omit_last_for_multi = omit_last_for_multi
        self.calls: list[list[str]] = []

    @property
    def engine_name(self) -> str:
        return "fake/word"

    def translate_batch(
        self,
        texts: list[str],
        target_lang: str,
        system_prompt: str,
        source_lang: str = "zh",
    ) -> dict[str, str]:
        self.calls.append(list(texts))
        if self.omit_last_for_multi and len(texts) > 1:
            texts = texts[:-1]
        return {text: f"译文:{text}" for text in texts}


class WordBatchingTests(unittest.TestCase):
    def test_word_batches_respect_character_budget(self) -> None:
        settings = WordBatchSettings()
        settings.max_paragraphs_per_batch = 4
        settings.max_chars_per_batch = 10
        settings.split_paragraph_chars = 1000
        engine = FakeWordEngine()

        result = translate_word_texts(
            ["甲甲甲甲", "乙乙乙乙", "丙丙丙丙", "丁丁丁丁"],
            engine,
            "fr",
            "system",
            settings,
            concurrency=1,
            source_lang="zh",
        )

        self.assertEqual(len(result), 4)
        self.assertEqual([len(call) for call in engine.calls], [2, 2])

    def test_word_batches_retry_missing_multi_item_response_as_smaller_batches(self) -> None:
        settings = WordBatchSettings()
        settings.max_paragraphs_per_batch = 4
        settings.max_chars_per_batch = 1000
        settings.split_paragraph_chars = 2000
        stats = WordBatchRunStats()
        errors: list[str] = []
        engine = FakeWordEngine(omit_last_for_multi=True)

        result = translate_word_texts(
            ["第一段", "第二段", "第三段"],
            engine,
            "fr",
            "system",
            settings,
            concurrency=1,
            error_callback=errors.append,
            source_lang="zh",
            stats=stats,
        )

        self.assertEqual(result["第一段"], "译文:第一段")
        self.assertEqual(result["第二段"], "译文:第二段")
        self.assertEqual(result["第三段"], "译文:第三段")
        self.assertGreaterEqual(stats.retry_count, 2)
        self.assertTrue(any(len(call) == 1 for call in engine.calls))
        self.assertTrue(errors)

    def test_word_batches_split_very_long_paragraphs_before_translation(self) -> None:
        settings = WordBatchSettings()
        settings.max_paragraphs_per_batch = 4
        settings.max_chars_per_batch = 24
        settings.split_paragraph_chars = 30
        stats = WordBatchRunStats()
        engine = FakeWordEngine()
        source = "第一句用于说明施工范围和参数。第二句继续描述材料要求；第三句补充质量验收要求。"

        result = translate_word_texts(
            [source],
            engine,
            "fr",
            "system",
            settings,
            concurrency=1,
            source_lang="zh",
            stats=stats,
        )

        self.assertEqual(stats.split_source_count, 1)
        translated_chunks = [chunk for call in engine.calls for chunk in call]
        self.assertGreater(len(translated_chunks), 1)
        self.assertTrue(all(len(chunk) < len(source) for chunk in translated_chunks))
        self.assertIn("译文:第一句", result[source])
        self.assertGreater(result[source].count("译文:"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
