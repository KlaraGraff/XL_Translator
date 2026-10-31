"""繁體中文目标语言：语言列表、提示词、简体字改繁体、各模块按中文处理、旧自定义语言迁移。"""

from __future__ import annotations

import unittest

from core import zh_hant
from core.cad_writeback import compatible_font
from core.image_generation import build_pdf_image_translation_prompt
from core.language_registry import (
    build_custom_target_lang_code,
    build_lang_pair,
    get_source_lang_display,
    get_supported_languages,
    get_supported_source_languages,
    get_target_lang_display,
    get_target_lang_prompt_name,
    resolve_language_code,
)
from core.pdf_vector.fonts import text_script
from core.pdf_word_route import east_asian_locale
from core.pptx_text import lang_tag
from core.tm_terms import is_cjk_target
from core.translation_filter import should_translate, validate_translation
from core.word_task_runner import _translation_in_unexpected_cjk
from engines.base_engine import TranslationEngine, get_target_lang_name
from settings import AppSettings


class ConversionTests(unittest.TestCase):
    def test_only_simplified_characters_change(self) -> None:
        self.assertEqual(zh_hant.to_traditional("這個软件很好用"), "這個軟件很好用")
        self.assertEqual(zh_hant.to_traditional("头发很长"), "頭髮很長")
        # 两岸通用的字、本来就是繁体的句子一个字都不改。
        self.assertEqual(zh_hant.to_traditional("皇后在台北的公里數"), "皇后在台北的公里數")
        self.assertEqual(zh_hant.to_traditional("後來"), "後來")
        self.assertEqual(zh_hant.to_traditional("Plan B 2025"), "Plan B 2025")

    def test_phrases_in_a_mixed_segment_use_the_phrase_table(self) -> None:
        self.assertEqual(zh_hant.to_traditional("干涉與干燥很干净"), "干涉與乾燥很乾淨")
        self.assertEqual(zh_hant.to_traditional("后来我们走了"), "後來我們走了")

    def test_detection(self) -> None:
        self.assertEqual(zh_hant.simplified_characters("我们的系统"), ["们", "统"])
        self.assertTrue(zh_hant.is_traditional_text("會議將於下週舉行"))
        self.assertFalse(zh_hant.is_traditional_text("会议"))
        self.assertFalse(zh_hant.is_traditional_text("Meeting"))

    def test_codes(self) -> None:
        for code in ("zh_hant", "zh-Hant", "zh-TW", "ZH_HK"):
            self.assertTrue(zh_hant.is_traditional_chinese(code), code)
            self.assertTrue(zh_hant.is_chinese_lang(code), code)
        self.assertFalse(zh_hant.is_traditional_chinese("zh"))
        self.assertTrue(zh_hant.is_chinese_lang("zh"))
        self.assertFalse(zh_hant.is_chinese_lang("x-custom-abc"))


class RegistryTests(unittest.TestCase):
    def test_target_list_has_both_variants(self) -> None:
        targets = get_supported_languages(None)
        self.assertEqual(targets["简体中文"], "zh")
        self.assertEqual(targets["繁體中文"], "zh_hant")
        self.assertEqual(get_target_lang_display("zh"), "简体中文")
        # API 调用方传 zh-TW 时，输出文件名里写「繁體中文」而不是「zh-TW」。
        self.assertEqual(get_target_lang_display("zh-TW"), "繁體中文")
        for alias in ("中文", "简体", "Chinese"):
            self.assertEqual(resolve_language_code(alias, targets), "zh", alias)
        for alias in ("繁体中文", "繁體", "Traditional Chinese", "zh-TW", "正體中文"):
            self.assertEqual(resolve_language_code(alias, targets), "zh_hant", alias)

    def test_source_language_is_one_chinese(self) -> None:
        sources = get_supported_source_languages()
        self.assertEqual(sources["中文"], "zh")
        self.assertNotIn("zh_hant", sources.values())
        self.assertEqual(get_source_lang_display("zh"), "中文")

    def test_memory_pair_splits_cleanly(self) -> None:
        self.assertEqual(build_lang_pair("zh_hant", "en"), "en-zh_hant")
        self.assertEqual("en-zh_hant".partition("-")[2], "zh_hant")

    def test_prompt_name_spells_out_traditional(self) -> None:
        name = get_target_lang_prompt_name("zh_hant")
        self.assertIn("繁體中文", name)
        self.assertIn("不得夾雜簡體字", name)
        self.assertEqual(get_target_lang_name("zh_hant"), name)
        self.assertEqual(get_target_lang_prompt_name("fr"), "法文")

    def test_image_prompt_asks_for_traditional(self) -> None:
        prompt = build_pdf_image_translation_prompt("繁體中文", target_lang_code="zh_hant")
        self.assertIn("Traditional Chinese", prompt)
        self.assertIn("never use Simplified", prompt)
        self.assertNotIn("Use concise Simplified Chinese", prompt)


class EngineTests(unittest.TestCase):
    def test_every_engine_output_is_fixed(self) -> None:
        class Sloppy(TranslationEngine):
            def translate_batch(self, texts, target_lang, system_prompt, source_lang="zh"):
                return {text: "這個软件很好" for text in texts}

        class Wrapper(Sloppy):
            def translate_batch(self, texts, target_lang, system_prompt, source_lang="zh"):
                return super().translate_batch(texts, target_lang, system_prompt, source_lang)

        self.assertEqual(Sloppy().translate_batch(["a"], "zh_hant", ""), {"a": "這個軟件很好"})
        self.assertEqual(Wrapper().translate_batch(["a"], "zh_hant", ""), {"a": "這個軟件很好"})
        # 简体目标原样。
        self.assertEqual(Sloppy().translate_batch(["a"], "zh", ""), {"a": "這個软件很好"})


class ModuleTreatsTraditionalAsChineseTests(unittest.TestCase):
    def test_word_route_accepts_chinese_output(self) -> None:
        # 反馈 #24：Word 路线把繁体译文当成「拉丁字母语言却冒出汉字」，234 格保留英文。
        self.assertFalse(_translation_in_unexpected_cjk("Revenue", "營收", "zh_hant"))
        # 自定义语言不知道用什么文字写，也不查。
        self.assertFalse(_translation_in_unexpected_cjk("Revenue", "營收", build_custom_target_lang_code("粤语")))
        self.assertTrue(_translation_in_unexpected_cjk("Revenue", "营收", "fr"))

    def test_should_translate(self) -> None:
        # 英文文件里本来就是繁体的格不送；夹着简体字的要改成繁体。
        self.assertFalse(should_translate("會議記錄", target_lang="zh_hant", source_lang="en"))
        self.assertTrue(should_translate("会议记录", target_lang="zh_hant", source_lang="en"))
        self.assertTrue(should_translate("Meeting minutes", target_lang="zh_hant", source_lang="en"))
        # 简体原文译成繁体：有简体字的送，两岸写法一样的不送。
        self.assertTrue(should_translate("会议记录", target_lang="zh_hant", source_lang="zh"))
        self.assertFalse(should_translate("台北", target_lang="zh_hant", source_lang="zh"))

    def test_validation_wants_chinese(self) -> None:
        self.assertFalse(validate_translation("Meeting minutes", "Meeting notes", target_lang="zh_hant", source_lang="en").is_pass)
        self.assertTrue(validate_translation("Meeting minutes", "會議記錄", target_lang="zh_hant", source_lang="en").is_pass)

    def test_layout_and_fonts(self) -> None:
        self.assertEqual(lang_tag("zh_hant"), "zh-TW")
        self.assertEqual(lang_tag("zh"), "zh-CN")
        self.assertEqual(east_asian_locale("zh_hant"), "zh-TW")
        self.assertTrue(is_cjk_target("zh_hant"))
        self.assertEqual(text_script("會議", "zh_hant"), "zh_hant")
        self.assertEqual(text_script("会议", "zh"), "zh")
        self.assertEqual(compatible_font("zh_hant"), ("gbenor.shx", "chineset.shx"))
        self.assertEqual(compatible_font("zh"), ("gbenor.shx", "gbcbig.shx"))


class SettingsMigrationTests(unittest.TestCase):
    def test_custom_traditional_chinese_becomes_builtin(self) -> None:
        custom = build_custom_target_lang_code("繁體中文")
        other = build_custom_target_lang_code("粤语")
        settings = AppSettings.model_validate({
            "custom_target_langs": [
                {"name": "繁體中文", "code": custom, "description": "台灣用字"},
                {"name": "粤语", "code": other},
            ],
            "target_lang": custom,
            "word_target_lang": custom,
            "pdf": {"target_lang": custom},
            "recent_target_langs": [custom, "fr"],
        })
        self.assertEqual(settings.target_lang, "zh_hant")
        self.assertEqual(settings.word_target_lang, "zh_hant")
        self.assertEqual(settings.pdf.target_lang, "zh_hant")
        self.assertEqual(settings.recent_target_langs[:2], ["zh_hant", "fr"])
        self.assertEqual([entry.name for entry in settings.custom_target_langs], ["粤语"])

    def test_old_chinese_target_still_resolves(self) -> None:
        settings = AppSettings.model_validate({"source_lang": "en", "target_lang": "中文", "pdf": {"target_lang": "zh"}})
        self.assertEqual(settings.target_lang, "zh")
        self.assertEqual(settings.pdf.target_lang, "zh")

    def test_interface_language(self) -> None:
        settings = AppSettings.model_validate({"appearance": {"ui_language": "zh-Hant"}})
        self.assertEqual(settings.appearance.ui_language, "zh-Hant")


if __name__ == "__main__":
    unittest.main(verbosity=2)
