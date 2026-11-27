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

from core.pdf_word_lines import _fits, _Line, _prune, restore_line_order

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


_COLUMN = [(100, 100, "1. Alpha one"), (100, 118, "7 x"), (120, 136, "2. Beta"), (120, 154, "3. Gamma three")]
_PROSE = ("The quick brown fox jumps over the lazy dog and keeps", "running far away from the old farm while the tired",
          "extraordinarily sleepy farmer rests.")


@unittest.skipIf(pymupdf is None, "PyMuPDF not installed")
class HardBreakTests(unittest.TestCase):
    """pdf2docx 把一行一行摆的字（证明的步骤、一列依据）拼成一段，行间是换行符、靠右缩进逼它折行。"""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.pdf = Path(tmp.name) / "in.pdf"
        self.docx = Path(tmp.name) / "out.docx"

    def _make(self, lines, cell: str) -> None:
        pdf = pymupdf.open()
        page = pdf.new_page(width=595, height=842)
        for x, y, text in lines:
            page.insert_text((x, y), text, fontname="helv", fontsize=11)
        pdf.save(str(self.pdf))
        doc = Document()
        doc.element.body[-1].addprevious(parse_xml(
            f'<w:tbl {nsdecls("w")}><w:tblGrid><w:gridCol w:w="8000"/></w:tblGrid><w:tr>'
            f'<w:trPr><w:trHeight w:val="1400" w:hRule="exact"/></w:trPr><w:tc>{cell}</w:tc></w:tr></w:tbl>'))
        doc.save(str(self.docx))

    @staticmethod
    def _runs(*lines: str) -> str:
        # 每个词一个 run（上下标、字体不同时 pdf2docx 就是这样切的）；行与行之间一个只装换行符的 run，
        # 下一行开头再用一个制表符缩进（讲义里的代码就是这样）
        runs = []
        for number, line in enumerate(lines):
            if number:
                runs += ["<w:r><w:br/></w:r>", "<w:r><w:tab/></w:r>"]
            runs += [f'<w:r><w:t xml:space="preserve">{word} </w:t></w:r>' for word in line.split()]
        return ('<w:p><w:pPr><w:spacing w:before="0" w:after="0" w:line="360" w:lineRule="exact"/>'
                f'<w:ind w:left="0" w:right="3000"/></w:pPr>{"".join(runs)}</w:p>')

    def test_lines_in_one_paragraph_become_one_paragraph_each(self) -> None:
        self._make(_COLUMN, self._runs(*(text for _x, _y, text in _COLUMN)))
        self.assertEqual(restore_line_order(self.docx, self.pdf), 1)
        cell = Document(str(self.docx)).tables[0].cell(0, 0)
        # 「7 x」只有两个字：整页对齐时太短对不上，按「PDF 剩下的字里只出现一次」补上
        self.assertEqual([p.text.strip() for p in cell.paragraphs], ["1. Alpha one", "7 x", "2. Beta", "3. Gamma three"])
        self.assertEqual([p._p.find(f"{qn('w:pPr')}/{qn('w:ind')}").get(qn("w:left")) for p in cell.paragraphs],
                         ["0", "0", "400", "400"])
        for tag in ("w:br", "w:tab"):
            self.assertEqual(sum(len(p._p.findall(f".//{qn(tag)}")) for p in cell.paragraphs), 0,
                             "行间的换行符、缩进用的制表符去掉")

    def test_column_beside_the_steps_is_split_row_by_row(self) -> None:
        # 证明右边那一列依据：「Premise」后面放不下下一个「Premise」，单看像折行，可每一行都对着左边的一步
        steps = [(100, 100, "1. A"), (120, 118, "2. Beta gamma delta"), (100, 136, "3. C")]
        reasons = [(300, 100, "Premise"), (300, 118, "Premise"), (300, 136, "Assumption")]
        pdf = pymupdf.open()
        page = pdf.new_page(width=595, height=842)
        for x, y, text in steps + reasons:
            page.insert_text((x, y), text, fontname="helv", fontsize=11)
        pdf.save(str(self.pdf))
        doc = Document()
        doc.element.body[-1].addprevious(parse_xml(
            f'<w:tbl {nsdecls("w")}><w:tblGrid><w:gridCol w:w="4000"/><w:gridCol w:w="4000"/></w:tblGrid><w:tr>'
            f'<w:tc>{self._runs(*(text for _x, _y, text in steps))}</w:tc>'
            f'<w:tc>{self._runs(*(text for _x, _y, text in reasons))}</w:tc></w:tr></w:tbl>'))
        doc.save(str(self.docx))
        self.assertEqual(restore_line_order(self.docx, self.pdf), 2)
        table = Document(str(self.docx)).tables[0]
        self.assertEqual([p.text.strip() for p in table.cell(0, 1).paragraphs], ["Premise", "Premise", "Assumption"])
        befores = [[p._p.find(f"{qn('w:pPr')}/{qn('w:spacing')}").get(qn("w:before")) for p in table.cell(0, n).paragraphs]
                   for n in (0, 1)]
        self.assertEqual(befores[0][0], befores[1][0], "同一行的两格从同一个上沿量起，左右对齐")

    def test_justified_prose_beside_code_lines_keeps_its_wraps(self) -> None:
        # 一格里先是两端对齐的一段话（每行排满），下面是一行一行的代码：只拆代码，话不拆成半句
        prose = [(100, 100, "The quick brown fox jumps over"), (100, 114, "the lazy dog.")]
        code = [(100, 140, "x = 1"), (120, 154, "y = 2")]
        self._make(prose + code, self._runs(*(text for _x, _y, text in prose))
                   + self._runs(*(text for _x, _y, text in code)))
        self.assertEqual(restore_line_order(self.docx, self.pdf), 1)
        cell = Document(str(self.docx)).tables[0].cell(0, 0)
        self.assertEqual([" ".join(p.text.split()) for p in cell.paragraphs],
                         ["The quick brown fox jumps over the lazy dog.", "x = 1", "y = 2"])

    def test_wrapped_prose_is_left_alone(self) -> None:
        # 自动折行：上一行后面放不下下一行的第一个词
        lines = [(100, 100 + 14 * number, text) for number, text in enumerate(_PROSE)]
        self._make(lines, self._runs(*_PROSE))
        self.assertEqual(restore_line_order(self.docx, self.pdf), 0)


class AlignmentTests(unittest.TestCase):
    def test_a_match_spilling_into_the_next_paragraph_is_dropped(self) -> None:
        # 讲义第 33 页：Word 里「10. P₂」后面一段是「13. …」，PDF 上「10. P₂」后面紧跟「11.」，
        # 先后顺序对齐时把「13.」的「1」连带对到了「11.」的「1」上，两段各有一个字落到别的行，整格放弃重排
        word = "10.P2" + "13.(¬P1)"
        mapping = {index: index for index in range(6)}  # 「10.P2」+ 下一段的「1」，一口气对上
        mapping.update({20 + offset: 6 + offset for offset in range(7)})  # 「3.(¬P1)」对在别处
        pruned = _prune(mapping, [0, 5], len(word))
        self.assertNotIn(5, pruned, "跨到下一段的那一个字去掉")
        self.assertEqual({key: value for key, value in pruned.items() if key != 5}, {
            key: value for key, value in mapping.items() if key != 5}, "别的都留着")

    def test_a_short_paragraph_matched_with_its_neighbour_stays(self) -> None:
        mapping = {index: index for index in range(9)}  # 「Premise」「x2」两段连着对上，第二段只有两个字
        self.assertEqual(_prune(mapping, [0, 7], 9), mapping)

class FitsTests(unittest.TestCase):
    """讲义第 26 页真值表：一格里只有 T、F，整页到处都是同样的字，对齐对到了别的列、别的表上，
    这一格被当成一行一行摆的重排，打乱了。对出来的行放不进格子，就不信。"""

    @staticmethod
    def _cell(width: int, height: int):
        return parse_xml(
            f'<w:tr {nsdecls("w")}><w:trPr><w:trHeight w:val="{height}" w:hRule="exact"/></w:trPr>'
            f'<w:tc><w:tcPr><w:tcW w:w="{width}" w:type="dxa"/></w:tcPr><w:p/></w:tc></w:tr>').find(qn("w:tc"))

    @staticmethod
    def _located(*boxes):
        return [(None, None, 0, [(number, _Line(box)) for number, box in enumerate(boxes)])]

    def test_lines_inside_the_cell_fit(self) -> None:
        tc = self._cell(600, 600)  # 30 × 30 磅
        self.assertTrue(_fits(tc, self._located((100, 100, 106, 110), (100, 114, 106, 124))))

    def test_lines_from_other_columns_or_tables_do_not_fit(self) -> None:
        tc = self._cell(600, 600)
        self.assertFalse(_fits(tc, self._located((100, 100, 106, 110), (220, 114, 226, 124))), "左右散到别的列")
        self.assertFalse(_fits(tc, self._located((100, 100, 106, 110), (100, 240, 106, 250))), "上下跑到别的表")


if __name__ == "__main__":
    unittest.main()
