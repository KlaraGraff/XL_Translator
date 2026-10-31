"""
User-editable settings persisted to local JSON files.
API keys are stored separately in keys.json with OS-level permissions.
"""
import getpass
import json
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import uuid
from contextlib import contextmanager
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from loguru import logger
from pydantic import BaseModel, Field, PrivateAttr, field_validator, model_validator

from config import (
    APP_DATA_DIR,
    BACKUPS_DIR,
    CLOUD_ENGINES,
    CONCURRENCY_DEFAULT,
    DEFAULT_CLOUD_MODEL,
    DEFAULT_CLOUD_PROVIDER,
    DEFAULT_CUSTOM_OPENAI_API_KEY,
    DEFAULT_CUSTOM_OPENAI_BASE_URL,
    DEFAULT_LOCAL_MODEL_PROVIDER,
    EXCEL_REVIEW_EXISTING_FILL_POLICY_DEFAULT,
    EXCEL_REVIEW_MARK_DEFAULT,
    LM_STUDIO_BASE_URL,
    LOCAL_MODEL_PROVIDERS,
    OLLAMA_BASE_URL,
    DEFAULT_MAX_LEN,
    PDF_PAGE_CONCURRENCY_SAFETY_CAP,
    PDF_PAGE_RETRY_ATTEMPTS_DEFAULT,
    PDF_PAGE_RETRY_ATTEMPTS_MAX,
    PDF_PAGE_RETRY_ATTEMPTS_MIN,
    REVIEW_MARK_COLOR_DEFAULTS,
    get_cloud_concurrency_bounds,
    get_concurrency_cap,
    get_default_concurrency,
    get_local_concurrency_bounds,
    KEYS_PATH,
    LEGACY_DOMAIN_PRESET_NAMES,
    NO_EXTRA_PROMPT,
    RESERVED_EXTRA_PROMPT_NAMES,
    SETTINGS_SCHEMA_VERSION,
    SETTINGS_PATH,
    WORD_BATCH_CHARS_DEFAULT,
    WORD_BATCH_PARAGRAPHS_DEFAULT,
    WORD_BATCH_SPLIT_CHARS_DEFAULT,
    WORD_REVIEW_HIGHLIGHT_COLOR_DEFAULT,
    WORD_REVIEW_HIGHLIGHT_DEFAULT,
    WORD_STRICT_RETRY_ATTEMPTS_DEFAULT,
    WORD_STRICT_RETRY_ATTEMPTS_MAX,
    RATE_FORMULA_BATCH_MAX,
    RATE_FORMULA_BATCH_MIN,
    RATE_ITEM_RETRY_MAX,
    RATE_ITEM_RETRY_MIN,
    REQUEST_RETRY_ATTEMPTS_MAX,
    REQUEST_RETRY_ATTEMPTS_MIN,
    PROCESS_RETENTION_FAILED_RUNS_DEFAULT,
    PROCESS_RETENTION_MB_DEFAULT,
    PROCESS_RETENTION_MB_MAX,
    PROCESS_RETENTION_MB_MIN,
    PROCESS_RETENTION_RUNS_MAX,
    PROCESS_RETENTION_RUNS_MIN,
    PROCESS_RETENTION_SUCCEEDED_RUNS_DEFAULT,
    WORD_STRICT_RETRY_ATTEMPTS_MIN,
    normalize_cloud_base_url,
    configured_cloud_base_url,
    is_valid_concurrency_unlock_code,
)

from core.text_transport import normalize_api_mode
from urllib.parse import urlsplit

from core.language_registry import (
    CustomTargetLang,
    get_default_source_lang,
    get_default_target_lang,
    get_supported_languages,
    get_supported_source_languages,
    is_supported_source_lang,
    is_supported_target_lang,
    normalize_custom_target_langs,
    normalize_recent_target_langs,
    remember_recent_target_lang,
    resolve_language_code,
    is_auto_source_lang,
)

_KEY_OVERRIDE_LOCAL = threading.local()
_LOCAL_FILE_LOCKS: dict[str, threading.RLock] = {}
_LOCAL_FILE_LOCKS_GUARD = threading.Lock()
API_KEY_SCOPE_SEPARATOR = "::"


def _normalize_api_key_provider(provider: str) -> str:
    return str(provider or "").strip()


def _normalize_api_key_base_url(provider: str, base_url: str = "") -> str:
    raw_base_url = str(base_url or "").strip()
    if not raw_base_url:
        return ""
    return normalize_cloud_base_url(provider, raw_base_url)


def api_key_scope(provider: str, base_url: str = "") -> str:
    """Return the storage key for one provider/Base URL credential scope."""
    normalized_provider = _normalize_api_key_provider(provider)
    if not normalized_provider:
        return ""
    normalized_base_url = _normalize_api_key_base_url(
        normalized_provider,
        base_url,
    )
    if not normalized_base_url:
        return normalized_provider
    return f"{normalized_provider}{API_KEY_SCOPE_SEPARATOR}{normalized_base_url}"


CONNECTION_KEY_SCOPE_PREFIX = "conn"


def connection_key_scope(connection_id: str) -> str:
    """Return the storage key for one connection's credential.

    Connections inside one pool routinely point at the same provider and Base
    URL with different accounts, so the provider/Base URL scope cannot tell
    them apart.  Keys for connections are therefore stored under their own
    stable id and only fall back to the provider scope for data written before
    pools existed.
    """
    normalized = str(connection_id or "").strip()
    if not normalized:
        return ""
    return f"{CONNECTION_KEY_SCOPE_PREFIX}{API_KEY_SCOPE_SEPARATOR}{normalized}"


def is_connection_key_scope(scope: str) -> bool:
    """Return whether a stored scope belongs to one pool connection."""
    raw_scope = str(scope or "").strip()
    return raw_scope.startswith(
        f"{CONNECTION_KEY_SCOPE_PREFIX}{API_KEY_SCOPE_SEPARATOR}"
    )


def connection_id_from_key_scope(scope: str) -> str:
    """Return the connection id carried by a connection-scoped key."""
    if not is_connection_key_scope(scope):
        return ""
    return str(scope).strip().split(API_KEY_SCOPE_SEPARATOR, 1)[1]


def parse_api_key_scope(scope: str) -> tuple[str, str]:
    """Split a persisted credential scope into provider and Base URL.

    Connection-scoped keys carry an opaque id instead of a provider, so they
    resolve to an empty pair; callers that need them must ask for the
    connection id explicitly rather than inventing a "conn" provider.
    """
    raw_scope = str(scope or "").strip()
    if not raw_scope:
        return "", ""
    if is_connection_key_scope(raw_scope):
        return "", ""
    if API_KEY_SCOPE_SEPARATOR not in raw_scope:
        return raw_scope, ""
    provider, base_url = raw_scope.split(API_KEY_SCOPE_SEPARATOR, 1)
    provider = _normalize_api_key_provider(provider)
    return provider, _normalize_api_key_base_url(provider, base_url)


def _legacy_provider_aliases(provider: str) -> tuple[str, ...]:
    normalized_provider = _normalize_api_key_provider(provider)
    if normalized_provider == "custom_openai":
        return ("lanyi",)
    if normalized_provider == "lanyi":
        return ("custom_openai",)
    return ()


def _api_key_lookup_scopes(provider: str, base_url: str = "") -> list[str]:
    normalized_provider = _normalize_api_key_provider(provider)
    if not normalized_provider:
        return []
    normalized_base_url = _normalize_api_key_base_url(
        normalized_provider,
        base_url,
    )
    lookup_providers = (
        normalized_provider,
        *_legacy_provider_aliases(normalized_provider),
    )
    scopes: list[str] = []
    if normalized_base_url:
        for lookup_provider in lookup_providers:
            scopes.append(api_key_scope(lookup_provider, normalized_base_url))
    for lookup_provider in lookup_providers:
        scopes.append(api_key_scope(lookup_provider))
    return list(dict.fromkeys(scope for scope in scopes if scope))


def _clamp_int(value, *, minimum: int, maximum: int, fallback: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(minimum, min(maximum, number))


def _normalize_hex_color(value: str, *, fallback: str) -> str:
    cleaned = str(value or "").strip().lstrip("#").upper()
    if len(cleaned) == 6 and all(char in "0123456789ABCDEF" for char in cleaned):
        return cleaned
    return fallback


def _default_review_mark_colors(legacy_color: str | None = None) -> dict[str, str]:
    if legacy_color:
        return {mark: legacy_color for mark in REVIEW_MARK_COLOR_DEFAULTS}
    return dict(REVIEW_MARK_COLOR_DEFAULTS)


def _review_mark_colors_from_payload(payload: dict) -> dict[str, str]:
    raw_colors = payload.get("mark_colors")
    if isinstance(raw_colors, dict) and raw_colors:
        colors = dict(raw_colors)
    else:
        legacy_color = _normalize_hex_color(
            payload.get("highlight_color", ""),
            fallback=WORD_REVIEW_HIGHLIGHT_COLOR_DEFAULT,
        )
        colors = _default_review_mark_colors(
            legacy_color
            if legacy_color != WORD_REVIEW_HIGHLIGHT_COLOR_DEFAULT
            else None
        )

    normalized: dict[str, str] = {}
    defaults = _default_review_mark_colors()
    for mark, default_color in defaults.items():
        normalized[mark] = _normalize_hex_color(
            colors.get(mark, ""),
            fallback=default_color,
        )
    return normalized


def _normalize_local_provider(value: str) -> str:
    provider = str(value or DEFAULT_LOCAL_MODEL_PROVIDER).strip()
    return provider if provider in set(LOCAL_MODEL_PROVIDERS.values()) else DEFAULT_LOCAL_MODEL_PROVIDER


def _default_local_base_url(provider: str) -> str:
    normalized = _normalize_local_provider(provider)
    if normalized == "lm_studio":
        return LM_STUDIO_BASE_URL
    if normalized == "ollama":
        return OLLAMA_BASE_URL
    return ""


THINKING_MODES = ("auto", "off", "on")


def normalize_thinking_mode(value) -> str:
    """深度思考设置只认 auto / off / on，其余（含旧文件里没有这一项）一律按 auto。"""
    value = str(value or "").strip()
    return value if value in THINKING_MODES else "auto"


def new_connection_id() -> str:
    """Return a stable id for one pool connection."""
    return uuid.uuid4().hex


class TextProtocolSettings(BaseModel):
    api_mode: str = "auto"

    @model_validator(mode="before")
    @classmethod
    def _migrate_text_protocol(cls, data):
        if not isinstance(data, dict):
            return data
        data = dict(data)
        if "api_mode" not in data:
            # The only historical domain rule lives in additive migration, not
            # request routing. Existing asxs connections keep their working route.
            url = str(data.get("cloud_base_url") or data.get("base_url") or "")
            data["api_mode"] = "responses" if urlsplit(url).hostname == "api.asxs.top" else "auto"
        data["api_mode"] = normalize_api_mode(data["api_mode"])
        return data


class ModelConnection(TextProtocolSettings):
    """One independently selectable connection inside a model role's pool.

    A pool is an ordered list: entry 0 is the primary and is kept mirrored
    onto the role's legacy single-connection fields, so configuration written
    by this version stays readable by the previous one.
    """

    id: str = Field(default_factory=new_connection_id)
    label: str = ""
    provider: str = DEFAULT_CLOUD_PROVIDER
    model: str = ""
    cloud_provider: str = ""
    cloud_model: str = ""
    cloud_base_url: str = ""
    cloud_api_mode: str = "auto"
    # 深度思考（目前只有阿里百炼的千问用得上）：auto 不传参数、由模型自己决定，
    # off / on 发 enable_thinking=false / true。跟随连接用来源连接的设置。
    thinking: str = "auto"
    local_provider: str = ""
    # A legacy text follower may have a distinct local model name.  Keep it
    # when its source switches between cloud and local primaries.
    local_model: str = ""
    local_base_url: str = ""
    follow_model: str = ""
    base_url: str = ""
    # Empty only while loading an older pool entry.  _sync_connection_pool
    # infers its former role-wide mode once and persists the explicit value.
    connection_mode: str = ""
    source_role: str = "independent"
    availability_status: str = "unknown"
    availability_message: str = ""
    availability_checked_at: str = ""
    availability_signature: str = ""

    @model_validator(mode="after")
    def _normalize_connection(self):
        self.id = str(self.id or "").strip() or new_connection_id()
        self.label = str(self.label or "").strip()
        self.provider = str(self.provider or DEFAULT_CLOUD_PROVIDER).strip()
        self.model = str(self.model or "").strip()
        self.cloud_provider = str(self.cloud_provider or "").strip()
        self.cloud_model = str(self.cloud_model or "").strip()
        self.cloud_base_url = str(self.cloud_base_url or "").strip().rstrip("/")
        self.cloud_api_mode = normalize_api_mode(self.cloud_api_mode)
        self.thinking = normalize_thinking_mode(self.thinking)
        self.local_provider = str(self.local_provider or "").strip()
        self.local_model = str(self.local_model or "").strip()
        self.local_base_url = str(self.local_base_url or "").strip().rstrip("/")
        self.follow_model = str(self.follow_model or "").strip()
        self.base_url = str(self.base_url or "").strip().rstrip("/")
        if self.connection_mode not in {"", "cloud", "local", "follow"}:
            self.connection_mode = "cloud"
        if self.source_role not in MODEL_ROLE_SOURCE_VALUES:
            self.source_role = "independent"
        if self.connection_mode != "follow":
            self.source_role = "independent"
        if self.availability_status not in {"unknown", "available", "unavailable"}:
            self.availability_status = "unknown"
        return self

    @property
    def display_label(self) -> str:
        if self.label:
            # 跟随连接没起名时，界面会把「跟随 + 用途名」存成连接名；用途改名后旧名照新说法显示。
            return _renamed_follow_label(self.label)
        # 没起名的连接以前直接显示 Base URL 或内部标识（custom_openai），设置页
        # 和任务中心里读起来像代码。改成服务商名，有地址时补上主机名好区分多条连接。
        name = _PROVIDER_DISPLAY_NAMES.get(self.provider, self.provider)
        host = urlsplit(self.base_url).hostname if self.base_url else ""
        return f"{name} · {host}" if host else name


class ModelUpgradeState(BaseModel):
    """Non-secret record of one connection's automatic model upgrade."""

    source_model: str = ""
    candidate_model: str = ""
    connection_signature: str = ""
    consecutive_failures: int = Field(default=0, ge=0, le=3)
    status: str = ""


_SEEDED_CONNECTION_PREFIX = "seed-"

# V10.3.2 起「文档翻译」改称「文本翻译」，「PDF 与图片翻译」改称「图像翻译」，「PDF 与图片审核」改称「图像翻译审核」。
# 按前缀换，旧版本存下的写法（如「跟随文档翻译（Excel / Word）」）也照新说法显示。
_RENAMED_FOLLOW_PREFIXES = (
    ("跟随文档翻译", "跟随文本翻译"),
    ("跟随PDF 与图片翻译", "跟随图像翻译"),
    ("跟随 PDF 与图片翻译", "跟随图像翻译"),
    ("跟随PDF 与图片审核", "跟随图像翻译审核"),
    ("跟随 PDF 与图片审核", "跟随图像翻译审核"),
)


def _renamed_follow_label(label: str) -> str:
    for old, new in _RENAMED_FOLLOW_PREFIXES:
        if label.startswith(old):
            return new + label[len(old):]
    return label


_PROVIDER_DISPLAY_NAMES = {
    **{value: label for label, value in {**CLOUD_ENGINES, **LOCAL_MODEL_PROVIDERS}.items()},
    "custom_local": "自定义本地模型",
}


def _sync_connection_pool(owner) -> None:
    """Keep a pool and its owner's legacy single-connection fields in step.

    Entry 0 *is* the legacy connection.  Mirroring it both ways means settings
    written by this version stay loadable by the version before pools existed,
    so rolling the app back does not strand a configured connection.
    """
    connections = [conn for conn in (owner.connections or []) if conn is not None]
    if not connections:
        # Marked so AppSettings can give it an id derived from its role.  A
        # random id here would differ on every load until something saved,
        # and any key stored against it would be orphaned on the next read.
        connections = [
            ModelConnection(
                id=f"{_SEEDED_CONNECTION_PREFIX}{uuid.uuid4().hex}",
                provider=owner.local_provider if owner.mode == "local" and owner.source_role == "independent" else owner.cloud_provider,
                model=owner.local_model if owner.mode == "local" and owner.source_role == "independent" else owner.cloud_model,
                local_model=owner.local_model,
                cloud_provider=owner.cloud_provider,
                cloud_model=owner.cloud_model,
                cloud_base_url=owner.cloud_base_url,
                cloud_api_mode=owner.api_mode,
                local_provider=owner.local_provider,
                local_base_url=owner.local_base_url,
                follow_model=owner.cloud_model if owner.source_role != "independent" else "",
                base_url=owner.local_base_url if owner.mode == "local" and owner.source_role == "independent" else owner.cloud_base_url,
                api_mode=owner.api_mode,
                connection_mode=(
                    "follow" if owner.source_role != "independent" else owner.mode
                ),
                source_role=owner.source_role,
                availability_status=owner.availability_status,
                availability_message=owner.availability_message,
                availability_checked_at=owner.availability_checked_at,
                availability_signature=owner.availability_signature,
            )
        ]
    else:
        primary = connections[0]
        if not primary.connection_mode:
            primary.connection_mode = (
                "follow" if owner.source_role != "independent" else owner.mode
            )
            primary.source_role = owner.source_role
            primary.local_model = owner.local_model
            primary.cloud_provider = owner.cloud_provider
            primary.cloud_model = owner.cloud_model
            primary.cloud_base_url = owner.cloud_base_url
            primary.cloud_api_mode = owner.api_mode
            primary.local_provider = owner.local_provider
            primary.local_base_url = owner.local_base_url
            if primary.connection_mode == "follow":
                primary.follow_model = owner.cloud_model
            if primary.connection_mode == "local":
                primary.provider = owner.local_provider
                primary.model = owner.local_model
                primary.base_url = owner.local_base_url
        elif owner.source_role != (
            primary.source_role if primary.connection_mode == "follow" else "independent"
        ):
            # The role-wide field is entry 0's compatibility alias.  A file
            # whose two representations disagree may have been edited by an
            # older build, which only knew the role-wide value.
            primary.connection_mode = (
                "follow" if owner.source_role != "independent" else owner.mode
            )
            primary.source_role = owner.source_role
        if primary.connection_mode in {"cloud", "follow"}:
            primary.model = owner.cloud_model
            if primary.connection_mode == "follow":
                primary.follow_model = owner.cloud_model
        if primary.connection_mode == "cloud":
            primary.provider = owner.cloud_provider
            primary.base_url = owner.cloud_base_url
            primary.api_mode = owner.api_mode
        primary.availability_status = owner.availability_status
        primary.availability_message = owner.availability_message
        primary.availability_checked_at = owner.availability_checked_at
        primary.availability_signature = owner.availability_signature

    seen: set[str] = set()
    for index, conn in enumerate(connections):
        if not conn.connection_mode:
            conn.connection_mode = "cloud"
        if conn.connection_mode == "local":
            conn.local_provider = conn.local_provider or conn.provider
            conn.local_model = conn.local_model or conn.model
            conn.local_base_url = conn.local_base_url or conn.base_url
            if index == 0:
                conn.cloud_provider = conn.cloud_provider or owner.cloud_provider
                conn.cloud_model = conn.cloud_model or owner.cloud_model
                conn.cloud_base_url = conn.cloud_base_url or owner.cloud_base_url
                conn.cloud_api_mode = conn.cloud_api_mode or owner.api_mode
        else:
            conn.cloud_provider = conn.cloud_provider or conn.provider
            conn.cloud_model = conn.cloud_model or conn.model
            conn.cloud_base_url = conn.cloud_base_url or conn.base_url
            conn.cloud_api_mode = conn.cloud_api_mode or conn.api_mode
            if conn.connection_mode == "follow":
                conn.follow_model = conn.follow_model or conn.model
            if index == 0:
                conn.local_provider = conn.local_provider or owner.local_provider
                conn.local_model = conn.local_model or owner.local_model
                conn.local_base_url = conn.local_base_url or owner.local_base_url
        if index == 0 and conn.connection_mode == "follow":
            # Keep the role-wide compatibility field aligned with the primary.
            owner.source_role = conn.source_role
        elif index == 0:
            owner.source_role = "independent"
            owner.mode = conn.connection_mode
        if conn.id in seen:
            conn.id = new_connection_id()
        seen.add(conn.id)
    owner.connections = connections


class CloudProviderConfig(TextProtocolSettings):
    """Provider-specific model and Base URL values for one model role."""

    cloud_model: str = ""
    cloud_base_url: str = ""

    @model_validator(mode="after")
    def _normalize_values(self):
        self.cloud_model = str(self.cloud_model or "").strip()
        self.cloud_base_url = str(self.cloud_base_url or "").strip().rstrip("/")
        return self


def _normalize_provider_configs(
    configs: dict[str, CloudProviderConfig] | dict[str, dict] | None,
) -> dict[str, CloudProviderConfig]:
    if not isinstance(configs, dict):
        return {}
    normalized: dict[str, CloudProviderConfig] = {}
    for raw_provider, raw_config in configs.items():
        provider = str(raw_provider or "").strip()
        if not provider:
            continue
        try:
            config = (
                raw_config
                if isinstance(raw_config, CloudProviderConfig)
                else CloudProviderConfig.model_validate(raw_config or {})
            )
        except Exception:
            config = CloudProviderConfig()
        normalized[provider] = CloudProviderConfig(
            api_mode=config.api_mode,
            cloud_model=config.cloud_model,
            cloud_base_url=configured_cloud_base_url(provider, config.cloud_base_url),
        )
    return normalized


def get_cloud_provider_config(owner, provider: str) -> CloudProviderConfig:
    """Resolve one provider's remembered model/Base URL for an engine or role."""
    provider_name = str(provider or DEFAULT_CLOUD_PROVIDER).strip()
    configs = _normalize_provider_configs(getattr(owner, "cloud_provider_configs", {}))
    config = configs.get(provider_name)
    if config is not None:
        return config

    current_provider = str(getattr(owner, "cloud_provider", "") or "").strip()
    if provider_name == current_provider:
        return CloudProviderConfig(
            api_mode=owner.api_mode,
            cloud_model=str(getattr(owner, "cloud_model", "") or "").strip(),
            cloud_base_url=configured_cloud_base_url(
                provider_name,
                str(getattr(owner, "cloud_base_url", "") or "").strip(),
            ),
        )
    return CloudProviderConfig(
        cloud_model="",
        cloud_base_url=configured_cloud_base_url(provider_name, ""),
    )


def set_cloud_provider_config(
    owner,
    provider: str,
    *,
    cloud_model: str | None = None,
    cloud_base_url: str | None = None,
    api_mode: str | None = None,
) -> CloudProviderConfig:
    """Store provider-specific values and keep legacy current fields in sync."""
    provider_name = str(provider or DEFAULT_CLOUD_PROVIDER).strip()
    current = get_cloud_provider_config(owner, provider_name)
    model = (
        current.cloud_model
        if cloud_model is None
        else str(cloud_model or "").strip()
    )
    base_url_raw = (
        current.cloud_base_url
        if cloud_base_url is None
        else str(cloud_base_url or "").strip()
    )
    config = CloudProviderConfig(
        api_mode=normalize_api_mode(api_mode) if api_mode is not None else current.api_mode,
        cloud_model=model,
        cloud_base_url=configured_cloud_base_url(provider_name, base_url_raw),
    )
    owner.cloud_provider_configs = _normalize_provider_configs(
        getattr(owner, "cloud_provider_configs", {}),
    )
    owner.cloud_provider_configs[provider_name] = config
    if provider_name == str(getattr(owner, "cloud_provider", "") or "").strip():
        owner.api_mode = config.api_mode
        owner.cloud_model = config.cloud_model
        owner.cloud_base_url = config.cloud_base_url
    return config


def select_cloud_provider_config(owner, provider: str) -> CloudProviderConfig:
    """Switch an engine/role to a provider and load that provider's remembered values."""
    provider_name = str(provider or DEFAULT_CLOUD_PROVIDER).strip()
    owner.cloud_provider_configs = _normalize_provider_configs(
        getattr(owner, "cloud_provider_configs", {}),
    )
    config = owner.cloud_provider_configs.get(provider_name)
    if config is None:
        config = CloudProviderConfig(
            cloud_model="",
            cloud_base_url=configured_cloud_base_url(provider_name, ""),
        )
    owner.cloud_provider = provider_name
    owner.api_mode = config.api_mode
    owner.cloud_model = config.cloud_model
    owner.cloud_base_url = config.cloud_base_url
    return config


MODEL_ROLE_SOURCE_VALUES = {
    "independent",
    "translation",
    "cleaner",
    "image",
    "pdf_review",
}


class EngineSettings(TextProtocolSettings):
    mode: str = "cloud"  # "cloud" | "local"
    # Translation used to be the only follow *source*.  It can now follow a
    # role that is itself independent, so it needs the same field as the rest;
    # "independent" keeps every existing settings file behaving as before.
    source_role: str = "independent"
    cloud_provider: str = DEFAULT_CLOUD_PROVIDER
    cloud_model: str = DEFAULT_CLOUD_MODEL
    cloud_base_url: str = DEFAULT_CUSTOM_OPENAI_BASE_URL
    cloud_provider_configs: dict[str, CloudProviderConfig] = Field(default_factory=dict)
    local_provider: str = DEFAULT_LOCAL_MODEL_PROVIDER
    local_model: str = ""
    local_base_url: str = OLLAMA_BASE_URL
    ollama_model: str = ""
    concurrency: int = Field(
        default=CONCURRENCY_DEFAULT,
        ge=1,
        le=2**31 - 1,
    )
    ollama_concurrency: int = Field(
        default=get_default_concurrency("local"),
        ge=1,
        le=2**31 - 1,
    )
    concurrency_unlocked: bool = False
    concurrency_unlock_code: str = ""
    batch_size: int = Field(default=20, ge=1, le=2**31 - 1)
    availability_status: str = "unknown"
    availability_message: str = ""
    availability_checked_at: str = ""
    availability_signature: str = ""
    connections: list[ModelConnection] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_concurrency(cls, data):
        """Compat: fall back to legacy ollama_concurrency when needed."""
        if not isinstance(data, dict):
            return data

        migrated = dict(data)
        if "concurrency" not in migrated:
            legacy = migrated.get("ollama_concurrency")
            if legacy is not None:
                migrated["concurrency"] = legacy

        if "concurrency" in migrated:
            migrated["concurrency"] = _clamp_int(
                migrated.get("concurrency"),
                minimum=1,
                maximum=get_concurrency_cap(),
                fallback=CONCURRENCY_DEFAULT,
            )

        if "ollama_concurrency" in migrated:
            migrated["ollama_concurrency"] = _clamp_int(
                migrated.get("ollama_concurrency"),
                minimum=1,
                maximum=get_concurrency_cap(),
                fallback=get_default_concurrency("local"),
            )

        migrated.setdefault("local_provider", DEFAULT_LOCAL_MODEL_PROVIDER)
        if "local_model" not in migrated:
            migrated["local_model"] = str(migrated.get("ollama_model") or "").strip()
        if "local_base_url" not in migrated:
            migrated["local_base_url"] = _default_local_base_url(
                migrated.get("local_provider"),
            )
        migrated.setdefault("concurrency_unlocked", False)
        if is_valid_concurrency_unlock_code(str(migrated.get("concurrency_unlock_code") or "").strip()):
            migrated["concurrency_unlocked"] = True
        return migrated

    @model_validator(mode="after")
    def _normalize_concurrency_ranges(self):
        cloud_min, cloud_max = get_cloud_concurrency_bounds(self.concurrency_unlocked)
        self.concurrency = max(cloud_min, min(cloud_max, self.concurrency))

        local_min, local_max = get_local_concurrency_bounds(self.concurrency_unlocked)
        self.ollama_concurrency = max(local_min, min(local_max, self.ollama_concurrency))
        self.local_provider = _normalize_local_provider(self.local_provider)
        # A role can never follow itself; core enforces the rest of the graph.
        if self.source_role not in MODEL_ROLE_SOURCE_VALUES - {"translation"}:
            self.source_role = "independent"
        self.cloud_provider = str(self.cloud_provider or DEFAULT_CLOUD_PROVIDER).strip()
        self.cloud_model = str(self.cloud_model or "").strip()
        self.cloud_base_url = configured_cloud_base_url(
            self.cloud_provider,
            self.cloud_base_url,
        )
        self.cloud_provider_configs = _normalize_provider_configs(self.cloud_provider_configs)
        if self.cloud_model or self.cloud_base_url:
            existing = self.cloud_provider_configs.get(self.cloud_provider)
            if existing is None or not (existing.cloud_model or existing.cloud_base_url):
                self.cloud_provider_configs[self.cloud_provider] = CloudProviderConfig(
                    api_mode=self.api_mode,
                    cloud_model=self.cloud_model,
                    cloud_base_url=self.cloud_base_url,
                )
            else:
                self.api_mode = existing.api_mode
                self.cloud_model = existing.cloud_model
                self.cloud_base_url = existing.cloud_base_url
        if not str(self.local_base_url or "").strip():
            self.local_base_url = _default_local_base_url(self.local_provider)
        self.local_model = str(self.local_model or "").strip()
        self.ollama_model = self.local_model if self.local_provider == "ollama" else str(
            self.ollama_model or ""
        ).strip()
        if self.availability_status not in {"unknown", "available", "unavailable"}:
            self.availability_status = "unknown"
        self.availability_message = str(self.availability_message or "").strip()
        self.availability_checked_at = str(self.availability_checked_at or "").strip()
        self.availability_signature = str(self.availability_signature or "").strip()
        _sync_connection_pool(self)
        return self


class TMSettings(BaseModel):
    """记忆库。v37 起加了词条采用方面的几项（加法改动：旧文件按默认补齐）。"""

    max_len: int = Field(default=DEFAULT_MAX_LEN, ge=1, le=200)
    # 高级模式：记忆库页、编辑窗口和任务详情显示采用方式、生效范围、使用次数等。
    advanced_mode: bool = False
    # 「始终采用」的词条在句中出现时同样采用；关闭时只在整段相同时采用。
    apply_in_sentences: bool = True
    # 译文未采用设定译法时自动重译的最多次数；0 表示不重译、直接标记需复核。
    term_retry_attempts: int = Field(default=2, ge=0, le=5)
    # 「自动识别场景」下，短词在新场景里的译法自动保存为带备注的新词条。
    auto_save_short_scene: bool = True
    # 单批涉及的词条超过此数时把批拆小，词条一条不丢。
    term_batch_budget: int = Field(default=20, ge=5, le=100)
    # 本地模型也收到「按场景选用」的候选译法（本地模型较易受干扰，默认关闭）。
    local_scene_candidates: bool = False


class OutputSettings(BaseModel):
    keep_original_sheets: bool = True
    formula_display_value_backfill: bool = True
    use_custom_output_dir: bool = False
    custom_output_dir: str = ""
    # 任务文件夹（当前默认）或直接把译文放回源文件夹。旧设置没有此字段时按任务文件夹补齐。
    output_mode: Literal["task_folder", "source_folder"] = "task_folder"
    # 首选位置无法创建或写入时使用；再失败由运行时退回桌面。
    fallback_output_dir: str = ""
    enable_excel_autofit: bool = False
    lock_row_height: bool = True


class ExcelOutputSettings(BaseModel):
    """Settings owned solely by the Excel translation surface.

    The previous ``output`` object remains for non-Excel callers while new
    Excel tasks always freeze this object.  This avoids a Word/PDF edit
    changing a future Excel task (E4B-01/E4B-11).
    """

    keep_original_sheets: bool = True
    translate_output_filename: bool = False
    translate_sheet_names: bool = True
    output_translation_only: bool = False
    formula_display_value_backfill: bool = True
    use_custom_output_dir: bool = False
    custom_output_dir: str = ""
    output_mode: Literal["task_folder", "source_folder"] = "task_folder"
    fallback_output_dir: str = ""
    enable_excel_autofit: bool = False
    lock_row_height: bool = False
    # 批注（V10.1 起）。旧版 settings.json 没有这个键时按默认值补齐（加法改动，无需迁移）：
    # 批注默认不翻。文本框和形状里的字始终翻译，不再是开关；旧设置里留下的
    # translate_shapes（包括关掉的）读取时忽略。
    translate_comments: bool = False


class WordOutputSettings(BaseModel):
    """Output settings owned solely by the Word translation surface.

    Word always writes a new bilingual ``.docx`` into a task-unique output
    directory.  Keeping its output-root choice separate from the legacy
    shared output object and the Excel page prevents a future edit on either
    page from changing an already-configured Word task.
    """

    use_custom_output_dir: bool = False
    translate_output_filename: bool = False
    custom_output_dir: str = ""
    output_mode: Literal["task_folder", "source_folder"] = "task_folder"
    fallback_output_dir: str = ""
    output_translation_only: bool = False


class PdfOutputSettings(BaseModel):
    """Output and evidence retention owned only by PDF/image translation.

    PDF/image tasks generate a package of page evidence in addition to final
    files.  They must not inherit Excel/Word's output root or mutate it.
    """

    use_custom_output_dir: bool = False
    translate_output_filename: bool = False
    custom_output_dir: str = ""
    output_mode: Literal["task_folder", "source_folder"] = "task_folder"
    fallback_output_dir: str = ""


class CadOutputSettings(BaseModel):
    """Output choices owned by the CAD translation surface."""

    use_custom_output_dir: bool = False
    custom_output_dir: str = ""
    output_mode: Literal["task_folder", "source_folder"] = "task_folder"
    fallback_output_dir: str = ""
    translate_output_filename: bool = False
    # 以下三项是 CAD 页右栏「本类型选项」的开关，和 Excel/Word/PDF 一样改了就存。
    # 旧版 settings.json 没有这几个键时按默认值补齐（加法改动，无需迁移）。
    include_block_text: bool = True
    keep_work_dxf: bool = False
    copy_related_files: bool = False
    # v34 起：译文排版、显示模式、锁定不翻译的图块/图层、译文字体。旧文件没有这些键时
    # 按默认值补齐（加法改动，无需迁移）。
    # 译文会压到旁边的文字时缩小字号；先缩到默认比例，再按 0.1 一档缩到最小比例。
    # 比例按目标语言分两套：下面这对用于中文以外的目标语言，``*_zh`` 那对用于目标语言为中文
    # （默认值见 core.cad_layout.default_fit_settings）。开关两套共用。
    fit_layout: bool = True
    fit_default_scale: float = 0.7
    fit_min_scale: float = 0.4
    # 目标语言为中文时的那对比例。旧文件没有这两个键时按默认值补齐；旧版只有一对比例，
    # 用户改过的（不是 0.7 / 0.4）照搬过来，免得换成中文后自己的设置失效。加法改动，不升版本号。
    fit_default_scale_zh: float = 1.0
    fit_min_scale_zh: float = 0.4
    # translation 纯译文；source_first 原文在上、译文在下；translation_first 译文在上。
    display_mode: str = "translation"
    # 用户手写的锁定名单，逗号或换行分隔，可用 * 通配；原样保存，下次打开照原样显示。
    locked_blocks: str = ""
    locked_layers: str = ""
    # compatible：译文改用 AutoCAD 自带字体写回；original：沿用原文字样式。
    translation_font: str = "compatible"
    # DWG 译文图纸的版本：source 跟随原图，或 2018/2013/2010/2007/2004/2000。旧文件没有这个键时
    # 按默认值补齐；不升版本号，老版本应用读到这个键也只是忽略。
    output_version: str = "source"

    @model_validator(mode="before")
    @classmethod
    def _seed_chinese_fit_scales(cls, data):
        if not isinstance(data, dict) or "fit_default_scale_zh" in data or "fit_min_scale_zh" in data:
            return data
        from core.cad_layout import DEFAULT_FIT_SETTINGS, FitSettings

        legacy = FitSettings(
            data.get("fit_default_scale", DEFAULT_FIT_SETTINGS.default_scale),
            data.get("fit_min_scale", DEFAULT_FIT_SETTINGS.min_scale),
        ).normalized()
        if legacy == DEFAULT_FIT_SETTINGS:
            return data
        return {**data, "fit_default_scale_zh": legacy.default_scale, "fit_min_scale_zh": legacy.min_scale}

    @model_validator(mode="after")
    def _normalize_cad_layout(self):
        from core.cad_layout import FitSettings

        fit = FitSettings(self.fit_default_scale, self.fit_min_scale).normalized()
        self.fit_default_scale = fit.default_scale
        self.fit_min_scale = fit.min_scale
        fit_zh = FitSettings(self.fit_default_scale_zh, self.fit_min_scale_zh).normalized()
        self.fit_default_scale_zh = fit_zh.default_scale
        self.fit_min_scale_zh = fit_zh.min_scale
        if self.display_mode not in {"translation", "source_first", "translation_first"}:
            self.display_mode = "translation"
        if self.translation_font not in {"compatible", "original"}:
            self.translation_font = "compatible"
        if self.output_version not in {"source", "2018", "2013", "2010", "2007", "2004", "2000"}:
            self.output_version = "source"
        self.locked_blocks = str(self.locked_blocks or "")[:4000]
        self.locked_layers = str(self.locked_layers or "")[:4000]
        return self


class ExcelReviewSettings(BaseModel):
    mark_review_items: bool = EXCEL_REVIEW_MARK_DEFAULT
    existing_fill_policy: str = EXCEL_REVIEW_EXISTING_FILL_POLICY_DEFAULT
    mark_colors: dict[str, str] = Field(default_factory=_default_review_mark_colors)

    @model_validator(mode="after")
    def _normalize_existing_fill_policy(self):
        allowed = {"skip", "overwrite", "red_font"}
        policy = str(self.existing_fill_policy or "").strip()
        if policy not in allowed:
            policy = EXCEL_REVIEW_EXISTING_FILL_POLICY_DEFAULT
        self.existing_fill_policy = policy
        self.mark_colors = _review_mark_colors_from_payload(
            {"mark_colors": self.mark_colors}
        )
        return self


class WordBatchSettings(BaseModel):
    max_paragraphs_per_batch: int = Field(
        default=WORD_BATCH_PARAGRAPHS_DEFAULT,
        ge=1,
        le=2**31 - 1,
    )
    max_chars_per_batch: int = Field(
        default=WORD_BATCH_CHARS_DEFAULT,
        ge=1,
        le=2**31 - 1,
    )
    split_paragraph_chars: int = Field(
        default=WORD_BATCH_SPLIT_CHARS_DEFAULT,
        ge=1,
        le=2**31 - 1,
    )
    strict_retry_attempts: int = Field(
        default=WORD_STRICT_RETRY_ATTEMPTS_DEFAULT,
        ge=WORD_STRICT_RETRY_ATTEMPTS_MIN,
        le=WORD_STRICT_RETRY_ATTEMPTS_MAX,
    )

    @model_validator(mode="after")
    def _normalize_thresholds(self):
        self.max_paragraphs_per_batch = min(8, self.max_paragraphs_per_batch)
        # Kept in the saved schema for older settings files; the batch budget
        # is now the sole user-facing limit and also governs long paragraphs.
        self.split_paragraph_chars = self.max_chars_per_batch
        return self


# 速率与重试每一项的存储上下限。这里只挡住明显不合法的值（0 路并发、负数），
# 按云端 / 本地模式收紧的可填范围在 core/surface_rate.py，运行时按那里收敛：
# 换成本地模型后，云端那一档存下的 30 路不该让整份设置读不进来。
_RATE_FIELD_LIMITS: dict[str, tuple[int, int]] = {
    "concurrency": (1, 2**31 - 1),
    "batch_items": (1, 2**31 - 1),
    "batch_chars": (1, 2**31 - 1),
    "request_retry": (REQUEST_RETRY_ATTEMPTS_MIN, REQUEST_RETRY_ATTEMPTS_MAX),
    "item_retry": (RATE_ITEM_RETRY_MIN, RATE_ITEM_RETRY_MAX),
    "formula_batch": (RATE_FORMULA_BATCH_MIN, RATE_FORMULA_BATCH_MAX),
}


class SurfaceRateSettings(BaseModel):
    """一种文件类型的速率与重试（v38 起，「翻译参数」各页签里的同一组设置）。

    None 表示没改过：按文本翻译模型是云端还是本地取默认值（core/surface_rate.py），
    换成本地模型时跟着换成本地那一档。填了数就固定用这个数。
    """

    concurrency: int | None = None
    batch_items: int | None = None
    batch_chars: int | None = None
    request_retry: int | None = None
    item_retry: int | None = None
    # 只有 Word 用：补公式位置时每组最多几个公式（反馈 #19）
    formula_batch: int | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_rate_values(cls, data):
        if not isinstance(data, dict):
            return data
        migrated = dict(data)
        for key, (minimum, maximum) in _RATE_FIELD_LIMITS.items():
            raw = migrated.get(key)
            if raw in ("", None) or isinstance(raw, bool):
                migrated[key] = None
                continue
            try:
                value = int(raw)
            except (TypeError, ValueError):
                migrated[key] = None
                continue
            migrated[key] = max(minimum, min(maximum, value))
        return migrated


class RateSettings(BaseModel):
    """各文件类型的速率与重试（v38 起）。

    v37 及以前，同时请求数和每批条数按模型存在 model_throughput_profiles 里，Word 的
    批次在 word_batch，Excel / CAD 的失败重试在 request_retry。升级时
    _migrate_rate_settings 移除旧的两节，这里留空，一律按新默认值走（改过的值只留在
    升级前的备份里）。PDF 整页图像翻译的三项（同时生成页数、单页重试次数、同时审核
    页数）仍在 pdf 节。
    """

    excel: SurfaceRateSettings = Field(default_factory=SurfaceRateSettings)
    word: SurfaceRateSettings = Field(default_factory=SurfaceRateSettings)
    # PDF 的文字翻译：转 Word 翻译和原位替换（含文字识别）共用这一组。
    pdf_text: SurfaceRateSettings = Field(default_factory=SurfaceRateSettings)
    cad: SurfaceRateSettings = Field(default_factory=SurfaceRateSettings)


class ProcessRetentionRule(BaseModel):
    """一类任务过程文件的保留上限：最多几次、最多多大，任一先到就清理最早的。

    None 表示这一项不设上限（界面上留空）；两项都为 None 时这一类永不自动清理。
    """

    max_runs: int | None = Field(default=PROCESS_RETENTION_FAILED_RUNS_DEFAULT, ge=PROCESS_RETENTION_RUNS_MIN, le=PROCESS_RETENTION_RUNS_MAX)
    max_mb: int | None = Field(default=PROCESS_RETENTION_MB_DEFAULT, ge=PROCESS_RETENTION_MB_MIN, le=PROCESS_RETENTION_MB_MAX)


def _succeeded_retention_rule() -> ProcessRetentionRule:
    return ProcessRetentionRule(max_runs=PROCESS_RETENTION_SUCCEEDED_RUNS_DEFAULT)


class ProcessRetentionSettings(BaseModel):
    """任务过程文件的自动保留上限（v36 起），见 core/process_artifacts.py。

    succeeded 对应正常完成的任务，failed 对应需复核、出错的任务（过程目录里的
    ``normal`` / ``errors`` 两类）。旧设置文件没有这一节，读取时按默认值补齐（加法改动，
    无需迁移）。
    """

    succeeded: ProcessRetentionRule = Field(default_factory=_succeeded_retention_rule)
    failed: ProcessRetentionRule = Field(default_factory=ProcessRetentionRule)


class WordReviewSettings(BaseModel):
    highlight_unresolved: bool = WORD_REVIEW_HIGHLIGHT_DEFAULT
    highlight_color: str = WORD_REVIEW_HIGHLIGHT_COLOR_DEFAULT
    mark_colors: dict[str, str] = Field(default_factory=_default_review_mark_colors)
    # Word must preserve a user's existing highlight by default while still
    # making a machine review item visible.  Excel owns a different policy.
    existing_highlight_policy: str = "red_underline"

    @model_validator(mode="before")
    @classmethod
    def _seed_mark_colors(cls, data):
        if not isinstance(data, dict):
            return data
        payload = dict(data)
        payload["mark_colors"] = _review_mark_colors_from_payload(payload)
        return payload

    @model_validator(mode="after")
    def _normalize_highlight_color(self):
        self.highlight_color = _normalize_hex_color(
            self.highlight_color,
            fallback=WORD_REVIEW_HIGHLIGHT_COLOR_DEFAULT,
        )
        self.mark_colors = _review_mark_colors_from_payload(
            {
                "highlight_color": self.highlight_color,
                "mark_colors": self.mark_colors,
            }
        )
        allowed = {"skip", "overwrite", "red_underline"}
        policy = str(self.existing_highlight_policy or "").strip()
        if policy not in allowed:
            policy = "red_underline"
        self.existing_highlight_policy = policy
        return self


class WordConversionSettings(BaseModel):
    use_native_preprocessing: bool = True
    prefer_native_word: bool = True

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_native_preference(cls, data):
        if not isinstance(data, dict):
            return data
        migrated = dict(data)
        if "use_native_preprocessing" not in migrated and "prefer_native_word" in migrated:
            migrated["use_native_preprocessing"] = bool(migrated.get("prefer_native_word"))
        if "prefer_native_word" not in migrated and "use_native_preprocessing" in migrated:
            migrated["prefer_native_word"] = bool(migrated.get("use_native_preprocessing"))
        return migrated

    @model_validator(mode="after")
    def _sync_legacy_native_preference(self):
        self.prefer_native_word = bool(self.use_native_preprocessing)
        return self


class ModelRoleSettings(TextProtocolSettings):
    """Cloud access settings owned by one model role."""

    source_role: str = "independent"
    # Only a text-capability role can actually run locally; the capability
    # guard in core.model_roles is what rejects local for the image and review
    # roles, so these fields exist on the shared class without offering it.
    mode: str = "cloud"  # "cloud" | "local"
    cloud_provider: str = DEFAULT_CLOUD_PROVIDER
    cloud_model: str = DEFAULT_CLOUD_MODEL
    cloud_base_url: str = DEFAULT_CUSTOM_OPENAI_BASE_URL
    cloud_provider_configs: dict[str, CloudProviderConfig] = Field(default_factory=dict)
    local_provider: str = DEFAULT_LOCAL_MODEL_PROVIDER
    local_model: str = ""
    local_base_url: str = OLLAMA_BASE_URL
    availability_status: str = "unknown"
    availability_message: str = ""
    availability_checked_at: str = ""
    availability_signature: str = ""
    connections: list[ModelConnection] = Field(default_factory=list)

    @model_validator(mode="after")
    def _normalize_role(self):
        if self.source_role not in MODEL_ROLE_SOURCE_VALUES:
            self.source_role = "independent"
        if self.mode not in {"cloud", "local"}:
            self.mode = "cloud"
        self.local_provider = _normalize_local_provider(self.local_provider)
        if not str(self.local_base_url or "").strip():
            self.local_base_url = _default_local_base_url(self.local_provider)
        self.local_model = str(self.local_model or "").strip()
        if self.availability_status not in {"unknown", "available", "unavailable"}:
            self.availability_status = "unknown"
        self.cloud_provider = str(self.cloud_provider or DEFAULT_CLOUD_PROVIDER).strip()
        self.cloud_model = str(self.cloud_model or "").strip()
        self.cloud_base_url = configured_cloud_base_url(
            self.cloud_provider,
            self.cloud_base_url,
        )
        self.cloud_provider_configs = _normalize_provider_configs(self.cloud_provider_configs)
        if self.cloud_model or self.cloud_base_url:
            existing = self.cloud_provider_configs.get(self.cloud_provider)
            if existing is None or not (existing.cloud_model or existing.cloud_base_url):
                self.cloud_provider_configs[self.cloud_provider] = CloudProviderConfig(
                    api_mode=self.api_mode,
                    cloud_model=self.cloud_model,
                    cloud_base_url=self.cloud_base_url,
                )
            else:
                self.api_mode = existing.api_mode
                self.cloud_model = existing.cloud_model
                self.cloud_base_url = existing.cloud_base_url
        _sync_connection_pool(self)
        return self


class PdfSettings(BaseModel):
    target_lang: str = "zh"
    page_retry_attempts: int = Field(
        default=PDF_PAGE_RETRY_ATTEMPTS_DEFAULT,
        ge=PDF_PAGE_RETRY_ATTEMPTS_MIN,
        le=PDF_PAGE_RETRY_ATTEMPTS_MAX,
    )
    page_generation_concurrency: int | None = Field(
        default=None,
        ge=1,
        le=2**31 - 1,
    )
    # 开启逐页审核时同时审核的页数；None 表示默认值（v38 起，原来跟审核模型的速率）。
    review_concurrency: int | None = Field(
        default=None,
        ge=1,
        le=2**31 - 1,
    )
    review_enabled: bool = False
    generate_compressed_pdf: bool = True
    # This controls independent image *inputs* only.  It never changes the
    # visual translation protocol for pages contained in a PDF.
    include_images: bool = True
    # 「跳过大幅面页」：工程 PDF 里 A3 及以上的大幅面页（多为 CAD 图纸）不送
    # 翻译模型，直接从源 PDF 矢量原样导入输出——这类页经不起栅格化，且大多数
    # 也不需要翻译。判定规则见 core.pdf_image_translation.is_oversized_page。
    skip_oversized_pages: bool = True
    # 翻译方式（Beta 分支新增，见 core.pdf_routes）：
    #   image —— 整页图像翻译（默认，和以前一样；大幅面页按上面的开关跳过或翻译）
    #   auto  —— 自动组合：逐页按类型处理，扫描页按 auto_scan_route，可提取文字的页
    #            按 auto_text_route（转 Word 时连续几页合成一段转 Word 翻译，再转回
    #            PDF 拼回原位；输出始终是 PDF，also_save_word 可另存 Word），图纸和
    #            大幅面页按 auto_drawing_route；首选用不了时按 auto_fallback 保底。
    # 分支早先还有 inplace / word 两个单一路线，读到时换算成等价的自动组合（见下面的
    # 校验）。没有版本号升级：都是加法字段，旧版本读到会忽略；分支若合入主线再升版。
    translation_route: Literal["image", "inplace", "word", "auto"] = "image"
    auto_scan_route: Literal["image", "skip"] = "image"
    auto_text_route: Literal["word", "inplace", "image"] = "word"
    auto_drawing_route: Literal["inplace", "skip", "image"] = "inplace"
    auto_fallback: Literal["image", "skip"] = "image"
    # 自动组合：整份文件都能提取文字、整份转 Word 翻译时，除了 PDF 另存一份 Word。默认关。
    also_save_word: bool = False
    # 以前的「大幅面起点」（A3 / A2 / A1 / A0）。2026-10-21 起自动组合先看内容再看尺寸
    # （见 core.pdf_routes），不再读这一项；字段留着，旧设置文件照常读写。
    oversized_page_threshold: Literal["A3", "A2", "A1", "A0"] = "A3"
    # 「自动识别场景」：翻译前先用文本翻译模型读一遍 PDF 的文字层，写一段文档说明（什么
    # 文档、什么专业、有歧义的短词指什么），带进每页的翻译要求里。扫描件没有文字
    # 层时这一步自动跳过。v33 新增，默认开。
    smart_context: bool = True
    # 原位替换写回译文（2026-10-21 起支持中文以外的目标语言）：与 CAD「译文写回」同一套规则。
    # 译文字体：sans 无衬线字体；original 接近原文字体（原文是衬线字体时用衬线字体）。
    # 放不下时先缩到初始比例、再按 0.1 一档缩到最小比例，仍放不下就按下面的策略处理。
    # 比例按目标语言分两套，``*_zh`` 那对用于中文，默认值与 CAD 相同
    # （core.cad_layout.default_fit_settings）。加法字段，旧文件按默认值补齐，不升版本号。
    inplace_font: Literal["sans", "original"] = "sans"
    # write_translation：有译文就写入，超出原位的项目列入复核；
    # preserve_original：版式优先，放不下时保留原文。
    inplace_overflow_policy: Literal["write_translation", "preserve_original"] = "write_translation"
    inplace_fit_default_scale: float = 0.7
    inplace_fit_min_scale: float = 0.4
    inplace_fit_default_scale_zh: float = 1.0
    inplace_fit_min_scale_zh: float = 0.4

    def inplace_fit(self, target_lang: str | None = None):
        """目标语言对应的那对缩放比例（core.cad_layout.FitSettings）。"""
        from core.cad_layout import FitSettings, is_chinese_target

        if is_chinese_target(self.target_lang if target_lang is None else target_lang):
            return FitSettings(self.inplace_fit_default_scale_zh, self.inplace_fit_min_scale_zh).normalized()
        return FitSettings(self.inplace_fit_default_scale, self.inplace_fit_min_scale).normalized()

    @model_validator(mode="after")
    def _normalize_inplace_layout(self):
        from core.cad_layout import FitSettings

        fit = FitSettings(self.inplace_fit_default_scale, self.inplace_fit_min_scale).normalized()
        self.inplace_fit_default_scale, self.inplace_fit_min_scale = fit.default_scale, fit.min_scale
        fit_zh = FitSettings(self.inplace_fit_default_scale_zh, self.inplace_fit_min_scale_zh).normalized()
        self.inplace_fit_default_scale_zh, self.inplace_fit_min_scale_zh = fit_zh.default_scale, fit_zh.min_scale
        return self

    @model_validator(mode="before")
    @classmethod
    def _normalize_blankable_concurrency(cls, data):
        if not isinstance(data, dict):
            return data
        migrated = dict(data)
        for key in ("page_generation_concurrency", "review_concurrency"):
            if migrated.get(key) in ("", None):
                migrated[key] = None
        # 分支早先的单一路线换算成等价的自动组合
        route = migrated.get("translation_route")
        if route == "inplace":
            migrated.update(translation_route="auto", auto_text_route="inplace", auto_drawing_route="inplace")
        elif route == "word":
            migrated.update(
                translation_route="auto",
                auto_text_route="word",
                auto_drawing_route="image" if migrated.get("skip_oversized_pages") is False else "skip",
            )
        # 认不出的取值（比如更新版本写下的新选项）退回默认，不能让整份设置读不进来。
        for key, allowed in _PDF_ROUTE_CHOICES.items():
            if key in migrated and migrated[key] not in allowed:
                migrated.pop(key)
        return migrated


_PDF_ROUTE_CHOICES: dict[str, tuple[str, ...]] = {
    "translation_route": ("image", "auto"),
    "auto_scan_route": ("image", "skip"),
    "auto_text_route": ("word", "inplace", "image"),
    "auto_drawing_route": ("inplace", "skip", "image"),
    "auto_fallback": ("image", "skip"),
    "oversized_page_threshold": ("A3", "A2", "A1", "A0"),
    "inplace_font": ("sans", "original"),
    "inplace_overflow_policy": ("write_translation", "preserve_original"),
}


class ModelThroughputSettings(BaseModel):
    """Per effective model tuning values."""

    batch_size: int | None = None
    concurrency: int | None = None
    output_translation_only: bool = False

    @model_validator(mode="before")
    @classmethod
    def _normalize_blank_values(cls, data):
        if not isinstance(data, dict):
            return data
        migrated = dict(data)
        for key in ("batch_size", "concurrency"):
            if migrated.get(key) in ("", None):
                migrated[key] = None
        return migrated


class UpdateSettings(BaseModel):
    # Update reminders are deliberately independent from a manual update
    # check.  A user can pause background notices without losing the ability
    # to check a release from the title bar.
    notifications_paused: bool = False
    ignored_release_version: str = ""
    last_background_check_at: str = ""

    @model_validator(mode="before")
    @classmethod
    def _normalize_update_payload(cls, data):
        if not isinstance(data, dict):
            return data
        migrated = dict(data)
        migrated["ignored_release_version"] = str(
            migrated.get("ignored_release_version") or ""
        ).strip()
        migrated["notifications_paused"] = bool(migrated.get("notifications_paused", False))
        migrated["last_background_check_at"] = str(
            migrated.get("last_background_check_at") or ""
        ).strip()
        return migrated


class OnboardingSettings(BaseModel):
    """State for the local quick-start flow; no legacy data is consulted."""

    quick_start_completed: bool = False
    # PDF / CAD 页的图文指引看过没有。旧设置文件没有这两项，读进来是 False：
    # 升级上来的老用户进这两页时也会自动弹一次（maple 2026-10-27 定的）。
    pdf_guide_seen: bool = False
    cad_guide_seen: bool = False


UI_LANGUAGES = ("zh", "zh-Hant", "en", "fr")
# 配色方案 id，与 ui/src/palette.ts、ui/src/styles/palettes.css 保持一致。
PALETTES = ("liuguang", "graphite", "warm", "cool", "celadon", "amber")
# 默认「流光」（v41 起），排在第一位；v40 及以前默认「素雅灰」（id 沿用早期名称 graphite），
# 旧文件里的 graphite 按 _migrate_palette_default 换成流光。
# v30 及更早的设置文件没有配色方案字段，补齐时同样取这个值。
DEFAULT_PALETTE = "liuguang"
PRE_V41_DEFAULT_PALETTE = "graphite"


class AppearanceSettings(BaseModel):
    """Persisted Tauri shell preferences shared across desktop launches."""

    # 默认浅色（V10 起）。v29 及更早的设置文件里存的 "system" 绝大多数是当时的
    # 默认值而不是用户选的，读取时按 _migrate_theme_default 换成 "light"。
    theme: str = "light"
    # 界面语言：中文 / English / Français。界面文字由前端按词条表翻译，后端只存选择。
    ui_language: str = "zh"
    # 配色方案（v31 起）：浅色模式和深色模式各选一个，可以跨方案搭配；theme 仍决定当前
    # 显示哪种模式。旧文件没有这两个字段，读取时按默认值补齐。
    palette_light: str = DEFAULT_PALETTE
    palette_dark: str = DEFAULT_PALETTE

    @model_validator(mode="after")
    def _normalize_theme(self):
        if self.theme not in {"system", "light", "dark"}:
            self.theme = "light"
        if self.ui_language not in UI_LANGUAGES:
            self.ui_language = "zh"
        if self.palette_light not in PALETTES:
            self.palette_light = DEFAULT_PALETTE
        if self.palette_dark not in PALETTES:
            self.palette_dark = DEFAULT_PALETTE
        return self


class ExtensionSettings(BaseModel):
    """一个运行时注册的翻译类型（键为 ``ext:<id>``）的设置，v39 起。

    内置四个页面的设置是 ``AppSettings`` 上按页面名平铺的字段（``excel_target_lang``、
    ``cad_domain_preset`` …）；运行时注册的类型没法往 ``AppSettings`` 上加字段，它的那份
    放在 ``AppSettings.extensions[<id>]``。读写一律经 ``core.surface_settings`` 的
    ``surface_setting`` / ``set_surface_setting``，调用方不必分辨是哪一种。
    加法改动：旧文件没有 ``extensions`` 时为空字典，不需要迁移。
    """

    target_lang: str = Field(default_factory=get_default_target_lang)
    source_lang: str = "auto"
    # 与内置页面同样的两层提示词：「自动识别场景」开关 + 选中的「补充要求」。
    domain_preset: str = "无"
    smart_context: bool = True
    domain_custom_prompts: dict[str, dict[str, str] | str] = Field(default_factory=dict)
    # 这个类型自己的开关（描述符里的 options），按开关的 key 存。
    options: dict[str, bool | int | float | str] = Field(default_factory=dict)
    # v39 的「显示在侧栏」和位置。v40 起由 ``AppSettings.translation_types`` 管（order 升级时
    # 读一次），界面不再写这两项；留着是为了旧文件照样能读。
    pinned: bool = True
    order: int = 0
    # 内置规则：空表示用默认的那份（清单 adapter.json 的 ``prompt``，没有则是
    # ``config.CORE_TRANSLATION_PROMPT``）；用户在「翻译参数」里改过才有值（v40 起）。
    prompt: str = ""
    # 输出位置（与 Word 页的 word_output 同两项）：关着时写在来源文件夹里。加法改动，旧文件读时补默认值。
    use_custom_output_dir: bool = False
    custom_output_dir: str = ""
    output_mode: Literal["task_folder", "source_folder"] = "task_folder"
    fallback_output_dir: str = ""


class TranslationTypesSettings(BaseModel):
    """侧栏「翻译」组：全部翻译类型（内置四种与 ``ext:<id>``）的顺序和是否显示，v40 起。

    「扩展」页里改。内置四种默认显示；扩展类型默认不显示，在「扩展」页打开，随应用提供、
    清单写了 ``visible_by_default`` 的（PPT）除外。不显示只是不放进侧栏：页面照常注册，任务中心「打开工作区」、命令行
    和 MCP 照常能用。至少要留一种显示，由 PUT /api/settings 和界面两头拦。
    """

    #: 类型键的顺序；没列到的（新装的类型等）按 GET /api/surfaces 的顺序排在后面。空表示默认顺序。
    order: list[str] = Field(default_factory=list)
    #: 「显示在侧栏」拨过的类型；没拨过的按默认（见 ``shows``）。
    visible: dict[str, bool] = Field(default_factory=dict)

    @field_validator("order", mode="before")
    @classmethod
    def _keys(cls, value: Any) -> list[str]:
        keys: list[str] = []
        for item in value if isinstance(value, list) else []:
            key = item.strip().lower() if isinstance(item, str) else ""
            if key and key not in keys:
                keys.append(key)
        return keys

    @field_validator("visible", mode="before")
    @classmethod
    def _switches(cls, value: Any) -> dict[str, bool]:
        switches: dict[str, bool] = {}
        for item, flag in value.items() if isinstance(value, dict) else ():
            key = item.strip().lower() if isinstance(item, str) else ""
            if key and isinstance(flag, bool):
                switches[key] = flag
        return switches

    def shows(self, key: str, *, visible_by_default: bool | None = None) -> bool:
        """``key`` 是否显示在侧栏：拨过的按拨的；没拨过的按 ``visible_by_default``（页面的
        ``Surface.visible_by_default``），不给时内置显示、扩展类型不显示。"""
        normalized = str(key or "").strip().lower()
        default = not normalized.startswith("ext:") if visible_by_default is None else visible_by_default
        return self.visible.get(normalized, default)


def _adopt_builtin_custom_target_langs(payload: dict) -> None:
    """自定义语言的名字后来成了内置语言（「繁體中文」）：用到它的地方改指内置语言。

    以前没有内置繁体时，用户自建过名为「繁體中文」的自定义语言；内置之后同名的自定义语言
    会被 normalize_custom_target_langs 去掉。只去掉不改引用，选它的任务会悄悄落回默认的
    英文。这里先把引用换成内置语言代码，自定义语言本身照旧由后面的校验去掉。
    """
    from core.language_registry import (
        decode_custom_target_lang_code,
        get_builtin_supported_target_languages_map,
    )

    builtin = get_builtin_supported_target_languages_map()
    remap: dict[str, str] = {}
    for item in payload.get("custom_target_langs") or []:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip()
        name = str(item.get("name") or decode_custom_target_lang_code(code) or "").strip()
        builtin_code = resolve_language_code(name, builtin) if name else None
        if code and builtin_code:
            remap[code] = builtin_code
    if not remap:
        return
    for field_name in ("target_lang", "excel_target_lang", "word_target_lang", "cad_target_lang", "tm_target_lang"):
        value = payload.get(field_name)
        if isinstance(value, str) and value in remap:
            payload[field_name] = remap[value]
    pdf_payload = payload.get("pdf")
    if isinstance(pdf_payload, dict) and pdf_payload.get("target_lang") in remap:
        payload["pdf"] = {**pdf_payload, "target_lang": remap[pdf_payload["target_lang"]]}
    recent = payload.get("recent_target_langs")
    if isinstance(recent, list):
        payload["recent_target_langs"] = [remap.get(value, value) if isinstance(value, str) else value for value in recent]


class AppSettings(BaseModel):
    _runtime_model_role: str = PrivateAttr(default="translation")
    # settings_for_text_role 填入：这次拨号的连接的深度思考设置（auto / off / on）。
    _runtime_thinking: str = PrivateAttr(default="auto")
    engine: EngineSettings = Field(default_factory=EngineSettings)
    tm: TMSettings = Field(default_factory=TMSettings)
    output: OutputSettings = Field(default_factory=OutputSettings)
    excel_output: ExcelOutputSettings = Field(default_factory=ExcelOutputSettings)
    word_output: WordOutputSettings = Field(default_factory=WordOutputSettings)
    pdf_output: PdfOutputSettings = Field(default_factory=PdfOutputSettings)
    excel_review: ExcelReviewSettings = Field(default_factory=ExcelReviewSettings)
    rate: RateSettings = Field(default_factory=RateSettings)
    process_retention: ProcessRetentionSettings = Field(default_factory=ProcessRetentionSettings)
    word_review: WordReviewSettings = Field(default_factory=WordReviewSettings)
    word_conversion: WordConversionSettings = Field(default_factory=WordConversionSettings)
    cleaner_model_role: ModelRoleSettings = Field(
        default_factory=lambda: ModelRoleSettings(source_role="translation")
    )
    image_model_role: ModelRoleSettings = Field(
        default_factory=lambda: ModelRoleSettings(source_role="translation", cloud_model="")
    )
    pdf_review_model_role: ModelRoleSettings = Field(
        default_factory=lambda: ModelRoleSettings(source_role="translation", cloud_model="")
    )
    pdf: PdfSettings = Field(default_factory=PdfSettings)
    # 关闭时多余的连接只作故障切换备用；打开后并行任务才会分散到不同连接上。
    spread_tasks_across_connections: bool = False
    model_throughput_profiles: dict[str, ModelThroughputSettings] = Field(default_factory=dict)
    model_upgrade_states: dict[str, ModelUpgradeState] = Field(default_factory=dict)
    update: UpdateSettings = Field(default_factory=UpdateSettings)
    onboarding: OnboardingSettings = Field(default_factory=OnboardingSettings)
    appearance: AppearanceSettings = Field(default_factory=AppearanceSettings)
    settings_version: int = SETTINGS_SCHEMA_VERSION
    source_lang: str = Field(default_factory=get_default_source_lang)
    target_lang: str = Field(default_factory=get_default_target_lang)
    excel_source_lang: str = "auto"
    word_source_lang: str = "auto"
    excel_target_lang: str = Field(default_factory=get_default_target_lang)
    word_target_lang: str = "fr"
    # CAD 页自己的一对语言，与 Excel/Word 分开记忆；图纸最常见的是外文 → 中文。
    cad_source_lang: str = "auto"
    cad_target_lang: str = "zh"
    tm_source_lang: str = "zh"
    tm_target_lang: str = Field(default_factory=get_default_target_lang)
    recent_tm_lang_pairs: list[str] = Field(default_factory=list)
    custom_target_langs: list[CustomTargetLang] = Field(default_factory=list)
    recent_target_langs: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_image_model(cls, data):
        """Upgrade retired models once while loading an older settings payload."""
        if isinstance(data, BaseModel):
            data = data.model_dump(mode="python")
        if not isinstance(data, dict):
            return data
        migrated = dict(data)
        # 「自动识别场景」在开发版里试用时叫过「智能」，已存下的设置跟着改名。
        for field in _PRE_V33_DEFAULT_DOMAINS:
            if str(migrated.get(field) or "").strip() == "智能":
                migrated[field] = "自动识别场景"
        # This destructive migration belongs to v27.  Later additive schema
        # bumps must not re-map a user's explicit v27 model selection.
        if _extract_settings_version(migrated) >= 27:
            return migrated

        def migrate_model(value):
            model = str(value or "").strip()
            if model == "gpt-image-2":
                return "gpt-image-2.5-flare"
            if model.lower().startswith("deepseek-"):
                return "deepseek-flash"
            return value

        def migrate_role(payload):
            if isinstance(payload, BaseModel):
                payload = payload.model_dump(mode="python")
            if not isinstance(payload, dict):
                return payload
            role = dict(payload)
            for field in ("cloud_model", "model"):
                if field in role:
                    role[field] = migrate_model(role[field])
            if isinstance(role.get("cloud_provider_configs"), dict):
                role["cloud_provider_configs"] = {
                    provider: (
                        {**config, "cloud_model": migrate_model(config.get("cloud_model"))}
                        if isinstance(config, dict) else config
                    )
                    for provider, config in role["cloud_provider_configs"].items()
                }
            if isinstance(role.get("connections"), list):
                role["connections"] = [
                    {**conn, "model": migrate_model(conn.get("model"))}
                    if isinstance(conn, dict) else conn
                    for conn in role["connections"]
                ]
            return role

        for role_name in ("engine", "cleaner_model_role", "image_model_role", "pdf_review_model_role"):
            if role_name in migrated:
                migrated[role_name] = migrate_role(migrated[role_name])
        for field in ("cleaner_model",):
            if field in migrated:
                migrated[field] = migrate_model(migrated[field])
        return migrated
    # v35 起提示词分两层（见 config.CORE_TRANSLATION_PROMPT）：内置规则固定，
    # ``*_smart_context`` 是「自动识别场景」开关，``*_domain_preset`` 只表示选中的
    # 「补充要求」——「无」或 ``*_domain_custom_prompts`` 里的一个名字。
    # ``*_custom_prompt``、``*_domain_name_overrides``、``*_domain_prompt_overrides``、
    # ``*_domain_disabled_presets`` 是 v34 及以前的场景预设数据，读到旧文件时由
    # ``migrate_prompt_layers`` 转换后清空，字段本身留着，旧文件和旧导出包才读得进来。
    domain_preset: str = "无"
    domain_smart_context: bool = True
    custom_prompt: str = ""
    # Excel and Word intentionally own separate domain/prompt state.  The
    # legacy global fields remain as an inert compatibility surface for CLI
    # callers; page-aware task code reads the fields below.
    excel_domain_preset: str = "无"
    excel_smart_context: bool = True
    excel_custom_prompt: str = ""
    excel_domain_name_overrides: dict[str, str] = Field(default_factory=dict)
    # Prompt 覆盖新版本按「预设名 → {_base: Prompt}」保存；旧版目标语言映射仍可
    # 读取，便于升级后逐步收敛，不让旧配置失效。
    excel_domain_prompt_overrides: dict[str, dict[str, str]] = Field(default_factory=dict)
    # User-managed named prompt presets. Values may be a language map or a
    # legacy/plain string; the API normalizes writes while reads stay tolerant.
    excel_domain_custom_prompts: dict[str, dict[str, str] | str] = Field(default_factory=dict)
    excel_domain_disabled_presets: list[str] = Field(default_factory=list)
    word_domain_preset: str = "无"
    word_smart_context: bool = True
    word_custom_prompt: str = ""
    word_domain_name_overrides: dict[str, str] = Field(default_factory=dict)
    word_domain_prompt_overrides: dict[str, dict[str, str]] = Field(default_factory=dict)
    word_domain_custom_prompts: dict[str, dict[str, str] | str] = Field(default_factory=dict)
    word_domain_disabled_presets: list[str] = Field(default_factory=list)
    cad_domain_preset: str = "无"
    cad_smart_context: bool = True
    cad_custom_prompt: str = ""
    cad_domain_name_overrides: dict[str, str] = Field(default_factory=dict)
    cad_domain_prompt_overrides: dict[str, dict[str, str]] = Field(default_factory=dict)
    cad_domain_custom_prompts: dict[str, dict[str, str] | str] = Field(default_factory=dict)
    cad_domain_disabled_presets: list[str] = Field(default_factory=list)
    cad_output: CadOutputSettings = Field(default_factory=CadOutputSettings)
    # 运行时注册的翻译类型各自的设置，按类型 id（不带 ``ext:``）存，见 ExtensionSettings。
    extensions: dict[str, ExtensionSettings] = Field(default_factory=dict)
    # 侧栏「翻译」组里全部翻译类型的顺序与隐藏（「扩展」页，v40 起）。
    translation_types: TranslationTypesSettings = Field(default_factory=TranslationTypesSettings)
    last_source_folder: str = ""
    last_excel_source_folder: str = ""
    last_word_source_folder: str = ""
    last_pdf_source_folder: str = ""
    last_cad_source_folder: str = ""
    cleaner_mode: str = "diff"  # 清洗始终先生成建议，确认后才写入
    cleaner_engine: str = DEFAULT_CLOUD_PROVIDER
    cleaner_model: str = ""
    cleaner_prompt_extras: dict[str, str] = Field(default_factory=dict)
    cleaner_full_prompt_overrides: dict[str, str] = Field(default_factory=dict)
    domain_name_overrides: dict[str, str] = Field(default_factory=dict)
    domain_prompt_overrides: dict[str, dict[str, str]] = Field(default_factory=dict)
    # Compatibility fields populated when a page-owned configuration is
    # activated into the legacy global translation context.
    domain_custom_prompts: dict[str, dict[str, str] | str] = Field(default_factory=dict)
    domain_disabled_presets: list[str] = Field(default_factory=list)

    # What was on disk when this object was loaded.  ``save_settings`` diffs
    # against it so a write only touches the fields this caller changed; see
    # ``_settings_delta``.  Private attributes never take part in validation
    # or ``model_dump``, so this stays invisible to the API surface.
    _persisted_snapshot: dict | None = PrivateAttr(default=None)

    @model_validator(mode="before")
    @classmethod
    def _migrate_model_role_payload(cls, data):
        if not isinstance(data, dict):
            return data
        migrated = dict(data)
        # Preserve pre-page-split settings while giving Excel and Word
        # independent domain/Prompt state going forward.
        if "domain_preset" in migrated:
            migrated.setdefault("excel_domain_preset", migrated.get("domain_preset"))
            migrated.setdefault("word_domain_preset", migrated.get("domain_preset"))
            migrated.setdefault("cad_domain_preset", migrated.get("domain_preset"))
        if "custom_prompt" in migrated:
            migrated.setdefault("excel_custom_prompt", migrated.get("custom_prompt"))
            migrated.setdefault("word_custom_prompt", migrated.get("custom_prompt"))
            migrated.setdefault("cad_custom_prompt", migrated.get("custom_prompt"))
        if "domain_name_overrides" in migrated:
            migrated.setdefault("excel_domain_name_overrides", migrated.get("domain_name_overrides"))
            migrated.setdefault("word_domain_name_overrides", migrated.get("domain_name_overrides"))
            migrated.setdefault("cad_domain_name_overrides", migrated.get("domain_name_overrides"))
        if "domain_prompt_overrides" in migrated:
            migrated.setdefault("excel_domain_prompt_overrides", migrated.get("domain_prompt_overrides"))
            migrated.setdefault("word_domain_prompt_overrides", migrated.get("domain_prompt_overrides"))

        # 旧版 Prompt 覆盖是「预设名 → Prompt」的扁平字典，不区分目标语言。升级时
        # 把它归入当时磁盘上的目标语言名下（用户最后在用的语言，最可能就是这份
        # 覆盖写作时面向的语言），其他语言回内置默认。嵌套的新形态原样通过——这个
        # 迁移必须幂等，因为每次校验都会经过这里。放在上面的 setdefault 之后，
        # 让页面拆分前的旧全局覆盖也按各自页面的语言各包一层。
        def _wrap_flat_prompt_overrides(field: str, lang_field: str) -> None:
            raw = migrated.get(field)
            if not isinstance(raw, dict):
                return
            home_lang = ""
            wrapped: dict[str, dict[str, str]] = {}
            for preset, value in raw.items():
                if isinstance(value, dict):
                    wrapped[str(preset)] = {
                        str(k): v for k, v in value.items() if isinstance(v, str)
                    }
                elif isinstance(value, str):
                    if not home_lang:
                        # 语言字段稍后会被 _normalize_target_lang_state 归一化成
                        # 语言代码（例如「日语」→ ja）。覆盖的键必须先过同一道
                        # 解析，否则落在没人查的原始字符串键下、覆盖无声失效。
                        # 只在真的碰到扁平值时才解析：嵌套稳态下这里一次都不跑。
                        home_lang = resolve_language_code(
                            str(
                                migrated.get(lang_field)
                                or migrated.get("target_lang")
                                or ""
                            ),
                            get_supported_languages(
                                migrated.get("custom_target_langs") or [],
                                include_optional=True,
                            ),
                        ) or get_default_target_lang()
                    wrapped[str(preset)] = {home_lang: value}
            migrated[field] = wrapped

        _wrap_flat_prompt_overrides("excel_domain_prompt_overrides", "excel_target_lang")
        _wrap_flat_prompt_overrides("word_domain_prompt_overrides", "word_target_lang")
        _wrap_flat_prompt_overrides("domain_prompt_overrides", "target_lang")
        engine_payload = dict(migrated.get("engine") or {})

        if "cleaner_model_role" not in migrated:
            cleaner_provider = str(
                migrated.get("cleaner_engine")
                or engine_payload.get("cloud_provider")
                or DEFAULT_CLOUD_PROVIDER
            ).strip()
            cleaner_model = str(migrated.get("cleaner_model") or "").strip()
            follows_translation = (
                not cleaner_model
                and cleaner_provider
                == str(engine_payload.get("cloud_provider") or DEFAULT_CLOUD_PROVIDER).strip()
            )
            migrated["cleaner_model_role"] = {
                "source_role": "translation" if follows_translation else "independent",
                "cloud_provider": cleaner_provider or DEFAULT_CLOUD_PROVIDER,
                "cloud_model": cleaner_model
                or str(engine_payload.get("cloud_model") or DEFAULT_CLOUD_MODEL).strip(),
                "cloud_base_url": str(
                    engine_payload.get("cloud_base_url") or DEFAULT_CUSTOM_OPENAI_BASE_URL
                ).strip(),
            }

        migrated.setdefault(
            "image_model_role",
            {
                "source_role": "translation",
                "cloud_provider": str(
                    engine_payload.get("cloud_provider") or DEFAULT_CLOUD_PROVIDER
                ).strip(),
                "cloud_base_url": str(
                    engine_payload.get("cloud_base_url") or DEFAULT_CUSTOM_OPENAI_BASE_URL
                ).strip(),
                "availability_status": "unknown",
            },
        )
        migrated.setdefault(
            "pdf_review_model_role",
            {
                "source_role": "translation",
                "cloud_provider": str(
                    engine_payload.get("cloud_provider") or DEFAULT_CLOUD_PROVIDER
                ).strip(),
                "cloud_base_url": str(
                    engine_payload.get("cloud_base_url") or DEFAULT_CUSTOM_OPENAI_BASE_URL
                ).strip(),
                "availability_status": "unknown",
            },
        )
        migrated.setdefault("pdf", PdfSettings().model_dump())
        migrated.setdefault("cad_output", CadOutputSettings().model_dump())
        migrated.setdefault("model_throughput_profiles", {})
        migrated.setdefault("update", UpdateSettings().model_dump())
        return migrated

    @model_validator(mode="before")
    @classmethod
    def _migrate_custom_target_lang_payload(cls, data):
        if not isinstance(data, dict):
            return data
        migrated = dict(data)
        _adopt_builtin_custom_target_langs(migrated)
        migrated["custom_target_langs"] = [
            entry.model_dump()
            for entry in normalize_custom_target_langs(migrated.get("custom_target_langs"))
        ]
        return migrated

    @model_validator(mode="after")
    def _stabilize_seeded_connection_ids(self):
        """Give a freshly seeded pool an id derived from its role.

        load_settings() does not persist on a fresh install, so a random id
        would differ on every read and any key saved against it would be
        orphaned by the next one.  Ids that came from stored JSON are left
        untouched.
        """
        for role_key, owner in (
            ("translation", self.engine),
            ("cleaner", self.cleaner_model_role),
            ("image", self.image_model_role),
            ("pdf_review", self.pdf_review_model_role),
        ):
            for connection in owner.connections:
                if connection.id.startswith(_SEEDED_CONNECTION_PREFIX):
                    connection.id = f"pool-{role_key}"
        return self

    @model_validator(mode="after")
    def _normalize_locked_pdf_concurrency(self):
        if not self.engine.concurrency_unlocked:
            for key in ("page_generation_concurrency", "review_concurrency"):
                value = getattr(self.pdf, key)
                if value is not None:
                    setattr(self.pdf, key, min(PDF_PAGE_CONCURRENCY_SAFETY_CAP, value))
            self.engine.batch_size = max(5, min(30, self.engine.batch_size))
        return self

    @model_validator(mode="after")
    def _normalize_target_lang_state(self):
        self.custom_target_langs = normalize_custom_target_langs(self.custom_target_langs)
        default_source_lang = get_default_source_lang()
        default_target_lang = get_default_target_lang()
        default_pdf_target_lang = "zh"
        target_supported_map = get_supported_languages(
            self.custom_target_langs,
            include_optional=True,
        )
        source_supported_map = get_supported_source_languages()

        if is_auto_source_lang(self.source_lang):
            self.source_lang = "auto"
        else:
            resolved_source_lang = resolve_language_code(
                self.source_lang,
                source_supported_map,
            )
            self.source_lang = resolved_source_lang or default_source_lang

        for field_name in ("excel_source_lang", "word_source_lang", "cad_source_lang"):
            value = getattr(self, field_name, "auto")
            if is_auto_source_lang(value):
                setattr(self, field_name, "auto")
            else:
                resolved = resolve_language_code(value, source_supported_map)
                setattr(self, field_name, resolved or default_source_lang)

        for field_name in ("excel_target_lang", "word_target_lang", "cad_target_lang"):
            value = getattr(self, field_name, default_target_lang)
            resolved = resolve_language_code(value, target_supported_map)
            setattr(self, field_name, resolved or default_target_lang)

        resolved_tm_source = resolve_language_code(self.tm_source_lang, source_supported_map)
        self.tm_source_lang = resolved_tm_source or default_source_lang
        resolved_tm_target = resolve_language_code(self.tm_target_lang, target_supported_map)
        self.tm_target_lang = resolved_tm_target or default_target_lang
        self.recent_tm_lang_pairs = [
            str(pair).strip()
            for pair in self.recent_tm_lang_pairs
            if isinstance(pair, str) and "-" in pair
        ][:20]

        resolved_target_lang = resolve_language_code(
            self.target_lang,
            target_supported_map,
        )
        if resolved_target_lang:
            self.target_lang = resolved_target_lang
        resolved_pdf_target_lang = resolve_language_code(
            self.pdf.target_lang,
            target_supported_map,
        )
        if resolved_pdf_target_lang:
            self.pdf.target_lang = resolved_pdf_target_lang

        self.recent_target_langs = normalize_recent_target_langs(
            self.recent_target_langs,
            self.custom_target_langs,
            include_optional=True,
        )

        if not is_auto_source_lang(self.source_lang) and not is_supported_source_lang(
            self.source_lang
        ):
            self.source_lang = default_source_lang

        if not is_supported_target_lang(
            self.target_lang,
            self.custom_target_langs,
            include_optional=True,
        ):
            self.target_lang = (
                self.recent_target_langs[0]
                if self.recent_target_langs else default_target_lang
            )

        if self.source_lang == default_source_lang and self.target_lang == self.source_lang:
            self.target_lang = next(
                (lang for lang in self.recent_target_langs if lang != self.source_lang),
                default_target_lang,
            )

        if not is_supported_target_lang(
            self.pdf.target_lang,
            self.custom_target_langs,
            include_optional=True,
        ):
            self.pdf.target_lang = default_pdf_target_lang

        self.recent_target_langs = remember_recent_target_lang(
            self.recent_target_langs,
            self.target_lang,
            self.custom_target_langs,
            include_optional=True,
        )
        return self


def _seed_packaged_default_api_key() -> None:
    """Seed the packaged default API key on first launch."""
    if KEYS_PATH.exists():
        return
    default_api_key = str(DEFAULT_CUSTOM_OPENAI_API_KEY or "").strip()
    if not default_api_key or default_api_key in {"*", "**", "***"}:
        return
    save_key(DEFAULT_CLOUD_PROVIDER, default_api_key)


def _local_file_lock(path: Path) -> threading.RLock:
    """Return the in-process lock paired with one inter-process lock file."""
    lock_key = str(path.resolve())
    with _LOCAL_FILE_LOCKS_GUARD:
        return _LOCAL_FILE_LOCKS.setdefault(lock_key, threading.RLock())


@contextmanager
def _exclusive_file_lock(path: Path):
    """Serialize a short file transaction across threads and app processes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    local_lock = _local_file_lock(path)
    with local_lock, path.open("a+b") as lock_file:
        if os.name == "nt":
            import msvcrt

            if lock_file.seek(0, os.SEEK_END) == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def restrict_windows_file_to_owner(path: Path) -> bool:
    """Give only the current account access to one file on Windows.

    Windows has no equivalent of ``chmod 0600``: the POSIX mode is ignored, so
    a secrets file inherits whatever the parent directory grants.  ``icacls``
    is the one tool always present on a stock install — drop inherited entries
    and grant full access to this account alone.

    Best effort by design.  The user data directory is already per-account
    (``%LOCALAPPDATA%``), so a failure here narrows nothing that was open
    before; it just means the file keeps the directory's inherited ACL.
    Returns whether the tightening was applied.
    """
    if os.name != "nt":
        return False
    account = (os.environ.get("USERNAME") or "").strip()
    if not account:
        try:
            account = getpass.getuser().strip()
        except Exception:
            return False
    if not account:
        return False
    domain = (os.environ.get("USERDOMAIN") or "").strip()
    principal = f"{domain}\\{account}" if domain else account
    try:
        completed = subprocess.run(
            [
                "icacls",
                str(path),
                "/inheritance:r",
                "/grant:r",
                f"{principal}:F",
            ],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except Exception as exc:
        logger.warning(f"未能收紧文件权限（icacls 不可用）：{type(exc).__name__}")
        return False
    if completed.returncode != 0:
        logger.warning(f"未能收紧文件权限：icacls 退出码 {completed.returncode}")
        return False
    return True


def _write_text_atomic(
    path: Path,
    content: str,
    *,
    file_mode: int | None = None,
) -> None:
    """Flush and atomically replace a text file using a unique sibling temp file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temp_path = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temp_path = Path(raw_temp_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as temp_file:
            fd = -1
            temp_file.write(content)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        if file_mode is not None:
            if os.name == "nt":
                # Tighten before the rename so the destination never exists
                # with a looser ACL; a DACL set here survives ``os.replace``.
                restrict_windows_file_to_owner(temp_path)
            else:
                temp_path.chmod(file_mode)
        os.replace(temp_path, path)
        if os.name != "nt":
            try:
                directory_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
    finally:
        if fd >= 0:
            os.close(fd)
        temp_path.unlink(missing_ok=True)


def write_private_text_file(path: Path, content: str) -> None:
    """Atomically write sensitive text with owner-only POSIX permissions."""
    _write_text_atomic(
        path,
        content,
        file_mode=stat.S_IRUSR | stat.S_IWUSR,
    )


def _extract_settings_version(data: dict) -> int:
    """Read the persisted settings schema version; old files default to 0."""
    raw_version = data.get("settings_version", 0)
    try:
        version = int(raw_version)
    except (TypeError, ValueError):
        return 0
    return max(version, 0)


def _migrate_theme_default(payload: dict, stored_version: int) -> dict:
    """v30: the default theme became "light"; carry old files' default along.

    Up to v29 the default was "system", and nothing in a v29 file tells a
    default apart from a deliberate pick.  Almost every install never touched
    it, so an old "system" is read as the old default and becomes "light".
    Done at read time (not in a model validator) so a user who picks "system"
    again after the upgrade is never re-migrated by a later validation of the
    same in-memory payload.
    """
    if stored_version >= 30:
        return payload
    appearance = payload.get("appearance")
    if not isinstance(appearance, dict) or appearance.get("theme", "system") != "system":
        return payload
    migrated = dict(payload)
    migrated["appearance"] = {**appearance, "theme": "light"}
    return migrated


def _migrate_palette_default(payload: dict, stored_version: int) -> dict:
    """v41: the default palette became 「流光」 (liuguang); carry old files' default along.

    Up to v40 the default was 「素雅灰」 (graphite), and nothing in a v40 file tells
    the default apart from a deliberate pick.  Almost every install never touched
    it, so an old "graphite" is read as the old default and becomes "liuguang";
    any other palette was picked on purpose and is kept.  Same reasoning and same
    read-time placement as ``_migrate_theme_default``.
    """
    if stored_version >= 41:
        return payload
    appearance = payload.get("appearance")
    if not isinstance(appearance, dict):
        return payload
    changed = {
        key: DEFAULT_PALETTE
        for key in ("palette_light", "palette_dark")
        if appearance.get(key) == PRE_V41_DEFAULT_PALETTE
    }
    if not changed:
        return payload
    migrated = dict(payload)
    migrated["appearance"] = {**appearance, **changed}
    return migrated


# v33 之前各页面的默认领域：Excel「无」，其余「同步工程场景」。
_PRE_V33_DEFAULT_DOMAINS = {
    "domain_preset": "同步工程场景",
    "excel_domain_preset": "无",
    "word_domain_preset": "同步工程场景",
    "cad_domain_preset": "同步工程场景",
}


def _migrate_smart_domain_default(payload: dict, stored_version: int) -> dict:
    """v33: every page's default domain became 「自动识别场景」; carry old defaults along.

    Same reasoning as the theme migration above: an old file cannot tell the
    default apart from a deliberate pick, and almost nobody changed it, so a
    page still on its pre-v33 default moves to 「自动识别场景」.  A page set to any
    other domain (or a named prompt) keeps it.
    """
    if stored_version >= 33:
        return payload
    migrated = dict(payload)
    for key, old_default in _PRE_V33_DEFAULT_DOMAINS.items():
        if str(migrated.get(key, old_default) or old_default).strip() == old_default:
            migrated[key] = "自动识别场景"
    return migrated


# 提示词各层所在的字段，按页面分：前缀 "" 是给 CLI 和任务快照用的全局兼容字段。
_PROMPT_LAYER_SURFACES = {
    "": "domain_smart_context",
    "excel_": "excel_smart_context",
    "word_": "word_smart_context",
    "cad_": "cad_smart_context",
}
# V9 旧版单条自定义提示词转成补充要求时用的名字。
LEGACY_CUSTOM_PROMPT_NAME = "旧版自定义提示词"


def _prompt_text(value: object) -> str:
    """One prompt out of a stored value: ``{"_base": ...}``, an old language map, or a string."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        text = str(value.get("_base") or "").strip()
        if text:
            return text
        return next(
            (str(item).strip() for item in value.values() if isinstance(item, str) and item.strip()),
            "",
        )
    return ""


def _unique_extra_name(name: str, taken: set[str]) -> str:
    """A name for a converted prompt that clashes with neither reserved names nor existing ones."""
    base = name.strip() or LEGACY_CUSTOM_PROMPT_NAME
    if base in RESERVED_EXTRA_PROMPT_NAMES:
        base = f"{base}（旧版修改）"
    candidate = base
    index = 2
    while candidate in taken or candidate in RESERVED_EXTRA_PROMPT_NAMES:
        candidate = f"{base}{index}"
        index += 1
    return candidate


def migrate_prompt_layers(payload: dict, prefixes: Iterable[str] | None = None) -> dict:
    """v35: turn the old per-page 「场景预设」 into 内置规则 + 自动识别开关 + 补充要求.

    Up to v34 each page picked one whole prompt: 「无」, one of four built-in
    scenes (any of them editable), a named prompt of the user's, or the V9
    single custom prompt.  From v35 the built-in rules are fixed in code, so:

    * a page on 「自动识别场景」 or one of the three retired scenes keeps
      auto-detect on; 「无」, a named prompt or the V9 custom prompt turns it off,
      which is what those pages were doing before;
    * every edited built-in scene and the V9 custom prompt becomes a named
      补充要求 so nothing the user wrote is lost, and a page that was on an
      edited scene keeps that text selected;
    * the old override / rename / disable fields are emptied.

    Pure: works on a copy of a settings (or export-bundle) dict.  Only the
    page prefixes passed in are touched; a key the payload does not carry reads
    as that version's default.
    """
    migrated = dict(payload)
    if prefixes is None:
        # 页面拆分前的文件只有全局字段：先照 AppSettings 的校验器那样分给各页面，
        # 再逐页转换，否则各页面会按缺省值处理，丢掉用户当时的选择。
        for field in ("domain_preset", "custom_prompt", "domain_name_overrides", "domain_prompt_overrides"):
            if field in migrated:
                for page in ("excel_", "word_", "cad_"):
                    migrated.setdefault(f"{page}{field}", migrated[field])
    for prefix in (prefixes if prefixes is not None else _PROMPT_LAYER_SURFACES):
        smart_key = _PROMPT_LAYER_SURFACES[prefix]
        preset_key = f"{prefix}domain_preset"
        custom_key = f"{prefix}custom_prompt"
        names_key = f"{prefix}domain_name_overrides"
        overrides_key = f"{prefix}domain_prompt_overrides"
        named_key = f"{prefix}domain_custom_prompts"
        disabled_key = f"{prefix}domain_disabled_presets"

        preset = str(migrated.get(preset_key, "自动识别场景") or "").strip() or "自动识别场景"
        if preset == "智能":
            preset = "自动识别场景"
        raw_named = migrated.get(named_key)
        extras: dict[str, dict[str, str]] = {}
        for raw_name, raw_value in (raw_named.items() if isinstance(raw_named, dict) else ()):
            name = str(raw_name or "").strip()
            text = _prompt_text(raw_value)
            if name and text:
                if name in RESERVED_EXTRA_PROMPT_NAMES:
                    name = _unique_extra_name(name, set(extras))
                extras[name] = {"_base": text}
        name_overrides = migrated.get(names_key)
        name_overrides = name_overrides if isinstance(name_overrides, dict) else {}
        overrides = migrated.get(overrides_key)
        converted: dict[str, str] = {}
        for scene in LEGACY_DOMAIN_PRESET_NAMES:
            text = _prompt_text(overrides.get(scene)) if isinstance(overrides, dict) else ""
            if not text:
                continue
            shown = str(name_overrides.get(scene) or "").strip() or scene
            name = _unique_extra_name(shown, set(extras))
            extras[name] = {"_base": text}
            converted[scene] = name
        legacy_text = str(migrated.get(custom_key) or "").strip()
        if legacy_text:
            legacy_name = _unique_extra_name(LEGACY_CUSTOM_PROMPT_NAME, set(extras))
            extras[legacy_name] = {"_base": legacy_text}
            converted["自定义"] = legacy_name

        if preset in LEGACY_DOMAIN_PRESET_NAMES:
            smart, selected = True, converted.get(preset, NO_EXTRA_PROMPT)
        elif preset == "自定义":
            smart, selected = False, converted.get("自定义", NO_EXTRA_PROMPT)
        elif preset in extras:
            smart, selected = False, preset
        elif preset == NO_EXTRA_PROMPT:
            smart, selected = False, NO_EXTRA_PROMPT
        else:
            # 指向一个已经不存在的提示词：按新默认来。
            smart, selected = True, NO_EXTRA_PROMPT

        migrated[smart_key] = smart
        migrated[preset_key] = selected
        migrated[named_key] = extras
        migrated[custom_key] = ""
        migrated[names_key] = {}
        migrated[overrides_key] = {}
        migrated[disabled_key] = []
    return migrated


def _has_legacy_prompt_data(payload: dict | None) -> bool:
    """Whether the v35 prompt migration rewrites something the user chose or wrote.

    A file still on the old defaults carries nothing worth a backup; one with an
    edited scene, a renamed or disabled scene, the V9 custom prompt, or a page on
    one of the retired scenes does.
    """
    if not isinstance(payload, dict):
        return False
    for prefix in _PROMPT_LAYER_SURFACES:
        for field in ("custom_prompt", "domain_name_overrides", "domain_prompt_overrides", "domain_disabled_presets"):
            if payload.get(f"{prefix}{field}"):
                return True
        preset = str(payload.get(f"{prefix}domain_preset") or "").strip()
        if preset in LEGACY_DOMAIN_PRESET_NAMES[1:] or preset == "自定义":
            return True
    return False


def _raw_settings_payload() -> dict | None:
    """The settings file as stored, before any read-time migration (None if unreadable)."""
    try:
        payload = json.loads(SETTINGS_PATH.read_bytes().decode("utf-8-sig"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _raw_prompt_choices(stored_version: int) -> dict | None:
    """The stored file with pre-v33 default scenes already read as 自动识别场景.

    A v30–v32 file that still names 同步工程场景 is on the old default, not a
    choice; v33 would have moved it to 自动识别场景 anyway, so it must not
    trigger the v35 backup on its own.
    """
    raw = _raw_settings_payload()
    return None if raw is None else _migrate_smart_domain_default(raw, stored_version)


def _migrate_prompt_layers(payload: dict, stored_version: int) -> dict:
    """Read-time hook for :func:`migrate_prompt_layers` (files older than v35)."""
    if stored_version >= 35:
        return payload
    return migrate_prompt_layers(payload)


# v37 及以前的默认值。只用来判断用户改没改过速率：改过的话升级前先备份旧文件，
# 因为升级后这些值一律改用 v38 的新默认值（maple 2026-10-14 定）。默认值本身在
# v38 变了（并发 20 → 30、Word 字符 800 → 1000 等），所以这里写死旧值，不引用
# config 里的现行常量。
_PRE_V38_TEXT_THROUGHPUT_DEFAULTS = {
    "cloud": {"batch_size": 20, "concurrency": 20},
    "local": {"batch_size": 12, "concurrency": 3},
}
_PRE_V38_PAGE_CONCURRENCY_DEFAULTS = {"cloud": 2, "local": 3}
_PRE_V38_REVIEW_CONCURRENCY_DEFAULTS = {"cloud": 1, "local": 3}
_PRE_V38_PAGE_RETRY_DEFAULT = 1
_PRE_V38_REQUEST_RETRY_DEFAULT = 2
_PRE_V38_WORD_BATCH_DEFAULTS = {
    "max_chars_per_batch": ("batch_chars", 800),
    "max_paragraphs_per_batch": ("batch_items", 8),
    "strict_retry_attempts": ("item_retry", 3),
}
RATE_SURFACE_KEYS = ("excel", "word", "pdf_text", "cad")
# PDF 整页图像翻译的三项，v38 起归「翻译参数 · 速率与重试」，升级时同样回到默认值。
_PDF_RATE_KEYS = ("page_generation_concurrency", "page_retry_attempts", "review_concurrency")


def _pre_v38_role_throughput(payload: dict) -> dict[str, tuple[str, object, object]]:
    """The per-model rate each role's primary connection ran with before v38.

    Returns ``role -> (mode, batch_size, concurrency)`` for roles that had a
    saved profile.  The profile key depends on the resolved model (following
    roles included), so this goes through the same resolver the runtime used.
    Best effort: anything that fails just means "nothing was tuned".
    """
    profiles = payload.get("model_throughput_profiles")
    if not isinstance(profiles, dict) or not profiles:
        return {}
    try:
        from core.model_roles import (
            ROLE_IMAGE,
            ROLE_PDF_REVIEW,
            ROLE_TRANSLATION,
            resolve_effective_model_config,
        )
        from core.model_throughput import model_throughput_key

        settings = AppSettings.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 - a migration must never block loading
        logger.debug(f"v38 速率迁移：读取旧速率失败，按没改过处理：{exc!r}")
        return {}
    found: dict[str, tuple[str, object, object]] = {}
    for role in (ROLE_TRANSLATION, ROLE_IMAGE, ROLE_PDF_REVIEW):
        try:
            config = resolve_effective_model_config(settings, role)
        except Exception:  # noqa: BLE001 - an unusable role has nothing to carry
            continue
        profile = profiles.get(model_throughput_key(config))
        if not isinstance(profile, dict):
            continue
        mode = "local" if config.mode == "local" else "cloud"
        found[role] = (mode, profile.get("batch_size"), profile.get("concurrency"))
    return found


def _changed_int(value: object, old_default: int) -> int | None:
    """The stored value if the user changed it, else None."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return None if value == old_default else value


def _pre_v38_tuned_rate(payload: dict) -> list[str]:
    """v37 及以前用户自己改过的速率与重试项（只用于决定升级前要不要备份）。

    word_batch、request_retry 两节里不等于旧默认值的项，文档翻译、PDF 与图片翻译、
    审核三个模型主用连接按模型存的速率里改过的项，以及 pdf 节里填过的同时生成页数 /
    单页重试次数。返回的是「节.项」名，空表示全在旧默认值上。
    """
    tuned: list[str] = []
    request_retry = payload.get("request_retry")
    if isinstance(request_retry, dict):
        for surface in ("excel", "cad"):
            if _changed_int(request_retry.get(surface), _PRE_V38_REQUEST_RETRY_DEFAULT) is not None:
                tuned.append(f"request_retry.{surface}")
    word_batch = payload.get("word_batch")
    if isinstance(word_batch, dict):
        for old_key, (_field, old_default) in _PRE_V38_WORD_BATCH_DEFAULTS.items():
            if _changed_int(word_batch.get(old_key), old_default) is not None:
                tuned.append(f"word_batch.{old_key}")
    for role, (mode, batch_size, concurrency) in _pre_v38_role_throughput(payload).items():
        if role == "translation":
            old = _PRE_V38_TEXT_THROUGHPUT_DEFAULTS[mode]
            pairs = ((batch_size, old["batch_size"]), (concurrency, old["concurrency"]))
        elif role == "image":
            pairs = ((concurrency, _PRE_V38_PAGE_CONCURRENCY_DEFAULTS[mode]),)
        else:
            pairs = ((concurrency, _PRE_V38_REVIEW_CONCURRENCY_DEFAULTS[mode]),)
        if any(_changed_int(value, old_default) is not None for value, old_default in pairs):
            tuned.append(f"model_throughput_profiles.{role}")
    pdf = payload.get("pdf")
    if isinstance(pdf, dict):
        if pdf.get("page_generation_concurrency") not in (None, ""):
            tuned.append("pdf.page_generation_concurrency")
        if _changed_int(pdf.get("page_retry_attempts"), _PRE_V38_PAGE_RETRY_DEFAULT) is not None:
            tuned.append("pdf.page_retry_attempts")
    return tuned


def _migrate_rate_settings(payload: dict, stored_version: int) -> dict:
    """v38: 速率与重试挪到「翻译参数」，按文件类型保存，升级时一律改用新默认值。

    v37 的值是按旧默认值调的（同时请求数上限 20、Word 每批 800 字等），带到新位置
    会让新默认值对调过的人不生效，所以不带（maple 2026-10-14 定）：

    * word_batch、request_retry 两节移除，rate 节为空（各项按云端/本地取默认值）；
    * pdf 节的同时生成页数、单页重试次数、同时审核页数回到默认值；
    * 按模型存的 model_throughput_profiles 保留（清洗记忆库仍按它取速率），文档翻译、
      PDF 与图片翻译、审核三个模型不再读它。

    用户改过的值只留在备份里：第一次写回前 save_settings 按 _has_legacy_rate_data
    先备份旧文件。
    """
    if stored_version >= 38:
        return payload
    migrated = dict(payload)
    migrated.pop("request_retry", None)
    migrated.pop("word_batch", None)
    migrated["rate"] = {}
    if isinstance(migrated.get("pdf"), dict):
        migrated["pdf"] = {
            key: value for key, value in migrated["pdf"].items() if key not in _PDF_RATE_KEYS
        }
    return migrated


def _migrate_translation_types(payload: dict, stored_version: int) -> dict:
    """v40: 侧栏里各翻译类型的顺序和显示改记在 ``translation_types`` 一处（内置四种也能改）。

    v39 只有运行时注册的类型有 ``extensions.<id>.pinned`` / ``order``。调过顺序（有非 0 的
    order）的，按内置四种在前、扩展类型按 order 排在后面写成 ``order``，与 v39 侧栏里看到的
    顺序一致。``pinned`` 不再沿用：v40 起扩展类型默认不显示，要用的在「扩展」页打开
    （v39 没有发布过，「默认显示」只在开发版里出现过）。旧字段原样留着，不再写。
    """
    if stored_version >= 40 or "translation_types" in payload:
        return payload
    extensions = payload.get("extensions")
    entries = [
        (ext_id, entry) for ext_id, entry in (extensions.items() if isinstance(extensions, dict) else ())
        if isinstance(entry, dict)
    ]

    def position(entry: dict) -> int:
        value = entry.get("order")
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    order: list[str] = []
    if any(position(entry) for _ext_id, entry in entries):
        ranked = sorted(enumerate(entries), key=lambda item: (position(item[1][1]), item[0]))
        order = ["excel", "word", "pdf", "cad", *(f"ext:{ext_id}" for _index, (ext_id, _entry) in ranked)]
    migrated = dict(payload)
    migrated["translation_types"] = {"order": order, "visible": {}}
    return migrated


def _has_legacy_rate_data(stored_version: int) -> bool:
    """Whether the v38 upgrade drops a rate value the user had changed.

    A file still on the old defaults loses nothing when everything moves to the
    new defaults, so like the v35 prompt migration it needs no backup.
    """
    if stored_version >= 38:
        return False
    raw = _raw_settings_payload()
    return raw is not None and bool(_pre_v38_tuned_rate(raw))


class SettingsSchemaError(ValueError):
    """The settings file could not be read *and* could not be backed up.

    Recovery is automatic now, so a merely old or malformed file never reaches
    here.  What does is the one case where recovering would destroy something
    unrecoverable: a file this build cannot read but that is still there and
    still, for all we know, the user's whole configuration.  Overwriting it
    without a copy set aside is the one outcome worse than an error message,
    so this is raised instead — and the maintenance page's explicit reset
    overrides it, which is the way out.
    """


# ── 数据恢复记录 ──────────────────────────────────────────
# One small file records the last time Translator had to back a data file up
# and start over.  Only that path needs telling the user about: adopting an
# older settings file or upgrading the TM in place keeps their data, so it
# stays silent.
RECOVERY_PATH = APP_DATA_DIR / "recovery.json"
SETTINGS_RECOVERY_SCOPE = "settings"
TM_RECOVERY_SCOPE = "tm"
KEYS_RECOVERY_SCOPE = "keys"


def read_recovery_record() -> dict[str, dict]:
    """Return the last recovery event per scope; empty when nothing happened."""
    try:
        payload = json.loads(RECOVERY_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning(f"recovery.json 无法读取，按无恢复记录处理：{exc}")
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        str(scope): event
        for scope, event in payload.items()
        if isinstance(event, dict)
    }


def record_recovery_event(
    scope: str,
    *,
    stored_version: int | None,
    current_version: int,
    backup_path: str,
) -> None:
    """Remember one backup-and-rebuild so the UI can point at the backup.

    Best effort on purpose: failing to record the notice must never turn into
    a failure to recover, which is the very dead end this change removes.
    """
    lock_path = RECOVERY_PATH.with_name(f".{RECOVERY_PATH.name}.lock")
    event = {
        "stored_version": stored_version,
        "current_version": int(current_version),
        "backup_path": str(backup_path or ""),
        "recorded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    try:
        with _exclusive_file_lock(lock_path):
            record = read_recovery_record()
            record[str(scope)] = event
            _write_text_atomic(
                RECOVERY_PATH,
                json.dumps(record, indent=2, ensure_ascii=False),
            )
    except Exception as exc:
        logger.warning(f"恢复记录写入失败（不影响已完成的恢复）：{exc}")


def clear_recovery_record(scopes: list[str] | None = None) -> bool:
    """Drop the recovery notice once the user has acknowledged it.

    scopes=None 维持旧语义：整个文件删掉。传了 scopes 就只清这些作用域——
    「知道了」只应该消掉横幅上真正展示过的事件；没露过面的作用域（比如横幅
    已在屏时才惰性写入的 keys 事件）必须留到它露面的那天，不能被顺手抹掉。
    """
    lock_path = RECOVERY_PATH.with_name(f".{RECOVERY_PATH.name}.lock")
    with _exclusive_file_lock(lock_path):
        existed = RECOVERY_PATH.exists()
        if scopes is None:
            RECOVERY_PATH.unlink(missing_ok=True)
            return existed
        if not existed:
            return False
        wanted = {str(scope) for scope in scopes}
        record = read_recovery_record()
        remaining = {
            scope: event for scope, event in record.items() if scope not in wanted
        }
        if len(remaining) == len(record):
            return False
        if remaining:
            _write_text_atomic(
                RECOVERY_PATH,
                json.dumps(remaining, indent=2, ensure_ascii=False),
            )
        else:
            RECOVERY_PATH.unlink(missing_ok=True)
        return True


def _timestamped_backup_name(prefix: str, suffix: str) -> str:
    return f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}{suffix}"


def _backup_settings_file(stem: str = "settings_unusable") -> str:
    """Copy the settings file aside; return the backup path.

    ``stem`` names why: an unusable file, or a good one about to be upgraded.
    """
    target_dir = BACKUPS_DIR / "settings"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / _timestamped_backup_name(stem, ".json")
    # Copy rather than move: a rename that fails halfway would take the only
    # copy of the user's configuration with it.
    shutil.copy2(SETTINGS_PATH, target)
    return str(target)


def _inspect_settings_file() -> tuple[str, int | None, dict | None]:
    """Classify settings.json without changing it.

    ``missing``  no file yet — defaults are correct.
    ``current``  version matches and the content validates.
    ``adopted``  an older version whose content still validates: it is taken
                 over as-is and restamped on the next write.  A version bump
                 on its own is not a reason to refuse the user's data.
    ``unusable`` invalid content, or written by a newer build (a downgrade) —
                 backed up, then replaced.
    ``unreadable`` the file is there but the OS would not hand it over: a
                 permission problem, or Windows AV/backup software holding it
                 open.  Told apart from ``unusable`` on purpose — the content
                 is very likely intact, and it cannot be backed up while it
                 cannot be read, so it must not be overwritten either.
    """
    if not SETTINGS_PATH.exists():
        return "missing", None, None
    try:
        raw_bytes = SETTINGS_PATH.read_bytes()
    except OSError as exc:
        logger.warning(f"settings.json 暂时读不到（不会覆盖，原文件保留）：{exc}")
        return "unreadable", None, None
    try:
        # 解码放在「内容」这一档里：被别的编辑器存成 GBK 之类的文件读得出来、
        # 只是内容不对，应当备份后重建，而不是一路抛 UnicodeDecodeError 让设置页
        # 和维护页一起 500（审计 高-2）。带 BOM 的 UTF-8 照常读。
        payload = json.loads(raw_bytes.decode("utf-8-sig"))
        if not isinstance(payload, dict):
            raise ValueError("settings.json 顶层必须是 JSON 对象")
    except Exception:
        return "unusable", None, None
    stored_version = _extract_settings_version(payload)
    if stored_version > SETTINGS_SCHEMA_VERSION:
        return "unusable", stored_version, payload
    try:
        AppSettings.model_validate(payload)
    except Exception:
        return "unusable", stored_version, payload
    if stored_version < SETTINGS_SCHEMA_VERSION:
        return "adopted", stored_version, _migrate_translation_types(_migrate_rate_settings(
            _migrate_prompt_layers(
                _migrate_smart_domain_default(
                    _migrate_palette_default(_migrate_theme_default(payload, stored_version), stored_version),
                    stored_version,
                ),
                stored_version,
            ),
            stored_version,
        ), stored_version)
    return "current", stored_version, payload


def _recreate_settings_file(
    stored_version: int | None, *, force: bool = False
) -> AppSettings:
    """Back the unusable settings file up and put a working default in place.

    The backup is a precondition, not a courtesy: without it the rebuild is
    indistinguishable from deleting the user's configuration.  If the copy
    cannot be made the rebuild is refused — except under ``force``, which is
    the maintenance page's explicit reset, where discarding the old file is
    the whole point of the button the user pressed.
    """
    backup_path = ""
    try:
        backup_path = _backup_settings_file()
    except OSError as exc:
        if not force:
            logger.warning(f"settings.json 备份失败：{exc}")
            raise SettingsSchemaError(
                f"无法读取设置文件，也未能备份到 {BACKUPS_DIR / 'settings'}。原文件已原样保留；如确定放弃该文件，"
                "请在「设置 → 数据与维护」中点击「重置设置」。"
            ) from exc
        # An explicit reset says the old file is expendable, so a failed
        # backup is not a reason to leave the app unable to save anything.
        logger.warning(f"settings.json 备份失败，按显式重置继续重建：{exc}")
    fresh = AppSettings()
    _write_text_atomic(SETTINGS_PATH, fresh.model_dump_json(indent=2))
    logger.warning(
        "settings.json 无法使用（stored_version={}），已备份到 {} 并重建默认配置。",
        stored_version,
        backup_path or "（备份失败）",
    )
    record_recovery_event(
        SETTINGS_RECOVERY_SCOPE,
        stored_version=stored_version,
        current_version=SETTINGS_SCHEMA_VERSION,
        backup_path=backup_path,
    )
    return fresh


def get_settings_schema_status() -> dict[str, object]:
    """Report what the settings file on disk is, without touching it.

    ``can_write`` answers "can this file be written to as it stands".  It is
    False only for ``unusable`` and ``unreadable``, and for ``unusable`` the
    write still is not refused: the writer backs the file up and rebuilds it
    first.  Only ``unreadable`` can actually turn a save away, because there
    the alternative is overwriting a file we could not copy.
    """
    state, stored_version, _payload = _inspect_settings_file()
    return {
        "state": state,
        "current_version": SETTINGS_SCHEMA_VERSION,
        "stored_version": stored_version,
        "can_write": state not in {"unusable", "unreadable"},
    }


def recover_settings_file_if_needed() -> bool:
    """Back up and rebuild the settings file if it cannot be used as it stands.

    Deliberately separate from ``load_settings``.  Reading is something a
    dozen call sites do, several of them concurrently and one of them from
    inside ``save_settings``'s own lock; letting a *read* rewrite the file made
    the repair race with a save that had just completed, and could leave two
    backups and two notices behind for one broken file.  So the repair lives
    here, always under the lock, and is called at startup and from the health
    check — never as a side effect of loading.
    """
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = SETTINGS_PATH.with_name(f".{SETTINGS_PATH.name}.lock")
    with _exclusive_file_lock(lock_path):
        state, stored_version, _payload = _inspect_settings_file()
        if state not in {"unusable", "unreadable"}:
            return False
        _recreate_settings_file(stored_version)
        return True


def _remember_persisted_snapshot(settings: AppSettings) -> AppSettings:
    """Record what this object looked like when it came off disk.

    ``save_settings`` needs a "before" picture to work out which fields the
    caller actually edited.  Taking it here — once per load — keeps every
    read-modify-write caller unaware of the mechanism.
    """
    settings._persisted_snapshot = settings.model_dump(mode="json")
    return settings


def carry_settings_baseline(source: AppSettings, target: AppSettings) -> AppSettings:
    """Hand one object's load-time snapshot to its replacement.

    Endpoints that rebuild settings through ``model_validate`` (whole-payload
    PUT, model-config import) end up with a fresh object that has no snapshot
    of its own.  Without this the merge in ``save_settings`` degrades to a full
    overwrite for exactly the endpoints most likely to race.
    """
    target._persisted_snapshot = source._persisted_snapshot
    return target


_SETTINGS_FIELD_DELETED = "delete"
_SETTINGS_FIELD_SET = "set"
_SETTINGS_FIELD_MERGE = "merge"


def _settings_delta(baseline: dict, updated: dict) -> dict:
    """Describe what changed between two settings snapshots.

    Entries are tagged rather than stored bare so a nested change can be told
    apart from a whole-value replacement that happens to be a dict.
    """
    delta: dict[str, tuple[str, object]] = {}
    for key, new_value in updated.items():
        if key not in baseline:
            delta[key] = (_SETTINGS_FIELD_SET, new_value)
            continue
        old_value = baseline[key]
        if isinstance(new_value, dict) and isinstance(old_value, dict):
            nested = _settings_delta(old_value, new_value)
            if nested:
                delta[key] = (_SETTINGS_FIELD_MERGE, nested)
        elif old_value != new_value:
            delta[key] = (_SETTINGS_FIELD_SET, new_value)
    for key in baseline:
        if key not in updated:
            delta[key] = (_SETTINGS_FIELD_DELETED, None)
    return delta


def _apply_settings_delta(target: dict, delta: dict) -> dict:
    """Replay one caller's edits on top of the current file contents."""
    merged = dict(target)
    for key, (action, value) in delta.items():
        if action == _SETTINGS_FIELD_DELETED:
            merged.pop(key, None)
        elif action == _SETTINGS_FIELD_MERGE:
            current = merged.get(key)
            merged[key] = _apply_settings_delta(
                current if isinstance(current, dict) else {},
                value,
            )
        else:
            merged[key] = value
    return merged


def _adopt_settings_state(target: AppSettings, source: AppSettings) -> None:
    """Make an in-memory settings object match what was just persisted.

    The endpoint that called ``save_settings`` returns its own object to the
    UI.  After a merge that object is missing whatever a concurrent request
    changed, and echoing that back would invite the UI to write the stale
    value again on its next save.
    """
    target.__dict__.update(source.__dict__)
    target.__pydantic_fields_set__.update(source.__pydantic_fields_set__)


def _merged_settings_payload(settings: AppSettings) -> AppSettings | None:
    """Fold this caller's edits into the settings file as it stands now.

    Returns ``None`` when there is nothing to merge against (no load-time
    snapshot) or when the merged result would not validate, in which case the
    caller falls back to writing its own object wholesale.
    """
    baseline = settings._persisted_snapshot
    if baseline is None:
        return None
    updated = settings.model_dump(mode="json")
    delta = _settings_delta(baseline, updated)
    current = load_settings().model_dump(mode="json")
    merged = _apply_settings_delta(current, delta)
    try:
        return AppSettings.model_validate(merged)
    except Exception as exc:
        # Two independent edits should not be able to make settings
        # unsavable.  Fall back to this caller's own view instead.
        logger.warning(f"设置合并结果未通过校验，已按本次调用的完整内容保存：{exc}")
        return None


def _pin_manual_model_edits(settings: AppSettings) -> None:
    """Stop automation when a saved edit changes a promoted connection."""
    baseline = settings._persisted_snapshot
    if not isinstance(baseline, dict):
        return
    for field_name, role_name in (
        ("engine", "translation"),
        ("cleaner_model_role", "cleaner"),
        ("pdf_review_model_role", "pdf_review"),
    ):
        old_owner = baseline.get(field_name)
        owner = getattr(settings, field_name)
        if not isinstance(old_owner, dict):
            continue
        old_connections = {
            str(conn.get("id") or ""): conn
            for conn in old_owner.get("connections") or []
            if isinstance(conn, dict)
        }
        for index, connection in enumerate(owner.connections):
            state_key = f"{role_name}:{connection.id}"
            state = settings.model_upgrade_states.get(state_key)
            previous = old_connections.get(connection.id)
            if state is None or previous is None or state.status == "manual":
                continue
            changed = any(
                getattr(connection, field) != previous.get(field)
                for field in ("provider", "model", "base_url", "api_mode")
            )
            if index == 0:
                changed = changed or any(
                    getattr(owner, field) != old_owner.get(field)
                    for field in ("cloud_provider", "cloud_model", "cloud_base_url", "api_mode")
                )
            if changed:
                settings.model_upgrade_states[state_key] = ModelUpgradeState(
                    source_model=owner.cloud_model if index == 0 else connection.model,
                    status="manual",
                )
        if role_name == "translation":
            continue
        # A following role uses the source pool's id but owns its model name.
        # Its idle owned pool will not show the edit, so compare the role field.
        if owner.cloud_model != old_owner.get("cloud_model"):
            prefix = f"{role_name}:"
            for state_key in tuple(settings.model_upgrade_states):
                if state_key.startswith(prefix):
                    settings.model_upgrade_states[state_key] = ModelUpgradeState(
                        source_model=owner.cloud_model,
                        status="manual",
                    )


def load_settings() -> AppSettings:
    """Load the settings file, adopting an older one and never writing to it.

    A schema version that has moved on is not by itself a reason to ignore the
    user's configuration: almost every bump is additive, so if the content
    still validates it is taken over unchanged and simply restamped the next
    time anything is saved.

    A file this build cannot use yields defaults *in memory* only.  Repairing
    it is ``recover_settings_file_if_needed``'s job, so that a read can never
    overwrite anything and can never race a concurrent save.
    """
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    state, stored_version, payload = _inspect_settings_file()
    if state in {"unusable", "unreadable"}:
        logger.warning(
            f"settings.json 当前无法使用（{state}，stored_version={stored_version}），"
            "本次按默认设置运行，原文件未改动。"
        )
        settings = AppSettings()
    elif payload is None:
        settings = AppSettings()
    else:
        settings = AppSettings.model_validate(payload)
        if state == "adopted":
            logger.info(
                f"settings.json 为 v{stored_version}，内容与当前 "
                f"v{SETTINGS_SCHEMA_VERSION} 兼容，已直接接管。"
            )
    try:
        _seed_packaged_default_api_key()
    except Exception as seed_exc:
        logger.warning(f"默认 API Key 初始化失败，已保留当前设置：{seed_exc}")
    return _remember_persisted_snapshot(settings)


def save_settings(settings: AppSettings, *, replace_incompatible: bool = False) -> None:
    """Persist one caller's edits without discarding a concurrent writer's.

    Every settings endpoint is a read-modify-write and FastAPI runs the
    synchronous handlers in a thread pool, so two of them overlap whenever the
    user flips two switches quickly.  Writing the whole model back would drop
    whatever the other request changed in between (a lost update: the file
    itself stays valid, the content does not).

    So the recovery check, the re-read and the write all happen inside the
    same cross-process lock, and only the fields that changed since
    ``load_settings`` are replayed on top of the file as it stands at that
    moment.

    A write is never refused over a schema version.  If the file on disk turns
    out to be broken it is backed up and rebuilt here first — an app that
    answers every save with an error and offers no way out is worse than one
    that starts the configuration over and says where the old copy went.  The
    single exception is a file that cannot even be copied, where the rebuild
    would be an unrecoverable loss; ``replace_incompatible`` (the maintenance
    page's reset) overrides that.
    """
    _pin_manual_model_edits(settings)
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = SETTINGS_PATH.with_name(f".{SETTINGS_PATH.name}.lock")
    with _exclusive_file_lock(lock_path):
        state, stored_version, _payload = _inspect_settings_file()
        if state in {"unusable", "unreadable"}:
            _recreate_settings_file(stored_version, force=replace_incompatible)
        elif state == "adopted" and stored_version < SETTINGS_SCHEMA_VERSION:
            # Model upgrades are destructive to the on-disk representation.
            # Keep the exact pre-upgrade file before the first write; subsequent
            # saves see the current schema and do not create repeat backups.
            # v35 的提示词迁移会改写用户自己写的内容（场景改写转成补充要求、
            # 旧字段清空），第一次写回前同样留一份原文件。
            # v38 的速率与重试一律改用新默认值，旧的 word_batch / request_retry
            # 两节随之移除；用户改过速率的，同样先留一份。
            if (
                _has_legacy_rate_data(stored_version)
                or AppSettings._migrate_legacy_image_model(_payload) != _payload
                or (stored_version < 35 and _has_legacy_prompt_data(_raw_prompt_choices(stored_version)))
            ):
                _backup_settings_file(f"settings_v{stored_version}_before_upgrade")
        # An explicit reset deliberately discards whatever is on disk, so it
        # must never be merged with it.
        merged = None if replace_incompatible else _merged_settings_payload(settings)
        if merged is not None:
            _adopt_settings_state(settings, merged)
        settings.settings_version = SETTINGS_SCHEMA_VERSION
        _write_text_atomic(
            SETTINGS_PATH,
            settings.model_dump_json(indent=2),
        )
        _remember_persisted_snapshot(settings)
    logger.debug(f"配置已保存：{SETTINGS_PATH}")


def update_settings_atomically(update: Callable[[AppSettings], bool]) -> bool:
    """Apply a conditional background edit to the latest settings under lock.

    Automatic model checks finish after network I/O.  By then the user may
    have edited a connection, so a saved snapshot cannot be trusted.  The
    callback must recheck its exact target and return False when stale.
    """
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = SETTINGS_PATH.with_name(f".{SETTINGS_PATH.name}.lock")
    with _exclusive_file_lock(lock_path):
        state, stored_version, payload = _inspect_settings_file()
        if state in {"unusable", "unreadable"}:
            return False
        current = AppSettings.model_validate(payload) if payload is not None else AppSettings()
        if not update(current):
            return False
        if state == "adopted" and payload is not None and _has_legacy_rate_data(stored_version):
            # 后台写入也可能是升级后的第一次写：先留一份旧文件（见 save_settings）。
            _backup_settings_file(f"settings_v{stored_version}_before_upgrade")
        current.settings_version = SETTINGS_SCHEMA_VERSION
        _write_text_atomic(SETTINGS_PATH, current.model_dump_json(indent=2))
        return True


def _backup_keys_file() -> str:
    """Copy the unusable keys.json aside; return the backup path.

    Mirrors ``_backup_settings_file``: a copy, never a move, so a failure
    partway through never takes the only copy of the user's keys with it.
    """
    target_dir = BACKUPS_DIR / "keys"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / _timestamped_backup_name("keys_unusable", ".json")
    shutil.copy2(KEYS_PATH, target)
    return str(target)


def _load_keys_unlocked(*, strict: bool, force: bool = False) -> dict[str, str]:
    """Read keys.json, telling "cannot parse" apart from "cannot read".

    Mirrors ``_inspect_settings_file``'s split between ``unusable``（内容损坏，
    可以放心备份后当空表续写）和 ``unreadable``（文件可能完好，只是这一刻打不
    开——Windows 上被杀毒/备份软件占住，或者一次 EACCES/EIO）。后者绝不能走自
    愈：读不出来就复制不出来，复制不出来就不能覆盖，否则「暂时占用」会被写成
    「永久丢失」。``force`` 是维护页「清空全部 Key」的显式放弃——那条路本来就
    要整个删除文件，一次读取失败或备份失败不该拦着它。
    """
    if not KEYS_PATH.exists():
        return {}
    try:
        raw_bytes = KEYS_PATH.read_bytes()
    except OSError as exc:
        if not strict:
            logger.warning(f"keys.json 暂时读取不到：{exc}")
            return {}
        if not force:
            logger.warning(f"keys.json 暂时读取不到，拒绝写入：{exc}")
            raise SettingsSchemaError(
                "暂时无法读取保存 API 密钥的文件（可能被其他程序占用），为避免数据丢失，"
                "未做任何改动；如确定放弃该文件，"
                "请在「设置 → 数据与维护」中点击「删除全部密钥」。"
            ) from exc
        logger.warning(f"keys.json 暂时读取不到，按显式清空继续：{exc}")
        return {}
    try:
        # 同 settings.json：编码不对算内容损坏，走下面的备份后续写（审计 中-6）。
        payload = json.loads(raw_bytes.decode("utf-8-sig"))
        if not isinstance(payload, dict):
            raise ValueError("keys.json 顶层必须是 JSON 对象")
        return payload
    except Exception as exc:
        if not strict:
            logger.warning(f"keys.json 解析失败：{exc}")
            return {}
        # 写入路径（保存/删除一把 Key、清空全部 Key）：内容损坏的文件不能让
        # 「保存」和「清空」两条路都拒绝还不给出路——那正是 clear_tm 对坏 TM
        # 库的处理方式，keys.json 补齐同等待遇。
        if force:
            # force 是「调用方已明确放弃这份文件」（维护页删除全部 Key）。按钮
            # 承诺的是删除，不是搬家：不做备份也不记恢复事件——否则 backups/
            # 里会静默留一份含明文 Key 的副本，且用户亲手清空后还会看到一条
            # 催他重填的警告横幅。审计前的 clear 同样不留副本。
            logger.warning(f"keys.json 无法解析，按显式清空继续，不留备份：{exc}")
            return {}
        # 备份是前置条件，不是客气话：备份做不成就说明这次改动本身就是一次
        # 不可恢复的丢失，必须拒绝。这也保证恢复事件里的 backup_path 永不为空。
        try:
            backup_path = _backup_keys_file()
        except OSError as backup_exc:
            logger.warning(f"keys.json 已损坏且备份失败：{backup_exc}")
            raise SettingsSchemaError(
                "保存 API 密钥的文件已损坏且未能备份，为避免数据丢失，未做任何改动。"
                "如确定放弃该文件，请在「设置 → 数据与维护」中点击「删除全部密钥」。"
            ) from backup_exc
        logger.warning(
            "keys.json 无法解析（{}），已备份到 {} 并按空表继续写入。",
            exc,
            backup_path,
        )
        record_recovery_event(
            KEYS_RECOVERY_SCOPE,
            stored_version=None,
            current_version=1,
            backup_path=backup_path,
        )
        return {}


def load_keys() -> dict[str, str]:
    """Load API keys; return an empty dict if the file is missing."""
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    return _load_keys_unlocked(strict=False)


# ── 密钥来源标记 ──────────────────────────────────────────
# 「导出含 Key」以前会把本机密钥库里的所有密钥一并带走，包括那些本来就是从别人的
# 配置文件导入进来的。于是密钥会连环传播：A 导给 B，B 再导给 C，A 的密钥就到了 C
# 手上，而 A 完全不知情。要挡住这条链，密钥必须能回答「这是谁的」。
#
# 为什么不直接改 keys.json 的格式？它是一张扁平的 {作用域: 密钥字符串} 表，读它的
# 地方遍布引擎、连接池、诊断与维护页。把值换成 {"key": ..., "origin": ...} 这样的
# 对象，等于要求每一个读取方同步改写，还要给老文件写一次就地迁移——而这是密钥文件，
# 迁移写坏的代价是用户所有连接同时失效。
#
# 所以用一份旁路文件，只记「哪些作用域是导入来的」，密钥库本身零改动、零迁移。
# 老用户没有这份文件 → 集合为空 → 所有密钥都算「自己的」→ 导出行为和升级前完全
# 一致，不会有人在升级后突然发现导出的文件里少了东西。
KEY_ORIGINS_FILENAME = "key_origins.json"
KEY_ORIGIN_LOCAL = "local"
KEY_ORIGIN_IMPORTED = "imported"
_KEY_ORIGINS_VERSION = 1


def key_origins_path() -> Path:
    """标记文件的位置：始终跟着 keys.json 走，和它同目录、同权限。

    从 ``KEYS_PATH`` 推导而不是单独定义常量，是为了让任何把密钥库指向别处的调用方
    （测试用的临时目录就是这么做的）自动带上标记文件，不会出现「密钥进了临时目录、
    标记写进了用户真实数据目录」这种串味。
    """
    return KEYS_PATH.with_name(KEY_ORIGINS_FILENAME)


def _keys_lock_path() -> Path:
    """密钥库和它的来源标记共用同一把锁。

    两份文件必须在同一个事务里更新，否则会出现「密钥写进去了、标记没写」的半截
    状态——那正好是最糟的一种：一把导入来的密钥被当成自己的，下次导出照样传出去。
    """
    return KEYS_PATH.with_name(f".{KEYS_PATH.name}.lock")


def _load_imported_key_scopes_unlocked() -> set[str]:
    path = key_origins_path()
    if not path.exists():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        scopes = payload.get("imported_scopes") if isinstance(payload, dict) else None
        if not isinstance(scopes, list):
            raise ValueError("imported_scopes 必须是 JSON 数组")
        return {str(scope).strip() for scope in scopes if str(scope).strip()}
    except Exception as exc:
        # 标记文件坏了不该让保存密钥失败：最坏结果只是把导入来的密钥当成自己的，
        # 也就是退回到没有这份文件时的老行为，而不是让用户存不进 Key。
        logger.warning(f"{KEY_ORIGINS_FILENAME} 解析失败：{exc}")
        return set()


def _write_imported_key_scopes_unlocked(scopes: set[str]) -> None:
    path = key_origins_path()
    if not scopes:
        # 一个都不剩就把文件删掉，回到「老用户没有这份文件」的同一种状态，
        # 免得留下一个空壳还要维护。
        path.unlink(missing_ok=True)
        return
    write_private_text_file(
        path,
        json.dumps(
            {"version": _KEY_ORIGINS_VERSION, "imported_scopes": sorted(scopes)},
            indent=2,
            ensure_ascii=False,
        ),
    )


def _record_key_origin_unlocked(scope: str, origin: str, *, has_key: bool) -> None:
    """在密钥写入的同一个事务里更新来源标记。调用方必须已持有密钥库的锁。"""
    scopes = _load_imported_key_scopes_unlocked()
    if not has_key:
        # 密钥被删了，标记也得跟着走，否则将来同一个作用域上存了新 Key，
        # 会被一条没人指得到的旧标记继续当成「导入来的」而永远导不出去。
        changed = scope in scopes
        scopes.discard(scope)
    elif origin == KEY_ORIGIN_IMPORTED:
        changed = scope not in scopes
        scopes.add(scope)
    else:
        # 用户在面板上自己重新填了一次 Key，这把 Key 就是他自己的了，可以再导出。
        # 这正是「除非 API Key 发生了改变」那一条。
        changed = scope in scopes
        scopes.discard(scope)
    if changed:
        _write_imported_key_scopes_unlocked(scopes)


def load_imported_key_scopes() -> set[str]:
    """返回所有「从别人的配置导入进来」的密钥作用域。"""
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    return _load_imported_key_scopes_unlocked()


def is_imported_connection_key(connection_id: str) -> bool:
    """这条连接自己那把 Key 是不是导入来的。"""
    scope = connection_key_scope(connection_id)
    if not scope:
        return False
    return scope in load_imported_key_scopes()


def is_imported_provider_key(provider: str, base_url: str = "") -> bool:
    """``get_key`` 在这个 provider/Base URL 上会取到的那把 Key 是不是导入来的。

    必须按 ``get_key`` 的同一套回退顺序找到「真正会被取到的那个作用域」再判断：
    只看精确作用域的话，一把存在旧别名作用域下的导入密钥会被当成本机自己的。
    回退到环境变量时返回 False——那是本机自己的环境，不属于别人传过来的配置。
    """
    normalized_provider = _normalize_api_key_provider(provider)
    if not normalized_provider:
        return False
    keys = load_keys()
    imported = load_imported_key_scopes()
    for scope in _api_key_lookup_scopes(normalized_provider, base_url):
        if str(keys.get(scope) or "").strip():
            return scope in imported
    return False


def save_key(
    provider: str,
    api_key: str,
    base_url: str = "",
    *,
    origin: str = KEY_ORIGIN_LOCAL,
) -> None:
    """Save or remove the API key for one provider/Base URL scope.

    ``origin`` 只有导入配置那条路径传 ``imported``；用户自己在面板上填的、测试连接时
    顺手保存的，全部走默认的 ``local``，因而可以随「导出含 Key」传出去。
    """
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    scope = api_key_scope(provider, base_url)
    if not scope:
        return
    with _exclusive_file_lock(_keys_lock_path()):
        keys = _load_keys_unlocked(strict=True)
        if api_key:
            keys[scope] = api_key
        else:
            keys.pop(scope, None)
        write_private_text_file(
            KEYS_PATH,
            json.dumps(keys, indent=2, ensure_ascii=False),
        )
        _record_key_origin_unlocked(scope, origin, has_key=bool(api_key))
    logger.debug(f"API Key 已更新：scope={scope} origin={origin}")


@contextmanager
def provider_key_overrides(overrides: dict[str, str] | None):
    """Temporarily use provider API keys captured by one task snapshot.

    Overrides are thread-local so a task runner can keep using its captured
    credentials without mutating the global key store or affecting other tasks.
    """
    previous = getattr(_KEY_OVERRIDE_LOCAL, "overrides", None)
    normalized = {
        str(provider or "").strip(): str(api_key or "").strip()
        for provider, api_key in (overrides or {}).items()
        if str(provider or "").strip()
    }
    _KEY_OVERRIDE_LOCAL.overrides = normalized
    try:
        yield
    finally:
        if previous is None:
            try:
                delattr(_KEY_OVERRIDE_LOCAL, "overrides")
            except AttributeError:
                pass
        else:
            _KEY_OVERRIDE_LOCAL.overrides = previous


def current_key_overrides() -> dict[str, str] | None:
    """Return the API key overrides active on the calling thread, if any.

    The overrides live in a thread-local, so worker threads never see them.
    Anything that may build engines on another thread has to capture this
    snapshot first and re-enter ``provider_key_overrides`` over there.
    """
    overrides = getattr(_KEY_OVERRIDE_LOCAL, "overrides", None)
    return dict(overrides) if isinstance(overrides, dict) else None


def save_connection_key(
    connection_id: str,
    api_key: str,
    *,
    origin: str = KEY_ORIGIN_LOCAL,
) -> None:
    """Save or remove the API key owned by one pool connection.

    ``origin`` 的含义同 ``save_key``：默认是「本机自己填的」，只有导入配置那条路径
    会传 ``imported``，从而把这把 Key 排除在下一次「导出含 Key」之外。
    """
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    scope = connection_key_scope(connection_id)
    if not scope:
        return
    with _exclusive_file_lock(_keys_lock_path()):
        keys = _load_keys_unlocked(strict=True)
        if api_key:
            keys[scope] = api_key
        else:
            keys.pop(scope, None)
        write_private_text_file(
            KEYS_PATH,
            json.dumps(keys, indent=2, ensure_ascii=False),
        )
        _record_key_origin_unlocked(scope, origin, has_key=bool(api_key))
    logger.debug(f"API Key 已更新：scope={scope} origin={origin}")


def delete_connection_key(connection_id: str) -> None:
    """Delete the API key owned by one pool connection."""
    save_connection_key(connection_id, "")


def get_connection_scoped_key(connection_id: str) -> str:
    """Get only the key stored under one connection's own scope.

    Kept separate from the provider fallback so callers can decide where the
    fallback comes from; ``core.model_roles`` needs it to stay on its own
    ``get_key`` reference.
    """
    scope = connection_key_scope(connection_id)
    if not scope:
        return ""
    overrides = getattr(_KEY_OVERRIDE_LOCAL, "overrides", None)
    if isinstance(overrides, dict):
        value = str(overrides.get(scope) or "").strip()
        if value:
            return value
    return str(load_keys().get(scope) or "").strip()


def get_connection_key(connection_id: str, provider: str, base_url: str = "") -> str:
    """Get one connection's API key, falling back to the provider scope.

    The fallback is what keeps configurations written before pools existed
    working: their single connection has no connection-scoped key yet, so it
    still resolves through provider/Base URL.
    """
    return get_connection_scoped_key(connection_id) or get_key(provider, base_url)


def mask_api_key(api_key: str) -> str:
    """Return a display-only hint for a saved key: first and last few characters.

    An empty API Key field cannot tell "nothing saved" apart from "saved but
    hidden", so the panel needs something to show.  Only the head and tail are
    kept, and the middle is a fixed-width mask so the real length never leaks.
    """
    value = str(api_key or "").strip()
    if not value:
        return ""
    if len(value) <= 12:
        # No head/tail for short keys, and a fixed-width mask so even their
        # exact length stays hidden.
        return "•" * 6
    return f"{value[:4]}{'•' * 6}{value[-4:]}"


def get_key(provider: str, base_url: str = "") -> str:
    """Get the API key for one provider/Base URL scope."""
    normalized_provider = _normalize_api_key_provider(provider)
    overrides = getattr(_KEY_OVERRIDE_LOCAL, "overrides", None)
    lookup_scopes = _api_key_lookup_scopes(normalized_provider, base_url)
    if isinstance(overrides, dict):
        for scope in lookup_scopes:
            value = str(overrides.get(scope) or "").strip()
            if value:
                return value

    keys = load_keys()
    for scope in lookup_scopes:
        value = str(keys.get(scope) or "").strip()
        if value:
            return value

    for env_name in _api_key_env_names(normalized_provider):
        value = str(os.environ.get(env_name) or "").strip()
        if value:
            return value
    return ""


def _api_key_env_names(provider: str) -> tuple[str, ...]:
    normalized_provider = _normalize_api_key_provider(provider)
    if normalized_provider == "openai":
        return ("OPENAI_API_KEY",)
    if normalized_provider == "claude":
        return ("ANTHROPIC_API_KEY", "CLAUDE_API_KEY")
    if normalized_provider == "dashscope":
        return ("DASHSCOPE_API_KEY", "DASHSCOPE_API_KEY_ID")
    if normalized_provider == "zhipu":
        return ("ZHIPUAI_API_KEY", "ZHIPU_API_KEY")
    if normalized_provider == "siliconflow":
        return ("SILICONFLOW_API_KEY",)
    if normalized_provider == "custom_openai":
        return (
            "CUSTOM_OPENAI_API_KEY",
            "OPENAI_COMPATIBLE_API_KEY",
            "TRANSLATOR_API_KEY",
        )
    if normalized_provider == "lanyi":
        return ("LANYI_API_KEY",)
    if normalized_provider == "deepseek":
        return ("DEEPSEEK_API_KEY",)
    if normalized_provider == "xiaomi":
        return ("XIAOMI_API_KEY", "MIMO_API_KEY")
    return ()


def delete_key(provider: str, base_url: str = "") -> None:
    """Delete the API key for one provider/Base URL scope."""
    save_key(provider, "", base_url)


def delete_all_keys() -> int:
    """Delete every locally persisted API key without exposing their values."""
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _exclusive_file_lock(_keys_lock_path()):
        # force=True：这是维护页「删除全部 API Key」按钮本身，用户已经在按下
        # 它的那一刻放弃了旧文件——不该因为读取或备份失败就拒绝执行删除。
        keys = _load_keys_unlocked(strict=True, force=True)
        removed = len(keys)
        KEYS_PATH.unlink(missing_ok=True)
        # 来源标记跟着密钥一起消失，别留下指向不存在密钥的孤儿标记：
        # 留着的话，用户之后在同一个作用域重新填的 Key 会被当成导入来的，导不出去。
        _write_imported_key_scopes_unlocked(set())
    logger.info("已删除全部本地 API Key：count={}", removed)
    return removed
