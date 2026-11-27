"""看一看「一行一行摆的表格按原文一行一段重排」在某一页为什么做了、为什么没做（反馈 #28 讲义第 33 页）。只测量。

用法（仓库根目录，用软件自己的 Python）：

    python agent/testing/pdf_word_lines_dump.py 原文.pdf 页码 [页码 ...]

只转换、不翻译（这一步先不重排），然后每一页：
- 每张表、每个格子：是不是交错（后一段的行在前一段上面）、有没有硬换行（下一行的第一个词放得进上一行，
  原文却换了行）；
- 格子里每一段：几个字没和 PDF 对上，每一行在 PDF 上的上沿、左右、第一个词多宽，从这段的第几个字起；
- 再真的重排一次，列出重排了的格子里现在每一段的段前距、缩进和文字。
只写到临时文件夹。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from docx import Document

import core.pdf_word_lines as lines_module
import core.pdf_word_route as route
from core.pdf_word_fit import page_groups

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _tables(group: list) -> list:
    return [table for element in group
            for table in ([element] if element.tag == f"{_W}tbl" else element.iter(f"{_W}tbl"))]


def _cells(group: list, page) -> list[list[tuple]]:
    """每张表：[(格子, [(段, where, 起点, 行)])]，和 core.pdf_word_lines._restore_page 一样对齐。"""
    chars = lines_module._page_chars(page)
    page_text = "".join(char[0] for char in chars)
    paragraphs = [p for element in group
                  for p in ([element] if element.tag == f"{_W}p" else element.iter(f"{_W}p"))]
    texts = [lines_module._positions(p) for p in paragraphs]
    offsets, offset = {}, 0
    for paragraph, (text, where) in zip(paragraphs, texts, strict=True):
        offsets[paragraph] = (offset, where)
        offset += len(text)
    mapping = lines_module._full_alignment(page_text, "".join(text for text, _where in texts))
    reverse = {value: key for key, value in mapping.items()}
    found = []
    for table in _tables(group):
        cells = []
        for tc in table.iter(f"{_W}tc"):
            located = []
            for paragraph in tc.findall(f"{_W}p"):
                start, where = offsets.get(paragraph, (0, []))
                if where:
                    located.append((paragraph, where, start,
                                    lines_module._lines_of(paragraph, where, start, reverse, chars)))
            cells.append((tc, located, reverse))
        found.append(cells)
    return found


def _show_before(page_number: int, tables: list) -> None:
    for table_number, cells in enumerate(tables, 1):
        print(f"  表 {table_number}")
        for cell_number, (_tc, located, reverse) in enumerate(cells, 1):
            if not located:
                continue
            print(f"    格 {cell_number}：交错={lines_module._interleaved(located)} "
                  f"硬换行={lines_module._hard_break(located)}")
            for _paragraph, where, start, lines in located:
                text = "".join(node.text[index] for node, index in where)
                missing = sum(1 for number in range(len(where)) if reverse.get(start + number) is None)
                print(f"      段「{text[:40]}」 {missing}/{len(where)} 个字没对上")
                for begin, line in lines:
                    print(f"        第 {begin:3} 字起 上沿 {line.top:6.1f} 左 {line.left:6.1f} 右 {line.right:6.1f} "
                          f"第一个词宽 {line.word:5.1f}「{text[begin:begin + 14]}」")


def _show_after(docx: Path, page_number: int) -> None:
    group = page_groups(Document(str(docx)).element.body)[page_number - 1]
    for table_number, table in enumerate(_tables(group), 1):
        for cell_number, tc in enumerate(table.iter(f"{_W}tc"), 1):
            rows = []
            for p in tc.findall(f"{_W}p"):
                spacing = p.find(f"{_W}pPr/{_W}spacing")
                ind = p.find(f"{_W}pPr/{_W}ind")
                text = "".join(node.text or "" for node in p.iter(f"{_W}t"))
                rows.append(f"        段前 {spacing.get(f'{_W}before') if spacing is not None else '-'} "
                            f"行高 {spacing.get(f'{_W}line') if spacing is not None else '-'} "
                            f"左缩进 {ind.get(f'{_W}left') if ind is not None else '-'}「{text[:40]}」")
            print(f"    表 {table_number} 格 {cell_number}：")
            print("\n".join(rows))


def main() -> None:
    args = sys.argv[1:]
    if len(args) < 2:
        print(__doc__)
        raise SystemExit(2)
    pdf = Path(args[0])
    pages = [int(arg) for arg in args[1:]]
    import pymupdf

    original = route.restore_line_order
    with tempfile.TemporaryDirectory() as folder:
        docx = Path(folder) / "converted.docx"
        route.restore_line_order = lambda _docx, _pdf: 0
        try:
            route.convert_pdf_to_docx(pdf, docx)
        finally:
            route.restore_line_order = original
        groups = page_groups(Document(str(docx)).element.body)
        with pymupdf.open(str(pdf)) as document:
            print(f"Word {len(groups)} 页，PDF {len(document)} 页")
            for page_number in pages:
                print(f"== 第 {page_number} 页（重排前）")
                _show_before(page_number, _cells(groups[page_number - 1], document[page_number - 1]))
        changed = original(docx, pdf)
        print(f"== 重排了 {changed} 个格子；各页现在的样子：")
        for page_number in pages:
            print(f"  第 {page_number} 页")
            _show_after(docx, page_number)


if __name__ == "__main__":
    main()
