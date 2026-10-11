"""Provider API keys stored in keys.json."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Response

from core.model_roles import (
    ROLE_CLEANER,
    ROLE_IMAGE,
    ROLE_PDF_REVIEW,
    ROLE_TRANSLATION,
    list_role_connections,
    model_config_signature,
    reset_model_role_availability,
    reset_role_connection_availability,
    resolve_effective_model_config,
)
from settings import (
    AppSettings,
    delete_key,
    get_connection_scoped_key,
    get_key,
    load_keys,
    load_settings,
    parse_api_key_scope,
    save_key,
    save_settings,
)
from api.schemas import (
    ApiKeyPayload,
)
from api.routes._model_helpers import (
    _effective_role_signatures,
)


def register(app: FastAPI) -> None:
    """Register the keys routes on ``app``."""

    @app.get("/api/keys")
    def list_keys() -> dict[str, list[dict[str, str | bool]]]:
        scopes = []
        for scope, value in sorted(load_keys().items()):
            provider, base_url = parse_api_key_scope(scope)
            scopes.append(
                {
                    "scope": scope,
                    "provider": provider,
                    "base_url": base_url,
                    "has_key": bool(str(value or "").strip()),
                }
            )
        return {"keys": scopes}

    @app.put("/api/keys/{provider}")
    def put_key(provider: str, payload: ApiKeyPayload) -> dict[str, Any]:
        if not provider.strip():
            raise HTTPException(422, "Provider is required.")
        settings = load_settings()
        before_signatures = _effective_role_signatures(settings)
        before_credentials = _connection_credentials(settings)
        save_key(provider, payload.api_key, payload.base_url)
        _reset_roles_with_changed_effective_signature(
            settings,
            before_signatures,
            message="API Key 已变化，请重新测试当前配置。",
        )
        _reset_connections_with_changed_credential(
            settings,
            before_credentials,
            message="API Key 已变化，请重新测试当前配置。",
        )
        save_settings(settings)
        return {
            "provider": provider,
            "base_url": payload.base_url,
            "has_key": bool(payload.api_key.strip()),
        }

    @app.delete("/api/keys/{provider}", status_code=204)
    def remove_key(provider: str, base_url: str = "") -> Response:
        settings = load_settings()
        before_signatures = _effective_role_signatures(settings)
        before_credentials = _connection_credentials(settings)
        delete_key(provider, base_url)
        _reset_roles_with_changed_effective_signature(
            settings,
            before_signatures,
            message="API Key 已变化，请重新测试当前配置。",
        )
        _reset_connections_with_changed_credential(
            settings,
            before_credentials,
            message="API Key 已变化，请重新测试当前配置。",
        )
        save_settings(settings)
        return Response(status_code=204)


def _connection_credentials(settings: AppSettings) -> dict[tuple[str, str], str]:
    """按 (角色, 连接 id) 记下每条连接**实际会用到**的密钥。

    角色级签名只覆盖四条主用连接，非主用连接靠 provider + Base URL 作用域回落取
    密钥（``core/model_roles.py::_connection_api_key``）。轮换那个作用域下的密钥
    时，它们的「测试通过」不属于任何角色签名，于是原样留着——面板上仍是绿点，可
    那把测出绿点的密钥已经不存在了。
    """
    snapshot: dict[tuple[str, str], str] = {}
    for role in (ROLE_TRANSLATION, ROLE_CLEANER, ROLE_IMAGE, ROLE_PDF_REVIEW):
        try:
            connections = list_role_connections(settings, role)
        except Exception:
            continue
        for connection in connections:
            snapshot[(role, connection.id)] = get_connection_scoped_key(
                connection.id
            ) or get_key(connection.provider, connection.base_url)
    return snapshot


def _reset_connections_with_changed_credential(
    settings: AppSettings,
    before: dict[tuple[str, str], str],
    *,
    message: str,
) -> None:
    """Invalidate pool entries whose resolved key is no longer the tested one."""
    for (role, connection_id), previous in before.items():
        try:
            connections = list_role_connections(settings, role)
        except Exception:
            continue
        for connection in connections:
            if connection.id != connection_id:
                continue
            current = get_connection_scoped_key(connection.id) or get_key(
                connection.provider, connection.base_url
            )
            if current != previous:
                reset_role_connection_availability(
                    settings, role, connection_id, message=message
                )


def _reset_roles_with_changed_effective_signature(
    settings: AppSettings,
    before_signatures: dict[str, str],
    *,
    message: str,
) -> None:
    """Invalidate only role tests whose effective connection identity changed."""
    for role in (ROLE_TRANSLATION, ROLE_CLEANER, ROLE_IMAGE, ROLE_PDF_REVIEW):
        try:
            after_signature = model_config_signature(
                resolve_effective_model_config(settings, role)
            )
        except Exception:
            after_signature = ""
        if before_signatures.get(role) != after_signature:
            reset_model_role_availability(settings, role, message=message)
