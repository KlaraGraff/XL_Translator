"""PDF 转 Word 后，把丢了的上标、下标补回来。

讲义里的 2^k、x_i 在 PDF 里是一个小一号、抬高（或压低）一点的字。pdf2docx 只看 MuPDF
给的「上标」标记，排版软件做出来的 PDF 多半没有这个标记，于是转出来成了同一行里一个
小一号的普通字，前面还多了一个空格：「m = 2 k」，翻译后读成「2k」（反馈 #28）。

做法：在原 PDF 里按每一行的基线找出比这一行正文小、又明显抬高或压低的那几段字，记下
它紧跟在什么字后面；再到转出的 Word 同一页里找同样的字号变小、同样文字、同样前文的
run，标成上标或下标，字号还原成正文字号（Word 自己会缩小上下标），pdf2docx 加在前面
的空格（原文里没有）去掉。

找不到对应的 run、PDF 打不开、没有 PyMuPDF 时什么都不改。
"""
from __future__ import annotations

from itertools import pairwise
from pathlib import Path

from loguru import logger

from core.pdf_word_fit import page_groups

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

#: 比这一行正文字号小到这个比例以下才算上下标。
SMALL_RATIO = 0.85
#: 基线抬高（下标是压低）超过正文字号的这么多才算。
SHIFT_RATIO = 0.15
#: 记前文末尾这么多个字，用来在 Word 里认出同一处。
_TAIL_CHARS = 8

SUPER, SUB = "superscript", "subscript"


def _key(text: str) -> str:
    return "".join(text.split())


def pdf_scripts(page) -> list[tuple[str, str, str, bool]]:
    """这一页的上下标：[(前文末尾, 上下标文字, superscript/subscript, 原文前面有没有空格)]。

    上标常被 MuPDF 拆成单独的一行，所以不按行找：对每段字找紧挨在它左边、上下有重叠的
    那段字当前文，比前文小一号、基线又明显错开的就是上下标。"""
    spans = [span for block in page.get_text("dict").get("blocks", [])
             for line in block.get("lines", []) for span in line.get("spans", [])
             if span.get("text", "").strip()]
    found = []
    largest = max((span["size"] for span in spans), default=0)
    for span in spans:
        if span["size"] > largest * SMALL_RATIO:
            continue  # 这一页最大的字号附近的字不可能是上下标，省掉逐个比较
        x0, y0, _x1, y1 = span["bbox"]
        best = None
        for other in spans:
            if other is span or other["size"] * SMALL_RATIO < span["size"]:
                continue
            ox0, oy0, ox1, oy1 = other["bbox"]
            gap = x0 - ox1
            if ox0 >= x0 or not -1 <= gap <= other["size"] or min(y1, oy1) <= max(y0, oy0):
                continue
            if best is None or ox1 > best["bbox"][2]:
                best = other
        if best is None:
            continue
        shift = best["origin"][1] - span["origin"][1]  # PyMuPDF 的 y 向下，抬高时为正
        if abs(shift) < best["size"] * SHIFT_RATIO:
            continue
        before = best["text"]
        found.append((_key(before)[-_TAIL_CHARS:], _key(span["text"]), SUPER if shift > 0 else SUB,
                      before[-1:].isspace()))
    return found


def _run_text(run) -> str:
    return "".join(node.text or "" for node in run.iter(f"{_W}t"))


def _size(run) -> int | None:
    node = run.find(f"{_W}rPr/{_W}sz")
    try:
        return int(node.get(f"{_W}val")) if node is not None else None
    except (TypeError, ValueError):
        return None


def _mark(run, kind: str, size: int | None) -> None:
    from docx.oxml import OxmlElement

    r_pr = run.find(f"{_W}rPr")
    if r_pr is None:
        r_pr = OxmlElement("w:rPr")
        run.insert(0, r_pr)
    vert = r_pr.find(f"{_W}vertAlign")
    if vert is None:
        vert = OxmlElement("w:vertAlign")
        # rPr 里 vertAlign 排在 sz/szCs/highlight/u/effect/bdr/shd/fitText 之后
        follow = None
        for name in ("rtl", "cs", "em", "lang", "eastAsianLayout", "specVanish", "oMath"):
            follow = r_pr.find(f"{_W}{name}")
            if follow is not None:
                break
        if follow is not None:
            follow.addprevious(vert)
        else:
            r_pr.append(vert)
    vert.set(f"{_W}val", kind)
    sz = r_pr.find(f"{_W}sz")
    if size and sz is not None:
        sz.set(f"{_W}val", str(size))
        sz_cs = r_pr.find(f"{_W}szCs")
        if sz_cs is not None:
            sz_cs.set(f"{_W}val", str(size))


def _strip_trailing_space(run) -> None:
    nodes = list(run.iter(f"{_W}t"))
    if nodes and nodes[-1].text and nodes[-1].text.endswith(" "):
        nodes[-1].text = nodes[-1].text.rstrip(" ")


def _apply(paragraph, scripts: list[tuple[str, str, str, bool]]) -> int:
    runs = [child for child in paragraph if child.tag == f"{_W}r"]
    marked = 0
    for prev, run in pairwise(runs):
        text = _key(_run_text(run))
        if not text or run.find(f"{_W}rPr/{_W}vertAlign") is not None:
            continue
        prev_size, size = _size(prev), _size(run)
        if not prev_size or not size or size > prev_size * SMALL_RATIO:
            continue
        before = _key(_run_text(prev))
        for tail, script, kind, spaced in scripts:
            if script == text and before and (before.endswith(tail) or tail.endswith(before)):
                _mark(run, kind, prev_size)
                if not spaced:
                    _strip_trailing_space(prev)
                marked += 1
                break
    return marked


def restore_scripts_from_pdf(docx_path: Path, pdf_path: Path) -> int:
    """把原 PDF 里的上下标在转出的 Word 里标回来，返回标了几处。"""
    try:
        from core.pdf_component import require_fitz

        fitz = require_fitz()
    except Exception:  # noqa: BLE001 - no PyMuPDF: leave the runs as converted
        return 0
    from docx import Document

    doc = Document(str(docx_path))
    groups = page_groups(doc.element.body)
    marked = 0
    try:
        pdf = fitz.open(pdf_path)
    except Exception as exc:  # noqa: BLE001 - unreadable PDF: nothing to compare with
        logger.info(f"[PDF] 打不开原 PDF 对照上下标：{exc!r}")
        return 0
    with pdf:
        if len(groups) != len(pdf):
            return 0
        for page_index, group in enumerate(groups):
            scripts = pdf_scripts(pdf[page_index])
            if not scripts:
                continue
            for element in group:
                paragraphs = [element] if element.tag == f"{_W}p" else list(element.iter(f"{_W}p"))
                for paragraph in paragraphs:
                    marked += _apply(paragraph, scripts)
    if marked:
        doc.save(str(docx_path))
        logger.info(f"[PDF] 按原文补回了 {marked} 处上标、下标")
    return marked
