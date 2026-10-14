"""CAD：译文不伸出图框（原文所在的闭合矩形，含插入的图块里画的框）。"""
from __future__ import annotations

import math

import pytest

from core.cad_layout import _frame_rectangles, estimate_text_width
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


def test_frames_in_scaled_rotated_and_nested_blocks_are_placed_in_drawing_coordinates():
    records = _parse_dxf(_dxf(
        (0, "SECTION"), (2, "BLOCKS"),
        *_frame_block("INNER", base=(10, 10), rect=((10, 10), (20, 10), (20, 15), (10, 15))),
        (0, "BLOCK"), (2, "OUTER"), (10, "0"), (20, "0"),
        *_insert("INNER", x=100, y=0, scale=2, rotation=90),
        (0, "ENDBLK"),
        (0, "ENDSEC"),
        (0, "SECTION"), (2, "ENTITIES"),
        *_insert("OUTER", x=0, y=1000),
        # 旧式 POLYLINE：顶点在后面的 VERTEX 里。
        (0, "POLYLINE"), (8, "0"), (66, "1"), (70, "1"),
        (0, "VERTEX"), (8, "0"), (10, "0"), (20, "0"),
        (0, "VERTEX"), (8, "0"), (10, "5"), (20, "0"),
        (0, "VERTEX"), (8, "0"), (10, "5"), (20, "3"),
        (0, "VERTEX"), (8, "0"), (10, "0"), (20, "3"),
        (0, "SEQEND"),
        (0, "ENDSEC"), (0, "EOF"),
    ))
    rects = _frame_rectangles(records)
    model = sorted(
        (min(x for x, _ in corners), max(x for x, _ in corners), min(y for _, y in corners), max(y for _, y in corners))
        for corners in rects[("SPACE", "0")]
    )
    # INNER 的 10×5 框：减去基点、放大 2 倍、转 90°，插在 OUTER 的 (100, 0)，OUTER 又插在 (0, 1000)。
    assert [tuple(round(value, 6) for value in rect) for rect in model] == [(0, 5, 0, 3), (90, 100, 1000, 1020)]
    assert all(math.isfinite(value) for rect in model for value in rect)


def test_blocks_inserting_each_other_do_not_loop():
    records = _parse_dxf(_dxf(
        (0, "SECTION"), (2, "BLOCKS"),
        (0, "BLOCK"), (2, "A"), (10, "0"), (20, "0"), *_insert("B"),
        *_lwpolyline(((0, 0), (1, 0), (1, 1), (0, 1))), (0, "ENDBLK"),
        (0, "BLOCK"), (2, "B"), (10, "0"), (20, "0"), *_insert("A"), (0, "ENDBLK"),
        (0, "ENDSEC"),
        (0, "SECTION"), (2, "ENTITIES"), *_insert("A"), (0, "ENDSEC"), (0, "EOF"),
    ))
    assert len(_frame_rectangles(records)[("SPACE", "0")]) == 1
