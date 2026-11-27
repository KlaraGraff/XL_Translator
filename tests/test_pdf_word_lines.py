"""PDF 转 Word 后，交错的几列字按原文一行一行排回来（反馈 #28 讲义第 33 页证明表）。

pdf2docx 把同一缩进的几行拼成一段、靠大行距和折行摆位置；不同缩进的行交错时排不出来，
只转换时步骤被格子切掉，翻译后散到整页、顺序全乱。
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

from core.pdf_word_lines import restore_line_order

_STEPS = [(100, 100, "1. Alpha one"), (120, 118, "2. Beta two"), (100, 136, "3. Gamma three")]


def _cell_paragraph(text: str, left: int) -> str:
    return (f'<w:p><w:pPr><w:spacing w:before="0" w:after="0" w:line="720" w:lineRule="exact"/>'
            f'<w:ind w:left="{left}" w:right="1800"/></w:pPr>'
            f'<w:r><w:rPr><w:sz w:val="22"/></w:rPr><w:t xml:space="preserve">{text}</w:t></w:r></w:p>')


@unittest.skipIf(pymupdf is None, "PyMuPDF not installed")
class RestoreLineOrderTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.pdf = Path(tmp.name) / "in.pdf"
        self.docx = Path(tmp.name) / "out.docx"

    def _make(self, cell: str) -> None:
        pdf = pymupdf.open()
        page = pdf.new_page(width=595, height=842)
        for x, y, text in _STEPS:
            page.insert_text((x, y), text, fontname="helv", fontsize=11)
        pdf.save(str(self.pdf))
        doc = Document()
        doc.element.body[-1].addprevious(parse_xml(
            f'<w:tbl {nsdecls("w")}><w:tblGrid><w:gridCol w:w="6000"/></w:tblGrid><w:tr>'
            f'<w:trPr><w:trHeight w:val="1400" w:hRule="exact"/></w:trPr><w:tc>{cell}</w:tc></w:tr></w:tbl>'))
        doc.save(str(self.docx))

    def _cell(self):
        cell = Document(str(self.docx)).tables[0].cell(0, 0)
        return [(p.text, p._p.find(f"{qn('w:pPr')}/{qn('w:ind')}").get(qn("w:left"))) for p in cell.paragraphs]

    def test_interleaved_steps_are_put_back_in_order(self) -> None:
        # 第 1、3 步同一缩进被拼成一段（靠折行摆到第 3 步的高度），第 2 步缩进一层自成一段
        self._make(_cell_paragraph("1. Alpha one 3. Gamma three", 0) + _cell_paragraph("2. Beta two", 400))
        self.assertEqual(restore_line_order(self.docx, self.pdf), 1)
        cell = self._cell()
        self.assertEqual([text.strip() for text, _left in cell], ["1. Alpha one", "2. Beta two", "3. Gamma three"])
        self.assertEqual([left for _text, left in cell], ["0", "400", "0"])

    def test_lines_already_in_order_are_left_alone(self) -> None:
        self._make(_cell_paragraph("1. Alpha one", 0) + _cell_paragraph("2. Beta two", 400)
                   + _cell_paragraph("3. Gamma three", 0))
        self.assertEqual(restore_line_order(self.docx, self.pdf), 0)


if __name__ == "__main__":
    unittest.main()
