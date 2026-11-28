"""PDF 转 Word 后，框里一行一张表、行高从框顶一路累加的，改回每行自己的高度（反馈 #28 讲义第 68 页）。

讲义第 68 页的例题框（一整块浅红底）：pdf2docx 把框里每一行字做成一张一行的表，中间隔着 1 磅的空段。
第一张表的行高正常，后面每一张的行高和段前距却都是从第一张表的顶上量起的：432、922、1412、1684、
1954…… 1/20 磅一路累加，段前距也跟着累加（字压在格子底部）。几行字的框排出来七八百磅高，一页放不下，
缩到最小字号也放不下；以前还因为找页对错了地方，把第 66 页缩到最小，报成「第 66 页放不下」。

认法：几张一行的表挨着（中间只有空段），宽度、缩进一样；后一张的行高比前一张大，段前距也不比前一张
的行高小——字在前一张表的底下面，说明是从同一个顶上量的。改法：行高、段前距都减去前一张原来的行高，
得到这一行自己的高度。前一张不是这样累加的（行高变小了，或者字在前一张的高度之内）就停。
"""
from __future__ import annotations

from itertools import pairwise
from pathlib import Path

from loguru import logger

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

#: 段前距比前一张表的行高少这么多以内（1/20 磅），仍算从同一个顶上量的（取整误差）。
_SLACK = 40


def _number(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _shape(table):
    """一行的表：(行, 行高, 宽度和缩进)；不是一行、没有行高时返回 None。"""
    rows = table.findall(f"{_W}tr")
    if len(rows) != 1:
        return None
    height = rows[0].find(f"{_W}trPr/{_W}trHeight")
    value = _number(height.get(f"{_W}val")) if height is not None else None
    if value is None:
        return None
    ind = table.find(f"{_W}tblPr/{_W}tblInd")
    grid = tuple(column.get(f"{_W}w") for column in table.findall(f"{_W}tblGrid/{_W}gridCol"))
    return rows[0], value, (ind.get(f"{_W}w") if ind is not None else None, grid)


def _firsts(row) -> list:
    """每个格子的第一段。"""
    return [p for p in (tc.find(f"{_W}p") for tc in row.findall(f"{_W}tc")) if p is not None]


def _before(paragraph) -> float:
    spacing = paragraph.find(f"{_W}pPr/{_W}spacing")
    return (_number(spacing.get(f"{_W}before")) or 0.0) if spacing is not None else 0.0


def _spacer(element) -> bool:
    return (element.tag == f"{_W}p" and not "".join(node.text or "" for node in element.iter(f"{_W}t")).strip()
            and next(element.iter(f"{_W}drawing", f"{_W}pict", f"{_W}sectPr"), None) is None)


def _runs(body) -> list[list]:
    """挨着的一行表（中间只隔空段），宽度、缩进一样的一串。"""
    runs, current, key = [], [], None
    for element in body:
        if element.tag == f"{_W}tbl":
            shape = _shape(element)
            if shape is not None and (not current or shape[2] == key):
                current.append(shape)
                key = shape[2]
                continue
            runs.append(current)
            current, key = ([shape], shape[2]) if shape is not None else ([], None)
        elif not _spacer(element):
            runs.append(current)
            current, key = [], None
    runs.append(current)
    return [run for run in runs if len(run) > 1]


def unstack_rows(docx_path: Path, _pdf_path: Path | None = None) -> int:
    """从框顶一路累加的行高、段前距改回每行自己的；返回改了几行（_pdf_path 不用，和别的修补一样的参数）。"""
    from docx import Document

    doc = Document(str(docx_path))
    fixed = 0
    for run in _runs(doc.element.body):
        for (_row, previous, _key), (row, height, _key2) in pairwise(run):
            firsts = _firsts(row)
            if not firsts or height <= previous or min(_before(p) for p in firsts) < previous - _SLACK:
                continue
            row.find(f"{_W}trPr/{_W}trHeight").set(f"{_W}val", str(round(height - previous)))
            for paragraph in firsts:
                spacing = paragraph.find(f"{_W}pPr/{_W}spacing")
                spacing.set(f"{_W}before", str(round(max(0.0, _before(paragraph) - previous))))
            fixed += 1
    if fixed:
        doc.save(str(docx_path))
        logger.info(f"[PDF] {fixed} 行一行一张的表，行高从框顶累加，改回每行自己的高度")
    return fixed
