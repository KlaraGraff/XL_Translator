"""PDF 转 Word 后，把公式里画出来的横线（上划线、分数线）补回来。

LaTeX 的 \\overline{A ∪ B}（补集）、\\frac{1}{1 − r} 的分数线在 PDF 里不是字，是画的一条细
横线。pdf2docx 只留文字和表格线，这些线丢了：「A ∪ B 的补集 = Ā ∩ B̄」成了
「A ∪ B = A ∩ B」，德摩根律读出来是错的；分数成了上下两行字（反馈 #28 数学讲义）。

做法：在原 PDF 里找短的细横线，看它和上下的字怎么挨着：
- 上划线：线紧贴在一段字的上沿（不到 1 磅），线的左右两头和这段字对齐；
- 分数线：线上面紧挨着分子、下面紧挨着分母，线和分子、分母合起来一样宽。
在 Word 里认出同一段字：把这一页 PDF 的字和 Word 的字整页逐个对齐（连着好几个字都一样才算
对上），线盖住的那几个 PDF 字对到哪几个 Word 字，就是哪几个；不按「前后几个字一样」去搜，
免得落到别处同样的字母上（讲义第 48 页「A 和 B 的交集」里的 A 被错加了横线）。
把这段字做成一个 Word 公式：上划线用「上横线」，一条线整条盖住它下面的整段字（A ∪ B 上面
是一条，不是 A、B 各一截）。分数的分子、分母在 Word 里连在同一段时（pdf2docx 靠换行、折行把
它们排成上下两行，行距常常只有半行，叠成一团），整个做成一个 Word 分数；分在两段时，给分母
加上横线（分子比分母宽时给分子加下横线）。上下标跟着做成公式里的上下标。
翻译时 Word 公式整个原样保留（core.word_inline_marks 当占位符发给模型）。

框线、表格线不算：它们比上下的字宽得多，或者同时挨着好几行字。
那段字跨了超链接、夹着制表符或图片时不改。找不到对应的字、PDF 打不开、没有 PyMuPDF 时什么都不改。
"""
from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

from loguru import logger

from core.pdf_word_fit import page_groups

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_M = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"

#: 横线在字的上面、下面（Word 公式 m:bar 的 m:pos）
OVERLINE = "top"
UNDERLINE = "bot"

#: 细横线：粗细不超过这么多磅；太短的（不到 2 磅）多半是字的一部分。
_MAX_THICKNESS = 1.2
_MIN_LENGTH = 2.0
#: 上划线和字的上沿最多隔这么多磅。
_OVERLINE_GAP = 0.6
#: 分数线和分子的下沿、分母的上沿最多隔这么多磅。
_FRACTION_GAP = 3.0
#: 线的两头和字的两头差不超过这么多磅才算对齐。
_ALIGN = 1.5
#: PDF 和 Word 两边连着至少这么多个字一样，才算对上了同一处（单个字母到处都有，对不准）。
_MIN_MATCH = 4
#: 分数线比分子、分母宽出来的最多这么多磅（分子带上标时上标不挨着线，量出来的字窄一截）。
_FRACTION_SLACK = 12.0


def _strokes(page) -> list[tuple[float, float, float]]:
    """这一页的短细横线：[(左, 右, y)]。"""
    found = []
    for drawing in page.get_drawings():
        items = drawing.get("items", [])
        if len(items) != 1:
            continue  # TeX 的横线是单独画的一笔；图里的圆、框是好多段连成的，其中平的一小段不算
        for item in items:
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


def _page_chars(page) -> list[tuple[str, tuple, int, int]]:
    """这一页有形的字：[(字, 框, 第几行, 第几个字)]，按 PDF 里的先后排。"""
    chars = []
    line_index = 0
    for block in page.get_text("rawdict").get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                for char in span.get("chars", []):
                    if char.get("c", "").strip():
                        chars.append((char["c"], tuple(char["bbox"]), line_index, len(chars)))
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


def pdf_rules(page) -> tuple[str, list[tuple[list[int], str, tuple[list[int], list[int], int] | None]]]:
    """这一页的字（不含空白，按 PDF 里的先后连成一串）和要补的线：
    [(线下面或上面那段字是第几个字, 横线在上还是在下, 分数)]。

    分数是（分子是第几个字, 分母是第几个字, 分子上沿到分母下沿多高，1/20 磅）；上划线是 None。"""
    chars = _page_chars(page)
    found = []
    for x0, x1, y in _strokes(page):
        below = _covered(chars, x0, x1, lambda box, y=y: -1 < box[1] - y <= _FRACTION_GAP)
        above = _covered(chars, x0, x1, lambda box, y=y: -1 < y - box[3] <= _FRACTION_GAP)
        if not _one_line(below):
            continue  # 同时挨着好几行字：表格线
        below = _with_scripts(chars, below, x0, x1)
        close = [char for char in below if char[1][1] - y <= _OVERLINE_GAP]
        if close and len(close) == len(below) and _aligned(below, x0, x1):
            # 上划线：紧贴在字上沿。上面挨着的是上一行字的下沿（字母的尾巴），不是分子——
            # 分数的分母离分数线有一两磅（TeX 的规矩），不会贴得这么近
            found.append((_indices(below), OVERLINE, None))
            continue
        if not above or not _one_line(above):
            continue  # 上面没有字（不是分数），或者同时挨着好几行字（表格线）
        above = _with_scripts(chars, above, x0, x1)
        # 分数线：和分子、分母里较宽的那个一样长
        if _aligned(below, x0, x1) or _extent(below)[1] - _extent(below)[0] >= _extent(above)[1] - _extent(above)[0]:
            target, mark = below, OVERLINE
        else:
            target, mark = above, UNDERLINE
        left = min(_extent(below)[0], _extent(above)[0])
        right = max(_extent(below)[1], _extent(above)[1])
        if abs(left - x0) > _FRACTION_SLACK or abs(right - x1) > _FRACTION_SLACK:
            continue  # 比分子分母都宽得多：框线
        height = max(char[1][3] for char in below) - min(char[1][1] for char in above)
        found.append((_indices(target), mark, (_indices(above), _indices(below), round(height * 20))))
    return "".join(char[0] for char in chars), found


def _indices(chars) -> list[int]:
    return sorted(char[3] for char in chars)


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


def _plain_run(run, paragraph) -> bool:
    """段落直属、只有格式和一段字的 run 才动（超链接里的、夹着制表符图片的不动）。"""
    return (run.tag == f"{_W}r" and run.getparent() is paragraph
            and all(child.tag in (f"{_W}rPr", f"{_W}t") for child in run)
            and len(run.findall(f"{_W}t")) == 1)


def _split(run, offset: int):
    """run 在第 offset 个字处切成两个格式一样的 run，返回后一个。"""
    clone = copy.deepcopy(run)
    node, clone_node = run.find(f"{_W}t"), clone.find(f"{_W}t")
    text = node.text or ""
    node.text, clone_node.text = text[:offset], text[offset:]
    for item in (node, clone_node):
        item.set(_XML_SPACE, "preserve")
    run.addnext(clone)
    return clone


def _script(run) -> str | None:
    found = run.find(f"{_W}rPr/{_W}vertAlign")
    value = found.get(f"{_W}val") if found is not None else None
    return value if value in ("superscript", "subscript") else None


def _math_run(run, text: str):
    """w:r → m:r：字体、字号照抄（上下标标记去掉，公式里用 m:sSup / m:sSub）。"""
    math_run = run.makeelement(f"{_M}r", {})
    r_pr = run.find(f"{_W}rPr")
    if r_pr is not None:
        r_pr = copy.deepcopy(r_pr)
        for node in r_pr.findall(f"{_W}vertAlign"):
            r_pr.remove(node)
        math_run.append(r_pr)
    node = math_run.makeelement(f"{_M}t", {_XML_SPACE: "preserve"})
    node.text = text
    math_run.append(node)
    return math_run


def _math_items(runs) -> list:
    """一串 w:r 换成公式里的一串元素：上下标接在前一个字后面做成 m:sSup / m:sSub。"""
    items: list = []
    for run in runs:
        text = run.find(f"{_W}t").text or ""
        kind = _script(run)
        name, part_name = ("sSup", "sup") if kind == "superscript" else ("sSub", "sub")
        if kind is not None and items and items[-1].tag == f"{_M}{name}":
            # 上标拆成了几个 run（rⁿ⁺¹ 的 n 和 +1 字体不同）：接在同一个上标里
            if text:
                items[-1].find(f"{_M}{part_name}").append(_math_run(run, text))
            continue
        base_run = items[-1] if items and items[-1].tag == f"{_M}r" else None
        stripped = (base_run.find(f"{_M}t").text or "").rstrip() if base_run is not None else ""
        if kind is None or base_run is None or not stripped:
            if text:
                items.append(_math_run(run, text))
            continue
        # 底数是前面最后一个字
        base = copy.deepcopy(base_run)
        base.find(f"{_M}t").text = stripped[-1]
        if len(stripped) > 1:
            base_run.find(f"{_M}t").text = stripped[:-1]
        else:
            items.pop()
        script = run.makeelement(f"{_M}{name}", {})
        base_holder = script.makeelement(f"{_M}e", {})
        base_holder.append(base)
        part = script.makeelement(f"{_M}{part_name}", {})
        part.append(_math_run(run, text))
        script.append(base_holder)
        script.append(part)
        items.append(script)
    return items


def _bar(paragraph, runs: list, position: str) -> None:
    """几个 run 做成一个带横线的 Word 公式，放回原来的位置。"""
    math = paragraph.makeelement(f"{_M}oMath", {})
    bar = math.makeelement(f"{_M}bar", {})
    props = bar.makeelement(f"{_M}barPr", {})
    props.append(props.makeelement(f"{_M}pos", {f"{_M}val": position}))
    body = bar.makeelement(f"{_M}e", {})
    for item in _math_items(runs):
        body.append(item)
    bar.append(props)
    bar.append(body)
    math.append(bar)
    runs[0].addprevious(math)
    for run in runs:
        paragraph.remove(run)


def _space_beside(neighbour, at_end: bool, paragraph) -> None:
    """TeX 的 PDF 里没有空格字：公式和前后紧挨的字母、数字之间补一个空格。"""
    if neighbour is None or neighbour.tag != f"{_W}r" or neighbour.getparent() is not paragraph:
        return
    node = neighbour.find(f"{_W}t")
    if node is None or not node.text:
        return
    text = node.text
    edge = text[-1] if at_end else text[0]
    if edge.isalnum():
        node.text = text + " " if at_end else " " + text
        node.set(_XML_SPACE, "preserve")


def _runs_between(paragraph, first_run, last_run, allowed) -> list | None:
    """first_run 到 last_run 这一串兄弟节点；有一个不是 allowed 的就返回 None。"""
    runs = []
    run = first_run
    while run is not None:
        if not allowed(run, paragraph):
            return None
        runs.append(run)
        if run is last_run:
            return runs
        run = run.getnext()
    return None


def _cut(paragraph, where, start: int, length: int) -> list | None:
    """段落里第 start 个字起的 length 个字（不算空白）所在的 run，头尾多出的字切出去。"""
    first_node, first_index = where[start]
    last_node, last_index = where[start + length - 1]
    first_run, last_run = first_node.getparent(), last_node.getparent()
    runs = _runs_between(paragraph, first_run, last_run, _plain_run)
    if not runs:
        return None
    if (last_node.text or "")[last_index + 1:]:
        _split(last_run, last_index + 1)
    if first_index:
        piece = _split(first_run, first_index)
        if runs[-1] is first_run:
            runs[-1] = piece
        runs[0] = piece
    return runs


def _apply(paragraph, where, start: int, length: int, position: str) -> bool:
    """段落里第 start 个字起的 length 个字（不算空白）做成带横线的公式。动不了时返回 False。"""
    runs = _cut(paragraph, where, start, length)
    if runs is None:
        return False
    before, after = runs[0].getprevious(), runs[-1].getnext()
    _bar(paragraph, runs, position)
    _space_beside(before, True, paragraph)
    _space_beside(after, False, paragraph)
    return True


def _separator(run, paragraph) -> bool:
    """分子和分母中间只隔着空白、制表符、换行。"""
    return (run.tag == f"{_W}r" and run.getparent() is paragraph
            and all(child.tag in (f"{_W}rPr", f"{_W}tab", f"{_W}br")
                    or (child.tag == f"{_W}t" and not (child.text or "").strip()) for child in run))


def _line_spacing(paragraph) -> tuple[Any, int]:
    spacing = paragraph.find(f"{_W}pPr/{_W}spacing")
    if spacing is None or spacing.get(f"{_W}lineRule") not in ("exact", "atLeast"):
        return spacing, 0
    try:
        return spacing, int(spacing.get(f"{_W}line") or 0)
    except ValueError:
        return spacing, 0


def _fit_fraction_line(paragraph, height: int) -> None:
    """分子、分母原来各占一行（固定行距），现在合成一行分数：行距改成「至少」原文分数那么高，
    段前距减掉多出来的一半，分数的中线还在原来两行的中线上，下面的版面不往下推。"""
    spacing, line = _line_spacing(paragraph)
    if spacing is None or not line:
        return
    try:
        before = int(spacing.get(f"{_W}before") or 0)
    except ValueError:
        before = 0
    # 行高按 Word 排出来的分数自己定（「至少」原来一行那么高）；硬撑成原文的高度时，多出来的
    # 空白垫在分数上面，同一行的「rᵏ =」被压到下面
    spacing.set(f"{_W}before", str(max(0, before - (max(line, height) - 2 * line) // 2)))
    spacing.set(f"{_W}lineRule", "atLeast")


_WORD = re.compile(r"[^\W\d_]{2,}")


def _display(paragraph, math) -> None:
    """这一段只有这个分数（前面最多几个制表符，后面最多一个逗号、句点）时，整段做成一个独立
    公式：分数按独立公式的大小排（行内公式里的分数 Word 会缩小）。段首的制表符换成同样宽的缩进。

    分数前面还有算式（rᵏ =）时不做：Word 的 Cambria Math 比原文的字宽，独立公式在格子里放不下
    就会在等号处折成两行，分母被格子切掉（本机 math 第 2 页）。行内公式不折行。"""
    others = [child for child in paragraph if child.tag != f"{_W}pPr" and child is not math]
    if any(_plain_run(child, paragraph) for child in math.itersiblings(preceding=True)):
        return
    if any(not (_plain_run(child, paragraph) or _separator(child, paragraph)) for child in others):
        return
    if any(_WORD.search(child.find(f"{_W}t").text or "") for child in others if _plain_run(child, paragraph)):
        return
    lead = 0
    for child in paragraph:
        if child is math or _plain_run(child, paragraph):
            break
        lead += len(child.findall(f"{_W}tab"))
    before = [child for child in math.itersiblings(preceding=True) if _plain_run(child, paragraph)][::-1]
    after = [child for child in math.itersiblings() if _plain_run(child, paragraph)]
    for item in reversed(_math_items(before)):
        math.insert(0, item)
    for item in _math_items(after):
        math.append(item)
    p_pr = paragraph.find(f"{_W}pPr")
    jc = p_pr.find(f"{_W}jc") if p_pr is not None else None
    align = {"center": "center", "right": "right", "end": "right"}.get(jc.get(f"{_W}val") if jc is not None else "", "left")
    para = paragraph.makeelement(f"{_M}oMathPara", {})
    para_pr = para.makeelement(f"{_M}oMathParaPr", {})
    para_pr.append(para_pr.makeelement(f"{_M}jc", {f"{_M}val": align}))
    para.append(para_pr)
    math.addprevious(para)
    para.append(math)
    for child in others:
        paragraph.remove(child)
    if lead and p_pr is not None:
        stop = p_pr.find(f"{_W}tabs/{_W}tab")
        ind = p_pr.find(f"{_W}ind")
        if stop is not None and ind is not None:
            try:
                ind.set(f"{_W}left", str(int(ind.get(f"{_W}left") or 0) + int(stop.get(f"{_W}pos") or 0)))
            except ValueError:
                pass


def _apply_fraction(paragraph, where, start: int, numerator: int, denominator: int, height: int) -> bool:
    """分子、分母在同一段里（pdf2docx 把它们排成上下两行）：合成一个 Word 分数。"""
    den_runs = _cut(paragraph, where, start + numerator, denominator)
    if den_runs is None:
        return False
    num_runs = _cut(paragraph, where, start, numerator)
    if num_runs is None:
        return False
    between = _runs_between(paragraph, num_runs[-1].getnext(), den_runs[0].getprevious(), _separator) \
        if num_runs[-1].getnext() is not den_runs[0] else []
    if between is None:
        return False
    before, after = num_runs[0].getprevious(), den_runs[-1].getnext()
    math = paragraph.makeelement(f"{_M}oMath", {})
    fraction = math.makeelement(f"{_M}f", {})
    for name, runs in (("num", num_runs), ("den", den_runs)):
        part = fraction.makeelement(f"{_M}{name}", {})
        for item in _math_items(runs):
            part.append(item)
        fraction.append(part)
    math.append(fraction)
    num_runs[0].addprevious(math)
    for run in (*num_runs, *between, *den_runs):
        paragraph.remove(run)
    _space_beside(before, True, paragraph)
    _space_beside(after, False, paragraph)
    _fit_fraction_line(paragraph, height)
    _display(paragraph, math)
    return True


def restore_rules_from_pdf(docx_path: Path, pdf_path: Path) -> int:
    """把原 PDF 里的上划线、分数线在转出的 Word 里补回来（做成带横线的 Word 公式），返回补了几处。"""
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
            page_text, rules = pdf_rules(pdf[page_index])
            if rules:
                marked += _restore_page(group, page_text, rules)
    if marked:
        doc.save(str(docx_path))
        logger.info(f"[PDF] 按原文补回了 {marked} 处上划线、分数线（Word 公式）")
    return marked


def _alignment(page_text: str, word_text: str) -> dict[int, int]:
    """PDF 这一页的字和 Word 这一页的字逐个对上：第几个 PDF 字 → 第几个 Word 字。
    只认两边连着好几个字都一样的地方，对不上的字不在里面。"""
    from difflib import SequenceMatcher

    mapping: dict[int, int] = {}
    matcher = SequenceMatcher(None, page_text, word_text, autojunk=False)
    for block in matcher.get_matching_blocks():
        if block.size < _MIN_MATCH:
            continue
        for offset in range(block.size):
            mapping[block.a + offset] = block.b + offset
    return mapping


def _span(mapping: dict[int, int], indices: list[int], spans) -> tuple[int, int, int] | None:
    """PDF 里这几个字在 Word 里是哪一段、从第几个字起、几个字。必须一个不落、连在一起、在同一段。"""
    if not indices or any(index not in mapping for index in indices):
        return None
    targets = sorted(mapping[index] for index in indices)
    if targets[-1] - targets[0] + 1 != len(targets):
        return None
    for number, (offset, length) in enumerate(spans):
        if offset <= targets[0] and targets[-1] < offset + length:
            return number, targets[0] - offset, len(targets)
    return None


def _restore_page(group: list, page_text: str, rules) -> int:
    paragraphs = [p for element in group
                  for p in ([element] if element.tag == f"{_W}p" else element.iter(f"{_W}p"))]
    texts = [_positions(p) for p in paragraphs]
    spans, offset = [], 0
    for text, _where in texts:
        spans.append((offset, len(text)))
        offset += len(text)
    mapping = _alignment(page_text, "".join(text for text, _where in texts))
    hits: dict[int, list] = {}
    for indices, position, fraction in rules:
        if fraction is not None:
            numerator, denominator, height = fraction
            top, bottom = _span(mapping, numerator, spans), _span(mapping, denominator, spans)
            if top is not None and bottom is not None and top[0] == bottom[0] and top[1] + top[2] == bottom[1]:
                # 分子、分母连在同一段里（pdf2docx 排成上下两行）：整个做成 Word 分数
                hits.setdefault(top[0], []).append((top[1], top[2] + bottom[2], (top[2], height)))
                continue
        found = _span(mapping, indices, spans)
        if found is not None:
            hits.setdefault(found[0], []).append((found[1], found[2], position))
    marked = 0
    for number, items in hits.items():
        paragraph, (_text, where) = paragraphs[number], texts[number]
        taken: list[tuple[int, int]] = []
        # 同一段里从后往前改：前面的字在哪个 w:t、第几个不会因为后面切开 run 而变
        for start, length, how in sorted(items, key=lambda item: item[0], reverse=True):
            if any(start < other + size and other < start + length for other, size in taken):
                continue
            taken.append((start, length))
            if isinstance(how, tuple):
                numerator, height = how
                marked += _apply_fraction(paragraph, where, start, numerator, length - numerator, height)
            else:
                marked += _apply(paragraph, where, start, length, how)
    return marked
