"""PDF 转 Word 后表格的列宽定义（本机实测，一份 3 页的中英审核清单）。

pdf2docx 给每个格子写了宽度，表格的列宽定义却是四列等宽。Microsoft Word 打开正常，
WPS 打开时正文列只有四分之一宽，字竖着排。转换后按格子宽度把列宽定义改对。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from docx.shared import Twips

from core.pdf_word_route import sync_table_grids


def _grid(path: Path, index: int = 0) -> list[str]:
    tbl = Document(str(path)).tables[index]._tbl
    return [col.get(qn("w:w")) for col in tbl.findall(f"{qn('w:tblGrid')}/{qn('w:gridCol')}")]


class TableGridTests(unittest.TestCase):
    def _save(self, doc) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "converted.docx"
        doc.save(str(path))
        return path

    def _checklist(self, *, merged_header: bool = False):
        """和 pdf2docx 的写法一样：add_table 得到等宽的列宽定义，再逐格设宽度。"""
        doc = Document()
        table = doc.add_table(rows=3, cols=4)
        widths = (462, 8036, 566, 568)
        for row in table.rows:
            for cell, width in zip(row.cells, widths):
                cell.width = Twips(width)
        if merged_header:
            # 表头「MANDATORY REQUIREMENTS」占前两列，后两列是「是」「否」
            merged = table.cell(0, 0).merge(table.cell(0, 1))
            merged.width = Twips(462 + 8036)
            for row in table.rows[1:]:
                row.cells[0].width = Twips(462)
        return doc

    def test_grid_follows_cell_widths(self):
        path = self._save(self._checklist())
        self.assertEqual(len(set(_grid(path))), 1)  # 改之前：四列等宽
        self.assertEqual(sync_table_grids(path), 1)
        self.assertEqual(_grid(path), ["462", "8036", "566", "568"])
        self.assertEqual(sync_table_grids(path), 0)  # 已经对了就不再改

    def test_merged_header_row_still_resolves(self):
        path = self._save(self._checklist(merged_header=True))
        self.assertEqual(sync_table_grids(path), 1)
        self.assertEqual(_grid(path), ["462", "8036", "566", "568"])

    def test_span_fills_the_one_missing_column(self):
        doc = Document()
        table = doc.add_table(rows=2, cols=3)
        merged = table.cell(0, 0).merge(table.cell(0, 1))
        merged.width = Twips(5000)
        table.cell(0, 2).width = Twips(1000)
        table.cell(1, 0).width = Twips(1200)
        # 第二行后两格不写宽度：第 2 列只能由跨列格子减出来
        for cell in table.rows[1].cells[1:]:
            tc_w = cell._tc.tcPr.find(qn("w:tcW")) if cell._tc.tcPr is not None else None
            if tc_w is not None:
                cell._tc.tcPr.remove(tc_w)
        path = self._save(doc)
        self.assertEqual(sync_table_grids(path), 1)
        self.assertEqual(_grid(path), ["1200", "3800", "1000"])

    def test_columns_only_inside_spans_split_evenly(self):
        doc = Document()
        table = doc.add_table(rows=2, cols=4)
        top = table.cell(0, 0).merge(table.cell(0, 1))
        top.width = Twips(3000)
        table.cell(0, 2).width = Twips(1000)
        table.cell(0, 3).width = Twips(1000)
        bottom = table.cell(1, 0).merge(table.cell(1, 1))
        bottom.width = Twips(3000)
        table.cell(1, 2).width = Twips(1000)
        table.cell(1, 3).width = Twips(1000)
        path = self._save(doc)
        self.assertEqual(sync_table_grids(path), 1)
        self.assertEqual(_grid(path), ["1500", "1500", "1000", "1000"])

    def test_unknown_widths_leave_grid_alone(self):
        doc = Document()
        doc.add_table(rows=2, cols=3)  # 格子都没写宽度
        path = self._save(doc)
        before = _grid(path)
        self.assertEqual(sync_table_grids(path), 0)
        self.assertEqual(_grid(path), before)


if __name__ == "__main__":
    unittest.main()
