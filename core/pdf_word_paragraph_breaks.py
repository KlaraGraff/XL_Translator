"""PDF 转 Word 后，一段里「换行 + 制表符缩进 + 大写字母开头」处拆成两段（反馈 #28 讲义第 21 页）。

pdf2docx 有时把上一段的最后一行和下一段（首行缩进）写进同一段：行间一个换行符，下一段开头的首行缩进
写成一个制表符。上一段末尾恰好没有句号时（讲义命题 2.5「…A ∧ C is unsatisfiable」），「合并折行」
把换行当成句中折行换成空格，两段并成一句，译文里「(ii) 如果 C 不可满足…」和下一段「接下来我们再列出
几个等价关系」挤在同一行。

折行折下来的下一行从左边起，不会先空一个制表符；换行后紧跟制表符、再接大写字母开头的字，是新的一段。
这里拆开：换行去掉，制表符留着（照旧按制表位缩进），段落格式照抄，分节信息只留在最后一段。
"""
from __future__ import annotations

import copy

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _only(run, tag: str) -> bool:
    children = [child for child in run if child.tag != f"{_W}rPr"]
    return bool(children) and all(child.tag == f"{_W}{tag}" for child in children)


def _line_break(run) -> bool:
    return _only(run, "br") and all(br.get(f"{_W}type") in (None, "textWrapping") and not br.get(f"{_W}clear")
                                    for br in run.findall(f"{_W}br"))


def _text(run) -> str:
    return "".join(node.text or "" for node in run.findall(f"{_W}t"))


def split_at_indented_breaks(p) -> int:
    """拆开 p（w:p 元素）里换行后缩进另起的段，返回拆了几处。"""
    runs = [child for child in p if child.tag == f"{_W}r"]
    cuts = []
    for index in range(1, len(runs) - 2):
        if not (_line_break(runs[index]) and _only(runs[index + 1], "tab")):
            continue
        after = next((_text(run) for run in runs[index + 2:] if _text(run).strip()), "").lstrip()
        before = "".join(_text(run) for run in runs[:index]).strip()
        if before and after[:1].isupper():
            cuts.append(index)
    if not cuts:
        return 0
    pieces = [p]
    p_pr = p.find(f"{_W}pPr")
    anchor = p
    for number, cut in enumerate(cuts):
        end = cuts[number + 1] if number + 1 < len(cuts) else len(runs)
        piece = p.makeelement(f"{_W}p", {})
        if p_pr is not None:
            piece.append(copy.deepcopy(p_pr))
        for run in runs[cut + 1:end]:
            piece.append(run)
        p.remove(runs[cut])
        anchor.addnext(piece)
        anchor = piece
        pieces.append(piece)
    # 分节信息只留在最后一段；段前距只给第一段，段后距只给最后一段
    for index, piece in enumerate(pieces):
        piece_pr = piece.find(f"{_W}pPr")
        if piece_pr is None:
            continue
        if index < len(pieces) - 1:
            for sect in piece_pr.findall(f"{_W}sectPr"):
                piece_pr.remove(sect)
        spacing = piece_pr.find(f"{_W}spacing")
        if spacing is not None:
            if index > 0:
                spacing.set(f"{_W}before", "0")
            if index < len(pieces) - 1:
                spacing.set(f"{_W}after", "0")
    return len(cuts)
