"""补丁式 xlsx 写入器的契约测试。

重点验证三件事：
  1. 未被修改的部件在输出包里逐字节保持原样（尤其是 WPS cellimages.xml、
     Excel 富数据部件、media 图片）；
  2. 悬浮图片在行高调整后不被拉伸——twoCellAnchor 变成带原渲染尺寸的 oneCellAnchor；
  3. 译文、复核标色、行高、原文分表克隆的语义与旧路径一致。
"""
from __future__ import annotations

import hashlib
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from lxml import etree
from openpyxl import Workbook, load_workbook

from core.mixed_language import MIXED_MARK_FOREIGN_NOISE, MIXED_MARK_UNRESOLVED
from core.bilingual_writer import write_bilingual_file
from core.xlsx_patcher import (
    EMU_PER_POINT,
    NS_MAIN,
    NS_XDR,
    _column_letter,
)

CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

WPS_CELLIMAGES = (
    b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    b'<etc:cellImages xmlns:etc="http://www.wps.cn/officeDocument/2017/etCustomData">'
    b"<etc:cellImage>ID_1</etc:cellImage></etc:cellImages>"
)
RICH_VALUE_PART = (
    b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    b'<rvData xmlns="http://schemas.microsoft.com/office/spreadsheetml/2017/richdata"'
    b' count="1"><rv s="0"><v>0</v></rv></rvData>'
)

PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6360000002000100ffff03000006"
    "000557bfabd40000000049454e44ae426082"
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _part_hashes(path: Path) -> dict[str, str]:
    with zipfile.ZipFile(path) as archive:
        return {name: _sha(archive.read(name)) for name in archive.namelist()}


def _read_part(path: Path, name: str) -> bytes:
    with zipfile.ZipFile(path) as archive:
        return archive.read(name)


class XlsxPatcherFixture:
    """手工往 openpyxl 生成的工作簿里塞进 openpyxl 自己不认识的部件。"""

    @staticmethod
    def build(root: Path) -> Path:
        base = root / "base.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "报价"
        sheet["A1"] = "施工内容"
        sheet["A2"] = "很长很长的施工说明文字需要换行才放得下所以行高会被自动调整"
        sheet["A3"] = "配电箱"
        sheet["B1"] = "=SUM(C1:C2)"
        sheet["C1"] = 123
        second = workbook.create_sheet("附表")
        second["A1"] = "配电箱"
        workbook.save(base)
        workbook.close()

        target = root / "fixture.xlsx"
        XlsxPatcherFixture._inject(base, target)
        return target

    @staticmethod
    def _inject(source: Path, target: Path) -> None:
        with zipfile.ZipFile(source) as archive:
            parts = {name: archive.read(name) for name in archive.namelist()}

        # ── 分表 1：DISPIMG 公式 + 富数据单元格 + 共享字符串引用 ──────────
        sheet_xml = etree.fromstring(parts["xl/worksheets/sheet1.xml"])
        sheet_data = sheet_xml.find(f"{{{NS_MAIN}}}sheetData")
        row = etree.SubElement(sheet_data, f"{{{NS_MAIN}}}row")
        row.set("r", "5")
        dispimg = etree.SubElement(row, f"{{{NS_MAIN}}}c")
        dispimg.set("r", "A5")
        dispimg.set("t", "str")
        etree.SubElement(dispimg, f"{{{NS_MAIN}}}f").text = '_xlfn.DISPIMG("ID_1",1)'
        etree.SubElement(dispimg, f"{{{NS_MAIN}}}v").text = "配电箱"

        rich = etree.SubElement(row, f"{{{NS_MAIN}}}c")
        rich.set("r", "B5")
        rich.set("vm", "1")
        rich.set("t", "s")
        etree.SubElement(rich, f"{{{NS_MAIN}}}v").text = "0"

        # 共享字符串引用（openpyxl 保存时只会写 inlineStr，这里手工补一个）
        shared = etree.SubElement(row, f"{{{NS_MAIN}}}c")
        shared.set("r", "C5")
        shared.set("t", "s")
        etree.SubElement(shared, f"{{{NS_MAIN}}}v").text = "0"

        # ── 悬浮图片：twoCellAnchor 跨 1-4 行 ───────────────────────────
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

        parts["xl/drawings/drawing1.xml"] = (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<xdr:wsDr xmlns:xdr="{NS_XDR}"'
            f' xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
            f' xmlns:r="{REL_NS}">'
            f'<xdr:twoCellAnchor editAs="oneCell">'
            f"<xdr:from><xdr:col>3</xdr:col><xdr:colOff>0</xdr:colOff>"
            f"<xdr:row>0</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from>"
            f"<xdr:to><xdr:col>5</xdr:col><xdr:colOff>0</xdr:colOff>"
            f"<xdr:row>3</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:to>"
            f"<xdr:pic><xdr:nvPicPr>"
            f'<xdr:cNvPr id="1" name="Picture 1"/><xdr:cNvPicPr/></xdr:nvPicPr>'
            f'<xdr:blipFill><a:blip r:embed="rIdImg"/>'
            f"<a:stretch><a:fillRect/></a:stretch></xdr:blipFill>"
            f'<xdr:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/></a:xfrm>'
            f'<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></xdr:spPr></xdr:pic>'
            f"<xdr:clientData/></xdr:twoCellAnchor></xdr:wsDr>"
        ).encode()

        parts["xl/drawings/_rels/drawing1.xml.rels"] = (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="{PKG_REL_NS}">'
            f'<Relationship Id="rIdImg" Type="{REL_NS}/image"'
            f' Target="../media/image1.png"/></Relationships>'
        ).encode()
        parts["xl/media/image1.png"] = PNG_BYTES

        # ── 共享字符串表 ───────────────────────────────────────────────
        parts["xl/sharedStrings.xml"] = (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<sst xmlns="{NS_MAIN}" count="2" uniqueCount="1">'
            f"<si><t>配电箱</t></si></sst>"
        ).encode()

        # ── WPS cellimages + Excel 富数据部件 ─────────────────────────
        parts["xl/cellimages.xml"] = WPS_CELLIMAGES
        parts["xl/richData/rdrichvalue.xml"] = RICH_VALUE_PART

        workbook_rels = etree.fromstring(parts["xl/_rels/workbook.xml.rels"])
        for rel_id, rel_type, rel_target in (
            ("rIdShared", f"{REL_NS}/sharedStrings", "sharedStrings.xml"),
            (
                "rIdCellImages",
                "http://www.wps.cn/officeDocument/2020/cellImage",
                "cellimages.xml",
            ),
            (
                "rIdRichValue",
                "http://schemas.microsoft.com/office/2017/06/relationships/rdrichvalue",
                "richData/rdrichvalue.xml",
            ),
        ):
            rel = etree.SubElement(workbook_rels, f"{{{PKG_REL_NS}}}Relationship")
            rel.set("Id", rel_id)
            rel.set("Type", rel_type)
            rel.set("Target", rel_target)
        parts["xl/_rels/workbook.xml.rels"] = etree.tostring(
            workbook_rels, xml_declaration=True, encoding="UTF-8", standalone=True
        )

        content_types = etree.fromstring(parts["[Content_Types].xml"])
        for part_name, content_type in (
            (
                "/xl/sharedStrings.xml",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml",
            ),
            ("/xl/drawings/drawing1.xml", "application/vnd.openxmlformats-officedocument.drawing+xml"),
            ("/xl/cellimages.xml", "application/vnd.wps-officedocument.cellimage+xml"),
            ("/xl/richData/rdrichvalue.xml", "application/vnd.ms-excel.rdrichvalue+xml"),
        ):
            override = etree.SubElement(content_types, f"{{{CT_NS}}}Override")
            override.set("PartName", part_name)
            override.set("ContentType", content_type)
        default = etree.SubElement(content_types, f"{{{CT_NS}}}Default")
        default.set("Extension", "png")
        default.set("ContentType", "image/png")
        parts["[Content_Types].xml"] = etree.tostring(
            content_types, xml_declaration=True, encoding="UTF-8", standalone=True
        )

        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in parts.items():
                archive.writestr(name, data)


class XlsxPatcherTests(unittest.TestCase):
    TRANSLATIONS = {
        "施工内容": "Construction scope",
        "很长很长的施工说明文字需要换行才放得下所以行高会被自动调整": (
            "A very long construction note that needs wrapping"
        ),
        "配电箱": "Distribution box",
    }

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.fixture = XlsxPatcherFixture.build(self.root)
        self.before = _part_hashes(self.fixture)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _write(self, **overrides) -> Path:
        kwargs = dict(
            source_path=self.fixture,
            output_dir=self.root / "out",
            translations=dict(self.TRANSLATIONS),
            target_lang="en",
            source_lang="zh",
            keep_original_sheets=False,
            formula_display_value_backfill=False,
            enable_print_guard=False,
        )
        kwargs.update(overrides)
        return write_bilingual_file(**kwargs)

    # ── 部件保真 ──────────────────────────────────────────────────────────
    def test_untouched_parts_are_byte_identical(self) -> None:
        output = self._write()
        after = _part_hashes(output)

        self.assertEqual(set(self.before) - set(after), set(), "输出包丢失了原有部件")

        expected_modified = {
            "xl/worksheets/sheet1.xml",
            "xl/worksheets/sheet2.xml",
            "xl/drawings/drawing1.xml",
            "xl/styles.xml",
        }
        actually_modified = {
            name for name, digest in self.before.items() if after[name] != digest
        }
        self.assertTrue(
            actually_modified <= expected_modified,
            f"意外重写了部件：{sorted(actually_modified - expected_modified)}",
        )
        # 未修改部件必须逐字节一致
        for name in set(self.before) - expected_modified:
            self.assertEqual(after[name], self.before[name], f"部件被改动：{name}")

    def test_embedded_image_parts_survive(self) -> None:
        output = self._write()
        self.assertEqual(_read_part(output, "xl/cellimages.xml"), WPS_CELLIMAGES)
        self.assertEqual(_read_part(output, "xl/richData/rdrichvalue.xml"), RICH_VALUE_PART)
        self.assertEqual(_read_part(output, "xl/media/image1.png"), PNG_BYTES)
        self.assertEqual(
            _read_part(output, "xl/sharedStrings.xml"),
            _read_part(self.fixture, "xl/sharedStrings.xml"),
            "回填走 inlineStr，共享字符串表不应被改写",
        )

    def test_dispimg_and_rich_data_cells_are_untouched(self) -> None:
        output = self._write(formula_display_value_backfill=True)
        root = etree.fromstring(_read_part(output, "xl/worksheets/sheet1.xml"))
        cells = {
            cell.get("r"): cell
            for cell in root.iter(f"{{{NS_MAIN}}}c")
        }

        dispimg = cells["A5"]
        self.assertEqual(
            dispimg.findtext(f"{{{NS_MAIN}}}f"), '_xlfn.DISPIMG("ID_1",1)'
        )
        self.assertEqual(dispimg.findtext(f"{{{NS_MAIN}}}v"), "配电箱")

        rich = cells["B5"]
        self.assertEqual(rich.get("vm"), "1")
        self.assertEqual(rich.get("t"), "s")
        self.assertEqual(rich.findtext(f"{{{NS_MAIN}}}v"), "0")

        # 同样的原文在普通共享字符串单元格里必须照常翻译
        self.assertEqual(cells["C5"].get("t"), "inlineStr")
        self.assertEqual(
            "".join(cells["C5"].itertext()).strip(), "配电箱\nDistribution box"
        )

    # ── 悬浮图片防变形 ────────────────────────────────────────────────────
    def test_floating_image_keeps_original_rendered_size(self) -> None:
        source_sheet = etree.fromstring(_read_part(self.fixture, "xl/worksheets/sheet1.xml"))
        original_heights = {
            int(row.get("r")): float(row.get("ht") or 15.0)
            for row in source_sheet.iter(f"{{{NS_MAIN}}}row")
        }
        expected_cy = int(
            round(sum(original_heights.get(index, 15.0) for index in (1, 2, 3)) * EMU_PER_POINT)
        )

        output = self._write()
        drawing = etree.fromstring(_read_part(output, "xl/drawings/drawing1.xml"))

        self.assertIsNone(drawing.find(f"{{{NS_XDR}}}twoCellAnchor"))
        anchor = drawing.find(f"{{{NS_XDR}}}oneCellAnchor")
        self.assertIsNotNone(anchor, "受影响的悬浮图片应改为 oneCellAnchor")

        children = [etree.QName(child).localname for child in anchor]
        self.assertEqual(children[:2], ["from", "ext"])
        self.assertIn("clientData", children)

        ext = anchor.find(f"{{{NS_XDR}}}ext")
        self.assertEqual(int(ext.get("cy")), expected_cy)
        self.assertGreater(int(ext.get("cx")), 0)

        # 行高确实变了，否则这条断言没有意义
        out_sheet = etree.fromstring(_read_part(output, "xl/worksheets/sheet1.xml"))
        row2 = next(
            row for row in out_sheet.iter(f"{{{NS_MAIN}}}row") if row.get("r") == "2"
        )
        self.assertEqual(row2.get("customHeight"), "1")
        self.assertGreater(float(row2.get("ht")), original_heights.get(2, 15.0))

    def test_locked_row_height_leaves_anchor_alone(self) -> None:
        output = self._write(lock_row_height=True)
        self.assertEqual(
            _read_part(output, "xl/drawings/drawing1.xml"),
            _read_part(self.fixture, "xl/drawings/drawing1.xml"),
            "锁定行高时行高不变，图片锚点不该被动",
        )

    # ── 回填 / 标色 / 行高语义 ────────────────────────────────────────────
    def test_translations_and_review_marks(self) -> None:
        review_positions: list[dict[str, str]] = []
        output = self._write(
            translations={"施工内容": "Construction scope", "配电箱": "配电箱"},
            review_marks={"施工内容": MIXED_MARK_FOREIGN_NOISE},
            review_positions=review_positions,
        )

        workbook = load_workbook(output)
        try:
            sheet = workbook["报价"]
            self.assertEqual(sheet["A1"].value, "施工内容\nConstruction scope")
            self.assertTrue(sheet["A1"].alignment.wrap_text)
            self.assertEqual(sheet["A1"].fill.fill_type, "solid")
            # 译文与原文相同 → 自动标成未解决
            self.assertEqual(sheet["A3"].value, "配电箱")
            self.assertEqual(sheet["A3"].fill.fill_type, "solid")
            # 公式单元格原样保留
            self.assertEqual(sheet["B1"].value, "=SUM(C1:C2)")
            self.assertEqual(sheet["C1"].value, 123)
        finally:
            workbook.close()

        self.assertEqual(
            [(item["worksheet"], item["cell"], item["action"]) for item in review_positions],
            [
                ("报价", "A1", "marked_fill"),
                ("报价", "A3", "marked_fill"),
                ("报价", "C5", "marked_fill"),
                ("附表", "A1", "marked_fill"),
            ],
        )

    def test_existing_fill_policy_skip_preserves_fill(self) -> None:
        review_positions: list[dict[str, str]] = []
        source = self.root / "filled.xlsx"
        shutil.copy2(self.fixture, source)
        workbook = load_workbook(source)
        from openpyxl.styles import PatternFill

        workbook["报价"]["A1"].fill = PatternFill(fill_type="solid", fgColor="FF92D050")
        workbook.save(source)
        workbook.close()

        output = self._write(
            source_path=source,
            review_marks={"施工内容": MIXED_MARK_UNRESOLVED},
            existing_fill_policy="skip",
            review_positions=review_positions,
        )

        written = load_workbook(output)
        try:
            self.assertTrue(str(written["报价"]["A1"].fill.fgColor.rgb).endswith("92D050"))
        finally:
            written.close()
        self.assertIn(
            {
                "worksheet": "报价",
                "cell": "A1",
                "category": MIXED_MARK_UNRESOLVED,
                "action": "preserved_existing_fill",
            },
            review_positions,
        )

    # ── 原文分表克隆 ──────────────────────────────────────────────────────
    def test_keep_original_sheets_clones_images_and_content(self) -> None:
        output = self._write(keep_original_sheets=True)

        workbook = load_workbook(output)
        try:
            self.assertEqual(
                workbook.sheetnames, ["报价", "附表", "报价_原文", "附表_原文"]
            )
            self.assertEqual(workbook["报价_原文"]["A1"].value, "施工内容")
            self.assertEqual(workbook["报价"]["A1"].value, "施工内容\nConstruction scope")
        finally:
            workbook.close()

        with zipfile.ZipFile(output) as archive:
            names = set(archive.namelist())
            content_types = archive.read("[Content_Types].xml").decode()
            clone_rels = None
            for name in names:
                if name.startswith("xl/worksheets/_rels/") and "sheet1" not in name:
                    clone_rels = archive.read(name).decode()

        self.assertIn("xl/worksheets/sheet3.xml", names)
        self.assertIn("xl/drawings/drawing2.xml", names)
        self.assertIn("xl/drawings/_rels/drawing2.xml.rels", names)
        self.assertIn("/xl/worksheets/sheet3.xml", content_types)
        self.assertIn("/xl/drawings/drawing2.xml", content_types)
        self.assertIsNotNone(clone_rels)
        self.assertIn("drawing2.xml", clone_rels)

        # 克隆分表的图片保持原始 twoCellAnchor（原文分表行高也没变）
        self.assertEqual(
            _read_part(output, "xl/drawings/drawing2.xml"),
            _read_part(self.fixture, "xl/drawings/drawing1.xml"),
        )

    def test_long_sheet_name_clone_is_truncated_and_unique(self) -> None:
        source = self.root / "long.xlsx"
        workbook = Workbook()
        workbook.active.title = "A" * 30
        workbook.active["A1"] = "施工内容"
        workbook.create_sheet("B" * 30)["A1"] = "施工内容"
        workbook.save(source)
        workbook.close()

        output = self._write(source_path=source, keep_original_sheets=True)
        written = load_workbook(output)
        try:
            self.assertEqual(len(written.sheetnames), 4)
            for name in written.sheetnames:
                self.assertLessEqual(len(name), 31)
            self.assertEqual(len(set(written.sheetnames)), 4)
        finally:
            written.close()


class ColumnLetterTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        for index, letters in ((1, "A"), (26, "Z"), (27, "AA"), (703, "AAA")):
            self.assertEqual(_column_letter(index), letters)


if __name__ == "__main__":
    unittest.main(verbosity=2)
