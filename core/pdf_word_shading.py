"""PDF 转 Word 后，表格格子的底色按原页面重新上色。

讲义里的「Note / Example / Exercise」框常画成两层：底下一整块深色（标题条的颜色），
上面再盖一块浅色（正文的底色），标题文字是白色、正文是黑色。pdf2docx 只认出底下那块
深色，把它当成正文格子的底色，浅色那层丢了，黑字压在深蓝、深红上看不见；有的框的标题
格子反而没上色，白字落在白底上（反馈 #28，104 页课程讲义）。

做法：转完 Word 后，每个有字的格子到原 PDF 那一页找到它的第一行字，看那一行字背后实际
是什么颜色（取那一块像素里最多的颜色，字本身占少数），和格子的底色差得明显就改成原页面
的颜色。格子没有底色（白底）时只在字是浅色的时候才上色：白底黑字的普通表格不动，也不会
把可能垫在格子下面的图片盖住。

找不到字、PDF 打不开、没有 PyMuPDF 时什么都不改。
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from loguru import logger

from core.pdf_word_fit import page_groups

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

#: 格子底色和原页面颜色在任一通道上差这么多（0–255）才改。
COLOR_TOLERANCE = 48
#: 浅色字：亮度高于这个（0–1）。白底上的浅色字看不清，才给没底色的格子上色。
LIGHT_TEXT = 0.6
#: 找格子位置用的一句话最多取这么多个字；太长容易跨行找不到。
_SNIPPET_CHARS = 40
_MIN_SNIPPET_CHARS = 3
#: 取色时把页面放大到这么多倍（72 dpi 的倍数），细字的笔画不会占多数。
_RENDER_ZOOM = 2


def _hex_to_rgb(value: str | None) -> tuple[int, int, int] | None:
    if not value or value.lower() == "auto" or len(value) != 6:
        return None
    try:
        return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
    except ValueError:
        return None


def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return "".join(f"{channel:02X}" for channel in rgb)


def _luminance(rgb: tuple[int, int, int]) -> float:
    r, g, b = rgb
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255


def _differs(a: tuple[int, int, int], b: tuple[int, int, int]) -> bool:
    return max(abs(x - y) for x, y in zip(a, b)) > COLOR_TOLERANCE


def _own_paragraphs(tc) -> list:
    """格子自己的段落（嵌在里面的表格的段落不算，那些格子自己判断）。"""
    return [child for child in tc if child.tag == f"{_W}p"]


def _paragraph_text(paragraph) -> str:
    return "".join(node.text or "" for node in paragraph.iter(f"{_W}t"))


def _cell_fill(tc) -> tuple[object | None, tuple[int, int, int] | None]:
    shd = tc.find(f"{_W}tcPr/{_W}shd")
    if shd is None:
        return None, None
    return shd, _hex_to_rgb(shd.get(f"{_W}fill"))


def _text_color(paragraphs) -> tuple[int, int, int] | None:
    """格子里字最多的那种颜色（没写颜色的算黑色）。"""
    counts: Counter = Counter()
    for paragraph in paragraphs:
        for run in paragraph.iter(f"{_W}r"):
            text = "".join(node.text or "" for node in run.iter(f"{_W}t")).strip()
            if not text:
                continue
            color = run.find(f"{_W}rPr/{_W}color")
            rgb = _hex_to_rgb(color.get(f"{_W}val")) if color is not None else None
            counts[rgb or (0, 0, 0)] += len(text)
    return counts.most_common(1)[0][0] if counts else None


def _snippets(paragraphs) -> list[str]:
    """拿来在 PDF 里找格子位置的几句话：先用第一段开头，找不到再试后面几段。"""
    result = []
    for paragraph in paragraphs:
        text = " ".join(_paragraph_text(paragraph).split())
        if len(text) >= _MIN_SNIPPET_CHARS:
            result.append(text[:_SNIPPET_CHARS].strip())
        if len(result) == 3:
            break
    return result


def _background_color(page, rect, fitz) -> tuple[int, int, int] | None:
    """原页面这一块里最多的颜色：字的笔画占少数，最多的就是字背后的底色。"""
    clip = rect & page.rect
    if clip.is_empty or clip.width < 1 or clip.height < 1:
        return None
    pix = page.get_pixmap(matrix=fitz.Matrix(_RENDER_ZOOM, _RENDER_ZOOM), clip=clip, alpha=False,
                          colorspace=fitz.csRGB)
    samples = pix.samples
    stride, n = pix.stride, pix.n
    counts: Counter = Counter()
    for y in range(pix.height):
        row = samples[y * stride: y * stride + pix.width * n]
        for x in range(0, len(row), n):
            counts[(row[x], row[x + 1], row[x + 2])] += 1
    if not counts:
        return None
    return counts.most_common(1)[0][0]


def _find_rect(page, snippets: list[str], used: list):
    """第一句能在这一页找到的话所在的那一行；同一句出现几次时取还没被别的格子用过的。"""
    for snippet in snippets:
        try:
            hits = page.search_for(snippet)
        except Exception as exc:  # noqa: BLE001 - odd characters in a snippet: try the next one
            logger.debug(f"[PDF] 在原页面里找「{snippet}」出错：{exc!r}")
            continue
        for hit in hits:
            if not any(abs(hit.x0 - u.x0) < 0.5 and abs(hit.y0 - u.y0) < 0.5 for u in used):
                used.append(hit)
                return hit
    return None


def _set_fill(tc, shd, rgb: tuple[int, int, int]) -> None:
    if shd is None:
        from docx.oxml import OxmlElement

        tc_pr = tc.find(f"{_W}tcPr")
        if tc_pr is None:
            tc_pr = OxmlElement("w:tcPr")
            tc.insert(0, tc_pr)
        shd = OxmlElement("w:shd")
        # tcPr 里 shd 排在 tcBorders 之后、noWrap/tcMar/vAlign 等之前
        anchor = None
        for name in ("noWrap", "tcMar", "textDirection", "tcFitText", "vAlign", "hideMark"):
            anchor = tc_pr.find(f"{_W}{name}")
            if anchor is not None:
                break
        if anchor is not None:
            anchor.addprevious(shd)
        else:
            tc_pr.append(shd)
    shd.set(f"{_W}val", "clear")
    shd.set(f"{_W}color", "auto")
    shd.set(f"{_W}fill", _rgb_to_hex(rgb))


def recolor_cells_from_pdf(docx_path: Path, pdf_path: Path) -> int:
    """按原页面的颜色改正格子底色，返回改了几个格子。"""
    try:
        from core.pdf_component import require_fitz

        fitz = require_fitz()
    except Exception:  # noqa: BLE001 - no PyMuPDF: leave the colours as converted
        return 0
    from docx import Document

    doc = Document(str(docx_path))
    groups = page_groups(doc.element.body)
    changed = 0
    try:
        pdf = fitz.open(pdf_path)
    except Exception as exc:  # noqa: BLE001 - unreadable PDF: nothing to compare with
        logger.info(f"[PDF] 打不开原 PDF 对照格子底色：{exc!r}")
        return 0
    with pdf:
        if len(groups) != len(pdf):
            logger.info(f"[PDF] 转出的 Word 有 {len(groups)} 个分页节，原文 {len(pdf)} 页，不对照格子底色")
            return 0
        for page_index, group in enumerate(groups):
            cells = [tc for element in group for tc in element.iter(f"{_W}tc")]
            if not cells:
                continue
            page = pdf[page_index]
            used: list = []
            for tc in cells:
                paragraphs = _own_paragraphs(tc)
                snippets = _snippets(paragraphs)
                if not snippets:
                    continue
                shd, fill = _cell_fill(tc)
                text_rgb = _text_color(paragraphs)
                if fill is None and (text_rgb is None or _luminance(text_rgb) <= LIGHT_TEXT):
                    continue  # 白底深色字：普通表格，不动
                rect = _find_rect(page, snippets, used)
                if rect is None:
                    continue
                original = _background_color(page, rect, fitz)
                if original is None or not _differs(original, fill or (255, 255, 255)):
                    continue
                _set_fill(tc, shd, original)
                changed += 1
    if changed:
        doc.save(str(docx_path))
        logger.info(f"[PDF] 按原页面颜色改正了 {changed} 个格子的底色")
    return changed
