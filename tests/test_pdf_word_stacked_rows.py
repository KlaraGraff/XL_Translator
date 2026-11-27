"""框里一行一张表、行高从框顶一路累加的，改回每行自己的高度（反馈 #28 讲义第 68 页）。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from docx import Document
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls, qn

from core.pdf_word_stacked_rows import unstack_rows


def _table(height: int, before: int, text: str, width: int = 9000) -> str:
    return (f'<w:tbl {nsdecls("w")}><w:tblPr><w:tblInd w:w="300" w:type="dxa"/></w:tblPr>'
            f'<w:tblGrid><w:gridCol w:w="{width}"/></w:tblGrid>'
            f'<w:tr><w:trPr><w:trHeight w:val="{height}" w:hRule="exact"/></w:trPr><w:tc><w:p>'
            f'<w:pPr><w:spacing w:before="{before}" w:after="0"/></w:pPr><w:r><w:t>{text}</w:t></w:r>'
            f'</w:p></w:tc></w:tr></w:tbl>')


_SPACER = f'<w:p {nsdecls("w")}><w:pPr><w:spacing w:before="0" w:after="0" w:line="20" w:lineRule="exact"/></w:pPr></w:p>'


class UnstackRowsTests(unittest.TestCase):
    def _run(self, *elements: str) -> list[tuple[int, int]]:
        doc = Document()
        body = doc.element.body
        for element in elements:
            body[-1].addprevious(parse_xml(element))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "converted.docx"
        doc.save(str(path))
        self.fixed = unstack_rows(path)
        rows = []
        for table in Document(str(path)).element.body.iter(qn("w:tbl")):
            height = table.find(f"{qn('w:tr')}/{qn('w:trPr')}/{qn('w:trHeight')}").get(qn("w:val"))
            before = next(table.iter(qn("w:spacing"))).get(qn("w:before"))
            rows.append((int(height), int(before)))
        return rows

    def test_rows_measured_from_the_box_top_get_their_own_height(self):
        # 讲义第 68 页例题框：432、922、1412 是从框顶量的，段前距 154、644、1134 也是
        rows = self._run(_table(432, 154, "In Figure 31"), _SPACER, _table(922, 644, "{(x, x), (x, z)}"),
                         _SPACER, _table(1412, 1134, "This is transitive"), _SPACER,
                         _table(1684, 1406, "a few cases"))
        self.assertEqual(rows, [(432, 154), (490, 212), (490, 212), (272, 0)])
        self.assertEqual(self.fixed, 3)

    def test_rows_with_their_own_heights_are_left_alone(self):
        # 讲义第 67 页注记框：每行行高本来就是自己的
        rows = self._run(_table(400, 154, "Remark"), _SPACER, _table(270, 26, "two-way arrow"),
                         _SPACER, _table(270, 26, "such a two-way arrow"))
        self.assertEqual(rows, [(400, 154), (270, 26), (270, 26)])
        self.assertEqual(self.fixed, 0)

    def test_taller_row_with_text_inside_the_previous_height_is_left_alone(self):
        # 后一行高一些，但字在前一行的高度以内：不是从同一个顶上量的
        rows = self._run(_table(300, 50, "short row"), _SPACER, _table(700, 60, "a taller row"))
        self.assertEqual(rows, [(300, 50), (700, 60)])

    def test_rows_of_different_boxes_are_left_alone(self):
        wider = self._run(_table(432, 154, "one box"), _SPACER, _table(922, 644, "another box", width=6000))
        self.assertEqual(wider, [(432, 154), (922, 644)])
        text_between = self._run(_table(432, 154, "one box"),
                                 f'<w:p {nsdecls("w")}><w:r><w:t>Body text between</w:t></w:r></w:p>',
                                 _table(922, 644, "another box"))
        self.assertEqual(text_between, [(432, 154), (922, 644)])


if __name__ == "__main__":
    unittest.main()
