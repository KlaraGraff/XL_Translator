"""页眉页脚里只差数字的几行（各页页码）按同一种写法输出。

本机实测（法译中，PDF 转 Word）：三页页脚「Page 1」「Page 2」「Page 3」各自翻译，
成了「第 1 页」「第2页」「第3页」。现在以数字最小那行的译文为样板，其余页换上各自的数字。
"""
from __future__ import annotations

import unittest

from core.word_task_runner import _fill_numbered_line, _numbered_header_footer_fills

FOOTER = [{"kind": "footer"}]


class FillNumberedLineTests(unittest.TestCase):
    def test_other_page_takes_the_template_with_its_own_number(self) -> None:
        self.assertEqual(_fill_numbered_line("Page 1", "第 1 页", "Page 3"), "第 3 页")

    def test_several_numbers_map_by_value(self) -> None:
        self.assertEqual(_fill_numbered_line("Page 1 sur 3", "第 1 页，共 3 页", "Page 3 sur 3"), "第 3 页，共 3 页")
        self.assertEqual(_fill_numbered_line("Page 1 of 3", "共 3 页，第 1 页", "Page 2 of 3"), "共 3 页，第 2 页")

    def test_template_with_numbers_written_out_is_not_used(self) -> None:
        self.assertIsNone(_fill_numbered_line("Page 1", "第一页", "Page 2"))

    def test_template_with_an_extra_number_is_not_used(self) -> None:
        self.assertIsNone(_fill_numbered_line("Page 1", "第 1 页 2026", "Page 2"))


class NumberedHeaderFooterFillTests(unittest.TestCase):
    def test_page_numbers_follow_the_first_page(self) -> None:
        translations = {"Page 1": "第 1 页", "Page 2": "第2页", "Page 3": "第3页", "Titre": "标题"}
        locations = {"Page 1": FOOTER, "Page 2": FOOTER, "Page 3": FOOTER, "Titre": FOOTER}
        self.assertEqual(
            _numbered_header_footer_fills(translations, locations), {"Page 2": "第 2 页", "Page 3": "第 3 页"}
        )

    def test_whole_footer_line_with_a_page_number(self) -> None:
        translations = {
            "Méthodologie d’intervention\tPage 2": "干预方法\t第2页",
            "Méthodologie d’intervention\tPage 1": "干预方法\t第 1 页",
        }
        locations = dict.fromkeys(translations, FOOTER)
        self.assertEqual(
            _numbered_header_footer_fills(translations, locations),
            {"Méthodologie d’intervention\tPage 2": "干预方法\t第 2 页"},
        )

    def test_body_paragraphs_are_left_alone(self) -> None:
        translations = {"Article 1": "第一条", "Article 2": "第 2 条"}
        locations = dict.fromkeys(translations, [{"kind": "paragraph"}])
        self.assertEqual(_numbered_header_footer_fills(translations, locations), {})

    def test_line_whose_template_does_not_fit_keeps_its_translation(self) -> None:
        translations = {"Page 1": "第一页", "Page 2": "第2页"}
        locations = dict.fromkeys(translations, FOOTER)
        self.assertEqual(_numbered_header_footer_fills(translations, locations), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
