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
    model_config_signature,
    provider_supports_capability,
    reset_model_role_availability,
    resolve_effective_model_config,
    settings_for_text_role,
    validate_all_model_roles,
)
from core.model_throughput import (
    batch_size_bounds,
    concurrency_bounds,
    get_model_throughput,
    reset_model_throughput,
    set_model_throughput,
)
from core.pdf_image_translation import scan_pdf_path
from core.pdf_review import check_pdf_review_connectivity
from core.tm_cleaner import CleanSuggestion, apply_suggestions, run_cleaning
from core.word_document import scan_word_path
from core.engine_dispatcher import build_engine
from config import DOMAIN_PRESETS
from settings import (
    AppSettings,
    delete_key,
    get_key,
    load_keys,
    load_settings,
    parse_api_key_scope,
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
        # Cleaning is suggestion-only.  Applying a suggestion always requires
        # the explicit /clean/apply confirmation route.
        return {"suggestions": [_json_safe(item) for item in suggestions]}

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
        if role == ROLE_TRANSLATION:
            engine = settings.engine
            if payload.mode is not None and engine.mode != payload.mode:
                engine.mode = payload.mode
                changed = True
            if payload.provider is not None:
                field = "local_provider" if engine.mode == "local" else "cloud_provider"
                if getattr(engine, field) != payload.provider:
                    setattr(engine, field, payload.provider)
                    changed = True
            if payload.model is not None:
                field = "local_model" if engine.mode == "local" else "cloud_model"
                if getattr(engine, field) != payload.model:
                    setattr(engine, field, payload.model)
                    changed = True
            if payload.base_url is not None:
                field = "local_base_url" if engine.mode == "local" else "cloud_base_url"
                if getattr(engine, field) != payload.base_url:
                    setattr(engine, field, payload.base_url)
                    changed = True
            if engine.mode == "cloud":
                set_cloud_provider_config(
                    engine,
                    engine.cloud_provider,
                    cloud_model=engine.cloud_model,
                    cloud_base_url=engine.cloud_base_url,
                )
        else:
            role_settings = {
                ROLE_CLEANER: settings.cleaner_model_role,
                ROLE_IMAGE: settings.image_model_role,
                ROLE_PDF_REVIEW: settings.pdf_review_model_role,
            }[role]
            for field, value in (
                ("source_role", payload.source_role),
                ("cloud_provider", payload.provider),
                ("cloud_model", payload.model),
                ("cloud_base_url", payload.base_url),
            ):
                if value is not None and getattr(role_settings, field) != value:
                    setattr(role_settings, field, value)
                    changed = True
            if role_settings.source_role == "independent":
                set_cloud_provider_config(
                    role_settings,
                    role_settings.cloud_provider,
                    cloud_model=role_settings.cloud_model,
                    cloud_base_url=role_settings.cloud_base_url,
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
        return _model_role_payload(settings, role)

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
        "source_role": config.source_role,
        "follows": config.follows,
        "availability_status": config.availability_status,
        "availability_message": config.availability_message,
        "availability_checked_at": getattr(owner, "availability_checked_at", ""),
        "availability_signature": config.availability_signature,
        "has_api_key": bool(config.api_key),
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
