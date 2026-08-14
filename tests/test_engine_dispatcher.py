from __future__ import annotations

import unittest
from unittest.mock import patch

from core.api_concurrency_control import ApiKeyTemporarilyUnavailableError
from core.engine_dispatcher import (
    TranslationBatchRunStats,
    build_engine,
    translate_texts,
    translate_texts_with_sources,
)
from core.language_preflight import TranslationLanguageResult
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


class AlwaysFailingExcelEngine(TranslationEngine):
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    @property
    def engine_name(self) -> str:
        return "fake/always-failing"

    def translate_batch(
        self,
        texts: list[str],
        target_lang: str,
        system_prompt: str,
        source_lang: str = "zh",
    ) -> dict[str, str]:
        self.calls.append(list(texts))
        raise RuntimeError("upstream 503: service unavailable")


class AlwaysFailingSourcesEngine(TranslationEngine):
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    @property
    def engine_name(self) -> str:
        return "fake/always-failing-sources"

    def translate_batch(self, texts, target_lang, system_prompt, source_lang="zh"):
        raise AssertionError("the automatic-language path must not call translate_batch")

    def translate_batch_with_sources(
        self, texts, target_lang, system_prompt, source_lang="auto"
    ):
        self.calls.append(list(texts))
        raise RuntimeError("upstream 503: service unavailable")


class PermanentFailureSourcesEngine(TranslationEngine):
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    @property
    def engine_name(self) -> str:
        return "fake/permanent-sources"

    def translate_batch(self, texts, target_lang, system_prompt, source_lang="zh"):
        raise AssertionError("the automatic-language path must not call translate_batch")

    def translate_batch_with_sources(
        self, texts, target_lang, system_prompt, source_lang="auto"
    ):
        self.calls.append(list(texts))
        exc = RuntimeError("401 unauthorized: invalid API key")
        exc.status_code = 401
        raise exc


class SourcesReportingEngine(TranslationEngine):
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    @property
    def engine_name(self) -> str:
        return "fake/sources"

    def translate_batch(self, texts, target_lang, system_prompt, source_lang="zh"):
        raise AssertionError("the automatic-language path must not call translate_batch")

    def translate_batch_with_sources(
        self, texts, target_lang, system_prompt, source_lang="auto"
    ):
        self.calls.append(list(texts))
        return [
            TranslationLanguageResult(
                text,
                f"{target_lang}:{text}",
                source_lang="en",
                target_lang=target_lang,
            )
            for text in texts
        ]


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
        # The user is told we slowed down; the internal capacity numbers stay
        # in the debug log, because they are group-level and match nothing the
        # user configured.
        self.assertTrue(any("已自动放慢发送速度" in message for message in errors), errors)

    def test_translate_texts_waits_out_a_limit_at_minimum_capacity(self) -> None:
        # A key already walked down to the minimum cap is the normal state of a
        # busy account; the run must wait rather than throw away every request
        # already paid for.
        engine = ConcurrencyLimitExcelEngine(fail_count=2)

        with patch("core.api_concurrency_control.MINIMUM_CAPACITY_BASE_DELAY", 0.01), \
             patch("core.api_concurrency_control.MINIMUM_CAPACITY_MAX_DELAY", 0.01):
            result = translate_texts(
                ["alpha"],
                engine,
                "fr",
                "system prompt",
                batch_size=20,
                concurrency=1,
                source_lang="en",
            )

        self.assertEqual(result, {"alpha": "translated:alpha"})
        self.assertEqual(len(engine.calls), 3)

    def test_translate_texts_reports_key_unavailable_after_the_grace_window(self) -> None:
        engine = ConcurrencyLimitExcelEngine(fail_count=10**6)

        with patch("core.api_concurrency_control.MINIMUM_CAPACITY_BASE_DELAY", 0.01), \
             patch("core.api_concurrency_control.MINIMUM_CAPACITY_MAX_DELAY", 0.01), \
             patch("core.api_concurrency_control.MINIMUM_CAPACITY_GRACE_SECONDS", 0.05), \
             self.assertRaises(ApiKeyTemporarilyUnavailableError):
            translate_texts(
                ["alpha"],
                engine,
                "fr",
                "system prompt",
                batch_size=20,
                concurrency=1,
                source_lang="en",
            )

        # It kept trying instead of dying on the first at-minimum 429.
        self.assertGreater(len(engine.calls), 1)

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

    def test_a_batch_that_always_fails_is_reported_as_untranslated(self) -> None:
        # The file still gets the source text so the run can finish, but the
        # count must reach the task result: a user must never receive source
        # text presented as translation with no warning anywhere.
        engine = AlwaysFailingExcelEngine()
        stats = TranslationBatchRunStats()
        errors: list[str] = []

        with patch("core.engine_dispatcher._SPLIT_RETRY_BASE_DELAY", 0.0), \
             patch("core.engine_dispatcher._SPLIT_RETRY_MAX_DELAY", 0.0):
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

        self.assertEqual(result, {"alpha": "alpha", "beta": "beta"})
        self.assertEqual(stats.untranslated_count, 2)
        self.assertEqual(
            sorted(item["source"] for item in stats.failed_items),
            ["alpha", "beta"],
        )
        self.assertTrue(any("未能翻译" in message for message in errors))

    def test_bisecting_stops_at_the_depth_cap(self) -> None:
        # 30 items used to bisect down to singletons: 59 nodes, each with its
        # own tenacity budget. The cap keeps the tree at 15 nodes.
        engine = AlwaysFailingExcelEngine()
        stats = TranslationBatchRunStats()
        texts = [f"item-{index:02d}" for index in range(30)]

        with patch("core.engine_dispatcher._SPLIT_RETRY_BASE_DELAY", 0.0), \
             patch("core.engine_dispatcher._SPLIT_RETRY_MAX_DELAY", 0.0):
            result = translate_texts(
                texts,
                engine,
                "fr",
                "system prompt",
                batch_size=30,
                concurrency=1,
                source_lang="en",
                stats=stats,
            )

        self.assertEqual(len(engine.calls), 15)
        self.assertEqual(stats.untranslated_count, 30)
        self.assertEqual(result, {text: text for text in texts})
        self.assertTrue(all(len(call) > 1 for call in engine.calls[-8:]))

    def test_split_retries_back_off_before_recursing(self) -> None:
        engine = AlwaysFailingExcelEngine()
        delays: list[float] = []

        with patch("core.engine_dispatcher.time.sleep", side_effect=delays.append):
            translate_texts(
                ["alpha", "beta"],
                engine,
                "fr",
                "system prompt",
                batch_size=20,
                concurrency=1,
                source_lang="en",
            )

        self.assertTrue(delays)
        self.assertGreater(sum(delays), 0.0)

    def test_with_sources_marks_untranslated_items_and_keeps_them_out_of_tm(self) -> None:
        engine = AlwaysFailingSourcesEngine()
        stats = TranslationBatchRunStats()

        with patch("core.engine_dispatcher._SPLIT_RETRY_BASE_DELAY", 0.0), \
             patch("core.engine_dispatcher._SPLIT_RETRY_MAX_DELAY", 0.0):
            result = translate_texts_with_sources(
                ["alpha", "beta"],
                engine,
                "fr",
                "system prompt",
                batch_size=20,
                concurrency=1,
                stats=stats,
            )

        self.assertEqual(stats.untranslated_count, 2)
        self.assertEqual({item.source_lang for item in result.values()}, {"und"})
        self.assertFalse(any(item.tm_eligible for item in result.values()))

    def test_with_sources_does_not_split_a_permanent_failure(self) -> None:
        # This variant never consulted _is_permanent_request_error, so a
        # rejected key still cost a full bisection tree of requests.
        engine = PermanentFailureSourcesEngine()
        stats = TranslationBatchRunStats()

        result = translate_texts_with_sources(
            ["alpha", "beta", "gamma"],
            engine,
            "fr",
            "system prompt",
            batch_size=20,
            concurrency=1,
            stats=stats,
        )

        self.assertEqual(engine.calls, [["alpha", "beta", "gamma"]])
        self.assertEqual(stats.untranslated_count, 3)
        self.assertEqual(stats.retry_count, 0)
        self.assertEqual(set(result), {"alpha", "beta", "gamma"})

    def test_a_failover_wrapper_translates_the_automatic_language_path(self) -> None:
        # Regression for the multi-connection + auto-source-language path that
        # returned an entire document untouched while reporting success.
        from core.failover_engine import FailoverTranslationEngine
        from settings import ModelConnection

        pool = [
            ModelConnection(label="A", provider="custom_openai", base_url="https://a.example/v1"),
            ModelConnection(label="B", provider="custom_openai", base_url="https://b.example/v1"),
        ]
        real = SourcesReportingEngine()
        stats = TranslationBatchRunStats()
        engine = FailoverTranslationEngine(
            build_engine_for=lambda conn: real, candidates=pool
        )

        result = translate_texts_with_sources(
            ["alpha", "beta"],
            engine,
            "fr",
            "system prompt",
            batch_size=20,
            concurrency=1,
            stats=stats,
        )

        self.assertEqual(
            {source: item.translation for source, item in result.items()},
            {"alpha": "fr:alpha", "beta": "fr:beta"},
        )
        self.assertEqual(stats.untranslated_count, 0)

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


class QualityFilterResetTrackingTests(unittest.TestCase):
    """质量校验重置不许静默：每条被回退的原文必须记入 stats，任务层才报得出来。

    历史缺陷：_apply_quality_filter 把不合格译文重置回原文时只写一条汇总
    日志，结果报告里毫无痕迹——用户拿到的文件里那些格子是原文，却以为翻完了。
    """

    def test_reset_items_are_recorded_in_stats(self) -> None:
        from core.engine_dispatcher import _apply_quality_filter

        stats = TranslationBatchRunStats()
        results = {
            "施工方案总说明": " 施工方案总说明 ",  # same_as_source → 重置
            "养护要求": "Exigences de cure",  # 合格译文 → 不动
        }
        _apply_quality_filter(results, "fr", source_lang="zh", stats=stats)

        self.assertEqual(results["施工方案总说明"], "施工方案总说明")
        self.assertEqual(results["养护要求"], "Exigences de cure")
        self.assertEqual(stats.quality_reset_count, 1)
        self.assertEqual(stats.quality_reset_items, ["施工方案总说明"])

    def test_no_stats_object_still_resets_without_error(self) -> None:
        from core.engine_dispatcher import _apply_quality_filter

        results = {"施工方案总说明": " 施工方案总说明 "}
        _apply_quality_filter(results, "fr", source_lang="zh", stats=None)
        self.assertEqual(results["施工方案总说明"], "施工方案总说明")


if __name__ == "__main__":
    unittest.main(verbosity=2)
