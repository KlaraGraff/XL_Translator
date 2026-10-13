"""PDF 翻译方式（Beta 分支）：每一页走哪条路线。

四种处理：
- image   —— 整页图像翻译（原来的做法）：整页交给图像模型重新生成。
- inplace —— 原位替换（保留矢量）：在原 PDF 上删掉原文字形，按原位置写译文。
- word    —— 转成 Word 再翻译：连续的正文页转成 .docx，走 Word 翻译流程，再转回
             PDF 拼回原位；整份都是正文的文件直接输出 .docx。
- skip    —— 原样保留，不翻译。

设置只有两种：整页图像翻译（原来的做法），或「自动组合」——逐页按类型处理：
扫描页、正文页、图纸和大幅面页各有一个首选方式；首选在这一页用不了时（没有
图像模型、原位替换不支持目标语言、转 Word 失败、本机没有 Word……）改用「保底
方式」。

页面类型只看 PDF 本身（页面尺寸、可见文字、矢量密度），不调用模型。
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


@dataclass
class PageKind:
    page_number: int
    kind: str
    oversized: bool
    words: int = 0
    vector_items: int = 0
    slanted_items: int = 0


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


def classify_page(page, page_number: int, *, oversized: bool) -> PageKind:
    """page: a PyMuPDF page."""
    if oversized:
        return PageKind(page_number, KIND_DRAWING, True)
    visible = 0
    try:
        for span in page.get_texttrace():
            # 扫描件「可搜索 PDF」叠的隐形文字层（渲染模式 3）不算：看得见的字是像素。
            if span.get("type") != 3 and span.get("opacity", 1) > 0:
                visible += len(span.get("chars", ()))
    except Exception:  # noqa: BLE001 - unreadable text layer counts as none
        visible = 0
    if visible < _VISIBLE_MIN_CHARS:
        has_ink = bool(page.get_images(full=False)) or bool(page.get_drawings())
        return PageKind(page_number, KIND_SCAN if has_ink else KIND_BLANK, False)
    items = slanted = 0
    try:
        for path in page.get_drawings():
            for item in path["items"]:
                items += 1
                if item[0] == "c":
                    slanted += 1
                elif item[0] == "l" and abs(item[1].x - item[2].x) > 0.5 and abs(item[1].y - item[2].y) > 0.5:
                    slanted += 1
    except Exception:  # noqa: BLE001 - cannot read the vectors: judge by the text
        pass
    words = sum(1 for word in page.get_text("words") if any(ch.isalpha() for ch in word[4]))
    drawing = slanted >= _DRAWING_SLANTED_ITEMS or items >= _DRAWING_ITEMS
    return PageKind(page_number, KIND_DRAWING if drawing else KIND_TEXT, False, words, items, slanted)


def classify_document(path: str | Path, *, is_oversized) -> DocumentKind:
    """Sort one PDF into text document / drawing / scan / mixed.

    ``is_oversized(width_pt, height_pt)`` decides the large-format pages.
    """
    try:
        import fitz

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


def file_uses_word(pdf_settings, document: DocumentKind | None) -> bool:
    """Whole-file decision: only a pure text document is converted to Word."""
    if document is None or document.kind != KIND_TEXT:
        return False
    route = getattr(pdf_settings, "translation_route", ROUTE_IMAGE)
    return route == "auto" and getattr(pdf_settings, "auto_text_route", ROUTE_WORD) == ROUTE_WORD


def page_route(pdf_settings, *, oversized_a3: bool, oversized: bool, page_kind: str) -> str:
    """The preferred route of one page of a file that is handled page by page.

    oversized_a3: the fixed A3 test behind the original 「跳过 A3 及更大的页面」.
    oversized:    the user's large-format threshold (A3/A2/A1/A0).
    page_kind:    classify_page() of this page (text / drawing / scan / blank).

    Whether the preferred route can actually be used on this machine (image
    model set, target language, Word available...) is the runner's call; when it
    cannot, the page takes ``fallback_route``.
    """
    route = getattr(pdf_settings, "translation_route", ROUTE_IMAGE)
    if route == "auto":
        if oversized or page_kind == KIND_DRAWING:
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
