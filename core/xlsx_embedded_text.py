"""Excel 单元格之外的文字：文本框/形状里的字和批注。

抽取与写回都按「段」走：文本框按 DrawingML 的段落（``a:p``）整段翻译，批注按整条
翻译。和单元格一样进全局去重词库、记忆库、质量检查和残留修补，写回时按译文表查。

写回规则跟单元格对齐：
  - 双语：文本框在原段后面插一段译文（沿用原段的段落格式和第一个文字片段的字体），
    批注在原文后换行接译文；
  - 仅显示译文：原段只留一个文字片段，换成译文；批注作者署名（「张三:」）保留。
文本框尺寸是固定的：按框的大小和字号估算译文占多少行，放不下就按比例缩小这个文本框
里的字号，缩到下限仍放不下的记进复核清单。

新版对话式批注（threadedComments）Excel 还会在传统批注里留一份兼容副本，副本里的
原文同步替换，两边看到的一致。

抽取和写回各自独立地按同一顺序遍历同一份文件，用部件名 + 段序号做定位键：抽取时
标记哪些键需要翻译（补译模式下已有译文的段不再送翻），写回时只改这些键。
"""
from __future__ import annotations

import copy
import math
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from core.translation_coverage import (
    looks_like_source_text,
    looks_like_target_text,
    split_existing_bilingual_text,
)
from core.text_box_fit import ParagraphMetrics, fitting_scale, wrapped_line_count
from core.translation_protocol import extract_replace_translation
from core.xlsx_patcher import (
    NS_A,
    NS_MAIN,
    NS_REL,
    NS_XDR,
    REL_TYPE_COMMENTS,
    REL_TYPE_DRAWING,
    REL_TYPE_OFFICE_DOCUMENT,
    REL_TYPE_THREADED_COMMENT,
    REL_TYPE_VML_DRAWING,
    REL_TYPE_WORKSHEET,
    XML_SPACE,
    _Package,
    _parse,
    _rels_path,
    _resolve_part,
    _ILLEGAL_XML_CHAR_RE,
    _serialize,
)

KIND_SHAPE = "shape"
KIND_COMMENT = "comment"
KIND_THREADED_COMMENT = "threaded_comment"

NS_THREADED = "http://schemas.microsoft.com/office/spreadsheetml/2018/threadedcomments"

# 文本框里的字缩到原来的这个比例还放不下，就不再缩，记进复核清单。
SHAPE_TEXT_MIN_SCALE = 0.6
# 框的尺寸读不出来时退回按文字总宽度估：译文总宽度超出原文这么多以内不缩。
SHAPE_TEXT_GROWTH_TOLERANCE = 1.08
_EMU_PER_PT = 12700
# DrawingML 文字框默认内边距（EMU）：左右 0.1 英寸，上下 0.05 英寸。
_DEFAULT_INSETS = {"lIns": 91440, "rIns": 91440, "tIns": 45720, "bIns": 45720}
# 文字片段没写字号、文本框也没给默认值时，Excel 形状的默认字号（百分之一磅）。
DEFAULT_SHAPE_FONT_SIZE = 1100

REVIEW_CATEGORY_SHAPE_OVERFLOW = "shape_text_overflow"
REVIEW_ACTION_LISTED_ONLY = "listed_only"


def _a(tag: str) -> str:
    return f"{{{NS_A}}}{tag}"


def _x(tag: str) -> str:
    return f"{{{NS_XDR}}}{tag}"


def _m(tag: str) -> str:
    return f"{{{NS_MAIN}}}{tag}"


def _tc(tag: str) -> str:
    return f"{{{NS_THREADED}}}{tag}"


@dataclass(frozen=True)
class EmbeddedTextUnit:
    kind: str
    sheet: str
    key: str
    text: str
    # 文本框名（「TextBox 3」）或批注所在单元格，用来在复核清单里定位。
    label: str = ""


@dataclass
class EmbeddedTextOptions:
    """写回时用的开关与译文。``allowed_keys`` 为 None 表示不限制（全量翻译）。"""

    shapes: bool = False
    comments: bool = False
    translations: dict[str, str] = field(default_factory=dict)
    allowed_keys: frozenset[str] | None = None

    @property
    def enabled(self) -> bool:
        return self.shapes or self.comments


@dataclass
class EmbeddedWriteStats:
    shape_paragraphs: int = 0
    comments: int = 0
    shrunk_shapes: int = 0


# ── 遍历 ──────────────────────────────────────────────────────────────────────


@dataclass
class _SheetParts:
    name: str
    drawing: str = ""
    comments: str = ""
    threaded: str = ""
    vml: str = ""


def _sheet_parts(package: _Package) -> list[_SheetParts]:
    root_rels_data = package.read("_rels/.rels")
    if root_rels_data is None:
        return []
    workbook_part = ""
    for rel in _parse(root_rels_data):
        if rel.get("Type") == REL_TYPE_OFFICE_DOCUMENT:
            workbook_part = _resolve_part("", rel.get("Target") or "")
            break
    workbook_data = package.read(workbook_part) if workbook_part else None
    workbook_rels_data = package.read(_rels_path(workbook_part)) if workbook_part else None
    if workbook_data is None or workbook_rels_data is None:
        return []
    targets = {
        rel.get("Id"): _resolve_part(workbook_part, rel.get("Target") or "")
        for rel in _parse(workbook_rels_data)
        if rel.get("Type") == REL_TYPE_WORKSHEET
    }
    sheets_el = _parse(workbook_data).find(_m("sheets"))
    result: list[_SheetParts] = []
    if sheets_el is None:
        return result
    for el in sheets_el:
        if not isinstance(el.tag, str) or el.tag != _m("sheet"):
            continue
        sheet_part = targets.get(el.get(f"{{{NS_REL}}}id"))
        if not sheet_part or not package.has(sheet_part):
            continue
        parts = _SheetParts(name=str(el.get("name") or ""))
        rels_data = package.read(_rels_path(sheet_part))
        if rels_data is not None:
            for rel in _parse(rels_data):
                if str(rel.get("TargetMode") or "") == "External":
                    continue
                target = _resolve_part(sheet_part, rel.get("Target") or "")
                rel_type = rel.get("Type")
                if rel_type == REL_TYPE_DRAWING:
                    parts.drawing = target
                elif rel_type == REL_TYPE_COMMENTS:
                    parts.comments = target
                elif rel_type == REL_TYPE_THREADED_COMMENT:
                    parts.threaded = target
                elif rel_type == REL_TYPE_VML_DRAWING:
                    parts.vml = target
        result.append(parts)
    return result


def _text_shapes(drawing_root) -> list:
    """所有带文字框体的形状（含组合形状里的子形状），按文档顺序。"""
    return [sp for sp in drawing_root.iter(_x("sp")) if sp.find(_x("txBody")) is not None]


def _shape_label(sp) -> str:
    props = sp.find(f"{_x('nvSpPr')}/{_x('cNvPr')}")
    return str(props.get("name") or "") if props is not None else ""


def _paragraph_text(paragraph) -> str | None:
    """段落的纯文本；含域（页码、日期之类）的段落返回 None，不翻译。"""
    parts: list[str] = []
    for child in paragraph:
        if child.tag == _a("r"):
            t = child.find(_a("t"))
            parts.append(t.text or "" if t is not None else "")
        elif child.tag == _a("br"):
            parts.append("\n")
        elif child.tag == _a("fld"):
            return None
    return "".join(parts)


def _shape_paragraphs(drawing_root):
    """(键后缀, 形状, 段落, 段落文本)。段序号在整个绘图部件里连续编号。"""
    index = 0
    for sp in _text_shapes(drawing_root):
        for paragraph in sp.find(_x("txBody")).iter(_a("p")):
            yield f"p{index}", sp, paragraph, _paragraph_text(paragraph)
            index += 1


def _comment_runs(text_el) -> list:
    """批注正文里的文字节点：``<text><t/></text>`` 或 ``<text><r><t/></r>…</text>``。"""
    return [node for node in text_el.iter(_m("t"))]


def _comment_full_text(text_el) -> str:
    return "".join(node.text or "" for node in _comment_runs(text_el))


def _author_prefix(full_text: str, author: str) -> str:
    """传统批注常以「作者:」加换行开头，这一行是署名，不翻译。"""
    author = (author or "").strip()
    if not author:
        return ""
    head = f"{author}:"
    if not full_text.startswith(head):
        return ""
    rest = full_text[len(head):]
    newline = len(rest) - len(rest.lstrip("\r\n"))
    return head + rest[:newline]


def _comment_authors(comments_root) -> list[str]:
    authors_el = comments_root.find(_m("authors"))
    if authors_el is None:
        return []
    return [str(el.text or "") for el in authors_el.iter(_m("author"))]


def _legacy_comments(comments_root):
    """(引用单元格, 批注元素, 正文元素, 作者署名前缀)。"""
    authors = _comment_authors(comments_root)
    for comment in comments_root.iter(_m("comment")):
        text_el = comment.find(_m("text"))
        if text_el is None:
            continue
        try:
            author = authors[int(comment.get("authorId") or 0)]
        except (ValueError, IndexError):
            author = ""
        full_text = _comment_full_text(text_el)
        yield str(comment.get("ref") or ""), comment, text_el, _author_prefix(full_text, author)


def _threaded_refs(package: _Package, parts: _SheetParts) -> set[str]:
    if not parts.threaded:
        return set()
    data = package.read(parts.threaded)
    if data is None:
        return set()
    return {
        str(el.get("ref") or "")
        for el in _parse(data).iter(_tc("threadedComment"))
    }


# ── 抽取 ──────────────────────────────────────────────────────────────────────


def _is_pending(
    text: str,
    next_text: str | None,
    *,
    coverage: tuple[str, str] | None,
) -> bool:
    """补译模式下这一段还要不要翻：已经是译文、或后面紧跟着译文的不再翻。"""
    if coverage is None:
        return True
    source_lang, target_lang = coverage
    if split_existing_bilingual_text(text, source_lang=source_lang, target_lang=target_lang):
        return False
    if not looks_like_source_text(text, source_lang=source_lang, target_lang=target_lang):
        return False
    if next_text and looks_like_target_text(
        next_text, source_lang=source_lang, target_lang=target_lang
    ):
        return False
    return True


def collect_embedded_units(
    xlsx_path: str | Path,
    *,
    shapes: bool,
    comments: bool,
    coverage: tuple[str, str] | None = None,
) -> list[EmbeddedTextUnit]:
    """列出需要翻译的文本框段落和批注。

    ``coverage``：补译模式传 (源语言, 目标语言)，已经带译文的段落/批注不再列出。
    全量模式传 None，所有非空段落都列出（是否值得翻由调用方的 should_translate 判断）。
    """
    units: list[EmbeddedTextUnit] = []
    if not (shapes or comments):
        return units
    with _Package(Path(xlsx_path)) as package:
        for parts in _sheet_parts(package):
            if shapes and parts.drawing:
                data = package.read(parts.drawing)
                if data is not None:
                    rows = list(_shape_paragraphs(_parse(data)))
                    for position, (suffix, sp, _paragraph, text) in enumerate(rows):
                        if text is None or not text.strip():
                            continue
                        next_text = None
                        if position + 1 < len(rows) and rows[position + 1][1] is sp:
                            next_text = rows[position + 1][3]
                        if not _is_pending(text, next_text, coverage=coverage):
                            continue
                        units.append(
                            EmbeddedTextUnit(
                                kind=KIND_SHAPE,
                                sheet=parts.name,
                                key=f"{parts.drawing}#{suffix}",
                                text=text.strip(),
                                label=_shape_label(sp),
                            )
                        )
            if not comments:
                continue
            threaded_refs = _threaded_refs(package, parts)
            if parts.comments:
                data = package.read(parts.comments)
                if data is not None:
                    for ref, _comment, text_el, prefix in _legacy_comments(_parse(data)):
                        # 对话式批注的兼容副本跟着正本改，不单独送翻。
                        if ref in threaded_refs:
                            continue
                        body = _comment_full_text(text_el)[len(prefix):]
                        if not body.strip() or not _is_pending(body, None, coverage=coverage):
                            continue
                        units.append(
                            EmbeddedTextUnit(
                                kind=KIND_COMMENT,
                                sheet=parts.name,
                                key=f"{parts.comments}#{ref}",
                                text=body.strip(),
                                label=ref,
                            )
                        )
            if parts.threaded:
                data = package.read(parts.threaded)
                if data is not None:
                    for comment in _parse(data).iter(_tc("threadedComment")):
                        text_el = comment.find(_tc("text"))
                        body = str(text_el.text or "") if text_el is not None else ""
                        if not body.strip() or not _is_pending(body, None, coverage=coverage):
                            continue
                        units.append(
                            EmbeddedTextUnit(
                                kind=KIND_THREADED_COMMENT,
                                sheet=parts.name,
                                key=f"{parts.threaded}#{comment.get('id') or ''}",
                                text=body.strip(),
                                label=str(comment.get("ref") or ""),
                            )
                        )
    return units


# ── 写回 ──────────────────────────────────────────────────────────────────────


def _lookup(options: EmbeddedTextOptions, key: str, text: str) -> str:
    if options.allowed_keys is not None and key not in options.allowed_keys:
        return ""
    source = (text or "").strip()
    if not source:
        return ""
    translation = str(options.translations.get(source) or "").strip()
    if not translation:
        return ""
    translation = _ILLEGAL_XML_CHAR_RE.sub("", extract_replace_translation(translation)).strip()
    if not translation or translation == source:
        return ""
    return translation


def _display_width(text: str) -> int:
    return sum(
        2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        for char in text
        if char not in "\r\n"
    )


def _set_t(t_el, text: str) -> None:
    t_el.text = text
    if text != text.strip():
        t_el.set(XML_SPACE, "preserve")


def _new_run(template_rpr, text: str):
    run = _a_element("r")
    if template_rpr is not None:
        run.append(copy.deepcopy(template_rpr))
    t = _a_element("t")
    _set_t(t, text)
    run.append(t)
    return run


def _a_element(tag: str):
    from lxml import etree

    return etree.Element(_a(tag))


def _fill_paragraph(paragraph, text: str, template_rpr) -> None:
    """段落里只留新文字：按换行拆成 文字片段 + 换行 + 文字片段。"""
    insert_at = 0
    ppr = paragraph.find(_a("pPr"))
    if ppr is not None:
        insert_at = list(paragraph).index(ppr) + 1
    nodes = []
    for line_index, line in enumerate(text.split("\n")):
        if line_index:
            br = _a_element("br")
            if template_rpr is not None:
                br.append(copy.deepcopy(template_rpr))
            nodes.append(br)
        nodes.append(_new_run(template_rpr, line))
    for offset, node in enumerate(nodes):
        paragraph.insert(insert_at + offset, node)


def _first_run_rpr(paragraph):
    run = paragraph.find(_a("r"))
    return run.find(_a("rPr")) if run is not None else None


def _rewrite_shape_paragraph(paragraph, translation: str, *, translation_only: bool) -> None:
    template_rpr = _first_run_rpr(paragraph)
    if translation_only:
        template_rpr = copy.deepcopy(template_rpr) if template_rpr is not None else None
        for child in list(paragraph):
            if child.tag in {_a("r"), _a("br")}:
                paragraph.remove(child)
        _fill_paragraph(paragraph, translation, template_rpr)
        return
    new_paragraph = _a_element("p")
    ppr = paragraph.find(_a("pPr"))
    if ppr is not None:
        new_paragraph.append(copy.deepcopy(ppr))
    _fill_paragraph(new_paragraph, translation, template_rpr)
    end = paragraph.find(_a("endParaRPr"))
    if end is not None:
        new_paragraph.append(copy.deepcopy(end))
    paragraph.addnext(new_paragraph)


def _base_font_size(tx_body) -> int:
    for path in (f"{_a('lstStyle')}/{_a('lvl1pPr')}/{_a('defRPr')}",):
        el = tx_body.find(path)
        if el is not None and el.get("sz"):
            try:
                return int(el.get("sz"))
            except ValueError:
                pass
    return DEFAULT_SHAPE_FONT_SIZE


def _shrink_shape_text(tx_body, scale: float) -> None:
    base = _base_font_size(tx_body)
    # 固定行距（磅值）不随字号变，要一起缩，否则字小了行还是那么高。
    for line_spacing in tx_body.iter(_a("lnSpc")):
        points = line_spacing.find(_a("spcPts"))
        if points is not None:
            points.set("val", str(max(100, int(round(_emu_attr(points, "val", 0) * scale)))))
    for paragraph in tx_body.iter(_a("p")):
        ppr_default = paragraph.find(f"{_a('pPr')}/{_a('defRPr')}")
        paragraph_base = base
        if ppr_default is not None and ppr_default.get("sz"):
            try:
                paragraph_base = int(ppr_default.get("sz"))
            except ValueError:
                pass
        targets = []
        for run in paragraph:
            if run.tag in {_a("r"), _a("br")}:
                rpr = run.find(_a("rPr"))
                if rpr is None:
                    rpr = _a_element("rPr")
                    run.insert(0, rpr)
                targets.append(rpr)
            elif run.tag == _a("endParaRPr"):
                targets.append(run)
        for rpr in targets:
            try:
                size = int(rpr.get("sz") or paragraph_base)
            except ValueError:
                size = paragraph_base
            rpr.set("sz", str(max(100, int(round(size * scale)))))


@dataclass(frozen=True)
class _ShapeLayout:
    """估算文字排版用的量：框内可用宽高（磅，读不出为 None）和各段的排版量。"""

    width: float | None
    height: float | None
    wrap: bool
    paragraphs: tuple[ParagraphMetrics, ...]


def _emu_attr(el, name: str, default: int) -> int:
    try:
        return int(el.get(name)) if el is not None and el.get(name) is not None else default
    except ValueError:
        return default


def _paragraph_font_size(paragraph, base: int) -> int:
    for path in (f"{_a('r')}/{_a('rPr')}", f"{_a('pPr')}/{_a('defRPr')}", _a("endParaRPr")):
        el = paragraph.find(path)
        if el is not None and el.get("sz"):
            try:
                return int(el.get("sz"))
            except ValueError:
                pass
    return base


def _spacing_child(paragraph, level_ppr, tag: str):
    """段落自己写的行距/段距，没写就用文本框列表样式第一级的。"""
    for ppr in (paragraph.find(_a("pPr")), level_ppr):
        el = ppr.find(_a(tag)) if ppr is not None else None
        if el is not None:
            return el
    return None


def _spacing_value(el, size_pt: float) -> tuple[float | None, float | None]:
    """(倍数, 磅值)：a:spcPct 是千分之一个百分点，a:spcPts 是百分之一磅。"""
    if el is None:
        return None, None
    pct = el.find(_a("spcPct"))
    if pct is not None:
        return _emu_attr(pct, "val", 100000) / 100000, None
    pts = el.find(_a("spcPts"))
    if pts is not None:
        return None, _emu_attr(pts, "val", 0) / 100
    return None, None


def _paragraph_metrics(paragraph, base: int, level_ppr) -> ParagraphMetrics:
    size_pt = _paragraph_font_size(paragraph, base) / 100
    factor, exact = _spacing_value(_spacing_child(paragraph, level_ppr, "lnSpc"), size_pt)
    spacing = 0.0
    for tag in ("spcBef", "spcAft"):
        pct, pts = _spacing_value(_spacing_child(paragraph, level_ppr, tag), size_pt)
        spacing += pts if pts is not None else (pct or 0.0) * size_pt
    return ParagraphMetrics(
        text=_paragraph_text(paragraph) or "",
        size_pt=size_pt,
        line_factor=factor if factor is not None else 1.0,
        exact_line_pt=exact,
        spacing_pt=spacing,
    )


def _shape_layout(sp) -> _ShapeLayout:
    tx_body = sp.find(_x("txBody"))
    base = _base_font_size(tx_body)
    level_ppr = tx_body.find(f"{_a('lstStyle')}/{_a('lvl1pPr')}")
    paragraphs = tuple(_paragraph_metrics(p, base, level_ppr) for p in tx_body.iter(_a("p")))
    body_pr = tx_body.find(_a("bodyPr"))
    wrap = body_pr is None or body_pr.get("wrap") != "none"
    ext = sp.find(f"{_x('spPr')}/{_a('xfrm')}/{_a('ext')}")
    width = height = None
    cx, cy = _emu_attr(ext, "cx", 0), _emu_attr(ext, "cy", 0)
    if cx > 0 and cy > 0:
        insets = {name: _emu_attr(body_pr, name, value) for name, value in _DEFAULT_INSETS.items()}
        if body_pr is not None and body_pr.get("vert") not in (None, "horz"):
            cx, cy = cy, cx  # 竖排：按转过来的框算
        width = max(1.0, (cx - insets["lIns"] - insets["rIns"]) / _EMU_PER_PT)
        height = max(1.0, (cy - insets["tIns"] - insets["bIns"]) / _EMU_PER_PT)
    return _ShapeLayout(width, height, wrap, paragraphs)


def _fitting_scale(before: _ShapeLayout, after: _ShapeLayout) -> tuple[float, bool]:
    """译文放进原框要把字号缩到多少（1 = 不用缩），以及缩到下限时是否放得下。"""
    if after.width is None or after.height is None:
        old = sum(_display_width(p.text) for p in before.paragraphs)
        new = sum(_display_width(p.text) for p in after.paragraphs)
        if old <= 0 or new <= old * SHAPE_TEXT_GROWTH_TOLERANCE:
            return 1.0, True
        wanted = math.sqrt(old / new)  # 框的面积固定，文字占的面积随字号平方变化。
        return max(SHAPE_TEXT_MIN_SCALE, round(wanted, 2)), wanted >= SHAPE_TEXT_MIN_SCALE
    return fitting_scale(
        list(before.paragraphs),
        list(after.paragraphs),
        width_pt=after.width,
        height_pt=after.height,
        wrap=after.wrap,
        min_scale=SHAPE_TEXT_MIN_SCALE,
    )


def _apply_shapes(
    package: _Package,
    parts: _SheetParts,
    options: EmbeddedTextOptions,
    *,
    translation_only: bool,
    review_marks: dict[str, str],
    review_positions: list[dict[str, str]] | None,
    stats: EmbeddedWriteStats,
) -> None:
    data = package.read(parts.drawing)
    if data is None:
        return
    root = _parse(data)
    planned: list[tuple] = []
    for suffix, sp, paragraph, text in _shape_paragraphs(root):
        if text is None:
            continue
        translation = _lookup(options, f"{parts.drawing}#{suffix}", text)
        if translation:
            planned.append((sp, paragraph, text.strip(), translation))
    if not planned:
        return

    shapes = list({id(item[0]): item[0] for item in planned}.values())
    before = {id(sp): _shape_layout(sp) for sp in shapes}
    for sp, paragraph, source, translation in planned:
        _rewrite_shape_paragraph(paragraph, translation, translation_only=translation_only)
        stats.shape_paragraphs += 1
        category = review_marks.get(source)
        if category and review_positions is not None:
            review_positions.append(
                {
                    "worksheet": parts.name,
                    "cell": _shape_label(sp),
                    "category": category,
                    "action": REVIEW_ACTION_LISTED_ONLY,
                }
            )

    for sp in shapes:
        scale, fits = _fitting_scale(before[id(sp)], _shape_layout(sp))
        if scale >= 1:
            continue
        _shrink_shape_text(sp.find(_x("txBody")), scale)
        stats.shrunk_shapes += 1
        if not fits and review_positions is not None:
            review_positions.append(
                {
                    "worksheet": parts.name,
                    "cell": _shape_label(sp),
                    "category": REVIEW_CATEGORY_SHAPE_OVERFLOW,
                    "action": REVIEW_ACTION_LISTED_ONLY,
                }
            )
    package.write(parts.drawing, _serialize(root))


def _replace_comment_text(text_el, prefix: str, body: str) -> None:
    """保留署名所在的文字片段，正文换成 ``body``，字体沿用正文第一个片段。"""
    runs = [child for child in text_el if child.tag == _m("r")]
    plain = text_el.find(_m("t"))
    if not runs:
        if plain is None:
            plain = _m_element("t")
            text_el.append(plain)
        _set_t(plain, prefix + body)
        return
    consumed = 0
    keep: list = []
    body_rpr = None
    for run in runs:
        t = run.find(_m("t"))
        text = t.text or "" if t is not None else ""
        if prefix and consumed + len(text) <= len(prefix):
            keep.append(run)
            consumed += len(text)
            continue
        if body_rpr is None:
            body_rpr = run.find(_m("rPr"))
        text_el.remove(run)
    remaining_prefix = prefix[consumed:]
    run = _m_element("r")
    if body_rpr is not None:
        run.append(copy.deepcopy(body_rpr))
    t = _m_element("t")
    _set_t(t, remaining_prefix + body)
    t.set(XML_SPACE, "preserve")
    run.append(t)
    text_el.append(run)


def _m_element(tag: str):
    from lxml import etree

    return etree.Element(_m(tag))


def _apply_comments(
    package: _Package,
    parts: _SheetParts,
    options: EmbeddedTextOptions,
    *,
    translation_only: bool,
    review_marks: dict[str, str],
    review_positions: list[dict[str, str]] | None,
    stats: EmbeddedWriteStats,
) -> None:
    # 对话式批注：正本改写，同时记下「原文 → 新文」供兼容副本同步。
    fallback_edits: dict[str, list[tuple[str, str]]] = {}
    if parts.threaded:
        data = package.read(parts.threaded)
        if data is not None:
            root = _parse(data)
            changed = False
            for comment in root.iter(_tc("threadedComment")):
                text_el = comment.find(_tc("text"))
                if text_el is None:
                    continue
                body = str(text_el.text or "")
                translation = _lookup(
                    options, f"{parts.threaded}#{comment.get('id') or ''}", body
                )
                if not translation:
                    continue
                new_body = translation if translation_only else f"{body.rstrip()}\n{translation}"
                text_el.text = new_body
                changed = True
                stats.comments += 1
                ref = str(comment.get("ref") or "")
                fallback_edits.setdefault(ref, []).append((body, new_body))
                _note_review(review_marks, body, parts.name, ref, review_positions)
            if changed:
                package.write(parts.threaded, _serialize(root))

    if not parts.comments:
        return
    data = package.read(parts.comments)
    if data is None:
        return
    root = _parse(data)
    threaded_refs = _threaded_refs(package, parts)
    changed = False
    shown: dict[str, tuple[str, str, float]] = {}
    for ref, _comment, text_el, prefix in _legacy_comments(root):
        before = _comment_full_text(text_el)
        if ref in threaded_refs:
            edits = fallback_edits.get(ref)
            if not edits:
                continue
            for node in _comment_runs(text_el):
                value = node.text or ""
                for old, new in edits:
                    if old and old in value:
                        value = value.replace(old, new.replace("\n", "\n    "), 1)
                node.text = value
            changed = True
            shown[ref] = (before, _comment_full_text(text_el), _comment_font_size(text_el))
            continue
        body = _comment_full_text(text_el)[len(prefix):]
        translation = _lookup(options, f"{parts.comments}#{ref}", body)
        if not translation:
            continue
        new_body = translation if translation_only else f"{body.rstrip()}\n{translation}"
        _replace_comment_text(text_el, prefix, new_body)
        changed = True
        stats.comments += 1
        shown[ref] = (before, _comment_full_text(text_el), _comment_font_size(text_el))
        _note_review(review_marks, body, parts.name, ref, review_positions)
    if changed:
        package.write(parts.comments, _serialize(root))
    if shown and parts.vml:
        _grow_comment_boxes(package, parts.vml, shown)


# ── 批注框跟着译文变大 ────────────────────────────────────────────────────────
# 批注框的大小记在工作表的 VML 绘图里（v:shape 的 style 宽高 + x:Anchor 锚点），
# 不会随文字自动撑大：原文下面接一行译文，悬停时译文就被框挡住。只加高、不改宽。
# VML 是 Excel 沿用下来的老格式，不一定是规整的 XML，这里按文本改，不整体解析。

_VML_SHAPE_RE = re.compile(
    r"<(?P<p>(?:[\w.-]+:)?)shape\b(?P<attrs>[^>]*)>(?P<body>.*?)</(?P=p)shape>", re.S
)
_VML_STYLE_RE = re.compile(r"""(?P<q>["'])(?P<style>[^"']*)(?P=q)""")
_VML_SIZE_RE = re.compile(r"(?P<key>width|height)\s*:\s*(?P<num>[\d.]+)(?P<unit>pt|px|in|cm|mm)?", re.I)
_VML_ANCHOR_RE = re.compile(r"(<(?:[\w.-]+:)?Anchor>)([^<]*)(</)")
_PT_PER_UNIT = {"pt": 1.0, "px": 0.75, "in": 72.0, "cm": 72 / 2.54, "mm": 72 / 25.4, "": 0.75}
# 批注框里文字到边框的距离（VML 文本框默认左右 0.1 英寸、上下 0.05 英寸）。
_COMMENT_INSET_PT = (7.2, 3.6)
_DEFAULT_COMMENT_FONT_PT = 9.0
_DEFAULT_ROW_PT = 15.0


def _comment_font_size(text_el) -> float:
    for sz in text_el.iter(_m("sz")):
        try:
            return float(sz.get("val"))
        except (TypeError, ValueError):
            continue
    return _DEFAULT_COMMENT_FONT_PT


def _cell_index(ref: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"\$?([A-Za-z]{1,3})\$?(\d+)", ref or "")
    if not match:
        return None
    column = 0
    for char in match.group(1).upper():
        column = column * 26 + ord(char) - 64
    return int(match.group(2)) - 1, column - 1


def _vml_number(body: str, tag: str) -> int | None:
    match = re.search(rf"<(?:[\w.-]+:)?{tag}>\s*(\d+)\s*<", body)
    return int(match.group(1)) if match else None


def _grow_comment_boxes(package: _Package, vml_part: str, shown: dict[str, tuple[str, str, float]]) -> None:
    data = package.read(vml_part)
    if data is None:
        return
    text = data.decode("utf-8", errors="surrogateescape")
    wanted = {index: value for ref, value in shown.items() if (index := _cell_index(ref)) is not None}

    def grow(match: re.Match) -> str:
        body = match.group("body")
        if "Note" not in body and "Note" not in match.group("attrs"):
            return match.group(0)
        key = (_vml_number(body, "Row"), _vml_number(body, "Column"))
        if key not in wanted:
            return match.group(0)
        old_text, new_text, size_pt = wanted[key]
        style_match = re.search(r"\bstyle\s*=\s*" + _VML_STYLE_RE.pattern, match.group("attrs"))
        if style_match is None:
            return match.group(0)
        style = style_match.group("style")
        sizes = {
            m.group("key").lower(): (float(m.group("num")), (m.group("unit") or "").lower())
            for m in _VML_SIZE_RE.finditer(style)
        }
        if "width" not in sizes or "height" not in sizes:
            return match.group(0)
        width_pt = sizes["width"][0] * _PT_PER_UNIT[sizes["width"][1]]
        height_num, height_unit = sizes["height"]
        height_pt = height_num * _PT_PER_UNIT[height_unit]
        usable = max(1.0, width_pt - 2 * _COMMENT_INSET_PT[0])


        def needed_pt(text: str) -> float:
            return wrapped_line_count(text, size_pt, usable) * size_pt * 1.3 + 2 * _COMMENT_INSET_PT[1]

        # 原文本来就超出框的（对话式批注的兼容副本常这样），只为新增的译文加高，
        # 不替原文把框撑开。
        overflow_before = max(0.0, needed_pt(old_text) - height_pt)
        needed = needed_pt(new_text) - overflow_before
        if needed <= height_pt:
            return match.group(0)
        new_height = math.ceil(needed / _PT_PER_UNIT[height_unit])
        new_style = _VML_SIZE_RE.sub(
            lambda m: f"height:{new_height}{m.group('unit') or ''}" if m.group("key").lower() == "height" else m.group(0),
            style,
        )
        attrs = match.group("attrs").replace(style, new_style, 1)
        extra_rows = math.ceil((needed - height_pt) / _DEFAULT_ROW_PT)

        def move_anchor(anchor: re.Match) -> str:
            values = [value.strip() for value in anchor.group(2).split(",")]
            if len(values) != 8 or not all(value.lstrip("-").isdigit() for value in values):
                return anchor.group(0)
            values[6] = str(int(values[6]) + extra_rows)
            return f"{anchor.group(1)}{', '.join(values)}{anchor.group(3)}"

        new_body = _VML_ANCHOR_RE.sub(move_anchor, body, count=1)
        prefix = match.group("p")
        return f"<{prefix}shape{attrs}>{new_body}</{prefix}shape>"

    updated = _VML_SHAPE_RE.sub(grow, text)
    if updated != text:
        package.write(vml_part, updated.encode("utf-8", errors="surrogateescape"))


def _note_review(
    review_marks: dict[str, str],
    source: str,
    sheet: str,
    ref: str,
    review_positions: list[dict[str, str]] | None,
) -> None:
    category = review_marks.get((source or "").strip())
    if category and review_positions is not None:
        review_positions.append(
            {
                "worksheet": sheet,
                "cell": ref,
                "category": category,
                "action": REVIEW_ACTION_LISTED_ONLY,
            }
        )


def apply_embedded_translations(
    package: _Package,
    options: EmbeddedTextOptions,
    *,
    translation_only: bool,
    review_marks: dict[str, str] | None = None,
    review_positions: list[dict[str, str]] | None = None,
) -> EmbeddedWriteStats:
    """把文本框和批注的译文写进包里（只登记改写，保存由调用方负责）。

    必须在「保留原文表」拍快照之后、单元格写回之前调用：快照拿的是原文，
    单元格那一轮冻结图片锚点时读到的绘图部件已经是写好译文的版本。
    """
    stats = EmbeddedWriteStats()
    if not options.enabled:
        return stats
    marks = dict(review_marks or {})
    for parts in _sheet_parts(package):
        if options.shapes and parts.drawing:
            _apply_shapes(
                package,
                parts,
                options,
                translation_only=translation_only,
                review_marks=marks,
                review_positions=review_positions,
                stats=stats,
            )
        if options.comments:
            _apply_comments(
                package,
                parts,
                options,
                translation_only=translation_only,
                review_marks=marks,
                review_positions=review_positions,
                stats=stats,
            )
    return stats
