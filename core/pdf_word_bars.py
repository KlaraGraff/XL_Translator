"""PDF 转 Word 后，带底色的标题条不比原文高。

pdf2docx 把框上方的空白也算进了第一行格子：格子的行高多出一截，第一段的段前距垫着
这截空白。格子涂了底色，这截空白就跟着涂成深色，标题条比原文高出一倍（反馈 #28 讲义
第 101 页「Example」：原文条高 18 磅，转出来 38 磅，多出的 20 磅原文是白的）。

做法：对着原 PDF 找到第一行格子里第一行字，从字的上沿往上看，底色一直延伸到哪儿，
那才是条的上沿。段前距多出来的那截（字上沿到条上沿之外的部分）从格子里拿出来：行高、
段前距各减掉这一截，表格前面补一段同样高的空段，上下的版面不动。

只动「一行行高固定、格子有底色、第一段有段前距」的表格第一行，而且只在原 PDF 上
看得出字上方的底色确实没铺到那么高时才动。找不到字、PDF 打不开、没有 PyMuPDF 时什么都不改。
"""
from __future__ import annotations

from pathlib import Path

from loguru import logger

from core.pdf_word_fit import page_groups
from core.pdf_word_shading import (
    _cell_fill,
    _differs,
    _keys,
    _own_paragraphs,
    _PageChars,
    _paragraph_text,
)

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

#: 段前距比原文条里字上方的底色多出这么多（1/20 磅）才改，免得一两磅的取整误差也动。
_MIN_EXCESS = 40
#: 取色时把页面放大到这么多倍（72 dpi 的倍数）。
_ZOOM = 4
#: 一排像素里有这么多是格子的底色，才算这一排还在条里（字的笔画、边框占少数）。
_FILL_SHARE = 0.6


def _spacing_before(paragraph) -> tuple[object | None, int]:
    spacing = paragraph.find(f"{_W}pPr/{_W}spacing")
    if spacing is None or spacing.get(f"{_W}beforeLines") is not None:
        return spacing, 0
    try:
        return spacing, int(spacing.get(f"{_W}before") or 0)
    except ValueError:
        return spacing, 0


def _fill_top(page, rect, fill, before_pt: float, fitz) -> float | None:
    """从这一行字的上沿往上，底色一直铺到的高度（PDF 坐标，磅）。看不出来时返回 None。"""
    top = max(rect.y0 - before_pt - 2, page.rect.y0)
    clip = fitz.Rect(rect.x0, top, rect.x1, rect.y0) & page.rect
    if clip.is_empty or clip.width < 1 or clip.height < 1:
        return None
    pix = page.get_pixmap(matrix=fitz.Matrix(_ZOOM, _ZOOM), clip=clip, alpha=False, colorspace=fitz.csRGB)
    samples, stride, n = pix.samples, pix.stride, pix.n
    reached = None
    for y in range(pix.height - 1, -1, -1):
        row = samples[y * stride: y * stride + pix.width * n]
        same = sum(1 for x in range(0, len(row), n) if not _differs((row[x], row[x + 1], row[x + 2]), fill))
        if same < _FILL_SHARE * pix.width:
            break
        reached = y
    if reached is None:
        return None
    return clip.y0 + reached / _ZOOM


def _spacer(parent, height: int):
    """表格前面垫高用的空段：行高固定成这一截，段落标记的字号压到最小。"""
    from docx.oxml import parse_xml
    from docx.oxml.ns import nsdecls

    return parse_xml(
        f'<w:p {nsdecls("w")}><w:pPr><w:spacing w:before="0" w:after="0" w:line="{height}" w:lineRule="exact"/>'
        '<w:rPr><w:sz w:val="2"/><w:szCs w:val="2"/></w:rPr></w:pPr></w:p>'
    )


def _trim_row(tr, excess: int) -> bool:
    """第一行的行高、每个有字格子第一段的段前距都减掉 excess。有一格减不了就整行不动。"""
    height = tr.find(f"{_W}trPr/{_W}trHeight")
    if height is None or height.get(f"{_W}hRule") != "exact":
        return False
    try:
        row_height = int(height.get(f"{_W}val") or 0)
    except ValueError:
        return False
    if row_height - excess <= 0:
        return False
    firsts = []
    for tc in tr.findall(f"{_W}tc"):
        paragraphs = [p for p in _own_paragraphs(tc) if _paragraph_text(p).strip()]
        if not paragraphs:
            continue
        spacing, before = _spacing_before(paragraphs[0])
        if spacing is None or before < excess:
            return False
        firsts.append((spacing, before))
    height.set(f"{_W}val", str(row_height - excess))
    for spacing, before in firsts:
        spacing.set(f"{_W}before", str(before - excess))
    return True


def trim_bar_padding(docx_path: Path, pdf_path: Path) -> int:
    """带底色的表格第一行里多垫的空白挪到表格前面，返回改了几张表。"""
    try:
        from core.pdf_component import require_fitz

        fitz = require_fitz()
    except Exception:  # noqa: BLE001 - no PyMuPDF: leave the layout as converted
        return 0
    from docx import Document

    doc = Document(str(docx_path))
    groups = page_groups(doc.element.body)
    try:
        pdf = fitz.open(pdf_path)
    except Exception as exc:  # noqa: BLE001 - unreadable PDF: nothing to compare with
        logger.info(f"[PDF] 打不开原 PDF 对照标题条高度：{exc!r}")
        return 0
    changed = 0
    with pdf:
        if len(groups) != len(pdf):
            return 0
        for page_index, group in enumerate(groups):
            cells = [tc for element in group for tc in element.iter(f"{_W}tc")]
            if not cells:
                continue
            page = pdf[page_index]
            chars = _PageChars(page)
            done: set = set()
            for tc in cells:
                paragraphs = _own_paragraphs(tc)
                keys = _keys(paragraphs)
                # 每个有字的格子都按顺序占一处，同样的字出现几次时才对得上是哪一处
                rect = chars.find(keys, fitz) if keys else None
                tr = tc.getparent()
                tbl = tr.getparent() if tr is not None else None
                if rect is None or tbl is None or id(tbl) in done or tbl.find(f"{_W}tr") is not tr:
                    continue
                _shd, fill = _cell_fill(tc)
                texts = [p for p in paragraphs if _paragraph_text(p).strip()]
                if fill is None or not texts:
                    continue
                done.add(id(tbl))
                _spacing, before = _spacing_before(texts[0])
                if before < _MIN_EXCESS:
                    continue
                top = _fill_top(page, rect, fill, before / 20, fitz)
                if top is None:
                    continue
                excess = before - round((rect.y0 - top) * 20)
                if excess < _MIN_EXCESS or not _trim_row(tr, excess):
                    continue
                tbl.addprevious(_spacer(tbl, excess))
                changed += 1
    if changed:
        doc.save(str(docx_path))
        logger.info(f"[PDF] {changed} 个带底色的标题条去掉了上方多垫的空白")
    return changed
