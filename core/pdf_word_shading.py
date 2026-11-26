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
#: 没字的深色格子至少这么高（1/20 磅，约一行字）才当成框的一块；更矮的可能是一条线。
_MIN_BLOCK_HEIGHT = 200
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


def _only_choice(choices: list | None):
    """几个格子学到的颜色都差不多（取色时边上差一两个色阶）就用最多的那个；差得明显
    （同一种错色有的该改成浅蓝、有的该改成白）就不猜。"""
    if not choices:
        return None
    first = choices[0]
    if any(_differs(first, other) for other in choices):
        return None
    return Counter(choices).most_common(1)[0][0]


class _Learned:
    """有字的格子找到位置后学到的「pdf2docx 给的颜色 → 原页面的颜色」。

    找不到位置的格子（「T」「F」这种短字、真值表一格一列、没字的格子）照着改：
    先看同一张表里有没有同色的格子找到了位置（标题条上空着的那一格跟着标题条，
    不会被改浅）；同一张表里没有，再看同一页、最后看整份文件，深色字压在这个颜色
    上的格子是不是都改成了同一个颜色。"""

    def __init__(self) -> None:
        self.by_table: dict = {}
        self.by_page: dict = {}
        self.overall: dict = {}

    def add(self, tc, page_index: int, fill, original, dark_text: bool) -> None:
        self.by_table.setdefault(_table_of(tc), {}).setdefault(fill, []).append((original, dark_text))
        if dark_text:
            self.by_page.setdefault(page_index, {}).setdefault(fill, []).append(original)
            self.overall.setdefault(fill, []).append(original)

    def guess(self, tc, page_index: int, fill, has_text: bool):
        in_table = self.by_table.get(_table_of(tc), {}).get(fill, [])
        if not has_text:
            if in_table:
                return _only_choice([original for original, _dark in in_table])
            if _row_height(tc) < _MIN_BLOCK_HEIGHT:
                return None  # 没字的矮格子：可能就是一条深色的线，不动
        else:
            dark_in_table = [original for original, dark in in_table if dark]
            if dark_in_table:
                return _only_choice(dark_in_table)
        # 没字的一大块（框跨页时留在这一页顶上的空白部分）、找不到位置的短字：
        # 同一页、再到整份文件里这种错色都改成了什么
        return (_only_choice(self.by_page.get(page_index, {}).get(fill))
                or _only_choice(self.overall.get(fill)))


def _row_height(tc) -> int:
    row = tc.getparent()
    height = row.find(f"{_W}trPr/{_W}trHeight") if row is not None else None
    try:
        return int(float(height.get(f"{_W}val"))) if height is not None else 0
    except (TypeError, ValueError):
        return 0


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
        learned = _Learned()
        unresolved: list = []  # (页序, 格子, 有没有字)
        for page_index, group in enumerate(groups):
            cells = [tc for element in group for tc in element.iter(f"{_W}tc")]
            if not cells:
                continue
            chars = _PageChars(pdf[page_index])
            for tc in cells:
                paragraphs = _own_paragraphs(tc)
                keys = _keys(paragraphs)
                shd, fill = _cell_fill(tc)
                text_rgb = _text_color(paragraphs)
                dark_text = text_rgb is None or _luminance(text_rgb) <= LIGHT_TEXT
                # 每个有字的格子都按顺序占一处，同样的字出现几次时才对得上是哪一处
                rect = chars.find(keys, fitz) if keys else None
                if fill is None and dark_text:
                    continue  # 白底深色字：普通表格，不动
                original = _background_color(pdf[page_index], rect, fitz) if rect is not None else None
                if original is None:
                    if fill is not None and dark_text:  # 没字的格子也在这里（text_rgb 为 None）
                        # 自己没字、里面套着表格的格子（框里的真值表）：里面的字也压在它的底色上
                        has_text = bool("".join(node.text or "" for node in tc.iter(f"{_W}t")).strip())
                        unresolved.append((page_index, tc, has_text))
                    continue
                if fill is not None:
                    learned.add(tc, page_index, fill, original, dark_text)
                if not _differs(original, fill or (255, 255, 255)):
                    continue
                _set_fill(tc, shd, original)
                changed += 1
        for page_index, tc, has_text in unresolved:
            shd, fill = _cell_fill(tc)
            replacement = learned.guess(tc, page_index, fill, has_text)
            if replacement is None or fill is None or not _differs(replacement, fill):
                continue
            _set_fill(tc, shd, replacement)
            changed += 1
    if changed:
        doc.save(str(docx_path))
        logger.info(f"[PDF] 按原页面颜色改正了 {changed} 个格子的底色")
    return changed
