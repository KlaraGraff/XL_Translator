"""Preflight checks for translation backend configuration.

These checks only verify that required local configuration is present. They do
not send network requests or prove the API is reachable.
"""

from __future__ import annotations

from dataclasses import dataclass

from config import CLOUD_ENGINES, LM_STUDIO_BASE_URL, OLLAMA_BASE_URL, normalize_cloud_base_url
from settings import AppSettings, get_key


@dataclass(frozen=True)
class ApiConfigCheckResult:
    ok: bool
    message: str = ""
    status: str = "ok"
    detail: str = ""


def check_translation_api_config(settings: AppSettings) -> ApiConfigCheckResult:
    """Return whether the selected translation backend has required config."""
    engine = settings.engine
    if engine.mode == "local":
        provider = str(engine.local_provider or "ollama").strip()
        model = str(engine.local_model or engine.ollama_model or "").strip()
        base_url = str(engine.local_base_url or "").strip()
        if not model:
            return ApiConfigCheckResult(
                ok=False,
                status="missing_local_model",
                message="请先配置本地模型名称，再开始翻译。",
            )
        if provider == "ollama" and not base_url:
            engine.local_base_url = OLLAMA_BASE_URL
        elif provider == "lm_studio" and not base_url:
            engine.local_base_url = LM_STUDIO_BASE_URL
        elif provider == "custom_local" and not base_url:
            return ApiConfigCheckResult(
                ok=False,
                status="missing_local_base_url",
                message="请先填写本地模型服务 Base URL，再开始翻译。",
            )
        return ApiConfigCheckResult(ok=True)

    provider = str(engine.cloud_provider or "").strip()
    provider_label = _provider_label(provider)
    if not str(engine.cloud_model or "").strip():
        return ApiConfigCheckResult(
            ok=False,
            status="missing_model",
            message=f"{provider_label} 尚未填写模型名称。请先在左侧“模型配置”中完成配置。",
        )

    if provider in {"custom_openai"} and not normalize_cloud_base_url(provider, engine.cloud_base_url):
        return ApiConfigCheckResult(
            ok=False,
            status="missing_base_url",
            message=f"{provider_label} 尚未填写 Base URL。请先在左侧“模型配置”中完成配置。",
        )

    if not str(get_key(provider) or "").strip():
        return ApiConfigCheckResult(
            ok=False,
            status="missing_api_key",
            message=f"{provider_label} 尚未填写 API Key。请先在左侧“模型配置”中完成配置后再开始翻译。",
        )

    return ApiConfigCheckResult(ok=True)


def _provider_label(provider: str) -> str:
    for label, value in CLOUD_ENGINES.items():
        if value == provider:
            return label
    return provider or "当前云端 API"
