from __future__ import annotations

import unittest
from unittest.mock import patch

from core.api_concurrency_control import ApiKeyTemporarilyUnavailableError
from core.engine_dispatcher import TranslationBatchRunStats, build_engine, translate_texts
from engines.base_engine import TranslationEngine
from settings import AppSettings, EngineSettings


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


class ConcurrencyLimitExcelEngine(TranslationEngine):
    def __init__(self, *, fail_count: int) -> None:
        self.fail_count = fail_count
        self.calls: list[list[str]] = []

    @property
    def engine_name(self) -> str:
        return "fake/excel-cloud"

    def translate_batch(
        self,
        texts: list[str],
        target_lang: str,
        system_prompt: str,
        source_lang: str = "zh",
    ) -> dict[str, str]:
        self.calls.append(list(texts))
        if self.fail_count > 0:
            self.fail_count -= 1
            raise RuntimeError("too many concurrent requests: concurrency limit reached")
        return {text: f"translated:{text}" for text in texts}


class PermanentFailureExcelEngine(TranslationEngine):
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    @property
    def engine_name(self) -> str:
        return "fake/permanent-failure"

    def translate_batch(
        self,
        texts: list[str],
        target_lang: str,
        system_prompt: str,
        source_lang: str = "zh",
    ) -> dict[str, str]:
        self.calls.append(list(texts))
        exc = RuntimeError("401 unauthorized: invalid API key")
        exc.status_code = 401
        raise exc


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

    def test_translate_texts_retries_same_batch_after_concurrency_limit(self) -> None:
        engine = ConcurrencyLimitExcelEngine(fail_count=1)
        stats = TranslationBatchRunStats()
        errors: list[str] = []

        result = translate_texts(
            ["alpha", "beta"],
            engine,
            "fr",
            "system prompt",
            batch_size=20,
            concurrency=5,
            error_callback=errors.append,
            source_lang="en",
            stats=stats,
        )

        self.assertEqual(result["alpha"], "translated:alpha")
        self.assertEqual(result["beta"], "translated:beta")
        self.assertEqual(engine.calls, [["alpha", "beta"], ["alpha", "beta"]])
        self.assertEqual(stats.retry_count, 0)
        self.assertEqual(stats.adaptive_concurrency_reductions, 1)
        self.assertEqual(stats.adaptive_lowest_concurrency, 4)
        self.assertTrue(any("降至 4" in message for message in errors))

    def test_translate_texts_reports_key_unavailable_at_minimum_capacity(self) -> None:
        engine = ConcurrencyLimitExcelEngine(fail_count=2)

        with self.assertRaises(ApiKeyTemporarilyUnavailableError):
            translate_texts(
                ["alpha"],
                engine,
                "fr",
                "system prompt",
                batch_size=20,
                concurrency=1,
                source_lang="en",
            )

    def test_permanent_auth_failure_does_not_recursively_split_batch(self) -> None:
        engine = PermanentFailureExcelEngine()
        stats = TranslationBatchRunStats()

        result = translate_texts(
            ["alpha", "beta", "gamma"],
            engine,
            "fr",
            "system prompt",
            batch_size=20,
            concurrency=1,
            source_lang="en",
            stats=stats,
        )

        self.assertEqual(engine.calls, [["alpha", "beta", "gamma"]])
        self.assertEqual(result, {text: text for text in ("alpha", "beta", "gamma")})
        self.assertEqual(stats.failed_batch_count, 3)

    def test_build_engine_uses_lm_studio_as_local_openai_provider(self) -> None:
        settings = AppSettings(
            engine=EngineSettings(
                mode="local",
                local_provider="lm_studio",
                local_model="qwen-local",
                local_base_url="http://localhost:1234/v1",
            )
        )

        with (
            patch("core.engine_dispatcher.get_key", return_value=""),
            patch("engines.openai_engine.OpenAIEngine") as engine_cls,
        ):
            build_engine(settings)

        self.assertEqual(engine_cls.call_args.kwargs["api_key"], "local-model")
        self.assertEqual(engine_cls.call_args.kwargs["model"], "qwen-local")
        self.assertEqual(engine_cls.call_args.kwargs["base_url"], "http://localhost:1234/v1")
        self.assertEqual(
            engine_cls.call_args.kwargs["engine_name_prefix"],
            "local_openai/lm_studio",
        )

    def test_openai_engine_uses_httpx_without_sdk_client(self) -> None:
        from engines.openai_engine import OpenAIEngine

        engine = OpenAIEngine(api_key="key", model="model")

        self.assertEqual(engine._base_url, "https://api.openai.com/v1")
        self.assertFalse(hasattr(engine, "_client"))

    def test_claude_engine_uses_httpx_without_sdk_client(self) -> None:
        from engines.claude_engine import ClaudeEngine

        engine = ClaudeEngine(api_key="key", model="model")

        self.assertEqual(engine._base_url, "https://api.anthropic.com/v1")
        self.assertFalse(hasattr(engine, "_client"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
