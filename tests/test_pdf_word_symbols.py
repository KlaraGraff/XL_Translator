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

    def test_symbols_next_to_tabs_and_breaks_are_split_too(self) -> None:
        # 证明步骤表「11. ¬P1 12. P2 ∧(¬P2)」：一段字里夹着制表符和换行
        doc = Document()
        doc.element.body[-1].addprevious(parse_xml(
            f'<w:p {nsdecls("w")}><w:r><w:rPr><w:rFonts w:ascii="Chivo" w:hAnsi="Chivo"/></w:rPr>'
            '<w:t>11. ¬P1</w:t><w:tab/><w:t xml:space="preserve">12. P2 ∧(¬P2)</w:t><w:br/><w:t>13.</w:t></w:r></w:p>'))
        doc.save(str(self.path))
        self.assertEqual(math_symbols_in_math_font(self.path), 1)
        runs = list(Document(str(self.path)).element.body.iter(qn("w:r")))
        shapes = [[child.tag.split("}")[1] for child in run if child.tag != qn("w:rPr")] for run in runs]
        self.assertEqual(shapes, [["t", "tab", "t"], ["t"], ["t", "br", "t"]])
        fonts = [run.find(f"{qn('w:rPr')}/{qn('w:rFonts')}").get(qn("w:ascii")) for run in runs]
        self.assertEqual(fonts, ["Chivo", MATH_FONT, "Chivo"])

    def test_running_twice_changes_nothing(self) -> None:
        math_symbols_in_math_font(self.path)
        self.assertEqual(math_symbols_in_math_font(self.path), 0)


if __name__ == "__main__":
    unittest.main()


class TexGlyphTests(unittest.TestCase):
    """TeX 的大号运算符字体不按 Unicode 编码：∑ 存成 P / X，∫ 是 R / Z；黑板粗体 R 是 ℝ。"""

    def _doc(self, runs: list[tuple[str, str]]) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "out.docx"
        doc = Document()
        body = "".join(
            f'<w:r><w:rPr><w:rFonts w:ascii="{font}" w:hAnsi="{font}"/></w:rPr>'
            f'<w:t xml:space="preserve">{text}</w:t></w:r>'
            for font, text in runs
        )
        doc.element.body[-1].addprevious(parse_xml(f'<w:p {nsdecls("w")}>{body}</w:p>'))
        doc.save(str(path))
        return path

    def test_big_operators_and_blackboard_letters(self) -> None:
        from core.pdf_word_symbols import tex_glyphs_to_unicode

        path = self._doc([
            ("LMRoman10", "Prove that "), ("LMMathExtension10", "P"), ("LMMathItalic10", "n k"),
            ("LMRoman10", " and "), ("CMEX10", "X Z"), ("LMRoman10", " on "), ("ABCDEF+MSBM10", "R N"),
            ("LMRoman10", " PXZ stays"),
        ])
        self.assertEqual(tex_glyphs_to_unicode(path), 3)
        runs = _runs(path)
        self.assertEqual([text for text, _ in runs],
                         ["Prove that ", "∑", "n k", " and ", "∑ ∫", " on ", "ℝ ℕ", " PXZ stays"])
        self.assertEqual(runs[1][1].get(qn("w:ascii")), MATH_FONT)
        self.assertEqual(runs[7][1].get(qn("w:ascii")), "LMRoman10")  # 正文里的字母不动

    def test_tall_delimiter_pieces_become_one_character(self) -> None:
        from core.pdf_word_symbols import tex_glyphs_to_unicode

        # 拼高的大括号：上段 { + 中段 + 下段 + 竖线段，只留一个 {
        path = self._doc([("CMEX10", "8&lt;:&gt;")])
        tex_glyphs_to_unicode(path)
        self.assertEqual(_runs(path)[0][0], "{")
