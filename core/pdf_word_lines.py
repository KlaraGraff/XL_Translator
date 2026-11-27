"""PDF 转 Word 后，一行一行摆的字按原文一行一段排回来（反馈 #28 讲义第 33 页证明表、代码框）。

讲义的证明表（Fitch 证明）左边是 17 步推理，缩进分三层（1. P₁ ∨ P₂ 顶格，3. P₁ 缩一层，
4. ¬P₁ 缩两层），右边是每一步的依据（Premise、Assumption、∧Intro 3,4……），一行对一行。
pdf2docx 把同一缩进的几行拼成一段，靠很大的固定行距和自动折行把它们摆到各自的高度：
「11. ¬P₁ 12. P₂ ∧ (¬P₂) 1. P₁ ∨ P₂ …」是一段，行距 51.6 磅。一段只能排在上一段下面，
不同缩进的段交错不了，于是排出来比格子高得多：只转换时左边的步骤被格子切掉，几乎全丢；
翻译后行高放开，步骤按段的顺序散到整页、顺序全乱，右边的依据（中文短，几个挤一行）也对不上。

代码框、方框里单独一行的公式也一样：pdf2docx 把一行一行摆的字拼成一段，行间放换行符、下一行
开头放制表符缩进、再用右缩进逼它折行；后面「合并折行」又把换行符换成空格，翻译后整段重排，
代码挤成一团。

做法：把这一页 PDF 的字和 Word 的字整页逐个对齐（core.pdf_word_rules 同样的办法），看出每一段
里的字原来各在 PDF 的哪一行。一格里的段和 PDF 的行上下顺序对不上（后一段的行在前一段的行
上面），或者有一处换行不是排满了自动折行（下一行的第一个词放得进上一行，或者下一行不在紧挨着
的下面、左边没对齐），这一格就是一行一行摆的，这张表就要重排：一行一行摆的格子除了排满了的
正文折行，每一行都切开；别的格子只在硬换行处切开；和别的格子里另起一段的行一样高的行也切开。
切开的每一块按原文从上到下排，缩进、段前距、行高按原文这几行的位置定，同一行的几格从同一个
上沿量起。字的格式（上下标、字体）不动。

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

#: 一个空格大约多宽（占行高的几成）。判断是不是硬换行时宁可算宽一点：算宽了只会少认硬换行。
_SPACE = 0.4

#: 两个字隔开超过行高的这么几成，算词与词之间（空格约占三成，字母之间几乎不留空）。
_WORD_GAP = 0.15

#: 两格里的两行上沿差不到行高的这么几成，算同一行（字体不同，字框的上沿差一两磅）。
_ROW_MATCH = 0.3


class _Line:
    """PDF 上的一行（上沿、下沿、左边、右边，磅），和这一行第一个词的宽度。"""

    def __init__(self, box: tuple) -> None:
        self.top, self.bottom, self.left, self.right = box[1], box[3], box[0], box[2]
        self.word = box[2] - box[0]
        self._in_word = True

    def holds(self, box: tuple) -> bool:
        middle = (box[1] + box[3]) / 2
        return self.top <= middle <= self.bottom

    def add(self, box: tuple) -> None:
        # 和前一个字隔开了（字母之间几乎不留空，词与词之间隔一个空格），第一个词就到头了
        if self._in_word and box[0] - self.right > _WORD_GAP * (self.bottom - self.top):
            self._in_word = False
        self.left, self.right = min(self.left, box[0]), max(self.right, box[2])
        if self._in_word:
            self.word = self.right - self.left


def _lines_of(paragraph, where, offset: int, reverse: dict[int, int], chars) -> list[tuple[int, _Line]]:
    """这一段里每换一行 PDF 的位置：[(第几个字（不算空白）起, 那一行)]。上下标落在本行的上下沿之内。"""
    found: list[tuple[int, _Line]] = []
    for number in range(len(where)):
        pdf_index = reverse.get(offset + number)
        if pdf_index is None:
            continue
        box = chars[pdf_index][1]
        if found and found[-1][1].holds(box):
            found[-1][1].add(box)
            continue
        found.append((number, _Line(box)))
    return found


def _widest(located) -> float | None:
    return max((line.right for *_rest, lines in located for _start, line in lines), default=None)


def _below(above: _Line, below: _Line, first: bool) -> bool:
    """下一行能是上一行折下来的：紧挨在下面；上一行不是这段的第一行时，两行左边对齐
    （第一行可以有首行缩进、悬挂缩进，折下来的各行都从同一个左边起）。"""
    return (below.top > above.top + _SAME_ROW and below.top - above.bottom < above.bottom - above.top
            and (first or abs(below.left - above.left) <= _SAME_ROW))


def _wrapped(above: _Line, below: _Line, widest: float, first: bool) -> bool:
    """上一行到下一行是排满了自动折行：下一行是折下来的样子，上一行后面也放不下下一行的第一个词
    （这一格的版心至少有最宽的那一行那么宽）。不是自动折行的，就是一行一行摆的（硬换行）。"""
    height = above.bottom - above.top
    return _below(above, below, first) and above.right + _SPACE * height + below.word > widest - _SAME_ROW


def _hard_break(located) -> bool:
    """这一格有一段在 PDF 上是一行一行摆的：有一处换行不是排满了自动折行（pdf2docx 拼成一段、
    行间放换行符、靠右缩进逼它折行）。"""
    widest = _widest(located)
    return widest is not None and any(
        not _wrapped(lines[number - 1][1], lines[number][1], widest, number == 1)
        for *_rest, lines in located for number in range(1, len(lines)))


def _full(above: _Line, below: _Line, widest: float, first: bool) -> bool:
    """上一行排满了（右边到了这一格最宽的那一行），下一行是折下来的样子：两端对齐的正文折行。"""
    return _below(above, below, first) and above.right >= widest - _SAME_ROW


def _cuts(cells: list) -> list[list[set[int]]]:
    """每格每段在第几行前面切开。
    一行一行摆的格子（有硬换行，或者段和行的上下顺序交错）：除了排满了的折行，每一行都切开。
    光看一处换行常常分不出来（证明里最长的那一步后面紧跟着一步，看着像折行），既然这一格是一行
    一行摆的，就都当一行一段。别的格子只在硬换行处切。最后，和别的格子里另起一段的行一样高的行
    也切开（证明右边那一列依据「Premise」「Premise」，每一行都对着左边的一步）。"""
    first: list[list[set[int]]] = []
    starts: list[list[float]] = []
    for located in cells:
        widest = _widest(located)
        by_lines = _interleaved(located) or _hard_break(located)
        cell_cuts, tops = [], []
        for *_rest, lines in located:
            cut = set()
            for number in range(1, len(lines)):
                above, below = lines[number - 1][1], lines[number][1]
                if widest is None or not (_full(above, below, widest, number == 1) if by_lines
                                          else _wrapped(above, below, widest, number == 1)):
                    cut.add(number)
            cell_cuts.append(cut)
            tops += [line.top for number, (_start, line) in enumerate(lines) if number == 0 or number in cut]
        first.append(cell_cuts)
        starts.append(tops)
    # 一格切开的行又可能对着别的格子里的行：反复对照，直到不再多切
    changed = True
    while changed:
        changed = False
        for index, located in enumerate(cells):
            others = [top for other, tops in enumerate(starts) if other != index for top in tops]
            for (*_rest, lines), cut in zip(located, first[index], strict=True):
                for number in range(1, len(lines)):
                    line = lines[number][1]
                    if number not in cut and any(
                            abs(line.top - top) <= _ROW_MATCH * (line.bottom - line.top) for top in others):
                        cut.add(number)
                        starts[index].append(line.top)
                        changed = True
    return first


def _cut_paragraph(paragraph, where, starts: list[int]) -> list[list] | None:
    """在这几个字（不算空白）前面把段切开，返回每一块的子节点。切点落在不是普通字的 run 里时返回 None。"""
    # 记着元素本身，不记 id()：lxml 的元素对象没人引用就会回收，id 会被别的元素重用
    heads: list = []
    for start in sorted(starts, reverse=True):
        node, index = where[start]
        run = node.getparent()
        if not _plain_run(run, paragraph):
            return None
        heads.append(_split(run, index) if index else run)
    blocks: list[list] = [[]]
    for child in paragraph:
        if child.tag == f"{_W}pPr":
            continue
        if any(child is head for head in heads) and blocks[-1]:
            blocks.append([])
        blocks[-1].append(child)
    return blocks


def _blank_run(node) -> bool:
    """只装着换行符（w:br，不是分页、分栏）、制表符或空白的 run：原来靠它们换到下一行、缩进到位。"""
    children = [child for child in node if child.tag != f"{_W}rPr"]
    if node.tag != f"{_W}r" or not children:
        return False
    for child in children:
        if child.tag == f"{_W}br":
            if child.get(f"{_W}type") not in (None, "textWrapping") or child.get(f"{_W}clear") not in (None, "none"):
                return False
        elif child.tag == f"{_W}t":
            if (child.text or "").strip():
                return False
        elif child.tag not in (f"{_W}tab", f"{_W}cr"):
            return False
    return True


def _twips(points: float) -> str:
    return str(max(0, round(points * 20)))


def _set_layout(paragraph, left: float, first: float, before: float, height: float) -> None:
    """缩进、首行缩进、段前距、行高按原文这几行定；右缩进去掉，靠左。"""
    p_pr = paragraph.find(f"{_W}pPr")
    if p_pr is None:
        p_pr = paragraph.makeelement(f"{_W}pPr", {})
        paragraph.insert(0, p_pr)
    for name in ("spacing", "ind", "jc"):
        for node in p_pr.findall(f"{_W}{name}"):
            p_pr.remove(node)
    spacing = p_pr.makeelement(f"{_W}spacing", {f"{_W}before": _twips(before), f"{_W}after": "0",
                                                f"{_W}line": _twips(height), f"{_W}lineRule": "exact"})
    ind = p_pr.makeelement(f"{_W}ind", {f"{_W}left": _twips(left), f"{_W}right": "0", f"{_W}firstLine": _twips(first)})
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


def _origin_top(located) -> float | None:
    """格子的上沿（PDF 坐标）：第一段第一行的上沿减去它的段前距。"""
    for paragraph, _where, _offset, lines in located:
        if not lines:
            return None
        spacing = paragraph.find(f"{_W}pPr/{_W}spacing")
        return lines[0][1].top - _number(spacing.get(f"{_W}before") if spacing is not None else 0)
    return None


def _reorder_cell(tc, located: list[tuple[Any, Any, int, list[tuple[int, _Line]]]], cuts: list[set[int]],
                  origin_top: float) -> bool:
    """一格里的段在该切的行前面切开，按 PDF 上的位置从上到下重排。自动折行的几行留在一段里。
    origin_top 是这一行格子的上沿：同一行的几个格子用同一个，左右两列才对得齐。"""
    pieces: list[tuple[list[_Line], Any, list]] = []
    origin_left = None
    for (paragraph, where, _offset, lines), cut in zip(located, cuts, strict=True):
        if not lines:
            return False
        p_pr = paragraph.find(f"{_W}pPr")
        ind = p_pr.find(f"{_W}ind") if p_pr is not None else None
        jc = p_pr.find(f"{_W}jc") if p_pr is not None else None
        if origin_left is None and (jc is None or jc.get(f"{_W}val") in ("left", "both", "start")):
            origin_left = lines[0][1].left - _number(ind.get(f"{_W}left") if ind is not None else 0)
        heads = [0, *sorted(cut)]
        blocks = _cut_paragraph(paragraph, where, [lines[number][0] for number in heads[1:]])
        if blocks is None or len(blocks) != len(heads):
            return False
        for begin, end, nodes in zip(heads, [*heads[1:], len(lines)], blocks, strict=True):
            pieces.append(([line for _start, line in lines[begin:end]], paragraph, nodes))
    if origin_left is None:
        return False
    pieces.sort(key=lambda piece: (piece[0][0].top, piece[0][0].left))
    if any(b[0][0].top - a[0][0].top < _SAME_ROW for a, b in pairwise(pieces)):
        return False
    paragraphs = [paragraph for paragraph, *_rest in located]
    anchor = paragraphs[0]
    bottom = origin_top
    for lines, origin, nodes in pieces:
        paragraph = origin.makeelement(f"{_W}p", {})
        p_pr = origin.find(f"{_W}pPr")
        if p_pr is not None:
            copy_pr = copy.deepcopy(p_pr)
            for node in copy_pr.findall(f"{_W}sectPr"):
                copy_pr.remove(node)
            paragraph.append(copy_pr)
        # 原来行与行之间的换行符（w:br）、下一行开头缩进用的制表符留在了切开的地方：
        # 一行一段、缩进按原文定以后不要了
        while nodes and _blank_run(nodes[-1]):
            nodes.pop()
        while nodes and _blank_run(nodes[0]):
            nodes.pop(0)
        for node in nodes:
            paragraph.append(node)
        top, height = lines[0].top, lines[0].bottom - lines[0].top
        # 自动折行的几行：行高取原文的行距；固定行高时多出来的空在字的上面，段前距相应减掉
        pitch = (lines[-1].top - top) / (len(lines) - 1) if len(lines) > 1 else height
        left = min(line.left for line in lines)
        _set_layout(paragraph, left - origin_left, lines[0].left - left,
                    top - bottom - max(0.0, pitch - height), pitch)
        anchor.addprevious(paragraph)
        bottom = lines[-1].bottom
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
    # 还剩下的短截（「¬¬ 7」这种不到 _MIN_MATCH 个字的一行）：PDF 剩下的字里只出现一次，就是它
    mapped = set(mapping.values())
    number = 0
    while number < len(word_text):
        if number in mapped:
            number += 1
            continue
        end = number
        while end < len(word_text) and end not in mapped:
            end += 1
        piece = word_text[number:end]
        if len(piece) >= 2 and free.count(piece) == 1:
            at = free.find(piece)
            for offset in range(len(piece)):
                mapping[at + offset] = number + offset
            free = free[:at] + "\0" * len(piece) + free[at + len(piece):]
        number = end
    return mapping


def _restore_page(group: list, page) -> int:
    chars = _page_chars(page)
    page_text = "".join(char[0] for char in chars)
    paragraphs = [p for element in group
                  for p in ([element] if element.tag == f"{_W}p" else element.iter(f"{_W}p"))]
    texts = [_positions(p) for p in paragraphs]
    offsets, offset = {}, 0
    for paragraph, (text, where) in zip(paragraphs, texts, strict=True):
        offsets[paragraph] = (offset, where)
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
                start, where = offsets.get(paragraph, (0, []))
                if where:
                    located.append((paragraph, where, start, _lines_of(paragraph, where, start, reverse, chars)))
            cells.append((tc, located))
        if not any(_interleaved(located) or _hard_break(located) for _tc, located in cells):
            continue
        cuts = _cuts([located for _tc, located in cells])
        # 同一行的格子取最靠上的那个上沿（pdf2docx 给各格第一段估的段前距不一样，左右两列会错开几磅）
        rows: dict[Any, float] = {}
        for tc, located in cells:
            top = _origin_top(located)
            if top is not None:
                row = tc.getparent()
                rows[row] = min(rows.get(row, top), top)
        for (tc, located), cell_cuts in zip(cells, cuts, strict=True):
            if located and (any(cell_cuts) or _interleaved(located)) and tc.getparent() in rows:
                changed += _reorder_cell(tc, located, cell_cuts, rows[tc.getparent()])
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
