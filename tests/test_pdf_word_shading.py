"""PDF 转 Word 后，彩色框格子的底色按原页面改正、丢了的上标补回（反馈 #28）。

讲义的「Note」框画成深色一整块上面盖一块浅色，pdf2docx 只认出深色，正文格子成了深蓝底
黑字；标题格子有时又没上色，白字落在白底上。2^k 的 k 成了同一行里小一号的普通字。
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

from core.pdf_word_scripts import pdf_scripts, restore_scripts_from_pdf
from core.pdf_word_shading import recolor_cells_from_pdf

DARK = (10 / 255, 10 / 255, 90 / 255)
LIGHT = (0.88, 0.92, 1.0)
BODY = "The table size is usually a power of two, so m = 2"


def _make_pdf(path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    # 两层的框：深色整块，下面三分之二盖浅色
    page.draw_rect(pymupdf.Rect(60, 100, 535, 180), color=None, fill=DARK)
    page.draw_rect(pymupdf.Rect(62, 122, 533, 178), color=None, fill=LIGHT)
    page.insert_text((70, 115), "Note 1.1", fontname="hebo", fontsize=11, color=(1, 1, 1))
    page.insert_text((70, 140), BODY, fontname="helv", fontsize=10)
    page.insert_text((70 + pymupdf.get_text_length(BODY, "helv", 10), 136), "k", fontname="helv", fontsize=7)
    # 白底的普通表格
    page.insert_text((70, 260), "Plain cell text on white", fontname="helv", fontsize=10)
    doc.save(path)


def _cell(text_xml: str, fill: str | None) -> str:
    shd = f'<w:shd w:fill="{fill}"/>' if fill else ""
    return (f'<w:tbl {nsdecls("w")}><w:tblPr/><w:tblGrid><w:gridCol w:w="9500"/></w:tblGrid><w:tr><w:tc>'
            f'<w:tcPr><w:tcW w:type="dxa" w:w="9500"/><w:tcBorders/>{shd}<w:tcMar/></w:tcPr>'
            f'<w:p>{text_xml}</w:p></w:tc></w:tr></w:tbl>')


def _run(text: str, *, color: str = "000000", size: int = 20) -> str:
    return (f'<w:r><w:rPr><w:color w:val="{color}"/><w:sz w:val="{size}"/></w:rPr>'
            f'<w:t xml:space="preserve">{text}</w:t></w:r>')


def _make_docx(path: Path) -> None:
    doc = Document()
    body = doc.element.body
    final = body[-1]
    final.addprevious(parse_xml(_cell(_run("Note 1.1", color="FFFFFF", size=22), None)))
    final.addprevious(parse_xml(_cell(_run(BODY + " ") + _run("k", size=14), "0a0a5a")))
    final.addprevious(parse_xml(_cell(_run("Plain cell text on white"), None)))
    doc.save(str(path))


def _fills(path: Path) -> list[str | None]:
    doc = Document(str(path))
    result = []
    for tc in doc.element.body.iter(qn("w:tc")):
        shd = tc.find(f"{qn('w:tcPr')}/{qn('w:shd')}")
        result.append(shd.get(qn("w:fill")).upper() if shd is not None else None)
    return result


@unittest.skipIf(pymupdf is None, "PyMuPDF not installed")
class BoxShadingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.pdf = root / "box.pdf"
        self.docx = root / "box.docx"
        _make_pdf(self.pdf)
        _make_docx(self.docx)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_cells_take_the_colour_behind_their_text(self) -> None:
        self.assertEqual(recolor_cells_from_pdf(self.docx, self.pdf), 2)
        title, body, plain = _fills(self.docx)
        self.assertEqual(title, "0A0A5A")  # 白字的标题格子补上深蓝
        self.assertEqual(body, "E0EAFF")  # 正文格子从深蓝改回浅蓝
        self.assertIsNone(plain)  # 白底黑字的普通格子不动

    def test_matching_colour_is_left_alone(self) -> None:
        recolor_cells_from_pdf(self.docx, self.pdf)
        self.assertEqual(recolor_cells_from_pdf(self.docx, self.pdf), 0)

    def test_page_count_mismatch_changes_nothing(self) -> None:
        doc = Document(str(self.docx))
        final = doc.element.body[-1]
        final.addprevious(parse_xml(f'<w:p {nsdecls("w")}><w:pPr><w:sectPr/></w:pPr></w:p>'))
        doc.save(str(self.docx))
        self.assertEqual(recolor_cells_from_pdf(self.docx, self.pdf), 0)
        self.assertEqual(_fills(self.docx), [None, "0A0A5A", None])


@unittest.skipIf(pymupdf is None, "PyMuPDF not installed")
class ScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.pdf = root / "box.pdf"
        self.docx = root / "box.docx"
        _make_pdf(self.pdf)
        _make_docx(self.docx)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_pdf_superscript_is_found(self) -> None:
        with pymupdf.open(self.pdf) as doc:
            found = pdf_scripts(doc[0])
        self.assertEqual(found, [("wo,som=2", "k", "superscript", False)])

    def test_superscript_is_marked_and_glued(self) -> None:
        self.assertEqual(restore_scripts_from_pdf(self.docx, self.pdf), 1)
        doc = Document(str(self.docx))
        runs = [r for r in doc.element.body.iter(qn("w:r")) if "".join(t.text or "" for t in r.iter(qn("w:t"))) in (BODY, "k")]
        before, k = runs
        self.assertEqual(before.find(qn("w:t")).text, BODY)  # pdf2docx 加的空格去掉了
        r_pr = k.find(qn("w:rPr"))
        self.assertEqual(r_pr.find(qn("w:vertAlign")).get(qn("w:val")), "superscript")
        self.assertEqual(r_pr.find(qn("w:sz")).get(qn("w:val")), "20")  # 字号还原，Word 自己缩小
        # 再跑一次不重复标
        self.assertEqual(restore_scripts_from_pdf(self.docx, self.pdf), 0)


if __name__ == "__main__":
    unittest.main()
