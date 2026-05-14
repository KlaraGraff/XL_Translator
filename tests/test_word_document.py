from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from core.translation_filter import is_translation_redundant
from core.word_document import (
    extract_word_segments,
    scan_word_path,
    write_bilingual_docx,
)
from core.word_task_runner import _needs_word_translation_retry
from core.word_task_runner import _build_source_excerpt, _write_word_quality_report


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

    def test_word_automatic_numbering_is_flattened_in_bilingual_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source_path = temp_path / "numbered.docx"
            output_dir = temp_path / "out"
            self._build_lower_letter_numbered_docx(source_path)

            segments = extract_word_segments(
                source_path,
                target_lang="fr",
                source_lang="zh",
            )
            sources = {segment.source for segment in segments}
            self.assertIn("包括但不限于根据合同约定或确定的款项；", sources)
            self.assertNotIn("a. 包括但不限于根据合同约定或确定的款项；", sources)

            out_path = write_bilingual_docx(
                source_path=source_path,
                output_dir=output_dir,
                translations={
                    "包括但不限于根据合同约定或确定的款项；": "y compris les montants prévus au contrat ;",
                    "乙方未履行整改通知规定的违约补救义务；": (
                        "La partie B n'a pas rempli les obligations de remédiation ;"
                    ),
                },
                target_lang="fr",
                source_lang="zh",
            )

            out_doc = Document(str(out_path))
            paragraph_texts = [paragraph.text for paragraph in out_doc.paragraphs]
            self.assertIn("a. 包括但不限于根据合同约定或确定的款项；", paragraph_texts)
            self.assertIn("a. y compris les montants prévus au contrat ;", paragraph_texts)
            self.assertIn("b. 乙方未履行整改通知规定的违约补救义务；", paragraph_texts)
            self.assertIn(
                "b. La partie B n'a pas rempli les obligations de remédiation ;",
                paragraph_texts,
            )

            for paragraph in out_doc.paragraphs[:4]:
                self.assertEqual(self._paragraph_num_id(paragraph), "0")

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

    def test_word_retry_only_targets_unresolved_chinese_sources(self) -> None:
        self.assertTrue(
            _needs_word_translation_retry(
                "2、增设墙体厚度300mm",
                "2、增设墙体厚度300mm",
                source_lang="zh",
            )
        )
        self.assertTrue(
            _needs_word_translation_retry(
                "2、增设墙体厚度300mm",
                "",
                source_lang="zh",
            )
        )
        self.assertFalse(
            _needs_word_translation_retry(
                "Plan de construction pour la saison des pluies",
                "",
                source_lang="zh",
            )
        )
        self.assertFalse(
            _needs_word_translation_retry(
                "2、增设墙体厚度300mm",
                "2. Le mur ajouté aura une épaisseur de 300 mm.",
                source_lang="zh",
                target_lang="fr",
            )
        )
        self.assertTrue(
            _needs_word_translation_retry(
                "2、增设墙体厚度300mm",
                "2. Le mur ajouté 增设墙体 épaisseur 300 mm.",
                source_lang="zh",
                target_lang="fr",
            )
        )

    def test_french_decimal_commas_pass_number_integrity_check(self) -> None:
        source = (
            "2、增设墙体厚度300mm，宽度8.925米，高度1.7米，"
            "底部标高-0.800米，顶部标高0.900米，预留200mm厚B30混凝土。"
        )
        translated = (
            "2. Ajouter un mur d'une épaisseur de 300 mm, largeur 8,925 m, "
            "hauteur 1,7 m, cote inférieure -0,800 m, cote supérieure 0,900 m, "
            "avec une réservation de 200 mm en béton B30."
        )

        self.assertFalse(
            is_translation_redundant(
                source,
                translated,
                target_lang="fr",
                source_lang="zh",
            )
        )

    def test_word_segments_keep_section_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = Path(temp_dir) / "sections.docx"
            doc = Document()
            doc.add_heading("一、工程概况", level=1)
            doc.add_paragraph("施工范围：增设墙体。")
            doc.add_paragraph("（一）材料要求")
            doc.add_paragraph("混凝土强度等级为B30。")
            doc.save(str(source_path))

            segments = extract_word_segments(
                source_path,
                target_lang="fr",
                source_lang="zh",
            )
            paths = {segment.source: segment.section_path for segment in segments}

            self.assertEqual(paths["施工范围：增设墙体。"], "一、工程概况")
            self.assertEqual(paths["混凝土强度等级为B30。"], "一、工程概况 / （一）材料要求")

    def test_word_quality_report_records_location_and_excerpt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            source = "这是一个较长的施工段落，用于验证报告会记录开头和结尾，方便用户定位。"
            issue = {
                "file": "方案",
                "section_path": "一、工程概况 / （一）材料要求",
                "location_label": "正文段落 8",
                "snippet": _build_source_excerpt(source, head=10, tail=8),
                "problem": "初次翻译未获得有效译文",
                "status": "已自动单段重试并恢复译文。",
                "severity": "resolved",
            }

            report_path = _write_word_quality_report(
                output_dir=output_dir,
                file_results=[{"name": "方案", "success": True}],
                issues=[issue],
                elapsed_sec=1.25,
                tm_hit_count=2,
                api_call_count=3,
            )

            self.assertIsNotNone(report_path)
            content = report_path.read_text(encoding="utf-8")
            self.assertIn("已自动处理", content)
            self.assertIn("一、工程概况 / （一）材料要求", content)
            self.assertIn("正文段落 8", content)
            self.assertIn(issue["snippet"], content)

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

    @staticmethod
    def _build_lower_letter_numbered_docx(path: Path) -> None:
        doc = Document()
        WordDocumentTests._set_default_num_id_9_to_lower_letter(doc)
        first = doc.add_paragraph("包括但不限于根据合同约定或确定的款项；")
        second = doc.add_paragraph("乙方未履行整改通知规定的违约补救义务；")
        for paragraph in (first, second):
            WordDocumentTests._set_paragraph_num_pr(paragraph, num_id="9", ilvl="0")
        doc.save(str(path))

    @staticmethod
    def _set_default_num_id_9_to_lower_letter(doc: Document) -> None:
        numbering_root = doc.part.numbering_part.element
        target_abstract_id = None
        for num in numbering_root.findall(qn("w:num")):
            if num.get(qn("w:numId")) == "9":
                abstract_id = num.find(qn("w:abstractNumId"))
                target_abstract_id = abstract_id.get(qn("w:val")) if abstract_id is not None else None
                break
        if target_abstract_id is None:
            return
        for abstract_num in numbering_root.findall(qn("w:abstractNum")):
            if abstract_num.get(qn("w:abstractNumId")) != target_abstract_id:
                continue
            level = abstract_num.find(qn("w:lvl"))
            if level is None:
                return
            num_format = level.find(qn("w:numFmt"))
            if num_format is None:
                num_format = OxmlElement("w:numFmt")
                level.append(num_format)
            num_format.set(qn("w:val"), "lowerLetter")
            level_text = level.find(qn("w:lvlText"))
            if level_text is None:
                level_text = OxmlElement("w:lvlText")
                level.append(level_text)
            level_text.set(qn("w:val"), "%1.")

    @staticmethod
    def _set_paragraph_num_pr(paragraph, *, num_id: str, ilvl: str) -> None:
        p_pr = paragraph._p.get_or_add_pPr()
        num_pr = OxmlElement("w:numPr")
        ilvl_element = OxmlElement("w:ilvl")
        ilvl_element.set(qn("w:val"), ilvl)
        num_id_element = OxmlElement("w:numId")
        num_id_element.set(qn("w:val"), num_id)
        num_pr.append(ilvl_element)
        num_pr.append(num_id_element)
        p_pr.append(num_pr)

    @staticmethod
    def _paragraph_num_id(paragraph) -> str:
        p_pr = getattr(paragraph._p, "pPr", None)
        if p_pr is None:
            return ""
        num_pr = p_pr.find(qn("w:numPr"))
        if num_pr is None:
            return ""
        num_id = num_pr.find(qn("w:numId"))
        return num_id.get(qn("w:val")) if num_id is not None else ""


if __name__ == "__main__":
    unittest.main(verbosity=2)
