"""Fetch model lists from OpenAI-compatible providers."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import httpx

from config import LANYI_BASE_URL


DEFAULT_MODEL_LIST_TIMEOUT_SECONDS = 12.0
OPENAI_COMPATIBLE_MODEL_PROVIDERS = {
    "openai",
    "custom_openai",
    "lanyi",
    "siliconflow",
}


@dataclass(frozen=True)
class ModelCatalogResult:
    ok: bool
    models: list[str]
    message: str
    detail: str = ""
    status: str = "ok"


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
    if provider == "openai":
        return "https://api.openai.com/v1"
    if provider == "lanyi" and not normalized:
        return LANYI_BASE_URL.rstrip("/")
    return normalized


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

    if provider_name not in OPENAI_COMPATIBLE_MODEL_PROVIDERS:
        return ModelCatalogResult(
            ok=False,
            models=[],
            status="unsupported_provider",
            message="当前服务商暂不支持自动获取模型列表。",
        )
    if not key:
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

    url = _append_url_path(normalized_base_url, "/models")
    headers = {"Authorization": f"Bearer {key}"}

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

    return ModelCatalogResult(
        ok=True,
        models=models,
        status="ok",
        message=f"已获取 {len(models)} 个模型。",
        detail=url,
    )
