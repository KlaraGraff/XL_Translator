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

import unicodedata
from collections import Counter
from pathlib import Path

from loguru import logger

from core.pdf_word_fit import page_groups

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

#: 格子底色和原页面颜色在任一通道上差这么多（0–255）才改。
COLOR_TOLERANCE = 48
#: 浅色字：亮度高于这个（0–1）。白底上的浅色字看不清，才给没底色的格子上色。
LIGHT_TEXT = 0.6
#: 找格子位置用的一段字最多取这么多个（不算空白）。
_SNIPPET_CHARS = 30
#: 整段找不到时只用开头这么多个字再找一次。
_SHORT_KEY_CHARS = 8
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


def _compact(text: str) -> str:
    """去掉空白、按 NFKC 统一写法：pdf2docx 常丢掉或多加词间空格（LaTeX 讲义的字体
    尤其如此），连字「ﬁ」在两边也可能写法不同；只比非空白的字才对得上。"""
    return "".join(unicodedata.normalize("NFKC", text).split())


def _keys(paragraphs) -> list[str]:
    """拿来在 PDF 里找格子位置的几段字：先用第一段开头，找不到再试后面几段。"""
    result = []
    for paragraph in paragraphs:
        text = _compact(_paragraph_text(paragraph))
        if len(text) >= _MIN_SNIPPET_CHARS:
            result.append(text[:_SNIPPET_CHARS])
        if len(result) == 3:
            break
    return result


class _PageChars:
    """一页 PDF 上的字，去掉空白后连成一串，每个字记着它所在的框和那一行的框。"""

    def __init__(self, page) -> None:
        self.text_parts: list[str] = []
        self.boxes: list[tuple] = []  # (字的框, 行的框)，和 text 一个字一项
        raw = page.get_text("rawdict")
        for block in raw.get("blocks", []):
            for line in block.get("lines", []):
                line_box = line["bbox"]
                for span in line.get("spans", []):
                    for char in span.get("chars", []):
                        for piece in _compact(char.get("c", "")):
                            self.text_parts.append(piece)
                            self.boxes.append((char["bbox"], line_box))
        self.text = "".join(self.text_parts)
        self.used: set[int] = set()

    def find(self, keys: list[str], fitz):
        """第一段能在这一页找到的字在第一行占的那一块；同样的字出现几次时取还没被别的
        格子用过的那一处。一段字整段找不到时（中间有上下标、换了顺序），再用它的开头试。"""
        for key in keys:
            for length in dict.fromkeys((len(key), min(len(key), _SHORT_KEY_CHARS))):
                part = key[:length]
                if len(part) < _MIN_SNIPPET_CHARS:
                    continue
                start = self.text.find(part)
                while start != -1 and start in self.used:
                    start = self.text.find(part, start + 1)
                if start == -1:
                    continue
                self.used.add(start)
                return self._first_line_rect(start, length, fitz)
        return None

    def _first_line_rect(self, start: int, length: int, fitz):
        first_line = self.boxes[start][1]
        rect = None
        for index in range(start, start + length):
            char_box, line_box = self.boxes[index]
            if line_box != first_line:
                break
            rect = fitz.Rect(char_box) if rect is None else rect | fitz.Rect(char_box)
        if rect is None:
            return None
        # 上下撑满这一行：只取字的框时太矮，细字的笔画会占多数
        return fitz.Rect(rect.x0, first_line[1], rect.x1, first_line[3])


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


def _table_of(tc):
    row = tc.getparent()
    return row.getparent() if row is not None else None


def _recolor_blank_cells(cells: list, table_changes: dict) -> int:
    """没字的格子找不到位置，但 pdf2docx 给它上的是和同一张表里有字的格子一样的错色
    （深色外层）：那几个有字的格子从这个颜色改成了什么，它也改成什么。多个颜色对不上
    或表里没有改过的格子时不动。"""
    changed = 0
    for tc in cells:
        shd, fill = _cell_fill(tc)
        if fill is None:
            continue
        replacement = table_changes.get(_table_of(tc), {}).get(fill)
        if replacement is None:
            continue
        _set_fill(tc, shd, replacement)
        changed += 1
    return changed


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
        # 同一张表里有字的格子从什么颜色改成了什么颜色：没字的格子照着改（见下）
        table_changes: dict = {}
        blank_cells: list = []
        for page_index, group in enumerate(groups):
            cells = [tc for element in group for tc in element.iter(f"{_W}tc")]
            if not cells:
                continue
            chars = _PageChars(pdf[page_index])
            for tc in cells:
                paragraphs = _own_paragraphs(tc)
                keys = _keys(paragraphs)
                if not keys:
                    blank_cells.append(tc)
                    continue
                # 每个有字的格子都按顺序占一处，同样的字出现几次时才对得上是哪一处
                rect = chars.find(keys, fitz)
                shd, fill = _cell_fill(tc)
                text_rgb = _text_color(paragraphs)
                if fill is None and (text_rgb is None or _luminance(text_rgb) <= LIGHT_TEXT):
                    continue  # 白底深色字：普通表格，不动
                if rect is None:
                    continue
                original = _background_color(pdf[page_index], rect, fitz)
                if original is None or not _differs(original, fill or (255, 255, 255)):
                    continue
                if fill is not None:
                    table_changes.setdefault(_table_of(tc), {})[fill] = original
                _set_fill(tc, shd, original)
                changed += 1
        changed += _recolor_blank_cells(blank_cells, table_changes)
    if changed:
        doc.save(str(docx_path))
        logger.info(f"[PDF] 按原页面颜色改正了 {changed} 个格子的底色")
    return changed
