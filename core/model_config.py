"""Portable model configuration import and export helpers.

The JSON schema is shared by the native Qt UI and the sidecar API so users can
move model profiles, scoped credentials, and throughput tuning without a UI
dependency.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app_meta import APP_NAME, APP_VERSION
from config import CLOUD_ENGINES, normalize_cloud_base_url
from core.model_roles import (
    ROLE_CLEANER,
    ROLE_IMAGE,
    ROLE_PDF_REVIEW,
    ROLE_TRANSLATION,
    SOURCE_INDEPENDENT,
    resolve_effective_model_config,
    role_label,
)
from core.model_throughput import get_model_throughput, set_model_throughput
from settings import AppSettings, get_key, parse_api_key_scope, save_key

MODEL_CONFIG_EXPORT_TYPE = "translator_model_config"
MODEL_CONFIG_EXPORT_VERSION = 2
MODEL_CONFIG_SETTING_KEYS = (
    "engine",
    "cleaner_model_role",
    "image_model_role",
    "pdf_review_model_role",
)
MODEL_CONFIG_CLOUD_FIELDS = (
    "cloud_provider",
    "cloud_model",
    "cloud_base_url",
    "cloud_provider_configs",
)
MODEL_CONFIG_ROLE_CLOUD_FIELDS = (
    "source_role",
    *MODEL_CONFIG_CLOUD_FIELDS,
)
MODEL_PROFILE_ROLES = (
    ("translation", ROLE_TRANSLATION),
    ("cleaner", ROLE_CLEANER),
    ("pdf_translation", ROLE_IMAGE),
    ("pdf_review", ROLE_PDF_REVIEW),
)
MODEL_PROFILE_ROLE_BY_KEY = {
    profile_key: role for profile_key, role in MODEL_PROFILE_ROLES
}
MODEL_PROFILE_SETTING_KEY_BY_ROLE = {
    ROLE_TRANSLATION: "engine",
    ROLE_CLEANER: "cleaner_model_role",
    ROLE_IMAGE: "image_model_role",
    ROLE_PDF_REVIEW: "pdf_review_model_role",
}

ApiKeyGetter = Callable[[str, str], str]
ApiKeySaver = Callable[..., None]


@dataclass(frozen=True)
class ImportedModelConfig:
    """Validated model-settings and key updates extracted from JSON."""

    model_config: dict[str, dict[str, Any]]
    api_keys: dict[str, str]
    scoped_api_keys: list[dict[str, str]]
    throughput_profiles: dict[str, dict[str, Any]]
    profile_throughputs: dict[str, dict[str, Any]]


def build_model_config_export_payload(
    settings: AppSettings,
    *,
    get_api_key: ApiKeyGetter = get_key,
) -> dict[str, Any]:
    """Build the current versioned, credential-inclusive export payload."""
    return {
        "type": MODEL_CONFIG_EXPORT_TYPE,
        "version": MODEL_CONFIG_EXPORT_VERSION,
        "app": APP_NAME,
        "app_version": APP_VERSION,
        "model_profiles": _model_profiles_for_export(settings, get_api_key=get_api_key),
    }


def parse_model_config_import(raw: object) -> ImportedModelConfig:
    """Parse legacy and current model-configuration JSON payloads."""
    if not isinstance(raw, dict):
        raise ValueError("Imported configuration must be a JSON object.")
    if isinstance(raw.get("model_profiles"), dict):
        return _parse_model_profiles(raw)

    source = raw.get("model_config")
    if not isinstance(source, dict):
        settings_payload = raw.get("settings")
        source = settings_payload if isinstance(settings_payload, dict) else raw

    model_config: dict[str, dict[str, Any]] = {}
    for key in MODEL_CONFIG_SETTING_KEYS:
        value = source.get(key)
        if not isinstance(value, dict):
            continue
        allowed_fields = (
            MODEL_CONFIG_CLOUD_FIELDS
            if key == "engine"
            else MODEL_CONFIG_ROLE_CLOUD_FIELDS
        )
        cloud_values = {
            field: value[field]
            for field in allowed_fields
            if field in value
        }
        if cloud_values:
            model_config[key] = cloud_values
    if not model_config:
        raise ValueError("No cloud model configuration was found in the import.")

    keys_raw = raw.get("api_keys", {})
    if keys_raw in (None, ""):
        keys_raw = {}
    if not isinstance(keys_raw, dict):
        raise ValueError("api_keys must be a JSON object.")
    cloud_providers = set(CLOUD_ENGINES.values())
    api_keys = {
        str(provider or "").strip(): str(api_key or "").strip()
        for provider, api_key in keys_raw.items()
        if str(provider or "").strip() in cloud_providers
        and str(api_key or "").strip()
    }
    return ImportedModelConfig(
        model_config=model_config,
        api_keys=api_keys,
        scoped_api_keys=_parse_scoped_api_keys(raw),
        throughput_profiles=_parse_throughput_profiles(raw),
        profile_throughputs={},
    )


def apply_model_config_import(
    settings: AppSettings,
    imported: ImportedModelConfig,
    *,
    save_api_key: ApiKeySaver = save_key,
) -> AppSettings:
    """Apply an import to a copied settings model and persist supplied keys.

    The caller owns persistence of the returned settings object. This lets the
    API and the native UI preserve their existing save timing and error UX.
    """
    payload = settings.model_dump(mode="json")
    for key in MODEL_CONFIG_SETTING_KEYS:
        if key not in imported.model_config:
            continue
        current = dict(payload.get(key) or {})
        current.update(imported.model_config[key])
        if key == "engine" and "mode" not in imported.model_config[key]:
            current["mode"] = "cloud"
        payload[key] = current
    if imported.throughput_profiles:
        profiles = dict(payload.get("model_throughput_profiles") or {})
        profiles.update(imported.throughput_profiles)
        payload["model_throughput_profiles"] = profiles

    updated = AppSettings.model_validate(payload)
    for provider, api_key in imported.api_keys.items():
        save_api_key(provider, api_key)
    for entry in imported.scoped_api_keys:
        save_api_key(entry["provider"], entry["api_key"], entry["base_url"])
    for role, throughput in imported.profile_throughputs.items():
        try:
            config = resolve_effective_model_config(updated, role)
            set_model_throughput(
                updated,
                config,
                batch_size=throughput.get("batch_size"),
                concurrency=throughput.get("concurrency"),
            )
        except Exception:
            continue
    return updated


def _model_profiles_for_export(
    settings: AppSettings,
    *,
    get_api_key: ApiKeyGetter,
) -> dict[str, dict[str, Any]]:
    payload = settings.model_dump(mode="json")
    profiles: dict[str, dict[str, Any]] = {}
    for profile_key, role in MODEL_PROFILE_ROLES:
        setting_key = MODEL_PROFILE_SETTING_KEY_BY_ROLE[role]
        owner = dict(payload.get(setting_key) or {})
        profile: dict[str, Any] = {
            "role": role,
            "label": role_label(role),
            "cloud": _cloud_profile_for_export(owner, get_api_key=get_api_key),
            "effective": _effective_profile_for_export(settings, role),
            "throughput": _throughput_profile_for_export(settings, role),
        }
        if role == ROLE_TRANSLATION:
            profile["mode"] = str(owner.get("mode") or "cloud").strip() or "cloud"
            profile["source_role"] = SOURCE_INDEPENDENT
            profile["local"] = _local_profile_for_export(owner)
        else:
            profile["source_role"] = str(
                owner.get("source_role") or SOURCE_INDEPENDENT
            ).strip()
        profiles[profile_key] = profile
    return profiles


def _cloud_profile_for_export(
    owner: dict[str, Any],
    *,
    get_api_key: ApiKeyGetter,
) -> dict[str, Any]:
    provider = str(owner.get("cloud_provider") or "").strip()
    base_url = normalize_cloud_base_url(
        provider,
        str(owner.get("cloud_base_url") or "").strip(),
    )
    profile: dict[str, Any] = {
        "provider": provider,
        "model": str(owner.get("cloud_model") or "").strip(),
        "base_url": base_url,
        "provider_configs": _provider_configs_for_export(owner, get_api_key=get_api_key),
    }
    api_key = str(get_api_key(provider, base_url) or "").strip()
    if api_key:
        profile["api_key"] = api_key
    return profile


def _provider_configs_for_export(
    owner: dict[str, Any],
    *,
    get_api_key: ApiKeyGetter,
) -> dict[str, dict[str, str]]:
    cloud_providers = set(CLOUD_ENGINES.values())
    provider_configs = dict(owner.get("cloud_provider_configs") or {})
    current_provider = str(owner.get("cloud_provider") or "").strip()
    if current_provider in cloud_providers:
        provider_configs.setdefault(
            current_provider,
            {
                "cloud_model": str(owner.get("cloud_model") or "").strip(),
                "cloud_base_url": str(owner.get("cloud_base_url") or "").strip(),
            },
        )

    profiles: dict[str, dict[str, str]] = {}
    for provider, raw_config in sorted(provider_configs.items()):
        provider = str(provider or "").strip()
        if provider not in cloud_providers or not isinstance(raw_config, dict):
            continue
        base_url = normalize_cloud_base_url(
            provider,
            str(raw_config.get("cloud_base_url") or "").strip(),
        )
        entry = {
            "model": str(raw_config.get("cloud_model") or "").strip(),
            "base_url": base_url,
        }
        api_key = str(get_api_key(provider, base_url) or "").strip()
        if api_key:
            entry["api_key"] = api_key
        profiles[provider] = entry
    return profiles


def _local_profile_for_export(engine: dict[str, Any]) -> dict[str, str]:
    return {
        "provider": str(engine.get("local_provider") or "").strip(),
        "model": str(
            engine.get("local_model") or engine.get("ollama_model") or ""
        ).strip(),
        "base_url": str(engine.get("local_base_url") or "").strip(),
    }


def _throughput_profile_for_export(settings: AppSettings, role: str) -> dict[str, Any]:
    try:
        config = resolve_effective_model_config(settings, role)
        throughput = get_model_throughput(settings, config)
    except Exception:
        return {}
    profile: dict[str, Any] = {
        "profile_key": throughput.profile_key,
        "concurrency": throughput.concurrency,
    }
    if throughput.batch_size is not None:
        profile["batch_size"] = throughput.batch_size
    return profile


def _effective_profile_for_export(settings: AppSettings, role: str) -> dict[str, Any]:
    try:
        config = resolve_effective_model_config(settings, role)
    except Exception:
        return {}
    profile: dict[str, Any] = {
        "mode": config.mode,
        "provider": config.provider,
        "model": config.model,
        "base_url": config.base_url,
        "source_role": config.source_role,
        "follows": config.follows,
    }
    if config.api_key:
        profile["api_key"] = config.api_key
    return profile


def _parse_model_profiles(raw: dict[str, Any]) -> ImportedModelConfig:
    profiles_raw = raw.get("model_profiles")
    if not isinstance(profiles_raw, dict):
        raise ValueError("model_profiles must be a JSON object.")

    model_config: dict[str, dict[str, Any]] = {}
    scoped_api_keys: list[dict[str, str]] = []
    profile_throughputs: dict[str, dict[str, Any]] = {}

    def add_key(provider: str, base_url: str, api_key: str) -> None:
        provider = str(provider or "").strip()
        if provider not in set(CLOUD_ENGINES.values()):
            return
        api_key = str(api_key or "").strip()
        if not api_key:
            return
        scoped_api_keys.append(
            {
                "provider": provider,
                "base_url": normalize_cloud_base_url(provider, base_url),
                "api_key": api_key,
            }
        )

    def cloud_values(profile: dict[str, Any]) -> dict[str, Any]:
        cloud = profile.get("cloud", {})
        if not isinstance(cloud, dict):
            cloud = {}
        provider = str(
            cloud.get("provider") or cloud.get("cloud_provider") or ""
        ).strip()
        model = str(cloud.get("model") or cloud.get("cloud_model") or "").strip()
        base_url = normalize_cloud_base_url(
            provider,
            str(cloud.get("base_url") or cloud.get("cloud_base_url") or "").strip(),
        )
        add_key(provider, base_url, str(cloud.get("api_key") or ""))

        provider_configs: dict[str, dict[str, str]] = {}
        configs_raw = cloud.get("provider_configs") or cloud.get("cloud_provider_configs")
        if isinstance(configs_raw, dict):
            for raw_provider, raw_config in configs_raw.items():
                config_provider = str(raw_provider or "").strip()
                if (
                    config_provider not in set(CLOUD_ENGINES.values())
                    or not isinstance(raw_config, dict)
                ):
                    continue
                config_base_url = normalize_cloud_base_url(
                    config_provider,
                    str(
                        raw_config.get("base_url")
                        or raw_config.get("cloud_base_url")
                        or ""
                    ).strip(),
                )
                provider_configs[config_provider] = {
                    "cloud_model": str(
                        raw_config.get("model") or raw_config.get("cloud_model") or ""
                    ).strip(),
                    "cloud_base_url": config_base_url,
                }
                add_key(
                    config_provider,
                    config_base_url,
                    str(raw_config.get("api_key") or ""),
                )
        return {
            "cloud_provider": provider,
            "cloud_model": model,
            "cloud_base_url": base_url,
            "cloud_provider_configs": provider_configs,
        }

    for profile_key, profile in profiles_raw.items():
        normalized_key = str(profile_key or "").strip()
        role = MODEL_PROFILE_ROLE_BY_KEY.get(normalized_key)
        if role is None and normalized_key == ROLE_IMAGE:
            role = ROLE_IMAGE
        if role is None or not isinstance(profile, dict):
            continue

        setting_key = MODEL_PROFILE_SETTING_KEY_BY_ROLE[role]
        values = cloud_values(profile)
        if role == ROLE_TRANSLATION:
            mode = str(profile.get("mode") or "cloud").strip()
            values["mode"] = mode if mode in {"cloud", "local"} else "cloud"
            local = profile.get("local", {})
            if isinstance(local, dict):
                values.update(
                    {
                        "local_provider": str(local.get("provider") or "").strip(),
                        "local_model": str(local.get("model") or "").strip(),
                        "local_base_url": str(local.get("base_url") or "").strip(),
                    }
                )
                values["ollama_model"] = values["local_model"]
        else:
            source_role = str(profile.get("source_role") or SOURCE_INDEPENDENT).strip()
            values["source_role"] = ROLE_IMAGE if source_role == "pdf_translation" else source_role
        model_config[setting_key] = values

        throughput = profile.get("throughput")
        if isinstance(throughput, dict):
            profile_throughputs[role] = throughput

    if not model_config:
        raise ValueError("No model configuration was found in the import.")
    return ImportedModelConfig(
        model_config=model_config,
        api_keys={},
        scoped_api_keys=scoped_api_keys,
        throughput_profiles={},
        profile_throughputs=profile_throughputs,
    )


def _parse_throughput_profiles(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    profiles = raw.get("model_throughput_profiles", {})
    if profiles in (None, ""):
        return {}
    if not isinstance(profiles, dict):
        raise ValueError("model_throughput_profiles must be a JSON object.")
    return {
        str(key): value
        for key, value in profiles.items()
        if str(key).strip() and isinstance(value, dict)
    }


def _parse_scoped_api_keys(raw: dict[str, Any]) -> list[dict[str, str]]:
    scoped_raw = raw.get("scoped_api_keys", [])
    if scoped_raw in (None, ""):
        scoped_raw = []
    cloud_providers = set(CLOUD_ENGINES.values())
    entries: list[dict[str, str]] = []

    def add_entry(provider: str, base_url: str, api_key: str) -> None:
        provider = str(provider or "").strip()
        if provider not in cloud_providers:
            return
        api_key = str(api_key or "").strip()
        if not api_key:
            return
        entries.append(
            {
                "provider": provider,
                "base_url": normalize_cloud_base_url(provider, base_url),
                "api_key": api_key,
            }
        )

    if isinstance(scoped_raw, list):
        for entry in scoped_raw:
            if not isinstance(entry, dict):
                continue
            provider = str(entry.get("provider") or "").strip()
            base_url = str(entry.get("base_url") or "").strip()
            if not provider:
                provider, parsed_base_url = parse_api_key_scope(entry.get("scope", ""))
                base_url = base_url or parsed_base_url
            add_entry(provider, base_url, str(entry.get("api_key") or ""))
        return entries

    if isinstance(scoped_raw, dict):
        for scope, value in scoped_raw.items():
            provider, base_url = parse_api_key_scope(scope)
            if isinstance(value, dict):
                provider = str(value.get("provider") or provider).strip()
                base_url = str(value.get("base_url") or base_url).strip()
                api_key = str(value.get("api_key") or "").strip()
            else:
                api_key = str(value or "").strip()
            add_entry(provider, base_url, api_key)
        return entries

    raise ValueError("scoped_api_keys must be an array or JSON object.")
