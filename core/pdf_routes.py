"""PDF 翻译方式（Beta 分支）：每一页走哪条路线。

四种处理：
- image   —— 整页图像翻译（原来的做法）：整页交给图像模型重新生成。
- inplace —— 原位替换（保留矢量）：在原 PDF 上删掉原文字形，按原位置写译文。
- word    —— 转成 Word 再翻译：连续几页可提取文字的页转成 .docx，走 Word 翻译
             流程，再转回 PDF 拼回原位；输出始终是 PDF（可另存一份 Word）。
- skip    —— 原样保留，不翻译。

设置只有两种：整页图像翻译（原来的做法），或「自动组合」——逐页按类型处理：
扫描页、可提取文字的页、图纸和大幅面页各有一个首选方式；首选在这一页用不了时（没有
图像模型、原位替换不支持目标语言、转 Word 失败、本机没有 Word……）改用「保底
方式」。

页面类型只看 PDF 本身（可见文字、矢量密度、页面尺寸），不调用模型。先看内容再看尺寸
（maple 2026-10-21）：扫描页不论多大都是扫描页；可提取文字的页放得进 Word 就按文字页
处理；线条密集的图纸页，以及大到放不进 Word 的文字页，才算「图纸和大图」。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ROUTE_IMAGE = "image"
ROUTE_INPLACE = "inplace"
ROUTE_WORD = "word"
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


def _kind_from_counts(page_number: int, *, oversized: bool, visible: int, has_ink: bool,
                     words: int, items: int, slanted: int) -> PageKind:
    """Content first, size second: see the module docstring."""
    drawing = slanted >= _DRAWING_SLANTED_ITEMS or items >= _DRAWING_ITEMS
    if visible < _VISIBLE_MIN_CHARS:
        # 没有看得见的文字：字画成线条的图纸（矢量密）还是图纸，其余是扫描件或空白页。
        if drawing:
            return PageKind(page_number, KIND_DRAWING, oversized, words, items, slanted)
        return PageKind(page_number, KIND_SCAN if has_ink else KIND_BLANK, oversized, words, items, slanted)
    kind = KIND_DRAWING if drawing or oversized else KIND_TEXT
    return PageKind(page_number, kind, oversized, words, items, slanted)


def needs_ocr(page: PageKind) -> bool:
    """原位替换这一页要不要跑文字识别（找画成线条、嵌在图片里的字）。

    能直接读到文字、线条也不密的页（文字页，或只因为放不进 Word 才算图纸的大页面）不跑：
    BitCloud E-001 这类大图纸上识别只认出符号乱码和公司标志，却要在慢电脑上多等几分钟
    （maple 2026-10-21）。线条密的图纸常有画成线条的字，读不到文字的页只能靠识别。
    """
    if page.kind == KIND_TEXT:
        return False
    dense = page.slanted_items >= _DRAWING_SLANTED_ITEMS or page.vector_items >= _DRAWING_ITEMS
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
    except Exception:  # noqa: BLE001 - cannot read the vectors: judge by the text
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
    kind = _kind_from_counts(page_number, oversized=oversized, visible=visible, has_ink=has_ink,
                             words=words, items=items, slanted=slanted)
    kind.image_grid = image_grid and kind.kind == KIND_TEXT
    return kind


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
    finally:
        textpage.close()
    words = sum(1 for token in (text or "").split() if any(ch.isalpha() for ch in token))
    return _kind_from_counts(page_number, oversized=oversized, visible=visible, has_ink=has_ink,
                             words=words, items=items, slanted=slanted)


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
            return getattr(pdf_settings, "auto_drawing_route", ROUTE_INPLACE)
        if page_kind == KIND_TEXT:
            return getattr(pdf_settings, "auto_text_route", ROUTE_WORD)
        if page_kind == KIND_SCAN:
            return getattr(pdf_settings, "auto_scan_route", ROUTE_IMAGE)
        return ROUTE_SKIP  # 空白页：原样放进去，不花钱
    # image: the original behaviour
    if oversized_a3 and bool(getattr(pdf_settings, "skip_oversized_pages", True)):
        return ROUTE_SKIP
    return ROUTE_IMAGE


def fallback_route(pdf_settings, *, oversized: bool, image_available: bool) -> str:
    """「保底方式」：首选用不了时。大幅面页不改用整页图像翻译（会画糊），保留原样。"""
    if (getattr(pdf_settings, "auto_fallback", ROUTE_IMAGE) == ROUTE_IMAGE
            and image_available and not oversized):
        return ROUTE_IMAGE
    return ROUTE_SKIP


def needs_image_model(pdf_settings) -> bool:
    """Whether a task with these settings can run without an image model at all.

    Only the single routes that never fall back need no image model; anything
    else may send a page to the image model, but the task still runs: pages
    that need it are kept as they are when no image model is set.
    """
    return getattr(pdf_settings, "translation_route", ROUTE_IMAGE) == ROUTE_IMAGE
