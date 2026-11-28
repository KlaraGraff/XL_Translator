"""PDF 转 Word 译成中日韩文时，紧挨着表格的那一段下面留出中文字多出来的高度（反馈 #28 讲义第 59 页）。

pdf2docx 的固定行距是照原文的英文字定的：11 号字行距 10.9 磅，英文字装得下。中文字（雅黑、苹方）
比英文字高，上下合起来约 1.3 个字号，固定行距装不下的部分伸到行外。段落和段落之间都一样伸出去，
看不出来；下面紧挨着一张表时，表的上边框、标题条的底色就压在字上（讲义第 59 页「下表以几种不同的
方式总结了这些定义」压着表格上边框、定义 6.6 最后一行贴着注记标题条）。

做法：译文是中日韩文时，固定行距比 1.3 个字号小、下面紧挨着表格（中间只隔空段）的段落，段后加上
差的那一截。别的段落不动，页面不会因此整体变长。
"""
from __future__ import annotations

from pathlib import Path

from loguru import logger

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

#: 中日韩字体上下合起来大约占几个字号（雅黑 1.29、苹方 1.32）。
_CJK_HEIGHT = 1.3
#: 没写字号时按 11 号算（1/2 磅）。
_DEFAULT_SIZE = 22


def _number(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _text(element) -> str:
    return "".join(node.text or "" for node in element.iter(f"{_W}t"))


def _spacer(element) -> bool:
    return (element.tag == f"{_W}p" and not _text(element).strip()
            and next(element.iter(f"{_W}drawing", f"{_W}pict", f"{_W}sectPr"), None) is None)


def _size(paragraph) -> float:
    """段落里最大的字号，1/20 磅。"""
    sizes = [_number(node.get(f"{_W}val")) for node in paragraph.iter(f"{_W}sz")]
    return max([size for size in sizes if size > 0] or [_DEFAULT_SIZE]) * 10


def _needs_room(paragraph) -> float:
    spacing = paragraph.find(f"{_W}pPr/{_W}spacing")
    if spacing is None or spacing.get(f"{_W}lineRule") != "exact" or not _text(paragraph).strip():
        return 0.0
    return max(0.0, _CJK_HEIGHT * _size(paragraph) - _number(spacing.get(f"{_W}line")))


def make_room_above_tables(docx_path: Path) -> int:
    """紧挨着表格的那一段，段后加上中文字伸出固定行距的那一截；返回加了几段。"""
    from docx import Document

    doc = Document(str(docx_path))
    changed = 0
    for container in [doc.element.body, *doc.element.body.iter(f"{_W}tc")]:
        children = list(container)
        for index, element in enumerate(children):
            if element.tag != f"{_W}p" or _spacer(element):
                continue
            after = index + 1
            while after < len(children) and _spacer(children[after]):
                after += 1
            if after >= len(children) or children[after].tag != f"{_W}tbl":
                continue
            room = _needs_room(element)
            if room <= 0:
                continue
            spacing = element.find(f"{_W}pPr/{_W}spacing")
            spacing.set(f"{_W}after", str(round(_number(spacing.get(f"{_W}after")) + room)))
            changed += 1
    if changed:
        doc.save(str(docx_path))
        logger.info(f"[PDF] {changed} 段紧挨着表格的段落，段后留出中文字比固定行距高出的那一截")
    return changed
