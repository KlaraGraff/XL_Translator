"""
User-editable settings persisted to local JSON files.
API keys are stored separately in keys.json with OS-level permissions.
"""
import json
import os
import stat
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

from loguru import logger
from pydantic import BaseModel, Field, model_validator

from config import (
    APP_DATA_DIR,
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
    availability_status: str = "unknown"
    availability_message: str = ""
    availability_checked_at: str = ""
    availability_signature: str = ""

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
        if self.availability_status not in {"unknown", "available", "unavailable"}:
            self.availability_status = "unknown"
        self.availability_message = str(self.availability_message or "").strip()
        self.availability_checked_at = str(self.availability_checked_at or "").strip()
        self.availability_signature = str(self.availability_signature or "").strip()
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
        if file_mode is not None and os.name != "nt":
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
    """The persisted new-baseline settings cannot be safely opened or changed."""


def get_settings_schema_status() -> dict[str, object]:
    """Inspect the current settings file without repairing or rewriting it.

    Version 26 is the v8 new-data baseline.  This function intentionally does
    not read legacy directories and does not apply any historical migration.
    A later release can add an explicit forward migration and backup step here.
    """
    if not SETTINGS_PATH.exists():
        return {
            "state": "missing",
            "current_version": SETTINGS_SCHEMA_VERSION,
            "stored_version": None,
            "can_write": True,
        }
    try:
        payload = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("settings.json 顶层必须是 JSON 对象")
        stored_version = _extract_settings_version(payload)
    except Exception:
        return {
            "state": "invalid",
            "current_version": SETTINGS_SCHEMA_VERSION,
            "stored_version": None,
            "can_write": False,
        }
    if stored_version > SETTINGS_SCHEMA_VERSION:
        return {
            "state": "future",
            "current_version": SETTINGS_SCHEMA_VERSION,
            "stored_version": stored_version,
            "can_write": False,
        }
    if stored_version < SETTINGS_SCHEMA_VERSION:
        return {
            "state": "incompatible",
            "current_version": SETTINGS_SCHEMA_VERSION,
            "stored_version": stored_version,
            "can_write": False,
        }
    try:
        AppSettings.model_validate(payload)
    except Exception:
        return {
            "state": "invalid",
            "current_version": SETTINGS_SCHEMA_VERSION,
            "stored_version": stored_version,
            "can_write": False,
        }
    return {
        "state": "current",
        "current_version": SETTINGS_SCHEMA_VERSION,
        "stored_version": stored_version,
        "can_write": True,
    }


def load_settings() -> AppSettings:
    """Load only the current baseline; preserve incompatible data untouched."""
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    if SETTINGS_PATH.exists():
        try:
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("settings.json 顶层必须是 JSON 对象")
            source_version = _extract_settings_version(data)
            if source_version != SETTINGS_SCHEMA_VERSION:
                raise SettingsSchemaError(
                    f"settings schema v{source_version} 与当前 v{SETTINGS_SCHEMA_VERSION} 不兼容"
                )
            settings = AppSettings.model_validate(data)
        except Exception as exc:
            logger.warning(
                "settings 未被读取或修复；保留原文件并以临时默认值启动：%s",
                type(exc).__name__,
            )
            return AppSettings()
        else:
            try:
                _seed_packaged_default_api_key()
            except Exception as seed_exc:
                logger.warning(f"默认 API Key 初始化失败，已保留当前设置：{seed_exc}")
            return settings
    settings = AppSettings()
    try:
        _seed_packaged_default_api_key()
    except Exception as seed_exc:
        logger.warning(f"默认 API Key 初始化失败，已使用默认设置：{seed_exc}")
    return settings


def save_settings(settings: AppSettings, *, replace_incompatible: bool = False) -> None:
    """Persist current-baseline settings without overwriting unsafe data by default."""
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    status = get_settings_schema_status()
    if not bool(status["can_write"]) and not replace_incompatible:
        raise SettingsSchemaError(
            "settings.json 不是当前可写 schema；请在维护页明确重置设置。"
        )
    settings.settings_version = SETTINGS_SCHEMA_VERSION
    lock_path = SETTINGS_PATH.with_name(f".{SETTINGS_PATH.name}.lock")
    with _exclusive_file_lock(lock_path):
        _write_text_atomic(
            SETTINGS_PATH,
            settings.model_dump_json(indent=2),
        )
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


def save_key(provider: str, api_key: str, base_url: str = "") -> None:
    """Save or remove the API key for one provider/Base URL scope."""
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    scope = api_key_scope(provider, base_url)
    if not scope:
        return
    lock_path = KEYS_PATH.with_name(f".{KEYS_PATH.name}.lock")
    with _exclusive_file_lock(lock_path):
        keys = _load_keys_unlocked(strict=True)
        if api_key:
            keys[scope] = api_key
        else:
            keys.pop(scope, None)
        write_private_text_file(
            KEYS_PATH,
            json.dumps(keys, indent=2, ensure_ascii=False),
        )
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
    return ()


def delete_key(provider: str, base_url: str = "") -> None:
    """Delete the API key for one provider/Base URL scope."""
    save_key(provider, "", base_url)


def delete_all_keys() -> int:
    """Delete every locally persisted API key without exposing their values."""
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = KEYS_PATH.with_name(f".{KEYS_PATH.name}.lock")
    with _exclusive_file_lock(lock_path):
        keys = _load_keys_unlocked(strict=True)
        removed = len(keys)
        KEYS_PATH.unlink(missing_ok=True)
    logger.info("已删除全部本地 API Key：count=%s", removed)
    return removed
