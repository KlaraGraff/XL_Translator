from __future__ import annotations

import ast
import inspect
import json
import time
import unittest
from unittest.mock import patch

from core import api_concurrency_control, mixed_language
import threading

from core.api_scheduler import API_REQUEST_CATEGORY_NORMAL, WeightedApiScheduler
from core.mixed_language import (
    MIXED_ACTION_EXISTING_BILINGUAL,
    MIXED_ACTION_FOREIGN_NOISE,
    MIXED_ACTION_TRANSLATE,
    MixedLanguageRunStats,
    _retry_one_mixed_source,
    _translate_mixed_batch_with_fallback,
    classify_mixed_language_source,
    split_mixed_language_sources,
    translate_mixed_language_texts,
)
from engines.base_engine import TranslationEngine


class _BackoffSleepRecorder:
    """记录限流退避拿到的 should_stop，并在这一觉里按下停止。"""

    def __init__(self) -> None:
        self.calls: list = []
        self.stop = threading.Event()

    def record(self, delay: float, should_stop) -> None:  # noqa: ANN001
        self.calls.append(should_stop)
        self.stop.set()


class _StoppedInBackoffEngine(TranslationEngine):
    """每次请求都报 429；退避里停止被置位，醒来后的请求就该直接取消。"""

    @property
    def engine_name(self) -> str:
        return "fake/limit-in-backoff"

    def translate_batch(
        self,
        texts: list[str],
        target_lang: str,
        system_prompt: str,
        source_lang: str = "zh",
    ) -> dict[str, str]:
        return {text: text for text in texts}

    def chat(self, system: str, user: str) -> str:
        raise RuntimeError("HTTP 429: Too Many Requests")


class StructuredMixedEngine(TranslationEngine):
    @property
    def engine_name(self) -> str:
        return "fake/structured-mixed"

    def translate_batch(
        self,
        texts: list[str],
        target_lang: str,
        system_prompt: str,
        source_lang: str = "zh",
    ) -> dict[str, str]:
        return {text: text for text in texts}

    def chat(self, system: str, user: str) -> str:
        payload = json.loads(user)
        results = []
        for item in reversed(payload):
            text = item["text"]
            if text == "内容 Contenu":
                results.append(
                    {
                        "id": item["id"],
                        "action": MIXED_ACTION_EXISTING_BILINGUAL,
                        "translation": "",
                        "note": "already translated",
                    }
                )
            elif text == "沉降观测 asdfg":
                results.append(
                    {
                        "id": item["id"],
                        "action": MIXED_ACTION_FOREIGN_NOISE,
                        "translation": "Observation du tassement",
                        "note": "noise",
                    }
                )
            else:
                results.append(
                    {
                        "id": item["id"],
                        "action": MIXED_ACTION_TRANSLATE,
                        "translation": "Projet BTR",
                        "note": "proper noun",
                    }
                )
        return json.dumps(results, ensure_ascii=False)


class MixedLanguageTests(unittest.TestCase):
    def test_short_label_routes_to_mixed_path(self) -> None:
        decision = classify_mixed_language_source(
            "内容 Contenu",
            target_lang="fr",
            source_lang="zh",
        )

        self.assertTrue(decision.is_mixed)
        self.assertEqual(decision.reason, "short_label")

    def test_long_body_whitelist_allows_units_models_and_abbreviations(self) -> None:
        source = "本工程采用 BTR 工艺，C30 混凝土，DN50 管道，GB/T 标准，厚度 300mm。"

        decision = classify_mixed_language_source(
            source,
            target_lang="fr",
            source_lang="zh",
        )

        self.assertFalse(decision.is_mixed)

    def test_long_body_whitelist_allows_units_attached_to_numbers(self) -> None:
        source = (
            "1.灌木种类：金叶女贞（具体草种由甲方确认）\n"
            "2.灌木规格：高度65-70cm，冠幅35cm（修剪后高度为60cm），9株/平方米\n"
            "3.其他：满足设计图纸、招标文件及相关技术规范要求"
        )

        decision = classify_mixed_language_source(
            source,
            target_lang="fr",
            source_lang="zh",
        )

        self.assertFalse(decision.is_mixed)

    def test_long_body_whitelist_allows_compound_units(self) -> None:
        source = "使用商品有机肥50Kg/亩，草籽按20g/㎡，操作平台荷载不超过1.5kN/m²。"

        decision = classify_mixed_language_source(
            source,
            target_lang="fr",
            source_lang="zh",
        )

        self.assertFalse(decision.is_mixed)

    def test_short_label_keeps_non_unit_abbreviation_on_mixed_path(self) -> None:
        decision = classify_mixed_language_source(
            "不含税单价/HT",
            target_lang="fr",
            source_lang="zh",
        )

        self.assertTrue(decision.is_mixed)
        self.assertEqual(decision.reason, "short_label")

    def test_long_body_residual_foreign_text_routes_to_mixed_path(self) -> None:
        source = "本段说明沉降观测点布置与复测要求 asdfg，其他内容均为中文工程说明。"

        decision = classify_mixed_language_source(
            source,
            target_lang="fr",
            source_lang="zh",
        )

        self.assertTrue(decision.is_mixed)
        self.assertEqual(decision.reason, "long_body")

    def test_target_chinese_disables_mixed_path(self) -> None:
        normal, mixed = split_mixed_language_sources(
            ["内容 Contenu"],
            target_lang="zh",
            source_lang="zh",
        )

        self.assertEqual(normal, ["内容 Contenu"])
        self.assertEqual(mixed, [])

    def test_structured_results_are_matched_by_id_not_order(self) -> None:
        texts = ["内容 Contenu", "沉降观测 asdfg", "贝特瑞 BTR 项目"]

        results = translate_mixed_language_texts(
            texts,
            engine=StructuredMixedEngine(),
            target_lang="fr",
            system_prompt="base",
            source_lang="zh",
            concurrency=1,
        )

        self.assertEqual(results["内容 Contenu"].action, MIXED_ACTION_EXISTING_BILINGUAL)
        self.assertEqual(results["沉降观测 asdfg"].action, MIXED_ACTION_FOREIGN_NOISE)
        self.assertEqual(results["沉降观测 asdfg"].translation, "Observation du tassement")
        self.assertEqual(results["贝特瑞 BTR 项目"].action, MIXED_ACTION_TRANSLATE)
        self.assertEqual(results["贝特瑞 BTR 项目"].translation, "Projet BTR")


class MixedLanguageStopTests(unittest.TestCase):
    def test_request_cut_off_by_stop_is_reported_as_stopped_not_failed(self) -> None:
        stop = {"set": False}
        errors: list[str] = []

        class _CutOffEngine(StructuredMixedEngine):
            def chat(self, system: str, user: str) -> str:
                # 用户按停止时在途请求被打断。
                stop["set"] = True
                raise ConnectionError("connection aborted while stopping")

        sources = ["项目 BTR", "设备 ABC 型号"]
        results = translate_mixed_language_texts(
            sources,
            engine=_CutOffEngine(),
            target_lang="fr",
            system_prompt="system",
            should_stop=lambda: stop["set"],
            error_callback=errors.append,
        )

        self.assertEqual(set(results), set(sources))
        for result in results.values():
            # 以前 note 是 str(exc)：任务层只认 stopped/missing_result，这些格子
            # 就成了「待人工确认」、被标记复核，日志还说「没能翻译」。
            self.assertEqual(result.note, "stopped")
        self.assertFalse([e for e in errors if "未能翻译" in e], errors)

    # 审计 中-4：限流退避最长要睡 30 秒，停止标志必须交给退避本身。
    # 这两条走的是混合语言里的两处调用点（普通批次 / 单条重试）。
    def test_batch_rate_limit_backoff_receives_should_stop(self) -> None:
        seen = _BackoffSleepRecorder()

        with patch("core.api_concurrency_control._interruptible_sleep", seen.record):
            _translate_mixed_batch_with_fallback(
                ["项目 BTR"],
                engine=_StoppedInBackoffEngine(),
                target_lang="fr",
                system_prompt="system",
                source_lang="zh",
                api_scheduler=WeightedApiScheduler(1),  # 已在最低档，直接进宽限等待
                request_category=API_REQUEST_CATEGORY_NORMAL,
                retry_attempts=1,
                should_stop=lambda: seen.stop.is_set(),
                error_callback=None,
                stats=MixedLanguageRunStats(),
            )

        self.assertTrue(seen.calls, "限流退避根本没有被调用，测试前提不成立")
        self.assertIsNotNone(seen.calls[0], "普通批次的限流退避没拿到停止标志")
        self.assertTrue(seen.calls[0](), "停止标志交给退避后必须能读到已停止")

    def test_retry_rate_limit_backoff_receives_should_stop(self) -> None:
        seen = _BackoffSleepRecorder()

        with patch("core.api_concurrency_control._interruptible_sleep", seen.record):
            _retry_one_mixed_source(
                "项目 BTR",
                engine=_StoppedInBackoffEngine(),
                target_lang="fr",
                system_prompt="system",
                source_lang="zh",
                api_scheduler=WeightedApiScheduler(1),
                retry_attempts=1,
                should_stop=lambda: seen.stop.is_set(),
                stats=MixedLanguageRunStats(),
            )

        self.assertTrue(seen.calls, "限流退避根本没有被调用，测试前提不成立")
        self.assertIsNotNone(seen.calls[0], "单条重试的限流退避没拿到停止标志")
        self.assertTrue(seen.calls[0](), "停止标志交给退避后必须能读到已停止")


class _RateLimitedEngine(StructuredMixedEngine):
    """每次 chat 都报上游限流（一次请求都没真正发出去）。"""

    engine_name = "fake/rate-limited"

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, system: str, user: str) -> str:
        self.calls += 1
        raise RuntimeError("429 Too Many Requests: too many concurrent requests")


class MixedLanguageRateLimitStopTests(unittest.TestCase):
    """中-2：混合语言的两处限流退避必须能被停止打断。

    退避里睡的是整整 30 秒（``MINIMUM_CAPACITY_MAX_DELAY``）。不把停止标志交给
    ``handle_api_concurrency_limit``，用户点了停止之后还要先把这一觉睡满，才轮得到
    下一行的停止检查——回调给 ``_interruptible_sleep`` 的那种「每次最多睡 0.25 秒」
    就在这里断了。
    """

    def setUp(self) -> None:
        super().setUp()
        for name in ("MINIMUM_CAPACITY_BASE_DELAY", "MINIMUM_CAPACITY_MAX_DELAY"):
            patcher = patch(f"core.api_concurrency_control.{name}", 30.0)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _stop_as_the_backoff_starts(self, stop: dict) -> None:
        """停止发生在退避睡着的那一刻——正好是停止标志唯一的用武之地。"""
        real_sleep = api_concurrency_control._interruptible_sleep

        def sleep_after_stop(delay: float, should_stop) -> None:
            stop["set"] = True  # 用户在这一觉里按了停止
            real_sleep(delay, should_stop)

        patcher = patch(
            "core.api_concurrency_control._interruptible_sleep",
            side_effect=sleep_after_stop,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_batch_fallback_hands_the_stop_signal_to_the_rate_limit_wait(self) -> None:
        stop = {"set": False}
        self._stop_as_the_backoff_starts(stop)
        engine = _RateLimitedEngine()
        scheduler = WeightedApiScheduler(1)  # 已在最低档：命中就直接进退避

        started = time.monotonic()
        results = _translate_mixed_batch_with_fallback(
            ["项目 BTR"],
            engine=engine,
            target_lang="fr",
            system_prompt="system",
            source_lang="zh",
            api_scheduler=scheduler,
            request_category=API_REQUEST_CATEGORY_NORMAL,
            retry_attempts=1,
            should_stop=lambda: stop["set"],
            error_callback=None,
            stats=MixedLanguageRunStats(),
        )

        self.assertLess(time.monotonic() - started, 5.0)
        self.assertEqual(engine.calls, 1)
        self.assertEqual(results["项目 BTR"].note, "stopped")

    def test_retry_loop_hands_the_stop_signal_to_the_rate_limit_wait(self) -> None:
        stop = {"set": False}
        self._stop_as_the_backoff_starts(stop)
        engine = _RateLimitedEngine()
        scheduler = WeightedApiScheduler(1)

        started = time.monotonic()
        result = _retry_one_mixed_source(
            "项目 BTR",
            engine=engine,
            target_lang="fr",
            system_prompt="system",
            source_lang="zh",
            api_scheduler=scheduler,
            retry_attempts=3,
            should_stop=lambda: stop["set"],
            stats=MixedLanguageRunStats(),
        )

        self.assertLess(time.monotonic() - started, 5.0)
        # 停止之后不许再发下一次重试。
        self.assertEqual(engine.calls, 1)
        self.assertEqual(result.note, "stopped")

    def test_every_rate_limit_call_site_gets_the_stop_signal(self) -> None:
        """按调用点逐个修必然还会漏，这里把「所有调用点都传」锁住。"""
        call_lines: list[int] = []
        missing: list[int] = []
        tree = ast.parse(inspect.getsource(mixed_language))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "id", None) != "handle_api_concurrency_limit":
                continue
            call_lines.append(node.lineno)
            if not any(keyword.arg == "should_stop" for keyword in node.keywords):
                missing.append(node.lineno)

        self.assertTrue(call_lines, "混合语言里没有找到限流退避调用点")
        self.assertEqual(missing, [], f"这些调用点没把停止标志交出去：{missing}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
