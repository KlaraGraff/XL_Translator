"""审计报告 1.2 / 1.3 / 4.8 / 4.9 / 4.10 的回归测试。

全部走真实的 openpyxl 工作簿 + ``write_bilingual_file`` 端到端路径，断言落在
输出文件的 XML 上，而不是辅助函数的返回值。补丁式写入的字节保真承诺（除被改的
分表 XML / drawing / styles.xml 外其余部件原样照抄）在每个场景里一并守住。
"""
from __future__ import annotations

import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from lxml import etree
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font

from core import bilingual_writer
from core.bilingual_writer import write_bilingual_file
from core.xlsx_patcher import (
    BASE_FONT_SIZE_PT,
    LINE_HEIGHT_RATIO,
    MAX_CELL_TEXT_LEN,
    NS_MAIN,
    NS_XDR,
    estimate_chars_per_line,
    estimate_required_lines,
    merged_rows_by_sheet,
)

NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

# 已经明确不该被重写的部件之外，允许变动的只有这几个。
ALLOWED_MODIFIED_PARTS = {
    "xl/worksheets/sheet1.xml",
    "xl/worksheets/sheet2.xml",
    "xl/drawings/drawing1.xml",
    "xl/styles.xml",
}


def _part_hashes(path: Path) -> dict[str, str]:
    with zipfile.ZipFile(path) as archive:
        return {
            name: hashlib.sha256(archive.read(name)).hexdigest()
            for name in archive.namelist()
        }


def _read_part(path: Path, name: str) -> bytes:
    with zipfile.ZipFile(path) as archive:
        return archive.read(name)


def _sheet_rows(path: Path, part: str = "xl/worksheets/sheet1.xml") -> dict[str, object]:
    root = etree.fromstring(_read_part(path, part))
    return {row.get("r"): row for row in root.iter(f"{{{NS_MAIN}}}row")}


class _PatcherDefectCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.out_dir = self.root / "out"
        self.logs: list[str] = []

    def _write(self, source: Path, translations: dict[str, str], **overrides) -> Path:
        kwargs = dict(
            source_path=source,
            output_dir=self.out_dir,
            translations=translations,
            target_lang="en",
            source_lang="zh",
            keep_original_sheets=False,
            formula_display_value_backfill=False,
            log_callback=self.logs.append,
        )
        kwargs.update(overrides)
        return write_bilingual_file(**kwargs)

    def assert_only_expected_parts_changed(self, source: Path, output: Path) -> None:
        before = _part_hashes(source)
        after = _part_hashes(output)
        self.assertEqual(set(before) - set(after), set(), "输出包丢失了原有部件")
        changed = {name for name, digest in before.items() if after[name] != digest}
        self.assertTrue(
            changed <= ALLOWED_MODIFIED_PARTS,
            f"补丁式写入被破坏，意外重写了部件：{sorted(changed - ALLOWED_MODIFIED_PARTS)}",
        )


class CellTextGuardTests(_PatcherDefectCase):
    """1.2 超 32767 字符 / 1.3 非法 XML 字符。"""

    def _single_cell_workbook(self, value: str) -> Path:
        path = self.root / "long.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "报价"
        sheet["A1"] = value
        workbook.save(path)
        workbook.close()
        return path

    def test_oversized_cell_is_truncated_and_reported(self) -> None:
        source_text = "施" * 20000
        translation = "Construction " * 2000  # 26000 字符
        source = self._single_cell_workbook(source_text)
        stats: dict[str, object] = {}

        output = self._write(source, {source_text: translation}, stats=stats)

        with zipfile.ZipFile(output) as archive:  # 包本身仍然可读
            self.assertIn("xl/worksheets/sheet1.xml", archive.namelist())
        workbook = load_workbook(output)
        try:
            written = workbook["报价"]["A1"].value
        finally:
            workbook.close()

        self.assertEqual(
            len(written),
            MAX_CELL_TEXT_LEN,
            "写入的单元格长度必须正好卡在 Excel 上限，多一个字符文件就打不开",
        )
        self.assertTrue(written.startswith(source_text[:100]))
        self.assertTrue(written.endswith("…"), "截断处要留可见记号")

        # 截断绝不能是静默的：统计出参和任务日志都要点名分表与坐标。
        self.assertEqual(stats["truncated_cells"], 1)
        self.assertEqual(stats["truncated_positions"], ["报价!A1"])
        truncation_logs = [line for line in self.logs if "截断" in line]
        self.assertTrue(truncation_logs, f"没有任何截断提示：{self.logs}")
        self.assertTrue(any("报价!A1" in line for line in truncation_logs))
        self.assertTrue(any(str(MAX_CELL_TEXT_LEN) in line for line in truncation_logs))

        self.assert_only_expected_parts_changed(source, output)

    def test_cell_within_limit_reports_no_truncation(self) -> None:
        source = self._single_cell_workbook("施工内容")
        stats: dict[str, object] = {}
        output = self._write(source, {"施工内容": "Construction scope"}, stats=stats)

        workbook = load_workbook(output)
        try:
            self.assertEqual(
                workbook["报价"]["A1"].value, "施工内容\nConstruction scope"
            )
        finally:
            workbook.close()
        self.assertEqual(stats["truncated_cells"], 0)
        self.assertEqual(stats["truncated_positions"], [])
        self.assertFalse([line for line in self.logs if "截断" in line])

    def test_illegal_xml_characters_do_not_abort_the_write(self) -> None:
        source = self._single_cell_workbook("施工内容")
        # \x0b 是 XML 1.0 不允许的码点，lxml 赋值时会直接抛 ValueError。
        output = self._write(
            source, {"施工内容": "Construction\x0bscope\x00end\x1f!"}
        )

        workbook = load_workbook(output)
        try:
            written = workbook["报价"]["A1"].value
        finally:
            workbook.close()
        self.assertEqual(written, "施工内容\nConstructionscopeend!")
        self.assertTrue(any("无法保存的特殊字符" in line for line in self.logs), self.logs)
        self.assert_only_expected_parts_changed(source, output)

    def test_legal_whitespace_survives_sanitizing(self) -> None:
        source = self._single_cell_workbook("施工内容")
        output = self._write(source, {"施工内容": "a\tb\nc"})
        workbook = load_workbook(output)
        try:
            self.assertEqual(workbook["报价"]["A1"].value, "施工内容\na\tb\nc")
        finally:
            workbook.close()

    def test_failed_patch_leaves_no_output_file(self) -> None:
        """补丁失败时输出目录必须干净——半成品副本的文件名和成品一模一样。"""
        source = self._single_cell_workbook("施工内容")
        self.out_dir.mkdir(parents=True, exist_ok=True)

        with mock.patch.object(
            bilingual_writer,
            "write_bilingual_workbook",
            side_effect=ValueError("boom"),
        ):
            with self.assertRaises(ValueError):
                self._write(source, {"施工内容": "Construction scope"})

        self.assertEqual(
            sorted(p.name for p in self.out_dir.iterdir()),
            [],
            "补丁失败后输出目录里不能残留任何文件（包括未翻译的副本和临时件）",
        )


class GroupedShapeTests(_PatcherDefectCase):
    """4.8 组合图形不能被整组尺寸压平。"""

    CHILD_EXTENTS = (("111", "222"), ("333", "444"))

    def _workbook_with_grouped_drawing(self) -> Path:
        base = self.root / "base.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "报价"
        # 第 1 行会被翻译 → 行高变化 → 锚点需要冻结。
        sheet["A1"] = "很长很长的施工说明文字需要换行才放得下所以行高会被自动调整"
        workbook.save(base)
        workbook.close()

        target = self.root / "grouped.xlsx"
        self._inject_drawing(base, target)
        return target

    def _inject_drawing(self, source: Path, target: Path) -> None:
        with zipfile.ZipFile(source) as archive:
            parts = {name: archive.read(name) for name in archive.namelist()}

        sheet_xml = etree.fromstring(parts["xl/worksheets/sheet1.xml"])
        drawing_el = etree.SubElement(sheet_xml, f"{{{NS_MAIN}}}drawing")
        drawing_el.set(f"{{{REL_NS}}}id", "rIdDraw")
        parts["xl/worksheets/sheet1.xml"] = etree.tostring(
            sheet_xml, xml_declaration=True, encoding="UTF-8", standalone=True
        )

        parts["xl/worksheets/_rels/sheet1.xml.rels"] = (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="{PKG_REL_NS}">'
            f'<Relationship Id="rIdDraw" Type="{REL_NS}/drawing"'
            f' Target="../drawings/drawing1.xml"/></Relationships>'
        ).encode()

        children = "".join(
            f'<xdr:sp><xdr:nvSpPr><xdr:cNvPr id="{index + 2}" name="Shape {index}"/>'
            f"<xdr:cNvSpPr/></xdr:nvSpPr><xdr:spPr>"
            f'<a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
            f'<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></xdr:spPr></xdr:sp>'
            for index, (cx, cy) in enumerate(self.CHILD_EXTENTS)
        )
        parts["xl/drawings/drawing1.xml"] = (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<xdr:wsDr xmlns:xdr="{NS_XDR}" xmlns:a="{NS_A}" xmlns:r="{REL_NS}">'
            f'<xdr:twoCellAnchor editAs="oneCell">'
            f"<xdr:from><xdr:col>3</xdr:col><xdr:colOff>0</xdr:colOff>"
            f"<xdr:row>0</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from>"
            f"<xdr:to><xdr:col>5</xdr:col><xdr:colOff>0</xdr:colOff>"
            f"<xdr:row>3</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:to>"
            f"<xdr:grpSp><xdr:nvGrpSpPr>"
            f'<xdr:cNvPr id="1" name="Group 1"/><xdr:cNvGrpSpPr/></xdr:nvGrpSpPr>'
            f'<xdr:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="10" cy="20"/>'
            f'<a:chOff x="0" y="0"/><a:chExt cx="10" cy="20"/></a:xfrm></xdr:grpSpPr>'
            f"{children}</xdr:grpSp>"
            f"<xdr:clientData/></xdr:twoCellAnchor></xdr:wsDr>"
        ).encode()

        content_types = etree.fromstring(parts["[Content_Types].xml"])
        override = etree.SubElement(content_types, f"{{{CT_NS}}}Override")
        override.set("PartName", "/xl/drawings/drawing1.xml")
        override.set(
            "ContentType",
            "application/vnd.openxmlformats-officedocument.drawing+xml",
        )
        parts["[Content_Types].xml"] = etree.tostring(
            content_types, xml_declaration=True, encoding="UTF-8", standalone=True
        )

        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in parts.items():
                archive.writestr(name, data)

    def test_child_shapes_keep_their_own_size(self) -> None:
        source = self._workbook_with_grouped_drawing()
        output = self._write(
            source,
            {
                "很长很长的施工说明文字需要换行才放得下所以行高会被自动调整": (
                    "A very long construction note that needs wrapping"
                )
            },
            # 只有之后还要跑 Excel autofit 时才改成定尺寸锚点，组合图形的外框问题出在这条路上。
            external_autofit_planned=True,
        )

        drawing = etree.fromstring(_read_part(output, "xl/drawings/drawing1.xml"))
        anchors = list(drawing)
        self.assertEqual(len(anchors), 1)
        self.assertEqual(etree.QName(anchors[0]).localname, "oneCellAnchor")

        anchor_ext = anchors[0].find(f"{{{NS_XDR}}}ext")
        anchor_cx = anchor_ext.get("cx")
        anchor_cy = anchor_ext.get("cy")

        group = anchors[0].find(f"{{{NS_XDR}}}grpSp")
        group_xfrm = group.find(f"{{{NS_XDR}}}grpSpPr/{{{NS_A}}}xfrm")
        group_ext = group_xfrm.find(f"{{{NS_A}}}ext")
        self.assertEqual(
            (group_ext.get("cx"), group_ext.get("cy")),
            (anchor_cx, anchor_cy),
            "组合图形的外框仍要跟着锚点走，否则渲染器读到 0 尺寸",
        )
        # chExt 保持原值，整组才是等比缩放而不是被压平。
        child_extent = group_xfrm.find(f"{{{NS_A}}}chExt")
        self.assertEqual((child_extent.get("cx"), child_extent.get("cy")), ("10", "20"))

        child_sizes = [
            (
                shape.find(f"{{{NS_XDR}}}spPr/{{{NS_A}}}xfrm/{{{NS_A}}}ext").get("cx"),
                shape.find(f"{{{NS_XDR}}}spPr/{{{NS_A}}}xfrm/{{{NS_A}}}ext").get("cy"),
            )
            for shape in group.findall(f"{{{NS_XDR}}}sp")
        ]
        self.assertEqual(
            child_sizes,
            list(self.CHILD_EXTENTS),
            "组内子形状各自的尺寸必须原样保留，被覆盖成整组尺寸就是图形被毁",
        )


class RowHeightScopeTests(_PatcherDefectCase):
    """4.9 未翻译的行不许改行高 / 4.10 公式源码不参与行高估算。"""

    LONG_SOURCE = "很长很长的施工说明文字需要换行才放得下所以行高会被自动调整"
    LONG_TARGET = "A very long construction note that definitely needs wrapping"
    UNTRANSLATED = "这一行本次一个字都没翻但是它同样很长会被估算成需要换很多行"

    def _workbook(self) -> Path:
        path = self.root / "rows.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "报价"
        sheet["A1"] = self.LONG_SOURCE
        sheet["A2"] = self.UNTRANSLATED
        sheet.row_dimensions[2].height = 20  # openpyxl 会自动带上 customHeight
        workbook.save(path)
        workbook.close()
        return path

    def test_untranslated_row_keeps_its_manual_height(self) -> None:
        source = self._workbook()
        output = self._write(source, {self.LONG_SOURCE: self.LONG_TARGET})

        rows = _sheet_rows(output)
        translated_row = rows["1"]
        self.assertEqual(translated_row.get("customHeight"), "1")
        self.assertGreater(
            float(translated_row.get("ht")),
            15.0,
            "被翻译的行仍然要按双语内容加高",
        )

        untouched_row = rows["2"]
        self.assertEqual(
            float(untouched_row.get("ht")),
            20.0,
            "本次一字未翻的行，手工设的行高必须原样保留",
        )
        self.assert_only_expected_parts_changed(source, output)

    def test_nothing_translated_leaves_the_sheet_byte_identical(self) -> None:
        source = self._workbook()
        output = self._write(source, {})

        before = _part_hashes(source)
        after = _part_hashes(output)
        self.assertEqual(
            after["xl/worksheets/sheet1.xml"],
            before["xl/worksheets/sheet1.xml"],
            "一个单元格都没改时分表 XML 不该被重写",
        )

    def test_formula_source_does_not_inflate_row_height(self) -> None:
        path = self.root / "formula.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "报价"
        sheet["A1"] = "配电箱"
        # 长嵌套公式：源码 400+ 字符，显示出来只是一个数字。
        sheet["B1"] = "=IF(" + "+".join(f"C{i}" for i in range(1, 120)) + ",1,2)"
        workbook.save(path)
        workbook.close()

        output = self._write(path, {"配电箱": "Distribution box"})

        row = _sheet_rows(output)["1"]
        height = float(row.get("ht"))
        # "配电箱\nDistribution box" 在默认列宽下约 3 行；公式源码若参与估算
        # 会算出几十行、把行高顶到 400pt 以上。
        self.assertLess(
            height,
            80.0,
            f"行高被公式源码撑到了 {height}pt，公式不该按源码文本参与换行估算",
        )
        self.assertGreater(height, 15.0)


class MergedCellRowHeightTests(_PatcherDefectCase):
    """合并单元格按整个合并区域的宽度估算行高；任何行都只加高、不压矮。"""

    TITLE = "工程量清单（土建工程）"
    TITLE_FR = "Bordereau quantitatif et estimatif (travaux de génie civil)"
    INFO = "工程名称：示例厂区综合楼项目　　　　编制日期：2026 年 9 月"
    INFO_FR = (
        "Projet : immeuble polyvalent de la zone industrielle (exemple)"
        "    Date d'établissement : septembre 2026"
    )
    SECTION = "一、土方工程"
    SECTION_FR = "I. Travaux de terrassement"
    ITEM = "配电箱"
    ITEM_FR = "Distribution box"
    NOTE = "施工期间须做好基坑排水和边坡监测"
    NOTE_FR = "Assurer le drainage des fouilles et la surveillance des talus pendant les travaux"

    def _bill_of_quantities(self, *, title_height: float = 32) -> Path:
        """仿工程量清单：标题、工程信息、分部标题都横跨 A:F 合并。"""
        path = self.root / "merged.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "清单"
        for letter, width in zip("ABCDEF", (7, 22, 46, 8, 11, 26)):
            sheet.column_dimensions[letter].width = width
        sheet.merge_cells("A1:F1")
        sheet["A1"] = self.TITLE
        sheet["A1"].font = Font(size=15, bold=True)
        sheet.row_dimensions[1].height = title_height
        sheet.merge_cells("A2:F2")  # 不设行高：按分表默认行高算
        sheet["A2"] = self.INFO
        sheet["A2"].font = Font(size=10)
        sheet.merge_cells("A4:F4")
        sheet["A4"] = self.SECTION
        sheet.row_dimensions[4].height = 22
        sheet["A5"] = self.ITEM  # 不合并的普通格，A 列只有 7 宽
        workbook.save(path)
        workbook.close()
        return path

    def _translations(self) -> dict[str, str]:
        return {
            self.TITLE: self.TITLE_FR,
            self.INFO: self.INFO_FR,
            self.SECTION: self.SECTION_FR,
            self.ITEM: self.ITEM_FR,
        }

    def test_merged_single_row_cells_use_the_whole_merge_width(self) -> None:
        source = self._bill_of_quantities()
        output = self._write(source, self._translations())
        rows = _sheet_rows(output)

        # 15pt 标题两行（原文一行、译文一行）。只按 A 列 7 宽估算会算成 10 行、154pt。
        title = float(rows["1"].get("ht"))
        self.assertGreaterEqual(title, 2 * 15 * LINE_HEIGHT_RATIO - 1e-6, "大字号标题按两行算也该比 32pt 高")
        self.assertLessEqual(title, 3 * 15 * LINE_HEIGHT_RATIO + 1e-6, f"合并标题行被撑到了 {title}pt")

        one_line = BASE_FONT_SIZE_PT * LINE_HEIGHT_RATIO
        info = float(rows["2"].get("ht"))
        self.assertLessEqual(info, 3 * one_line + 1e-6, f"合并的工程信息行被撑到了 {info}pt")
        self.assertGreater(info, 15.0)

        section = float(rows["4"].get("ht"))
        self.assertGreater(section, 22.0, "双语分部标题放不进原来的 22pt，仍要加高")
        self.assertLessEqual(section, 3 * one_line + 1e-6, f"合并的分部标题行被撑到了 {section}pt")

        # 不合并的格照旧按本列宽度估算："配电箱" 一行，"Distribution box" 在 7 宽里
        # 按字符硬断成三行。
        self.assertAlmostEqual(float(rows["5"].get("ht")), 4 * one_line, places=6)
        self.assertEqual(rows["5"].get("customHeight"), "1")

    def test_multi_row_merge_raises_only_its_last_row(self) -> None:
        path = self.root / "vertical.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "说明"
        sheet.column_dimensions["A"].width = 10
        sheet.merge_cells("A2:A4")
        sheet["A2"] = self.NOTE
        for row_num in (2, 3, 4):
            sheet.row_dimensions[row_num].height = 20
        workbook.save(path)
        workbook.close()

        output = self._write(path, {self.NOTE: self.NOTE_FR})
        rows = _sheet_rows(output)

        heights = [float(rows[str(row_num)].get("ht")) for row_num in (2, 3, 4)]
        self.assertEqual(heights[:2], [20.0, 20.0], "合并区域的首行和中间行不该被单独撑高")
        lines = estimate_required_lines(
            f"{self.NOTE}\n{self.NOTE_FR}", estimate_chars_per_line(10, BASE_FONT_SIZE_PT)
        )
        self.assertAlmostEqual(
            sum(heights),
            lines * BASE_FONT_SIZE_PT * LINE_HEIGHT_RATIO,
            places=3,
            msg="三行合起来正好补够所需高度：差额全加在最后一行",
        )

    def test_existing_taller_row_is_never_lowered(self) -> None:
        source = self._bill_of_quantities(title_height=60)
        path = self.root / "tall.xlsx"
        workbook = load_workbook(source)
        workbook.active.row_dimensions[5].height = 80
        workbook.save(path)
        workbook.close()

        output = self._write(path, self._translations())
        rows = _sheet_rows(output)

        self.assertEqual(float(rows["1"].get("ht")), 60.0, "原本就够高的合并标题行不许压矮")
        self.assertEqual(float(rows["5"].get("ht")), 80.0, "原本就够高的普通行不许压矮")



def _cell_style_attrs(path: Path, ref: str) -> dict[str, str]:
    """``ref`` 那一格最终样式里的 alignment 属性。"""
    sheet = etree.fromstring(_read_part(path, "xl/worksheets/sheet1.xml"))
    cell = next(c for c in sheet.iter(f"{{{NS_MAIN}}}c") if c.get("r") == ref)
    styles = etree.fromstring(_read_part(path, "xl/styles.xml"))
    xfs = styles.find(f"{{{NS_MAIN}}}cellXfs")
    xf = list(xfs)[int(cell.get("s") or 0)]
    alignment = xf.find(f"{{{NS_MAIN}}}alignment")
    return dict(alignment.attrib) if alignment is not None else {}


class FirstRowHeightTests(_PatcherDefectCase):
    """首行常见的几种排版：中文表头、大字号标题、溢出标题、同行不换行的长串。"""

    def _save(self, workbook: Workbook, name: str) -> Path:
        path = self.root / name
        workbook.save(path)
        workbook.close()
        return path

    def test_line_estimate_counts_chinese_as_wide_and_wraps_words(self) -> None:
        # 列宽 8.43 一行约放 10 个西文字符、4 个汉字。
        self.assertEqual(estimate_required_lines("施工期间须做好", 10), 2)
        self.assertEqual(estimate_required_lines("abcdefghij", 10), 1)
        # 西文在词间断开："Unité de" / "mesure"
        self.assertEqual(estimate_required_lines("Unité de mesure", 10), 2)
        # 比一行还长的词按字符硬断
        self.assertEqual(estimate_required_lines("Désignation", 5), 3)
        self.assertEqual(estimate_required_lines("计量单位\nUnit", 10), 2)

    def test_chinese_header_row_is_raised_enough(self) -> None:
        text = "施工期间须做好基坑排水和边坡监测工作，确保基坑安全"
        workbook = Workbook()
        sheet = workbook.active
        sheet.column_dimensions["A"].width = 20
        sheet["A1"] = text
        sheet["A1"].alignment = Alignment(wrap_text=True)
        path = self._save(workbook, "header.xlsx")

        output = self._write(path, {text: "Assurer le drainage"})
        height = float(_sheet_rows(output)["1"].get("ht"))
        # 25 个汉字在 20 宽里排 3 行，再加 1 行译文；按「一字一格」只会估成 2 行。
        self.assertAlmostEqual(height, 4 * BASE_FONT_SIZE_PT * LINE_HEIGHT_RATIO, places=3)

    def test_large_title_fits_fewer_characters_per_line(self) -> None:
        title, title_fr = "工程量清单（土建工程）", "Bordereau quantitatif et estimatif (travaux de génie civil)"
        workbook = Workbook()
        sheet = workbook.active
        for letter, width in zip("ABCDEF", (6, 22, 9, 9, 12, 16)):
            sheet.column_dimensions[letter].width = width
        sheet.merge_cells("A1:F1")
        sheet["A1"] = title
        sheet["A1"].font = Font(size=18, bold=True)
        sheet.row_dimensions[1].height = 30
        path = self._save(workbook, "title.xlsx")

        output = self._write(path, {title: title_fr})
        height = float(_sheet_rows(output)["1"].get("ht"))
        per_line = estimate_chars_per_line(74, 18)
        lines = estimate_required_lines(f"{title}\n{title_fr}", per_line)
        self.assertEqual(lines, 3, "18pt 时译文在 74 宽里要折成两行")
        self.assertAlmostEqual(height, 3 * 18 * LINE_HEIGHT_RATIO, places=3)

    def test_unwrapped_neighbour_does_not_raise_the_row(self) -> None:
        workbook = Workbook()
        sheet = workbook.active
        sheet.column_dimensions["B"].width = 10
        sheet["A1"] = "序号"
        sheet["B1"] = "DWG-REF-2026-0001-ARCH-STRUCT-ELEC-PLUMB"  # 不译、不换行，往右溢出
        path = self._save(workbook, "code.xlsx")

        output = self._write(path, {"序号": "N°"})
        # 只有 "序号\nN°" 两行；那串编码不换行，不该按 10 宽估成 4 行。
        self.assertAlmostEqual(
            float(_sheet_rows(output)["1"].get("ht")), 2 * BASE_FONT_SIZE_PT * LINE_HEIGHT_RATIO, places=3
        )

    def test_spilling_title_stays_unwrapped(self) -> None:
        workbook = Workbook()
        sheet = workbook.active
        sheet["A1"] = "设备材料表（第一册）"  # 没合并、不换行，靠右边空格子显示全
        sheet["A1"].font = Font(size=16, bold=True)
        sheet.row_dimensions[1].height = 24
        sheet["A2"] = "序号"
        sheet["B2"] = "名称"
        sheet["A3"] = "注：本表数量以图纸为准"  # 也往右溢出，但译文长得多
        path = self._save(workbook, "spill.xlsx")
        translations = {
            "设备材料表（第一册）": "Liste des équipements et matériaux (tome 1)",
            "序号": "N°",
            "注：本表数量以图纸为准": "Remarque : les quantités de ce tableau sont données à titre indicatif, "
            "seuls les plans font foi ; en cas de divergence, se référer aux plans et aux spécifications",
        }

        only = self._write(path, translations, output_translation_only=True)
        self.assertNotIn("wrapText", _cell_style_attrs(only, "A1"), "溢出标题被强行折进 A 列")
        self.assertEqual(float(_sheet_rows(only)["1"].get("ht")), 24.0)

        bilingual = self._write(path, translations, output_dir=self.root / "bi")
        self.assertNotIn("wrapText", _cell_style_attrs(bilingual, "A1"))
        # 双语按换行符两行；以前折在 8.43 宽的 A 列里是 112pt。
        self.assertAlmostEqual(float(_sheet_rows(bilingual)["1"].get("ht")), 2 * 16 * LINE_HEIGHT_RATIO, places=3)
        # 本列放得下原文的普通格照旧打开自动换行
        self.assertEqual(_cell_style_attrs(bilingual, "A2").get("wrapText"), "1")
        # 译文比原文长出好几倍的溢出格不再一行拖出去，在本列里折行
        self.assertEqual(_cell_style_attrs(only, "A3").get("wrapText"), "1")

    def test_text_hidden_under_a_merge_is_ignored(self) -> None:
        workbook = Workbook()
        sheet = workbook.active
        sheet["A1"] = "标题"
        sheet["B1"] = "被合并盖住的很长很长很长很长很长很长的一段文字"
        sheet.merge_cells("A1:D1")
        path = self._save(workbook, "covered.xlsx")
        # openpyxl 合并时会清掉被盖住的格；手工把文字塞回 XML 模拟其他软件写出的文件。
        with zipfile.ZipFile(path) as archive:
            parts = {name: archive.read(name) for name in archive.namelist()}
        sheet_xml = parts["xl/worksheets/sheet1.xml"].decode()
        hidden = '<c r="B1" t="inlineStr"><is><t>被合并盖住的很长很长很长很长很长很长的一段文字</t></is></c>'
        sheet_xml = sheet_xml.replace('<c r="B1"/>', hidden) if '<c r="B1"/>' in sheet_xml else sheet_xml.replace(
            "</c></row>", "</c>" + hidden + "</row>", 1
        )
        parts["xl/worksheets/sheet1.xml"] = sheet_xml.encode()
        with zipfile.ZipFile(path, "w") as archive:
            for name, data in parts.items():
                archive.writestr(name, data)

        output = self._write(path, {"标题": "Titre"})
        self.assertAlmostEqual(
            float(_sheet_rows(output)["1"].get("ht")), 2 * BASE_FONT_SIZE_PT * LINE_HEIGHT_RATIO, places=3
        )

    def test_locked_row_height_measures_merged_title_by_its_whole_width(self) -> None:
        title, title_fr = "工程量清单", "Bordereau quantitatif et estimatif des travaux"
        workbook = Workbook()
        sheet = workbook.active
        for letter in "ABCDEF":
            sheet.column_dimensions[letter].width = 12
        sheet.merge_cells("A1:F1")
        sheet["A1"] = title
        sheet["A1"].font = Font(size=14)
        sheet.row_dimensions[1].height = 24
        path = self._save(workbook, "locked.xlsx")

        self._write(path, {title: title_fr}, lock_row_height=True)
        self.assertFalse(
            [line for line in self.logs if "缩至最小字号" in line], "合并标题只按 A 列算会被缩到最小字号"
        )

    def test_merged_rows_by_sheet_lists_every_row_a_merge_covers(self) -> None:
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "清单"
        sheet.merge_cells("A1:F1")
        sheet.merge_cells("B3:B5")
        workbook.create_sheet("空表")
        path = self._save(workbook, "merges.xlsx")
        self.assertEqual(merged_rows_by_sheet(path), {"清单": {1, 3, 4, 5}})


class AutofitMergedRowTests(unittest.TestCase):
    """Excel 自动调整行高不认合并单元格：调整完把合并标题行恢复到不低于原来的高度。"""

    class _Range:
        def __init__(self, heights: dict[int, float], row: int) -> None:
            self._heights, self._row = heights, row

        @property
        def row_height(self) -> float:
            return self._heights[self._row]

        @row_height.setter
        def row_height(self, value: float) -> None:
            self._heights[self._row] = value

    class _Sheet:
        name = "清单"

        def __init__(self, heights: dict[int, float]) -> None:
            self.heights = heights
            sheet = self

            class _Rows:
                def autofit(self) -> None:  # Excel：合并标题行压回一行，普通行按内容
                    sheet.heights.update({1: 15.0, 2: 45.0})

            class _Used:
                rows = _Rows()

            self.used_range = _Used()

        def range(self, ref: str):
            return AutofitMergedRowTests._Range(self.heights, int(ref[1:]))

    def test_merged_rows_never_end_lower_than_before(self) -> None:
        sheet = self._Sheet({1: 75.6, 2: 30.8})
        bilingual_writer._autofit_keeping_merged_rows(sheet, {1})
        self.assertEqual(sheet.heights, {1: 75.6, 2: 45.0})


if __name__ == "__main__":
    unittest.main()
