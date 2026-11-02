"""PDF 转 Word 后左右分栏改成无边框表格（本机实测：签证审核清单每页顶上的两个框）。

pdf2docx 用「两栏连续分节 + 从下一栏开始的分节」摆左右并排的两块，WPS 把它们上下
叠起来，每页多占好几行。改成一行两格的表格；每页开头的「新页」分节不能丢。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from docx import Document
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls, qn

from core.pdf_word_columns import columns_to_tables
from core.pdf_word_fit import page_groups

_COLS = '<w:cols w:num="2" w:equalWidth="0"><w:col w:w="4500" w:space="0"/><w:col w:w="5200" w:space="0"/></w:cols>'


def _sect_paragraph(kind: str | None, *, cols: bool, text: str = "") -> str:
    type_xml = f'<w:type w:val="{kind}"/>' if kind else ""
    run = f"<w:r><w:t>{text}</w:t></w:r>" if text else ""
    return (f'<w:p {nsdecls("w")}><w:pPr><w:sectPr>{type_xml}{_COLS if cols else ""}'
            f'</w:sectPr></w:pPr>{run}</w:p>')


def _paragraph(text: str) -> str:
    return f'<w:p {nsdecls("w")}><w:r><w:t>{text}</w:t></w:r></w:p>'


def _box(text: str) -> str:
    return (f'<w:tbl {nsdecls("w")}><w:tblPr><w:tblW w:w="4000" w:type="dxa"/></w:tblPr>'
            f'<w:tblGrid><w:gridCol w:w="4000"/></w:tblGrid><w:tr><w:tc><w:p><w:r><w:t>{text}</w:t>'
            '</w:r></w:p></w:tc></w:tr></w:tbl>')


def _text(element) -> str:
    return "".join(node.text or "" for node in element.iter(qn("w:t")))


class ColumnsToTablesTests(unittest.TestCase):
    def _page(self, final, *, left: list[str], right: list[str], body_text: str) -> None:
        final.addprevious(parse_xml(_sect_paragraph(None, cols=False)))  # 每页开头的新页分节
        for xml in left:
            final.addprevious(parse_xml(xml))
        final.addprevious(parse_xml(_sect_paragraph("continuous", cols=True)))
        for xml in right:
            final.addprevious(parse_xml(xml))
        final.addprevious(parse_xml(_sect_paragraph("nextColumn", cols=True, text="Applicant’s E-Mail")))
        final.addprevious(parse_xml(_paragraph(body_text)))

    def _doc(self) -> Path:
        doc = Document()
        final = doc.element.body[-1]
        final.append(parse_xml(f'<w:type {nsdecls("w")} w:val="continuous"/>'))
        self._page(final, left=[_box("APPLICANT'S NAME"), _paragraph("Application QC")],
                   right=[_box("PASSPORT NUMBER")], body_text="SPECIAL REQUIREMENTS")
        final.addprevious(parse_xml(_sect_paragraph("continuous", cols=False)))
        self._page(final, left=[], right=[_box("PASSPORT NUMBER")], body_text="34 Copy of parents' passports")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "converted.docx"
        doc.save(str(path))
        return path

    def test_columns_become_a_two_cell_table(self):
        path = self._doc()
        self.assertEqual(columns_to_tables(path), 2)
        body = Document(str(path)).element.body
        layout = body.findall(qn("w:tbl"))
        self.assertEqual(len(layout), 2)
        first = layout[0]
        cells = first.findall(f"{qn('w:tr')}/{qn('w:tc')}")
        self.assertEqual(len(cells), 2)
        self.assertEqual([col.get(qn("w:w")) for col in first.iter(qn("w:gridCol"))][:2], ["4500", "5200"])
        left = _text(cells[0])
        right = _text(cells[1])
        self.assertIn("APPLICANT'S NAME", left)
        self.assertIn("Application QC", left)
        self.assertIn("PASSPORT NUMBER", right)
        # 带分节符那段的文字也搬进了右格，分节符留在正文里
        self.assertIn("Applicant’s E-Mail", right)
        self.assertEqual(cells[1][-1].tag, qn("w:p"))  # 格子以段落结尾
        # 表格在正文之前
        texts = [_text(child) for child in body]
        self.assertLess(texts.index(next(t for t in texts if "PASSPORT NUMBER" in t)),
                        texts.index("SPECIAL REQUIREMENTS"))
        # 不再有两栏、也不再有「下一栏」
        self.assertFalse([cols for cols in body.iter(qn("w:cols")) if cols.get(qn("w:num"))])
        self.assertFalse([kind for kind in body.iter(qn("w:type")) if kind.get(qn("w:val")) == "nextColumn"])

    def test_page_sections_survive(self):
        path = self._doc()
        before = len(page_groups(Document(str(path)).element.body))
        columns_to_tables(path)
        self.assertEqual(len(page_groups(Document(str(path)).element.body)), before)

    def test_empty_left_column_keeps_its_width(self):
        path = self._doc()
        columns_to_tables(path)
        second = Document(str(path)).element.body.findall(qn("w:tbl"))[1]
        cells = second.findall(f"{qn('w:tr')}/{qn('w:tc')}")
        self.assertEqual(cells[0].find(f"{qn('w:tcPr')}/{qn('w:tcW')}").get(qn("w:w")), "4500")
        self.assertEqual(_text(cells[0]).strip(), "")
        self.assertIn("PASSPORT NUMBER", _text(cells[1]))

    def test_single_column_document_untouched(self):
        doc = Document()
        doc.add_paragraph("plain text")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "plain.docx"
        doc.save(str(path))
        before = path.read_bytes()
        self.assertEqual(columns_to_tables(path), 0)
        self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
