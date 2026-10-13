"""提示词缓存：怎么排提示词，给哪些模型带缓存标识，读回命中了多少。

一份文档分很多批发给模型。多数服务按「前缀」缓存：这一批请求的开头和上一批
一字不差的那一段，按缓存价计费（常见是原价的一到两成）。所以：

1. **排列**：固定不变的内容放最前，每批不同的内容放最后。

   内置规则 → 补充要求 → 目标语言说明 → 返回格式要求 → 文档说明 → 本批词条、改正说明

   返回格式要求由引擎在最后一步接上（engines/），而文档说明、本批词条是一路
   上各层追加的。各层用 :func:`append_context_block` / :func:`append_batch_block`
   在提示词里留下分段记号，引擎用 :func:`compose_system` 把返回格式要求插到
   文档说明前面；发出前 :func:`split_system` 拆成「固定段 + 本批段」并去掉记号。
   记号是私用区字符，绝不能发给模型：凡是把提示词发出去的地方都要经过
   :func:`split_system` 或 :func:`plain_system`。

2. **标识**：按模型名认厂家（同一个模型可能挂在很多第三方平台上，按地址认
   不准），带上该厂家官方的缓存做法——Claude 加 ``cache_control`` 标记，GPT 系列加
   ``prompt_cache_key``，Grok 加 ``x-grok-conv-id`` 请求头。官方本来就自动缓存的
   （DeepSeek、通义、智谱、MiniMax、Kimi、小米、混元）什么都不加。平台不认、把
   请求退回来时，由 core.text_transport 去掉标识重发并记住（maple 2026-10-13）。

3. **用量**：各家响应里命中数的字段名不同，:func:`parse_usage` 都认；
   :class:`CacheUsageMeter` 由任务线程开着，翻译步骤结束时写一行日志。
"""

from __future__ import annotations

import hashlib
import re
import threading
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from typing import TypeVar

# ── 排列 ────────────────────────────────────────────────────────────────────

_CONTEXT_MARK = "xlt:context"
_BATCH_MARK = "xlt:batch"
_MARK_RE = re.compile("(xlt:(?:context|batch))")


def _join(parts) -> str:
    return "\n\n".join(text for text in (str(part or "").strip() for part in parts) if text)


def _append(prompt: str, mark: str, block: str) -> str:
    block = str(block or "").strip()
    if not block:
        return prompt
    return f"{str(prompt or '').rstrip()}{mark}{block}"


def append_context_block(prompt: str, block: str) -> str:
    """接上整份文档共用、但不同文档不同的一段（如「自动识别场景」的文档说明）。"""
    return _append(prompt, _CONTEXT_MARK, block)


def append_batch_block(prompt: str, block: str) -> str:
    """接上只属于这一批的一段（本批词条、改正说明、格式标记规则）。"""
    return _append(prompt, _BATCH_MARK, block)


def _sections(prompt: str) -> tuple[str, list[str], list[str]]:
    pieces = _MARK_RE.split(str(prompt or ""))
    head, contexts, batches = pieces[0], [], []
    for mark, text in zip(pieces[1::2], pieces[2::2]):
        (contexts if mark == _CONTEXT_MARK else batches).append(text)
    return head, contexts, batches


def compose_system(prompt: str, instruction: str = "") -> str:
    """引擎拼最终 system：返回格式要求插在文档说明之前，本批内容留在最后。

    结果仍带本批段的记号，交给发送层拆成固定段和本批段。
    """
    head, contexts, batches = _sections(prompt)
    stable = _join([head, instruction, *contexts])
    volatile = _join(batches)
    return f"{stable}{_BATCH_MARK}{volatile}" if volatile else stable


def split_system(system: str) -> tuple[str, str]:
    """拆成（固定段，本批段），记号全部去掉。固定段在同一份文档的各批之间不变。"""
    head, contexts, batches = _sections(system)
    return _join([head, *contexts]), _join(batches)


def plain_system(system: str) -> str:
    """发给模型的完整文字（固定段在前，本批段在后，没有记号）。"""
    return _join(split_system(system))


# ── 标识 ────────────────────────────────────────────────────────────────────

FAMILY_CLAUDE = "claude"
FAMILY_OPENAI = "openai"
FAMILY_GROK = "grok"

# 第三方平台常把型号写成「厂家/型号」或带前缀，所以只看名字里有没有这些词。
# gpt-oss 是开放权重模型，由各平台自己部署，不走 OpenAI 的缓存。
_OPENAI_MODEL_RE = re.compile(r"(?:^|[/:._\s-])(?:gpt-(?!oss)|chatgpt|o\d(?:$|[-_.])|codex)")


def model_cache_family(model: str) -> str:
    """按模型名认出需要带官方缓存标识的厂家；自动缓存的厂家返回空字符串。"""
    name = str(model or "").strip().lower()
    if not name:
        return ""
    if "claude" in name:
        return FAMILY_CLAUDE
    if "grok" in name:
        return FAMILY_GROK
    if _OPENAI_MODEL_RE.search(name):
        return FAMILY_OPENAI
    return ""


def prompt_cache_key(model: str, stable: str) -> str:
    """同一模型、同一固定段的请求共用的标识（不含任何原文以外的信息）。"""
    digest = hashlib.sha256(f"{model}\0{stable}".encode()).hexdigest()
    return f"xlt-{digest[:32]}"


def grok_conversation_id(model: str, stable: str) -> str:
    digest = hashlib.sha256(f"{model}\0{stable}".encode()).hexdigest()
    return str(uuid.UUID(hex=digest[:32]))


# ── 用量 ────────────────────────────────────────────────────────────────────


def _int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return max(0, int(value))
    return None


@dataclass(frozen=True)
class RequestUsage:
    input_tokens: int
    cached_tokens: int


def parse_usage(usage: object) -> RequestUsage | None:
    """从响应的 usage 里读输入总数和缓存命中数；读不到命中数时返回 None。

    认这些写法：OpenAI 及多数兼容服务 ``prompt_tokens_details.cached_tokens``；
    Responses ``input_tokens_details.cached_tokens``；DeepSeek
    ``prompt_cache_hit_tokens``；百炼部分地域 ``cached_tokens``；Anthropic
    ``cache_read_input_tokens``（它的 ``input_tokens`` 不含缓存部分，要加回去）。
    """
    if not isinstance(usage, Mapping):
        return None
    cache_read = _int(usage.get("cache_read_input_tokens"))
    if cache_read is not None or "cache_creation_input_tokens" in usage:
        cache_read = cache_read or 0
        total = (
            (_int(usage.get("input_tokens")) or 0)
            + cache_read
            + (_int(usage.get("cache_creation_input_tokens")) or 0)
        )
        return RequestUsage(total, cache_read)
    total = _int(usage.get("prompt_tokens"))
    if total is None:
        total = _int(usage.get("input_tokens"))
    cached = None
    for details_key in ("prompt_tokens_details", "input_tokens_details"):
        details = usage.get(details_key)
        if isinstance(details, Mapping):
            cached = _int(details.get("cached_tokens"))
            if cached is not None:
                break
    if cached is None:
        cached = _int(usage.get("prompt_cache_hit_tokens"))
    if cached is None:
        cached = _int(usage.get("cached_tokens"))
    if cached is None or total is None:
        return None
    return RequestUsage(total, min(cached, total))


@dataclass(frozen=True)
class UsageTotals:
    requests: int = 0
    input_tokens: int = 0
    cached_tokens: int = 0

    def minus(self, earlier: UsageTotals) -> UsageTotals:
        return UsageTotals(
            self.requests - earlier.requests,
            self.input_tokens - earlier.input_tokens,
            self.cached_tokens - earlier.cached_tokens,
        )


class CacheUsageMeter:
    """一个任务里所有模型请求读回的用量（只计读得到命中数的请求）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._totals = UsageTotals()

    def add(self, usage: RequestUsage) -> None:
        with self._lock:
            current = self._totals
            self._totals = UsageTotals(
                current.requests + 1,
                current.input_tokens + usage.input_tokens,
                current.cached_tokens + usage.cached_tokens,
            )

    def totals(self) -> UsageTotals:
        with self._lock:
            return self._totals


_METER: ContextVar[CacheUsageMeter | None] = ContextVar("prompt_cache_meter", default=None)


def current_meter() -> CacheUsageMeter | None:
    return _METER.get()


@contextmanager
def usage_metering():
    """任务线程开一个计数器；已经开着时沿用外层的。"""
    existing = _METER.get()
    if existing is not None:
        yield existing
        return
    meter = CacheUsageMeter()
    token = _METER.set(meter)
    try:
        yield meter
    finally:
        _METER.reset(token)


def record_usage(usage: object) -> None:
    meter = _METER.get()
    if meter is None:
        return
    parsed = parse_usage(usage)
    if parsed is not None:
        meter.add(parsed)


F = TypeVar("F", bound=Callable)


def carry_meter(fn: F) -> F:
    """把当前线程的计数器带进线程池里的 ``fn``（线程不继承 ContextVar）。"""
    meter = _METER.get()
    if meter is None:
        return fn

    @wraps(fn)
    def wrapped(*args, **kwargs):
        token = _METER.set(meter)
        try:
            return fn(*args, **kwargs)
        finally:
            _METER.reset(token)

    return wrapped  # type: ignore[return-value]


CACHE_USAGE_TEMPLATE = "模型输入共 {input} tokens，其中缓存命中 {cached}（{percent}%）"


def format_usage_line(totals: UsageTotals) -> str:
    """运行日志里的一行；没有读到用量时返回空字符串。"""
    if totals.requests <= 0 or totals.input_tokens <= 0:
        return ""
    percent = round(totals.cached_tokens * 100 / totals.input_tokens)
    return CACHE_USAGE_TEMPLATE.format(
        input=f"{totals.input_tokens:,}",
        cached=f"{totals.cached_tokens:,}",
        percent=percent,
    )
