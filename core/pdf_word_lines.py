"""PDF 转 Word 后，交错的几列字按原文一行一行排回来（反馈 #28 讲义第 33 页证明表）。

讲义的证明表（Fitch 证明）左边是 17 步推理，缩进分三层（1. P₁ ∨ P₂ 顶格，3. P₁ 缩一层，
4. ¬P₁ 缩两层），右边是每一步的依据（Premise、Assumption、∧Intro 3,4……），一行对一行。
pdf2docx 把同一缩进的几行拼成一段，靠很大的固定行距和自动折行把它们摆到各自的高度：
「11. ¬P₁ 12. P₂ ∧ (¬P₂) 1. P₁ ∨ P₂ …」是一段，行距 51.6 磅。一段只能排在上一段下面，
不同缩进的段交错不了，于是排出来比格子高得多：只转换时左边的步骤被格子切掉，几乎全丢；
翻译后行高放开，步骤按段的顺序散到整页、顺序全乱，右边的依据（中文短，几个挤一行）也对不上。

做法：把这一页 PDF 的字和 Word 的字整页逐个对齐（core.pdf_word_rules 同样的办法），看出每一段
里的字原来各在 PDF 的哪一行。一张表里只要有一格的段和 PDF 的行上下顺序对不上（后一段的行在
前一段的行上面），这张表就是靠折行摆位置的：把它每一格的段都按 PDF 的行拆开，一行一段，按原文
从上到下排，每段的缩进、段前距、行高按原文这一行的位置和高度定。字的格式（上下标、字体）不动。

拆不开（一行的分界落在超链接、公式这类不是普通字的地方）、同一格里有两行一样高（并排的字）、
找不到对应的字、PDF 打不开、没有 PyMuPDF 时，这张表不动。
"""
from __future__ import annotations

import copy
from itertools import pairwise
from pathlib import Path
from typing import Any

from loguru import logger

from core.pdf_word_fit import page_groups
from core.pdf_word_rules import (
    _MIN_MATCH,
    _alignment,
    _page_chars,
    _plain_run,
    _positions,
    _split,
)

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

#: 两行的上沿差不到这么多磅，算同一高度（并排的字），这一格不重排。
_SAME_ROW = 2.0


class _Line:
    """PDF 上的一行（上沿、下沿、左边，磅）。"""

    def __init__(self, top: float, bottom: float, left: float) -> None:
        self.top, self.bottom, self.left = top, bottom, left

    def holds(self, box: tuple) -> bool:
        middle = (box[1] + box[3]) / 2
        return self.top <= middle <= self.bottom


def _lines_of(paragraph, where, offset: int, reverse: dict[int, int], chars) -> list[tuple[int, _Line]]:
    """这一段里每换一行 PDF 的位置：[(第几个字（不算空白）起, 那一行)]。上下标落在本行的上下沿之内。"""
    found: list[tuple[int, _Line]] = []
    for number in range(len(where)):
        pdf_index = reverse.get(offset + number)
        if pdf_index is None:
            continue
        box = chars[pdf_index][1]
        if found and found[-1][1].holds(box):
            line = found[-1][1]
            line.left = min(line.left, box[0])
            continue
        found.append((number, _Line(box[1], box[3], box[0])))
    return found


def _cut_paragraph(paragraph, where, starts: list[int]) -> list[list] | None:
    """在这几个字（不算空白）前面把段切开，返回每一块的子节点。切点落在不是普通字的 run 里时返回 None。"""
    heads = set()
    for start in sorted(starts, reverse=True):
        node, index = where[start]
        run = node.getparent()
        if not _plain_run(run, paragraph):
            return None
        heads.add(id(_split(run, index)) if index else id(run))
    blocks: list[list] = [[]]
    for child in paragraph:
        if child.tag == f"{_W}pPr":
            continue
        if id(child) in heads and blocks[-1]:
            blocks.append([])
        blocks[-1].append(child)
    return blocks


def _twips(points: float) -> str:
    return str(max(0, round(points * 20)))


def _set_layout(paragraph, left: float, before: float, height: float) -> None:
    """缩进、段前距、行高按原文这一行定；右缩进、首行缩进去掉，靠左。"""
    p_pr = paragraph.find(f"{_W}pPr")
    if p_pr is None:
        p_pr = paragraph.makeelement(f"{_W}pPr", {})
        paragraph.insert(0, p_pr)
    for name in ("spacing", "ind", "jc"):
        for node in p_pr.findall(f"{_W}{name}"):
            p_pr.remove(node)
    spacing = p_pr.makeelement(f"{_W}spacing", {f"{_W}before": _twips(before), f"{_W}after": "0",
                                                f"{_W}line": _twips(height), f"{_W}lineRule": "exact"})
    ind = p_pr.makeelement(f"{_W}ind", {f"{_W}left": _twips(left), f"{_W}right": "0", f"{_W}firstLine": "0"})
    jc = p_pr.makeelement(f"{_W}jc", {f"{_W}val": "left"})
    # pPr 里的先后：… spacing、ind … jc … rPr、sectPr
    anchor = next((node for node in p_pr if node.tag in (f"{_W}rPr", f"{_W}sectPr", f"{_W}pPrChange")), None)
    for node in (spacing, ind, jc):
        if anchor is not None:
            anchor.addprevious(node)
        else:
            p_pr.append(node)


def _number(value) -> float:
    try:
        return float(value or 0) / 20
    except ValueError:
        return 0.0


def _reorder_cell(tc, located: list[tuple[Any, Any, int, list[tuple[int, _Line]]]]) -> bool:
    """一格里的段按 PDF 的行拆开、从上到下重排。"""
    pieces: list[tuple[_Line, Any, list]] = []
    origin_top = origin_left = None
    for paragraph, where, _offset, lines in located:
        if not lines:
            return False
        p_pr = paragraph.find(f"{_W}pPr")
        spacing = p_pr.find(f"{_W}spacing") if p_pr is not None else None
        ind = p_pr.find(f"{_W}ind") if p_pr is not None else None
        jc = p_pr.find(f"{_W}jc") if p_pr is not None else None
        if origin_top is None:
            # 格子的上沿：第一段第一行的上沿减去它的段前距
            origin_top = lines[0][1].top - _number(spacing.get(f"{_W}before") if spacing is not None else 0)
        if origin_left is None and (jc is None or jc.get(f"{_W}val") in ("left", "both", "start")):
            origin_left = lines[0][1].left - _number(ind.get(f"{_W}left") if ind is not None else 0)
        blocks = _cut_paragraph(paragraph, where, [start for start, _line in lines[1:]])
        if blocks is None or len(blocks) != len(lines):
            return False
        for (_start, line), nodes in zip(lines, blocks, strict=True):
            pieces.append((line, paragraph, nodes))
    if origin_top is None or origin_left is None:
        return False
    pieces.sort(key=lambda piece: (piece[0].top, piece[0].left))
    if any(b[0].top - a[0].top < _SAME_ROW for a, b in pairwise(pieces)):
        return False
    paragraphs = [paragraph for paragraph, *_rest in located]
    anchor = paragraphs[0]
    bottom = origin_top
    for line, origin, nodes in pieces:
        paragraph = origin.makeelement(f"{_W}p", {})
        p_pr = origin.find(f"{_W}pPr")
        if p_pr is not None:
            copy_pr = copy.deepcopy(p_pr)
            for node in copy_pr.findall(f"{_W}sectPr"):
                copy_pr.remove(node)
            paragraph.append(copy_pr)
        for node in nodes:
            paragraph.append(node)
        _set_layout(paragraph, line.left - origin_left, line.top - bottom, line.bottom - line.top)
        anchor.addprevious(paragraph)
        bottom = line.bottom
    for paragraph in paragraphs:
        # 原来的段只剩段落属性（分节信息留在最后一段上）
        if paragraph.find(f"{_W}pPr/{_W}sectPr") is None:
            tc.remove(paragraph)
        else:
            for child in list(paragraph):
                if child.tag != f"{_W}pPr":
                    paragraph.remove(child)
    return True


def _interleaved(located) -> bool:
    """这一格的段和 PDF 的行上下顺序对不上：后面的段里有一行在前面某一行的上面。"""
    highest = None
    for _paragraph, _where, _offset, lines in located:
        for _start, line in lines:
            if highest is not None and line.top < highest - _SAME_ROW:
                return True
            highest = line.top if highest is None else max(highest, line.top)
    return False


def _full_alignment(page_text: str, word_text: str) -> dict[int, int]:
    """先按先后顺序对齐；顺序对不上的（交错拼成一段的行）再一块一块到 PDF 剩下的字里找。"""
    mapping = _alignment(page_text, word_text)
    taken = set(mapping)
    mapped = set(mapping.values())
    free = "".join("\0" if index in taken else char for index, char in enumerate(page_text))
    number = 0
    while number < len(word_text):
        if number in mapped:
            number += 1
            continue
        end = number
        while end < len(word_text) and end not in mapped:
            end += 1
        # number..end 是 Word 里一整段没对上的字：从头起找 PDF 剩下的字里最长的同样一截
        while end - number >= _MIN_MATCH:
            length, at = 0, -1
            low, high = _MIN_MATCH, end - number
            while low <= high:
                middle = (low + high) // 2
                found = free.find(word_text[number:number + middle])
                if found != -1:
                    length, at = middle, found
                    low = middle + 1
                else:
                    high = middle - 1
            if at == -1:
                number += 1
                continue
            for offset in range(length):
                mapping[at + offset] = number + offset
            free = free[:at] + "\0" * length + free[at + length:]
            number += length
        number = max(number, end)
    return mapping


def _restore_page(group: list, page) -> int:
    chars = _page_chars(page)
    page_text = "".join(char[0] for char in chars)
    paragraphs = [p for element in group
                  for p in ([element] if element.tag == f"{_W}p" else element.iter(f"{_W}p"))]
    texts = [_positions(p) for p in paragraphs]
    offsets, offset = {}, 0
    for paragraph, (text, where) in zip(paragraphs, texts, strict=True):
        offsets[id(paragraph)] = (offset, where)
        offset += len(text)
    mapping = _full_alignment(page_text, "".join(text for text, _where in texts))
    reverse = {value: key for key, value in mapping.items()}
    changed = 0
    tables = [table for element in group
              for table in ([element] if element.tag == f"{_W}tbl" else element.iter(f"{_W}tbl"))]
    for table in tables:
        cells = []
        for tc in table.iter(f"{_W}tc"):
            located = []
            for paragraph in tc.findall(f"{_W}p"):
                start, where = offsets.get(id(paragraph), (0, []))
                if where:
                    located.append((paragraph, where, start, _lines_of(paragraph, where, start, reverse, chars)))
            cells.append((tc, located))
        if not any(_interleaved(located) for _tc, located in cells):
            continue
        for tc, located in cells:
            if located and any(len(lines) > 1 for *_rest, lines in located) or _interleaved(located):
                changed += _reorder_cell(tc, located)
    return changed


def restore_line_order(docx_path: Path, pdf_path: Path) -> int:
    """交错摆放的表格（证明表）按原文一行一段重排，返回重排了几格。"""
    try:
        from core.pdf_component import require_fitz

        fitz = require_fitz()
    except Exception:  # noqa: BLE001 - no PyMuPDF: leave the tables as converted
        return 0
    from docx import Document

    doc = Document(str(docx_path))
    groups = page_groups(doc.element.body)
    try:
        pdf = fitz.open(pdf_path)
    except Exception as exc:  # noqa: BLE001 - unreadable PDF: nothing to compare with
        logger.info(f"[PDF] 打不开原 PDF 对照表格里的行：{exc!r}")
        return 0
    changed = 0
    pages = []
    with pdf:
        if len(groups) != len(pdf):
            return 0
        for page_index, group in enumerate(groups):
            count = _restore_page(group, pdf[page_index])
            if count:
                pages.append(str(page_index + 1))
                changed += count
    if changed:
        doc.save(str(docx_path))
        logger.info(f"[PDF] 第 {'、'.join(pages)} 页共 {changed} 个格子里交错摆放的行按原文一行一段重排")
    return changed
