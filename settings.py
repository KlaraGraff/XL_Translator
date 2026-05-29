"""
User-editable settings persisted to local JSON files.
API keys are stored separately in keys.json with OS-level permissions.
"""
import json
import platform
import stat
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from loguru import logger
from pydantic import BaseModel, Field, model_validator

from config import (
    APP_DATA_DIR,
    BACKUPS_DIR,
    CONCURRENCY_DEFAULT,
    DEFAULT_CLOUD_MODEL,
    DEFAULT_CLOUD_PROVIDER,
    DEFAULT_CUSTOM_OPENAI_API_KEY,
    DEFAULT_CUSTOM_OPENAI_BASE_URL,
    DEFAULT_LOCAL_MODEL_PROVIDER,
    LM_STUDIO_BASE_URL,
    LOCAL_MODEL_PROVIDERS,
    OLLAMA_BASE_URL,
    DEFAULT_MAX_LEN,
    PDF_PAGE_CONCURRENCY_SAFETY_CAP,
    PDF_PAGE_RETRY_ATTEMPTS_DEFAULT,
    PDF_PAGE_RETRY_ATTEMPTS_MAX,
    PDF_PAGE_RETRY_ATTEMPTS_MIN,
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
    is_supported_source_lang,
    is_supported_target_lang,
    normalize_custom_target_langs,
    normalize_recent_target_langs,
    remember_recent_target_lang,
)

_KEY_OVERRIDE_LOCAL = threading.local()
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


def parse_api_key_scope(scope: str) -> tuple[str, str]:
    """Split a persisted credential scope into provider and Base URL."""
    raw_scope = str(scope or "").strip()
    if not raw_scope:
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


class EngineSettings(BaseModel):
    mode: str = "cloud"  # "cloud" | "local"
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

    @model_validator(mode="after")
    def _normalize_highlight_color(self):
        self.highlight_color = _normalize_hex_color(
            self.highlight_color,
            fallback=WORD_REVIEW_HIGHLIGHT_COLOR_DEFAULT,
        )
        return self


class WordConversionSettings(BaseModel):
    prefer_native_word: bool = True


class ModelRoleSettings(BaseModel):
    """Cloud access settings owned by one model role."""

    source_role: str = "independent"
    cloud_provider: str = DEFAULT_CLOUD_PROVIDER
    cloud_model: str = DEFAULT_CLOUD_MODEL
    cloud_base_url: str = DEFAULT_CUSTOM_OPENAI_BASE_URL
    cloud_provider_configs: dict[str, CloudProviderConfig] = Field(default_factory=dict)
    availability_status: str = "unknown"
    availability_message: str = ""
    availability_checked_at: str = ""
    availability_signature: str = ""

    @model_validator(mode="after")
    def _normalize_role(self):
        if self.source_role not in {"independent", "translation", "cleaner", "image"}:
            self.source_role = "independent"
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
        return self


class PdfSettings(BaseModel):
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
    image_translation_enabled: bool = False

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
    ignore_updates: bool = False
    ignored_release_version: str = ""
    last_prompted_major_version: str = ""

    @model_validator(mode="before")
    @classmethod
    def _normalize_update_payload(cls, data):
        if not isinstance(data, dict):
            return data
        migrated = dict(data)
        migrated["ignored_release_version"] = str(
            migrated.get("ignored_release_version") or ""
        ).strip()
        migrated["last_prompted_major_version"] = str(
            migrated.get("last_prompted_major_version") or ""
        ).strip()
        return migrated


class AppSettings(BaseModel):
    engine: EngineSettings = Field(default_factory=EngineSettings)
    tm: TMSettings = Field(default_factory=TMSettings)
    output: OutputSettings = Field(default_factory=OutputSettings)
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
    model_throughput_profiles: dict[str, ModelThroughputSettings] = Field(default_factory=dict)
    update: UpdateSettings = Field(default_factory=UpdateSettings)
    settings_version: int = SETTINGS_SCHEMA_VERSION
    source_lang: str = Field(default_factory=get_default_source_lang)
    target_lang: str = Field(default_factory=get_default_target_lang)
    custom_target_langs: list[CustomTargetLang] = Field(default_factory=list)
    recent_target_langs: list[str] = Field(default_factory=list)
    domain_preset: str = "同步工程场景"
    custom_prompt: str = ""
    last_source_folder: str = ""
    last_excel_source_folder: str = ""
    last_word_source_folder: str = ""
    last_pdf_source_folder: str = ""
    cleaner_mode: str = "diff"  # "diff" | "overwrite"
    cleaner_engine: str = DEFAULT_CLOUD_PROVIDER
    cleaner_model: str = ""
    auto_pin_after_clean: bool = False
    cleaner_prompt_extras: dict[str, str] = Field(default_factory=dict)
    cleaner_full_prompt_overrides: dict[str, str] = Field(default_factory=dict)
    domain_name_overrides: dict[str, str] = Field(default_factory=dict)
    domain_prompt_overrides: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _migrate_model_role_payload(cls, data):
        if not isinstance(data, dict):
            return data
        migrated = dict(data)
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
    def _normalize_target_lang_state(self):
        self.custom_target_langs = normalize_custom_target_langs(self.custom_target_langs)
        default_source_lang = get_default_source_lang()
        default_target_lang = get_default_target_lang()
        self.recent_target_langs = normalize_recent_target_langs(
            self.recent_target_langs,
            self.custom_target_langs,
            include_optional=True,
        )

        if not (
            is_supported_source_lang(self.source_lang)
            or is_supported_target_lang(
                self.source_lang,
                self.custom_target_langs,
                include_optional=True,
            )
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


def _write_text_atomic(path: Path, content: str) -> None:
    """Atomically replace a text file to reduce partial-write risk."""
    temp_path = path.with_name(f"{path.name}.tmp")
    temp_path.write_text(content, encoding="utf-8")
    temp_path.replace(path)


def _extract_settings_version(data: dict) -> int:
    """Read the persisted settings schema version; old files default to 0."""
    raw_version = data.get("settings_version", 0)
    try:
        version = int(raw_version)
    except (TypeError, ValueError):
        return 0
    return max(version, 0)


def _backup_settings_snapshot(raw_text: str, source_version: int | None, reason: str) -> Path:
    """Backup the pre-migration settings.json to a timestamped backup file."""
    backup_dir = BACKUPS_DIR / "settings"
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    version_tag = f"v{source_version}" if source_version is not None else "unknown"
    backup_path = backup_dir / f"settings_{reason}_{version_tag}_{timestamp}.json"
    backup_path.write_text(raw_text, encoding="utf-8")
    return backup_path


def _migrate_settings_to_v1(data: dict) -> dict:
    """Migrate legacy settings payloads to schema version 1."""
    migrated = dict(data)
    migrated.setdefault("recent_target_langs", [])
    migrated["settings_version"] = 1
    return migrated


def _migrate_settings_to_v2(data: dict) -> dict:
    """Migrate settings payloads to schema version 2."""
    migrated = dict(data)
    migrated.setdefault("custom_target_langs", [])
    migrated["settings_version"] = 2
    return migrated


def _migrate_settings_to_v3(data: dict) -> dict:
    """Migrate settings payloads to schema version 3."""
    migrated = dict(data)
    migrated["custom_target_langs"] = [
        entry.model_dump()
        for entry in normalize_custom_target_langs(migrated.get("custom_target_langs"))
    ]
    migrated["settings_version"] = 3
    return migrated


def _migrate_settings_to_v4(data: dict) -> dict:
    """Migrate settings payloads to schema version 4."""
    migrated = dict(data)
    tm_payload = dict(migrated.get("tm") or {})
    tm_payload.pop("short_len", None)
    migrated["tm"] = tm_payload
    migrated["settings_version"] = 4
    return migrated


def _migrate_settings_to_v5(data: dict) -> dict:
    """Migrate settings payloads to schema version 5."""
    migrated = dict(data)
    engine_payload = dict(migrated.get("engine") or {})
    engine_payload.setdefault("concurrency_unlocked", False)
    migrated["engine"] = engine_payload
    migrated["settings_version"] = 5
    return migrated


def _migrate_settings_to_v6(data: dict) -> dict:
    """Migrate settings payloads to schema version 6."""
    migrated = dict(data)
    output_payload = dict(migrated.get("output") or {})
    output_payload.setdefault("formula_display_value_backfill", True)
    migrated["output"] = output_payload
    migrated["settings_version"] = 6
    return migrated


def _migrate_settings_to_v7(data: dict) -> dict:
    """Migrate settings payloads to schema version 7."""
    migrated = dict(data)
    migrated.setdefault("word_batch", WordBatchSettings().model_dump())
    migrated["settings_version"] = 7
    return migrated


def _migrate_settings_to_v8(data: dict) -> dict:
    """Migrate settings payloads to schema version 8."""
    migrated = dict(data)
    word_batch_payload = dict(migrated.get("word_batch") or {})
    word_batch_payload.setdefault(
        "strict_retry_attempts",
        WORD_STRICT_RETRY_ATTEMPTS_DEFAULT,
    )
    migrated["word_batch"] = word_batch_payload
    migrated["settings_version"] = 8
    return migrated


def _migrate_settings_to_v9(data: dict) -> dict:
    """Migrate settings payloads to schema version 9."""
    migrated = dict(data)
    migrated.setdefault("word_review", WordReviewSettings().model_dump())
    migrated["settings_version"] = 9
    return migrated


def _migrate_settings_to_v10(data: dict) -> dict:
    """Migrate settings payloads to schema version 10."""
    migrated = dict(data)
    word_review_payload = dict(migrated.get("word_review") or {})
    word_review_payload["highlight_unresolved"] = WORD_REVIEW_HIGHLIGHT_DEFAULT
    migrated["word_review"] = word_review_payload
    migrated["settings_version"] = 10
    return migrated


def _migrate_settings_to_v11(data: dict) -> dict:
    """Migrate settings payloads to schema version 11."""
    migrated = dict(data)
    migrated.setdefault("word_conversion", WordConversionSettings().model_dump())
    migrated["settings_version"] = 11
    return migrated


def _migrate_settings_to_v12(data: dict) -> dict:
    """Migrate settings payloads to schema version 12."""
    migrated = dict(data)
    engine_payload = dict(migrated.get("engine") or {})
    cleaner_provider = str(
        migrated.get("cleaner_engine")
        or engine_payload.get("cloud_provider")
        or DEFAULT_CLOUD_PROVIDER
    ).strip()
    cleaner_model = str(migrated.get("cleaner_model") or "").strip()
    translation_provider = str(
        engine_payload.get("cloud_provider") or DEFAULT_CLOUD_PROVIDER
    ).strip()
    follows_translation = not cleaner_model and cleaner_provider == translation_provider
    migrated.setdefault(
        "cleaner_model_role",
        ModelRoleSettings(
            source_role="translation" if follows_translation else "independent",
            cloud_provider=cleaner_provider or DEFAULT_CLOUD_PROVIDER,
            cloud_model=cleaner_model
            or str(engine_payload.get("cloud_model") or DEFAULT_CLOUD_MODEL).strip(),
            cloud_base_url=str(
                engine_payload.get("cloud_base_url") or DEFAULT_CUSTOM_OPENAI_BASE_URL
            ).strip(),
        ).model_dump(),
    )
    migrated.setdefault(
        "image_model_role",
        ModelRoleSettings(
            source_role="translation",
            cloud_provider=translation_provider or DEFAULT_CLOUD_PROVIDER,
            cloud_model="",
            cloud_base_url=str(
                engine_payload.get("cloud_base_url") or DEFAULT_CUSTOM_OPENAI_BASE_URL
            ).strip(),
            availability_status="unknown",
        ).model_dump(),
    )
    migrated.setdefault("pdf", PdfSettings().model_dump())
    migrated["settings_version"] = 12
    return migrated


def _migrate_settings_to_v13(data: dict) -> dict:
    """Migrate settings payloads to schema version 13."""
    migrated = dict(data)
    engine_payload = dict(migrated.get("engine") or {})
    translation_provider = str(
        engine_payload.get("cloud_provider") or DEFAULT_CLOUD_PROVIDER
    ).strip()
    migrated.setdefault(
        "pdf_review_model_role",
        ModelRoleSettings(
            source_role="translation",
            cloud_provider=translation_provider or DEFAULT_CLOUD_PROVIDER,
            cloud_model="",
            cloud_base_url=str(
                engine_payload.get("cloud_base_url") or DEFAULT_CUSTOM_OPENAI_BASE_URL
            ).strip(),
            availability_status="unknown",
        ).model_dump(),
    )
    pdf_payload = dict(migrated.get("pdf") or {})
    pdf_payload.setdefault("review_enabled", False)
    migrated["pdf"] = pdf_payload
    migrated["settings_version"] = 13
    return migrated


def _migrate_settings_to_v14(data: dict) -> dict:
    """Migrate settings payloads to schema version 14."""
    migrated = dict(data)
    engine_payload = dict(migrated.get("engine") or {})
    local_provider = _normalize_local_provider(engine_payload.get("local_provider"))
    engine_payload.setdefault("local_provider", local_provider)
    engine_payload.setdefault(
        "local_model",
        str(engine_payload.get("ollama_model") or "").strip(),
    )
    engine_payload.setdefault("local_base_url", _default_local_base_url(local_provider))
    engine_payload.setdefault("ollama_model", engine_payload.get("local_model", ""))
    migrated["engine"] = engine_payload
    migrated["settings_version"] = 14
    return migrated


def _seed_provider_config_payload(payload: dict) -> dict:
    seeded = dict(payload or {})
    provider = str(seeded.get("cloud_provider") or DEFAULT_CLOUD_PROVIDER).strip()
    configs = dict(seeded.get("cloud_provider_configs") or {})
    if provider and provider not in configs:
        configs[provider] = {
            "cloud_model": str(seeded.get("cloud_model") or "").strip(),
            "cloud_base_url": normalize_cloud_base_url(
                provider,
                str(seeded.get("cloud_base_url") or "").strip(),
            ),
        }
    seeded["cloud_provider_configs"] = configs
    return seeded


def _migrate_settings_to_v15(data: dict) -> dict:
    """Migrate settings payloads to schema version 15."""
    migrated = dict(data)
    migrated["engine"] = _seed_provider_config_payload(dict(migrated.get("engine") or {}))
    for key in (
        "cleaner_model_role",
        "image_model_role",
        "pdf_review_model_role",
    ):
        if key in migrated:
            migrated[key] = _seed_provider_config_payload(dict(migrated.get(key) or {}))
    migrated["settings_version"] = 15
    return migrated


def _migrate_settings_to_v16(data: dict) -> dict:
    """Migrate settings payloads to schema version 16."""
    migrated = dict(data)
    migrated.setdefault("update", UpdateSettings().model_dump())
    migrated["settings_version"] = 16
    return migrated


def _migrate_settings_to_v17(data: dict) -> dict:
    """Split the legacy source path into page-specific source histories."""
    migrated = dict(data)
    legacy_source = str(migrated.get("last_source_folder") or "").strip().strip('"')
    source_keys = {
        "excel": "last_excel_source_folder",
        "word": "last_word_source_folder",
        "pdf": "last_pdf_source_folder",
    }
    for key in source_keys.values():
        migrated[key] = str(migrated.get(key) or "").strip().strip('"')

    if legacy_source:
        suffix = Path(legacy_source).suffix.lower()
        if suffix in {".xlsx", ".xls"}:
            target_keys = [source_keys["excel"]]
        elif suffix in {".docx", ".doc"}:
            target_keys = [source_keys["word"]]
        elif suffix == ".pdf":
            target_keys = [source_keys["pdf"]]
        else:
            target_keys = list(source_keys.values())

        for key in target_keys:
            if not str(migrated.get(key) or "").strip():
                migrated[key] = legacy_source

    migrated["settings_version"] = 17
    return migrated


def _migrate_settings_to_v18(data: dict) -> dict:
    """Add per-model throughput profiles."""
    migrated = dict(data)
    migrated.setdefault("model_throughput_profiles", {})
    migrated["settings_version"] = 18
    return migrated


def _migrate_settings_payload(data: dict, source_version: int) -> dict:
    """Apply sequential settings schema migrations until the latest version."""
    migrated = dict(data)
    current_version = max(source_version, 0)

    while current_version < SETTINGS_SCHEMA_VERSION:
        next_version = current_version + 1
        if next_version == 1:
            migrated = _migrate_settings_to_v1(migrated)
        elif next_version == 2:
            migrated = _migrate_settings_to_v2(migrated)
        elif next_version == 3:
            migrated = _migrate_settings_to_v3(migrated)
        elif next_version == 4:
            migrated = _migrate_settings_to_v4(migrated)
        elif next_version == 5:
            migrated = _migrate_settings_to_v5(migrated)
        elif next_version == 6:
            migrated = _migrate_settings_to_v6(migrated)
        elif next_version == 7:
            migrated = _migrate_settings_to_v7(migrated)
        elif next_version == 8:
            migrated = _migrate_settings_to_v8(migrated)
        elif next_version == 9:
            migrated = _migrate_settings_to_v9(migrated)
        elif next_version == 10:
            migrated = _migrate_settings_to_v10(migrated)
        elif next_version == 11:
            migrated = _migrate_settings_to_v11(migrated)
        elif next_version == 12:
            migrated = _migrate_settings_to_v12(migrated)
        elif next_version == 13:
            migrated = _migrate_settings_to_v13(migrated)
        elif next_version == 14:
            migrated = _migrate_settings_to_v14(migrated)
        elif next_version == 15:
            migrated = _migrate_settings_to_v15(migrated)
        elif next_version == 16:
            migrated = _migrate_settings_to_v16(migrated)
        elif next_version == 17:
            migrated = _migrate_settings_to_v17(migrated)
        elif next_version == 18:
            migrated = _migrate_settings_to_v18(migrated)
        else:
            raise ValueError(f"未实现的 settings 迁移版本：v{current_version} -> v{next_version}")
        current_version = next_version

    migrated["settings_version"] = SETTINGS_SCHEMA_VERSION
    return migrated


def load_settings() -> AppSettings:
    """Load settings; fall back to defaults if the file is missing or broken."""
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    if SETTINGS_PATH.exists():
        raw_text = ""
        try:
            raw_text = SETTINGS_PATH.read_text(encoding="utf-8")
            data = json.loads(raw_text)
            if not isinstance(data, dict):
                raise ValueError("settings.json 顶层必须是 JSON 对象")

            source_version = _extract_settings_version(data)
            if source_version < SETTINGS_SCHEMA_VERSION:
                backup_path = _backup_settings_snapshot(
                    raw_text,
                    source_version=source_version,
                    reason=f"upgrade_to_v{SETTINGS_SCHEMA_VERSION}",
                )
                data = _migrate_settings_payload(data, source_version)
                settings = AppSettings.model_validate(data)
                save_settings(settings)
                logger.info(
                    "检测到旧版 settings，已完成迁移并备份："
                    f"v{source_version} -> v{SETTINGS_SCHEMA_VERSION} | backup={backup_path}"
                )
                _seed_packaged_default_api_key()
                return settings

            settings = AppSettings.model_validate(data)
            if settings.model_dump() != data:
                save_settings(settings)
                logger.info("检测到 settings 内容已归一化，已自动回写最新格式")
            _seed_packaged_default_api_key()
            return settings
        except Exception as exc:
            if raw_text:
                try:
                    backup_path = _backup_settings_snapshot(
                        raw_text,
                        source_version=None,
                        reason="load_failed",
                    )
                    logger.warning(f"配置文件解析失败，已备份旧文件：{backup_path}")
                except Exception as backup_exc:
                    logger.warning(f"配置文件解析失败，且备份旧文件失败：{backup_exc}")
            logger.warning(f"配置文件解析失败，回退至默认配置：{exc}")
    settings = AppSettings()
    _seed_packaged_default_api_key()
    return settings


def save_settings(settings: AppSettings) -> None:
    """Persist settings to local JSON."""
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    settings.settings_version = SETTINGS_SCHEMA_VERSION
    _write_text_atomic(
        SETTINGS_PATH,
        settings.model_dump_json(indent=2),
    )
    logger.debug(f"配置已保存：{SETTINGS_PATH}")


def _apply_key_file_permissions(path: Path) -> None:
    """Restrict keys.json permissions to the current user when possible."""
    system = platform.system()
    try:
        if system in ("Darwin", "Linux"):
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except Exception as exc:
        logger.warning(f"无法锁定 keys.json 权限：{exc}（可继续运行，但建议手动检查）")


def load_keys() -> dict[str, str]:
    """Load API keys; return an empty dict if the file is missing."""
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    if KEYS_PATH.exists():
        try:
            return json.loads(KEYS_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(f"keys.json 解析失败：{exc}")
    return {}


def save_key(provider: str, api_key: str, base_url: str = "") -> None:
    """Save or remove the API key for one provider/Base URL scope."""
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    scope = api_key_scope(provider, base_url)
    if not scope:
        return
    keys = load_keys()
    if api_key:
        keys[scope] = api_key
    else:
        keys.pop(scope, None)
    KEYS_PATH.write_text(json.dumps(keys, indent=2, ensure_ascii=False), encoding="utf-8")
    _apply_key_file_permissions(KEYS_PATH)
    logger.debug(f"API Key 已更新：scope={scope}")


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
    return ""


def delete_key(provider: str, base_url: str = "") -> None:
    """Delete the API key for one provider/Base URL scope."""
    save_key(provider, "", base_url)
