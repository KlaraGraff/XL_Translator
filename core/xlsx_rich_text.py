"""Excel 单元格局部格式（富文本）：按片段读出，按片段写回。

一格里「注意：严禁带电作业」只有「严禁」加粗标红，在 xlsx 里存成几个 ``<r>`` 片段，
每个片段带自己的 ``<rPr>``。以前回填时整格写成一个纯文本，双语模式里原文那半截的
格式也跟着丢了。这里分两步保留：

1. 双语模式的原文：原来的几个片段原样照抄，接一个换行，再接译文。原文永远不丢格式。
2. 译文：格式和主体不同的那几截用 ``<g1>…</g1>`` 包住随原文发出（标记格式与 Word、
   PPT 共用，校验函数见 core/word_inline_marks.py），译文回来后按标记拆成片段，
   每截照抄原片段的 ``<rPr>``。标记对不上时，译文写成一个片段，只保留所有片段都
   一样的那部分格式（字号、字体……），不会把某一截的加粗或颜色铺满整段。

写出的片段拼起来必须和原来的整格纯文本一字不差，否则退回纯文本写法；所以这里不会
改变任何一格显示的文字。共享字符串表不动，富文本照旧写成 inlineStr。
"""
from __future__ import annotations

import copy
import re
import zipfile
from dataclasses import dataclass, field

from lxml import etree

from core.word_inline_marks import MAX_GROUPS_PER_UNIT, parse_marked, strip_marks

NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"

_LITERAL_TAG_RE = re.compile(r"</?g\d+>", re.IGNORECASE)
_OPEN_TAG_RE = re.compile(r"<\s*g\s*(\d+)\s*>", re.IGNORECASE)
# 和 xlsx_patcher._ILLEGAL_XML_CHAR_RE 同一套：XML 1.0 不接受的码点
_ILLEGAL_XML_CHAR_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff﷐-﷯￾￿]")

# 不影响「看起来是不是同一种格式」的属性：中西文混排时片段常按字体拆开
_IGNORED_RPR = frozenset(("rFont", "charset", "family", "scheme"))
_ON_OFF = frozenset(("b", "i", "strike", "outline", "shadow", "condense", "extend"))


def _m(tag: str) -> str:
    return f"{{{NS_MAIN}}}{tag}"


def _local(element) -> str:
    tag = element.tag
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


@dataclass(frozen=True)
class RichRun:
    text: str
    rpr: object | None
    element: object


@dataclass
class RichCell:
    """一格富文本：原片段、带标记的原文（去掉首尾空白），以及每对标记对应的格式。"""

    runs: list[RichRun]
    plain_text: str
    marked_text: str
    groups: dict[int, object | None] = field(default_factory=dict)
    base_rpr: object | None = None
    common_rpr: object | None = None


def string_item_runs(item) -> list[RichRun] | None:
    """``<si>`` / ``<is>`` 里的片段；不是富文本（没有 ``<r>``）时返回 None。"""
    if item is None:
        return None
    runs: list[RichRun] = []
    for child in item:
        tag = _local(child)
        if tag == "t":
            # 规范里 <t> 和 <r> 二选一；两样都有的不认识，按纯文本处理
            return None
        if tag != "r":
            continue
        text = "".join(node.text or "" for node in child if _local(node) == "t")
        runs.append(RichRun(text=text, rpr=child.find(_m("rPr")), element=child))
    return runs or None


def _on(element) -> bool:
    return str(element.get("val") or "").lower() not in ("0", "false", "off", "none")


def _look_key(rpr) -> tuple:
    if rpr is None:
        return ()
    items = []
    for child in rpr:
        tag = _local(child)
        if not tag or tag in _IGNORED_RPR:
            continue
        if tag in _ON_OFF:
            if _on(child):
                items.append((tag,))
            continue
        if tag == "u" and str(child.get("val") or "").lower() == "none":
            continue
        items.append((tag, tuple(sorted(child.attrib.items()))))
    return tuple(sorted(items))


def _canonical(element) -> bytes:
    return etree.tostring(element, method="c14n")


def _common_rpr(runs: list[RichRun]):
    """所有带文字的片段都一样的那部分格式；没有片段带格式就是 None（沿用单元格字体）。"""
    texted = [run for run in runs if run.text]
    if not texted or any(run.rpr is None for run in texted):
        return None
    first = texted[0].rpr
    others = [{_canonical(child) for child in run.rpr} for run in texted[1:]]
    kept = [child for child in first if all(_canonical(child) in other for other in others)]
    if not kept:
        return None
    rpr = etree.Element(first.tag)
    for child in kept:
        rpr.append(copy.deepcopy(child))
    return rpr


def rich_cell(runs: list[RichRun] | None) -> RichCell | None:
    """把片段整理成带标记的原文。格式统一、片段太碎、原文里本来就有标记样的文字时，
    ``groups`` 为空（只照抄原文、译文不分片段）。"""
    if not runs:
        return None
    plain_full = "".join(run.text for run in runs)
    plain = plain_full.strip()
    if not plain:
        return None
    common = _common_rpr(runs)

    # 按换行拆开：标记不能跨行（译文一行对一行地校验，见 word_inline_marks.marks_match）
    pieces: list[tuple[str, tuple, int]] = []  # (文字, 格式, 来自第几个片段)
    for index, run in enumerate(runs):
        look = _look_key(run.rpr)
        for part in re.split(r"(\n)", run.text):
            if part:
                pieces.append((part, look, index))

    weights: dict[tuple, int] = {}
    for text, look, _index in pieces:
        if text != "\n":
            weights[look] = weights.get(look, 0) + len(text.strip())
    first_seen = {look: position for position, (_t, look, _i) in reversed(list(enumerate(pieces)))}
    base_look = max(weights, key=lambda look: (weights[look], -first_seen[look]))
    base_rpr = next((runs[i].rpr for _t, look, i in pieces if look == base_look), None)

    keys: list[tuple | None] = [None if look == base_look else look for _t, look, _i in pieces]
    # 只有空白的那截夹在同一种格式中间就并进去，否则归主体
    for position, (text, _look, _index) in enumerate(pieces):
        if text == "\n":
            keys[position] = None
        elif not text.strip():
            before = keys[position - 1] if position else None
            after = keys[position + 1] if position + 1 < len(keys) else None
            keys[position] = before if before is not None and before == after else None

    lead = len(plain_full) - len(plain_full.lstrip())
    end_limit = len(plain_full) - (len(plain_full) - len(plain_full.rstrip()))
    groups: dict[int, object | None] = {}
    parts: list[str] = []
    next_id = 1
    current: tuple | None = None
    offset = 0
    for position, (text, _look, index) in enumerate(pieces):
        start, end = offset, offset + len(text)
        offset = end
        lo, hi = max(start, lead), min(end, end_limit)
        if lo >= hi:
            continue
        key = keys[position]
        if key != current:
            if current is not None:
                parts.append(f"</g{next_id - 1}>")
            if key is not None:
                groups[next_id] = runs[index].rpr
                parts.append(f"<g{next_id}>")
                next_id += 1
            current = key
        parts.append(plain_full[lo:hi])
    if current is not None:
        parts.append(f"</g{next_id - 1}>")
    marked = "".join(parts)

    usable = (
        bool(groups)
        and len(groups) <= MAX_GROUPS_PER_UNIT
        and not _LITERAL_TAG_RE.search(plain)
        and strip_marks(marked) == plain
    )
    if not usable:
        groups, marked = {}, plain
    return RichCell(
        runs=runs,
        plain_text=plain,
        marked_text=marked,
        groups=groups,
        base_rpr=base_rpr,
        common_rpr=common,
    )


def collect_marked_sources(xlsx_path, wanted: set[str] | None = None) -> dict[str, str]:
    """工作簿里带局部格式的文字：{整格纯文本（去首尾空白）: 带标记的原文}。

    读共享字符串表和各表里的 inlineStr。同一段文字格式不同时取先遇到的那种；写回时
    每格按自己的片段重新核对，对不上的那格退回整段写法。读不了的包返回空表（翻译照旧）。
    """
    found: dict[str, str] = {}

    def take(item) -> None:
        cell = rich_cell(string_item_runs(item))
        if cell is None or not cell.groups:
            return
        if wanted is not None and cell.plain_text not in wanted:
            return
        found.setdefault(cell.plain_text, cell.marked_text)

    parser = etree.XMLParser(resolve_entities=False, huge_tree=True, remove_blank_text=False)
    try:
        with zipfile.ZipFile(xlsx_path) as archive:
            for name in archive.namelist():
                lowered = name.lower()
                if lowered.endswith("sharedstrings.xml"):
                    root = etree.fromstring(archive.read(name), parser)
                    for item in root:
                        if _local(item) == "si":
                            take(item)
                elif lowered.startswith("xl/worksheets/") and lowered.endswith(".xml"):
                    data = archive.read(name)
                    if b"inlineStr" not in data:
                        continue
                    root = etree.fromstring(data, parser)
                    for item in root.iter(_m("is")):
                        take(item)
    except (OSError, zipfile.BadZipFile, etree.XMLSyntaxError, KeyError):
        return {}
    return found


def _split_marked(marked: str, sent: str, groups: dict[int, object | None], base_rpr):
    """带标记的译文 → [(文字, rPr)]；按行对照发出的原文校验，对不上返回 None。"""
    sent_lines = sent.split("\n")
    lines = marked.split("\n")
    if len(lines) != len(sent_lines):
        return None
    out: list[tuple[str, object | None]] = []
    for number, (line, sent_line) in enumerate(zip(lines, sent_lines)):
        if number:
            out.append(("\n", base_rpr))
        ids = [int(group) for group in _OPEN_TAG_RE.findall(sent_line)]
        pieces = parse_marked(line, ids)
        if pieces is None:
            return None
        for text, group in pieces:
            if text:
                out.append((text, base_rpr if group is None else groups.get(group)))
    return out


def _fit_edges(pieces: list[tuple[str, object | None]], target: str, edge_rpr):
    """片段拼起来和 target 只差首尾空白时，把首尾空白换成 target 的。"""
    joined = "".join(text for text, _ in pieces)
    if joined == target:
        return pieces
    if joined.strip() != target.strip() or not target.strip():
        return None
    trimmed = list(pieces)
    while trimmed and not trimmed[0][0].strip():
        trimmed.pop(0)
    while trimmed and not trimmed[-1][0].strip():
        trimmed.pop()
    trimmed[0] = (trimmed[0][0].lstrip(), trimmed[0][1])
    trimmed[-1] = (trimmed[-1][0].rstrip(), trimmed[-1][1])
    lead = target[: len(target) - len(target.lstrip())]
    trail = target[len(target.rstrip()):]
    if lead:
        trimmed.insert(0, (lead, edge_rpr))
    if trail:
        trimmed.append((trail, edge_rpr))
    return trimmed


def _new_run(text: str, rpr):
    run = etree.Element(_m("r"))
    if rpr is not None:
        run.append(copy.deepcopy(rpr))
    t = etree.SubElement(run, _m("t"))
    t.set(XML_SPACE, "preserve")
    t.text = text
    return run


def plan_rich_runs(
    cell: RichCell,
    *,
    cell_text: str,
    source_text: str,
    separator: str,
    bilingual: bool,
    marked_entry: tuple[str, str] | None,
) -> tuple[list, bool] | None:
    """要写进 ``<is>`` 的 ``<r>`` 列表，以及译文是否按标记分了片段。

    ``cell_text`` 是整段写法本来要写的文字（已清理非法字符、可能截断），写出的片段拼起来
    必须和它完全一样；做不到返回 None，调用方按整段写法写。
    """
    prefix = source_text + separator if bilingual else ""
    if bilingual and (not cell_text.startswith(prefix) or "".join(run.text for run in cell.runs) != source_text):
        return None
    translation = cell_text[len(prefix):]
    if not translation:
        return None

    pieces = None
    if cell.groups and marked_entry is not None:
        sent, marked = marked_entry
        if sent == cell.marked_text and strip_marks(marked).strip() == translation.strip():
            split = _split_marked(marked, sent, cell.groups, cell.base_rpr)
            if split is not None:
                split = [(_ILLEGAL_XML_CHAR_RE.sub("", text), rpr) for text, rpr in split]
                pieces = _fit_edges([item for item in split if item[0]], translation, cell.base_rpr)
    marked_used = pieces is not None
    if pieces is None:
        if not bilingual and cell.common_rpr is None:
            return None  # 和整段写法完全一样，不必拆
        pieces = [(translation, cell.common_rpr)]

    nodes = []
    if bilingual:
        nodes.extend(copy.deepcopy(run.element) for run in cell.runs)
        nodes.append(_new_run(separator, cell.common_rpr))
    nodes.extend(_new_run(text, rpr) for text, rpr in pieces if text)
    written = "".join("".join(t.text or "" for t in node if _local(t) == "t") for node in nodes)
    if written != cell_text:
        return None
    return nodes, marked_used


def write_rich_inline(cell_element, nodes) -> None:
    """单元格改成 inlineStr，内容是这些 ``<r>``（调用方已删掉旧的 v / is / f）。"""
    cell_element.set("t", "inlineStr")
    inline = etree.SubElement(cell_element, _m("is"))
    for node in nodes:
        # 注音（rPh）引用的是原文字符位置，照抄的片段里本来就没有；这里只放 r
        inline.append(node)


def restyle_runs(inline, *, size_ratio: float | None = None, font_color_argb: str | None = None) -> None:
    """锁定行高缩字、复核标红字时，片段自带的字号和颜色会盖过单元格样式，一并改掉。"""
    for rpr in inline.iter(_m("rPr")):
        if size_ratio is not None:
            size = rpr.find(_m("sz"))
            if size is not None:
                try:
                    value = float(size.get("val") or 0)
                except ValueError:
                    value = 0
                if value > 0:
                    scaled = round(value * size_ratio, 1)
                    size.set("val", str(int(scaled)) if scaled.is_integer() else str(scaled))
        if font_color_argb is not None:
            color = rpr.find(_m("color"))
            if color is not None:
                for key in list(color.attrib):
                    del color.attrib[key]
                color.set("rgb", font_color_argb)
