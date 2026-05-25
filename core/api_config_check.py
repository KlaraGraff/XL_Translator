"""Preflight checks for translation backend configuration.

These checks only verify that required local configuration is present. They do
not send network requests or prove the API is reachable.
"""

from __future__ import annotations

from dataclasses import dataclass

from config import CLOUD_ENGINES
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
        if not str(engine.ollama_model or "").strip():
            return ApiConfigCheckResult(
                ok=False,
                status="missing_local_model",
                message="请先配置本地 Ollama 模型名称，再开始翻译。",
            )
        return ApiConfigCheckResult(ok=True)

    provider = str(engine.cloud_provider or "").strip()
    if provider == "hermes":
        return _check_hermes_config()

    provider_label = _provider_label(provider)
    if not str(engine.cloud_model or "").strip():
        return ApiConfigCheckResult(
            ok=False,
            status="missing_model",
            message=f"{provider_label} 尚未填写模型名称。请先在左侧“模型配置”中完成配置。",
        )

    if provider in {"custom_openai", "siliconflow", "lanyi"} and not str(engine.cloud_base_url or "").strip():
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


def _check_hermes_config() -> ApiConfigCheckResult:
    try:
        from engines.hermes_engine import load_hermes_runtime_routes

        routes = load_hermes_runtime_routes()
    except Exception as exc:  # noqa: BLE001 - user-facing config validation
        return ApiConfigCheckResult(
            ok=False,
            status="hermes_config_error",
            message="Hermes 内置引擎尚未完成配置。请先配置 Hermes 主模型，或切换到其他云端服务商。",
            detail=str(exc),
        )

    if not routes:
        return ApiConfigCheckResult(
            ok=False,
            status="hermes_missing_route",
            message="Hermes 内置引擎未找到可用模型路由。请先配置 Hermes 主模型，或切换到其他云端服务商。",
        )

    route = routes[0]
    if not str(route.api_key or "").strip():
        hint = f"环境变量 {route.api_key_env}" if route.api_key_env else "对应服务商 API Key"
        return ApiConfigCheckResult(
            ok=False,
            status="hermes_missing_api_key",
            message="Hermes 内置引擎未找到 API Key。请先补全 Hermes 配置后再开始翻译。",
            detail=f"缺少：{hint}",
        )

    return ApiConfigCheckResult(ok=True)


def _provider_label(provider: str) -> str:
    for label, value in CLOUD_ENGINES.items():
        if value == provider:
            return label
    return provider or "当前云端 API"
