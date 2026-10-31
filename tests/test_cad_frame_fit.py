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
    assert any("超出图框" in message for message in messages)


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
    assert any("已缩到最小字号比例，仍与相邻文字重叠或超出图框" in message for message in messages)


def test_text_with_no_line_in_its_way_is_left_alone(tmp_path):
    # 框在别处：原文和译文旁边都没有线。
    drawing = _drawing(*_insert(x=500), *_text("20", NOTE, x=60, y=20), blocks=_frame_block())
    result, output, _messages = _translate(tmp_path, drawing, {NOTE: LONG})
    note = _entities(output, "TEXT")[0]
    assert _field(note, 40) == "2.5" and _field(note, 41) is None
    assert result.stats.layout_fitted == 0


@pytest.mark.parametrize("polyline", [{"closed": False}, {"bulge": 0.5}])
def test_translation_stops_before_an_open_or_curved_line_it_would_cross(tmp_path, polyline):
    # 不闭合的多段线、带圆弧的多段线围不成格子，但译文照样不该横穿原文旁边的线（门的开启弧、箭头）。
    drawing = _drawing(*_insert(), *_text("20", NOTE, x=60, y=20), blocks=_frame_block(**polyline))
    result, output, _messages = _translate(tmp_path, drawing, {NOTE: LONG})
    note = _entities(output, "TEXT")[0]
    right = _right_edge(note)
    if "bulge" in polyline:
        # 右边那段弧从 (100, 0) 鼓到 (100, 50) 的右侧，在字高范围里离原文最近的是弧的左端附近。
        assert right < 112
    else:
        assert right < 100
    assert result.stats.layout_fitted == 1


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
    # 「coupe」里的 p 往下伸：下面要留出下伸的深度。
    result, output, _messages = _translate(
        tmp_path, drawing, {"普通门": "Porte coupe-feu"}, display_mode="source_first",
    )
    texts = {_field(body, 1): body for body in _entities(output, "TEXT")}
    source, line = texts["普通门"], texts["Porte coupe-feu"]
    source_y, line_y, line_height = float(_field(source, 20)), float(_field(line, 20)), float(_field(line, 40))
    assert source_y > 2.75 and source_y + 2.5 < 8
    assert line_y - 0.33 * line_height >= 0
    assert line_y + line_height < source_y
    assert result.stats.layout_unresolved == 0


# ── 窄格子里最小比例仍放不下：折成两行 ──────────────────────────────


ROOM = "强弱电间"
ROOM_FR = "Local courant fort et courant faible"


def _room_drawing(width=3000):
    # 窄房间，房间名居中（中心对齐），字高 400：译文一行缩到最小比例也放不下。
    x = width / 2
    return _drawing(*_cell(0, 0, width, 6000), *_text("20", ROOM, x=x, y=3000, height=400,
                                                     extra=((72, "1"), (11, str(x)), (21, "3000"), (73, "2"))))


def _boxes(output):
    out = []
    for body in _entities(output, "TEXT"):
        left, right = _span(body)
        height, y = float(_field(body, 40)), float(_field(body, 21))
        out.append((_field(body, 1), left, right, y - height / 2, y + height / 2, _field(body, 5)))
    return sorted(out, key=lambda item: -item[3])


def test_long_name_in_a_narrow_cell_is_split_into_two_lines_inside_it(tmp_path):
    result, output, messages = _translate(tmp_path, _room_drawing(), {ROOM: ROOM_FR})
    lines = _boxes(output)
    assert [line[0] for line in lines] == ["Local courant fort", "et courant faible"]
    for _text_value, left, right, bottom, top, _handle in lines:
        assert 0 < left and right < 3000 and 0 < bottom and top < 6000
    # 上下两行不叠，第二行有自己的句柄。
    assert lines[1][4] < lines[0][3]
    assert lines[0][5] and lines[1][5] and lines[0][5] != lines[1][5]
    assert result.stats.layout_wrapped == 1 and result.stats.layout_unresolved == 0
    assert any("已在原位置分成两行" in message for message in messages)


def test_split_lines_of_a_bilingual_translation_are_both_removed_on_the_next_run(tmp_path):
    from core.cad_writeback import restore_previous_run

    # 双语的译文行字高是原文的 0.8，房间再窄一些它才放不下。
    result, output, _messages = _translate(tmp_path, _room_drawing(2200), {ROOM: ROOM_FR}, display_mode="source_first")
    texts = [_field(body, 1) for body in _entities(output, "TEXT")]
    assert texts == [ROOM, "Local courant fort", "et courant faible"]
    assert result.stats.layout_wrapped == 1
    records = _parse_dxf(output.read_text(encoding="utf-8"))
    restore_previous_run(records)
    values = [record.value for record in records if record.code == 1]
    assert values.count(ROOM) == 1 and not any("courant" in value for value in values)


def test_no_split_when_the_two_lines_would_not_fit_the_cell_height(tmp_path):
    drawing = _drawing(*_cell(0, 2780, 3000, 3220), *_text("20", ROOM, x=1500, y=3000, height=400,
                                                         extra=((72, "1"), (11, "1500"), (21, "3000"), (73, "2"))))
    result, output, _messages = _translate(tmp_path, drawing, {ROOM: ROOM_FR})
    assert [_field(body, 1) for body in _entities(output, "TEXT")] == [ROOM_FR]
    assert result.stats.layout_wrapped == 0 and result.stats.layout_unresolved == 1


# ── 门的开启弧、箭头挡住伸出去的译文；双语逐行说明不压下一行 ──────────────


def _arc(cx, cy, radius, start, end):
    return (0, "ARC"), (8, "0"), (10, str(cx)), (20, str(cy)), (40, str(radius)), (50, str(start)), (51, str(end))


def test_translation_stops_before_a_door_swing_and_an_arrow(tmp_path):
    centred = ((72, "1"), (11, "3000"), (21, "3300"), (73, "2"))
    arrow = _lwpolyline(((5200, 3000), (5800, 3300), (5200, 3600)), closed=False)
    drawing = _drawing(
        # 门厅左边是门的开启弧（圆心 (100, 2550)，半径 1500），右边是入口箭头。
        *_arc(100, 2550, 1500, 0, 90), *arrow,
        *_text("20", "门厅", x=3000, y=3300, height=400, extra=centred),
    )
    result, output, _messages = _translate(tmp_path, drawing, {"门厅": "Hall d'entrée principale"})
    left, right = _span(_entities(output, "TEXT")[0])
    # 字身高度范围里，弧最右到 x≈1460，箭头最左在 x≈5200 附近。
    assert left > 1460 and right < 5300
    assert result.stats.layout_fitted == 1


def test_bilingual_note_lines_leave_room_for_descenders_above_the_next_note(tmp_path):
    notes = {"一、尺寸以毫米计": "1. Dimensions en mm, cotes en mètres", "二、高差为三百": "2. Différence de niveau",
             "三、防火门设闭门器": "3. Les portes coupe-feu ont un groom"}
    drawing = _drawing(*(
        part for number, source in enumerate(notes)
        for part in _text(str(20 + number), source, x=0, y=1000 - 650 * number, height=320, extra=((73, "2"), (11, "0"), (21, str(1000 - 650 * number))))
    ))
    _result, output, _messages = _translate(tmp_path, drawing, notes, display_mode="source_first")
    boxes = []
    for body in _entities(output, "TEXT"):
        height, y = float(_field(body, 40)), float(_field(body, 21))
        drop = 0.33 * height if any(char in "gjpqy," for char in _field(body, 1)) else 0.0
        boxes.append((y - height / 2 - drop, y + height / 2, _field(body, 1)))
    boxes.sort(key=lambda box: -box[1])
    # 从上到下：原文、译文、原文、译文……任何两行（译文连下伸笔画）都不重叠。
    for (bottom, _top, upper), (_bottom, top, lower) in zip(boxes, boxes[1:], strict=False):
        assert bottom >= top, (upper, lower)


# ── 同一列说明统一字高 ──────────────────────────────────────────


def test_notes_in_one_column_share_the_smallest_size_and_the_heading_is_left_alone(tmp_path):
    # 「说明：」下面三条左端对齐：第 2 条贴着图框缩小了，第 1、3 条短、本来不用缩。
    notes = {"说明：": "Notes :", "单位为毫米": "Unites en mm",
             "未注明的墙体均为加气混凝土砌块墙": "Sauf indication contraire, tous les murs sont en blocs de beton cellulaire",
             "施工前复核尺寸": "Verifier sur site"}
    drawing = _drawing(*_insert(), *(
        part for number, source in enumerate(notes)
        for part in _text(str(20 + number), source, x=40, y=40 - 6 * number)
    ), blocks=_frame_block())
    result, output, messages = _translate(tmp_path, drawing, notes)
    sizes = {_field(body, 1): (float(_field(body, 40)), float(_field(body, 41, 1.0))) for body in _entities(output, "TEXT")}
    assert sizes["Notes :"] == (2.5, 1.0)
    numbered = [sizes[value] for source, value in notes.items() if not source.endswith("：")]
    assert len(set(numbered)) == 1 and numbered[0][0] < 2.5
    assert any("统一为相同字高" in message for message in messages)
    assert result.stats.layout_unresolved == 0


def test_texts_far_apart_are_not_a_column(tmp_path):
    long = "Sauf indication contraire, tous les murs sont en blocs de beton cellulaire"
    drawing = _drawing(
        *_insert(),
        *_text("20", "单位为毫米", x=40, y=40),
        # 隔得远（超过 4 倍字高）：不是同一列。
        *_text("21", "墙体", x=40, y=20),
        blocks=_frame_block(),
    )
    _result, output, _messages = _translate(tmp_path, drawing, {"单位为毫米": "Unites en mm", "墙体": long})
    sizes = {_field(body, 1): float(_field(body, 40)) for body in _entities(output, "TEXT")}
    assert sizes["Unites en mm"] == 2.5 and sizes[long] < 2.5
