"""PDF 转 Word 后，框里一行一张表的，同一段的几行并进同一格（反馈 #28 讲义第 59、60、66 页）。

讲义的注记框、例题框：pdf2docx 把框里每一行字做成一张一行一格的表，表和表之间隔着 1 磅的空段。
「合并折行」只看相邻的段落，隔着表格接不上，框里的段落就一行一段：翻译时一行一行地译，句子被切碎；
排满的行 pdf2docx 又标成居中，中文比英文短，译文每行各自居中、左边参差。

做法：相邻两张一行一格的表（中间只隔空段），宽度、缩进差不多，前一格最后一段和后一格第一段是同一段
折下来的（和合并折行同一个判断：没以句末标点结束、字体一样、不是列表项、中间没空行），底色也一样
（或者都是浅色；深色的标题条不并），原 PDF 上这两行也是上下挨着的（pdf2docx 偶尔把框里的行排乱，
讲义第 23 页的例题框），就把后一格的
内容挪进前一格，行高加上去（行距照原文：一张表的行高），后一张表和中间的空段去掉。并进来的几行随后由
合并折行接成一段。
"""
from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from loguru import logger

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_SPACE = re.compile(r"\s+")

#: 拆开的一行，每格至少有这么多个字（不算空格：这时 pdf2docx 的词间还没有空格）；真值表一格一个 T、F 的不算。
_CHARS = 8

#: 两张表的宽度、缩进相差这么多以内（1/20 磅）算同一个框：pdf2docx 估的框宽每行差一点。
_SLACK = 120


def _number(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _single_cell(table):
    """一行一格的表：返回 (行, 格)，不是返回 None。"""
    rows = table.findall(f"{_W}tr")
    if len(rows) != 1:
        return None
    cells = rows[0].findall(f"{_W}tc")
    if len(cells) != 1:
        return None
    return rows[0], cells[0]


def _box(table) -> tuple[float, float]:
    ind = table.find(f"{_W}tblPr/{_W}tblInd")
    width = sum(_number(column.get(f"{_W}w")) for column in table.findall(f"{_W}tblGrid/{_W}gridCol"))
    return (_number(ind.get(f"{_W}w")) if ind is not None else 0.0), width


#: 底色亮度（0–1）不低于这个算浅色：框里的浅底色、白底、没涂底色可以并；深色的标题条不和正文并。
_LIGHT = 0.75


def _fill(cell) -> str:
    shading = cell.find(f"{_W}tcPr/{_W}shd")
    fill = (shading.get(f"{_W}fill") or "").upper() if shading is not None else ""
    return "" if fill in ("", "AUTO") else fill


def _light(fill: str) -> bool:
    if not fill:
        return True
    try:
        red, green, blue = (int(fill[index:index + 2], 16) / 255 for index in (0, 2, 4))
    except ValueError:
        return False
    return 0.299 * red + 0.587 * green + 0.114 * blue >= _LIGHT


def _same_box(cell, other) -> bool:
    """底色一样，或者都是浅色（pdf2docx 给同一个框的几行取的底色常常不一样：浅粉、白、没涂）。"""
    first, second = _fill(cell), _fill(other)
    return first == second or (_light(first) and _light(second))


def _text(element) -> str:
    return "".join(node.text or "" for node in element.iter(f"{_W}t"))


def _spacer(element) -> bool:
    return (element.tag == f"{_W}p" and not _text(element).strip()
            and next(element.iter(f"{_W}drawing", f"{_W}pict", f"{_W}sectPr"), None) is None)


def _height(row) -> float:
    height = row.find(f"{_W}trPr/{_W}trHeight")
    return _number(height.get(f"{_W}val")) if height is not None else 0.0


def _keep_pitch(paragraph) -> None:
    """挪进来的这一行离上一行还是原来一行的高度。pdf2docx 把一行字摆在一张表的正中：表高 = 段前距 + 行距 +
    下面同样的空当（讲义第 59 页：26 + 218 + 26 = 270，正是原文的行距 13.5 磅）。挪进前一格后，段前距
    改成上下两份空当，这一行和上一行的距离仍是 270；表和表之间那 1 磅的空段不算。框的最后一行表高还带着
    框底的留白，所以不按表高算。"""
    spacing = paragraph.find(f"{_W}pPr/{_W}spacing")
    if spacing is None or spacing.get(f"{_W}lineRule") != "exact":
        return
    spacing.set(f"{_W}before", str(round(2 * _number(spacing.get(f"{_W}before")))))


def _last_cell(table):
    """表的最后一行只有一格时返回 (行, 格)：标题条和第一行字在同一张表里的（讲义第 66 页例题框）。"""
    rows = table.findall(f"{_W}tr")
    if not rows:
        return None
    cells = rows[-1].findall(f"{_W}tc")
    return (rows[-1], cells[0]) if len(cells) == 1 else None


def _texts(cell) -> list:
    return [p for p in cell.findall(f"{_W}p") if _text(p).strip()]


def _loosen(row) -> None:
    """并进来的行按译文、按 Word 的字重新折行，行数可能和原文差一行：行高改成最小值，不裁字。"""
    height = row.find(f"{_W}trPr/{_W}trHeight")
    if height is not None:
        height.set(f"{_W}hRule", "atLeast")


def _move_first_line(cell, paragraph, following, adjacent: Callable | None, continues: Callable) -> bool:
    """后一张表不是一行一格（图上标签那种表）、第一格第一段却是上一段折下来的（讲义第 66 页
    「attached.」）：只把这一段挪过来。一定要原 PDF 上上下挨着才挪。"""
    if adjacent is None or following.tag != f"{_W}tbl":
        return False
    rows = following.findall(f"{_W}tr")
    cells = rows[0].findall(f"{_W}tc") if rows else []
    texts = _texts(cells[0]) if cells else []
    if not texts or texts[0] is not next((p for p in cells[0].iter(f"{_W}p") if _text(p).strip()), None):
        return False
    first = texts[0]
    if not continues(paragraph, first) or not adjacent(paragraph, first):
        return False
    source = first.getparent()
    _keep_pitch(first)
    cell.append(first)
    if source.find(f"{_W}p") is None:
        source.append(source.makeelement(f"{_W}p", {}))
    return True


def join_box_rows(body, continues: Callable, adjacent: Callable | None = None, children: list | None = None) -> int:
    """把同一段折到后面几张一行表里的行并进前一格；返回并了几处。
    continues(前一段, 后一段) 收两个 w:p，判断后一段是不是前一段折下来的；adjacent(前一段, 后一段)
    看原 PDF 上这两行是不是上下挨着（pdf2docx 偶尔把框里的行排乱，乱了的不并）。children 是要看的
    那一段正文元素（一页），默认整篇。"""
    joined = 0
    children = list(body) if children is None else list(children)
    index = 0
    while index < len(children):
        table = children[index]
        shape = _last_cell(table) if table.tag == f"{_W}tbl" else None
        if shape is None:
            index += 1
            continue
        row, cell = shape
        after = index + 1
        while after < len(children) and _spacer(children[after]):
            after += 1
        following = children[after] if after < len(children) else None
        lasts = _texts(cell)
        if following is None or not lasts:
            index += 1
            continue
        other = _single_cell(following) if following.tag == f"{_W}tbl" else None
        firsts = _texts(other[1]) if other is not None else []
        if (other is None or not firsts or not _same_box(cell, other[1])
                or any(abs(a - b) > _SLACK for a, b in zip(_box(table), _box(following)))
                or not continues(lasts[-1], firsts[0])
                or (adjacent is not None and not adjacent(lasts[-1], firsts[0]))):
            if _move_first_line(cell, lasts[-1], following, adjacent, continues):
                _loosen(row)
                joined += 1
            index += 1
            continue
        spacers = children[index + 1:after]
        height = row.find(f"{_W}trPr/{_W}trHeight")
        if height is not None:
            height.set(f"{_W}val", str(round(_height(row) + _height(other[0]))))
        _loosen(row)
        _keep_pitch(firsts[0])
        for child in list(other[1]):
            if child.tag != f"{_W}tcPr":
                cell.append(child)
        for element in [*spacers, following]:
            body.remove(element)
        del children[index + 1:after + 1]
        joined += 1
    return joined


def _pdf_lines(page) -> dict[str, list[tuple[int, int, float]]]:
    """原 PDF 这一页每一行（去掉空白的文字）在第几块、块里第几行、上沿在哪（磅）。"""
    from core.pdf_word_full_lines import visual_lines

    found: dict[str, list[tuple[int, int, float]]] = {}
    for block_number, lines in enumerate(visual_lines(page)):
        for line_number, (text, box) in enumerate(lines):
            found.setdefault(text, []).append((block_number, line_number, box[1]))
    return found


def _key(element) -> str:
    return _SPACE.sub("", _text(element))


def join_split_cells(rows, lines) -> int:
    """一行里几格的字连起来正好是原 PDF 两端对齐排满的一行（中间空得宽，pdf2docx 拆成了两格，讲义第 66 页
    「…set {x, y, z}.」「The relation in (i) is」）：并成一格一段，译文不再一半被推到右边。lines 是
    core.pdf_word_full_lines.full_lines 认出的排满的行。每格都得是一段字：真值表一格一个 T、F，
    连起来也像一整行，不能并。并好的一段两端对齐。返回并了几行。"""
    merged = 0
    for row in rows:
        cells = row.findall(f"{_W}tc")
        filled = [cell for cell in cells if _texts(cell)]
        if len(cells) < 2 or len(filled) < 2 or any(len(_texts(cell)) != 1 for cell in filled):
            continue
        if any(len(_key(_texts(cell)[0])) < _CHARS for cell in filled):
            continue
        if "".join(_key(_texts(cell)[0]) for cell in filled) not in lines:
            continue
        first_cell, target = cells[0], _texts(filled[0])[0]
        if filled[0] is not first_cell:
            continue
        span = 0
        width = 0.0
        for cell in cells:
            grid = cell.find(f"{_W}tcPr/{_W}gridSpan")
            span += int(_number(grid.get(f"{_W}val"))) if grid is not None else 1
            width += _number(cell.find(f"{_W}tcPr/{_W}tcW").get(f"{_W}w")) if cell.find(f"{_W}tcPr/{_W}tcW") is not None else 0
        for cell in filled[1:]:
            runs = _texts(cell)[0].findall(f"{_W}r")
            if runs:
                space = runs[0].makeelement(f"{_W}r", {})
                text = space.makeelement(f"{_W}t", {"{http://www.w3.org/XML/1998/namespace}space": "preserve"})
                text.text = " "
                space.append(text)
                target.append(space)
            for run in runs:
                target.append(run)
        for cell in cells[1:]:
            row.remove(cell)
        tc_pr = first_cell.find(f"{_W}tcPr")
        if tc_pr is None:
            tc_pr = first_cell.makeelement(f"{_W}tcPr", {})
            first_cell.insert(0, tc_pr)
        if span > 1:
            grid = tc_pr.find(f"{_W}gridSpan")
            if grid is None:
                grid = tc_pr.makeelement(f"{_W}gridSpan", {})
                tc_pr.insert(1 if tc_pr.find(f"{_W}tcW") is not None else 0, grid)
            grid.set(f"{_W}val", str(span))
        if width and tc_pr.find(f"{_W}tcW") is not None:
            tc_pr.find(f"{_W}tcW").set(f"{_W}w", str(round(width)))
        _justify(target)
        merged += 1
    return merged


def _justify(paragraph) -> None:
    """并回来的是原文排满的一行：两端对齐，后面折下来的行才认得出是同一段（core.pdf_word_route._continues）。"""
    p_pr = paragraph.find(f"{_W}pPr")
    if p_pr is None:
        p_pr = paragraph.makeelement(f"{_W}pPr", {})
        paragraph.insert(0, p_pr)
    jc = p_pr.find(f"{_W}jc")
    if jc is None:
        jc = p_pr.makeelement(f"{_W}jc", {})
        p_pr.append(jc)
    jc.set(f"{_W}val", "both")


def _flatten(table) -> None:
    """格子里只有空段和一张一行一格的小表时，把小表的内容提出来（讲义第 23 页：pdf2docx 把排乱了的
    最后一行放进小表，前面垫一大段空白，留给被挪走的那几行）。"""
    shape = _single_cell(table)
    if shape is None:
        return
    row, cell = shape
    nested = [child for child in cell if child.tag == f"{_W}tbl"]
    others = [child for child in cell if child.tag == f"{_W}p"]
    if len(nested) != 1 or any(_text(p).strip() for p in others):
        return
    inner = _single_cell(nested[0])
    if inner is None:
        return
    for child in list(cell):
        if child.tag != f"{_W}tcPr":
            cell.remove(child)
    for child in list(inner[1]):
        if child.tag != f"{_W}tcPr":
            cell.append(child)
    height = row.find(f"{_W}trPr/{_W}trHeight")
    if height is not None:
        height.set(f"{_W}val", str(round(_height(inner[0]))))


def _top(table, lines: dict) -> float | None:
    """这张表里的字在原 PDF 上最靠上的那一行的上沿；有一段对不上原文时返回 None（不猜）。"""
    tops = []
    for paragraph in table.iter(f"{_W}p"):
        key = _key(paragraph)
        if not key:
            continue
        if key not in lines:
            return None
        tops.append(min(top for _block, _line, top in lines[key]))
    return min(tops) if tops else None


def order_box_rows(body, children: list, lines: dict) -> int:
    """框里一行一张表的几张表，顺序和原 PDF 不一样的（pdf2docx 偶尔排乱，讲义第 23 页例题框），按原文
    从上到下排回去；返回挪了几张。"""
    moved = 0
    index = 0
    while index < len(children):
        run, slots, cells = [], [], []
        cursor = index
        while cursor < len(children):
            element = children[cursor]
            shape = _single_cell(element) if element.tag == f"{_W}tbl" else None
            if shape is not None and (
                    not run or (all(abs(a - b) <= _SLACK for a, b in zip(_box(run[-1]), _box(element)))
                                and _same_box(cells[-1], shape[1]))):
                run.append(element)
                cells.append(shape[1])
                slots.append(cursor)
                cursor += 1
                while cursor < len(children) and _spacer(children[cursor]):
                    cursor += 1
                continue
            break
        if len(run) < 2:
            index = max(cursor, index + 1)
            continue
        found = [_top(table, lines) for table in run]
        tops = [top for top in found if top is not None]
        if len(tops) != len(run) or tops == sorted(tops):
            index = cursor
            continue
        for table in run:
            _flatten(table)
        found = [_top(table, lines) for table in run]
        tops = [top for top in found if top is not None]
        if len(tops) != len(run):
            index = cursor
            continue
        order = sorted(range(len(run)), key=lambda number: tops[number])
        holders = []
        for table in run:
            holder = table.makeelement(f"{_W}p", {})
            table.addprevious(holder)
            body.remove(table)
            holders.append(holder)
        for holder, number in zip(holders, order):
            holder.addprevious(run[number])
            body.remove(holder)
            children[slots[holders.index(holder)]] = run[number]
        moved += sum(1 for position, number in enumerate(order) if position != number)
        index = cursor
    return moved


def _continues_xml(paragraph_type, continues) -> Callable:
    """continues 收 python-docx 的段落；这里给它包一层，收 w:p。"""
    def check(first, second) -> bool:
        return continues(paragraph_type(first, None), paragraph_type(second, None))

    return check


def _each_page(docx_path: Path, pdf_path: Path, work: Callable) -> int:
    """逐页对照原 PDF：work(正文, 这一页的元素, 这一页原 PDF) 返回改了几处；改了就存盘。"""
    try:
        from core.pdf_component import require_fitz

        fitz = require_fitz()
    except Exception:  # noqa: BLE001 - no PyMuPDF: leave the rows as converted
        return 0
    from docx import Document

    from core.pdf_word_fit import page_groups

    doc = Document(str(docx_path))
    body = doc.element.body
    groups = page_groups(body)
    changed = 0
    with fitz.open(pdf_path) as pdf:
        if len(groups) != len(pdf):
            return 0
        for group, page in zip(groups, pdf):
            changed += work(body, group, page)
    if changed:
        doc.save(str(docx_path))
    return changed


def join_split_lines(docx_path: Path, pdf_path: Path) -> int:
    """原 PDF 的一行被 pdf2docx 拆成同一行的几格的，并回一格一段（join_split_cells）。"""
    from core.pdf_word_full_lines import full_lines

    def work(_body, group, page) -> int:
        lines = full_lines(page)
        return sum(join_split_cells(element.iter(f"{_W}tr"), lines) for element in group if element.tag == f"{_W}tbl")

    merged = _each_page(docx_path, pdf_path, work)
    if merged:
        logger.info(f"[PDF] {merged} 行原文的一行被拆成了几格，并回一格")
    return merged


def join_box_lines(docx_path: Path, pdf_path: Path) -> int:
    """对照原 PDF，框里一行一张表的：排乱的按原文排回去，同一段折下来的几行并进同一格；返回改了几处。"""
    from docx.text.paragraph import Paragraph

    from core.pdf_word_route import paragraph_continues

    continues = _continues_xml(Paragraph, paragraph_continues)

    def work(body, group, page) -> int:
        lines = _pdf_lines(page)

        def adjacent(first, second) -> bool:
            above = lines.get(_key(first), [])
            below = {(block, line) for block, line, _top in lines.get(_key(second), [])}
            return any((block, line + 1) in below for block, line, _top in above)

        return order_box_rows(body, group, lines) + join_box_rows(body, continues, adjacent, group)

    joined = _each_page(docx_path, pdf_path, work)
    if joined:
        logger.info(f"[PDF] 框里一行一张表的，{joined} 处排回原文顺序、并进同一格")
    return joined
