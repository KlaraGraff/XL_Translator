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

    def test_cells_are_found_when_the_converter_dropped_the_spaces(self) -> None:
        # pdf2docx 转 LaTeX 讲义时常丢词间空格（「Twosetsareequal…」），按原句找不到
        doc = Document()
        final = doc.element.body[-1]
        final.addprevious(parse_xml(_cell(_run(BODY.replace(" ", "")), "0a0a5a")))
        doc.save(str(self.docx))
        self.assertEqual(recolor_cells_from_pdf(self.docx, self.pdf), 1)
        self.assertEqual(_fills(self.docx), ["E0EAFF"])

    def test_blank_cell_follows_the_cells_of_its_table(self) -> None:
        # 两格一行：右边那格没字，pdf2docx 给它上的也是深色外层的颜色
        row = (f'<w:tbl {nsdecls("w")}><w:tblPr/><w:tblGrid><w:gridCol w:w="7000"/><w:gridCol w:w="2500"/></w:tblGrid>'
               f'<w:tr><w:tc><w:tcPr><w:shd w:fill="0a0a5a"/></w:tcPr><w:p>{_run(BODY)}</w:p></w:tc>'
               f'<w:tc><w:tcPr><w:shd w:fill="0a0a5a"/></w:tcPr><w:p/></w:tc></w:tr></w:tbl>')
        blank_elsewhere = _cell("", "0a0a5a")  # 别的表里没字的深色格子：不知道该是什么颜色，不动
        doc = Document()
        final = doc.element.body[-1]
        final.addprevious(parse_xml(row))
        final.addprevious(parse_xml(blank_elsewhere))
        doc.save(str(self.docx))
        self.assertEqual(recolor_cells_from_pdf(self.docx, self.pdf), 2)
        self.assertEqual(_fills(self.docx), ["E0EAFF", "E0EAFF", "0A0A5A"])

    def _row_table(self, cells: str, height: int = 400) -> str:
        return (f'<w:tbl {nsdecls("w")}><w:tblPr/><w:tblGrid><w:gridCol w:w="9500"/></w:tblGrid>'
                f'<w:tr><w:trPr><w:trHeight w:hRule="exact" w:val="{height}"/></w:trPr>{cells}</w:tr></w:tbl>')

    def test_short_and_unfound_cells_take_the_colour_learned_on_the_page(self) -> None:
        # 框里的真值表：「T」「F」太短找不到位置，套着它的外层格子自己没字；同一页正文格子
        # 学到「深蓝 → 浅蓝」，它们照着改。标题条上空着的那格跟着同表的标题，仍是深蓝。
        title_row = self._row_table(
            f'<w:tc><w:tcPr><w:shd w:fill="0a0a5a"/></w:tcPr><w:p>{_run("Note 1.1", color="FFFFFF")}</w:p></w:tc>'
            '<w:tc><w:tcPr><w:shd w:fill="0a0a5a"/></w:tcPr><w:p/></w:tc>')
        truth = self._row_table(
            '<w:tc><w:tcPr><w:shd w:fill="0a0a5a"/></w:tcPr>'
            + self._row_table(f'<w:tc><w:p>{_run("T")}</w:p></w:tc><w:tc><w:p>{_run("F")}</w:p></w:tc>')
            + '<w:p/></w:tc>')
        doc = Document()
        final = doc.element.body[-1]
        for xml in (title_row, _cell(_run(BODY.replace(" ", "")), "0a0a5a"), truth,
                    self._row_table(f'<w:tc><w:tcPr><w:shd w:fill="0a0a5a"/></w:tcPr><w:p>{_run("T")}</w:p></w:tc>'),
                    self._row_table('<w:tc><w:tcPr><w:shd w:fill="0a0a5a"/></w:tcPr><w:p/></w:tc>', height=600),
                    self._row_table('<w:tc><w:tcPr><w:shd w:fill="0a0a5a"/></w:tcPr><w:p/></w:tc>', height=20)):
            final.addprevious(parse_xml(xml))
        doc.save(str(self.docx))
        recolor_cells_from_pdf(self.docx, self.pdf)
        self.assertEqual(_fills(self.docx), [
            "0A0A5A", "0A0A5A",  # 标题和同一条上空着的格子
            "E0EAFF",  # 找到位置的正文
            "E0EAFF", None, None,  # 套着真值表的外层格子；里面的格子本来就没底色
            "E0EAFF",  # 只有一个「T」的格子
            "E0EAFF",  # 没字的一大块（框跨页留下的空白）
            "0A0A5A",  # 没字的一条细线：不动
        ])

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
