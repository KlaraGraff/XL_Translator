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

    def _make(self, cell: str, bar: bool = False) -> None:
        pdf = pymupdf.open()
        page = pdf.new_page(width=595, height=842)
        if bar:
            # 格子的底色（大块）和证明表左边的一条竖线、一条短横线（细线）
            page.draw_rect(pymupdf.Rect(50, 60, 500, 200), color=None, fill=(1, 0.95, 0.95))
            page.draw_line((110, 90), (110, 140), color=(0, 0, 0), width=0.4)
            page.draw_line((110, 104), (119, 104), color=(0, 0, 0), width=0.4)
        for x, y, text in _STEPS:
            page.insert_text((x, y), text, fontname="helv", fontsize=11)
        pdf.save(str(self.pdf))
        doc = Document()
        if bar:
            doc.add_paragraph()  # 表格前面的正文段（空段），线条图挂在这里
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

    @staticmethod
    def _bar_table(text: str = "") -> str:
        # pdf2docx 画线用的小表格：左边框是竖线，第一行的下边框是短横线；缩进从格子文字左边量起
        borders = '<w:tcBorders><w:start w:val="single" w:sz="4"/><w:bottom w:val="single" w:sz="4"/></w:tcBorders>'
        return (f'<w:tbl><w:tblPr><w:tblInd w:w="200" w:type="dxa"/></w:tblPr><w:tblGrid><w:gridCol w:w="180"/>'
                f'</w:tblGrid><w:tr><w:trPr><w:trHeight w:val="280" w:hRule="exact"/></w:trPr><w:tc><w:tcPr>{borders}'
                f'</w:tcPr><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:tc></w:tr></w:tbl>')

    def test_bar_tables_become_a_picture_of_the_lines(self) -> None:
        # 证明表的竖线、短横线是格子里只有边框的小表格：重排后去掉（不然挤到格子最下面把格子撑爆），
        # 原文那几条线画成透明的图，浮在字上面（格子有底色，放在字下面会被盖住），按页面定位
        self._make(_cell_paragraph("1. Alpha one 3. Gamma three", 0) + _cell_paragraph("2. Beta two", 400)
                   + self._bar_table(), bar=True)
        self.assertEqual(restore_line_order(self.docx, self.pdf), 1)
        doc = Document(str(self.docx))
        tc = doc.tables[0].cell(0, 0)._tc
        self.assertEqual(tc.findall(qn("w:tbl")), [])
        self.assertEqual(tc[-1].tag, qn("w:p"))
        # 和 pdf2docx 自己的图一样挂在表格外面的正文段上（挂在格子里，Word 会从格子量起）
        self.assertEqual(tc.findall(f".//{qn('wp:anchor')}"), [])
        anchors = doc.element.body.findall(f".//{qn('wp:anchor')}")
        self.assertEqual(len(anchors), 1)
        anchor = anchors[0]
        self.assertEqual(anchor.get("behindDoc"), "0")
        self.assertEqual(anchor.find(qn("wp:positionH")).get("relativeFrom"), "page")
        emu = 12700
        left = int(anchor.find(f"{qn('wp:positionH')}/{qn('wp:posOffset')}").text) / emu
        top = int(anchor.find(f"{qn('wp:positionV')}/{qn('wp:posOffset')}").text) / emu
        width = int(anchor.find(qn("wp:extent")).get("cx")) / emu
        height = int(anchor.find(qn("wp:extent")).get("cy")) / emu
        # 竖线在 x=110，线 90–140 高、横线到 119：底色那一大块不算进来
        self.assertAlmostEqual(left, 110 - 1.4, delta=0.5)
        self.assertAlmostEqual(top, 90 - 1.4, delta=0.5)
        self.assertAlmostEqual(width, 9 + 2.8, delta=0.5)
        self.assertAlmostEqual(height, 50 + 2.8, delta=0.5)
        self.assertEqual([p.text.strip() for p in doc.tables[0].cell(0, 0).paragraphs][:3],
                         ["1. Alpha one", "2. Beta two", "3. Gamma three"])

    def test_cell_with_a_nested_table_holding_text_is_left_alone(self) -> None:
        self._make(_cell_paragraph("1. Alpha one 3. Gamma three", 0) + _cell_paragraph("2. Beta two", 400)
                   + self._bar_table("x"), bar=True)
        self.assertEqual(restore_line_order(self.docx, self.pdf), 0)
        self.assertEqual(len(Document(str(self.docx)).tables[0].cell(0, 0)._tc.findall(qn("w:tbl"))), 1)

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

    def test_superscripts_in_a_formula_stay_on_their_line(self) -> None:
        # 讲义第 91 页 x^(y_t 2^t)：pdf2docx 把它放进一个格子；上标里的上标小一号、高出本行一截，中线落在
        # 本行外面，以前当成另起一行（还在上一行上面），整格当一行一行摆的切开重排，「t」跑到最上面、公式散了
        pdf = pymupdf.open()
        page = pdf.new_page(width=595, height=842)
        x = 100.0
        parts = (("x", 11, 0), ("yt", 7, 5), ("2", 5, 9), ("t", 4, 12))
        for text, size, rise in parts:
            page.insert_text((x, 120 - rise), text, fontname="helv", fontsize=size)
            x += pymupdf.get_text_length(text, fontname="helv", fontsize=size) + (2 if size == 11 else 0)
        pdf.save(str(self.pdf))
        doc = Document()
        runs = "".join(f'<w:r><w:t xml:space="preserve">{text}</w:t></w:r>' for text, _size, _rise in parts)
        doc.element.body[-1].addprevious(parse_xml(
            f'<w:tbl {nsdecls("w")}><w:tblGrid><w:gridCol w:w="4000"/></w:tblGrid><w:tr>'
            f'<w:tc><w:p>{runs}</w:p></w:tc></w:tr></w:tbl>'))
        doc.save(str(self.docx))
        self.assertEqual(restore_line_order(self.docx, self.pdf), 0)

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


# 算法框（反馈 #28 讲义第 91 页 Algorithm 4）：一行一条，行号顶格，循环体在行号后面空开一截缩进
_ALGORITHM = [((90, 100), "Algorithm 4 Repeated squaring"),
              ((90, 114), "Input: Base-2 representations of x and y in binary."),
              ((90, 128), "1: z := 1"), ((90, 142), "2: for i = 0 to t do"),
              ((90, 156), "3:", (128, "z := z * p")), ((90, 170), "4: end for")]


def _body_paragraph(text: str, line: int = 280) -> str:
    return (f'<w:p {nsdecls("w")}><w:pPr><w:spacing w:before="0" w:after="0" w:line="{line}" w:lineRule="exact"/>'
            f'<w:ind w:left="0" w:right="0"/><w:jc w:val="left"/></w:pPr>'
            f'<w:r><w:rPr><w:sz w:val="22"/></w:rPr><w:t xml:space="preserve">{text}</w:t></w:r></w:p>')


@unittest.skipIf(pymupdf is None, "PyMuPDF not installed")
class BodyParagraphTests(unittest.TestCase):
    """算法框转出来是正文段：pdf2docx 把几行拼成一段（换行符后来又换成空格），翻译后挤成一团。"""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.pdf = Path(tmp.name) / "in.pdf"
        self.docx = Path(tmp.name) / "out.docx"

    def _make(self, lines, paragraphs: list[str]) -> None:
        pdf = pymupdf.open()
        page = pdf.new_page(width=612, height=792)
        for (x, y), text, *rest in lines:
            page.insert_text((x, y), text, fontname="helv", fontsize=11)
            for x_more, more in rest:
                page.insert_text((x_more, y), more, fontname="helv", fontsize=11)
        pdf.save(str(self.pdf))
        doc = Document()  # Letter 纸，左右页边距 90 磅：正文这一栏从 90 到 522
        body = doc.element.body
        for paragraph in paragraphs:
            body[-1].addprevious(parse_xml(paragraph))
        doc.save(str(self.docx))

    def _paragraphs(self):
        return [p for p in Document(str(self.docx)).paragraphs if p.text.strip()]

    def test_algorithm_box_gets_one_paragraph_per_line(self) -> None:
        # 第 1 段：标题和 Input 拼在一起；第 2 段：4 行代码拼在一起，第 3 行的缩进只剩一个空格
        self._make(_ALGORITHM, [
            _body_paragraph("Algorithm 4 Repeated squaring Input: Base-2 representations of x and y in binary."),
            _body_paragraph("1: z := 1 2: for i = 0 to t do 3: z := z * p 4: end for")])
        self.assertEqual(restore_line_order(self.docx, self.pdf), 2)
        paragraphs = self._paragraphs()
        self.assertEqual([p.text.strip() for p in paragraphs],
                         ["Algorithm 4 Repeated squaring", "Input: Base-2 representations of x and y in binary.",
                          "1: z := 1", "2: for i = 0 to t do", "3: \tz := z * p", "4: end for"])
        # 「3:」后面空开的一截换成制表符，制表位在原文「z」的位置（从这一栏左边量起 128 - 90 = 38 磅）
        stops = [int(tab.get(qn("w:pos"))) for tab in paragraphs[4]._p.findall(f".//{qn('w:tabs')}/{qn('w:tab')}")]
        self.assertEqual(len(stops), 1)
        self.assertAlmostEqual(stops[0] / 20, 38, delta=1)
        # 各行段前距照原文的行距（14 磅一行），不是原来那么大的固定行高
        spacing = paragraphs[3]._p.find(f"{qn('w:pPr')}/{qn('w:spacing')}")
        self.assertEqual(spacing.get(qn("w:lineRule")), "exact")
        self.assertLess(int(spacing.get(qn("w:before"))) + int(spacing.get(qn("w:line"))), 14 * 20 + 40)

    def test_widest_line_followed_by_a_short_one_is_cut_after_a_laid_out_paragraph(self) -> None:
        # 「4: if … then」是这一段最宽的一行，单看「5:」放不进去，像折行；它紧跟在一行一行摆的标题段
        # 后面，按那一串段最宽的一行（Input 那一行）量就放得进。中间单独一行的「6: end if」也补上缩进
        lines = [((90, 100), "Algorithm 4 Repeated squaring"),
                 ((90, 114), "Input: Base-2 representations of x and y in binary."),
                 ((90, 128), "4:", (118, "if y is odd then")), ((90, 142), "5:", (136, "z := z * p")),
                 ((90, 156), "6:", (118, "end if"))]
        self._make(lines, [
            _body_paragraph("Algorithm 4 Repeated squaring Input: Base-2 representations of x and y in binary."),
            _body_paragraph("4: if y is odd then 5: z := z * p"), _body_paragraph("6: end if")])
        self.assertEqual(restore_line_order(self.docx, self.pdf), 2)
        self.assertEqual([p.text.strip() for p in self._paragraphs()][2:],
                         ["4: \tif y is odd then", "5: \tz := z * p", "6: \tend if"])

    def test_wrapped_prose_stays_one_paragraph(self) -> None:
        # 正文折行：下一行的第一个词放不进这几行里最宽的那一行，不切
        lines = [((90, 100), "The quick brown fox jumps over the lazy dog and keeps running far away from"),
                 ((90, 114), "the farm.")]
        text = "The quick brown fox jumps over the lazy dog and keeps running far away from the farm."
        self._make(lines, [_body_paragraph(text)])
        self.assertEqual(restore_line_order(self.docx, self.pdf), 0)
        self.assertEqual([p.text for p in self._paragraphs()], [text])

    def test_wrapped_item_after_a_laid_out_paragraph_stays_whole(self) -> None:
        # 方框里的列表（讲义第 51 页 Summary）：标题一行一段，后面的列表项在方框里折行，折行的那一行和
        # 方框里最宽的一行一样宽（方框比这一栏窄，按这一栏量，下一个词放得进）：不切
        lines = [((108, 100), "Summary"), ((108, 114), "Important ideas from Section 5."),
                 ((108, 128), "- Every set is a subset of itself, and the empty set is a subset of every"),
                 ((118, 142), "set."),
                 ((108, 156), "- The power set of A is the set of all subsets of A, for every set A at all"),
                 ((118, 170), "here.")]
        self._make(lines, [
            _body_paragraph("Summary Important ideas from Section 5."),
            _body_paragraph("- Every set is a subset of itself, and the empty set is a subset of every set."),
            _body_paragraph("- The power set of A is the set of all subsets of A, for every set A at all here.")])
        self.assertEqual(restore_line_order(self.docx, self.pdf), 1)
        self.assertEqual([p.text.strip() for p in self._paragraphs()],
                         ["Summary", "Important ideas from Section 5.",
                          "- Every set is a subset of itself, and the empty set is a subset of every set.",
                          "- The power set of A is the set of all subsets of A, for every set A at all here."])
