"""PDF 转 Word 后，公式里画出来的上划线、分数线补回来（反馈 #28 数学讲义：A ∪ B 的补集成了 A ∪ B）。

一条线整条盖住它下面的整段字：做成一个带横线的 Word 公式，不是每个字各加一截。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from docx import Document
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls, qn

try:
    import pymupdf
except ImportError:  # pragma: no cover - PyMuPDF is in requirements-test.txt
    pymupdf = None

from core.pdf_word_rules import OVERLINE, UNDERLINE, pdf_rules, restore_rules_from_pdf


def _text_box(page, text: str):
    hits = page.search_for(text)
    assert hits, text
    return hits[0]


def _make_pdf(path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    # 「we have AB = BA」里第一个 AB 上面有上划线
    page.insert_text((70, 100), "we have AB = BA here", fontname="helv", fontsize=11)
    line_top = page.get_text("dict")["blocks"][0]["lines"][0]["bbox"][1]
    ab = _text_box(page, "AB")
    page.draw_line((ab.x0, line_top - 0.3), (ab.x1, line_top - 0.3), width=0.4)
    # 分数 1 / (1 - r)：分子一行、分数线、分母一行
    page.insert_text((200, 160), "1", fontname="helv", fontsize=11)
    page.insert_text((190, 178), "1 - r", fontname="helv", fontsize=11)
    den = _text_box(page, "1 - r")
    num = _text_box(page, "1")
    bar_y = (num.y1 + den.y0) / 2
    page.draw_line((den.x0, bar_y), (den.x1, bar_y), width=0.4)
    # 框线：比字宽得多，不算
    page.insert_text((70, 260), "Boxed text", fontname="helv", fontsize=11)
    boxed = _text_box(page, "Boxed text")
    page.draw_line((60, boxed.y0 - 0.3), (530, boxed.y0 - 0.3), width=0.4)
    doc.save(path)


def _make_docx(path: Path) -> None:
    doc = Document()
    final = doc.element.body[-1]
    for text in ("we have AB = BA here", "1", "1 - r", "Boxed text"):
        final.addprevious(parse_xml(f'<w:p {nsdecls("w")}><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'))
    doc.save(str(path))


def _bars(paragraph) -> list[tuple[str, str]]:
    m = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
    return [(bar.find(f"{m}barPr/{m}pos").get(f"{m}val"), "".join(t.text or "" for t in bar.iter(f"{m}t")))
            for bar in paragraph.iter(f"{m}bar")]


@unittest.skipIf(pymupdf is None, "PyMuPDF not installed")
class RestoreRulesTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.pdf = Path(tmp.name) / "in.pdf"
        self.docx = Path(tmp.name) / "out.docx"
        _make_pdf(self.pdf)
        _make_docx(self.docx)

    def test_overline_and_fraction_bar_are_found_but_not_box_borders(self) -> None:
        text, rules = pdf_rules(pymupdf.open(str(self.pdf))[0])
        self.assertEqual(sorted(("".join(text[i] for i in indices), mark) for indices, mark, _fraction in rules),
                         sorted([("AB", OVERLINE), ("1-r", OVERLINE)]))

    def test_covered_text_becomes_one_barred_formula(self) -> None:
        self.assertEqual(restore_rules_from_pdf(self.docx, self.pdf), 2)
        paragraphs = Document(str(self.docx)).paragraphs
        self.assertEqual([p.text for p in paragraphs], ["we have  = BA here", "1", "", "Boxed text"])
        self.assertEqual(_bars(paragraphs[0]._p), [("top", "AB")], "一条横线盖住 AB 整段")
        self.assertEqual(_bars(paragraphs[2]._p), [("top", "1 - r")], "分母上面一条线")
        self.assertEqual(_bars(paragraphs[1]._p) + _bars(paragraphs[3]._p), [], "分子、框线里的字不动")

    def test_superscript_inside_the_bar_becomes_a_formula_superscript(self) -> None:
        doc = Document()
        doc.element.body[-1].addprevious(parse_xml(
            f'<w:p {nsdecls("w")}><w:r><w:t xml:space="preserve">1 - r</w:t></w:r>'
            '<w:r><w:rPr><w:vertAlign w:val="superscript"/></w:rPr><w:t>n+1</w:t></w:r></w:p>'
        ))
        paragraph = doc.paragraphs[0]._p
        from core.pdf_word_rules import _apply, _positions

        _text, where = _positions(paragraph)
        self.assertTrue(_apply(paragraph, where, 0, 6, UNDERLINE))
        m = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
        bar = paragraph.find(f"{m}oMath/{m}bar")
        self.assertEqual(bar.find(f"{m}barPr/{m}pos").get(f"{m}val"), "bot")
        body = bar.find(f"{m}e")
        self.assertEqual([child.tag.split("}")[1] for child in body], ["r", "sSup"])
        self.assertEqual(body.find(f"{m}r/{m}t").text, "1 - ")
        self.assertEqual(body.find(f"{m}sSup/{m}e/{m}r/{m}t").text, "r")
        self.assertEqual(body.find(f"{m}sSup/{m}sup/{m}r/{m}t").text, "n+1")
        self.assertIsNone(body.find(f".//{qn('w:vertAlign')}"), "公式里的上标不再带 w:vertAlign")

    def test_wider_numerator_gets_the_line_under_it(self) -> None:
        doc = pymupdf.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((190, 160), "1 - r + s", fontname="helv", fontsize=11)
        page.insert_text((205, 178), "2", fontname="helv", fontsize=11)
        num, den = _text_box(page, "1 - r + s"), _text_box(page, "2")
        bar_y = (num.y1 + den.y0) / 2
        page.draw_line((num.x0, bar_y), (num.x1, bar_y), width=0.4)
        text, rules = pdf_rules(page)
        self.assertEqual([("".join(text[i] for i in indices), mark) for indices, mark, _f in rules],
                         [("1-r+s", UNDERLINE)])
        self.assertEqual([("".join(text[i] for i in f[0]), "".join(text[i] for i in f[1])) for _i, _m, f in rules],
                         [("1-r+s", "2")])

    def test_fraction_in_one_paragraph_becomes_a_word_fraction(self) -> None:
        # pdf2docx 常把分子、分母放进同一段，靠换行或自动折行排成上下两行，行距只有半行时叠成一团
        doc = Document()
        doc.element.body[-1].addprevious(parse_xml(
            f'<w:p {nsdecls("w")}><w:pPr><w:spacing w:before="338" w:line="150" w:lineRule="exact"/></w:pPr>'
            '<w:r><w:t xml:space="preserve">we have 1</w:t></w:r><w:r><w:tab/></w:r>'
            '<w:r><w:t xml:space="preserve">1 - r here</w:t></w:r></w:p>'
        ))
        doc.save(str(self.docx))
        pdf = pymupdf.open()
        page = pdf.new_page(width=595, height=842)
        page.insert_text((70, 169), "we have", fontname="helv", fontsize=11)
        page.insert_text((130, 160), "1", fontname="helv", fontsize=11)
        page.insert_text((120, 178), "1 - r", fontname="helv", fontsize=11)
        page.insert_text((160, 169), "here", fontname="helv", fontsize=11)
        num, den = _text_box(page, "1"), _text_box(page, "1 - r")
        bar_y = (num.y1 + den.y0) / 2
        page.draw_line((den.x0, bar_y), (den.x1, bar_y), width=0.4)
        pdf.save(str(self.pdf))
        self.assertEqual(restore_rules_from_pdf(self.docx, self.pdf), 1)
        paragraph = Document(str(self.docx)).paragraphs[0]._p
        m = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
        fraction = paragraph.find(f"{m}oMath/{m}f")
        self.assertIsNotNone(fraction)
        self.assertEqual("".join(t.text for t in fraction.find(f"{m}num").iter(f"{m}t")), "1")
        self.assertEqual("".join(t.text for t in fraction.find(f"{m}den").iter(f"{m}t")), "1 - r")
        self.assertIsNone(paragraph.find(f".//{qn('w:tab')}"), "分子分母中间的制表符去掉")
        spacing = paragraph.find(f"{qn('w:pPr')}/{qn('w:spacing')}")
        # 合成一行：行距「至少」原文分数那么高，段前距减掉多出来的一半，不往下推
        line = int(spacing.get(qn("w:line")))
        self.assertEqual(spacing.get(qn("w:lineRule")), "atLeast")
        self.assertGreater(line, 300)
        self.assertEqual(int(spacing.get(qn("w:before"))), 338 - (line - 300) // 2)

    def test_bar_goes_on_the_same_letter_not_on_another_one_with_the_same_neighbours(self) -> None:
        # 讲义第 48 页：「A 和 B 的交集」里的 A 本来没有横线，横线在后面「Ā, the complement of A」那个 A 上
        sentence = "the region of A and B. Shaded: of A, the complement of A."
        pdf = pymupdf.open()
        page = pdf.new_page(width=595, height=842)
        page.insert_text((40, 100), sentence, fontname="helv", fontsize=11)
        line_top = page.get_text("dict")["blocks"][0]["lines"][0]["bbox"][1]
        a = next(box for box in page.search_for("A") if box.x0 > _text_box(page, "Shaded").x1)
        page.draw_line((a.x0, line_top - 0.3), (a.x1, line_top - 0.3), width=0.4)
        pdf.save(str(self.pdf))
        doc = Document()
        doc.element.body[-1].addprevious(parse_xml(
            f'<w:p {nsdecls("w")}><w:r><w:t xml:space="preserve">{sentence}</w:t></w:r></w:p>'))
        doc.save(str(self.docx))
        self.assertEqual(restore_rules_from_pdf(self.docx, self.pdf), 1)
        paragraph = Document(str(self.docx)).paragraphs[0]
        self.assertEqual(paragraph.text, "the region of A and B. Shaded: of , the complement of A.")
