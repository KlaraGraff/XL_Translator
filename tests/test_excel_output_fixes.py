"""Excel 输出的三处修复：原文副本不成组、仅译文文件名不带「双语」、空填充不再让扫描跳过。"""

from __future__ import annotations

import re
import tempfile
import unittest
import zipfile
from pathlib import Path

from openpyxl import Workbook
from openpyxl.comments import Comment

from core.bilingual_writer import bilingual_output_name, write_bilingual_file
from core.file_scanner import scan_excel_sources
from core.openpyxl_compat import load_workbook
from core.output_name_translation import avoid_bilingual_name_collision
from core.resume_detection import match_previous_output


def _tab_selected_sheets(path: Path) -> list[str]:
    """按工作簿里的表名返回带 tabSelected="1" 的分表。"""
    with zipfile.ZipFile(path) as package:
        workbook = package.read("xl/workbook.xml").decode("utf-8")
        rels = package.read("xl/_rels/workbook.xml.rels").decode("utf-8")
        targets = dict(re.findall(r'Id="([^"]+)"[^>]*Target="([^"]+)"', rels))
        targets.update(
            {rid: target for target, rid in re.findall(r'Target="([^"]+)"[^>]*Id="([^"]+)"', rels)}
        )
        selected = []
        for name, rid in re.findall(r'<sheet [^>]*name="([^"]+)"[^>]*r:id="([^"]+)"', workbook):
            part = "xl/" + targets[rid].lstrip("/").removeprefix("xl/")
            if 'tabSelected="1"' in package.read(part).decode("utf-8"):
                selected.append(name)
        return selected


def _with_empty_fill(source: Path, target: Path) -> None:
    """把 styles.xml 的填充表末尾加一个空的 <fill/>（Excel 能打开，openpyxl 原本读不了）。"""
    with zipfile.ZipFile(source) as zin, zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if info.filename == "xl/styles.xml":
                text = data.decode("utf-8")
                count = int(re.search(r'<fills count="(\d+)"', text).group(1))
                text = text.replace(f'<fills count="{count}">', f'<fills count="{count + 1}">')
                text = text.replace("</fills>", "<fill/></fills>")
                data = text.encode("utf-8")
            zout.writestr(info, data)


class OriginalSheetGroupingTests(unittest.TestCase):
    def test_cloned_original_sheet_is_not_selected_with_its_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "产值.xlsx"
            wb = Workbook()
            wb.active.title = "产值"
            wb.active["A1"] = "项目"
            wb.active.sheet_view.tabSelected = True  # Excel 保存时当前表都会带
            wb.save(source)
            self.assertEqual(_tab_selected_sheets(source), ["产值"])

            out_path = write_bilingual_file(
                source_path=source,
                output_dir=Path(tmp) / "out",
                translations={"项目": "Projet"},
                target_lang="fr",
                keep_original_sheets=True,
                formula_display_value_backfill=True,
            )

            self.assertEqual(_tab_selected_sheets(out_path), ["产值"])
            self.assertEqual(load_workbook(out_path).sheetnames, ["产值", "产值_原文"])

    def test_resumed_output_from_old_version_is_ungrouped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "旧产物.xlsx"
            wb = Workbook()
            wb.active.title = "产值"
            wb.active["A1"] = "项目"
            wb.active.sheet_view.tabSelected = True
            clone = wb.create_sheet("产值_原文")
            clone["A1"] = "项目"
            clone.sheet_view.tabSelected = True
            wb.save(source)
            self.assertEqual(_tab_selected_sheets(source), ["产值", "产值_原文"])

            out_path = write_bilingual_file(
                source_path=source,
                output_dir=Path(tmp) / "out",
                translations={"项目": "Projet"},
                target_lang="fr",
                keep_original_sheets=True,
                formula_display_value_backfill=True,
            )

            self.assertEqual(_tab_selected_sheets(out_path), ["产值"])
            self.assertEqual(load_workbook(out_path).sheetnames, ["产值", "产值_原文"])


class TranslationOnlyNameTests(unittest.TestCase):
    def test_name_drops_bilingual_label_only_for_translation_only(self) -> None:
        self.assertEqual(bilingual_output_name("报价.xlsx", "法文"), "报价_法文_双语.xlsx")
        self.assertEqual(
            bilingual_output_name("报价.xlsx", "法文", translation_only=True), "报价_法文.xlsx"
        )

    def test_excel_writer_uses_translation_only_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "报价.xlsx"
            wb = Workbook()
            wb.active["A1"] = "项目"
            wb.save(source)

            out_path = write_bilingual_file(
                source_path=source,
                output_dir=Path(tmp) / "out",
                translations={"项目": "Projet"},
                target_lang="fr",
                keep_original_sheets=False,
                formula_display_value_backfill=True,
                output_translation_only=True,
            )

            self.assertEqual(out_path.name, "报价_法文.xlsx")
            self.assertEqual(load_workbook(out_path).active["A1"].value, "Projet")

    def test_collision_check_looks_at_translation_only_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            (output / "报价_法文.xlsx").write_bytes(b"existing")
            self.assertEqual(
                avoid_bilingual_name_collision(output, "报价.xlsx", "fr", translation_only=True),
                "报价_2.xlsx",
            )
            self.assertEqual(avoid_bilingual_name_collision(output, "报价.xlsx", "fr"), "报价.xlsx")

    def test_resume_finds_translation_only_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            previous = Path(tmp) / "报价_法文.xlsx"
            previous.write_bytes(b"x")
            self.assertEqual(match_previous_output(tmp, ".", "报价.xlsx", "fr", "excel"), previous)


_THREADED_REL = "http://schemas.microsoft.com/office/2017/10/relationships/threadedComment"
_GUID = "{6F2A1C9E-0000-4000-8000-000000000001}"


def _with_threaded_comment(source: Path, target: Path) -> None:
    """给 C3 的传统批注配一条线程批注，像新版 Excel 存的那样：传统批注带 xr:uid。"""
    xr = "http://schemas.microsoft.com/office/spreadsheetml/2014/revision"
    with zipfile.ZipFile(source) as zin, zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if info.filename == "xl/comments/comment1.xml":
                text = data.decode("utf-8")
                text = text.replace("<comments ", f'<comments xmlns:xr="{xr}" ', 1)
                text = text.replace('shapeId="0">', f'shapeId="0" xr:uid="{_GUID}">', 1)
                data = text.encode("utf-8")
            elif info.filename == "xl/worksheets/_rels/sheet1.xml.rels":
                data = data.replace(
                    b"</Relationships>",
                    f'<Relationship Id="rIdTC" Type="{_THREADED_REL}" '
                    'Target="/xl/threadedComments/threadedComment1.xml"/></Relationships>'.encode(),
                )
            elif info.filename == "[Content_Types].xml":
                data = data.replace(
                    b"</Types>",
                    b'<Override PartName="/xl/threadedComments/threadedComment1.xml" '
                    b'ContentType="application/vnd.ms-excel.threadedcomments+xml"/></Types>',
                )
            zout.writestr(info, data)
        zout.writestr(
            "xl/threadedComments/threadedComment1.xml",
            '<ThreadedComments xmlns="http://schemas.microsoft.com/office/spreadsheetml/2018/threadedcomments">'
            f'<threadedComment ref="C3" dT="2026-01-01T00:00:00Z" personId="{_GUID}" id="{_GUID}">'
            "<text>线程批注</text></threadedComment></ThreadedComments>",
        )


class ThreadedCommentCloneTests(unittest.TestCase):
    def test_original_sheet_copy_does_not_share_threaded_comments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "base.xlsx"
            wb = Workbook()
            wb.active.title = "产值"
            wb.active["A1"] = "项目"
            wb.active["C3"].comment = Comment("[Threaded comment]\n线程批注", f"tc={_GUID}")
            wb.save(base)
            source = Path(tmp) / "产值.xlsx"
            _with_threaded_comment(base, source)

            out_path = write_bilingual_file(
                source_path=source,
                output_dir=Path(tmp) / "out",
                translations={"项目": "Projet"},
                target_lang="fr",
                keep_original_sheets=True,
                formula_display_value_backfill=True,
            )

            with zipfile.ZipFile(out_path) as package:
                rels = {
                    name: package.read(name).decode("utf-8")
                    for name in package.namelist()
                    if name.startswith("xl/worksheets/_rels/")
                }
                threaded_owners = [name for name, xml in rels.items() if _THREADED_REL in xml]
                self.assertEqual(len(rels), 2)
                self.assertEqual(len(threaded_owners), 1, "线程批注只能挂在一张表上")
                comment_parts = sorted(
                    name for name in package.namelist() if name.startswith("xl/comments/")
                )
                self.assertEqual(len(comment_parts), 2, "原文副本要有自己的一份传统批注")
                uids = [
                    part for part in comment_parts
                    if f'uid="{_GUID}"' in package.read(part).decode("utf-8")
                ]
                self.assertEqual(len(uids), 1, "原文副本的批注不能再指向那条线程批注")
            self.assertEqual(load_workbook(out_path).sheetnames, ["产值", "产值_原文"])


class EmptyFillTests(unittest.TestCase):
    def test_workbook_with_empty_fill_is_scanned_and_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "base.xlsx"
            wb = Workbook()
            wb.active["A1"] = "项目"
            wb.save(base)
            folder = Path(tmp) / "in"
            folder.mkdir()
            source = folder / "产值.xlsx"
            _with_empty_fill(base, source)
            base.unlink()

            result = scan_excel_sources(folder)
            self.assertEqual([item.path.name for item in result.items], ["产值.xlsx"])
            self.assertEqual(result.skipped, [])

            out_path = write_bilingual_file(
                source_path=source,
                output_dir=Path(tmp) / "out",
                translations={"项目": "Projet"},
                target_lang="fr",
                keep_original_sheets=True,
                formula_display_value_backfill=True,
                review_marks={"项目": "unresolved"},
            )
            with zipfile.ZipFile(out_path) as package:
                self.assertIn("<fill/>", package.read("xl/styles.xml").decode("utf-8"))
            self.assertEqual(load_workbook(out_path).active["A1"].value, "项目\nProjet")


if __name__ == "__main__":
    unittest.main()
