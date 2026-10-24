"""CAD：表格格子不会跟着译文变高，双语多出的一行、折成几行的英文按格子高度整段缩小。

格子尺寸照地磅基础图「埋件明细表」：字高 250，行高约 483，「名称」一列宽约 1220。
"""
from __future__ import annotations

import pytest

from core.cad_layout import FitSettings, _mtext_paragraphs, estimate_text_width, fit_table_cells, mtext_narrowing
from core.cad_translation import _parse_dxf, _plain_text
from tests.test_cad_new_features import _drawing, _entities, _field, _translate

ROW = 483.0


def _line(handle, y, x1=0.0, x2=5480.0):
    return (
        (0, "LINE"), (5, handle), (330, "2F"), (100, "AcDbEntity"), (8, "0"), (100, "AcDbLine"),
        (10, str(x1)), (20, str(y)), (30, "0"), (11, str(x2)), (21, str(y)), (31, "0"),
    )


def _cell(handle, text, y, *, x=1924.3, width=1221.5):
    return (
        (0, "MTEXT"), (5, handle), (330, "2F"), (100, "AcDbEntity"), (8, "0"), (100, "AcDbMText"),
        (10, str(x)), (20, str(y)), (30, "0"), (40, "250"), (41, str(width)), (71, "5"), (1, text),
        (7, "Standard"),
    )


def _table_block():
    """三行格子：每行中间一段多行文字，上下是横向格线。"""
    return (
        (0, "BLOCK"), (5, "2E"), (330, "2F"), (100, "AcDbEntity"), (8, "0"), (100, "AcDbBlockBegin"),
        (2, "*T4"), (70, "1"), (10, "0"), (20, "0"), (30, "0"), (3, "*T4"), (1, ""),
        *_cell("31", "基础板", -ROW / 2),
        *_cell("32", "护边角铁", -ROW * 1.5),
        *_cell("33", "M1", -ROW * 2.5, x=345.4, width=687.8),
        *_line("41", 0), *_line("42", -ROW), *_line("43", -ROW * 2), *_line("44", -ROW * 3),
        (0, "ENDBLK"), (5, "34"), (330, "2F"), (100, "AcDbEntity"), (8, "0"), (100, "AcDbBlockEnd"),
    )


def _table():
    def cell(text):
        return (
            (171, "1"), (172, "0"), (173, "0"), (174, "0"), (175, "1"), (176, "1"), (91, "262177"),
            (178, "0"), (145, "0.0"), (92, "0"), (1, text),
            (301, "CELL_VALUE"), (93, "6"), (90, "4"), (1, text), (94, "0"), (300, ""), (302, text),
            (304, "ACVALUE_END"),
        )
    return (
        (0, "ACAD_TABLE"), (5, "40"), (330, "1F"), (100, "AcDbEntity"), (8, "0"),
        (100, "AcDbBlockReference"), (2, "*T4"), (10, "0"), (20, "0"), (30, "0"),
        (100, "AcDbTable"), (280, "0"), (342, "3A"), (343, "2F"), (11, "1"), (21, "0"), (31, "0"),
        (90, "22"), (91, "3"), (92, "1"), (93, "0"), (94, "0"), (95, "0"), (96, "0"),
        (141, str(ROW)), (141, str(ROW)), (141, str(ROW)), (142, "5480"),
        *cell("基础板"), *cell("护边角铁"), *cell("M1"),
    )


TRANSLATIONS = {"基础板": "Base Plate", "护边角铁": "Edge protection angle steel"}


def _cell_heights(path):
    return {
        _plain_text("".join(r.value for r in body if r.code in {1, 3})).split("\n")[0]: float(_field(body, 40))
        for body in _entities(path, "MTEXT")
    }


def test_bilingual_cell_text_is_shrunk_to_fit_its_row(tmp_path):
    result, output, _messages = _translate(
        tmp_path, _drawing(*_table(), blocks=_table_block()), TRANSLATIONS,
        display_mode="source_first", translation_font="original",
    )
    heights = _cell_heights(output)
    # 两行（中文 + 小一号英文）放进一行格子：缩到默认的 0.7。
    assert heights["基础板"] == 175
    # 英文还要折成两行：一档档缩到最小比例 0.4 才放得下，不记进报告。
    assert heights["护边角铁"] == 100
    # 没改的格子不动。
    assert heights["M1"] == 250
    assert result.stats.layout_unresolved == 0


def test_cell_that_still_overflows_at_the_minimum_is_reported(tmp_path):
    result, output, _messages = _translate(
        tmp_path, _drawing(*_table(), blocks=_table_block()), TRANSLATIONS,
        display_mode="source_first", translation_font="original", fit_min_scale=0.6,
    )
    assert _cell_heights(output)["护边角铁"] == 150
    assert result.stats.layout_unresolved == 1


def test_translation_only_cell_shrinks_when_the_wrapped_text_overflows(tmp_path):
    _result, output, _messages = _translate(
        tmp_path, _drawing(*_table(), blocks=_table_block()), {**TRANSLATIONS, "基础板": "Plaque"},
        display_mode="translation", translation_font="original",
    )
    heights = _cell_heights(output)
    assert heights["Plaque"] == 250  # 一行放得下，不缩
    assert heights["Edge protection angle steel"] == 125  # 折成几行放不下，缩到放得下为止


def test_layout_fitting_off_leaves_cells_alone(tmp_path):
    _result, output, _messages = _translate(
        tmp_path, _drawing(*_table(), blocks=_table_block()), TRANSLATIONS,
        display_mode="source_first", translation_font="original", fit_layout=False,
    )
    assert set(_cell_heights(output).values()) == {250}


def test_text_outside_tables_is_not_touched():
    drawing = _drawing(*_cell("31", "基础板\\P{\\H0.8x;Base Plate}", -ROW / 2), *_line("41", 0), *_line("42", -ROW))
    records = _parse_dxf(drawing)
    ordinal = sum(1 for record in records[: next(i for i, r in enumerate(records) if r.value == "MTEXT")] if record.code == 0)
    assert fit_table_cells(records, {ordinal: "基础板"}, _plain_text, FitSettings()) == 0


def test_paragraph_heights_follow_relative_and_absolute_codes():
    assert _mtext_paragraphs("基础板\\P{\\H0.8x;Base Plate}", 250) == [(1.0, "基础板"), (0.8, "Base Plate")]
    assert _mtext_paragraphs("{\\H125;小字}\\P{\\W0.8;300×300}", 250) == [(0.5, "小字"), (1.0, "300×300")]


def test_bilingual_skips_codes_and_sizes_that_translate_to_themselves(tmp_path):
    """编号、规格译前译后一样（M1、300×300×20 → 300X300X20），双语时不再在下面重复一遍。"""
    result, output, _messages = _translate(
        tmp_path, _drawing(*_table(), blocks=_table_block()),
        {**TRANSLATIONS, "M1": "M1", "护边角铁": "护边角铁 "},
        display_mode="source_first", translation_font="original",
    )
    texts = {_field(body, 1) for body in _entities(output, "MTEXT")}
    assert "M1" in texts
    assert "护边角铁" in texts
    assert "基础板\\P{\\H0.8x;Base Plate}" in texts
    table = _entities(output, "ACAD_TABLE")[0]
    assert {record.value for record in table if record.code == 1} >= {"M1", "护边角铁"}
    assert result.stats.changed == 1


def test_one_long_word_is_shrunk_until_it_fits_the_cell_width(tmp_path):
    # 地磅基础图材料表表头「数量」→「Quantity」：CAD 不会把一个词折开，以前按折成两行算高度，
    # 判成放得下，单词照样伸过竖线压到旁边的表头。
    _result, output, _messages = _translate(
        tmp_path, _drawing(*_table(), blocks=_table_block()).replace("\nM1\n", "\n数量\n"),
        {**TRANSLATIONS, "数量": "Quantity"}, display_mode="translation", translation_font="original",
    )
    body = next(body for body in _entities(output, "MTEXT") if "Quantity" in _field(body, 1))
    raw = _field(body, 1)
    width = estimate_text_width("Quantity", float(_field(body, 40)), mtext_narrowing(raw))
    assert width <= 687.8
    # 先压窄（字高降得少一些）。
    assert mtext_narrowing(raw) == pytest.approx(0.7)
