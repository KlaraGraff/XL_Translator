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


class NumberedListPageTests(unittest.TestCase):
    """说明书第 7 页（2026-11-27）：编号第 6 段、小标题「stage two」、第 7 段和下面的红字。"""

    def _groups(self) -> list[str]:
        from core.pdf_vector.extract import group_paragraphs, native_rows, rules

        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        tw = fitz.TextWriter(page.rect)
        body, bold = fitz.Font("helv"), fitz.Font("hebo")
        y = 520
        x = tw.append((75, y), "6. Use the left and right keys to toggle, the ", font=body, fontsize=12)[1].x
        # 一个 20 pt、往下沉的「*」：行框往下多出 8 pt，压到下一行上
        x = tw.append((x, y + 8), "*", font=body, fontsize=20)[1].x
        tw.append((x, y), " indicates which mode is selected. Press", font=body, fontsize=12)
        tw.append((86, y + 14.7), "the enter key to continue.", font=body, fontsize=12)
        tw.append((75, y + 40), "stage two, pre-alignment", font=bold, fontsize=15)
        y = 590
        tw.append((75, y), "7. The next screen is", font=body, fontsize=12)
        tw.append((87, y + 15), "Pre-alignment sets up the amount of power you need for the distance and can indicate if",
                  font=body, fontsize=12)
        tw.append((87, y + 29), "you are receiving unwanted reflections from anything else in the beam path.",
                  font=body, fontsize=12)
        tw.write_text(page)
        V, Hs = rules(page, 72)
        return [r["source_text"] for r in group_paragraphs(native_rows(page, 72), V, Hs)]

    def test_a_large_glyph_in_a_line_does_not_cut_the_paragraph(self):
        groups = self._groups()
        self.assertIn("6. Use the left and right keys to toggle, the * indicates which mode is selected. "
                      "Press the enter key to continue.", groups)
        self.assertIn("stage two, pre-alignment", groups)  # the heading stays a heading

    def test_a_line_does_not_join_across_another_line(self):
        groups = self._groups()
        self.assertIn("7. The next screen is", groups)
        self.assertIn("Pre-alignment sets up the amount of power you need for the distance and can indicate if "
                      "you are receiving unwanted reflections from anything else in the beam path.", groups)


class BadgeAndRunInTests(unittest.TestCase):
    """说明书第 7 页（2026-11-27 Mac 复查）：标题里的带圈编号 ❸、从行中间开始折回行首的红字。"""

    GREY, RED, WHITE, BLACK = (0.63, 0.64, 0.65), (0.82, 0.14, 0.16), (1, 1, 1), (0.14, 0.12, 0.13)

    def _rows(self, build, grouped=False):
        from core.pdf_vector.extract import group_paragraphs, native_rows, rules

        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        build(page)
        rows = native_rows(page, 72)
        if grouped:
            V, Hs = rules(page, 72)
            rows = group_paragraphs(rows, V, Hs)
        return [r["source_text"] for r in rows]

    def _heading(self, page):
        font = fitz.Font("hebo")
        x = 75.0
        for text, colour in (("step  ", self.BLACK), ("3", self.WHITE), ("  commissioning", self.BLACK)):
            if colour == self.WHITE:
                page.draw_circle((x + 5.8, 83), 10.9, color=None, fill=self.BLACK)
            tw = fitz.TextWriter(page.rect)
            x = tw.append((x, 90), text, font=font, fontsize=21)[1].x
            tw.write_text(page, color=colour)

    def test_a_white_number_on_a_black_disc_is_kept_apart_from_the_words(self):
        self.assertEqual(self._rows(self._heading), ["step", "3", "commissioning"])

    def test_white_words_on_one_dark_bar_stay_one_line(self):
        def build(page):
            page.draw_rect(fitz.Rect(70, 70, 330, 96), color=None, fill=self.BLACK)
            page.insert_text((75, 90), "stage one commissioning", fontsize=18, fontname="hebo", color=self.WHITE)

        self.assertEqual(self._rows(build), ["stage one commissioning"])

    def test_a_dark_word_on_a_light_highlight_stays_in_its_sentence(self):
        def build(page):
            page.draw_rect(fitz.Rect(110, 78, 145, 94), color=None, fill=(1, 0.95, 0.4))
            page.insert_text((75, 90), "Do NOT put the reflector up.", fontsize=12, fontname="helv",
                             color=self.BLACK)

        self.assertEqual(self._rows(build), ["Do NOT put the reflector up."])

    def _wrapped_red(self, head_text, head_colour, below_right=False):
        def build(page):
            page.insert_text((86, 220), head_text, fontsize=12, fontname="helv", color=head_colour)
            page.draw_rect(fitz.Rect(245, 205, 290, 236), color=self.RED, width=1)  # the crossed-out picture
            page.insert_text((300, 220), "You cannot commission the beam if the reflector can be", fontsize=12,
                             fontname="helv", color=self.RED)
            page.insert_text((86, 234), "seen", fontsize=12, fontname="helv", color=self.RED)
            if below_right:
                page.insert_text((300, 234), "Power up the unit and wait", fontsize=12, fontname="helv",
                                 color=self.RED)
        return self._rows(build, grouped=True)

    def test_a_sentence_starting_mid_line_takes_its_wrapped_word_from_the_line_start(self):
        rows = self._wrapped_red("non-reflective black cloth or similar.", self.GREY)
        self.assertIn("You cannot commission the beam if the reflector can be seen", rows)
        self.assertIn("non-reflective black cloth or similar.", rows)

    def test_the_line_start_still_goes_to_the_head_of_its_own_line_when_that_runs_on(self):
        rows = self._wrapped_red("non-reflective black cloth or", self.RED)
        self.assertNotIn("You cannot commission the beam if the reflector can be seen", rows)

    def test_no_run_in_when_the_row_has_its_own_next_line(self):
        rows = self._wrapped_red("non-reflective black cloth or similar.", self.GREY, below_right=True)
        self.assertNotIn("You cannot commission the beam if the reflector can be seen", rows)
        self.assertIn("seen", rows)

    def test_whole_page_keeps_the_disc_number_and_writes_the_sentence_where_it_began(self):
        from core.pdf_vector import translate_page_inplace
        from core.pdf_vector.fonts import find_cjk_font

        if not find_cjk_font():
            self.skipTest("no CJK font on this machine: in place cannot write Chinese")

        T = {"step": "步骤", "commissioning": "调试",
             "You cannot commission the beam if the reflector can be seen": "如果能看到反射器，则无法调试光束"}

        def build(page):
            self._heading(page)
            page.insert_text((86, 220), "non-reflective black cloth or similar.", fontsize=12, fontname="helv",
                             color=self.GREY)
            page.draw_rect(fitz.Rect(245, 205, 290, 236), color=self.RED, width=1)
            page.insert_text((300, 220), "You cannot commission the beam if the reflector can be", fontsize=12,
                             fontname="helv", color=self.RED)
            page.insert_text((86, 234), "seen", fontsize=12, fontname="helv", color=self.RED)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            doc = fitz.open()
            build(doc.new_page(width=595, height=842))
            doc.save(root / "source.pdf")
            seen: list[str] = []

            def translate(texts, prompt, **_extra):
                seen.extend(texts)
                return {t: T.get(t, "灰色译文") for t in texts}

            res = translate_page_inplace(root / "source.pdf", 0, root / "work", translate=translate,
                                         source_lang="en", target_lang="zh")
            self.assertTrue(res.ok, res.error)
            self.assertNotIn("3", seen)
            out = fitz.open(res.output_pdf)[0]
            words = {w[4]: fitz.Rect(w[:4]) for w in out.get_text("words")}
            self.assertIn("3", words)  # the white 「3」 on the disc is untouched
            disc = fitz.Rect(words["3"].x0 - 3, 72, words["3"].x1 + 3, 94)
            for w, box in words.items():
                if "调试" in w or "步骤" in w:
                    self.assertFalse(box.intersects(disc), (w, box))
            sentence = next(box for w, box in words.items() if "反射器" in w)
            self.assertGreaterEqual(sentence.x0, 295)  # where the red sentence began, not under 「seen」
            self.assertFalse(any(w.startswith("看到") for w in words))


class RunInHeadingTests(unittest.TestCase):
    """讲义的粗体小标题后面接正文（「Negation.  We start with …」）：正文从行中间开始，
    折回行首继续。整段一起翻，按左对齐写，不因第一行起点靠右被当成居中。"""

    def test_paragraph_after_a_run_in_heading_is_one_left_aligned_paragraph(self):
        from core.pdf_vector.extract import group_paragraphs, native_rows, rules

        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 100), "Negation.", fontsize=11, fontname="hebo")
        page.insert_text((135, 100), "We start with the simplest logical operation: negation. If A is any", fontsize=11,
                         fontname="helv")
        page.insert_text((72, 113.5), "logical statement, then not A stands for the negation of A, and it is again",
                         fontsize=11, fontname="helv")
        page.insert_text((72, 127), "a logical statement whose value is the opposite one.", fontsize=11, fontname="helv")
        V, Hs = rules(page, 72)
        rows = group_paragraphs(native_rows(page, 72), V, Hs)
        (para,) = [r for r in rows if r["source_text"].startswith("We start")]
        self.assertTrue(para["source_text"].endswith("the opposite one."), para["source_text"])
        self.assertEqual(para["line_align"], "start")
        self.assertIn("Negation.", [r["source_text"] for r in rows])


class RunInGapTests(unittest.TestCase):
    """讲义第 18 页（2026-11-27 Mac 复查）：小标题「Implication.」译成「蕴含。」后，后面的正文
    原来从英文正文的起点开始写，中间空一大截；现在接在「蕴含。」后面。"""

    def test_the_sentence_starts_right_after_the_translated_heading(self):
        from core.pdf_vector import translate_page_inplace
        from core.pdf_vector.fonts import find_cjk_font

        if not find_cjk_font():
            self.skipTest("no CJK font on this machine: in place cannot write Chinese")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            doc = fitz.open()
            page = doc.new_page(width=595, height=842)
            page.insert_text((57, 552), "Implication.", fontsize=11, fontname="hebo")
            page.insert_text((131, 552), "Our next operation is called implication. Implication roughly corresponds to",
                             fontsize=11, fontname="helv")
            page.insert_text((57, 565.5), "statements in English of the form if A is true then B is true, and we write it",
                             fontsize=11, fontname="helv")
            page.insert_text((57, 579), "as an arrow between the two statements.", fontsize=11, fontname="helv")
            doc.save(root / "source.pdf")

            def translate(texts, prompt, **_extra):
                return {t: "蕴含。" if t == "Implication." else "我们的下一个运算叫做蕴含" * 3 for t in texts}

            res = translate_page_inplace(root / "source.pdf", 0, root / "work", translate=translate,
                                         source_lang="en", target_lang="zh")
            self.assertTrue(res.ok, res.error)
            chars = [(c["c"], fitz.Rect(c["bbox"]))
                     for b in fitz.open(res.output_pdf)[0].get_text("rawdict")["blocks"]
                     for ln in b.get("lines", []) for sp in ln["spans"] for c in sp["chars"]]
            stop = next(box for c, box in chars if c == "。")  # end of 「蕴含。」
            first = min((box for c, box in chars if c == "我" and abs(box.y0 - stop.y0) < 4), key=lambda r: r.x0)
            self.assertLess(first.x0 - stop.x1, 12)  # was 131 - (57 + 3 字) ≈ 40 pt of blank
            self.assertGreater(first.x0, stop.x1 - 0.5)  # and never on top of the heading


class NeighbourContextTests(unittest.TestCase):
    """说明书封面和第 7 页的屏幕框「the Firebeam / Xtra」（2026-11-27 Mac 复查）：「Xtra」单独送去，
    模型看不到上一行，把产品名译成「额外」。现在这一页的翻译要求后面附上短文字和相邻文字
    连起来的样子。"""

    def test_a_short_label_is_sent_with_the_lines_next_to_it(self):
        from core.pdf_vector import translate_page_inplace

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            doc = fitz.open()
            page = doc.new_page(width=595, height=842)
            page.draw_rect(fitz.Rect(258, 236, 336, 268), color=(0, 0, 0), width=1)
            page.insert_text((262, 250), "the Firebeam", fontsize=10, fontname="cour")
            page.insert_text((278, 262), "Xtra", fontsize=10, fontname="cour")
            page.insert_text((60, 400), "Power up the unit and wait for the display to settle.", fontsize=12,
                             fontname="helv")
            doc.save(root / "source.pdf")
            prompts: list[str] = []

            def translate(texts, prompt, **_extra):
                prompts.append(prompt)
                return {t: "译文" for t in texts}

            translate_page_inplace(root / "source.pdf", 0, root / "work", translate=translate,
                                   source_lang="en", target_lang="zh")
            self.assertTrue(prompts)
            self.assertIn("the Firebeam / 「Xtra」", prompts[0])
            # 长句不算短文字，不附
            self.assertNotIn("「Power up", prompts[0])


class LineStartPunctuationTests(unittest.TestCase):
    """讲义第 18 页（2026-11-27 Mac 复查）：页尾最后一句的「。」单独掉到下一行。"""

    def setUp(self):
        from core.pdf_vector.fonts import find_cjk_font

        path = find_cjk_font()
        if not path:
            self.skipTest("no CJK font on this machine")
        self.font = fitz.Font(fontfile=str(path))

    def test_a_full_stop_hangs_at_the_end_of_a_full_line(self):
        from core.pdf_vector.writeback import wrap

        self.assertEqual(wrap("一二三四五。", self.font, 10, [51, 50]), ["一二三四五。"])
        self.assertEqual(wrap("一二三四五。六", self.font, 10, [51, 50]), ["一二三四五。", "六"])

    def test_a_run_of_marks_takes_the_last_character_down_with_it(self):
        from core.pdf_vector.writeback import wrap

        self.assertEqual(wrap("一二三四五……", self.font, 10, [51, 50]), ["一二三四", "五……"])

    def test_ordinary_wrapping_is_unchanged(self):
        from core.pdf_vector.writeback import wrap

        self.assertEqual(wrap("一二三四五六七", self.font, 10, [40, 40]), ["一二三四", "五六七"])
        self.assertEqual(wrap("one two three four", self.font, 10, [45, 80]), ["one two", "three four"])
        self.assertIsNone(wrap("一二三四五六", self.font, 10, [51]))
