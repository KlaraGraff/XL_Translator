"""Model-role configuration helpers.

The UI lets one compact configuration surface switch between translation,
deep TM cleaning, and PDF image generation.  This module keeps the follow
rules and effective access resolution in one place so task code does not need
to duplicate sidebar behavior.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from config import (
    DEFAULT_CLOUD_MODEL,
    DEFAULT_CLOUD_PROVIDER,
    IMAGE_GENERATION_MODEL_PROVIDERS,
    VISION_TEXT_MODEL_PROVIDERS,
)
from settings import AppSettings, ModelRoleSettings, get_key

ROLE_TRANSLATION = "translation"
ROLE_CLEANER = "cleaner"
ROLE_IMAGE = "image"
ROLE_PDF_REVIEW = "pdf_review"

SOURCE_INDEPENDENT = "independent"

MODEL_ROLE_LABELS = {
    ROLE_TRANSLATION: "翻译模型",
    ROLE_CLEANER: "深度清洗模型",
    ROLE_IMAGE: "PDF 生图模型",
    ROLE_PDF_REVIEW: "PDF 翻译审核模型",
}

MODEL_ROLE_CAPABILITIES = {
    ROLE_TRANSLATION: "text",
    ROLE_CLEANER: "text",
    ROLE_IMAGE: "image",
    ROLE_PDF_REVIEW: "vision_text",
}

FOLLOW_SOURCE_LABELS = {
    SOURCE_INDEPENDENT: "独立配置",
    ROLE_TRANSLATION: "跟随翻译模型",
    ROLE_CLEANER: "跟随深度清洗模型",
}


class ModelRoleConfigError(ValueError):
    """Raised when a role configuration cannot be resolved."""


class ChainedModelFollowError(ModelRoleConfigError):
    """Raised when a role tries to follow a role that already follows another."""


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

    @property
    def engine_label(self) -> str:
        if self.mode == "local":
            return f"ollama/{self.model}"
        return f"{self.provider}/{self.model}"


def role_label(role: str) -> str:
    return MODEL_ROLE_LABELS.get(role, str(role or "模型"))


def role_capability(role: str) -> str:
    return MODEL_ROLE_CAPABILITIES.get(role, "text")


def get_role_settings(settings: AppSettings, role: str) -> ModelRoleSettings | None:
    if role == ROLE_CLEANER:
        return settings.cleaner_model_role
    if role == ROLE_IMAGE:
        return settings.image_model_role
    if role == ROLE_PDF_REVIEW:
        return settings.pdf_review_model_role
    return None


def allowed_source_roles(role: str) -> list[str]:
    if role == ROLE_CLEANER:
        return [SOURCE_INDEPENDENT, ROLE_TRANSLATION]
    if role == ROLE_IMAGE:
        return [SOURCE_INDEPENDENT, ROLE_TRANSLATION, ROLE_CLEANER]
    if role == ROLE_PDF_REVIEW:
        return [SOURCE_INDEPENDENT, ROLE_TRANSLATION]
    return [SOURCE_INDEPENDENT]


def normalize_source_role(role: str, source_role: str) -> str:
    source = str(source_role or SOURCE_INDEPENDENT).strip()
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
        "hermes",
        "claude",
        "openai",
        "custom_openai",
        "zhipu",
        "dashscope",
        "siliconflow",
        "lanyi",
    }


def resolve_effective_model_config(
    settings: AppSettings,
    role: str,
    *,
    _seen: tuple[str, ...] = (),
) -> EffectiveModelConfig:
    normalized_role = role if role in MODEL_ROLE_LABELS else ROLE_TRANSLATION
    if normalized_role in _seen:
        raise ChainedModelFollowError("模型配置来源存在循环，请改为独立配置。")

    if normalized_role == ROLE_TRANSLATION:
        if settings.engine.mode == "local":
            return EffectiveModelConfig(
                role=ROLE_TRANSLATION,
                label=role_label(ROLE_TRANSLATION),
                capability="text",
                mode="local",
                provider="ollama",
                model=str(settings.engine.ollama_model or "").strip(),
                base_url="",
                api_key="",
            )
        provider = str(settings.engine.cloud_provider or DEFAULT_CLOUD_PROVIDER).strip()
        return EffectiveModelConfig(
            role=ROLE_TRANSLATION,
            label=role_label(ROLE_TRANSLATION),
            capability="text",
            mode="cloud",
            provider=provider,
            model=str(settings.engine.cloud_model or DEFAULT_CLOUD_MODEL).strip(),
            base_url=str(settings.engine.cloud_base_url or "").strip(),
            api_key=get_key(provider),
        )

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
            provider = str(settings.engine.cloud_provider or DEFAULT_CLOUD_PROVIDER).strip()
            base_url = str(settings.engine.cloud_base_url or "").strip()
            api_key = get_key(provider)
        else:
            provider = source_config.provider
            base_url = source_config.base_url
            api_key = source_config.api_key
        return EffectiveModelConfig(
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

    provider = str(role_settings.cloud_provider or DEFAULT_CLOUD_PROVIDER).strip()
    return EffectiveModelConfig(
        role=normalized_role,
        label=role_label(normalized_role),
        capability=role_capability(normalized_role),
        mode="cloud",
        provider=provider,
        model=_role_model_name(role_settings, normalized_role),
        base_url=str(role_settings.cloud_base_url or "").strip(),
        api_key=get_key(provider),
        source_role=SOURCE_INDEPENDENT,
        follows=False,
        availability_status=role_settings.availability_status,
        availability_message=role_settings.availability_message,
        availability_signature=role_settings.availability_signature,
    )


def settings_for_text_role(settings: AppSettings, role: str) -> AppSettings:
    config = resolve_effective_model_config(settings, role)
    copy_settings = settings.model_copy(deep=True)
    if config.mode == "local":
        copy_settings.engine.mode = "local"
        copy_settings.engine.ollama_model = config.model
        return copy_settings
    copy_settings.engine.mode = "cloud"
    copy_settings.engine.cloud_provider = config.provider
    copy_settings.engine.cloud_model = config.model
    copy_settings.engine.cloud_base_url = config.base_url
    return copy_settings


def record_image_model_availability(
    settings: AppSettings,
    *,
    ok: bool,
    message: str,
    signature: str | None = None,
    checked_at: str = "",
) -> None:
    role_settings = settings.image_model_role
    role_settings.availability_status = "available" if ok else "unavailable"
    role_settings.availability_message = str(message or "").strip()
    role_settings.availability_signature = signature or image_model_signature(settings)
    role_settings.availability_checked_at = checked_at


def record_pdf_review_model_availability(
    settings: AppSettings,
    *,
    ok: bool,
    message: str,
    signature: str | None = None,
    checked_at: str = "",
) -> None:
    role_settings = settings.pdf_review_model_role
    role_settings.availability_status = "available" if ok else "unavailable"
    role_settings.availability_message = str(message or "").strip()
    role_settings.availability_signature = signature or pdf_review_model_signature(settings)
    role_settings.availability_checked_at = checked_at
