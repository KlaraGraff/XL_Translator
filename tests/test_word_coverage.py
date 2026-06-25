from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from docx import Document

from core.translation_coverage import (
    COVERAGE_COVERED,
    COVERAGE_SOURCE_ONLY,
)
from core.word_coverage import (
    build_word_coverage_plan,
    write_untranslated_docx,
)


class WordCoverageTests(unittest.TestCase):
    def test_adjacent_paragraph_covered_and_source_only_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.docx"
            doc = Document()
            doc.add_paragraph("项目名称")
            doc.add_paragraph("Project name")
            doc.add_paragraph("施工内容")
            table = doc.add_table(rows=1, cols=1)
            table.cell(0, 0).text = "设备安装"
            doc.save(source)

            plan = build_word_coverage_plan(source, target_lang="en", source_lang="zh")
            by_location = {unit.location: unit for unit in plan.units}

            self.assertEqual(by_location["body.paragraph[0]"].status, COVERAGE_COVERED)
            self.assertEqual(by_location["body.paragraph[2]"].status, COVERAGE_SOURCE_ONLY)
            self.assertEqual(by_location["table[0].cell[0]"].status, COVERAGE_SOURCE_ONLY)
            self.assertEqual(plan.source_texts, ["施工内容", "设备安装"])

            out_path = write_untranslated_docx(
                source_path=source,
                output_dir=Path(tmp) / "out",
                plan=plan,
                translations={
                    "施工内容": "Construction scope",
                    "设备安装": "Equipment installation",
                },
                target_lang="en",
                source_lang="zh",
            )

            out_doc = Document(str(out_path))
            self.assertEqual(
                [paragraph.text for paragraph in out_doc.paragraphs],
                ["项目名称", "Project name", "施工内容", "Construction scope"],
            )
            self.assertEqual(
                out_doc.tables[0].cell(0, 0).text,
                "设备安装\nEquipment installation",
            )

            second_plan = build_word_coverage_plan(
                out_path,
                target_lang="en",
                source_lang="zh",
            )
            self.assertEqual(second_plan.source_units, [])

    def test_duplicate_source_text_only_patches_untranslated_position(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.docx"
            doc = Document()
            doc.add_paragraph("项目名称")
            doc.add_paragraph("Project name")
            doc.add_paragraph("项目名称")
            doc.save(source)

            plan = build_word_coverage_plan(source, target_lang="en", source_lang="zh")

            self.assertEqual(len(plan.source_units), 1)
            self.assertEqual(plan.source_units[0].location, "body.paragraph[2]")

            out_path = write_untranslated_docx(
                source_path=source,
                output_dir=Path(tmp) / "out",
                plan=plan,
                translations={"项目名称": "Project name"},
                target_lang="en",
                source_lang="zh",
            )

            out_doc = Document(str(out_path))
            self.assertEqual(
                [paragraph.text for paragraph in out_doc.paragraphs],
                ["项目名称", "Project name", "项目名称", "Project name"],
            )

    def test_table_unit_line_is_not_misclassified_as_target_translation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "units.docx"
            doc = Document()
            table = doc.add_table(rows=1, cols=2)
            table.cell(0, 0).text = "水泥\n(kg/m³)"
            table.cell(0, 1).text = "砂子\nsable\n(kg/m³)"
            doc.save(source)

            plan = build_word_coverage_plan(source, target_lang="fr", source_lang="zh")
            by_location = {unit.location: unit for unit in plan.units}

            self.assertEqual(by_location["table[0].cell[0]"].status, COVERAGE_SOURCE_ONLY)
            self.assertEqual(by_location["table[0].cell[1]"].status, COVERAGE_COVERED)


if __name__ == "__main__":
    unittest.main(verbosity=2)
