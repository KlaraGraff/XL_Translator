"""Model roles, connections, connectivity checks, catalogs and throughput."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException

from core.connectivity_check import check_connectivity
from core.image_generation import check_image_generation_connectivity
from core.model_catalog import fetch_openai_compatible_models
from core.model_roles import (
    ROLE_CLEANER,
    ROLE_IMAGE,
    ROLE_PDF_REVIEW,
    ROLE_TRANSLATION,
    ModelRoleConfigError,
    add_role_connection,
    amend_model_role_availability_message,
    allowed_source_roles,
    list_role_connections,
    model_config_signature,
    model_role_owner,
    remove_role_connection,
    reorder_role_connections,
    update_role_connection,
    reset_model_role_availability,
    reset_role_connection_availability,
    resolve_effective_model_config,
    validate_all_model_roles,
    validate_model_capability,
)
from core.model_throughput import (
    batch_size_bounds,
    carry_over_throughput_profiles,
    concurrency_bounds,
    get_model_throughput,
    reset_model_throughput,
    set_model_throughput,
    throughput_profile_keys,
)
from core.pdf_review import REVIEW_MODEL_ADVICE, check_pdf_review_connectivity
from config import (
    CLOUD_PROVIDER_BASE_URL_DEFAULTS,
    CLOUD_API_KEY_PAGES_BY_HOST,
    CLOUD_PROVIDER_API_KEY_PAGES,
    CLOUD_PROVIDER_BASE_URL_DISABLED,
    CLOUD_PROVIDER_BASE_URL_HINTS,
    CLOUD_PROVIDER_BASE_URL_OPTIONS,
    DISABLED_BASE_URL_PLACEHOLDER,
    is_valid_concurrency_unlock_code,
    with_connection_failure_hint,
)
from settings import (
    AppSettings,
    ModelConnection,
    delete_connection_key,
    get_key,
    load_settings,
    save_connection_key,
    save_settings,
)
from api.schemas import (
    ModelFetchRequest,
    ModelCatalogRefreshPayload,
    ModelConnectivityPayload,
    ModelRoleUpdatePayload,
    ConnectionUpsertPayload,
    ConnectionReorderPayload,
    ThroughputPayload,
    ThroughputUnlockPayload,
)
from api.routes._shared import (
    _json_safe,
)
from api.routes._model_helpers import (
    _model_role_payload,
    _reject_illegal_follow_choice,
    _model_config_or_422,
    _effective_role_signatures,
)


def register(app: FastAPI) -> None:
    """Register the models routes on ``app``."""

    @app.get("/api/models/provider-defaults")
    def get_provider_defaults() -> dict[str, Any]:
        """Serve the Base URL presets so the form can prefill them on select.

        The panel used to leave Base URL untouched when the user picked a
        provider: the default was only applied server-side at save time, so a
        preset that existed in ``config.py`` looked like it did not exist at
        all.  Prefilling needs these values *before* the save, and a second
        copy of the table in the UI would drift the first time a provider's
        endpoint moves.

        ``api_key_pages`` (by provider) and ``api_key_pages_by_host`` (by Base
        URL host, checked first) feed the form's link to each provider's key
        console.  ``base_url_hints`` explains the Base URL for providers whose
        address depends on the plan (Chinese source, translated by the UI);
        ``base_url_options`` lists that provider's common addresses for the
        Base URL dropdown.
        No model name is prefilled: presets go stale too quickly.
        """
        return {
            "base_url_defaults": dict(CLOUD_PROVIDER_BASE_URL_DEFAULTS),
            "base_url_disabled": sorted(CLOUD_PROVIDER_BASE_URL_DISABLED),
            "disabled_placeholder": DISABLED_BASE_URL_PLACEHOLDER,
            "api_key_pages": dict(CLOUD_PROVIDER_API_KEY_PAGES),
            "api_key_pages_by_host": dict(CLOUD_API_KEY_PAGES_BY_HOST),
            "base_url_hints": dict(CLOUD_PROVIDER_BASE_URL_HINTS),
            "base_url_options": {
                provider: [dict(option) for option in options]
                for provider, options in CLOUD_PROVIDER_BASE_URL_OPTIONS.items()
            },
        }

    @app.get("/api/models/roles")
    def get_model_roles() -> dict[str, Any]:
        settings = load_settings()
        return {
            "roles": {
                role: _model_role_payload(settings, role)
                for role in (ROLE_TRANSLATION, ROLE_CLEANER, ROLE_IMAGE, ROLE_PDF_REVIEW)
            }
        }

    @app.put("/api/models/roles/{role}")
    def update_model_role(role: str, payload: ModelRoleUpdatePayload) -> dict[str, Any]:
        settings = load_settings()
        if role not in {ROLE_TRANSLATION, ROLE_CLEANER, ROLE_IMAGE, ROLE_PDF_REVIEW}:
            raise HTTPException(404, "未找到该模型用途，请刷新页面后重试。")
        before_signatures = _effective_role_signatures(settings)
        before_throughput_keys = throughput_profile_keys(settings)
        changed = False
        # 用户在面板上现选的跟随组合按严格规则判：读旧配置时不合法的跟随会静默降级为
        # 独立配置，那是为了让老设置文件还能打开；这里再降级就成了「存了别的、还不吭声」。
        _reject_illegal_follow_choice(role, payload.source_role)
        owner = model_role_owner(settings, role)
        primary = owner.connections[0]
        requested_source = (
            payload.source_role if payload.source_role is not None else primary.source_role
        )
        requested_mode = (
            "follow" if requested_source != "independent" else
            payload.mode or ("cloud" if primary.connection_mode == "follow" else primary.connection_mode)
        )
        before_primary = primary.model_dump()
        try:
            update_role_connection(
                settings, role, primary.id,
                connection_mode=requested_mode,
                source_role=requested_source,
                provider=payload.provider,
                model=payload.model,
                base_url=payload.base_url,
                api_mode=payload.api_mode,
            )
        except ModelRoleConfigError as exc:
            raise HTTPException(422, str(exc)) from exc
        changed = primary.model_dump() != before_primary
        try:
            # A changed translation connection can leave a following review role
            # unable to do its job.  That no longer blocks this save: only the
            # connection being edited must suit its own role; the follower is
            # shown as unavailable on the panel (see model_role_config_problem).
            after_configs = validate_all_model_roles(
                settings, strict_connection=(role, primary.id)
            )
        except Exception as exc:
            raise HTTPException(422, str(exc)) from exc
        if changed:
            for candidate_role, effective in after_configs.items():
                if before_signatures.get(candidate_role) != model_config_signature(effective):
                    reset_model_role_availability(settings, candidate_role)
            carry_over_throughput_profiles(settings, before_throughput_keys)
        save_settings(settings)
        # Re-read: the pool is only re-synced when settings are constructed, so
        # the in-memory object still carries the pre-edit connection list.
        return _model_role_payload(load_settings(), role)

    def _role_or_404(role: str) -> str:
        if role not in {ROLE_TRANSLATION, ROLE_CLEANER, ROLE_IMAGE, ROLE_PDF_REVIEW}:
            raise HTTPException(404, "未找到该模型用途，请刷新页面后重试。")
        return role

    def _validate_connection_choice(settings: AppSettings, role: str, connection: ModelConnection) -> None:
        if connection.connection_mode == "follow":
            if connection.source_role == "independent":
                raise HTTPException(422, "跟随连接必须选择来源。")
            _reject_illegal_follow_choice(role, connection.source_role)
            if connection.source_role not in allowed_source_roles(role, settings):
                raise HTTPException(422, "跟随来源的主用连接也在跟随其他模型，请直接选择最终来源。")
        try:
            validate_all_model_roles(settings, strict_connection=(role, connection.id))
        except Exception as exc:
            raise HTTPException(422, str(exc)) from exc

    def _requested_connection_mode(payload: ConnectionUpsertPayload) -> str | None:
        mode = payload.connection_mode
        source = payload.source_role
        if mode is None and source is not None:
            return "cloud" if source == "independent" else "follow"
        if mode == "follow" and source == "independent":
            raise HTTPException(422, "跟随连接必须选择来源。")
        if mode in {"cloud", "local"} and source not in {None, "independent"}:
            raise HTTPException(422, "独立连接不能同时指定跟随来源。")
        return mode

    @app.post("/api/models/roles/{role}/connections")
    def create_role_connection(
        role: str,
        payload: ConnectionUpsertPayload,
    ) -> dict[str, Any]:
        _role_or_404(role)
        settings = load_settings()
        requested_mode = _requested_connection_mode(payload) or "cloud"
        try:
            connection = add_role_connection(
                settings,
                role,
                label=payload.label or "",
                provider=payload.provider or "",
                model=payload.model or "",
                base_url=payload.base_url or "",
                api_mode=payload.api_mode or "auto",
                connection_mode=requested_mode,
                source_role=payload.source_role or "independent",
            )
        except ModelRoleConfigError as exc:
            raise HTTPException(422, str(exc)) from exc
        _validate_connection_choice(settings, role, connection)
        save_settings(settings)
        if payload.api_key:
            save_connection_key(connection.id, payload.api_key)
        return _model_role_payload(load_settings(), role)

    @app.put("/api/models/roles/{role}/connections/{connection_id}")
    def edit_role_connection(
        role: str,
        connection_id: str,
        payload: ConnectionUpsertPayload,
    ) -> dict[str, Any]:
        _role_or_404(role)
        settings = load_settings()
        requested_mode = _requested_connection_mode(payload)
        before_throughput_keys = throughput_profile_keys(settings)
        try:
            connection = update_role_connection(
                settings,
                role,
                connection_id,
                label=payload.label,
                provider=payload.provider,
                model=payload.model,
                base_url=payload.base_url,
                api_mode=payload.api_mode,
                connection_mode=requested_mode,
                source_role=payload.source_role,
                thinking=payload.thinking,
            )
        except ModelRoleConfigError as exc:
            raise HTTPException(422, str(exc)) from exc
        _validate_connection_choice(settings, role, connection)
        # An empty string means "leave the stored key alone", matching the
        # placeholder shown in the panel; only a non-empty value replaces it.
        if payload.api_key:
            reset_role_connection_availability(
                settings,
                role,
                connection_id,
                message="API 密钥已变化，请重新测试这条连接。",
            )
        carry_over_throughput_profiles(settings, before_throughput_keys)
        save_settings(settings)
        if payload.api_key:
            save_connection_key(connection_id, payload.api_key)
        return _model_role_payload(load_settings(), role)

    @app.delete("/api/models/roles/{role}/connections/{connection_id}")
    def drop_role_connection(role: str, connection_id: str) -> dict[str, Any]:
        _role_or_404(role)
        settings = load_settings()
        before_throughput_keys = throughput_profile_keys(settings)
        try:
            remove_role_connection(settings, role, connection_id)
            # 删除只查结构（跟随链、循环），不查能力：删掉一条连接永远应该能做到，
            # 顶上来的那条用不了的话，面板会把它标成「不可用」。
            validate_all_model_roles(settings)
        except ModelRoleConfigError as exc:
            raise HTTPException(422, str(exc)) from exc
        # A follower of this role's primary now resolves to the next entry's
        # endpoint; keep its tuning instead of dropping it to the recommendation.
        carry_over_throughput_profiles(settings, before_throughput_keys)
        save_settings(settings)
        delete_connection_key(connection_id)
        return _model_role_payload(load_settings(), role)

    @app.post("/api/models/roles/{role}/connections/reorder")
    def sort_role_connections(
        role: str,
        payload: ConnectionReorderPayload,
    ) -> dict[str, Any]:
        _role_or_404(role)
        settings = load_settings()
        before_throughput_keys = throughput_profile_keys(settings)
        try:
            reorder_role_connections(settings, role, list(payload.ordered_ids))
            # 设为主用是用户对这一条连接的明确选择，按它自己的能力把关。
            validate_all_model_roles(
                settings,
                strict_connection=(role, list_role_connections(settings, role)[0].id),
            )
        except ModelRoleConfigError as exc:
            raise HTTPException(422, str(exc)) from exc
        carry_over_throughput_profiles(settings, before_throughput_keys)
        save_settings(settings)
        return _model_role_payload(load_settings(), role)

    # ``text`` / ``image`` / ``pdf-review`` used to be three literal routes
    # declared ahead of ``/connectivity/{role}``.  Declaration order made them
    # win — and one of them collided with a real role key: the panel calls the
    # PDF 翻译模型 role ``image`` (ui/src/views/settings.ts), so every test of
    # that role landed on a handler that took no payload, dropped the panel's
    # ``connection_id`` and dialled the primary instead.  A non-primary
    # connection could never be tested, and its verdict silently overwrote the
    # primary's.  The parameterised route already accepts all three spellings
    # as aliases below, so the literals are gone rather than reordered.
    @app.post("/api/models/connectivity/{role}")
    def check_model_role_connectivity(
        role: str,
        payload: ModelConnectivityPayload | None = None,
    ) -> dict[str, Any]:
        """Test one role's connection and record the verdict on that entry.

        The panel sends the connection it is showing.  Without it the test
        always dialled the primary, so selecting a second connection and
        pressing 测试连接 reported on a configuration nobody was looking at.
        """
        settings = load_settings()
        connection_id = str(getattr(payload, "connection_id", "") or "").strip()
        role = {
            "text": ROLE_TRANSLATION,
            "image": ROLE_IMAGE,
            "pdf-review": ROLE_PDF_REVIEW,
        }.get(role, role)
        if role not in {ROLE_TRANSLATION, ROLE_CLEANER, ROLE_IMAGE, ROLE_PDF_REVIEW}:
            raise HTTPException(404, "未找到该模型用途，请刷新页面后重试。")
        try:
            config = resolve_effective_model_config(
                settings,
                role,
                connection_id=connection_id,
            )
            # 用 validate_model_capability 而不是裸 provider_supports_capability：
            # 后者的白名单只列云端服务商，本地运行器（ollama / lm_studio）永远不在
            # 里面，于是「本地模型 → 测试连接」一律 422「服务商 ollama 不支持 text
            # 能力」——用户根本没法从面板验证本机运行器是否连得上。本地模式的能力
            # 规则（只支持 text）由 validate_model_capability 统一裁定。
            validate_model_capability(config)
            if role == ROLE_TRANSLATION:
                result = check_connectivity(settings, connection_id=connection_id)
            elif role == ROLE_CLEANER:
                result = check_connectivity(
                    settings,
                    role=ROLE_CLEANER,
                    connection_id=connection_id,
                )
            elif role == ROLE_IMAGE:
                result = check_image_generation_connectivity(
                    settings,
                    connection_id=connection_id,
                )
            else:
                result = check_pdf_review_connectivity(
                    settings,
                    connection_id=connection_id,
                )
        except Exception as exc:
            raise HTTPException(422, str(exc)) from exc
        # 审核测试已判定是模型不能看图时，原因写的是换模型，不再引去查地址。
        hinted = (
            result
            if role == ROLE_PDF_REVIEW and REVIEW_MODEL_ADVICE in str(getattr(result, "message", ""))
            else with_connection_failure_hint(config.provider, result)
        )
        if hinted is not result:
            # 连接卡片上的「不可用」原因也带上这句，不只是一闪而过的提示条。
            amend_model_role_availability_message(
                settings, role, hinted.message, connection_id=connection_id,
            )
        save_settings(settings)
        return _json_safe(hinted)

    @app.post("/api/models/catalog/{role}")
    def fetch_saved_role_models(
        role: str,
        payload: ModelCatalogRefreshPayload | None = None,
    ) -> dict[str, Any]:
        """Fetch one role's session-only directory from saved effective config.

        The route intentionally receives no provider, base URL, model, or key
        draft values.  A directory is a suggestion for a *saved* connection;
        the model name remains manually editable and catalog success never
        counts as an ability test.
        """
        settings = load_settings()
        config = _model_config_or_422(
            settings,
            role,
            str(getattr(payload, "connection_id", "") or "").strip(),
        )
        if payload is not None and payload.refresh:
            from core.model_catalog import clear_model_catalog_cache

            clear_model_catalog_cache()
        result = fetch_openai_compatible_models(
            provider=config.provider,
            api_key=config.api_key,
            base_url=config.base_url,
            capability=config.capability,
        )
        return _json_safe(with_connection_failure_hint(config.provider, result))

    @app.post("/api/models/fetch")
    def fetch_models(request: ModelFetchRequest) -> dict[str, Any]:
        if request.refresh:
            from core.model_catalog import clear_model_catalog_cache

            clear_model_catalog_cache()
        api_key = request.api_key or get_key(request.provider, request.base_url)
        result = fetch_openai_compatible_models(
            provider=request.provider,
            api_key=api_key,
            base_url=request.base_url,
        )
        return _json_safe(result)

    # 速率按连接取：面板上选中第二条连接时调的是那条连接自己的速率。不带
    # connection_id 时仍是主用连接，旧调用方不受影响。
    @app.get("/api/models/throughput/{role}")
    def get_throughput(role: str, connection_id: str = "") -> dict[str, Any]:
        settings = load_settings()
        config = _model_config_or_422(settings, role, connection_id, check_capability=False)
        throughput = get_model_throughput(settings, config)
        return {
            "profile_key": throughput.profile_key,
            "batch_size": throughput.batch_size,
            "concurrency": throughput.concurrency,
            "batch_size_bounds": None if settings.engine.concurrency_unlocked else batch_size_bounds(config),
            "concurrency_bounds": None if settings.engine.concurrency_unlocked else concurrency_bounds(config),
        }

    @app.post("/api/models/throughput/unlock")
    def unlock_throughput(payload: ThroughputUnlockPayload) -> dict[str, Any]:
        if not is_valid_concurrency_unlock_code(payload.code.strip()):
            raise HTTPException(422, "解锁口令不正确。")
        settings = load_settings()
        settings.engine.concurrency_unlocked = True
        settings.engine.concurrency_unlock_code = payload.code.strip()
        save_settings(settings)
        return {"unlocked": True}

    @app.put("/api/models/throughput/{role}")
    def put_throughput(role: str, payload: ThroughputPayload, connection_id: str = "") -> dict[str, Any]:
        settings = load_settings()
        config = _model_config_or_422(settings, role, connection_id, check_capability=False)
        try:
            throughput = set_model_throughput(settings, config, batch_size=payload.batch_size, concurrency=payload.concurrency)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        save_settings(settings)
        return {
            "profile_key": throughput.profile_key,
            "batch_size": throughput.batch_size,
            "concurrency": throughput.concurrency,
            "batch_size_bounds": None if settings.engine.concurrency_unlocked else batch_size_bounds(config),
            "concurrency_bounds": None if settings.engine.concurrency_unlocked else concurrency_bounds(config),
        }

    @app.delete("/api/models/throughput/{role}")
    def reset_throughput(role: str, connection_id: str = "") -> dict[str, Any]:
        """Restore one role/model's recommended throughput profile."""
        settings = load_settings()
        config = _model_config_or_422(settings, role, connection_id, check_capability=False)
        throughput = reset_model_throughput(settings, config)
        save_settings(settings)
        return {
            "profile_key": throughput.profile_key,
            "batch_size": throughput.batch_size,
            "concurrency": throughput.concurrency,
            "batch_size_bounds": None if settings.engine.concurrency_unlocked else batch_size_bounds(config),
            "concurrency_bounds": None if settings.engine.concurrency_unlocked else concurrency_bounds(config),
        }
