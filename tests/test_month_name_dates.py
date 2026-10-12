"""「2026年10月12日」译成「12 octobre 2026」不能被当成缺数字退回原文。

内置规则要求日期里的中文年月日换成目标语言写法，月份数字因此常常写成月份名。
质量检查和残留修补的复核以前只认数字，正确译文被判 missing_number、整格退回原文
并标复核底色（Mac 真实模型测试，中译法 4 次全中）。
"""

from __future__ import annotations

import unittest

from core.residual_classifier import missing_number_tokens
from core.translation_filter import validate_translation


class MonthNameDateTests(unittest.TestCase):
    def test_month_written_as_name_passes(self) -> None:
        cases = [
            ("2026年10月12日", "12 octobre 2026", "fr"),
            ("签订日期：2026年10月12日", "Date de signature : 12 octobre 2026", "fr"),
            ("2026年10月12日", "October 12, 2026", "en"),
            ("2026年9月1日", "1 Sept. 2026", "en"),
            ("2026年10月12日", "12. Oktober 2026", "de"),
            ("2026年10月12日", "12 de octubre de 2026", "es"),
            ("2026年12月1日", "1 декабря 2026", "ru"),
            ("2026年5月5日", "5 maja 2026", "pl"),
            ("2026年3月1日至2026年5月31日", "du 1er mars 2026 au 31 mai 2026", "fr"),
            ("2026年10月", "octobre 2026", "fr"),
            ("2026年10月12日", "12.10.2026", "de"),
        ]
        for original, translated, lang in cases:
            with self.subTest(translated=translated):
                result = validate_translation(original, translated, target_lang=lang)
                self.assertEqual(result.status, "pass", result)
                self.assertEqual(missing_number_tokens(original, translated), [])

    def test_wrong_or_dropped_month_still_fails(self) -> None:
        cases = [
            ("2026年10月12日", "12 novembre 2026", "fr"),
            ("2026年10月12日", "12 2026", "fr"),
            ("2026年10月12日", "octobre 2026", "fr"),
            ("2026年5月5日", "5 major 2026", "en"),
            ("2026年6月1日", "1 junior 2026", "en"),
            ("工期3个月", "Durée : mois", "fr"),
        ]
        for original, translated, lang in cases:
            with self.subTest(translated=translated):
                result = validate_translation(original, translated, target_lang=lang)
                self.assertEqual(result.status, "fail", result)


if __name__ == "__main__":
    unittest.main()
