"""复查时带上前后文（反馈 #28）。

复查员以前只看到一条原文和它的译文。讲义表格里单独一格「pi」「Ai」「yi」，离开前后
几格谁也说不准是下标变量 pᵢ，还是圆周率、人名、缩写，于是复查一会儿判「通过」、一会儿
判「不一致」「无法确定」，同一份文件每跑一次结果都不一样。翻译时模型是整批看的，
看得到前后文，所以原样还回；复查也该看到同样的东西再下判断。

这里只负责从「按文档顺序排好的原文」里给每一条取前后几条，交给
core.translation_review.ReviewItem.context。用不用、怎么排顺序由各模式决定。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

#: 前后各取几条、每条最多几个字。内部数值，不是设置。
CONTEXT_BEFORE = 3
CONTEXT_AFTER = 3
CONTEXT_ITEM_CHARS = 60


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def neighbour_contexts(
    texts: Sequence[str],
    *,
    before: int = CONTEXT_BEFORE,
    after: int = CONTEXT_AFTER,
    item_chars: int = CONTEXT_ITEM_CHARS,
) -> dict[str, str]:
    """``{原文: "前面：… ｜ …；后面：… ｜ …"}``。同一句出现多次时用第一次的位置。"""
    order: list[str] = []
    seen: set[str] = set()
    for text in texts:
        text = str(text or "").strip()
        if text and text not in seen:
            seen.add(text)
            order.append(text)
    contexts: dict[str, str] = {}
    for index, text in enumerate(order):
        parts = []
        previous = [_clip(t, item_chars) for t in order[max(0, index - before):index]]
        following = [_clip(t, item_chars) for t in order[index + 1:index + 1 + after]]
        if previous:
            parts.append("前面：" + " ｜ ".join(previous))
        if following:
            parts.append("后面：" + " ｜ ".join(following))
        if parts:
            contexts[text] = "；".join(parts)
    return contexts


def contexts_by_file(segment_locations: Mapping[str, Sequence[Mapping]]) -> dict[str, str]:
    """Word：``segment_locations`` 按抽取顺序登记（正文段落、表格逐格……），按文件分开取前后文。"""
    files: dict[str, list[str]] = {}
    for source, locations in segment_locations.items():
        file_name = str(locations[0].get("file", "")) if locations else ""
        files.setdefault(file_name, []).append(source)
    contexts: dict[str, str] = {}
    for sources in files.values():
        for source, context in neighbour_contexts(sources).items():
            contexts.setdefault(source, context)
    return contexts
