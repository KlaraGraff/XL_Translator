"""Word 正文之外、但属于文档内容的文字：文本框和脚注/尾注。

python-docx 的段落遍历只走 body 的直接子节点，文本框（``w:txbxContent``，挂在
run 里的 DrawingML 或 VML 图形中）和脚注/尾注（独立的 footnotes.xml / endnotes.xml
部件）都走不到。这里把它们各自当成一篇「小文档」（story）：每个文本框、每条脚注
或尾注是一个 story，里面的段落和正文段落一样按整段翻译、进同一个去重词库，写回
时复用正文的段落改写函数。

写回规则跟正文对齐：
  - 原文 + 译文：在原段后面插一段译文（沿用原段格式）；
  - 仅显示译文：原段原地换成译文，脚注编号、图片之类的非文字节点原样留在原位。

两处和正文不一样：
  - 文本框尺寸是固定的。按框的大小和字号估算排版，放不下就按比例缩小这个文本框
    里的字号，缩到下限仍放不下的报给调用方进质量报告；
  - 同一个文本框在文件里常写两遍：DrawingML 正本（``mc:Choice``）和给旧版 Word
    看的 VML 备份（``mc:Fallback``）。只翻正本，写完把正本的内容复制到备份，两边
    看到的一致。

补译模式（只翻还没译过的）：只看像源语言的段落，后面紧跟着一段目标语言的段落就算
已经译过。
"""
from __future__ import annotations

import math
import unicodedata
from copy import deepcopy
from dataclasses import dataclass, field

from docx.opc.part import XmlPart
from docx.oxml import parse_xml
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph
from lxml import etree

from core.translation_coverage import looks_like_source_text, looks_like_target_text

KIND_TEXTBOX = "textbox"
KIND_FOOTNOTE = "footnote"
KIND_ENDNOTE = "endnote"
STORY_KINDS = frozenset({KIND_TEXTBOX, KIND_FOOTNOTE, KIND_ENDNOTE})

_MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
_MC_ALTERNATE = f"{{{_MC}}}AlternateContent"
_MC_CHOICE = f"{{{_MC}}}Choice"
_MC_FALLBACK = f"{{{_MC}}}Fallback"
_WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
_WPS = "http://schemas.microsoft.com/office/word/2010/wordprocessingShape"
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"

_NOTE_PARTS = {
    "/word/footnotes.xml": ("w:footnote", KIND_FOOTNOTE),
    "/word/endnotes.xml": ("w:endnote", KIND_ENDNOTE),
}
# 分隔线、续页分隔线这类条目是版式，不是作者写的注释。
_NOTE_LAYOUT_TYPES = frozenset({"separator", "continuationSeparator", "continuationNotice"})

# 文本框里的字缩到原来的这个比例还放不下，就不再缩，报进质量报告。
TEXTBOX_MIN_SCALE = 0.6
# 估算排版用的字宽和行高（相对字号）：西文按半个汉字宽、略放宽，行高按 1.25 倍。
_HALF_WIDTH_EM = 0.55
_LINE_HEIGHT_EM = 1.25
_EMU_PER_PT = 12700
_DEFAULT_INSETS = {"lIns": 91440, "rIns": 91440, "tIns": 45720, "bIns": 45720}


@dataclass
class _Story:
    kind: str
    location: str
    label: str
    paragraphs: list[Paragraph]
    doc: object
    # 文本框的 w:txbxContent；脚注/尾注为 None。
    container: object | None = None


@dataclass
class StoryWriteStats:
    textbox_paragraphs: int = 0
    note_paragraphs: int = 0
    shrunk_textboxes: int = 0
    highlight_count: int = 0
    highlight_skip_count: int = 0

    @property
    def total(self) -> int:
        return self.textbox_paragraphs + self.note_paragraphs


@dataclass
class _NotePart:
    part: object
    root: object
    live: bool


@dataclass
class WordStories:
    """一份已打开文档里的文本框和脚注/尾注。写完必须调 ``flush()``，改动才进文件。"""

    doc: object
    stories: list[_Story] = field(default_factory=list)
    _note_parts: list[_NotePart] = field(default_factory=list)

    @classmethod
    def load(cls, doc) -> "WordStories":
        result = cls(doc)
        result.stories.extend(_textbox_stories(doc))
        for part in doc.part.package.iter_parts():
            spec = _NOTE_PARTS.get(str(part.partname))
            if spec is None:
                continue
            if isinstance(part, XmlPart):
                note_part = _NotePart(part, part.element, live=True)
            else:
                try:
                    note_part = _NotePart(part, parse_xml(part.blob), live=False)
                except etree.XMLSyntaxError:
                    continue
            result._note_parts.append(note_part)
            result.stories.extend(_note_stories(doc, note_part.root, *spec))
        return result

    def flush(self, changed_textboxes: list) -> None:
        _sync_fallback_copies(changed_textboxes)
        for note_part in self._note_parts:
            if not note_part.live:
                note_part.part._blob = etree.tostring(
                    note_part.root, xml_declaration=True, encoding="UTF-8", standalone=True
                )


def _nearest_ancestor(element, tag: str):
    parent = element.getparent()
    while parent is not None:
        if parent.tag == tag:
            return parent
        parent = parent.getparent()
    return None


def _inside_fallback(element) -> bool:
    return _nearest_ancestor(element, _MC_FALLBACK) is not None


def _story_paragraphs(container, doc, own_tag: str) -> list[Paragraph]:
    """容器里属于它自己的段落（嵌套文本框里的段落归嵌套的那个 story）。"""
    tag = qn(own_tag)
    return [
        Paragraph(p, doc._body)
        for p in container.iter(qn("w:p"))
        if _nearest_ancestor(p, tag) is container and not _inside_fallback(p)
    ]


def _textbox_stories(doc) -> list[_Story]:
    stories: list[_Story] = []
    body = doc.element.body
    for index, box in enumerate(
        box for box in body.iter(qn("w:txbxContent")) if not _inside_fallback(box)
    ):
        paragraphs = _story_paragraphs(box, doc, "w:txbxContent")
        if paragraphs:
            stories.append(
                _Story(KIND_TEXTBOX, f"textbox[{index}]", f"文本框 {index + 1}", paragraphs, doc, box)
            )
    return stories


def _note_label(kind: str, note_id: str) -> str:
    return f"脚注 {note_id}" if kind == KIND_FOOTNOTE else f"尾注 {note_id}"


def _note_stories(doc, root, note_tag: str, kind: str) -> list[_Story]:
    stories: list[_Story] = []
    for note in root.iter(qn(note_tag)):
        if note.get(qn("w:type")) in _NOTE_LAYOUT_TYPES:
            continue
        note_id = note.get(qn("w:id")) or str(len(stories))
        paragraphs = _story_paragraphs(note, doc, note_tag)
        if paragraphs:
            stories.append(_Story(kind, f"{kind}[{note_id}]", _note_label(kind, note_id), paragraphs, doc))
    return stories


def _already_translated(paragraphs: list[Paragraph], index: int, *, target_lang: str, source_lang: str) -> bool:
    """补译：这一段后面紧跟着一段目标语言的段落，说明上次已经译过。"""
    if index + 1 >= len(paragraphs):
        return False
    following = (paragraphs[index + 1].text or "").strip()
    return bool(
        following
        and looks_like_target_text(following, source_lang=source_lang, target_lang=target_lang)
        and not looks_like_source_text(following, source_lang=source_lang, target_lang=target_lang)
    )


def iter_story_sources(
    stories: WordStories,
    *,
    target_lang: str,
    source_lang: str,
    untranslated_only: bool = False,
):
    """(story, 段序号, 段落, 原文)：需要翻译的段落，按文档顺序。"""
    from core.word_document import (
        _is_toc_or_field_paragraph,
        _is_translatable_source,
        _paragraph_source_text,
    )

    for story in stories.stories:
        for index, paragraph in enumerate(story.paragraphs):
            source = _paragraph_source_text(paragraph)
            if not _is_translatable_source(source, target_lang=target_lang, source_lang=source_lang):
                continue
            if _is_toc_or_field_paragraph(paragraph):
                continue
            if untranslated_only and (
                not looks_like_source_text(source, source_lang=source_lang, target_lang=target_lang)
                or _already_translated(story.paragraphs, index, target_lang=target_lang, source_lang=source_lang)
            ):
                continue
            yield story, index, paragraph, source


def apply_story_translations(
    doc,
    translations: dict[str, str],
    *,
    target_lang: str,
    source_lang: str,
    output_translation_only: bool = False,
    untranslated_only: bool = False,
    review_mark_map: dict[str, str] | None = None,
    highlight_policy: str = "",
    review_color_map: dict[str, str] | None = None,
    issue_callback=None,
) -> StoryWriteStats:
    """把文本框和脚注/尾注的译文写进已打开的文档（调用方负责 save）。"""
    from core.word_document import (
        _apply_paragraph_review_mark,
        _insert_translation_paragraph_after,
        _paragraph_has_field,
        _replace_paragraph_text,
        _replace_paragraph_text_around_fields,
        _resolve_translation,
    )

    stats = StoryWriteStats()
    stories = WordStories.load(doc)
    review_mark_map = review_mark_map or {}
    review_color_map = review_color_map or {}
    plan = list(
        iter_story_sources(
            stories,
            target_lang=target_lang,
            source_lang=source_lang,
            untranslated_only=untranslated_only,
        )
    )
    changed: dict[int, tuple[_Story, _BoxLayout | None]] = {}
    for story, _index, _paragraph, _source in plan:
        if story.kind == KIND_TEXTBOX and id(story) not in changed:
            changed[id(story)] = (story, None)
    for key, (story, _layout) in list(changed.items()):
        changed[key] = (story, _box_layout(story))

    written: set[int] = set()
    for story, _index, paragraph, source in plan:
        resolved = _resolve_translation(source, translations)
        review_mark = review_mark_map.get(source.strip())
        marked_paragraph = paragraph
        if resolved is not None:
            replace = resolved.replace_only or output_translation_only
            if replace and not _paragraph_has_field(paragraph):
                _replace_paragraph_text(paragraph, resolved.text, target_lang=target_lang)
            elif replace and _replace_paragraph_text_around_fields(
                paragraph, resolved.text, target_lang=target_lang
            ):
                pass
            else:
                marked_paragraph = _insert_translation_paragraph_after(
                    paragraph, resolved.text, target_lang=target_lang
                )
            written.add(id(story))
            if story.kind == KIND_TEXTBOX:
                stats.textbox_paragraphs += 1
            else:
                stats.note_paragraphs += 1
        if review_mark:
            if _apply_paragraph_review_mark(
                marked_paragraph, review_mark, highlight_policy, review_color_map
            ):
                stats.highlight_count += 1
            else:
                stats.highlight_skip_count += 1

    changed_boxes = []
    for story, before in changed.values():
        if id(story) not in written:
            continue
        changed_boxes.append(story.container)
        if before is None:
            continue
        after = _box_layout(story)
        scale, fits = _fitting_scale(before, after)
        if scale < 1:
            _shrink_story_text(story, scale)
            stats.shrunk_textboxes += 1
        if not fits and issue_callback is not None:
            issue_callback(
                {
                    "kind": "textbox_overflow",
                    "location": f"{story.location}.paragraph[0]",
                    "label": story.label,
                    "source": " ".join((p.text or "").strip() for p in story.paragraphs[:2]),
                }
            )
    stories.flush(changed_boxes)
    return stats


def extract_story_segments(
    doc,
    *,
    target_lang: str,
    source_lang: str,
    untranslated_only: bool = False,
):
    """文本框和脚注/尾注里要翻译的段落，作为 WordSegment 列表（不去重）。"""
    from core.word_document import WordSegment

    stories = WordStories.load(doc)
    return [
        WordSegment(
            source=source,
            kind=story.kind,
            location=f"{story.location}.paragraph[{index}]",
            section_path=story.label,
        )
        for story, index, _paragraph, source in iter_story_sources(
            stories,
            target_lang=target_lang,
            source_lang=source_lang,
            untranslated_only=untranslated_only,
        )
    ]


# ── VML 备份同步 ─────────────────────────────────────────────────────


def _outermost(element, tag: str):
    found = None
    parent = element.getparent()
    while parent is not None:
        if parent.tag == tag:
            found = parent
        parent = parent.getparent()
    return found


def _top_level_boxes(branch) -> list:
    tag = qn("w:txbxContent")
    return [box for box in branch.iter(tag) if not _within(box, branch, tag)]


def _within(box, branch, tag: str) -> bool:
    parent = box.getparent()
    while parent is not None and parent is not branch:
        if parent.tag == tag:
            return True
        parent = parent.getparent()
    return False


def _sync_fallback_copies(changed_boxes: list) -> None:
    """把改过的文本框正本复制到同一个 mc:AlternateContent 的 VML 备份里。"""
    handled: list = []
    for box in changed_boxes:
        alternate = _outermost(box, _MC_ALTERNATE)
        if alternate is None or any(alternate is seen for seen in handled):
            continue
        handled.append(alternate)
        choice = alternate.find(_MC_CHOICE)
        fallback = alternate.find(_MC_FALLBACK)
        if choice is None or fallback is None:
            continue
        sources = _top_level_boxes(choice)
        targets = _top_level_boxes(fallback)
        if not sources or len(sources) != len(targets):
            continue
        for source, target in zip(sources, targets):
            for child in list(target):
                target.remove(child)
            for child in source:
                target.append(deepcopy(child))


# ── 文本框放不下：估算排版、缩字号 ───────────────────────────────────


@dataclass(frozen=True)
class _BoxLayout:
    """框内可用宽高（磅）和每段的（显示宽度, 字号磅, 段前段后磅）。"""

    width: float
    height: float
    paragraphs: tuple[tuple[int, float, float], ...]


def _display_width(text: str) -> int:
    return sum(
        2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        for char in text
        if char not in "\r\n"
    )


def _int_attr(el, name: str, default: int) -> int:
    try:
        return int(el.get(name)) if el is not None and el.get(name) is not None else default
    except ValueError:
        return default


def _paragraph_spacing_pt(paragraph: Paragraph) -> float:
    total = 0.0
    for attr in ("space_before", "space_after"):
        value = None
        try:
            value = getattr(paragraph.paragraph_format, attr)
            style = paragraph.style
            while value is None and style is not None:
                value = getattr(style.paragraph_format, attr)
                style = style.base_style
        except Exception:  # noqa: BLE001 - 估算用，读不到按 0
            value = None
        total += value.pt if value is not None else 0.0
    return total


def _box_layout(story: _Story) -> _BoxLayout | None:
    """读 DrawingML 文本框的尺寸；读不到（纯 VML、自动调整框大小）返回 None，不缩。"""
    from core.word_document import _paragraph_effective_half_points

    box = story.container
    txbx = box.getparent()
    if txbx is None or txbx.tag != f"{{{_WPS}}}txbx":
        return None
    shape = txbx.getparent()
    body_pr = shape.find(f"{{{_WPS}}}bodyPr") if shape is not None else None
    if body_pr is not None and body_pr.find(f"{{{_A}}}spAutoFit") is not None:
        return None  # 「根据文字调整形状大小」：Word 自己会把框撑大
    holder = _nearest_ancestor(box, f"{{{_WP}}}anchor")
    if holder is None:
        holder = _nearest_ancestor(box, f"{{{_WP}}}inline")
    extent = holder.find(f"{{{_WP}}}extent") if holder is not None else None
    cx, cy = _int_attr(extent, "cx", 0), _int_attr(extent, "cy", 0)
    if cx <= 0 or cy <= 0:
        return None
    insets = {name: _int_attr(body_pr, name, value) for name, value in _DEFAULT_INSETS.items()}
    if body_pr is not None and body_pr.get("vert") not in (None, "horz"):
        cx, cy = cy, cx
    paragraphs = tuple(
        (
            _display_width(p.text or ""),
            _paragraph_effective_half_points(p) / 2,
            _paragraph_spacing_pt(p),
        )
        for p in _story_paragraphs(box, story.doc, "w:txbxContent")
    )
    return _BoxLayout(
        max(1.0, (cx - insets["lIns"] - insets["rIns"]) / _EMU_PER_PT),
        max(1.0, (cy - insets["tIns"] - insets["bIns"]) / _EMU_PER_PT),
        paragraphs,
    )


def _text_height(layout: _BoxLayout, scale: float) -> float:
    total = 0.0
    for units, size, spacing in layout.paragraphs:
        size *= scale
        line_width = units * _HALF_WIDTH_EM * size
        lines = max(1, math.ceil(line_width / layout.width))
        total += lines * _LINE_HEIGHT_EM * size + spacing
    return total


def _fitting_scale(before: _BoxLayout, after: _BoxLayout | None) -> tuple[float, bool]:
    """译文放进原框要把字号缩到多少（1 = 不用缩），以及缩到下限时是否放得下。

    原文本来就超出框的，以原文占的高度为准，不比原来更挤就行。
    """
    if after is None:
        return 1.0, True
    room = max(after.height, _text_height(before, 1.0))

    def fits(scale: float) -> bool:
        return _text_height(after, scale) <= room

    scale = 1.0
    while scale > TEXTBOX_MIN_SCALE and not fits(scale):
        scale = round(scale - 0.05, 2)
    scale = max(TEXTBOX_MIN_SCALE, scale)
    return scale, fits(scale)


def _shrink_story_text(story: _Story, scale: float) -> None:
    from docx.oxml import OxmlElement
    from docx.shared import Pt

    from core.word_document import _paragraph_effective_half_points, _rpr_half_points

    box = story.container
    for paragraph in _story_paragraphs(box, story.doc, "w:txbxContent"):
        base = _paragraph_effective_half_points(paragraph)
        for r in paragraph._p.iter(qn("w:r")):
            if _nearest_ancestor(r, qn("w:txbxContent")) is not box:
                continue
            r_pr = r.get_or_add_rPr()
            half_points = max(2, int(round((_rpr_half_points(r_pr) or base) * scale)))
            # 走 python-docx 的 setter，w:sz 才会落在 rPr 规定的位置上。
            r_pr.sz_val = Pt(half_points / 2)
            sz_cs = r_pr.find(qn("w:szCs"))
            if sz_cs is None:
                sz_cs = OxmlElement("w:szCs")
                r_pr.find(qn("w:sz")).addnext(sz_cs)
            sz_cs.set(qn("w:val"), str(half_points))
