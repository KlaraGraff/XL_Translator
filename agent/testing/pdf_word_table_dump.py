"""看一看 PDF 转 Word 后某一页的表格是怎么搭的（反馈 #28 讲义译文第 33、66 页放不下）。只测量。

用法（仓库根目录，用软件自己的 Python）：

    python agent/testing/pdf_word_table_dump.py 原文.pdf 页码 [--docx 译文.docx] [--grep 文字]

先只转换原文（不翻译），列出这一页每张表；给了 --docx 时再列出那份 Word 同一页的表，
两边对照。每张表列出：每行的行高和规则；每个格子的宽度、纵向合并、横向合并；格子里每段的
段前段后、行距、对齐、缩进、换行数和开头 30 个字。--grep 只列含这段字的表。
只写到临时文件夹。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from docx import Document

import core.pdf_word_route as route
from core.pdf_word_fit import page_groups

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_M = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"


def _text(node) -> str:
    return "".join(t.text or "" for t in node.iter(f"{_W}t", f"{_M}t"))


def _attrs(node, *names: str) -> str:
    if node is None:
        return "-"
    return ",".join(f"{name}={node.get(f'{_W}{name}')}" for name in names if node.get(f"{_W}{name}") is not None)


def _paragraph(p) -> str:
    p_pr = p.find(f"{_W}pPr")
    spacing = p_pr.find(f"{_W}spacing") if p_pr is not None else None
    jc = p_pr.find(f"{_W}jc") if p_pr is not None else None
    ind = p_pr.find(f"{_W}ind") if p_pr is not None else None
    breaks = len(p.findall(f".//{_W}br"))
    sizes = sorted({size.get(f"{_W}val") for size in p.iter(f"{_W}sz")})
    return (f"[{_attrs(spacing, 'before', 'after', 'line', 'lineRule')}] jc={jc.get(f'{_W}val') if jc is not None else '-'} "
            f"ind[{_attrs(ind, 'left', 'right', 'firstLine', 'hanging')}] br={breaks} sz={'/'.join(sizes)} "
            f"「{_text(p)[:30]}」")


def _dump(docx: Path, page_number: int, needle: str, label: str) -> None:
    groups = page_groups(Document(str(docx)).element.body)
    print(f"== {label}：第 {page_number} 页（共 {len(groups)} 页）")
    if page_number > len(groups):
        return
    tables = [table for element in groups[page_number - 1]
              for table in ([element] if element.tag == f"{_W}tbl" else element.iter(f"{_W}tbl"))]
    for number, table in enumerate(tables, 1):
        if needle and needle not in _text(table):
            continue
        layout = table.find(f"{_W}tblPr/{_W}tblLayout")
        grid = [col.get(f"{_W}w") for col in table.findall(f"{_W}tblGrid/{_W}gridCol")]
        print(f"  表 {number}：layout={layout.get(f'{_W}type') if layout is not None else '-'} 列宽={grid}")
        for row_number, tr in enumerate(table.findall(f"{_W}tr"), 1):
            height = tr.find(f"{_W}trPr/{_W}trHeight")
            cant = tr.find(f"{_W}trPr/{_W}cantSplit") is not None
            print(f"    行 {row_number}：高[{_attrs(height, 'val', 'hRule')}]{' 不跨页' if cant else ''}")
            for cell_number, tc in enumerate(tr.findall(f"{_W}tc"), 1):
                tc_pr = tc.find(f"{_W}tcPr")
                width = tc_pr.find(f"{_W}tcW") if tc_pr is not None else None
                merge = tc_pr.find(f"{_W}vMerge") if tc_pr is not None else None
                span = tc_pr.find(f"{_W}gridSpan") if tc_pr is not None else None
                print(f"      格 {cell_number}：宽={width.get(f'{_W}w') if width is not None else '-'}"
                      f" 纵向合并={merge.get(f'{_W}val', 'continue') if merge is not None else '-'}"
                      f" 跨列={span.get(f'{_W}val') if span is not None else '-'}")
                for p in tc.findall(f"{_W}p"):
                    print(f"        段 {_paragraph(p)}")


def main() -> None:
    args = sys.argv[1:]
    translated = needle = ""
    if "--docx" in args:
        index = args.index("--docx")
        translated = args[index + 1]
        del args[index:index + 2]
    if "--grep" in args:
        index = args.index("--grep")
        needle = args[index + 1]
        del args[index:index + 2]
    if len(args) != 2:
        print(__doc__)
        raise SystemExit(2)
    pdf, page_number = Path(args[0]), int(args[1])
    with tempfile.TemporaryDirectory() as folder:
        docx = Path(folder) / "converted.docx"
        route.convert_pdf_to_docx(pdf, docx)
        _dump(docx, page_number, needle, "只转换")
    if translated:
        _dump(Path(translated), page_number, needle, "译文")


if __name__ == "__main__":
    main()
