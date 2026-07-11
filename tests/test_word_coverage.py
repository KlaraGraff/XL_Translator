from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from docx import Document

from core.translation_coverage import (
    COVERAGE_COVERED,
    COVERAGE_IGNORED,
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

    def test_scheme_cover_protects_cover_but_translates_foreign_title_exception(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "固化地面专项施工方案.docx"
            doc = Document()
            doc.add_paragraph("贝特瑞地中海负极项目")
            doc.add_paragraph("固化地面专项施工方案")
            doc.add_paragraph("Specific construction plan for hardened floors")
            doc.add_paragraph("编制 PREPARED BY")
            table = doc.add_table(rows=2, cols=3)
            table.cell(0, 0).text = "文件编号 DOCNO."
            table.cell(0, 2).text = "ONEBTR-MS-035 Rev_A"
            table.cell(1, 0).text = "编制 PREPARED BY"
            doc.add_paragraph("第一章 编制依据")
            doc.add_paragraph("施工内容")
            doc.save(source)

            plan = build_word_coverage_plan(
                source,
                target_lang="fr",
                source_lang="zh",
                protect_scheme_cover=True,
            )
            by_location = {unit.location: unit for unit in plan.units}

            self.assertEqual(by_location["body.paragraph[0]"].status, COVERAGE_IGNORED)
            self.assertEqual(by_location["body.paragraph[1]"].status, COVERAGE_IGNORED)
            self.assertEqual(by_location["body.paragraph[2]"].status, COVERAGE_SOURCE_ONLY)
            self.assertEqual(by_location["body.paragraph[3]"].status, COVERAGE_IGNORED)
            self.assertEqual(by_location["body.paragraph[4]"].status, COVERAGE_SOURCE_ONLY)
            self.assertEqual(by_location["body.paragraph[5]"].status, COVERAGE_SOURCE_ONLY)
            table_units = [
                unit for unit in plan.units if unit.location.startswith("table[0].")
            ]
            self.assertTrue(table_units)
            self.assertTrue(all(unit.status == COVERAGE_IGNORED for unit in table_units))
            self.assertEqual(
                plan.source_texts,
                [
                    "Specific construction plan for hardened floors",
                    "第一章 编制依据",
                    "施工内容",
                ],
            )

    def test_scheme_cover_foreign_title_with_existing_translation_is_covered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "固化地面专项施工方案.docx"
            doc = Document()
            doc.add_paragraph("固化地面专项施工方案")
            doc.add_paragraph("Specific construction plan for hardened floors")
            doc.add_paragraph("Plan d'exécution spécifique pour sols durcis")
            doc.add_paragraph("第一章 编制依据")
            doc.save(source)

            plan = build_word_coverage_plan(
                source,
                target_lang="fr",
                source_lang="zh",
                protect_scheme_cover=True,
            )
            by_location = {unit.location: unit for unit in plan.units}

            self.assertEqual(by_location["body.paragraph[1]"].status, COVERAGE_COVERED)
            self.assertEqual(
                by_location["body.paragraph[1]"].target_text,
                "Plan d'exécution spécifique pour sols durcis",
            )

    def test_scheme_filename_without_cover_title_does_not_hide_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "施工方案修订说明.docx"
            doc = Document()
            doc.add_paragraph("项目背景")
            doc.add_paragraph("施工内容")
            doc.add_paragraph("1.1 作业范围")
            doc.save(source)

            plan = build_word_coverage_plan(
                source,
                target_lang="fr",
                source_lang="zh",
                protect_scheme_cover=True,
            )

            self.assertEqual(
                [unit.source_text for unit in plan.source_units],
                ["项目背景", "施工内容", "1.1 作业范围"],
            )

if __name__ == "__main__":
    unittest.main(verbosity=2)
