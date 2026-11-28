"""PDF 转 Word 后，原文两端对齐排满的行不再标成居中（反馈 #28 讲义第 59、60、66 页）。

pdf2docx 看一行左右空白一样就标成居中：两端对齐的正文，排满的行左右都顶到边，也被标成居中。
同一段折成几行的，合并折行会接成一段；一句话正好在行尾结束、下一行另起一句的，接不上，这一行
就单独一段、居中。中文比英文短，译文这一行缩在中间，左边参差。

做法：对照原 PDF，居中的段落找到原文那一行：那一块字有好几行、这一行左右都顶到这块字的边（差不多和整块一样宽）、块里还有
别的行也从左边起头（两端对齐的段落；居中的多行标题每行左边不齐），就是排满的正文行，改成两端对齐。
左右的空白按原文：pdf2docx 靠居中把这一行摆在格子正中，改成两端对齐后左右各缩进（格子宽减行宽）的一半。
单独一行的公式、图注自成一块，不动。
"""
from __future__ import annotations

import re
from pathlib import Path

from loguru import logger

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_SPACE = re.compile(r"\s+")

#: 行的左右边和这块字的边相差这么多磅以内算顶到边。
_EDGE = 1.5
#: 算缩进时行宽多留这么多（Word 里没有原文字体时换用的字稍宽，一点不留会多折出一行）。
_SLACK = 1.02
#: 排满的行至少有这块字这么宽（首行缩进的那一行左边空一点；同一块里并排的几个图上标签不算）。
_FULL = 0.8


def _normalize(text: str) -> str:
    return _SPACE.sub("", text)


#: 同一块里上沿相差这么多磅以内的几段算同一行（PyMuPDF 把中间空得宽的一行拆成两段，讲义第 66 页）。
_SAME_LINE = 1.0


def visual_lines(page) -> list[list[tuple[str, tuple[float, float, float, float]]]]:
    """原 PDF 这一页每一块字的每一行：(去掉空白的文字, (x0, y0, x1, y1))。PyMuPDF 把两端对齐、中间空得
    很宽的一行拆成左右两段时，按上沿接回一行（从左到右）。"""
    blocks = []
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue
        rows: list[list] = []
        for line in block.get("lines") or []:
            text = _normalize("".join(span.get("text", "") for span in line.get("spans", [])))
            if not text:
                continue
            box = tuple(line["bbox"])
            if rows and abs(rows[-1][0][1][1] - box[1]) <= _SAME_LINE:
                rows[-1].append((text, box))
            else:
                rows.append([(text, box)])
        joined = []
        for row in rows:
            row.sort(key=lambda item: item[1][0])
            boxes = [box for _text, box in row]
            joined.append(("".join(text for text, _box in row), (
                min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))))
        if joined:
            blocks.append(joined)
    return blocks


def full_lines(page) -> dict[str, float]:
    """原 PDF 这一页两端对齐排满的行：去掉空白的文字 → 行宽（磅）。"""
    found: dict[str, float] = {}
    for lines in visual_lines(page):
        if len(lines) < 2:
            continue
        x0 = min(box[0] for _text, box in lines)
        x1 = max(box[2] for _text, box in lines)
        left = [number for number, (_text, box) in enumerate(lines) if abs(box[0] - x0) <= _EDGE]
        for number, (text, box) in enumerate(lines):
            if (abs(box[2] - x1) > _EDGE or box[2] - box[0] < _FULL * (x1 - x0)
                    or not any(other != number for other in left)):
                continue
            found[text] = box[2] - box[0]
    return found


def _twips(node, name: str) -> float:
    try:
        return float(node.get(f"{_W}{name}") or 0) if node is not None else 0.0
    except ValueError:
        return 0.0


def _room(paragraph, section) -> float | None:
    """这一段能排字的宽度（1/20 磅）：在格子里是格子宽减去左右边距，在正文里是版心宽。"""
    parent = paragraph.getparent()
    if parent is not None and parent.tag == f"{_W}tc":
        width = _twips(parent.find(f"{_W}tcPr/{_W}tcW"), "w")
        margins = parent.find(f"{_W}tcPr/{_W}tcMar")
        if margins is not None:
            for side in ("start", "left", "end", "right"):
                width -= _twips(margins.find(f"{_W}{side}"), "w")
        return width or None
    if section is None:
        return None
    page = _twips(section.find(f"{_W}pgSz"), "w")
    margins = section.find(f"{_W}pgMar")
    width = page - _twips(margins, "left") - _twips(margins, "right")
    columns = section.find(f"{_W}cols")
    count = int(_twips(columns, "num")) if columns is not None else 1
    if count > 1:  # 分栏：一栏的宽度
        width = (width - _twips(columns, "space") * (count - 1)) / count
    return width if width > 0 else None


def _sections(group: list, final) -> dict:
    """每个顶层元素所在的分节（它后面最近的分节符，最后一节是正文末尾的 sectPr）。"""
    found, pending = {}, []
    for element in group:
        pending.append(element)
        mark = element.find(f"{_W}pPr/{_W}sectPr") if element.tag == f"{_W}p" else None
        if mark is not None:
            found.update((id(item), mark) for item in pending)
            pending = []
    found.update((id(item), final) for item in pending)
    return found


def _set_margins(paragraph, margin: float) -> None:
    """左右各缩进 margin（1/20 磅）：pdf2docx 靠居中把排满的行摆在格子正中，改成两端对齐后用缩进摆回去。"""
    p_pr = paragraph.find(f"{_W}pPr")
    ind = p_pr.find(f"{_W}ind")
    if ind is None:
        ind = p_pr.makeelement(f"{_W}ind", {})
        p_pr.append(ind)
    value = str(round(margin))
    for name in ("left", "right", "start", "end"):
        if name in ("left", "right") or ind.get(f"{_W}{name}") is not None:
            ind.set(f"{_W}{name}", value)


def _centered(paragraph) -> bool:
    jc = paragraph.find(f"{_W}pPr/{_W}jc")
    return jc is not None and jc.get(f"{_W}val") == "center"


def justify_full_lines(docx_path: Path, pdf_path: Path) -> int:
    """居中的段落是原文排满的正文行的，改成两端对齐；返回改了几段。"""
    try:
        from core.pdf_component import require_fitz

        fitz = require_fitz()
    except Exception:  # noqa: BLE001 - no PyMuPDF: leave the alignment as converted
        return 0
    from docx import Document

    from core.pdf_word_fit import page_groups

    doc = Document(str(docx_path))
    groups = page_groups(doc.element.body)
    changed = 0
    with fitz.open(pdf_path) as pdf:
        if len(groups) != len(pdf):
            return 0
        for group, page in zip(groups, pdf):
            centered = [p for element in group
                        for p in ([element] if element.tag == f"{_W}p" else element.iter(f"{_W}p"))
                        if _centered(p)]
            if not centered:
                continue
            lines = full_lines(page)
            sections = _sections(group, doc.element.body.find(f"{_W}sectPr"))
            for paragraph in centered:
                text = _normalize("".join(node.text or "" for node in paragraph.iter(f"{_W}t")))
                if not text or text not in lines:
                    continue
                paragraph.find(f"{_W}pPr/{_W}jc").set(f"{_W}val", "both")
                room = _room(paragraph, sections.get(id(paragraph)))
                if room is not None:
                    _set_margins(paragraph, max(0.0, (room - lines[text] * 20 * _SLACK) / 2))
                changed += 1
    if changed:
        doc.save(str(docx_path))
        logger.info(f"[PDF] {changed} 段原文排满的正文行不再居中，改成两端对齐")
    return changed
