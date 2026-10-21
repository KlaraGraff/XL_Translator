from __future__ import annotations

import errno
import threading
import time
import unittest
from unittest.mock import patch

from core.api_concurrency_control import ApiCredentialRejectedError, ApiKeyTemporarilyUnavailableError
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


class LocalDescriptorLimitExcelEngine(ConcurrencyLimitExcelEngine):
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
            raise OSError(errno.EMFILE, "Too many open files")
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
        self.assertTrue(any("已自动降低发送速度" in message for message in errors), errors)

    def test_slowdown_notice_names_the_calling_page(self) -> None:
        # CAD、PDF、PPT 共用这一套批次翻译，提示开头以前一律写成「Excel」。
        errors: list[str] = []
        translate_texts(
            ["alpha"],
            ConcurrencyLimitExcelEngine(fail_count=1),
            "fr",
            "system prompt",
            batch_size=20,
            concurrency=5,
            error_callback=errors.append,
            source_lang="en",
            context_label="PDF",
        )

        self.assertTrue(errors)
        self.assertTrue(all(message.startswith("PDF ") for message in errors), errors)

    def test_translate_texts_retries_same_batch_after_local_descriptor_limit(self) -> None:
        engine = LocalDescriptorLimitExcelEngine(fail_count=1)
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

        self.assertEqual(result, {"alpha": "translated:alpha", "beta": "translated:beta"})
        self.assertEqual(engine.calls, [["alpha", "beta"], ["alpha", "beta"]])
        self.assertEqual(stats.retry_count, 0)
        self.assertEqual(stats.adaptive_concurrency_reductions, 1)
        self.assertTrue(any("本机同时打开的网络连接过多" in message and "降低速度" in message for message in errors))

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

        # 一把被拒的 Key 不是「这一批坏了」：整个任务直接失败并说清原因，而不是每批
        # 退回原文、最后写出一份全是原文的文件再报「N 处需复核」。
        with self.assertRaises(ApiCredentialRejectedError) as caught:
            translate_texts(
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
        self.assertIn("API 密钥", str(caught.exception))
        self.assertEqual(getattr(caught.exception, "partial_results", {}), {})

    def test_missing_model_still_falls_back_without_splitting(self) -> None:
        # 只有 Key/账户被拒才整任务失败；模型名写错这类 404 仍按原来的方式记为未翻译。
        engine = PermanentFailureExcelEngine()
        stats = TranslationBatchRunStats()

        def missing_model(texts, *_args, **_kwargs):
            engine.calls.append(list(texts))
            exc = RuntimeError("404 model not found")
            exc.status_code = 404
            raise exc

        engine.translate_batch = missing_model
        result = translate_texts(
            ["alpha", "beta"], engine, "fr", "system prompt",
            batch_size=20, concurrency=1, source_lang="en", stats=stats,
        )
        self.assertEqual(engine.calls, [["alpha", "beta"]])
        self.assertEqual(result, {"alpha": "alpha", "beta": "beta"})
        self.assertEqual(stats.untranslated_count, 2)

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

        with self.assertRaises(ApiCredentialRejectedError):
            translate_texts_with_sources(
                ["alpha", "beta", "gamma"],
                engine,
                "fr",
                "system prompt",
                batch_size=20,
                concurrency=1,
                stats=stats,
            )

        self.assertEqual(engine.calls, [["alpha", "beta", "gamma"]])
        self.assertEqual(stats.retry_count, 0)

    def test_with_sources_sends_cloud_batches_concurrently(self) -> None:
        # 自动识别源语言（Excel 的默认）以前逐批串行，并发设置形同虚设：
        # 一份 30 批的表格比指定源语言慢了 7 倍。
        class SlowSourcesEngine(SourcesReportingEngine):
            def __init__(self) -> None:
                super().__init__()
                self.lock = threading.Lock()
                self.inflight = 0
                self.max_inflight = 0

            def translate_batch_with_sources(self, texts, target_lang, system_prompt, source_lang="auto"):
                with self.lock:
                    self.inflight += 1
                    self.max_inflight = max(self.max_inflight, self.inflight)
                time.sleep(0.05)
                with self.lock:
                    self.inflight -= 1
                return super().translate_batch_with_sources(texts, target_lang, system_prompt, source_lang)

        engine = SlowSourcesEngine()
        texts = [f"item {index}" for index in range(12)]
        progress: list[tuple[int, int]] = []

        result = translate_texts_with_sources(
            texts,
            engine,
            "fr",
            "system prompt",
            batch_size=2,
            concurrency=4,
            progress_callback=lambda done, total: progress.append((done, total)),
        )

        self.assertEqual(set(result), set(texts))
        self.assertEqual(len(engine.calls), 6)
        self.assertGreater(engine.max_inflight, 1)
        self.assertLessEqual(engine.max_inflight, 4)
        self.assertEqual(progress[-1], (12, 12))
        self.assertEqual([done for done, _ in progress], sorted(done for done, _ in progress))

    def test_with_sources_keeps_paid_batches_when_a_concurrent_batch_is_fatal(self) -> None:
        class OneFatalBatchEngine(SourcesReportingEngine):
            def translate_batch_with_sources(self, texts, target_lang, system_prompt, source_lang="auto"):
                if "bad" in texts:
                    raise ApiKeyTemporarilyUnavailableError("rate limited")
                return super().translate_batch_with_sources(texts, target_lang, system_prompt, source_lang)

        with self.assertRaises(ApiKeyTemporarilyUnavailableError) as raised:
            translate_texts_with_sources(
                ["alpha", "bad", "gamma"],
                OneFatalBatchEngine(),
                "fr",
                "system prompt",
                batch_size=1,
                concurrency=3,
            )

        self.assertEqual(set(raised.exception.partial_results), {"alpha", "gamma"})

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


class _TransportFailingEngine(TranslationEngine):
    """Fails like the OpenAI-compatible text transport: every error no_replay."""

    def __init__(self, *, failures: list[Exception]) -> None:
        self.failures = list(failures)
        self.calls: list[list[str]] = []

    @property
    def engine_name(self) -> str:
        return "fake/transport"

    def translate_batch(self, texts, target_lang, system_prompt, source_lang="zh"):
        self.calls.append(list(texts))
        if self.failures:
            raise self.failures.pop(0)
        return {text: f"ok:{text}" for text in texts}

    def translate_batch_with_sources(self, texts, target_lang, system_prompt, source_lang="auto"):
        return [
            TranslationLanguageResult(text, translated, source_lang="zh", target_lang=target_lang)
            for text, translated in self.translate_batch(texts, target_lang, system_prompt).items()
        ]


def _transport_http_error(status: int) -> Exception:
    import httpx

    from core.text_transport import TextTransportHTTPError

    request = httpx.Request("POST", "https://service.test/v1/chat/completions")
    response = httpx.Response(status, text="{}", request=request)
    return TextTransportHTTPError(f"HTTP {status}", request=request, response=response, kind="transient")


class TransientTransportRetryTests(unittest.TestCase):
    """A 5xx or a timeout from the text transport used to drop the batch on the
    first try and say it 「无法通过重试解决」."""

    def setUp(self) -> None:
        patcher = patch("core.engine_dispatcher.time.sleep")
        self.sleeps = patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_503_is_resent_as_the_same_batch(self) -> None:
        engine = _TransportFailingEngine(failures=[_transport_http_error(503)])
        errors: list[str] = []
        stats = TranslationBatchRunStats()
        result = translate_texts(
            ["alpha", "beta"], engine, "fr", "system prompt",
            batch_size=20, concurrency=1, error_callback=errors.append, source_lang="en", stats=stats,
        )
        self.assertEqual(engine.calls, [["alpha", "beta"], ["alpha", "beta"]])
        self.assertEqual(result, {"alpha": "ok:alpha", "beta": "ok:beta"})
        self.assertEqual(stats.untranslated_count, 0)
        self.assertEqual((stats.transient_retry_count, stats.retry_count), (1, 0))
        self.assertEqual(len(errors), 1)
        self.assertIn("稍后自动重试（第 1/2 次）", errors[0])
        self.assertTrue(self.sleeps.called)

    def test_a_timeout_is_resent_and_the_last_failure_says_it_was_retried(self) -> None:
        from core.text_transport import TextTransportError

        engine = _TransportFailingEngine(failures=[
            TextTransportError("read_timeout：请求超时", kind="read_timeout") for _ in range(5)
        ])
        errors: list[str] = []
        stats = TranslationBatchRunStats()
        result = translate_texts(
            ["alpha", "beta"], engine, "fr", "system prompt",
            batch_size=20, concurrency=1, error_callback=errors.append, source_lang="en", stats=stats,
        )
        # 1 try + 2 resends of the whole batch, then one halving; the halves
        # do not resend on their own.
        self.assertEqual(engine.calls, [["alpha", "beta"]] * 3 + [["alpha"], ["beta"]])
        self.assertEqual(result, {"alpha": "alpha", "beta": "beta"})
        self.assertEqual(stats.untranslated_count, 2)
        self.assertEqual(stats.retry_count, 1)
        self.assertIn("已拆分为更小的批次重试", errors[2])
        self.assertIn("该批 1 条内容已保留原文", errors[-1])
        self.assertFalse(any("无法通过重试" in message for message in errors))

    def test_a_batch_that_keeps_timing_out_is_halved_and_the_halves_translated(self) -> None:
        from core.text_transport import TextTransportError

        engine = _TransportFailingEngine(failures=[
            TextTransportError("模型服务在 180 秒内未返回完整结果。", kind="total_timeout")
            for _ in range(3)
        ])
        stats = TranslationBatchRunStats()
        result = translate_texts(
            ["alpha", "beta", "gamma"], engine, "fr", "system prompt",
            batch_size=20, concurrency=1, source_lang="en", stats=stats,
        )
        self.assertEqual(engine.calls[3:], [["alpha"], ["beta", "gamma"]])
        self.assertEqual(result, {text: f"ok:{text}" for text in ("alpha", "beta", "gamma")})
        self.assertEqual(stats.untranslated_count, 0)

    def test_a_refused_request_is_not_resent(self) -> None:
        engine = _TransportFailingEngine(failures=[_transport_http_error(422)])
        errors: list[str] = []
        translate_texts(
            ["alpha"], engine, "fr", "system prompt",
            batch_size=20, concurrency=1, error_callback=errors.append, source_lang="en",
        )
        self.assertEqual(len(engine.calls), 1)
        self.assertEqual(len(errors), 1)
        self.assertIn("一批翻译请求未成功", errors[0])

    def test_the_sources_path_resends_too(self) -> None:
        engine = _TransportFailingEngine(failures=[_transport_http_error(502)])
        result = translate_texts_with_sources(
            ["alpha"], engine, "fr", "system prompt", batch_size=20, concurrency=1,
        )
        self.assertEqual(len(engine.calls), 2)
        self.assertEqual(result["alpha"].translation, "ok:alpha")

    def test_the_retry_count_follows_the_setting(self) -> None:
        for retries in (0, 4):
            with self.subTest(retries=retries):
                engine = _TransportFailingEngine(failures=[_transport_http_error(503) for _ in range(9)])
                errors: list[str] = []
                translate_texts(
                    ["alpha"], engine, "fr", "system prompt", batch_size=20, concurrency=1,
                    error_callback=errors.append, source_lang="en", transient_retries=retries,
                )
                self.assertEqual(len(engine.calls), retries + 1)
                expected = f"重试 {retries} 次后仍失败" if retries else "一批翻译请求未成功"
                self.assertIn(expected, errors[-1])

    def test_the_first_wait_outlasts_the_transport_error_cache(self) -> None:
        # Resending inside the cache window only gets the cached failure back.
        from core.engine_dispatcher import _TRANSIENT_RETRY_BASE_DELAY
        from core.text_transport import FAILED_REQUEST_CACHE_SECONDS

        self.assertGreater(_TRANSIENT_RETRY_BASE_DELAY * 0.75, FAILED_REQUEST_CACHE_SECONDS)


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


class _ResendEngine(TranslationEngine):
    """First answer keeps "Acme" untouched; the second batch is slow."""

    def __init__(self) -> None:
        self.calls: list[tuple[float, list[str]]] = []
        self._lock = threading.Lock()
        self._asked: set[str] = set()

    @property
    def engine_name(self) -> str:
        return "fake/resend"

    def translate_batch(self, texts, target_lang, system_prompt, source_lang="zh"):
        with self._lock:
            self.calls.append((time.monotonic(), list(texts)))
            first = "Acme" not in self._asked
            self._asked.update(texts)
        if "slow" in texts[0]:
            time.sleep(0.6)
        return {text: (text if text == "Acme" and first else f"译{text}") for text in texts}


class ResendUnchangedTests(unittest.TestCase):
    def test_a_batch_resends_its_untouched_words_without_waiting_for_the_others(self) -> None:
        engine = _ResendEngine()
        started = time.monotonic()
        result = translate_texts(
            ["Acme", "door", "slow one", "slow two"], engine, "zh", "prompt",
            batch_size=2, concurrency=2, source_lang="en",
            resend_unchanged=lambda text, translated: translated in (None, text),
        )
        self.assertEqual(result["Acme"], "译Acme")
        self.assertEqual(result["slow one"], "译slow one")
        resend = [at for at, texts in engine.calls if texts == ["Acme"]]
        self.assertEqual(len(resend), 1)
        self.assertLess(resend[0] - started, 0.5)   # long before the slow batch came back

    def test_without_the_hook_nothing_is_resent(self) -> None:
        engine = _ResendEngine()
        result = translate_texts(["Acme", "door"], engine, "zh", "prompt", batch_size=2, concurrency=1,
                                 source_lang="en")
        self.assertEqual(len(engine.calls), 1)
        self.assertNotEqual(result.get("Acme"), "译Acme")
