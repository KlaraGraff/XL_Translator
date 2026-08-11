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
from datetime import datetime
from pathlib import Path

from loguru import logger
from pydantic import BaseModel, Field, PrivateAttr, model_validator

from config import (
    APP_DATA_DIR,
    BACKUPS_DIR,
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
    SETTINGS_SCHEMA_VERSION,
    SETTINGS_PATH,
    WORD_BATCH_CHARS_DEFAULT,
    WORD_BATCH_CHARS_MAX,
    WORD_BATCH_CHARS_MIN,
    WORD_BATCH_PARAGRAPHS_DEFAULT,
    WORD_BATCH_PARAGRAPHS_MAX,
    WORD_BATCH_PARAGRAPHS_MIN,
    WORD_BATCH_SPLIT_CHARS_DEFAULT,
    WORD_BATCH_SPLIT_CHARS_MAX,
    WORD_BATCH_SPLIT_CHARS_MIN,
    WORD_REVIEW_HIGHLIGHT_COLOR_DEFAULT,
    WORD_REVIEW_HIGHLIGHT_DEFAULT,
    WORD_STRICT_RETRY_ATTEMPTS_DEFAULT,
    WORD_STRICT_RETRY_ATTEMPTS_MAX,
    WORD_STRICT_RETRY_ATTEMPTS_MIN,
    normalize_cloud_base_url,
)

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


def new_connection_id() -> str:
    """Return a stable id for one pool connection."""
    return uuid.uuid4().hex


class ModelConnection(BaseModel):
    """One cloud connection inside a model role's pool.

    A pool is an ordered list: entry 0 is the primary and is kept mirrored
    onto the role's legacy single-connection fields, so configuration written
    by this version stays readable by the previous one.
    """

    id: str = Field(default_factory=new_connection_id)
    label: str = ""
    provider: str = DEFAULT_CLOUD_PROVIDER
    model: str = ""
    base_url: str = ""
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
        self.base_url = str(self.base_url or "").strip().rstrip("/")
        if self.availability_status not in {"unknown", "available", "unavailable"}:
            self.availability_status = "unknown"
        return self

    @property
    def display_label(self) -> str:
        if self.label:
            return self.label
        return self.base_url or self.provider


_SEEDED_CONNECTION_PREFIX = "seed-"


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
                provider=owner.cloud_provider,
                model=owner.cloud_model,
                base_url=owner.cloud_base_url,
                availability_status=owner.availability_status,
                availability_message=owner.availability_message,
                availability_checked_at=owner.availability_checked_at,
                availability_signature=owner.availability_signature,
            )
        ]
    else:
        primary = connections[0]
        primary.provider = owner.cloud_provider
        primary.model = owner.cloud_model
        primary.base_url = owner.cloud_base_url
        primary.availability_status = owner.availability_status
        primary.availability_message = owner.availability_message
        primary.availability_checked_at = owner.availability_checked_at
        primary.availability_signature = owner.availability_signature

    seen: set[str] = set()
    for conn in connections:
        if conn.id in seen:
            conn.id = new_connection_id()
        seen.add(conn.id)
    owner.connections = connections


class CloudProviderConfig(BaseModel):
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
            cloud_model=config.cloud_model,
            cloud_base_url=normalize_cloud_base_url(provider, config.cloud_base_url),
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
            cloud_model=str(getattr(owner, "cloud_model", "") or "").strip(),
            cloud_base_url=normalize_cloud_base_url(
                provider_name,
                str(getattr(owner, "cloud_base_url", "") or "").strip(),
            ),
        )
    return CloudProviderConfig(
        cloud_model="",
        cloud_base_url=normalize_cloud_base_url(provider_name, ""),
    )


def set_cloud_provider_config(
    owner,
    provider: str,
    *,
    cloud_model: str | None = None,
    cloud_base_url: str | None = None,
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
        cloud_model=model,
        cloud_base_url=normalize_cloud_base_url(provider_name, base_url_raw),
    )
    owner.cloud_provider_configs = _normalize_provider_configs(
        getattr(owner, "cloud_provider_configs", {}),
    )
    owner.cloud_provider_configs[provider_name] = config
    if provider_name == str(getattr(owner, "cloud_provider", "") or "").strip():
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
            cloud_base_url=normalize_cloud_base_url(provider_name, ""),
        )
    owner.cloud_provider = provider_name
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


class EngineSettings(BaseModel):
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
        le=get_concurrency_cap(),
    )
    ollama_concurrency: int = Field(
        default=get_default_concurrency("local"),
        ge=1,
        le=get_concurrency_cap(),
    )
    concurrency_unlocked: bool = False
    batch_size: int = Field(default=20, ge=5, le=30)
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
        self.cloud_base_url = normalize_cloud_base_url(
            self.cloud_provider,
            self.cloud_base_url,
        )
        self.cloud_provider_configs = _normalize_provider_configs(self.cloud_provider_configs)
        if self.cloud_model or self.cloud_base_url:
            existing = self.cloud_provider_configs.get(self.cloud_provider)
            if existing is None or not (existing.cloud_model or existing.cloud_base_url):
                self.cloud_provider_configs[self.cloud_provider] = CloudProviderConfig(
                    cloud_model=self.cloud_model,
                    cloud_base_url=self.cloud_base_url,
                )
            else:
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
    max_len: int = Field(default=DEFAULT_MAX_LEN, ge=1, le=200)


class OutputSettings(BaseModel):
    keep_original_sheets: bool = True
    formula_display_value_backfill: bool = True
    enable_print_guard: bool = False
    use_custom_output_dir: bool = False
    custom_output_dir: str = ""
    enable_excel_autofit: bool = False
    lock_row_height: bool = False
    enable_task_log: bool = False


class ExcelOutputSettings(BaseModel):
    """Settings owned solely by the Excel translation surface.

    The previous ``output`` object remains for non-Excel callers while new
    Excel tasks always freeze this object.  This avoids a Word/PDF edit
    changing a future Excel task (E4B-01/E4B-11).
    """

    keep_original_sheets: bool = True
    formula_display_value_backfill: bool = True
    use_custom_output_dir: bool = False
    custom_output_dir: str = ""
    enable_excel_autofit: bool = False
    lock_row_height: bool = False


class WordOutputSettings(BaseModel):
    """Output settings owned solely by the Word translation surface.

    Word always writes a new bilingual ``.docx`` into a task-unique output
    directory.  Keeping its output-root choice separate from the legacy
    shared output object and the Excel page prevents a future edit on either
    page from changing an already-configured Word task.
    """

    use_custom_output_dir: bool = False
    custom_output_dir: str = ""


class PdfOutputSettings(BaseModel):
    """Output and evidence retention owned only by PDF/image translation.

    PDF/image tasks generate a package of page evidence in addition to final
    files.  They must not inherit Excel/Word's output root or mutate it.
    """

    use_custom_output_dir: bool = False
    custom_output_dir: str = ""
    retain_page_materials: bool = True


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
        ge=WORD_BATCH_PARAGRAPHS_MIN,
        le=WORD_BATCH_PARAGRAPHS_MAX,
    )
    max_chars_per_batch: int = Field(
        default=WORD_BATCH_CHARS_DEFAULT,
        ge=WORD_BATCH_CHARS_MIN,
        le=WORD_BATCH_CHARS_MAX,
    )
    split_paragraph_chars: int = Field(
        default=WORD_BATCH_SPLIT_CHARS_DEFAULT,
        ge=WORD_BATCH_SPLIT_CHARS_MIN,
        le=WORD_BATCH_SPLIT_CHARS_MAX,
    )
    strict_retry_attempts: int = Field(
        default=WORD_STRICT_RETRY_ATTEMPTS_DEFAULT,
        ge=WORD_STRICT_RETRY_ATTEMPTS_MIN,
        le=WORD_STRICT_RETRY_ATTEMPTS_MAX,
    )

    @model_validator(mode="after")
    def _normalize_thresholds(self):
        self.split_paragraph_chars = max(
            self.max_chars_per_batch,
            self.split_paragraph_chars,
        )
        return self


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


class ModelRoleSettings(BaseModel):
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
        self.cloud_base_url = normalize_cloud_base_url(
            self.cloud_provider,
            self.cloud_base_url,
        )
        self.cloud_provider_configs = _normalize_provider_configs(self.cloud_provider_configs)
        if self.cloud_model or self.cloud_base_url:
            existing = self.cloud_provider_configs.get(self.cloud_provider)
            if existing is None or not (existing.cloud_model or existing.cloud_base_url):
                self.cloud_provider_configs[self.cloud_provider] = CloudProviderConfig(
                    cloud_model=self.cloud_model,
                    cloud_base_url=self.cloud_base_url,
                )
            else:
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
        le=PDF_PAGE_CONCURRENCY_SAFETY_CAP,
    )
    review_enabled: bool = False
    generate_compressed_pdf: bool = True
    # This controls independent image *inputs* only.  It never changes the
    # visual translation protocol for pages contained in a PDF.
    include_images: bool = False
    # 「跳过大幅面页」：工程 PDF 里 A3 及以上的大幅面页（多为 CAD 图纸）不送
    # 翻译模型，直接从源 PDF 矢量原样导入输出——这类页经不起栅格化，且大多数
    # 也不需要翻译。判定规则见 core.pdf_image_translation.is_oversized_page。
    skip_oversized_pages: bool = False

    @model_validator(mode="before")
    @classmethod
    def _normalize_blankable_concurrency(cls, data):
        if not isinstance(data, dict):
            return data
        migrated = dict(data)
        raw = migrated.get("page_generation_concurrency")
        if raw in ("", None):
            migrated["page_generation_concurrency"] = None
        return migrated


class ModelThroughputSettings(BaseModel):
    """Per effective model tuning values."""

    batch_size: int | None = None
    concurrency: int | None = None

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


class AppearanceSettings(BaseModel):
    """Persisted Tauri shell preferences shared across desktop launches."""

    theme: str = "system"
    model_config_panel_open: bool = False

    @model_validator(mode="after")
    def _normalize_theme(self):
        if self.theme not in {"system", "light", "dark"}:
            self.theme = "system"
        return self


class AppSettings(BaseModel):
    engine: EngineSettings = Field(default_factory=EngineSettings)
    tm: TMSettings = Field(default_factory=TMSettings)
    output: OutputSettings = Field(default_factory=OutputSettings)
    excel_output: ExcelOutputSettings = Field(default_factory=ExcelOutputSettings)
    word_output: WordOutputSettings = Field(default_factory=WordOutputSettings)
    pdf_output: PdfOutputSettings = Field(default_factory=PdfOutputSettings)
    excel_review: ExcelReviewSettings = Field(default_factory=ExcelReviewSettings)
    word_batch: WordBatchSettings = Field(default_factory=WordBatchSettings)
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
    update: UpdateSettings = Field(default_factory=UpdateSettings)
    onboarding: OnboardingSettings = Field(default_factory=OnboardingSettings)
    appearance: AppearanceSettings = Field(default_factory=AppearanceSettings)
    settings_version: int = SETTINGS_SCHEMA_VERSION
    source_lang: str = Field(default_factory=get_default_source_lang)
    target_lang: str = Field(default_factory=get_default_target_lang)
    excel_source_lang: str = "auto"
    word_source_lang: str = "auto"
    excel_target_lang: str = Field(default_factory=get_default_target_lang)
    word_target_lang: str = Field(default_factory=get_default_target_lang)
    tm_source_lang: str = "zh"
    tm_target_lang: str = Field(default_factory=get_default_target_lang)
    recent_tm_lang_pairs: list[str] = Field(default_factory=list)
    custom_target_langs: list[CustomTargetLang] = Field(default_factory=list)
    recent_target_langs: list[str] = Field(default_factory=list)
    domain_preset: str = "同步工程场景"
    custom_prompt: str = ""
    # Excel and Word intentionally own separate domain/prompt state.  The
    # legacy global fields remain as an inert compatibility surface for CLI
    # callers; page-aware task code reads the fields below.
    excel_domain_preset: str = "同步工程场景"
    excel_custom_prompt: str = ""
    excel_domain_name_overrides: dict[str, str] = Field(default_factory=dict)
    excel_domain_prompt_overrides: dict[str, str] = Field(default_factory=dict)
    word_domain_preset: str = "同步工程场景"
    word_custom_prompt: str = ""
    word_domain_name_overrides: dict[str, str] = Field(default_factory=dict)
    word_domain_prompt_overrides: dict[str, str] = Field(default_factory=dict)
    last_source_folder: str = ""
    last_excel_source_folder: str = ""
    last_word_source_folder: str = ""
    last_pdf_source_folder: str = ""
    cleaner_mode: str = "diff"  # 清洗始终先生成建议，确认后才写入
    cleaner_engine: str = DEFAULT_CLOUD_PROVIDER
    cleaner_model: str = ""
    auto_pin_after_clean: bool = False
    cleaner_prompt_extras: dict[str, str] = Field(default_factory=dict)
    cleaner_full_prompt_overrides: dict[str, str] = Field(default_factory=dict)
    domain_name_overrides: dict[str, str] = Field(default_factory=dict)
    domain_prompt_overrides: dict[str, str] = Field(default_factory=dict)

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
        if "custom_prompt" in migrated:
            migrated.setdefault("excel_custom_prompt", migrated.get("custom_prompt"))
            migrated.setdefault("word_custom_prompt", migrated.get("custom_prompt"))
        if "domain_name_overrides" in migrated:
            migrated.setdefault("excel_domain_name_overrides", migrated.get("domain_name_overrides"))
            migrated.setdefault("word_domain_name_overrides", migrated.get("domain_name_overrides"))
        if "domain_prompt_overrides" in migrated:
            migrated.setdefault("excel_domain_prompt_overrides", migrated.get("domain_prompt_overrides"))
            migrated.setdefault("word_domain_prompt_overrides", migrated.get("domain_prompt_overrides"))
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
                "cloud_model": "",
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
                "cloud_model": "",
                "cloud_base_url": str(
                    engine_payload.get("cloud_base_url") or DEFAULT_CUSTOM_OPENAI_BASE_URL
                ).strip(),
                "availability_status": "unknown",
            },
        )
        migrated.setdefault("pdf", PdfSettings().model_dump())
        migrated.setdefault("model_throughput_profiles", {})
        migrated.setdefault("update", UpdateSettings().model_dump())
        return migrated

    @model_validator(mode="before")
    @classmethod
    def _migrate_custom_target_lang_payload(cls, data):
        if not isinstance(data, dict):
            return data
        migrated = dict(data)
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

        for field_name in ("excel_source_lang", "word_source_lang"):
            value = getattr(self, field_name, "auto")
            if is_auto_source_lang(value):
                setattr(self, field_name, "auto")
            else:
                resolved = resolve_language_code(value, source_supported_map)
                setattr(self, field_name, resolved or default_source_lang)

        for field_name in ("excel_target_lang", "word_target_lang"):
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


def clear_recovery_record() -> bool:
    """Drop the recovery notice once the user has acknowledged it."""
    lock_path = RECOVERY_PATH.with_name(f".{RECOVERY_PATH.name}.lock")
    with _exclusive_file_lock(lock_path):
        existed = RECOVERY_PATH.exists()
        RECOVERY_PATH.unlink(missing_ok=True)
    return existed


def _timestamped_backup_name(prefix: str, suffix: str) -> str:
    return f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}{suffix}"


def _backup_settings_file() -> str:
    """Copy the unusable settings file aside; return the backup path."""
    target_dir = BACKUPS_DIR / "settings"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / _timestamped_backup_name("settings_unusable", ".json")
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
        raw = SETTINGS_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning(f"settings.json 暂时读不到（不会覆盖，原文件保留）：{exc}")
        return "unreadable", None, None
    try:
        payload = json.loads(raw)
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
        return "adopted", stored_version, payload
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
            raise SettingsSchemaError(
                f"settings.json 无法读取，也无法备份到 {BACKUPS_DIR / 'settings'}"
                f"（{exc}）。原文件已原样保留；如确定要放弃它，请在维护页执行"
                "「重置设置」。"
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
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = SETTINGS_PATH.with_name(f".{SETTINGS_PATH.name}.lock")
    with _exclusive_file_lock(lock_path):
        state, stored_version, _payload = _inspect_settings_file()
        if state in {"unusable", "unreadable"}:
            _recreate_settings_file(stored_version, force=replace_incompatible)
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


def _load_keys_unlocked(*, strict: bool) -> dict[str, str]:
    if not KEYS_PATH.exists():
        return {}
    try:
        payload = json.loads(KEYS_PATH.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("keys.json 顶层必须是 JSON 对象")
        return payload
    except Exception as exc:
        if strict:
            raise ValueError(f"keys.json 无法安全更新：{exc}") from exc
        logger.warning(f"keys.json 解析失败：{exc}")
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
    return ()


def delete_key(provider: str, base_url: str = "") -> None:
    """Delete the API key for one provider/Base URL scope."""
    save_key(provider, "", base_url)


def delete_all_keys() -> int:
    """Delete every locally persisted API key without exposing their values."""
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _exclusive_file_lock(_keys_lock_path()):
        keys = _load_keys_unlocked(strict=True)
        removed = len(keys)
        KEYS_PATH.unlink(missing_ok=True)
        # 来源标记跟着密钥一起消失，别留下指向不存在密钥的孤儿标记：
        # 留着的话，用户之后在同一个作用域重新填的 Key 会被当成导入来的，导不出去。
        _write_imported_key_scopes_unlocked(set())
    logger.info("已删除全部本地 API Key：count={}", removed)
    return removed
