"""PPT（.pptx、.pptm、.ppsx、.potx）取字与写回。

随应用发布的「PPT」类型（``adapters/pptx/``）的全部实际工作在这里，适配器本身只是一层
薄壳。直接用 lxml 改包里的 XML（不用 python-pptx：它读不到 ``mc:AlternateContent``
里的形状，保存时会重写载入的所有部件）；包的读写用 ``core/opc_package.OpcPackage``，
没改过的部件按原字节照抄。

**取哪些字、按什么顺序**

- 幻灯片按 ``presentation.xml`` 的 ``sldIdLst`` 经关系文件取（不按文件名排序），每页依次是
  形状树里的文字（文本框、形状、组合里的形状、表格单元格；``mc:AlternateContent`` 取
  ``mc:Choice``），然后是这一页的演讲者备注（备注页的正文占位符）。
- 之后是母版和版式上**不是占位符**的形状里的文字（每页都有的公司名、「内部资料」）；
  占位符上的「单击此处编辑标题」只是编辑时的提示，不翻。
- 一个 ``a:p`` 一个单元。``a:br`` 在文字里写成换行；页码、日期这类自动字段（``a:fld``）的
  文字算在原文里、列为受保护片段，写回时放回原来的字段元素。段内格式用 ``<gN>`` 标记
  （``core/drawingml_marks.py``，与 Word 同一套）。
- 每页形状之后、备注之前是这一页的图表和 SmartArt（``core/pptx_charts.py``、
  ``core/pptx_smartart.py``，各自的部件里取字和写回）。
- 图片、嵌入对象、批注里的文字不翻，扫描时报数；认不出的图表（Office 2016 起的瀑布图、
  旭日图等新图表）和缺部件的 SmartArt 也报数。

**写回**

- 仅译文：段数不变；片段数和原来一样时只换文字，否则按标记（或整段写进主体格式）重建，
  重建的片段沿用原片段的 ``a:rPr``（子元素顺序不变）、字段保留原 id、``a:endParaRPr`` 保留。
- 双语对照：原段后面插一段译文（复制原段的段落格式，加 ``a:buNone``，有项目符号时
  首行缩进归零，与原文文字对齐）；按段落出现的动画（``p:txEl/p:pRg``）改成覆盖
  「原段 + 译文段」；自动编号的原段写明 ``startAt``，编号不因中间插了译文段而错。
- 译文片段的 ``lang`` 改成目标语言；目标语言不是中日韩时段落写 ``latinLnBrk="0"``
  （为 1 时西文单词会被从中间断开）。字体不换。
- 竖排文本框里写进西文的列入需复核。
- 每个改过文字的部件写回后调一次 ``fit_after_write``（放不下时缩字，``core/pptx_fit.py``）。
- 保存后验收：包里的条目和原来一样、没改的部件逐字节相同、改过的部件能解析、形状数
  不变、没动过的形状位置尺寸不变、段数符合预期、没有残留标记（图表和 SmartArt 的部件
  各按自己的规则核对）。不通过就删掉输出文件、报这个文件失败，不留半成品。
"""

from __future__ import annotations

import re
import shutil
import zipfile
from collections.abc import Callable, Iterator, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lxml import etree

from core.drawingml_marks import (
    A_END_PARA_RPR,
    A_P,
    A_PPR,
    A_RPR,
    A_T,
    NS_A,
    ParagraphMarks,
    has_marks,
    paragraph_marks,
    read_pieces,
    write_paragraph,
)
from core.opc_package import OpcPackage, PackageVerificationError, rels_path, resolve_part
from core.pptx_fit import DEFAULT_GAP_MM, LAYOUT_FIT, FitIssue, fit_after_write

NS_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
NS_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"

_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
REL_OFFICE_DOCUMENT = _REL + "officeDocument"
REL_SLIDE = _REL + "slide"
REL_SLIDE_LAYOUT = _REL + "slideLayout"
REL_SLIDE_MASTER = _REL + "slideMaster"
REL_NOTES_SLIDE = _REL + "notesSlide"
REL_COMMENTS = _REL + "comments"
REL_MODERN_COMMENTS = "http://schemas.microsoft.com/office/2018/10/relationships/comments"

URI_TABLE = "http://schemas.openxmlformats.org/drawingml/2006/table"
URI_CHART = "http://schemas.openxmlformats.org/drawingml/2006/chart"
URI_CHART_EX = "http://schemas.microsoft.com/office/drawing/2014/chartex"
URI_DIAGRAM = "http://schemas.openxmlformats.org/drawingml/2006/diagram"
URI_OLE = "http://schemas.openxmlformats.org/presentationml/2006/ole"


def _p(tag: str) -> str:
    return f"{{{NS_P}}}{tag}"


def _a(tag: str) -> str:
    return f"{{{NS_A}}}{tag}"


def _r(tag: str) -> str:
    return f"{{{NS_R}}}{tag}"


P_SP = _p("sp")
P_GRP_SP = _p("grpSp")
P_GRAPHIC_FRAME = _p("graphicFrame")
P_PIC = _p("pic")
P_CXN_SP = _p("cxnSp")
P_CONTENT_PART = _p("contentPart")
P_TX_BODY = _p("txBody")
MC_ALTERNATE = f"{{{NS_MC}}}AlternateContent"
MC_CHOICE = f"{{{NS_MC}}}Choice"
MC_FALLBACK = f"{{{NS_MC}}}Fallback"
A_TX_BODY = _a("txBody")
A_BODY_PR = _a("bodyPr")
A_LST_STYLE = _a("lstStyle")

_SHAPE_TAGS = frozenset({P_SP, P_GRP_SP, P_GRAPHIC_FRAME, P_PIC, P_CXN_SP, P_CONTENT_PART})

#: 用户看得到的占位符名（需复核清单里的「第 3 页 · 标题」）；其余占位符和普通形状用形状名。
_PLACEHOLDER_LABELS = {
    "title": "标题",
    "ctrTitle": "标题",
    "subTitle": "副标题",
    "dt": "日期",
    "ftr": "页脚",
    "hdr": "页眉",
    "sldNum": "页码",
}
_TITLE_TYPES = frozenset({"title", "ctrTitle"})
#: 译成西文后在这几种竖排里不能正常显示（西文横躺或一个字母一行）。
_VERTICAL_REVIEW = frozenset({"eaVert", "wordArtVert", "wordArtVertRtl", "mongolianVert"})
_BULLET_TAGS = frozenset({_a("buChar"), _a("buAutoNum"), _a("buBlip")})
_BULLET_ALL = _BULLET_TAGS | {_a("buNone")}
#: ``a:pPr`` 子元素的规范顺序（插 ``a:buNone`` 时按它找位置；顺序错了 PowerPoint 会丢弃）。
_PPR_ORDER = (
    "lnSpc", "spcBef", "spcAft", "buClrTx", "buClr", "buSzTx", "buSzPct", "buSzPts", "buFontTx", "buFont",
    "buNone", "buAutoNum", "buChar", "buBlip", "tabLst", "defRPr", "extLst",
)

#: 设置里的目标语言代码 → 写进 ``a:rPr lang`` 的语言标记。不在表里的（自定义语言）不改 lang。
TARGET_LANG_TAGS = {
    "zh": "zh-CN", "en": "en-US", "fr": "fr-FR", "ar": "ar-SA", "vi": "vi-VN", "km": "km-KH", "es": "es-ES",
    "pt": "pt-PT", "de": "de-DE", "it": "it-IT", "ru": "ru-RU", "ja": "ja-JP", "ko": "ko-KR", "th": "th-TH",
    "id": "id-ID", "ms": "ms-MY", "tl": "fil-PH", "hi": "hi-IN", "bn": "bn-IN", "ur": "ur-PK", "fa": "fa-IR",
    "tr": "tr-TR", "he": "he-IL", "el": "el-GR", "pl": "pl-PL", "nl": "nl-NL", "sv": "sv-SE", "da": "da-DK",
    "no": "nb-NO", "fi": "fi-FI", "cs": "cs-CZ", "sk": "sk-SK", "sl": "sl-SI", "hu": "hu-HU", "ro": "ro-RO",
    "bg": "bg-BG", "uk": "uk-UA", "sr": "sr-Latn-RS", "hr": "hr-HR", "lt": "lt-LT", "lv": "lv-LV", "et": "et-EE",
    "sw": "sw-KE", "am": "am-ET", "ta": "ta-IN", "te": "te-IN", "ml": "ml-IN", "kn": "kn-IN", "mr": "mr-IN",
    "gu": "gu-IN", "pa": "pa-IN", "ne": "ne-NP", "si": "si-LK", "my": "my-MM", "lo": "lo-LA", "mn": "mn-MN",
    "kk": "kk-KZ", "uz": "uz-Latn-UZ", "az": "az-Latn-AZ",
}
_CJK_LANGS = frozenset({"zh", "ja", "ko"})

_URL_RE = re.compile(r"(?:https?|ftp)://[^\s<>\"'`　-〿一-鿿＀-￯]+")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_URL_TRAILING = ".,;:!?)]}'\""
_RESIDUAL_RE = re.compile(r"</?\s*g\s*\d+\s*>|⟦\s*\d+\s*⟧", re.IGNORECASE)


class PptxError(ValueError):
    """读不了或写不出这个文件；消息是给用户看的一句话。"""


# ── 读包 ────────────────────────────────────────────────────────────────────


def _parse(data: bytes):
    return etree.fromstring(data, parser=etree.XMLParser(huge_tree=True, remove_blank_text=False))


def _serialize(root) -> bytes:
    return etree.tostring(root.getroottree(), xml_declaration=True, encoding="UTF-8", standalone=True)


@dataclass
class _Rel:
    id: str
    type: str
    target: str
    external: bool


class _Deck:
    """一个打开的演示文稿：部件、关系、幻灯片和母版的顺序。"""

    def __init__(self, package: OpcPackage) -> None:
        self.package = package
        self._roots: dict[str, object] = {}
        self._rels: dict[str, list[_Rel]] = {}
        main = next((rel for rel in self.rels("") if rel.type == REL_OFFICE_DOCUMENT), None)
        if main is None or not package.has(main.target):
            raise PptxError("文件里没有找到演示文稿内容，可能不是 PowerPoint 文件。")
        self.main = main.target
        presentation = self.root(self.main)
        if presentation is None or etree.QName(presentation).localname != "presentation":
            raise PptxError("文件里没有找到演示文稿内容，可能不是 PowerPoint 文件。")
        by_id = {rel.id: rel for rel in self.rels(self.main)}
        self.slides = self._listed(presentation, "sldIdLst", "sldId", by_id, REL_SLIDE)
        self.masters = self._listed(presentation, "sldMasterIdLst", "sldMasterId", by_id, REL_SLIDE_MASTER)

    def _listed(self, root, list_tag: str, item_tag: str, by_id: Mapping[str, _Rel], rel_type: str) -> list[str]:
        container = root.find(_p(list_tag))
        parts: list[str] = []
        if container is None:
            return parts
        for item in container.findall(_p(item_tag)):
            rel = by_id.get(str(item.get(_r("id")) or ""))
            if rel is not None and rel.type == rel_type and not rel.external and self.package.has(rel.target):
                parts.append(rel.target)
        return parts

    def rels(self, part: str) -> list[_Rel]:
        if part in self._rels:
            return self._rels[part]
        data = self.package.read(rels_path(part) if part else "_rels/.rels")
        found: list[_Rel] = []
        if data:
            try:
                root = _parse(data)
            except etree.XMLSyntaxError:
                root = None
            for item in root if root is not None else ():
                if not isinstance(item.tag, str) or etree.QName(item).localname != "Relationship":
                    continue
                external = str(item.get("TargetMode") or "") == "External"
                target = str(item.get("Target") or "")
                found.append(
                    _Rel(
                        id=str(item.get("Id") or ""),
                        type=str(item.get("Type") or ""),
                        target=target if external else resolve_part(part, target),
                        external=external,
                    )
                )
        self._rels[part] = found
        return found

    def related(self, part: str, rel_type: str) -> list[str]:
        return [rel.target for rel in self.rels(part) if rel.type == rel_type and not rel.external]

    def root(self, part: str):
        if part not in self._roots:
            data = self.package.read(part)
            try:
                self._roots[part] = _parse(data) if data else None
            except etree.XMLSyntaxError as exc:
                raise PptxError("文件内容已损坏，无法读取。") from exc
        return self._roots[part]

    def layouts(self, master: str) -> list[str]:
        root = self.root(master)
        if root is None:
            return []
        by_id = {rel.id: rel for rel in self.rels(master)}
        return self._listed(root, "sldLayoutIdLst", "sldLayoutId", by_id, REL_SLIDE_LAYOUT)


class _PptPackage(OpcPackage):
    kind = "PPT"


def _open(path: Path) -> OpcPackage:
    try:
        return _PptPackage(path)
    except zipfile.BadZipFile as exc:
        with path.open("rb") as handle:
            head = handle.read(8)
        if head.startswith(b"\xd0\xcf\x11\xe0"):
            raise PptxError("此文件已加密，或实际是旧版 .ppt 格式，请在 PowerPoint 里另存为 .pptx 后再试。") from exc
        raise PptxError("文件已损坏或不是 PowerPoint 文件，无法读取。") from exc


# ── 形状树里的文字 ──────────────────────────────────────────────────────────


@dataclass
class _Slot:
    """一段要翻译的文字和它在文件里的位置。"""

    unit_id: str
    context: str
    kind: str
    marks: ParagraphMarks
    #: 段落所在的 ``a:txBody`` / ``p:txBody``。
    body: object
    #: 交给缩字的元素：形状的文字框，表格是整个 ``p:graphicFrame``。
    owner: object
    #: 形状在需复核清单里的位置（不带「第 N 段」）。
    owner_context: str
    #: 文字框的竖排方式（沿占位符继承）。
    vertical: str = "horz"
    #: 段落带项目符号（自己写的或沿占位符继承的）。
    bulleted: bool = False
    #: 形状 id（动画按它找段落）；表格单元格为空。
    shape_id: str = ""
    #: ``mc:Fallback`` 里同一段文字的副本，写回时同步。
    mirrors: list[ParagraphMarks] = field(default_factory=list)
    #: ``mc:Fallback`` 里对应的 ``owner``（有副本时）：缩字改过的字号、大小随后同步过去。
    twin_owner: object | None = None

    @property
    def paragraph(self):
        return self.marks.paragraph


@dataclass
class _Scope:
    """正在遍历的部件：在哪一页、什么部件、它的版式和母版。"""

    part: str
    label: str
    kind: str  # slide / notes / layout / master / graphic
    layout: object | None = None
    master: object | None = None
    #: 图表或 SmartArt（``kind == "graphic"``）：自己取字、写回、核对，不走段落槽位。
    graphic: GraphicText | None = None


def _ph(shape) -> tuple[str, str] | None:
    """占位符的（type, idx）；不是占位符返回 None。没写 type 的按 obj。"""
    for nv in shape:
        if not isinstance(nv.tag, str) or not etree.QName(nv).localname.startswith("nv"):
            continue
        nv_pr = nv.find(_p("nvPr"))
        ph = nv_pr.find(_p("ph")) if nv_pr is not None else None
        if ph is not None:
            return str(ph.get("type") or "obj"), str(ph.get("idx") or "0")
    return None


def _c_nv_pr(shape):
    for nv in shape:
        if isinstance(nv.tag, str) and etree.QName(nv).localname.startswith("nv"):
            item = nv.find(_p("cNvPr"))
            if item is not None:
                return item
    return None


def _shape_name(shape) -> str:
    item = _c_nv_pr(shape)
    name = " ".join(str(item.get("name") or "").split()) if item is not None else ""
    if name:
        return name
    return f"形状 {item.get('id')}" if item is not None and item.get("id") else "形状"


def _shape_id(shape) -> str:
    item = _c_nv_pr(shape)
    return str(item.get("id") or "") if item is not None else ""


def _find_placeholder(root, ph_type: str, idx: str):
    """版式或母版里对应的占位符：先按 idx，再按类型（标题类、正文类各自通用）。"""
    if root is None:
        return None
    candidates = [
        (shape, info)
        for shape in root.iter(P_SP)
        if (info := _ph(shape)) is not None
    ]
    if idx != "0":
        for shape, (_kind, other_idx) in candidates:
            if other_idx == idx:
                return shape
    family = _ph_family(ph_type)
    for shape, (kind, _other_idx) in candidates:
        if kind == ph_type:
            return shape
    for shape, (kind, _other_idx) in candidates:
        if _ph_family(kind) == family:
            return shape
    return None


def _ph_family(ph_type: str) -> str:
    if ph_type in _TITLE_TYPES:
        return "title"
    if ph_type in ("body", "obj", "subTitle", "tbl", "chart", "dgm", "media", "clipArt", "pic"):
        return "body"
    return ph_type


def _inherited(shape, scope: _Scope) -> list[object]:
    """这个占位符在版式、母版上对应的形状（近的在前）；不是占位符返回空列表。"""
    info = _ph(shape)
    if info is None or scope.kind not in ("slide", "layout"):
        return []
    chain = []
    if scope.kind == "slide":
        found = _find_placeholder(scope.layout, *info)
        if found is not None:
            chain.append(found)
            info = _ph(found) or info
    found = _find_placeholder(scope.master, *info)
    if found is not None:
        chain.append(found)
    return chain


def _body_pr(shape):
    body = shape.find(P_TX_BODY)
    return body.find(A_BODY_PR) if body is not None else None


def _vertical(shape, scope: _Scope) -> str:
    for item in (shape, *_inherited(shape, scope)):
        body_pr = _body_pr(item)
        if body_pr is not None and body_pr.get("vert"):
            return str(body_pr.get("vert"))
    return "horz"


def _level(paragraph) -> int:
    ppr = paragraph.find(A_PPR)
    try:
        return max(0, min(8, int(ppr.get("lvl") or 0))) if ppr is not None else 0
    except ValueError:
        return 0


def _bullet_in(ppr) -> bool | None:
    """这一级段落格式里写明了有没有项目符号：有 True、明说没有 False、没写 None。"""
    if ppr is None:
        return None
    for child in ppr:
        if child.tag == _a("buNone"):
            return False
        if child.tag in _BULLET_TAGS:
            return True
    return None


def _master_text_style(master, ph_type: str):
    if master is None:
        return None
    styles = master.find(_p("txStyles"))
    if styles is None:
        return None
    if ph_type in _TITLE_TYPES:
        return styles.find(_p("titleStyle"))
    if _ph_family(ph_type) == "body":
        return styles.find(_p("bodyStyle"))
    return styles.find(_p("otherStyle"))


def _bulleted(paragraph, shape, scope: _Scope) -> bool:
    level = f"lvl{_level(paragraph) + 1}pPr"
    explicit = _bullet_in(paragraph.find(A_PPR))
    if explicit is not None:
        return explicit
    for item in (shape, *_inherited(shape, scope)):
        body = item.find(P_TX_BODY)
        styles = body.find(A_LST_STYLE) if body is not None else None
        found = _bullet_in(styles.find(_a(level))) if styles is not None else None
        if found is not None:
            return found
    info = _ph(shape)
    if info is not None and scope.kind in ("slide", "layout"):
        style = _master_text_style(scope.master, info[0])
        found = _bullet_in(style.find(_a(level))) if style is not None else None
        if found is not None:
            return found
    return False


def _shape_children(container) -> Iterator[tuple[object, list[object]]]:
    """容器里的形状（``mc:AlternateContent`` 取 ``mc:Choice``），和 ``mc:Fallback`` 里对应的形状。"""
    for child in container:
        if child.tag in _SHAPE_TAGS:
            yield child, []
        elif child.tag == MC_ALTERNATE:
            choice = child.find(MC_CHOICE)
            fallback = child.find(MC_FALLBACK)
            if choice is None:
                continue
            chosen = [item for item in choice if item.tag in _SHAPE_TAGS]
            spare = [item for item in fallback if item.tag in _SHAPE_TAGS] if fallback is not None else []
            for index, item in enumerate(chosen):
                yield item, [spare[index]] if len(spare) == len(chosen) else []


def _graphic_uri(frame) -> str:
    graphic = frame.find(_a("graphic"))
    data = graphic.find(_a("graphicData")) if graphic is not None else None
    return str(data.get("uri") or "") if data is not None else ""


def _table(frame):
    graphic = frame.find(_a("graphic"))
    data = graphic.find(_a("graphicData")) if graphic is not None else None
    return data.find(_a("tbl")) if data is not None else None


def _text_paragraphs(body) -> list[tuple[object, list]]:
    """文字框里有字的段落和它们的片段（结构不认识的段落跳过，原样保留）。"""
    found = []
    for paragraph in body.findall(A_P):
        pieces = read_pieces(paragraph)
        if pieces is None or not "".join(piece.text for piece in pieces).strip():
            continue
        found.append((paragraph, pieces))
    return found


def _has_letters(text: str) -> bool:
    return any(char.isalpha() for char in text)


class _Walker:
    """按固定顺序走一遍所有要翻的段落；取字和写回走同一遍，单元 id 一一对得上。"""

    def __init__(self, deck: _Deck, *, include_notes: bool) -> None:
        self.deck = deck
        self.include_notes = include_notes

    def parts(self) -> Iterator[tuple[_Scope, list[_Slot]]]:
        deck = self.deck
        #: 已经取过的图表、SmartArt 部件（同一个部件被两页引用时只取一次）。
        seen: set[str] = set()
        for number, slide in enumerate(deck.slides, start=1):
            layout_part = next(iter(deck.related(slide, REL_SLIDE_LAYOUT)), "")
            master_part = next(iter(deck.related(layout_part, REL_SLIDE_MASTER)), "") if layout_part else ""
            scope = _Scope(
                slide,
                f"第 {number} 页",
                "slide",
                layout=deck.root(layout_part) if layout_part else None,
                master=deck.root(master_part) if master_part else None,
            )
            yield scope, self._slots(scope)
            yield from self._graphics(scope, seen)
            if self.include_notes:
                for notes in deck.related(slide, REL_NOTES_SLIDE)[:1]:
                    yield _Scope(notes, f"第 {number} 页备注", "notes"), self._notes_slots(notes, f"第 {number} 页备注")
        for number, master in enumerate(deck.masters, start=1):
            master_root = deck.root(master)
            scope = _Scope(master, f"母版 {number}", "master", master=master_root)
            yield scope, self._slots(scope)
            for layout in deck.layouts(master):
                root = deck.root(layout)
                name = ""
                c_sld = root.find(_p("cSld")) if root is not None else None
                if c_sld is not None:
                    name = " ".join(str(c_sld.get("name") or "").split())
                label = f"版式「{name}」" if name else "版式"
                scope = _Scope(layout, label, "layout", layout=root, master=master_root)
                yield scope, self._slots(scope)

    def _slots(self, scope: _Scope) -> list[_Slot]:
        root = self.deck.root(scope.part)
        c_sld = root.find(_p("cSld")) if root is not None else None
        tree = c_sld.find(_p("spTree")) if c_sld is not None else None
        slots: list[_Slot] = []
        if tree is None:
            return slots
        counter = [0]
        self._walk(tree, None, scope, slots, counter)
        return slots

    def _graphics(self, scope: _Scope, seen: set[str]) -> Iterator[tuple[_Scope, list[_Slot]]]:
        """这一页上的图表和 SmartArt，按形状顺序。"""
        root = self.deck.root(scope.part)
        tree = root.find(f"{_p('cSld')}/{_p('spTree')}") if root is not None else None
        if tree is None:
            return
        for shape in _all_shapes(tree):
            if shape.tag != P_GRAPHIC_FRAME:
                continue
            graphic = graphic_text(self.deck, scope.part, shape, self._owner_context(shape, scope))
            if graphic is None or graphic.part in seen:
                continue
            seen.add(graphic.part)
            yield _Scope(graphic.part, scope.label, "graphic", graphic=graphic), []

    def _next_id(self, scope: _Scope, counter: list[int]) -> str:
        counter[0] += 1
        return f"{scope.part}#{counter[0]}"

    def _walk(self, container, twin_container, scope: _Scope, slots: list[_Slot], counter: list[int]) -> None:
        shapes = list(_shape_children(container))
        twins = [shape for shape, _spare in _shape_children(twin_container)] if twin_container is not None else []
        if len(twins) != len(shapes):
            twins = []
        for index, (shape, spare) in enumerate(shapes):
            twin = spare[0] if spare else (twins[index] if twins else None)
            if twin is not None and twin.tag != shape.tag:
                twin = None
            if shape.tag == P_GRP_SP:
                self._walk(shape, twin, scope, slots, counter)
                continue
            if scope.kind in ("layout", "master") and _ph(shape) is not None:
                continue
            if shape.tag == P_SP:
                self._shape_slots(shape, twin, scope, slots, counter)
            elif shape.tag == P_GRAPHIC_FRAME and _graphic_uri(shape) == URI_TABLE:
                self._table_slots(shape, twin, scope, slots, counter)

    def _owner_context(self, shape, scope: _Scope) -> str:
        info = _ph(shape)
        label = _PLACEHOLDER_LABELS.get(info[0]) if info is not None else None
        return f"{scope.label} · {label or _shape_name(shape)}"

    def _shape_slots(self, shape, twin, scope: _Scope, slots: list[_Slot], counter: list[int]) -> None:
        body = shape.find(P_TX_BODY)
        if body is None:
            return
        found = _text_paragraphs(body)
        if not found:
            return
        info = _ph(shape)
        kind = "heading" if info is not None and info[0] in _TITLE_TYPES else "paragraph"
        owner_context = self._owner_context(shape, scope)
        vertical = _vertical(shape, scope)
        mirrors = self._mirror_paragraphs(twin.find(P_TX_BODY) if twin is not None else None, found)
        for number, (paragraph, pieces) in enumerate(found, start=1):
            marks = paragraph_marks(paragraph, pieces)
            unit_id = self._next_id(scope, counter)
            if not _has_letters(marks.plain):
                continue
            context = owner_context if len(found) == 1 else f"{owner_context} · 第 {number} 段"
            slots.append(
                _Slot(
                    unit_id=unit_id,
                    context=context,
                    kind=kind,
                    marks=marks,
                    body=body,
                    owner=body,
                    owner_context=owner_context,
                    vertical=vertical,
                    bulleted=_bulleted(paragraph, shape, scope),
                    shape_id=_shape_id(shape),
                    mirrors=mirrors[number - 1],
                    twin_owner=twin.find(P_TX_BODY) if mirrors[number - 1] else None,
                )
            )

    @staticmethod
    def _mirror_paragraphs(body, found: list[tuple[object, list]]) -> list[list[ParagraphMarks]]:
        """``mc:Fallback`` 里的同一段文字：段落文字和 Choice 里完全一样时逐段同步。"""
        empty: list[list[ParagraphMarks]] = [[] for _ in found]
        if body is None:
            return empty
        other = _text_paragraphs(body)
        if [_joined(pieces) for _paragraph, pieces in other] != [_joined(pieces) for _paragraph, pieces in found]:
            return empty
        return [[paragraph_marks(paragraph, pieces)] for paragraph, pieces in other]

    def _table_slots(self, frame, twin, scope: _Scope, slots: list[_Slot], counter: list[int]) -> None:
        table = _table(frame)
        if table is None:
            return
        name = _shape_name(frame)
        twin_table = _table(twin) if twin is not None else None
        twin_cells = list(twin_table.iter(_a("tc"))) if twin_table is not None else []
        cells = list(table.iter(_a("tc")))
        if len(twin_cells) != len(cells):
            twin_cells = []
        cell_index = 0
        for row_number, row in enumerate(table.findall(_a("tr")), start=1):
            for column_number, cell in enumerate(row.findall(_a("tc")), start=1):
                twin_cell = twin_cells[cell_index] if twin_cells else None
                cell_index += 1
                body = cell.find(A_TX_BODY)
                if body is None:
                    continue
                found = _text_paragraphs(body)
                owner_context = f"{scope.label} · {name} · 第 {row_number} 行第 {column_number} 列"
                mirrors = self._mirror_paragraphs(twin_cell.find(A_TX_BODY) if twin_cell is not None else None, found)
                for number, (paragraph, pieces) in enumerate(found, start=1):
                    marks = paragraph_marks(paragraph, pieces)
                    unit_id = self._next_id(scope, counter)
                    if not _has_letters(marks.plain):
                        continue
                    context = owner_context if len(found) == 1 else f"{owner_context} · 第 {number} 段"
                    slots.append(
                        _Slot(
                            unit_id=unit_id,
                            context=context,
                            kind="cell",
                            marks=marks,
                            body=body,
                            owner=frame,
                            owner_context=f"{scope.label} · {name}",
                            mirrors=mirrors[number - 1],
                            twin_owner=twin if mirrors[number - 1] else None,
                        )
                    )

    def _notes_slots(self, part: str, label: str) -> list[_Slot]:
        root = self.deck.root(part)
        slots: list[_Slot] = []
        if root is None:
            return slots
        counter = [0]
        scope = _Scope(part, label, "notes")
        for shape in root.iter(P_SP):
            info = _ph(shape)
            if info is None or info[0] != "body":
                continue
            body = shape.find(P_TX_BODY)
            if body is None:
                continue
            found = _text_paragraphs(body)
            for number, (paragraph, pieces) in enumerate(found, start=1):
                marks = paragraph_marks(paragraph, pieces)
                unit_id = self._next_id(scope, counter)
                if not _has_letters(marks.plain):
                    continue
                context = label if len(found) == 1 else f"{label} · 第 {number} 段"
                slots.append(
                    _Slot(
                        unit_id=unit_id,
                        context=context,
                        kind="notes",
                        marks=marks,
                        body=body,
                        owner=body,
                        owner_context=label,
                    )
                )
        return slots


def _joined(pieces: list) -> str:
    return "".join(piece.text for piece in pieces)


# ── 取字 ────────────────────────────────────────────────────────────────────


@dataclass
class PptxUnit:
    """一段要翻译的文字（字段与 ``translator_adapter.TextUnit`` 对应）。"""

    id: str
    text: str
    context: str
    kind: str
    #: 带 ``<gN>`` 标记的原文；段内格式统一时为空。
    marked: str = ""
    #: 要原样保留的片段：网址、邮箱、自动字段的文字。
    protected: list[str] = field(default_factory=list)


def _protected(marks: ParagraphMarks) -> list[str]:
    found = protected_text(marks.plain)
    found.extend(piece.text.strip() for piece in marks.pieces if piece.is_field and piece.text.strip())
    return [item for item in dict.fromkeys(found) if item in marks.plain]


def protected_text(text: str) -> list[str]:
    """文字里要原样保留的网址和邮箱。"""
    found: list[str] = []
    for match in _URL_RE.finditer(text):
        value = match.group(0).rstrip(_URL_TRAILING)
        if value:
            found.append(value)
    found.extend(match.group(0) for match in _EMAIL_RE.finditer(text))
    return [item for item in dict.fromkeys(found) if item in text]


def extract_units(path: str | Path, *, translate_notes: bool = True) -> list[PptxUnit]:
    """文件里要翻译的段落，按幻灯片顺序。"""
    with _open(Path(path)) as package:
        deck = _Deck(package)
        units: list[PptxUnit] = []
        for scope, slots in _Walker(deck, include_notes=translate_notes).parts():
            if scope.graphic is not None:
                units.extend(scope.graphic.units())
            for slot in slots:
                units.append(
                    PptxUnit(
                        id=slot.unit_id,
                        text=slot.marks.plain,
                        context=slot.context,
                        kind=slot.kind,
                        marked=slot.marks.marked,
                        protected=_protected(slot.marks),
                    )
                )
        return units


@dataclass
class PptxScan:
    unit_count: int
    warnings: list[str]


def scan_file(path: str | Path, *, translate_notes: bool = True) -> PptxScan:
    """单元数，和不翻的内容（图片、嵌入对象、认不出的图表和 SmartArt、批注）各有几处。"""
    with _open(Path(path)) as package:
        deck = _Deck(package)
        count = 0
        for scope, slots in _Walker(deck, include_notes=translate_notes).parts():
            count += len(slots)
            if scope.graphic is not None:
                count += len(scope.graphic.units())
        pictures = objects = charts = diagrams = comments = 0
        for slide in deck.slides:
            root = deck.root(slide)
            tree = root.find(f"{_p('cSld')}/{_p('spTree')}") if root is not None else None
            if tree is not None:
                for shape in _all_shapes(tree):
                    if shape.tag == P_PIC:
                        pictures += 1
                    elif shape.tag == P_GRAPHIC_FRAME:
                        uri = _graphic_uri(shape)
                        objects += uri == URI_OLE
                        if uri in (URI_CHART, URI_CHART_EX, URI_DIAGRAM) and graphic_text(deck, slide, shape, "") is None:
                            charts += uri != URI_DIAGRAM
                            diagrams += uri == URI_DIAGRAM
            for rel in deck.rels(slide):
                if rel.external or rel.type not in (REL_COMMENTS, REL_MODERN_COMMENTS):
                    continue
                comment_root = deck.root(rel.target)
                if comment_root is not None:
                    comments += sum(
                        1 for item in comment_root if isinstance(item.tag, str) and etree.QName(item).localname == "cm"
                    )
    warnings = []
    if pictures:
        warnings.append(f"{pictures} 张图片中的文字未翻译")
    if objects:
        warnings.append(f"{objects} 个嵌入对象中的文字未翻译")
    if charts:
        warnings.append(f"{charts} 个图表中的文字未翻译")
    if diagrams:
        warnings.append(f"{diagrams} 个 SmartArt 图形中的文字未翻译")
    if comments:
        warnings.append(f"{comments} 条批注未翻译")
    return PptxScan(unit_count=count, warnings=warnings)


def _all_shapes(container) -> Iterator[object]:
    for shape, _spare in _shape_children(container):
        yield shape
        if shape.tag == P_GRP_SP:
            yield from _all_shapes(shape)


# ── 写回 ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Translation:
    """一个单元写回什么：译文，和标记完整的带标记译文（没有为空）。"""

    text: str
    marked: str = ""


@dataclass
class ReviewItem:
    unit_id: str
    context: str
    reason: str


@dataclass
class WriteResult:
    path: str
    review: list[ReviewItem] = field(default_factory=list)


REASON_VERTICAL = "竖排文本框，译文可能需要改为横排"


def lang_tag(target_lang: str | None) -> str:
    """目标语言代码 → ``lang`` 属性值；认不出的（自定义语言）返回空串，不改 lang。"""
    value = str(target_lang or "").strip().replace("_", "-")
    if not value:
        return ""
    base = value.split("-")[0].lower()
    if "-" in value and base in TARGET_LANG_TAGS:
        return value
    return TARGET_LANG_TAGS.get(base, "")


def _is_cjk(target_lang: str | None) -> bool:
    return str(target_lang or "").strip().lower().replace("_", "-").split("-")[0] in _CJK_LANGS


def _ensure_ppr(paragraph):
    ppr = paragraph.find(A_PPR)
    if ppr is None:
        ppr = etree.Element(A_PPR)
        paragraph.insert(0, ppr)
    return ppr


def _set_lang(elements: list[object], paragraph, tag: str) -> None:
    for element in elements:
        rpr = element.find(A_RPR)
        if rpr is None:
            rpr = etree.Element(A_RPR)
            element.insert(0, rpr)
        rpr.set("lang", tag)
        rpr.attrib.pop("err", None)
    end = paragraph.find(A_END_PARA_RPR)
    if end is not None:
        end.set("lang", tag)


def _insert_in_order(ppr, element) -> None:
    name = etree.QName(element).localname
    rank = _PPR_ORDER.index(name)
    for index, child in enumerate(ppr):
        if not isinstance(child.tag, str):
            continue
        local = etree.QName(child).localname
        if local in _PPR_ORDER and _PPR_ORDER.index(local) > rank:
            ppr.insert(index, element)
            return
    ppr.append(element)


def _as_translation_paragraph(paragraph, *, bulleted: bool) -> None:
    """双语对照里的译文段：不带项目符号；原段有项目符号时首行缩进归零，与原文文字对齐。"""
    ppr = _ensure_ppr(paragraph)
    for child in list(ppr):
        if child.tag in _BULLET_ALL:
            ppr.remove(child)
    _insert_in_order(ppr, etree.Element(_a("buNone")))
    if bulleted:
        ppr.set("indent", "0")


@dataclass
class _Context:
    lang: str
    western: bool
    bilingual: bool


def write_translated_paragraph(marks: ParagraphMarks, value: Translation, context: _Context) -> object:
    """按模式写一段：仅译文改写原段；双语对照在原段后插一段译文（不带项目符号）。

    图表和 SmartArt 的段落也走这里。返回写了译文的那个 ``a:p``；双语对照复制原段失败时
    返回 None（原段不动）。
    """
    if not context.bilingual:
        _write_one(marks, value, context)
        return marks.paragraph
    copy = _translation_copy(marks)
    if copy is None:
        return None
    _write_one(copy, value, context)
    _as_translation_paragraph(copy.paragraph, bulleted=False)
    return copy.paragraph


def should_write(value: Translation | None, source: str) -> bool:
    """这个单元有没有要写回的译文（没有译文、译文为空或和原文一样时不写）。"""
    return value is not None and value.text != source and bool(value.text.strip())


def _write_one(marks: ParagraphMarks, value: Translation, context: _Context) -> None:
    written = write_paragraph(marks, value.text, value.marked)
    if context.lang:
        _set_lang(written, marks.paragraph, context.lang)
    if context.western and context.lang:
        _ensure_ppr(marks.paragraph).set("latinLnBrk", "0")


def _translation_copy(marks: ParagraphMarks) -> ParagraphMarks | None:
    """原段的副本，插在原段后面；返回副本的片段与标记（和原段一一对应）。"""
    paragraph = marks.paragraph
    copy = deepcopy(paragraph)
    copy.tail = None
    paragraph.addnext(copy)
    pieces = read_pieces(copy)
    if pieces is None:
        return None
    return paragraph_marks(copy, pieces)


#: 双语对照写回过程中给新插的译文段做的临时记号，保存前去掉。
_TRANSLATION_FLAG = "_translation"


def _freeze_numbering(body) -> None:
    """双语对照时写明自动编号的起始号：原段之间插了不带编号的译文段，编号仍是 1、2、3。"""
    counters: dict[int, tuple[str, int]] = {}
    for paragraph in body.findall(A_P):
        if paragraph.get(_TRANSLATION_FLAG) == "1":
            continue
        ppr = paragraph.find(A_PPR)
        level = _level(paragraph)
        for deeper in [key for key in counters if key > level]:
            del counters[deeper]
        auto = ppr.find(_a("buAutoNum")) if ppr is not None else None
        pieces = read_pieces(paragraph) or []
        if auto is None:
            if _joined(pieces).strip():
                counters.pop(level, None)
            continue
        scheme = str(auto.get("type") or "")
        try:
            start = int(auto.get("startAt") or 1)
        except ValueError:
            start = 1
        previous = counters.get(level)
        number = previous[1] if previous is not None and previous[0] == scheme else start
        counters[level] = (scheme, number + 1)
        auto.set("startAt", str(number))


def _remap_animations(root, inserted: Mapping[str, list[bool]]) -> None:
    """按段落出现的动画：段落序号改成插入译文段后的序号，范围覆盖「原段 + 译文段」。"""
    if not inserted:
        return
    for target in root.iter(_p("spTgt")):
        flags = inserted.get(str(target.get("spid") or ""))
        if not flags:
            continue
        for rng in target.iter(_p("pRg")):
            try:
                start, end = int(rng.get("st") or 0), int(rng.get("end") or 0)
            except ValueError:
                continue
            if not (0 <= start <= end < len(flags)):
                continue
            new_start = start + sum(flags[:start])
            new_end = end + sum(flags[:end]) + (1 if flags[end] else 0)
            rng.set("st", str(new_start))
            rng.set("end", str(new_end))


def write_file(
    source: str | Path,
    out_path: str | Path,
    translations: Mapping[str, Translation],
    *,
    bilingual: bool = False,
    target_lang: str | None = None,
    translate_notes: bool = True,
    layout_mode: str = LAYOUT_FIT,
    gap_mm: float = DEFAULT_GAP_MM,
    fit: Callable[..., list] | None = None,
) -> WriteResult:
    """把译文写进 ``source`` 的副本 ``out_path``，保存后验收；不通过时删掉输出、抛 ``PptxError``。

    ``layout_mode``：写回偏好，融合 / 保持字号 / 固定版面；``gap_mm``：融合模式加高、加宽时与
    旁边的形状和页边留的距离（都见 ``core.pptx_fit``，交给缩字那一步）。
    """
    source = Path(source)
    out_path = Path(out_path)
    fit = fit_after_write if fit is None else fit
    context = _Context(
        lang=lang_tag(target_lang),
        western=bool(target_lang) and not _is_cjk(target_lang),
        bilingual=bilingual,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, out_path)
    review: list[ReviewItem] = []
    checks: dict[str, _PartCheck | PartVerifier] = {}
    try:
        with _open(out_path) as package:
            deck = _Deck(package)
            for scope, slots in _Walker(deck, include_notes=translate_notes).parts():
                if scope.graphic is not None:
                    written = scope.graphic.write(translations, context)
                    review.extend(written.review)
                    for name, data in written.parts.items():
                        package.write(name, data)
                    checks.update(written.checks)
                    continue
                result = _write_part(scope, slots, translations, context, review)
                if result is None:
                    continue
                root = deck.root(scope.part)
                if scope.kind != "notes":
                    # 默认值不传（「融合」、默认间距），缩字那一步按自己的默认走。
                    extra: dict[str, Any] = {"layout_mode": layout_mode} if layout_mode != LAYOUT_FIT else {}
                    if gap_mm != DEFAULT_GAP_MM:
                        extra["gap_mm"] = gap_mm
                    for issue in fit(package.read, scope.part, root, list(result.touched), **extra) or []:
                        target = getattr(issue, "target", None)
                        slot = next((item for item in slots if item.owner is target), None)
                        review.append(
                            ReviewItem(
                                unit_id=slot.unit_id if slot is not None else "",
                                context=slot.owner_context if slot is not None else scope.label,
                                reason=str(getattr(issue, "reason", "") or "缩到下限仍放不下"),
                            )
                        )
                    for owner, twin in result.twins.items():
                        _sync_fit(_fit_host(owner), _fit_host(twin))
                checks[scope.part] = _part_check(root, result)
                package.write(scope.part, _serialize(root))
            try:
                package.save()
            except PackageVerificationError as exc:
                raise PptxError(str(exc)) from exc
        problem = _first_problem(source, out_path, checks)
        if problem:
            raise PptxError(f"写出的文件未通过检查，已放弃：{problem}")
    except BaseException:
        out_path.unlink(missing_ok=True)
        raise
    return WriteResult(path=str(out_path), review=review)


@dataclass
class _PartResult:
    #: 改过文字的元素（形状的文字框、表格的 ``p:graphicFrame``），按出现顺序、不重复。
    touched: list[object]
    #: 改过的文字框写完后应有的段数。
    paragraphs: dict[object, int]
    #: ``touched`` 里的元素 → ``mc:Fallback`` 里对应的那个（写了同样的文字）。
    twins: dict[object, object] = field(default_factory=dict)


def _write_part(
    scope: _Scope,
    slots: list[_Slot],
    translations: Mapping[str, Translation],
    context: _Context,
    review: list[ReviewItem],
) -> _PartResult | None:
    """写一个部件里的所有段落；什么都没改返回 None。"""
    touched: dict[object, None] = {}
    paragraphs: dict[object, int] = {}
    inserted: dict[str, set[object]] = {}
    reviewed: set[object] = set()
    twins: dict[object, object] = {}

    def count(paragraph) -> None:
        body = paragraph.getparent()
        if body is not None and body not in paragraphs:
            paragraphs[body] = len(body.findall(A_P))

    for slot in slots:
        value = translations.get(slot.unit_id)
        if not should_write(value, slot.marks.plain):
            continue
        for marks in (slot.marks, *slot.mirrors):
            count(marks.paragraph)
            if not context.bilingual:
                _write_one(marks, value, context)
                continue
            copy = _translation_copy(marks)
            if copy is None:
                continue
            paragraphs[marks.paragraph.getparent()] += 1
            copy.paragraph.set(_TRANSLATION_FLAG, "1")
            _write_one(copy, value, context)
            _as_translation_paragraph(copy.paragraph, bulleted=slot.bulleted)
        if context.bilingual and slot.shape_id:
            inserted.setdefault(slot.shape_id, set()).add(slot.paragraph)
        touched[slot.owner] = None
        if slot.mirrors and slot.twin_owner is not None:
            twins[slot.owner] = slot.twin_owner
        if context.western and slot.vertical in _VERTICAL_REVIEW and slot.owner not in reviewed:
            reviewed.add(slot.owner)
            review.append(ReviewItem(slot.unit_id, slot.owner_context, REASON_VERTICAL))
    if not touched:
        return None
    if context.bilingual:
        for body in paragraphs:
            _freeze_numbering(body)
        flags: dict[str, list[bool]] = {}
        for slot in slots:
            if slot.shape_id in inserted and slot.shape_id not in flags:
                originals = [item for item in slot.body.findall(A_P) if item.get(_TRANSLATION_FLAG) != "1"]
                flags[slot.shape_id] = [item in inserted[slot.shape_id] for item in originals]
        _remap_animations(slots[0].paragraph.getroottree().getroot(), flags)
        for body in paragraphs:
            for paragraph in body.findall(A_P):
                paragraph.attrib.pop(_TRANSLATION_FLAG, None)
    return _PartResult(touched=list(touched), paragraphs=paragraphs, twins=twins)


#: 缩字可能改的属性：字号、自动调整比例、位置大小、表格列宽行高。
_FIT_ATTRS = {
    A_RPR: ("sz",),
    A_END_PARA_RPR: ("sz",),
    _a("defRPr"): ("sz",),
    _a("normAutofit"): ("fontScale", "lnSpcReduction"),
    _a("off"): ("x", "y"),
    _a("ext"): ("cx", "cy"),
    _a("gridCol"): ("w",),
    _a("tr"): ("h",),
}
_AUTOFIT_TAGS = frozenset({_a("noAutofit"), _a("normAutofit"), _a("spAutoFit")})


def _fit_host(owner):
    """缩字改动所在的形状：文字框的父形状，表格就是 ``p:graphicFrame`` 本身。"""
    return owner.getparent() if owner.tag == P_TX_BODY else owner


def _sync_fit(source, target) -> None:
    """把缩字在 ``mc:Choice`` 形状上改的字号、自动调整、位置大小抄到 ``mc:Fallback`` 里对应的形状。

    两边结构一样的地方逐个元素抄 ``_FIT_ATTRS`` 里的属性；``a:bodyPr`` 的自动调整方式整个换成
    Choice 那份。结构不一样的子树不碰（Fallback 本来就可以和 Choice 不同）。
    """
    if source.tag != target.tag:
        return
    for key in _FIT_ATTRS.get(source.tag, ()):
        value = source.get(key)
        if value is not None:
            target.set(key, value)
    source_children = [child for child in source if isinstance(child.tag, str)]
    target_children = [child for child in target if isinstance(child.tag, str)]
    if source.tag == _a("bodyPr"):
        fit = next((child for child in source_children if child.tag in _AUTOFIT_TAGS), None)
        old = [child for child in target_children if child.tag in _AUTOFIT_TAGS]
        if fit is not None and not (len(old) == 1 and old[0].tag == fit.tag):
            for child in old:
                target.remove(child)
            # 规范顺序里自动调整排在 prstTxWarp 后面、scene3d 等前面。
            later = next(
                (child for child in target if isinstance(child.tag, str) and child.tag != _a("prstTxWarp")), None
            )
            if later is not None:
                later.addprevious(deepcopy(fit))
            else:
                target.append(deepcopy(fit))
            target_children = [child for child in target if isinstance(child.tag, str)]
            source_children = [child for child in source if isinstance(child.tag, str)]
    if [child.tag for child in source_children] != [child.tag for child in target_children]:
        return
    for source_child, target_child in zip(source_children, target_children):
        _sync_fit(source_child, target_child)


# ── 图表与 SmartArt 的接入点 ────────────────────────────────────────────────

#: 不在形状树里的部件（图表、SmartArt、图表的内嵌工作簿）写完后怎么核对：
#: ``(原字节, 新字节) -> 问题说明``，没问题返回空串。
PartVerifier = Callable[[bytes, bytes], str]


@dataclass
class GraphicWrite:
    """一个图表或 SmartArt 写回的结果：改过的部件、各自怎么核对、需复核的地方。"""

    parts: dict[str, bytes] = field(default_factory=dict)
    checks: dict[str, PartVerifier] = field(default_factory=dict)
    review: list[ReviewItem] = field(default_factory=list)


class GraphicText:
    """图表、SmartArt 这类有自己部件的形状：取字和写回走同一份解析，单元 id 对得上。"""

    #: 主部件名（单元 id 的前缀；同一部件只取一次）。
    part: str

    def units(self) -> list[PptxUnit]:
        raise NotImplementedError

    def write(self, translations: Mapping[str, Translation], context: _Context) -> GraphicWrite:
        raise NotImplementedError


def graphic_text(deck: _Deck, slide: str, frame, owner_context: str) -> GraphicText | None:
    """幻灯片上一个 ``p:graphicFrame`` 对应的图表或 SmartArt；不是、认不出或缺部件时返回 None。"""
    uri = _graphic_uri(frame)
    if uri == URI_CHART:
        from core.pptx_charts import chart_text

        return chart_text(deck, slide, frame, owner_context)
    if uri == URI_DIAGRAM:
        from core.pptx_smartart import diagram_text

        return diagram_text(deck, slide, frame, owner_context)
    return None


def text_problem(name: str, root) -> str:
    """改过的部件里段落结构是否正常、有没有临时记号和残留的格式标记。"""
    for paragraph in root.iter(A_P):
        children = [child for child in paragraph if isinstance(child.tag, str)]
        if any(child.tag == A_PPR for child in children[1:]) or any(
            child.tag == A_END_PARA_RPR for child in children[:-1]
        ):
            return f"{name} 的段落结构不对"
        if paragraph.get(_TRANSLATION_FLAG) is not None:
            return f"{name} 留有临时记号"
    for text in root.iter(A_T):
        if text.text and has_residual_marks(text.text):
            return "译文里残留了格式标记"
    return ""


def has_residual_marks(text: str) -> bool:
    return bool(_RESIDUAL_RE.search(text) or has_marks(text))


# ── 保存后验收 ──────────────────────────────────────────────────────────────


@dataclass
class _PartCheck:
    """一个改过的部件写完后应当是什么样：哪些形状可以变位置尺寸，哪些文字框各有几段。"""

    #: 改过文字的形状和包含它们的组合（按文档里第几个形状）：缩字可能改它们的位置尺寸。
    movable: set[int]
    #: 文档里第几个文字框 → 应有的段数。
    paragraphs: dict[int, int]


def _iter_shapes(root) -> Iterator[object]:
    for element in root.iter():
        if element.tag in _SHAPE_TAGS:
            yield element


def _iter_bodies(root) -> Iterator[object]:
    return root.iter(P_TX_BODY, A_TX_BODY)


def _part_check(root, result: _PartResult) -> _PartCheck:
    shapes = {shape: index for index, shape in enumerate(_iter_shapes(root))}
    movable: set[int] = set()
    for element in (*result.touched, *result.twins.values()):
        node = element
        while node is not None:
            if node in shapes:
                movable.add(shapes[node])
            node = node.getparent()
    bodies = {body: index for index, body in enumerate(_iter_bodies(root))}
    return _PartCheck(
        movable=movable,
        paragraphs={bodies[body]: total for body, total in result.paragraphs.items() if body in bodies},
    )


def _xfrm(shape) -> bytes:
    for tag in (_p("spPr"), _p("grpSpPr"), _p("xfrm")):
        found = shape.find(tag)
        if found is None:
            continue
        xfrm = found if tag == _p("xfrm") else found.find(_a("xfrm"))
        return etree.tostring(xfrm, method="c14n") if xfrm is not None else b""
    return b""


def _first_problem(source: Path, output: Path, checks: Mapping[str, _PartCheck | PartVerifier]) -> str:
    """重新打开写好的文件逐项核对；有问题返回一句说明，没问题返回空串。"""
    try:
        with zipfile.ZipFile(source) as before, zipfile.ZipFile(output) as after:
            before_infos = {info.filename: info for info in before.infolist()}
            after_infos = {info.filename: info for info in after.infolist()}
            if list(before_infos) != list(after_infos):
                return "文件里的部件与原文件不一致"
            for name, info in after_infos.items():
                check = checks.get(name)
                if check is None:
                    if info.CRC != before_infos[name].CRC:
                        return f"未改动的部件 {name} 内容变了"
                    continue
                if not isinstance(check, _PartCheck):
                    problem = check(before.read(name), after.read(name))
                    if problem:
                        return problem
                    continue
                try:
                    old_root = _parse(before.read(name))
                    new_root = _parse(after.read(name))
                except etree.XMLSyntaxError:
                    return f"{name} 无法解析"
                problem = _part_problem(name, old_root, new_root, check)
                if problem:
                    return problem
    except (OSError, zipfile.BadZipFile) as exc:
        return f"无法重新打开（{exc.__class__.__name__}）"
    return ""


def _part_problem(name: str, old_root, new_root, check: _PartCheck) -> str:
    old_shapes = list(_iter_shapes(old_root))
    new_shapes = list(_iter_shapes(new_root))
    if len(old_shapes) != len(new_shapes) or any(old.tag != new.tag for old, new in zip(old_shapes, new_shapes)):
        return f"{name} 的形状变了"
    for index, (old, new) in enumerate(zip(old_shapes, new_shapes)):
        if index not in check.movable and _xfrm(old) != _xfrm(new):
            return f"{name} 里未改动的形状位置或大小变了"
    bodies = list(_iter_bodies(new_root))
    for index, total in check.paragraphs.items():
        if index >= len(bodies) or len(bodies[index].findall(A_P)) != total:
            return f"{name} 的段落数不对"
    return text_problem(name, new_root)


__all__ = [
    "REASON_VERTICAL",
    "TARGET_LANG_TAGS",
    "FitIssue",
    "GraphicText",
    "GraphicWrite",
    "PartVerifier",
    "PptxError",
    "PptxScan",
    "PptxUnit",
    "ReviewItem",
    "Translation",
    "WriteResult",
    "extract_units",
    "fit_after_write",
    "graphic_text",
    "lang_tag",
    "protected_text",
    "scan_file",
    "should_write",
    "text_problem",
    "write_file",
    "write_translated_paragraph",
]
