"""Fetch model lists from OpenAI-compatible providers."""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass

import httpx

from config import LM_STUDIO_BASE_URL, OLLAMA_BASE_URL, normalize_cloud_base_url


DEFAULT_MODEL_LIST_TIMEOUT_SECONDS = 12.0
OPENAI_COMPATIBLE_MODEL_PROVIDERS = {
    "openai",
    "custom_openai",
    "lanyi",
    "siliconflow",
    "lm_studio",
    "custom_local",
}

# Model discovery is intentionally a process-local cache.  It is keyed by the
# effective connection identity (provider, normalized base URL and a
# non-secret API-key fingerprint), never persisted to settings or exports.
_MODEL_CATALOG_CACHE: dict[str, "ModelCatalogResult"] = {}
_MODEL_CATALOG_CACHE_LOCK = threading.RLock()


@dataclass(frozen=True)
class ModelCatalogResult:
    ok: bool
    models: list[str]
    message: str
    detail: str = ""
    status: str = "ok"


def clear_model_catalog_cache() -> None:
    """Drop the session-only model catalog cache.

    Callers use this after an explicit connection change or in isolated tests.
    No user settings or on-disk data are touched.
    """
    with _MODEL_CATALOG_CACHE_LOCK:
        _MODEL_CATALOG_CACHE.clear()


def _copy_result(result: ModelCatalogResult) -> ModelCatalogResult:
    """Return a defensive copy so callers cannot mutate cached model names."""
    return ModelCatalogResult(
        ok=result.ok,
        models=list(result.models),
        message=result.message,
        detail=result.detail,
        status=result.status,
    )


def _get_cached_result(signature: str) -> ModelCatalogResult | None:
    with _MODEL_CATALOG_CACHE_LOCK:
        result = _MODEL_CATALOG_CACHE.get(signature)
    return _copy_result(result) if result is not None else None


def _cache_success(signature: str, result: ModelCatalogResult) -> None:
    if not result.ok:
        return
    with _MODEL_CATALOG_CACHE_LOCK:
        _MODEL_CATALOG_CACHE[signature] = _copy_result(result)


def _hash_secret(secret: str) -> str:
    if not secret:
        return ""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12]


def build_model_catalog_signature(
    *,
    provider: str,
    api_key: str,
    base_url: str,
) -> str:
    """Return a non-secret signature for cached model-list results."""
    provider_name = str(provider or "").strip()
    return "|".join(
        [
            provider_name,
            _normalize_base_url(provider_name, base_url),
            _hash_secret(str(api_key or "").strip()),
        ]
    )


def _sanitize_error_message(exc: Exception, *, secret: str = "") -> str:
    message = str(exc).strip() or exc.__class__.__name__
    if secret:
        message = message.replace(secret, "***")
    if len(message) > 500:
        message = message[:497] + "..."
    return message


def _append_url_path(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _normalize_base_url(provider: str, base_url: str) -> str:
    normalized = str(base_url or "").strip().rstrip("/")
    if provider == "lm_studio" and not normalized:
        return LM_STUDIO_BASE_URL.rstrip("/")
    if provider == "ollama" and not normalized:
        return OLLAMA_BASE_URL.rstrip("/")
    if provider in {"ollama", "lm_studio", "custom_local"}:
        return normalized
    return normalize_cloud_base_url(provider, normalized)


def _extract_model_ids(payload: object) -> list[str]:
    if not isinstance(payload, dict):
        return []

    raw_items = payload.get("data")
    if raw_items is None:
        raw_items = payload.get("models")
    if not isinstance(raw_items, list):
        return []

    models: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        if isinstance(item, str):
            model_id = item.strip()
        elif isinstance(item, dict):
            model_id = str(
                item.get("id")
                or item.get("model")
                or item.get("name")
                or ""
            ).strip()
        else:
            model_id = ""

        if model_id and model_id not in seen:
            seen.add(model_id)
            models.append(model_id)
    return models


def fetch_ollama_models(
    *,
    base_url: str,
    timeout_seconds: float = DEFAULT_MODEL_LIST_TIMEOUT_SECONDS,
) -> ModelCatalogResult:
    """Fetch locally installed Ollama model names from `/api/tags`."""
    normalized_base_url = _normalize_base_url("ollama", base_url)
    signature = build_model_catalog_signature(
        provider="ollama",
        api_key="",
        base_url=normalized_base_url,
    )
    cached = _get_cached_result(signature)
    if cached is not None:
        return cached
    url = _append_url_path(normalized_base_url, "/api/tags")
    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            response = client.get(url)
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:  # noqa: BLE001 - converted to user-facing result.
        return ModelCatalogResult(
            ok=False,
            models=[],
            status="request_failed",
            message=f"Ollama 模型列表获取失败：{_sanitize_error_message(exc)}",
            detail=url,
        )

    models = _extract_model_ids(payload)
    if not models:
        return ModelCatalogResult(
            ok=False,
            models=[],
            status="empty_models",
            message="Ollama 已响应，但没有读取到已安装模型。",
            detail=url,
        )
    result = ModelCatalogResult(
        ok=True,
        models=models,
        status="ok",
        message=f"已获取 {len(models)} 个本地模型。",
        detail=url,
    )
    _cache_success(signature, result)
    return result


def fetch_openai_compatible_models(
    *,
    provider: str,
    api_key: str,
    base_url: str,
    timeout_seconds: float = DEFAULT_MODEL_LIST_TIMEOUT_SECONDS,
) -> ModelCatalogResult:
    """Fetch `/models` for OpenAI-compatible providers."""
    provider_name = str(provider or "").strip()
    key = str(api_key or "").strip()
    normalized_base_url = _normalize_base_url(provider_name, base_url)

    if provider_name == "ollama":
        return fetch_ollama_models(
            base_url=base_url,
            timeout_seconds=timeout_seconds,
        )
    if provider_name not in OPENAI_COMPATIBLE_MODEL_PROVIDERS:
        return ModelCatalogResult(
            ok=False,
            models=[],
            status="unsupported_provider",
            message="当前服务商暂不支持自动获取模型列表。",
        )
    local_openai_provider = provider_name in {"lm_studio", "custom_local"}
    if not key and not local_openai_provider:
        return ModelCatalogResult(
            ok=False,
            models=[],
            status="missing_api_key",
            message="请先填写 API Key，再获取模型列表。",
        )
    if not normalized_base_url:
        return ModelCatalogResult(
            ok=False,
            models=[],
            status="missing_base_url",
            message="请先填写 Base URL，再获取模型列表。",
        )

    signature = build_model_catalog_signature(
        provider=provider_name,
        api_key=key,
        base_url=normalized_base_url,
    )
    cached = _get_cached_result(signature)
    if cached is not None:
        return cached

    url = _append_url_path(normalized_base_url, "/models")
    headers = {"Authorization": f"Bearer {key}"} if key else {}

    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            response = client.get(url, headers=headers)
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:  # noqa: BLE001 - converted to user-facing result.
        return ModelCatalogResult(
            ok=False,
            models=[],
            status="request_failed",
            message=f"模型列表获取失败：{_sanitize_error_message(exc, secret=key)}",
            detail=url,
        )

    models = _extract_model_ids(payload)
    if not models:
        return ModelCatalogResult(
            ok=False,
            models=[],
            status="empty_models",
            message="接口已响应，但没有读取到可用模型。",
            detail=url,
        )

    result = ModelCatalogResult(
        ok=True,
        models=models,
        status="ok",
        message=f"已获取 {len(models)} 个模型。",
        detail=url,
    )
    _cache_success(signature, result)
    return result
