"""FastAPI application exposing the existing Translator core over loopback HTTP."""

from __future__ import annotations

import os

from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, Header, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, model_validator

from api.task_manager import (
    TaskConflictError,
    TaskInputError,
    TaskNotFoundError,
    TaskOptions,
    TranslationTaskManager,
)
from core import diagnostics, maintenance, tm_manager
from core.language_preflight import (
    build_language_preflight_prompt,
    extract_language_probe_texts,
    parse_preflight_languages,
)
from core.language_registry import (
    CustomTargetLang,
    append_custom_target_lang,
    is_custom_target_lang,
    normalize_custom_target_langs,
    get_language_catalog,
    get_default_source_selection,
    get_default_target_lang,
    get_source_language_options,
    get_target_language_options,
    get_tm_language_pairs,
    remove_custom_target_lang,
    update_custom_target_lang_display,
)
from core.connectivity_check import check_connectivity
from core.file_scanner import scan_excel_sources
from core.image_generation import check_image_generation_connectivity
from core.model_catalog import fetch_openai_compatible_models
from core.model_config import (
    apply_model_config_import,
    build_model_config_export_payload,
    parse_model_config_import,
)
from core.model_roles import (
    ROLE_CLEANER,
    ROLE_IMAGE,
    ROLE_PDF_REVIEW,
    ROLE_TRANSLATION,
    LOCAL_CAPABLE_CAPABILITIES,
    EffectiveModelConfig,
    ModelRoleConfigError,
    add_role_connection,
    allowed_source_roles,
    list_effective_role_connections,
    model_config_signature,
    model_role_owner,
    pool_role,
    remove_role_connection,
    reorder_role_connections,
    role_label,
    update_role_connection,
    provider_supports_capability,
    reset_model_role_availability,
    resolve_effective_model_config,
    validate_all_model_roles,
)
from core.model_throughput import (
    batch_size_bounds,
    concurrency_bounds,
    get_model_throughput,
    reset_model_throughput,
    set_model_throughput,
)
from core.pdf_image_translation import scan_pdf_sources
from core.pdf_review import check_pdf_review_connectivity
from core.tm_cleaner import CleanSuggestion, apply_suggestions
from core.word_document import scan_word_sources
from config import DOMAIN_PRESETS
from settings import (
    AppSettings,
    ModelConnection,
    SettingsSchemaError,
    delete_connection_key,
    delete_key,
    get_connection_scoped_key,
    get_key,
    load_keys,
    load_settings,
    mask_api_key,
    parse_api_key_scope,
    save_connection_key,
    save_key,
    save_settings,
    set_cloud_provider_config,
)


class ApiKeyPayload(BaseModel):
    api_key: str = Field(default="", max_length=16_384)
    base_url: str = Field(default="", max_length=2_048)


class ScanRequest(BaseModel):
    path: str = Field(min_length=1)
    surface: Literal["excel", "word", "pdf"]
    include_images: bool = False


class TaskStartRequest(BaseModel):
    source_path: str = ""
    surface: Literal["excel", "word", "pdf", "tm_clean"]
    selected_paths: list[str] = Field(default_factory=list)
    untranslated_only: bool = False
    protect_scheme_cover: bool = False
    allow_xls_fallback: bool = False
    allow_doc_fallback: bool = False
    include_images: bool = False
    source_lang: str | None = None
    target_lang: str | None = None
    allow_known_review_failure: bool = False
    lang_pair: str | None = None
    confirmation_token: str | None = None

    @model_validator(mode="after")
    def _require_source_path(self) -> "TaskStartRequest":
        # An empty path would resolve to the process cwd and trigger a
        # recursive scan there. Only tm_clean runs without a source path.
        if self.surface != "tm_clean" and not self.source_path.strip():
            raise ValueError("source_path is required for document tasks")
        return self


class TmEntryPayload(BaseModel):
    source_text: str = Field(min_length=1)
    target_text: str = Field(min_length=1)
    lang_pair: str = Field(min_length=3)
    sync_reverse: bool = False


class TmEntryUpdatePayload(BaseModel):
    source_text: str = Field(min_length=1)
    target_text: str = Field(min_length=1)
    sync_reverse: bool = False


class TmPinPayload(BaseModel):
    pinned: bool = True


class TmBulkPinPayload(TmPinPayload):
    ids: list[int] = Field(min_length=1)


class TmBulkDeletePayload(BaseModel):
    ids: list[int] = Field(min_length=1)


class TmImportPayload(BaseModel):
    lang_pair: str = Field(min_length=3)
    mode: Literal["skip", "overwrite", "keep_both"] = "skip"
    entries: list[dict[str, Any]]
    sync_reverse: bool = False


class TmFullImportPayload(BaseModel):
    format_version: Literal["tm-full-v1"]
    custom_target_langs: list[dict[str, Any]] = Field(default_factory=list)
    entries: list[dict[str, Any]] = Field(default_factory=list)
    conflict_candidates: list[dict[str, Any]] = Field(default_factory=list)
    mode: Literal["skip", "overwrite", "keep_both"] = "skip"
    code_map: dict[str, str] = Field(default_factory=dict)
    sync_reverse: bool = False


class TmSuggestionPayload(BaseModel):
    entry_id: int
    source_text: str = ""
    old_target: str = ""
    new_target: str = Field(min_length=1)
    accepted: bool = True


class TmApplySuggestionsPayload(BaseModel):
    suggestions: list[TmSuggestionPayload]
    auto_pin: bool = False
    sync_reverse: bool = False


class CustomTargetLanguagePayload(BaseModel):
    name: str = Field(min_length=1, max_length=32)
    description: str = Field(default="", max_length=2_000)


class LanguagePreflightRequest(BaseModel):
    file_id: str = Field(min_length=1)
    texts: list[str] = Field(default_factory=list)
    target_lang: str = Field(min_length=2)
    detected_languages: list[str] | None = None


class TmCleanRequest(BaseModel):
    lang_pair: str = Field(min_length=3)
    confirmation_token: str | None = None


class ModelFetchRequest(BaseModel):
    provider: str = Field(min_length=1)
    base_url: str = ""
    api_key: str = ""
    refresh: bool = False


class ModelCatalogRefreshPayload(BaseModel):
    refresh: bool = False


class ModelRoleUpdatePayload(BaseModel):
    source_role: str | None = None
    mode: Literal["cloud", "local"] | None = None
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None


class ConnectionUpsertPayload(BaseModel):
    label: str | None = None
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None


class ConnectionReorderPayload(BaseModel):
    ordered_ids: list[str]


class ModelRoleTestPayload(BaseModel):
    role: str


class DomainSettingsPayload(BaseModel):
    preset: str = "同步工程场景"
    custom_prompt: str = ""
    prompt_overrides: dict[str, str] = Field(default_factory=dict)
    name_overrides: dict[str, str] = Field(default_factory=dict)


class ThroughputPayload(BaseModel):
    batch_size: int | None = None
    concurrency: int | None = None


class UpdatePreferencesRequest(BaseModel):
    notifications_paused: bool | None = None
    ignored_release_version: str | None = Field(default=None, max_length=64)
    quick_start_completed: bool | None = None


class MaintenanceClearRequest(BaseModel):
    category: Literal["task_history", "logs", "diagnostics", "keys", "settings", "tm", "workspaces"]
    confirmation: bool = False
    lang_pair: str | None = Field(default=None, max_length=128)


class FullResetRequest(BaseModel):
    confirmation: bool = False
    phrase: str = ""


def _allowed_origins() -> list[str]:
    """Origins the local UI may call from.

    Production is the Tauri webview only.  `tauri dev` serves the UI from a
    vite server instead, so debug builds pass that origin in explicitly; a
    release build never sets the variable, leaving the allowlist unchanged.
    """
    origins = ["tauri://localhost", "http://tauri.localhost"]
    dev_origin = str(os.environ.get("TRANSLATOR_DEV_ORIGIN") or "").strip()
    if dev_origin.startswith(("http://127.0.0.1:", "http://localhost:")):
        origins.append(dev_origin)
    return origins


def create_app(
    *,
    task_manager: TranslationTaskManager | None = None,
    auth_token: str = "",
) -> FastAPI:
    """Create a local API app; an empty token keeps in-process tests simple."""
    app = FastAPI(title="Translator Sidecar API", version="1")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_allowed_origins(),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.task_manager = task_manager or TranslationTaskManager()
    app.state.auth_token = str(auth_token or "")

    @app.middleware("http")
    async def require_loopback_token(request, call_next):
        expected = app.state.auth_token
        # CORS preflight requests never carry the custom token. They do not
        # invoke an API handler; CORSMiddleware answers them before a browser
        # may issue the authenticated request.
        if (
            expected
            and request.method != "OPTIONS"
            and request.headers.get("X-Translator-Token") != expected
        ):
            return Response(status_code=401)
        return await call_next(request)

    @app.exception_handler(TaskNotFoundError)
    async def task_not_found(_request, _exc):
        return _json_error(404, "Task not found.")

    @app.exception_handler(TaskConflictError)
    async def task_conflict(_request, exc):
        return _json_error(409, str(exc), reason=exc.reason)

    @app.exception_handler(TaskInputError)
    async def task_input_error(_request, exc):
        return _json_error(422, str(exc))

    @app.exception_handler(SettingsSchemaError)
    async def settings_schema_error(_request, _exc):
        return _json_error(
            409,
            "设置不是当前可写格式；请在维护页明确重置设置。",
            reason="settings_schema_requires_reset",
        )

    @app.exception_handler(maintenance.MaintenanceError)
    async def maintenance_error(_request, exc):
        return _json_error(422, str(exc), reason="maintenance_operation_rejected")

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "service": "translator-sidecar"}

    @app.get("/api/languages")
    def get_languages() -> dict[str, Any]:
        """Return the single language directory used by every selector."""
        settings = load_settings()
        custom = settings.custom_target_langs
        return {
            "languages": get_language_catalog(custom),
            "source_options": get_source_language_options(custom),
            "target_options": get_target_language_options(custom),
            "defaults": {
                "source_lang": get_default_source_selection(),
                "target_lang": get_default_target_lang(),
                "pdf_target_lang": settings.pdf.target_lang,
            },
            "recent_target_langs": list(settings.recent_target_langs),
        }

    @app.post("/api/languages/custom", status_code=201)
    def create_custom_language(payload: CustomTargetLanguagePayload) -> dict[str, Any]:
        settings = load_settings()
        try:
            custom, code = append_custom_target_lang(
                settings.custom_target_langs,
                payload.name,
                payload.description,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        settings.custom_target_langs = custom
        save_settings(settings)
        return {"code": code, "name": payload.name.strip(), "description": payload.description.strip()}

    @app.put("/api/languages/custom/{language_code}")
    def update_custom_language(
        language_code: str,
        payload: CustomTargetLanguagePayload,
    ) -> dict[str, Any]:
        settings = load_settings()
        existing = next(
            (entry for entry in settings.custom_target_langs if entry.code == language_code),
            None,
        )
        if existing is None:
            raise HTTPException(404, "自定义语言不存在。")
        try:
            settings.custom_target_langs = update_custom_target_lang_display(
                settings.custom_target_langs,
                language_code,
                payload.name,
                payload.description,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        save_settings(settings)
        updated = next(
            item for item in settings.custom_target_langs if item.code == language_code
        )
        return {
            "code": language_code,
            "name": updated.name,
            "description": updated.description,
        }

    @app.delete("/api/languages/custom/{language_code}", status_code=204)
    def delete_custom_language(language_code: str) -> Response:
        settings = load_settings()
        existing = next(
            (entry for entry in settings.custom_target_langs if entry.code == language_code),
            None,
        )
        if existing is None:
            raise HTTPException(404, "自定义语言不存在。")
        if hasattr(tm_manager, "count_entries_referencing_language") and tm_manager.count_entries_referencing_language(language_code):
            raise HTTPException(409, "该自定义语言仍被 TM 条目引用，请先导出或清空相关语言对。")
        settings.custom_target_langs = remove_custom_target_lang(
            settings.custom_target_langs,
            language_code,
        )
        if settings.target_lang == language_code:
            settings.target_lang = "en"
        if settings.excel_target_lang == language_code:
            settings.excel_target_lang = "en"
        if settings.word_target_lang == language_code:
            settings.word_target_lang = "en"
        if settings.pdf.target_lang == language_code:
            settings.pdf.target_lang = "zh"
        save_settings(settings)
        return Response(status_code=204)

    @app.post("/api/languages/preflight")
    def language_preflight(payload: LanguagePreflightRequest) -> dict[str, Any]:
        samples = extract_language_probe_texts(payload.texts)
        detected = parse_preflight_languages(payload.detected_languages or [])
        return {
            "file_id": payload.file_id,
            "candidate_count": len(samples),
            "requested": bool(samples),
            "source_langs": list(detected),
            "tm_pairs": get_tm_language_pairs(detected, payload.target_lang),
            "prompt": build_language_preflight_prompt(samples) if samples else "",
        }

    @app.get("/api/tm/language-pairs")
    def get_tm_language_pairs_catalog() -> dict[str, Any]:
        settings = load_settings()
        source_options = [
            option for option in get_source_language_options(settings.custom_target_langs)
            if option.get("code") != "auto"
        ]
        target_options = get_target_language_options(settings.custom_target_langs)
        return {
            "source_options": source_options,
            "target_options": target_options,
            "selected": {
                "source_lang": settings.tm_source_lang,
                "target_lang": settings.tm_target_lang,
            },
            "recent": list(settings.recent_tm_lang_pairs),
        }

    @app.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        return load_settings().model_dump(mode="json")

    @app.put("/api/settings")
    def put_settings(payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise HTTPException(422, "Settings payload must be a JSON object.")
        before_settings = load_settings()
        before_signatures: dict[str, str] = {}
        for role in (ROLE_TRANSLATION, ROLE_CLEANER, ROLE_IMAGE, ROLE_PDF_REVIEW):
            try:
                before_signatures[role] = model_config_signature(
                    resolve_effective_model_config(before_settings, role)
                )
            except Exception:
                continue
        current = before_settings.model_dump(mode="json")
        merged = _deep_merge(current, payload)
        try:
            settings = AppSettings.model_validate(merged)
        except Exception as exc:
            raise HTTPException(422, str(exc)) from exc
        for role in (ROLE_TRANSLATION, ROLE_CLEANER, ROLE_IMAGE, ROLE_PDF_REVIEW):
            try:
                resolve_effective_model_config(settings, role)
            except Exception as exc:
                raise HTTPException(422, str(exc)) from exc
        for role in (ROLE_TRANSLATION, ROLE_CLEANER, ROLE_IMAGE, ROLE_PDF_REVIEW):
            try:
                changed = before_signatures.get(role) != model_config_signature(
                    resolve_effective_model_config(settings, role)
                )
            except Exception:
                changed = True
            if not changed:
                continue
            owner = settings.engine if role == ROLE_TRANSLATION else {
                ROLE_CLEANER: settings.cleaner_model_role,
                ROLE_IMAGE: settings.image_model_role,
                ROLE_PDF_REVIEW: settings.pdf_review_model_role,
            }[role]
            owner.availability_status = "unknown"
            owner.availability_message = "当前配置尚未测试。"
            owner.availability_signature = ""
            owner.availability_checked_at = ""
        save_settings(settings)
        return settings.model_dump(mode="json")

    @app.get("/api/domains/{surface}")
    def get_domain_settings(surface: str) -> dict[str, Any]:
        normalized = str(surface or "").strip().lower()
        if normalized not in {"excel", "word"}:
            raise HTTPException(404, "Unknown translation surface.")
        settings = load_settings()
        prefix = f"{normalized}_"
        return {
            "surface": normalized,
            "presets": sorted(DOMAIN_PRESETS),
            "preset": getattr(settings, f"{prefix}domain_preset"),
            "custom_prompt": getattr(settings, f"{prefix}custom_prompt"),
            "prompt_overrides": getattr(settings, f"{prefix}domain_prompt_overrides"),
            "name_overrides": getattr(settings, f"{prefix}domain_name_overrides"),
        }

    @app.put("/api/domains/{surface}")
    def put_domain_settings(surface: str, payload: DomainSettingsPayload) -> dict[str, Any]:
        normalized = str(surface or "").strip().lower()
        if normalized not in {"excel", "word"}:
            raise HTTPException(404, "Unknown translation surface.")
        preset = str(payload.preset or "").strip()
        if preset not in DOMAIN_PRESETS:
            raise HTTPException(422, "未知专业领域预设。")
        if preset == "自定义" and not str(payload.custom_prompt or "").strip():
            raise HTTPException(422, "自定义领域必须填写完整 Prompt。")
        settings = load_settings()
        prefix = f"{normalized}_"
        setattr(settings, f"{prefix}domain_preset", preset)
        setattr(settings, f"{prefix}custom_prompt", str(payload.custom_prompt or ""))
        setattr(settings, f"{prefix}domain_prompt_overrides", dict(payload.prompt_overrides))
        setattr(settings, f"{prefix}domain_name_overrides", dict(payload.name_overrides))
        save_settings(settings)
        return get_domain_settings(normalized)

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
        save_key(provider, payload.api_key, payload.base_url)
        _reset_roles_with_changed_effective_signature(
            settings,
            before_signatures,
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
        delete_key(provider, base_url)
        _reset_roles_with_changed_effective_signature(
            settings,
            before_signatures,
            message="API Key 已变化，请重新测试当前配置。",
        )
        save_settings(settings)
        return Response(status_code=204)

    @app.post("/api/sources/scan")
    def scan_sources(request: ScanRequest) -> dict[str, Any]:
        root = Path(request.path).expanduser()
        if request.surface == "excel":
            result = scan_excel_sources(root)
            payload = {
                "items": [_json_safe(item) for item in result.items],
                "skipped": [_json_safe(item) for item in result.skipped],
                "summary": result.summary,
                "risk": result.risk,
            }
            # ``result`` is a stable grouped alias for callers that consume
            # one typed scan object; top-level fields keep the Phase 1 route
            # backward compatible.
            payload["result"] = dict(payload)
            return payload
        elif request.surface == "word":
            result = scan_word_sources(root)
            payload = {
                "items": [_json_safe(item) for item in result.items],
                "skipped": [_json_safe(item) for item in result.skipped],
                "summary": result.summary,
                "risk": result.risk,
            }
            payload["result"] = dict(payload)
            return payload
        else:
            result = scan_pdf_sources(root, include_images=request.include_images)
            payload = {
                "items": [_json_safe(item) for item in result.items],
                "skipped": [_json_safe(item) for item in result.skipped],
                "summary": result.summary,
                "risk": result.risk,
            }
            payload["result"] = dict(payload)
            return payload

    def _task_options(request: TaskStartRequest) -> TaskOptions:
        return TaskOptions(
            untranslated_only=request.untranslated_only,
            protect_scheme_cover=request.protect_scheme_cover,
            allow_xls_fallback=request.allow_xls_fallback,
            allow_doc_fallback=request.allow_doc_fallback,
            include_images=request.include_images,
            source_lang=request.source_lang,
            target_lang=request.target_lang,
            allow_known_review_failure=request.allow_known_review_failure,
            lang_pair=request.lang_pair,
        )

    @app.post("/api/tasks/preflight")
    def preflight_task(request: TaskStartRequest) -> dict[str, Any]:
        return app.state.task_manager.preflight_task(
            surface=request.surface,
            source_path=request.source_path,
            selected_paths=request.selected_paths,
            options=_task_options(request),
        )

    @app.post("/api/tasks", status_code=202)
    def start_task(request: TaskStartRequest) -> dict[str, Any]:
        return app.state.task_manager.start_task(
            surface=request.surface,
            source_path=request.source_path,
            selected_paths=request.selected_paths,
            options=_task_options(request),
            confirmation_token=request.confirmation_token,
        )

    @app.get("/api/tasks")
    def list_tasks() -> dict[str, Any]:
        return app.state.task_manager.list_tasks()

    # Static task-center routes must be registered before the parameterized
    # task-id routes below; Starlette matches routes in declaration order.
    @app.get("/api/tasks/locks/current")
    def current_task_locks() -> dict[str, Any]:
        return {"reservations": app.state.task_manager.reservations()}

    @app.get("/api/tasks/resources")
    def task_resource_groups() -> dict[str, Any]:
        return {"groups": app.state.task_manager.resource_groups()}

    @app.delete("/api/tasks/history")
    def clear_task_history() -> dict[str, Any]:
        _require_no_active_tasks(app, category="task_history")
        return {
            "category": "task_history",
            "removed_count": app.state.task_manager.clear_history(),
            "outputs_affected": False,
            "restart_required": False,
        }

    @app.get("/api/tasks/{task_id}/results")
    def get_task_results(task_id: str) -> dict[str, Any]:
        return app.state.task_manager.task_results(task_id)

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: str) -> dict[str, Any]:
        return app.state.task_manager.task_status(task_id)

    @app.post("/api/tasks/{task_id}/stop")
    def stop_task(task_id: str) -> dict[str, Any]:
        return app.state.task_manager.stop_task(task_id)

    @app.post("/api/tasks/{task_id}/pause")
    def pause_task(task_id: str) -> dict[str, Any]:
        return app.state.task_manager.pause_task(task_id)

    @app.post("/api/tasks/{task_id}/resume")
    def resume_task(task_id: str) -> dict[str, Any]:
        return app.state.task_manager.resume_task(task_id)

    @app.post("/api/tasks/{task_id}/end-paused")
    def end_paused_task(task_id: str) -> dict[str, Any]:
        return app.state.task_manager.end_paused_task(task_id)

    @app.get("/api/tasks/{task_id}/events")
    def task_events(
        task_id: str,
        last_event_id: int = 0,
        last_event_header: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        after = last_event_id
        if last_event_header:
            try:
                after = int(last_event_header)
            except ValueError:
                pass
        return StreamingResponse(
            app.state.task_manager.iter_sse(task_id, after_event_id=after),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/tm/entries")
    def list_tm_entries(
        lang_pair: str,
        keyword: str = "",
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        tm_manager.init_db()
        rows, total = tm_manager.search_entries(
            lang_pair,
            keyword,
            page=max(1, page),
            page_size=min(max(1, page_size), 200),
        )
        return {
            "entries": rows,
            "total": total,
            "stats": tm_manager.get_stats(lang_pair),
            "pin_count": tm_manager.get_pin_count(lang_pair, keyword),
        }

    @app.post("/api/tm/entries", status_code=201)
    def create_tm_entry(payload: TmEntryPayload) -> dict[str, bool]:
        tm_manager.init_db()
        return {
            "changed": tm_manager.insert_manual_entry(
                payload.source_text,
                payload.target_text,
                payload.lang_pair,
                sync_reverse=payload.sync_reverse,
            )
        }

    @app.put("/api/tm/entries/{entry_id}")
    def update_tm_entry(entry_id: int, payload: TmEntryUpdatePayload) -> dict[str, bool]:
        tm_manager.init_db()
        changed = tm_manager.update_entry_full(
            entry_id,
            payload.source_text,
            payload.target_text,
            sync_reverse=payload.sync_reverse,
        )
        if not changed:
            raise HTTPException(409, "Entry is missing or conflicts with an existing source.")
        return {"changed": True}

    @app.delete("/api/tm/entries/{entry_id}")
    def delete_tm_entry(entry_id: int) -> dict[str, bool]:
        tm_manager.init_db()
        deleted = tm_manager.delete_entry(entry_id)
        if not deleted:
            raise HTTPException(409, "固定或不存在的词条不能删除；请先解除固定。")
        return {"deleted": True}

    @app.post("/api/tm/entries/{entry_id}/pin")
    def pin_tm_entry(entry_id: int, payload: TmPinPayload) -> dict[str, bool]:
        tm_manager.init_db()
        tm_manager.pin_entry(entry_id, payload.pinned)
        return {"changed": True}

    @app.post("/api/tm/entries/bulk/pin")
    def bulk_pin_tm_entries(payload: TmBulkPinPayload) -> dict[str, int]:
        tm_manager.init_db()
        tm_manager.bulk_pin_entries(payload.ids, payload.pinned)
        return {"count": len(payload.ids)}

    @app.post("/api/tm/entries/bulk/delete")
    def bulk_delete_tm_entries(payload: TmBulkDeletePayload) -> dict[str, int]:
        tm_manager.init_db()
        return tm_manager.delete_entries(payload.ids)

    @app.get("/api/tm/export")
    def export_tm_entries(lang_pair: str) -> dict[str, Any]:
        tm_manager.init_db()
        return {"lang_pair": lang_pair, "entries": tm_manager.get_all_entries_for_export(lang_pair)}

    @app.get("/api/tm/export/full")
    def export_full_tm() -> dict[str, Any]:
        tm_manager.init_db()
        settings = load_settings()
        return tm_manager.get_full_export(settings.custom_target_langs)

    @app.post("/api/tm/import")
    def import_tm_entries(payload: TmImportPayload) -> dict[str, int]:
        tm_manager.init_db()
        return tm_manager.import_entries(
            payload.entries,
            payload.lang_pair,
            payload.mode,
            sync_reverse=payload.sync_reverse,
        )

    @app.post("/api/tm/import/full")
    def import_full_tm(payload: TmFullImportPayload) -> dict[str, int]:
        tm_manager.init_db()
        settings = load_settings()
        current = normalize_custom_target_langs(settings.custom_target_langs)
        by_code = {entry.code: entry for entry in current}
        code_map = {str(key).strip(): str(value).strip() for key, value in payload.code_map.items()}

        for raw in payload.custom_target_langs:
            incoming = CustomTargetLang.model_validate(raw)
            source_code = incoming.code.strip()
            if not source_code:
                raise HTTPException(422, "完整 TM 备份中的自定义语言缺少内部代码。")
            target_code = code_map.get(source_code, source_code)
            if not is_custom_target_lang(source_code) or not is_custom_target_lang(target_code):
                raise HTTPException(
                    422,
                    "完整 TM 备份中的自定义语言代码必须是有效的 x-custom-* 内部代码。",
                )
            existing = by_code.get(target_code)
            if existing is not None:
                mapped_explicitly = source_code in code_map
                if not mapped_explicitly and (
                    existing.name != incoming.name or existing.description != incoming.description
                ):
                    raise HTTPException(
                        409,
                        f"自定义语言代码 {source_code} 已存在且定义不同；请提供 code_map 后重试。",
                    )
                continue
            if target_code != source_code:
                incoming = incoming.model_copy(update={"code": target_code})
            by_code[target_code] = incoming
            current.append(incoming)

        custom_codes = set(by_code)

        def remap_pair(pair: object) -> str:
            parsed = tm_manager.split_lang_pair(str(pair or ""))
            if parsed is None:
                raise HTTPException(422, f"无效的 TM 语言对：{pair}")
            source, target = parsed
            source = code_map.get(source, source)
            target = code_map.get(target, target)
            if source.startswith("x-custom-"):
                raise HTTPException(422, "自定义语言只能作为目标语言，不能恢复为 TM 源语言。")
            if target.startswith("x-custom-") and target not in custom_codes:
                raise HTTPException(422, f"TM 语言对引用了未定义的自定义目标语言：{target}")
            return f"{source}-{target}"

        grouped: dict[str, list[dict[str, Any]]] = {}
        for entry in payload.entries:
            mapped = dict(entry)
            mapped["lang_pair"] = remap_pair(mapped.get("lang_pair"))
            grouped.setdefault(mapped["lang_pair"], []).append(mapped)
        mapped_conflicts = []
        for candidate in payload.conflict_candidates:
            mapped = dict(candidate)
            mapped["lang_pair"] = remap_pair(mapped.get("lang_pair"))
            mapped_conflicts.append(mapped)

        settings.custom_target_langs = current
        save_settings(settings)
        inserted = skipped = duplicates = 0
        for pair, entries in grouped.items():
            result = tm_manager.import_entries(
                entries,
                pair,
                payload.mode,
                sync_reverse=payload.sync_reverse and not pair.split("-", 1)[1].startswith("x-custom-"),
                preserve_status=True,
            )
            inserted += result.get("inserted", 0)
            skipped += result.get("skipped", 0)
            duplicates += result.get("duplicates", 0)
        restored_conflicts = tm_manager.import_conflict_candidates(
            mapped_conflicts,
        )
        return {
            "inserted": inserted,
            "skipped": skipped,
            "duplicates": duplicates,
            "custom_languages": len(current),
            "conflicts": restored_conflicts,
        }

    @app.get("/api/tm/conflicts")
    def list_tm_conflicts(lang_pair: str | None = None) -> dict[str, Any]:
        tm_manager.init_db()
        return {"conflicts": tm_manager.list_conflict_candidates(lang_pair)}

    @app.post("/api/tm/conflicts/{candidate_id}/resolve")
    def resolve_tm_conflict(candidate_id: int, payload: dict[str, str]) -> dict[str, bool]:
        action = str(payload.get("action") or "").strip()
        try:
            resolved = tm_manager.resolve_conflict_candidate(candidate_id, action)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if not resolved:
            raise HTTPException(409, "冲突候选不存在、已处理或当前词条已发生变化。")
        return {"resolved": True}

    @app.get("/api/tm/clean/suggestions")
    def list_tm_clean_suggestions(lang_pair: str | None = None) -> dict[str, Any]:
        tm_manager.init_db()
        return {"suggestions": tm_manager.list_cleaning_suggestions(lang_pair)}

    @app.post("/api/tm/clean", status_code=202)
    def clean_tm_entries(payload: TmCleanRequest) -> dict[str, Any]:
        """Compatibility wrapper for the unified scheduled TM-clean task.

        The former synchronous endpoint could bypass resource risk checks.  It
        now has identical token semantics to the task-center route.
        """
        return app.state.task_manager.start_task(
            surface="tm_clean",
            source_path=payload.lang_pair,
            options=TaskOptions(lang_pair=payload.lang_pair),
            confirmation_token=payload.confirmation_token,
        )

    @app.post("/api/tm/clean/apply")
    def apply_tm_suggestions(payload: TmApplySuggestionsPayload) -> dict[str, int]:
        tm_manager.init_db()
        suggestions = [
            CleanSuggestion(
                entry_id=item.entry_id,
                source_text=item.source_text,
                old_target=item.old_target,
                new_target=item.new_target,
                accepted=item.accepted,
            )
            for item in payload.suggestions
        ]
        return {
            "applied": apply_suggestions(
                suggestions,
                auto_pin=payload.auto_pin,
                sync_reverse=payload.sync_reverse,
            )
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
            raise HTTPException(404, "Unknown model role.")
        before_signatures = _effective_role_signatures(settings)
        changed = False
        # All four roles now carry the same fields, so one branch serves them
        # all: translation just happens to keep its copy on ``engine``.
        owner = model_role_owner(settings, role)
        for field, value in (
            ("source_role", payload.source_role),
            ("mode", payload.mode),
        ):
            if value is not None and getattr(owner, field) != value:
                setattr(owner, field, value)
                changed = True
        local = owner.mode == "local"
        for field, value in (
            ("local_provider" if local else "cloud_provider", payload.provider),
            ("local_model" if local else "cloud_model", payload.model),
            ("local_base_url" if local else "cloud_base_url", payload.base_url),
        ):
            if value is not None and getattr(owner, field) != value:
                setattr(owner, field, value)
                changed = True
        if not local and owner.source_role == "independent":
            set_cloud_provider_config(
                owner,
                owner.cloud_provider,
                cloud_model=owner.cloud_model,
                cloud_base_url=owner.cloud_base_url,
            )
        try:
            # A changed translation connection can make a following image or
            # review role illegal.  Do not persist an invalid shared graph.
            after_configs = validate_all_model_roles(settings)
        except Exception as exc:
            raise HTTPException(422, str(exc)) from exc
        if changed:
            for candidate_role, effective in after_configs.items():
                if before_signatures.get(candidate_role) != model_config_signature(effective):
                    reset_model_role_availability(settings, candidate_role)
        save_settings(settings)
        # Re-read: the pool is only re-synced when settings are constructed, so
        # the in-memory object still carries the pre-edit connection list.
        return _model_role_payload(load_settings(), role)

    def _role_or_404(role: str) -> str:
        if role not in {ROLE_TRANSLATION, ROLE_CLEANER, ROLE_IMAGE, ROLE_PDF_REVIEW}:
            raise HTTPException(404, "Unknown model role.")
        return role

    def _own_pool_or_422(settings: AppSettings, role: str) -> None:
        """Reject pool edits on a role that is following another one.

        The panel shows the source's pool while following, so an edit here would
        either miss (ids belong to the source) or silently change a pool the user
        is not looking at.  Both are worse than saying which role owns it.
        """
        owner_role = pool_role(settings, role)
        if owner_role != role:
            raise HTTPException(
                422,
                f"{role_label(role)}正在跟随{role_label(owner_role)}，"
                f"连接列表属于{role_label(owner_role)}。"
                "请切换到该角色编辑，或先改为独立配置。",
            )

    @app.post("/api/models/roles/{role}/connections")
    def create_role_connection(
        role: str,
        payload: ConnectionUpsertPayload,
    ) -> dict[str, Any]:
        _role_or_404(role)
        settings = load_settings()
        _own_pool_or_422(settings, role)
        try:
            connection = add_role_connection(
                settings,
                role,
                label=payload.label or "",
                provider=payload.provider or "",
                model=payload.model or "",
                base_url=payload.base_url or "",
            )
        except ModelRoleConfigError as exc:
            raise HTTPException(422, str(exc)) from exc
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
        _own_pool_or_422(settings, role)
        try:
            update_role_connection(
                settings,
                role,
                connection_id,
                label=payload.label,
                provider=payload.provider,
                model=payload.model,
                base_url=payload.base_url,
            )
        except ModelRoleConfigError as exc:
            raise HTTPException(422, str(exc)) from exc
        save_settings(settings)
        # An empty string means "leave the stored key alone", matching the
        # placeholder shown in the panel; only a non-empty value replaces it.
        if payload.api_key:
            save_connection_key(connection_id, payload.api_key)
        return _model_role_payload(load_settings(), role)

    @app.delete("/api/models/roles/{role}/connections/{connection_id}")
    def drop_role_connection(role: str, connection_id: str) -> dict[str, Any]:
        _role_or_404(role)
        settings = load_settings()
        _own_pool_or_422(settings, role)
        try:
            remove_role_connection(settings, role, connection_id)
        except ModelRoleConfigError as exc:
            raise HTTPException(422, str(exc)) from exc
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
        _own_pool_or_422(settings, role)
        try:
            reorder_role_connections(settings, role, list(payload.ordered_ids))
        except ModelRoleConfigError as exc:
            raise HTTPException(422, str(exc)) from exc
        save_settings(settings)
        return _model_role_payload(load_settings(), role)

    @app.post("/api/models/connectivity/{role}")
    def check_model_role_connectivity(role: str) -> dict[str, Any]:
        settings = load_settings()
        role = {
            "text": ROLE_TRANSLATION,
            "image": ROLE_IMAGE,
            "pdf-review": ROLE_PDF_REVIEW,
        }.get(role, role)
        if role not in {ROLE_TRANSLATION, ROLE_CLEANER, ROLE_IMAGE, ROLE_PDF_REVIEW}:
            raise HTTPException(404, "Unknown model role.")
        try:
            config = resolve_effective_model_config(settings, role)
            if not provider_supports_capability(config.provider, config.capability):
                raise ValueError(f"服务商 {config.provider} 不支持 {config.capability} 能力。")
            if role == ROLE_TRANSLATION:
                result = check_connectivity(settings)
            elif role == ROLE_CLEANER:
                result = check_connectivity(settings, role=ROLE_CLEANER)
            elif role == ROLE_IMAGE:
                result = check_image_generation_connectivity(settings)
            else:
                result = check_pdf_review_connectivity(settings)
        except Exception as exc:
            raise HTTPException(422, str(exc)) from exc
        save_settings(settings)
        return _json_safe(result)

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
        config = _model_config_or_422(settings, role)
        if payload is not None and payload.refresh:
            from core.model_catalog import clear_model_catalog_cache

            clear_model_catalog_cache()
        result = fetch_openai_compatible_models(
            provider=config.provider,
            api_key=config.api_key,
            base_url=config.base_url,
        )
        return _json_safe(result)

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

    @app.get("/api/models/throughput/{role}")
    def get_throughput(role: str) -> dict[str, Any]:
        settings = load_settings()
        config = _model_config_or_422(settings, role)
        throughput = get_model_throughput(settings, config)
        return {
            "profile_key": throughput.profile_key,
            "batch_size": throughput.batch_size,
            "concurrency": throughput.concurrency,
            "batch_size_bounds": batch_size_bounds(config),
            "concurrency_bounds": concurrency_bounds(config),
        }

    @app.put("/api/models/throughput/{role}")
    def put_throughput(role: str, payload: ThroughputPayload) -> dict[str, Any]:
        settings = load_settings()
        config = _model_config_or_422(settings, role)
        throughput = set_model_throughput(
            settings,
            config,
            batch_size=payload.batch_size,
            concurrency=payload.concurrency,
        )
        save_settings(settings)
        return {
            "profile_key": throughput.profile_key,
            "batch_size": throughput.batch_size,
            "concurrency": throughput.concurrency,
        }

    @app.delete("/api/models/throughput/{role}")
    def reset_throughput(role: str) -> dict[str, Any]:
        """Restore one role/model's recommended throughput profile."""
        settings = load_settings()
        config = _model_config_or_422(settings, role)
        throughput = reset_model_throughput(settings, config)
        save_settings(settings)
        return {
            "profile_key": throughput.profile_key,
            "batch_size": throughput.batch_size,
            "concurrency": throughput.concurrency,
            "batch_size_bounds": batch_size_bounds(config),
            "concurrency_bounds": concurrency_bounds(config),
        }

    @app.post("/api/models/connectivity/text")
    def check_text_connectivity() -> dict[str, Any]:
        settings = load_settings()
        result = check_connectivity(settings)
        save_settings(settings)
        return _json_safe(result)

    @app.post("/api/models/connectivity/image")
    def check_image_connectivity() -> dict[str, Any]:
        settings = load_settings()
        result = check_image_generation_connectivity(settings)
        save_settings(settings)
        return _json_safe(result)

    @app.post("/api/models/connectivity/pdf-review")
    def check_review_connectivity() -> dict[str, Any]:
        settings = load_settings()
        result = check_pdf_review_connectivity(settings)
        save_settings(settings)
        return _json_safe(result)

    @app.get("/api/model-config/export")
    def export_model_config(
        include_api_key: bool = False,
        include_api_keys: bool | None = None,
        confirm_sensitive: bool = False,
    ) -> dict[str, Any]:
        if include_api_keys is not None:
            include_api_key = bool(include_api_keys)
        if include_api_key and not confirm_sensitive:
            raise HTTPException(422, "导出 API Key 前必须明确确认敏感配置导出。")
        return build_model_config_export_payload(
            load_settings(),
            include_api_key=include_api_key,
        )

    @app.post("/api/model-config/import/preview")
    def preview_model_config_import(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            imported = parse_model_config_import(payload)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        role_names = {
            "engine": ROLE_TRANSLATION,
            "cleaner_model_role": ROLE_CLEANER,
            "image_model_role": ROLE_IMAGE,
            "pdf_review_model_role": ROLE_PDF_REVIEW,
        }
        return {
            "version": 3,
            "roles": [
                {
                    "role": role_names.get(setting_key, setting_key),
                    "fields": sorted(values),
                }
                for setting_key, values in imported.model_config.items()
            ],
            "throughput_profile_count": len(imported.profile_throughputs)
            + len(imported.throughput_profiles),
            "api_key_count": len(imported.api_keys) + len(imported.scoped_api_keys),
        }

    @app.post("/api/model-config/import")
    def import_model_config(payload: dict[str, Any]) -> dict[str, Any]:
        throughput_errors: list[str] = []
        try:
            imported = parse_model_config_import(payload)
            settings = apply_model_config_import(
                load_settings(),
                imported,
                throughput_errors=throughput_errors,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        save_settings(settings)
        return {
            "settings": settings.model_dump(mode="json"),
            "imported_key_count": len(imported.api_keys) + len(imported.scoped_api_keys),
            # Roles whose imported batch_size/concurrency could not be applied;
            # the rest of the import still succeeded.
            "skipped_throughput_roles": throughput_errors,
        }

    @app.get("/api/updates/state")
    def update_state() -> dict[str, Any]:
        settings = load_settings()
        return _update_state_payload(settings)

    @app.get("/api/updates/check")
    def check_updates(mode: Literal["manual", "background"] = "manual") -> dict[str, Any]:
        settings = load_settings()
        if mode == "background":
            deferred = _background_update_deferred(settings)
            if deferred:
                return {
                    "ok": True,
                    "status": "deferred",
                    "message": "后台更新检查暂不执行。",
                    "reason": deferred,
                    **_update_state_payload(settings),
                }
        from core.update_checker import check_for_updates

        result = check_for_updates()
        if mode == "background":
            # The network check can take seconds; reload before stamping the
            # timestamp so settings saved meanwhile are not clobbered.
            settings = load_settings()
            settings.update.last_background_check_at = datetime.now(timezone.utc).isoformat()
            save_settings(settings)
        if result.status == "error":
            diagnostics.record_system_diagnostic(
                phase="update_check",
                error_code=result.diagnostic_code or "update_check_failed",
            )
        payload = _json_safe(result)
        if mode == "background":
            payload["notification_suppressed"] = _background_notice_suppressed(settings, payload)
        return payload

    @app.put("/api/updates/preferences")
    def update_preferences(payload: UpdatePreferencesRequest) -> dict[str, Any]:
        settings = load_settings()
        changed = payload.model_fields_set
        if "notifications_paused" in changed:
            settings.update.notifications_paused = bool(payload.notifications_paused)
        if "ignored_release_version" in changed:
            settings.update.ignored_release_version = str(
                payload.ignored_release_version or ""
            ).strip()
        if "quick_start_completed" in changed:
            settings.onboarding.quick_start_completed = bool(payload.quick_start_completed)
        save_settings(settings)
        return _update_state_payload(settings)

    @app.get("/api/maintenance/overview")
    def maintenance_overview() -> dict[str, Any]:
        return maintenance.data_overview(
            active_task_count=app.state.task_manager.active_task_count(),
        )

    @app.post("/api/maintenance/clear")
    def maintenance_clear(payload: MaintenanceClearRequest) -> dict[str, Any]:
        category = payload.category
        if category in {"task_history", "logs", "diagnostics", "keys", "tm"}:
            _require_no_active_tasks(app, category=category)
        if category in {"keys", "settings", "tm", "workspaces"} and not payload.confirmation:
            raise HTTPException(422, "此操作需要明确确认。")
        if category == "task_history":
            return {
                "category": "task_history",
                "removed_count": app.state.task_manager.clear_history(),
                "outputs_affected": False,
                "restart_required": False,
            }
        if category == "logs":
            return maintenance.clear_logs().as_dict()
        if category == "diagnostics":
            return maintenance.clear_diagnostics().as_dict()
        if category == "keys":
            return maintenance.clear_keys().as_dict()
        if category == "settings":
            return maintenance.reset_settings().as_dict()
        if category == "tm":
            return maintenance.clear_tm(lang_pair=payload.lang_pair).as_dict()
        if category == "workspaces":
            return maintenance.clear_owned_workspaces().as_dict()
        raise HTTPException(422, "不支持的维护类别。")

    @app.post("/api/maintenance/reset-full")
    def reset_full_local_data(payload: FullResetRequest) -> dict[str, Any]:
        _require_no_active_tasks(app, category="reset_full")
        if not payload.confirmation or payload.phrase != "RESET":
            raise HTTPException(422, "完整重置需要勾选确认并输入 RESET。")
        return maintenance.reset_all_local_data().as_dict()

    @app.get("/api/diagnostics")
    def list_diagnostics() -> dict[str, Any]:
        return {
            "records": diagnostics.public_diagnostic_records(),
            "overview": diagnostics.diagnostic_overview(),
        }

    @app.get("/api/diagnostics/history.zip")
    def download_diagnostics_history() -> StreamingResponse:
        payload, filename, count = diagnostics.build_diagnostics_history_zip_bytes()
        return _zip_response(payload, filename, count=count)

    @app.get("/api/diagnostics/{record_id}.zip")
    def download_diagnostic_record(record_id: str) -> StreamingResponse:
        record = diagnostics.find_diagnostic_record(record_id)
        if record is None:
            raise HTTPException(404, "Diagnostic record not found.")
        payload, filename = diagnostics.build_diagnostic_zip_bytes(record["record_dir"])
        return _zip_response(payload, filename)

    @app.delete("/api/diagnostics/{record_id}")
    def delete_diagnostic_record(record_id: str) -> dict[str, Any]:
        _require_no_active_tasks(app, category="diagnostics")
        return maintenance.delete_diagnostic(record_id).as_dict()

    return app


def _require_no_active_tasks(app: FastAPI, *, category: str) -> None:
    active_count = app.state.task_manager.active_task_count()
    if active_count:
        raise HTTPException(
            409,
            "存在活动任务，不能清理会影响其记录或冻结凭据的数据。",
            headers={"X-Translator-Reason": f"active_tasks_block_{category}"},
        )


def _update_state_payload(settings: AppSettings) -> dict[str, Any]:
    return {
        "preferences": {
            "notifications_paused": settings.update.notifications_paused,
            "ignored_release_version": settings.update.ignored_release_version,
            "last_background_check_at": settings.update.last_background_check_at,
            "quick_start_completed": settings.onboarding.quick_start_completed,
        },
        "background_due": _background_update_deferred(settings) is None,
    }


def _background_update_deferred(settings: AppSettings) -> str | None:
    if not settings.onboarding.quick_start_completed:
        return "quick_start_incomplete"
    if settings.update.notifications_paused:
        return "notifications_paused"
    raw_timestamp = str(settings.update.last_background_check_at or "").strip()
    if not raw_timestamp:
        return None
    try:
        previous = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
        if previous.tzinfo is None:
            previous = previous.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    if datetime.now(timezone.utc) - previous < timedelta(hours=24):
        return "interval_not_elapsed"
    return None


def _background_notice_suppressed(settings: AppSettings, result: dict[str, Any]) -> bool:
    if result.get("status") != "available":
        return True
    return str(result.get("latest_version") or "") == settings.update.ignored_release_version


def _json_error(status_code: int, detail: str, *, reason: str | None = None) -> Response:
    payload: dict[str, Any] = {"detail": detail}
    if reason:
        payload["reason"] = reason
    return JSONResponse(status_code=status_code, content=payload)


def _connection_payload(
    connection: ModelConnection,
    index: int,
    config: EffectiveModelConfig,
) -> dict[str, Any]:
    """Describe one pool connection, including a masked hint of its saved key."""
    api_key = get_connection_scoped_key(connection.id) or (
        config.api_key if index == 0 else ""
    )
    return {
        "id": connection.id,
        "label": connection.label,
        "display_label": connection.display_label,
        "provider": connection.provider,
        "model": connection.model,
        "base_url": connection.base_url,
        "availability_status": connection.availability_status,
        "availability_message": connection.availability_message,
        "availability_checked_at": connection.availability_checked_at,
        "has_api_key": bool(api_key),
        "api_key_preview": mask_api_key(api_key),
        "primary": index == 0,
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
        # A following role reuses its source's credentials, so the source's
        # pool is what it dials.  Serving its own idle pool here made the panel
        # label a followed connection with a name nothing was connecting to.
        "connections": [
            _connection_payload(connection, index, config)
            for index, connection in enumerate(
                list_effective_role_connections(settings, role)
            )
        ],
        "connection_pool_role": pool_role(settings, role),
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
            "batch_size": batch_size_bounds(config),
            "concurrency": concurrency_bounds(config),
        },
    }


def _model_config_or_422(settings: AppSettings, role: str):
    if role not in {ROLE_TRANSLATION, ROLE_CLEANER, ROLE_IMAGE, ROLE_PDF_REVIEW}:
        raise HTTPException(404, "Unknown model role.")
    try:
        return resolve_effective_model_config(settings, role)
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


def _zip_response(payload: bytes, filename: str, *, count: int | None = None) -> StreamingResponse:
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    if count is not None:
        headers["X-Translator-Record-Count"] = str(count)
    return StreamingResponse(iter([payload]), media_type="application/zip", headers=headers)


def _deep_merge(current: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    merged = dict(current)
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return _json_safe(asdict(value))
    if hasattr(value, "model_dump"):
        return _json_safe(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)
