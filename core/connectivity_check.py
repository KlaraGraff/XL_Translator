"""Lightweight connectivity checks for configured translation backends."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from config import (
    LM_STUDIO_BASE_URL,
    OLLAMA_BASE_URL,
    normalize_cloud_base_url,
)
from settings import AppSettings, get_key


DEFAULT_TIMEOUT_SECONDS = 12.0
TEST_SYSTEM_PROMPT = "你是连接测试助手。"
TEST_USER_PROMPT = "请只回复 OK，用于确认当前 API 配置可用。"

OPENAI_COMPATIBLE_PROVIDERS = {
    "openai",
    "custom_openai",
    "lanyi",
    "siliconflow",
}

@dataclass(frozen=True)
class ConnectivityResult:
    ok: bool
    status: str
    message: str
    provider: str = ""
    model: str = ""
    detail: str = ""


def _sanitize_error_message(exc: Exception, *, secret: str = "") -> str:
    message = str(exc).strip() or exc.__class__.__name__
    if secret:
        message = message.replace(secret, "***")
    if len(message) > 500:
        message = message[:497] + "..."
    return message


def _normalize_base_url(base_url: str, *, default_url: str = "") -> str:
    normalized = str(base_url or "").strip().rstrip("/")
    if normalized:
        return normalized
    return default_url.rstrip("/")


def _append_url_path(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _supports_asxs_responses_route(base_url: str) -> bool:
    normalized = str(base_url or "").strip()
    if not normalized:
        return False
    parsed = urlparse(normalized)
    return parsed.netloc.lower() == "api.asxs.top"


def _raise_for_status(response: httpx.Response) -> None:
    try:
        response.raise_for_status()
    except Exception as exc:
        body = str(getattr(response, "text", "") or "").strip()
        status_code = getattr(response, "status_code", "")
        if body:
            raise RuntimeError(f"HTTP {status_code}: {body[:300]}") from exc
        raise


def _extract_ollama_model_names(payload: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for item in payload.get("models") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        model = str(item.get("model") or "").strip()
        if name:
            names.add(name)
        if model:
            names.add(model)
    return names


def _check_ollama_model(
    model: str,
    *,
    base_url: str = OLLAMA_BASE_URL,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> ConnectivityResult:
    model_name = str(model or "").strip()
    if not model_name:
        return ConnectivityResult(
            ok=False,
            status="missing_model",
            message="请先填写 Ollama 模型名称。",
            provider="ollama",
        )

    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            response = client.get(_append_url_path(base_url or OLLAMA_BASE_URL, "/api/tags"))
            _raise_for_status(response)
            payload = response.json()
    except Exception as exc:  # noqa: BLE001 - converted to UI-safe status.
        return ConnectivityResult(
            ok=False,
            status="unreachable",
            message=f"Ollama 服务不可用：{_sanitize_error_message(exc)}",
            provider="ollama",
            model=model_name,
        )

    if not isinstance(payload, dict):
        return ConnectivityResult(
            ok=False,
            status="invalid_response",
            message="Ollama 返回格式异常。",
            provider="ollama",
            model=model_name,
        )

    installed_models = _extract_ollama_model_names(payload)
    if model_name not in installed_models:
        return ConnectivityResult(
            ok=False,
            status="model_missing",
            message=f"Ollama 服务可用，但未找到模型：{model_name}。",
            provider="ollama",
            model=model_name,
            detail="请先在终端运行 ollama pull 对应模型，或改选已安装模型。",
        )

    return ConnectivityResult(
        ok=True,
        status="ok",
        message=f"Ollama 服务可用，模型 {model_name} 已安装。",
        provider="ollama",
        model=model_name,
    )


def _check_openai_compatible(
    *,
    provider: str,
    api_key: str,
    model: str,
    base_url: str,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    require_api_key: bool = True,
) -> ConnectivityResult:
    if require_api_key and not api_key:
        return ConnectivityResult(
            ok=False,
            status="missing_api_key",
            message="请先填写 API Key。",
            provider=provider,
            model=model,
        )
    if not model:
        return ConnectivityResult(
            ok=False,
            status="missing_model",
            message="请先填写模型名称。",
            provider=provider,
        )

    normalized_base_url = normalize_cloud_base_url(provider, base_url)
    if not normalized_base_url:
        return ConnectivityResult(
            ok=False,
            status="missing_base_url",
            message="请先填写 Base URL。",
            provider=provider,
            model=model,
        )
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if _supports_asxs_responses_route(normalized_base_url):
        url = _append_url_path(normalized_base_url, "/responses")
        payload: dict[str, Any] = {
            "model": model,
            "instructions": TEST_SYSTEM_PROMPT,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": TEST_USER_PROMPT,
                        }
                    ],
                }
            ],
            "store": False,
            "stream": False,
        }
    else:
        url = _append_url_path(normalized_base_url, "/chat/completions")
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": TEST_SYSTEM_PROMPT},
                {"role": "user", "content": TEST_USER_PROMPT},
            ],
        }

    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            response = client.post(url, headers=headers, json=payload)
            _raise_for_status(response)
    except Exception as exc:  # noqa: BLE001 - converted to UI-safe status.
        return ConnectivityResult(
            ok=False,
            status="request_failed",
            message=f"连接测试失败：{_sanitize_error_message(exc, secret=api_key)}",
            provider=provider,
            model=model,
            detail=normalized_base_url,
        )

    return ConnectivityResult(
        ok=True,
        status="ok",
        message=f"{provider} 连接可用，模型 {model} 响应成功。",
        provider=provider,
        model=model,
        detail=normalized_base_url,
    )


def _check_claude(
    *,
    api_key: str,
    model: str,
    base_url: str = "",
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> ConnectivityResult:
    if not api_key:
        return ConnectivityResult(
            ok=False,
            status="missing_api_key",
            message="请先填写 API Key。",
            provider="claude",
            model=model,
        )
    if not model:
        return ConnectivityResult(
            ok=False,
            status="missing_model",
            message="请先填写模型名称。",
            provider="claude",
        )

    normalized_base_url = normalize_cloud_base_url("claude", base_url)
    url = _append_url_path(normalized_base_url, "/messages")
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "max_tokens": 8,
        "system": TEST_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": TEST_USER_PROMPT}],
    }

    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            response = client.post(url, headers=headers, json=payload)
            _raise_for_status(response)
    except Exception as exc:  # noqa: BLE001 - converted to UI-safe status.
        return ConnectivityResult(
            ok=False,
            status="request_failed",
            message=f"Claude 连接测试失败：{_sanitize_error_message(exc, secret=api_key)}",
            provider="claude",
            model=model,
            detail=normalized_base_url,
        )

    return ConnectivityResult(
        ok=True,
        status="ok",
        message=f"Claude 连接可用，模型 {model} 响应成功。",
        provider="claude",
        model=model,
        detail=normalized_base_url,
    )


def _check_zhipu(
    *,
    api_key: str,
    model: str,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> ConnectivityResult:
    if not api_key:
        return ConnectivityResult(
            ok=False,
            status="missing_api_key",
            message="请先填写 API Key。",
            provider="zhipu",
            model=model,
        )
    if not model:
        return ConnectivityResult(
            ok=False,
            status="missing_model",
            message="请先填写模型名称。",
            provider="zhipu",
        )

    url = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": TEST_SYSTEM_PROMPT},
            {"role": "user", "content": TEST_USER_PROMPT},
        ],
    }

    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            response = client.post(url, headers=headers, json=payload)
            _raise_for_status(response)
    except Exception as exc:  # noqa: BLE001 - converted to UI-safe status.
        return ConnectivityResult(
            ok=False,
            status="request_failed",
            message=f"智谱连接测试失败：{_sanitize_error_message(exc, secret=api_key)}",
            provider="zhipu",
            model=model,
        )

    return ConnectivityResult(
        ok=True,
        status="ok",
        message=f"智谱连接可用，模型 {model} 响应成功。",
        provider="zhipu",
        model=model,
    )


def _check_dashscope(
    *,
    api_key: str,
    model: str,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> ConnectivityResult:
    if not api_key:
        return ConnectivityResult(
            ok=False,
            status="missing_api_key",
            message="请先填写 API Key。",
            provider="dashscope",
            model=model,
        )
    if not model:
        return ConnectivityResult(
            ok=False,
            status="missing_model",
            message="请先填写模型名称。",
            provider="dashscope",
        )

    url = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": TEST_SYSTEM_PROMPT},
            {"role": "user", "content": TEST_USER_PROMPT},
        ],
    }

    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            response = client.post(url, headers=headers, json=payload)
            _raise_for_status(response)
    except Exception as exc:  # noqa: BLE001 - converted to UI-safe status.
        return ConnectivityResult(
            ok=False,
            status="request_failed",
            message=f"百炼连接测试失败：{_sanitize_error_message(exc, secret=api_key)}",
            provider="dashscope",
            model=model,
        )

    return ConnectivityResult(
        ok=True,
        status="ok",
        message=f"百炼连接可用，模型 {model} 响应成功。",
        provider="dashscope",
        model=model,
    )


def _check_local_openai_compatible(
    *,
    provider: str,
    model: str,
    base_url: str,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> ConnectivityResult:
    normalized_base_url = _normalize_base_url(
        base_url,
        default_url=LM_STUDIO_BASE_URL if provider == "lm_studio" else "",
    )
    if not normalized_base_url:
        return ConnectivityResult(
            ok=False,
            status="missing_base_url",
            message="请先填写本地模型服务 Base URL。",
            provider=provider,
            model=model,
        )
    return _check_openai_compatible(
        provider=provider,
        api_key="",
        model=model,
        base_url=normalized_base_url,
        timeout_seconds=timeout_seconds,
        require_api_key=False,
    )


def check_connectivity(
    settings: AppSettings,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> ConnectivityResult:
    engine_settings = settings.engine

    if engine_settings.mode == "local":
        local_provider = str(engine_settings.local_provider or "ollama").strip()
        model = str(engine_settings.local_model or engine_settings.ollama_model or "").strip()
        if local_provider == "ollama":
            return _check_ollama_model(
                model,
                base_url=engine_settings.local_base_url or OLLAMA_BASE_URL,
                timeout_seconds=timeout_seconds,
            )
        if local_provider in {"lm_studio", "custom_local"}:
            return _check_local_openai_compatible(
                provider=local_provider,
                model=model,
                base_url=engine_settings.local_base_url,
                timeout_seconds=timeout_seconds,
            )
        return ConnectivityResult(
            ok=False,
            status="unsupported_provider",
            message=f"暂不支持该本地模型服务：{local_provider or '未设置'}。",
            provider=local_provider,
            model=model,
        )

    provider = str(engine_settings.cloud_provider or "").strip()
    model = str(engine_settings.cloud_model or "").strip()

    if provider in OPENAI_COMPATIBLE_PROVIDERS:
        return _check_openai_compatible(
            provider=provider,
            api_key=get_key(provider),
            model=model,
            base_url=normalize_cloud_base_url(provider, engine_settings.cloud_base_url),
            timeout_seconds=timeout_seconds,
        )

    if provider == "claude":
        return _check_claude(
            api_key=get_key(provider),
            model=model,
            base_url=normalize_cloud_base_url(provider, engine_settings.cloud_base_url),
            timeout_seconds=timeout_seconds,
        )

    if provider == "zhipu":
        return _check_zhipu(
            api_key=get_key(provider),
            model=model,
            timeout_seconds=timeout_seconds,
        )

    if provider == "dashscope":
        return _check_dashscope(
            api_key=get_key(provider),
            model=model,
            timeout_seconds=timeout_seconds,
        )

    return ConnectivityResult(
        ok=False,
        status="unsupported_provider",
        message=f"暂不支持该服务商的连接测试：{provider or '未设置'}。",
        provider=provider,
        model=model,
    )
