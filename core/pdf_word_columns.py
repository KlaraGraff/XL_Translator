"""PDF 转 Word 后，把 pdf2docx 用「分栏」摆的左右并排内容改成无边框表格。

pdf2docx 遇到左右并排的两块（签证审核清单每页顶上的「申请人姓名」「护照号码」两个框）
时，开一个两栏的连续分节放左边那块，再开一个「从下一栏开始」的分节放右边那块。
Microsoft Word 照样左右并排；WPS 却把两块上下叠起来，每页多占好几行，右上角的标志也
被挤掉一半（maple 2026-11-02 用 WPS 导出：Word 里 3 页的审核清单在 WPS 里成了 5 页）。

改成一行几格、没有边框的表格：每一栏是一格，宽度照原来的栏宽，格子里放原来那一栏的
内容。表格在 Word 和 WPS 里排法一致。分节符都留着（每页开头的「新页」分节不能丢，
core.pdf_word_guard、core.pdf_word_fit 按它分页），只是改成单栏、连续。
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _section_blocks(body) -> list[tuple[list, Any, Any]]:
    """[(这一节的元素, 这一节的 sectPr, 带着 sectPr 的那一段或 None)]；最后的 body sectPr 也算一节。"""
    blocks: list[tuple[list, Any, Any]] = []
    current: list = []
    for child in body:
        if child.tag == f"{_W}sectPr":
            blocks.append((current, child, None))
            current = []
            continue
        sect = child.find(f"{_W}pPr/{_W}sectPr") if child.tag == f"{_W}p" else None
        if sect is not None:
            blocks.append((current, sect, child))
            current = []
        else:
            current.append(child)
    return blocks


def _kind(sect_pr) -> str:
    kind = sect_pr.find(f"{_W}type")
    return kind.get(f"{_W}val") if kind is not None else "nextPage"


def _columns(sect_pr) -> list[int]:
    """这一节的各栏宽度（含栏间距）；单栏返回空表。"""
    cols = sect_pr.find(f"{_W}cols")
    if cols is None:
        return []
    try:
        count = int(cols.get(f"{_W}num", "1"))
    except ValueError:
        return []
    if count < 2:
        return []
    defined = cols.findall(f"{_W}col")
    if len(defined) >= count:
        widths = []
        for col in defined[:count]:
            try:
                widths.append(int(float(col.get(f"{_W}w", "0"))) + int(float(col.get(f"{_W}space", "0"))))
            except ValueError:
                return []
        return widths if all(width > 0 for width in widths) else []
    # 等宽分栏：按版心宽度平分
    size, margin = sect_pr.find(f"{_W}pgSz"), sect_pr.find(f"{_W}pgMar")
    try:
        text_width = (int(size.get(f"{_W}w")) - int(margin.get(f"{_W}left")) - int(margin.get(f"{_W}right")))
        space = int(float(cols.get(f"{_W}space", "720")))
    except (AttributeError, TypeError, ValueError):
        return []
    width = (text_width - space * (count - 1)) // count
    return [width + space] * (count - 1) + [width] if width > 0 else []


def _has_visible_content(element) -> bool:
    if "".join(node.text or "" for node in element.iter(f"{_W}t")).strip():
        return True
    return any(True for _ in element.iter(f"{_W}drawing", f"{_W}pict", f"{_W}object"))


def _tiny_paragraph(maker):
    paragraph = maker.makeelement(f"{_W}p", {})
    p_pr = paragraph.makeelement(f"{_W}pPr", {})
    p_pr.append(p_pr.makeelement(f"{_W}spacing", {f"{_W}before": "0", f"{_W}after": "0",
                                                   f"{_W}line": "20", f"{_W}lineRule": "exact"}))
    r_pr = p_pr.makeelement(f"{_W}rPr", {})
    r_pr.append(r_pr.makeelement(f"{_W}sz", {f"{_W}val": "2"}))
    p_pr.append(r_pr)
    paragraph.append(p_pr)
    return paragraph


def _shrink_paragraph(paragraph) -> None:
    """只剩段落标记、带着分节符的空段：行高压到最小。"""
    p_pr = paragraph.find(f"{_W}pPr")
    spacing = p_pr.find(f"{_W}spacing")
    if spacing is None:
        spacing = p_pr.makeelement(f"{_W}spacing", {})
        anchor = next((child for child in p_pr if child.tag in (f"{_W}ind", f"{_W}jc", f"{_W}rPr", f"{_W}sectPr")), None)
        if anchor is not None:
            anchor.addprevious(spacing)
        else:
            p_pr.append(spacing)
    for name, value in (("before", "0"), ("after", "0"), ("line", "20"), ("lineRule", "exact")):
        spacing.set(f"{_W}{name}", value)


def _single_column(sect_pr) -> None:
    cols = sect_pr.find(f"{_W}cols")
    if cols is not None:
        for col in cols.findall(f"{_W}col"):
            cols.remove(col)
        for name in ("num", "equalWidth", "sep"):
            if cols.get(f"{_W}{name}") is not None:
                del cols.attrib[f"{_W}{name}"]
    kind = sect_pr.find(f"{_W}type")
    if kind is not None and kind.get(f"{_W}val") == "nextColumn":
        kind.set(f"{_W}val", "continuous")


def _layout_table(maker, widths: list[int], cells: list[list]):
    """一行几格、没有边框、格子内边距为 0 的表格。"""
    tbl = maker.makeelement(f"{_W}tbl", {})
    tbl_pr = tbl.makeelement(f"{_W}tblPr", {})
    tbl_pr.append(tbl_pr.makeelement(f"{_W}tblW", {f"{_W}w": str(sum(widths)), f"{_W}type": "dxa"}))
    tbl_pr.append(tbl_pr.makeelement(f"{_W}tblInd", {f"{_W}w": "0", f"{_W}type": "dxa"}))
    borders = tbl_pr.makeelement(f"{_W}tblBorders", {})
    for side in ("top", "left", "bottom", "right", "insideH", "insideV"):
        borders.append(borders.makeelement(f"{_W}{side}", {f"{_W}val": "nil"}))
    tbl_pr.append(borders)
    tbl_pr.append(tbl_pr.makeelement(f"{_W}tblLayout", {f"{_W}type": "fixed"}))
    margins = tbl_pr.makeelement(f"{_W}tblCellMar", {})
    for side in ("top", "left", "bottom", "right"):
        margins.append(margins.makeelement(f"{_W}{side}", {f"{_W}w": "0", f"{_W}type": "dxa"}))
    tbl_pr.append(margins)
    tbl.append(tbl_pr)
    grid = tbl.makeelement(f"{_W}tblGrid", {})
    for width in widths:
        grid.append(grid.makeelement(f"{_W}gridCol", {f"{_W}w": str(width)}))
    tbl.append(grid)
    row = tbl.makeelement(f"{_W}tr", {})
    for width, content in zip(widths, cells):
        cell = row.makeelement(f"{_W}tc", {})
        tc_pr = cell.makeelement(f"{_W}tcPr", {})
        tc_pr.append(tc_pr.makeelement(f"{_W}tcW", {f"{_W}w": str(width), f"{_W}type": "dxa"}))
        cell.append(tc_pr)
        for element in content:
            cell.append(element)
        if not content or content[-1].tag != f"{_W}p":
            # 格子必须以段落结尾
            cell.append(_tiny_paragraph(cell))
        row.append(cell)
    tbl.append(row)
    return tbl


def columns_to_tables(path: Path) -> int:
    """把左右分栏改成无边框表格，返回改了几处。"""
    from docx import Document

    doc = Document(str(path))
    body = doc.element.body
    blocks = _section_blocks(body)
    converted = 0
    index = 0
    while index < len(blocks):
        widths = _columns(blocks[index][1])
        if not widths or _kind(blocks[index][1]) == "nextColumn":
            index += 1
            continue
        group = [blocks[index]]
        while (index + len(group) < len(blocks) and len(group) < len(widths)
               and _kind(blocks[index + len(group)][1]) == "nextColumn"
               and _columns(blocks[index + len(group)][1]) == widths):
            group.append(blocks[index + len(group)])
        index += len(group)
        first = group[0][0][0] if group[0][0] else group[0][2]
        if len(group) < 2 or first is None:
            continue
        cells: list[list] = []
        for elements, _sect, sect_paragraph in group:
            content = list(elements)
            if sect_paragraph is not None and _has_visible_content(sect_paragraph):
                # 这一栏的最后一段带着分节符：内容挪进格子，分节符留在原处一个空段里
                moved = copy.deepcopy(sect_paragraph)
                moved_pr = moved.find(f"{_W}pPr")
                moved_pr.remove(moved_pr.find(f"{_W}sectPr"))
                for child in list(sect_paragraph):
                    if child.tag != f"{_W}pPr":
                        sect_paragraph.remove(child)
                content.append(moved)
            cells.append(content)
        # 先在原位置放个记号：表格要装进的元素可能正是第一个元素
        marker = body.makeelement(f"{_W}p", {})
        first.addprevious(marker)
        marker.addprevious(_layout_table(body, widths[:len(group)], cells))
        body.remove(marker)
        for _elements, sect, sect_paragraph in group:
            _single_column(sect)
            if sect_paragraph is not None:
                _shrink_paragraph(sect_paragraph)
        converted += 1
    if converted:
        doc.save(str(path))
    return converted
