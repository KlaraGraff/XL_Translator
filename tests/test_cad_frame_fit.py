"""CAD：译文不伸出图框、不越出格子（原文四周的实线，含插入的图块里画的），相邻长译文互相让开。"""
from __future__ import annotations

import pytest

from core.cad_layout import _line_segments, estimate_text_width
from core.cad_translation import _parse_dxf
from tests.test_cad_new_features import _drawing, _dxf, _entities, _field, _text, _translate

NOTE = "说明"
LONG = "Les dimensions sont indiquées en millimètres"


def _lwpolyline(points, *, closed=True, bulge=None):
    pairs = [(0, "LWPOLYLINE"), (8, "0"), (90, str(len(points))), (70, "1" if closed else "0")]
    for x, y in points:
        pairs += [(10, str(x)), (20, str(y))]
        if bulge is not None:
            pairs.append((42, str(bulge)))
    return tuple(pairs)


def _frame_block(name="TK-A2", *, base=(0, 0), rect=((0, 0), (100, 0), (100, 50), (0, 50)), **polyline):
    return (
        (0, "BLOCK"), (8, "0"), (2, name), (70, "0"), (10, str(base[0])), (20, str(base[1])), (30, "0"),
        *_lwpolyline(rect, **polyline),
        (0, "ENDBLK"),
    )


def _insert(name="TK-A2", *, x=0, y=0, scale=1, rotation=0):
    return (
        (0, "INSERT"), (8, "0"), (2, name), (10, str(x)), (20, str(y)), (30, "0"),
        (41, str(scale)), (42, str(scale)), (50, str(rotation)),
    )


def _right_edge(body):
    x = float(_field(body, 10))
    return x + estimate_text_width(_field(body, 1), float(_field(body, 40)), float(_field(body, 41, 1.0)))


def test_note_next_to_the_frame_is_shrunk_to_stay_inside_it(tmp_path):
    # 「说明」贴着图框右边摆，旁边没有别的文字：以前译文直接伸出图框。
    drawing = _drawing(*_insert(), *_text("20", NOTE, x=60, y=20), blocks=_frame_block())
    result, output, messages = _translate(tmp_path, drawing, {NOTE: LONG})
    note = _entities(output, "TEXT")[0]
    assert _field(note, 1) == LONG
    assert float(_field(note, 41)) == pytest.approx(0.7)
    assert _right_edge(note) < 100
    assert result.stats.layout_fitted == 1 and result.stats.layout_unresolved == 0
    assert any("伸出图框" in message for message in messages)


def test_bilingual_line_under_the_source_stays_inside_the_frame(tmp_path):
    drawing = _drawing(*_insert(), *_text("20", NOTE, x=60, y=20), blocks=_frame_block())
    result, output, _messages = _translate(tmp_path, drawing, {NOTE: LONG}, display_mode="source_first")
    texts = {_field(body, 1): body for body in _entities(output, "TEXT")}
    assert _field(texts[NOTE], 40) == "2.5"
    assert _right_edge(texts[LONG]) < 100
    assert result.stats.layout_unresolved == 0


def test_translation_that_cannot_fit_in_the_frame_is_reported(tmp_path):
    drawing = _drawing(*_insert(), *_text("20", NOTE, x=90, y=20), blocks=_frame_block())
    result, output, messages = _translate(tmp_path, drawing, {NOTE: LONG})
    note = _entities(output, "TEXT")[0]
    # 停在最小比例，记进报告请人复核。
    assert float(_field(note, 40)) == pytest.approx(2.5 * 0.4 / 0.7)
    assert result.stats.layout_unresolved == 1
    assert any("缩到最小比例仍会压到旁边的文字或伸出图框" in message for message in messages)


@pytest.mark.parametrize(
    "blocks, entities",
    [
        # 原文不在框里（框在别处）。
        (_frame_block(), (*_insert(x=500), )),
        # 不闭合的多段线、带圆弧的多段线都不是框。
        (_frame_block(closed=False), _insert()),
        (_frame_block(bulge=0.5), _insert()),
    ],
)
def test_text_outside_any_closed_rectangle_is_left_alone(tmp_path, blocks, entities):
    drawing = _drawing(*entities, *_text("20", NOTE, x=60, y=20), blocks=blocks)
    result, output, _messages = _translate(tmp_path, drawing, {NOTE: LONG})
    note = _entities(output, "TEXT")[0]
    assert _field(note, 40) == "2.5" and _field(note, 41) is None
    assert result.stats.layout_fitted == 0


def _bounds(segments):
    return sorted(
        tuple(round(value, 6) for value in (min(x1, x2), max(x1, x2), min(y1, y2), max(y1, y2)))
        for x1, y1, x2, y2 in segments
    )


def test_lines_in_scaled_rotated_and_nested_blocks_are_placed_in_drawing_coordinates():
    records = _parse_dxf(_dxf(
        (0, "SECTION"), (2, "BLOCKS"),
        *_frame_block("INNER", base=(10, 10), rect=((10, 10), (20, 10), (20, 15), (10, 15))),
        (0, "BLOCK"), (2, "OUTER"), (10, "0"), (20, "0"),
        *_insert("INNER", x=100, y=0, scale=2, rotation=90),
        (0, "ENDBLK"),
        (0, "ENDSEC"),
        (0, "SECTION"), (2, "ENTITIES"),
        *_insert("OUTER", x=0, y=1000),
        # 旧式 POLYLINE：顶点在后面的 VERTEX 里；第二段带凸度，是圆弧。
        (0, "POLYLINE"), (8, "0"), (66, "1"), (70, "0"),
        (0, "VERTEX"), (8, "0"), (10, "0"), (20, "0"),
        (0, "VERTEX"), (8, "0"), (10, "5"), (20, "0"), (42, "1"),
        (0, "VERTEX"), (8, "0"), (10, "5"), (20, "3"),
        (0, "SEQEND"),
        (0, "LINE"), (8, "0"), (10, "7"), (20, "0"), (11, "7"), (21, "3"),
        (0, "ENDSEC"), (0, "EOF"),
    ))
    model = _bounds(_line_segments(records)[("SPACE", "0")])
    # INNER 的 10×5 框：减去基点、放大 2 倍、转 90°，插在 OUTER 的 (100, 0)，OUTER 又插在 (0, 1000)：
    # 四条边落在 x 90～100、y 1000～1020。
    assert model == [
        (0, 5, 0, 0), (7, 7, 0, 3),
        (90, 90, 1000, 1020), (90, 100, 1000, 1000), (90, 100, 1020, 1020), (100, 100, 1000, 1020),
    ]


def test_dash_dot_axis_lines_are_not_cell_borders():
    records = _parse_dxf(_dxf(
        (0, "SECTION"), (2, "TABLES"),
        (0, "TABLE"), (2, "LAYER"),
        (0, "LAYER"), (2, "AXIS"), (6, "CENTER"),
        (0, "LAYER"), (2, "WALL"), (6, "Continuous"),
        (0, "ENDTAB"), (0, "ENDSEC"),
        (0, "SECTION"), (2, "ENTITIES"),
        (0, "LINE"), (8, "AXIS"), (10, "0"), (20, "0"), (11, "0"), (21, "9"),
        (0, "LINE"), (8, "WALL"), (10, "1"), (20, "0"), (11, "1"), (21, "9"),
        (0, "LINE"), (8, "WALL"), (6, "DASHED"), (10, "2"), (20, "0"), (11, "2"), (21, "9"),
        (0, "ENDSEC"), (0, "EOF"),
    ))
    assert _bounds(_line_segments(records)[("SPACE", "0")]) == [(1, 1, 0, 9)]


def test_blocks_inserting_each_other_do_not_loop():
    records = _parse_dxf(_dxf(
        (0, "SECTION"), (2, "BLOCKS"),
        (0, "BLOCK"), (2, "A"), (10, "0"), (20, "0"), *_insert("B"),
        *_lwpolyline(((0, 0), (1, 0), (1, 1), (0, 1))), (0, "ENDBLK"),
        (0, "BLOCK"), (2, "B"), (10, "0"), (20, "0"), *_insert("A"), (0, "ENDBLK"),
        (0, "ENDSEC"),
        (0, "SECTION"), (2, "ENTITIES"), *_insert("A"), (0, "ENDSEC"), (0, "EOF"),
    ))
    assert len(_line_segments(records)[("SPACE", "0")]) == 4


# ── 第二轮：simplex 真实字宽、互相收窄、LINE 画的格子 ──────────────────


def _line(x1, y1, x2, y2):
    return (0, "LINE"), (8, "0"), (10, str(x1)), (20, str(y1)), (11, str(x2)), (21, str(y2))


def _cell(left, bottom, right, top):
    return (*_line(left, bottom, right, bottom), *_line(right, bottom, right, top),
            *_line(right, top, left, top), *_line(left, top, left, bottom))


def _centered(handle, text, *, x, y):
    return _text(handle, text, x=x, y=y, extra=((72, "1"), (11, str(x)), (21, str(y))))


def _span(body):
    width = estimate_text_width(_field(body, 1), float(_field(body, 40)), float(_field(body, 41, 1.0)))
    if _field(body, 72) == "1":
        center = float(_field(body, 11))
        return center - width / 2, center + width / 2
    left = float(_field(body, 10))
    return left, left + width


def test_simplex_latin_width_follows_the_real_glyphs():
    # AutoCAD 2026 里 simplex.shx、字高 400 实测：「Salle de réception」约 5505，「Local de distribution
    # électrique」约 9565。旧的一刀切 0.6 em 只估出 4300、7700 左右，译文就伸出去了。
    assert estimate_text_width("Salle de réception", 400, 1.0, "hershey") == pytest.approx(5505, rel=0.03)
    assert estimate_text_width("Local de distribution électrique", 400, 1.0, "hershey") == pytest.approx(9565, rel=0.03)
    assert estimate_text_width("é", 1, 1.0, "hershey") == estimate_text_width("e", 1, 1.0, "hershey")
    # 汉字仍按大字体估。
    assert estimate_text_width("说明", 1, 1.0, "hershey") == estimate_text_width("说明", 1, 1.0)


def test_two_long_room_names_each_keep_to_their_half_of_the_gap(tmp_path):
    # 两个居中的房间名都译得很长：以前各自只躲对方的原文，结果两段译文在中间叠在一起。
    drawing = _drawing(*_centered("20", "会议室", x=0, y=0), *_centered("21", "配电室", x=40, y=0))
    _result, output, _messages = _translate(
        tmp_path, drawing, {"会议室": "Salle de réunion", "配电室": "Local de distribution électrique"},
    )
    first, second = (_span(body) for body in _entities(output, "TEXT"))
    # 原文之间的空各分一半；一边用不完，另一边可以多占。
    assert first[1] <= second[0]


def test_translation_stays_in_a_table_cell_drawn_with_lines(tmp_path):
    drawing = _drawing(*_cell(0, 0, 30, 10), *_cell(30, 0, 80, 10), *_text("20", "普通门", x=2, y=3.75))
    result, output, _messages = _translate(tmp_path, drawing, {"普通门": "Porte ordinaire en bois"})
    door = _entities(output, "TEXT")[0]
    assert _span(door)[1] < 30
    assert result.stats.layout_fitted == 1 and result.stats.layout_unresolved == 0


def test_bilingual_line_stays_above_the_row_line_and_the_source_moves_up_to_make_room(tmp_path):
    # 行高 3.2 倍字高、原文居中：新增的译文行放在原文下面会压到这一行的底线。
    drawing = _drawing(*_cell(0, 0, 60, 8), *_cell(0, 8, 60, 16), *_text("20", "普通门", x=2, y=2.75))
    result, output, _messages = _translate(
        tmp_path, drawing, {"普通门": "Porte ordinaire"}, display_mode="source_first",
    )
    texts = {_field(body, 1): body for body in _entities(output, "TEXT")}
    source, line = texts["普通门"], texts["Porte ordinaire"]
    source_y, line_y, line_height = float(_field(source, 20)), float(_field(line, 20)), float(_field(line, 40))
    assert source_y > 2.75 and source_y + 2.5 < 8
    assert line_y - 0.33 * line_height >= 0
    assert line_y + line_height < source_y
    assert result.stats.layout_unresolved == 0
