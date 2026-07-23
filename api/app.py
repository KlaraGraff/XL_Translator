"""FastAPI application exposing the existing Translator core over loopback HTTP."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, Header, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from api.task_manager import (
    TaskConflictError,
    TaskInputError,
    TaskNotFoundError,
    TaskOptions,
    TranslationTaskManager,
)
from core import data_migration, diagnostics, tm_manager
from core.language_preflight import (
    build_language_preflight_prompt,
    extract_language_probe_texts,
    parse_preflight_languages,
)
from core.language_registry import (
    append_custom_target_lang,
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
from core.file_scanner import scan_path
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
    resolve_effective_model_config,
    settings_for_text_role,
)
from core.model_throughput import (
    batch_size_bounds,
    concurrency_bounds,
    get_model_throughput,
    set_model_throughput,
)
from core.pdf_image_translation import scan_pdf_path
from core.pdf_review import check_pdf_review_connectivity
from core.tm_cleaner import CleanSuggestion, apply_suggestions, run_cleaning
from core.word_document import scan_word_path
from core.engine_dispatcher import build_engine
from settings import (
    AppSettings,
    delete_key,
    get_key,
    load_keys,
    load_settings,
    parse_api_key_scope,
    save_key,
    save_settings,
)


class ApiKeyPayload(BaseModel):
    api_key: str = Field(default="", max_length=16_384)
    base_url: str = Field(default="", max_length=2_048)


class ScanRequest(BaseModel):
    path: str = Field(min_length=1)
    surface: Literal["excel", "word", "pdf"]
    include_images: bool = False


class TaskStartRequest(BaseModel):
    source_path: str = Field(min_length=1)
    surface: Literal["excel", "word", "pdf"]
    selected_paths: list[str] = Field(default_factory=list)
    untranslated_only: bool = False
    protect_scheme_cover: bool = False
    allow_xls_fallback: bool = False
    include_images: bool = False
    source_lang: str | None = None
    target_lang: str | None = None


class TmEntryPayload(BaseModel):
    source_text: str = Field(min_length=1)
    target_text: str = Field(min_length=1)
    lang_pair: str = Field(min_length=3)


class TmEntryUpdatePayload(BaseModel):
    source_text: str = Field(min_length=1)
    target_text: str = Field(min_length=1)


class TmPinPayload(BaseModel):
    pinned: bool = True


class TmBulkPinPayload(TmPinPayload):
    ids: list[int] = Field(min_length=1)


class TmImportPayload(BaseModel):
    lang_pair: str = Field(min_length=3)
    mode: Literal["skip", "overwrite", "keep_both"] = "skip"
    entries: list[dict[str, Any]]


class TmSuggestionPayload(BaseModel):
    entry_id: int
    source_text: str = ""
    old_target: str = ""
    new_target: str = Field(min_length=1)
    accepted: bool = True


class TmApplySuggestionsPayload(BaseModel):
    suggestions: list[TmSuggestionPayload]
    auto_pin: bool = False


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
    overwrite: bool = False


class ModelFetchRequest(BaseModel):
    provider: str = Field(min_length=1)
    base_url: str = ""
    api_key: str = ""


class ThroughputPayload(BaseModel):
    batch_size: int | None = None
    concurrency: int | None = None


class MigrationApplyRequest(BaseModel):
    action: Literal["full", "non_conflicting", "skip"]
    include_support_files: bool = False


class UpdateIgnoreRequest(BaseModel):
    ignored_release_version: str = ""
    ignore_updates: bool = True


def create_app(
    *,
    task_manager: TranslationTaskManager | None = None,
    auth_token: str = "",
) -> FastAPI:
    """Create a local API app; an empty token keeps in-process tests simple."""
    app = FastAPI(title="Translator Sidecar API", version="1")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["tauri://localhost", "http://tauri.localhost"],
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
        return _json_error(409, str(exc))

    @app.exception_handler(TaskInputError)
    async def task_input_error(_request, exc):
        return _json_error(422, str(exc))

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

    @app.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        return load_settings().model_dump(mode="json")

    @app.put("/api/settings")
    def put_settings(payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise HTTPException(422, "Settings payload must be a JSON object.")
        current = load_settings().model_dump(mode="json")
        merged = _deep_merge(current, payload)
        try:
            settings = AppSettings.model_validate(merged)
        except Exception as exc:
            raise HTTPException(422, str(exc)) from exc
        save_settings(settings)
        return settings.model_dump(mode="json")

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
        save_key(provider, payload.api_key, payload.base_url)
        return {
            "provider": provider,
            "base_url": payload.base_url,
            "has_key": bool(payload.api_key.strip()),
        }

    @app.delete("/api/keys/{provider}", status_code=204)
    def remove_key(provider: str, base_url: str = "") -> Response:
        delete_key(provider, base_url)
        return Response(status_code=204)

    @app.post("/api/sources/scan")
    def scan_sources(request: ScanRequest) -> dict[str, Any]:
        root = Path(request.path).expanduser()
        if request.surface == "excel":
            items = scan_path(root)
        elif request.surface == "word":
            items = scan_word_path(root)
        else:
            items = scan_pdf_path(root, include_images=request.include_images)
        return {"items": [_json_safe(item) for item in items]}

    @app.post("/api/tasks", status_code=202)
    def start_task(request: TaskStartRequest) -> dict[str, Any]:
        return app.state.task_manager.start_task(
            surface=request.surface,
            source_path=request.source_path,
            selected_paths=request.selected_paths,
            options=TaskOptions(
                untranslated_only=request.untranslated_only,
                protect_scheme_cover=request.protect_scheme_cover,
                allow_xls_fallback=request.allow_xls_fallback,
                include_images=request.include_images,
                source_lang=request.source_lang,
                target_lang=request.target_lang,
            ),
        )

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: str) -> dict[str, Any]:
        return app.state.task_manager.task_status(task_id)

    @app.post("/api/tasks/{task_id}/stop")
    def stop_task(task_id: str) -> dict[str, Any]:
        return app.state.task_manager.stop_task(task_id)

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

    @app.get("/api/tasks/locks/current")
    def current_task_locks() -> dict[str, Any]:
        return {"reservations": app.state.task_manager.reservations()}

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
            )
        }

    @app.put("/api/tm/entries/{entry_id}")
    def update_tm_entry(entry_id: int, payload: TmEntryUpdatePayload) -> dict[str, bool]:
        tm_manager.init_db()
        changed = tm_manager.update_entry_full(
            entry_id,
            payload.source_text,
            payload.target_text,
        )
        if not changed:
            raise HTTPException(409, "Entry is missing or conflicts with an existing source.")
        return {"changed": True}

    @app.delete("/api/tm/entries/{entry_id}", status_code=204)
    def delete_tm_entry(entry_id: int) -> Response:
        tm_manager.init_db()
        tm_manager.delete_entry(entry_id)
        return Response(status_code=204)

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

    @app.get("/api/tm/export")
    def export_tm_entries(lang_pair: str) -> dict[str, Any]:
        tm_manager.init_db()
        return {"lang_pair": lang_pair, "entries": tm_manager.get_all_entries_for_export(lang_pair)}

    @app.post("/api/tm/import")
    def import_tm_entries(payload: TmImportPayload) -> dict[str, int]:
        tm_manager.init_db()
        return tm_manager.import_entries(payload.entries, payload.lang_pair, payload.mode)

    @app.post("/api/tm/clean")
    def clean_tm_entries(payload: TmCleanRequest) -> dict[str, Any]:
        tm_manager.init_db()
        settings = load_settings()
        clean_settings = settings_for_text_role(settings, ROLE_CLEANER)
        config = resolve_effective_model_config(settings, ROLE_CLEANER)
        throughput = get_model_throughput(settings, config)
        suggestions = run_cleaning(
            payload.lang_pair,
            build_engine(clean_settings),
            batch_size=throughput.batch_size or clean_settings.engine.batch_size,
            concurrency=throughput.concurrency,
            extra_prompt=settings.cleaner_prompt_extras.get(payload.lang_pair, ""),
            full_override_prompt=settings.cleaner_full_prompt_overrides.get(
                payload.lang_pair,
                "",
            ),
            custom_target_langs=settings.custom_target_langs,
        )
        result = {"suggestions": [_json_safe(item) for item in suggestions]}
        if payload.overwrite:
            result["applied"] = apply_suggestions(
                suggestions,
                auto_pin=settings.auto_pin_after_clean,
            )
        return result

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
        return {"applied": apply_suggestions(suggestions, auto_pin=payload.auto_pin)}

    @app.get("/api/models/roles")
    def get_model_roles() -> dict[str, Any]:
        settings = load_settings()
        return {
            "roles": {
                role: _model_role_payload(settings, role)
                for role in (ROLE_TRANSLATION, ROLE_CLEANER, ROLE_IMAGE, ROLE_PDF_REVIEW)
            }
        }

    @app.post("/api/models/fetch")
    def fetch_models(request: ModelFetchRequest) -> dict[str, Any]:
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

    @app.post("/api/models/connectivity/text")
    def check_text_connectivity() -> dict[str, Any]:
        return _json_safe(check_connectivity(load_settings()))

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
    def export_model_config() -> dict[str, Any]:
        return build_model_config_export_payload(load_settings())

    @app.post("/api/model-config/import")
    def import_model_config(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            imported = parse_model_config_import(payload)
            settings = apply_model_config_import(load_settings(), imported)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        save_settings(settings)
        return {
            "settings": settings.model_dump(mode="json"),
            "imported_key_count": len(imported.api_keys) + len(imported.scoped_api_keys),
        }

    @app.get("/api/updates/check")
    def check_updates() -> dict[str, Any]:
        from core.update_checker import check_for_updates

        return _json_safe(check_for_updates())

    @app.put("/api/updates/preferences")
    def update_preferences(payload: UpdateIgnoreRequest) -> dict[str, Any]:
        settings = load_settings()
        settings.update.ignore_updates = payload.ignore_updates
        settings.update.ignored_release_version = payload.ignored_release_version.strip()
        save_settings(settings)
        return settings.update.model_dump(mode="json")

    @app.get("/api/diagnostics")
    def list_diagnostics() -> dict[str, Any]:
        return {"records": diagnostics.list_diagnostic_records()}

    @app.get("/api/diagnostics/history.zip")
    def download_diagnostics_history() -> StreamingResponse:
        payload, filename, count = diagnostics.build_diagnostics_history_zip_bytes()
        return _zip_response(payload, filename, count=count)

    @app.get("/api/diagnostics/{record_id}.zip")
    def download_diagnostic_record(record_id: str) -> StreamingResponse:
        record = next(
            (
                item
                for item in diagnostics.list_diagnostic_records()
                if item.get("record_id") == record_id
            ),
            None,
        )
        if record is None:
            raise HTTPException(404, "Diagnostic record not found.")
        payload, filename = diagnostics.build_diagnostic_zip_bytes(record["record_dir"])
        return _zip_response(payload, filename)

    @app.get("/api/migration/inspect")
    def inspect_migration() -> dict[str, Any]:
        return _json_safe(data_migration.inspect_data_migration())

    @app.post("/api/migration/apply")
    def apply_migration(payload: MigrationApplyRequest) -> dict[str, Any]:
        plan = data_migration.inspect_data_migration()
        if payload.action == "skip":
            marker = data_migration.mark_migration_skipped(plan)
            return {"marker_path": str(marker), "status": "skipped"}
        if payload.action == "full":
            result = data_migration.migrate_legacy_data(
                plan,
                include_support_files=payload.include_support_files,
            )
        else:
            result = data_migration.migrate_non_conflicting_legacy_data(
                plan,
                include_support_files=payload.include_support_files,
            )
        return _json_safe(result)

    return app


def _json_error(status_code: int, detail: str) -> Response:
    return JSONResponse(status_code=status_code, content={"detail": detail})


def _model_role_payload(settings: AppSettings, role: str) -> dict[str, Any]:
    config = _model_config_or_422(settings, role)
    throughput = get_model_throughput(settings, config)
    return {
        "role": config.role,
        "label": config.label,
        "capability": config.capability,
        "mode": config.mode,
        "provider": config.provider,
        "model": config.model,
        "base_url": config.base_url,
        "source_role": config.source_role,
        "follows": config.follows,
        "availability_status": config.availability_status,
        "availability_message": config.availability_message,
        "throughput": {
            "profile_key": throughput.profile_key,
            "batch_size": throughput.batch_size,
            "concurrency": throughput.concurrency,
        },
    }


def _model_config_or_422(settings: AppSettings, role: str):
    if role not in {ROLE_TRANSLATION, ROLE_CLEANER, ROLE_IMAGE, ROLE_PDF_REVIEW}:
        raise HTTPException(404, "Unknown model role.")
    try:
        return resolve_effective_model_config(settings, role)
    except Exception as exc:
        raise HTTPException(422, str(exc)) from exc


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
