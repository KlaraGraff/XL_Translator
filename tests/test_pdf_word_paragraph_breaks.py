"""一段里「换行 + 制表符缩进 + 大写字母开头」处拆成两段（反馈 #28 讲义第 21 页）。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from docx import Document
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls, qn


def _paragraph(*parts: str) -> str:
    runs = []
    for part in parts:
        if part == "BR":
            runs.append("<w:r><w:br/></w:r>")
        elif part == "TAB":
            runs.append("<w:r><w:tab/></w:r>")
        else:
            runs.append(f'<w:r><w:t xml:space="preserve">{part}</w:t></w:r>')
    return (f'<w:p {nsdecls("w")}><w:pPr><w:spacing w:before="62" w:after="0"/></w:pPr>{"".join(runs)}</w:p>')


class SplitAtIndentedBreaksTests(unittest.TestCase):
    def _merged(self, *parts: str) -> list[str]:
        from core.pdf_word_route import merge_wrapped_lines

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "breaks.docx"
        doc = Document()
        doc.element.body[-1].addprevious(parse_xml(_paragraph(*parts)))
        doc.save(str(path))
        merge_wrapped_lines(path)
        return [p.text for p in Document(str(path)).paragraphs]

    def test_next_paragraph_after_a_break_and_indent_is_split_off(self) -> None:
        # 命题 2.5 (ii) 末尾没有句号，下一段首行缩进写成制表符：以前换行被当成折行，两段并成一句
        texts = self._merged("(ii) If C is unsatisfiable, then A ∧ C is unsatisfiable ", "BR", "TAB",
                             "Next we collect a few more equivalences.")
        self.assertEqual(texts, ["(ii) If C is unsatisfiable, then A ∧ C is unsatisfiable ",
                                 "\tNext we collect a few more equivalences."])

    def test_wrapped_line_is_still_joined(self) -> None:
        texts = self._merged("If C is unsatisfiable, then", "BR", "A is equivalent to B.")
        self.assertEqual(texts, ["If C is unsatisfiable, then A is equivalent to B."])

    def test_split_paragraph_keeps_spacing_before_only_on_the_first(self) -> None:
        from core.pdf_word_paragraph_breaks import split_at_indented_breaks

        p = parse_xml(_paragraph("first part", "BR", "TAB", "Second part"))
        body = parse_xml(f'<w:body {nsdecls("w")}/>')
        body.append(p)
        self.assertEqual(split_at_indented_breaks(p), 1)
        first, second = body.findall(qn("w:p"))
        self.assertEqual(second.find(f"{qn('w:pPr')}/{qn('w:spacing')}").get(qn("w:before")), "0")
        self.assertEqual(first.findall(f".//{qn('w:br')}"), [])


if __name__ == "__main__":
    unittest.main()
