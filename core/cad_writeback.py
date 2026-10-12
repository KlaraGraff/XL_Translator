"""DXF surgery for the CAD write-back: text styles, added entities, markers.

译文写回原本只改文字内容。双语显示和「译文用兼容字体」还需要动三样东西：

* 文字样式表里新增一个译文专用样式（AutoCAD 自带的 simplex.shx，中文用
  gbenor.shx + gbcbig.shx），译文改用它，客户电脑上没有天正、第三方字体也不会
  被随机替换，法语重音字母也不会落到中文大字体里显示成问号；
* 双语时在单行文字旁边新增一段译文文字（复制原文字的全部属性，换内容、字高和位置）；
* 给我们新增或改成双语的文字打一个隐藏标记（扩展数据，应用名 ``XL_TRANSLATOR``），
  同一张图再翻一次时先把旧译文拿掉、恢复原文，不会叠出第二份。

新增的表记录和实体都要有不重复的句柄，并把文件头的 ``$HANDSEED`` 往后推。
没有句柄的老式 DXF（R12）不加句柄；没有样式表 / 应用名表的图纸不加样式或标记，
照常写回译文。
"""
from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any

APP_NAME = "XL_TRANSLATOR"
TRANSLATION_STYLE = "XLT_TRANSLATION"
# 扩展数据里第一个字符串说明这段文字是怎么来的。
MARK_ADDED = "added"          # 双语时新增的译文文字：再翻一次时整个删掉
MARK_COMBINED = "combined"    # 原文和译文写在同一段里：后面跟着原文，再翻一次时恢复
MARK_TABLE = "table"          # 表格单元格改成了双语：后面跟着 {实体内位置: 原值} 的 JSON
_XDATA_CHUNK = 240

# 译文专用样式的字体：都是 AutoCAD 安装自带的（AutoCAD 2026 for Mac 的安装清单与
# 签名校验核实过）。simplex 收了除 œ 以外的法语字母；中文用国标字体加国标大字体。
_LATIN_FONT = ("simplex.shx", "")
_CJK_FONT = ("gbenor.shx", "gbcbig.shx")
_LATIN_LANGS = frozenset({
    "en", "fr", "es", "pt", "de", "it", "nl", "ca", "ro", "pl", "cs", "sk", "sv",
    "da", "no", "nb", "fi", "id", "ms", "tr", "vi", "sw", "af", "hu", "hr", "sl",
})

# SHX 字体里可能没有的排版符号换成同义的 ASCII；œ/Œ 按法语排版的通行写法拆成两个字母。
_SHX_SAFE = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"',
    "\u2013": "-", "\u2014": "-", "\u2212": "-",
    "\u2026": "...",
    "\u00a0": " ", "\u202f": " ", "\u2009": " ", "\u2007": " ",
    "\u0153": "oe", "\u0152": "OE",
})

_CJK_RE = re.compile(r"[\u2e80-\u9fff\uac00-\ud7af\uf900-\ufaff\uff00-\uffef]")


def compatible_font(target_lang: str) -> tuple[str, str] | None:
    """译文专用样式用的 ``(字体, 大字体)``；这个目标语言没有合适的自带字体时返回 None。"""
    base = str(target_lang or "").split("-", 1)[0].split("_", 1)[0].lower()
    if base == "zh":
        return _CJK_FONT
    if base in _LATIN_LANGS:
        return _LATIN_FONT
    return None


def shx_safe(text: str) -> str:
    return text.translate(_SHX_SAFE)


def has_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text))


def _entity_ranges(records: Sequence[Any]) -> list[tuple[int, int]]:
    starts = [index for index, record in enumerate(records) if record.code == 0]
    starts.append(len(records))
    return [(start, end) for start, end in zip(starts, starts[1:]) if start < end]


def _copy(record: Any, *, code: int | None = None, value: str) -> Any:
    if code is None or code == record.code:
        return replace(record, value=value)
    code_text = str(code).rjust(len(record.code_text)) if record.code_text else str(code)
    return replace(record, code=code, value=value, code_text=code_text)


class HandleAllocator:
    """Hands out unused handles and keeps ``$HANDSEED`` ahead of them."""

    def __init__(self, records: list[Any]) -> None:
        self.records = records
        highest = 0
        self.enabled = False
        for record in records:
            if record.code in {5, 105}:
                try:
                    highest = max(highest, int(record.value.strip(), 16))
                    self.enabled = True
                except ValueError:
                    continue
        for index, record in enumerate(records[:-1]):
            if record.code == 9 and record.value.strip().upper() == "$HANDSEED" and records[index + 1].code == 5:
                try:
                    highest = max(highest, int(records[index + 1].value.strip(), 16) - 1)
                except ValueError:
                    pass
                break
        self._next = highest + 1

    def next(self) -> str | None:
        if not self.enabled:
            return None
        handle = self._next
        self._next += 1
        return format(handle, "X")

    def finish(self) -> None:
        """Push ``$HANDSEED`` past every handle handed out."""
        if not self.enabled:
            return
        for index, record in enumerate(self.records[:-1]):
            if record.code == 9 and record.value.strip().upper() == "$HANDSEED" and self.records[index + 1].code == 5:
                self.records[index + 1] = _copy(self.records[index + 1], value=format(self._next, "X"))
                return


def _table_bounds(records: Sequence[Any], table: str) -> tuple[int, int] | None:
    """``(TABLE 记录的下标, ENDTAB 的下标)``。"""
    for index, record in enumerate(records[:-1]):
        if record.code == 0 and record.value.strip().upper() == "TABLE":
            following = records[index + 1]
            if following.code == 2 and following.value.strip().upper() == table:
                for end in range(index + 1, len(records)):
                    if records[end].code == 0 and records[end].value.strip().upper() == "ENDTAB":
                        return index, end
                return None
    return None


def _strip_owned_extras(body: list[Any]) -> list[Any]:
    """Drop reactor/extension-dictionary groups and XDATA from a cloned record.

    Those point at objects owned by the original; a copy that pointed at them
    too would give one dictionary two owners.
    """
    kept: list[Any] = []
    in_group = False
    for record in body:
        if record.code == 102:
            in_group = record.value.strip().startswith("{") and not in_group
            continue
        if in_group or record.code >= 1000 or record.code == 360:
            continue
        kept.append(record)
    return kept


def _table_record_names(records: Sequence[Any], start: int, end: int, kind: str) -> dict[str, tuple[int, int]]:
    names: dict[str, tuple[int, int]] = {}
    for lo, hi in _entity_ranges(records[start:end]):
        lo += start
        hi += start
        if records[lo].value.strip().upper() != kind:
            continue
        name = next((r.value.strip().upper() for r in records[lo + 1:hi] if r.code == 2), "")
        if name:
            names[name] = (lo, hi)
    return names


def _ensure_table_record(
    records: list[Any],
    handles: HandleAllocator,
    table: str,
    name: str,
    edit: Callable[[list[Any]], list[Any]],
    template_ok: Callable[[list[Any]], bool] = lambda body: True,
) -> bool:
    bounds = _table_bounds(records, table)
    if bounds is None:
        return False
    start, end = bounds
    existing = _table_record_names(records, start, end, table)
    if name.upper() in existing:
        lo, hi = existing[name.upper()]
        records[lo:hi] = edit(list(records[lo:hi]))
        return True
    template: tuple[int, int] | None = None
    for lo, hi in existing.values():
        if template_ok(list(records[lo + 1:hi])):
            template = (lo, hi)
            break
    if template is None:
        return False
    lo, hi = template
    clone = [records[lo], *_strip_owned_extras(list(records[lo + 1:hi]))]
    handle = handles.next()
    rebuilt: list[Any] = []
    for record in clone:
        if record.code == 5:
            if handle is None:
                continue
            record = _copy(record, value=handle)
        elif record.code == 2:
            record = _copy(record, value=name)
        rebuilt.append(record)
    rebuilt = edit(rebuilt)
    records[end:end] = rebuilt
    # 表头的 70 是记录条数（AutoCAD 读图时不靠它，但保持准确）。
    for index in range(start + 1, end):
        if records[index].code == 0:
            break
        if records[index].code == 70:
            try:
                records[index] = _copy(records[index], value=str(int(records[index].value.strip()) + 1).rjust(len(records[index].value)))
            except ValueError:
                pass
            break
    return True


def ensure_translation_style(records: list[Any], handles: HandleAllocator, font: tuple[str, str]) -> bool:
    """Add (or refresh) the ``XLT_TRANSLATION`` text style using ``font``."""
    main, big = font

    def edit(body: list[Any]) -> list[Any]:
        out: list[Any] = []
        seen: set[int] = set()
        for record in body:
            code = record.code
            if code >= 1000:
                continue
            if code == 70:
                record = _copy(record, value="0")
            elif code == 40:
                record = _copy(record, value="0.0")
            elif code == 41:
                record = _copy(record, value="1.0")
            elif code == 50:
                record = _copy(record, value="0.0")
            elif code == 71:
                record = _copy(record, value="0")
            elif code == 3:
                record = _copy(record, value=main)
            elif code == 4:
                record = _copy(record, value=big)
            seen.add(code)
            out.append(record)
        anchor = next((i for i, r in enumerate(out) if r.code == 3), None)
        if anchor is None:
            return out
        if 4 not in seen:
            out.insert(anchor + 1, _copy(out[anchor], code=4, value=big))
        return out

    def usable(body: list[Any]) -> bool:
        # 形文件（70 的第 1 位）不是文字样式，不能拿来当模板。
        flags = next((r.value for r in body if r.code == 70), "0")
        try:
            return not int(str(flags).strip()) & 1 and any(r.code == 3 for r in body)
        except ValueError:
            return False

    return _ensure_table_record(records, handles, "STYLE", TRANSLATION_STYLE, edit, usable)


def ensure_app_id(records: list[Any], handles: HandleAllocator) -> bool:
    def edit(body: list[Any]) -> list[Any]:
        return [_copy(r, value="0") if r.code == 70 else r for r in body if r.code < 1000]

    return _ensure_table_record(records, handles, "APPID", APP_NAME, edit)


def _chunk_escapes(value: str) -> list[str]:
    tokens = re.findall(r"\\U\+[0-9A-Fa-f]{4}|\\M\+[1-5][0-9A-Fa-f]{4}|.", value, flags=re.S)
    chunks: list[str] = []
    current = ""
    for token in tokens:
        if current and len(current) + len(token) > _XDATA_CHUNK:
            chunks.append(current)
            current = ""
        current += token
    if current:
        chunks.append(current)
    return chunks


def marker_records(template: Any, kind: str, source: str = "") -> list[Any]:
    """Our XDATA group: application name, ``kind``, then the original text in chunks."""
    out = [_copy(template, code=1001, value=APP_NAME), _copy(template, code=1000, value=kind)]
    out.extend(_copy(template, code=1000, value=chunk) for chunk in _chunk_escapes(source))
    return out


def read_marker(body: Sequence[Any]) -> tuple[str, str] | None:
    """``(kind, payload)`` of our XDATA on an entity, or None."""
    for index, record in enumerate(body):
        if record.code == 1001 and record.value.strip().upper() == APP_NAME:
            values: list[str] = []
            for following in body[index + 1:]:
                if following.code == 1001:
                    break
                if following.code == 1000:
                    values.append(following.value)
            if not values:
                return None
            return values[0], "".join(values[1:])
    return None


def strip_marker(body: list[Any]) -> list[Any]:
    out: list[Any] = []
    skipping = False
    for record in body:
        if record.code == 1001:
            skipping = record.value.strip().upper() == APP_NAME
            if skipping:
                continue
        if skipping and record.code >= 1000:
            continue
        skipping = False
        out.append(record)
    return out


def restore_previous_run(records: list[Any]) -> int:
    """Undo an earlier bilingual write-back so the drawing is translated afresh.

    Added translation entities are removed; entities that held 原文 + 译文 get
    their original text back.  Returns how many entities were touched.
    """
    touched = 0
    for start, end in reversed(_entity_ranges(records)):
        body = records[start + 1:end]
        marker = read_marker(body)
        if marker is None:
            continue
        kind, payload = marker
        touched += 1
        if kind == MARK_ADDED:
            del records[start:end]
            continue
        cleaned = strip_marker(list(body))
        if kind == MARK_COMBINED and payload:
            cleaned = _restore_text(records[start].value.strip().upper(), cleaned, payload)
        elif kind == MARK_TABLE and payload:
            cleaned = _restore_cells(cleaned, payload)
        records[start + 1:end] = cleaned
    return touched


def _restore_cells(body: list[Any], payload: str) -> list[Any]:
    """表格单元格写回原值；位置按实体的 ``0`` 记录算起（1 是 body 的第一条）。"""
    try:
        originals = json.loads(payload)
    except ValueError:
        return body
    for offset, value in originals.items():
        index = int(offset) - 1
        if 0 <= index < len(body) and isinstance(value, str):
            body[index] = _copy(body[index], value=value)
    return body


def _restore_text(entity_type: str, body: list[Any], source: str) -> list[Any]:
    if entity_type in {"MLEADER", "MULTILEADER"}:
        index = next((i for i, r in enumerate(body) if r.code == 304), None)
        if index is not None:
            body[index] = _copy(body[index], value=source)
        return body
    if entity_type == "MTEXT":
        first = next((i for i, r in enumerate(body) if r.code in {1, 3}), None)
        if first is None:
            return body
        template = body[first]
        rest = [r for r in body if r.code not in {1, 3}]
        chunks = _chunk_escapes(source) or [""]
        texts = [_copy(template, code=3, value=chunk) for chunk in chunks[:-1]]
        texts.append(_copy(template, code=1, value=chunks[-1]))
        insert_at = sum(1 for r in body[:first] if r.code not in {1, 3})
        return rest[:insert_at] + texts + rest[insert_at:]
    index = next((i for i, r in enumerate(body) if r.code == 1), None)
    if index is not None:
        body[index] = _copy(body[index], value=source)
    return body


def set_style(body: list[Any], style: str) -> list[Any]:
    """Point an entity body (records after the ``0`` record) at text style ``style``."""
    for index, record in enumerate(body):
        if record.code == 101:
            break
        if record.code == 7:
            body[index] = _copy(record, value=style)
            return body
    anchor = next((i for i, r in enumerate(body) if r.code == 1), None)
    if anchor is None:
        anchor = next((i for i, r in enumerate(body) if r.code == 40), None)
    if anchor is not None:
        body.insert(anchor + 1, _copy(body[anchor], code=7, value=style))
    return body


def append_marker(body: list[Any], marker: list[Any]) -> list[Any]:
    """XDATA always comes last in an entity."""
    return [*strip_marker(body), *marker]


@dataclass(frozen=True)
class LinePlacement:
    """把新增的译文行放在原文下方（``below``）或上方，``scale`` 是译文字高比例。"""

    below: bool = True
    scale: float = 0.8
    gap: float = 0.4


def _float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _box(valign: int, anchor: float, height: float) -> tuple[float, float]:
    if valign == 3:
        return anchor - height, anchor
    if valign == 2:
        return anchor - height / 2, anchor + height / 2
    return anchor, anchor + height


def _anchor(valign: int, bottom: float, top: float) -> float:
    if valign == 3:
        return top
    if valign == 2:
        return (bottom + top) / 2
    return bottom


def added_line(
    original: Sequence[Any],
    text_value: str,
    handle: str | None,
    placement: LinePlacement,
    style: str | None,
    marker: list[Any],
) -> list[Any]:
    """A copy of a TEXT entity holding ``text_value``, one line below/above it.

    ``original`` is the whole entity (its ``0`` record first).
    """
    # 新的 0 记录要是另一个对象：写回后按对象认实体（见 cad_translation 的防重叠）。
    head, body = replace(original[0]), _strip_owned_extras(list(original[1:]))
    fields: dict[int, str] = {}
    for record in body:
        fields.setdefault(record.code, record.value)
    height = _float(fields.get(40), 0.0)
    new_height = height * placement.scale
    valign = int(_float(fields.get(73), 0))
    halign = int(_float(fields.get(72), 0))
    rotation = math.radians(_float(fields.get(50), 0.0))
    use_second = bool(halign or valign) and (11 in fields or 21 in fields)
    anchor_y_world = (_float(fields.get(11)), _float(fields.get(21))) if use_second else (_float(fields.get(10)), _float(fields.get(20)))
    # 在文字自己的方向上算上下：原文的框，往下（或上）隔一段，放新的框。
    local_y = -anchor_y_world[0] * math.sin(rotation) + anchor_y_world[1] * math.cos(rotation)
    bottom, top = _box(valign, local_y, height)
    gap = placement.gap * height
    if placement.below:
        new_top = bottom - gap
        new_bottom = new_top - new_height
    else:
        new_bottom = top + gap
        new_top = new_bottom + new_height
    shift = _anchor(valign, new_bottom, new_top) - local_y
    dx, dy = -shift * math.sin(rotation), shift * math.cos(rotation)
    out: list[Any] = [head]
    text_done = False
    for record in body:
        code = record.code
        if code == 5:
            if handle is None:
                continue
            record = _copy(record, value=handle)
        elif code == 1 and not text_done:
            record = _copy(record, value=text_value)
            text_done = True
        elif code == 40 and height > 0:
            record = _copy(record, value=format(new_height, ".12g"))
        elif code in {10, 11}:
            record = _copy(record, value=format(_float(record.value) + dx, ".12g"))
        elif code in {20, 21}:
            record = _copy(record, value=format(_float(record.value) + dy, ".12g"))
        out.append(record)
    body_out = out[1:]
    if style:
        body_out = set_style(body_out, style)
    return [head, *body_out, *marker]
