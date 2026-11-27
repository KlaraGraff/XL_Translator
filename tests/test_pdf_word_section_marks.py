"""两端对齐的每页最后一段，分节符挪到自己的空段：Word 不再把最后一行拉满（反馈 #28 讲义第 66 页）。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from docx import Document
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls, qn

from core.pdf_word_fit import page_groups
from core.pdf_word_section_marks import split_justified_section_marks


def _page_end(text: str, jc: str) -> str:
    return (f'<w:p {nsdecls("w")}><w:pPr><w:jc w:val="{jc}"/><w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
            f'</w:sectPr></w:pPr><w:r><w:t>{text}</w:t></w:r></w:p>')


class SplitJustifiedSectionMarksTests(unittest.TestCase):
    def test_justified_page_end_gets_its_own_section_mark(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "pages.docx"
        doc = Document()
        body = doc.element.body
        body[-1].addprevious(parse_xml(_page_end("Loops are their own reverse.", "both")))
        body[-1].addprevious(parse_xml(_page_end("A heading on the left", "left")))
        doc.save(str(path))
        self.assertEqual(split_justified_section_marks(path), 1)
        body = Document(str(path)).element.body
        paragraphs = body.findall(qn("w:p"))
        text, mark, left = paragraphs[-3:]
        self.assertIsNone(text.find(f"{qn('w:pPr')}/{qn('w:sectPr')}"))
        self.assertIsNotNone(mark.find(f"{qn('w:pPr')}/{qn('w:sectPr')}"))
        self.assertEqual("".join(node.text or "" for node in mark.iter(qn("w:t"))), "")
        self.assertEqual(mark.find(f"{qn('w:pPr')}/{qn('w:spacing')}").get(qn("w:line")), "20")
        # 靠左的段不动；分页还是一页一节
        self.assertIsNotNone(left.find(f"{qn('w:pPr')}/{qn('w:sectPr')}"))
        self.assertEqual(len(page_groups(body)), 3)


if __name__ == "__main__":
    unittest.main()
