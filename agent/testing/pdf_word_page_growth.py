"""量一量 PDF 转 Word 为什么多出页来（反馈 #28）。只测量，不改软件的行为。

用法（在仓库根目录，用软件自己的 Python）：

    python agent/testing/pdf_word_page_growth.py 原文.pdf [原文2.pdf ...] [--translated 译文.docx ...]

对每份 PDF 只转换、不翻译，做四种对照，各自用本机的 Word（没有 Word 时用 LibreOffice）
转回 PDF，数页数，并列出哪几页原文占了不止一页：

- 现在：软件现在的做法；
- 不补空格：跳过 restore_word_spaces（LaTeX 的 PDF 里没有空格字，转换时按正常宽度补回）；
- 正文换 Times New Roman：LaTeX 正文字体（LMRoman、CMR 这类，本机多半没装）换成人人都有的字体；
- 正文换 Cambria：同上，换成 Cambria。

--translated 给的是软件翻好的 Word（「同时输出 Word」或任务过程文件夹里的那份）：
数它自己转回 PDF 的页数，再做「正文换字体」两种对照。

只写到临时文件夹，不动原文件。
"""
from __future__ import annotations

import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from docx import Document  # noqa: E402
from docx.oxml.ns import qn  # noqa: E402

import core.pdf_word_route as route  # noqa: E402
from core.pdf_word_fit import _anchors, page_groups, pages_per_group, pdf_page_texts  # noqa: E402

# LaTeX 正文字体（不含数学字体：那些已经换成 Cambria Math）
_TEX_TEXT_FONT = re.compile(r"^(?:[A-Z]{6}\+)?(?:LMRoman|LMSans|CMR\d|CMBX|CMTI|CMSL|CMSS|SFRM|SFBX|SFTI)", re.I)


def _render(docx: Path, out: Path) -> tuple[int, list[int] | None, str]:
    pdf = out.with_suffix(".pdf")
    engine = route.docx_to_pdf(docx, pdf)
    texts = pdf_page_texts(pdf)
    groups = page_groups(Document(str(docx)).element.body)
    per_group = pages_per_group(_anchors(groups), texts)
    over = [index + 1 for index, count in enumerate(per_group) if count > 1] if per_group else None
    return len(texts), over, engine


def _swap_text_font(source: Path, target: Path, font: str) -> int:
    doc = Document(str(source))
    count = 0
    for fonts in doc.element.iter(qn("w:rFonts")):
        for key in ("w:ascii", "w:hAnsi", "w:cs"):
            name = fonts.get(qn(key))
            if name and _TEX_TEXT_FONT.match(name):
                fonts.set(qn(key), font)
                count += 1
    doc.save(str(target))
    return count


def _report(label: str, result: tuple[int, list[int] | None, str]) -> None:
    pages, over, engine = result
    shown = "对不上位置" if over is None else ("无" if not over else "、".join(map(str, over)))
    print(f"  {label:<22} {pages:>4} 页   占了不止一页的原文页：{shown}   （{engine}）")


def measure_pdf(pdf: Path, work: Path) -> None:
    import pymupdf

    print(f"== {pdf.name}：原文 {pymupdf.open(str(pdf)).page_count} 页（只转换、不翻译）")
    current = work / f"{pdf.stem}_现在.docx"
    route.convert_pdf_to_docx(pdf, current, target_lang="zh")
    _report("现在", _render(current, current))

    original = route.restore_word_spaces
    route.restore_word_spaces = lambda path, words=None: 0
    try:
        no_spaces = work / f"{pdf.stem}_不补空格.docx"
        route.convert_pdf_to_docx(pdf, no_spaces, target_lang="zh")
    finally:
        route.restore_word_spaces = original
    _report("不补空格", _render(no_spaces, no_spaces))

    for font in ("Times New Roman", "Cambria"):
        swapped = work / f"{pdf.stem}_{font.replace(' ', '')}.docx"
        changed = _swap_text_font(current, swapped, font)
        _report(f"正文换 {font}（{changed} 处）", _render(swapped, swapped))


def measure_translated(docx: Path, work: Path) -> None:
    print(f"== {docx.name}（译文）")
    copy = work / f"{docx.stem}_译文.docx"
    shutil.copy2(docx, copy)
    _report("译文现在", _render(copy, copy))
    for font in ("Times New Roman", "Cambria"):
        swapped = work / f"{docx.stem}_译文_{font.replace(' ', '')}.docx"
        changed = _swap_text_font(copy, swapped, font)
        _report(f"正文换 {font}（{changed} 处）", _render(swapped, swapped))


def main(argv: list[str]) -> int:
    pdfs: list[Path] = []
    translated: list[Path] = []
    target = pdfs
    for arg in argv:
        if arg == "--translated":
            target = translated
            continue
        target.append(Path(arg))
    if not pdfs and not translated:
        print(__doc__)
        return 2
    with tempfile.TemporaryDirectory(prefix="page_growth_") as tmp:
        work = Path(tmp)
        for pdf in pdfs:
            measure_pdf(pdf, work)
        for docx in translated:
            measure_translated(docx, work)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
