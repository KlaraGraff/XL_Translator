"""看一看某一页缩小字号（放不下的页逐页缩字）前后有什么变化（反馈 #28 讲义图 28 的箭头在译文里丢了）。

用法（仓库根目录，用软件自己的 Python）：

    python agent/testing/pdf_word_shrink_page.py 原文.pdf 页码 输出文件夹 [--ratio 0.73]

只转换、不翻译，然后：
- 列出这一页里每张图片：浮动还是嵌在字里、在第几张表第几行第几格、是否限定在格子里
  （layoutInCell）、是否在文字下方（behindDoc）、位置偏移和大小（1/20 磅）；
- 把这一页按 --ratio（默认 0.73，三轮缩字后的比例）缩一次，和翻译时放不下的页一样；
- 输出文件夹里写 缩字前.docx/.pdf、缩字后.docx/.pdf（用 Word 转 PDF），对照看图在不在。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from docx import Document

import core.pdf_word_route as route
from core.pdf_word_fit import _default_size, page_groups, shrink_group

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_WP = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
_EMU_PER_TWIP = 635


def _twips(value) -> str:
    try:
        return str(round(int(value) / _EMU_PER_TWIP))
    except (TypeError, ValueError):
        return "-"


def _where(drawing) -> str:
    cell = row = table = None
    node = drawing.getparent()
    while node is not None:
        if node.tag == f"{_W}tc" and cell is None:
            cell = node
        elif node.tag == f"{_W}tr" and row is None:
            row = node
        elif node.tag == f"{_W}tbl" and table is None:
            table = node
        node = node.getparent()
    if table is None:
        return "不在表格里"
    rows = table.findall(f"{_W}tr")
    height = row.find(f"{_W}trPr/{_W}trHeight") if row is not None else None
    rule = f"{height.get(f'{_W}val')}/{height.get(f'{_W}hRule')}" if height is not None else "-"
    cells = row.findall(f"{_W}tc") if row is not None else []
    return (f"表格第 {rows.index(row) + 1 if row in rows else '?'} 行（行高 {rule}）"
            f"第 {cells.index(cell) + 1 if cell in cells else '?'} 格")


def _pictures(group: list) -> None:
    found = 0
    for element in group:
        for drawing in element.iter(f"{_W}drawing"):
            found += 1
            anchor = drawing.find(f"{_WP}anchor")
            extent = drawing.find(f".//{_WP}extent")
            size = f"{_twips(extent.get('cx'))}×{_twips(extent.get('cy'))}" if extent is not None else "-"
            if anchor is None:
                print(f"    图 {found}：嵌在字里，大小 {size}，{_where(drawing)}")
                continue
            offsets = []
            for axis in ("positionH", "positionV"):
                position = anchor.find(f"{_WP}{axis}")
                offset = position.find(f"{_WP}posOffset") if position is not None else None
                relative = position.get("relativeFrom") if position is not None else "-"
                offsets.append(f"{axis}={relative}:{_twips(offset.text) if offset is not None else '-'}")
            print(f"    图 {found}：浮动，大小 {size}，{_where(drawing)}，layoutInCell={anchor.get('layoutInCell')}，"
                  f"behindDoc={anchor.get('behindDoc')}，{' '.join(offsets)}")
    if not found:
        print("    这一页没有图片")


def main() -> None:
    args = sys.argv[1:]
    ratio = 0.73
    if "--ratio" in args:
        index = args.index("--ratio")
        ratio = float(args[index + 1])
        del args[index:index + 2]
    if len(args) != 3:
        print(__doc__)
        raise SystemExit(2)
    pdf, page_number, out = Path(args[0]), int(args[1]), Path(args[2])
    out.mkdir(parents=True, exist_ok=True)
    before = out / "缩字前.docx"
    after = out / "缩字后.docx"
    route.convert_pdf_to_docx(pdf, before)
    doc = Document(str(before))
    groups = page_groups(doc.element.body)
    print(f"第 {page_number} 页（共 {len(groups)} 页）的图片：")
    _pictures(groups[page_number - 1])
    shrink_group(groups[page_number - 1], ratio, _default_size(doc))
    doc.save(str(after))
    for docx in (before, after):
        route.docx_to_pdf(docx, docx.with_suffix(".pdf"))
    print(f"已写出：{before}、{after} 和对应的 PDF（看第 {page_number} 页）")


if __name__ == "__main__":
    main()
