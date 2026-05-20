"""Adaptive handling for upstream API concurrency-limit feedback."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from typing import Any

from core.api_scheduler import (
    API_CONCURRENCY_ACTION_REDUCED,
    ApiConcurrencyLimitDecision,
    WeightedApiScheduler,
)


class ApiKeyTemporarilyUnavailableError(RuntimeError):
    """Raised when an API key remains concurrency-limited at the minimum cap."""


_CONCURRENCY_LIMIT_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\btoo\s+many\s+concurrent\b",
        r"\bmax(?:imum)?\s+concurrent\b",
        r"\bconcurrent(?:\s+\w+){0,4}\s+(?:limit|limited|exceeded|reached|quota|capacity)\b",
        r"\bconcurrency(?:\s+\w+){0,4}\s+(?:limit|limited|exceeded|reached|quota|capacity)\b",
        r"\b(?:limit|limited|exceeded|reached|quota|capacity)(?:\s+\w+){0,4}\s+concurren",
        r"\btoo\s+many\s+requests\b",
        r"\brate\s+limit(?:ed|ing)?\b",
        r"\bresource\s+exhausted\b",
        r"并发.{0,16}(?:上限|限制|限流|超限|过多|过高|达到|已满|超过|耗尽)",
        r"(?:上限|限制|限流|超限|过多|过高|达到|已满|超过|耗尽).{0,16}并发",
        r"同时.{0,12}请求.{0,12}(?:上限|限制|限流|超限|过多|过高|超过)",
        r"请求.{0,12}(?:过于频繁|太频繁|过多)",
    )
)

_NON_TEMPORARY_LIMIT_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"insufficient[_\s-]*quota",
        r"\bquota\s+exceeded\b",
        r"\bbilling\b",
        r"\bpayment\b",
        r"\bbalance\b",
        r"余额不足",
        r"额度不足",
        r"欠费",
        r"未授权",
        r"unauthorized",
        r"forbidden",
        r"invalid\s+api\s+key",
        r"api\s+key\s+(?:invalid|expired|disabled)",
        r"model\s+(?:not\s+found|does\s+not\s+exist)",
        r"模型不存在",
    )
)


def is_api_concurrency_limit_error(exc: BaseException) -> bool:
    """Return True for upstream feedback that lowering concurrency may fix."""
    texts = list(_collect_exception_texts(exc))
    if not texts:
        return False

    combined = "\n".join(texts)
    if _has_concurrency_pattern(combined):
        return not _has_non_temporary_limit_pattern_without_concurrency(combined)
    return False


def handle_api_concurrency_limit(
    exc: BaseException,
    *,
    scheduler: WeightedApiScheduler,
    request_generation: int | None,
    context_label: str,
    error_callback: Callable[[str], None] | None = None,
) -> ApiConcurrencyLimitDecision | None:
    """Apply adaptive concurrency policy when an exception carries limit feedback."""
    if not is_api_concurrency_limit_error(exc):
        return None

    decision = scheduler.register_concurrency_limit_hit(request_generation)
    if decision.action == API_CONCURRENCY_ACTION_REDUCED and error_callback:
        error_callback(
            (
                f"{context_label} 检测到上游并发/限流反馈，"
                f"已将本次任务 API 并发上限从 {decision.previous_capacity} "
                f"降至 {decision.current_capacity}，正在重试当前批次。"
            )
        )

    if decision.should_retry:
        return decision

    raise ApiKeyTemporarilyUnavailableError(
        (
            "当前 API Key 暂时不可用：上游在本次任务已降至最低 "
            f"API 并发 {decision.current_capacity} 后仍反馈并发达到上限。"
            "请稍后重试，或更换 API Key 后重新开始。"
        )
    ) from exc


def _has_concurrency_pattern(text: str) -> bool:
    return any(pattern.search(text) for pattern in _CONCURRENCY_LIMIT_PATTERNS)


def _has_non_temporary_limit_pattern_without_concurrency(text: str) -> bool:
    if not any(pattern.search(text) for pattern in _NON_TEMPORARY_LIMIT_PATTERNS):
        return False
    return not re.search(r"concurr|并发|同时.{0,8}请求", text, re.IGNORECASE)


def _collect_exception_texts(exc: BaseException) -> Iterable[str]:
    seen: set[int] = set()
    stack: list[BaseException] = [exc]

    while stack:
        current = stack.pop()
        current_id = id(current)
        if current_id in seen:
            continue
        seen.add(current_id)

        message = str(current).strip()
        if message:
            yield message
        yield current.__class__.__name__

        for attr in ("status_code", "code", "type"):
            value = getattr(current, attr, None)
            if value not in (None, ""):
                yield str(value)

        body = getattr(current, "body", None)
        if body not in (None, ""):
            yield _stringify_payload(body)

        response = getattr(current, "response", None)
        if response is not None:
            yield from _collect_response_texts(response)

        for linked in (current.__cause__, current.__context__):
            if linked is not None:
                stack.append(linked)


def _collect_response_texts(response: Any) -> Iterable[str]:
    status_code = getattr(response, "status_code", None)
    if status_code not in (None, ""):
        yield f"HTTP {status_code}"

    for attr in ("text", "content"):
        value = getattr(response, attr, None)
        if value not in (None, b"", ""):
            yield _stringify_payload(value)

    json_method = getattr(response, "json", None)
    if callable(json_method):
        try:
            yield _stringify_payload(json_method())
        except Exception:
            return


def _stringify_payload(payload: Any) -> str:
    if isinstance(payload, bytes):
        return payload.decode("utf-8", errors="replace")
    if isinstance(payload, (dict, list, tuple)):
        try:
            return json.dumps(payload, ensure_ascii=False)
        except Exception:
            return str(payload)
    return str(payload)
