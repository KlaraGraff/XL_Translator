"""「智能」领域模式：翻译前的文档说明、短词的记忆库规则、旧设置升级。

各页面的整条流程另在 Excel / Word / PDF 的契约测试里；这里钉住共用的部分。
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import settings as settings_module
from config import DOMAIN_PRESETS
from core import tm_manager
from core.cad_translation import CadPipelineOptions, CadTranslationPipeline
from core.document_profile import (
    SMART_PRESET,
    append_profile,
    drop_unlocked_short_hits,
    is_context_sensitive,
    request_document_profile,
    select_profile_sample,
)
from core.engine_dispatcher import get_system_prompt
from core.image_generation import build_pdf_image_translation_prompt
from settings import AppSettings
from tests.app_data_isolation import IsolatedAppDataTestCase


class ContextSensitiveTextTests(unittest.TestCase):
    def test_short_words_and_abbreviations_depend_on_context(self) -> None:
        for text in ("甲", "乙", "详图", "FM", "B", "Rev.", " 甲 "):
            self.assertTrue(is_context_sensitive(text), text)

    def test_longer_text_numbers_and_codes_do_not(self) -> None:
        for text in ("设备基坑", "Door", "M1523", "1:100", "甲1", "", "—", "Fire door"):
            self.assertFalse(is_context_sensitive(text), text)


class ProfileSampleTests(unittest.TestCase):
    def test_frequent_labels_first_then_longest_notes_without_pure_numbers(self) -> None:
        note = "所有防火门均应满足耐火完整性和隔热性要求，" * 3
        texts = ["甲"] * 5 + ["乙"] * 3 + ["1:100", "2830", note, "门窗表"]
        sample = select_profile_sample(texts)
        self.assertEqual(sample[:2], ["甲", "乙"])
        self.assertIn(note.strip(), sample)
        self.assertNotIn("1:100", sample)
        self.assertNotIn("2830", sample)

    def test_codes_are_capped_and_budget_is_respected(self) -> None:
        codes = [f"FM{i}" for i in range(100)]
        notes = [f"第{i}条说明文字" + "很长" * 70 for i in range(200)]
        sample = select_profile_sample(codes + notes)
        self.assertLessEqual(sum(1 for item in sample if item.startswith("FM")), 30)
        self.assertLessEqual(sum(len(item) + 1 for item in sample), 6000)
        self.assertTrue(all(len(item) <= 160 for item in sample))


class RequestProfileTests(unittest.TestCase):
    def test_profile_is_cleaned_and_logged(self) -> None:
        logs: list[tuple[str, str]] = []
        calls: list[tuple[str, str]] = []

        def chat(system: str, user: str) -> str:
            calls.append((system, user))
            return "```\n建筑施工图。\n「甲」指甲级防火门。\n```"

        profile = request_document_profile(
            chat, ["甲", "防火门"], target_language="英文", log=lambda *args: logs.append(args)
        )
        self.assertEqual(profile, "建筑施工图。\n「甲」指甲级防火门。")
        self.assertIn("英文", calls[0][0])
        self.assertEqual(calls[0][1], "甲\n防火门")
        self.assertEqual(logs[0][0], "INFO")
        self.assertIn("甲级防火门", logs[0][1])

    def test_failure_returns_empty_and_translation_goes_on(self) -> None:
        logs: list[tuple[str, str]] = []

        def chat(_system: str, _user: str) -> str:
            raise TimeoutError("slow")

        self.assertEqual(
            request_document_profile(chat, ["甲"], target_language="英文", log=lambda *args: logs.append(args)),
            "",
        )
        self.assertEqual(logs, [("WARN", "智能模式：文档说明没有生成（TimeoutError），按普通方式继续翻译")])

    def test_nothing_to_read_makes_no_request(self) -> None:
        def chat(_system: str, _user: str) -> str:
            raise AssertionError("不该请求")

        self.assertEqual(request_document_profile(chat, ["1:100", "  "], target_language="英文"), "")

    def test_append_profile(self) -> None:
        self.assertEqual(append_profile("system", ""), "system")
        combined = append_profile("system", "建筑施工图。")
        self.assertTrue(combined.startswith("system\n\n本文档说明"))
        self.assertTrue(combined.endswith("建筑施工图。"))


class SmartPresetTests(unittest.TestCase):
    def test_smart_preset_has_a_prompt_and_is_the_default_everywhere(self) -> None:
        self.assertIn(SMART_PRESET, DOMAIN_PRESETS)
        settings = AppSettings()
        for key in ("domain_preset", "excel_domain_preset", "word_domain_preset", "cad_domain_preset"):
            self.assertEqual(getattr(settings, key), SMART_PRESET, key)
        self.assertTrue(settings.pdf.smart_context)
        prompt = get_system_prompt(settings, target_lang="en", source_lang="zh", page_key="cad")
        self.assertIn("文档说明为准", prompt)


class ShortHitTests(unittest.TestCase):
    def test_short_auto_hits_are_dropped_unless_the_user_fixed_them(self) -> None:
        hits = {"甲": "Party A", "FM": "Fire door", "防火门耐火等级": "Fire rating"}

        def locked(texts: list[str], pair: str) -> dict[str, str | None]:
            self.assertEqual(pair, "zh-en")
            return {text: ("Fire door" if text == "FM" else None) for text in texts}

        dropped = drop_unlocked_short_hits(hits, {"zh-en": list(hits)}, locked)
        self.assertEqual(dropped, 1)
        self.assertEqual(hits, {"FM": "Fire door", "防火门耐火等级": "Fire rating"})


class LockedTmLookupTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.db_path = root / "tm.db"
        for patcher in (
            patch.dict(os.environ, {"TRANSLATOR_APP_DATA_DIR": str(root)}),
            patch.object(tm_manager, "DB_PATH", self.db_path),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        tm_manager.init_db()

    def _seed(self, source: str, target: str, word_type: str, pinned: int = 0) -> None:
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                "INSERT INTO tm_entries (source_text, source_hash, target_text, lang_pair, "
                "word_type, source_engine, pinned) VALUES (?, ?, ?, 'zh-en', ?, 'test', ?)",
                [source, tm_manager._make_hash(source, "zh-en"), target, word_type, pinned],
            )
            conn.commit()
        finally:
            conn.close()

    def test_locked_only_returns_manual_pinned_and_cleaning_locked_entries(self) -> None:
        self._seed("甲", "Party A", "auto")
        self._seed("乙", "Class B", tm_manager.MANUAL_WORD_TYPE)
        self._seed("丙", "Class C", "auto", pinned=1)
        self._seed("丁", "Class D", tm_manager.CLEANING_LOCKED_WORD_TYPE)
        texts = ["甲", "乙", "丙", "丁"]
        self.assertEqual(
            tm_manager.lookup_batch(texts, "zh-en"),
            {"甲": "Party A", "乙": "Class B", "丙": "Class C", "丁": "Class D"},
        )
        locked = tm_manager.lookup_batch(texts, "zh-en", locked_only=True)
        self.assertEqual({k: v for k, v in locked.items() if v}, {"乙": "Class B", "丙": "Class C", "丁": "Class D"})


class SmartDomainMigrationTests(IsolatedAppDataTestCase):
    def _write(self, payload: dict) -> None:
        self.app_data_dir.mkdir(parents=True, exist_ok=True)
        (self.app_data_dir / "settings.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_old_defaults_move_to_smart_and_deliberate_choices_stay(self) -> None:
        self._write(
            {
                "settings_version": 32,
                "excel_domain_preset": "无",
                "word_domain_preset": "资料管理场景",
                "cad_domain_preset": "同步工程场景",
            }
        )
        loaded = settings_module.load_settings()
        self.assertEqual(loaded.excel_domain_preset, SMART_PRESET)
        self.assertEqual(loaded.word_domain_preset, "资料管理场景")
        self.assertEqual(loaded.cad_domain_preset, SMART_PRESET)
        self.assertEqual(loaded.domain_preset, SMART_PRESET)

    def test_current_version_keeps_an_explicit_old_default(self) -> None:
        self._write({"settings_version": settings_module.SETTINGS_SCHEMA_VERSION, "cad_domain_preset": "同步工程场景"})
        self.assertEqual(settings_module.load_settings().cad_domain_preset, "同步工程场景")


def _dxf(*texts: str) -> str:
    return "".join(f"0\nTEXT\n1\n{text}\n" for text in texts)


class CadSmartModeTests(unittest.TestCase):
    def test_drawing_profile_is_requested_once_and_reaches_every_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "doors.dxf"
            source.write_text(_dxf("甲", "防火门", "甲", "M1523"), encoding="utf-8")
            requested: list[list[str]] = []
            contexts: list[str] = []

            def profile(texts: list[str], log) -> str:
                requested.append(list(texts))
                log("INFO", "智能模式：已读过 2 条文字，文档说明：门窗图")
                return "门窗图。「甲」指甲级防火门。"

            def translate(texts, glossary, context=""):
                contexts.append(context)
                return {"甲": "Class A", "防火门": "Fire door"}

            result = CadTranslationPipeline(
                converter=None, translator=translate, profile_requester=profile
            ).translate_file(source, root / "doors_en.dxf", options=CadPipelineOptions(target_lang="en"))

            self.assertEqual(len(requested), 1)
            self.assertIn("甲", requested[0])
            self.assertEqual(contexts, ["门窗图。「甲」指甲级防火门。"])
            self.assertIn("Class A", (root / "doors_en.dxf").read_text(encoding="utf-8"))
            self.assertEqual(result.stats.ai_translated, 2)

    def test_translator_without_context_parameter_still_works(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "doors.dxf"
            source.write_text(_dxf("甲"), encoding="utf-8")
            result = CadTranslationPipeline(
                converter=None,
                translator=lambda texts, glossary: {"甲": "Class A"},
                profile_requester=lambda texts, log: "门窗图。",
            ).translate_file(source, root / "doors_en.dxf", options=CadPipelineOptions(target_lang="en"))
            self.assertEqual(result.stats.ai_translated, 1)


class PdfPromptContextTests(unittest.TestCase):
    def test_document_context_goes_before_review_feedback(self) -> None:
        prompt = build_pdf_image_translation_prompt(
            "English",
            target_lang_code="en",
            document_context="门窗图。「甲」指甲级防火门。",
            review_feedback="第 1 行仍有漏译。",
        )
        self.assertIn("Document context", prompt)
        self.assertLess(prompt.index("甲级防火门"), prompt.index("第 1 行仍有漏译"))
        self.assertNotIn("Document context", build_pdf_image_translation_prompt("English", target_lang_code="en"))


class PdfProfileRequesterTests(unittest.TestCase):
    def test_missing_document_model_warns_once_and_pdf_goes_on(self) -> None:
        from api.surfaces.pdf import _document_profile_requester

        logs: list[tuple[str, str]] = []
        with patch("core.engine_dispatcher.build_role_engine", side_effect=RuntimeError("no key")) as build:
            requester = _document_profile_requester(AppSettings(), ())
            self.assertEqual(requester(["甲"], lambda *args: logs.append(args)), "")
            self.assertEqual(requester(["乙"], lambda *args: logs.append(args)), "")
        self.assertEqual(build.call_count, 1)
        self.assertEqual([level for level, _message in logs], ["WARN"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
