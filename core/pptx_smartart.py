"""PPT 里 SmartArt 的文字：内容（``dataN.xml``）和上一次排版的结果（``drawingN.xml``）两边一起改。

SmartArt 在幻灯片上是一个 ``p:graphicFrame``，``dgm:relIds`` 的 ``r:dm`` 经幻灯片的关系文件
指向内容部件：``dgm:ptLst`` 里每个节点（``dgm:pt``）的 ``dgm:t`` 是一个文字框，一段一个
单元，和幻灯片上的段落一样带 ``<gN>`` 标记、一样写回。

排版结果部件（MS-ODRAWXML 写明它是给不会自己排 SmartArt 的软件显示用的，LibreOffice
显示的就是它）由内容部件 ``dsp:dataModelExt`` 的 ``relId`` 找到：这个 id 是幻灯片关系文件
里的（个别文件在内容部件自己的关系里，两处都找），不按文件编号猜。排版结果里每个形状
（``dsp:sp``）的 ``modelId`` 是内容里一个排版节点（``type="pres"``），它的
``presAssocID`` 指向显示的那个内容节点；一个形状里也可能有几个节点的文字（上级节点 +
下级要点）。所以排版结果按段落写：段落文字和这个 SmartArt 里某个内容段落完全一样、且
这段文字只有一种译法时写同样的译文，优先找形状对应的那个节点。对不上的段落不改，
这个 SmartArt 列入需复核（PowerPoint 打开时按内容重排，显示没问题；其他软件可能显示原文）。

形状的位置大小一个不动。PowerPoint 打开时按内容重排、自己缩字（推断，待 Mac 实测）；
排版结果是别的软件直接显示的，译文放不下时把排版结果里所有带字形状的字号按同一个比例
缩小（SmartArt 本来就是同一级形状字一样大），比例按 ``core/text_box_fit`` 的估算，
最多缩到 60%。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from lxml import etree

from core.drawingml_marks import A_END_PARA_RPR, A_P, A_RPR, NS_A, ParagraphMarks, paragraph_marks, read_pieces
from core.pptx_text import (
    GraphicText,
    GraphicWrite,
    PptxUnit,
    ReviewItem,
    Translation,
    _Context,
    _Deck,
    _parse,
    _protected,
    _r,
    _serialize,
    should_write,
    text_problem,
    write_translated_paragraph,
)
from core.text_box_fit import MIN_SCALE, ParagraphMetrics, fitting_scale

NS_DGM = "http://schemas.openxmlformats.org/drawingml/2006/diagram"
NS_DSP = "http://schemas.microsoft.com/office/drawing/2008/diagram"
_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
REL_DIAGRAM_DATA = _REL + "diagramData"
REL_DIAGRAM_DRAWING = "http://schemas.microsoft.com/office/2007/relationships/diagramDrawing"


def _dgm(tag: str) -> str:
    return f"{{{NS_DGM}}}{tag}"


def _dsp(tag: str) -> str:
    return f"{{{NS_DSP}}}{tag}"


DGM_REL_IDS = _dgm("relIds")
DGM_PT = _dgm("pt")
DGM_PT_LST = _dgm("ptLst")
DGM_T = _dgm("t")
DGM_PR_SET = _dgm("prSet")
DGM_CXN_LST = _dgm("cxnLst")
DSP_DATA_MODEL_EXT = _dsp("dataModelExt")
DSP_SP = _dsp("sp")
DSP_TX_BODY = _dsp("txBody")
DSP_TX_XFRM = _dsp("txXfrm")
DSP_SP_PR = _dsp("spPr")

EMU_PER_PT = 12700
#: ``a:bodyPr`` 没写内边距时的默认值（EMU）：左右 0.1 英寸，上下 0.05 英寸。
_DEFAULT_INSETS = {"lIns": 91440, "rIns": 91440, "tIns": 45720, "bIns": 45720}
_DEFAULT_SIZE = 1800

REASON_DRAWING_NOT_SYNCED = "SmartArt 有文字未能同步到显示副本，在 PowerPoint 以外的软件里可能仍显示原文"


def _has_letters(text: str) -> bool:
    return any(char.isalpha() for char in text)


@dataclass
class _Node:
    unit_id: str
    context: str
    marks: ParagraphMarks
    #: 所在的内容节点（``dgm:pt`` 的 ``modelId``）。
    model_id: str


def _paragraphs(body) -> list[ParagraphMarks]:
    found = []
    for paragraph in body.findall(A_P):
        pieces = read_pieces(paragraph)
        if pieces is not None and "".join(piece.text for piece in pieces).strip():
            found.append(paragraph_marks(paragraph, pieces))
    return found


class DiagramText(GraphicText):
    """一个 SmartArt 的内容部件（单元 id 的前缀）和排版结果部件。"""

    def __init__(self, part: str, root, owner_context: str, drawing_part: str = "", drawing=None) -> None:
        self.part = part
        self.root = root
        self.owner_context = owner_context
        self.drawing_part = drawing_part
        self.drawing = drawing
        self.nodes: list[_Node] = []
        #: 排版节点 → 它显示的内容节点（``presAssocID``）。
        self.shown: dict[str, str] = {}
        self._collect()

    def _collect(self) -> None:
        points = self.root.find(DGM_PT_LST)
        if points is None:
            return
        counter = 0
        item = 0
        for point in points.findall(DGM_PT):
            model_id = str(point.get("modelId") or "")
            settings = point.find(DGM_PR_SET)
            if point.get("type") == "pres" and settings is not None and settings.get("presAssocID"):
                self.shown[model_id] = str(settings.get("presAssocID"))
            body = point.find(DGM_T)
            if body is None:
                continue
            found = _paragraphs(body)
            if not any(_has_letters(marks.plain) for marks in found):
                continue
            item += 1
            for number, marks in enumerate(found, start=1):
                counter += 1
                if not _has_letters(marks.plain):
                    continue
                context = f"{self.owner_context} · 第 {item} 项"
                if len(found) > 1:
                    context += f" · 第 {number} 段"
                self.nodes.append(_Node(f"{self.part}#{counter}", context, marks, model_id))

    def units(self) -> list[PptxUnit]:
        return [
            PptxUnit(
                id=node.unit_id,
                text=node.marks.plain,
                context=node.context,
                kind="paragraph",
                marked=node.marks.marked,
                protected=_protected(node.marks),
            )
            for node in self.nodes
        ]

    def write(self, translations: Mapping[str, Translation], context: _Context) -> GraphicWrite:
        result = GraphicWrite()
        written: list[tuple[_Node, Translation]] = []
        for node in self.nodes:
            value = translations.get(node.unit_id)
            if should_write(value, node.marks.plain) and write_translated_paragraph(node.marks, value, context) is not None:
                written.append((node, value))
        if not written:
            return result
        result.parts[self.part] = _serialize(self.root)
        result.checks[self.part] = _verify_data
        if self.drawing is not None:
            missed = self._write_drawing(written, context)
            result.parts[self.drawing_part] = _serialize(self.drawing)
            result.checks[self.drawing_part] = _verify_drawing
            if missed:
                result.review.append(ReviewItem(written[0][0].unit_id, self.owner_context, REASON_DRAWING_NOT_SYNCED))
        return result

    def _write_drawing(self, written: list[tuple[_Node, Translation]], context: _Context) -> bool:
        """把译文写进排版结果；有带字的段落对不上时返回 True。"""
        by_text: dict[str, list[tuple[_Node, Translation]]] = {}
        for node, value in written:
            by_text.setdefault(node.marks.plain, []).append((node, value))
        untranslated = {node.marks.plain for node in self.nodes} - set(by_text)
        missed = False
        changed: list[tuple[object, list[ParagraphMarks]]] = []
        for shape in self.drawing.iter(DSP_SP):
            body = shape.find(DSP_TX_BODY)
            if body is None:
                continue
            model_id = str(shape.get("modelId") or "")
            shown = self.shown.get(model_id, model_id)
            before = _paragraphs(body)
            touched = False
            for marks in before:
                candidates = by_text.get(marks.plain)
                if not candidates:
                    # 原文本来就没翻（译文和原文一样、模型没给）的段落不算对不上。
                    missed = missed or (_has_letters(marks.plain) and marks.plain not in untranslated)
                    continue
                own = [item for item in candidates if item[0].model_id == shown]
                choices = own or candidates
                if len({(value.text, value.marked) for _node, value in choices}) != 1:
                    missed = True
                    continue
                node, value = choices[0]
                # 排版结果的片段常比内容多几个属性（字号）；标记结构和内容一样才用带标记的译文。
                use = value if node.marks.marked == marks.marked else Translation(value.text)
                if write_translated_paragraph(marks, use, context) is None:
                    missed = True
                else:
                    touched = True
            if touched:
                changed.append((shape, before))
        _shrink_drawing(self.drawing, changed)
        return missed


def _emu(value, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default


def _metrics(body) -> list[ParagraphMetrics]:
    found = []
    for marks in _paragraphs(body):
        paragraph = marks.paragraph
        sizes = [_emu(item.get("sz"), _DEFAULT_SIZE) for item in paragraph.iter(A_RPR)] or [_DEFAULT_SIZE]
        line = paragraph.find(f"{{{NS_A}}}pPr/{{{NS_A}}}lnSpc/{{{NS_A}}}spcPct")
        factor = _emu(line.get("val"), 100000) / 100000 if line is not None else 1.0
        found.append(ParagraphMetrics(marks.plain, max(sizes) / 100, line_factor=factor))
    return found


def _text_box(shape) -> tuple[float, float] | None:
    """形状里文字区的宽高（磅）：``dsp:txXfrm``，没有时用形状大小，减去内边距。"""
    item = shape.find(DSP_TX_XFRM)
    if item is None:
        sp_pr = shape.find(DSP_SP_PR)
        item = sp_pr.find(f"{{{NS_A}}}xfrm") if sp_pr is not None else None
    ext = item.find(f"{{{NS_A}}}ext") if item is not None else None
    body_pr = shape.find(f"{DSP_TX_BODY}/{{{NS_A}}}bodyPr")
    if ext is None:
        return None
    insets = {key: _emu(body_pr.get(key) if body_pr is not None else None, value) for key, value in _DEFAULT_INSETS.items()}
    width = (_emu(ext.get("cx"), 0) - insets["lIns"] - insets["rIns"]) / EMU_PER_PT
    height = (_emu(ext.get("cy"), 0) - insets["tIns"] - insets["bIns"]) / EMU_PER_PT
    return (width, height) if width > 0 and height > 0 else None


def _shrink_drawing(drawing, changed: list[tuple[object, list[ParagraphMarks]]]) -> None:
    """译文放不下时，排版结果里所有带字形状的字号按同一比例缩小（下限 60%）。"""
    scale = 1.0
    for shape, before in changed:
        box = _text_box(shape)
        body = shape.find(DSP_TX_BODY)
        if box is None or body is None:
            continue
        body_pr = body.find(f"{{{NS_A}}}bodyPr")
        original = [ParagraphMetrics(marks.plain, 1) for marks in before]
        needed, _fits = fitting_scale(
            original,
            _metrics(body),
            width_pt=box[0],
            height_pt=box[1],
            wrap=body_pr is None or body_pr.get("wrap") != "none",
            min_scale=MIN_SCALE,
        )
        scale = min(scale, needed)
    if scale >= 1.0:
        return
    for shape in drawing.iter(DSP_SP):
        body = shape.find(DSP_TX_BODY)
        if body is None or not _paragraphs(body):
            continue
        for item in body.iter(A_RPR, A_END_PARA_RPR):
            item.set("sz", str(max(100, round(_emu(item.get("sz"), _DEFAULT_SIZE) * scale))))


def diagram_text(deck: _Deck, slide: str, frame, owner_context: str) -> DiagramText | None:
    """幻灯片上 SmartArt 框对应的内容和排版结果；找不到内容部件时返回 None。"""
    rel_ids = next((item for item in frame.iter(DGM_REL_IDS)), None)
    if rel_ids is None:
        return None
    slide_rels = {rel.id: rel for rel in deck.rels(slide)}
    data_rel = slide_rels.get(str(rel_ids.get(_r("dm")) or ""))
    if data_rel is None or data_rel.external or data_rel.type != REL_DIAGRAM_DATA:
        return None
    root = deck.root(data_rel.target)
    if root is None or root.find(DGM_PT_LST) is None:
        return None
    drawing_part = ""
    extension = next((item for item in root.iter(DSP_DATA_MODEL_EXT)), None)
    rel_id = str(extension.get("relId") or "") if extension is not None else ""
    if rel_id:
        own_rels = {rel.id: rel for rel in deck.rels(data_rel.target)}
        for rels in (slide_rels, own_rels):
            rel = rels.get(rel_id)
            if rel is not None and not rel.external and rel.type == REL_DIAGRAM_DRAWING:
                drawing_part = rel.target
                break
    drawing = deck.root(drawing_part) if drawing_part and deck.package.has(drawing_part) else None
    if drawing is None or etree.QName(drawing).localname != "drawing":
        drawing_part, drawing = "", None
    return DiagramText(data_rel.target, root, owner_context, drawing_part, drawing)


# ── 保存后核对 ────────────────────────────────────────────────────────────────


def _verify_data(old: bytes, new: bytes) -> str:
    """节点一个不少、连接关系不变、段落结构正常、没有残留标记。"""
    try:
        before, after = _parse(old), _parse(new)
    except etree.XMLSyntaxError:
        return "SmartArt 内容部件无法解析"
    ids_before = [point.get("modelId") for point in before.iter(DGM_PT)]
    ids_after = [point.get("modelId") for point in after.iter(DGM_PT)]
    if ids_before != ids_after:
        return "SmartArt 的节点变了"
    links = [etree.tostring(item, method="c14n") for root in (before, after) for item in root.iter(DGM_CXN_LST)]
    if links[: len(links) // 2] != links[len(links) // 2 :]:
        return "SmartArt 的连接关系变了"
    return text_problem("SmartArt", after)


def _geometry(root) -> list[bytes]:
    found = []
    for shape in root.iter(DSP_SP):
        for tag in (DSP_SP_PR, DSP_TX_XFRM):
            item = shape.find(tag)
            xfrm = item.find(f"{{{etree.QName(A_P).namespace}}}xfrm") if item is not None and tag == DSP_SP_PR else item
            found.append(etree.tostring(xfrm, method="c14n") if xfrm is not None else b"")
    return found


def _verify_drawing(old: bytes, new: bytes) -> str:
    """形状一个不少、位置大小不变、段落结构正常、没有残留标记。"""
    try:
        before, after = _parse(old), _parse(new)
    except etree.XMLSyntaxError:
        return "SmartArt 显示部件无法解析"
    if [shape.get("modelId") for shape in before.iter(DSP_SP)] != [shape.get("modelId") for shape in after.iter(DSP_SP)]:
        return "SmartArt 的形状变了"
    if _geometry(before) != _geometry(after):
        return "SmartArt 里形状的位置或大小变了"
    return text_problem("SmartArt", after)


__all__ = ["REASON_DRAWING_NOT_SYNCED", "DiagramText", "diagram_text"]
