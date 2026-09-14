"""Word translation-only output keeps the replace-only contract."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from docx import Document

from core.word_coverage import build_word_coverage_plan, write_untranslated_docx
from core.word_document import write_bilingual_docx
from settings import WordOutputSettings


class WordTranslationOnlyTests(unittest.TestCase):
    def _source(self, root: Path) -> Path:
        doc = Document()
        doc.add_paragraph("正文原文")
        table = doc.add_table(rows=1, cols=1)
        table.cell(0, 0).text = "表格原文"
        section = doc.sections[0]
        section.header.paragraphs[0].text = "页眉原文"
        section.footer.paragraphs[0].text = "页脚原文"
        path = root / "source.docx"
        doc.save(path)
        return path

    def test_full_write_replaces_body_table_header_footer_and_default_is_bilingual(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            source = self._source(root)
            translations = {
                "正文原文": "Body translation",
                "表格原文": "Table translation",
                "页眉原文": "Header translation",
                "页脚原文": "Footer translation",
            }
            only = write_bilingual_docx(
                source_path=source,
                output_dir=root / "only",
                translations=translations,
                target_lang="en",
                translate_headers_footers=True,
                output_translation_only=True,
            )
            bilingual = write_bilingual_docx(
                source_path=source,
                output_dir=root / "bilingual",
                translations=translations,
                target_lang="en",
                translate_headers_footers=True,
            )
            self.assertEqual(Document(only).paragraphs[0].text, "Body translation")
            self.assertEqual(Document(only).tables[0].cell(0, 0).text, "Table translation")
            self.assertEqual(Document(only).sections[0].header.paragraphs[0].text, "Header translation")
            self.assertEqual(Document(only).sections[0].footer.paragraphs[0].text, "Footer translation")
            self.assertIn("正文原文", Document(bilingual).paragraphs[0].text)
            self.assertIn("Body translation", Document(bilingual).paragraphs[1].text)

    def test_coverage_write_replaces_source_only_positions(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            source = self._source(root)
            plan = build_word_coverage_plan(source, target_lang="en", source_lang="zh")
            output = write_untranslated_docx(
                source_path=source,
                output_dir=root / "coverage",
                plan=plan,
                translations={
                    "正文原文": "Body translation",
                    "表格原文": "Table translation",
                },
                target_lang="en",
                output_translation_only=True,
            )
            doc = Document(output)
            self.assertEqual(doc.paragraphs[0].text, "Body translation")
            self.assertEqual(doc.tables[0].cell(0, 0).text, "Table translation")

    def test_setting_defaults_to_bilingual(self) -> None:
        self.assertFalse(WordOutputSettings().output_translation_only)


if __name__ == "__main__":
    unittest.main(verbosity=2)
