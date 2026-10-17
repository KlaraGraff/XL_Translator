"""DrawingML 段内局部格式（PPT 的 ``a:p``）：读成文字、加 ``<gN>…</gN>`` 标记、按译文写回。

和 Word 的 ``core/word_inline_marks.py`` 同一套标记写法和校验（``strip_marks``、
``parse_marked``、``marks_match`` 直接用那边的）：一段里格式和主体不同的那几截（「注意：」
加粗、带链接的词、m² 的上标）用 ``<g1>…</g1>`` 包住随原文发出，模型把标记放到对应的
译文上，写回时每截按原来的格式各写一个片段。

和 Word 不同的地方：

- 一段里可以有换行（``a:br``）。文字里写成 ``\\n``，标记不跨行（``marks_match`` 按行核对）；
  换行处先关上当前这对标记，下一行同样的格式换一个新编号。
- 页码、日期这类自动字段（``a:fld``）各占一对标记，写回时放回原来的字段元素（id 不变），
  模型在标记里写了什么都不用。
- 判断「格式是否不同」时忽略语言、拼写检查、字距、字体名这些不影响外观的东西
  （中西文混排的段落常按文种拆成几个片段）。

保底与 Word 一致：结构不简单（段里有公式等不认识的元素）的段落不翻；标记少了、多了、
乱了，整段写进主体格式（字数最多的那种格式），不报错。片段数和原来一样时只换各片段的
文字，片段的格式原样不动（不重建）。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field

import re

from lxml import etree

from core.word_inline_marks import MAX_GROUPS_PER_UNIT, parse_marked, strip_marks

NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"


def _a(tag: str) -> str:
    return f"{{{NS_A}}}{tag}"


A_P = _a("p")
A_R = _a("r")
A_BR = _a("br")
A_FLD = _a("fld")
A_T = _a("t")
A_RPR = _a("rPr")
A_PPR = _a("pPr")
A_END_PARA_RPR = _a("endParaRPr")

_TAG_ID_RE = re.compile(r"<\s*g\s*(\d+)\s*>", re.IGNORECASE)
_PARAGRAPH_TAGS = frozenset({A_PPR, A_R, A_BR, A_FLD, A_END_PARA_RPR})

#: ``a:rPr`` 上不影响外观的属性：语言、拼写检查和智能标记的状态、字距、东亚排版细节。
_IGNORED_ATTRS = frozenset(
    {"lang", "altLang", "dirty", "err", "noProof", "smtClean", "smtId", "bmk", "kern", "spc", "kumimoji",
     "normalizeH"}
)
#: 这些值等于没写（不加粗、无下划线……）。
_NEUTRAL_VALUES = {
    "b": {"0", "false"},
    "i": {"0", "false"},
    "u": {"none"},
    "strike": {"noStrike"},
    "cap": {"none"},
    "baseline": {"0"},
}
#: ``a:rPr`` 里不影响「是不是同一种格式」的子元素：按文种分开的字体名、扩展数据。
_IGNORED_CHILDREN = frozenset({_a("latin"), _a("ea"), _a("cs"), _a("sym"), _a("extLst"), _a("rtl")})


@dataclass
class Piece:
    """段落里的一个片段：文字片段、换行或自动字段。"""

    element: object
    text: str

    @property
    def tag(self) -> str:
        return self.element.tag  # type: ignore[attr-defined]

    @property
    def is_break(self) -> bool:
        return self.tag == A_BR

    @property
    def is_field(self) -> bool:
        return self.tag == A_FLD

    @property
    def rpr(self):
        return self.element.find(A_RPR)  # type: ignore[attr-defined]


@dataclass(frozen=True)
class Group:
    """一对标记：这截译文用哪个片段的格式写，或者放回哪个字段。"""

    source: Piece


@dataclass
class ParagraphMarks:
    """一段的纯文字（去掉首尾空白）和标记版本。"""

    paragraph: object
    pieces: list[Piece]
    #: 去掉首尾空白后的整段文字，换行写成 ``\\n``。
    plain: str
    #: 带 ``<gN>`` 标记的版本；格式统一或不能加标记时为空。
    marked: str = ""
    groups: dict[int, Group] = field(default_factory=dict)
    #: 主体格式来自哪个片段（没有文字片段时为 None）。
    base: Piece | None = None
    #: 被去掉的首尾空白，写回时原样补上。
    lead: str = ""
    trail: str = ""


def _run_text(element) -> str:
    return "".join(node.text or "" for node in element.iter(A_T))


def read_pieces(paragraph) -> list[Piece] | None:
    """段落的片段；段里有不认识的元素（公式、内容控件）时返回 None，这一段不翻。"""
    pieces: list[Piece] = []
    for child in paragraph:
        tag = child.tag
        if not isinstance(tag, str):
            continue
        if tag not in _PARAGRAPH_TAGS:
            return None
        if tag == A_BR:
            pieces.append(Piece(child, "\n"))
        elif tag in (A_R, A_FLD):
            pieces.append(Piece(child, _run_text(child)))
    return pieces


def _canonical(element) -> tuple:
    attrs = tuple(sorted((str(key), str(value)) for key, value in element.attrib.items()))
    children = tuple(_canonical(child) for child in element if isinstance(child.tag, str))
    return (element.tag, attrs, children)


def look_key(rpr) -> tuple:
    """一个片段「看起来」的样子：只留影响外观的属性和子元素（颜色、链接、上下标……）。"""
    if rpr is None:
        return ()
    attrs = []
    for key, value in rpr.attrib.items():
        key = str(key)
        if key in _IGNORED_ATTRS or str(value) in _NEUTRAL_VALUES.get(key, ()):
            continue
        attrs.append((key, str(value)))
    children = tuple(
        _canonical(child)
        for child in rpr
        if isinstance(child.tag, str) and child.tag not in _IGNORED_CHILDREN
    )
    return (tuple(sorted(attrs)), children)


def paragraph_marks(paragraph, pieces: list[Piece]) -> ParagraphMarks:
    """算出一段的纯文字和标记版本（``groups`` 为空表示不需要标记）。"""
    full = "".join(piece.text for piece in pieces)
    plain = full.strip()
    lead = full[: len(full) - len(full.lstrip())]
    trail = full[len(full.rstrip()):] if plain else ""
    runs = [piece for piece in pieces if piece.tag == A_R and piece.text]
    weights: dict[tuple, int] = {}
    for piece in runs:
        look = look_key(piece.rpr)
        weights[look] = weights.get(look, 0) + len(piece.text.strip())
    base_look = max(weights, key=lambda look: (weights[look], -_first_index(runs, look))) if weights else None
    base = next((piece for piece in runs if look_key(piece.rpr) == base_look), None)
    result = ParagraphMarks(paragraph=paragraph, pieces=pieces, plain=plain, base=base, lead=lead, trail=trail)
    if not plain or strip_marks(plain) != plain:
        return result
    if any(piece.is_field and not piece.text.strip() for piece in pieces):
        # 没有文字的字段放不进标记里，按整段写法处理（字段留在原来的相对位置）。
        return result

    # 每一截归到哪一组：主体格式的文字不加标记；字段各自一组；只有空白的那截夹在同一组
    # 中间就并进去，否则归主体。
    keys: list[object] = []
    for index, piece in enumerate(pieces):
        if piece.is_break:
            keys.append("break")
        elif piece.is_field:
            keys.append(("field", index))
        else:
            look = look_key(piece.rpr)
            keys.append(None if look == base_look else ("look", look))
    for index, piece in enumerate(pieces):
        if piece.is_break or piece.is_field or piece.text.strip():
            continue
        before = keys[index - 1] if index else None
        after = keys[index + 1] if index + 1 < len(keys) else None
        same = before is not None and before == after and before != "break"
        keys[index] = before if same and before[0] == "look" else None  # type: ignore[index]

    groups: dict[int, Group] = {}
    parts: list[str] = []
    current: object = None
    offset = 0
    end_limit = len(full) - len(trail)
    for index, piece in enumerate(pieces):
        start, end = offset, offset + len(piece.text)
        offset = end
        lo, hi = max(start, len(lead)), min(end, end_limit)
        if lo >= hi:
            continue
        text = full[lo:hi]
        key = keys[index]
        if key == "break":
            if current is not None:
                parts.append(f"</g{len(groups)}>")
                current = None
            parts.append(text)
            continue
        if key != current:
            if current is not None:
                parts.append(f"</g{len(groups)}>")
            if key is not None:
                groups[len(groups) + 1] = Group(piece)
                parts.append(f"<g{len(groups)}>")
            current = key
        parts.append(text)
    if current is not None:
        parts.append(f"</g{len(groups)}>")
    if not groups or len(groups) > MAX_GROUPS_PER_UNIT:
        return result
    marked = "".join(parts)
    if strip_marks(marked) != plain:
        return result
    result.marked = marked
    result.groups = groups
    return result


def _first_index(pieces: list[Piece], look: tuple) -> int:
    return next(index for index, piece in enumerate(pieces) if look_key(piece.rpr) == look)


# ── 写回 ────────────────────────────────────────────────────────────────────


@dataclass
class _Out:
    """要写进段落的一截：文字和它沿用格式的那个原片段，或者一个原样放回的字段。"""

    text: str
    source: Piece | None
    field: Piece | None = None


def _segments_from_marked(marks: ParagraphMarks, marked_translation: str) -> list[_Out] | None:
    lines = marked_translation.split("\n")
    sent_lines = marks.marked.split("\n")
    if len(lines) != len(sent_lines):
        return None
    out: list[_Out] = []
    for line_number, (line, sent_line) in enumerate(zip(lines, sent_lines)):
        ids = [int(number) for number in _TAG_ID_RE.findall(sent_line)]
        if ids:
            pieces = parse_marked(line, ids)
        else:
            pieces = [(line, None)] if strip_marks(line) == line else None
        if pieces is None:
            return None
        if line_number:
            out.append(_Out("\n", marks.base))
        for text, group in pieces:
            if group is None:
                if text:
                    out.append(_Out(text, marks.base))
                continue
            source = marks.groups[group].source
            if source.is_field:
                out.append(_Out("", None, field=source))
            elif text:
                out.append(_Out(text, source))
    return out


def _segments_plain(marks: ParagraphMarks, translation: str) -> list[_Out]:
    """没有可用的标记：整段写进主体格式；字段放回它的文字在译文里出现的位置。"""
    base = marks.base
    leading: list[_Out] = []
    placed: list[_Out] = []
    trailing: list[_Out] = []
    rest = translation
    before = ""
    for piece in marks.pieces:
        if not piece.is_field:
            before += piece.text
            continue
        found = rest.find(piece.text) if piece.text else -1
        if found >= 0:
            if rest[:found]:
                placed.append(_Out(rest[:found], base))
            placed.append(_Out("", None, field=piece))
            rest = rest[found + len(piece.text):]
        else:
            # 字段的文字没在译文里出现：原来在段首的放段首，其余放段尾。
            (trailing if before.strip() else leading).append(_Out("", None, field=piece))
        before += piece.text
    if rest:
        placed.append(_Out(rest, base))
    return leading + placed + trailing


def translation_segments(marks: ParagraphMarks, translation: str, marked_translation: str = "") -> list[_Out]:
    """译文要写成哪几截。带标记的译文能用就按标记，否则整段写法。"""
    if marked_translation and marks.groups and strip_marks(marked_translation) == translation:
        segments = _segments_from_marked(marks, marked_translation)
        if segments is not None:
            return segments
    return _segments_plain(marks, translation)


def _new_run(text: str, source: Piece | None):
    run = etree.Element(A_R)
    rpr = source.rpr if source is not None else None
    run.append(deepcopy(rpr) if rpr is not None else etree.Element(A_RPR))
    t = etree.SubElement(run, A_T)
    t.text = text
    return run


def _new_break(source: Piece | None):
    br = etree.Element(A_BR)
    rpr = source.rpr if source is not None else None
    br.append(deepcopy(rpr) if rpr is not None else etree.Element(A_RPR))
    return br


def _merged(segments: list[_Out]) -> list[_Out]:
    """相邻、沿用同一个原片段格式的几截并成一截（首尾空白和正文并在一起）。"""
    merged: list[_Out] = []
    for segment in segments:
        last = merged[-1] if merged else None
        if last is not None and segment.field is None and last.field is None and last.source is segment.source:
            last.text += segment.text
        else:
            merged.append(_Out(segment.text, segment.source, segment.field))
    return merged


def _nodes(segments: list[_Out]) -> list[tuple[object, Piece | None]]:
    """[(新元素, 它沿用格式的原片段)]；文字里的换行拆成 ``a:br``。"""
    nodes: list[tuple[object, Piece | None]] = []
    for segment in _merged(segments):
        if segment.field is not None:
            nodes.append((deepcopy(segment.field.element), segment.field))
            continue
        lines = segment.text.split("\n")
        for number, line in enumerate(lines):
            if number:
                nodes.append((_new_break(segment.source), None))
            if line:
                nodes.append((_new_run(line, segment.source), segment.source))
    return nodes


def write_paragraph(marks: ParagraphMarks, translation: str, marked_translation: str = "") -> list[object]:
    """把译文写进这一段（首尾空白照原样），返回写过文字的片段元素（之后改语言用）。

    译文拆出来的片段和原来一一对应（同样的顺序、同样的格式来源）时只换各片段的文字；
    否则删掉原来的文字片段、换行和字段，按译文重新生成。
    """
    paragraph = marks.paragraph
    segments = translation_segments(marks, translation, marked_translation)
    if marks.lead:
        segments.insert(0, _Out(marks.lead, marks.base))
    if marks.trail:
        segments.append(_Out(marks.trail, marks.base))
    nodes = _nodes(segments)
    old = list(marks.pieces)
    if len(nodes) == len(old) and all(
        new.tag == piece.tag and _same_source(source, piece)  # type: ignore[attr-defined]
        for (new, source), piece in zip(nodes, old)
    ):
        written = []
        for (new, _source), piece in zip(nodes, old):
            if piece.tag == A_R:
                _set_text(piece.element, _run_text(new))
                written.append(piece.element)
        return written
    anchor = paragraph.find(A_END_PARA_RPR)  # type: ignore[attr-defined]
    for piece in old:
        paragraph.remove(piece.element)  # type: ignore[attr-defined]
    for new, _source in nodes:
        if anchor is not None:
            anchor.addprevious(new)
        else:
            paragraph.append(new)  # type: ignore[attr-defined]
    return [new for new, _source in nodes if new.tag in (A_R, A_BR)]  # type: ignore[attr-defined]


def _same_source(source: Piece | None, piece: Piece) -> bool:
    """新片段可以直接写进原来这个位置的片段：同一个片段、换行，或外观相同的文字片段。"""
    if source is piece or piece.is_break:
        return True
    return source is not None and piece.tag == A_R and look_key(source.rpr) == look_key(piece.rpr)


def _set_text(run, text: str) -> None:
    texts = run.findall(A_T)
    if not texts:
        etree.SubElement(run, A_T).text = text
        return
    texts[0].text = text
    for extra in texts[1:]:
        run.remove(extra)


def has_marks(text: str) -> bool:
    """文字里还留着 ``<gN>`` 标记（写完自检用）。"""
    return strip_marks(text) != text


__all__ = [
    "A_BR",
    "A_END_PARA_RPR",
    "A_FLD",
    "A_P",
    "A_PPR",
    "A_R",
    "A_RPR",
    "A_T",
    "NS_A",
    "Group",
    "ParagraphMarks",
    "Piece",
    "has_marks",
    "look_key",
    "paragraph_marks",
    "read_pieces",
    "translation_segments",
    "write_paragraph",
]
