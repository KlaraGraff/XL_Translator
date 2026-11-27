"""PDF 转 Word 后，带底色的标题条不比原文高（反馈 #28 讲义第 101 页「Example」条高了一倍）。

pdf2docx 把框上方的空白算进了第一行格子，格子涂了底色，这截空白也成了深色。
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

from core.pdf_word_bars import trim_bar_padding

DARK = (0x4D / 255, 0, 0)
LIGHT = (1.0, 0.95, 0.95)


def _make_pdf(path: Path, bar_top: float) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((70, 80), "Text above the box", fontname="helv", fontsize=10)
    # 两层的框：深色整块（标题条），下面盖浅色正文
    page.draw_rect(pymupdf.Rect(57, bar_top, 538, 300), color=None, fill=DARK)
    page.draw_rect(pymupdf.Rect(58, bar_top + 18.4, 537, 299), color=None, fill=LIGHT)
    page.insert_text((70, bar_top + 12), "Example", fontname="hebo", fontsize=11, color=(1, 1, 1))
    page.insert_text((70, bar_top + 34), "If we want to apply the extended algorithm", fontname="helv", fontsize=10)
    doc.save(path)


def _line_top(pdf: Path, text: str) -> float:
    page = pymupdf.open(str(pdf))[0]
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            if "".join(span["text"] for span in line["spans"]).startswith(text):
                return line["bbox"][1]
    raise AssertionError(text)


def _table(rows: list[tuple[int, str, int, str]]) -> str:
    """rows: (行高, 底色, 段前距, 字)"""
    xml = f'<w:tbl {nsdecls("w")}><w:tblPr/><w:tblGrid><w:gridCol w:w="9600"/></w:tblGrid>'
    for height, fill, before, text in rows:
        xml += (f'<w:tr><w:trPr><w:trHeight w:hRule="exact" w:val="{height}"/></w:trPr><w:tc>'
                f'<w:tcPr><w:tcW w:type="dxa" w:w="9600"/><w:shd w:val="clear" w:color="auto" w:fill="{fill}"/></w:tcPr>'
                f'<w:p><w:pPr><w:spacing w:before="{before}" w:after="0" w:line="218" w:lineRule="exact"/></w:pPr>'
                f'<w:r><w:t>{text}</w:t></w:r></w:p></w:tc></w:tr>')
    return xml + "</w:tbl>"


def _make_docx(path: Path, title_before: int, title_height: int, body_before: int) -> None:
    doc = Document()
    final = doc.element.body[-1]
    final.addprevious(parse_xml(
        f'<w:p {nsdecls("w")}><w:pPr><w:spacing w:before="1200" w:after="0" w:line="218" w:lineRule="exact"/></w:pPr>'
        '<w:r><w:t>Text above the box</w:t></w:r></w:p>'
    ))
    final.addprevious(parse_xml(_table([(title_height, "4C0000", title_before, "Example")])))
    final.addprevious(parse_xml(_table([(398, "FFF2F2", body_before, "If we want to apply the extended algorithm")])))
    doc.save(str(path))


def _looks(path: Path) -> tuple[list[str], list[tuple[str, str]]]:
    body = Document(str(path)).element.body
    order = []
    for child in body:
        if child.tag == qn("w:p"):
            text = "".join(node.text or "" for node in child.iter(qn("w:t")))
            spacing = child.find(f"{qn('w:pPr')}/{qn('w:spacing')}")
            order.append(f"p:{text or 'spacer ' + spacing.get(qn('w:line'))}")
        elif child.tag == qn("w:tbl"):
            order.append("tbl")
    rows = []
    for tbl in body.iter(qn("w:tbl")):
        tr = tbl.find(qn("w:tr"))
        height = tr.find(f"{qn('w:trPr')}/{qn('w:trHeight')}").get(qn("w:val"))
        before = tr.find(f".//{qn('w:spacing')}").get(qn("w:before"))
        rows.append((height, before))
    return order, rows


@unittest.skipIf(pymupdf is None, "PyMuPDF not installed")
class TrimBarPaddingTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def _run(self, padding: int) -> tuple[int, list[str], list[tuple[str, str]]]:
        """padding：pdf2docx 在标题格子里多垫的段前距（1/20 磅）。"""
        pdf, docx = self.dir / "in.pdf", self.dir / "out.docx"
        bar_top = 122.8
        _make_pdf(pdf, bar_top)
        # 段前距按原文：字的上沿到条（框）的上沿，再加多垫的那截
        title_before = round((_line_top(pdf, "Example") - bar_top) * 20) + padding
        body_before = round((_line_top(pdf, "If we want") - bar_top - 18.4) * 20)
        self.title_before, self.body_before = title_before, body_before
        _make_docx(docx, title_before, 368 + padding, body_before)
        changed = trim_bar_padding(docx, pdf)
        return (changed, *_looks(docx))

    def test_white_space_above_the_bar_moves_out_of_the_coloured_cell(self) -> None:
        # 讲义第 101 页：段前距 484（24.2 磅）、行高 766，原文字上方的底色只有 3.5 磅，多垫了约 414
        changed, order, rows = self._run(padding=414)
        self.assertEqual(changed, 1)
        self.assertEqual(order[0], "p:Text above the box")
        self.assertTrue(order[1].startswith("p:spacer "), order)
        excess = int(order[1].split()[-1])
        self.assertAlmostEqual(excess, 414, delta=10)  # 多垫的约 20 磅挪到表格前面
        self.assertEqual(rows[0], (str(368 + 414 - excess), str(self.title_before - excess)))  # 条高、段前距各减掉同样一截
        self.assertEqual(rows[1], ("398", str(self.body_before)), "正文格子不动")

    def test_bar_that_already_matches_the_original_is_left_alone(self) -> None:
        # 段前距只有字上方本来的那点底色：不动
        changed, order, rows = self._run(padding=0)
        self.assertEqual(changed, 0)
        self.assertEqual(order, ["p:Text above the box", "tbl", "tbl"])
        self.assertEqual(rows[0], ("368", str(self.title_before)))
