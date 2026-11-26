"""查 PDF 转 Word 后某几页为什么还挤到下一页（反馈 #28）。只测量，不改软件的行为。

用法（仓库根目录，用软件自己的 Python）：

    python agent/testing/pdf_word_page_budget.py 原文.pdf 页码 [页码 ...]

只转换、不翻译，再用本机 Word 转回 PDF。对每个给的原文页码列出：
- 这一节的页面高度、上下边距、页脚距离和页脚那段的行高、段前段后；
- 这一页在 Word 里按段前、行高、段后、表格行高加起来一共多高，正文区能放多高；
- 最高的几块（段落或表格）和它们的开头几个字；
- 这一页最后三行原文，在 Word 转回的 PDF 里落在第几页、什么位置。
文字只截开头 30 个字。只写到临时文件夹。
"""
from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from docx import Document
from docx.oxml.ns import qn

import core.pdf_word_route as route
from core.pdf_word_fit import page_groups

_TWIP = 20


def _int(element, name: str, default: int = 0) -> int:
    try:
        return int(element.get(qn("w:" + name))) if element is not None and element.get(qn("w:" + name)) else default
    except ValueError:
        return default


def _text(element) -> str:
    return "".join(node.text or "" for node in element.iter(qn("w:t"))).strip()


def _paragraph_height(p) -> tuple[int, str]:
    spacing = p.find(f"{qn('w:pPr')}/{qn('w:spacing')}")
    rule = spacing.get(qn("w:lineRule")) if spacing is not None else None
    line = _int(spacing, "line", 240)
    note = "" if rule == "exact" else f"（行距 {rule or '默认'} {line}，按单倍估）"
    height = line if rule in ("exact", "atLeast") else 240 * line // 240
    return _int(spacing, "before") + height + _int(spacing, "after"), note


def _table_height(tbl) -> tuple[int, str]:
    total = 0
    rules = set()
    for tr in tbl.findall(qn("w:tr")):
        height = tr.find(f"{qn('w:trPr')}/{qn('w:trHeight')}")
        total += _int(height, "val")
        rules.add(height.get(qn("w:hRule")) if height is not None else "无")
    return total, "" if rules == {"exact"} else f"（行高规则 {sorted(map(str, rules))}）"


def _section_of(group):
    for element in reversed(group):
        sect = element.find(f"{qn('w:pPr')}/{qn('w:sectPr')}") if element.tag == qn("w:p") else None
        if sect is not None:
            return sect
    return None


def _lines(pdf: Path) -> list[tuple[int, float, str]]:
    import pymupdf

    out = []
    for index, page in enumerate(pymupdf.open(str(pdf))):
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                text = "".join(span["text"] for span in line["spans"])
                out.append((index + 1, line["bbox"][1], text))
    return out


def _key(text: str) -> str:
    return re.sub(r"\W", "", text).lower()


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    import pymupdf

    pdf = Path(argv[0])
    pages = [int(arg) for arg in argv[1:]]
    with tempfile.TemporaryDirectory(prefix="page_budget_") as tmp:
        docx = Path(tmp) / f"{pdf.stem}.docx"
        route.convert_pdf_to_docx(pdf, docx, target_lang="zh")
        rendered = Path(tmp) / f"{pdf.stem}_word.pdf"
        engine = route.docx_to_pdf(docx, rendered)
        doc = Document(str(docx))
        groups = page_groups(doc.element.body)
        sections = {id(section._sectPr): section for section in doc.sections}
        out_lines = _lines(rendered)
        src = pymupdf.open(str(pdf))
        print(f"原文 {len(src)} 页，Word 转回 {pymupdf.open(str(rendered)).page_count} 页（{engine}）")
        for number in pages:
            print(f"== 原文第 {number} 页")
            group = groups[number - 1]
            sect = _section_of(group)
            if sect is None:
                sect = doc.element.body.find(qn("w:sectPr"))
            size = sect.find(qn("w:pgSz"))
            margins = sect.find(qn("w:pgMar"))
            page_h, top, bottom, footer_d = (_int(size, "h"), _int(margins, "top"), _int(margins, "bottom"),
                                             _int(margins, "footer"))
            footer_h = 0
            section = sections.get(id(sect))
            if section is not None and not section.footer.is_linked_to_previous:
                for child in section.footer._element:
                    if child.tag == qn("w:p"):
                        height, _ = _paragraph_height(child)
                        footer_h += height
                        spacing = child.find(f"{qn('w:pPr')}/{qn('w:spacing')}")
                        print(f"  页脚段「{_text(child)[:30]}」段前 {_int(spacing, 'before')} 行高 {_int(spacing, 'line')} 段后 {_int(spacing, 'after')}")
                    elif child.tag == qn("w:tbl"):
                        footer_h += _table_height(child)[0]
                        print(f"  页脚表格「{_text(child)[:30]}」")
            body_bottom = max(bottom, footer_d + footer_h)
            available = page_h - top - body_bottom
            print(f"  页高 {page_h} 上边距 {top} 下边距 {bottom} 页脚距离 {footer_d} 页脚高 {footer_h}"
                  f" → 正文区 {available}（1/20 磅）")
            blocks = []
            for element in group:
                if element.tag == qn("w:p"):
                    height, note = _paragraph_height(element)
                    kind = "段"
                elif element.tag == qn("w:tbl"):
                    height, note = _table_height(element)
                    kind = "表"
                else:
                    continue
                blocks.append((height, kind, note, _text(element)[:30]))
            total = sum(block[0] for block in blocks)
            print(f"  这一页 {len(blocks)} 块，加起来 {total}，{'超出' if total > available else '还剩'} {abs(available - total)}")
            for height, kind, note, text in sorted(blocks, reverse=True)[:6]:
                print(f"    {kind} 高 {height}{note}「{text}」")
            notes = [block for block in blocks if block[2]]
            if notes:
                print(f"  行高不固定的块 {len(notes)} 个（Word 实际排出来可能更高）：")
                for height, kind, note, text in notes[:8]:
                    print(f"    {kind}{note}「{text}」")
            tail = [line for line in _lines_of_page(src, number) if _key(line)][-3:]
            for text in tail:
                hits = [(page, round(y, 1)) for page, y, out in out_lines if _key(out) and _key(out) == _key(text)]
                print(f"  原文末行「{text[:30]}」→ Word 转回的 PDF：{hits[:3] or '没对上'}")
    return 0


def _lines_of_page(src, number: int) -> list[str]:
    page = src[number - 1]
    return ["".join(span["text"] for span in line["spans"])
            for block in page.get_text("dict")["blocks"] for line in block.get("lines", [])]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
