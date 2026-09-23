"""Bounded, same-origin OpenAI text protocol negotiation.

The cache is process-local (10 minute success / 2 second failure TTL), never
stores credentials, and shares only a route/capabilities, never business output.
One leader per identity negotiates; waiting workers remain cancellable.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

import httpx

CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 120.0  # idle read, separately from the whole operation's deadline
TOTAL_TIMEOUT = 180.0
MAX_SENDS = 6  # includes protocol negotiation and authorized connection failover


class TextTransportError(ValueError):
    """A transport failure that must not be replayed by batch splitting."""
    no_replay = True

    def __init__(self, message: str, *, kind: str = "invalid_response"):
        super().__init__(message)
        self.kind = kind


class TextTransportHTTPError(httpx.HTTPStatusError):
    """A completed HTTP failure that callers may classify but must not replay."""

    no_replay = True

    def __init__(self, message: str, *, request: httpx.Request,
                 response: httpx.Response, kind: str):
        super().__init__(message, request=request, response=response)
        self.kind = kind


@dataclass
class RequestBudget:
    deadline: float = field(default_factory=lambda: time.monotonic() + TOTAL_TIMEOUT)
    remaining: int = MAX_SENDS
    should_stop: Callable[[], bool] | None = None

    def check(self) -> float:
        if self.should_stop and self.should_stop():
            raise TextTransportError("请求已取消。", kind="cancelled")
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise TextTransportError("请求总时限已用尽；协议仍未确定。", kind="total_timeout")
        return left

    def take(self) -> None:
        self.check()
        if self.remaining <= 0:
            raise TextTransportError("本次请求尝试预算已用尽。", kind="budget_exhausted")
        self.remaining -= 1


_BUDGET: ContextVar[RequestBudget | None] = ContextVar("text_request_budget", default=None)


@contextmanager
def request_budget(*, seconds=TOTAL_TIMEOUT, should_stop=None):
    existing = _BUDGET.get()
    if existing is not None:
        yield existing
        return
    budget = RequestBudget(deadline=time.monotonic() + seconds, should_stop=should_stop)
    token = _BUDGET.set(budget)
    try:
        yield budget
    finally:
        _BUDGET.reset(token)


def bounded_text_operation(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with request_budget(should_stop=kwargs.get("should_stop")):
            return fn(*args, **kwargs)
    return wrapped


@dataclass(frozen=True)
class Route:
    mode: str
    url: str
    omitted: frozenset[str] = frozenset()


@dataclass
class _Entry:
    event: threading.Event = field(default_factory=threading.Event)
    route: Route | None = None
    error: Exception | None = None
    expires: float = 0


_CACHE: dict[str, _Entry] = {}
_LOCK = threading.Lock()


def _response_body(response: httpx.Response) -> str:
    try:
        return response.text
    except httpx.ResponseNotRead:
        return ""


def _copy_cached_error(exc: Exception) -> Exception:
    """Give every caller an equivalent exception with its own traceback."""
    if isinstance(exc, httpx.HTTPStatusError):
        request = httpx.Request(exc.request.method, exc.request.url)
        response = httpx.Response(
            exc.response.status_code,
            headers=exc.response.headers,
            text=_response_body(exc.response),
            request=request,
        )
        return TextTransportHTTPError(
            str(exc),
            request=request,
            response=response,
            kind=getattr(exc, "kind", classify_http_error(exc)[0]),
        )
    copied = TextTransportError(
        str(exc),
        kind=getattr(exc, "kind", "request_failed"),
    )
    if hasattr(exc, "status_code"):
        copied.status_code = exc.status_code
    return copied


def _sanitize_error(exc: Exception, api_key: str) -> Exception:
    def replacement(value: str) -> str:
        return value.replace(api_key, "***") if api_key else value
    if isinstance(exc, httpx.HTTPStatusError):
        request = httpx.Request(exc.request.method, exc.request.url)
        response = httpx.Response(
            exc.response.status_code,
            headers=exc.response.headers,
            text=replacement(_response_body(exc.response)),
            request=request,
        )
        return TextTransportHTTPError(
            replacement(str(exc)),
            request=request,
            response=response,
            kind=classify_http_error(exc)[0],
        )
    sanitized = TextTransportError(
        replacement(str(exc)),
        kind=getattr(exc, "kind", "request_failed"),
    )
    if hasattr(exc, "status_code"):
        sanitized.status_code = exc.status_code
    return sanitized


def clear_protocol_cache():
    """Explicit revalidation hook; in-flight leaders still wake their waiters."""
    with _LOCK:
        _CACHE.clear()


def normalize_api_mode(value: str) -> str:
    value = str(value or "auto").strip()
    value = {"codex_responses": "responses", "chat_completions": "chat"}.get(value, value)
    if value not in {"auto", "chat", "responses"}:
        raise ValueError("api_mode 必须为 auto、chat 或 responses")
    return value


def candidate_routes(base_url: str, mode: str) -> list[Route]:
    parsed = urlsplit(base_url.strip().rstrip("/"))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("文本服务地址必须是无内嵌凭据的 HTTP(S) URL")
    if parsed.query or parsed.fragment:
        raise ValueError("文本服务 Base URL 不支持查询参数或片段")
    base = urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))
    # Explicit endpoint URLs are also a way of locking a custom path.
    suffix = next((s for s in ("/chat/completions", "/responses") if base.endswith(s)), "")
    if suffix:
        detected = "responses" if suffix == "/responses" else "chat"
        if mode != "auto" and mode != detected:
            raise ValueError("显式协议与请求地址后缀不一致")
        return [Route(detected, base)]
    if mode != "auto":
        return [Route(mode, base + ("/responses" if mode == "responses" else "/chat/completions"))]
    # Preserve old root -> /v1 behavior in auto; never strip a custom prefix.
    primary = base + "/v1" if not parsed.path else base
    routes = [Route("chat", primary + "/chat/completions"), Route("responses", primary + "/responses")]
    if parsed.path in {"", "/v1"}:
        root = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
        routes.append(Route("responses", root + "/responses"))
    return routes


def _response_text(payload: object) -> str:
    if not isinstance(payload, dict) or payload.get("status") != "completed":
        raise TextTransportError("Responses 返回未完成或失败的结果。", kind="incomplete")
    chunks = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        if item.get("status") not in {None, "completed"}:
            raise TextTransportError("Responses 消息未完成。", kind="incomplete")
        for part in item.get("content") or []:
            if isinstance(part, dict) and part.get("type") == "output_text" and isinstance(part.get("text"), str):
                chunks.append(part["text"])
    text = "".join(chunks)
    if not text.strip():
        raise TextTransportError("Responses 返回成功但未包含可解析正文")
    return text


def extract_chat_text(payload: object) -> str:
    if not isinstance(payload, dict):
        raise TextTransportError("Chat Completions 返回格式异常")
    choices = payload.get("choices")
    first = choices[0] if isinstance(choices, list) and choices else None
    if not isinstance(first, dict) or first.get("finish_reason") != "stop":
        raise TextTransportError("Chat Completions 结果为空或未正常完成", kind="incomplete")
    message = first.get("message") or {}
    text = message.get("content") if isinstance(message, dict) else None
    if not isinstance(text, str) or not text.strip():
        raise TextTransportError("Chat Completions 返回未包含可用正文")
    return text


class ResponsesEvents:
    def __init__(self):
        self.deltas: list[str] = []
        self.text: str | None = None

    def feed(self, line: str):
        if not line.startswith("data:"):
            return
        raw = line[5:].strip()
        if raw == "[DONE]":
            return
        try:
            event = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise TextTransportError("Responses 事件不是有效 JSON") from exc
        if not isinstance(event, dict):
            raise TextTransportError("Responses 事件格式异常")
        kind = event.get("type")
        if kind in {"response.failed", "response.incomplete", "error"}:
            raise TextTransportError("Responses 返回失败或未完成事件", kind="incomplete")
        if kind == "response.output_text.delta" and isinstance(event.get("delta"), str):
            self.deltas.append(event["delta"])
        if kind == "response.completed":
            # Completed output is authoritative: never append it to deltas.
            self.text = _response_text(event.get("response"))

    def finish(self) -> str:
        if self.text is None:
            raise TextTransportError("Responses 流在完成事件前中断；部分正文未采用。", kind="incomplete")
        return self.text


def extract_responses_events(lines) -> str:
    parser = ResponsesEvents()
    for line in lines:
        parser.feed(line.decode("utf-8") if isinstance(line, bytes) else str(line))
    return parser.finish()


def classify_http_error(exc: httpx.HTTPStatusError) -> tuple[str, str]:
    response = exc.response
    status = response.status_code
    body = response.text.casefold()
    try:
        error = response.json().get("error", {})
        error = error if isinstance(error, dict) else {}
    except (ValueError, AttributeError):
        error = {}
    code = str(error.get("code") or "").casefold()
    if status in {401, 402, 403} or any(s in body for s in ("insufficient_quota", "quota exceeded", "余额不足", "额度不足")):
        return "credential", ""
    if status == 429 or status >= 500:
        return "transient", ""
    if code in {"model_not_found", "invalid_model"} or ("model" in body and any(s in body for s in ("not found", "does not exist", "not exist", "unknown model"))):
        return "model", ""
    # Only known optional parameters are removable; input and JSON contracts aren't.
    if status in {400, 422} and any(s in body for s in ("unsupported", "unknown parameter", "unrecognized", "not supported")):
        param = str(error.get("param") or "")
        if param in {"store", "stream"}:
            return "parameter", param
    if status in {400, 404, 405, 410, 422}:
        if code in {"unsupported_protocol", "unsupported_endpoint", "route_not_found", "endpoint_not_found"}:
            return "route", ""
        if any(s in body for s in ("cannot post ", "unknown endpoint", "unknown route", "no route", "route not found", "endpoint not found", "unsupported endpoint", "unsupported protocol")):
            return "route", ""
        if any(s in body for s in ("chat/completions", "responses api", "wire_api", "/responses")) and any(s in body for s in ("not supported", "unsupported", "use ", "only supports")):
            return "route", ""
    return "request", ""


def _payload(route: Route, model: str, system: str, user: str) -> dict:
    if route.mode == "chat":
        return {"model": model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    body = {"model": model, "instructions": system, "input": [{"role": "user", "content": [{"type": "input_text", "text": user}]}], "store": False, "stream": True}
    return {k: v for k, v in body.items() if k not in route.omitted}


async def _send_async(route: Route, api_key: str, payload: dict, budget: RequestBudget) -> str:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    timeout = httpx.Timeout(connect=CONNECT_TIMEOUT, read=READ_TIMEOUT, write=30.0, pool=CONNECT_TIMEOUT)
    # trust_env uses the same OS/environment proxy policy for tests and translation.
    # Never follow a redirect with credentials, including cross-origin redirects.
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=True) as client:
        async with client.stream("POST", route.url, headers=headers, json=payload) as response:
            if response.status_code >= 300:
                await response.aread()
                response.raise_for_status()
            if "text/event-stream" in response.headers.get("content-type", "").lower():
                if route.mode != "responses":
                    raise TextTransportError("非流式 Chat 请求返回了未约定的事件流")
                parser = ResponsesEvents()
                async for line in response.aiter_lines():
                    budget.check()
                    parser.feed(line)
                    if parser.text is not None:
                        return parser.finish()
                return parser.finish()
            await response.aread()
            try:
                payload = response.json()
            except ValueError as exc:
                raise TextTransportError("HTTP 200 正文不是有效 JSON 或 Responses 事件流") from exc
            return _response_text(payload) if route.mode == "responses" else extract_chat_text(payload)


async def _send_cancellable(route, api_key, payload, budget):
    task = asyncio.create_task(_send_async(route, api_key, payload, budget))
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=min(0.1, budget.check()))
        return await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def _send(route, api_key, payload, budget):
    budget.take()
    try:
        return asyncio.run(_send_cancellable(route, api_key, payload, budget))
    except httpx.TimeoutException as exc:
        kind = "connect_timeout" if isinstance(exc, httpx.ConnectTimeout) else "read_timeout"
        raise TextTransportError(f"{kind}：请求超时，未确定服务是否已执行；不自动切换协议。", kind=kind) from exc
    except (httpx.ReadError, httpx.RemoteProtocolError) as exc:
        raise TextTransportError("响应读取中断，未取得完整结果；不自动重发。", kind="incomplete") from exc


def request_text(*, base_url: str, api_key: str, model: str, system: str, user: str,
                 api_mode: str = "auto", connection_id: str = "", total_seconds=TOTAL_TIMEOUT,
                 should_stop=None, model_role: str = "translation") -> tuple[str, Route]:
    if connection_id:
        from core.model_auto_upgrade import effective_model_after_rollback

        model = effective_model_after_rollback(connection_id, model, base_url, model_role)
    mode = normalize_api_mode(api_mode)
    routes = candidate_routes(base_url, mode)
    identity = hashlib.sha256(json.dumps([connection_id, base_url, model, mode, hashlib.sha256(api_key.encode()).hexdigest()], ensure_ascii=False).encode()).hexdigest()
    with request_budget(seconds=total_seconds, should_stop=should_stop) as budget:
        with _LOCK:
            now = time.monotonic()
            for key, entry in list(_CACHE.items()):
                if entry.event.is_set() and entry.expires <= now:
                    del _CACHE[key]
            entry = _CACHE.get(identity)
            leader = entry is None
            if leader:
                entry = _Entry()
                _CACHE[identity] = entry
        if not leader:
            while not entry.event.wait(min(0.05, budget.check())):
                pass
            if entry.error:
                error_kind = (
                    classify_http_error(entry.error)[0]
                    if isinstance(entry.error, httpx.HTTPStatusError)
                    else getattr(entry.error, "kind", "")
                )
                if error_kind == "model" and connection_id:
                    from core.model_auto_upgrade import effective_model_after_rollback

                    predecessor = effective_model_after_rollback(
                        connection_id, model, base_url, model_role,
                    )
                    if predecessor != model:
                        return request_text(
                            base_url=base_url, api_key=api_key, model=predecessor,
                            system=system, user=user, api_mode=api_mode,
                            connection_id=connection_id, total_seconds=total_seconds,
                            should_stop=should_stop, model_role=model_role,
                        )
                raise _copy_cached_error(entry.error)
            routes = [entry.route] + [r for r in routes if r != entry.route]
        try:
            # Cached success runs concurrently. If capability changes, invalidate and
            # fail this call; the next call elects a single revalidation leader.
            if not leader:
                route = routes[0]
                try:
                    return _send(route, api_key, _payload(route, model, system, user), budget), route
                except httpx.HTTPStatusError as exc:
                    if classify_http_error(exc)[0] in {"route", "parameter"}:
                        with _LOCK:
                            if _CACHE.get(identity) is entry:
                                del _CACHE[identity]
                            if budget.remaining <= 0:
                                raise TextTransportError("协议候选已耗尽。", kind="budget_exhausted") from exc
                    raise
            # Keep a local attempt cap as well as the wall-clock budget. The
            # latter is deliberately consumed by _send; the local counter
            # also protects callers that replace _send in tests/integrations.
            send_attempts = 0
            for route_index, route in enumerate(routes):
                while True:
                    try:
                        send_attempts += 1
                        text = _send(route, api_key, _payload(route, model, system, user), budget)
                        entry.route = route
                        entry.expires = time.monotonic() + 600
                        return text, route
                    except httpx.HTTPStatusError as exc:
                        kind, param = classify_http_error(exc)
                        if budget.remaining <= 0 or send_attempts >= MAX_SENDS:
                            raise TextTransportError("协议候选已耗尽。", kind="budget_exhausted") from exc
                        if kind == "parameter" and param in _payload(route, model, system, user) and param not in route.omitted:
                            route = Route(route.mode, route.url, route.omitted | {param})
                            continue
                        if mode == "auto" and kind == "route" and route != routes[-1]:
                            break
                        raise
            raise TextTransportError("协议候选已耗尽。", kind="budget_exhausted")
        except Exception as exc:
            if leader:
                # Retain only sanitized failures; no request payloads or credentials.
                entry.error = _sanitize_error(exc, api_key)
                entry.expires = time.monotonic() + 2
            # A candidate promoted by this app can become unavailable
            # unavailable after startup.  Restore its predecessor only for a
            # definitive model error, then safely retry this same text request.
            # A rejected model has not processed the user's request, unlike a
            # timeout or incomplete response, so this replay cannot duplicate
            # a translation that may already have run.
            kind = (
                classify_http_error(exc)[0]
                if isinstance(exc, httpx.HTTPStatusError)
                else getattr(exc, "kind", "")
            )
            if kind == "model" and connection_id:
                from core.model_auto_upgrade import rollback_upgraded_model

                try:
                    previous_model = rollback_upgraded_model(
                        connection_id, model, base_url, api_key=api_key,
                        model_role=model_role,
                    )
                except Exception:
                    previous_model = ""
                if previous_model and budget.remaining > 0:
                    return request_text(
                        base_url=base_url, api_key=api_key, model=previous_model,
                        system=system, user=user, api_mode=api_mode,
                        connection_id=connection_id, total_seconds=total_seconds,
                        should_stop=should_stop, model_role=model_role,
                    )
            if leader:
                raise _copy_cached_error(entry.error) from exc
            raise _sanitize_error(exc, api_key) from exc
        finally:
            if leader:
                entry.event.set()
