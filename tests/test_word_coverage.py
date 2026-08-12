from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from core.translation_coverage import (
    COVERAGE_COVERED,
    COVERAGE_IGNORED,
    COVERAGE_SOURCE_ONLY,
)
from core.word_coverage import (
    build_word_coverage_plan,
    write_untranslated_docx,
)


def _append_auto_toc_field(paragraph) -> None:
    """把一个空跑（run）标记为 Word 自动生成的 TOC 域，模拟 Insert TOC 之后的段落。"""
    paragraph.add_run()
    field_start = OxmlElement("w:fldChar")
    field_start.set(qn("w:fldCharType"), "begin")
    instruction = OxmlElement("w:instrText")
    instruction.set(qn("xml:space"), "preserve")
    instruction.text = ' TOC \\o "1-3" '
    field_end = OxmlElement("w:fldChar")
    field_end.set(qn("w:fldCharType"), "end")
    paragraph._p.append(field_start)
    paragraph._p.append(instruction)
    paragraph._p.append(field_end)


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

    def test_front_matter_protects_cover_and_auto_toc_before_first_chapter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "封面加自动目录.docx"
            doc = Document()
            doc.add_paragraph("某某工程施工方案")
            doc.add_paragraph("编制单位：某某公司")
            _append_auto_toc_field(doc.add_paragraph())
            doc.add_paragraph("第一章 工程概况")
            doc.add_paragraph("施工内容")
            doc.save(source)

            plan = build_word_coverage_plan(
                source,
                target_lang="fr",
                source_lang="zh",
                protect_front_matter=True,
            )

            self.assertTrue(plan.front_matter.found)
            self.assertEqual(plan.front_matter.heading_text, "第一章 工程概况")
            self.assertEqual(plan.front_matter.protected_paragraph_count, 2)
            by_location = {unit.location: unit for unit in plan.units}
            self.assertEqual(by_location["body.paragraph[0]"].status, COVERAGE_IGNORED)
            self.assertEqual(by_location["body.paragraph[1]"].status, COVERAGE_IGNORED)
            # 域段落本身没有可见文字，_classify_body_paragraphs 从不为空段落生成 unit，
            # 这里不用断言 paragraph[2]；重点是它前后的边界判断没有被打乱。
            self.assertEqual(by_location["body.paragraph[3]"].status, COVERAGE_SOURCE_ONLY)
            self.assertEqual(by_location["body.paragraph[4]"].status, COVERAGE_SOURCE_ONLY)

    def test_front_matter_hand_typed_toc_leader_is_not_mistaken_for_body_heading(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "封面加手打目录.docx"
            doc = Document()
            doc.add_paragraph("某某工程施工方案")
            # 手打目录条目本身以"第一章"开头——这正是 guardrail A 要防的假阳性：
            # 不带这条正则的话，扫描会在这里就误判成正文标题，导致目录本身反而被当正文翻译。
            doc.add_paragraph("第一章 工程概况 ...... 1")
            doc.add_paragraph("第二章 施工部署 ...... 5")
            doc.add_paragraph("第一章 工程概况")
            doc.add_paragraph("施工内容")
            doc.save(source)

            plan = build_word_coverage_plan(
                source,
                target_lang="fr",
                source_lang="zh",
                protect_front_matter=True,
            )

            self.assertTrue(plan.front_matter.found)
            self.assertEqual(plan.front_matter.heading_text, "第一章 工程概况")
            self.assertEqual(plan.front_matter.protected_paragraph_count, 3)
            by_location = {unit.location: unit for unit in plan.units}
            self.assertEqual(by_location["body.paragraph[0]"].status, COVERAGE_IGNORED)
            self.assertEqual(by_location["body.paragraph[1]"].status, COVERAGE_IGNORED)
            self.assertEqual(by_location["body.paragraph[2]"].status, COVERAGE_IGNORED)
            self.assertEqual(by_location["body.paragraph[3]"].status, COVERAGE_SOURCE_ONLY)
            self.assertEqual(by_location["body.paragraph[4]"].status, COVERAGE_SOURCE_ONLY)

    def test_front_matter_bare_numeric_heading_without_decimal_point(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "无点号标题.docx"
            doc = Document()
            doc.add_paragraph("某某工程施工方案")
            doc.add_paragraph("1 概述")
            doc.add_paragraph("施工内容")
            doc.save(source)

            plan = build_word_coverage_plan(
                source,
                target_lang="fr",
                source_lang="zh",
                protect_front_matter=True,
            )

            self.assertTrue(plan.front_matter.found)
            self.assertEqual(plan.front_matter.heading_text, "1 概述")
            by_location = {unit.location: unit for unit in plan.units}
            self.assertEqual(by_location["body.paragraph[0]"].status, COVERAGE_IGNORED)
            self.assertEqual(by_location["body.paragraph[1]"].status, COVERAGE_SOURCE_ONLY)
            self.assertEqual(by_location["body.paragraph[2]"].status, COVERAGE_SOURCE_ONLY)

    def test_front_matter_word_heading_style_without_recognizable_text_pattern(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "样式标题.docx"
            doc = Document()
            doc.add_paragraph("某某工程施工方案")
            # Word 内置"标题 1"样式，但文字本身既不带章节前缀也不带数字——只能靠样式识别。
            doc.add_heading("工程概况说明", level=1)
            doc.add_paragraph("施工内容")
            doc.save(source)

            plan = build_word_coverage_plan(
                source,
                target_lang="fr",
                source_lang="zh",
                protect_front_matter=True,
            )

            self.assertTrue(plan.front_matter.found)
            self.assertEqual(plan.front_matter.heading_text, "工程概况说明")
            by_location = {unit.location: unit for unit in plan.units}
            self.assertEqual(by_location["body.paragraph[0]"].status, COVERAGE_IGNORED)
            self.assertEqual(by_location["body.paragraph[2]"].status, COVERAGE_SOURCE_ONLY)

    def test_front_matter_protects_nothing_when_no_heading_is_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "无标题文档.docx"
            doc = Document()
            doc.add_paragraph("项目背景")
            doc.add_paragraph("施工内容")
            doc.add_paragraph("验收标准")
            doc.save(source)

            plan = build_word_coverage_plan(
                source,
                target_lang="fr",
                source_lang="zh",
                protect_front_matter=True,
            )

            # 找不到正文标题时绝不能把整份文档当封面吞掉：断言"没保护任何内容"。
            self.assertFalse(plan.front_matter.found)
            self.assertEqual(plan.front_matter.protected_paragraph_count, 0)
            self.assertEqual(
                [unit.source_text for unit in plan.source_units],
                ["项目背景", "施工内容", "验收标准"],
            )

    def test_front_matter_protects_cover_table_before_first_chapter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "表格封面.docx"
            doc = Document()
            cover_table = doc.add_table(rows=2, cols=2)
            cover_table.cell(0, 0).text = "文件编号"
            cover_table.cell(0, 1).text = "ONEBTR-MS-035"
            cover_table.cell(1, 0).text = "编制人"
            cover_table.cell(1, 1).text = "张三"
            doc.add_paragraph("第一章 工程概况")
            body_table = doc.add_table(rows=1, cols=1)
            body_table.cell(0, 0).text = "工程量清单"
            doc.save(source)

            plan = build_word_coverage_plan(
                source,
                target_lang="fr",
                source_lang="zh",
                protect_front_matter=True,
            )

            self.assertTrue(plan.front_matter.found)
            cover_units = [unit for unit in plan.units if unit.location.startswith("table[0].")]
            body_table_units = [unit for unit in plan.units if unit.location.startswith("table[1].")]
            self.assertTrue(cover_units)
            self.assertTrue(all(unit.status == COVERAGE_IGNORED for unit in cover_units))
            self.assertTrue(body_table_units)
            self.assertTrue(all(unit.status == COVERAGE_SOURCE_ONLY for unit in body_table_units))

    def test_translation_carrying_a_numbering_prefix_still_counts_as_covered(self) -> None:
        """译文开头留着中文编号（一.1）不能让整对段落被判成未译源文。"""
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "编号前缀.docx"
            doc = Document()
            doc.add_paragraph("一.1 抢工背景分析")
            doc.add_paragraph(
                "一.1 Analyse du contexte de l’accélération des travaux"
            )
            doc.save(source)

            plan = build_word_coverage_plan(source, target_lang="fr", source_lang="zh")
            by_location = {unit.location: unit for unit in plan.units}

            self.assertEqual(by_location["body.paragraph[0]"].status, COVERAGE_COVERED)
            self.assertEqual(plan.source_units, [])
            self.assertEqual(len(plan.residual_units), 1)
            self.assertEqual(plan.residual_units[0].data["residual_cjk"], ["一"])
            self.assertEqual(
                plan.residual_units[0].data["residual_location"], "body.paragraph[1]"
            )

    def test_untranslated_chinese_paragraph_is_still_reported(self) -> None:
        """放宽判定不能把真正没翻的中文段落也放过去。"""
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "漏译.docx"
            doc = Document()
            doc.add_paragraph("Analyse du contexte de l’accélération des travaux")
            doc.add_paragraph("夜间施工必须配备足够的照明设备，并落实专人值守。")
            doc.save(source)

            plan = build_word_coverage_plan(source, target_lang="fr", source_lang="zh")
            by_location = {unit.location: unit for unit in plan.units}

            self.assertEqual(
                by_location["body.paragraph[1]"].status, COVERAGE_SOURCE_ONLY
            )
            self.assertEqual(plan.residual_units, [])

    def test_front_matter_disabled_by_default_leaves_cover_translatable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "默认不保护.docx"
            doc = Document()
            doc.add_paragraph("某某工程施工方案")
            doc.add_paragraph("第一章 工程概况")
            doc.save(source)

            plan = build_word_coverage_plan(source, target_lang="fr", source_lang="zh")

            self.assertFalse(plan.front_matter.found)
            by_location = {unit.location: unit for unit in plan.units}
            self.assertEqual(by_location["body.paragraph[0]"].status, COVERAGE_SOURCE_ONLY)


if __name__ == "__main__":
    unittest.main(verbosity=2)
