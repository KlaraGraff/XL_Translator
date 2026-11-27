"""PDF 转 Word 后，把公式里画出来的横线（上划线、分数线）补回来。

LaTeX 的 \\overline{A ∪ B}（补集）、\\frac{1}{1 − r} 的分数线在 PDF 里不是字，是画的一条细
横线。pdf2docx 只留文字和表格线，这些线丢了：「Ā ∪ B̄ = A ∩ B 的补集」成了
「A ∪ B = A ∩ B」，德摩根律读出来是错的；分数成了上下两行字（反馈 #28 数学讲义）。

做法：在原 PDF 里找短的细横线，看它和上下的字怎么挨着：
- 上划线：线紧贴在一段字的上沿（不到 1 磅），线的左右两头和这段字对齐；
- 分数线：线上面紧挨着分子、下面紧挨着分母，线和分子、分母里较宽的那个一样长。
在 Word 里找到同一段字（连同它前面几个字，好认出是哪一处），上划线、分数线的分母在每个
字后面加 U+0305（组合上划线），分子比分母宽时在分子每个字后面加 U+0332（组合下划线）。
这是字的一部分，翻译、复制都跟着走，Word 和 WPS 都能显示。

框线、表格线不算：它们比上下的字宽得多，或者同时挨着好几行字。
找不到对应的字、PDF 打不开、没有 PyMuPDF 时什么都不改。
"""
from __future__ import annotations

from pathlib import Path

from loguru import logger

from core.pdf_word_fit import page_groups

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

OVERLINE = "̅"
UNDERLINE = "̲"

#: 细横线：粗细不超过这么多磅；太短的（不到 2 磅）多半是字的一部分。
_MAX_THICKNESS = 1.2
_MIN_LENGTH = 2.0
#: 上划线和字的上沿最多隔这么多磅。
_OVERLINE_GAP = 0.6
#: 分数线和分子的下沿、分母的上沿最多隔这么多磅。
_FRACTION_GAP = 3.0
#: 线的两头和字的两头差不超过这么多磅才算对齐。
_ALIGN = 1.5
#: 在 Word 里认出同一处时，带上前面这么多个字、后面这么多个字。
_TAIL_CHARS = 8
_HEAD_CHARS = 3
#: 分数线比分子、分母宽出来的最多这么多磅（分子带上标时上标不挨着线，量出来的字窄一截）。
_FRACTION_SLACK = 12.0


def _strokes(page) -> list[tuple[float, float, float]]:
    """这一页的短细横线：[(左, 右, y)]。"""
    found = []
    for drawing in page.get_drawings():
        for item in drawing.get("items", []):
            if item[0] == "l":
                a, b = item[1], item[2]
                if abs(a.y - b.y) > 0.3 or abs(a.x - b.x) < _MIN_LENGTH:
                    continue
                found.append((min(a.x, b.x), max(a.x, b.x), (a.y + b.y) / 2))
            elif item[0] == "re":
                rect = item[1]
                if rect.height > _MAX_THICKNESS or rect.width < _MIN_LENGTH:
                    continue
                found.append((rect.x0, rect.x1, (rect.y0 + rect.y1) / 2))
    return found


def _page_chars(page) -> list[tuple[str, tuple, int]]:
    """这一页有形的字：[(字, 框, 第几行)]，按行、行内从左到右排。"""
    chars = []
    line_index = 0
    for block in page.get_text("rawdict").get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                for char in span.get("chars", []):
                    if char.get("c", "").strip():
                        chars.append((char["c"], tuple(char["bbox"]), line_index))
            line_index += 1
    return chars


def _covered(chars, x0: float, x1: float, test) -> list:
    return [char for char in chars if x0 - 0.5 <= (char[1][0] + char[1][2]) / 2 <= x1 + 0.5 and test(char[1])]


def _with_scripts(all_chars, near, x0: float, x1: float) -> list:
    """挨着线的那几个字，再加上和它们上下有重叠的上下标（上标不挨着线，但算在分子里）。"""
    if not near:
        return near
    top = min(char[1][1] for char in near)
    bottom = max(char[1][3] for char in near)
    return _covered(all_chars, x0, x1, lambda box: box[1] < bottom and box[3] > top)


def _one_line(chars) -> bool:
    return bool(chars) and len({char[2] for char in chars}) == 1


def _extent(chars) -> tuple[float, float]:
    return min(char[1][0] for char in chars), max(char[1][2] for char in chars)


def _aligned(chars, x0: float, x1: float) -> bool:
    left, right = _extent(chars)
    return abs(left - x0) <= _ALIGN and abs(right - x1) <= _ALIGN


def _context(all_chars, chars) -> tuple[str, str]:
    """同一行里紧挨在前面的几个字、紧跟在后面的几个字。"""
    line = chars[0][2]
    left, right = _extent(chars)
    same = [char for char in all_chars if char[2] == line and char not in chars]
    before = "".join(char[0] for char in same if char[1][2] <= left + 0.5)[-_TAIL_CHARS:]
    after = "".join(char[0] for char in same if char[1][0] >= right - 0.5)[:_HEAD_CHARS]
    return before, after


def pdf_rules(page) -> list[tuple[str, str, str, str]]:
    """这一页要补的线：[(前文末尾, 线下面或上面的那段字, 后文开头, 组合符)]。"""
    chars = _page_chars(page)
    found = []
    for x0, x1, y in _strokes(page):
        below = _covered(chars, x0, x1, lambda box, y=y: -1 < box[1] - y <= _FRACTION_GAP)
        above = _covered(chars, x0, x1, lambda box, y=y: -1 < y - box[3] <= _FRACTION_GAP)
        if not _one_line(below) or (above and not _one_line(above)):
            continue  # 同时挨着好几行字：表格线
        below = _with_scripts(chars, below, x0, x1)
        above = _with_scripts(chars, above, x0, x1)
        close = [char for char in below if char[1][1] - y <= _OVERLINE_GAP]
        if not above and close and len(close) == len(below) and _aligned(below, x0, x1):
            target, mark = below, OVERLINE  # 上划线
        elif above:
            # 分数线：和分子、分母里较宽的那个一样长
            if _aligned(below, x0, x1) or _extent(below)[1] - _extent(below)[0] >= _extent(above)[1] - _extent(above)[0]:
                target, mark = below, OVERLINE
            else:
                target, mark = above, UNDERLINE
            left = min(_extent(below)[0], _extent(above)[0])
            right = max(_extent(below)[1], _extent(above)[1])
            if abs(left - x0) > _FRACTION_SLACK or abs(right - x1) > _FRACTION_SLACK:
                continue  # 比分子分母都宽得多：框线
        else:
            continue
        target = sorted(target, key=lambda char: char[1][0])
        before, after = _context(chars, target)
        found.append((before, "".join(char[0] for char in target), after, mark))
    return found


def _positions(paragraph) -> tuple[str, list[tuple[object, int]]]:
    """段落里不是空白的字连成一串，每个字记着它在哪个 w:t、第几个。"""
    text = []
    where = []
    for node in paragraph.iter(f"{_W}t"):
        for index, char in enumerate(node.text or ""):
            if not char.isspace():
                text.append(char)
                where.append((node, index))
    return "".join(text), where


def _mark(where, start: int, length: int, mark: str, pending: dict) -> None:
    for node, index in where[start:start + length]:
        pending.setdefault(node, set()).add((index, mark))


def _apply(pending: dict) -> None:
    for node, marks in pending.items():
        text = node.text or ""
        for index, mark in sorted(marks, reverse=True):
            text = text[:index + 1] + mark + text[index + 1:]
        node.text = text


def restore_rules_from_pdf(docx_path: Path, pdf_path: Path) -> int:
    """把原 PDF 里的上划线、分数线在转出的 Word 里补回来，返回补了几处。"""
    try:
        from core.pdf_component import require_fitz

        fitz = require_fitz()
    except Exception:  # noqa: BLE001 - no PyMuPDF: leave the text as converted
        return 0
    from docx import Document

    doc = Document(str(docx_path))
    groups = page_groups(doc.element.body)
    marked = 0
    try:
        pdf = fitz.open(pdf_path)
    except Exception as exc:  # noqa: BLE001 - unreadable PDF: nothing to compare with
        logger.info(f"[PDF] 打不开原 PDF 对照上划线、分数线：{exc!r}")
        return 0
    with pdf:
        if len(groups) != len(pdf):
            return 0
        for page_index, group in enumerate(groups):
            rules = pdf_rules(pdf[page_index])
            if not rules:
                continue
            paragraphs = [p for element in group
                          for p in ([element] if element.tag == f"{_W}p" else element.iter(f"{_W}p"))]
            texts = [_positions(p) for p in paragraphs]
            used: set = set()
            pending: dict = {}
            for before, covered, after, mark in rules:
                hit = _locate(texts, before, covered, after, used)
                if hit is None:
                    continue
                where, start = hit
                used.add((id(where), start))
                _mark(where, start, len(covered), mark, pending)
                marked += 1
            _apply(pending)
    if marked:
        doc.save(str(docx_path))
        logger.info(f"[PDF] 按原文补回了 {marked} 处上划线、分数线")
    return marked


def _patterns(before: str, covered: str, after: str):
    """在 Word 里找这段字的几种写法，先用前后文都带上的，认得最准；分数的分子、分母在
    Word 里常自成一段，前后文对不上，再逐步少带一点。单独一个字母不带前后文认不出来。"""
    yield before, after
    yield before, ""
    yield "", after
    for length in range(len(before) - 1, 0, -1):
        yield before[len(before) - length:], ""
    if len(covered) >= 2:
        yield "", ""


def _locate(texts, before: str, covered: str, after: str, used: set):
    for head, tail in _patterns(before, covered, after):
        if not head and not tail and len(covered) < 2:
            continue
        pattern = head + covered + tail
        for text, where in texts:
            start = text.find(pattern)
            while start != -1:
                position = start + len(head)
                if (id(where), position) not in used:
                    return where, position
                start = text.find(pattern, start + 1)
    return None
