"""FastAPI application exposing the existing Translator core over loopback HTTP."""

from __future__ import annotations

import os
import secrets
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from api.task_manager import (
    TaskConflictError,
    TaskInputError,
    TaskNotFoundError,
    TranslationTaskManager,
)
from core import maintenance, tm_manager
from core.task_history import TaskHistoryError
from core.model_auto_upgrade import auto_upgrade_models_on_startup
from settings import (
    SettingsSchemaError,
    recover_settings_file_if_needed,
)
from api.routes import (
    cad as cad_routes,
    ocr as ocr_routes,
    pdf2docx as pdf2docx_routes,
    pdf_routes as pdf_route_routes,
    settings as settings_routes,
    domains as domains_routes,
    keys as keys_routes,
    tasks as tasks_routes,
    tm as tm_routes,
    models as models_routes,
    config_transfer as config_transfer_routes,
    maintenance as maintenance_routes,
    feedback as feedback_routes,
)
from api.routes._shared import _json_error
from api.routes.config_transfer import CORRUPT_IMPORT_DETAIL
from api.schemas import (
    ApiKeyPayload,
    ScanRequest,
    CadScanRequest,
    PdfPageActionRequest,
    TaskStartRequest,
    TmEntryPayload,
    TmEntryUpdatePayload,
    TmPinPayload,
    TmBulkPinPayload,
    TmBulkDeletePayload,
    TmImportPayload,
    TmFullImportPayload,
    DataHealthDismissPayload,
    TmSuggestionPayload,
    TmApplySuggestionsPayload,
    CustomTargetLanguagePayload,
    LanguagePreflightRequest,
    TmCleanRequest,
    ModelFetchRequest,
    ModelCatalogRefreshPayload,
    ModelConnectivityPayload,
    ModelRoleUpdatePayload,
    ConnectionUpsertPayload,
    ConnectionReorderPayload,
    ModelRoleTestPayload,
    DomainSettingsPayload,
    DomainPromptOptimizePayload,
    ThroughputPayload,
    ThroughputUnlockPayload,
    UpdatePreferencesRequest,
    MaintenanceClearRequest,
    FullResetRequest,
)

# Re-exported for callers and tests that import them from ``api.app``.
__all__ = [
    "CORRUPT_IMPORT_DETAIL",
    "create_app",
    "ApiKeyPayload",
    "ScanRequest",
    "CadScanRequest",
    "PdfPageActionRequest",
    "TaskStartRequest",
    "TmEntryPayload",
    "TmEntryUpdatePayload",
    "TmPinPayload",
    "TmBulkPinPayload",
    "TmBulkDeletePayload",
    "TmImportPayload",
    "TmFullImportPayload",
    "DataHealthDismissPayload",
    "TmSuggestionPayload",
    "TmApplySuggestionsPayload",
    "CustomTargetLanguagePayload",
    "LanguagePreflightRequest",
    "TmCleanRequest",
    "ModelFetchRequest",
    "ModelCatalogRefreshPayload",
    "ModelConnectivityPayload",
    "ModelRoleUpdatePayload",
    "ConnectionUpsertPayload",
    "ConnectionReorderPayload",
    "ModelRoleTestPayload",
    "DomainSettingsPayload",
    "DomainPromptOptimizePayload",
    "ThroughputPayload",
    "ThroughputUnlockPayload",
    "UpdatePreferencesRequest",
    "MaintenanceClearRequest",
    "FullResetRequest",
]


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

    @asynccontextmanager
    async def lifespan(instance: FastAPI):
        # Repair the settings file once, here, before any request can read it.
        # Loading deliberately never writes, so without this a file that had
        # to be rebuilt would stay broken until the user happened to save.
        try:
            await run_in_threadpool(recover_settings_file_if_needed)
        except Exception as exc:  # noqa: BLE001 - never block startup on this
            logger.warning(f"启动时的设置文件自检失败：{exc}")
        upgrade_stop = threading.Event()
        threading.Thread(
            target=auto_upgrade_models_on_startup,
            args=(upgrade_stop,),
            name="model-auto-upgrade",
            daemon=True,
        ).start()
        yield
        upgrade_stop.set()
        # Closing the window used to kill running tasks outright, leaving the
        # LibreOffice profile, the Word temp docx directory and PDF page
        # workspaces behind and the history stuck on "running".  Give the
        # runners their chance to unwind instead.
        await run_in_threadpool(instance.state.task_manager.shutdown)

    app = FastAPI(title="Translator Sidecar API", version="1", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_allowed_origins(),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.task_manager = task_manager or TranslationTaskManager()
    app.state.auth_token = str(auth_token or "")

    # Route registration order matters wherever a literal path and a
    # parameterised sibling overlap (Starlette matches in declaration
    # order), so keep this list in the order the routes were written.
    cad_routes.register(app)
    ocr_routes.register(app)
    pdf2docx_routes.register(app)
    pdf_route_routes.register(app)

    @app.middleware("http")
    async def require_loopback_token(request, call_next):
        expected = app.state.auth_token
        # CORS preflight requests never carry the custom token. They do not
        # invoke an API handler; CORSMiddleware answers them before a browser
        # may issue the authenticated request.
        if (
            expected
            and request.method != "OPTIONS"
            # Constant-time comparison: a plain ``!=`` leaks how many leading
            # characters of the loopback token a guess got right.
            # Compare bytes: ``compare_digest`` rejects non-ASCII ``str``
            # inputs, and a header is only latin-1 decoded on the way in.
            and not secrets.compare_digest(
                str(request.headers.get("X-Translator-Token") or "").encode("utf-8"),
                str(expected).encode("utf-8"),
            )
        ):
            return Response(status_code=401)
        return await call_next(request)

    @app.exception_handler(TaskNotFoundError)
    async def task_not_found(_request, _exc):
        return _json_error(404, "找不到这个任务，可能已被清除。")

    @app.exception_handler(TaskConflictError)
    async def task_conflict(_request, exc):
        return _json_error(409, str(exc), reason=exc.reason)

    @app.exception_handler(TaskInputError)
    async def task_input_error(_request, exc):
        return _json_error(422, str(exc), reason=getattr(exc, "reason", "invalid_input"))

    @app.exception_handler(SettingsSchemaError)
    async def settings_schema_error(_request, exc):
        # An old or malformed settings file is recovered automatically now, so
        # the only way to land here is a file that can be neither read nor
        # copied.  The exception carries the path it failed on, which is the
        # part the user can actually act on — do not replace it with a
        # generic sentence.
        return _json_error(409, str(exc), reason="settings_file_unreadable")

    @app.exception_handler(tm_manager.TmSchemaError)
    async def tm_schema_error(_request, exc):
        # Same shape as the settings one: an old memory database is upgraded
        # in place now, so what reaches here is a file that can be neither
        # opened nor copied, or one another task is still holding.  Both carry
        # their own actionable sentence.
        return _json_error(409, str(exc), reason="tm_database_unreadable")

    @app.exception_handler(TaskHistoryError)
    async def task_history_error(_request, exc):
        return _json_error(409, str(exc), reason="task_history_write_blocked")

    @app.exception_handler(maintenance.MaintenanceError)
    async def maintenance_error(_request, exc):
        return _json_error(422, str(exc), reason="maintenance_operation_rejected")

    settings_routes.register(app)
    domains_routes.register(app)
    keys_routes.register(app)
    tasks_routes.register(app)
    tm_routes.register(app)
    models_routes.register(app)
    config_transfer_routes.register(app)
    maintenance_routes.register(app)
    feedback_routes.register(app)


    return app
