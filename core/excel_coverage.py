"""Excel coverage detection and position-based untranslated-only writing."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core import bilingual_writer, xlsx_patcher
from core.language_registry import get_target_lang_display
from core.translation_coverage import (
    COVERAGE_AMBIGUOUS,
    COVERAGE_COVERED,
    COVERAGE_IGNORED,
    COVERAGE_SOURCE_ONLY,
    CoverageUnit,
    clean_coverage_text,
    coverage_summary,
    looks_like_source_text,
    looks_like_target_text,
    split_existing_bilingual_text,
)


@dataclass
class ExcelCoveragePlan:
    path: Path
    units: list[CoverageUnit]
    sheet_count: int

    @property
    def source_units(self) -> list[CoverageUnit]:
        return [unit for unit in self.units if unit.status == COVERAGE_SOURCE_ONLY]

    @property
    def source_texts(self) -> list[str]:
        seen: set[str] = set()
        texts: list[str] = []
        for unit in self.source_units:
            source = unit.source_text.strip()
            if source and source not in seen:
                seen.add(source)
                texts.append(source)
        return texts

    @property
    def summary(self) -> dict[str, int]:
        return coverage_summary(self.units)


def build_excel_coverage_plan(
    path: str | Path,
    *,
    target_lang: str,
    source_lang: str = "zh",
    formula_display_value_backfill: bool = True,
) -> ExcelCoveragePlan:
    """Classify app-style bilingual Excel cells by coverage status."""
    from openpyxl import load_workbook

    source_path = Path(path)
    wb = load_workbook(str(source_path), read_only=True, data_only=False)
    # 第二次 load 也会失败（同一个文件损坏、内存不够、被别的进程锁住），失败时
    # 第一个工作簿还开着——read_only 的 openpyxl 抱着 zip 文件句柄不放，异常一路
    # 抛出去就再没人关得掉它。Windows 上这个句柄还会让后续的改名/删除直接失败。
    try:
        wb_values = (
            load_workbook(str(source_path), read_only=True, data_only=True)
            if formula_display_value_backfill
            else None
        )
    except BaseException:
        wb.close()
        raise
    units: list[CoverageUnit] = []
    try:
        workbook_sheet_names = set(wb.sheetnames)
        for ws in wb.worksheets:
            if _is_generated_original_sheet(ws.title, workbook_sheet_names):
                continue
            # 公式格的显示值一次性读成内存映射。read_only 工作表的
            # ``ws[coord]`` 每次都要重新解析整张 sheet XML，逐格去问是 O(n²)：
            # 实测 1600 个公式格就要 7.7s，万级公式表直接卡到分钟级。
            values_by_coordinate = (
                _DisplayValues(wb_values[ws.title]) if wb_values is not None else None
            )
            for row in ws.iter_rows():
                for cell in row:
                    raw = _resolve_cell_text(
                        cell, values_by_coordinate, formula_display_value_backfill
                    )
                    if raw is None:
                        continue
                    unit = _classify_excel_cell(
                        raw,
                        sheet_name=ws.title,
                        coordinate=cell.coordinate,
                        source_lang=source_lang,
                        target_lang=target_lang,
                        is_formula=getattr(cell, "data_type", None) == "f",
                        formula_display_value_backfill=formula_display_value_backfill,
                    )
                    if unit is not None:
                        units.append(unit)
        return ExcelCoveragePlan(
            path=source_path,
            units=units,
            sheet_count=len(wb.worksheets),
        )
    finally:
        wb.close()
        if wb_values is not None:
            wb_values.close()


def write_untranslated_excel_file(
    *,
    source_path: str | Path,
    output_dir: str | Path,
    plan: ExcelCoveragePlan,
    translations: dict[str, str],
    target_lang: str,
    source_lang: str = "zh",
    keep_original_sheets: bool = True,
    formula_display_value_backfill: bool = True,
    lock_row_height: bool = False,
    review_marks: dict[str, str] | None = None,
    review_mark_colors: dict[str, str] | None = None,
    mark_review_items: bool = False,
    existing_fill_policy: str | None = None,
    review_positions: list[dict[str, str]] | None = None,
    log_callback=None,
    original_path: Path | None = None,
    external_autofit_planned: bool = False,
    stats: dict[str, object] | None = None,
) -> Path:
    """Copy an Excel file and patch translations only at plan-limited source-only positions.

    坐标限定 + 文本匹配双重守卫都交给 ``xlsx_patcher.write_bilingual_workbook`` 的
    ``allowed_positions`` 机制完成：只有 ``plan.source_units`` 里的 (sheet, coordinate)
    才允许改写，且写入时会用当前单元格的实际文本重新核对是否仍匹配 ``translations``
    的键，防止误伤已经变化的单元格或同文本的其它位置。

    复核改判过的单元格（格里已经躺着一条可疑译文）按格子的**完整文字**建键：补上的
    新译文接在整格内容后面，原有的原文和那条可疑译文一个字都不删。删掉的话，万一模型
    判错了，用户连"原来写的是什么"都看不到；留着最多是一格里两条译文，肉眼一比就知道
    该留哪条。底色也跟着按完整文字建一份键，否则标记落不到这一格上。
    """
    source_path = Path(source_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    lang_display = bilingual_writer._sanitize_filename_fragment(
        get_target_lang_display(target_lang, include_optional=True)
    )
    basename = original_path.name if original_path else source_path.name
    if basename.lower().endswith(".xls"):
        basename = basename[:-4] + ".xlsx"
    out_path = output_dir / bilingual_writer.bilingual_output_name(basename, lang_display)

    allowed_positions: dict[str, set[str]] = {}
    scoped_translations: dict[str, str] = {}
    scoped_marks: dict[str, str] = dict(review_marks or {})
    for unit in plan.source_units:
        sheet_name = str(unit.data.get("sheet") or "")
        coordinate = str(unit.data.get("coordinate") or "")
        if not sheet_name or not coordinate:
            continue
        source_key = unit.source_text.strip()
        translation = str(translations.get(source_key) or "").strip()
        if not translation:
            continue
        allowed_positions.setdefault(sheet_name, set()).add(coordinate)
        scoped_translations[source_key] = translation
        # 复核改判过来的格子：格里是「原文＋可疑译文」，写入器核对的是这一整串。
        cell_key = str(unit.data.get("cell_text") or "").strip()
        if cell_key and cell_key != source_key:
            scoped_translations[cell_key] = translation
            mark = scoped_marks.get(source_key)
            if mark:
                scoped_marks[cell_key] = mark

    # 调用方传了 stats 就直接写进去，省一次拷贝；没传就用本地临时字典。
    if stats is None:
        stats = {}
    # 补丁失败时不能在输出目录里留下一个「看起来正常」的未翻译副本，
    # 所以先在临时文件上打补丁，成功了再改名。见 bilingual_writer.patch_into_output。
    bilingual_writer.patch_into_output(
        source_path,
        out_path,
        lambda staging: xlsx_patcher.write_bilingual_workbook(
            staging,
            translations=scoped_translations,
            target_lang=target_lang,
            source_lang=source_lang,
            keep_original_sheets=keep_original_sheets,
            formula_display_value_backfill=formula_display_value_backfill,
            lock_row_height=lock_row_height,
            review_marks=scoped_marks,
            review_mark_colors=review_mark_colors,
            mark_review_items=mark_review_items,
            existing_fill_policy=xlsx_patcher.normalize_existing_fill_policy(
                existing_fill_policy or ""
            ),
            review_positions=review_positions,
            log_callback=log_callback,
            allowed_positions=allowed_positions,
            external_autofit_planned=external_autofit_planned,
            stats=stats,
        ),
    )
    write_count = stats.get("mutated_cells", 0)

    if log_callback:
        log_callback(f"[OK] 已输出：{out_path.name}（补译 {write_count} 个单元格）")
    return out_path


def _classify_excel_cell(
    raw: str,
    *,
    sheet_name: str,
    coordinate: str,
    source_lang: str,
    target_lang: str,
    is_formula: bool = False,
    formula_display_value_backfill: bool = True,
) -> CoverageUnit | None:
    text = clean_coverage_text(raw)
    if not text:
        return None

    location = f"{sheet_name}!{coordinate}"
    # cell_text 是这一格当时的完整文字。补译复核会把「原文＋译文挤在同一格」的单元格
    # 打回重译，那种 unit 的 source_text 只是格里的原文那一半，写入器按它去核对格子会
    # 对不上（格里还有旧译文）——认这一条。
    data = {"sheet": sheet_name, "coordinate": coordinate, "cell_text": text}

    if is_formula and not formula_display_value_backfill:
        # 公式格的产品约定：译文以「值」覆盖公式——原公式在「_原文」分表和原始
        # 文件里都完好，所以「公式显示值回填」开着（默认）时公式格按显示值走下面
        # 的普通分类，补译与全量同一条规则。只有用户明确关掉回填才保护公式：此时
        # 这一格的「文本」是公式源码，送翻是白花一次 API 调用译公式源码，写回还会
        # 把 <f> 删掉换成静态译文，两头都不该发生，直接判 ignored。
        return CoverageUnit(
            source_text=text,
            status=COVERAGE_IGNORED,
            location=location,
            kind="cell",
            reason="公式单元格：公式显示值回填已关闭，不覆盖以保留公式。",
            data=data,
        )

    split = split_existing_bilingual_text(
        text,
        source_lang=source_lang,
        target_lang=target_lang,
    )
    if split is not None:
        source, target = split
        return CoverageUnit(
            source_text=source,
            target_text=target,
            status=COVERAGE_COVERED,
            location=location,
            kind="cell",
            reason="同一单元格已包含源文和目标语言译文。",
            data=data,
        )

    if looks_like_source_text(text, source_lang=source_lang, target_lang=target_lang):
        return CoverageUnit(
            source_text=text,
            status=COVERAGE_SOURCE_ONLY,
            location=location,
            kind="cell",
            reason="单元格包含源语言文本，未识别到目标语言译文。",
            data=data,
        )

    if looks_like_target_text(text, source_lang=source_lang, target_lang=target_lang):
        return CoverageUnit(
            source_text="",
            target_text=text,
            status=COVERAGE_IGNORED,
            location=location,
            kind="cell",
            reason="单元格看起来是目标语言译文，默认跳过。",
            data=data,
        )

    if len(text.splitlines()) > 1:
        return CoverageUnit(
            source_text=text,
            status=COVERAGE_AMBIGUOUS,
            location=location,
            kind="cell",
            reason="多行单元格无法可靠拆分源文和译文。",
            data=data,
        )

    return CoverageUnit(
        source_text=text,
        status=COVERAGE_IGNORED,
        location=location,
        kind="cell",
        reason="单元格不符合补译候选规则。",
        data=data,
    )


class _DisplayValues:
    """一张分表的公式显示值，整表读一次、之后按坐标查表。

    openpyxl 的 read_only 工作表不缓存单元格：``ws["B7"]`` 每次都从头重新解析
    这张分表的 XML。逐个公式格去问就成了 O(n²)。这里在第一次真正需要显示值时
    整表遍历一遍，之后都是字典查询。没有公式格的分表连这一遍都不会跑。
    """

    def __init__(self, worksheet) -> None:
        self._worksheet = worksheet
        self._values: dict[str, str] | None = None

    def get(self, coordinate: str) -> str | None:
        if self._values is None:
            # 错误值公式格（算出来是 #N/A / #REF! 的格）在 data_only 视图里的
            # 「值」就是那串错误符号本身，也是字符串。进了显示值映射就会被当成
            # 正文送翻——写入端拦得住改写（_is_error_cell），但词条那次 API 调用
            # 的钱已经花了，补译报告还会把它算成「未翻译内容」。这里直接不收。
            self._values = {
                cell.coordinate: cell.value
                for row in self._worksheet.iter_rows()
                for cell in row
                if isinstance(cell.value, str)
                and getattr(cell, "data_type", None) != "e"
            }
        return self._values.get(coordinate)


def _resolve_cell_text(
    cell,
    display_values: _DisplayValues | None,
    formula_display_value_backfill: bool,
) -> str | None:
    value = cell.value
    if not isinstance(value, str):
        return None
    if getattr(cell, "data_type", None) == "e":
        # 错误值格（``<c t="e">``）的「值」就是 ``#N/A`` / ``#REF!`` 这串符号本身，
        # openpyxl 把它读成字符串，于是整条管线都把它当正文：排进待译词条 →
        # 花一次 API 调用译成「不适用」→ 写入端按同一把键命中这一格。写入端已经
        # 挡住了改写（xlsx_patcher._is_error_cell），这里连词条都不该收——收下就是
        # 白花钱，还会让补译报告把这一格算成「未翻译内容」。
        return None
    if getattr(cell, "data_type", None) != "f":
        return value
    if xlsx_patcher.is_dispimg_formula_text(value):
        # WPS 的 DISPIMG「嵌入图片」格：格里的公式是图片引用，缓存显示值只是
        # 占位文本。写入层（xlsx_patcher）对这类格无条件跳过、永远写不进去——
        # 把它的显示值排进补译候选，等于花一次 API 调用换一个永远清不掉的
        # 「未翻译」计数（续译弹窗每次都报同样的数字）。两个开关分支都不收。
        return None
    if not formula_display_value_backfill:
        return value
    if display_values is None:
        return None
    return display_values.get(cell.coordinate)


def _is_generated_original_sheet(sheet_name: str, workbook_sheet_names: set[str]) -> bool:
    """这张分表是不是我们自己上一轮克隆出来的「_原文」分表（补译时要跳过）。

    判据交给生成方 ``xlsx_patcher``，它才知道分表名是怎么被 31 字符上限截断、
    怎么加去重后缀的。这里不做任何字符串反推——原名超 27 字符时「_原文」会被
    截没，反推必然落空，结果就是把自己生成的原文分表当成待译内容重翻一遍。
    """
    return xlsx_patcher.is_generated_original_sheet_title(
        sheet_name, workbook_sheet_names
    )
