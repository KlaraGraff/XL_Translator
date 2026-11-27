"""看一看 PDF 转 Word 时补回的上划线、分数线落在了哪些字上（反馈 #28 讲义第 48 页）。只测量。

用法（仓库根目录，用软件自己的 Python）：

    python agent/testing/pdf_word_rules_dump.py 原文.pdf 页码 [页码 ...]

只转换、不翻译，列出这些页：
- 原文 PDF 里认出来的每条线：上划线还是分数线、线的位置、线下面（或上面）是哪几个字、前后各 12 个字；
- 转出的 Word 里每个带横线的公式、每个分数，连同前后各 12 个字。
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
from core.pdf_word_rules import _strokes, pdf_rules

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_M = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
_AROUND = 12


def _pdf_side(pdf: Path, page_number: int) -> None:
    import pymupdf

    page = pymupdf.open(str(pdf))[page_number - 1]
    text, rules = pdf_rules(page)
    print(f"  原文第 {page_number} 页：短细横线 {len(_strokes(page))} 条，认出 {len(rules)} 条")
    for indices, mark, fraction in rules:
        first, last = indices[0], indices[-1]
        covered = "".join(text[i] for i in indices)
        kind = "分数线" if fraction is not None else "上划线"
        print(f"    {kind} {mark:3}  …{text[max(0, first - _AROUND):first]}【{covered}】{text[last + 1:last + 1 + _AROUND]}…")
        if fraction is not None:
            numerator, denominator, height = fraction
            print(f"        分子【{''.join(text[i] for i in numerator)}】 分母【{''.join(text[i] for i in denominator)}】 高 {height}")


def _word_text(node) -> str:
    return "".join(t.text or "" for t in node.iter(f"{_W}t", f"{_M}t"))


def _word_side(docx: Path, page_number: int) -> None:
    groups = page_groups(Document(str(docx)).element.body)
    if page_number > len(groups):
        print(f"  Word 只有 {len(groups)} 页")
        return
    print(f"  转出的 Word 第 {page_number} 页：")
    for element in groups[page_number - 1]:
        for paragraph in ([element] if element.tag == f"{_W}p" else element.iter(f"{_W}p")):
            for math in paragraph.iter(f"{_M}oMath"):
                before = "".join(_word_text(node) for node in reversed(list(math.itersiblings(preceding=True))))
                after = "".join(_word_text(node) for node in math.itersiblings())
                kind = "分数" if math.find(f"{_M}f") is not None else "横线"
                print(f"    {kind}  …{before[-_AROUND:]}【{_word_text(math)}】{after[:_AROUND]}…")


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        raise SystemExit(2)
    pdf = Path(sys.argv[1])
    pages = [int(value) for value in sys.argv[2:]]
    with tempfile.TemporaryDirectory() as folder:
        docx = Path(folder) / "converted.docx"
        route.convert_pdf_to_docx(pdf, docx)
        for page_number in pages:
            _pdf_side(pdf, page_number)
            _word_side(docx, page_number)


if __name__ == "__main__":
    main()
