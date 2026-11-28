"""PDF 转 Word 后，两端对齐的每页最后一段，分节符挪到自己的一个空段里（反馈 #28 讲义第 66 页）。

pdf2docx 每一页是一节，分节信息（sectPr）放在这一页最后一段的段落属性里。这一段两端对齐时，
Word 把分节符当成行尾的换行，最后一行（本该靠左的短行）也拉满整行：译文第 66 页最后一行
「是 双 向 箭 头 ， 因 为 …」字距拉得很开。LibreOffice 不这样，Word 会。

做法：这种段后面加一个空段，分节信息挪过去；空段行高固定 1 磅，几乎不占地方。
"""
from __future__ import annotations

from pathlib import Path

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _justified(p_pr) -> bool:
    jc = p_pr.find(f"{_W}jc")
    return jc is not None and jc.get(f"{_W}val") in ("both", "distribute")


def split_justified_section_marks(docx_path: Path) -> int:
    """两端对齐、带分节信息的段，分节信息挪进后面新加的空段。返回挪了几处。"""
    from docx import Document

    doc = Document(str(docx_path))
    moved = 0
    for paragraph in doc.element.body.findall(f"{_W}p"):
        p_pr = paragraph.find(f"{_W}pPr")
        sect_pr = p_pr.find(f"{_W}sectPr") if p_pr is not None else None
        if sect_pr is None or not _justified(p_pr):
            continue
        if not "".join(node.text or "" for node in paragraph.iter(f"{_W}t")).strip():
            continue
        mark = paragraph.makeelement(f"{_W}p", {})
        mark_pr = mark.makeelement(f"{_W}pPr", {})
        mark_pr.append(mark_pr.makeelement(f"{_W}spacing", {f"{_W}before": "0", f"{_W}after": "0",
                                                            f"{_W}line": "20", f"{_W}lineRule": "exact"}))
        r_pr = mark_pr.makeelement(f"{_W}rPr", {})
        r_pr.append(r_pr.makeelement(f"{_W}sz", {f"{_W}val": "2"}))
        mark_pr.append(r_pr)
        mark_pr.append(sect_pr)  # 挪过去（从原来的段里拿走）
        mark.append(mark_pr)
        paragraph.addnext(mark)
        moved += 1
    if moved:
        doc.save(str(docx_path))
    return moved
