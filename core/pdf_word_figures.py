"""PDF 转 Word 后，摆在图上的标签表格不再用底色把图盖住（反馈 #28 讲义第 48 页图 13–15）。

讲义的维恩图：pdf2docx 把图（圆、阴影）做成一张摆在固定位置、在文字下方（behindDoc）的
图片，图上的标签 A、B 放进一张表格的格子里，格子又照着图上那一块的颜色涂了底色（阴影处
灰色、别处白色）。格子在文字这一层，图片在它下面：灰格子把两个圆整个盖住，只剩一块灰色
（图 13）；白格子在标签下面盖出一小块白底（图 14、15）。图片里本来就有这些颜色。

做法：找出这一页在文字下方、按页面定位的图片占的地方，以及原 PDF 上落在图里的那几个字。
一张表里按行看：一行里每个有字的格子，字都在图里那几个字里，这一行就是摆在图上的标签：
这一行格子的底色都去掉，让下面的图露出来；夹在两行标签中间、一个字也没有的行也在图上，一样去掉。
图注那一行（字不在图里）、正文表格（有一格的字不在图里）不动。讲义第 48 页图 14、15 的标签和
图注在同一张表里，以前按整张表看，有图注就整张不动，白底留着。
PDF 打不开、没有 PyMuPDF 时什么都不改。
"""
from __future__ import annotations

from pathlib import Path

from loguru import logger

from core.pdf_word_fit import page_groups
from core.pdf_word_shading import _compact

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_WP = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
_EMU_PER_POINT = 12700


def _offset(anchor, axis: str) -> float | None:
    position = anchor.find(f"{_WP}{axis}")
    if position is None or position.get("relativeFrom") != "page":
        return None
    offset = position.find(f"{_WP}posOffset")
    try:
        return int(offset.text) / _EMU_PER_POINT if offset is not None and offset.text else None
    except ValueError:
        return None


def _pictures_behind(group: list, fitz) -> list:
    """这一页在文字下方、按页面定位的图片占的地方（PDF 坐标，磅）。"""
    rects = []
    for element in group:
        for anchor in element.iter(f"{_WP}anchor"):
            if anchor.get("behindDoc") not in ("1", "true"):
                continue
            left, top = _offset(anchor, "positionH"), _offset(anchor, "positionV")
            extent = anchor.find(f"{_WP}extent")
            if left is None or top is None or extent is None:
                continue
            try:
                width = int(extent.get("cx") or 0) / _EMU_PER_POINT
                height = int(extent.get("cy") or 0) / _EMU_PER_POINT
            except ValueError:
                continue
            rects.append(fitz.Rect(left, top, left + width, top + height))
    return rects


def _cell_text(tc) -> str:
    return "".join(node.text or "" for node in tc.iter(f"{_W}t"))


def _text_inside(page, pictures) -> list[str]:
    """每张图片里面那些字（去掉空白，按 PDF 里的先后连成一串）。"""
    found = ["" for _picture in pictures]
    for block in page.get_text("rawdict").get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                for char in span.get("chars", []):
                    x0, y0, x1, y1 = char["bbox"]
                    middle = ((x0 + x1) / 2, (y0 + y1) / 2)
                    for number, picture in enumerate(pictures):
                        if picture.x0 <= middle[0] <= picture.x1 and picture.y0 <= middle[1] <= picture.y1:
                            found[number] += _compact(char.get("c", ""))
    return found


def _rows_on_pictures(table, inside: list[str]) -> list:
    """这张表里摆在图上的那几行：有字的格子都是图里的字（图上的标签）的行，以及夹在这样两行
    中间、一个字也没有的行（标签上下之间，也在图上）。图下面的图注那一行（字不在图里）不算。
    只看这张表自己的行，套在里面的表单独算。"""
    rows = table.findall(f"{_W}tr")
    kinds = []
    for row in rows:
        texts = [_compact(_cell_text(tc)) for tc in row.findall(f"{_W}tc")]
        texts = [text for text in texts if text]
        if not texts:
            kinds.append(None)
        else:
            kinds.append(all(any(text in words for words in inside) for text in texts))
    labels = [number for number, kind in enumerate(kinds) if kind]
    if not labels:
        return []
    return [row for number, row in enumerate(rows)
            if kinds[number] or (kinds[number] is None and labels[0] < number < labels[-1])]


def _table_of(node):
    parent = node.getparent()
    while parent is not None and parent.tag != f"{_W}tbl":
        parent = parent.getparent()
    return parent


def _clear_fills(table) -> int:
    """去掉这张表（或这一行）里格子的底色，套在里面的表不算。"""
    cleared = 0
    owner = table if table.tag == f"{_W}tbl" else table.getparent()
    for shd in list(table.iter(f"{_W}shd")):
        parent = shd.getparent()
        if parent is None or parent.tag != f"{_W}tcPr" or _table_of(parent) is not owner:
            continue
        fill = (shd.get(f"{_W}fill") or "auto").lower()
        if fill in ("auto", "none") and shd.get(f"{_W}val") in (None, "clear", "nil"):
            continue
        parent.remove(shd)
        cleared += 1
    return cleared


def uncover_pictures(docx_path: Path, pdf_path: Path) -> int:
    """摆在图上的标签表格去掉格子底色，返回去掉了几个格子的底色。"""
    try:
        from core.pdf_component import require_fitz

        fitz = require_fitz()
    except Exception:  # noqa: BLE001 - no PyMuPDF: leave the colours as converted
        return 0
    from docx import Document

    doc = Document(str(docx_path))
    groups = page_groups(doc.element.body)
    cleared = 0
    try:
        pdf = fitz.open(pdf_path)
    except Exception as exc:  # noqa: BLE001 - unreadable PDF: nothing to compare with
        logger.info(f"[PDF] 打不开原 PDF 对照图上的标签：{exc!r}")
        return 0
    with pdf:
        if len(groups) != len(pdf):
            return 0
        for page_index, group in enumerate(groups):
            pictures = _pictures_behind(group, fitz)
            if not pictures:
                continue
            inside = _text_inside(pdf[page_index], pictures)
            tables = [table for element in group
                      for table in ([element] if element.tag == f"{_W}tbl" else element.iter(f"{_W}tbl"))]
            for table in tables:
                cleared += sum(_clear_fills(row) for row in _rows_on_pictures(table, inside))
    if cleared:
        doc.save(str(docx_path))
        logger.info(f"[PDF] 摆在图上的标签表格去掉了 {cleared} 个格子的底色，下面的图露出来")
    return cleared
