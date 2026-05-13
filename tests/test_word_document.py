from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from docx import Document

from core.word_document import (
    extract_word_segments,
    scan_word_path,
    write_bilingual_docx,
)


class WordDocumentTests(unittest.TestCase):
    def test_extract_and_write_bilingual_docx(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_path = temp_path / "sample.docx"
            output_dir = temp_path / "out"
            self._build_sample_docx(source_path)

            segments = extract_word_segments(
                source_path,
                target_lang="en",
                source_lang="zh",
            )
            sources = {segment.source for segment in segments}

            self.assertIn("项目名称：测试工程", sources)
            self.assertIn("设备\n安装", sources)
            self.assertNotIn("12345", sources)

            out_path = write_bilingual_docx(
                source_path=source_path,
                output_dir=output_dir,
                translations={
                    "项目名称：测试工程": "Project name: Test Project",
                    "设备\n安装": "Equipment installation",
                },
                target_lang="en",
                source_lang="zh",
            )

            out_doc = Document(str(out_path))
            paragraph_texts = [paragraph.text for paragraph in out_doc.paragraphs]
            self.assertIn("项目名称：测试工程", paragraph_texts)
            self.assertIn("Project name: Test Project", paragraph_texts)

            cell_text = out_doc.tables[0].cell(0, 0).text
            self.assertEqual(cell_text, "设备\n安装\nEquipment installation")

    def test_scan_word_path_ignores_temp_and_generated_output_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "source.docx"
            temp_docx = root / "~$source.docx"
            output_dir = root / "source_翻译输出_20260513_120000"
            generated_path = output_dir / "generated.docx"
            output_dir.mkdir()

            self._build_sample_docx(source_path)
            self._build_sample_docx(generated_path)
            temp_docx.write_text("not a real docx", encoding="utf-8")

            items = scan_word_path(root)

            self.assertEqual([item.path for item in items], [source_path])

    @staticmethod
    def _build_sample_docx(path: Path) -> None:
        doc = Document()
        doc.add_paragraph("项目名称：测试工程")
        doc.add_paragraph("12345")
        table = doc.add_table(rows=1, cols=1)
        cell = table.cell(0, 0)
        cell.text = "设备"
        cell.add_paragraph("安装")
        doc.save(str(path))


if __name__ == "__main__":
    unittest.main(verbosity=2)
