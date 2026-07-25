"""Model-role configuration helpers.

The UI lets one compact configuration surface switch between translation,
deep TM cleaning, and PDF image generation.  This module keeps the follow
rules and effective access resolution in one place so task code does not need
to duplicate sidebar behavior.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace as dataclass_replace

from config import (
    DEFAULT_CLOUD_MODEL,
    DEFAULT_CLOUD_PROVIDER,
    IMAGE_GENERATION_MODEL_PROVIDERS,
    VISION_TEXT_MODEL_PROVIDERS,
)
from settings import (
    AppSettings,
    ModelConnection,
    ModelRoleSettings,
    get_cloud_provider_config,
    get_connection_scoped_key,
    get_key,
    set_cloud_provider_config,
)

ROLE_TRANSLATION = "translation"
ROLE_CLEANER = "cleaner"
ROLE_IMAGE = "image"
ROLE_PDF_REVIEW = "pdf_review"

SOURCE_INDEPENDENT = "independent"

MODEL_ROLE_LABELS = {
    ROLE_TRANSLATION: "翻译模型",
    ROLE_CLEANER: "深度清洗模型",
    ROLE_IMAGE: "PDF 翻译模型",
    ROLE_PDF_REVIEW: "PDF 翻译审核模型",
}

MODEL_ROLE_CAPABILITIES = {
    ROLE_TRANSLATION: "text",
    ROLE_CLEANER: "text",
    ROLE_IMAGE: "image",
    ROLE_PDF_REVIEW: "vision_text",
}

MODEL_ROLES = (
    ROLE_TRANSLATION,
    ROLE_CLEANER,
    ROLE_IMAGE,
    ROLE_PDF_REVIEW,
)

FOLLOW_SOURCE_LABELS = {
    SOURCE_INDEPENDENT: "独立配置",
    ROLE_TRANSLATION: "跟随翻译模型",
    ROLE_CLEANER: "跟随深度清洗模型",
    ROLE_IMAGE: "跟随PDF翻译模型",
}


class ModelRoleConfigError(ValueError):
    """Raised when a role configuration cannot be resolved."""


class ChainedModelFollowError(ModelRoleConfigError):
    """Raised when a role tries to follow a role that already follows another."""


class LocalModelFollowNotAllowedError(ModelRoleConfigError):
    """Raised when a cloud-only role tries to follow a local translation model."""


class ModelCapabilityError(ModelRoleConfigError):
    """Raised when a provider cannot satisfy a role's required capability."""


@dataclass(frozen=True)
class EffectiveModelConfig:
    role: str
    label: str
    capability: str
    mode: str
    provider: str
    model: str
    base_url: str
    api_key: str
    source_role: str = SOURCE_INDEPENDENT
    follows: bool = False
    availability_status: str = "unknown"
    availability_message: str = ""
    availability_signature: str = ""
    # Which pool entry produced this config, so a task can record and log the
    # connection it actually ran on rather than just "the role's connection".
    connection_id: str = ""
    connection_label: str = ""

    @property
    def engine_label(self) -> str:
        if self.mode == "local":
            return f"{self.provider}/{self.model}"
        return f"{self.provider}/{self.model}"


def role_label(role: str) -> str:
    return MODEL_ROLE_LABELS.get(role, str(role or "模型"))


def role_capability(role: str) -> str:
    return MODEL_ROLE_CAPABILITIES.get(role, "text")


def _connection_api_key(connection: ModelConnection, provider: str, base_url: str) -> str:
    """Resolve a connection's key, falling back to this module's get_key.

    The fallback deliberately goes through the module-level ``get_key`` so the
    provider/Base URL lookup stays one substitutable seam for callers and tests.
    """
    return get_connection_scoped_key(connection.id) or get_key(provider, base_url)


def role_pool_owner(settings: AppSettings, role: str):
    """Return the settings object that owns one role's connection pool."""
    if role == ROLE_TRANSLATION:
        return settings.engine
    owner = get_role_settings(settings, role)
    if owner is None:
        raise ModelRoleConfigError(f"未知模型用途：{role}")
    return owner


def list_role_connections(settings: AppSettings, role: str) -> list[ModelConnection]:
    """Return one role's ordered pool.

    Entry 0 is the primary.  The pool always has at least one entry because
    the settings validator seeds it from the legacy single connection.
    """
    return list(role_pool_owner(settings, role).connections or [])


def find_role_connection(
    settings: AppSettings,
    role: str,
    connection_id: str = "",
) -> ModelConnection:
    """Return the requested pool entry, or the primary when unspecified.

    An unknown id falls back to the primary rather than raising: a pool entry
    can be removed while a task still holds its id, and degrading to the
    primary is safer than failing the task outright.
    """
    connections = list_role_connections(settings, role)
    if not connections:
        raise ModelRoleConfigError(f"{role_label(role)}没有可用的连接。")
    wanted = str(connection_id or "").strip()
    if wanted:
        for connection in connections:
            if connection.id == wanted:
                return connection
    return connections[0]


def _apply_primary_to_legacy_fields(owner) -> None:
    """Copy entry 0 onto the legacy single-connection fields.

    The settings validator syncs legacy fields *onto* entry 0, so any edit that
    changes which entry is primary has to push the new primary outwards first
    or the validator will put the old values straight back.
    """
    primary = owner.connections[0]
    owner.cloud_provider = primary.provider
    owner.cloud_model = primary.model
    owner.cloud_base_url = primary.base_url
    # The per-provider stash outranks the flat fields inside the validator, so
    # promoting an entry has to update it too or the old endpoint comes back.
    set_cloud_provider_config(
        owner,
        primary.provider,
        cloud_model=primary.model,
        cloud_base_url=primary.base_url,
    )
    owner.availability_status = primary.availability_status
    owner.availability_message = primary.availability_message
    owner.availability_checked_at = primary.availability_checked_at
    owner.availability_signature = primary.availability_signature


def add_role_connection(
    settings: AppSettings,
    role: str,
    *,
    label: str = "",
    provider: str = "",
    model: str = "",
    base_url: str = "",
) -> ModelConnection:
    """Append a new entry to one role's pool and return it."""
    owner = role_pool_owner(settings, role)
    primary = owner.connections[0]
    connection = ModelConnection(
        label=label,
        provider=provider or primary.provider,
        model=model or primary.model,
        base_url=base_url,
    )
    owner.connections = [*owner.connections, connection]
    return connection


def update_role_connection(
    settings: AppSettings,
    role: str,
    connection_id: str,
    *,
    label: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
) -> ModelConnection:
    """Edit one pool entry in place."""
    owner = role_pool_owner(settings, role)
    connection = find_role_connection(settings, role, connection_id)
    if connection.id != str(connection_id or "").strip():
        raise ModelRoleConfigError("找不到要修改的连接。")
    if label is not None:
        connection.label = str(label).strip()
    changed_endpoint = False
    for field_name, value in (
        ("provider", provider),
        ("model", model),
        ("base_url", base_url),
    ):
        if value is None:
            continue
        setattr(connection, field_name, str(value).strip())
        changed_endpoint = True
    if changed_endpoint:
        # The endpoint moved, so any prior test result no longer describes it.
        connection.availability_status = "unknown"
        connection.availability_message = "当前配置尚未测试。"
        connection.availability_signature = ""
        connection.availability_checked_at = ""
    if owner.connections[0].id == connection.id:
        _apply_primary_to_legacy_fields(owner)
    return connection


def remove_role_connection(settings: AppSettings, role: str, connection_id: str) -> None:
    """Remove one pool entry; a role must keep at least one."""
    owner = role_pool_owner(settings, role)
    wanted = str(connection_id or "").strip()
    remaining = [conn for conn in owner.connections if conn.id != wanted]
    if len(remaining) == len(owner.connections):
        raise ModelRoleConfigError("找不到要删除的连接。")
    if not remaining:
        raise ModelRoleConfigError(f"{role_label(role)}至少要保留一条连接。")
    owner.connections = remaining
    _apply_primary_to_legacy_fields(owner)


def reorder_role_connections(
    settings: AppSettings,
    role: str,
    ordered_ids: list[str],
) -> None:
    """Reorder a pool; entry 0 becomes the new primary."""
    owner = role_pool_owner(settings, role)
    by_id = {conn.id: conn for conn in owner.connections}
    wanted = [str(value or "").strip() for value in ordered_ids]
    if sorted(wanted) != sorted(by_id):
        raise ModelRoleConfigError("排序必须包含且只包含当前的全部连接。")
    owner.connections = [by_id[connection_id] for connection_id in wanted]
    _apply_primary_to_legacy_fields(owner)


def get_role_settings(settings: AppSettings, role: str) -> ModelRoleSettings | None:
    if role == ROLE_CLEANER:
        return settings.cleaner_model_role
    if role == ROLE_IMAGE:
        return settings.image_model_role
    if role == ROLE_PDF_REVIEW:
        return settings.pdf_review_model_role
    return None


def model_role_owner(settings: AppSettings, role: str):
    """Return the settings object that owns one role's test state.

    The translation role is stored on ``engine`` while the other roles have
    their own settings objects.  Keeping this mapping here prevents callers
    that construct a temporary text-engine settings copy from accidentally
    persisting its test result on the translation role.
    """
    if role == ROLE_TRANSLATION:
        return settings.engine
    owner = get_role_settings(settings, role)
    if owner is None:
        raise ModelRoleConfigError(f"未知模型用途：{role}")
    return owner


def reset_model_role_availability(
    settings: AppSettings,
    role: str,
    *,
    message: str = "当前配置尚未测试。",
) -> None:
    """Mark one role as requiring an explicit connectivity re-test."""
    owner = model_role_owner(settings, role)
    owner.availability_status = "unknown"
    owner.availability_message = str(message or "当前配置尚未测试。").strip()
    owner.availability_signature = ""
    owner.availability_checked_at = ""


def reset_all_model_role_availability(
    settings: AppSettings,
    *,
    message: str = "当前配置尚未测试。",
) -> None:
    """Reset all four role test states after a configuration import."""
    for role in MODEL_ROLES:
        reset_model_role_availability(settings, role, message=message)


def record_model_role_availability(
    settings: AppSettings,
    role: str,
    *,
    ok: bool,
    message: str,
    signature: str | None = None,
    checked_at: str = "",
) -> None:
    """Persist an explicit test result on the role that was actually tested."""
    owner = model_role_owner(settings, role)
    owner.availability_status = "available" if ok else "unavailable"
    owner.availability_message = str(message or "").strip()
    owner.availability_signature = signature or model_config_signature(
        resolve_effective_model_config(settings, role)
    )
    owner.availability_checked_at = str(checked_at or "").strip()


def validate_all_model_roles(
    settings: AppSettings,
) -> dict[str, EffectiveModelConfig]:
    """Resolve all roles before saving a shared configuration edit.

    A translation connection can be reused by cleaner/image/review roles.
    Therefore changing one role may invalidate another role even when its own
    settings block was untouched.  Saving only after this full validation
    keeps an impossible reuse graph out of persistent settings.
    """
    return {
        role: resolve_effective_model_config(settings, role)
        for role in MODEL_ROLES
    }


def allowed_source_roles(role: str) -> list[str]:
    if role == ROLE_CLEANER:
        return [SOURCE_INDEPENDENT, ROLE_TRANSLATION]
    if role == ROLE_IMAGE:
        return [SOURCE_INDEPENDENT, ROLE_TRANSLATION]
    if role == ROLE_PDF_REVIEW:
        return [SOURCE_INDEPENDENT, ROLE_TRANSLATION, ROLE_IMAGE]
    return [SOURCE_INDEPENDENT]


def normalize_source_role(role: str, source_role: str) -> str:
    source = str(source_role or SOURCE_INDEPENDENT).strip()
    if source not in allowed_source_roles(role) and source != SOURCE_INDEPENDENT:
        raise ChainedModelFollowError(
            f"{role_label(role)}不能跟随{role_label(source)}，请直接选择翻译模型或独立连接。"
        )
    return source if source in allowed_source_roles(role) else SOURCE_INDEPENDENT


def source_label(source_role: str) -> str:
    return FOLLOW_SOURCE_LABELS.get(source_role, FOLLOW_SOURCE_LABELS[SOURCE_INDEPENDENT])


def _role_model_name(role_settings: ModelRoleSettings, role: str) -> str:
    model = str(role_settings.cloud_model or "").strip()
    if model:
        return model
    if role in {ROLE_IMAGE, ROLE_PDF_REVIEW}:
        return ""
    return DEFAULT_CLOUD_MODEL


def _hash_secret(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def model_config_signature(config: EffectiveModelConfig) -> str:
    return "|".join(
        [
            config.role,
            config.capability,
            config.mode,
            config.provider,
            config.model,
            config.base_url.rstrip("/"),
            _hash_secret(config.api_key),
        ]
    )


def image_model_signature(settings: AppSettings) -> str:
    return model_config_signature(resolve_effective_model_config(settings, ROLE_IMAGE))


def pdf_review_model_signature(settings: AppSettings) -> str:
    return model_config_signature(resolve_effective_model_config(settings, ROLE_PDF_REVIEW))


def image_generation_provider_values() -> set[str]:
    return set(IMAGE_GENERATION_MODEL_PROVIDERS.values())


def vision_text_provider_values() -> set[str]:
    return set(VISION_TEXT_MODEL_PROVIDERS.values())


def provider_supports_capability(provider: str, capability: str) -> bool:
    provider_value = str(provider or "").strip()
    if capability == "image":
        return provider_value in image_generation_provider_values()
    if capability == "vision_text":
        return provider_value in vision_text_provider_values()
    return provider_value in {
        "claude",
        "openai",
        "custom_openai",
        "zhipu",
        "dashscope",
        "siliconflow",
        "lanyi",
    }


def validate_model_capability(config: EffectiveModelConfig) -> None:
    """Reject a role/provider combination before a task or test can start.

    Model names are intentionally not treated as a capability guarantee.  The
    provider allow-list is the early, deterministic guard; the role-specific
    connectivity test remains the authoritative protocol check.
    """
    if config.mode == "local" and config.capability != "text":
        raise ModelCapabilityError(
            f"{config.label}只支持云端{config.capability}能力，不能使用本地模型。"
        )
    if config.mode == "cloud" and not provider_supports_capability(
        config.provider,
        config.capability,
    ):
        raise ModelCapabilityError(
            f"服务商 {config.provider or '未配置'} 不支持{config.label}所需的"
            f" {config.capability} 能力，请改用具备该能力的连接。"
        )


def _availability_for_config(
    config: EffectiveModelConfig,
    role_settings: ModelRoleSettings | None,
) -> EffectiveModelConfig:
    if role_settings is None:
        return config
    current_signature = model_config_signature(config)
    if role_settings.availability_signature != current_signature:
        return dataclass_replace(
            config,
            availability_status="unknown",
            availability_message="当前配置尚未测试。",
            availability_signature=current_signature,
        )
    return dataclass_replace(
        config,
        availability_status=str(role_settings.availability_status or "unknown"),
        availability_message=str(role_settings.availability_message or ""),
        availability_signature=current_signature,
    )


def resolve_effective_model_config(
    settings: AppSettings,
    role: str,
    *,
    connection_id: str = "",
    _seen: tuple[str, ...] = (),
) -> EffectiveModelConfig:
    """Resolve one role's effective connection.

    ``connection_id`` selects a pool entry; omitting it resolves the primary,
    which is mirrored onto the legacy fields and therefore behaves exactly as
    it did before pools existed.
    """
    normalized_role = role if role in MODEL_ROLE_LABELS else ROLE_TRANSLATION
    if normalized_role in _seen:
        raise ChainedModelFollowError("模型配置来源存在循环，请改为独立配置。")

    if normalized_role == ROLE_TRANSLATION:
        if settings.engine.mode == "local":
            provider = str(settings.engine.local_provider or "ollama").strip()
            config = EffectiveModelConfig(
                role=ROLE_TRANSLATION,
                label=role_label(ROLE_TRANSLATION),
                capability="text",
                mode="local",
                provider=provider,
                model=str(settings.engine.local_model or settings.engine.ollama_model or "").strip(),
                base_url=str(settings.engine.local_base_url or "").strip(),
                api_key="",
            )
            validate_model_capability(config)
            return _availability_for_config(config, settings.engine)
        connection = find_role_connection(settings, ROLE_TRANSLATION, connection_id)
        is_primary = connection.id == list_role_connections(settings, ROLE_TRANSLATION)[0].id
        if is_primary:
            # The primary keeps reading through cloud_provider_configs so that
            # switching provider still restores that provider's saved model.
            provider = str(settings.engine.cloud_provider or DEFAULT_CLOUD_PROVIDER).strip()
            provider_config = get_cloud_provider_config(settings.engine, provider)
            model = provider_config.cloud_model or DEFAULT_CLOUD_MODEL
            base_url = provider_config.cloud_base_url
        else:
            provider = connection.provider or DEFAULT_CLOUD_PROVIDER
            model = connection.model or DEFAULT_CLOUD_MODEL
            base_url = connection.base_url
        config = EffectiveModelConfig(
            role=ROLE_TRANSLATION,
            label=role_label(ROLE_TRANSLATION),
            capability="text",
            mode="cloud",
            provider=provider,
            model=model,
            base_url=base_url,
            api_key=_connection_api_key(connection, provider, base_url),
            connection_id=connection.id,
            connection_label=connection.display_label,
        )
        validate_model_capability(config)
        return _availability_for_config(config, settings.engine)

    role_settings = get_role_settings(settings, normalized_role)
    if role_settings is None:
        raise ModelRoleConfigError(f"未知模型用途：{role}")

    source = normalize_source_role(normalized_role, role_settings.source_role)
    if source != role_settings.source_role:
        role_settings.source_role = source

    if source != SOURCE_INDEPENDENT:
        source_settings = get_role_settings(settings, source)
        if source_settings is not None and source_settings.source_role != SOURCE_INDEPENDENT:
            raise ChainedModelFollowError(
                f"{role_label(normalized_role)}不能跟随已经跟随其他模型的{role_label(source)}，"
                "请直接选择最终来源。"
            )
        source_config = resolve_effective_model_config(
            settings,
            source,
            _seen=(*_seen, normalized_role),
        )
        if source_config.mode == "local":
            raise LocalModelFollowNotAllowedError(
                _local_follow_not_allowed_message(normalized_role)
            )
        else:
            provider = source_config.provider
            base_url = source_config.base_url
            api_key = source_config.api_key
        config = EffectiveModelConfig(
            role=normalized_role,
            label=role_label(normalized_role),
            capability=role_capability(normalized_role),
            mode="cloud",
            provider=provider,
            model=_role_model_name(role_settings, normalized_role),
            base_url=base_url,
            api_key=api_key,
            source_role=source,
            follows=True,
            availability_status=role_settings.availability_status,
            availability_message=role_settings.availability_message,
            availability_signature=role_settings.availability_signature,
        )
        validate_model_capability(config)
        return _availability_for_config(config, role_settings)

    connection = find_role_connection(settings, normalized_role, connection_id)
    if connection.id == list_role_connections(settings, normalized_role)[0].id:
        provider = str(role_settings.cloud_provider or DEFAULT_CLOUD_PROVIDER).strip()
        provider_config = get_cloud_provider_config(role_settings, provider)
        model = provider_config.cloud_model or _role_model_name(role_settings, normalized_role)
        base_url = provider_config.cloud_base_url
    else:
        provider = connection.provider or DEFAULT_CLOUD_PROVIDER
        model = connection.model or _role_model_name(role_settings, normalized_role)
        base_url = connection.base_url
    config = EffectiveModelConfig(
        role=normalized_role,
        label=role_label(normalized_role),
        capability=role_capability(normalized_role),
        mode="cloud",
        provider=provider,
        model=model,
        base_url=base_url,
        api_key=_connection_api_key(connection, provider, base_url),
        connection_id=connection.id,
        connection_label=connection.display_label,
        source_role=SOURCE_INDEPENDENT,
        follows=False,
        availability_status=role_settings.availability_status,
        availability_message=role_settings.availability_message,
        availability_signature=role_settings.availability_signature,
    )
    validate_model_capability(config)
    return _availability_for_config(config, role_settings)


def _local_follow_not_allowed_message(role: str) -> str:
    role_name = role_label(role)
    reason = {
        ROLE_CLEANER: "深度清洗需要云端文本模型。",
        ROLE_IMAGE: "PDF 翻译需要云端图像生成模型。",
        ROLE_PDF_REVIEW: "翻译审核需要云端图像理解模型。",
    }.get(role, f"{role_name}只支持云端模型。")
    return (
        "跟随来源不可用：翻译模型当前是本地模型，请改为独立云端配置。"
        f"\n原因：{reason}"
    )


def settings_for_text_role(
    settings: AppSettings,
    role: str,
    *,
    connection_id: str = "",
) -> AppSettings:
    config = resolve_effective_model_config(settings, role, connection_id=connection_id)
    copy_settings = settings.model_copy(deep=True)
    if config.mode == "local":
        copy_settings.engine.mode = "local"
        copy_settings.engine.local_provider = config.provider
        copy_settings.engine.local_model = config.model
        copy_settings.engine.local_base_url = config.base_url
        copy_settings.engine.ollama_model = config.model
        return copy_settings
    copy_settings.engine.mode = "cloud"
    copy_settings.engine.cloud_provider = config.provider
    copy_settings.engine.cloud_model = config.model
    copy_settings.engine.cloud_base_url = config.base_url
    set_cloud_provider_config(
        copy_settings.engine,
        config.provider,
        cloud_model=config.model,
        cloud_base_url=config.base_url,
    )
    return copy_settings


def record_image_model_availability(
    settings: AppSettings,
    *,
    ok: bool,
    message: str,
    signature: str | None = None,
    checked_at: str = "",
) -> None:
    record_model_role_availability(
        settings,
        ROLE_IMAGE,
        ok=ok,
        message=message,
        signature=signature or image_model_signature(settings),
        checked_at=checked_at,
    )


def record_pdf_review_model_availability(
    settings: AppSettings,
    *,
    ok: bool,
    message: str,
    signature: str | None = None,
    checked_at: str = "",
) -> None:
    record_model_role_availability(
        settings,
        ROLE_PDF_REVIEW,
        ok=ok,
        message=message,
        signature=signature or pdf_review_model_signature(settings),
        checked_at=checked_at,
    )
