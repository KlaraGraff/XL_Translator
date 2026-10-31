"""双语时把「一列上下排的说明」整列重新排开，译文不再压到下一条上。

图纸里的「技术要求」「说明」常常是一条一段多行文字（MTEXT），一条条上下排成一列，
行与行之间只空一个字高左右，编号（「1.」「2.」）往往是左边单独的一段文字，第二行
有时是另一段文字，甚至放在一个只有文字的图块里。双语时译文接在每一条的原文后面换行
写（见 core.cad_translation._write_bilingual），可下面紧挨着就是下一条，没有地方放；
这些文字又多是「底边对齐」，多出的译文让整段往上长，原文顶到上一条。

这里在写回之后看一遍：认出这样的一列，

* 每条改成「顶边对齐」，原文的位置和字号不动；
* 从上往下排：每条的译文放在它自己的原文下面（译文在上时放在上面），下面的各条连同
  左边的编号依次往下挪，挪到不再相压为止；原本就有空的地方不挪；
* 下面到图框、图签、别的文字之前的空间不够时，译文按缩字设置一档档缩小（最小到
  「最小比例」），左对齐的条目同时可以把换行宽度放宽到右边的图框线或别的文字前面；
  条目之间的空也可以收窄一些；
* 到最小比例还放不下的，按最小比例排好，记进报告请人复核。

只处理不旋转、在模型空间或图纸空间里直接放着、至少有两条改成了双语的列。中间隔着
横线的（画线的表格）不算一列，那是 :func:`core.cad_layout.fit_cell_mtext` 管的。
"""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from itertools import pairwise
from typing import Any

from core.cad_layout import (
    FitSettings,
    _box,
    _collect_entities,
    _entity_ranges,
    _float,
    _format_number,
    _horizontal_span,
    _line_segments,
    _mtext_extent,
    _mtext_paragraphs,
    _mtext_raw,
    _table_block_names,
    _TextEntity,
    _vertical_span,
    _write_mtext_content,
    _write_size,
)

# 上下两条之间的空不超过这么多（按字高计）才算同一列。
_ROW_GAP = 3.0
# 同一列各条的左边相差不超过这么多（按字高计）；编号在列左边这么远以内。
_LEFT_TOLERANCE = 1.5
_LABEL_REACH = 6.0
# 成句的说明至少这么多个字（不算空格）。
_MIN_NOTE_CHARS = 6
# 编号：「1.」「2、」「(3)」「a)」「十一、」这一类。
_LABEL_RE = re.compile(r"^\s*[(（]?(?:\d{1,3}|[A-Za-z]|[一二三四五六七八九十]{1,3})[.、)）:：]?\s*$")
# 同一列各条的字高相差不超过这个倍数。
_HEIGHT_RATIO = 1.5
# 空间不够时条目之间最少留这么多空（按字高计）。
_MIN_ROW_GAP = 0.4
# 离下面的图框线、别的文字，右边的图框线至少留这么多空（按字高计）。
_EDGE_PAD = 0.5
# 底边/中间对齐 → 同一横向对齐的顶边对齐。
_TOP_ATTACHMENT = {4: 1, 5: 2, 6: 3, 7: 1, 8: 2, 9: 3}


@dataclass
class _Row:
    """列里的一条：一段多行文字（seed 为 True 时是改成了双语的），或一个只有文字的图块。"""

    kind: str  # "MTEXT" / "TEXT" / "INSERT"
    start: int
    end: int
    height: float
    left: float
    right: float
    # 原文（双语之前）的上下边：判断是不是一列、条目间原来的空。
    top: float
    bottom: float
    # 现在（加了译文后）的内容上边，挪图块时用。
    current_top: float = 0.0
    entity: _TextEntity | None = None
    seed: bool = False
    paragraphs: list[tuple[float, str]] = field(default_factory=list)
    translation: tuple[int, int] = (0, 0)  # 译文段落在 paragraphs 里的 [起, 止)
    full_height: float = 0.0
    source: str = ""
    labels: list[tuple[str, int, int]] = field(default_factory=list)  # (kind, start, end)
    # 只有一段双语多行文字的图块（只插入了一次）：图块的纵向比例、那段文字原文顶边在图块里的位置。
    scale_y: float = 1.0
    inner_top: float = 0.0


def _move(records: list[Any], start: int, end: int, kind: str, dy: float) -> None:
    """把一个实体上下挪 ``dy``：MTEXT、INSERT 只挪插入点（MTEXT 的 11/21 是方向），TEXT 两个点一起挪。"""
    if abs(dy) < 1e-9:
        return
    codes = {20} if kind in {"MTEXT", "INSERT"} else {20, 21}
    for index in range(start + 1, end):
        record = records[index]
        if record.code == 101:
            break
        if record.code in codes:
            records[index] = replace(record, value=_format_number(_float(record.value) + dy))


def _set_attachment(records: list[Any], start: int, end: int, attachment: int) -> None:
    for index in range(start + 1, end):
        record = records[index]
        if record.code == 101:
            break
        if record.code == 71:
            records[index] = replace(record, value=str(attachment).rjust(len(record.value)))
            return


def _text_only_blocks(records: Sequence[Any]) -> dict[str, tuple[float, float]]:
    """只有单行/多行文字的图块：名字（大写）→ 基点。"""
    blocks: dict[str, tuple[float, float]] = {}
    name, base, only_text, has_text = "", (0.0, 0.0), True, False
    for start, end in _entity_ranges(records):
        if start >= end:
            continue
        entity_type = records[start].value.strip().upper()
        if entity_type == "BLOCK":
            fields: dict[int, str] = {}
            for record in records[start + 1:end]:
                fields.setdefault(record.code, record.value)
            name = str(fields.get(2, "")).strip().upper()
            base = (_float(fields.get(10)), _float(fields.get(20)))
            only_text, has_text = True, False
        elif entity_type == "ENDBLK":
            if name and only_text and has_text:
                blocks[name] = base
            name = ""
        elif name:
            if entity_type in {"TEXT", "MTEXT"}:
                has_text = True
            else:
                only_text = False
    return blocks


def _source_box(entity: _TextEntity, source: str) -> tuple[float, float, float, float]:
    """原文（双语之前）排出来的 ``(左, 右, 下, 上)``。"""
    paragraphs = [(1.0, line) for line in source.split("\n")]
    width, height = _mtext_extent(paragraphs, entity.height, entity.ref_width, entity.font_class)
    left, right = _horizontal_span(entity.halign, entity.anchor_x, width)
    bottom, top = _vertical_span(entity.valign, entity.anchor_y, height)
    return left, right, bottom, top


def _content_height(row: _Row, scale: float, wrap: float) -> float:
    paragraphs = [
        (factor * scale if row.translation[0] <= number < row.translation[1] else factor, text)
        for number, (factor, text) in enumerate(row.paragraphs)
    ]
    font = row.entity.font_class if row.entity else "shx"
    if row.kind == "INSERT":
        # 图块里的文字按图块自己的坐标排，再乘纵向比例。
        return _mtext_extent(paragraphs, row.height / row.scale_y, wrap, font)[1] * row.scale_y
    return _mtext_extent(paragraphs, row.height, wrap, font)[1]


def fit_bilingual_lists(
    records: list[Any],
    sources: Mapping[int, str],
    plain_text: Callable[[str], str],
    *,
    below: bool,
    bilingual_scale: float,
    settings: FitSettings | None = None,
    unresolved: list[dict[str, Any]] | None = None,
) -> int:
    """Re-flow columns of stacked bilingual MTEXT notes so translations do not cover the next note.

    ``sources`` maps the ordinal of each MTEXT turned bilingual to its source text; the translation
    is the paragraph group written in ``{\\H<bilingual_scale>x;…}`` after the source (``below``) or
    before it. Returns how many notes were moved or resized.
    """
    if not sources:
        return 0
    settings = (settings or FitSettings()).normalized()
    entities = _collect_entities(records, plain_text)
    table_blocks = _table_block_names(records)
    text_blocks = {name: base for name, base in _text_only_blocks(records).items() if name not in table_blocks}

    # 每个容器里的候选：文字实体和只有文字的图块。
    items: dict[str, list[_Row]] = {}
    block_entities: dict[str, list[_TextEntity]] = {}
    for entity in entities:
        if entity.container[0] == "BLOCK":
            block_entities.setdefault(entity.container[1], []).append(entity)
    for entity in entities:
        if entity.container[0] != "SPACE":
            continue
        rotated = min(entity.rotation % 360.0, 360.0 - entity.rotation % 360.0) > 0.1
        source = sources.get(entity.entity_no)
        if source is not None and entity.entity_type == "MTEXT" and not rotated:
            source = plain_text(source)
            left, right, bottom, top = _source_box(entity, source)
            raw = _mtext_raw(records, entity.start, entity.end)
            paragraphs = _mtext_paragraphs(raw, entity.height)
            count = len(source.split("\n"))
            if len(paragraphs) <= count:
                continue
            translation = (count, len(paragraphs)) if below else (0, len(paragraphs) - count)
            row = _Row("MTEXT", entity.start, entity.end, entity.height, left, right, top, bottom,
                       entity=entity, seed=True, paragraphs=paragraphs, translation=translation, source=source)
        else:
            box = _box(entity)
            row = _Row(entity.entity_type, entity.start, entity.end, entity.height,
                       box.left, box.right, box.top, box.bottom, entity=entity)
            if rotated:
                row.kind = "OTHER"
        row.current_top = row.top
        items.setdefault(entity.container[1], []).append(row)
    inserts = []
    for start, end in _entity_ranges(records):
        if records[start].value.strip().upper() != "INSERT":
            continue
        fields: dict[int, str] = {}
        for record in records[start + 1:end]:
            if record.code == 101:
                break
            fields.setdefault(record.code, record.value)
        inserts.append((start, end, fields))
    references: dict[str, int] = {}
    for _start, _end, fields in inserts:
        name = str(fields.get(2, "")).strip().upper()
        references[name] = references.get(name, 0) + 1
    for start, end, fields in inserts:
        name = str(fields.get(2, "")).strip().upper()
        if name not in text_blocks or abs(_float(fields.get(50))) > 0.1:
            continue
        sx, sy = _float(fields.get(41), 1.0) or 1.0, _float(fields.get(42), 1.0) or 1.0
        if sx <= 0 or sy <= 0 or not block_entities.get(name):
            continue
        bx, by = text_blocks[name]
        x, y = _float(fields.get(10)), _float(fields.get(20))
        boxes = [_box(member) for member in block_entities[name]]
        originals = []
        for member in block_entities[name]:
            source = sources.get(member.entity_no)
            if source is not None and member.entity_type == "MTEXT":
                originals.append(_source_box(member, plain_text(source)))
            else:
                box = _box(member)
                originals.append((box.left, box.right, box.bottom, box.top))

        def place_x(value: float, x: float = x, bx: float = bx, sx: float = sx) -> float:
            return x + (value - bx) * sx

        def place_y(value: float, y: float = y, by: float = by, sy: float = sy) -> float:
            return y + (value - by) * sy

        row = _Row(
            "INSERT", start, end, min(member.height for member in block_entities[name]) * sy,
            place_x(min(box[0] for box in originals)), place_x(max(box[1] for box in originals)),
            place_y(max(box[3] for box in originals)), place_y(min(box[2] for box in originals)),
        )
        row.current_top = place_y(max(box.top for box in boxes))
        row.full_height = row.current_top - place_y(min(box.bottom for box in boxes))
        members = block_entities[name]
        inners = [member for member in members if member.entity_type == "MTEXT" and member.entity_no in sources]
        inner = inners[0] if inners else members[0]
        if len(inners) == 1 and references.get(name) == 1 and abs(inner.rotation) < 0.1:
            # 图块里只有这一段双语文字（旁边可以有上标一类的小字）、图块只插入了一次：
            # 和直接放着的一样排（译文可缩小）。
            source = plain_text(sources[inner.entity_no])
            paragraphs = _mtext_paragraphs(_mtext_raw(records, inner.start, inner.end), inner.height)
            count = len(source.split("\n"))
            if len(paragraphs) > count:
                row.seed, row.entity, row.source, row.paragraphs = True, inner, source, paragraphs
                row.translation = (count, len(paragraphs)) if below else (0, len(paragraphs) - count)
                row.height, row.scale_y = inner.height * sy, sy
                row.inner_top = originals[members.index(inner)][3]
        container = "1" if str(fields.get(67, "0")).strip() == "1" else "0"
        items.setdefault(container, []).append(row)

    segments = _line_segments(records)
    changed = 0
    moves: list[tuple[str, int, int, float]] = []
    edits: list[tuple[_Row, float, float, float]] = []  # (条目, 新顶边, 译文比例, 换行宽度)
    for space, rows in items.items():
        lines = segments.get(("SPACE", space), [])
        for column in _columns(rows, lines):
            result = _layout(column, rows, lines, settings, bilingual_scale)
            if result is None:
                continue
            placements, scale, wraps, fits = result
            for row, new_top in zip(column, placements, strict=True):
                if row.kind == "MTEXT" and row.seed:
                    edits.append((row, new_top, scale, wraps[id(row)]))
                elif row.kind == "INSERT" and row.seed:
                    # 图块里的文字改成顶边对齐、留在原处，整个图块挪到新位置。
                    edits.append((row, row.inner_top, scale, wraps[id(row)]))
                    moves.append((row.kind, row.start, row.end, new_top - row.top))
                else:
                    shift = new_top - (row.current_top if row.kind == "INSERT" else row.top)
                    if abs(shift) > 1e-9:
                        moves.append((row.kind, row.start, row.end, shift))
                # 编号和这一条的顶边对齐（译文在上时对着译文第一行）。
                for kind, start, end in row.labels:
                    moves.append((kind, start, end, new_top - row.top))
            changed += len(column)
            if not fits and unresolved is not None:
                first = next(row for row in column if row.seed)
                unresolved.append({
                    "source": first.source,
                    "translation": " ".join(text.strip() for text in (
                        first.paragraphs[n][1] for n in range(*first.translation)) if text.strip()),
                    "entity": first.entity.entity_no if first.entity else -1,
                })

    # 从后往前改：改内容会增减记录，前面实体的位置不受影响。
    work: list[tuple[int, Callable[[], None]]] = []
    for kind, start, end, shift in moves:
        work.append((start, lambda kind=kind, start=start, end=end, shift=shift: _move(records, start, end, kind, shift)))
    for row, new_top, scale, wrap in edits:
        start = row.entity.start if row.entity else row.start
        work.append((start, lambda row=row, new_top=new_top, scale=scale, wrap=wrap:
                     _apply_mtext(records, row, new_top, scale, wrap, bilingual_scale, below)))
    for _start, action in sorted(work, key=lambda item: item[0], reverse=True):
        action()
    return changed


def _apply_mtext(records: list[Any], row: _Row, new_top: float, scale: float, wrap: float,
                 bilingual_scale: float, below: bool) -> None:
    entity = row.entity
    assert entity is not None
    start, end = entity.start, entity.end
    # 顶边对齐，插入点放到新的顶边上。
    current = next((int(_float(r.value)) for r in records[start + 1:end] if r.code == 71), 1)
    if current in _TOP_ATTACHMENT:
        _set_attachment(records, start, end, _TOP_ATTACHMENT[current])
    _move(records, start, end, "MTEXT", new_top - entity.anchor_y)
    if abs(wrap - entity.ref_width) > 1e-6:
        _write_size(records, start, end, entity.height, wrap)
        end = next((index for index in range(start + 1, len(records)) if records[index].code == 0), len(records))
    if abs(scale - bilingual_scale) > 1e-9:
        raw = _mtext_raw(records, start, end)
        marker = f"{{\\H{bilingual_scale}x;"
        position = raw.rfind(marker) if below else raw.find(marker)
        if position >= 0:
            raw = raw[:position] + f"{{\\H{_format_number(scale)}x;" + raw[position + len(marker):]
            _write_mtext_content(records, start, end, raw)


def _columns(rows: Sequence[_Row], lines: Sequence[tuple[float, float, float, float]]) -> list[list[_Row]]:
    """找出「一列上下排的说明」：至少两条双语多行文字，左边对齐，上下空不大，中间没有横线。"""
    seeds = sorted((row for row in rows if row.seed), key=lambda row: -row.top)
    used: set[int] = set()
    columns: list[list[_Row]] = []
    for seed in seeds:
        if id(seed) in used:
            continue
        reach = _LEFT_TOLERANCE * seed.height

        def in_column(row: _Row, seed: _Row = seed, reach: float = reach) -> bool:
            # 左边对齐、横向和这一条大半重合（左边单独的编号不算，它跟着自己那一条挪）。
            overlap = min(row.right, seed.right) - max(row.left, seed.left)
            return (row.kind in {"MTEXT", "TEXT", "INSERT"} and abs(row.left - seed.left) <= reach
                    and overlap > 0.5 * min(row.right - row.left, seed.right - seed.left)
                    and 1 / _HEIGHT_RATIO <= row.height / seed.height <= _HEIGHT_RATIO)

        candidates = sorted((row for row in rows if in_column(row) and id(row) not in used), key=lambda row: -row.top)
        start = candidates.index(seed)
        column = [seed]
        for row in candidates[start + 1:]:
            previous = column[-1]
            gap = previous.bottom - row.top
            if gap < -0.3 * min(previous.height, row.height) or gap > _ROW_GAP * previous.height:
                break
            if _crossed(lines, previous, row):
                break
            column.append(row)
        # 至少两条是成句的说明（系统图里「保护」「测量」这类贴着符号的短标注不挪，挪开就和符号对不上了）。
        if sum(1 for row in column if row.seed and len("".join(row.source.split())) >= _MIN_NOTE_CHARS) >= 2:
            used.update(id(row) for row in column)
            columns.append(column)
    for column in columns:
        _attach_labels(column, rows, used)
    return columns


def _crossed(lines: Sequence[tuple[float, float, float, float]], upper: _Row, lower: _Row) -> bool:
    """两条之间有没有横线隔着（画线的表格）。"""
    low, high = lower.top - 0.1 * lower.height, upper.bottom + 0.1 * upper.height
    left = max(upper.left, lower.left)
    right = min(upper.right, lower.right)
    for x1, y1, x2, y2 in lines:
        if abs(y1 - y2) > 1e-6 * max(abs(x2 - x1), 1.0):
            continue
        if low < y1 < high and min(x1, x2) <= right and max(x1, x2) >= left:
            return True
    return False


def _attach_labels(column: list[_Row], rows: Sequence[_Row], used: set[int]) -> None:
    """列左边的编号（「1.」「2.」）跟着它那一条一起挪。"""
    left = min(row.left for row in column)
    height = column[0].height
    for row in rows:
        if id(row) in used or row.kind not in {"MTEXT", "TEXT"}:
            continue
        if row.right > left + 0.5 * height or row.left < left - _LABEL_REACH * height:
            continue
        if row.entity is None or not _LABEL_RE.match(row.entity.text):
            continue
        best, overlap = None, 0.0
        for member in column:
            amount = min(member.top, row.top) - max(member.bottom, row.bottom)
            if amount > overlap:
                best, overlap = member, amount
        if best is not None and overlap > 0.5 * min(row.top - row.bottom, best.top - best.bottom):
            best.labels.append((row.kind, row.start, row.end))
            used.add(id(row))


def _layout(
    column: list[_Row],
    rows: Sequence[_Row],
    lines: Sequence[tuple[float, float, float, float]],
    settings: FitSettings,
    bilingual_scale: float,
) -> tuple[list[float], float, dict[int, float], bool] | None:
    """``(每条的新顶边, 译文比例, 每条的换行宽度, 放不放得下)``；本来就不相压时返回 ``None``。"""
    members = {id(row) for row in column}
    labels = {(start, end) for row in column for _kind, start, end in row.labels}
    others = [row for row in rows if id(row) not in members and (row.start, row.end) not in labels]
    height = max(row.height for row in column)
    pad = _EDGE_PAD * height
    left = min(row.left for row in column)
    right = max(row.right for row in column)
    top, bottom = column[0].top, column[-1].bottom

    # 右边能放宽到哪里：竖线、右边的文字，在本列上下范围内离得最近的。
    right_limit: float | None = None
    for x1, y1, x2, y2 in lines:
        if abs(x1 - x2) > 1e-6 * max(abs(y2 - y1), 1.0):
            continue
        if x1 >= right - 0.1 * height and min(y1, y2) < top and max(y1, y2) > bottom - 3 * height:
            right_limit = x1 if right_limit is None else min(right_limit, x1)
    for row in others:
        if row.left >= right - 0.1 * height and row.bottom < top and row.top > bottom - 3 * height:
            right_limit = row.left if right_limit is None else min(right_limit, row.left)

    def wrap_for(row: _Row, widen: bool) -> float:
        entity = row.entity
        assert entity is not None
        if row.kind == "INSERT":
            return entity.ref_width
        if not widen or entity.halign != 0 or right_limit is None:
            return entity.ref_width
        # 放宽到右边的线或文字前面；原来的宽度已经伸过去的，收回来（不窄于原文一行）。
        return max(row.right - row.left, right_limit - pad - row.left)

    def floor_below(span_right: float) -> float | None:
        """本列最后一条下面，挡着的横线或文字的上边。"""
        found: float | None = None
        for x1, y1, x2, y2 in lines:
            if abs(y1 - y2) > 1e-6 * max(abs(x2 - x1), 1.0):
                continue
            if y1 < bottom and min(x1, x2) < span_right and max(x1, x2) > left:
                found = y1 if found is None else max(found, y1)
        for row in others:
            if row.top < bottom and row.left < span_right and row.right > left and row.kind != "OTHER":
                found = row.top if found is None else max(found, row.top)
        return found

    gaps = [max(0.0, upper.bottom - lower.top) for upper, lower in pairwise(column)]

    def place(scale: float, widen: bool, tight: bool) -> tuple[list[float], float, dict[int, float]]:
        placements: list[float] = []
        wraps: dict[int, float] = {}
        cursor: float | None = None
        for index, row in enumerate(column):
            new_top = row.top
            if cursor is not None:
                gap = gaps[index - 1]
                if tight:
                    gap = min(gap, _MIN_ROW_GAP * row.height)
                new_top = min(row.top, cursor - gap)
            if row.seed:
                wraps[id(row)] = wrap_for(row, widen)
                size = _content_height(row, scale / bilingual_scale, wraps[id(row)])
            elif row.kind == "INSERT":
                size = row.full_height
            else:
                size = row.top - row.bottom
            placements.append(new_top)
            cursor = new_top - size
        assert cursor is not None
        return placements, cursor, wraps

    scales = [bilingual_scale, *(tier for tier in settings.tiers() if tier < bilingual_scale - 1e-9)]
    # 字大优先：每一档先试原宽度，再放宽到右边，再收窄条目之间的空，都不行才缩下一档。
    options = [(scale, widen, tight) for scale in scales for widen, tight in ((False, False), (True, False), (True, True))]
    last = None
    for scale, widen, tight in options:
        placements, end, wraps = place(scale, widen, tight)
        span_right = max([right, *(row.left + wrap for row in column if row.seed for wrap in [wraps[id(row)]])])
        floor = floor_below(span_right)
        last = (placements, scale, wraps)
        if floor is None or end >= floor + pad:
            if scale == bilingual_scale and not widen and placements == [row.top for row in column] and all(
                row.entity is None or row.entity.valign == 3 for row in column if row.seed
            ):
                # 已经是顶边对齐、谁也不用挪：不改。
                return None
            return placements, scale, wraps, True
    assert last is not None
    return last[0], last[1], last[2], False
