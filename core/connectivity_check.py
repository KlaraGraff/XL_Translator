"""Lightweight connectivity checks for configured translation backends."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from config import DEFAULT_CUSTOM_OPENAI_BASE_URL, LANYI_BASE_URL, OLLAMA_BASE_URL
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

HERMES_OPENAI_COMPATIBLE_PROVIDERS = {
    "custom",
    "custom_openai",
    "openai",
    "openrouter",
    "siliconflow",
    "deepseek",
    "xai",
    "ai-gateway",
    "google",
    "gemini",
    "huggingface",
    "kimi-coding",
    "zai",
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


def _official_provider_base_url(provider: str, base_url: str) -> str:
    if provider == "openai":
        return ""
    normalized = str(base_url or "").strip().rstrip("/")
    custom_default = DEFAULT_CUSTOM_OPENAI_BASE_URL.rstrip("/")
    if provider == "claude" and normalized == custom_default:
        return ""
    return normalized


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
            response = client.get(_append_url_path(OLLAMA_BASE_URL, "/api/tags"))
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
) -> ConnectivityResult:
    if not api_key:
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

    normalized_base_url = _normalize_base_url(base_url, default_url="https://api.openai.com/v1")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
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

    normalized_base_url = _normalize_base_url(base_url, default_url="https://api.anthropic.com/v1")
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


def _check_hermes(
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> ConnectivityResult:
    try:
        from engines.hermes_engine import load_hermes_runtime_routes

        routes = load_hermes_runtime_routes()
    except Exception as exc:  # noqa: BLE001 - converted to UI-safe status.
        return ConnectivityResult(
            ok=False,
            status="config_error",
            message=f"Hermes 配置不可用：{_sanitize_error_message(exc)}",
            provider="hermes",
        )

    if not routes:
        return ConnectivityResult(
            ok=False,
            status="config_error",
            message="Hermes 未返回可用模型路由。",
            provider="hermes",
        )

    route = routes[0]
    provider = route.provider.lower()
    if provider == "anthropic":
        return _check_claude(
            api_key=route.api_key,
            model=route.model,
            base_url=route.base_url,
            timeout_seconds=timeout_seconds,
        )

    if provider in HERMES_OPENAI_COMPATIBLE_PROVIDERS or route.base_url:
        return _check_openai_compatible(
            provider=f"hermes/{route.provider}",
            api_key=route.api_key,
            model=route.model,
            base_url=route.base_url,
            timeout_seconds=timeout_seconds,
        )

    return ConnectivityResult(
        ok=False,
        status="unsupported_provider",
        message=f"Hermes 当前 provider 暂不支持连接测试：{route.provider}。",
        provider="hermes",
        model=route.model,
    )


def check_connectivity(
    settings: AppSettings,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> ConnectivityResult:
    engine_settings = settings.engine

    if engine_settings.mode == "local":
        return _check_ollama_model(
            engine_settings.ollama_model,
            timeout_seconds=timeout_seconds,
        )

    provider = str(engine_settings.cloud_provider or "").strip()
    model = str(engine_settings.cloud_model or "").strip()

    if provider == "hermes":
        return _check_hermes(timeout_seconds=timeout_seconds)

    if provider in OPENAI_COMPATIBLE_PROVIDERS:
        base_url = _official_provider_base_url(provider, engine_settings.cloud_base_url)
        if provider == "lanyi" and not base_url:
            base_url = LANYI_BASE_URL
        return _check_openai_compatible(
            provider=provider,
            api_key=get_key(provider),
            model=model,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
        )

    if provider == "claude":
        return _check_claude(
            api_key=get_key(provider),
            model=model,
            base_url=_official_provider_base_url(provider, engine_settings.cloud_base_url),
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
