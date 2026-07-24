"""Lightweight connectivity checks for configured translation backends."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from config import (
    DASHSCOPE_OPENAI_BASE_URL,
    LM_STUDIO_BASE_URL,
    OLLAMA_BASE_URL,
    ZHIPU_OPENAI_BASE_URL,
    normalize_cloud_base_url,
)
from settings import AppSettings, get_cloud_provider_config, get_key


DEFAULT_TIMEOUT_SECONDS = 12.0
TEST_SYSTEM_PROMPT = "你是连接测试助手。"
TEST_USER_PROMPT = "请只回复 OK，用于确认当前 API 配置可用。"
CLEANER_TEST_SYSTEM_PROMPT = (
    "你是模型连通性测试助手。严格只输出 JSON 数组，不要输出解释。"
    "数组中必须保留输入 id，并提供 suggested 字符串。"
)
CLEANER_TEST_USER_PROMPT = (
    '[{"id":"connectivity-probe","source":"Concrete","current":"Concrete"}]'
)

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

    url = _append_url_path(ZHIPU_OPENAI_BASE_URL, "/chat/completions")
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

    url = _append_url_path(DASHSCOPE_OPENAI_BASE_URL, "/chat/completions")
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


def _check_connectivity(
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
    provider_config = get_cloud_provider_config(engine_settings, provider)
    model = str(provider_config.cloud_model or "").strip()
    base_url = normalize_cloud_base_url(provider, provider_config.cloud_base_url)

    if provider in OPENAI_COMPATIBLE_PROVIDERS:
        return _check_openai_compatible(
            provider=provider,
            api_key=get_key(provider, base_url),
            model=model,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
        )

    if provider == "claude":
        return _check_claude(
            api_key=get_key(provider, base_url),
            model=model,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
        )

    if provider == "zhipu":
        return _check_zhipu(
            api_key=get_key(provider, base_url),
            model=model,
            timeout_seconds=timeout_seconds,
        )

    if provider == "dashscope":
        return _check_dashscope(
            api_key=get_key(provider, base_url),
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


def _check_cleaner_json_protocol(
    settings: AppSettings,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> ConnectivityResult:
    """Verify that the cleaner model can return its required JSON contract.

    A generic ``OK`` text reply proves only transport.  TM cleaning also
    depends on a machine-readable item id and suggestion field, so its active
    user-initiated test sends one minimal synthetic item and validates that
    response shape before reporting success.
    """
    del timeout_seconds  # Engine adapters own their request timeout settings.
    from core.api_config_check import check_translation_api_config
    from core.engine_dispatcher import build_engine
    from core.model_roles import ROLE_CLEANER, resolve_effective_model_config, settings_for_text_role
    from engines.base_engine import strip_markdown_json

    config = resolve_effective_model_config(settings, ROLE_CLEANER)
    cleaner_settings = settings_for_text_role(settings, ROLE_CLEANER)
    preflight = check_translation_api_config(cleaner_settings)
    if not preflight.ok:
        return ConnectivityResult(
            ok=False,
            status=preflight.status,
            message=preflight.message,
            provider=config.provider,
            model=config.model,
            detail=preflight.detail,
        )
    try:
        raw = build_engine(cleaner_settings).chat(
            CLEANER_TEST_SYSTEM_PROMPT,
            CLEANER_TEST_USER_PROMPT,
        )
        payload = json.loads(strip_markdown_json(str(raw or "")))
        if (
            not isinstance(payload, list)
            or len(payload) != 1
            or not isinstance(payload[0], dict)
            or str(payload[0].get("id") or "") != "connectivity-probe"
            or not isinstance(payload[0].get("suggested"), str)
        ):
            raise ValueError("清洗测试响应未满足 JSON 数组、id 与 suggested 字段协议。")
    except Exception as exc:  # noqa: BLE001 - converted to UI-safe result.
        return ConnectivityResult(
            ok=False,
            status="invalid_cleaner_protocol",
            message=(
                "深度清洗连接测试失败："
                f"{_sanitize_error_message(exc, secret=config.api_key)}"
            ),
            provider=config.provider,
            model=config.model,
            detail=normalize_cloud_base_url(config.provider, config.base_url),
        )
    return ConnectivityResult(
        ok=True,
        status="ok",
        message=f"{config.label}连接与 JSON 清洗协议测试通过。",
        provider=config.provider,
        model=config.model,
        detail=normalize_cloud_base_url(config.provider, config.base_url),
    )


def check_connectivity(
    settings: AppSettings,
    *,
    role: str = "translation",
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> ConnectivityResult:
    """Run a text-role test and persist it on that role's owning settings.

    ``cleaner`` shares the text transport surface but has an additional JSON
    contract.  Its effective engine configuration is constructed from a
    temporary copy, while the availability result is deliberately written back
    to the original cleaner role instead of the copied translation engine.
    """
    from core.model_roles import (
        ROLE_CLEANER,
        ROLE_TRANSLATION,
        model_config_signature,
        record_model_role_availability,
        resolve_effective_model_config,
    )

    normalized_role = str(role or ROLE_TRANSLATION).strip()
    if normalized_role not in {ROLE_TRANSLATION, ROLE_CLEANER}:
        raise ValueError("连接测试只支持翻译模型或深度清洗模型。")
    config = resolve_effective_model_config(settings, normalized_role)
    result = (
        _check_cleaner_json_protocol(settings, timeout_seconds=timeout_seconds)
        if normalized_role == ROLE_CLEANER
        else _check_connectivity(settings, timeout_seconds=timeout_seconds)
    )
    try:
        record_model_role_availability(
            settings,
            normalized_role,
            ok=result.ok,
            message=result.message,
            signature=model_config_signature(config),
            checked_at=datetime.now().isoformat(timespec="seconds"),
        )
    except Exception:
        # A connectivity result is still useful even if status bookkeeping
        # cannot be updated for malformed settings.
        pass
    return result
