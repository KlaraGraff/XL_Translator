"""开始翻译前：所选 PDF 里有多少页要用「PDF 处理组件」，本机没装时这些页会怎样。

自动组合里，图纸和大幅面页首选原位替换，可提取文字的页首选转 Word，两者都要用「PDF
处理组件」。没装时这些页改用保底方式；A3 及以上的大幅面页不改用整页图像翻译，直接保留
原样（core.pdf_routes.fallback_route）。反馈 #33：一张 A3 总平面图因此一页也没翻，任务
跑完才说没有结果。这里在开始前按任务里同样的规则逐页算一遍，让开始按钮先把话说清楚。

页面类型用随安装包分发的 pypdfium2 判断（没装组件时任务里也是这样判断的），不渲染、
不调用模型。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from loguru import logger

from core.pdf_routes import (
    KIND_BLANK,
    ROUTE_IMAGE,
    ROUTE_INPLACE,
    ROUTE_WORD,
    classify_page_pdfium,
    exceeds_word_page,
    fallback_route,
    inplace_target_supported,
    page_route,
)

ROUTE_NEEDS_COMPONENT = (ROUTE_INPLACE, ROUTE_WORD)


@dataclass
class PdfComponentNeed:
    """pages：首选方式要用组件的页；kept_pages：其中没有别的方式可用、会保留原样的页。"""

    pages: int = 0
    kept_pages: int = 0
    files: list[str] = field(default_factory=list)
    kept_files: list[str] = field(default_factory=list)

    @property
    def needed(self) -> bool:
        return self.pages > 0


def pdf_component_need(
    items: Iterable[Any], pdf_settings: Any, *, image_available: bool
) -> PdfComponentNeed:
    """逐页算出要用组件的页。只看 PDF（图片文件不用组件）；读不了的文件跳过，任务里照常报错。"""
    need = PdfComponentNeed()
    if getattr(pdf_settings, "translation_route", "image") != "auto":
        return need
    inplace_ok = inplace_target_supported(str(getattr(pdf_settings, "target_lang", "") or ""))
    for item in items:
        if str(getattr(item, "source_type", "pdf") or "pdf") != "pdf":
            continue
        path = Path(getattr(item, "path", ""))
        name = str(getattr(item, "relative_path", "") or getattr(item, "name", "") or path.name)
        try:
            pages, kept = _file_need(path, pdf_settings, inplace_ok=inplace_ok, image_available=image_available)
        except Exception as exc:  # noqa: BLE001 - 读不了的文件留给任务本身去报
            logger.debug(f"[PDF 处理组件] 开始前判断页面类型失败（{path.name}）：{exc!r}")
            continue
        if pages:
            need.pages += pages
            need.files.append(name)
        if kept:
            need.kept_pages += kept
            need.kept_files.append(name)
    return need


def _file_need(path: Path, pdf_settings: Any, *, inplace_ok: bool, image_available: bool) -> tuple[int, int]:
    import pypdfium2 as pdfium

    # 跟任务里的判断一致（core.pdf_image_translation._decide_page_route）。
    from core.pdf_image_translation import is_oversized_page

    pages = kept = 0
    doc = pdfium.PdfDocument(str(path))
    try:
        for index in range(len(doc)):
            page = doc[index]
            try:
                width, height = page.get_size()
                kind = classify_page_pdfium(page, index + 1, oversized=exceeds_word_page(width, height)).kind
            finally:
                page.close()
            if kind == KIND_BLANK:
                continue
            oversized_a3 = is_oversized_page(width, height)
            route = page_route(
                pdf_settings,
                oversized_a3=oversized_a3,
                oversized=exceeds_word_page(width, height),
                page_kind=kind,
            )
            if route not in ROUTE_NEEDS_COMPONENT:
                continue
            if route == ROUTE_INPLACE and not inplace_ok:
                # 目标语言原位替换写不了：装了组件也一样改用保底方式，不算在组件头上。
                continue
            pages += 1
            if fallback_route(pdf_settings, oversized=oversized_a3, image_available=image_available) != ROUTE_IMAGE:
                kept += 1
    finally:
        doc.close()
    return pages, kept
