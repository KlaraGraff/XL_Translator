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


def test_leader_note_that_cannot_fit_its_shoulder_is_split_into_two_lines(tmp_path):
    # maple 2026-10-31：缩到最小仍放不下就折两行。两行收在原来那一行的高度里，不越过肩线，
    # 不压到上方摞着的另一行标注。
    block, leader = _leader(shoulder=20)
    above = (
        (0, "TEXT"), (5, "80"), (8, "NOTE"), (67, "1"), (100, "AcDbEntity"), (100, "AcDbText"),
        (10, "120"), (20, str(100.5 + HEIGHT * 1.25)), (30, "0"), (40, str(HEIGHT)), (1, "成品"), (100, "AcDbText"),
    )
    drawing = _drawing(*leader, *above, blocks=block)
    result, output, _messages = _translate(tmp_path, drawing, {NOTE: LONG})
    assert result.stats.layout_wrapped == 1 and result.stats.layout_unresolved == 0
    first, second = _entities(output, "ATTRIB")
    assert f"{_field(first, 1)} {_field(second, 1)}" == LONG
    assert _field(second, 2) == _field(first, 2) == "上标"
    assert _field(second, 5) not in {"60", "61", "62", "70", "71", "72", "80"}
    height = float(_field(first, 40))
    lower, upper = sorted((first, second), key=lambda body: float(_field(body, 21)))
    # 底对齐：下面那行的底边不低于肩线，上面那行顶边不碰上方那行标注。
    assert float(_field(lower, 21)) >= 100
    assert float(_field(upper, 21)) + height <= 100.5 + HEIGHT * 1.25
    for body in (first, second):
        assert _left_edge(body) >= 150 - 20 - 1e-6
