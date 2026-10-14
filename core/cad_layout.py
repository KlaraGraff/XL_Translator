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
一起。

格线也是边界：「说明」一类贴着图框右边摆的文字旁边往往没有别的文字，表格、图签的
格子和房间的墙是用线画的，译文照样会伸出去。原文四周都有实线围着（图框内框、表格
格子、图签、房间，含插入的图块里画的）时，译文同样不越过这些线；双语时新增的译文行
也留在原文的格子里。

缩多少由用户设置决定（见 :class:`FitSettings`）：放不下时先缩到「默认比例」，同一张
图里缩过的字大小一致；还放不下就每次再缩 0.1，直到放下或到「最小比例」。到了最小
比例仍放不下的停在最小比例，并记进报告请人复核。
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
# simplex.shx / romans.shx（译文样式 XLT_TRANSLATION 用的就是 simplex）是 Hershey 单线字体，
# 逐字宽度可查：下面是 Hershey「futural」（即 simplex）每个 ASCII 字符的前进宽度，单位是
# 大写字母高 21 的字体单位，AutoCAD 的字高就是这个 21。法文等西文平均每字约 0.8 字高，
# 比上面 SHX 通用系数的 0.6 宽三成：2026-10-14 在 AutoCAD 2026 里量「Salle de réception」
# 「Local de distribution électrique」，实际宽与本表相差不到 6%。
_HERSHEY_FONTS = frozenset({"simplex", "romans"})
_HERSHEY_UNITS = 21.0
_HERSHEY_WIDTHS = dict(zip(
    " !\"#$%&'()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_`abcdefghijklmnopqrstuvwxyz{|}~",
    (
        16, 10, 16, 21, 20, 24, 26, 10, 14, 14, 16, 26, 8, 26, 8, 22, 20, 20, 20, 20, 20, 20, 20, 20, 20, 20,
        8, 8, 24, 26, 24, 18, 27, 18, 21, 21, 21, 19, 18, 21, 22, 8, 16, 21, 17, 24, 22, 22, 21, 22, 21, 20, 16,
        22, 18, 24, 20, 18, 20, 14, 14, 14, 16, 18, 8, 19, 19, 18, 19, 18, 12, 19, 19, 8, 10, 17, 8, 30, 19, 19,
        19, 19, 13, 17, 12, 19, 16, 22, 17, 16, 17, 14, 8, 14, 24,
    ),
    strict=True,
))
# 表里没有的字符：弯引号、长破折号按对应的直写法，其余（œ、°、€ 等）按小写 o 的宽度。
_HERSHEY_ALIASES = {"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-", "\u2014": "-", "\u00a0": " "}
_HERSHEY_DEFAULT = 19

# 整体缩放（字高缩放 × 字宽缩放）不小于 0.7 时只收字宽，字高不变，和旁边的文字、
# 标注线还对得齐；再多才开始降字高，字宽停在原来的 0.7。
_WIDTH_ONLY_SCALE = 0.7
# 默认比例之后每档再缩多少。
_SCALE_STEP = 0.1
# 设置的取值范围（界面同样限制，这里兜底旧数据和手改的 settings.json）。
DEFAULT_SCALE_RANGE = (0.5, 1.0)
MIN_SCALE_RANGE = (0.3, 1.0)
# 收缩后与相邻文字之间留的空隙（按原字高 × 原宽度比例计）。
_PAD_EM = 0.3

_SINGLE_LINE_TYPES = frozenset({"TEXT", "ATTRIB"})
_NEIGHBOR_TYPES = frozenset({"TEXT", "ATTRIB", "MTEXT"})
# 一个容器里最多认这么多条线（成千上万次插入同一个图块时不至于拖慢）。
_MAX_SEGMENTS = 200000
# POLYLINE 的 70 组码：1 闭合；8/16/64 是三维多段线、网格，不是平面上的框。
_POLYLINE_NOT_FLAT = 8 | 16 | 64
# AutoCAD 的 %%c（直径）%%d（度）%%p（正负）各显示成一个字符，%%u/%%o 只是下划线/上划线开关。
_PERCENT_SYMBOL_RE = re.compile(r"%%[cdpCDP]")
_PERCENT_TOGGLE_RE = re.compile(r"%%[uoUO]")
_MTEXT_LINE_SPACING = 1.67


def is_chinese_target(target_lang: str) -> bool:
    """目标语言是不是中文（简体、繁体都算）。"""
    return str(target_lang or "").strip().lower().startswith(("zh", "cn"))


@dataclass(frozen=True)
class FitSettings:
    """放不下时缩到多少：先到 ``default_scale``，再按 0.1 一档降到 ``min_scale``。"""

    default_scale: float = 0.7
    min_scale: float = 0.4

    def normalized(self) -> "FitSettings":
        default = min(max(_float(self.default_scale, 0.7), DEFAULT_SCALE_RANGE[0]), DEFAULT_SCALE_RANGE[1])
        minimum = min(max(_float(self.min_scale, 0.4), MIN_SCALE_RANGE[0]), default)
        return FitSettings(round(default, 2), round(minimum, 2))

    def tiers(self) -> list[float]:
        """从大到小的各档整体缩放比例，第一档是默认比例，最后一档是最小比例。"""
        settings = self.normalized()
        tiers = [settings.default_scale]
        current = settings.default_scale
        while current - _SCALE_STEP > settings.min_scale + 1e-9:
            current = round(current - _SCALE_STEP, 2)
            tiers.append(current)
        if tiers[-1] > settings.min_scale + 1e-9:
            tiers.append(settings.min_scale)
        return tiers


# 两套默认比例，按目标语言自动套用（2026-10-13 用 4 张真实图纸实测，见 PR 说明）：
# * 中文译成外文：译文普遍是原文的三四倍宽，同一张图里要缩的字成百上千，先统一缩到 0.7，
#   同一张图里缩过的字大小一致；再往下最多到 0.4；降到 0.3 也只多放下约一成，字却小得难认。
# * 外文译成中文：只有百分之几的译文比原文宽，多是「C → 丙」「N° → 序号」这类只长两三成的，
#   刚好放下要 0.8 上下。初始比例取 1.0：不预先统一缩小，放不下时从原字号起每次缩 0.1，
#   缩到刚好放下为止，整张图的观感和原图一致。最小比例同样 0.4：
#   双语时新增的中文行要挤进原文和下一行之间，提高到 0.5，放不下的行会从约四成升到六成多。
DEFAULT_FIT_SETTINGS = FitSettings(0.7, 0.4)
DEFAULT_FIT_SETTINGS_ZH = FitSettings(1.0, 0.4)


def default_fit_settings(target_lang: str) -> FitSettings:
    """目标语言对应的那套默认比例。"""
    return DEFAULT_FIT_SETTINGS_ZH if is_chinese_target(target_lang) else DEFAULT_FIT_SETTINGS


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


def _hershey_ems(char: str) -> float:
    char = _HERSHEY_ALIASES.get(char, char)
    width = _HERSHEY_WIDTHS.get(char)
    if width is None:
        # 带重音的字母（é、à、ç）和不带的同宽。
        base = unicodedata.normalize("NFD", char)[:1]
        width = _HERSHEY_WIDTHS.get(base, _HERSHEY_DEFAULT)
    return width / _HERSHEY_UNITS


def estimate_text_width(text: str, height: float, width_factor: float, font_class: str = "shx") -> float:
    """Rough rendered width of one line of CAD text in drawing units."""
    metrics = _TTF_METRICS if font_class == "ttf" else _SHX_METRICS
    ems = 0.0
    for char in _display_text(text):
        if font_class == "hershey" and unicodedata.east_asian_width(char) not in {"W", "F"}:
            if not unicodedata.combining(char):
                ems += _hershey_ems(char)
            continue
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
            if font.endswith(_TTF_SUFFIXES):
                fonts[current[2].strip().upper()] = "ttf"
            elif font.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].removesuffix(".shx") in _HERSHEY_FONTS:
                fonts[current[2].strip().upper()] = "hershey"
            else:
                fonts[current[2].strip().upper()] = "shx"

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


def _to_local(x: float, y: float, rotation: float) -> tuple[float, float]:
    """A point in a frame rotated by ``rotation`` degrees (that text's baseline runs along +x)."""
    angle = math.radians(rotation)
    cos, sin = math.cos(angle), math.sin(angle)
    return x * cos + y * sin, -x * sin + y * cos


def _local(entity: _TextEntity) -> tuple[float, float]:
    """Anchor in a frame rotated so the entity's baseline runs along +x."""
    return _to_local(entity.anchor_x, entity.anchor_y, entity.rotation)


_Segment = tuple[float, float, float, float]


def _linetypes(records: Sequence[Any]) -> dict[str, str]:
    """``LAYER`` 表：图层名（大写）→ 线型名（大写）。"""
    layers: dict[str, str] = {}
    in_layer_table = False
    current: dict[int, str] | None = None

    def flush() -> None:
        if current is not None and 2 in current:
            layers[current[2].strip().upper()] = current.get(6, "CONTINUOUS").strip().upper()

    for index, record in enumerate(records):
        if record.code == 0:
            value = record.value.strip().upper()
            flush()
            current = None
            if value == "TABLE":
                following = records[index + 1] if index + 1 < len(records) else None
                in_layer_table = bool(following and following.code == 2 and following.value.strip().upper() == "LAYER")
            elif value == "ENDTAB":
                in_layer_table = False
            elif value == "LAYER" and in_layer_table:
                current = {}
            continue
        if current is not None and record.code in {2, 6} and record.code not in current:
            current[record.code] = record.value
    flush()
    return layers


def _solid(fields: Mapping[int, str], layers: Mapping[str, str]) -> bool:
    """实线才算格线；点划线（轴线）、虚线这类穿过文字的辅助线不算。"""
    linetype = str(fields.get(6, "BYLAYER")).strip().upper()
    if linetype == "BYLAYER":
        linetype = layers.get(str(fields.get(8, "0")).strip().upper(), "CONTINUOUS")
    return linetype in {"CONTINUOUS", "BYBLOCK", ""}


def _line_segments(records: Sequence[Any]) -> dict[tuple[str, str], list[_Segment]]:
    """每个容器（模型空间、图纸空间、各个图块）里的直线段，换算到该容器的坐标。

    LINE、多段线（LWPOLYLINE、旧式 POLYLINE）的直边都算；插入的图块（图框、图签通常是
    一个图块）里的线按插入点、比例、旋转换算出来，算进插入它的容器，图块里再插图块的
    一层层往下找。格子、图框、房间的墙都是这样画出来的。
    """
    layers = _linetypes(records)
    own: dict[tuple[str, str], list[_Segment]] = {}
    inserts: dict[tuple[str, str], list[tuple[str, float, float, float, float, float]]] = {}
    bases: dict[str, tuple[float, float]] = {}
    block = ""

    def add_polyline(container: tuple[str, str], points: list[tuple[float, float, float]], closed: bool) -> None:
        # 每个点带着从它出发那条边的凸度：不为 0 是圆弧，跳过。
        edges = list(zip(points, points[1:] + (points[:1] if closed else []), strict=False))
        for (x1, y1, bulge), (x2, y2, _next) in edges:
            if abs(bulge) <= 1e-9 and (x1, y1) != (x2, y2):
                own.setdefault(container, []).append((x1, y1, x2, y2))

    # 旧式 POLYLINE 的顶点是后面一串 VERTEX，到 SEQEND 为止。
    polyline: tuple[tuple[str, str], bool, list[tuple[float, float, float]]] | None = None
    for start, end in _entity_ranges(records):
        if start >= end:
            continue
        entity_type = records[start].value.strip().upper()
        body = records[start + 1:end]
        if polyline is not None and entity_type == "VERTEX":
            fields = {record.code: record.value for record in reversed(body)}
            polyline[2].append((_float(fields.get(10)), _float(fields.get(20)), _float(fields.get(42))))
            continue
        if polyline is not None:
            add_polyline(*polyline[:1], polyline[2], polyline[1])
            polyline = None
        if entity_type == "BLOCK":
            fields = {record.code: record.value for record in reversed(body)}
            block = str(fields.get(2, "")).strip().upper()
            bases[block] = (_float(fields.get(10)), _float(fields.get(20)))
            continue
        if entity_type == "ENDBLK":
            block = ""
            continue
        if entity_type not in {"LINE", "LWPOLYLINE", "POLYLINE", "INSERT"}:
            continue
        fields: dict[int, str] = {}
        points: list[tuple[float, float, float]] = []
        for record in body:
            if entity_type == "LWPOLYLINE" and record.code == 10:
                points.append((_float(record.value), 0.0, 0.0))
            elif entity_type == "LWPOLYLINE" and record.code == 20 and points:
                points[-1] = (points[-1][0], _float(record.value), 0.0)
            elif entity_type == "LWPOLYLINE" and record.code == 42 and points:
                points[-1] = (points[-1][0], points[-1][1], _float(record.value))
            else:
                fields.setdefault(record.code, record.value)
        if block:
            container = ("BLOCK", block)
        else:
            container = ("SPACE", "1" if str(fields.get(67, "0")).strip() == "1" else "0")
        if entity_type == "INSERT":
            name = str(fields.get(2, "")).strip().upper()
            if name:
                inserts.setdefault(container, []).append((
                    name,
                    _float(fields.get(10)), _float(fields.get(20)),
                    _float(fields.get(41), 1.0) or 1.0, _float(fields.get(42), 1.0) or 1.0,
                    _float(fields.get(50)),
                ))
            continue
        if not _solid(fields, layers):
            continue
        flags = _int(fields.get(70))
        if entity_type == "LINE":
            x1, y1, x2, y2 = (_float(fields.get(code)) for code in (10, 20, 11, 21))
            if (x1, y1) != (x2, y2):
                own.setdefault(container, []).append((x1, y1, x2, y2))
        elif entity_type == "LWPOLYLINE":
            add_polyline(container, points, bool(flags & 1))
        elif not flags & _POLYLINE_NOT_FLAT:
            polyline = (container, bool(flags & 1), [])

    resolved: dict[tuple[str, str], list[_Segment]] = {}
    in_progress: set[tuple[str, str]] = set()

    def resolve(container: tuple[str, str]) -> list[_Segment]:
        if container in resolved:
            return resolved[container]
        if container in in_progress:
            # 图块互相插入：不再往下找。
            return []
        in_progress.add(container)
        result = list(own.get(container, []))
        for name, x, y, scale_x, scale_y, rotation in inserts.get(container, []):
            if len(result) >= _MAX_SEGMENTS:
                break
            base_x, base_y = bases.get(name, (0.0, 0.0))
            angle = math.radians(rotation)
            cos, sin = math.cos(angle), math.sin(angle)

            def place(px: float, py: float) -> tuple[float, float]:
                sx, sy = (px - base_x) * scale_x, (py - base_y) * scale_y
                return x + sx * cos - sy * sin, y + sx * sin + sy * cos

            for x1, y1, x2, y2 in resolve(("BLOCK", name))[:_MAX_SEGMENTS - len(result)]:
                result.append((*place(x1, y1), *place(x2, y2)))
        in_progress.discard(container)
        resolved[container] = result
        return result

    return {container: segments for container in set(own) | set(inserts) if (segments := resolve(container))}


class _CellIndex:
    """一个容器、一个文字方向上的格线（在文字自己的坐标系里只留横平竖直的）。

    竖线按纵向分条、横线按横向分条，条内按位置排好；找一段文字四周最近的格线时只看
    它所在的那一条。
    """

    _BUCKETS = 512

    def __init__(self, segments: Sequence[_Segment], rotation: float, extent: tuple[float, float, float, float]) -> None:
        verticals: list[tuple[float, float, float]] = []
        horizontals: list[tuple[float, float, float]] = []
        for x1, y1, x2, y2 in segments:
            (ax, ay), (bx, by) = _to_local(x1, y1, rotation), _to_local(x2, y2, rotation)
            length = math.hypot(bx - ax, by - ay)
            if abs(ax - bx) <= 1e-6 * length:
                verticals.append(((ax + bx) / 2, min(ay, by), max(ay, by)))
            elif abs(ay - by) <= 1e-6 * length:
                horizontals.append(((ay + by) / 2, min(ax, bx), max(ax, bx)))
        left, right, bottom, top = extent
        self.vertical = self._buckets(verticals, bottom, top)
        self.horizontal = self._buckets(horizontals, left, right)

    def _buckets(self, lines: list[tuple[float, float, float]], low: float, high: float):
        size = max((high - low) / self._BUCKETS, 1e-9)
        buckets: dict[int, list[tuple[float, float, float]]] = {}
        for line in lines:
            _position, start, end = line
            first = max(math.floor((start - low) / size), 0)
            last = min(math.floor((end - low) / size), self._BUCKETS)
            for bucket in range(first, last + 1):
                buckets.setdefault(bucket, []).append(line)
        for lines_in_bucket in buckets.values():
            lines_in_bucket.sort()
        return low, size, buckets, {key: [line[0] for line in value] for key, value in buckets.items()}

    @staticmethod
    def _nearest(index, along: float, span: tuple[float, float], position: float, upward: bool) -> float | None:
        """离 ``position`` 最近、在它 ``upward`` 一侧、并且盖住 ``span`` 的线的位置。"""
        low, size, buckets, positions = index
        bucket = math.floor((along - low) / size)
        lines, keys = buckets.get(bucket, []), positions.get(bucket, [])
        if upward:
            order = range(bisect.bisect_left(keys, position), len(lines))
        else:
            order = range(bisect.bisect_right(keys, position) - 1, -1, -1)
        for number in order:
            line_position, start, end = lines[number]
            if start <= span[0] and end >= span[1]:
                return line_position
        return None

    def cell(self, box: _TextBox) -> _TextBox | None:
        """原文四周都有格线围着时（表格格子、图签、图框、房间），这个格子的四条边。

        竖线要盖住整行字高，横线要盖住原文中点；穿过原文的线不算（估算宽度留两成余量）。
        """
        height = box.top - box.bottom
        middle_x, middle_y = (box.left + box.right) / 2, (box.bottom + box.top) / 2
        half = (box.right - box.left) / 2 * 0.8
        band = (box.bottom + 0.1 * height, box.top - 0.1 * height)
        right = self._nearest(self.vertical, middle_y, band, middle_x + half, upward=True)
        left = self._nearest(self.vertical, middle_y, band, middle_x - half, upward=False)
        top = self._nearest(self.horizontal, middle_x, (middle_x, middle_x), box.top - 0.1 * height, upward=True)
        bottom = self._nearest(self.horizontal, middle_x, (middle_x, middle_x), box.bottom + 0.1 * height, upward=False)
        if None in (left, right, bottom, top):
            return None
        return _TextBox(-1, left, right, bottom, top)


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


def _scales(scale: float) -> tuple[float, float]:
    """``(字高缩放, 字宽缩放)``，两者相乘等于 ``scale``：先收字宽，收到 0.7 再降字高。"""
    if scale >= _WIDTH_ONLY_SCALE:
        return 1.0, scale
    return scale / _WIDTH_ONLY_SCALE, _WIDTH_ONLY_SCALE


def _pick_scale(fit: float, settings: FitSettings) -> tuple[float, bool]:
    """按档位挑整体缩放比例：``(比例, 是否放得下)``。

    ``fit`` 是刚好放下需要的比例。能放下的最大一档就是答案；连最小比例都放不下时
    停在最小比例。
    """
    tiers = settings.tiers()
    for tier in tiers:
        if tier <= fit + 1e-9:
            return tier, True
    return tiers[-1], False


def _format_number(value: float) -> str:
    return format(value, ".12g")


def fit_translated_texts(
    records: list[Any],
    originals: Mapping[int, str],
    plain_text: Callable[[str], str],
    settings: FitSettings | None = None,
    unresolved: list[dict[str, str]] | None = None,
) -> int:
    """Shrink translated TEXT/ATTRIB entities that would overlap a neighbour or leave their frame.

    ``originals`` maps the ordinal of each translated entity (its position
    among ``0`` records, stable across MTEXT write-back) to its source text.
    Edits ``records`` in place and returns how many entities were shrunk.
    The frame is the cell of solid lines around the source text (see
    :meth:`_CellIndex.cell`). Entities that still overlap at the minimum scale are appended to
    ``unresolved`` as ``{"source", "translation"}``.
    """
    if not originals:
        return 0
    settings = (settings or FitSettings()).normalized()
    entities = _collect_entities(records, plain_text)
    groups: dict[tuple[tuple[str, str], float], list[_TextBox]] = {}
    for entity in entities:
        groups.setdefault((entity.container, round(entity.rotation, 1)), []).append(_box(entity))
    indexes: dict[tuple[tuple[str, str], float], tuple[list[float], list[_TextBox], float]] = {}
    for key, boxes in groups.items():
        boxes.sort(key=lambda box: box.bottom)
        tallest = max(box.top - box.bottom for box in boxes)
        indexes[key] = ([box.bottom for box in boxes], boxes, tallest)
    segments = _line_segments(records)
    cell_indexes: dict[tuple[tuple[str, str], float], _CellIndex] = {}
    by_no = {entity.entity_no: entity for entity in entities}

    def original_span(neighbor_no: int, box: _TextBox) -> tuple[float, float]:
        """邻居也是译文时，它的原文占的横向范围（判断谁本来在哪一边）。"""
        neighbor = by_no.get(neighbor_no)
        source = originals.get(neighbor_no)
        if neighbor is None or source is None or neighbor.entity_type not in _SINGLE_LINE_TYPES:
            return box.left, box.right
        width = estimate_text_width(plain_text(source), neighbor.height, neighbor.width_factor, neighbor.font_class)
        return _horizontal_span(neighbor.halign, _local(neighbor)[0], width)

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
        old_center = (old_left + old_right) / 2
        group = (entity.container, round(entity.rotation, 1))
        bottoms, boxes, tallest = indexes[group]
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
            # 原文本来就压着的（或估算误差内的）不算：只看原文旁边、译文伸过去才碰到的。邻居也是
            # 变长的译文时按它的原文判断左右，两段之间原有的空隙两边各让一半（对方用不完的归这边），
            # 不然两段长译文都把对方当成「本来就压着」，谁也不缩。
            source_left, source_right = original_span(box.entity_no, box)
            if source_left >= old_center and source_left > x and box.left < new_right:
                gap = max(source_left - old_right, 0.0)
                share = max(gap / 2, gap - max(source_left - box.left, 0.0))
                right_limit = min(right_limit, old_right + share)
            elif source_right <= old_center and source_right < x and box.right > new_left:
                gap = max(old_left - source_right, 0.0)
                share = max(gap / 2, gap - max(box.right - source_right, 0.0))
                left_limit = max(left_limit, old_left - share)
        if group not in cell_indexes:
            cell_indexes[group] = _CellIndex(
                segments.get(entity.container, ()), entity.rotation,
                (min(box.left for box in boxes), max(box.right for box in boxes), bottoms[0], max(box.top for box in boxes)),
            )
        # 原文四周有格线围着（表格格子、图签、图框内框、房间的墙），译文也不越过这些线。
        cell = cell_indexes[group].cell(_TextBox(entity.entity_no, old_left, old_right, bottom, top))
        if cell is not None:
            if new_right > cell.right:
                right_limit = min(right_limit, cell.right)
            if new_left < cell.left:
                left_limit = max(left_limit, cell.left)
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
        scale, fits = _pick_scale(max(room, 0.0) / needed, settings)
        if not fits and unresolved is not None and not any(item.get("entity") == entity.entity_no for item in unresolved):
            unresolved.append({"source": plain_text(original), "translation": entity.text, "entity": entity.entity_no})
        height_scale, width_scale = _scales(scale)
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


# 双语新增的译文行和原文之间至少留这么多空（按原文字高计）；原本的空是 0.4。
_MIN_LINE_GAP = 0.15
# 西文下伸笔画（p、g、q）在基线以下的深度，按字高计：simplex 是 7/21。
_DESCENDER = 0.33


def fit_added_lines(
    records: list[Any],
    added: Mapping[int, tuple[int, bool]],
    plain_text: Callable[[str], str],
    settings: FitSettings | None = None,
    unresolved: list[dict[str, str]] | None = None,
) -> int:
    """Shrink and move bilingual translation lines that would cover the next line or cell border.

    ``added`` maps the ordinal of each added translation TEXT to
    ``(ordinal of its source TEXT, placed below)``.  表格和逐行说明里行距很紧，
    原文下方另起的一行会压到下一行的原文上：这时先把两行之间的空收窄，再按设置的
    档位（相对原文字号）缩小译文行，让它落在原文和下一行之间。到最小比例仍放不下的
    停在最小比例，记进 ``unresolved``。返回调整过的行数。
    """
    if not added:
        return 0
    settings = (settings or FitSettings()).normalized()
    entities = _collect_entities(records, plain_text)
    by_no = {entity.entity_no: entity for entity in entities}
    groups: dict[tuple[tuple[str, str], float], list[_TextBox]] = {}
    for entity in entities:
        groups.setdefault((entity.container, round(entity.rotation, 1)), []).append(_box(entity))

    segments = _line_segments(records)
    cell_indexes: dict[tuple[tuple[str, str], float], _CellIndex] = {}

    # (实体, 新字高或 None, 上下挪多少)：译文行缩字并挪位置，原文只挪位置。
    moves: list[tuple[_TextEntity, float | None, float]] = []
    fitted = 0
    for added_no, (source_no, below) in added.items():
        line, source = by_no.get(added_no), by_no.get(source_no)
        if line is None or source is None:
            continue
        line_box, source_box = _box(line), _box(source)
        source_height = source.height
        obstacles = []
        group = (line.container, round(line.rotation, 1))
        if group not in cell_indexes:
            boxes = groups[group]
            cell_indexes[group] = _CellIndex(
                segments.get(line.container, ()), line.rotation,
                (min(box.left for box in boxes), max(box.right for box in boxes),
                 min(box.bottom for box in boxes), max(box.top for box in boxes)),
            )
        # 原文在表格格子里：译文行也留在格子里，连 p、g 这类下伸的笔画都不压到格子的横线上。
        cell = cell_indexes[group].cell(source_box)
        descender = _DESCENDER * line.height
        if cell is not None:
            if below and cell.bottom > line_box.bottom - descender:
                obstacles.append(cell.bottom + descender)
            elif not below and cell.top < line_box.top:
                obstacles.append(cell.top)
        for box in groups.get((line.container, round(line.rotation, 1)), []):
            if box.entity_no in {added_no, source_no}:
                continue
            # 和原文在同一行的（左右相邻的文字）不算，那是横向防重叠管的。
            same_line = min(box.top, source_box.top) - max(box.bottom, source_box.bottom)
            if same_line > 0.3 * min(box.top - box.bottom, source_height):
                continue
            if min(box.right, line_box.right) - max(box.left, line_box.left) <= 0.1 * source_height:
                continue
            if below and box.top > line_box.bottom and box.bottom < source_box.bottom:
                obstacles.append(box.top)
            elif not below and box.bottom < line_box.top and box.top > source_box.top:
                obstacles.append(box.bottom)
        if not obstacles:
            continue
        if below:
            available = source_box.bottom - max(obstacles)
        else:
            available = min(obstacles) - source_box.top
        line_height = line.height
        min_gap = _MIN_LINE_GAP * source_height
        if cell is not None and available - min_gap < line_height:
            # 格子里原文另一侧还有空（表格行高往往是字高的两三倍，原文居中）：原文往那边挪，
            # 给译文行腾地方，挪到离格线或那边的文字还留一道空为止。
            if below:
                limit = cell.top - min_gap
                for box in groups[group]:
                    if box.entity_no not in {added_no, source_no} and source_box.top <= box.bottom < limit and \
                            min(box.right, source_box.right) - max(box.left, source_box.left) > 0:
                        limit = box.bottom - min_gap
                room = limit - source_box.top
            else:
                limit = cell.bottom + min_gap
                for box in groups[group]:
                    if box.entity_no not in {added_no, source_no} and limit < box.top <= source_box.bottom and \
                            min(box.right, source_box.right) - max(box.left, source_box.left) > 0:
                        limit = box.top + min_gap
                room = source_box.bottom - limit
            lift = max(0.0, min(room, line_height + min_gap - available))
            if lift:
                available += lift
                shift = lift if below else -lift
                source_box = replace(source_box, bottom=source_box.bottom + shift, top=source_box.top + shift)
                moves.append((source, None, shift))
        if available - min_gap >= line_height:
            new_height = line_height
            fits = True
        else:
            fit = max(available - min_gap, 0.0) / source_height
            tiers = [tier * source_height for tier in settings.tiers() if tier * source_height < line_height]
            tiers = tiers or [min(line_height, settings.min_scale * source_height)]
            new_height = next((height for height in tiers if height <= fit * source_height + 1e-9), tiers[-1])
            fits = new_height <= fit * source_height + 1e-9
        if not fits and unresolved is not None:
            unresolved.append({"source": source.text, "translation": line.text, "entity": added_no})
        current_gap = source_box.bottom - line_box.top if below else line_box.bottom - source_box.top
        gap = min(current_gap, available - new_height) if fits else min_gap
        if below:
            new_top = source_box.bottom - gap
            new_bottom = new_top - new_height
        else:
            new_bottom = source_box.top + gap
            new_top = new_bottom + new_height
        bottom, top = _vertical_span(line.valign, _local(line)[1], line_height)
        new_anchor = {3: new_top, 2: (new_bottom + new_top) / 2}.get(line.valign, new_bottom)
        old_anchor = {3: top, 2: (bottom + top) / 2}.get(line.valign, bottom)
        moves.append((line, new_height, new_anchor - old_anchor))
        fitted += 1

    # 改字高可能多插一条记录，从后往前改，前面实体的位置不受影响。
    for entity, new_height, shift in sorted(moves, key=lambda item: item[0].start, reverse=True):
        _shift_entity(records, entity.start, entity.end, entity.rotation, shift)
        if new_height is not None:
            _write_size(records, entity.start, entity.end, new_height, entity.width_factor)
    return fitted


def _shift_entity(records: list[Any], start: int, end: int, rotation: float, shift: float) -> None:
    """把单行文字沿它自己的「上下」方向移动 ``shift``（两个插入点一起移）。"""
    if not shift:
        return
    angle = math.radians(rotation)
    dx, dy = -shift * math.sin(angle), shift * math.cos(angle)
    for index in range(start + 1, end):
        record = records[index]
        if record.code == 101:
            break
        if record.code in {10, 11}:
            records[index] = replace(record, value=_format_number(_float(record.value) + dx))
        elif record.code in {20, 21}:
            records[index] = replace(record, value=_format_number(_float(record.value) + dy))


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


# ── 表格格子里的文字 ─────────────────────────────────────────
#
# ACAD_TABLE 显示的是一个匿名图块（*T…）：格线是 LINE，每格一段多行文字，宽度（组码 41）
# 就是格子宽度减去边距，CAD 按这个宽度自动换行。译文写进去以后（双语多了一行，英文又比
# 中文长、会折成两三行），格子本身不会跟着变高，字就压到下一行格子里。这里按格子高度
# 把这段多行文字整体缩小（字高，组码 40；双语那行是相对字高，跟着一起缩），档位和别处
# 防重叠一样，到最小比例还放不下的记进报告。

# 格子里文字最多占格子高度的多少（留出上下边距）。
_TABLE_CELL_FILL = 0.92
# 自动换行按估算宽度算，单词不能从中间断开，宁可多算一点。
_WRAP_SLACK = 1.08
_MTEXT_TOKEN_RE = re.compile(
    r"(?P<para>\\P)"
    r"|\\H(?P<height>[0-9.]+)(?P<rel>[xX]?);"
    r"|\\[fFwWcCaAtTqQsSp][^;]*;"
    r"|\\[LlOoKk]"
    r"|\\(?P<escaped>.)"
    r"|(?P<open>\{)|(?P<close>\})"
    r"|(?P<text>[^\\{}]+)",
    re.S,
)


def _table_block_names(records: Sequence[Any]) -> set[str]:
    """ACAD_TABLE 显示用的匿名图块名（大写）。AcDbTable 部分里的组码 2 是长文字分段，不算。"""
    names: set[str] = set()
    for start, end in _entity_ranges(records):
        if records[start].value.strip().upper() != "ACAD_TABLE":
            continue
        for record in records[start + 1:end]:
            if record.code == 100 and record.value.strip() == "AcDbTable":
                break
            if record.code == 2:
                names.add(record.value.strip().upper())
                break
    return names


def _mtext_paragraphs(raw: str, height: float) -> list[tuple[float, str]]:
    """多行文字按 ``\\P`` 分段：``[(这一段的字高倍数, 段落文字)]``。

    字高倍数取这一段里文字用到的最大字高（``\\H0.8x;`` 相对、``\\H120;`` 绝对），
    大括号里设的字高出了括号就恢复。
    """
    paragraphs: list[tuple[float, str]] = []
    stack: list[float] = []
    factor = 1.0
    line_factor = 0.0
    parts: list[str] = []
    for match in _MTEXT_TOKEN_RE.finditer(raw):
        if match.group("para") is not None:
            paragraphs.append((line_factor or factor, "".join(parts)))
            parts, line_factor = [], 0.0
        elif match.group("height") is not None:
            value = _float(match.group("height"), 1.0)
            if value > 0:
                factor = factor * value if match.group("rel") else (value / height if height > 0 else 1.0)
        elif match.group("open") is not None:
            stack.append(factor)
        elif match.group("close") is not None:
            factor = stack.pop() if stack else factor
        else:
            text = match.group("text") if match.group("text") is not None else match.group("escaped")
            if text is not None:
                parts.append(text)
                if text.strip():
                    line_factor = max(line_factor, factor)
    paragraphs.append((line_factor or factor, "".join(parts)))
    return paragraphs


def _cell_content_height(
    paragraphs: Sequence[tuple[float, str]], height: float, ref_width: float, font_class: str,
) -> float:
    """按格子宽度自动换行后，这段多行文字从第一行顶到最后一行底大约多高。"""
    lines: list[float] = []
    for factor, text in paragraphs:
        line_height = height * factor
        width = estimate_text_width(text, line_height, 1.0, font_class) * _WRAP_SLACK
        count = max(1, math.ceil(width / ref_width)) if ref_width > 0 and width > 0 else 1
        lines.extend([line_height] * count)
    if not lines:
        return 0.0
    return lines[0] + _MTEXT_LINE_SPACING * sum(lines[1:])


def fit_table_cells(
    records: list[Any],
    originals: Mapping[int, str],
    plain_text: Callable[[str], str],
    settings: FitSettings | None = None,
    unresolved: list[dict[str, str]] | None = None,
) -> int:
    """Shrink translated MTEXT in table cells so it stays inside its row.

    ``originals`` maps the ordinal of each translated MTEXT entity (its position
    among ``0`` records) to its source text; only the ones inside a table's
    display block are looked at. 格子上下边界取这段文字上方、下方最近的横向格线。
    原文本来就放不下的格子按原文的高度算，不为译文多缩。返回缩小了几格。
    """
    if not originals:
        return 0
    settings = (settings or FitSettings()).normalized()
    blocks = _table_block_names(records)
    if not blocks:
        return 0
    fonts = _style_fonts(records)
    rules: dict[str, list[tuple[float, float, float]]] = {}
    candidates: list[tuple[str, int, int, int, dict[int, str], str]] = []
    block = ""
    for entity_no, (start, end) in enumerate(_entity_ranges(records)):
        entity_type = records[start].value.strip().upper()
        if entity_type == "BLOCK":
            block = next((r.value.strip().upper() for r in records[start + 1:end] if r.code == 2), "")
            continue
        if entity_type == "ENDBLK":
            block = ""
            continue
        if block not in blocks:
            continue
        fields: dict[int, str] = {}
        raw: list[str] = []
        for record in records[start + 1:end]:
            if record.code == 101:
                break
            if entity_type == "MTEXT" and record.code in {1, 3}:
                raw.append(record.value)
                continue
            fields.setdefault(record.code, record.value)
        if entity_type == "LINE":
            y1, y2 = _float(fields.get(20)), _float(fields.get(21))
            x1, x2 = _float(fields.get(10)), _float(fields.get(11))
            if abs(y1 - y2) <= 1e-6 * max(1.0, abs(y1)) and abs(x2 - x1) > 0:
                rules.setdefault(block, []).append((y1, min(x1, x2), max(x1, x2)))
        elif entity_type == "MTEXT" and entity_no in originals:
            candidates.append((block, entity_no, start, end, fields, "".join(raw)))

    edits: list[tuple[int, int, float]] = []
    for block, entity_no, start, end, fields, raw in candidates:
        if abs(_float(fields.get(50))) > 1e-6 or abs(_float(fields.get(21))) > 1e-6:
            continue  # 竖排、斜放的格子不管
        height = _float(fields.get(40))
        if height <= 0:
            continue
        x, y = _float(fields.get(10)), _float(fields.get(20))
        span = max(height * 0.01, 1e-6)
        crossing = [rule for rule, left, right in rules.get(block, []) if left - span <= x <= right + span]
        above = [rule for rule in crossing if rule > y + span]
        below = [rule for rule in crossing if rule < y - span]
        if not above or not below:
            continue
        available = (min(above) - max(below)) * _TABLE_CELL_FILL
        ref_width = _float(fields.get(41))
        font_class = fonts.get(str(fields.get(7, "STANDARD")).strip().upper(), "shx")
        paragraphs = _mtext_paragraphs(raw, height)
        original = originals[entity_no]
        limit = max(available, _cell_content_height([(1.0, original)], height, ref_width, font_class))
        if _cell_content_height(paragraphs, height, ref_width, font_class) <= limit:
            continue
        tiers = settings.tiers()
        scale = next(
            (tier for tier in tiers if _cell_content_height(paragraphs, height * tier, ref_width, font_class) <= limit),
            None,
        )
        if scale is None:
            scale = tiers[-1]
            if unresolved is not None and not any(item.get("entity") == entity_no for item in unresolved):
                translation = " ".join(
                    text.strip() for _factor, text in paragraphs if text.strip() and text.strip() != original.strip()
                )
                unresolved.append({"source": original, "translation": translation or plain_text(raw), "entity": entity_no})
        edits.append((start, end, height * scale))

    for start, end, new_height in sorted(edits, reverse=True):
        for index in range(start + 1, end):
            if records[index].code == 101:
                break
            if records[index].code == 40:
                records[index] = replace(records[index], value=_format_number(new_height))
                break
    return len(edits)
