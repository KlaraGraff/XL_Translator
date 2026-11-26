"""原位替换拼段：不同字体的两行不是同一段（2026-11-26，设备手册第 8 页）。

屏幕示意框里的按键名「Press」（打字机体）正好在下面那段正文（窄体、小写开头）的左上方，
原来被当成这段的第一行，送去翻译的是「Press thefirebeam is looking…」，模型译成
「按下;thefirebeam…」。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pytest

fitz = pytest.importorskip("fitz")


class FontFamilyParagraphTests(unittest.TestCase):
    def _texts(self, label_font: str) -> list[str]:
        from core.pdf_vector import translate_page_inplace

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            doc = fitz.open()
            page = doc.new_page(width=595, height=842)
            page.insert_text((112, 228), "Press", fontsize=12, fontname=label_font)
            page.insert_text((101, 256), "the beam is looking for the far wall of the building", fontsize=12,
                             fontname="helv")
            page.insert_text((101, 272), "and the signal strength is shown on the display", fontsize=12,
                             fontname="helv")
            doc.save(root / "source.pdf")
            seen: list[str] = []

            def translate(texts, prompt, **_extra):
                seen.extend(texts)
                return {t: "译文" for t in texts}

            translate_page_inplace(root / "source.pdf", 0, root / "work", translate=translate,
                                   source_lang="en", target_lang="zh")
            return seen

    def test_a_label_in_another_font_is_not_the_first_line_of_the_paragraph(self):
        texts = self._texts("cour")
        self.assertIn("Press", texts)
        self.assertTrue(any(t.startswith("the beam is looking") and "signal strength" in t for t in texts), texts)

    def test_a_bold_word_in_the_same_typeface_still_joins(self):
        texts = self._texts("hebo")  # Helvetica-Bold
        self.assertTrue(any(t.startswith("Press the beam is looking") for t in texts), texts)


if __name__ == "__main__":
    unittest.main()


class LegendAndArrowTests(unittest.TestCase):
    """设备手册第 5 页颜色图例、第 8 页「Press → (right)」（2026-11-26）。"""

    def _page(self, build):
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        build(page)
        return doc, page

    def test_leading_spaces_do_not_stretch_the_box_over_the_colour_dot(self):
        from core.pdf_vector.extract import native_rows

        def build(page):
            page.draw_circle((79, 210), 5, color=None, fill=(0.14, 0.12, 0.13))
            page.insert_text((73.7, 214), "      Black    zone +", fontsize=12, fontname="helv")

        doc, page = self._page(build)
        (row,) = native_rows(page, 72)
        self.assertEqual(row["source_text"], "Black zone +")
        self.assertGreater(row["display_box"][0], 85)  # starts at 'B', right of the dot

    def test_a_drawn_arrow_between_two_words_makes_two_labels(self):
        from core.pdf_vector.extract import native_rows

        def build(page):
            page.insert_text((242.6, 308), "Press          (right)", fontsize=10, fontname="helv")
            page.draw_line((269.5, 304.6), (279.1, 304.6), color=(0, 0, 0), width=0.99)
            page.draw_polyline([(278.6, 300.7), (282.8, 304.6), (278.6, 308.5), (278.6, 300.7)],
                               color=None, fill=(0, 0, 0), closePath=True)

        doc, page = self._page(build)
        self.assertEqual([r["source_text"] for r in native_rows(page, 72)], ["Press", "(right)"])

    def test_words_with_only_space_between_stay_one_line(self):
        from core.pdf_vector.extract import native_rows

        doc, page = self._page(lambda page: page.insert_text((100, 300), "Press          enter", fontsize=10))
        self.assertEqual([r["source_text"] for r in native_rows(page, 72)], ["Press enter"])

    def test_a_standalone_minus_at_the_end_does_not_continue_into_the_next_line(self):
        from core.pdf_vector.extract import continues

        self.assertFalse(continues("Grey zone -", "Green earth (screen)"))
        self.assertTrue(continues("the exam-", "Ple of the beam"))
