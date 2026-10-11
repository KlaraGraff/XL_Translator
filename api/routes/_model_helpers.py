"""Model-role payload builders and validation shared by the model and settings routes."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from core.model_roles import (
    ROLE_CLEANER,
    ROLE_IMAGE,
    ROLE_PDF_REVIEW,
    ROLE_TRANSLATION,
    LOCAL_CAPABLE_CAPABILITIES,
    EffectiveModelConfig,
    ModelRoleConfigError,
    allowed_source_roles,
    list_role_connections,
    model_config_signature,
    normalize_source_role,
    resolve_effective_model_config,
)
from core.model_throughput import (
    batch_size_bounds,
    concurrency_bounds,
    get_model_throughput,
)
from settings import (
    AppSettings,
    ModelConnection,
    mask_api_key,
)


def _connection_payload(
    connection: ModelConnection,
    index: int,
    config: EffectiveModelConfig,
    role: str,
    settings: AppSettings,
) -> dict[str, Any]:
    """Describe one pool connection, including a masked hint of its saved key."""
    # 必须和拨号时的取值顺序一致（core/model_roles.py::_connection_api_key）：连接
    # 作用域优先，取不到就回落到 provider + Base URL 作用域。以前非主用连接不做这个
    # 回落，于是任何靠 provider 作用域拿密钥的第二条连接（带 Key 导入进来的、老配置
    # 升上来的）都被标成「无密钥」，密钥框也不显示已保存掩码——它其实翻译得好好的。
    api_key = config.api_key
    return {
        "id": connection.id,
        "label": connection.label,
        "display_label": connection.display_label,
        "provider": config.provider,
        "model": config.model,
        "base_url": config.base_url,
        "api_mode": config.api_mode,
        "connection_mode": connection.connection_mode,
        "source_role": connection.source_role,
        "connection_pool_role": role,
        "own_provider": connection.provider,
        "own_model": connection.model,
        "own_local_model": connection.local_model,
        "own_base_url": connection.base_url,
        "own_api_mode": connection.api_mode,
        "own_cloud_provider": connection.cloud_provider,
        "own_cloud_model": connection.cloud_model,
        "own_cloud_base_url": connection.cloud_base_url,
        "own_cloud_api_mode": connection.cloud_api_mode,
        "own_local_provider": connection.local_provider,
        "own_local_base_url": connection.local_base_url,
        "availability_status": config.availability_status,
        "availability_message": config.availability_message,
        "availability_checked_at": connection.availability_checked_at,
        "has_api_key": bool(api_key),
        "api_key_preview": mask_api_key(api_key),
        "primary": index == 0,
        # 每条连接自己的速率。面板的「速率设置」挂在连接详情下，以前却永远读写主用
        # 连接的那份：选中第二条连接改并发，改的其实是主用连接。
        **_throughput_payload(settings, config),
    }


def _throughput_payload(settings: AppSettings, config: EffectiveModelConfig) -> dict[str, Any]:
    throughput = get_model_throughput(settings, config)
    unlocked = settings.engine.concurrency_unlocked
    return {
        "throughput": {
            "profile_key": throughput.profile_key,
            "batch_size": throughput.batch_size,
            "concurrency": throughput.concurrency,
        },
        "throughput_bounds": {
            "batch_size": None if unlocked else batch_size_bounds(config),
            "concurrency": None if unlocked else concurrency_bounds(config),
        },
    }


def _model_role_payload(settings: AppSettings, role: str) -> dict[str, Any]:
    config = _model_config_or_422(settings, role)
    throughput = get_model_throughput(settings, config)
    owner = settings.engine if role == ROLE_TRANSLATION else {
        ROLE_CLEANER: settings.cleaner_model_role,
        ROLE_IMAGE: settings.image_model_role,
        ROLE_PDF_REVIEW: settings.pdf_review_model_role,
    }[role]
    return {
        "role": config.role,
        "label": config.label,
        "capability": config.capability,
        "mode": config.mode,
        "provider": config.provider,
        "model": config.model,
        "base_url": config.base_url,
        "api_mode": config.api_mode,
        # Every role edits its own pool.  A follow row exposes effective
        # endpoint values while keeping its own model and stored endpoint.
        "connections": [
            _connection_payload(
                connection, index,
                _model_config_or_422(settings, role, connection.id), role, settings,
            )
            for index, connection in enumerate(
                list_role_connections(settings, role)
            )
        ],
        "connection_pool_role": role,
        "source_role": config.source_role,
        # Which follow sources are legal *right now*: a role that already
        # follows something cannot be followed, or it would form a chain.  The
        # panel builds its 连接方式 list from this instead of hardcoding pairs.
        "source_role_options": allowed_source_roles(role, settings),
        "supports_local": config.capability in LOCAL_CAPABLE_CAPABILITIES,
        "follows": config.follows,
        "availability_status": config.availability_status,
        "availability_message": config.availability_message,
        "availability_checked_at": getattr(owner, "availability_checked_at", ""),
        "availability_signature": config.availability_signature,
        "has_api_key": bool(config.api_key),
        "api_key_preview": mask_api_key(config.api_key),
        "throughput": {
            "profile_key": throughput.profile_key,
            "batch_size": throughput.batch_size,
            "concurrency": throughput.concurrency,
        },
        "throughput_bounds": {
            "batch_size": None if settings.engine.concurrency_unlocked else batch_size_bounds(config),
            "concurrency": None if settings.engine.concurrency_unlocked else concurrency_bounds(config),
        },
    }


MODEL_ROLE_SETTING_KEYS = {
    ROLE_TRANSLATION: "engine",
    ROLE_CLEANER: "cleaner_model_role",
    ROLE_IMAGE: "image_model_role",
    ROLE_PDF_REVIEW: "pdf_review_model_role",
}


def _reject_illegal_follow_choice(role: str, source_role: str | None) -> None:
    """Reject a follow combination the user is explicitly asking for.

    Only an explicit choice reaches this: a value merely inherited from an old
    settings file must keep loading (it degrades to 独立配置 on read), or the
    user would be locked out of saving anything at all.
    """
    if source_role is None:
        return
    try:
        normalize_source_role(role, source_role, strict=True)
    except ModelRoleConfigError as exc:
        raise HTTPException(422, str(exc)) from exc


def _reject_illegal_follow_payload(payload: dict[str, Any]) -> None:
    """Apply the same rule to the roles a raw settings payload names itself."""
    if not isinstance(payload, dict):
        return
    for role, setting_key in MODEL_ROLE_SETTING_KEYS.items():
        block = payload.get(setting_key)
        if not isinstance(block, dict) or "source_role" not in block:
            continue
        _reject_illegal_follow_choice(role, str(block.get("source_role") or ""))


def _model_config_or_422(settings: AppSettings, role: str, connection_id: str = ""):
    if role not in {ROLE_TRANSLATION, ROLE_CLEANER, ROLE_IMAGE, ROLE_PDF_REVIEW}:
        raise HTTPException(404, "Unknown model role.")
    try:
        return resolve_effective_model_config(settings, role, connection_id=connection_id)
    except Exception as exc:
        raise HTTPException(422, str(exc)) from exc


def _effective_role_signatures(settings: AppSettings) -> dict[str, str]:
    """Return currently resolvable role signatures for mutation invalidation."""
    signatures: dict[str, str] = {}
    for role in (ROLE_TRANSLATION, ROLE_CLEANER, ROLE_IMAGE, ROLE_PDF_REVIEW):
        try:
            signatures[role] = model_config_signature(
                resolve_effective_model_config(settings, role)
            )
        except Exception:
            # The following validation will return the useful configuration
            # error.  Missing signatures must still cause a reset if repaired.
            continue
    return signatures
