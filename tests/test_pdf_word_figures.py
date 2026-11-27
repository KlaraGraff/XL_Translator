"""PDF 转 Word 后，摆在图上的标签表格不再用底色把图盖住（反馈 #28 讲义图 13：维恩图的两个圆
被灰格子整个盖住，只剩一块灰色；图 14、15 标签下面一小块白底）。"""
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

from core.pdf_word_figures import uncover_pictures

_WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"


def _cell(text: str, fill: str) -> str:
    return (f'<w:tc><w:tcPr><w:tcW w:w="1000" w:type="dxa"/><w:shd w:val="clear" w:color="auto" w:fill="{fill}"/>'
            f'</w:tcPr><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:tc>')


@unittest.skipIf(pymupdf is None, "PyMuPDF not installed")
class UncoverPicturesTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.pdf = Path(tmp.name) / "in.pdf"
        self.docx = Path(tmp.name) / "out.docx"
        pdf = pymupdf.open()
        page = pdf.new_page(width=595, height=842)
        page.insert_text((210, 260), "A", fontname="helv", fontsize=10)
        page.insert_text((300, 260), "B", fontname="helv", fontsize=10)
        page.insert_text((70, 500), "Name Value", fontname="helv", fontsize=10)
        pdf.save(str(self.pdf))
        doc = Document()
        body = doc.element.body
        # 图片：离页顶 200 磅、离左边 180 磅，宽 200、高 120 磅，在文字下方
        body[-1].addprevious(parse_xml(
            f'<w:p {nsdecls("w")} xmlns:wp="{_WP}"><w:r><w:drawing><wp:anchor behindDoc="1">'
            f'<wp:positionH relativeFrom="page"><wp:posOffset>{180 * 12700}</wp:posOffset></wp:positionH>'
            f'<wp:positionV relativeFrom="page"><wp:posOffset>{200 * 12700}</wp:posOffset></wp:positionV>'
            f'<wp:extent cx="{200 * 12700}" cy="{120 * 12700}"/></wp:anchor></w:drawing></w:r></w:p>'))
        for cells in ((_cell("A", "BFBFBF") + _cell("", "BFBFBF") + _cell("B", "FFFFFF")),
                      (_cell("Name", "DDDDDD") + _cell("A", "DDDDDD"))):
            body[-1].addprevious(parse_xml(
                f'<w:tbl {nsdecls("w")}><w:tblGrid/><w:tr>{cells}</w:tr></w:tbl>'))
        doc.save(str(self.docx))

    def test_label_table_on_a_picture_loses_its_fills(self) -> None:
        self.assertEqual(uncover_pictures(self.docx, self.pdf), 3)
        tables = Document(str(self.docx)).tables
        self.assertEqual(len(list(tables[0]._tbl.iter(qn("w:shd")))), 0, "图上的标签表：灰格子、白格子都去掉底色")
        self.assertEqual(len(list(tables[1]._tbl.iter(qn("w:shd")))), 2, "有一格的字不在图里：正文表格不动")

    def _page(self, texts, tables) -> None:
        """一页 PDF（texts：[(x, y, 字)]）和一份 Word：图片同上，后面跟着这几张表。"""
        pdf = pymupdf.open()
        page = pdf.new_page(width=595, height=842)
        for x, y, text in texts:
            page.insert_text((x, y), text, fontname="helv", fontsize=10)
        pdf.save(str(self.pdf))
        doc = Document()
        body = doc.element.body
        body[-1].addprevious(parse_xml(
            f'<w:p {nsdecls("w")} xmlns:wp="{_WP}"><w:r><w:drawing><wp:anchor behindDoc="1">'
            f'<wp:positionH relativeFrom="page"><wp:posOffset>{180 * 12700}</wp:posOffset></wp:positionH>'
            f'<wp:positionV relativeFrom="page"><wp:posOffset>{200 * 12700}</wp:posOffset></wp:positionV>'
            f'<wp:extent cx="{200 * 12700}" cy="{120 * 12700}"/></wp:anchor></w:drawing></w:r></w:p>'))
        for rows in tables:
            body[-1].addprevious(parse_xml(f'<w:tbl {nsdecls("w")}><w:tblGrid/>{rows}</w:tbl>'))
        doc.save(str(self.docx))

    def test_label_rows_lose_fills_but_the_caption_row_keeps_them(self) -> None:
        # 讲义图 15：标签和图注拼在同一张表里；标签那几行在图上，图注那一行不在
        self._page([(210, 230, "A"), (300, 230, "B"), (210, 290, "A"), (300, 290, "B"), (210, 400, "Name")],
                   [(f"<w:tr>{_cell('A', 'FFFFFF')}{_cell('B', 'FFFFFF')}</w:tr>"
                     f"<w:tr>{_cell('', 'BFBFBF')}{_cell('', 'BFBFBF')}</w:tr>"
                     f"<w:tr>{_cell('A', 'FFFFFF')}{_cell('B', 'FFFFFF')}</w:tr>"
                     f"<w:tr>{_cell('Name', 'DDDDDD')}{_cell('', 'DDDDDD')}</w:tr>")])
        uncover_pictures(self.docx, self.pdf)
        table = Document(str(self.docx)).tables[0]._tbl
        left = [len(list(row.iter(qn("w:shd")))) for row in table.findall(qn("w:tr"))]
        self.assertEqual(left, [0, 0, 0, 2], "标签行和夹在中间的空行去掉底色，图注那一行不动")

    def test_labels_of_a_figure_without_a_picture_keep_the_fill(self) -> None:
        # 讲义图 13：Word 里没有图片，灰底就是格子的底色；它的标签 A、B 在别的图（有图片）上也有
        self._page([(210, 260, "A"), (300, 260, "B"), (210, 600, "A"), (300, 600, "B")],
                   [f"<w:tr>{_cell('A', 'FFFFFF')}{_cell('B', 'FFFFFF')}</w:tr>",
                    f"<w:tr>{_cell('A', 'A9A9A9')}{_cell('B', 'A9A9A9')}</w:tr>"])
        uncover_pictures(self.docx, self.pdf)
        tables = Document(str(self.docx)).tables
        self.assertEqual(len(list(tables[0]._tbl.iter(qn("w:shd")))), 0, "压在图片上的标签去底色")
        self.assertEqual(len(list(tables[1]._tbl.iter(qn("w:shd")))), 2, "没压在图片上的不动")

if __name__ == "__main__":
    unittest.main()
