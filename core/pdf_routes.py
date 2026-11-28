"""PDF 翻译方式（Beta 分支）：每一页走哪条路线。

五种处理：
- image   —— 整页图像翻译（原来的做法）：整页交给图像模型重新生成。
- babeldoc —— 段落重排（「段落重排组件」BabelDOC）：在原 PDF 上按段落排入译文，公式、图、
             表格线原样保留；逐页检查不过的页改走 word。
- inplace —— 原位替换（保留矢量）：在原 PDF 上删掉原文字形，按原位置写译文。
- word    —— 转成 Word 再翻译：连续几页可提取文字的页转成 .docx，走 Word 翻译
             流程，再转回 PDF 拼回原位；输出始终是 PDF（可另存一份 Word）。
- skip    —— 原样保留，不翻译。

设置只有两种（maple 2026-11-28）：「整页图像翻译」，或「按页自动选择」——程序逐页按类型选方式，
用户不再挑：可提取文字的页段落重排（没装组件、没通过检查的页转 Word），图纸和大幅面页原位
替换，扫描页整页图像翻译。首选在这一页用不了时（没有图像模型、原位替换不支持目标语言、转
Word 失败、本机没有 Word……）程序自己选保底：能用整页图像翻译就用，否则原样保留。

页面类型只看 PDF 本身（可见文字、矢量密度、页面尺寸），不调用模型。先看内容再看尺寸
（maple 2026-10-21）：扫描页不论多大都是扫描页；可提取文字的页放得进 Word 就按文字页
处理；线条密集、字都是零散标注的图纸页，以及大到放不进 Word 的文字页，才算「图纸和
大图」。成段正文配一张插图的页线条再密也是文字页（_PROSE_MIN_SHARE，反馈 #34）。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

ROUTE_IMAGE = "image"
ROUTE_INPLACE = "inplace"
ROUTE_WORD = "word"
# 段落重排（BabelDOC，见 core/pdf_babeldoc_pages.py）：可提取文字的页在原 PDF 上按段落排入译文；
# 没装组件或没通过逐页检查的页改走转 Word。
ROUTE_BABELDOC = "babeldoc"
ROUTE_SKIP = "skip"

KIND_TEXT = "text"          # 文字文档
KIND_DRAWING = "drawing"    # 图纸（大幅面，或字都是零散短标签）
KIND_SCAN = "scan"          # 扫描件（取不到可见文字）
KIND_MIXED = "mixed"
KIND_BLANK = "blank"

# 小页面上的图纸：CAD 出的图有成千上万段斜线和曲线（尺寸、填充、圆弧、符号），
# 文字文档只有表格线、下划线，加上 logo 也不过几十段。
_DRAWING_SLANTED_ITEMS = 1000
_DRAWING_ITEMS = 8000
_VISIBLE_MIN_CHARS = 20
# 一页正文配一张插图（流程图、函数曲线）：插图自己就有上千段曲线，只数线条会把整页判成
# 图纸，选了「图纸和大图：原样保留」时整页正文都不翻（反馈 #34，每页约 300 词正文配一张
# 1000 来段曲线的流程图）。图纸上的字是零散的短标注，说明栏那几行长字在整张图上只占一小块；
# 正文页的正文成段成行，占掉页面一大块。所以线条再密，正文行占到页面 15% 的页仍是文字页。
# 正文行：一行至少 20 个字母或汉字（「FIRE DOOR FD-1」「LEVEL +3.500」这类标注都不够）；
# 面积按行长 × 字号算。自造样例实测：165×242 mm 页配流程图，正文 300 词 25%、200 词约 16%、
# 150 词 12%；整页流程图配三行图注 2%；A4、A3 图纸带 15–30 行总说明 4–5%，说明写满整个
# 右栏（40 行）10–12%（pypdfium2 会把同一行上的几个标注连成一段，量得略多）。
_PROSE_LINE_MIN_LETTERS = 20
_PROSE_MIN_SHARE = 0.15
# 整页大图上叠着表格线的页（地勘报告的钻孔柱状图：一张位图铺满大半页，表头和分栏线
# 画在图上）：pdf2docx 把这张图往表格的每个格子里各复制一遍，一页转出几十页
# （maple 2026-10-31，322 页的报告转回 2581 页）。图上的字都在位图里，转 Word 也翻
# 不到。实测 60 页柱状图大图占 44–64%、压在图上的直线 41–152 条；正常页的大图上
# 至多零星几条线（个别页 102 条，误判只是改走保底方式）。
_IMAGE_GRID_MIN_AREA = 0.4
_IMAGE_GRID_MIN_LINES = 30
# Word 的页面最大 22 英寸（55.88 cm）。长边超过它的文字页转不了 Word，按图纸处理。
WORD_MAX_PAGE_EDGE_PT = 22 * 72


#: 原位替换写不了这种目标语言时，页面改用保底方式的原因。
INPLACE_TARGET_UNSUPPORTED = "原位替换暂不支持该目标语言（文字需连写排版）"


def inplace_target_supported(target_lang: str) -> bool:
    """原位替换能不能译成这种语言：阿拉伯文、泰文、印度各文字等要连写或从右往左排的不能
    （见 core.pdf_vector.fonts）；中文、日文、韩文和用拉丁、西里尔、希腊字母的都可以。"""
    from core.pdf_vector.fonts import target_supported

    return target_supported(str(target_lang or ""))


def exceeds_word_page(width_pt: float, height_pt: float) -> bool:
    """这一页放不进 Word 的页面（A2 及更大的页都放不下）。"""
    return max(float(width_pt), float(height_pt)) > WORD_MAX_PAGE_EDGE_PT + 0.5


@dataclass
class PageKind:
    page_number: int
    kind: str
    oversized: bool
    words: int = 0
    vector_items: int = 0
    slanted_items: int = 0
    # 文字页里「整页大图上叠着表格线」的页：不转 Word（见 _IMAGE_GRID_MIN_AREA）。
    image_grid: bool = False
    # 正文行占页面的比例（见 _PROSE_MIN_SHARE）；只在线条密到够判图纸、又放得进 Word 的页上算。
    prose_share: float = 0.0


@dataclass
class DocumentKind:
    kind: str
    pages: list[PageKind] = field(default_factory=list)
    error: str = ""

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def count(self, kind: str) -> int:
        return sum(1 for page in self.pages if page.kind == kind)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _dense_lines(items: int, slanted: int) -> bool:
    """线条密到像图纸（还要看正文占多少，见 _PROSE_MIN_SHARE）。"""
    return slanted >= _DRAWING_SLANTED_ITEMS or items >= _DRAWING_ITEMS


def prose_share_of(lines, page_area: float) -> float:
    """正文行占页面的比例。lines: (这一行的字母和汉字数, 这一行的面积)。"""
    if page_area <= 0:
        return 0.0
    area = sum(line_area for letters, line_area in lines if letters >= _PROSE_LINE_MIN_LETTERS)
    return min(1.0, max(0.0, area / page_area))


def _kind_from_counts(page_number: int, *, oversized: bool, visible: int, has_ink: bool,
                     words: int, items: int, slanted: int, prose_share: float = 0.0) -> PageKind:
    """Content first, size second: see the module docstring."""
    dense = _dense_lines(items, slanted)
    if visible < _VISIBLE_MIN_CHARS:
        # 没有看得见的文字：字画成线条的图纸（矢量密）还是图纸，其余是扫描件或空白页。
        if dense:
            return PageKind(page_number, KIND_DRAWING, oversized, words, items, slanted)
        return PageKind(page_number, KIND_SCAN if has_ink else KIND_BLANK, oversized, words, items, slanted)
    # 线条密、正文又少的才是图纸；成段正文配一张插图的页还是文字页（见 _PROSE_MIN_SHARE）。
    drawing = dense and prose_share < _PROSE_MIN_SHARE
    kind = KIND_DRAWING if drawing or oversized else KIND_TEXT
    return PageKind(page_number, kind, oversized, words, items, slanted, prose_share=prose_share)


def kept_by_setting_reason(*, short_mm: float, long_mm: float) -> str:
    """按设置原样保留的页（不是出了问题）：只剩整页图像翻译开着「跳过 A3 及更大的页面」这一种。

    按页自动选择没有「这类页原样保留」的选项（2026-11-28）：它保留的页都是用不了的（kept_reason）
    或空白页（blank_page），各有说明。"""
    return f"为大幅面（{short_mm:.0f}×{long_mm:.0f} mm），已开启「跳过 A3 及更大的页面」"


def needs_ocr(page: PageKind) -> bool:
    """原位替换这一页要不要跑文字识别（找画成线条、嵌在图片里的字）。

    能直接读到文字、线条也不密的页（文字页，或只因为放不进 Word 才算图纸的大页面）不跑：
    BitCloud E-001 这类大图纸上识别只认出符号乱码和公司标志，却要在慢电脑上多等几分钟
    （maple 2026-10-21）。线条密的图纸常有画成线条的字，读不到文字的页只能靠识别。
    """
    if page.kind == KIND_TEXT:
        return False
    dense = _dense_lines(page.vector_items, page.slanted_items)
    return not (page.kind == KIND_DRAWING and page.words > 0 and not dense)


def classify_page(page, page_number: int, *, oversized: bool) -> PageKind:
    """page: a PyMuPDF page.  ``oversized``: too large for a Word page (exceeds_word_page)."""
    visible = 0
    try:
        for span in page.get_texttrace():
            # 扫描件「可搜索 PDF」叠的隐形文字层（渲染模式 3）不算：看得见的字是像素。
            if span.get("type") != 3 and span.get("opacity", 1) > 0:
                visible += len(span.get("chars", ()))
    except Exception:  # noqa: BLE001 - unreadable text layer counts as none
        visible = 0
    items = slanted = 0
    drawings = []
    try:
        drawings = page.get_drawings()
        for path in drawings:
            for item in path["items"]:
                items += 1
                if item[0] == "c":
                    slanted += 1
                elif item[0] == "l" and abs(item[1].x - item[2].x) > 0.5 and abs(item[1].y - item[2].y) > 0.5:
                    slanted += 1
            if items >= _DRAWING_ITEMS or slanted >= _DRAWING_SLANTED_ITEMS:
                break  # 已经够判成图纸了，大图纸不必数完
    except Exception as exc:  # noqa: BLE001 - cannot read the vectors: judge by the text
        logger.debug(f"读取页面矢量线条失败，已忽略：{exc!r}")
        pass
    has_ink = bool(drawings)
    image_grid = visible >= _VISIBLE_MIN_CHARS and _lines_over_big_image(page, drawings) >= _IMAGE_GRID_MIN_LINES
    if visible < _VISIBLE_MIN_CHARS and not has_ink:
        try:
            has_ink = bool(page.get_images(full=False))
        except Exception:  # noqa: BLE001
            has_ink = False
    words = (sum(1 for word in page.get_text("words") if any(ch.isalpha() for ch in word[4]))
             if visible >= _VISIBLE_MIN_CHARS else 0)
    prose_share = 0.0
    if words and not oversized and _dense_lines(items, slanted):
        prose_share = prose_share_of(_text_lines(page), abs(page.rect))
    kind = _kind_from_counts(page_number, oversized=oversized, visible=visible, has_ink=has_ink,
                             words=words, items=items, slanted=slanted, prose_share=prose_share)
    kind.image_grid = image_grid and kind.kind == KIND_TEXT
    return kind


def _text_lines(page) -> list[tuple[int, float]]:
    """PyMuPDF 这一页每行文字：(字母和汉字数, 行宽 × 字号)。面积按字号算，不按字体自带的
    行高：各家字体的行高差得多，pdfium 那边量出来的也不一样（见 classify_page_pdfium）。"""
    lines: list[tuple[int, float]] = []
    for block in page.get_text("dict").get("blocks", ()):
        for line in block.get("lines", ()):
            spans = line.get("spans", ())
            letters = sum(1 for span in spans for ch in str(span.get("text", "")) if ch.isalpha())
            size = max((float(span.get("size", 0) or 0) for span in spans), default=0.0)
            x0, y0, x1, y1 = line.get("bbox", (0, 0, 0, 0))
            dir_x, dir_y = line.get("dir", (1, 0))
            # 竖排的行（dir 朝下）取高当行长
            length = abs(x1 - x0) if abs(dir_x) >= abs(dir_y) else abs(y1 - y0)
            lines.append((letters, length * size))
    return lines


def _lines_over_big_image(page, drawings: list) -> int:
    """页上最大的一张图占页面 ≥40% 时，压在这张图上的直线有几条；没有这样的图返回 0。

    只数直线（"l"）：柱状图的表头、分栏线都是直线；矩形是正文表格自己的格子。"""
    page_area = abs(page.rect)
    if page_area <= 0:
        return 0
    try:
        boxes = [page.rect & item["bbox"] for item in page.get_image_info()]
    except Exception:  # noqa: BLE001 - unreadable images: nothing to judge by
        return 0
    boxes = [box for box in boxes if not box.is_empty]
    if not boxes:
        return 0
    big = max(boxes, key=abs)
    if abs(big) < _IMAGE_GRID_MIN_AREA * page_area:
        return 0
    lines = 0
    for path in drawings:
        for item in path["items"]:
            if item[0] == "l" and big.contains((item[1] + item[2]) * 0.5):
                lines += 1
    return lines


def classify_page_pdfium(page, page_number: int, *, oversized: bool) -> PageKind:
    """没装「PDF 处理组件」时用随安装包分发的 pypdfium2 判断，规则同 classify_page。

    计数口径和 PyMuPDF 不完全一样（矩形按几条边计），但只用来分扫描页、空白页、可提取
    文字的页和图纸：没有组件时后两种都改用保底方式，差别不影响结果。
    """
    import ctypes

    import pypdfium2.raw as pdfium_c

    has_ink = False
    visible = items = slanted = curve_points = 0
    x, y = ctypes.c_float(), ctypes.c_float()
    textpage = page.get_textpage()
    try:
        for obj in page.get_objects(max_depth=8):
            kind = obj.type
            if kind == pdfium_c.FPDF_PAGEOBJ_FORM:
                continue
            if kind == pdfium_c.FPDF_PAGEOBJ_TEXT:
                # 扫描件「可搜索 PDF」叠的隐形文字层不算：看得见的字是像素。
                if pdfium_c.FPDFTextObj_GetTextRenderMode(obj.raw) != pdfium_c.FPDF_TEXTRENDERMODE_INVISIBLE:
                    # 返回的是 UTF-16 字节数，含结尾的两个零字节。
                    size = pdfium_c.FPDFTextObj_GetText(obj.raw, textpage.raw, None, 0)
                    visible += max(0, (int(size) - 2) // 2)
                continue
            has_ink = True
            if kind != pdfium_c.FPDF_PAGEOBJ_PATH or items >= _DRAWING_ITEMS or slanted >= _DRAWING_SLANTED_ITEMS:
                continue
            last = None
            for index in range(pdfium_c.FPDFPath_CountSegments(obj.raw)):
                segment = pdfium_c.FPDFPath_GetPathSegment(obj.raw, index)
                if not segment or not pdfium_c.FPDFPathSegment_GetPoint(segment, ctypes.byref(x), ctypes.byref(y)):
                    continue
                point = (x.value, y.value)
                seg_type = pdfium_c.FPDFPathSegment_GetType(segment)
                if seg_type == pdfium_c.FPDF_SEGMENT_LINETO:
                    items += 1
                    if last is not None and abs(point[0] - last[0]) > 0.5 and abs(point[1] - last[1]) > 0.5:
                        slanted += 1
                elif seg_type == pdfium_c.FPDF_SEGMENT_BEZIERTO:
                    curve_points += 1
                    if curve_points % 3 == 0:  # 一段曲线三个点
                        items += 1
                        slanted += 1
                last = point
        text = textpage.get_text_bounded() if visible >= _VISIBLE_MIN_CHARS else ""
        prose_share = 0.0
        if text and not oversized and _dense_lines(items, slanted):
            # 一行文字一个矩形；高按这行中间那个字的字号算（loose box 的高就是字号），与 PyMuPDF 同口径。
            lines = []
            box = pdfium_c.FS_RECTF()
            for index in range(textpage.count_rects()):
                left, bottom, right, top = textpage.get_rect(index)
                letters = sum(1 for ch in textpage.get_text_bounded(left, bottom, right, top) if ch.isalpha())
                char = pdfium_c.FPDFText_GetCharIndexAtPos(textpage.raw, (left + right) / 2, (bottom + top) / 2,
                                                           abs(right - left) / 2 + 1, abs(top - bottom) / 2 + 1)
                size = abs(top - bottom)
                if char >= 0 and pdfium_c.FPDFText_GetLooseCharBox(textpage.raw, char, ctypes.byref(box)):
                    size = abs(box.top - box.bottom) or size
                lines.append((letters, max(0.0, right - left) * size))
            width, height = page.get_size()
            prose_share = prose_share_of(lines, float(width) * float(height))
    finally:
        textpage.close()
    words = sum(1 for token in (text or "").split() if any(ch.isalpha() for ch in token))
    return _kind_from_counts(page_number, oversized=oversized, visible=visible, has_ink=has_ink,
                             words=words, items=items, slanted=slanted, prose_share=prose_share)


def _classify_with_pdfium(path: str | Path, *, is_oversized) -> list[PageKind]:
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(str(path))
    try:
        pages: list[PageKind] = []
        for index in range(len(doc)):
            page = doc[index]
            try:
                width, height = page.get_size()
                pages.append(classify_page_pdfium(page, index + 1, oversized=is_oversized(width, height)))
            finally:
                page.close()
        return pages
    finally:
        doc.close()


def classify_document(path: str | Path, *, is_oversized) -> DocumentKind:
    """Sort one PDF into text document / drawing / scan / mixed.

    ``is_oversized(width_pt, height_pt)``: pages too large for Word (text there counts as a drawing).
    """
    from core.pdf_component import is_available, require_fitz

    try:
        if not is_available(refresh=True):
            pages = _classify_with_pdfium(path, is_oversized=is_oversized)
        else:
            fitz = require_fitz()
            with fitz.open(path) as doc:
                pages = [
                    classify_page(page, index + 1, oversized=is_oversized(page.rect.width, page.rect.height))
                    for index, page in enumerate(doc)
                ]
    except Exception as exc:  # noqa: BLE001 - unreadable: let the page route handle it
        return DocumentKind(KIND_MIXED, [], error=str(exc))
    kinds = {page.kind for page in pages if page.kind != KIND_BLANK}
    if not kinds:
        kind = KIND_SCAN
    elif len(kinds) == 1:
        kind = next(iter(kinds))
    else:
        kind = KIND_MIXED
    return DocumentKind(kind, pages)


def page_route(pdf_settings, *, oversized_a3: bool, oversized: bool, page_kind: str) -> str:
    """The preferred route of one page of a file that is handled page by page.

    oversized_a3: the fixed A3 test behind the original 「跳过 A3 及更大的页面」.
    oversized:    too large for a Word page (exceeds_word_page).
    page_kind:    classify_page() of this page (text / drawing / scan / blank).

    Whether the preferred route can actually be used on this machine (image
    model set, target language, Word available...) is the runner's call; when it
    cannot, the page takes ``fallback_route``.
    """
    route = getattr(pdf_settings, "translation_route", ROUTE_IMAGE)
    if route == "auto":
        # 先看内容再看尺寸：扫描页不论多大都按扫描页；文字页放不进 Word 才按图纸。
        if page_kind == KIND_DRAWING or (page_kind == KIND_TEXT and oversized):
            return ROUTE_INPLACE
        if page_kind == KIND_TEXT:
            # 没装段落重排组件、没通过逐页检查的页由 runner 改走转 Word（core/pdf_babeldoc_pages.py）。
            return ROUTE_BABELDOC
        if page_kind == KIND_SCAN:
            return ROUTE_IMAGE
        return ROUTE_SKIP  # 空白页：原样放进去，不花钱
    # image: the original behaviour
    if oversized_a3 and bool(getattr(pdf_settings, "skip_oversized_pages", True)):
        return ROUTE_SKIP
    return ROUTE_IMAGE


def fallback_route(*, oversized: bool, image_available: bool) -> str:
    """保底：首选用不了时，能用整页图像翻译就用；没有图像模型，或是大幅面页（会画糊），保留原样。"""
    if image_available and not oversized:
        return ROUTE_IMAGE
    return ROUTE_SKIP


@dataclass(frozen=True)
class PageKindCounts:
    """按页自动选择会怎样处理一份 PDF：各类页几页（空白页不算）。图纸含放不进 Word 的大页，
    与 page_route 同一口径。"""

    text: int = 0
    drawing: int = 0
    scan: int = 0


_PAGE_KIND_CACHE: dict[tuple[str, int, int], PageKindCounts] = {}
_PAGE_KIND_CACHE_MAX = 256


def count_page_kinds(path: str | Path) -> PageKindCounts:
    """逐页分类，数出文字页、图纸、扫描页。用随安装包分发的 pypdfium2（没装组件时任务里也是
    这样判断的），不渲染、不调用模型。同一份文件（路径、大小、修改时间都没变）只算一次。
    读不了时抛异常，由调用方决定怎么说。"""
    import pypdfium2 as pdfium

    file = Path(path)
    stat = file.stat()
    key = (str(file), stat.st_size, stat.st_mtime_ns)
    cached = _PAGE_KIND_CACHE.get(key)
    if cached is not None:
        return cached
    text = drawing = scan = 0
    doc = pdfium.PdfDocument(str(file))
    try:
        for index in range(len(doc)):
            page = doc[index]
            try:
                width, height = page.get_size()
                oversized = exceeds_word_page(width, height)
                kind = classify_page_pdfium(page, index + 1, oversized=oversized).kind
            finally:
                page.close()
            if kind == KIND_DRAWING or (kind == KIND_TEXT and oversized):
                drawing += 1
            elif kind == KIND_TEXT:
                text += 1
            elif kind == KIND_SCAN:
                scan += 1
    finally:
        doc.close()
    counts = PageKindCounts(text=text, drawing=drawing, scan=scan)
    if len(_PAGE_KIND_CACHE) >= _PAGE_KIND_CACHE_MAX:
        _PAGE_KIND_CACHE.pop(next(iter(_PAGE_KIND_CACHE)))
    _PAGE_KIND_CACHE[key] = counts
    return counts
