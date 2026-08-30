"""should_translate 是全流程唯一的「这条要不要送去翻」闸门。

Word 抽取与写入（word_document._is_translatable_source）、Excel 词条收集
（task_runner._collect_texts）、Excel 写入器（xlsx_patcher._plan_cell_mutation）
全部经过它；补译分类（translation_coverage.looks_like_source_text）的最终放行
也要过它，只是前面另有「像译文」「纯数字符号」两道先行否决。它判 False 的内容
不会被抽取、不会被写入、也不会出现在任何报告里——整份文档静悄悄地没有译文。

这个文件盯的是「源语言不是中文、目标语言也不是中文」那一格：产品支持 59×59 个
语言对，而这条分支当年只按「中文文档里夹英文」写。
"""

from __future__ import annotations

import unittest

from core.translation_filter import should_translate


class SpacelessScriptTests(unittest.TestCase):
    """泰语、高棉语、老挝语、缅甸语词与词之间不打空格。

    一整句话走到规则 6 就是「一个词」，旧的 ^[A-Za-z]+$ 一律不匹配——整份泰语
    合同会被判成「没有要翻译的内容」，任务跑完输出一个原样的副本。
    """

    CASES = {
        "th": "สัญญาก่อสร้างอาคาร",
        "km": "កិច្ចសន្យាសំណង់",
        "lo": "ສັນຍາກໍ່ສ້າງ",
        "my": "ဆောက်လုပ်ရေးစာချုပ်",
    }

    def test_whole_sentences_are_translated_for_every_target(self) -> None:
        for lang, text in self.CASES.items():
            for target in ("en", "fr", "zh"):
                with self.subTest(source=lang, target=target):
                    self.assertTrue(
                        should_translate(text, target_lang=target, source_lang=lang)
                    )


class SingleWordTests(unittest.TestCase):
    """非拉丁文字的单词，以及只差一个变音字符的拉丁语单词。"""

    def test_non_latin_words_are_translated(self) -> None:
        cases = [
            ("ru", "Договор"),
            ("el", "Σύμβαση"),
            ("ja", "おしらせ"),
            ("ko", "계약서"),
        ]
        for lang, text in cases:
            with self.subTest(text=text):
                self.assertTrue(
                    should_translate(text, target_lang="en", source_lang=lang)
                )

    def test_one_accent_no_longer_hides_a_latin_word(self) -> None:
        # Straße 只比 Strasse 多一个字符，Généralités 是法语合同的常见小标题。
        for text in ("Straße", "Généralités", "Bâtiment"):
            with self.subTest(text=text):
                self.assertTrue(
                    should_translate(text, target_lang="en", source_lang="de")
                )


class AsciiBehaviourIsUnchangedTests(unittest.TestCase):
    """放宽只能落在非 ASCII 字母上。

    「长度 > 3」当年是为中文文档里夹的英文缩写和型号定的，中译外是主用法，
    这条不能松——松了就是每份图纸把 DN、PE 这类符号都送去翻一遍。
    """

    def test_ascii_abbreviations_and_model_codes_are_still_skipped(self) -> None:
        for text in ("DN", "PE", "Ltd", "A3B12", "DN100", "123", "±5%"):
            with self.subTest(text=text):
                self.assertFalse(
                    should_translate(text, target_lang="en", source_lang="zh")
                )

    def test_ascii_words_and_phrases_still_go_through(self) -> None:
        for text in ("Contract", "Bauvertrag", "Conditions générales"):
            with self.subTest(text=text):
                self.assertTrue(
                    should_translate(text, target_lang="en", source_lang="zh")
                )

    def test_unit_symbols_carrying_a_single_greek_letter_stay_skipped(self) -> None:
        """μm、Nº 这种「一个非 ASCII 字母 + ASCII」的短串是单位符号，不是词。"""
        for text in ("μm", "Nº", "Ω"):
            with self.subTest(text=text):
                self.assertFalse(
                    should_translate(text, target_lang="en", source_lang="zh")
                )

    def test_model_codes_in_other_scripts_are_skipped_like_ascii_ones(self) -> None:
        # 规则 4 的字母类也一并放宽，否则 Договор12 会绕过型号保护。
        for text in ("Réf12", "Договор12"):
            with self.subTest(text=text):
                self.assertFalse(
                    should_translate(text, target_lang="en", source_lang="fr")
                )


class ChineseTargetBranchTests(unittest.TestCase):
    """外译中那条分支本来就是 Unicode 感知的，这次不动它。"""

    def test_chinese_text_is_still_skipped_when_target_is_chinese(self) -> None:
        self.assertFalse(should_translate("工程", target_lang="zh", source_lang="en"))

    def test_foreign_text_is_still_translated_when_target_is_chinese(self) -> None:
        for text in ("Договор", "สัญญาก่อสร้างอาคาร", "Straße"):
            with self.subTest(text=text):
                self.assertTrue(
                    should_translate(text, target_lang="zh", source_lang="auto")
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
