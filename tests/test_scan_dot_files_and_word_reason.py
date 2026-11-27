"""文件夹扫描跳过点开头的文件；Word 读不了的文件给一句看得懂的话。

macOS 在 U 盘、网络盘上给每个文件配一个「._文件名」，它不是文档。以前 Excel / Word / CAD
的文件夹扫描会把它们逐个报成「读取失败」（PDF 和扩展类型一直是跳过的）。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import openpyxl
from docx import Document

from core.cad_translation import list_cad_files
from core.file_scanner import scan_excel_sources
from core.source_classify import classify_source_paths
from core.word_document import scan_word_sources

_APPLE_DOUBLE = b"\x00\x05\x16\x07" + b"\x00" * 32


class ScanDotFilesTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        book = openpyxl.Workbook()
        book.active["A1"] = "设备名称"
        book.save(self.root / "表.xlsx")
        document = Document()
        document.add_paragraph("检修日期")
        document.save(str(self.root / "文.docx"))
        (self.root / "图.dxf").write_text("0\nEOF\n", encoding="utf-8")
        for name in ("._表.xlsx", "._文.docx", "._图.dxf", ".DS_Store"):
            (self.root / name).write_bytes(_APPLE_DOUBLE)

    def test_excel_folder_scan_skips_dot_files(self) -> None:
        result = scan_excel_sources(self.root)
        self.assertEqual([item.path.name for item in result.items], ["表.xlsx"])
        self.assertEqual(result.skipped, [])

    def test_word_folder_scan_skips_dot_files(self) -> None:
        result = scan_word_sources(self.root)
        self.assertEqual([item.path.name for item in result.items], ["文.docx"])
        self.assertEqual(result.skipped, [])

    def test_cad_folder_scan_skips_dot_files(self) -> None:
        self.assertEqual([path.name for path in list_cad_files([self.root])], ["图.dxf"])

    def test_entry_page_counts_skip_dot_files(self) -> None:
        [item] = classify_source_paths([str(self.root)]).items
        self.assertEqual(item.suffixes, {"xlsx": 1, "docx": 1, "dxf": 1})

    def test_word_unreadable_file_reason_is_readable(self) -> None:
        (self.root / "坏的.docx").write_bytes(b"not a docx")
        result = scan_word_sources(self.root)
        [skipped] = result.skipped
        self.assertEqual(skipped.path.name, "坏的.docx")
        self.assertTrue(skipped.reason.startswith("读取失败："), skipped.reason)
        # 不再是 python-docx 的英文原话，也不带本机路径。
        self.assertNotIn("Package not found", skipped.reason)
        self.assertNotIn(str(self.root), skipped.reason)


if __name__ == "__main__":
    unittest.main()
