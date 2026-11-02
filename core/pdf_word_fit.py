"""转 Word 翻译后，一页原文仍排成一页译文：放不下的页缩小字号。

pdf2docx 每页原文开一个「新页」分节（core.pdf_word_guard 同样按此分页）。译文比原文
长时，一页排不下就溢出到下一页：maple 2026-11-02 的签证审核清单原文 3 页，转回 PDF
成了 5 页，第 2、4 页只有几行，页脚的「第 2 页 / 共 3 页」也落在第 4 页，和原件对不上。

做法：转回 PDF 后，按每页原文独有的几句话找出它在译文 PDF 里从第几页开始，占了不止
一页的，把这一页原文里的字号、段前段后、固定行距和表格最小行高一起按比例缩小，再转
一次 PDF，最多缩到 MIN_SCALE。只动放不下的页，放得下的页原样不变。
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

#: 每轮在上一轮基础上再缩这么多；三轮后约为原来的 73%。
SHRINK_STEP = 0.9
#: 最小缩到原来的这么多，再小就不好读了（与 CAD 降字高的下限一致）。
MIN_SCALE = 0.7
#: 找页用的一句话先取前这么多个字（去掉空白后），和别的页重复就逐步加长；太长容易跨页。
_ANCHOR_LENGTHS = (12, 20, 30)
_MIN_ANCHOR_CHARS = 6
#: 只用每页开头这几段找页：页尾的句子（「第 2 页 / 共 3 页」）溢出时会落到下一页。
_TOP_PARAGRAPHS = 6
_SPACE = re.compile(r"\s+")


@dataclass
class FitResult:
    pages: int
    # 缩过的页（从 1 数，按这一段的页序）→ 最终比例
    scaled: dict[int, float] = field(default_factory=dict)
    # 缩到下限仍放不下的页
    still_over: list[int] = field(default_factory=list)


def _starts_page(sect_pr) -> bool:
    kind = sect_pr.find(f"{_W}type")
    value = kind.get(f"{_W}val") if kind is not None else "nextPage"
    return value in ("nextPage", "oddPage", "evenPage")


def page_groups(body) -> list[list]:
    """body 的顶层块按原文页分组：每组是一页原文的全部块（含结束各分节的段落）。"""
    sections: list[tuple[bool, list]] = []
    current: list = []
    for element in body:
        if element.tag == f"{_W}sectPr":
            sections.append((_starts_page(element), current))
            current = []
            continue
        current.append(element)
        sect_pr = element.find(f"{_W}pPr/{_W}sectPr") if element.tag == f"{_W}p" else None
        if sect_pr is not None:
            sections.append((_starts_page(sect_pr), current))
            current = []
    if current:
        sections.append((False, current))
    groups: list[list] = []
    for index, (starts_page, elements) in enumerate(sections):
        if index == 0 or starts_page or not groups:
            groups.append([])
        groups[-1].extend(elements)
    return groups


def _normalize(text: str) -> str:
    return _SPACE.sub("", text)


def _paragraph_texts(group: list) -> list[str]:
    texts = []
    for element in group:
        paragraphs = [element] if element.tag == f"{_W}p" else list(element.iter(f"{_W}p"))
        for paragraph in paragraphs:
            text = _normalize("".join(node.text or "" for node in paragraph.iter(f"{_W}t")))
            if text:
                texts.append(text)
    return texts


def _anchors(groups: list[list]) -> list[list[str]]:
    """每页原文开头几段里、在别的页都没出现过的句子开头（页眉框里的护照号这类每页都有，
    不能用）。只看开头几段：页尾的句子溢出时会落到下一页，拿它找页就找错了页。"""
    texts = [_paragraph_texts(group) for group in groups]
    joined = ["".join(items) for items in texts]
    result = []
    for index, items in enumerate(texts):
        others = [text for other, text in enumerate(joined) if other != index]
        anchors = []
        for text in items[:_TOP_PARAGRAPHS]:
            for length in (*_ANCHOR_LENGTHS, len(text)):
                anchor = text[:length]
                if len(anchor) >= _MIN_ANCHOR_CHARS and not any(anchor in other for other in others):
                    anchors.append(anchor)
                    break
            if len(anchors) == 3:
                break
        result.append(anchors)
    return result


def pdf_page_texts(pdf_path: Path) -> list[str]:
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(str(pdf_path))
    try:
        texts = []
        for index in range(len(doc)):
            page = doc[index]
            textpage = page.get_textpage()
            try:
                texts.append(_normalize(textpage.get_text_bounded()))
            finally:
                textpage.close()
                page.close()
        return texts
    finally:
        doc.close()


def pages_per_group(anchors: list[list[str]], page_texts: list[str]) -> list[int] | None:
    """每页原文在译文 PDF 里占了几页；有一页找不到、或位置对不上就返回 None（不猜）。"""
    starts: list[int] = []
    for group_anchors in anchors:
        # 每页原文都另起一页：从上一页原文开始的下一页往后找
        begin = starts[-1] + 1 if starts else 0
        hits = [next((page for page in range(begin, len(page_texts)) if anchor in page_texts[page]), None)
                for anchor in group_anchors]
        hits = [page for page in hits if page is not None]
        if not hits:
            return None
        starts.append(min(hits))
    if not starts or starts[0] != 0:
        return None
    ends = starts[1:] + [len(page_texts)]
    return [end - start for start, end in zip(starts, ends)]


def _scale_value(element, attribute: str, ratio: float, minimum: int = 0) -> None:
    value = element.get(attribute)
    if value is None:
        return
    try:
        number = float(value)
    except ValueError:
        return
    element.set(attribute, str(max(minimum, int(round(number * ratio)))))


def _ensure_mark_size(paragraph, default_size: int) -> None:
    p_pr = paragraph.find(f"{_W}pPr")
    if p_pr is None:
        p_pr = paragraph.makeelement(f"{_W}pPr", {})
        paragraph.insert(0, p_pr)
    r_pr = p_pr.find(f"{_W}rPr")
    if r_pr is None:
        r_pr = p_pr.makeelement(f"{_W}rPr", {})
        # pPr 里 rPr 排在 sectPr、pPrChange 之前
        anchor = p_pr.find(f"{_W}sectPr")
        if anchor is None:
            anchor = p_pr.find(f"{_W}pPrChange")
        if anchor is not None:
            anchor.addprevious(r_pr)
        else:
            p_pr.append(r_pr)
    if r_pr.find(f"{_W}sz") is None:
        r_pr.append(r_pr.makeelement(f"{_W}sz", {f"{_W}val": str(default_size)}))


def shrink_group(group: list, ratio: float, default_size: int = 22) -> None:
    """一页原文里的字号、段前段后、固定或最小行距、表格最小行高都乘以 ratio。"""
    for element in group:
        for run in element.iter(f"{_W}r"):
            if not "".join(node.text or "" for node in run.iter(f"{_W}t")).strip():
                continue
            r_pr = run.find(f"{_W}rPr")
            if r_pr is None:
                r_pr = run.makeelement(f"{_W}rPr", {})
                run.insert(0, r_pr)
            if r_pr.find(f"{_W}sz") is None:
                # 没写字号的（链接文字等）按默认字号补上，否则缩不到它
                size = r_pr.makeelement(f"{_W}sz", {f"{_W}val": str(default_size)})
                r_pr.append(size)
        paragraphs = [element] if element.tag == f"{_W}p" else list(element.iter(f"{_W}p"))
        for paragraph in paragraphs:
            if "".join(node.text or "" for node in paragraph.iter(f"{_W}t")).strip():
                continue
            # 空段（表格里打勾的空格子、分节段）按默认字号撑着行高：给段落标记也写上字号
            _ensure_mark_size(paragraph, default_size)
        for tag in (f"{_W}sz", f"{_W}szCs"):
            for size in element.iter(tag):
                _scale_value(size, f"{_W}val", ratio, minimum=2)
        for spacing in element.iter(f"{_W}spacing"):
            if spacing.getparent() is None or spacing.getparent().tag != f"{_W}pPr":
                continue
            _scale_value(spacing, f"{_W}before", ratio)
            _scale_value(spacing, f"{_W}after", ratio)
            if spacing.get(f"{_W}lineRule") in ("exact", "atLeast"):
                _scale_value(spacing, f"{_W}line", ratio, minimum=1)
        for height in element.iter(f"{_W}trHeight"):
            if height.get(f"{_W}hRule") != "exact":
                _scale_value(height, f"{_W}val", ratio)


def _default_size(doc) -> int:
    size = doc.styles.element.find(f"{_W}docDefaults/{_W}rPrDefault/{_W}rPr/{_W}sz")
    try:
        return int(size.get(f"{_W}val")) if size is not None else 22
    except (TypeError, ValueError):
        return 22


def fit_pages(docx_path: Path, pdf_path: Path, source_pages: int,
              export: Callable[[Path, Path], object]) -> FitResult:
    """pdf_path 是 docx_path 刚转出的 PDF。放不下的页逐轮缩小、重新转 PDF；改写两份文件。"""
    from docx import Document

    page_texts = pdf_page_texts(pdf_path)
    if len(page_texts) <= source_pages:
        return FitResult(pages=len(page_texts))
    doc = Document(str(docx_path))
    groups = page_groups(doc.element.body)
    if len(groups) != source_pages:
        logger.info(f"[PDF] 译文 Word 有 {len(groups)} 个分页节，原文 {source_pages} 页，不逐页缩字")
        return FitResult(pages=len(page_texts))
    anchors = _anchors(groups)
    default_size = _default_size(doc)
    scale = [1.0] * source_pages
    over: list[int] = []
    while True:
        used = pages_per_group(anchors, page_texts)
        if used is None:
            logger.info("[PDF] 译文 PDF 里找不到某一页原文的位置，不逐页缩字")
            over = []
            break
        over = [index for index, count in enumerate(used) if count > 1]
        shrinkable = [index for index in over if scale[index] * SHRINK_STEP >= MIN_SCALE - 1e-9]
        if not over or not shrinkable:
            break
        for index in shrinkable:
            shrink_group(groups[index], SHRINK_STEP, default_size)
            scale[index] *= SHRINK_STEP
        doc.save(str(docx_path))
        export(docx_path, pdf_path)
        page_texts = pdf_page_texts(pdf_path)
        if len(page_texts) <= source_pages:
            over = []
            break
    return FitResult(
        pages=len(page_texts),
        scaled={index + 1: round(value, 2) for index, value in enumerate(scale) if value < 1.0},
        still_over=[index + 1 for index in over],
    )
