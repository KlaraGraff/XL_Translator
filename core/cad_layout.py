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
# 引线肩线离块属性最多这么远（按字高计）才算这段文字的肩线。
_SHOULDER_GAP = 1.0
# 引线标注折成两行时：上方（下方）没有别的标注，最多往外多占这么多（按原字高计）；两行之间的空
# （按新字高计）；离上方那行标注至少留的空（按原字高计）。
_LEADER_WRAP_REACH = 1.0
_LEADER_WRAP_GAP = 0.35
_LEADER_WRAP_CLEAR = 0.1


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


def _arc_chords(cx: float, cy: float, radius: float, start: float, sweep: float) -> list[_Segment]:
    """圆弧按每段不超过 22.5° 折成弦（``start``、``sweep`` 为弧度，逆时针为正）。"""
    if radius <= 0 or not sweep:
        return []
    steps = max(2, math.ceil(abs(sweep) / (math.pi / 8)))
    points = [(cx + radius * math.cos(start + sweep * step / steps), cy + radius * math.sin(start + sweep * step / steps))
              for step in range(steps + 1)]
    return [(x1, y1, x2, y2) for (x1, y1), (x2, y2) in zip(points, points[1:], strict=False)]


def _bulge_chords(x1: float, y1: float, x2: float, y2: float, bulge: float) -> list[_Segment]:
    """多段线上凸度为 ``bulge`` 的一段圆弧折成弦。"""
    chord = math.hypot(x2 - x1, y2 - y1)
    if chord <= 0:
        return []
    # 圆心在弦的左侧（凸度为正时）偏 chord·(1-b²)/(4b) 处；圆心角 4·atan(b)。
    offset = chord * (1 - bulge * bulge) / (4 * bulge)
    cx = (x1 + x2) / 2 - (y2 - y1) / chord * offset
    cy = (y1 + y2) / 2 + (x2 - x1) / chord * offset
    return _arc_chords(cx, cy, math.hypot(x1 - cx, y1 - cy), math.atan2(y1 - cy, x1 - cx), 4 * math.atan(bulge))


def _line_segments(records: Sequence[Any]) -> dict[tuple[str, str], list[_Segment]]:
    """每个容器（模型空间、图纸空间、各个图块）里的直线段，换算到该容器的坐标。"""
    return _geometry(records)[0]


def _geometry(
    records: Sequence[Any],
) -> tuple[dict[tuple[str, str], list[_Segment]], dict[tuple[str, str], list[_Segment]]]:
    """每个容器（模型空间、图纸空间、各个图块）里的 ``(直线段, 圆弧折成的弦)``，换算到该容器的坐标。

    LINE、多段线（LWPOLYLINE、旧式 POLYLINE）的直边都算；插入的图块（图框、图签通常是
    一个图块）里的线按插入点、比例、旋转换算出来，算进插入它的容器，图块里再插图块的
    一层层往下找。格子、图框、房间的墙都是这样画出来的。ARC、CIRCLE 和多段线的圆弧段
    （门的开启线、轴号圈）折成弦单独放：它们不当格线，只用来挡住伸过去的译文。

    图纸空间（布局）里的视口显示着模型空间的墙和轴线：这些线按视口比例换算到图纸上、
    裁到视口框以内，也算进视口所在的容器（见 :func:`_viewport_segments`）。写在布局里的
    引线标注以前看不到墙，译文一路压过去。
    """
    layers = _linetypes(records)
    own: dict[tuple[str, str], list[_Segment]] = {}
    own_curves: dict[tuple[str, str], list[_Segment]] = {}
    inserts: dict[tuple[str, str], list[tuple[str, float, float, float, float, float]]] = {}
    bases: dict[str, tuple[float, float]] = {}
    viewports: list[tuple[tuple[str, str], _Viewport]] = []
    block = ""

    def add_polyline(container: tuple[str, str], points: list[tuple[float, float, float]], closed: bool) -> None:
        # 每个点带着从它出发那条边的凸度：不为 0 是圆弧。
        edges = list(zip(points, points[1:] + (points[:1] if closed else []), strict=False))
        for (x1, y1, bulge), (x2, y2, _next) in edges:
            if (x1, y1) == (x2, y2):
                continue
            if abs(bulge) <= 1e-9:
                own.setdefault(container, []).append((x1, y1, x2, y2))
            else:
                own_curves.setdefault(container, []).extend(_bulge_chords(x1, y1, x2, y2, bulge))

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
        if entity_type not in {"LINE", "LWPOLYLINE", "POLYLINE", "INSERT", "ARC", "CIRCLE", "VIEWPORT"}:
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
        if entity_type == "VIEWPORT":
            viewport = _Viewport.parse(fields)
            if viewport is not None:
                viewports.append((container, viewport))
            continue
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
        elif entity_type in {"ARC", "CIRCLE"}:
            cx, cy, radius = _float(fields.get(10)), _float(fields.get(20)), _float(fields.get(40))
            if entity_type == "ARC":
                start = math.radians(_float(fields.get(50)))
                sweep = (math.radians(_float(fields.get(51))) - start) % (2 * math.pi) or 2 * math.pi
            else:
                start, sweep = 0.0, 2 * math.pi
            chords = _arc_chords(cx, cy, radius, start, sweep)
            if _float(fields.get(230), 1.0) < 0:
                # 拉伸方向朝 -Z（镜像过的图块里常见）：物体坐标系的 x 轴反向。
                chords = [(-x1, y1, -x2, y2) for x1, y1, x2, y2 in chords]
            own_curves.setdefault(container, []).extend(chords)
        elif entity_type == "LWPOLYLINE":
            add_polyline(container, points, bool(flags & 1))
        elif not flags & _POLYLINE_NOT_FLAT:
            polyline = (container, bool(flags & 1), [])

    def resolver(source: dict[tuple[str, str], list[_Segment]]) -> dict[tuple[str, str], list[_Segment]]:
        resolved: dict[tuple[str, str], list[_Segment]] = {}
        in_progress: set[tuple[str, str]] = set()

        def resolve(container: tuple[str, str]) -> list[_Segment]:
            if container in resolved:
                return resolved[container]
            if container in in_progress:
                # 图块互相插入：不再往下找。
                return []
            in_progress.add(container)
            result = list(source.get(container, []))
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

        return {container: segments for container in set(source) | set(inserts) if (segments := resolve(container))}

    segments, curves = resolver(own), resolver(own_curves)
    model = ("SPACE", "0")
    for container, viewport in viewports:
        for resolved in (segments, curves):
            shown = _viewport_segments(viewport, resolved.get(model, ()))
            if shown:
                resolved[container] = [*resolved.get(container, []), *shown][:_MAX_SEGMENTS]
    return segments, curves


@dataclass(frozen=True)
class _Viewport:
    """图纸空间里的一个视口：纸面上的框，和它显示的那块模型空间（只认俯视的）。"""

    center_x: float
    center_y: float
    half_width: float
    half_height: float
    model_x: float
    model_y: float
    scale: float
    twist: float

    @classmethod
    def parse(cls, fields: Mapping[int, str]) -> _Viewport | None:
        width, height, view_height = _float(fields.get(40)), _float(fields.get(41)), _float(fields.get(45))
        # 69=1 是布局本身的整页视图，不显示模型；68=0 是关掉的视口。
        if _int(fields.get(69), 0) == 1 or _int(fields.get(68), 1) == 0:
            return None
        if width <= 0 or height <= 0 or view_height <= 0:
            return None
        # 斜着看（三维视图）的视口，模型线投到纸上不是这样换算，不认。
        direction = (_float(fields.get(16)), _float(fields.get(26)), _float(fields.get(36), 1.0))
        if abs(direction[0]) > 1e-9 or abs(direction[1]) > 1e-9 or direction[2] <= 0:
            return None
        return cls(
            _float(fields.get(10)), _float(fields.get(20)), width / 2, height / 2,
            _float(fields.get(17)) + _float(fields.get(12)), _float(fields.get(27)) + _float(fields.get(22)),
            height / view_height, math.radians(_float(fields.get(51))),
        )

    def place(self, x: float, y: float) -> tuple[float, float]:
        dx, dy = (x - self.model_x) * self.scale, (y - self.model_y) * self.scale
        # 视图扭转角是把模型绕视图中心转了这么多再显示。
        cos, sin = math.cos(self.twist), math.sin(self.twist)
        return self.center_x + dx * cos - dy * sin, self.center_y + dx * sin + dy * cos


def _clip(segment: _Segment, left: float, bottom: float, right: float, top: float) -> _Segment | None:
    """线段裁到矩形以内（Liang–Barsky）；完全在外面时返回 None。"""
    x1, y1, x2, y2 = segment
    dx, dy = x2 - x1, y2 - y1
    low, high = 0.0, 1.0
    for p, q in ((-dx, x1 - left), (dx, right - x1), (-dy, y1 - bottom), (dy, top - y1)):
        if abs(p) < 1e-12:
            if q < 0:
                return None
            continue
        t = q / p
        if p < 0:
            low = max(low, t)
        else:
            high = min(high, t)
        if low > high:
            return None
    return x1 + low * dx, y1 + low * dy, x1 + high * dx, y1 + high * dy


def _viewport_segments(viewport: _Viewport, model: Sequence[_Segment]) -> list[_Segment]:
    """模型空间的线经过这个视口显示在纸面上的那几段（纸面坐标）。"""
    left, right = viewport.center_x - viewport.half_width, viewport.center_x + viewport.half_width
    bottom, top = viewport.center_y - viewport.half_height, viewport.center_y + viewport.half_height
    out: list[_Segment] = []
    for x1, y1, x2, y2 in model:
        clipped = _clip((*viewport.place(x1, y1), *viewport.place(x2, y2)), left, bottom, right, top)
        if clipped is not None and (clipped[0], clipped[1]) != (clipped[2], clipped[3]):
            out.append(clipped)
    return out


class _ObstacleIndex:
    """一个容器、一个文字方向上的全部实线和弧（斜线、箭头、门的开启弧都在内），按纵向分条。

    译文比原文伸出去的那一段要是会横穿原文没碰到的线，就停在线前面。
    """

    _BUCKETS = 512

    def __init__(self, segments: Sequence[_Segment], rotation: float, bottom: float, top: float) -> None:
        self.low = bottom
        self.size = max((top - bottom) / self._BUCKETS, 1e-9)
        self.buckets: dict[int, list[tuple[float, float, float, float]]] = {}
        for x1, y1, x2, y2 in segments:
            a, b = _to_local(x1, y1, rotation), _to_local(x2, y2, rotation)
            first = max(math.floor((min(a[1], b[1]) - self.low) / self.size), 0)
            last = min(math.floor((max(a[1], b[1]) - self.low) / self.size), self._BUCKETS)
            for bucket in range(first, last + 1):
                self.buckets.setdefault(bucket, []).append((*a, *b))

    def limits(self, band: tuple[float, float], old: tuple[float, float],
               new: tuple[float, float]) -> tuple[float, float]:
        """``(右边界, 左边界)``：在字身 ``band`` 这个高度范围里，原文（``old``）两侧、
        译文（``new``）伸过去会碰到的最近的线。原文本来就碰着的线不算。"""
        right, left = math.inf, -math.inf
        first = max(math.floor((band[0] - self.low) / self.size), 0)
        last = min(math.floor((band[1] - self.low) / self.size), self._BUCKETS)
        seen: set[tuple[float, float, float, float]] = set()
        for bucket in range(first, last + 1):
            for segment in self.buckets.get(bucket, ()):
                if segment in seen:
                    continue
                seen.add(segment)
                ax, ay, bx, by = segment
                if max(ay, by) < band[0] or min(ay, by) > band[1]:
                    continue
                # 线落在这一高度范围里的那一截的横向范围。
                xs = []
                for y in (max(min(ay, by), band[0]), min(max(ay, by), band[1])):
                    xs.append(ax if ay == by else ax + (bx - ax) * (y - ay) / (by - ay))
                if ay == by:
                    xs = [ax, bx]
                low, high = min(xs), max(xs)
                if high >= old[0] and low <= old[1]:
                    continue
                if low >= old[1] and low < new[1]:
                    right = min(right, low)
                elif high <= old[0] and high > new[0]:
                    left = max(left, high)
        return right, left


def _shoulder(
    index: _ObstacleIndex, entity: _TextEntity, x: float, bottom: float, top: float, old: tuple[float, float],
) -> tuple[float, float, float] | None:
    """块属性下方（底对齐、上标）或上方（顶对齐、下标）紧挨着的引线肩线 ``(左端, 右端, 高度位置)``。

    肩线是一条横线，盖住原文，左对齐的文字插入点在它左端、右对齐的在右端。找不到返回 None。
    """
    height = entity.height
    if entity.halign not in {0, 2}:
        return None
    reach = _SHOULDER_GAP * height
    band = (bottom - reach, bottom + 0.05 * height) if entity.valign in {0, 1} else (top - 0.05 * height, top + reach)
    best: tuple[float, float, float, float] | None = None
    first = max(math.floor((band[0] - index.low) / index.size), 0)
    last = min(math.floor((band[1] - index.low) / index.size), index._BUCKETS)
    for bucket in range(first, last + 1):
        for ax, ay, bx, by in index.buckets.get(bucket, ()):
            if abs(ay - by) > 1e-6 * max(1.0, abs(ax - bx)) or not band[0] <= ay <= band[1]:
                continue
            low, high = min(ax, bx), max(ax, bx)
            if low > old[0] + 0.2 * height or high < old[1] - 0.2 * height:
                continue
            end = high if entity.halign == 2 else low
            if abs(x - end) > 2 * height:
                continue
            distance = abs(ay - (bottom if entity.valign in {0, 1} else top))
            if best is None or distance < best[0]:
                best = (distance, low, high, ay)
    return None if best is None else best[1:]


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
        # 按宽度（组码 41）在词间换行：比宽度还长的单词 CAD 不断开，照样伸出去。
        paragraphs = [(1.0, line) for line in entity.text.split("\n")] or [(1.0, "")]
        width, total_height = _mtext_extent(paragraphs, entity.height, entity.ref_width, entity.font_class)
    else:
        width = estimate_text_width(entity.text, entity.height, entity.width_factor, entity.font_class)
        total_height = entity.height
    left, right = _horizontal_span(entity.halign, x, width)
    bottom, top = _vertical_span(entity.valign, y, total_height)
    return _TextBox(entity.entity_no, left, right, bottom, top)


def scale_parts(scale: float) -> tuple[float, float]:
    """``(字高缩放, 字宽缩放)``，两者相乘等于 ``scale``：先收字宽，收到 0.7 再降字高。

    PDF 原位替换写回译文时用同一套拆法（core/pdf_vector/writeback.py）。"""
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
    new_handle: Callable[[], str | None] | None = None,
    wrapped: list[dict[str, str]] | None = None,
) -> int:
    """Shrink translated TEXT/ATTRIB entities that would overlap a neighbour or leave their frame.

    ``originals`` maps the ordinal of each translated entity (its position
    among ``0`` records, stable across MTEXT write-back) to its source text.
    Edits ``records`` in place and returns how many entities were shrunk.
    The frame is the cell of solid lines around the source text (see
    :meth:`_CellIndex.cell`). Entities that still overlap at the minimum scale are appended to
    ``unresolved`` as ``{"source", "translation"}``.

    With ``new_handle`` (hands out entity handles), a TEXT inside a cell that does not fit even at
    the minimum scale is split at a space into two lines stacked in its place when they fit there
    (see :func:`_wrap_plan`); the second line is a new TEXT right after the first. Each split is
    appended to ``wrapped`` and is not counted in the return value.
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
    segments, curves = _geometry(records)
    cell_indexes: dict[tuple[tuple[str, str], float], _CellIndex] = {}
    obstacle_indexes: dict[tuple[tuple[str, str], float], _ObstacleIndex] = {}
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
    splits: list[_WrapPlan] = []
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
        # 格子以外的线也挡着：入口箭头、门的开启弧、斜线，原文没碰到，译文伸过去就会横穿。
        if group not in obstacle_indexes:
            obstacle_indexes[group] = _ObstacleIndex(
                [*segments.get(entity.container, ()), *curves.get(entity.container, ())], entity.rotation,
                # 上下各多留一个字高：最低、最高那行文字的引线肩线也要找得到（见 _shoulder）。
                bottoms[0] - tallest, max(box.top for box in boxes) + tallest,
            )
        line_right, line_left = obstacle_indexes[group].limits(
            (bottom + 0.15 * height, top - 0.15 * height), (old_left, old_right), (new_left, new_right),
        )
        right_limit, left_limit = min(right_limit, line_right), max(left_limit, line_left)
        shoulder = None
        if entity.entity_type == "ATTRIB":
            # 引线标注（块属性写在引线的水平肩线上方或下方，一端对齐肩线端点）：译文不越过肩线两端。
            shoulder = _shoulder(obstacle_indexes[group], entity, x, bottom, top, (old_left, old_right))
            if shoulder is not None:
                if new_left < shoulder[0]:
                    left_limit = max(left_limit, shoulder[0])
                if new_right > shoulder[1]:
                    right_limit = min(right_limit, shoulder[1])
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
        if not fits and new_handle is not None and shoulder is not None:
            # 引线上的长说明（maple 2026-10-31 选「放不下就折两行」）：贴着肩线的那一行不动，第二行
            # 摞在离开肩线的一侧，两行一起收进到上（下）方那行标注为止的空当，不挪动别的标注。
            # 第二行是同一个图块引用里紧跟着的一个属性副本。
            plan = _leader_wrap_plan(entity, records, _vertical_neighbours(indexes[group], entity, bottom, top),
                                     room, calibration, settings, (new_left, new_right))
            if plan is not None:
                splits.append(plan)
                if wrapped is not None:
                    wrapped.append({"source": plain_text(original), "translation": entity.text, "entity": entity.entity_no})
                continue
        if not fits and new_handle is not None and entity.entity_type == "TEXT" and cell is not None:
            # 格子里最小比例一行仍放不下（窄房间的长房间名、图签格子里的长工程名）：在空格处分成两行，
            # 两行叠在原来那一行的位置上，上下不碰别的文字和格线时就这么放。不在格子里的（「标签：
            # 数值」这类挨着摆的）不折，照旧列入复核。
            plan = _wrap_plan(entity, records, boxes[lo:hi] + _vertical_neighbours(indexes[group], entity, bottom, top),
                              cell, room, calibration, settings)
            if plan is not None:
                splits.append(plan)
                if wrapped is not None:
                    wrapped.append({"source": plain_text(original), "translation": entity.text, "entity": entity.entity_no})
                continue
        if not fits and unresolved is not None and not any(item.get("entity") == entity.entity_no for item in unresolved):
            unresolved.append({"source": plain_text(original), "translation": entity.text, "entity": entity.entity_no})
        height_scale, width_scale = scale_parts(scale)
        edits.append((entity, height * height_scale, width_factor * width_scale))

    # 从后往前改：改字高、插入第二行都会让后面的记录后移。
    changes = [(entity.start, entity, height, width, None) for entity, height, width in edits]
    changes += [(plan.entity.start, plan.entity, plan.height, plan.width_factor, plan) for plan in splits]
    for _start, entity, new_height, new_width_factor, plan in sorted(changes, key=lambda item: item[0], reverse=True):
        _write_size(records, entity.start, entity.end, new_height, new_width_factor)
        if plan is not None:
            _apply_wrap(records, plan, new_handle)
    return len(edits)


# 折成两行时，两行之间的空（按新字高计）。
_WRAP_GAP = 0.5


@dataclass(frozen=True)
class _WrapPlan:
    entity: _TextEntity
    first: str  # 第一行、第二行在 DXF 里的原始写法（转义照旧）
    second: str
    height: float
    width_factor: float
    first_shift: float  # 两行各自沿文字的上下方向挪多少
    second_shift: float


def _vertical_neighbours(index: tuple[list[float], list[_TextBox], float], entity: _TextEntity,
                         bottom: float, top: float) -> list[_TextBox]:
    """上下各一行高范围内的文字：折成两行后会占到原来那一行的上下方。"""
    bottoms, boxes, tallest = index
    height = top - bottom
    lo = bisect.bisect_left(bottoms, bottom - height - tallest)
    hi = bisect.bisect_right(bottoms, top + height)
    return [box for box in boxes[lo:hi] if box.entity_no != entity.entity_no]


def _text_record(records: Sequence[Any], start: int, end: int) -> int | None:
    for index in range(start + 1, end):
        if records[index].code == 101:
            break
        if records[index].code == 1:
            return index
    return None


def _wrap_plan(
    entity: _TextEntity,
    records: Sequence[Any],
    neighbours: Sequence[_TextBox],
    cell: _TextBox,
    room: float,
    calibration: float,
    settings: FitSettings,
) -> _WrapPlan | None:
    """一行放不下的译文在哪个空格处分成两行、缩到多大：两行都要放进 ``room`` 宽，
    叠起来的高度不碰原来上下方的文字和格线。放不下就是 None（照旧列入复核）。"""
    split = _split_words(entity, records)
    if split is None:
        return None
    raw, raw_spaces, words = split
    height, width_factor = entity.height, entity.width_factor
    best: tuple[float, int] | None = None
    for cut in range(1, len(words)):
        first, second = " ".join(words[:cut]).strip(), " ".join(words[cut:]).strip()
        if not first or not second:
            continue
        widest = max(estimate_text_width(part, height, width_factor, entity.font_class) for part in (first, second))
        if best is None or widest < best[0]:
            best = (widest, cut)
    if best is None:
        return None
    widest, cut = best
    x, y = _local(entity)
    bottom, top = _vertical_span(entity.valign, y, height)
    middle = (bottom + top) / 2
    old_anchor = {3: top, 2: middle}.get(entity.valign, bottom)
    fit = max(room, 0.0) / (widest * calibration)
    for tier in settings.tiers():
        if tier > fit + 1e-9:
            continue
        height_scale, width_scale = scale_parts(tier)
        new_height = height * height_scale
        offset = (1 + _WRAP_GAP) * new_height / 2
        left, right = _horizontal_span(entity.halign, x, widest * tier)
        # 两行先以原来那一行为中心上下摊开；上方或下方有东西（双语时紧挨着的原文）时，
        # 改为顶对齐往下排、底对齐往上排。
        for centre in (middle, top - offset - new_height / 2, bottom + offset + new_height / 2):
            span_bottom = centre - offset - new_height / 2 - _DESCENDER * new_height
            span_top = centre + offset + new_height / 2
            if span_bottom < cell.bottom or span_top > cell.top:
                continue
            if any(
                box.top > span_bottom and box.bottom < span_top and box.right > left and box.left < right
                # 和原来那一行同一行的左右邻居由横向的边界管，这里只看上下方。
                and min(box.top, top) - max(box.bottom, bottom) < 0.3 * min(box.top - box.bottom, top - bottom)
                for box in neighbours
            ):
                continue

            def shift(line_centre: float, new_height: float = new_height) -> float:
                box_bottom = line_centre - new_height / 2
                anchor = {3: box_bottom + new_height, 2: line_centre}.get(entity.valign, box_bottom)
                return anchor - old_anchor

            return _WrapPlan(
                entity, raw[:raw_spaces[cut - 1]], raw[raw_spaces[cut - 1] + 1:],
                new_height, width_factor * width_scale, shift(centre + offset), shift(centre - offset),
            )
    return None


def _split_words(entity: _TextEntity, records: Sequence[Any]) -> tuple[str, list[int], list[str]] | None:
    """``(原始写法, 原始写法里空格的位置, 显示文字按空格分开的词)``；没法按空格分时返回 None。"""
    index = _text_record(records, entity.start, entity.end)
    if index is None:
        return None
    raw = records[index].value
    raw_spaces = [position for position, char in enumerate(raw) if char == " "]
    words = entity.text.split(" ")
    # 转义（%%c、\U+00E9）里没有空格：原始写法和显示文字的空格一一对应才分。
    if len(words) < 2 or len(raw_spaces) != len(words) - 1:
        return None
    return raw, raw_spaces, words


def _leader_wrap_plan(
    entity: _TextEntity,
    records: Sequence[Any],
    neighbours: Sequence[_TextBox],
    room: float,
    calibration: float,
    settings: FitSettings,
    span: tuple[float, float],
) -> _WrapPlan | None:
    """引线上的长说明分成两行：贴着肩线的那一行原地不动（上标底对齐、下标顶对齐），另一行摞在
    外侧。字高取空当放得下的最大值（不超过原字高），字宽再按 ``room`` 收；整体缩放（字高 × 字宽）
    不小于最小比例。放不下返回 None（照旧列入复核）。"""
    split = _split_words(entity, records)
    if split is None:
        return None
    raw, raw_spaces, words = split
    height, width_factor = entity.height, entity.width_factor
    best: tuple[float, int] | None = None
    for cut in range(1, len(words)):
        first, second = " ".join(words[:cut]).strip(), " ".join(words[cut:]).strip()
        if not first or not second:
            continue
        widest = max(estimate_text_width(part, height, width_factor, entity.font_class) for part in (first, second))
        if best is None or widest < best[0]:
            best = (widest, cut)
    if best is None:
        return None
    widest, cut = best
    x, y = _local(entity)
    bottom, top = _vertical_span(entity.valign, y, height)
    upward = entity.valign in {0, 1}
    # 外侧到哪为止：横向和这段译文重叠、在外侧的最近一行文字。
    limit = top + _LEADER_WRAP_REACH * height if upward else bottom - _LEADER_WRAP_REACH * height
    for box in neighbours:
        if box.right <= span[0] or box.left >= span[1]:
            continue
        if upward and box.bottom >= top - 0.3 * height:
            limit = min(limit, box.bottom - _LEADER_WRAP_CLEAR * height)
        elif not upward and box.top <= bottom + 0.3 * height:
            limit = max(limit, box.top + _LEADER_WRAP_CLEAR * height)
    slot = limit - bottom if upward else top - limit
    new_height = min(height, slot / (2 + _LEADER_WRAP_GAP))
    if new_height <= 0:
        return None
    height_scale = new_height / height
    needed = widest * height_scale * calibration
    width_scale = min(1.0, max(room, 0.0) / needed) if needed > 0 else 1.0
    if height_scale * width_scale < settings.min_scale - 1e-9:
        return None
    pitch = (1 + _LEADER_WRAP_GAP) * new_height
    first, second = raw[:raw_spaces[cut - 1]], raw[raw_spaces[cut - 1] + 1:]
    # 第一行在上：上标时第一行往上挪一行，下标时第二行往下挪一行。
    return _WrapPlan(
        entity, first, second, new_height, width_factor * width_scale,
        pitch if upward else 0.0, 0.0 if upward else -pitch,
    )


def _apply_wrap(records: list[Any], plan: _WrapPlan, new_handle: Callable[[], str | None] | None) -> None:
    """第一行写回原实体，第二行是紧跟在后面的一个副本（新句柄，不带原实体的反应器和扩展数据，
    双语新增行的标记照带：再翻一次时两行一起删掉）。

    块属性（引线标注）不在块参照里加第二个同名属性：动态块重新求值时会按标记把属性摆回
    定义的位置，两行就乱了（反馈 #23，maple 在 AutoCAD 里双击后看到）。原属性留在原位放
    贴着肩线的那一行，另一行写成块参照后面一行普通文字（见 :func:`_apply_attribute_wrap`）。"""
    from core.cad_writeback import MARK_ADDED, _strip_owned_extras, marker_records, read_marker

    entity = plan.entity
    if entity.entity_type == "ATTRIB":
        _apply_attribute_wrap(records, plan, new_handle)
        return
    start = entity.start
    end = next((index for index in range(start + 1, len(records)) if records[index].code == 0), len(records))
    index = _text_record(records, start, end)
    if index is None:
        return
    marked = read_marker(records[start + 1:end])
    copy = [replace(records[start]), *_strip_owned_extras(list(records[start + 1:end]))]
    copy_index = next(position for position, record in enumerate(copy) if record.code == 1)
    copy[copy_index] = replace(copy[copy_index], value=plan.second)
    handle = new_handle() if new_handle is not None else None
    copy = [record if record.code != 5 else replace(record, value=handle) for record in copy if record.code != 5 or handle]
    if marked is not None and marked[0] == MARK_ADDED:
        copy += marker_records(records[start], MARK_ADDED)
    _shift_entity(copy, 0, len(copy), entity.rotation, plan.second_shift)
    records[index] = replace(records[index], value=plan.first)
    _shift_entity(records, start, end, entity.rotation, plan.first_shift)
    records[end:end] = copy


def _apply_attribute_wrap(records: list[Any], plan: _WrapPlan, new_handle: Callable[[], str | None] | None) -> None:
    from core.cad_writeback import _strip_owned_extras, attribute_as_text

    entity = plan.entity
    start = entity.start
    end = next((index for index in range(start + 1, len(records)) if records[index].code == 0), len(records))
    index = _text_record(records, start, end)
    if index is None:
        return
    # 原位（挪动 0）的那一行留给属性，另一行写成普通文字。
    if plan.first_shift == 0:
        kept, kept_shift, loose, loose_shift = plan.first, plan.first_shift, plan.second, plan.second_shift
    else:
        kept, kept_shift, loose, loose_shift = plan.second, plan.second_shift, plan.first, plan.first_shift
    # 块参照（属性前面最近的 INSERT）的属主就是这行文字所在的空间；文字放在它的 SEQEND 后面。
    insert = next((i for i in range(start - 1, -1, -1)
                   if records[i].code == 0 and records[i].value.strip().upper() in {"INSERT", "MINSERT"}), None)
    owner = None
    if insert is not None:
        owner = next((r.value for r in records[insert + 1:start] if r.code == 330), None)
    after = end
    while after < len(records):
        if records[after].code == 0 and records[after].value.strip().upper() == "SEQEND":
            after = next((i for i in range(after + 1, len(records)) if records[i].code == 0), len(records))
            break
        if records[after].code == 0 and records[after].value.strip().upper() != "ATTRIB":
            break
        after += 1
    copy = _strip_owned_extras(list(records[start:end]))
    copy = attribute_as_text(copy, text=loose, owner=owner)
    handle = new_handle() if new_handle is not None else None
    copy = [record if record.code != 5 else replace(record, value=handle) for record in copy if record.code != 5 or handle]
    _write_size(copy, 0, len(copy), plan.height, plan.width_factor)
    _shift_entity(copy, 0, len(copy), entity.rotation, loose_shift)
    records[index] = replace(records[index], value=kept)
    _shift_entity(records, start, end, entity.rotation, kept_shift)
    records[after:after] = copy


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
_DESCENDER_CHARS = frozenset("gjpqyçÇ,;()[]{}|_")


def _has_descender(text: str) -> bool:
    return any(char in _DESCENDER_CHARS for char in text)


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
    # 每段文字现在的外框：挪过的随时更新，后面的行按挪过之后的位置躲。
    current = {entity.entity_no: _box(entity) for entity in entities}
    groups: dict[tuple[tuple[str, str], float], list[int]] = {}
    for entity in entities:
        groups.setdefault((entity.container, round(entity.rotation, 1)), []).append(entity.entity_no)

    segments = _line_segments(records)
    cell_indexes: dict[tuple[tuple[str, str], float], _CellIndex] = {}

    # (实体, 新字高或 None, 上下挪多少)：译文行缩字并挪位置，原文只挪位置。
    moves: list[tuple[_TextEntity, float | None, float]] = []
    fitted = 0
    # 从上往下排（译文在上时从下往上）：一行挪完，下一行按它的新位置让开。
    order = sorted(
        (item for item in added.items() if item[0] in by_no and item[1][0] in by_no),
        key=lambda item: -current[item[1][0]].top if item[1][1] else current[item[1][0]].bottom,
    )
    for added_no, (source_no, below) in order:
        line, source = by_no[added_no], by_no[source_no]
        line_box, source_box = current[added_no], current[source_no]
        source_height = source.height
        obstacles = []
        group = (line.container, round(line.rotation, 1))
        if group not in cell_indexes:
            boxes = [current[number] for number in groups[group]]
            cell_indexes[group] = _CellIndex(
                segments.get(line.container, ()), line.rotation,
                (min(box.left for box in boxes), max(box.right for box in boxes),
                 min(box.bottom for box in boxes), max(box.top for box in boxes)),
            )
        # 原文在表格格子里：译文行也留在格子里，连 p、g 这类下伸的笔画都不压到格子的横线上。
        cell = cell_indexes[group].cell(source_box)
        # 译文行在下方、又有 p、g 这类往下伸的字母时，它下面要多留下伸的深度（随字高缩放）。
        drop = _DESCENDER if below and _has_descender(line.text) else 0.0
        descender = drop * line.height
        if cell is not None:
            if below and cell.bottom > line_box.bottom - descender:
                obstacles.append(cell.bottom)
            elif not below and cell.top < line_box.top:
                obstacles.append(cell.top)
        for box in (current[number] for number in groups[group]):
            if box.entity_no in {added_no, source_no}:
                continue
            # 和原文在同一行的（左右相邻的文字）不算，那是横向防重叠管的。
            same_line = min(box.top, source_box.top) - max(box.bottom, source_box.bottom)
            if same_line > 0.3 * min(box.top - box.bottom, source_height):
                continue
            if min(box.right, line_box.right) - max(box.left, line_box.left) <= 0.1 * source_height:
                continue
            if below and box.top > line_box.bottom - descender and box.bottom < source_box.bottom:
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
        # 下面那道空（下伸笔画）按字高算进去：放得下要 ``available - min_gap >= 字高 × reach``。
        reach = 1 + drop
        if cell is not None and available - min_gap < line_height * reach:
            # 格子里原文另一侧还有空（表格行高往往是字高的两三倍，原文居中）：原文往那边挪，
            # 给译文行腾地方，挪到离格线或那边的文字还留一道空为止。
            if below:
                limit = cell.top - min_gap
                for box in (current[number] for number in groups[group]):
                    if box.entity_no not in {added_no, source_no} and source_box.top <= box.bottom < limit and \
                            min(box.right, source_box.right) - max(box.left, source_box.left) > 0:
                        limit = box.bottom - min_gap
                room = limit - source_box.top
            else:
                limit = cell.bottom + min_gap
                for box in (current[number] for number in groups[group]):
                    if box.entity_no not in {added_no, source_no} and limit < box.top <= source_box.bottom and \
                            min(box.right, source_box.right) - max(box.left, source_box.left) > 0:
                        limit = box.top + min_gap
                room = source_box.bottom - limit
            lift = max(0.0, min(room, line_height * reach + min_gap - available))
            if lift:
                available += lift
                shift = lift if below else -lift
                source_box = replace(source_box, bottom=source_box.bottom + shift, top=source_box.top + shift)
                current[source_no] = source_box
                moves.append((source, None, shift))
        if available - min_gap >= line_height * reach:
            new_height = line_height
            fits = True
        else:
            fit = max(available - min_gap, 0.0) / reach / source_height
            tiers = [tier * source_height for tier in settings.tiers() if tier * source_height < line_height]
            tiers = tiers or [min(line_height, settings.min_scale * source_height)]
            new_height = next((height for height in tiers if height <= fit * source_height + 1e-9), tiers[-1])
            fits = new_height <= fit * source_height + 1e-9
        if not fits and unresolved is not None:
            unresolved.append({"source": source.text, "translation": line.text, "entity": added_no})
        current_gap = source_box.bottom - line_box.top if below else line_box.bottom - source_box.top
        # 放得下时，挤出来的空上下各留一半，不贴着任何一边。
        gap = min(current_gap, max(min_gap, (available - new_height * reach) / 2)) if fits else min_gap
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
        ratio = new_height / line_height if line_height else 1.0
        x = _local(line)[0]
        current[added_no] = _TextBox(added_no, *_horizontal_span(line.halign, x, (line_box.right - line_box.left) * ratio),
                                     new_bottom, new_top)
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


def text_heights(records: Sequence[Any], plain_text: Callable[[str], str]) -> dict[int, float]:
    """每段单行文字（TEXT/ATTRIB）的字高，按实体序号：缩字前记下来，给 :func:`unify_column_heights` 用。"""
    return {entity.entity_no: entity.height for entity in _collect_entities(records, plain_text)
            if entity.entity_type in _SINGLE_LINE_TYPES}


# 同一列：上下相邻两行的插入点相距不超过原字高的这么多倍（双语时中间隔着一行原文）。
_COLUMN_SPACING = 4.0


def unify_column_heights(
    records: list[Any],
    translated: Mapping[int, tuple[str, float]],
    plain_text: Callable[[str], str],
) -> int:
    """同一列译文统一字高：左端对齐、上下相邻、原字高相同的一列单行译文（逐条说明、备注），
    有的缩小了、有的没缩时，整列都改成其中最小的字高和最小的宽度比例。

    ``translated`` 是实体序号 → ``(原文, 缩字前的字高)``，只看这些译文。原文以冒号结尾的
    （「说明：」「注：」这类标题）不算进列。只会让字变小，不会压到别处。返回改过的条数。
    """
    if not translated:
        return 0
    columns: dict[tuple[Any, ...], list[tuple[float, _TextEntity]]] = {}
    for entity in _collect_entities(records, plain_text):
        item = translated.get(entity.entity_no)
        if item is None or entity.entity_type != "TEXT" or entity.halign != 0:
            continue
        source, height = item
        if height <= 0 or plain_text(source).rstrip().endswith((":", "：")):
            continue
        x, y = _local(entity)
        # 插入点横坐标、原字高按原字高的一成取整，差一点的也算同一列。
        key = (entity.container, round(entity.rotation, 1), entity.font_class,
               round(x / (0.1 * height)), round(height, 3))
        columns.setdefault(key, []).append((y, entity))
    edits: list[tuple[_TextEntity, float, float]] = []
    for key, members in columns.items():
        height = key[-1]
        members.sort(key=lambda item: -item[0])
        run: list[_TextEntity] = []
        for index, (y, entity) in enumerate(members):
            run.append(entity)
            last = index == len(members) - 1
            if last or y - members[index + 1][0] > _COLUMN_SPACING * height:
                if len(run) > 1:
                    target_height = min(member.height for member in run)
                    target_width = min(member.width_factor for member in run)
                    edits.extend((member, target_height, target_width) for member in run
                                 if (member.height, member.width_factor) != (target_height, target_width))
                run = []
    for entity, new_height, new_width_factor in sorted(edits, key=lambda item: item[0].start, reverse=True):
        _write_size(records, entity.start, entity.end, new_height, new_width_factor)
    return len(edits)


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
        source_width, source_height = _mtext_extent([(1.0, original)], height, ref_width, font_class)
        limit = max(available, _cell_content_height([(1.0, original)], height, ref_width, font_class), source_height)
        # 格子宽度就是换行宽度：一个词比它还宽时 CAD 不断开，伸过竖线压到旁边的格子。
        width_limit = max(ref_width, source_width) if ref_width > 0 else math.inf
        can_narrow = not _MTEXT_WIDTH_CODE_RE.search(raw)

        def overflow(split: tuple[float, float]) -> float:
            """排好后比格子大多少倍（不大于 1 就放得下）。"""
            height_scale, width_factor = split
            width, _total = _mtext_extent(paragraphs, height * height_scale, ref_width, font_class, width_factor)
            total = _cell_content_height(paragraphs, height * height_scale, ref_width / width_factor, font_class)
            return max(width / width_limit, total / limit if limit > 0 else math.inf)

        def fits(split: tuple[float, float]) -> bool:
            return overflow(split) <= 1 + 1e-9

        if fits((1.0, 1.0)):
            continue
        tiers = settings.tiers()
        split = next((split for tier in tiers for split in _splits(tier, can_narrow) if fits(split)), None)
        if split is None:
            split = min(_splits(tiers[-1], can_narrow), key=overflow)
            if unresolved is not None and not any(item.get("entity") == entity_no for item in unresolved):
                translation = " ".join(
                    text.strip() for _factor, text in paragraphs if text.strip() and text.strip() != original.strip()
                )
                unresolved.append({"source": original, "translation": translation or plain_text(raw), "entity": entity_no})
        edits.append((start, end, height * split[0], split[1]))

    for start, end, new_height, width_factor in sorted(edits, reverse=True):
        _write_mtext_size(records, start, end, new_height, None, width_factor)
    return len(edits)


# ── 用线画的表格里的多行文字 ─────────────────────────────────
#
# 明细表、材料表很多不是 ACAD_TABLE，而是用线画出格子、每格放一段多行文字（MTEXT）。
# 多行文字设了宽度（组码 41）时 CAD 按这个宽度自动换行：中文四五个字的宽度，换成英文要
# 折成三四行，一路压到下一行格子里；没设宽度（或宽度比格子还宽）的不换行，一直往右伸过
# 格线。这里和单行文字一样认原文四周的格线：先把换行宽度改成格子能给的宽度（不越过格线），
# 还放不下再按档位整段缩小字高；到最小比例仍放不下的记进报告。


def _wrap_tokens(text: str) -> list[tuple[str, bool]]:
    """``[(词, 前面是不是空格)]``：西文按空格分词，汉字一个字一个词（CAD 在汉字之间也能换行）。"""
    tokens: list[tuple[str, bool]] = []
    word: list[str] = []
    space = False

    def flush() -> None:
        nonlocal space
        if word:
            tokens.append(("".join(word), space))
            word.clear()
            space = False

    for char in text:
        if char.isspace():
            flush()
            space = bool(tokens)
        elif unicodedata.east_asian_width(char) in {"W", "F"}:
            flush()
            tokens.append((char, space))
            space = False
        else:
            word.append(char)
    flush()
    return tokens


def _wrap_lines(text: str, height: float, wrap: float, font_class: str) -> tuple[int, float]:
    """按宽度 ``wrap`` 自动换行后 ``(行数, 最宽一行的宽度)``；``wrap`` 不大于 0 时不换行。

    一个词比 ``wrap`` 还宽时 CAD 不会从中间断开，那一行就是这个词的宽度。
    """
    if wrap <= 0:
        return 1, estimate_text_width(text, height, 1.0, font_class)
    space = estimate_text_width(" ", height, 1.0, font_class)
    # 估算宁可偏宽：换行宽度按略窄算，多算出来的行只会让字多缩一点。
    limit = wrap / _WRAP_SLACK
    count, line, widest = 1, 0.0, 0.0
    for token, after_space in _wrap_tokens(text):
        width = estimate_text_width(token, height, 1.0, font_class)
        extra = width + (space if line and after_space else 0.0)
        if line and line + extra > limit:
            widest = max(widest, line)
            count, line = count + 1, width
        else:
            line += extra
    return count, max(widest, line)


def _mtext_extent(
    paragraphs: Sequence[tuple[float, str]], height: float, wrap: float, font_class: str, width_factor: float = 1.0,
) -> tuple[float, float]:
    """一段多行文字按宽度 ``wrap`` 排好后大约 ``(多宽, 多高)``；``width_factor`` 是整段压窄的比例。"""
    lines: list[float] = []
    widest = 0.0
    for factor, text in paragraphs:
        line_height = height * factor
        # 压窄 w 倍的字在宽度 wrap 里排，和原宽的字在 wrap / w 里排一样。
        count, width = _wrap_lines(text, line_height, wrap / width_factor if wrap > 0 else 0.0, font_class)
        widest = max(widest, width * width_factor)
        lines.extend([line_height] * count)
    if not lines:
        return 0.0, 0.0
    return widest, lines[0] + _MTEXT_LINE_SPACING * sum(lines[1:])


def _mtext_raw(records: Sequence[Any], start: int, end: int) -> str:
    parts: list[str] = []
    for record in records[start + 1:end]:
        if record.code == 101:
            break
        if record.code in {1, 3}:
            parts.append(record.value)
    return "".join(parts)


# 多行文字的内容超过 250 个字符时分成几条组码 3，最后一条是组码 1。
_MTEXT_CHUNK = 250
_MTEXT_CHUNK_TOKEN_RE = re.compile(r"\\U\+[0-9A-Fa-f]{4}|\\M\+[1-5][0-9A-Fa-f]{4}|.", re.DOTALL)
# 内容里已经有压窄/放宽的格式码时不再整段压窄（会被里面的码盖掉，估算也对不上）。
_MTEXT_WIDTH_CODE_RE = re.compile(r"\\[wW]")


def mtext_narrowing(raw: str) -> float:
    """缩字时写在多行文字内容最前面的整段压窄比例（``\\W0.7;``），没有就是 1。"""
    match = re.match(r"\\W([0-9.]+);", raw)
    return _float(match.group(1), 1.0) if match else 1.0


def _write_mtext_content(records: list[Any], start: int, end: int, raw: str) -> None:
    """把多行文字的内容（组码 3…、1）换成 ``raw``，按 250 个字符分段，转义不拆开。"""
    indices: list[int] = []
    for index in range(start + 1, end):
        if records[index].code == 101:
            break
        if records[index].code in {1, 3}:
            indices.append(index)
    if not indices:
        return
    chunks: list[str] = []
    current = ""
    for token in _MTEXT_CHUNK_TOKEN_RE.findall(raw):
        if current and len(current) + len(token) > _MTEXT_CHUNK:
            chunks.append(current)
            current = ""
        current += token
    chunks.append(current)
    template = records[indices[0]]
    code_text = template.code_text or ""
    texts = [replace(template, code=3, value=chunk, code_text="3".rjust(len(code_text)) if code_text else "3")
             for chunk in chunks[:-1]]
    texts.append(replace(template, code=1, value=chunks[-1], code_text="1".rjust(len(code_text)) if code_text else "1"))
    for index in reversed(indices):
        del records[index]
    records[indices[0]:indices[0]] = texts


def _write_mtext_size(
    records: list[Any], start: int, end: int, height: float, wrap: float | None, width_factor: float = 1.0,
) -> None:
    """改多行文字的字高（组码 40），``wrap`` 不为 ``None`` 时连同换行宽度（组码 41）；
    ``width_factor`` 小于 1 时在内容前加压窄的格式码 ``\\W``。"""
    if wrap is None:
        for index in range(start + 1, end):
            if records[index].code == 101:
                break
            if records[index].code == 40:
                records[index] = replace(records[index], value=_format_number(height))
                break
    else:
        _write_size(records, start, end, height, wrap)
    if width_factor < 1.0 - 1e-9:
        end = next((index for index in range(start + 1, len(records)) if records[index].code == 0), len(records))
        _write_mtext_content(records, start, end, f"\\W{_format_number(width_factor)};" + _mtext_raw(records, start, end))


def _splits(scale: float, can_narrow: bool) -> list[tuple[float, float]]:
    """整体缩放 ``scale`` 可以怎么拆成 ``(字高比例, 压窄比例)``：先试和单行文字一样先压窄的拆法
    （字高不变，和旁边对得齐），横向放得下、竖向放不下（双语多一行、折行）时再试只降字高。"""
    splits = [scale_parts(scale)] if can_narrow else []
    if (scale, 1.0) not in splits:
        splits.append((scale, 1.0))
    return splits


@dataclass
class _CellFit:
    entity: _TextEntity
    source: str
    height_scale: float
    width_factor: float
    wrap: float | None
    can_narrow: bool


def _unify_cell_columns(fits: Sequence[_CellFit]) -> None:
    """同一列格子里的多行文字统一大小（和单行文字的 :func:`unify_column_heights` 一样）：
    对齐方式、插入点横坐标、原字高都相同、上下相邻的一列，有的缩了、有的没缩时，整列改成
    其中最小的字高比例和最小的整体缩放。只会让字变小，换行宽度不变，所以都还在格子里。原文以冒号
    结尾的（「备注：」这类标题）不算进列。"""
    columns: dict[tuple[Any, ...], list[_CellFit]] = {}
    for fit in fits:
        entity = fit.entity
        if fit.source.rstrip().endswith((":", "：")):
            continue
        x, _y = _local(entity)
        key = (entity.container, round(entity.rotation, 1), entity.font_class, entity.halign,
               round(x / (0.1 * entity.height)), round(entity.height, 3))
        columns.setdefault(key, []).append(fit)
    for members in columns.values():
        members.sort(key=lambda fit: -_local(fit.entity)[1])
        run: list[_CellFit] = []
        for index, fit in enumerate(members):
            run.append(fit)
            last = index == len(members) - 1
            if last or _local(fit.entity)[1] - _local(members[index + 1].entity)[1] > _COLUMN_SPACING * fit.entity.height:
                if len(run) > 1:
                    # 字高取最小的，整体缩放（字高 × 压窄）也取最小的：每格的字都不比它自己算出来的
                    # 更高、更宽，换行宽度不变就都还放得下。
                    height_scale = min(member.height_scale for member in run)
                    overall = min(member.height_scale * member.width_factor for member in run)
                    width_factor = min(1.0, overall / height_scale)
                    for member in run:
                        if member.can_narrow:
                            member.height_scale, member.width_factor = height_scale, width_factor
                        else:
                            member.height_scale = min(member.height_scale, height_scale * width_factor)
                run = []


def fit_cell_mtext(
    records: list[Any],
    originals: Mapping[int, str],
    plain_text: Callable[[str], str],
    settings: FitSettings | None = None,
    unresolved: list[dict[str, str]] | None = None,
) -> int:
    """Keep translated MTEXT inside the cell of lines drawn around its source text.

    ``originals`` maps the ordinal of each translated MTEXT entity to its source text; MTEXT in an
    ACAD_TABLE display block is left to :func:`fit_table_cells`, MTEXT without solid lines on all
    four sides is left alone. The wrapping width (group 41) is first changed to what the cell
    allows; if the text still does not fit it is shrunk by the usual tiers, narrowed first (an
    inline ``\\W`` code, down to 0.7) and then lowered (group 40), as :func:`scale_parts` splits
    them for single-line text. Cells of one column end up the same size. Returns how many
    entities were changed.
    """
    if not originals:
        return 0
    settings = (settings or FitSettings()).normalized()
    table_blocks = _table_block_names(records)
    entities = _collect_entities(records, plain_text)
    extents: dict[tuple[tuple[str, str], float], list[float]] = {}
    for entity in entities:
        box = _box(entity)
        key = (entity.container, round(entity.rotation, 1))
        extent = extents.setdefault(key, [box.left, box.right, box.bottom, box.top])
        extent[:] = [min(extent[0], box.left), max(extent[1], box.right), min(extent[2], box.bottom), max(extent[3], box.top)]
    segments: dict[tuple[str, str], list[_Segment]] | None = None
    cell_indexes: dict[tuple[tuple[str, str], float], _CellIndex] = {}
    fits: list[_CellFit] = []
    for entity in entities:
        source = originals.get(entity.entity_no)
        if source is None or entity.entity_type != "MTEXT":
            continue
        if entity.container[0] == "BLOCK" and entity.container[1] in table_blocks:
            continue
        height, ref, font = entity.height, max(entity.ref_width, 0.0), entity.font_class
        x, y = _local(entity)
        source_paragraphs = [(1.0, line) for line in plain_text(source).split("\n")]
        source_width, source_height = _mtext_extent(source_paragraphs, height, ref, font)
        left, right = _horizontal_span(entity.halign, x, source_width)
        bottom, top = _vertical_span(entity.valign, y, source_height)
        key = (entity.container, round(entity.rotation, 1))
        if key not in cell_indexes:
            if segments is None:
                segments = _line_segments(records)
            cell_indexes[key] = _CellIndex(segments.get(entity.container, ()), entity.rotation, tuple(extents[key]))
        cell = cell_indexes[key].cell(_TextBox(entity.entity_no, left, right, bottom, top))
        if cell is None:
            continue
        pad = _PAD_EM * height
        if entity.halign == 1:
            room_width = 2 * min(cell.right - x, x - cell.left) - pad
        elif entity.halign == 2:
            room_width = x - cell.left - pad
        else:
            room_width = cell.right - x - pad
        if entity.valign == 2:
            room_height = 2 * min(y - cell.bottom, cell.top - y)
        elif entity.valign == 1:
            room_height = cell.top - y
        else:
            room_height = y - cell.bottom
        room_height -= pad / 2
        if room_width <= 0 or room_height <= 0:
            continue
        # 原文本来就放不下的格子按原文的大小算，不为译文多缩。
        room_width, room_height = max(room_width, source_width), max(room_height, source_height)
        raw = _mtext_raw(records, entity.start, entity.end)
        paragraphs = _mtext_paragraphs(raw, height)
        can_narrow = not _MTEXT_WIDTH_CODE_RE.search(raw)
        # 先照原来的换行宽度，不行再换成格子给的宽度（没设宽度的原本不换行）。
        wraps = [ref] + ([room_width] if abs(room_width - ref) > 1e-6 * max(room_width, 1.0) else [])

        def overflow(split: tuple[float, float], wrap: float) -> float:
            """排好后比格子大多少倍（不大于 1 就放得下）。"""
            width, total = _mtext_extent(paragraphs, height * split[0], wrap, font, split[1])
            return max(width / room_width, total / room_height)

        def fits_in(split: tuple[float, float], wrap: float) -> bool:
            return overflow(split, wrap) <= 1 + 1e-9

        scales = [1.0, *(tier for tier in settings.tiers() if tier < 1.0 - 1e-9)]
        choice = next(((split, wrap) for scale in scales for split in _splits(scale, can_narrow) for wrap in wraps
                       if fits_in(split, wrap)), None)
        if choice is None:
            # 最小比例也放不下：取超出最少的拆法和换行宽度。
            choice = min(((split, wrap) for split in _splits(scales[-1], can_narrow) for wrap in wraps),
                         key=lambda item: overflow(*item))
            if unresolved is not None and not any(item.get("entity") == entity.entity_no for item in unresolved):
                source_plain = plain_text(source)
                translation = " ".join(
                    text.strip() for _factor, text in paragraphs if text.strip() and text.strip() != source_plain.strip()
                )
                unresolved.append({"source": source_plain, "translation": translation or entity.text, "entity": entity.entity_no})
        (height_scale, width_factor), wrap = choice
        fits.append(_CellFit(entity, plain_text(source), height_scale, width_factor, None if wrap == ref else wrap, can_narrow))
    _unify_cell_columns(fits)
    changed = [fit for fit in fits if fit.wrap is not None or fit.height_scale < 1.0 - 1e-9 or fit.width_factor < 1.0 - 1e-9]
    for fit in sorted(changed, key=lambda fit: fit.entity.start, reverse=True):
        entity = fit.entity
        _write_mtext_size(records, entity.start, entity.end, entity.height * fit.height_scale, fit.wrap, fit.width_factor)
    return len(changed)
