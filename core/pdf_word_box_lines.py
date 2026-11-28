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


def join_box_rows(body, continues: Callable, adjacent: Callable | None = None, children: list | None = None) -> int:
    """把同一段折到后面几张一行表里的行并进前一格；返回并了几张表。
    continues(前一段, 后一段) 收两个 w:p，判断后一段是不是前一段折下来的；adjacent(前一段, 后一段)
    看原 PDF 上这两行是不是上下挨着（pdf2docx 偶尔把框里的行排乱，乱了的不并）。children 是要看的
    那一段正文元素（一页），默认整篇。"""
    joined = 0
    children = list(body) if children is None else list(children)
    index = 0
    while index < len(children):
        table = children[index]
        shape = _single_cell(table) if table.tag == f"{_W}tbl" else None
        if shape is None:
            index += 1
            continue
        row, cell = shape
        after = index + 1
        while after < len(children) and _spacer(children[after]):
            after += 1
        following = children[after] if after < len(children) else None
        other = _single_cell(following) if following is not None and following.tag == f"{_W}tbl" else None
        lasts = [p for p in cell.findall(f"{_W}p") if _text(p).strip()]
        firsts = [p for p in other[1].findall(f"{_W}p") if _text(p).strip()] if other is not None else []
        if (other is None or not lasts or not firsts or not _same_box(cell, other[1])
                or any(abs(a - b) > _SLACK for a, b in zip(_box(table), _box(following)))
                or not continues(lasts[-1], firsts[0])
                or (adjacent is not None and not adjacent(lasts[-1], firsts[0]))):
            index += 1
            continue
        spacers = children[index + 1:after]
        height = row.find(f"{_W}trPr/{_W}trHeight")
        if height is not None:
            # 并成一段后按译文、按 Word 的字重新折行，行数可能和原文差一行：最小值，不裁字
            height.set(f"{_W}val", str(round(_height(row) + _height(other[0]))))
            height.set(f"{_W}hRule", "atLeast")
        _keep_pitch(firsts[0])
        for child in list(other[1]):
            if child.tag != f"{_W}tcPr":
                cell.append(child)
        for element in [*spacers, following]:
            body.remove(element)
        del children[index + 1:after + 1]
        joined += 1
    return joined


def _pdf_lines(page) -> dict[str, list[tuple[int, int]]]:
    """原 PDF 这一页每一行（去掉空白的文字）在第几块、块里第几行。"""
    found: dict[str, list[tuple[int, int]]] = {}
    for block_number, block in enumerate(page.get_text("dict").get("blocks", [])):
        if block.get("type") != 0:
            continue
        for line_number, line in enumerate(block.get("lines") or []):
            text = _SPACE.sub("", "".join(span.get("text", "") for span in line.get("spans", [])))
            if text:
                found.setdefault(text, []).append((block_number, line_number))
    return found


def _continues_xml(paragraph_type, continues) -> Callable:
    """continues 收 python-docx 的段落；这里给它包一层，收 w:p。"""
    def check(first, second) -> bool:
        return continues(paragraph_type(first, None), paragraph_type(second, None))

    return check


def join_box_lines(docx_path: Path, pdf_path: Path) -> int:
    """对照原 PDF，把框里同一段折下来、一行一张表的几行并进同一格；返回并了几张表。"""
    try:
        from core.pdf_component import require_fitz

        fitz = require_fitz()
    except Exception:  # noqa: BLE001 - no PyMuPDF: leave the rows as converted
        return 0
    from docx import Document
    from docx.text.paragraph import Paragraph

    from core.pdf_word_fit import page_groups
    from core.pdf_word_route import paragraph_continues

    doc = Document(str(docx_path))
    body = doc.element.body
    groups = page_groups(body)
    joined = 0
    with fitz.open(pdf_path) as pdf:
        if len(groups) != len(pdf):
            return 0
        for group, page in zip(groups, pdf):
            lines = _pdf_lines(page)

            def adjacent(first, second, lines=lines) -> bool:
                above = lines.get(_SPACE.sub("", _text(first)), [])
                below = set(lines.get(_SPACE.sub("", _text(second)), []))
                return any((block, line + 1) in below for block, line in above)

            joined += join_box_rows(body, _continues_xml(Paragraph, paragraph_continues), adjacent, group)
    if joined:
        doc.save(str(docx_path))
        logger.info(f"[PDF] 框里一行一张表的，{joined} 处同一段折下来的行并进同一格")
    return joined
