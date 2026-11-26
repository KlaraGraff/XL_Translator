"""代码段（整段等宽字体）原样保留、不送翻（maple 2026-11-26 选 A：Word 和 PDF 转 Word 都保留）。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from docx import Document
from docx.enum.style import WD_STYLE_TYPE

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.translation_coverage import COVERAGE_IGNORED, COVERAGE_SOURCE_ONLY  # noqa: E402
from core.word_code import is_monospace_font  # noqa: E402
from core.word_coverage import build_word_coverage_plan  # noqa: E402
from core.word_document import extract_word_segments  # noqa: E402

CODE = "def push(self, item): self.items.append(item)"
PROSE = "A stack keeps the most recent item on top."


def _para(doc, *parts):
    """parts: (text, font or None)。"""
    paragraph = doc.add_paragraph()
    for text, font in parts:
        run = paragraph.add_run(text)
        if font:
            run.font.name = font
    return paragraph


def _sources(doc) -> set[str]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "doc.docx"
        doc.save(path)
        return {s.source for s in extract_word_segments(path, target_lang="zh", source_lang="en")}


class MonospaceFontTests(unittest.TestCase):
    def test_names(self) -> None:
        for name in ("Consolas", "Courier New", "Menlo", "LMMono10", "ABCDEF+LMMonoLt10-Regular", "CMTT10",
                     "Source Code Pro", "DejaVu Sans Mono", "JetBrains Mono"):
            self.assertTrue(is_monospace_font(name), name)
        for name in ("", "Calibri", "LMRoman10", "Monotype Corsiva", "Times New Roman", "Cambria Math"):
            self.assertFalse(is_monospace_font(name), name)


class CodeExtractionTests(unittest.TestCase):
    def test_whole_monospace_paragraph_is_not_sent(self) -> None:
        doc = Document()
        _para(doc, (PROSE, None))
        _para(doc, ("Use a list as the store.", None))
        _para(doc, (CODE, "Consolas"))
        _para(doc, ("print(", "Courier New"), ("stack.pop()", "Courier New"), (")", "Courier New"))
        mixed = "Call collections.deque for a faster queue."
        _para(doc, ("Call ", None), ("collections.deque", "Consolas"), (" for a faster queue.", None))
        sources = _sources(doc)
        self.assertNotIn(CODE, sources)
        self.assertNotIn("print(stack.pop())", sources)
        self.assertIn(PROSE, sources)
        self.assertIn(mixed, sources)

    def test_code_style_is_followed(self) -> None:
        doc = Document()
        style = doc.styles.add_style("Code Block", WD_STYLE_TYPE.PARAGRAPH)
        style.font.name = "Menlo"
        _para(doc, (PROSE, None))
        _para(doc, ("More words in a normal sentence here.", None))
        doc.add_paragraph(CODE, style="Code Block")
        self.assertNotIn(CODE, _sources(doc))

    def test_table_cells(self) -> None:
        doc = Document()
        _para(doc, (PROSE, None))
        table = doc.add_table(rows=1, cols=2)
        command, meaning = table.rows[0].cells
        command.paragraphs[0].add_run("git status --short").font.name = "LMMono10"
        meaning.paragraphs[0].add_run("show changed files")
        sources = _sources(doc)
        self.assertNotIn("git status --short", sources)
        self.assertIn("show changed files", sources)

    def test_same_text_as_prose_elsewhere_is_translated(self) -> None:
        doc = Document()
        _para(doc, (PROSE, None))
        _para(doc, ("Return the top item.", "Consolas"))
        _para(doc, ("Return the top item.", None))
        self.assertIn("Return the top item.", _sources(doc))

    def test_all_monospace_document_is_translated(self) -> None:
        # 老式打字稿整篇 Courier：分不出代码，照翻。
        doc = Document()
        _para(doc, (PROSE, "Courier New"))
        _para(doc, ("Use a list as the store.", "Courier New"))
        self.assertIn(PROSE, _sources(doc))


class CodeCoverageTests(unittest.TestCase):
    def test_code_left_as_is_is_not_reported_untranslated(self) -> None:
        doc = Document()
        _para(doc, ("栈把最近放进去的元素放在最上面。", None))
        _para(doc, (CODE, "Consolas"))
        _para(doc, (PROSE, None))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.docx"
            doc.save(path)
            plan = build_word_coverage_plan(path, target_lang="zh", source_lang="en")
        status = {unit.source_text: unit.status for unit in plan.units if unit.source_text}
        self.assertEqual(status[CODE], COVERAGE_IGNORED)
        self.assertEqual(status[PROSE], COVERAGE_SOURCE_ONLY)


if __name__ == "__main__":
    unittest.main(verbosity=2)
