"""补译模式必须保留公式单元格：高-6 回归测试。

复现路径：`core/excel_coverage.py` 把公式格的文本（显示值或公式源码，取决于
``formula_display_value_backfill``）当成普通单元格文本去分类，命中
``COVERAGE_SOURCE_ONLY`` 后送去翻译；写盘时 `core/xlsx_patcher.py` 用同一把 key
命中这一格，`_set_cell_inline_text` 删掉 ``<f>`` 换成静态译文——公式永久丢失，
还白花一次 API 调用去翻译公式源码或其显示值。

两个方向都要盖到：
  * ``formula_display_value_backfill=True``（默认）：公式缓存值本身含源语言文本；
  * ``formula_display_value_backfill=False``（审查报告标题里「关闭公式回填」的
    那一支）：公式源码字符串被当成源文本。
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from lxml import etree
from openpyxl import Workbook, load_workbook

from core.excel_coverage import build_excel_coverage_plan, write_untranslated_excel_file
from core.translation_coverage import COVERAGE_IGNORED, COVERAGE_SOURCE_ONLY
from core.xlsx_patcher import NS_MAIN, write_bilingual_workbook


def _m(tag: str) -> str:
    return f"{{{NS_MAIN}}}{tag}"


def _build_fixture(root: Path) -> Path:
    """一张真实 xlsx：A1 是待补译的普通源文本，A2 是公式格。

    openpyxl 保存公式格时只写 ``<f>``，不带缓存值——真实 Excel 保存过的文件
    才会带 ``<v>``。这里存完后用 lxml 补一个缓存值上去，模拟“Excel 算过一次”
    的公式格：显示值是 "施工内容合计"（含源语言文本，能触发误判）。
    """
    base = root / "base.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "报价"
    sheet["A1"] = "施工内容"
    sheet["A2"] = '=A1&"合计"'
    workbook.save(base)
    workbook.close()

    with zipfile.ZipFile(base) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}

    sheet_xml = etree.fromstring(parts["xl/worksheets/sheet1.xml"])
    formula_cell = sheet_xml.find(f".//{_m('c')}[@r='A2']")
    formula_cell.set("t", "str")
    v_el = formula_cell.find(_m("v"))
    if v_el is None:
        v_el = etree.SubElement(formula_cell, _m("v"))
    v_el.text = "施工内容合计"
    parts["xl/worksheets/sheet1.xml"] = etree.tostring(
        sheet_xml, xml_declaration=True, encoding="UTF-8", standalone=True
    )

    target = root / "fixture.xlsx"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in parts.items():
            archive.writestr(name, data)
    return target


def _formula_text(path: Path, sheet: str, coordinate: str) -> str | None:
    """直接读原始 XML 的 ``<f>``，不经过 openpyxl 的 data_only 折叠。"""
    with zipfile.ZipFile(path) as archive:
        wb = load_workbook(path, read_only=True)
        try:
            index = wb.sheetnames.index(sheet) + 1
        finally:
            wb.close()
        xml = archive.read(f"xl/worksheets/sheet{index}.xml")
    root = etree.fromstring(xml)
    cell = root.find(f".//{_m('c')}[@r='{coordinate}']")
    if cell is None:
        return None
    f_el = cell.find(_m("f"))
    return f_el.text if f_el is not None else None


class ExcelBackfillFormulaTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.fixture = _build_fixture(self.root)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _run_full_chain(self, *, formula_display_value_backfill: bool) -> Path:
        plan = build_excel_coverage_plan(
            self.fixture,
            target_lang="en",
            source_lang="zh",
            formula_display_value_backfill=formula_display_value_backfill,
        )

        # 公式格绝不能进入待译集合，不管显示值还是公式源码看起来多像源文本。
        formula_unit = next(
            unit for unit in plan.units if unit.data.get("coordinate") == "A2"
        )
        self.assertEqual(formula_unit.status, COVERAGE_IGNORED)
        self.assertNotIn(formula_unit.source_text, plan.source_texts)
        self.assertTrue(
            all(u.data.get("coordinate") != "A2" for u in plan.source_units)
        )

        # 非公式格照常判定为待补译。
        plain_unit = next(
            unit for unit in plan.units if unit.data.get("coordinate") == "A1"
        )
        self.assertEqual(plain_unit.status, COVERAGE_SOURCE_ONLY)
        self.assertIn("施工内容", plan.source_texts)

        translations = {text: f"[EN]{text}" for text in plan.source_texts}
        # 万一实现有疏漏、公式的源文本真被送去"翻译"了，也让写入端有值可用，
        # 这样测试断言公式被保留就不会因为 KeyError 而意外通过。
        translations.setdefault(formula_unit.source_text, "[EN]" + formula_unit.source_text)

        out_path = write_untranslated_excel_file(
            source_path=self.fixture,
            output_dir=self.root / f"out_{formula_display_value_backfill}",
            plan=plan,
            translations=translations,
            target_lang="en",
            source_lang="zh",
            keep_original_sheets=False,
            formula_display_value_backfill=formula_display_value_backfill,
        )
        return out_path

    def test_formula_preserved_with_backfill_on(self) -> None:
        out_path = self._run_full_chain(formula_display_value_backfill=True)

        formula_text = _formula_text(out_path, "报价", "A2")
        self.assertIsNotNone(formula_text, "A2 的 <f> 元素被删掉了，公式丢失")
        self.assertEqual(formula_text, 'A1&"合计"')

        wb = load_workbook(out_path)
        try:
            self.assertEqual(wb["报价"]["A1"].value, "施工内容\n[EN]施工内容")
        finally:
            wb.close()

    def test_formula_preserved_with_backfill_off(self) -> None:
        out_path = self._run_full_chain(formula_display_value_backfill=False)

        formula_text = _formula_text(out_path, "报价", "A2")
        self.assertIsNotNone(formula_text, "A2 的 <f> 元素被删掉了，公式丢失")
        self.assertEqual(formula_text, 'A1&"合计"')

        wb = load_workbook(out_path)
        try:
            self.assertEqual(wb["报价"]["A1"].value, "施工内容\n[EN]施工内容")
        finally:
            wb.close()

    # ── finding #15：绕过覆盖率层，直接考 xlsx_patcher 自己那层公式守卫 ──────

    def test_second_formula_guard_blocks_even_when_coverage_layer_bypassed(self) -> None:
        """覆盖率层（core/excel_coverage.py）本该先把公式格挡在补译坐标集合外，
        上面两个测试验证的就是那一层。但 core/xlsx_patcher.py 的
        write_bilingual_workbook 自己也留了一道独立兜底（约 1352 行附近：
        ``if allowed_coordinates is not None and formula_el is not None:
        position_allowed = False``）——不管调用方传来的坐标集合里有没有公式格，
        公式格永远不可写。这条测试直接调 write_bilingual_workbook，故意把公式
        格的坐标塞进 allowed_positions（模拟坐标计算在别处出错、越过了覆盖率层
        的场景），验证这道独立守卫真的兜住了，不依赖上游算对。
        """
        out_path = self.root / "bypass_out.xlsx"
        shutil.copy(self.fixture, out_path)

        write_bilingual_workbook(
            out_path,
            translations={
                "施工内容": "[EN]施工内容",
                # 公式格的显示值也给出「译文」，这样如果守卫没拦住，mutation
                # 一定会发生（不是恰好没命中翻译表才侥幸没改）。
                "施工内容合计": "[EN]施工内容合计",
            },
            target_lang="en",
            source_lang="zh",
            keep_original_sheets=False,
            formula_display_value_backfill=True,
            mark_review_items=False,
            # 故意把公式格 A2 也放进允许坐标集合——覆盖率层永远不会这样传，
            # 这里是直接白盒测试 xlsx_patcher 自己的独立兜底。
            allowed_positions={"报价": {"A1", "A2"}},
        )

        formula_text = _formula_text(out_path, "报价", "A2")
        self.assertIsNotNone(formula_text, "A2 的 <f> 元素被删掉了，公式丢失")
        self.assertEqual(formula_text, 'A1&"合计"')

        wb = load_workbook(out_path)
        try:
            # A2 仍然是公式，没有被坐标限定牵着改写成静态双语文本。
            self.assertEqual(wb["报价"]["A2"].value, '=A1&"合计"')
            # A1（非公式格）该翻的照常翻了——证明公式格是被专门挡下来的，
            # 不是 allowed_positions 机制整体失效导致两格都没写。
            self.assertEqual(wb["报价"]["A1"].value, "施工内容\n[EN]施工内容")
        finally:
            wb.close()


if __name__ == "__main__":
    unittest.main()
