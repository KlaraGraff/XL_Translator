"""JSONL worker used by the Tauri desktop shell.

The worker keeps the Python product core as the single business-logic owner.
Tauri talks to it through newline-delimited JSON over stdin/stdout.
"""
# ruff: noqa: E402 - the packaged worker must add the repo root before app imports.
from __future__ import annotations

import json
import sys
import threading
import traceback
import uuid
from pathlib import Path
from typing import Any

if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from loguru import logger

logger.remove()
logger.add(sys.stderr, level="INFO")

from app_meta import APP_NAME, APP_VERSION
from config import (
    CHUNK_CLOUD_MAX,
    CHUNK_CLOUD_MIN,
    CHUNK_LOCAL_MAX,
    CHUNK_LOCAL_MIN,
    CLOUD_ENGINES,
    DEFAULT_CLOUD_MODEL,
    DEFAULT_CLOUD_PROVIDER,
    DEFAULT_CUSTOM_OPENAI_BASE_URL,
    DOMAIN_PRESETS,
    OLLAMA_RECOMMENDED_MODELS,
    WORD_BATCH_CHARS_DEFAULT,
    WORD_BATCH_CHARS_MAX,
    WORD_BATCH_CHARS_MIN,
    WORD_BATCH_PARAGRAPHS_DEFAULT,
    WORD_BATCH_PARAGRAPHS_MAX,
    WORD_BATCH_PARAGRAPHS_MIN,
    WORD_BATCH_SPLIT_CHARS_DEFAULT,
    WORD_BATCH_SPLIT_CHARS_MAX,
    WORD_BATCH_SPLIT_CHARS_MIN,
    WORD_STRICT_RETRY_ATTEMPTS_DEFAULT,
    WORD_STRICT_RETRY_ATTEMPTS_MAX,
    WORD_STRICT_RETRY_ATTEMPTS_MIN,
    get_cloud_concurrency_bounds,
    get_default_concurrency,
    get_local_concurrency_bounds,
)
from core.connectivity_check import check_connectivity
from core.file_scanner import scan_path
from core.headless_translate import run_translation_path
from core.headless_word_translate import run_word_translation_path
from core.language_registry import (
    build_lang_pair,
    get_default_source_lang,
    get_default_target_lang,
    get_supported_languages,
    get_supported_source_languages,
)
from core.tm_manager import (
    delete_entry,
    get_pin_count,
    get_stats,
    init_db,
    insert_manual_entry,
    pin_entry,
    search_entries,
    update_entry_full,
)
from core.update_checker import check_for_updates
from core.word_document import scan_word_path
from settings import AppSettings, get_key, load_settings, save_key, save_settings


JsonDict = dict[str, Any]


class WorkerContext:
    def __init__(self) -> None:
        self.write_lock = threading.Lock()
        self.tasks_lock = threading.Lock()
        self.active_tasks: dict[str, threading.Thread] = {}

    def write(self, payload: JsonDict) -> None:
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self.write_lock:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()

    def respond_ok(self, request_id: str, result: Any = None) -> None:
        self.write({"id": request_id, "ok": True, "result": result})

    def respond_error(
        self,
        request_id: str,
        message: str,
        *,
        code: str = "error",
        detail: str = "",
    ) -> None:
        self.write(
            {
                "id": request_id,
                "ok": False,
                "error": {
                    "code": code,
                    "message": message,
                    "detail": detail,
                },
            }
        )

    def event(self, name: str, payload: JsonDict | None = None) -> None:
        self.write({"event": name, "payload": payload or {}})

    def register_task(self, task_id: str, thread: threading.Thread) -> None:
        with self.tasks_lock:
            self.active_tasks[task_id] = thread

    def finish_task(self, task_id: str) -> None:
        with self.tasks_lock:
            self.active_tasks.pop(task_id, None)


def main() -> int:
    ctx = WorkerContext()
    init_db()
    ctx.event("worker.ready", {"version": APP_VERSION})

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            ctx.event(
                "worker.protocol_error",
                {"message": f"Invalid JSON: {exc}"},
            )
            continue

        request_id = str(request.get("id") or "")
        command = str(request.get("command") or "")
        payload = request.get("payload") or {}
        if not request_id:
            ctx.event("worker.protocol_error", {"message": "Request id is required."})
            continue
        if not command:
            ctx.respond_error(request_id, "Command is required.", code="bad_request")
            continue

        try:
            result = dispatch_command(ctx, command, payload)
            ctx.respond_ok(request_id, result)
        except Exception as exc:  # noqa: BLE001 - worker boundary.
            ctx.respond_error(
                request_id,
                str(exc) or exc.__class__.__name__,
                detail=traceback.format_exc(),
            )

    return 0


def dispatch_command(ctx: WorkerContext, command: str, payload: JsonDict) -> Any:
    if command == "ping":
        return {"pong": True, "version": APP_VERSION}
    if command == "app.bootstrap":
        return build_bootstrap_payload()
    if command == "settings.load":
        return serialize_settings(load_settings())
    if command == "settings.save":
        return save_settings_payload(payload)
    if command == "settings.save_key":
        return save_key_payload(payload)
    if command == "connectivity.check":
        return connectivity_check_payload(payload)
    if command == "updates.check":
        return check_for_updates(current_version=APP_VERSION).__dict__
    if command == "excel.scan":
        return scan_excel_payload(payload)
    if command == "word.scan":
        return scan_word_payload(payload)
    if command == "task.start_excel":
        return start_translation_task(ctx, "excel", payload)
    if command == "task.start_word":
        return start_translation_task(ctx, "word", payload)
    if command == "tm.search":
        return tm_search_payload(payload)
    if command == "tm.add":
        return tm_add_payload(payload)
    if command == "tm.update":
        return tm_update_payload(payload)
    if command == "tm.delete":
        delete_entry(int(payload.get("id")))
        return {"deleted": True}
    if command == "tm.pin":
        pin_entry(int(payload.get("id")), bool(payload.get("pinned", True)))
        return {"updated": True}
    raise ValueError(f"Unknown command: {command}")


def build_bootstrap_payload() -> JsonDict:
    settings = load_settings()
    lang_pair = build_lang_pair(settings.target_lang, source_lang=settings.source_lang)
    return {
        "app": {
            "name": APP_NAME,
            "version": APP_VERSION,
        },
        "settings": serialize_settings(settings),
        "keys": build_key_presence(),
        "engine": {
            "cloudEngines": CLOUD_ENGINES,
            "defaultCloudProvider": DEFAULT_CLOUD_PROVIDER,
            "defaultCloudModel": DEFAULT_CLOUD_MODEL,
            "defaultCustomOpenAiBaseUrl": DEFAULT_CUSTOM_OPENAI_BASE_URL,
            "ollamaRecommendedModels": OLLAMA_RECOMMENDED_MODELS,
            "cloudBatchRange": [CHUNK_CLOUD_MIN, CHUNK_CLOUD_MAX],
            "localBatchRange": [CHUNK_LOCAL_MIN, CHUNK_LOCAL_MAX],
            "cloudConcurrencyRange": list(get_cloud_concurrency_bounds(False)),
            "localConcurrencyRange": list(get_local_concurrency_bounds(False)),
            "unlockedCloudConcurrencyRange": list(get_cloud_concurrency_bounds(True)),
            "unlockedLocalConcurrencyRange": list(get_local_concurrency_bounds(True)),
            "defaultCloudConcurrency": get_default_concurrency("cloud"),
            "defaultLocalConcurrency": get_default_concurrency("local"),
        },
        "wordBatch": {
            "paragraphs": [
                WORD_BATCH_PARAGRAPHS_MIN,
                WORD_BATCH_PARAGRAPHS_MAX,
                WORD_BATCH_PARAGRAPHS_DEFAULT,
            ],
            "chars": [
                WORD_BATCH_CHARS_MIN,
                WORD_BATCH_CHARS_MAX,
                WORD_BATCH_CHARS_DEFAULT,
            ],
            "splitChars": [
                WORD_BATCH_SPLIT_CHARS_MIN,
                WORD_BATCH_SPLIT_CHARS_MAX,
                WORD_BATCH_SPLIT_CHARS_DEFAULT,
            ],
            "retryAttempts": [
                WORD_STRICT_RETRY_ATTEMPTS_MIN,
                WORD_STRICT_RETRY_ATTEMPTS_MAX,
                WORD_STRICT_RETRY_ATTEMPTS_DEFAULT,
            ],
        },
        "languages": {
            "source": get_supported_source_languages(),
            "target": get_supported_languages(
                settings.custom_target_langs,
                include_optional=True,
            ),
            "defaultSource": get_default_source_lang(),
            "defaultTarget": get_default_target_lang(),
        },
        "tm": {
            "langPair": lang_pair,
            "stats": get_stats(lang_pair),
            "pinCount": get_pin_count(lang_pair),
        },
        "domain": {
            "presets": DOMAIN_PRESETS,
        },
    }


def serialize_settings(settings: AppSettings) -> JsonDict:
    return settings.model_dump(mode="json")


def build_key_presence() -> JsonDict:
    providers = set(CLOUD_ENGINES.values()) | {
        "openai",
        "custom_openai",
        "claude",
        "zhipu",
        "dashscope",
        "siliconflow",
        "lanyi",
    }
    return {provider: bool(get_key(provider)) for provider in sorted(providers)}


def save_settings_payload(payload: JsonDict) -> JsonDict:
    raw_settings = payload.get("settings")
    if not isinstance(raw_settings, dict):
        raise ValueError("settings.save requires a settings object.")
    settings = AppSettings.model_validate(raw_settings)
    save_settings(settings)
    return serialize_settings(settings)


def save_key_payload(payload: JsonDict) -> JsonDict:
    provider = str(payload.get("provider") or "").strip()
    api_key = str(payload.get("apiKey") or "")
    if not provider:
        raise ValueError("provider is required.")
    save_key(provider, api_key)
    return {"provider": provider, "present": bool(api_key)}


def connectivity_check_payload(payload: JsonDict) -> JsonDict:
    raw_settings = payload.get("settings")
    settings = (
        AppSettings.model_validate(raw_settings)
        if isinstance(raw_settings, dict)
        else load_settings()
    )
    return check_connectivity(settings).__dict__


def scan_excel_payload(payload: JsonDict) -> JsonDict:
    path = require_path(payload)
    items = scan_path(path)
    return {
        "path": str(path),
        "count": len(items),
        "items": [
            {
                "path": str(item.path),
                "name": item.name,
                "sizeKb": item.size_kb,
                "sheets": item.sheets,
            }
            for item in items
        ],
    }


def scan_word_payload(payload: JsonDict) -> JsonDict:
    path = require_path(payload)
    items = scan_word_path(path)
    return {
        "path": str(path),
        "count": len(items),
        "items": [
            {
                "path": str(item.path),
                "name": item.path.stem,
                "sizeKb": round(item.path.stat().st_size / 1024, 1),
            }
            for item in items
        ],
    }


def start_translation_task(
    ctx: WorkerContext,
    task_type: str,
    payload: JsonDict,
) -> JsonDict:
    path = require_path(payload)
    settings_payload = payload.get("settings")
    settings = (
        AppSettings.model_validate(settings_payload)
        if isinstance(settings_payload, dict)
        else load_settings()
    )
    save_settings(settings)

    task_id = str(uuid.uuid4())

    def emit(event_payload: JsonDict) -> None:
        ctx.event(
            "task.event",
            {
                "taskId": task_id,
                "taskType": task_type,
                **event_payload,
            },
        )

    def run() -> None:
        ctx.event(
            "task.started",
            {"taskId": task_id, "taskType": task_type, "path": str(path)},
        )
        try:
            if task_type == "word":
                result = run_word_translation_path(
                    path,
                    settings=settings,
                    event_handler=emit,
                ).to_dict()
            else:
                result = run_translation_path(
                    path,
                    settings=settings,
                    allow_xls_fallback=bool(payload.get("allowXlsFallback", False)),
                    event_handler=emit,
                ).to_dict()
            ctx.event(
                "task.completed",
                {
                    "taskId": task_id,
                    "taskType": task_type,
                    "result": result,
                },
            )
        except Exception as exc:  # noqa: BLE001 - background task boundary.
            ctx.event(
                "task.failed",
                {
                    "taskId": task_id,
                    "taskType": task_type,
                    "message": str(exc) or exc.__class__.__name__,
                    "detail": traceback.format_exc(),
                },
            )
        finally:
            ctx.finish_task(task_id)

    thread = threading.Thread(target=run, daemon=True)
    ctx.register_task(task_id, thread)
    thread.start()
    return {"taskId": task_id, "taskType": task_type}


def tm_search_payload(payload: JsonDict) -> JsonDict:
    settings = load_settings()
    lang_pair = str(payload.get("langPair") or "").strip()
    if not lang_pair:
        lang_pair = build_lang_pair(settings.target_lang, source_lang=settings.source_lang)
    keyword = str(payload.get("keyword") or "")
    page = max(1, int(payload.get("page") or 1))
    page_size = min(max(1, int(payload.get("pageSize") or 50)), 200)
    rows, total = search_entries(lang_pair, keyword, page, page_size)
    return {
        "langPair": lang_pair,
        "keyword": keyword,
        "page": page,
        "pageSize": page_size,
        "total": total,
        "rows": rows,
        "stats": get_stats(lang_pair),
        "pinCount": get_pin_count(lang_pair, keyword),
    }


def tm_add_payload(payload: JsonDict) -> JsonDict:
    lang_pair = require_lang_pair(payload)
    source = str(payload.get("source") or "")
    target = str(payload.get("target") or "")
    ok = insert_manual_entry(source, target, lang_pair)
    return {"inserted": ok}


def tm_update_payload(payload: JsonDict) -> JsonDict:
    entry_id = int(payload.get("id"))
    source = str(payload.get("source") or "")
    target = str(payload.get("target") or "")
    ok = update_entry_full(entry_id, source, target)
    return {"updated": ok}


def require_path(payload: JsonDict) -> Path:
    raw_path = str(payload.get("path") or "").strip().strip('"')
    if not raw_path:
        raise ValueError("path is required.")
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = path.resolve()
    return path


def require_lang_pair(payload: JsonDict) -> str:
    lang_pair = str(payload.get("langPair") or "").strip()
    if not lang_pair:
        raise ValueError("langPair is required.")
    return lang_pair


if __name__ == "__main__":
    raise SystemExit(main())
