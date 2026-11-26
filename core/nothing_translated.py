"""一轮翻译里每一条都因为请求失败没拿到译文：任务（或这张图纸）算失败，不交出原文副本。

``translate_texts`` 把接口放弃的批次按原文占位交回，好让其余批次照常写进文件。可如果
每一批都失败了（例如 DeepSeek 的返回解不开，好友反馈 2026-11-25：53 条一条没翻），
写出来的就是一份原样的文件，日志却说「已生成翻译图纸」。这里在调用方拿到结果后检查：
全部条目都记在 ``stats.untranslated_sources`` 里、又不是用户按了停止，就抛
:class:`NothingTranslatedError`。

它继承 :class:`ApiCredentialRejectedError`（与 Key 被拒同一处理）：各页的批次循环和任务
收尾已经把这类错误当成「整个任务失败」，不再把已有的占位当成部分结果写出去。

用在这一轮就是整份文件的翻译请求的地方：Excel 主翻译、每张 CAD 图纸、扩展格式（PPT 等）
每个文件的主翻译。Word 的主翻译（core/word_task_runner，PDF 转 Word 也走它）按拆到单段
仍失败的片段判断；PDF 原位替换逐页发请求，整份文件判断（core/pdf_task_report
``inplace_requests_all_failed``）。三处用的是同一句 :func:`nothing_translated_message`。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from core.api_concurrency_control import ApiCredentialRejectedError


class NothingTranslatedError(ApiCredentialRejectedError):
    """Every text of the run came back without a translation because its requests failed."""


def nothing_translated_message(count: int, reason: str) -> str:
    reason = str(reason or "").strip().rstrip("。.") or "模型服务返回了错误"
    return (
        f"{count} 条内容的翻译请求全部失败，没有获得任何译文：{reason}。"
        "未生成仅含原文的文件。请稍后重试，或在「设置 → 模型服务」中检查此连接。"
    )


def raise_if_nothing_translated(
    texts: Iterable[str],
    stats: object,
    *,
    should_stop: Callable[[], bool] | None = None,
) -> None:
    """``texts`` 一条都没拿到译文、全是请求失败时抛 :class:`NothingTranslatedError`。"""
    unique = {str(text or "") for text in texts} - {""}
    if not unique or (should_stop is not None and should_stop()):
        return
    failed = set(getattr(stats, "untranslated_sources", ()) or ())
    stopped = set(getattr(stats, "stopped_sources", ()) or ())
    if unique & stopped or not unique <= failed:
        return
    raise NothingTranslatedError(nothing_translated_message(len(unique), failed_request_reason(stats)))


def failed_request_reason(stats: object) -> str:
    """最后一批失败的原因，换成一句人话（与运行日志同一标准）；没有失败的批次时为空。

    原始报错只进调试日志。Word 按单段失败时的同一个函数（user_facing_batch_reason）取原因。
    """
    items = list(getattr(stats, "failed_items", ()) or ())
    if not items:
        return ""
    from core.engine_dispatcher import user_facing_batch_reason

    return user_facing_batch_reason(items[-1].get("error", ""))
