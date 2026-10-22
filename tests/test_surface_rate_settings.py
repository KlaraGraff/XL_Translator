"""「翻译参数」里按文件类型的速率与重试（settings v38 起）。

Excel、Word、PDF 文字、CAD 各存一组五项，None 表示按云端/本地取默认值；PDF 整页
图像翻译另有三项存在 settings.pdf。v37 及以前分散在 word_batch、request_retry 和
按模型的速率里，升级时一律改用新默认值，改过的值只留在升级前的备份里。
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from core.engine_dispatcher import TranslationBatchRunStats, translate_texts
from core.document_config import (
    DOCUMENT_CONFIG_EXPORT_TYPE,
    DOCUMENT_CONFIG_SECTIONS,
    apply_document_config_import,
    build_document_config_export_payload,
    parse_document_config_import,
)
from core.model_roles import ROLE_IMAGE, ROLE_TRANSLATION, resolve_effective_model_config
from core.model_throughput import set_model_throughput
from core.surface_rate import (
    rate_bounds,
    resolve_pdf_page_concurrency,
    resolve_pdf_review_concurrency,
    resolve_surface_rate,
    task_role_rate,
)
from core.word_batching import WordBatchRunStats, translate_word_texts
from engines.base_engine import TranslationEngine
import settings as settings_module
from settings import AppSettings, SETTINGS_SCHEMA_VERSION, WordBatchSettings, set_cloud_provider_config
from tests.app_data_isolation import IsolatedAppDataTestCase


class SurfaceRateDefaultsTests(unittest.TestCase):
    def test_cloud_defaults(self) -> None:
        settings = AppSettings()
        for surface in ("excel", "pdf_text", "cad"):
            with self.subTest(surface=surface):
                rate = resolve_surface_rate(settings, surface, mode="cloud")
                self.assertEqual(
                    (rate.concurrency, rate.batch_items, rate.batch_chars, rate.request_retry, rate.item_retry),
                    (30, 20, 1000, 2, 3),
                )
        word = resolve_surface_rate(settings, "word", mode="cloud")
        self.assertEqual((word.concurrency, word.batch_items, word.batch_chars), (30, 8, 1000))

    def test_local_defaults(self) -> None:
        settings = AppSettings()
        excel = resolve_surface_rate(settings, "excel", mode="local")
        self.assertEqual((excel.concurrency, excel.batch_items, excel.batch_chars), (3, 10, 800))
        word = resolve_surface_rate(settings, "word", mode="local")
        self.assertEqual((word.batch_items, word.batch_chars), (8, 800))

    def test_pdf_image_defaults(self) -> None:
        settings = AppSettings()
        self.assertEqual(resolve_pdf_page_concurrency(settings, mode="cloud"), 10)
        self.assertEqual(resolve_pdf_page_concurrency(settings, mode="local"), 3)
        self.assertEqual(resolve_pdf_review_concurrency(settings, mode="cloud"), 10)
        self.assertEqual(resolve_pdf_review_concurrency(settings, mode="local"), 3)
        self.assertEqual(settings.pdf.page_retry_attempts, 1)

    def test_stored_values_win_and_are_clamped_to_the_locked_range(self) -> None:
        settings = AppSettings.model_validate(
            {"rate": {"cad": {"concurrency": 999, "batch_items": 12, "batch_chars": 100, "request_retry": 0}}}
        )
        rate = resolve_surface_rate(settings, "cad", mode="cloud")
        self.assertEqual(rate.concurrency, 50)
        self.assertEqual(rate.batch_items, 12)
        self.assertEqual(rate.batch_chars, 800)
        self.assertEqual(rate.request_retry, 0)
        # 解锁后并发、条数和字符不设上限。
        settings.engine.concurrency_unlocked = True
        self.assertEqual(resolve_surface_rate(settings, "cad", mode="cloud").concurrency, 999)

    def test_word_keeps_its_eight_paragraph_cap_and_one_recovery_round(self) -> None:
        settings = AppSettings.model_validate({"rate": {"word": {"batch_items": 30, "item_retry": 0}}})
        rate = resolve_surface_rate(settings, "word", mode="cloud")
        self.assertEqual(rate.batch_items, 8)
        self.assertEqual(rate.item_retry, 1)
        self.assertEqual(rate_bounds("word", "cloud", unlocked=True)["batch_items"], (1, 8))

    def test_blank_and_bool_values_mean_default(self) -> None:
        settings = AppSettings.model_validate({"rate": {"excel": {"concurrency": "", "batch_items": True}}})
        self.assertIsNone(settings.rate.excel.concurrency)
        self.assertIsNone(settings.rate.excel.batch_items)

    def test_the_scheduler_reserves_the_surface_rate_not_the_model_profile(self) -> None:
        settings = AppSettings()
        set_cloud_provider_config(
            settings.engine, "custom_openai", cloud_model="m", cloud_base_url="https://api.example/v1"
        )
        config = resolve_effective_model_config(settings, ROLE_TRANSLATION)
        set_model_throughput(settings, config, batch_size=11, concurrency=4)
        settings.rate.cad.concurrency = 7
        self.assertEqual(task_role_rate(settings, "cad_translate", config), (7, 20))
        self.assertEqual(task_role_rate(settings, "excel_translate", config), (30, 20))

    def test_a_format_adapter_runs_at_the_rate_the_scheduler_reserved(self) -> None:
        # 运行时注册的类型在「翻译参数」里没有自己的一组：同时请求数和每批条数取文本翻译模型的
        # 吞吐量，与调度器为 page_key = "adapter" 预留的一致。
        settings = AppSettings()
        set_cloud_provider_config(
            settings.engine, "custom_openai", cloud_model="m", cloud_base_url="https://api.example/v1"
        )
        config = resolve_effective_model_config(settings, ROLE_TRANSLATION)
        set_model_throughput(settings, config, batch_size=11, concurrency=4)
        settings.rate.cad.concurrency = 7
        rate = resolve_surface_rate(settings, "ext:text")
        self.assertEqual((rate.concurrency, rate.batch_items), task_role_rate(settings, "adapter", config))
        self.assertEqual((rate.concurrency, rate.batch_items), (4, 11))
        self.assertEqual((rate.request_retry, rate.item_retry), (2, 3))


class DocumentBundleTests(unittest.TestCase):
    def test_rate_travels_with_the_document_config_bundle(self) -> None:
        self.assertIn("rate", DOCUMENT_CONFIG_SECTIONS)
        self.assertNotIn("request_retry", DOCUMENT_CONFIG_SECTIONS)
        self.assertNotIn("word_batch", DOCUMENT_CONFIG_SECTIONS)
        settings = AppSettings.model_validate({"rate": {"excel": {"request_retry": 4}, "cad": {"batch_items": 15}}})
        exported = build_document_config_export_payload(settings)
        self.assertEqual(exported["document"]["rate"]["excel"]["request_retry"], 4)

        local = AppSettings.model_validate({"rate": {"word": {"batch_chars": 1500}}})
        merged = apply_document_config_import(local, parse_document_config_import(exported))
        self.assertEqual(merged.rate.excel.request_retry, 4)
        self.assertEqual(merged.rate.cad.batch_items, 15)

    def test_a_v4_bundle_leaves_the_importers_rate_alone(self) -> None:
        bundle = {
            "type": DOCUMENT_CONFIG_EXPORT_TYPE,
            "version": 4,
            "document": {
                "word_batch": {"max_chars_per_batch": 1200, "strict_retry_attempts": 3},
                "request_retry": {"excel": 4, "cad": 2},
                "pdf": {"page_generation_concurrency": 6, "page_retry_attempts": 3, "review_enabled": True},
            },
        }
        local = AppSettings.model_validate({
            "rate": {"word": {"batch_chars": 1500}},
            "pdf": {"page_retry_attempts": 2},
        })
        merged = apply_document_config_import(local, parse_document_config_import(bundle))
        # 和设置文件升级一样，旧包里的速率与重试不用，导入方保留自己的。
        self.assertEqual(merged.rate.word.batch_chars, 1500)
        self.assertIsNone(merged.rate.excel.request_retry)
        self.assertIsNone(merged.pdf.page_generation_concurrency)
        self.assertEqual(merged.pdf.page_retry_attempts, 2)
        # 同一节里的其他设置照常导入。
        self.assertTrue(merged.pdf.review_enabled)


def _v37_payload(**extra: object) -> dict:
    settings = AppSettings()
    set_cloud_provider_config(
        settings.engine, "custom_openai", cloud_model="m", cloud_base_url="https://api.example/v1"
    )
    payload = json.loads(settings.model_dump_json())
    payload.pop("rate", None)
    payload["pdf"].pop("review_concurrency", None)
    payload["settings_version"] = 37
    payload["word_batch"] = {
        "max_paragraphs_per_batch": 8,
        "max_chars_per_batch": 800,
        "split_paragraph_chars": 800,
        "strict_retry_attempts": 3,
    }
    payload["request_retry"] = {"excel": 2, "cad": 2}
    payload.update(extra)
    return payload


class SettingsFileUpgradeTests(IsolatedAppDataTestCase):
    def _write(self, payload: dict) -> None:
        self.app_data_dir.mkdir(parents=True, exist_ok=True)
        (self.app_data_dir / "settings.json").write_text(json.dumps(payload), encoding="utf-8")

    def _backups(self) -> list:
        root = self.app_data_dir / "backups" / "settings"
        return sorted(root.glob("settings_v37_before_upgrade_*.json")) if root.exists() else []

    def _assert_all_rate_on_new_defaults(self, loaded: AppSettings) -> None:
        for surface in ("excel", "word", "pdf_text", "cad"):
            stored = getattr(loaded.rate, surface)
            self.assertEqual(stored.model_dump(), dict.fromkeys(stored.model_dump()), surface)
        self.assertIsNone(loaded.pdf.page_generation_concurrency)
        self.assertIsNone(loaded.pdf.review_concurrency)
        self.assertEqual(loaded.pdf.page_retry_attempts, 1)

    def test_tuned_values_take_the_new_defaults_and_the_old_file_is_backed_up(self) -> None:
        payload = _v37_payload(request_retry={"excel": 4, "cad": 2})
        payload["word_batch"]["max_chars_per_batch"] = 500
        payload["word_batch"]["max_paragraphs_per_batch"] = 5
        payload["pdf"]["page_generation_concurrency"] = 6
        payload["pdf"]["page_retry_attempts"] = 3
        source = AppSettings.model_validate({k: v for k, v in payload.items() if k not in {"word_batch", "request_retry"}})
        translation = resolve_effective_model_config(source, ROLE_TRANSLATION)
        set_model_throughput(source, translation, batch_size=15, concurrency=8)
        payload["model_throughput_profiles"] = json.loads(source.model_dump_json())["model_throughput_profiles"]
        self._write(payload)

        loaded = settings_module.load_settings()
        self._assert_all_rate_on_new_defaults(loaded)
        self.assertEqual(resolve_surface_rate(loaded, "excel").concurrency, 30)
        self.assertEqual(resolve_surface_rate(loaded, "word").batch_chars, 1000)
        self.assertEqual(resolve_surface_rate(loaded, "word").batch_items, 8)

        settings_module.save_settings(loaded)
        backups = self._backups()
        self.assertEqual(len(backups), 1)
        backup = json.loads(backups[0].read_text(encoding="utf-8"))
        self.assertEqual(backup["request_retry"]["excel"], 4)
        self.assertEqual(backup["word_batch"]["max_chars_per_batch"], 500)
        self.assertEqual(backup["pdf"]["page_generation_concurrency"], 6)
        stored = json.loads((self.app_data_dir / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["settings_version"], SETTINGS_SCHEMA_VERSION)
        self.assertNotIn("word_batch", stored)
        self.assertNotIn("request_retry", stored)
        self.assertEqual(stored["rate"]["excel"]["request_retry"], None)
        # 按模型的速率留着：清洗记忆库仍按它取。
        self.assertEqual(stored["model_throughput_profiles"], payload["model_throughput_profiles"])

    def test_a_tuned_model_speed_alone_is_reset_and_backed_up(self) -> None:
        payload = _v37_payload()
        source = AppSettings.model_validate({k: v for k, v in payload.items() if k not in {"word_batch", "request_retry"}})
        set_model_throughput(source, resolve_effective_model_config(source, ROLE_IMAGE), concurrency=4)
        payload["model_throughput_profiles"] = json.loads(source.model_dump_json())["model_throughput_profiles"]
        self._write(payload)

        loaded = settings_module.load_settings()
        self._assert_all_rate_on_new_defaults(loaded)
        self.assertEqual(resolve_pdf_page_concurrency(loaded, mode="cloud"), 10)

        settings_module.save_settings(loaded)
        self.assertEqual(len(self._backups()), 1)

    def test_a_file_on_the_old_defaults_takes_the_new_defaults_without_a_backup(self) -> None:
        self._write(_v37_payload())

        loaded = settings_module.load_settings()
        self._assert_all_rate_on_new_defaults(loaded)
        self.assertEqual(resolve_surface_rate(loaded, "excel").concurrency, 30)

        settings_module.save_settings(loaded)
        self.assertEqual(self._backups(), [])


class _OneStubbornItemEngine(TranslationEngine):
    """「坏」在多条一批时总被漏掉，单独发时前几次也漏掉。"""

    def __init__(self, single_failures: int) -> None:
        self.single_failures = single_failures
        self.calls: list[list[str]] = []

    @property
    def engine_name(self) -> str:
        return "fake/stubborn"

    def translate_batch(self, texts, target_lang, system_prompt, source_lang="zh"):
        self.calls.append(list(texts))
        if "坏" in texts and (len(texts) > 1 or self.single_failures > 0):
            if len(texts) == 1:
                self.single_failures -= 1
            return {text: f"ok:{text}" for text in texts if text != "坏"}
        return {text: f"ok:{text}" for text in texts}


class _TransientOnceEngine(TranslationEngine):
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    @property
    def engine_name(self) -> str:
        return "fake/transient"

    def translate_batch(self, texts, target_lang, system_prompt, source_lang="zh"):
        self.calls.append(list(texts))
        if len(self.calls) == 1:
            import httpx

            from core.text_transport import TextTransportHTTPError

            request = httpx.Request("POST", "https://service.test/v1/chat/completions")
            response = httpx.Response(503, text="{}", request=request)
            raise TextTransportHTTPError("HTTP 503", request=request, response=response, kind="transient")
        return {text: f"ok:{text}" for text in texts}


class RetryBehaviourTests(unittest.TestCase):
    def test_item_retry_sends_a_leftover_item_on_its_own(self) -> None:
        for retries, expected in ((0, "坏"), (3, "ok:坏")):
            with self.subTest(item_retries=retries):
                engine = _OneStubbornItemEngine(single_failures=2)
                stats = TranslationBatchRunStats()
                with patch("core.engine_dispatcher._backoff_sleep"):
                    result = translate_texts(
                        ["甲", "乙", "坏"], engine, "fr", "p", 3, 1, stats=stats, item_retries=retries,
                    )
                self.assertEqual(result["坏"], expected)
                self.assertEqual(result["甲"], "ok:甲")
                self.assertEqual(stats.item_retry_count, 0 if retries == 0 else 2)

    def test_word_resends_the_same_batch_on_a_transient_failure_before_splitting(self) -> None:
        engine = _TransientOnceEngine()
        stats = WordBatchRunStats()
        with patch("core.engine_dispatcher._backoff_sleep"):
            result = translate_word_texts(
                ["第一段。", "第二段。"], engine, "fr", "p", WordBatchSettings(), 1,
                stats=stats, transient_retries=2,
            )
        self.assertEqual(result, {"第一段。": "ok:第一段。", "第二段。": "ok:第二段。"})
        self.assertEqual(engine.calls, [["第一段。", "第二段。"], ["第一段。", "第二段。"]])
        self.assertEqual(stats.transient_retry_count, 1)


if __name__ == "__main__":
    unittest.main()
