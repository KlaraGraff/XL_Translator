"""PPT 译文写回后，按真实字宽估算改过字的框还放不放得下，放不下就缩字或加高。

包 2（``core/pptx_text.py``）每写完一个部件（幻灯片、版式、母版）调用一次
``fit_after_write``，传入改过文字的 ``a:txBody`` 和含改过单元格的 ``p:graphicFrame``。

字号、字体、行距、内边距、换行、自动调整方式和框的位置大小都沿继承链算（PowerPoint
实测的规矩，依据 GenOffice 的探测结果）：

  - 占位符：片段 ``a:rPr`` → 段落 ``a:pPr/a:defRPr`` → 形状 ``a:lstStyle`` → 版式里对应的
    占位符 → 母版里对应的占位符 → 母版 ``p:txStyles``（标题 / 正文 / 其他）；
    对应关系先按 (type, idx)，再按 idx（跳过页脚、日期这类功能占位符），再按 type。
    占位符没写位置大小、``bodyPr`` 没写的属性也按同样顺序从版式、母版取。
  - 其他形状（文本框、自选图形、表格单元格）：片段 → 段落 → 形状 → ``presentation.xml``
    的 ``defaultTextStyle``（不看母版的 otherStyle）。
  - 主题字体 ``+mn-lt``、``+mj-ea`` 等从母版关联的主题解析成真名；东亚字体留空时按
    语言取 ``a:font script="Hans"`` 这类按文字给的字体。
  - 组合里的形状按各层组合的 ``chOff/chExt`` 换算成真实大小。

排版按 PowerPoint 的量法：单倍行高 = 1.2 个字号（与字体无关），百分比行距在此基础上乘，
固定行距按磅值；文本框第一段的段前距不算；表格单元格最后一段的段后距也不算；
字宽用 ``core/font_metrics`` 量真实字体，找不到时用估算。

三种自动调整方式各按作者的本意处理：

  - 「溢出时缩排文字」（``a:normAutofit``）：PowerPoint 打开文件时不重算，只按文件里存的
    ``fontScale``/``lnSpcReduction`` 画（GenOffice 实测：只写光秃秃的 ``normAutofit``
    会按 100% 溢出着画），原值是按原文算的，所以按译文重算，取 PowerPoint 自己的档位
    （字号 100、92.5、85、77.5、70、62.5%，行距减 0、0、10、10、20、20%），字号本身不动；
    缩到最后一档仍放不下才往下方空白处加高。
  - 「根据文字调整形状大小」（``a:spAutoFit``）和「不自动调整」：先往下方空白处加高（不换行
    的框先按对齐方式加宽），不压到别的形状、不出页面；空白不够再按比例缩各片段的 ``sz``
    （固定行距一起缩），每档 5%，最低 60%（``text_box_fit.MIN_SCALE``）。
  - 表格：PowerPoint 的表格不吃自动调整，行会跟着字撑高，所以同样先长进下方空白；放不下
    再整张表一起缩字号，保持字号统一。
  - 加高、加宽都和旁边的形状、页边留出 ``gap_pt``（默认 6 磅，约 2 毫米，页面上可选），不紧挨着；
    原稿里本来就比这更近的方向不再长。

以上是默认的「融合」。页面上的写回偏好（``layout_mode``）另有两种：

  - 「保持字号」（``keep_size``）：字号一律不动，框和表格按需要往下长，不管下面有没有东西；
    长了以后盖住别的形状或超出页面的进复核清单。
  - 「固定版面」（``fixed``）：所有框和表格的位置大小都不动，只缩字：溢出缩排的框照档位缩，
    其余的框和表格缩 ``sz``。

「不换行」的框加宽、缩到下限都放不下时智能换行：改成在框宽内折行（``wrap="square"``），
除非折行后往下冒出的比一行往旁边冒出的还多（保持字号模式一律折行）。
缩到下限仍放不下的返回 ``FitIssue``，进需复核清单。
竖排（``eaVert`` 等）和艺术字变形的框不动（竖排由包 2 列入复核）。

写回之前那份部件（``read_part(part_name)`` 读到的原字节）用来知道原文排得多满：
原文本来就超出框的，译文放到同样程度不算放不下；不换行的框以原文最长一行为宽度上限。
"""
from __future__ import annotations

import math
import posixpath
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from lxml import etree

from core.font_metrics import (
    FontLibrary,
    ResolvedFace,
    default_library,
    fallback_advance,
    is_wide,
)
from core.text_box_fit import MIN_SCALE

NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
NS_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
NS_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"

REL_SLIDE_LAYOUT = f"{NS_R}/slideLayout"
REL_SLIDE_MASTER = f"{NS_R}/slideMaster"
REL_THEME = f"{NS_R}/theme"
REL_OFFICE_DOCUMENT = f"{NS_R}/officeDocument"
REL_TABLE_STYLES = f"{NS_R}/tableStyles"

#: 写回偏好（页面上的三选一）：融合（默认）、保持字号、固定版面。
LAYOUT_FIT = "fit"
LAYOUT_KEEP_SIZE = "keep_size"
LAYOUT_FIXED = "fixed"
LAYOUT_MODES = (LAYOUT_FIT, LAYOUT_KEEP_SIZE, LAYOUT_FIXED)
#: 融合模式加高、加宽时，与旁边的形状和页边至少留的距离（磅），页面上可选。
DEFAULT_GAP_PT = 6.0

#: ``FitIssue.reason``：缩到下限、往下方空白处加高也放不下。
REASON_OVERFLOW = f"缩到 {round(MIN_SCALE * 100)}% 仍放不下"
#: 「保持字号」：框加高后盖住了别的形状 / 超出页面 / 框不能加高（组合、旋转、竖排、多栏）。
REASON_KEEP_COVERS = "字号不变，加高后盖住了其他内容"
REASON_KEEP_OFF_PAGE = "字号不变，加高后超出页面"
REASON_KEEP_OVERFLOW = "字号不变，这个框不能加高，文字超出框"

# PowerPoint「溢出时缩排文字」的档位（fontScale, lnSpcReduction），GenOffice 从真实文件
# 归纳（92500/0、85000/10000、62500/20000…）；下限 60% 取到 62.5% 这一档为止。
NORM_AUTOFIT_STEPS: tuple[tuple[float, float], ...] = tuple(
    step
    for step in (
        (1.0, 0.0),
        (0.925, 0.0),
        (0.85, 0.1),
        (0.775, 0.1),
        (0.7, 0.2),
        (0.625, 0.2),
        (0.55, 0.2),
    )
    if step[0] >= MIN_SCALE
)
# 「不自动调整」和表格：字号每档缩 5%，直到下限。
EXPLICIT_SCALE_STEP = 0.05

# PowerPoint 单倍行高 = 1.2 个字号，与字体无关（GenOffice 对十种字体实测）。
PPT_SINGLE_LINE_EM = 1.2
EMU_PER_PT = 12700
_DEFAULT_INSETS = {"lIns": 91440, "tIns": 45720, "rIns": 91440, "bIns": 45720}
_DEFAULT_CELL_MARGINS = {"marL": 91440, "marT": 45720, "marR": 91440, "marB": 45720}
_DEFAULT_SIZE_PT = 18.0
_DEFAULT_TAB_PT = 72.0
_FIT_EPSILON_PT = 0.01
_SKIPPED_VERT = {"eaVert", "wordArtVert", "wordArtVertRtl", "mongolianVert"}
_SWAPPED_VERT = {"vert", "vert270"}
_PLAIN_WARPS = {None, "textNoShape", "textPlain"}

_TITLE_TYPES = {"title", "ctrTitle"}
_BODY_TYPES = {"body", "subTitle", "obj"}
# 页脚、日期这类功能占位符：只按 idx 对应时跳过，免得母版的 dt idx="2" 把样式给了正文 idx="2"。
_FUNCTION_TYPES = {"dt", "ftr", "sldNum", "sldImg", "hdr", "clipArt", "dgm", "media", "oleObj", "chart", "tbl", "pic"}

# 不放在行首的标点（前面的字一起带走）、不放在行尾的标点（和后面的字一起走）。
_NO_LINE_START = set("，。、；：？！）》」』】〕〉’”…·%‰°℃,.;:?!)]}»")
_NO_LINE_END = set("（《「『【〔〈‘“([{«")
_BREAK_AFTER = set("-‐–—")
_SPACES = {" ", "　", " ", " ", " "}
# 宽度不定的标点：片段语言是中日韩时 PowerPoint 用东亚字体画（全角）。
_AMBIGUOUS = set("“”‘’…—·")
_EA_SCRIPTS = {"zh-cn": "Hans", "zh-sg": "Hans", "zh-tw": "Hant", "zh-hk": "Hant", "zh-mo": "Hant"}


@dataclass(frozen=True)
class FitIssue:
    """缩到下限也放不下的框，进需复核清单。"""

    #: 部件名，例如 ``ppt/slides/slide3.xml``。
    part_name: str
    #: 调用方传进 ``touched`` 的那个 ``a:txBody`` 或 ``p:graphicFrame``（同一个对象）。
    target: Any
    #: 整句中文原因，例如「缩到 60% 仍放不下」。
    reason: str


ReadPart = Callable[[str], "bytes | None"]


def fit_after_write(
    read_part: ReadPart,
    part_name: str,
    root,
    touched: Iterable,
    *,
    layout_mode: str = LAYOUT_FIT,
    gap_pt: float = DEFAULT_GAP_PT,
    fonts: FontLibrary | None = None,
) -> list[FitIssue]:
    """按译文调整 ``touched`` 里各框的字号或大小，就地改 ``root``；返回放不下的框。

    ``read_part(name)`` 读包里任意部件的当前字节（版式、母版、主题、关系文件、
    ``presentation.xml``；``part_name`` 本身还没写回时读到的是原文）。只改 ``touched``
    里的形状。备注页等不是幻灯片、版式、母版的部件直接返回空列表。
    ``layout_mode``：``"fit"`` 融合（先加高再缩字），``"keep_size"`` 保持字号（只加高），
    ``"fixed"`` 固定版面（只缩字）；认不得的按融合。``gap_pt``：融合模式加高、加宽时与旁边的
    形状和页边至少留的距离（磅）；原稿里本来就比这更近的方向不再长。
    """
    if not _FITTABLE_PART.match(part_name or ""):
        return []
    elements = list(touched or [])
    if not elements:
        return []
    ctx = _PartContext(read_part, part_name, root, fonts or default_library())
    ctx.layout_mode = layout_mode if layout_mode in LAYOUT_MODES else LAYOUT_FIT
    ctx.gap = max(0.0, float(gap_pt)) * EMU_PER_PT
    issues: list[FitIssue] = []
    done: set = set()
    for element in elements:
        target = element
        if element.tag in _TEXT_BODY_TAGS and _ancestor(element, _a("tc")) is not None:
            target = _ancestor(element, _p("graphicFrame"))
        if target is None or target in done:
            continue
        done.add(target)
        if target.tag == _p("graphicFrame"):
            reason = _fit_table(ctx, target)
        elif target.tag in _TEXT_BODY_TAGS:
            reason = _fit_shape(ctx, target)
        else:
            continue
        if reason:
            issues.append(FitIssue(part_name=part_name, target=element, reason=reason))
    return issues


_FITTABLE_PART = re.compile(r"^ppt/(slides|slideLayouts|slideMasters)/[^/]+\.xml$")
# 形状里的是 p:txBody，表格单元格里的是 a:txBody。
_TEXT_BODY_TAGS = {f"{{{NS_P}}}txBody", f"{{{NS_A}}}txBody"}


def _a(tag: str) -> str:
    return f"{{{NS_A}}}{tag}"


def _p(tag: str) -> str:
    return f"{{{NS_P}}}{tag}"


def _ancestor(element, tag: str):
    node = element.getparent()
    while node is not None:
        if node.tag == tag:
            return node
        node = node.getparent()
    return None


def _int(value, default: int | None = None) -> int | None:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _parse(data: bytes | None):
    if not data:
        return None
    try:
        return etree.fromstring(data, parser=etree.XMLParser(huge_tree=True, resolve_entities=False))
    except etree.XMLSyntaxError:
        return None


# ---------------------------------------------------------------- 部件与继承链


class _StyleLayer:
    """一层列表样式（``a:lstStyle``、``p:bodyStyle``、``p:defaultTextStyle``……）。"""

    def __init__(self, element) -> None:
        self.element = element

    def candidates(self, level: int) -> list:
        found = []
        if self.element is None:
            return found
        lvl = self.element.find(_a(f"lvl{level + 1}pPr"))
        if lvl is not None:
            found.append(lvl)
        default = self.element.find(_a("defPPr"))
        if default is not None:
            found.append(default)
        return found


@dataclass
class _Placeholder:
    type: str
    idx: str
    sp: Any


@dataclass
class _ThemeFonts:
    major: dict[str, str] = field(default_factory=dict)
    minor: dict[str, str] = field(default_factory=dict)
    major_scripts: dict[str, str] = field(default_factory=dict)
    minor_scripts: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, root) -> _ThemeFonts:
        fonts = cls()
        scheme = root.find(f".//{_a('fontScheme')}") if root is not None else None
        if scheme is None:
            return fonts
        for name, slots, scripts in (
            ("majorFont", fonts.major, fonts.major_scripts),
            ("minorFont", fonts.minor, fonts.minor_scripts),
        ):
            group = scheme.find(_a(name))
            if group is None:
                continue
            for slot in ("latin", "ea", "cs"):
                el = group.find(_a(slot))
                if el is not None:
                    slots[slot] = el.get("typeface") or ""
            for el in group.findall(_a("font")):
                if el.get("script"):
                    scripts[el.get("script")] = el.get("typeface") or ""
        return fonts

    def resolve(self, typeface: str | None, lang: str) -> str:
        """``+mn-lt``、``+mj-ea`` 这类主题字体引用 → 真实字体名；其余原样返回。"""
        if not typeface or not typeface.startswith("+"):
            return typeface or ""
        match = re.fullmatch(r"\+(mj|mn)-(lt|ea|cs)", typeface)
        if not match:
            return ""
        major = match.group(1) == "mj"
        slots = self.major if major else self.minor
        slot = {"lt": "latin", "ea": "ea", "cs": "cs"}[match.group(2)]
        value = slots.get(slot, "")
        if value or slot != "ea":
            return value
        scripts = self.major_scripts if major else self.minor_scripts
        for script in (_ea_script(lang), "Hans", "Jpan", "Hant", "Hang"):
            if script and scripts.get(script):
                return scripts[script]
        return ""


def _ea_script(lang: str) -> str:
    lang = (lang or "").casefold()
    if lang in _EA_SCRIPTS:
        return _EA_SCRIPTS[lang]
    if lang.startswith("zh"):
        return "Hans"
    if lang.startswith("ja"):
        return "Jpan"
    if lang.startswith("ko"):
        return "Hang"
    return ""


def _is_cjk_lang(lang: str | None) -> bool:
    return bool(_ea_script(lang or ""))


class _PartContext:
    """一个部件排版要用的东西：主题字体、母版版式样式、占位符、页面大小、障碍物。"""

    def __init__(self, read_part: ReadPart, part_name: str, root, fonts: FontLibrary) -> None:
        self.read_part = read_part
        self.part_name = part_name
        self.root = root
        self.fonts = fonts
        #: 写回偏好，见 ``LAYOUT_MODES``。
        self.layout_mode = LAYOUT_FIT
        self.gap = DEFAULT_GAP_PT * EMU_PER_PT
        self._parsed: dict[str, Any] = {}
        self.measurer = _Measurer(fonts)

        presentation_name = self._presentation_name()
        presentation = self._load(presentation_name)
        size = presentation.find(_p("sldSz")) if presentation is not None else None
        self.slide_cx = _int(size.get("cx"), 9144000) if size is not None else 9144000
        self.slide_cy = _int(size.get("cy"), 6858000) if size is not None else 6858000
        default_style = presentation.find(_p("defaultTextStyle")) if presentation is not None else None
        self.default_layer = _StyleLayer(default_style)
        self.table_styles = self._load(self._related(presentation_name, REL_TABLE_STYLES))

        kind = part_name.split("/")[1]
        layout_name = master_name = None
        if kind == "slides":
            layout_name = self._related(part_name, REL_SLIDE_LAYOUT)
            master_name = self._related(layout_name, REL_SLIDE_MASTER) if layout_name else None
        elif kind == "slideLayouts":
            master_name = self._related(part_name, REL_SLIDE_MASTER)
        else:
            master_name = part_name
        layout = self._load(layout_name) if kind == "slides" else None
        master = root if kind == "slideMasters" else self._load(master_name)
        self.layout_phs = _placeholders(layout)
        self.master_phs = _placeholders(master)
        styles = master.find(_p("txStyles")) if master is not None else None
        self.master_styles = {
            family: _StyleLayer(styles.find(_p(f"{family}Style")) if styles is not None else None)
            for family in ("title", "body", "other")
        }
        self.theme = _ThemeFonts.load(self._load(self._related(master_name, REL_THEME)) if master_name else None)

        # 版式、母版上不是占位符的形状（logo、线条）在幻灯片上也画着，加高时同样不能压到。
        self.inherited_obstacles: list[_Rect] = []
        shows_layout = kind == "slides" and root.get("showMasterSp") != "0"
        shows_master = kind in ("slides", "slideLayouts") and root.get("showMasterSp") != "0"
        if shows_layout and layout is not None:
            self.inherited_obstacles += _decoration_rects(layout)
            shows_master = layout.get("showMasterSp") != "0"
        if shows_master and master is not None and kind != "slideMasters":
            self.inherited_obstacles += _decoration_rects(master)

        original = read_part(part_name)
        original_root = _parse(original)
        self.original_shapes: dict[str, Any] = {}
        if original_root is not None and etree.tostring(original_root) != etree.tostring(root):
            self.original_shapes = _shapes_by_id(original_root)

    # -- 读部件

    def _load(self, name: str | None):
        if not name:
            return None
        if name not in self._parsed:
            try:
                data = self.read_part(name)
            except Exception:  # noqa: BLE001 - 读不到就当没有，退回默认值
                data = None
            self._parsed[name] = _parse(data)
        return self._parsed[name]

    def _presentation_name(self) -> str:
        rels = self._load("_rels/.rels")
        if rels is not None:
            for rel in rels.findall(f"{{{NS_PKG_REL}}}Relationship"):
                if rel.get("Type") == REL_OFFICE_DOCUMENT and rel.get("Target"):
                    return rel.get("Target").lstrip("/")
        return "ppt/presentation.xml"

    def _related(self, part_name: str | None, rel_type: str) -> str | None:
        if not part_name:
            return None
        directory, base = posixpath.split(part_name)
        rels = self._load(posixpath.join(directory, "_rels", f"{base}.rels"))
        if rels is None:
            return None
        for rel in rels.findall(f"{{{NS_PKG_REL}}}Relationship"):
            if rel.get("Type") == rel_type and rel.get("TargetMode") != "External":
                target = rel.get("Target") or ""
                if target.startswith("/"):
                    return target.lstrip("/")
                return posixpath.normpath(posixpath.join(directory, target))
        return None

    # -- 占位符继承

    def placeholder(self, sp) -> tuple[str, str] | None:
        ph = sp.find(f"{_p('nvSpPr')}/{_p('nvPr')}/{_p('ph')}")
        if ph is None:
            return None
        return ph.get("type") or "obj", ph.get("idx") or ""

    def inherited_shapes(self, sp) -> list:
        """版式、母版里与这个占位符对应的形状（先版式后母版）。"""
        ph = self.placeholder(sp)
        if ph is None:
            return []
        found = []
        for table in (self.layout_phs, self.master_phs):
            match = _match_placeholder(table, *ph)
            if match is not None:
                found.append(match.sp)
        return found

    def style_layers(self, sp) -> list[_StyleLayer]:
        """形状自己的 lstStyle 之外，往下继承的几层列表样式。"""
        ph = self.placeholder(sp)
        if ph is None:
            return [self.default_layer]
        layers = []
        for shape in self.inherited_shapes(sp):
            layers.append(_StyleLayer(shape.find(f"{_p('txBody')}/{_a('lstStyle')}")))
        ph_type = ph[0]
        family = "title" if ph_type in _TITLE_TYPES else "body" if ph_type in _BODY_TYPES else "other"
        layers.append(self.master_styles[family])
        layers.append(self.default_layer)
        return layers

    def table_bold(self, tbl) -> Callable[[int, int, int, int], bool]:
        """表格样式让哪些单元格加粗：返回 (行, 列, 行数, 列数) → 是否加粗。"""
        props = tbl.find(_a("tblPr"))
        flags = {name: props is not None and props.get(name) in ("1", "true") for name in
                 ("firstRow", "lastRow", "firstCol", "lastCol")}
        style_id = props.findtext(_a("tableStyleId")) if props is not None else None
        bold_parts = {"firstRow", "lastRow", "firstCol", "lastCol"}
        if style_id and self.table_styles is not None:
            for style in self.table_styles.findall(_a("tblStyle")):
                if style.get("styleId") == style_id:
                    bold_parts = set()
                    for part in ("firstRow", "lastRow", "firstCol", "lastCol"):
                        text_style = style.find(f"{_a(part)}/{_a('tcTxStyle')}")
                        if text_style is not None and text_style.get("b") == "on":
                            bold_parts.add(part)
                    break

        def bold(row: int, col: int, rows: int, cols: int) -> bool:
            return (
                ("firstRow" in bold_parts and flags["firstRow"] and row == 0)
                or ("lastRow" in bold_parts and flags["lastRow"] and row == rows - 1)
                or ("firstCol" in bold_parts and flags["firstCol"] and col == 0)
                or ("lastCol" in bold_parts and flags["lastCol"] and col == cols - 1)
            )

        return bold


def _placeholders(root) -> list[_Placeholder]:
    if root is None:
        return []
    found = []
    for sp in root.iter(_p("sp")):
        ph = sp.find(f"{_p('nvSpPr')}/{_p('nvPr')}/{_p('ph')}")
        if ph is not None:
            found.append(_Placeholder(ph.get("type") or "obj", ph.get("idx") or "", sp))
    return found


def _family(ph_type: str) -> str:
    if ph_type in _TITLE_TYPES:
        return "title"
    if ph_type in _BODY_TYPES:
        return "body"
    return ph_type


def _match_placeholder(table: list[_Placeholder], ph_type: str, idx: str) -> _Placeholder | None:
    for entry in table:
        if entry.type == ph_type and entry.idx == idx:
            return entry
    if idx:
        for entry in table:
            if entry.idx == idx and entry.type not in _FUNCTION_TYPES:
                return entry
    for entry in table:
        if entry.type == ph_type:
            return entry
    family = _family(ph_type)
    for entry in table:
        if _family(entry.type) == family:
            return entry
    return None


def _shapes_by_id(root) -> dict[str, Any]:
    shapes: dict[str, Any] = {}
    for element in root.iter(_p("sp"), _p("graphicFrame")):
        if _ancestor(element, f"{{{NS_MC}}}Fallback") is not None:
            continue
        nv = element.find("*/" + _p("cNvPr"))
        if nv is not None and nv.get("id") and nv.get("id") not in shapes:
            shapes[nv.get("id")] = element
    return shapes


def _shape_id(element) -> str | None:
    nv = element.find("*/" + _p("cNvPr"))
    return nv.get("id") if nv is not None else None


# ---------------------------------------------------------------- 几何


@dataclass
class _Rect:
    x: float
    y: float
    cx: float
    cy: float

    @property
    def right(self) -> float:
        return self.x + self.cx

    @property
    def bottom(self) -> float:
        return self.y + self.cy

    def intersects(self, other: _Rect) -> bool:
        return self.x < other.right and other.x < self.right and self.y < other.bottom and other.y < self.bottom


def _xfrm_of(element):
    """形状的变换：p:sp/p:pic/p:cxnSp 在 spPr 里，组合在 grpSpPr 里，图形框（表格）是 p:xfrm。"""
    if element.tag == _p("graphicFrame"):
        return element.find(_p("xfrm"))
    if element.tag == _p("grpSp"):
        return element.find(f"{_p('grpSpPr')}/{_a('xfrm')}")
    return element.find(f"{_p('spPr')}/{_a('xfrm')}")


def _xfrm_rect(xfrm) -> _Rect | None:
    if xfrm is None:
        return None
    off, ext = xfrm.find(_a("off")), xfrm.find(_a("ext"))
    if off is None or ext is None:
        return None
    return _Rect(_int(off.get("x"), 0), _int(off.get("y"), 0), _int(ext.get("cx"), 0), _int(ext.get("cy"), 0))


def _group_chain(element) -> list:
    groups = []
    node = element.getparent()
    while node is not None:
        if node.tag == _p("grpSp"):
            groups.append(node)
        node = node.getparent()
    return groups


def _to_slide(rect: _Rect, groups: list) -> tuple[_Rect, float, float]:
    """组合里的坐标换算到页面上；返回 (页面上的框, 横向缩放, 纵向缩放)。"""
    sx = sy = 1.0
    for group in groups:
        xfrm = group.find(f"{_p('grpSpPr')}/{_a('xfrm')}")
        if xfrm is None:
            continue
        off, ext = xfrm.find(_a("off")), xfrm.find(_a("ext"))
        ch_off, ch_ext = xfrm.find(_a("chOff")), xfrm.find(_a("chExt"))
        if off is None or ext is None:
            continue
        ch_x = _int(ch_off.get("x"), 0) if ch_off is not None else 0
        ch_y = _int(ch_off.get("y"), 0) if ch_off is not None else 0
        ch_cx = _int(ch_ext.get("cx"), 0) if ch_ext is not None else 0
        ch_cy = _int(ch_ext.get("cy"), 0) if ch_ext is not None else 0
        gx = (_int(ext.get("cx"), 0) / ch_cx) if ch_cx > 0 else 1.0
        gy = (_int(ext.get("cy"), 0) / ch_cy) if ch_cy > 0 else 1.0
        rect = _Rect(
            _int(off.get("x"), 0) + (rect.x - ch_x) * gx,
            _int(off.get("y"), 0) + (rect.y - ch_y) * gy,
            rect.cx * gx,
            rect.cy * gy,
        )
        sx, sy = sx * gx, sy * gy
    return rect, sx, sy


def _rotated(element) -> bool:
    for node in (element, *_group_chain(element)):
        xfrm = _xfrm_of(node)
        if xfrm is not None and _int(xfrm.get("rot"), 0) % 21600000 != 0:
            return True
    return False


def _top_level_children(tree) -> list:
    children = []
    for child in tree:
        if child.tag == f"{{{NS_MC}}}AlternateContent":
            choice = child.find(f"{{{NS_MC}}}Choice")
            if choice is not None:
                children.extend(choice)
        else:
            children.append(child)
    return children


def _has_text(sp) -> bool:
    return any((t.text or "").strip() for t in sp.iter(_a("t")))


def _hidden(element) -> bool:
    nv = element.find("*/" + _p("cNvPr"))
    return nv is not None and nv.get("hidden") in ("1", "true")


_DRAWN_TAGS = {"sp", "grpSp", "graphicFrame", "pic", "cxnSp", "contentPart"}


def _decoration_rects(root) -> list[_Rect]:
    """版式、母版上不是占位符的形状。"""
    tree = root.find(f"{_p('cSld')}/{_p('spTree')}")
    if tree is None:
        return []
    rects = []
    for child in _top_level_children(tree):
        if etree.QName(child).localname not in _DRAWN_TAGS or _hidden(child):
            continue
        if child.find(f"*/{_p('nvPr')}/{_p('ph')}") is not None:
            continue
        rect = _xfrm_rect(_xfrm_of(child))
        if rect is not None:
            rects.append(rect)
    return rects


def _shape_rect(ctx: _PartContext, element) -> _Rect | None:
    """形状自己的位置大小（组合里的是组合坐标）；占位符没写的从版式、母版取。"""
    rect = _xfrm_rect(_xfrm_of(element))
    if rect is not None or element.tag != _p("sp"):
        return rect
    for shape in ctx.inherited_shapes(element):
        rect = _xfrm_rect(_xfrm_of(shape))
        if rect is not None:
            return rect
    return None


def _obstacles(ctx: _PartContext, own) -> list[_Rect]:
    tree = ctx.root.find(f"{_p('cSld')}/{_p('spTree')}")
    rects = list(ctx.inherited_obstacles)
    if tree is None:
        return rects
    for child in _top_level_children(tree):
        if child is own or etree.QName(child).localname not in _DRAWN_TAGS or _hidden(child):
            continue
        if child.tag == _p("sp") and ctx.placeholder(child) is not None and not _has_text(child):
            continue  # 空占位符放映时不显示
        rect = _shape_rect(ctx, child)
        if rect is not None:
            rects.append(rect)
    return rects


def _room_below(ctx: _PartContext, own, rect: _Rect, gap: float = 0.0) -> float:
    """框下边到下面第一个形状（或页面底边）之间的空白，再减去要留的 ``gap``，EMU。
    原本就和框重叠的形状不算。"""
    limit = float(ctx.slide_cy)
    for other in _obstacles(ctx, own):
        if other.intersects(rect):
            continue
        if other.x < rect.right and other.right > rect.x and other.y >= rect.bottom - 1:
            limit = min(limit, other.y)
    return max(0.0, limit - rect.bottom - gap)


def _room_sideways(ctx: _PartContext, own, rect: _Rect, gap: float = 0.0) -> tuple[float, float]:
    """框左右两边到最近形状（或页面边）的空白，各减去要留的 ``gap``，EMU。"""
    left, right = rect.x, float(ctx.slide_cx) - rect.right
    for other in _obstacles(ctx, own):
        if other.intersects(rect) or not (other.y < rect.bottom and other.bottom > rect.y):
            continue
        if other.x >= rect.right - 1:
            right = min(right, other.x - rect.right)
        elif other.right <= rect.x + 1:
            left = min(left, rect.x - other.right)
    return max(0.0, left - gap), max(0.0, right - gap)


# ---------------------------------------------------------------- 文字模型


@dataclass(frozen=True)
class _RunStyle:
    size: float  # 磅，缩放前
    bold: bool
    italic: bool
    latin: str
    ea: str
    caps: bool
    spacing: float  # 字间距，磅
    script: float  # 上下标画成 2/3 大
    cjk_lang: bool


@dataclass
class _Para:
    pieces: list[tuple[str, _RunStyle]]
    end_style: _RunStyle
    line_pct: float | None
    line_pts: float | None
    before: tuple[float | None, float | None]  # (百分比, 磅)
    after: tuple[float | None, float | None]
    mar_l: float
    mar_r: float
    indent: float
    bullet_em: float
    bullet_scale: float
    latin_break: bool
    tab: float
    align: str
    # 改字号时要写 sz 的元素（rPr / endParaRPr 的宿主）和它们的原字号（磅）。
    sized: list[tuple[Any, float]] = field(default_factory=list)


class _Measurer:
    """量单个字的宽度（字号的倍数）。按片段字体先找真字体，找不到用估算。"""

    def __init__(self, fonts: FontLibrary) -> None:
        self.fonts = fonts
        self._faces: dict[tuple[str, bool, bool], ResolvedFace | None] = {}
        self._cache: dict[tuple[str, str, str, bool, bool, bool], float] = {}

    def _face(self, name: str, bold: bool, italic: bool) -> ResolvedFace | None:
        key = (name, bold, italic)
        if key not in self._faces:
            self._faces[key] = self.fonts.resolve(name, bold=bold, italic=italic) if name else None
        return self._faces[key]

    def em(self, char: str, style: _RunStyle) -> float:
        key = (char, style.latin, style.ea, style.bold, style.italic, style.cjk_lang)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        east_asian = is_wide(char) or (style.cjk_lang and char in _AMBIGUOUS)
        order = (style.ea, style.latin) if east_asian else (style.latin, style.ea)
        value = None
        for name in order:
            face = self._face(name, style.bold, style.italic)
            if face is not None:
                value = face.advance(char)
                if value is not None:
                    break
        if value is None:
            value = fallback_advance(char)
        self._cache[key] = value
        return value


def _first_attr(chain: list, name: str):
    for el in chain:
        if el is not None and el.get(name) is not None:
            return el.get(name)
    return None


def _first_child(chain: list, *tags: str):
    for el in chain:
        if el is None:
            continue
        for tag in tags:
            child = el.find(_a(tag))
            if child is not None:
                return child
    return None


def _bool_attr(value) -> bool | None:
    if value is None:
        return None
    return value in ("1", "true", "on")


def _spacing(el) -> tuple[float | None, float | None]:
    """(百分比, 磅)：a:spcPct 是千分之一个百分点，a:spcPts 是百分之一磅。"""
    if el is None:
        return None, None
    pct = el.find(_a("spcPct"))
    if pct is not None:
        return _int(pct.get("val"), 100000) / 100000, None
    pts = el.find(_a("spcPts"))
    if pts is not None:
        return None, _int(pts.get("val"), 0) / 100
    return None, None


class _TextBodyReader:
    """把一个 txBody 读成排版用的段落，属性都沿继承链解析好。"""

    def __init__(self, ctx: _PartContext, layers: list[_StyleLayer], font_ref: str | None = None,
                 bold_override: bool = False) -> None:
        self.ctx = ctx
        self.layers = layers
        self.font_ref = font_ref
        self.bold_override = bold_override

    def paragraphs(self, tx_body) -> list[_Para]:
        own = _StyleLayer(tx_body.find(_a("lstStyle")))
        return [self._paragraph(p, [own, *self.layers]) for p in tx_body.findall(_a("p"))]

    def _paragraph(self, paragraph, layers: list[_StyleLayer]) -> _Para:
        ppr = paragraph.find(_a("pPr"))
        level = max(0, min(8, _int(ppr.get("lvl"), 0) if ppr is not None else 0))
        chain = ([ppr] if ppr is not None else []) + [el for layer in layers for el in layer.candidates(level)]
        defaults = [el.find(_a("defRPr")) for el in chain]
        defaults = [el for el in defaults if el is not None]

        pieces: list[tuple[str, _RunStyle]] = []
        sized: list[tuple[Any, float]] = []
        for child in paragraph:
            if child.tag in (_a("r"), _a("fld")):
                rpr = child.find(_a("rPr"))
                style = self._style([rpr, *defaults])
                pieces.append((child.findtext(_a("t")) or "", style))
                sized.append((child, style.size))
            elif child.tag == _a("br"):
                style = self._style([child.find(_a("rPr")), *defaults])
                pieces.append(("\n", style))
                sized.append((child, style.size))
        end = paragraph.find(_a("endParaRPr"))
        end_style = self._style([end, *defaults])
        if end is not None or not pieces:
            sized.append((paragraph, end_style.size))

        line_pct, line_pts = _spacing(_first_child(chain, "lnSpc"))
        if line_pct is None and line_pts is None:
            line_pct = 1.0
        bullet = _first_child(chain, "buNone", "buChar", "buAutoNum", "buBlip")
        bullet_em, bullet_scale = 0.0, 1.0
        if bullet is not None and bullet.tag != _a("buNone"):
            size_pct = _first_child(chain, "buSzPct")
            if size_pct is not None:
                bullet_scale = _int(size_pct.get("val"), 100000) / 100000
            glyph = bullet.get("char") or "•" if bullet.tag == _a("buChar") else "8."
            base = pieces[0][1] if pieces else end_style
            font = _first_child(chain, "buFont")
            if font is not None and font.get("typeface") and bullet.tag == _a("buChar"):
                typeface = self.ctx.theme.resolve(font.get("typeface"), "")
                base = _RunStyle(base.size, False, False, typeface, typeface, False, 0.0, 1.0, False)
            bullet_em = sum(self.ctx.measurer.em(ch, base) for ch in glyph)
        return _Para(
            pieces=pieces,
            end_style=end_style,
            line_pct=line_pct,
            line_pts=line_pts,
            before=_spacing(_first_child(chain, "spcBef")),
            after=_spacing(_first_child(chain, "spcAft")),
            mar_l=(_int(_first_attr(chain, "marL"), 0) or 0) / EMU_PER_PT,
            mar_r=(_int(_first_attr(chain, "marR"), 0) or 0) / EMU_PER_PT,
            indent=(_int(_first_attr(chain, "indent"), 0) or 0) / EMU_PER_PT,
            bullet_em=bullet_em,
            bullet_scale=bullet_scale,
            latin_break=_bool_attr(_first_attr(chain, "latinLnBrk")) is True,
            tab=(_int(_first_attr(chain, "defTabSz"), None) or _DEFAULT_TAB_PT * EMU_PER_PT) / EMU_PER_PT,
            align=_first_attr(chain, "algn") or "l",
            sized=sized,
        )

    def _style(self, chain: list) -> _RunStyle:
        size = _int(_first_attr(chain, "sz"), None)
        bold = _bool_attr(_first_attr(chain, "b"))
        italic = _bool_attr(_first_attr(chain, "i"))
        lang = _first_attr(chain, "lang") or ""
        alt = _first_attr(chain, "altLang") or ""
        theme = self.ctx.theme
        latin = _first_child(chain, "latin")
        ea = _first_child(chain, "ea")
        ref = self.font_ref
        latin_name = latin.get("typeface") if latin is not None else (f"+{ref}-lt" if ref else "+mn-lt")
        ea_name = ea.get("typeface") if ea is not None else (f"+{ref}-ea" if ref else "+mn-ea")
        ea_lang = lang if _is_cjk_lang(lang) else alt
        baseline = _int(_first_attr(chain, "baseline"), 0) or 0
        return _RunStyle(
            size=(size / 100) if size else _DEFAULT_SIZE_PT,
            bold=bool(bold) if bold is not None else self.bold_override,
            italic=bool(italic),
            latin=theme.resolve(latin_name, lang),
            ea=theme.resolve(ea_name, ea_lang),
            caps=_first_attr(chain, "cap") == "all",
            spacing=(_int(_first_attr(chain, "spc"), 0) or 0) / 100,
            script=2 / 3 if baseline else 1.0,
            cjk_lang=_is_cjk_lang(lang) or (not lang and _is_cjk_lang(alt)),
        )


# ---------------------------------------------------------------- 排版估算


Sizer = Callable[[float], float]


def autofit_sizer(font_scale: float) -> Sizer:
    """「溢出时缩排文字」：PowerPoint 按 round(字号 × fontScale) 的整磅画（GenOffice 实测）。"""
    if font_scale >= 1:
        return lambda size: size
    return lambda size: max(1.0, math.floor(size * font_scale + 0.5))


def explicit_sizer(scale: float) -> Sizer:
    """直接改 sz 时写进去的字号：按比例缩，向下取到半磅，不小于 1 磅。"""
    if scale >= 1:
        return lambda size: size
    return lambda size: max(1.0, math.floor(size * scale * 2) / 2)


@dataclass
class _Atom:
    kind: str  # word / space / tab / break
    widths: list[tuple[float, float, float]]  # 每个字：(字宽倍数, 原字号, 字间距)
    size: float  # 这一段里最大的原字号（算行高）


@dataclass(frozen=True)
class _Layout:
    height: float
    widest: float


def _atoms(para: _Para, measurer: _Measurer) -> list[_Atom]:
    atoms: list[_Atom] = []
    word: list[tuple[float, float, float]] = []
    word_size = 0.0
    glue_next = False

    def flush() -> None:
        nonlocal word, word_size
        if word:
            atoms.append(_Atom("word", word, word_size))
            word, word_size = [], 0.0

    for text, style in para.pieces:
        if style.caps:
            text = text.upper()
        size = style.size * style.script
        for char in text:
            if char == "\n":
                flush()
                atoms.append(_Atom("break", [], size))
                glue_next = False
                continue
            if char == "\t":
                flush()
                atoms.append(_Atom("tab", [], size))
                continue
            entry = (measurer.em(char, style), size, style.spacing)
            if char in _SPACES:
                flush()
                atoms.append(_Atom("space", [entry], size))
                glue_next = False
                continue
            if char in _NO_LINE_START and (word or (atoms and atoms[-1].kind == "word")):
                if not word:
                    previous = atoms.pop()
                    word, word_size = previous.widths, previous.size
                word.append(entry)
                word_size = max(word_size, size)
                continue
            standalone = is_wide(char) or para.latin_break
            if standalone and not glue_next:
                flush()
            word.append(entry)
            word_size = max(word_size, size)
            glue_next = char in _NO_LINE_END
            if (standalone or char in _BREAK_AFTER) and not glue_next:
                flush()
    flush()
    return atoms


def _atom_width(atom: _Atom, sizer: Sizer) -> float:
    return sum(em * sizer(size) + spacing for em, size, spacing in atom.widths)


class _Lines:
    """一段文字的折行：记每行的最大字号（缩放后）和最宽一行。"""

    def __init__(self, start: float) -> None:
        self.sizes: list[float] = []
        self.widest = 0.0
        self.x = self.content_x = start
        self.size = 0.0
        self.has_word = False

    def end(self, size_hint: float) -> None:
        self.sizes.append(self.size or size_hint)
        self.widest = max(self.widest, self.content_x)
        self.x = self.content_x = 0.0
        self.size = 0.0
        self.has_word = False

    def put(self, width: float, size: float) -> None:
        self.x += width
        self.content_x = self.x
        self.size = max(self.size, size)
        self.has_word = True


def _break_lines(para: _Para, atoms: list[_Atom], avail: float | None, sizer: Sizer) -> _Lines:
    """按可用宽度（磅，None = 不换行）贪心折行：西文按词，中日韩逐字，词比整行宽时按字硬断。"""
    if para.bullet_em:
        bullet_x = max(para.mar_l + para.indent, 0.0)
        bullet_w = para.bullet_em * para.bullet_scale * sizer(
            para.pieces[0][1].size if para.pieces else para.end_style.size)
        start = max(bullet_x + bullet_w - para.mar_l, 0.0)
    else:
        start = max(para.indent, -para.mar_l)
    lines = _Lines(start)
    limit = None if avail is None else avail + _FIT_EPSILON_PT
    for atom in atoms:
        size = sizer(atom.size)
        if atom.kind == "break":
            lines.end(size)
        elif atom.kind == "tab":
            lines.x = (math.floor(lines.x / para.tab) + 1) * para.tab if para.tab > 0 else lines.x
            if limit is not None and lines.x > limit and lines.has_word:
                lines.end(size)
                lines.x = para.tab
            lines.content_x = lines.x
            lines.size = max(lines.size, size)
        elif atom.kind == "space":
            lines.x += _atom_width(atom, sizer)
            lines.size = max(lines.size, size)
        else:
            width = _atom_width(atom, sizer)
            if limit is not None and lines.x + width > limit and lines.has_word:
                lines.end(size)
            if limit is not None and lines.x + width > limit:
                for em, char_size, spacing in atom.widths:
                    char_width = em * sizer(char_size) + spacing
                    if lines.x + char_width > limit and lines.x > 0:
                        lines.end(sizer(char_size))
                    lines.put(char_width, sizer(char_size))
            else:
                lines.put(width, size)
    lines.end(sizer(para.end_style.size * para.end_style.script))
    return lines


def _layout(paras: list[_Para], atoms_list: list[list[_Atom]], width: float | None, sizer: Sizer,
            reduction: float = 0.0, line_scale: float = 1.0, trim_edges: bool = False) -> _Layout:
    """按框宽（磅，None = 不换行）排一遍，返回总高度和最宽一行（含缩进）。"""
    height = 0.0
    widest = 0.0
    for index, (para, atoms) in enumerate(zip(paras, atoms_list)):
        avail = None if width is None else max(1.0, width - para.mar_l - para.mar_r)
        lines = _break_lines(para, atoms, avail, sizer)
        widest = max(widest, para.mar_l + lines.widest)
        single_first = PPT_SINGLE_LINE_EM * lines.sizes[0]
        for line in lines.sizes:
            if para.line_pts is not None:
                height += para.line_pts * line_scale
            else:
                height += PPT_SINGLE_LINE_EM * line * (para.line_pct or 1.0) * (1 - reduction)
        before_pct, before_pts = para.before
        after_pct, after_pts = para.after
        if index > 0:
            height += before_pts if before_pts is not None else (before_pct or 0.0) * single_first
        if not (trim_edges and index == len(paras) - 1):
            height += after_pts if after_pts is not None else (after_pct or 0.0) * single_first
    return _Layout(height, widest)


# ---------------------------------------------------------------- 写回


def _set_size(host, size_pt: float) -> None:
    """给 a:r / a:fld / a:br（或段落的 endParaRPr）写字号。"""
    if host.tag == _a("p"):
        rpr = host.find(_a("endParaRPr"))
        if rpr is None:
            rpr = etree.SubElement(host, _a("endParaRPr"))
    else:
        rpr = host.find(_a("rPr"))
        if rpr is None:
            rpr = etree.Element(_a("rPr"))
            host.insert(0, rpr)
    rpr.set("sz", str(round(size_pt * 100)))


def _apply_explicit(paras: list[_Para], tx_bodies: list, scale: float) -> None:
    """按比例写各片段的 sz；固定行距（磅值）不随字号变，一起缩，否则字小了行还是那么高。"""
    sizer = explicit_sizer(scale)
    for para in paras:
        for host, size in para.sized:
            _set_size(host, sizer(size))
    for tx_body, body_paras in tx_bodies:
        for paragraph, para in zip(tx_body.findall(_a("p")), body_paras):
            if para.line_pts is None:
                continue
            ppr = paragraph.find(_a("pPr"))
            if ppr is None:
                ppr = etree.Element(_a("pPr"))
                paragraph.insert(0, ppr)
            line = ppr.find(_a("lnSpc"))
            if line is None:
                line = etree.Element(_a("lnSpc"))
                ppr.insert(0, line)
            for child in list(line):
                line.remove(child)
            etree.SubElement(line, _a("spcPts")).set("val", str(max(100, round(para.line_pts * scale * 100))))


_AUTOFIT_TAGS = (_a("noAutofit"), _a("normAutofit"), _a("spAutoFit"))


def _write_norm_autofit(body_pr, font_scale: float, reduction: float) -> None:
    current = None
    for tag in _AUTOFIT_TAGS:
        current = body_pr.find(tag)
        if current is not None:
            break
    element = etree.Element(_a("normAutofit"))
    if font_scale < 1:
        element.set("fontScale", str(round(font_scale * 100000)))
    if reduction > 0:
        element.set("lnSpcReduction", str(round(reduction * 100000)))
    if current is not None:
        body_pr.replace(current, element)
    else:
        warp = body_pr.find(_a("prstTxWarp"))
        body_pr.insert(body_pr.index(warp) + 1 if warp is not None else 0, element)


def _ensure_xfrm(ctx: _PartContext, sp, rect: _Rect):
    """占位符没写自己的位置大小时，照继承来的写一份再改。"""
    xfrm = _xfrm_of(sp)
    if xfrm is not None and xfrm.find(_a("off")) is not None and xfrm.find(_a("ext")) is not None:
        return xfrm
    sp_pr = sp.find(_p("spPr"))
    if sp_pr is None:
        sp_pr = etree.Element(_p("spPr"))
        nv = sp.find(_p("nvSpPr"))
        sp.insert(sp.index(nv) + 1 if nv is not None else 0, sp_pr)
    if xfrm is None:
        xfrm = etree.Element(_a("xfrm"))
        sp_pr.insert(0, xfrm)
    for child in list(xfrm):
        xfrm.remove(child)
    etree.SubElement(xfrm, _a("off"), x=str(int(rect.x)), y=str(int(rect.y)))
    etree.SubElement(xfrm, _a("ext"), cx=str(int(rect.cx)), cy=str(int(rect.cy)))
    return xfrm


def _resize(ctx: _PartContext, sp, rect: _Rect, *, dx_left: float = 0, dx_right: float = 0, dy: float = 0) -> None:
    xfrm = _ensure_xfrm(ctx, sp, rect)
    off, ext = xfrm.find(_a("off")), xfrm.find(_a("ext"))
    if dx_left:
        off.set("x", str(math.floor(_int(off.get("x"), 0) - dx_left)))
    if dx_left or dx_right:
        ext.set("cx", str(math.ceil(_int(ext.get("cx"), 0) + dx_left + dx_right)))
    if dy:
        ext.set("cy", str(math.ceil(_int(ext.get("cy"), 0) + dy)))


# ---------------------------------------------------------------- 文本框


@dataclass
class _Body:
    kind: str  # norm / sp / none
    wrap: bool
    vert: str | None
    insets: dict[str, int]
    columns: int
    column_gap: int
    warp: str | None


def _resolve_body(ctx: _PartContext, sp, tx_body) -> _Body:
    chain = [tx_body.find(_a("bodyPr"))]
    chain += [shape.find(f"{_p('txBody')}/{_a('bodyPr')}") for shape in ctx.inherited_shapes(sp)]
    kind = "none"
    for body_pr in chain:
        if body_pr is None:
            continue
        found = next((tag for tag in _AUTOFIT_TAGS if body_pr.find(tag) is not None), None)
        if found is not None:
            kind = {_a("noAutofit"): "none", _a("normAutofit"): "norm", _a("spAutoFit"): "sp"}[found]
            break
    warp_el = chain[0].find(_a("prstTxWarp")) if chain[0] is not None else None
    return _Body(
        kind=kind,
        wrap=_first_attr(chain, "wrap") != "none",
        vert=_first_attr(chain, "vert"),
        insets={name: _int(_first_attr(chain, name), default) for name, default in _DEFAULT_INSETS.items()},
        columns=max(1, _int(_first_attr(chain, "numCol"), 1) or 1),
        column_gap=_int(_first_attr(chain, "spcCol"), 0) or 0,
        warp=warp_el.get("prst") if warp_el is not None else None,
    )


def _font_ref(sp) -> str | None:
    ref = sp.find(f"{_p('style')}/{_a('fontRef')}")
    if ref is None:
        return None
    return {"major": "mj", "minor": "mn"}.get(ref.get("idx") or "")


def _measure_shape(ctx: _PartContext, sp, tx_body) -> tuple[list[_Para], list[list[_Atom]]]:
    reader = _TextBodyReader(ctx, ctx.style_layers(sp), font_ref=_font_ref(sp))
    paras = reader.paragraphs(tx_body)
    return paras, [_atoms(para, ctx.measurer) for para in paras]


def _fit_shape(ctx: _PartContext, tx_body) -> str | None:
    """调整一个文本框；放下了返回 None，否则返回进需复核清单的原因。"""
    sp = tx_body.getparent()
    if sp is None or sp.tag != _p("sp"):
        return None
    body = _resolve_body(ctx, sp, tx_body)
    if body.vert in _SKIPPED_VERT or body.warp not in _PLAIN_WARPS:
        return None
    local = _shape_rect(ctx, sp)
    if local is None or local.cx <= 0 or local.cy <= 0:
        return None
    groups = _group_chain(sp)
    rect, _sx, _sy = _to_slide(local, groups)
    paras, atoms = _measure_shape(ctx, sp, tx_body)
    if not any(text.strip() for para in paras for text, _style in para.pieces):
        return None

    swapped = body.vert in _SWAPPED_VERT
    box_w = (rect.cy if swapped else rect.cx) / EMU_PER_PT
    box_h = (rect.cx if swapped else rect.cy) / EMU_PER_PT
    inner_w = max(1.0, box_w - (body.insets["lIns"] + body.insets["rIns"]) / EMU_PER_PT)
    inner_h = max(1.0, box_h - (body.insets["tIns"] + body.insets["bIns"]) / EMU_PER_PT)
    column_w = max(1.0, (inner_w - (body.columns - 1) * body.column_gap / EMU_PER_PT) / body.columns)
    capacity = inner_h * body.columns
    movable = not groups and not swapped and not _rotated(sp) and body.columns == 1
    can_grow = movable and ctx.layout_mode != LAYOUT_FIXED

    def attempt(wrap: bool, apply: bool) -> tuple[str | None, float]:
        """按 wrap（是否在框宽内折行）算一遍；apply 为假时只算不改。

        返回（进复核的原因，放下了为 None；最后那份排版冒出框外多少磅，往下和往旁边相加）。
        """
        width = column_w if wrap else None
        before = _original_layout(ctx, sp, width)
        room_w = max(inner_w, before.widest if before else 0.0)
        tolerated_h = before.height if before else 0.0

        def excess(layout: _Layout, height: float) -> float:
            return _excess(layout, room_w if not wrap else None, height)

        if ctx.layout_mode == LAYOUT_KEEP_SIZE:
            reason = _keep_size(ctx, sp, tx_body, rect, body, paras, atoms, inner_w, inner_h,
                                wrap=wrap, movable=movable, tolerated_h=tolerated_h, apply=apply)
            return reason, 0.0

        if body.kind == "norm":
            layout = None
            chosen = NORM_AUTOFIT_STEPS[-1]
            for font_scale, reduction in NORM_AUTOFIT_STEPS:
                layout = _layout(paras, atoms, width, autofit_sizer(font_scale), reduction)
                if not excess(layout, capacity):
                    chosen = (font_scale, reduction)
                    break
            if apply:
                _write_norm_autofit(tx_body.find(_a("bodyPr")), *chosen)
            over = excess(layout, capacity)
            if not over:
                return None, 0.0
            grown = can_grow and wrap and _grow_down(ctx, sp, rect, layout.height - inner_h, apply=apply)
            return (None, 0.0) if grown else (REASON_OVERFLOW, over)

        if can_grow:
            over = _fit_auto_resize(ctx, sp, tx_body, rect, body, paras, atoms, inner_w, inner_h,
                                    tolerated_h, wrap=wrap, apply=apply)
            return (REASON_OVERFLOW if over else None), over

        room_h = max(capacity, tolerated_h)
        scale, layout = _explicit_scale(paras, atoms, width, lambda lay: not excess(lay, room_h))
        if apply and scale < 1:
            _apply_explicit(paras, [(tx_body, paras)], scale)
        over = excess(layout, room_h)
        return (REASON_OVERFLOW if over else None), over

    if body.wrap:
        return attempt(True, apply=True)[0]
    unwrapped, unwrapped_over = attempt(False, apply=False)
    if unwrapped is None:
        return attempt(False, apply=True)[0]
    # 「不换行」的框加宽、缩到最小都放不下时智能换行（CAD 同一做法）：改成在框宽内折行。
    # 折行后往下冒出的比一行往旁边冒出的还多时保留一行；保持字号模式一律折行（至少不横着出页）。
    # 折行后仍没完全放下的照样进复核。
    wrapped, wrapped_over = attempt(True, apply=False)
    if wrapped is None or ctx.layout_mode == LAYOUT_KEEP_SIZE or wrapped_over < unwrapped_over:
        tx_body.find(_a("bodyPr")).set("wrap", "square")
        return attempt(True, apply=True)[0]
    return attempt(False, apply=True)[0]


def _excess(layout: _Layout, room_w: float | None, room_h: float) -> float:
    """排版冒出 room_w × room_h 多少磅（往下 + 往旁边；room_w 为 None 表示折行、不算宽）；
    放得下为 0。"""
    over = max(0.0, layout.height - room_h - _FIT_EPSILON_PT)
    if room_w is not None:
        over += max(0.0, layout.widest - room_w - _FIT_EPSILON_PT)
    return over


def _original_layout(ctx: _PartContext, sp, width: float | None) -> _Layout | None:
    """原文在同一个框里的排版（写回前那份部件里同 id 的形状），读不到返回 None。"""
    shape_id = _shape_id(sp)
    original = ctx.original_shapes.get(shape_id) if shape_id else None
    tx_body = original.find(_p("txBody")) if original is not None else None
    if tx_body is None:
        return None
    paras, atoms = _measure_shape(ctx, original, tx_body)
    return _layout(paras, atoms, width, autofit_sizer(1.0))


def _explicit_scale(paras, atoms, width, accept: Callable[[_Layout], bool],
                    trim_edges: bool = False) -> tuple[float, _Layout]:
    scale = 1.0
    while True:
        layout = _layout(paras, atoms, width, explicit_sizer(scale), line_scale=scale, trim_edges=trim_edges)
        if accept(layout) or scale <= MIN_SCALE + 1e-9:
            return scale, layout
        scale = max(MIN_SCALE, round(scale - EXPLICIT_SCALE_STEP, 2))


def _grow_down(ctx: _PartContext, sp, rect: _Rect, extra_pt: float, *, apply: bool = True) -> bool:
    """框往下加高 extra_pt 磅（只长进下方空白）；放下了返回 True，空白不够不动、返回 False。"""
    if extra_pt <= _FIT_EPSILON_PT:
        return True
    extra = extra_pt * EMU_PER_PT
    if _room_below(ctx, sp, rect, ctx.gap) + 1 < extra:
        return False
    if apply:
        _resize(ctx, sp, rect, dy=extra)
    return True


def _widen(align: str, need: float, left: float, right: float) -> tuple[float, float]:
    """不换行的框要加宽 need（EMU）：居中两边各一半，右对齐往左，其余往右；不超过两边的空。"""
    if align == "ctr":
        half = min(need / 2, left, right)
        return half, half
    if align == "r":
        return min(need, left), 0.0
    return 0.0, min(need, right)


def _fit_auto_resize(ctx, sp, tx_body, rect, body, paras, atoms, inner_w, inner_h, tolerated_h=0.0,
                     *, wrap: bool, apply: bool = True) -> float:
    """先往空白处长（不换行的先加宽），长不下再缩字号；返回最后冒出多少磅，放下了为 0。

    tolerated_h 是原文在这个框里排出的高度：原文本来就超出框，译文排到同样高度也算放下。
    """
    width = inner_w if wrap else None
    full = _layout(paras, atoms, width, explicit_sizer(1.0))
    dx_left = dx_right = 0.0
    room_w = inner_w
    if not wrap and full.widest > inner_w + _FIT_EPSILON_PT:
        need = (full.widest - inner_w) * EMU_PER_PT
        dx_left, dx_right = _widen(paras[0].align if paras else "l", need, *_room_sideways(ctx, sp, rect, ctx.gap))
        room_w = inner_w + (dx_left + dx_right) / EMU_PER_PT
    grown = _Rect(rect.x - dx_left, rect.y, rect.cx + dx_left + dx_right, rect.cy)
    grow_h = inner_h + _room_below(ctx, sp, grown, ctx.gap) / EMU_PER_PT
    room_h = max(grow_h, tolerated_h)

    def over(layout: _Layout) -> float:
        return _excess(layout, None if wrap else room_w, room_h)

    scale, layout = _explicit_scale(paras, atoms, width, lambda lay: not over(lay))
    if not apply:
        return over(layout)
    if scale < 1:
        _apply_explicit(paras, [(tx_body, paras)], scale)
    if not wrap:
        used = max(0.0, layout.widest - inner_w) * EMU_PER_PT
        if dx_left and dx_right:
            dx_left = dx_right = min(dx_left, used / 2)
        elif dx_left:
            dx_left = min(dx_left, used)
        else:
            dx_right = min(dx_right, used)
    dy = max(0.0, min(layout.height, grow_h) - inner_h) * EMU_PER_PT
    if dx_left or dx_right or dy:
        _resize(ctx, sp, rect, dx_left=dx_left, dx_right=dx_right, dy=dy)
    return over(layout)


def _keep_size(ctx, sp, tx_body, rect, body, paras, atoms, inner_w, inner_h, *, wrap: bool, movable: bool,
               tolerated_h: float, apply: bool) -> str | None:
    """「保持字号」：字号不动，框按需要往下长（不换行的先在页面内按对齐加宽），不管下面有没有东西。

    放得下返回 None；长了以后盖住别的形状或超出页面、或者框不能加高（组合、旋转、竖排、
    多栏）时返回原因，进复核清单。
    """
    sizer = autofit_sizer(1.0) if body.kind == "norm" else explicit_sizer(1.0)
    if apply and body.kind == "norm":
        _write_norm_autofit(tx_body.find(_a("bodyPr")), 1.0, 0.0)
    layout = _layout(paras, atoms, inner_w if wrap else None, sizer)
    dx_left = dx_right = 0.0
    if not wrap and layout.widest > inner_w + _FIT_EPSILON_PT:
        if not movable:
            return REASON_KEEP_OVERFLOW
        need = (layout.widest - inner_w) * EMU_PER_PT
        dx_left, dx_right = _widen(paras[0].align if paras else "l", need, rect.x, float(ctx.slide_cx) - rect.right)
        if dx_left + dx_right + 1 < need:
            return REASON_KEEP_OFF_PAGE
    dy = 0.0
    if layout.height > max(inner_h, tolerated_h) + _FIT_EPSILON_PT:
        if not movable:
            return REASON_KEEP_OVERFLOW
        dy = (layout.height - inner_h) * EMU_PER_PT
    if not (dx_left or dx_right or dy):
        return None
    widened = _Rect(rect.x - dx_left, rect.y, rect.cx + dx_left + dx_right, rect.cy)
    side_left, side_right = _room_sideways(ctx, sp, rect)
    covers = dx_left > side_left + 1 or dx_right > side_right + 1 or _room_below(ctx, sp, widened) + 1 < dy
    if apply:
        _resize(ctx, sp, rect, dx_left=dx_left, dx_right=dx_right, dy=dy)
    if widened.bottom + dy > ctx.slide_cy + 1:
        return REASON_KEEP_OFF_PAGE
    return REASON_KEEP_COVERS if covers else None


# ---------------------------------------------------------------- 表格


@dataclass
class _Cell:
    tx_body: Any
    row: int
    rows: int
    width_pt: float
    margins: dict[str, int]
    paras: list[_Para]
    atoms: list[list[_Atom]]


def _table_cells(ctx: _PartContext, tbl, sx: float) -> tuple[list[_Cell], list[float]]:
    columns = [(_int(col.get("w"), 0) or 0) * sx / EMU_PER_PT for col in tbl.findall(f"{_a('tblGrid')}/{_a('gridCol')}")]
    rows = tbl.findall(_a("tr"))
    heights = [(_int(tr.get("h"), 0) or 0) / EMU_PER_PT for tr in rows]
    bold = ctx.table_bold(tbl)
    cells: list[_Cell] = []
    for r, tr in enumerate(rows):
        c = 0
        for tc in tr.findall(_a("tc")):
            span = max(1, _int(tc.get("gridSpan"), 1) or 1)
            merged = tc.get("hMerge") in ("1", "true") or tc.get("vMerge") in ("1", "true")
            tx_body = tc.find(_a("txBody"))
            if not merged and tx_body is not None:
                tc_pr = tc.find(_a("tcPr"))
                margins = {name: _int(tc_pr.get(name) if tc_pr is not None else None, default)
                           for name, default in _DEFAULT_CELL_MARGINS.items()}
                width = sum(columns[c:c + span]) - (margins["marL"] + margins["marR"]) / EMU_PER_PT
                reader = _TextBodyReader(ctx, [ctx.default_layer],
                                         bold_override=bold(r, c, len(rows), len(columns)))
                paras = reader.paragraphs(tx_body)
                cells.append(_Cell(tx_body, r, max(1, _int(tc.get("rowSpan"), 1) or 1), max(1.0, width),
                                   margins, paras, [_atoms(p, ctx.measurer) for p in paras]))
            c += span
    return cells, heights


def _table_height(cells: list[_Cell], heights: list[float], scale: float) -> float:
    rows = list(heights)
    needs = []
    for cell in cells:
        layout = _layout(cell.paras, cell.atoms, cell.width_pt, explicit_sizer(scale), line_scale=scale,
                         trim_edges=True)
        need = layout.height + (cell.margins["marT"] + cell.margins["marB"]) / EMU_PER_PT
        needs.append((cell, need))
        if cell.rows == 1 and cell.row < len(rows):
            rows[cell.row] = max(rows[cell.row], need)
    for cell, need in needs:
        if cell.rows > 1 and cell.row < len(rows):
            last = min(len(rows), cell.row + cell.rows) - 1
            deficit = need - sum(rows[cell.row:last + 1])
            if deficit > 0:
                rows[last] += deficit
    return sum(rows)


def _fit_table(ctx: _PartContext, frame) -> str | None:
    tbl = frame.find(f".//{_a('tbl')}")
    rect = _xfrm_rect(_xfrm_of(frame))
    if tbl is None or rect is None:
        return None
    groups = _group_chain(frame)
    slide_rect, sx, _sy = _to_slide(rect, groups)
    cells, heights = _table_cells(ctx, tbl, sx)
    if not cells:
        return None
    # 表格的行跟着字撑高，和「根据文字调整形状大小」一个意思：先长进下方空白（不出页面、
    # 不压到别的形状），放不下再整张表一起缩字号，保持字号统一。「固定版面」时不长，
    # 缩到原来的行高里（PowerPoint 照样会撑高放不下的行，所以缩不下的进复核）。
    # 「保持字号」时字号不动，行照样撑高，长出来的部分盖住别的形状或出页面的进复核。
    rows = sum(heights)
    original = ctx.original_shapes.get(_shape_id(frame) or "")
    original_tbl = original.find(f".//{_a('tbl')}") if original is not None else None
    original_need = 0.0
    if original_tbl is not None:
        original_cells, original_heights = _table_cells(ctx, original_tbl, sx)
        original_need = _table_height(original_cells, original_heights, 1.0)
    room = _room_below(ctx, frame, slide_rect, ctx.gap) / EMU_PER_PT

    if ctx.layout_mode == LAYOUT_KEEP_SIZE:
        extra = _table_height(cells, heights, 1.0) - max(rows, original_need)
        if extra <= _FIT_EPSILON_PT:
            return None
        if slide_rect.bottom + extra * EMU_PER_PT > ctx.slide_cy + 1:
            return REASON_KEEP_OFF_PAGE
        return REASON_KEEP_COVERS if room + _FIT_EPSILON_PT < extra else None

    capacity = rows
    if ctx.layout_mode != LAYOUT_FIXED and not groups and not _rotated(frame):
        capacity += room
    capacity = max(capacity, original_need)
    scale = 1.0
    while True:
        height = _table_height(cells, heights, scale)
        if height <= capacity + _FIT_EPSILON_PT or scale <= MIN_SCALE + 1e-9:
            break
        scale = max(MIN_SCALE, round(scale - EXPLICIT_SCALE_STEP, 2))
    if scale < 1:
        all_paras = [para for cell in cells for para in cell.paras]
        _apply_explicit(all_paras, [(cell.tx_body, cell.paras) for cell in cells], scale)
    return None if height <= capacity + _FIT_EPSILON_PT else REASON_OVERFLOW
