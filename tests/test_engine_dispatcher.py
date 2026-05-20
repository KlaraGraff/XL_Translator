from __future__ import annotations

import unittest

from core.engine_dispatcher import TranslationBatchRunStats, translate_texts
from engines.base_engine import TranslationEngine


class FakeExcelEngine(TranslationEngine):
    def __init__(self, *, omit_last_for_multi: bool = False) -> None:
        self.omit_last_for_multi = omit_last_for_multi
        self.calls: list[list[str]] = []

    @property
    def engine_name(self) -> str:
        return "fake/excel"

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
        return {text: f"translated:{len(text)}" for text in texts}


class EngineDispatcherTests(unittest.TestCase):
    def test_translate_texts_splits_large_payloads_by_character_budget(self) -> None:
        engine = FakeExcelEngine()
        stats = TranslationBatchRunStats()
        texts = ["a" * 1500, "b" * 1500, "c" * 1500]

        result = translate_texts(
            texts,
            engine,
            "fr",
            "system prompt",
            batch_size=20,
            concurrency=1,
            source_lang="en",
            stats=stats,
        )

        self.assertEqual(len(result), 3)
        self.assertEqual([len(call) for call in engine.calls], [2, 1])
        self.assertEqual(stats.batch_count, 2)
        self.assertGreaterEqual(stats.max_request_weight, 2)

    def test_translate_texts_retries_smaller_batches_when_response_is_incomplete(self) -> None:
        engine = FakeExcelEngine(omit_last_for_multi=True)
        stats = TranslationBatchRunStats()
        errors: list[str] = []

        result = translate_texts(
            ["alpha", "beta"],
            engine,
            "fr",
            "system prompt",
            batch_size=20,
            concurrency=1,
            error_callback=errors.append,
            source_lang="en",
            stats=stats,
        )

        self.assertEqual(result["alpha"], "translated:5")
        self.assertEqual(result["beta"], "translated:4")
        self.assertEqual([len(call) for call in engine.calls], [2, 1, 1])
        self.assertGreaterEqual(stats.retry_count, 1)
        self.assertTrue(errors)


if __name__ == "__main__":
    unittest.main(verbosity=2)
