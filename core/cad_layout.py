"""Keep translated single-line CAD text from running into the text next to it.

图纸里常见「标签 + 数值」两段文字紧挨着摆：「设备基坑：」后面紧跟一段单独的
「2830X1700mm」，「坑底标高」后面紧跟「-0.090」。中文四五个字的位置，换成
英文要十几二十个字母；单行文字（TEXT/ATTRIB）只会从插入点往一个方向伸长，
伸出去就压在后面那段数值上，两段叠成一团看不清。

这里在写回译文之后看一遍：译文估算的宽度会碰到同一行上相邻的文字时，按能用
的空间把这段译文的字宽比例（必要时连同字高）缩小，让它停在相邻文字前面。
没有碰到别的文字的译文一律不动。

没有字体文件可量，宽度按字体类型粗估（SHX 单线字体与 TrueType 各一套系数，
系数取自真实图纸的实测）。估算宁可偏宽：多缩一点只是字窄一些，少缩了就还是叠在
一起。缩放有下限（字高不低于原来的一半），空间实在太小时仍可能有少量重叠。
"""
from __future__ import annotations

import bisect
import math
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

# 单个字符的前进宽度，单位是「字高 × 宽度比例」。
# SHX：GBCBIG 等大字体的汉字约 0.73，SIMPLEX/gbenor 等西文平均约 0.59（盛泰
# 项目实图里「设备基坑：」「坑底标高」两处量得），这里各取略宽的值。
# TrueType：宋体/黑体汉字是满宽 1.0，Arial 一类西文平均约 0.55。
_SHX_METRICS = {"wide": 0.75, "narrow": 0.6, "space": 0.5}
_TTF_METRICS = {"wide": 1.0, "narrow": 0.56, "space": 0.3}
_TTF_SUFFIXES = (".ttf", ".ttc", ".otf")

# 缩放下限：字高不低于原来的一半，字宽比例不低于原来的 0.35。
# 只需要收一点（不小于 0.7）时只收字宽，字高不变；再多才开始降字高。
_MIN_HEIGHT_SCALE = 0.5
_MIN_WIDTH_SCALE = 0.35
_WIDTH_ONLY_SCALE = 0.7
# 收缩后与相邻文字之间留的空隙（按原字高 × 原宽度比例计）。
_PAD_EM = 0.3

_SINGLE_LINE_TYPES = frozenset({"TEXT", "ATTRIB"})
_NEIGHBOR_TYPES = frozenset({"TEXT", "ATTRIB", "MTEXT"})
# AutoCAD 的 %%c（直径）%%d（度）%%p（正负）各显示成一个字符，%%u/%%o 只是下划线/上划线开关。
_PERCENT_SYMBOL_RE = re.compile(r"%%[cdpCDP]")
_PERCENT_TOGGLE_RE = re.compile(r"%%[uoUO]")
_MTEXT_LINE_SPACING = 1.67


@dataclass(frozen=True)
class _TextBox:
    entity_no: int
    left: float
    right: float
    bottom: float
    top: float


@dataclass
class _TextEntity:
    entity_no: int
    start: int
    end: int
    entity_type: str
    container: tuple[str, str]
    rotation: float
    anchor_x: float
    anchor_y: float
    height: float
    width_factor: float
    halign: int
    valign: int
    font_class: str
    text: str
    ref_width: float = 0.0


def _float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _display_text(text: str) -> str:
    text = _PERCENT_TOGGLE_RE.sub("", text)
    text = _PERCENT_SYMBOL_RE.sub("x", text)
    return text.replace("%%%", "%")


def estimate_text_width(text: str, height: float, width_factor: float, font_class: str = "shx") -> float:
    """Rough rendered width of one line of CAD text in drawing units."""
    metrics = _TTF_METRICS if font_class == "ttf" else _SHX_METRICS
    ems = 0.0
    for char in _display_text(text):
        if char == " ":
            ems += metrics["space"]
        elif unicodedata.east_asian_width(char) in {"W", "F"}:
            ems += metrics["wide"]
        elif not unicodedata.combining(char):
            ems += metrics["narrow"]
    return ems * abs(height) * abs(width_factor)


def _style_fonts(records: Sequence[Any]) -> dict[str, str]:
    """``STYLE`` 表：样式名（大写）→ 字体类型 ``shx`` / ``ttf``。"""
    fonts: dict[str, str] = {}
    in_style_table = False
    current: dict[int, str] | None = None

    def flush() -> None:
        if current is not None and 2 in current:
            font = current.get(3, "").strip().lower()
            fonts[current[2].strip().upper()] = "ttf" if font.endswith(_TTF_SUFFIXES) else "shx"

    for index, record in enumerate(records):
        if record.code == 0:
            value = record.value.strip().upper()
            flush()
            current = None
            if value == "TABLE":
                following = records[index + 1] if index + 1 < len(records) else None
                in_style_table = bool(following and following.code == 2 and following.value.strip().upper() == "STYLE")
            elif value == "ENDTAB":
                in_style_table = False
            elif value == "STYLE" and in_style_table:
                current = {}
            continue
        if current is not None and record.code in {2, 3} and record.code not in current:
            current[record.code] = record.value
    flush()
    return fonts


def _entity_ranges(records: Sequence[Any]) -> list[tuple[int, int]]:
    starts = [index for index, record in enumerate(records) if record.code == 0]
    starts.append(len(records))
    return list(zip(starts, starts[1:]))


def _collect_entities(
    records: Sequence[Any], plain_text: Callable[[str], str],
) -> list[_TextEntity]:
    fonts = _style_fonts(records)
    entities: list[_TextEntity] = []
    block = ""
    for entity_no, (start, end) in enumerate(_entity_ranges(records)):
        if start >= end:
            continue
        entity_type = records[start].value.strip().upper()
        if entity_type == "BLOCK":
            block = next((r.value.strip().upper() for r in records[start + 1:end] if r.code == 2), "")
            continue
        if entity_type == "ENDBLK":
            block = ""
            continue
        if entity_type not in _NEIGHBOR_TYPES:
            continue
        fields: dict[int, str] = {}
        text_parts: list[str] = []
        for record in records[start + 1:end]:
            if record.code == 101:
                # ATTRIB 里内嵌的多行文字对象自带一套坐标与字高，到这里为止。
                break
            if record.code in {1, 3}:
                if entity_type == "MTEXT" or record.code == 1:
                    text_parts.append(record.value)
                continue
            fields.setdefault(record.code, record.value)
        text = plain_text("".join(text_parts))
        if not text:
            continue
        height = _float(fields.get(40), 0.0)
        if height <= 0:
            continue
        if block:
            container = ("BLOCK", block)
        else:
            container = ("SPACE", "1" if str(fields.get(67, "0")).strip() == "1" else "0")
        if entity_type == "MTEXT":
            if 11 in fields or 21 in fields:
                rotation = math.degrees(math.atan2(_float(fields.get(21)), _float(fields.get(11), 1.0)))
            else:
                rotation = _float(fields.get(50))
            anchor_x, anchor_y = _float(fields.get(10)), _float(fields.get(20))
            attachment = _int(fields.get(71), 1)
            halign = {1: 0, 2: 1, 3: 2, 4: 0, 5: 1, 6: 2, 7: 0, 8: 1, 9: 2}.get(attachment, 0)
            valign = {1: 3, 2: 3, 3: 3, 4: 2, 5: 2, 6: 2, 7: 1, 8: 1, 9: 1}.get(attachment, 3)
            width_factor = 1.0
            ref_width = _float(fields.get(41))
        else:
            rotation = _float(fields.get(50))
            halign = _int(fields.get(72))
            valign = _int(fields.get(74 if entity_type == "ATTRIB" else 73))
            if (halign or valign) and (11 in fields or 21 in fields):
                anchor_x, anchor_y = _float(fields.get(11)), _float(fields.get(21))
            else:
                anchor_x, anchor_y = _float(fields.get(10)), _float(fields.get(20))
            width_factor = _float(fields.get(41), 1.0) or 1.0
            ref_width = 0.0
        entities.append(
            _TextEntity(
                entity_no=entity_no,
                start=start,
                end=end,
                entity_type=entity_type,
                container=container,
                rotation=rotation % 360.0,
                anchor_x=anchor_x,
                anchor_y=anchor_y,
                height=height,
                width_factor=width_factor,
                halign=halign,
                valign=valign,
                font_class=fonts.get(str(fields.get(7, "STANDARD")).strip().upper(), "shx"),
                text=text,
                ref_width=ref_width,
            )
        )
    return entities


def _local(entity: _TextEntity) -> tuple[float, float]:
    """Anchor in a frame rotated so the entity's baseline runs along +x."""
    angle = math.radians(entity.rotation)
    cos, sin = math.cos(angle), math.sin(angle)
    return entity.anchor_x * cos + entity.anchor_y * sin, -entity.anchor_x * sin + entity.anchor_y * cos


def _horizontal_span(halign: int, x: float, width: float) -> tuple[float, float]:
    if halign in {1, 4}:
        return x - width / 2, x + width / 2
    if halign == 2:
        return x - width, x
    return x, x + width


def _vertical_span(valign: int, y: float, height: float) -> tuple[float, float]:
    if valign == 3:
        return y - height, y
    if valign == 2:
        return y - height / 2, y + height / 2
    return y, y + height


def _box(entity: _TextEntity) -> _TextBox:
    x, y = _local(entity)
    if entity.entity_type == "MTEXT":
        lines = [line for line in entity.text.split("\n")] or [""]
        width = max(estimate_text_width(line, entity.height, 1.0, entity.font_class) for line in lines)
        if entity.ref_width > 0:
            width = min(width, entity.ref_width)
        total_height = entity.height * (1 + (len(lines) - 1) * _MTEXT_LINE_SPACING)
    else:
        width = estimate_text_width(entity.text, entity.height, entity.width_factor, entity.font_class)
        total_height = entity.height
    left, right = _horizontal_span(entity.halign, x, width)
    bottom, top = _vertical_span(entity.valign, y, total_height)
    return _TextBox(entity.entity_no, left, right, bottom, top)


def _scales(fit: float) -> tuple[float, float]:
    """``(字高缩放, 字宽缩放)``，两者相乘尽量等于 ``fit``。"""
    if fit >= _WIDTH_ONLY_SCALE:
        return 1.0, fit
    height_scale = max(_MIN_HEIGHT_SCALE, fit / _WIDTH_ONLY_SCALE)
    width_scale = max(_MIN_WIDTH_SCALE, fit / height_scale)
    return height_scale, width_scale


def _format_number(value: float) -> str:
    return format(value, ".12g")


def fit_translated_texts(
    records: list[Any],
    originals: Mapping[int, str],
    plain_text: Callable[[str], str],
) -> int:
    """Shrink translated TEXT/ATTRIB entities that would overlap a neighbour.

    ``originals`` maps the ordinal of each translated entity (its position
    among ``0`` records, stable across MTEXT write-back) to its source text.
    Edits ``records`` in place and returns how many entities were shrunk.
    """
    if not originals:
        return 0
    entities = _collect_entities(records, plain_text)
    groups: dict[tuple[tuple[str, str], float], list[_TextBox]] = {}
    for entity in entities:
        groups.setdefault((entity.container, round(entity.rotation, 1)), []).append(_box(entity))
    indexes: dict[tuple[tuple[str, str], float], tuple[list[float], list[_TextBox], float]] = {}
    for key, boxes in groups.items():
        boxes.sort(key=lambda box: box.bottom)
        tallest = max(box.top - box.bottom for box in boxes)
        indexes[key] = ([box.bottom for box in boxes], boxes, tallest)

    edits: list[tuple[_TextEntity, float, float]] = []
    for entity in entities:
        original = originals.get(entity.entity_no)
        if original is None or entity.entity_type not in _SINGLE_LINE_TYPES or entity.halign not in {0, 1, 2, 4}:
            # 72=3（对齐）/5（布满）由 CAD 自己挤进两点之间，不会伸出去。
            continue
        height, width_factor = entity.height, entity.width_factor
        new_width = estimate_text_width(entity.text, height, width_factor, entity.font_class)
        old_width = estimate_text_width(plain_text(original), height, width_factor, entity.font_class)
        if new_width <= old_width:
            continue
        x, y = _local(entity)
        bottom, top = _vertical_span(entity.valign, y, height)
        new_left, new_right = _horizontal_span(entity.halign, x, new_width)
        old_left, old_right = _horizontal_span(entity.halign, x, old_width)
        bottoms, boxes, tallest = indexes[(entity.container, round(entity.rotation, 1))]
        lo = bisect.bisect_left(bottoms, bottom - tallest)
        hi = bisect.bisect_right(bottoms, top)
        right_limit = math.inf
        left_limit = -math.inf
        for box in boxes[lo:hi]:
            if box.entity_no == entity.entity_no:
                continue
            overlap = min(top, box.top) - max(bottom, box.bottom)
            if overlap < 0.3 * min(top - bottom, box.top - box.bottom):
                continue
            # 原文本来就压着的（或估算误差内的）不算：只看原文旁边、译文伸过去才碰到的。
            if box.left >= (old_left + old_right) / 2 and box.left > x and box.left < new_right:
                right_limit = min(right_limit, box.left)
            elif box.right <= (old_left + old_right) / 2 and box.right < x and box.right > new_left:
                left_limit = max(left_limit, box.right)
        if entity.halign in {1, 4}:
            half = min(right_limit - x, x - left_limit)
            allowed = 2 * half
        elif entity.halign == 2:
            allowed = x - left_limit
        else:
            allowed = right_limit - x
        if not math.isfinite(allowed):
            continue
        # 估算的原文已经比可用空间宽：说明这套字体比系数窄，译文估算同比例收一收。
        calibration = max(0.5, min(1.0, allowed / old_width)) if old_width > 0 else 1.0
        needed = new_width * calibration
        room = allowed - _PAD_EM * height * width_factor
        if needed <= room:
            continue
        height_scale, width_scale = _scales(max(room, 0.0) / needed)
        edits.append((entity, height * height_scale, width_factor * width_scale))

    for entity, new_height, new_width_factor in sorted(edits, key=lambda item: item[0].start, reverse=True):
        _write_size(records, entity.start, entity.end, new_height, new_width_factor)
    return len(edits)


def _write_size(records: list[Any], start: int, end: int, height: float, width_factor: float) -> None:
    height_index: int | None = None
    width_index: int | None = None
    for index in range(start + 1, end):
        code = records[index].code
        if code == 101:
            break
        if code == 40 and height_index is None:
            height_index = index
        elif code == 41 and width_index is None:
            width_index = index
    if height_index is None:
        return
    records[height_index] = replace(records[height_index], value=_format_number(height))
    if width_index is not None:
        records[width_index] = replace(records[width_index], value=_format_number(width_factor))
        return
    template = records[height_index]
    code_text = "41".rjust(len(template.code_text)) if template.code_text else "41"
    records.insert(
        height_index + 1,
        replace(template, code=41, value=_format_number(width_factor), code_text=code_text),
    )


def short_text_rows(
    records: Sequence[Any],
    plain_text: Callable[[str], str],
    is_short: Callable[[str], bool],
    *,
    limit: int = 40,
) -> list[str]:
    """How short labels read together with the text beside them on the drawing.

    天正把门号「FM甲1021」存成「FM」「甲」「1021」三段紧挨着的单行文字。翻译时
    「甲」单独送去，连通读全图的模型也只看到一个孤立的「甲」，于是猜成合同甲方
    或火灾危险性类别。这里把每段短文字和同一行上紧挨着的文字按从左到右连起来
    （「FM 甲 1021」），按出现次数返回最常见的 ``limit`` 种，给写图纸说明的那次请求看。
    """
    entities = _collect_entities(records, plain_text)
    groups: dict[tuple[tuple[str, str], float], list[_TextEntity]] = {}
    for entity in entities:
        if entity.entity_type in _SINGLE_LINE_TYPES:
            groups.setdefault((entity.container, round(entity.rotation, 1)), []).append(entity)
    rows: dict[str, int] = {}
    for group in groups.values():
        shorts = [entity for entity in group if is_short(entity.text)]
        if not shorts:
            continue
        boxes = {entity.entity_no: _box(entity) for entity in group}
        # 按基线高度排好，只在上下差不多一行高的范围里找邻居。
        ordered = sorted(group, key=lambda entity: boxes[entity.entity_no].bottom)
        bottoms = [boxes[entity.entity_no].bottom for entity in ordered]
        for short in shorts:
            box = boxes[short.entity_no]
            line_height = box.top - box.bottom
            lo = bisect.bisect_left(bottoms, box.bottom - line_height)
            hi = bisect.bisect_right(bottoms, box.bottom + line_height)
            row = [short]
            for other in ordered[lo:hi]:
                if other is short:
                    continue
                other_box = boxes[other.entity_no]
                overlap = min(box.top, other_box.top) - max(box.bottom, other_box.bottom)
                if overlap < 0.5 * min(line_height, other_box.top - other_box.bottom):
                    continue
                gap = max(other_box.left - box.right, box.left - other_box.right)
                if gap > 1.5 * short.height:
                    continue
                row.append(other)
            if len(row) < 2:
                continue
            row.sort(key=lambda entity: boxes[entity.entity_no].left)
            text = " ".join(" ".join(entity.text.split()) for entity in row)
            rows[text] = rows.get(text, 0) + 1
    ranked = sorted(rows, key=lambda text: (-rows[text], text))
    return ranked[:limit]
