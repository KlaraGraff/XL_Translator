"""PDF 转 Word 翻译后，译文里新写的数学符号改用 Cambria Math（反馈 #28）。

译文整句写进原来那段字：「假设 A 是任意命题。则 A ∨」用的是讲义正文的 Chivo，用户电脑
上没有，Mac 的 Word 换成 Cambria 也没有 ∨ 的字形，显示成方框。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from docx import Document
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls, qn

from core.pdf_word_symbols import MATH_FONT, math_symbols_in_math_font

SENTENCE = "假设 A 是任意命题。则 A ∨ (¬A) 恒真。"


def _runs(path: Path) -> list[tuple[str, dict]]:
    result = []
    for run in Document(str(path)).element.body.iter(qn("w:r")):
        fonts = run.find(f"{qn('w:rPr')}/{qn('w:rFonts')}")
        text = "".join(node.text or "" for node in run.iter(qn("w:t")))
        result.append((text, dict(fonts.attrib) if fonts is not None else {}))
    return result


class MathSymbolFontTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "out.docx"
        doc = Document()
        doc.element.body[-1].addprevious(parse_xml(
            f'<w:p {nsdecls("w")}><w:r><w:rPr><w:rFonts w:eastAsia="Microsoft YaHei" w:ascii="Chivo" '
            f'w:hAnsi="Chivo" w:hint="eastAsia"/><w:i/></w:rPr><w:t>{SENTENCE}</w:t></w:r>'
            f'<w:r><w:rPr><w:rFonts w:asciiTheme="minorHAnsi" w:hAnsiTheme="minorHAnsi"/></w:rPr><w:t>⇒</w:t></w:r></w:p>'))
        doc.save(str(self.path))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_symbols_are_split_out_in_cambria_math(self) -> None:
        self.assertEqual(math_symbols_in_math_font(self.path), 2)
        runs = _runs(self.path)
        self.assertEqual("".join(text for text, _ in runs), SENTENCE + "⇒")
        by_text = {text: fonts for text, fonts in runs}
        for symbol in ("∨ ", "⇒"):
            fonts = by_text[symbol]
            for slot in ("ascii", "hAnsi", "eastAsia", "cs"):
                self.assertEqual(fonts[qn(f"w:{slot}")], MATH_FONT)
            self.assertNotIn(qn("w:hint"), fonts)
            self.assertFalse(any(name.endswith("Theme") for name in fonts))
        # 别的字还是原来的字体，¬ 普通字体都有，不换
        self.assertEqual(by_text["假设 A 是任意命题。则 A "][qn("w:ascii")], "Chivo")
        self.assertEqual(by_text["(¬A) 恒真。"][qn("w:ascii")], "Chivo")
        # 斜体等其它格式跟着拆出来的每一段
        italic = [run for run in Document(str(self.path)).element.body.iter(qn("w:r"))
                  if run.find(f"{qn('w:rPr')}/{qn('w:i')}") is not None]
        self.assertEqual(len(italic), 3)

    def test_running_twice_changes_nothing(self) -> None:
        math_symbols_in_math_font(self.path)
        self.assertEqual(math_symbols_in_math_font(self.path), 0)


if __name__ == "__main__":
    unittest.main()
