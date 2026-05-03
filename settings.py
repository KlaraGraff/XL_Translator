"""
User-editable settings persisted to local JSON files.
API keys are stored separately in keys.json with OS-level permissions.
"""
import json
import platform
import stat
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
    DEFAULT_MAX_LEN,
    get_cloud_concurrency_bounds,
    get_concurrency_cap,
    get_default_concurrency,
    get_local_concurrency_bounds,
    KEYS_PATH,
    SETTINGS_SCHEMA_VERSION,
    SETTINGS_PATH,
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


def _clamp_int(value, *, minimum: int, maximum: int, fallback: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(minimum, min(maximum, number))


class EngineSettings(BaseModel):
    mode: str = "cloud"  # "cloud" | "local"
    cloud_provider: str = DEFAULT_CLOUD_PROVIDER
    cloud_model: str = DEFAULT_CLOUD_MODEL
    cloud_base_url: str = DEFAULT_CUSTOM_OPENAI_BASE_URL
    ollama_model: str = "qwen2.5:14b"
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

        migrated.setdefault("concurrency_unlocked", False)
        return migrated

    @model_validator(mode="after")
    def _normalize_concurrency_ranges(self):
        cloud_min, cloud_max = get_cloud_concurrency_bounds(self.concurrency_unlocked)
        self.concurrency = max(cloud_min, min(cloud_max, self.concurrency))

        local_min, local_max = get_local_concurrency_bounds(self.concurrency_unlocked)
        self.ollama_concurrency = max(local_min, min(local_max, self.ollama_concurrency))
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


class AppSettings(BaseModel):
    engine: EngineSettings = Field(default_factory=EngineSettings)
    tm: TMSettings = Field(default_factory=TMSettings)
    output: OutputSettings = Field(default_factory=OutputSettings)
    settings_version: int = SETTINGS_SCHEMA_VERSION
    source_lang: str = "zh"
    target_lang: str = "en"
    custom_target_langs: list[CustomTargetLang] = Field(default_factory=list)
    recent_target_langs: list[str] = Field(default_factory=list)
    domain_preset: str = "同步工程场景"
    custom_prompt: str = ""
    last_source_folder: str = ""
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

        if not is_supported_source_lang(self.source_lang):
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
    if DEFAULT_CLOUD_PROVIDER == "hermes":
        return
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


def save_key(provider: str, api_key: str) -> None:
    """Save or remove the API key for one provider."""
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    keys = load_keys()
    if api_key:
        keys[provider] = api_key
    else:
        keys.pop(provider, None)
    KEYS_PATH.write_text(json.dumps(keys, indent=2, ensure_ascii=False), encoding="utf-8")
    _apply_key_file_permissions(KEYS_PATH)
    logger.debug(f"API Key 已更新：provider={provider}")


def get_key(provider: str) -> str:
    """Get the API key for one provider."""
    return load_keys().get(provider, "")


def delete_key(provider: str) -> None:
    """Delete the API key for one provider."""
    save_key(provider, "")
