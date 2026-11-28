"""框里一行一张表的，同一段的几行并进同一格；原文排满的行不再标成居中（反馈 #28 讲义第 59、60、66 页）。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from docx import Document
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls, qn
from docx.text.paragraph import Paragraph

from core.pdf_word_box_lines import join_box_rows, join_split_cells, order_box_rows
from core.pdf_word_cjk_room import make_room_above_tables
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

    def test_a_wrapped_word_put_in_the_figure_label_table_comes_back(self):
        # 讲义第 66 页：框的最后一个词「attached.」被 pdf2docx 放进了下面图标签那张表的第一格
        labels = _row("attached.", jc="left", fill="FFFFFF").replace(
            '<w:tblGrid><w:gridCol w:w="9582"/></w:tblGrid>',
            '<w:tblGrid><w:gridCol w:w="4791"/><w:gridCol w:w="4791"/></w:tblGrid>').replace(
            "</w:tc></w:tr>", '</w:tc><w:tc><w:p><w:r><w:t>(ii)</w:t></w:r></w:p></w:tc></w:tr>')
        doc = self._merge(_row("since x and y do not have a loop", jc="left", fill="FFF2F2"), _SPACER, labels,
                          adjacent=lambda _a, _b: True)
        self.assertEqual(doc.tables[0].rows[0].cells[0].text, "since x and y do not have a loop attached.")
        self.assertEqual([c.text for c in doc.tables[1].rows[0].cells], ["", "(ii)"])

    def test_a_box_of_another_width_is_another_box(self):
        doc = self._merge(_row("A line that does not end"), _SPACER, _row("in another box", width=6000))
        self.assertEqual(len(doc.tables), 2)


def _cells(*texts: str, widths: tuple[int, ...] = ()) -> str:
    widths = widths or tuple(9582 // len(texts) for _ in texts)
    cells = "".join(f'<w:tc><w:tcPr><w:tcW w:w="{w}" w:type="dxa"/></w:tcPr><w:p><w:pPr><w:jc w:val="left"/></w:pPr>'
                    f'<w:r><w:t xml:space="preserve">{t}</w:t></w:r></w:p></w:tc>' for t, w in zip(texts, widths))
    grid = "".join(f'<w:gridCol w:w="{w}"/>' for w in widths)
    return f'<w:tbl {nsdecls("w")}><w:tblPr/><w:tblGrid>{grid}</w:tblGrid><w:tr>{cells}</w:tr></w:tbl>'


class SplitAndOrderTests(unittest.TestCase):
    def test_one_pdf_line_split_into_two_cells_joins_into_one(self):
        # 讲义第 66 页：两端对齐的一行中间空得宽，pdf2docx 拆成两格，译文一半被推到右边
        table = parse_xml(_cells("The diagrams show relations on the set {x, y, z}.", "The relation in (i) is",
                                 widths=(7066, 2516)))
        lines = {"Thediagramsshowrelationsontheset{x,y,z}.Therelationin(i)is": 450.7}
        self.assertEqual(join_split_cells(table.iter(qn("w:tr")), lines), 1)
        cells = table.findall(f"{qn('w:tr')}/{qn('w:tc')}")
        self.assertEqual(len(cells), 1)
        self.assertEqual(Paragraph(cells[0].find(qn("w:p")), None).text,
                         "The diagrams show relations on the set {x, y, z}. The relation in (i) is")
        self.assertEqual(cells[0].find(f"{qn('w:tcPr')}/{qn('w:gridSpan')}").get(qn("w:val")), "2")
        self.assertEqual(cells[0].find(f"{qn('w:tcPr')}/{qn('w:tcW')}").get(qn("w:w")), "9582")
        self.assertEqual(cells[0].find(f"{qn('w:p')}/{qn('w:pPr')}/{qn('w:jc')}").get(qn("w:val")), "both")

    def test_truth_table_cells_and_other_lines_stay_apart(self):
        truth = parse_xml(_cells("T", "F", "T"))
        self.assertEqual(join_split_cells(truth.iter(qn("w:tr")), {"TFT": 400.0}), 0)
        other = parse_xml(_cells("Premise number one", "Conjunction intro"))
        self.assertEqual(join_split_cells(other.iter(qn("w:tr")), {"Somethingelse": 400.0}), 0)

    def test_box_lines_out_of_pdf_order_are_put_back(self):
        # 讲义第 23 页例题框：最后一行被 pdf2docx 放进第二张表的小表里，前面垫了一大段空白
        nested = (f'<w:tbl {nsdecls("w")}><w:tblPr><w:tblInd w:w="0" w:type="dxa"/></w:tblPr>'
                  f'<w:tblGrid><w:gridCol w:w="9582"/></w:tblGrid><w:tr><w:trPr><w:trHeight w:val="270"/></w:trPr>'
                  f'<w:tc><w:p><w:r><w:t>is true or false?</w:t></w:r></w:p></w:tc></w:tr></w:tbl>')
        holder = _row("x").replace(
            '<w:p><w:pPr><w:spacing w:before="26" w:after="0"/><w:jc w:val="center"/></w:pPr>'
            '<w:r><w:rPr><w:sz w:val="22"/></w:rPr><w:t xml:space="preserve">x</w:t></w:r></w:p>',
            f'<w:p><w:pPr><w:spacing w:before="1955"/></w:pPr></w:p>{nested.replace(nsdecls("w"), "")}<w:p/>')
        doc = Document()
        body = doc.element.body
        for element in (_row("We consider the domain."), holder, _row("We can do so with the formula.")):
            body[-1].addprevious(parse_xml(element))
        lines = {"Weconsiderthedomain.": [(0, 0, 100.0)], "istrueorfalse?": [(0, 5, 170.0)],
                 "Wecandosowiththeformula.": [(0, 3, 140.0)]}
        children = list(body)
        self.assertEqual(order_box_rows(body, children, lines), 2)
        texts = [Paragraph(p, None).text for p in body.iter(qn("w:p")) if Paragraph(p, None).text]
        self.assertEqual(texts, ["We consider the domain.", "We can do so with the formula.", "is true or false?"])
        self.assertEqual(len(list(body.iter(qn("w:tbl")))), 3)  # 小表提了出来，不再套在大表里

    def test_lines_in_pdf_order_are_left_alone(self):
        doc = Document()
        body = doc.element.body
        for element in (_row("First line"), _row("Second line")):
            body[-1].addprevious(parse_xml(element))
        lines = {"Firstline": [(0, 0, 100.0)], "Secondline": [(0, 1, 114.0)]}
        self.assertEqual(order_box_rows(body, list(body), lines), 0)


class CjkRoomTests(unittest.TestCase):
    def test_paragraph_right_above_a_table_gets_room_for_cjk_glyphs(self):
        # 讲义第 59 页：固定行距 10.9 磅装得下英文字，装不下中文字，表的上边框压字
        doc = Document()
        body = doc.element.body
        above = (f'<w:p {nsdecls("w")}><w:pPr><w:spacing w:before="100" w:after="0" w:line="218" '
                 f'w:lineRule="exact"/></w:pPr><w:r><w:rPr><w:sz w:val="22"/></w:rPr><w:t>The table below</w:t></w:r></w:p>')
        elsewhere = above.replace("The table below", "Plain text")
        for element in (elsewhere, above, _SPACER, _row("cell")):
            body[-1].addprevious(parse_xml(element))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "room.docx"
        doc.save(str(path))
        self.assertEqual(make_room_above_tables(path), 1)
        spacings = [p.find(f"{qn('w:pPr')}/{qn('w:spacing')}") for p in Document(str(path)).element.body.findall(qn("w:p"))]
        self.assertEqual(spacings[0].get(qn("w:after")), "0")
        self.assertEqual(spacings[1].get(qn("w:after")), str(round(1.3 * 220 - 218)))


class _Page:
    def __init__(self, blocks):
        self.blocks = blocks

    def get_text(self, _kind):
        return {"blocks": self.blocks}


def _line(x0: float, x1: float, text: str, y: float = 0) -> dict:
    return {"bbox": (x0, y, x1, y + 10), "spans": [{"text": text}]}


class FullLinesTests(unittest.TestCase):
    def test_lines_reaching_both_edges_of_a_justified_block(self):
        block = {"type": 0, "bbox": (72, 0, 523, 60), "lines": [
            _line(72.3, 523.0, "Figure 22 shows two functions. The function in (i) is a bijection.", 0),
            _line(72.3, 523.0, "We can see this because every dot has exactly one arrow pointing to it.", 14),
            _line(72.3, 471.0, "So the function in (ii) is not a bijection.", 28)]}
        widths = full_lines(_Page([block]))
        self.assertEqual(set(widths), {
            "Figure22showstwofunctions.Thefunctionin(i)isabijection.",
            "Wecanseethisbecauseeverydothasexactlyonearrowpointingtoit."})
        self.assertAlmostEqual(widths["Wecanseethisbecauseeverydothasexactlyonearrowpointingtoit."], 450.7)

    def test_centered_lines_labels_and_single_lines_are_not_full(self):
        title = {"type": 0, "bbox": (100, 0, 400, 30), "lines": [
            _line(100, 400, "A centered title that is long"), _line(180, 320, "second line", 15)]}
        labels = {"type": 0, "bbox": (100, 0, 400, 10), "lines": [_line(100, 120, "(i)"), _line(380, 400, "(ii)")]}
        formula = {"type": 0, "bbox": (150, 0, 450, 10), "lines": [_line(150, 450, "{(x, x), (x, z)}")]}
        self.assertEqual(full_lines(_Page([title, labels, formula])), {})

    def test_a_wide_gap_split_by_pymupdf_is_one_line(self):
        # 讲义第 66 页：两端对齐的一行中间空得宽，PyMuPDF 拆成左右两段；按上沿接回一行
        block = {"type": 0, "bbox": (72, 0, 523, 60), "lines": [
            _line(72.3, 405.7, "The diagrams show relations on the set {x, y, z}.", 0),
            _line(415.9, 523.0, "The relation in (i) is", 0),
            _line(72.3, 523.0, "{(x, x), (y, y)}. This is a reflexive relation, since every element", 13.6),
            _line(72.3, 120.9, "attached.", 27.2)]}
        self.assertIn("Thediagramsshowrelationsontheset{x,y,z}.Therelationin(i)is", full_lines(_Page([block])))


if __name__ == "__main__":
    unittest.main()
