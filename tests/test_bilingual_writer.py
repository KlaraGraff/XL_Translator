from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill

from core.bilingual_writer import bilingual_output_name, build_output_dir, write_bilingual_file
from core.mixed_language import (
    MIXED_COLOR_FOREIGN_NOISE,
    MIXED_COLOR_UNRESOLVED,
    MIXED_MARK_FOREIGN_NOISE,
    MIXED_MARK_UNRESOLVED,
)


class BilingualWriterTests(unittest.TestCase):
    def test_excel_review_mark_fills_unfilled_cell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = self._workbook(Path(tmp), "source.xlsx", "项目")
            out_path = write_bilingual_file(
                source_path=source,
                output_dir=Path(tmp) / "out",
                translations={"项目": "Projet"},
                target_lang="fr",
                keep_original_sheets=False,
                formula_display_value_backfill=True,
                review_marks={"项目": MIXED_MARK_FOREIGN_NOISE},
            )

            wb = load_workbook(out_path)
            try:
                cell = wb.active["A1"]
                self.assertEqual(cell.value, "项目\nProjet")
                self.assertTrue(str(cell.fill.fgColor.rgb).endswith(MIXED_COLOR_FOREIGN_NOISE))
            finally:
                wb.close()

    def test_excel_review_mark_uses_configured_color_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = self._workbook(Path(tmp), "source.xlsx", "项目")
            out_path = write_bilingual_file(
                source_path=source,
                output_dir=Path(tmp) / "out",
                translations={"项目": "Projet"},
                target_lang="fr",
                keep_original_sheets=False,
                formula_display_value_backfill=True,
                review_marks={"项目": MIXED_MARK_UNRESOLVED},
                review_mark_colors={MIXED_MARK_UNRESOLVED: "DDEBFF"},
            )

            wb = load_workbook(out_path)
            try:
                cell = wb.active["A1"]
                self.assertEqual(cell.value, "项目\nProjet")
                self.assertTrue(str(cell.fill.fgColor.rgb).endswith("DDEBFF"))
            finally:
                wb.close()

    def test_excel_review_mark_uses_red_font_when_existing_fill_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = self._workbook(
                Path(tmp),
                "source.xlsx",
                "项目",
                fill_color="92D050",
                font_color="0000FF",
            )
            out_path = write_bilingual_file(
                source_path=source,
                output_dir=Path(tmp) / "out",
                translations={"项目": "Projet"},
                target_lang="fr",
                keep_original_sheets=False,
                formula_display_value_backfill=True,
                review_marks={"项目": MIXED_MARK_UNRESOLVED},
                existing_fill_policy="red_font",
            )

            wb = load_workbook(out_path)
            try:
                cell = wb.active["A1"]
                self.assertTrue(str(cell.fill.fgColor.rgb).endswith("92D050"))
                self.assertTrue(str(cell.font.color.rgb).endswith("FF0000"))
            finally:
                wb.close()

    def test_excel_review_mark_can_skip_existing_fill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = self._workbook(
                Path(tmp),
                "source.xlsx",
                "项目",
                fill_color="92D050",
                font_color="0000FF",
            )
            out_path = write_bilingual_file(
                source_path=source,
                output_dir=Path(tmp) / "out",
                translations={"项目": "Projet"},
                target_lang="fr",
                keep_original_sheets=False,
                formula_display_value_backfill=True,
                review_marks={"项目": MIXED_MARK_UNRESOLVED},
                existing_fill_policy="skip",
            )

            wb = load_workbook(out_path)
            try:
                cell = wb.active["A1"]
                self.assertTrue(str(cell.fill.fgColor.rgb).endswith("92D050"))
                self.assertTrue(str(cell.font.color.rgb).endswith("0000FF"))
            finally:
                wb.close()

    def test_excel_retained_original_is_marked_as_unresolved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = self._workbook(Path(tmp), "source.xlsx", "项目")
            out_path = write_bilingual_file(
                source_path=source,
                output_dir=Path(tmp) / "out",
                translations={"项目": "项目"},
                target_lang="fr",
                keep_original_sheets=False,
                formula_display_value_backfill=True,
            )

            wb = load_workbook(out_path)
            try:
                cell = wb.active["A1"]
                self.assertEqual(cell.value, "项目")
                self.assertTrue(str(cell.fill.fgColor.rgb).endswith(MIXED_COLOR_UNRESOLVED))
            finally:
                wb.close()

    def test_excel_review_mark_can_be_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = self._workbook(Path(tmp), "source.xlsx", "项目")
            out_path = write_bilingual_file(
                source_path=source,
                output_dir=Path(tmp) / "out",
                translations={"项目": "Projet"},
                target_lang="fr",
                keep_original_sheets=False,
                formula_display_value_backfill=True,
                review_marks={"项目": MIXED_MARK_FOREIGN_NOISE},
                mark_review_items=False,
            )

            wb = load_workbook(out_path)
            try:
                cell = wb.active["A1"]
                self.assertEqual(cell.value, "项目\nProjet")
                self.assertEqual(str(cell.fill.fgColor.rgb), "00000000")
            finally:
                wb.close()

    def test_excel_disabled_review_mark_skips_retained_original_auto_mark(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = self._workbook(Path(tmp), "source.xlsx", "项目")
            out_path = write_bilingual_file(
                source_path=source,
                output_dir=Path(tmp) / "out",
                translations={"项目": "项目"},
                target_lang="fr",
                keep_original_sheets=False,
                formula_display_value_backfill=True,
                mark_review_items=False,
            )

            wb = load_workbook(out_path)
            try:
                cell = wb.active["A1"]
                self.assertEqual(cell.value, "项目")
                self.assertEqual(str(cell.fill.fgColor.rgb), "00000000")
            finally:
                wb.close()

    def test_plain_text_containing_dispimg_is_not_erased(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = self._workbook(Path(tmp), "source.xlsx", "DISPIMG operating note")
            out_path = write_bilingual_file(
                source_path=source,
                output_dir=Path(tmp) / "out",
                translations={"DISPIMG operating note": "Note de fonctionnement DISPIMG"},
                target_lang="fr",
                source_lang="en",
                keep_original_sheets=False,
                formula_display_value_backfill=True,
            )

            wb = load_workbook(out_path)
            try:
                self.assertEqual(
                    wb.active["A1"].value,
                    "DISPIMG operating note\nNote de fonctionnement DISPIMG",
                )
            finally:
                wb.close()

    def test_wps_dispimg_formula_is_preserved(self) -> None:
        # 补丁式写入器保留 WPS 嵌入图片公式，图片不再随翻译丢失。
        with tempfile.TemporaryDirectory() as tmp:
            source = self._workbook(Path(tmp), "source.xlsx", '=DISPIMG("ID_1",1)')
            out_path = write_bilingual_file(
                source_path=source,
                output_dir=Path(tmp) / "out",
                translations={},
                target_lang="fr",
                keep_original_sheets=False,
                formula_display_value_backfill=False,
            )

            wb = load_workbook(out_path, data_only=False)
            try:
                self.assertEqual(wb.active["A1"].value, '=DISPIMG("ID_1",1)')
            finally:
                wb.close()

    @staticmethod
    def _workbook(
        root: Path,
        name: str,
        value: str,
        *,
        fill_color: str | None = None,
        font_color: str | None = None,
    ) -> Path:
        path = root / name
        wb = Workbook()
        ws = wb.active
        ws["A1"] = value
        if fill_color:
            ws["A1"].fill = PatternFill(fill_type="solid", fgColor=f"FF{fill_color}")
        if font_color:
            ws["A1"].font = Font(color=f"FF{font_color}")
        wb.save(path)
        wb.close()
        return path


if __name__ == "__main__":
    unittest.main(verbosity=2)


class OutputDirNamingTests(unittest.TestCase):
    """输出目录名要能被人读、被人复制，所以不带微秒也不带哈希。"""

    def test_name_is_source_folder_plus_timestamp_to_the_second(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "fixtures"
            source.mkdir()
            name = build_output_dir(source).name
            self.assertTrue(name.startswith("fixtures_翻译输出_"))
            stamp = name.removeprefix("fixtures_翻译输出_")
            self.assertRegex(stamp, r"^\d{8}_\d{6}$")

    def test_same_second_rerun_still_gets_a_distinct_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "fixtures"
            source.mkdir()
            first = build_output_dir(source)
            first.mkdir()
            second = build_output_dir(source)
            self.assertNotEqual(first, second)
            self.assertEqual(second.name, f"{first.name}_2")

    def test_custom_root_is_honoured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "fixtures"
            source.mkdir()
            custom = Path(tmp) / "elsewhere"
            custom.mkdir()
            self.assertEqual(build_output_dir(source, custom).parent, custom)


class BilingualOutputNameTests(unittest.TestCase):
    """双语产物的命名。家族规则：{原名}_{语言}_{形态}，与 PDF 的 _高清/_压缩 同构。"""

    def test_language_and_form_go_after_the_stem(self) -> None:
        self.assertEqual(
            bilingual_output_name("合同报价表.xlsx", "中文"),
            "合同报价表_中文_双语.xlsx",
        )

    def test_translation_sorts_immediately_after_its_source(self) -> None:
        """这条是整次改名的唯一理由，必须锁住。

        `.`(0x2E) 排在 `_`(0x5F) 前面，所以后缀式译文紧跟在原文后面；而前缀式的
        「双语(中文)_」以 U+53CC 开头，会被甩到所有英文名之后，跟原文彻底脱节。
        混一批西文名进来一起排，才测得出这个差别。
        """
        sources = ["alpha.xlsx", "report.docx", "zulu.xlsx"]
        names = sources + [bilingual_output_name(name, "中文") for name in sources]
        ordered = sorted(names)
        for source in sources:
            translated = bilingual_output_name(source, "中文")
            self.assertEqual(
                ordered[ordered.index(source) + 1],
                translated,
                f"{translated} 没有紧挨着 {source}：{ordered}",
            )

    def test_only_the_last_suffix_is_treated_as_extension(self) -> None:
        self.assertEqual(
            bilingual_output_name("a.b.c.xls", "中文"),
            "a.b.c_中文_双语.xls",
        )

    def test_source_stem_is_kept_verbatim_but_language_is_sanitised(self) -> None:
        """原文件名主干原样保留——它是磁盘上已经存在的名字，再洗一遍只会凭空
        制造和源文件对不上的风险。语言是用户在设置里填的，必须洗。"""
        self.assertEqual(
            bilingual_output_name("a:b.xlsx", "zh/CN"),
            "a:b_zh_CN_双语.xlsx",
        )
