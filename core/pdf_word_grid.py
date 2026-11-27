"""PDF 转 Word 后，格子里挤着几行几列字的表格按原文的行列拆成真正的格子（反馈 #28 讲义第 26 页真值表）。

讲义的真值表是 5 列 5 行，原文只在第 2、3 列之间和第 4、5 列之间画了竖线，表头下面画了一条横线。
pdf2docx 按画出来的线认表格：只认出 3 列 2 行。于是一格里装着两列四行的 T、F：每行一段（或一段里
用换行符隔开），同一行的两列之间有的用制表符、有的只是一个空格，靠缩进和制表位摆到原来的位置。
后面「合并折行」又把这些短行当成折开的半句并成一段，翻译后 T、F 挤在一起、列对不上。

做法：在 PDF 上找到这张表的字（表格前后两段和 PDF 对上了，中间那截的字和表格里的字一个不差），
按字的位置分出原文的行（上下）和列（左右空开一截），再和 Word 表格的格子对照：每一格的字正好是
原文的几行几列（从上到下、从左到右连起来一字不差）。对得上，就把每一格拆成这几行几列：
- 原来格子之间的边框、底色留着，新拆出来的格子之间不画线；
- 列宽按原文各列之间空白的中线分，行高按原文的行距分，加起来和原来一样；
- 每格里的字连同格式（粗体、斜体、上下标）原样搬过去；拆开的列，原文是居中摆的就居中；没拆开的
  列，缩进、对齐照原来那一段。
表格是这一页最前或最后的东西时（后面只有页码），范围敞着的那一头一行一行去掉，直到字对上。

不拆：表格里套着表格、有合并的格子、有图；PDF 上找不到这张表或字对不上；原文一行只有一列的字
（一格里折成几行的正文，不是表格的行）；pdf2docx 自己当成一段里自然折行、别的格子在这里也没有
另起一行的地方；每格只有一行字、靠段前距错开高低的（图上的标注）。
"""
from __future__ import annotations

import copy
from collections import Counter
from itertools import pairwise
from pathlib import Path

from loguru import logger

from core.pdf_word_fit import page_groups
from core.pdf_word_rules import _alignment, _page_chars, _positions

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"

#: 左右两截字隔开超过行高的这么几成，算两列（词与词之间的空格约占三成）。
_COLUMN_GAP = 0.5

#: 字高不到这一行正文的这么几成，算上标、下标。
_SCRIPT = 0.85

#: 一列各格的字中线相差不到这么多磅，算居中摆的。
_CENTERED = 1.5

#: 行列分法最多试这么多种，再多就不拆（大表格对不上时不白算）。
_MAX_TRIES = 20000


class _Row:
    """原文的一行：上下沿按这一行最高的字定，上下标算在里面。"""

    def __init__(self, char) -> None:
        box = char[1]
        self.top, self.bottom = box[1], box[3]
        self.chars = [char]

    def takes(self, char) -> bool:
        box = char[1]
        middle = (box[1] + box[3]) / 2
        if self.top <= middle <= self.bottom:
            return True
        height = box[3] - box[1]
        # 小一号的上下标：和这一行上下有重叠就算这一行
        return height < _SCRIPT * (self.bottom - self.top) and box[1] < self.bottom and box[3] > self.top


def _rows(chars) -> list[_Row]:
    rows: list[_Row] = []
    for char in sorted(chars, key=lambda item: item[1][1] - item[1][3]):  # 先放大的字
        row = next((row for row in rows if row.takes(char)), None)
        if row is None:
            rows.append(_Row(char))
        else:
            row.chars.append(char)
    rows.sort(key=lambda row: row.top)
    for row in rows:
        row.chars.sort(key=lambda item: (item[1][0], item[1][1]))
    return rows


def _columns(rows: list[_Row]) -> list[tuple[float, float]]:
    """原文的列：所有行的字左右连起来，隔开超过 _COLUMN_GAP 个行高的地方分列。"""
    height = sorted(row.bottom - row.top for row in rows)[len(rows) // 2]
    spans = sorted((char[1][0], char[1][2]) for row in rows for char in row.chars)
    columns: list[list[float]] = []
    for left, right in spans:
        if columns and left - columns[-1][1] < _COLUMN_GAP * height:
            columns[-1][1] = max(columns[-1][1], right)
        else:
            columns.append([left, right])
    return [(left, right) for left, right in columns]


def _column_of(columns: list[tuple[float, float]], char) -> int:
    middle = (char[1][0] + char[1][2]) / 2
    return min(range(len(columns)), key=lambda index: 0 if columns[index][0] <= middle <= columns[index][1]
               else min(abs(middle - columns[index][0]), abs(middle - columns[index][1])))


def _cells(table) -> list[list]:
    return [tr.findall(f"{_W}tc") for tr in table.findall(f"{_W}tr")]


def _simple(table) -> bool:
    """没有套表格、没有合并格子、没有图（图在 PDF 上的位置这里不知道，拆格子会把图和字拆散）、
    每行格子一样多。"""
    if table.find(f".//{_W}tc//{_W}tbl") is not None:
        return False
    if any(node.tag in (f"{_W}drawing", f"{_W}pict", f"{_W}object") for node in table.iter()):
        return False
    if table.find(f".//{_W}vMerge") is not None or table.find(f".//{_W}gridSpan") is not None:
        return False
    rows = _cells(table)
    return bool(rows) and len({len(row) for row in rows}) == 1 and len(rows[0]) > 0


def _partition(total: int, parts: int, check) -> list[tuple[int, int]] | None:
    """total 个行（列）分成 parts 组，check(组号, 起, 止) 都对。只要第一种对得上的分法。"""
    budget = [_MAX_TRIES]

    def search(group: int, start: int) -> list[tuple[int, int]] | None:
        if group == parts:
            return [] if start == total else None
        for end in range(start + 1, total - (parts - group - 1) + 1):
            budget[0] -= 1
            if budget[0] < 0:
                return None
            if check(group, start, end):
                rest = search(group + 1, end)
                if rest is not None:
                    return [(start, end), *rest]
        return None

    return search(0, 0)


def _segments(paragraphs) -> list[int]:
    """一格里每一段、每个换行符（w:br）后面的第一个字是第几个字（不算空白）：pdf2docx 在这里另起了一行。"""
    starts, count = [], 0
    for paragraph in paragraphs:
        starts.append(count)
        for node in paragraph.iter(f"{_W}t", f"{_W}br"):
            if node.tag == f"{_W}br":
                if node.get(f"{_W}type") in (None, "textWrapping"):
                    starts.append(count)
                continue
            count += sum(1 for char in node.text or "" if not char.isspace())
    return starts


def _band(group_paragraphs, table_paragraphs, reverse, chars) -> tuple[float, float]:
    """表格在 PDF 上的上下范围：前一段最后一个对上的字的下沿、后一段第一个对上的字的上沿。"""
    first = group_paragraphs.index(table_paragraphs[0])
    last = group_paragraphs.index(table_paragraphs[-1])
    starts = []
    count = 0
    for paragraph in group_paragraphs:
        starts.append(count)
        count += len(_positions(paragraph)[0])
    ends = starts[1:] + [count]
    top, bottom = float("-inf"), float("inf")
    for number in range(starts[first] - 1, -1, -1):
        if number in reverse:
            top = chars[reverse[number]][1][3]
            break
    for number in range(ends[last], count):
        if number in reverse:
            bottom = chars[reverse[number]][1][1]
            break
    return top, bottom


def _inside(chars, top: float, bottom: float, want: Counter) -> list | None:
    """表格范围里的字，要和表格里的字一个不差。表格是这一页最前或最后的东西时，范围有一头敞着
    （页码、页眉也落在里面）：从敞着的那一头一行一行去掉，直到对上为止。对不上返回 None。"""
    inside = [char for char in chars if top <= (char[1][1] + char[1][3]) / 2 <= bottom]
    rows = sorted(_rows(inside), key=lambda row: row.top)
    while True:
        if Counter(char[0] for char in inside) == want:
            return inside
        if not rows or (top > float("-inf") and bottom < float("inf")):
            return None
        drop = rows.pop() if bottom == float("inf") else rows.pop(0)
        inside = [char for char in inside if not any(char is item for item in drop.chars)]


def _text_of(chars) -> str:
    return "".join(char[0] for char in chars)


def _plan(table, chars) -> dict | None:
    """这张表按原文能拆成几行几列，每个原来的格子拆成哪几行哪几列。拆不了返回 None。"""
    grid = _cells(table)
    rows = _rows(chars)
    if len(rows) < 1:
        return None
    columns = _columns(rows)
    blocks = [[[] for _ in columns] for _ in rows]
    for number, row in enumerate(rows):
        for char in row.chars:
            blocks[number][_column_of(columns, char)].append(char)
    texts = [[[_positions(p)[0] for p in tc.findall(f"{_W}p")] for tc in tr] for tr in grid]
    cell_text = [["".join(parts) for parts in tr] for tr in texts]
    old_rows, old_columns = len(grid), len(grid[0])
    if len(rows) == old_rows and len(columns) == old_columns:
        return None

    def column_ok(k: int, start: int, end: int) -> bool:
        want = "".join(cell_text[r][k] for r in range(old_rows))
        got = "".join(_text_of(blocks[i][j]) for i in range(len(rows)) for j in range(start, end))
        return want == got

    column_groups = _partition(len(columns), old_columns, column_ok)
    if column_groups is None:
        return None

    def row_ok(r: int, start: int, end: int) -> bool:
        want = "".join(cell_text[r])
        got = "".join(_text_of(blocks[i][j]) for first, last in column_groups
                      for i in range(start, end) for j in range(first, last))
        return want == got

    row_groups = _partition(len(rows), old_rows, row_ok)
    if row_groups is None:
        return None
    # 一格里是几行字：原文每一行都要有两列以上的字（不然是格子里折成几行的正文），
    # 而且 pdf2docx 在某一格这里另起了一行（段落或换行符），这一格上下两边都有字（每格只有一行字、
    # 靠段前距错开高低的，是图上的标注，不用拆）
    if len(rows) > old_rows:
        if len(columns) < 2 or any(sum(1 for block in blocks[i] if block) < 2 for i in range(len(rows))):
            return None
        for r, (first, last) in enumerate(row_groups):
            starts = [set(_segments(grid[r][k].findall(f"{_W}p"))) for k in range(old_columns)]
            for offset in range(1, last - first):
                if not any(0 < _row_start(blocks, first, offset, column_groups[k]) < len(cell_text[r][k])
                           and _row_start(blocks, first, offset, column_groups[k]) in starts[k]
                           for k in range(old_columns)):
                    return None
    # 一格里分出几列：每一列至少有两行字（只有一行时，一行里空开一大截不算分列，如「P₁, P₂  ∴ P₁」）
    if len(columns) > old_columns and (
            len(rows) < 2 or any(sum(1 for i in range(len(rows)) if blocks[i][j]) < 2 for j in range(len(columns)))):
        return None
    return {"rows": rows, "columns": columns, "blocks": blocks, "row_groups": row_groups,
            "column_groups": column_groups}


def _row_start(blocks, first: int, offset: int, column_group: tuple[int, int]) -> int:
    """原来那一格里，从第 first 行起的第 offset 行的第一个字是这一格的第几个字。"""
    c_first, c_last = column_group
    return sum(len(blocks[i][j]) for i in range(first, first + offset) for j in range(c_first, c_last))


def _twips(points: float) -> int:
    return max(0, round(points * 20))


def _width(tc) -> int | None:
    node = tc.find(f"{_W}tcPr/{_W}tcW")
    try:
        return int(node.get(f"{_W}w")) if node is not None and node.get(f"{_W}type") in (None, "dxa") else None
    except (TypeError, ValueError):
        return None


def _column_widths(plan: dict, old_widths: list[int]) -> list[int] | None:
    """新的各列宽（twip）：原来每格的宽度不变，格子里按原文各列之间空白的中线分开。"""
    columns, groups = plan["columns"], plan["column_groups"]
    # 原来格子的左边界在 PDF 上的位置：两格之间的空白里，按各格宽度推出整张表的左边
    cumulative = [sum(old_widths[:k]) / 20 for k in range(len(old_widths) + 1)]
    guesses = [(columns[first - 1][1] + columns[first][0]) / 2 - cumulative[k]
               for k, (first, _last) in enumerate(groups) if k]
    if guesses:
        origin = sorted(guesses)[len(guesses) // 2]
    else:
        origin = (columns[0][0] + columns[-1][1]) / 2 - cumulative[-1] / 2
    widths = []
    for k, (first, last) in enumerate(groups):
        left, right = origin + cumulative[k], origin + cumulative[k + 1]
        edges = [left] + [min(max((columns[j - 1][1] + columns[j][0]) / 2, left), right)
                          for j in range(first + 1, last)] + [right]
        parts = [_twips(b - a) for a, b in pairwise(edges)]
        parts[-1] += old_widths[k] - sum(parts)  # 四舍五入的零头补在最后一列，总宽不变
        if any(part <= 0 for part in parts):
            return None
        widths += parts
    return widths


def _row_heights(plan: dict, table) -> list[tuple[int | None, str | None]]:
    """新的各行行高（twip、规则）：原来每行的高度按原文各行的行距分开。"""
    rows, groups = plan["rows"], plan["row_groups"]
    heights = []
    for tr, (first, last) in zip(table.findall(f"{_W}tr"), groups, strict=True):
        node = tr.find(f"{_W}trPr/{_W}trHeight")
        rule = node.get(f"{_W}hRule") if node is not None else None
        try:
            total = int(node.get(f"{_W}val")) if node is not None else None
        except (TypeError, ValueError):
            total = None
        if total is None or last - first == 1:
            heights += [(total, rule)] * (last - first) if last - first == 1 else [(None, rule)] * (last - first)
            continue
        pitches = [rows[i + 1].top - rows[i].top for i in range(first, last - 1)]
        pitches.append(sum(pitches) / len(pitches))
        parts = [round(total * pitch / sum(pitches)) for pitch in pitches]
        parts[-1] += total - sum(parts)
        heights += [(part, rule) for part in parts]
    return heights


def _set(parent, tag: str, attrs: dict[str, str], before: tuple[str, ...] = ()) -> None:
    for node in parent.findall(f"{_W}{tag}"):
        parent.remove(node)
    node = parent.makeelement(f"{_W}{tag}", {f"{_W}{key}": value for key, value in attrs.items()})
    anchor = next((child for child in parent if child.tag in {f"{_W}{name}" for name in before}), None)
    if anchor is not None:
        anchor.addprevious(node)
    else:
        parent.append(node)


_TC_ORDER = ("cnfStyle", "tcW", "gridSpan", "hMerge", "vMerge", "tcBorders", "shd", "noWrap", "tcMar",
             "textDirection", "tcFitText", "vAlign", "hideMark")


def _borders(old_tc, sides: dict[str, bool]):
    """新格子的边框：在原来格子边上的那几条边照原来的，新拆出来的边不画线。"""
    old = old_tc.find(f"{_W}tcPr/{_W}tcBorders")
    borders = old_tc.makeelement(f"{_W}tcBorders", {})
    for side in ("top", "left", "bottom", "right"):
        if sides[side]:
            found = None
            if old is not None:
                found = old.find(f"{_W}{side}")
                if found is None:
                    found = old.find(f"{_W}{ {'left': 'start', 'right': 'end'}.get(side, side)}")
            if found is not None:
                borders.append(copy.deepcopy(found))
        else:
            borders.append(borders.makeelement(f"{_W}{side}", {f"{_W}val": "nil"}))
    return borders


def _run_pieces(paragraph, where, begin: int, end: int) -> list:
    """这一段里第 begin 到 end-1 个字（不算空白）连同中间的空格，复制成 run（格式原样）。
    中间的制表符、换行符换成空格。"""
    first_node, first_index = where[begin]
    last_node, last_index = where[end - 1]
    runs = []
    started = False
    for run in paragraph.iter(f"{_W}r"):
        nodes = [child for child in run if child.tag != f"{_W}rPr"]
        keep = []
        for node in nodes:
            if node.tag == f"{_W}t":
                text = node.text or ""
                start, stop = 0, len(text)
                if node is first_node:
                    started, start = True, first_index
                if not started:
                    continue
                if node is last_node:
                    stop = last_index + 1
                keep.append(text[start:stop])
                if node is last_node:
                    break
            elif started and node.tag in (f"{_W}tab", f"{_W}br", f"{_W}cr"):
                keep.append(" ")
        if keep and "".join(keep):
            clone = run.makeelement(f"{_W}r", {})
            r_pr = run.find(f"{_W}rPr")
            if r_pr is not None:
                clone.append(copy.deepcopy(r_pr))
            t = clone.makeelement(f"{_W}t", {_XML_SPACE: "preserve"})
            t.text = "".join(keep)
            clone.append(t)
            runs.append(clone)
        if any(node is last_node for node in nodes):
            break
    return runs


def _line_heights(plan: dict, table) -> list[str | None]:
    """新的每一行里各段的固定行高取原来那几格里最小的（原来最后一列的段行高大几磅，拆开后字往下沉）。"""
    grid = _cells(table)
    found: list[str | None] = []
    for r, (first, last) in enumerate(plan["row_groups"]):
        values = []
        for tc in grid[r]:
            for spacing in tc.iter(f"{_W}spacing"):
                try:
                    values.append(int(spacing.get(f"{_W}line")))
                except (TypeError, ValueError):
                    continue
        found += [str(min(values)) if values else None] * (last - first)
    return found


def _new_paragraph(source, runs: list, align: str | None, line: str | None = None):
    """新格子里的一段：格式照原来那一段，段前段后距去掉。align 为 None 时（这一列没拆开，格子的宽度、
    位置和原来一样）左缩进、对齐、制表位照原来那一段，右缩进、首行缩进去掉（原来逼着几行折开用的，
    现在一格一行）；拆开的列按 align 对齐、不缩进。"""
    paragraph = source.makeelement(f"{_W}p", {})
    p_pr = source.find(f"{_W}pPr")
    p_pr = copy.deepcopy(p_pr) if p_pr is not None else paragraph.makeelement(f"{_W}pPr", {})
    for tag in ("sectPr", "framePr") if align is None else ("tabs", "ind", "jc", "sectPr", "framePr"):
        for node in p_pr.findall(f"{_W}{tag}"):
            p_pr.remove(node)
    spacing = p_pr.find(f"{_W}spacing")
    if spacing is not None:
        spacing.set(f"{_W}before", "0")
        spacing.set(f"{_W}after", "0")
        if line is not None and spacing.get(f"{_W}lineRule") == "exact":
            spacing.set(f"{_W}line", line)
    ind = p_pr.find(f"{_W}ind")
    if align is None and ind is not None:
        for name in ("right", "end", "firstLine", "hanging"):
            ind.attrib.pop(f"{_W}{name}", None)
        ind.set(f"{_W}right", "0")
    if align is not None:
        _set(p_pr, "ind", {"left": "0", "right": "0", "firstLine": "0"}, ("contextualSpacing", "mirrorIndents",
                                                                          "suppressOverlap", "jc", "textDirection",
                                                                          "textAlignment", "outlineLvl", "rPr"))
        _set(p_pr, "jc", {"val": align}, ("textDirection", "textAlignment", "outlineLvl", "rPr"))
    paragraph.append(p_pr)
    for run in runs:
        paragraph.append(run)
    if p_pr.find(f"{_W}rPr") is not None and runs:
        p_pr.remove(p_pr.find(f"{_W}rPr"))
        p_pr.append(copy.deepcopy(runs[0].find(f"{_W}rPr")) if runs[0].find(f"{_W}rPr") is not None
                    else p_pr.makeelement(f"{_W}rPr", {}))
    return paragraph


def _alignments(plan: dict) -> list[str]:
    """每一列：原文里这一列各格的字中线对齐就居中，左边对齐就靠左，右边对齐就靠右。"""
    found = []
    for j in range(len(plan["columns"])):
        boxes = [(min(c[1][0] for c in block), max(c[1][2] for c in block))
                 for block in (row[j] for row in plan["blocks"]) if block]
        lefts = [left for left, _right in boxes]
        rights = [right for _left, right in boxes]
        middles = [(left + right) / 2 for left, right in boxes]
        if max(lefts) - min(lefts) <= _CENTERED:
            found.append("left")
        elif max(middles) - min(middles) <= _CENTERED:
            found.append("center")
        elif max(rights) - min(rights) <= _CENTERED:
            found.append("right")
        else:
            found.append("left")
    return found


def _rebuild(table, plan: dict) -> bool:
    grid = _cells(table)
    old_widths = [_width(tc) for tc in grid[0]]
    if any(width is None for width in old_widths):
        return False
    widths = _column_widths(plan, [int(width) for width in old_widths])  # type: ignore[arg-type]
    if widths is None:
        return False
    heights = _row_heights(plan, table)
    lines = _line_heights(plan, table)
    aligns = _alignments(plan)
    blocks, row_groups, column_groups = plan["blocks"], plan["row_groups"], plan["column_groups"]
    # 每个原来格子里的字（不算空白）：(段, where, 第几个字)，从上到下、从左到右
    sources = [[[(p, where, number) for p in tc.findall(f"{_W}p") for where in [_positions(p)[1]]
                 for number in range(len(where))] for tc in tr] for tr in grid]
    new_rows = []
    old_trs = table.findall(f"{_W}tr")
    row_number = 0
    for r, (first, last) in enumerate(row_groups):
        for i in range(first, last):
            tr = old_trs[r].makeelement(f"{_W}tr", {})
            for attr, value in old_trs[r].attrib.items():
                tr.set(attr, value)
            old_tr_pr = old_trs[r].find(f"{_W}trPr")
            tr_pr = copy.deepcopy(old_tr_pr) if old_tr_pr is not None else tr.makeelement(f"{_W}trPr", {})
            height, rule = heights[row_number]
            if height is not None:
                _set(tr_pr, "trHeight", {"val": str(height), **({"hRule": rule} if rule else {})})
            tr.append(tr_pr)
            column_number = 0
            for k, (c_first, c_last) in enumerate(column_groups):
                old_tc = grid[r][k]
                offset = sum(len(blocks[row][j]) for row in range(first, i) for j in range(c_first, c_last))
                for j in range(c_first, c_last):
                    tc = old_tc.makeelement(f"{_W}tc", {})
                    old_tc_pr = old_tc.find(f"{_W}tcPr")
                    tc_pr = copy.deepcopy(old_tc_pr) if old_tc_pr is not None else tc.makeelement(f"{_W}tcPr", {})
                    tc_pr.attrib.clear()
                    _set(tc_pr, "tcW", {"w": str(widths[column_number]), "type": "dxa"}, _TC_ORDER[2:])
                    for node in tc_pr.findall(f"{_W}tcBorders"):
                        tc_pr.remove(node)
                    borders = _borders(old_tc, {"top": i == first, "bottom": i == last - 1,
                                                "left": j == c_first, "right": j == c_last - 1})
                    anchor = next((node for node in tc_pr if node.tag in {f"{_W}{name}" for name in _TC_ORDER[6:]}),
                                  None)
                    if anchor is not None:
                        anchor.addprevious(borders)
                    else:
                        tc_pr.append(borders)
                    tc.append(tc_pr)
                    count = len(blocks[i][j])
                    chars = sources[r][k][offset:offset + count]
                    offset += count
                    old_paragraphs = old_tc.findall(f"{_W}p")
                    source = chars[0][0] if chars else (old_paragraphs[0] if old_paragraphs else None)
                    runs: list = []
                    # 一格的字可能落在原来的两段里（一行接着上一段）：一段一段搬
                    pieces: list[tuple] = []
                    for paragraph, where, number in chars:
                        if pieces and pieces[-1][0] is paragraph and pieces[-1][3] == number:
                            pieces[-1] = (paragraph, where, pieces[-1][2], number + 1)
                        else:
                            pieces.append((paragraph, where, number, number + 1))
                    for paragraph, where, begin, end in pieces:
                        if runs:
                            space = paragraph.makeelement(f"{_W}r", {})
                            t = space.makeelement(f"{_W}t", {_XML_SPACE: "preserve"})
                            t.text = " "
                            space.append(t)
                            runs.append(space)
                        runs += _run_pieces(paragraph, where, begin, end)
                    if source is None:
                        tc.append(tc.makeelement(f"{_W}p", {}))
                    else:
                        tc.append(_new_paragraph(source, runs, aligns[j] if c_last - c_first > 1 else None,
                                                  lines[row_number]))
                    tr.append(tc)
                    column_number += 1
            new_rows.append(tr)
            row_number += 1
    grid_node = table.find(f"{_W}tblGrid")
    if grid_node is not None:
        for node in list(grid_node):
            grid_node.remove(node)
        for width in widths:
            grid_node.append(grid_node.makeelement(f"{_W}gridCol", {f"{_W}w": str(width)}))
    anchor = old_trs[-1]  # 新的行接在原来最后一行后面，再去掉原来的行
    for tr in new_rows:
        anchor.addnext(tr)
        anchor = tr
    for tr in old_trs:
        table.remove(tr)
    return True


def _refine_page(group: list, page) -> int:
    chars = _page_chars(page)
    page_text = "".join(char[0] for char in chars)
    paragraphs = [p for element in group
                  for p in ([element] if element.tag == f"{_W}p" else element.iter(f"{_W}p"))]
    word_text = "".join(_positions(p)[0] for p in paragraphs)
    mapping = _alignment(page_text, word_text)
    reverse = {value: key for key, value in mapping.items()}
    # 套在别的表格里的表格也算（讲义的真值表套在例题的彩色框里）
    tables = [table for element in group for table in element.iter(f"{_W}tbl")]
    changed = 0
    for table in tables:
        if not _simple(table):
            continue
        table_paragraphs = [p for p in table.iter(f"{_W}p") if _positions(p)[0]]
        if not table_paragraphs:
            continue
        top, bottom = _band(paragraphs, table_paragraphs, reverse, chars)
        inside = _inside(chars, top, bottom, Counter("".join(_positions(p)[0] for p in table_paragraphs)))
        if inside is None:
            continue
        plan = _plan(table, inside)
        if plan is None:
            continue
        if _rebuild(table, plan):
            changed += 1
    return changed


def refine_tables_from_pdf(docx_path: Path, pdf_path: Path) -> int:
    """格子里挤着几行几列字的表格按原文的行列拆开，返回拆了几张表。"""
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
        logger.info(f"[PDF] 打不开原 PDF 对照表格的行列：{exc!r}")
        return 0
    changed = 0
    pages = []
    with pdf:
        if len(groups) != len(pdf):
            return 0
        for page_index, group in enumerate(groups):
            count = _refine_page(group, pdf[page_index])
            if count:
                pages.append(str(page_index + 1))
                changed += count
    if changed:
        doc.save(str(docx_path))
        logger.info(f"[PDF] 第 {'、'.join(pages)} 页共 {changed} 张表格按原文的行列拆开")
    return changed
