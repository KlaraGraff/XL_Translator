"""转 Word 的正文段：pdf2docx 转坏了的页，以及转回 PDF 后页数暴增的段。

maple 2026-10-31 的地勘报告：322 页整本转 Word，转回 2581 页，页码全部错开。两种转坏：
- 钻孔柱状图（一张大图上叠着表格线）：pdf2docx 把整张图往表格的每个格子里各塞一份，
  一页里同一张图出现 360–564 次，转回 PDF 一页变成几十页；
- 岩芯图：拆成一千多个浮动碎块（wp:anchor），在 Word 里叠成几页残线。
正常页里同一张图至多出现 4 次，浮动块至多 21 个。

第一种多数在转换前就认得出来（core.pdf_routes 的 image_grid）；这里在转完、翻译之前逐页
再查一次，查出的页抽出来按保底方式处理，其余页重新转。最后一道：整段转回的 PDF 页数
比原文多出太多，就不拼进成品。
"""
from __future__ import annotations

import zipfile
from collections import Counter
from pathlib import Path

from loguru import logger

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A_BLIP = "{http://schemas.openxmlformats.org/drawingml/2006/main}blip"
_V_IMAGEDATA = "{urn:schemas-microsoft-com:vml}imagedata"
_WP_ANCHOR = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}anchor"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

#: 一页里同一张图被引用超过这么多次，算转坏了（正常页 ≤4，转坏的页 ≥360）。
MAX_SAME_IMAGE_PER_PAGE = 50
#: 一页里的浮动块超过这么多个，算转坏了（正常页 ≤21，转坏的页 ≥1011）。
MAX_ANCHORS_PER_PAGE = 300
#: 转回的 PDF 最多可以比原文多几页：中文换行后表格、照片挤到下一页是常事
#: （实测 260 页正文转回 292 页），多出一半以上就是转坏了。
SEGMENT_PAGE_GROWTH = 1.5
SEGMENT_PAGE_SLACK = 3

#: 页面路线说明里的原因（后面接「，改用整页图像翻译」）。界面按原文查 i18n 词条。
REASON_IMAGE_GRID = "整页图片上叠有表格线，不适合转成 Word"
REASON_BROKEN_PAGE = "转成 Word 后版面出错"
REASON_PAGE_GROWTH = "转回的 PDF 页数比原文多出太多"


def _section_starts_page(sect_pr) -> bool:
    kind = sect_pr.find(f"{_W}type")
    value = kind.get(f"{_W}val") if kind is not None else "nextPage"
    return value in ("nextPage", "oddPage", "evenPage")


def docx_page_stats(docx_path: Path) -> list[tuple[int, int]]:
    """转出的 .docx 每一页的（同一张图最多被引用几次, 浮动块个数）。

    pdf2docx 每页开一个「新页」分节，页内的分栏是「连续」分节；分节的开始方式记在
    结束这一节的 sectPr 里。逐个顶层块流式读，166 MB 的文件也不整份载入。"""
    sections: list[tuple[bool, Counter, int]] = []
    refs: Counter = Counter()
    anchors = 0
    with zipfile.ZipFile(docx_path) as archive, archive.open("word/document.xml") as stream:
        from lxml import etree

        for _event, element in etree.iterparse(stream, events=("end",)):
            parent = element.getparent()
            if parent is None or parent.tag != f"{_W}body":
                continue
            for blip in element.iter(_A_BLIP):
                refs[blip.get(f"{_R}embed") or blip.get(f"{_R}link") or ""] += 1
            for image in element.iter(_V_IMAGEDATA):
                refs[image.get(f"{_R}id") or ""] += 1
            anchors += sum(1 for _ in element.iter(_WP_ANCHOR))
            sect_pr = element if element.tag == f"{_W}sectPr" else element.find(f"{_W}pPr/{_W}sectPr")
            if sect_pr is not None:
                sections.append((_section_starts_page(sect_pr), refs, anchors))
                refs, anchors = Counter(), 0
            element.clear()
            while element.getprevious() is not None:
                del parent[0]
    if refs or anchors:
        sections.append((False, refs, anchors))
    pages: list[tuple[Counter, int]] = []
    for index, (starts_page, section_refs, section_anchors) in enumerate(sections):
        if index == 0 or starts_page or not pages:
            pages.append((Counter(), 0))
        page_refs, page_anchors = pages[-1]
        page_refs.update(section_refs)
        pages[-1] = (page_refs, page_anchors + section_anchors)
    return [(max(page_refs.values(), default=0), page_anchors) for page_refs, page_anchors in pages]


def broken_docx_pages(docx_path: Path, page_count: int) -> list[int]:
    """转坏了的页（从 1 数，按这一段的页序）。分节对不上原文页数时认不出是哪页，返回空，
    交给最后一道页数检查。"""
    try:
        stats = docx_page_stats(docx_path)
    except Exception as exc:  # noqa: BLE001 - an unreadable file is the converter's failure, not ours
        logger.warning(f"[PDF] 检查转出的 Word 失败：{exc!r}")
        return []
    if len(stats) != page_count:
        logger.info(f"[PDF] 转出的 Word 有 {len(stats)} 个分页节，原文 {page_count} 页，不逐页检查")
        return []
    return [index for index, (same_image, anchors) in enumerate(stats, start=1)
            if same_image > MAX_SAME_IMAGE_PER_PAGE or anchors > MAX_ANCHORS_PER_PAGE]


def segment_grew_too_much(source_pages: int, output_pages: int) -> bool:
    """整段转回 PDF 的页数是不是多得不正常。"""
    return output_pages > max(source_pages * SEGMENT_PAGE_GROWTH, source_pages + SEGMENT_PAGE_SLACK)


class BrokenPagesError(Exception):
    """pdf2docx 把这一段里的某几页转坏了；``pages`` 从 1 数，按这一段的页序。"""

    def __init__(self, pages: list[int]):
        super().__init__(f"broken pages: {pages}")
        self.pages = pages
