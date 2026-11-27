"""看一看 PDF 转 Word 后某一页图上的标签是怎么搭的，底色从哪来（反馈 #28 讲义第 48 页图 13–15）。只测量。

用法（仓库根目录，用软件自己的 Python）：

    python agent/testing/pdf_word_figures_dump.py 原文.pdf 页码 [页码 ...]

只转换、不翻译（这一步先不去底色），然后每一页列出：
- 每张图片：浮动还是嵌在字里、是否在文字下方（behindDoc）、相对什么定位、位置和大小（磅）、
  在不在表格里；在文字下方、按页面定位的，再列出原 PDF 上落在图里的字；
- 每张表：套在哪张表里、每格的字和底色（tcPr/shd）、格子里的字对上了原 PDF 的几个字、是不是都压在
  图片上，以及「去底色」处理哪几行；
- 这一页别的会涂底色的地方：段落底色（pPr/shd）、字的底色（rPr/shd、highlight）、带填充的文本框和形状。
只写到临时文件夹。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from docx import Document

import core.pdf_word_figures as figures
import core.pdf_word_route as route
from core.pdf_word_fit import page_groups
from core.pdf_word_shading import _compact

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_WP = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_WPS = "{http://schemas.microsoft.com/office/word/2010/wordprocessingShape}"
_V = "{urn:schemas-microsoft-com:vml}"
_EMU_PER_POINT = 12700


def _points(value) -> str:
    try:
        return f"{int(value) / _EMU_PER_POINT:.1f}"
    except (TypeError, ValueError):
        return "-"


def _inside_table(node) -> str:
    depth = 0
    parent = node.getparent()
    while parent is not None:
        if parent.tag == f"{_W}tbl":
            depth += 1
        parent = parent.getparent()
    return f"在第 {depth} 层表格里" if depth else "不在表格里"


def _pictures(group: list) -> None:
    number = 0
    for element in group:
        for drawing in element.iter(f"{_W}drawing"):
            number += 1
            anchor = drawing.find(f"{_WP}anchor")
            extent = drawing.find(f".//{_WP}extent")
            size = (f"{_points(extent.get('cx'))}×{_points(extent.get('cy'))}" if extent is not None else "-")
            if anchor is None:
                print(f"    图 {number}：嵌在字里，大小 {size} 磅，{_inside_table(drawing)}")
                continue
            where = []
            for axis in ("positionH", "positionV"):
                position = anchor.find(f"{_WP}{axis}")
                offset = position.find(f"{_WP}posOffset") if position is not None else None
                relative = position.get("relativeFrom") if position is not None else "-"
                where.append(f"{axis}={relative}:{_points(offset.text) if offset is not None else '-'}")
            print(f"    图 {number}：浮动，大小 {size} 磅，behindDoc={anchor.get('behindDoc')} "
                  f"layoutInCell={anchor.get('layoutInCell')} {' '.join(where)}，{_inside_table(drawing)}")


def _fill(node) -> str:
    shd = node.find(f"{_W}shd") if node is not None else None
    if shd is None:
        return "无"
    return f"{shd.get(f'{_W}val')}/{shd.get(f'{_W}fill')}"


def _tables(group: list, inside: list[str], glyphs: dict, pictures) -> None:
    tables = [table for element in group
              for table in ([element] if element.tag == f"{_W}tbl" else element.iter(f"{_W}tbl"))]
    for number, table in enumerate(tables, 1):
        chosen = figures._rows_on_pictures(table, inside, glyphs, pictures)
        rows = table.findall(f"{_W}tr")
        print(f"    表 {number}：{len(rows)} 行，{_inside_table(table)}；去底色的行：{[rows.index(row) + 1 for row in chosen]}")
        for row_number, row in enumerate(rows, 1):
            for tc in row.findall(f"{_W}tc"):
                text = _compact(figures._cell_text(tc))
                boxes = glyphs.get(tc, [])
                where = (f"对上 {len(boxes)} 个字，{'都压在图片上' if figures._on_picture(boxes, pictures) else '不全在图片上'}"
                         if boxes else "没对上")
                print(f"      行 {row_number} 格 底色 {_fill(tc.find(f'{_W}tcPr'))}「{text[:30]}」{where if text else ''}")


def _other_fills(group: list) -> None:
    found = []
    for element in group:
        for shd in element.iter(f"{_W}shd"):
            parent = shd.getparent()
            if parent is not None and parent.tag != f"{_W}tcPr":
                owner = parent.getparent()
                text = "".join(node.text or "" for node in owner.iter(f"{_W}t")) if owner is not None else ""
                found.append(f"{parent.tag.split('}')[1]}/shd {shd.get(f'{_W}fill')}「{_compact(text)[:20]}」")
        for highlight in element.iter(f"{_W}highlight"):
            found.append(f"highlight {highlight.get(f'{_W}val')}")
        for shape in element.iter(f"{_WPS}wsp"):
            fill = shape.find(f".//{_A}solidFill")
            color = fill[0].get("val") if fill is not None and len(fill) else "无"
            text = "".join(node.text or "" for node in shape.iter(f"{_W}t"))
            found.append(f"形状/文本框 填充 {color}「{_compact(text)[:20]}」")
        for shape in element.iter(f"{_V}shape", f"{_V}rect", f"{_V}textbox"):
            found.append(f"VML {shape.tag.split('}')[1]} fillcolor={shape.get('fillcolor')}")
    print(f"    别的底色：{'；'.join(found) if found else '没有'}")


def main() -> None:
    args = sys.argv[1:]
    if len(args) < 2:
        print(__doc__)
        raise SystemExit(2)
    pdf = Path(args[0])
    pages = [int(arg) for arg in args[1:]]
    from core.pdf_component import require_fitz

    fitz = require_fitz()
    original = route.uncover_pictures
    with tempfile.TemporaryDirectory() as folder:
        docx = Path(folder) / "converted.docx"
        route.uncover_pictures = lambda _docx, _pdf: 0
        try:
            route.convert_pdf_to_docx(pdf, docx)
        finally:
            route.uncover_pictures = original
        groups = page_groups(Document(str(docx)).element.body)
        with fitz.open(pdf) as document:
            print(f"Word {len(groups)} 页，PDF {len(document)} 页")
            for page_number in pages:
                group = groups[page_number - 1]
                print(f"== 第 {page_number} 页")
                _pictures(group)
                behind = figures._pictures_behind(group, fitz)
                inside = figures._text_inside(document[page_number - 1], behind) if behind else []
                for rect, words in zip(behind, inside, strict=True):
                    print(f"    在文字下方的图 {tuple(round(v, 1) for v in rect)} 里的字：「{words[:60]}」")
                _tables(group, inside, figures._glyphs(group, document[page_number - 1]) if behind else {}, behind)
                _other_fills(group)


if __name__ == "__main__":
    main()
