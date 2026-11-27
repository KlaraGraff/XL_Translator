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


if __name__ == "__main__":
    unittest.main()
