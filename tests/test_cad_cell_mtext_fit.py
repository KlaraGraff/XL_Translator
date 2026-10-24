"""CAD：用线画的明细表里每格一段多行文字（不是 ACAD_TABLE），译文也不出格（反馈 #21）。

用户的「油浸式接地变压器明细表」：名称一列每格一段设了宽度的多行文字，英文按中文那么窄
的宽度自动换行，折成三四行压到下一行；备注一列「外购」没设宽度，「Purchased」伸出表格外框。
"""
from __future__ import annotations

import pytest

from core.cad_layout import (
    _mtext_extent,
    _mtext_paragraphs,
    _mtext_raw,
    _wrap_lines,
    _write_mtext_size,
    estimate_text_width,
    mtext_narrowing,
)
from core.cad_translation import _parse_dxf, _plain_text
from tests.test_cad_new_features import _drawing, _entities, _field, _translate

ROW = 8.0
HEIGHT = 3.5
# 列：序号 0–15，名称 15–60，数量 60–75，备注 75–95。
COLUMNS = (0.0, 15.0, 60.0, 75.0, 95.0)
NAMES = {"油样活门": "Oil sampling valve", "中性点导电杆": "Neutral conductor rod", "铭牌": "Lettering plate"}


def _line(x1, y1, x2, y2):
    return (0, "LINE"), (8, "0"), (10, str(x1)), (20, str(y1)), (11, str(x2)), (21, str(y2))


def _mtext(handle, text, *, x, y, width=0.0, attachment=5, height=HEIGHT):
    return (
        (0, "MTEXT"), (5, handle), (330, "1F"), (100, "AcDbEntity"), (8, "0"), (100, "AcDbMText"),
        (10, str(x)), (20, str(y)), (30, "0"), (40, str(height)), (41, str(width)), (71, str(attachment)),
        (1, text), (7, "Standard"),
    )


def _schedule(names=tuple(NAMES), *, remark="外购", name_width=18.0):
    """几行明细：名称居中、设了宽度；备注左对齐、没设宽度；格线都是 LINE。"""
    rows = len(names)
    lines = [_line(COLUMNS[0], -ROW * row, COLUMNS[-1], -ROW * row) for row in range(rows + 1)]
    lines += [_line(x, 0, x, -ROW * rows) for x in COLUMNS]
    cells = []
    for row, name in enumerate(names):
        middle = -ROW * row - ROW / 2
        cells.append(_mtext(f"{0x30 + row * 2:X}", name, x=37.5, y=middle, width=name_width))
        cells.append(_mtext(f"{0x31 + row * 2:X}", remark, x=76.0, y=middle, attachment=4))
    return _drawing(*(record for line in lines for record in line), *(record for cell in cells for record in cell))


def _cells(path):
    return {
        _plain_text("".join(r.value for r in body if r.code in {1, 3})): body for body in _entities(path, "MTEXT")
    }


def _extent(body, font_class="shx"):
    raw = "".join(r.value for r in body if r.code in {1, 3})
    height = float(_field(body, 40))
    return _mtext_extent(
        _mtext_paragraphs(raw, height), height, float(_field(body, 41, 0.0)), font_class, mtext_narrowing(raw),
    )


def _raw(body):
    return "".join(r.value for r in body if r.code in {1, 3})


def _scale(body):
    """整体缩放：字高比例 × 压窄比例。"""
    return float(_field(body, 40)) / HEIGHT * mtext_narrowing(_raw(body))


def _translate_schedule(tmp_path, drawing=None, **options):
    options = {"target_lang": "en", "fit_default_scale": 1.0, "fit_min_scale": 0.3, **options}
    return _translate(tmp_path, drawing or _schedule(), {**NAMES, "外购": "Purchased"}, **options)


@pytest.mark.parametrize(("translation_font", "font_class"), [("original", "shx"), ("compatible", "hershey")])
def test_wrapped_names_get_the_cell_width_and_stay_in_their_row(tmp_path, translation_font, font_class):
    # 修好前：宽度 18 照旧，「Oil sampling valve」折成三行，高出这一行。
    (tmp_path / "before").mkdir()
    name = _cells(_translate_schedule(tmp_path / "before", fit_layout=False, translation_font=translation_font)[1])["Oil sampling valve"]
    assert _extent(name, font_class)[1] > ROW
    result, output, messages = _translate_schedule(tmp_path, translation_font=translation_font)
    cells = _cells(output)
    for translation in NAMES.values():
        body = cells[translation]
        width, total = _extent(body, font_class)
        # 宽度放到名称一列能给的宽度（不越过两边的格线），整段高度在这一行里。
        assert 18 < float(_field(body, 41)) <= COLUMNS[2] - COLUMNS[1]
        assert 37.5 - width / 2 > COLUMNS[1] and 37.5 + width / 2 < COLUMNS[2]
        assert total < ROW
    # 放宽后「Neutral conductor rod」一行还差一点：原字体时和单行文字一样先压窄、字高不变；
    # 同一列三格大小一致。
    sizes = {(_field(cells[name], 40), mtext_narrowing(_raw(cells[name]))) for name in NAMES.values()}
    assert len(sizes) == 1
    height, narrowing = sizes.pop()
    assert float(height) / HEIGHT * narrowing < 1
    if font_class == "shx":
        assert float(height) == pytest.approx(HEIGHT) and 0.7 <= narrowing < 1
    assert result.stats.layout_unresolved == 0
    assert any("已缩小字宽或字高" in message for message in messages)


def test_unwrapped_remark_is_shrunk_inside_the_outer_frame(tmp_path):
    result, output, _messages = _translate_schedule(tmp_path)
    remarks = [body for body in _entities(output, "MTEXT") if "Purchased" in "".join(r.value for r in body if r.code == 1)]
    assert len(remarks) == len(NAMES)
    for body in remarks:
        width, total = _extent(body)
        assert 76.0 + width < COLUMNS[-1]
        assert total < ROW
        # 一个词放不下：先压窄，字高不变。
        assert mtext_narrowing(_raw(body)) < 1 and float(_field(body, 40)) == pytest.approx(HEIGHT)
    assert result.stats.layout_unresolved == 0


def test_text_that_cannot_fit_even_at_the_minimum_is_reported(tmp_path):
    long_name = "Neutral conductor rod with insulating sleeve and terminal clamp assembly for transformer"
    drawing = _schedule(("中性点导电杆",))
    result, output, messages = _translate(
        tmp_path, drawing, {"中性点导电杆": long_name, "外购": "Purchased"},
        target_lang="en", fit_default_scale=1.0, fit_min_scale=0.5,
    )
    body = _cells(output)[long_name]
    assert _scale(body) == pytest.approx(0.5)
    assert result.stats.layout_unresolved == 1
    assert any("缩到最小比例仍会压到旁边的文字或伸出图框" in message for message in messages)


def test_mtext_without_lines_around_it_is_left_alone(tmp_path):
    drawing = _drawing(*_mtext("30", "油样活门", x=37.5, y=-4, width=12.0))
    result, output, _messages = _translate(tmp_path, drawing, NAMES, target_lang="en")
    body = _cells(output)["Oil sampling valve"]
    assert _field(body, 40) == str(HEIGHT) and _field(body, 41) == "12.0"
    assert result.stats.layout_fitted == 0


def test_cell_that_already_fits_is_not_touched(tmp_path):
    result, output, _messages = _translate(
        tmp_path, _schedule(("铭牌",), remark="", name_width=40.0), {"铭牌": "Plate"}, target_lang="en",
    )
    body = _cells(output)["Plate"]
    assert _field(body, 40) == str(HEIGHT) and _field(body, 41) == "40.0"
    assert result.stats.layout_fitted == 0


def test_bilingual_cell_keeps_both_lines_in_the_row(tmp_path):
    result, output, _messages = _translate_schedule(tmp_path, display_mode="source_first")
    for body in _entities(output, "MTEXT"):
        raw = "".join(r.value for r in body if r.code in {1, 3})
        if "Oil sampling valve" in raw:
            assert _extent(body)[1] < ROW
            break
    else:
        raise AssertionError("bilingual cell not found")
    assert result.stats.layout_unresolved == 0


def test_wrap_counts_lines_by_words_and_chinese_characters():
    one = estimate_text_width("Oil", 1, 1.0)
    assert _wrap_lines("Oil sampling valve", 1, 0, "shx")[0] == 1
    count, widest = _wrap_lines("Oil sampling valve", 1, one * 1.2, "shx")
    # 一个词比宽度还宽时不从中间断开。
    assert count == 3 and widest == pytest.approx(estimate_text_width("sampling", 1, 1.0))
    assert _wrap_lines("油样活门", 1, estimate_text_width("油样", 1, 1.0) * 1.1, "shx")[0] == 2


def test_text_with_its_own_width_code_only_gets_lower(tmp_path):
    # 原文自己带了 \\W 码：整段压窄会被它盖掉，只降字高。
    drawing = _drawing(
        *(record for x in (15, 60) for record in _line(x, 0, x, -ROW)),
        *_line(15, 0, 60, 0), *_line(15, -ROW, 60, -ROW),
        *_mtext("30", "\\W1.2;中性点导电杆", x=37.5, y=-ROW / 2, width=18.0),
    )
    _result, output, _messages = _translate(tmp_path, drawing, {"中性点导电杆": "Neutral conductor rod assembly"},
                                            target_lang="en", fit_default_scale=1.0, fit_min_scale=0.3)
    body = _entities(output, "MTEXT")[0]
    assert _raw(body).startswith("\\W1.2;") and float(_field(body, 40)) < HEIGHT


def test_narrowing_code_is_written_before_long_content_split_in_chunks():
    text = "Lettering plate " * 20
    records = _parse_dxf(_drawing(*_mtext("30", text[:250], x=0, y=0)[:-2], (3, text[:250]), (1, text[250:]),
                                  (7, "Standard")))
    start = next(i for i, r in enumerate(records) if r.code == 0 and r.value == "MTEXT")
    end = next(i for i in range(start + 1, len(records)) if records[i].code == 0)
    _write_mtext_size(records, start, end, 2.0, None, 0.7)
    end = next(i for i in range(start + 1, len(records)) if records[i].code == 0)
    chunks = [r for r in records[start + 1:end] if r.code in {1, 3}]
    assert all(len(r.value) <= 250 for r in chunks) and chunks[-1].code == 1
    assert _mtext_raw(records, start, end) == "\\W0.7;" + text
