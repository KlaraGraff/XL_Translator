"""一页原文仍排成一页译文（本机实测：3 页的中英签证审核清单，转回 PDF 成了 5 页）。

放不下的页缩小字号、段距、行高，重新转 PDF；放得下的页不动。表格里打勾的空格子没写
字号，按默认 11 号撑着行高，也要一起缩。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from docx import Document
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls, qn

from core import pdf_word_fit
from core.pdf_word_fit import _anchors, fit_pages, page_groups, pages_per_group, shrink_group

_PAGE_BREAK = '<w:p {ns}><w:pPr><w:sectPr/></w:pPr></w:p>'
_COLUMN_BREAK = '<w:p {ns}><w:pPr><w:sectPr><w:type w:val="continuous"/></w:sectPr></w:pPr></w:p>'


def _paragraph(text: str, size: int = 18) -> str:
    return (f'<w:p {nsdecls("w")}><w:pPr><w:spacing w:before="100" w:after="40" w:line="220" '
            f'w:lineRule="exact"/></w:pPr><w:r><w:rPr><w:sz w:val="{size}"/></w:rPr>'
            f'<w:t>{text}</w:t></w:r></w:p>')


class PdfWordFitTests(unittest.TestCase):
    def _doc(self, pages: list[list[str]]):
        """和 pdf2docx 一样：每页第一节是「新页」分节，页内其余是「连续」分节（分栏）。"""
        doc = Document()
        body = doc.element.body
        final = body[-1]  # 文档最后的 sectPr：结束最后一页的「连续」分节
        final.append(parse_xml(f'<w:type {nsdecls("w")} w:val="continuous"/>'))
        for index, paragraphs in enumerate(pages):
            first, *rest = paragraphs
            final.addprevious(parse_xml(_paragraph(first)))
            final.addprevious(parse_xml(_PAGE_BREAK.format(ns=nsdecls("w"))))
            for text in rest:
                final.addprevious(parse_xml(_paragraph(text)))
            if index < len(pages) - 1:
                final.addprevious(parse_xml(_COLUMN_BREAK.format(ns=nsdecls("w"))))
        return doc

    def _save(self, doc) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "translated.docx"
        doc.save(str(path))
        return path

    def test_groups_follow_new_page_sections_only(self):
        doc = self._doc([["第一页开头独有的一句话", "第一页第二句话内容"], ["第二页开头独有的一句话"]])
        groups = page_groups(doc.element.body)
        self.assertEqual(len(groups), 2)

    def test_pages_per_group_skips_repeated_headers(self):
        anchors = [["第一页开头独有的"], ["第二页开头独有的"], ["第三页开头独有的"]]
        texts = ["护照号码第一页开头独有的一句", "后半截", "第二页开头独有的", "第三页开头独有的"]
        self.assertEqual(pages_per_group(anchors, texts), [2, 1, 1])
        self.assertIsNone(pages_per_group([["找不到的一句话"]], texts))

    def test_anchors_come_from_the_top_of_each_page(self):
        # 每页前几段开头都一样（模板句），只有更长的开头或页尾页码不同：用加长的开头，不用页尾
        same = "Unique opening line of page"
        tail = [f"filler line {n} that repeats" for n in range(8)]
        doc = self._doc([[f"{same} one", *tail, "Page 1 of 2"], [f"{same} two", *tail, "Page 2 of 2"]])
        anchors = _anchors(page_groups(doc.element.body))
        self.assertEqual(anchors, [["Uniqueopeninglineofpageone"], ["Uniqueopeninglineofpagetwo"]])

    def test_footer_only_pages_are_not_located(self):
        tail = [f"filler line {n} that repeats" for n in range(8)]
        doc = self._doc([[*tail, "Page 1 of 2"], [*tail, "Page 2 of 2"]])
        anchors = _anchors(page_groups(doc.element.body))
        self.assertEqual(anchors, [[], []])
        self.assertIsNone(pages_per_group(anchors, ["a", "Page1of2", "Page2of2"]))

    def test_first_page_must_start_the_pdf(self):
        self.assertIsNone(pages_per_group([["第一页开头"], ["第二页开头"]], ["溢出", "第一页开头", "第二页开头"]))

    def test_shrink_scales_sizes_spacing_and_empty_cells(self):
        doc = Document()
        table = doc.add_table(rows=1, cols=2)
        table.cell(0, 0).paragraphs[0].add_run("Cover letter / 介绍信").font.size = None
        tr_pr = table.rows[0]._tr.get_or_add_trPr()
        tr_pr.append(parse_xml(f'<w:trHeight {nsdecls("w")} w:hRule="atLeast" w:val="400"/>'))
        body = doc.element.body
        body[-1].addprevious(parse_xml(_paragraph("正文", size=20)))
        shrink_group(list(body)[:-1], 0.9, default_size=22)
        sizes = [int(size.get(qn("w:val"))) for size in body.iter(qn("w:sz"))]
        # 正文 20→18；表格里没写字号的文字和空格子的段落标记按默认 22→20
        self.assertIn(18, sizes)
        self.assertEqual(sizes.count(20), 2)
        self.assertEqual(table.rows[0]._tr.find(f"{qn('w:trPr')}/{qn('w:trHeight')}").get(qn("w:val")), "360")
        spacing = body.find(f".//{qn('w:pPr')}/{qn('w:spacing')}")
        self.assertEqual((spacing.get(qn("w:before")), spacing.get(qn("w:line"))), ("90", "198"))

    def test_fit_shrinks_only_the_overflowing_page(self):
        doc = self._doc([["第一页开头独有的一句话"], ["第二页开头独有的一句话"]])
        path = self._save(doc)
        pdf = path.with_suffix(".pdf")
        renders = iter([
            ["第一页开头独有的一句话", "溢出的几行", "第二页开头独有的一句话"],  # 第一次转出：3 页
            ["第一页开头独有的一句话", "第二页开头独有的一句话"],              # 缩一次后：2 页
        ])
        exports: list[Path] = []
        with mock.patch.object(pdf_word_fit, "pdf_page_texts", side_effect=lambda _pdf: next(renders)):
            result = fit_pages(path, pdf, 2, lambda docx, _pdf: exports.append(docx))
        self.assertEqual((result.pages, result.scaled, result.still_over), (2, {1: 0.9}, []))
        self.assertEqual(len(exports), 1)
        groups = page_groups(Document(str(path)).element.body)
        first = [int(size.get(qn("w:val"))) for element in groups[0] for size in element.iter(qn("w:sz"))]
        second = [int(size.get(qn("w:val"))) for element in groups[1] for size in element.iter(qn("w:sz"))]
        self.assertIn(16, first)       # 18 × 0.9
        self.assertNotIn(16, second)   # 放得下的页不动
        self.assertIn(18, second)

    def test_gives_up_at_minimum_scale(self):
        path = self._save(self._doc([["第一页开头独有的一句话"], ["第二页开头独有的一句话"]]))
        always_over = ["第一页开头独有的一句话", "溢出", "第二页开头独有的一句话"]
        with mock.patch.object(pdf_word_fit, "pdf_page_texts", return_value=always_over):
            result = fit_pages(path, path.with_suffix(".pdf"), 2, lambda _docx, _pdf: None)
        self.assertEqual(result.still_over, [1])
        self.assertGreaterEqual(result.scaled[1], pdf_word_fit.MIN_SCALE)

    def test_section_count_mismatch_leaves_file_alone(self):
        path = self._save(self._doc([["第一页开头独有的一句话"], ["第二页开头独有的一句话"]]))
        before = path.read_bytes()
        with mock.patch.object(pdf_word_fit, "pdf_page_texts", return_value=["a", "b", "c", "d"]):
            result = fit_pages(path, path.with_suffix(".pdf"), 3, lambda _docx, _pdf: None)
        self.assertEqual((result.pages, result.scaled), (4, {}))
        self.assertEqual(path.read_bytes(), before)

    def test_failure_midway_restores_both_files(self):
        from core.pdf_word_segments import PdfWordSegmentsMixin

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        docx, pdf = Path(tmp.name) / "seg.docx", Path(tmp.name) / "seg.pdf"
        docx.write_bytes(b"docx-before")
        pdf.write_bytes(b"pdf-before")

        def half_done(docx_path, pdf_path, *_args):
            docx_path.write_bytes(b"docx-shrunk")
            pdf_path.unlink()  # Word 转 PDF 先删旧文件，然后失败
            raise RuntimeError("Word 没有生成 PDF")

        runner = PdfWordSegmentsMixin()
        logs: list[tuple[str, str]] = []
        runner._log = lambda level, message: logs.append((level, message))  # type: ignore[attr-defined]
        record = mock.Mock()
        record.name = "清单.pdf"
        with mock.patch.object(pdf_word_fit, "fit_pages", side_effect=half_done):
            pages = runner._fit_word_segment(record, [1, 2, 3], docx, pdf, 5)
        self.assertEqual(pages, 5)
        self.assertEqual((docx.read_bytes(), pdf.read_bytes()), (b"docx-before", b"pdf-before"))
        self.assertEqual(sorted(path.name for path in Path(tmp.name).iterdir()), ["seg.docx", "seg.pdf"])
        self.assertEqual(logs[-1][0], "WARN")


if __name__ == "__main__":
    unittest.main()
