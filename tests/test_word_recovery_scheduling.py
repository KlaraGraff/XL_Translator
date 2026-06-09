from __future__ import annotations

import unittest

from core.word_task_runner import _MainTranslationDrainGate, _WordRecoveryPool
from engines.base_engine import TranslationEngine
from settings import WordBatchSettings


class RetryRecoveryEngine(TranslationEngine):
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    @property
    def engine_name(self) -> str:
        return "fake/retry-recovery"

    def translate_batch(
        self,
        texts: list[str],
        target_lang: str,
        system_prompt: str,
        source_lang: str = "zh",
    ) -> dict[str, str]:
        self.calls.append(list(texts))
        return {text: "Wall construction requirements" for text in texts}


class WordRecoverySchedulingTests(unittest.TestCase):
    def test_recovery_waits_until_all_main_queues_are_drained(self) -> None:
        engine = RetryRecoveryEngine()
        pool = _WordRecoveryPool(
            engine=engine,
            target_lang="en",
            retry_prompt="retry",
            retry_batch_settings=WordBatchSettings(max_paragraphs_per_batch=1),
            retry_attempts=3,
            source_lang="zh",
            api_scheduler=None,
            concurrency=2,
            should_stop=lambda: False,
            enable_semantic=False,
            defer_until_started=True,
        )
        gate = _MainTranslationDrainGate(queue_count=2, on_all_drained=pool.start)

        pool.add_candidate("墙体施工要求", "")
        gate.queue_drained()

        self.assertEqual(engine.calls, [])

        gate.queue_drained()
        outcome = pool.wait_for_completion()

        self.assertEqual(engine.calls, [["墙体施工要求"]])
        self.assertEqual(outcome.accepted_translations["墙体施工要求"], "Wall construction requirements")
        self.assertEqual(outcome.unresolved_sources, [])

    def test_deferred_recovery_completes_when_task_stopped_before_start(self) -> None:
        engine = RetryRecoveryEngine()
        stopped = False
        pool = _WordRecoveryPool(
            engine=engine,
            target_lang="en",
            retry_prompt="retry",
            retry_batch_settings=WordBatchSettings(max_paragraphs_per_batch=1),
            retry_attempts=3,
            source_lang="zh",
            api_scheduler=None,
            concurrency=2,
            should_stop=lambda: stopped,
            enable_semantic=False,
            defer_until_started=True,
        )

        pool.add_candidate("墙体施工要求", "")
        stopped = True
        outcome = pool.wait_for_completion()

        self.assertEqual(engine.calls, [])
        self.assertEqual(outcome.unresolved_sources, ["墙体施工要求"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
