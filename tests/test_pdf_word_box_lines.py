"""框里一行一张表的，同一段的几行并进同一格；原文排满的行不再标成居中（反馈 #28 讲义第 59、60、66 页）。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from docx import Document
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls, qn
from docx.text.paragraph import Paragraph

from core.pdf_word_box_lines import join_box_rows
from core.pdf_word_full_lines import full_lines
from core.pdf_word_route import merge_wrapped_lines, paragraph_continues


def _row(text: str, *, fill: str = "F2F2FF", jc: str = "center", height: int = 270, width: int = 9582) -> str:
    return (f'<w:tbl {nsdecls("w")}><w:tblPr><w:tblInd w:w="0" w:type="dxa"/></w:tblPr>'
            f'<w:tblGrid><w:gridCol w:w="{width}"/></w:tblGrid><w:tr><w:trPr><w:trHeight w:val="{height}"/></w:trPr>'
            f'<w:tc><w:tcPr><w:shd w:val="clear" w:color="auto" w:fill="{fill}"/></w:tcPr><w:p><w:pPr>'
            f'<w:spacing w:before="26" w:after="0"/><w:jc w:val="{jc}"/></w:pPr>'
            f'<w:r><w:rPr><w:sz w:val="22"/></w:rPr><w:t xml:space="preserve">{text}</w:t></w:r></w:p></w:tc></w:tr></w:tbl>')


_SPACER = f'<w:p {nsdecls("w")}><w:pPr><w:spacing w:before="0" w:after="0" w:line="20" w:lineRule="exact"/></w:pPr></w:p>'


class BoxLinesTests(unittest.TestCase):
    def _merge(self, *elements: str, adjacent=None):
        doc = Document()
        body = doc.element.body
        for element in elements:
            body[-1].addprevious(parse_xml(element))
        join_box_rows(body, lambda a, b: paragraph_continues(Paragraph(a, body), Paragraph(b, body)), adjacent)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "box.docx"
        doc.save(str(path))
        merge_wrapped_lines(path)
        return Document(str(path))

    def test_lines_of_one_paragraph_in_a_box_join_into_one_cell(self):
        # 讲义第 59 页注记框：每行一张表，以前一行一段、一行一行地译，译文每行居中
        doc = self._merge(_row("If f is not onto then its image is a proper subset of B, call it X. If we redefine f so"),
                          _SPACER, _row("that it becomes a function from A to X then it becomes onto. In this way we"),
                          _SPACER, _row("mind though that this can be difficult.", jc="left", fill="FFFFFF", height=410))
        tables = doc.tables
        self.assertEqual(len(tables), 1)
        cell = tables[0].rows[0].cells[0]
        self.assertEqual([p.text for p in cell.paragraphs], [(
            "If f is not onto then its image is a proper subset of B, call it X. If we redefine f so that it becomes "
            "a function from A to X then it becomes onto. In this way we mind though that this can be difficult.")])
        self.assertEqual(cell.paragraphs[0].paragraph_format.alignment, 3)  # 两端对齐
        height = tables[0].rows[0]._tr.find(f"{qn('w:trPr')}/{qn('w:trHeight')}").get(qn("w:val"))
        # 行高按原文一行一张表的高度加起来，表和表之间的空段不算
        self.assertEqual(height, str(270 + 270 + 410))

    def test_header_bar_and_finished_sentences_stay_apart(self):
        # 深色的标题条「Remark」不和框里的正文并；以句号结束的一行和下一张表不并
        doc = self._merge(_row("Remark", fill="00004C", jc="left"), _SPACER,
                          _row("Contrast the definition of a bijective function."), _SPACER,
                          _row("A second sentence starts here.", jc="left"))
        self.assertEqual([t.rows[0].cells[0].text for t in doc.tables],
                         ["Remark", "Contrast the definition of a bijective function.", "A second sentence starts here."])

    def test_lines_out_of_order_in_the_pdf_stay_apart(self):
        # 讲义第 23 页例题框：pdf2docx 把「be the primeness predicate…」排到了后面，接在「Do you think this
        # sentence」后面读着也通，但原 PDF 上这两行不挨着：不并
        doc = self._merge(_row("positive integer y such that x is greater. Do you think this sentence"), _SPACER,
                          _row("be the primeness predicate. Say we want to express"),
                          adjacent=lambda _a, _b: False)
        self.assertEqual(len(doc.tables), 2)

    def test_a_box_of_another_width_is_another_box(self):
        doc = self._merge(_row("A line that does not end"), _SPACER, _row("in another box", width=6000))
        self.assertEqual(len(doc.tables), 2)


class _Page:
    def __init__(self, blocks):
        self.blocks = blocks

    def get_text(self, _kind):
        return {"blocks": self.blocks}


def _line(x0: float, x1: float, text: str) -> dict:
    return {"bbox": (x0, 0, x1, 10), "spans": [{"text": text}]}


class FullLinesTests(unittest.TestCase):
    def test_lines_reaching_both_edges_of_a_justified_block(self):
        block = {"type": 0, "bbox": (72, 0, 523, 60), "lines": [
            _line(72.3, 523.0, "Figure 22 shows two functions. The function in (i) is a bijection."),
            _line(72.3, 523.0, "We can see this because every dot has exactly one arrow pointing to it."),
            _line(72.3, 471.0, "So the function in (ii) is not a bijection.")]}
        widths = full_lines(_Page([block]))
        self.assertEqual(set(widths), {
            "Figure22showstwofunctions.Thefunctionin(i)isabijection.",
            "Wecanseethisbecauseeverydothasexactlyonearrowpointingtoit."})
        self.assertAlmostEqual(widths["Wecanseethisbecauseeverydothasexactlyonearrowpointingtoit."], 450.7)

    def test_centered_lines_labels_and_single_lines_are_not_full(self):
        title = {"type": 0, "bbox": (100, 0, 400, 30), "lines": [
            _line(100, 400, "A centered title that is long"), _line(180, 320, "second line")]}
        labels = {"type": 0, "bbox": (100, 0, 400, 10), "lines": [_line(100, 120, "(i)"), _line(380, 400, "(ii)")]}
        formula = {"type": 0, "bbox": (150, 0, 450, 10), "lines": [_line(150, 450, "{(x, x), (x, z)}")]}
        self.assertEqual(full_lines(_Page([title, labels, formula])), {})


if __name__ == "__main__":
    unittest.main()
