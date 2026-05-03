"""
Hermes 内置翻译引擎。

职责：
1. 读取 ~/.hermes/config.yaml 的主模型路由；
2. 从 ~/.hermes/.env 或当前进程环境解析对应 API Key；
3. 将 Hermes 路由映射为当前项目内的具体翻译引擎。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

from engines.base_engine import TranslationEngine


@dataclass(frozen=True)
class HermesRuntimeRoute:
    provider: str
    model: str
    base_url: str = ""
    api_key_env: str = ""
    api_key: str = ""
    api_mode: str = ""

    @property
    def label(self) -> str:
        if self.base_url:
            return f"{self.provider}/{self.model} @ {self.base_url}"
        return f"{self.provider}/{self.model}"


def _parse_scalar(raw: str) -> str:
    value = str(raw or "").strip()
    if value in {"", "null", "Null", "NULL", "~", "''", '""'}:
        return ""
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    return value


def _load_hermes_yaml(path: Path) -> dict[str, object]:
    if not path.exists():
        raise FileNotFoundError(f"未找到 Hermes 配置文件：{path}")

    model_payload: dict[str, str] = {}
    current_section = ""

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue

        indent = len(raw_line) - len(raw_line.lstrip(" "))
        stripped = raw_line.strip()

        if indent == 0 and not stripped.startswith("- "):
            current_section = stripped[:-1] if stripped.endswith(":") else ""
            continue

        if current_section == "model" and indent >= 2 and ":" in stripped:
            key, value = stripped.split(":", 1)
            model_payload[key.strip()] = _parse_scalar(value)
            continue

    return {"model": model_payload}


def _load_hermes_env_values(env_path: Path) -> dict[str, str]:
    if not env_path.exists():
        return {}
    return {
        str(key): str(value)
        for key, value in dotenv_values(env_path).items()
        if key and value is not None
    }


def _resolve_secret(env_name: str, env_values: dict[str, str]) -> str:
    if not env_name:
        return ""
    return str(os.getenv(env_name) or env_values.get(env_name) or "").strip()


def _normalize_route(
    payload: dict[str, str],
    *,
    env_values: dict[str, str],
    default_model: str,
) -> HermesRuntimeRoute:
    provider = str(payload.get("provider") or "").strip()
    if not provider:
        raise ValueError("Hermes 路由缺少 provider")

    model = str(payload.get("model") or default_model or "").strip()
    if not model:
        raise ValueError(f"Hermes 路由缺少 model：provider={provider}")

    api_key_env = str(payload.get("api_key_env") or "").strip()
    return HermesRuntimeRoute(
        provider=provider,
        model=model,
        base_url=str(payload.get("base_url") or "").strip().rstrip("/"),
        api_key_env=api_key_env,
        api_key=_resolve_secret(api_key_env, env_values),
        api_mode=str(payload.get("api_mode") or "").strip(),
    )


def load_hermes_runtime_routes(
    config_path: Path | None = None,
    env_path: Path | None = None,
) -> list[HermesRuntimeRoute]:
    hermes_home = Path.home() / ".hermes"
    config_file = config_path or (hermes_home / "config.yaml")
    env_file = env_path or (hermes_home / ".env")

    payload = _load_hermes_yaml(config_file)
    env_values = _load_hermes_env_values(env_file)
    model_payload = dict(payload.get("model") or {})
    default_model = str(model_payload.get("default") or "").strip()

    primary_route = _normalize_route(
        model_payload,
        env_values=env_values,
        default_model=default_model,
    )
    return [primary_route]


def _build_route_engine(route: HermesRuntimeRoute) -> TranslationEngine:
    provider = route.provider.lower()

    if provider == "anthropic":
        from engines.claude_engine import ClaudeEngine

        return ClaudeEngine(
            api_key=route.api_key,
            model=route.model,
            base_url=route.base_url,
        )

    openai_compatible_providers = {
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
    if provider in openai_compatible_providers or route.base_url:
        from engines.openai_engine import OpenAIEngine

        return OpenAIEngine(
            api_key=route.api_key,
            model=route.model,
            base_url=route.base_url,
            api_mode=route.api_mode,
        )

    raise ValueError(f"Hermes 内置引擎暂不支持 provider={route.provider}")


class HermesEngine(TranslationEngine):
    def __init__(
        self,
        config_path: Path | None = None,
        env_path: Path | None = None,
    ):
        self._routes = load_hermes_runtime_routes(config_path=config_path, env_path=env_path)
        self._engines: list[TranslationEngine | None] = [None] * len(self._routes)

    @property
    def engine_name(self) -> str:
        primary = self._routes[0]
        return f"hermes/{primary.model}"

    def _get_engine(self, index: int) -> TranslationEngine:
        cached = self._engines[index]
        if cached is None:
            cached = _build_route_engine(self._routes[index])
            self._engines[index] = cached
        return cached

    def _call_with_failover(self, method_name: str, *args, **kwargs):
        errors: list[str] = []
        for index, route in enumerate(self._routes):
            engine = self._get_engine(index)
            method = getattr(engine, method_name)
            try:
                return method(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - 需要把 Hermes 主路异常汇总给上层
                errors.append(f"{route.label}: {exc}")
        raise RuntimeError("Hermes 内置引擎所有路线均失败：" + " | ".join(errors))

    def translate_batch(
        self,
        texts: list[str],
        target_lang: str,
        system_prompt: str,
        source_lang: str = "zh",
    ) -> dict[str, str]:
        return self._call_with_failover(
            "translate_batch",
            texts,
            target_lang,
            system_prompt,
            source_lang=source_lang,
        )

    def chat(self, system: str, user: str) -> str:
        return self._call_with_failover("chat", system, user)
