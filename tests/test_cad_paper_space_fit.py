"""CAD：写在布局（图纸空间）里的引线标注也避开视口里显示的墙线，不越过引线肩线（反馈 #23）。"""
from __future__ import annotations

import pytest

from core.cad_layout import _Viewport, _viewport_segments, estimate_text_width
from tests.test_cad_new_features import _drawing, _entities, _field, _translate

NOTE = "焊接"
LONG = "Soudure continue sur toute la longueur"
HEIGHT = 3.0


def _viewport(*, center=(100, 100), size=(200, 200), model_center=(5000, 5000), view_height=4000, handle="40"):
    # 纸面上 200×200 的视口显示模型空间中心 (5000, 5000)、高 4000 的一块：比例 1:20。
    return (
        (0, "VIEWPORT"), (5, handle), (8, "0"), (67, "1"),
        (10, str(center[0])), (20, str(center[1])), (30, "0"), (40, str(size[0])), (41, str(size[1])),
        (68, "2"), (69, "2"), (12, str(model_center[0])), (22, str(model_center[1])),
        (16, "0"), (26, "0"), (36, "1"), (17, "0"), (27, "0"), (37, "0"), (45, str(view_height)), (51, "0"),
    )


def _model_wall(x, handle="50"):
    """模型空间里一道竖墙（纸面上 x = 100 + (x - 5000) / 20）。"""
    return ((0, "LINE"), (5, handle), (8, "WALL"), (10, str(x)), (20, "3000"), (30, "0"), (11, str(x)), (21, "7000"), (31, "0"))


def _leader(*, right=150, y=100, shoulder=60, handle="60"):
    """布局里的引线块：一条肩线，块属性右对齐写在肩线右端上方（上标、底对齐）。"""
    block = (
        (0, "BLOCK"), (5, "70"), (8, "0"), (2, "*U1"), (70, "1"), (10, "0"), (20, "0"), (30, "0"),
        (0, "LINE"), (5, "71"), (8, "0"), (10, str(right - shoulder)), (20, str(y)), (30, "0"),
        (11, str(right)), (21, str(y)), (31, "0"),
        (0, "ENDBLK"), (5, "72"), (8, "0"),
    )
    width = estimate_text_width(NOTE, HEIGHT, 1.0)
    entities = (
        (0, "INSERT"), (5, handle), (8, "0"), (67, "1"), (2, "*U1"), (66, "1"), (10, "0"), (20, "0"), (30, "0"),
        (0, "ATTRIB"), (5, "61"), (8, "NOTE"), (67, "1"), (100, "AcDbEntity"), (100, "AcDbText"),
        (10, str(right - width)), (20, str(y + 0.5)), (30, "0"), (40, str(HEIGHT)), (1, NOTE), (72, "2"),
        (11, str(right)), (21, str(y + 0.5)), (31, "0"), (100, "AcDbAttribute"), (2, "上标"), (70, "0"), (74, "1"),
        (0, "SEQEND"), (5, "62"), (8, "0"),
    )
    return block, entities


def _left_edge(body):
    right = float(_field(body, 11))
    return right - estimate_text_width(_field(body, 1), float(_field(body, 40)), float(_field(body, 41, 1.0)))


def test_viewport_shows_model_lines_at_its_scale_and_clips_them():
    viewport = _Viewport.parse({10: "100", 20: "100", 40: "200", 41: "200", 69: "2", 12: "5000", 22: "5000", 45: "4000"})
    assert viewport is not None and viewport.scale == pytest.approx(0.05)
    [segment] = _viewport_segments(viewport, [(4000, 3000, 4000, 7000), (90000, 0, 90000, 10)])
    # x 4000 → 纸面 50；y 3000..7000 → 0..200，正好是视口框。
    assert segment == pytest.approx((50, 0, 50, 200))
    # 布局本身的整页视图（69=1）不显示模型。
    assert _Viewport.parse({40: "200", 41: "200", 69: "1", 45: "4000"}) is None


def test_leader_note_in_a_layout_stops_at_the_wall_shown_in_the_viewport(tmp_path):
    # 肩线很长（不挡），墙在纸面 x=100：以前看不到模型空间的墙，译文一路往左压过去。
    block, leader = _leader(shoulder=200)
    drawing = _drawing(*_viewport(), *_model_wall(5000), *leader, blocks=block)
    result, output, _messages = _translate(tmp_path, drawing, {NOTE: LONG})
    attrib = _entities(output, "ATTRIB")[0]
    assert _field(attrib, 1) == LONG
    assert _left_edge(attrib) >= 100
    assert result.stats.layout_fitted == 1


def test_leader_note_stays_on_its_shoulder_line(tmp_path):
    block, leader = _leader(shoulder=60)
    drawing = _drawing(*leader, blocks=block)
    result, output, _messages = _translate(tmp_path, drawing, {NOTE: LONG})
    attrib = _entities(output, "ATTRIB")[0]
    assert _left_edge(attrib) >= 150 - 60
    assert result.stats.layout_fitted == 1


def test_wall_outside_the_viewport_frame_is_not_shown(tmp_path):
    # 墙在模型空间里，但不在这个视口显示的范围内（纸面 x = -150，视口框是 0..200）。
    block, leader = _leader(shoulder=200)
    drawing = _drawing(*_viewport(), *_model_wall(0), *leader, blocks=block)
    result, output, _messages = _translate(tmp_path, drawing, {NOTE: LONG})
    attrib = _entities(output, "ATTRIB")[0]
    assert _field(attrib, 40) == str(HEIGHT) and _field(attrib, 41) is None
    assert result.stats.layout_fitted == 0


def test_leader_note_that_cannot_fit_its_shoulder_stays_one_line_at_minimum_scale(tmp_path):
    # 反馈 #23：引线块多是动态块，折出来的第二行在 AutoCAD 里和属性对不齐、压到别的标注；
    # maple 2026-10-31 定的退路：引线说明不折行，缩到最小比例，译文照写，列入复核。
    block, leader = _leader(shoulder=20)
    drawing = _drawing(*leader, blocks=block)
    result, output, _messages = _translate(tmp_path, drawing, {NOTE: LONG})
    assert result.stats.layout_wrapped == 0 and result.stats.layout_unresolved == 1
    [attrib] = _entities(output, "ATTRIB")
    assert _field(attrib, 1) == LONG
    assert not [body for body in _entities(output, "TEXT") if _field(body, 1) and LONG.split(" ")[0] in _field(body, 1)]
    scale = float(_field(attrib, 40)) / HEIGHT * float(_field(attrib, 41))
    assert scale == pytest.approx(0.4, abs=0.01)


def _loose_attdef(handle="90", tag="标准段剖面", *, block=False):
    return (
        (0, "ATTDEF"), (5, handle), (330, "1F"), (100, "AcDbEntity"), (8, "NOTE"), (67, "1"), (100, "AcDbText"),
        (10, "100"), (20, "50"), (30, "0"), (40, "4.0"), (1, ""), (7, "Standard"), (72, "0"),
        (100, "AcDbAttributeDefinition"), (280, "0"), (3, "图纸标题（中文） View Title (Chinese)"), (2, tag),
        (70, "0"), (73, "0"), (74, "0"),
    )


@pytest.mark.parametrize("display_mode", ["target_only", "source_first"])
def test_view_title_drawn_as_a_loose_attribute_definition_is_translated(tmp_path, display_mode):
    # 反馈 #23：视图标题是直接放在布局上的属性定义，默认值为空，图上显示的是标记。以前只读默认值，
    # 整条漏翻。现在翻译标记，写回成同一位置、同一图层的一行普通文字（标记里不能有空格）。
    drawing = _drawing(*_loose_attdef())
    result, output, _messages = _translate(
        tmp_path, drawing, {"标准段剖面": "Coupe de la section type"}, display_mode=display_mode,
    )
    assert not _entities(output, "ATTDEF")
    [title] = _entities(output, "TEXT")
    expected = "Coupe de la section type" if display_mode == "target_only" else "标准段剖面 / Coupe de la section type"
    assert _field(title, 1) == expected
    assert (_field(title, 5), _field(title, 8), _field(title, 67), _field(title, 10)) == ("90", "NOTE", "1", "100")
    assert _field(title, 2) is None and _field(title, 3) is None and _field(title, 73) == "0"
    assert result.stats.changed == 1


def test_underlined_view_title_shrinks_to_stay_on_its_underline(tmp_path):
    # 反馈 #23：视图标题下面有一条下划线，译文比原文长时伸过下划线右端，以前不缩（不算碰撞）。
    # 现在和引线肩线一样：横线盖住原文、文字一端对齐横线端点时，译文不越过横线两端。
    underline = ((0, "LINE"), (5, "95"), (8, "0"), (67, "1"), (10, "98"), (20, "49"), (30, "0"),
                 (11, "125"), (21, "49"), (31, "0"))
    drawing = _drawing(*_loose_attdef(), *underline)
    result, output, _messages = _translate(tmp_path, drawing, {"标准段剖面": "Coupe de la section type"})
    [title] = _entities(output, "TEXT")
    width = estimate_text_width(_field(title, 1), float(_field(title, 40)), float(_field(title, 41, 1.0)))
    assert float(_field(title, 40)) * float(_field(title, 41, 1.0)) < 4.0
    assert 100 + width <= 125 + 1e-6
    assert result.stats.layout_fitted == 1


def test_attribute_definitions_inside_blocks_keep_reading_the_default_value(tmp_path):
    block = (
        (0, "BLOCK"), (5, "70"), (8, "0"), (2, "TITLE"), (70, "2"), (10, "0"), (20, "0"), (30, "0"),
        *_loose_attdef("91"),
        (0, "ENDBLK"), (5, "72"), (8, "0"),
    )
    insert = ((0, "INSERT"), (5, "60"), (8, "0"), (2, "TITLE"), (10, "0"), (20, "0"), (30, "0"))
    result, output, _messages = _translate(tmp_path, _drawing(*insert, blocks=block), {"标准段剖面": "Coupe"})
    assert _field(_entities(output, "ATTDEF")[0], 2) == "标准段剖面"
    assert result.stats.changed == 0


def test_start_point_of_right_aligned_text_follows_the_translation(tmp_path):
    # 反馈 #23：右对齐的文字只改了对齐点处的文字，起点（10/20）还在原文左端；AutoCAD 打开时和
    # 「CAD快速看图」都拿起点摆文字，右端就对不上。写完译文后起点按新宽度重算，上下位置不变。
    block, leader = _leader(shoulder=20)
    untouched = (
        (0, "TEXT"), (5, "80"), (8, "0"), (100, "AcDbEntity"), (100, "AcDbText"),
        (10, "1"), (20, "300"), (30, "0"), (40, "3"), (1, "B-B"), (72, "2"), (11, "50"), (21, "300.6"), (31, "0"),
        (100, "AcDbText"),
    )
    _result, output, _messages = _translate(tmp_path, _drawing(*leader, *untouched, blocks=block), {NOTE: LONG})
    [attrib] = _entities(output, "ATTRIB")
    width = estimate_text_width(_field(attrib, 1), float(_field(attrib, 40)), float(_field(attrib, 41)))
    assert float(_field(attrib, 10)) == pytest.approx(float(_field(attrib, 11)) - width, abs=1e-3)
    assert float(_field(attrib, 20)) == pytest.approx(float(_field(attrib, 21)))
    # 没翻译、没改字号的文字原样不动（哪怕起点本来就对不上）。
    kept = next(body for body in _entities(output, "TEXT") if _field(body, 1) == "B-B")
    assert (_field(kept, 10), _field(kept, 20)) == ("1", "300")
