"""CAD 双语：一列上下排的说明（每条一段多行文字）整列往下排开，译文不压到下一条上（反馈 #26）。"""
from __future__ import annotations

import re

import pytest

from core.cad_layout import _collect_entities, _mtext_extent, _mtext_paragraphs, _mtext_raw, _vertical_span
from core.cad_translation import _parse_dxf, _plain_text
from tests.test_cad_new_features import _drawing, _entities, _translate

NOTES = {
    "基础图仅供参考，用户可根据当地实际情况修改。":
        "Le plan de fondation est donné à titre indicatif, le client peut l'adapter aux conditions locales.",
    "电缆室底面须向排污口略有倾斜，以免积水。":
        "Le fond du local des câbles doit être légèrement incliné vers l'évacuation pour éviter l'eau stagnante.",
    "砖墙抹面采用防水砂浆，厚度二十毫米。":
        "Les murs en briques sont enduits d'un mortier hydrofuge de vingt millimètres d'épaisseur.",
    "踏步的位置可根据要求由用户自定。":
        "L'emplacement des marches peut être défini par le client selon ses besoins.",
    "箱变外围如做围栏需距外沿一米以上。":
        "Une éventuelle clôture autour du poste doit se trouver à plus d'un mètre du bord extérieur.",
}
HEIGHT = 2.5
PITCH = 6.0
TOP_NOTE_Y = 100.0


def _mtext(handle, text, *, x, y, height=HEIGHT, width=100.0, attachment=7):
    return (
        (0, "MTEXT"), (5, handle), (330, "1F"), (100, "AcDbEntity"), (8, "0"), (100, "AcDbMText"),
        (10, str(x)), (20, str(y)), (30, "0"), (40, str(height)), (41, str(width)),
        (71, str(attachment)), (72, "5"), (1, text), (7, "Standard"),
    )


def _line(handle, x1, y1, x2, y2):
    return (
        (0, "LINE"), (5, handle), (330, "1F"), (100, "AcDbEntity"), (8, "0"), (100, "AcDbLine"),
        (10, str(x1)), (20, str(y1)), (30, "0"), (11, str(x2)), (21, str(y2)), (31, "0"),
    )


def _frame(bottom=10.0):
    """图框（四条边）和图签的上边线。"""
    return (
        *_line("A1", 0, 0, 200, 0), *_line("A2", 200, 0, 200, 120), *_line("A3", 200, 120, 0, 120),
        *_line("A4", 0, 120, 0, 0), *_line("A5", 0, bottom, 200, bottom),
    )


def _notes(notes=NOTES, *, separators=False, labels=True):
    parts = []
    for number, source in enumerate(notes):
        y = TOP_NOTE_Y - PITCH * number
        parts += _mtext(f"{30 + number:X}", source, x=20, y=y)
        if labels:
            parts += _mtext(f"{50 + number:X}", f"{number + 1}.", x=12, y=y, width=0)
        if separators:
            parts += _line(f"{70 + number:X}", 10, y - 2, 150, y - 2)
    return tuple(parts)


def _layout(path):
    """输出里每段多行文字：原文（第一段）→ (上边, 下边, 对齐方式, 内容)。"""
    records = _parse_dxf(path.read_text(encoding="utf-8"))
    out = {}
    for entity in _collect_entities(records, _plain_text):
        if entity.entity_type != "MTEXT":
            continue
        raw = _mtext_raw(records, entity.start, entity.end)
        paragraphs = _mtext_paragraphs(raw, entity.height)
        _width, height = _mtext_extent(paragraphs, entity.height, entity.ref_width, entity.font_class)
        bottom, top = _vertical_span(entity.valign, entity.anchor_y, height)
        attachment = next(int(r.value) for r in records[entity.start:entity.end] if r.code == 71)
        out[paragraphs[0][1]] = (top, bottom, attachment, raw)
    return out


def test_stacked_notes_are_reflowed_so_each_translation_sits_under_its_own_note(tmp_path):
    drawing = _drawing(*_frame(), *_notes())
    result, output, _messages = _translate(tmp_path, drawing, NOTES, display_mode="source_first")
    layout = _layout(output)
    notes = [layout[source] for source in NOTES]
    # 原文改成顶边对齐，第一条原文的位置不动。
    assert all(note[2] == 1 for note in notes)
    assert notes[0][0] == pytest.approx(TOP_NOTE_Y + HEIGHT)
    # 从上到下依次排开，任何两条（连同译文）都不重叠，最后一条在图签线上面。
    for upper, lower in zip(notes, notes[1:], strict=False):
        assert upper[1] >= lower[0] - 1e-6
    assert notes[-1][1] > 10
    # 编号跟着自己那一条挪：顶边对齐。
    for number, note in enumerate(notes):
        label_top = layout[f"{number + 1}."][0]
        assert label_top == pytest.approx(note[0], abs=0.01)
    assert result.stats.layout_unresolved == 0


def test_translation_is_shrunk_and_widened_when_the_room_below_is_short(tmp_path):
    # 图签线就在最后一条下面不远：原字号排不下，译文缩小、放宽到图框右边。
    drawing = _drawing(*_frame(bottom=70), *_notes())
    result, output, _messages = _translate(tmp_path, drawing, NOTES, display_mode="source_first")
    layout = _layout(output)
    notes = [layout[source] for source in NOTES]
    scales = {float(match) for note in notes for match in re.findall(r"\{\\H([0-9.]+)x;", note[3])}
    assert len(scales) == 1 and scales.pop() < 0.8
    for upper, lower in zip(notes, notes[1:], strict=False):
        assert upper[1] >= lower[0] - 1e-6
    assert notes[-1][1] > 70 or result.stats.layout_unresolved >= 1
    # 换行宽度放宽到图框右边线前面（原来 100）。
    widths = [float(next(r.value for r in body if r.code == 41)) for body in _entities(output, "MTEXT")
              if _plain_text(next(r.value for r in body if r.code == 1)).startswith(tuple(NOTES))]
    assert widths and all(100 < width < 200 for width in widths)


def test_translation_first_keeps_the_column_order(tmp_path):
    drawing = _drawing(*_frame(), *_notes())
    _result, output, _messages = _translate(tmp_path, drawing, NOTES, display_mode="translation_first")
    layout = _layout(output)
    notes = sorted((value for key, value in layout.items() if key in NOTES.values()), key=lambda note: -note[0])
    assert len(notes) == len(NOTES)
    for upper, lower in zip(notes, notes[1:], strict=False):
        assert upper[1] >= lower[0] - 1e-6


def test_notes_separated_by_table_lines_are_left_to_the_cell_fit(tmp_path):
    drawing = _drawing(*_frame(), *_notes(separators=True))
    _result, output, _messages = _translate(tmp_path, drawing, NOTES, display_mode="source_first")
    layout = _layout(output)
    assert all(layout[source][2] == 7 for source in NOTES)


def test_short_labels_next_to_symbols_are_not_moved(tmp_path):
    # 系统图里「保护」「测量」这类贴着符号的短标注：挪开就和符号对不上了。
    labels = {"保护": "Protection", "测量": "Mesure"}
    drawing = _drawing(*_frame(), *_notes(labels, labels=False))
    _result, output, _messages = _translate(tmp_path, drawing, labels, display_mode="source_first")
    layout = _layout(output)
    assert all(layout[source][2] == 7 for source in labels)


def test_translation_only_mode_is_unchanged(tmp_path):
    drawing = _drawing(*_frame(), *_notes())
    _result, output, _messages = _translate(tmp_path, drawing, NOTES)
    layout = _layout(output)
    assert all(layout[translation][2] == 7 for translation in NOTES.values())
