"""PDF 转 Word 后，格子里挤着几行几列字的表格按原文的行列拆开（反馈 #28 讲义第 26 页真值表）。

pdf2docx 按画出来的线认表格，真值表只认出 2 列 2 行：一格里装着两列四行的 T、F，T/F 挤在一起、列对不上。
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

from core.pdf_word_grid import refine_tables_from_pdf

# 原文：3 列 5 行，A、B 两列之间没有竖线，B 和 A&B 之间有一条
_ROWS = [("A", "B", "AB"), ("T", "T", "T"), ("T", "F", "F"), ("F", "T", "F"), ("F", "F", "F")]
_X = (200, 220, 252)


def _paragraph(*parts: str) -> str:
    """一段：几截字之间放制表符（pdf2docx 靠制表位把同一行的两列摆开）。"""
    runs = '<w:r><w:tab/></w:r>'.join(f'<w:r><w:t xml:space="preserve">{part}</w:t></w:r>' for part in parts)
    return f'<w:p><w:pPr><w:spacing w:line="220" w:lineRule="exact"/><w:ind w:left="100"/></w:pPr>{runs}</w:p>'


def _cell(width: int, paragraphs: str) -> str:
    return f'<w:tc><w:tcPr><w:tcW w:w="{width}" w:type="dxa"/></w:tcPr>{paragraphs}</w:tc>'


def _table(rows: list[str], widths: tuple[int, ...]) -> str:
    grid = "".join(f'<w:gridCol w:w="{width}"/>' for width in widths)
    return (f'<w:tbl {nsdecls("w")}><w:tblPr><w:tblW w:w="{sum(widths)}" w:type="dxa"/></w:tblPr>'
            f'<w:tblGrid>{grid}</w:tblGrid>{"".join(f"<w:tr>{row}</w:tr>" for row in rows)}</w:tbl>')


def _truth_table() -> str:
    """pdf2docx 认出来的样子：表头一行，下面一行装着 4 行 T、F；左格每行一段，右格 4 行并成一段。"""
    head = _cell(800, _paragraph("A", "B")) + _cell(700, _paragraph("AB"))
    body = (_cell(800, "".join(_paragraph(a, b) for a, b, _c in _ROWS[1:]))
            + _cell(700, _paragraph(" ".join(c for _a, _b, c in _ROWS[1:]))))
    return _table([head, body], (800, 700))


def _texts(table) -> list[list[str]]:
    return [[cell.text.strip() for cell in row.cells] for row in table.rows]


@unittest.skipIf(pymupdf is None, "PyMuPDF not installed")
class RefineTablesTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.pdf = Path(tmp.name) / "in.pdf"
        self.docx = Path(tmp.name) / "out.docx"

    def _make(self, words: list[tuple[float, float, str]], body: list[str]) -> None:
        pdf = pymupdf.open()
        page = pdf.new_page(width=612, height=792)
        for x, y, text in words:
            page.insert_text((x, y), text, fontname="helv", fontsize=11)
        pdf.save(str(self.pdf))
        doc = Document()
        for element in body:
            doc.element.body[-1].addprevious(parse_xml(element))
        doc.save(str(self.docx))

    @staticmethod
    def _text(text: str) -> str:
        return f'<w:p {nsdecls("w")}><w:r><w:t>{text}</w:t></w:r></w:p>'

    def _truth_words(self) -> list[tuple[float, float, str]]:
        return [(x, 100 + 14 * row, text) for row, texts in enumerate(_ROWS) for x, text in zip(_X, texts)]

    def test_truth_table_is_split_into_the_original_rows_and_columns(self) -> None:
        self._make([(90, 80, "Before the table."), *self._truth_words(), (90, 190, "After the table.")],
                   [self._text("Before the table."), _truth_table(), self._text("After the table.")])
        self.assertEqual(refine_tables_from_pdf(self.docx, self.pdf), 1)
        table = Document(str(self.docx)).tables[0]
        self.assertEqual(_texts(table), [list(row) for row in _ROWS])
        # 两列拆开的那一格按原文空白的中线分宽，总宽不变
        widths = [int(width) for width in (col.get(qn("w:w")) for col in table._tbl.tblGrid.findall(qn("w:gridCol")))]
        self.assertEqual(len(widths), 3)
        self.assertEqual(widths[0] + widths[1], 800)
        self.assertEqual(widths[2], 700)

    def test_table_at_the_bottom_of_the_page_above_the_page_number(self) -> None:
        # 表格后面没有正文，只有页码：范围敞着的那一头把页码那一行去掉再对
        self._make([(90, 80, "Before the table."), *self._truth_words(), (300, 300, "17")],
                   [self._text("Before the table."), _truth_table()])
        self.assertEqual(refine_tables_from_pdf(self.docx, self.pdf), 1)
        self.assertEqual(_texts(Document(str(self.docx)).tables[0]), [list(row) for row in _ROWS])

    def test_figure_labels_at_different_heights_are_left_alone(self) -> None:
        # 两张并排的图上的标注（讲义第 48 页图 15）：每格一行字，「(i)」「(ii)」靠段前距摆在图下面，
        # 原文看着是两行，但没有哪一格里有两行字，不是表格的行
        texts = ("A", "(i)", "B", "A", "(ii)", "B")
        labels = _table(["".join(_cell(600, _paragraph(text)) for text in texts)], (600,) * 6)
        words = [(200 + 40 * number, 130 if text.startswith("(") else 100, text) for number, text in enumerate(texts)]
        self._make([(90, 80, "Before the figure."), *words, (90, 160, "After the figure.")],
                   [self._text("Before the figure."), labels, self._text("After the figure.")])
        self.assertEqual(refine_tables_from_pdf(self.docx, self.pdf), 0)

    def test_table_with_a_picture_is_left_alone(self) -> None:
        table = _truth_table().replace('<w:t xml:space="preserve">AB</w:t>',
                                       '<w:t xml:space="preserve">AB</w:t><w:pict/>')
        self._make([(90, 80, "Before the table."), *self._truth_words(), (90, 190, "After the table.")],
                   [self._text("Before the table."), table, self._text("After the table.")])
        self.assertEqual(refine_tables_from_pdf(self.docx, self.pdf), 0)


if __name__ == "__main__":
    unittest.main()
