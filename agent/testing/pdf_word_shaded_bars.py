"""看一看 PDF 转 Word 后带底色的标题条为什么比原文高（反馈 #28 第 101、105 页）。只测量。

用法（仓库根目录，用软件自己的 Python）：

    python agent/testing/pdf_word_shaded_bars.py 原文.pdf 页码 [页码 ...]

只转换、不翻译，列出这些页：
- 原文 PDF 里填了颜色的矩形（位置、高度、颜色），以及落在矩形里的第一行字；
- 转出的 Word 里这一页带底色的表格：每行的行高、格子的底色、格子里每段的段前段后、行高和开头几个字。
文字只截开头 30 个字。只写到临时文件夹。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from docx import Document
from docx.oxml.ns import qn

import core.pdf_word_route as route
from core.pdf_word_fit import page_groups


def _pdf_fills(pdf: Path, page_number: int) -> None:
    import pymupdf

    page = pymupdf.open(str(pdf))[page_number - 1]
    lines = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            text = "".join(span["text"] for span in line["spans"]).strip()
            if text:
                lines.append((pymupdf.Rect(line["bbox"]), text))
    print(f"  原文 PDF 第 {page_number} 页，填色矩形（高 ≥ 8 磅）：")
    for drawing in page.get_drawings():
        fill = drawing.get("fill")
        rect = drawing.get("rect")
        if not fill or rect is None or rect.height < 8 or rect.width < 40:
            continue
        inside = [(r, t) for r, t in lines if rect.contains(pymupdf.Point((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2))]
        first = f"第一行字 y {inside[0][0].y0:.1f}–{inside[0][0].y1:.1f}「{inside[0][1][:30]}」" if inside else "里面没有字"
        color = "#" + "".join(f"{round(c * 255):02X}" for c in fill[:3])
        print(f"    y {rect.y0:6.1f}–{rect.y1:6.1f}（高 {rect.height:5.1f}）x {rect.x0:5.1f}–{rect.x1:5.1f} {color}  {first}")


def _attrs(element, names) -> str:
    if element is None:
        return "-"
    return " ".join(f"{name}={element.get(qn('w:' + name))}" for name in names if element.get(qn("w:" + name)) is not None)


def _docx_tables(docx: Path, page_number: int) -> None:
    groups = page_groups(Document(str(docx)).element.body)
    if page_number > len(groups):
        print(f"  Word 只有 {len(groups)} 组，没有第 {page_number} 页")
        return
    print(f"  Word 里第 {page_number} 页带底色的表格：")
    for element in groups[page_number - 1]:
        if element.tag != qn("w:tbl"):
            continue
        shaded = [tc for tc in element.iter(qn("w:tc")) if tc.find(f"{qn('w:tcPr')}/{qn('w:shd')}") is not None]
        if not shaded:
            continue
        for row_index, tr in enumerate(element.findall(qn("w:tr"))):
            height = tr.find(f"{qn('w:trPr')}/{qn('w:trHeight')}")
            print(f"    行 {row_index + 1}：行高 {_attrs(height, ('val', 'hRule'))}")
            for tc in tr.findall(qn("w:tc")):
                shd = tc.find(f"{qn('w:tcPr')}/{qn('w:shd')}")
                margins = tc.find(f"{qn('w:tcPr')}/{qn('w:tcMar')}")
                print(f"      格子 底色 {_attrs(shd, ('fill',))} 边距 {[(m.tag.split('}')[1], m.get(qn('w:w'))) for m in margins] if margins is not None else '-'}")
                for p in tc.findall(qn("w:p")):
                    text = "".join(node.text or "" for node in p.iter(qn("w:t"))).strip()
                    spacing = p.find(f"{qn('w:pPr')}/{qn('w:spacing')}")
                    print(f"        段 {_attrs(spacing, ('before', 'after', 'line', 'lineRule'))}「{text[:30]}」")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    pdf = Path(argv[0])
    pages = [int(arg) for arg in argv[1:]]
    with tempfile.TemporaryDirectory(prefix="shaded_bars_") as tmp:
        docx = Path(tmp) / f"{pdf.stem}.docx"
        route.convert_pdf_to_docx(pdf, docx, target_lang="zh")
        for page in pages:
            print(f"== 第 {page} 页")
            _pdf_fills(pdf, page)
            _docx_tables(docx, page)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
