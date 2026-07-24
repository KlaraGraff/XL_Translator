"""Task lifecycle and SSE event storage for the local API."""

from __future__ import annotations

import json
import hashlib
import threading
import time
import uuid
from collections.abc import Callable, Generator
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from core import tm_manager
from core.file_scanner import scan_path
from core.model_api_identity import task_api_context_for_page
from core.engine_dispatcher import activate_translation_surface
from core.language_registry import normalize_source_selection
from core.pdf_image_translation import PdfImageTranslationRunner, scan_pdf_path
from core.task_resources import TaskResourceLease, TaskResourceRegistry
from core.task_runner import (
    DoneMsg,
    ErrorMsg,
    LogMsg,
    PdfPageRecoveryStatusMsg,
    PdfReviewStatusMsg,
    ProgressMsg,
    StatusMsg,
    StoppedMsg,
    TaskRunner,
    WordRecoveryStatusMsg,
)
from core.word_document import scan_word_path
from core.word_task_runner import WordTaskRunner
from settings import AppSettings, load_settings

TaskSurface = Literal["excel", "word", "pdf"]

_PAGE_BY_SURFACE = {
    "excel": "excel_translate",
    "word": "word_translate",
    "pdf": "pdf_translate",
}
_LABEL_BY_SURFACE = {
    "excel": "Excel translation",
    "word": "Word translation",
    "pdf": "PDF translation",
}


class Runner(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...

    def needs_poll(self) -> bool: ...

    def get_message(self, timeout: float = 0.05) -> Any: ...


class TaskNotFoundError(KeyError):
    """Raised when a task ID does not belong to this sidecar."""


class TaskConflictError(RuntimeError):
    """Raised when a task would share an upstream API resource."""


class TaskInputError(ValueError):
    """Raised when a source path has no usable files for the selected surface."""


@dataclass(frozen=True)
class TaskOptions:
    untranslated_only: bool = False
    protect_scheme_cover: bool = False
    allow_xls_fallback: bool = False
    include_images: bool = False
    source_lang: str | None = None
    target_lang: str | None = None


@dataclass
class ApiTask:
    task_id: str
    surface: TaskSurface
    source_path: str
    runner: Runner
    lease: TaskResourceLease
    created_at: float
    model_snapshot: dict[str, dict[str, object]] = field(default_factory=dict)
    task_snapshot: dict[str, object] = field(default_factory=dict)
    state: str = "running"
    result: dict[str, Any] | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    next_event_id: int = 1
    terminal: bool = False
    condition: threading.Condition = field(default_factory=threading.Condition)


class TranslationTaskManager:
    """Owns active runners, upstream locks, and replayable SSE event streams."""

    def __init__(
        self,
        *,
        settings_loader: Callable[[], AppSettings] = load_settings,
        registry: TaskResourceRegistry | None = None,
    ) -> None:
        self._settings_loader = settings_loader
        self._registry = registry or TaskResourceRegistry()
        self._tasks: dict[str, ApiTask] = {}
        self._lock = threading.RLock()

    def start_task(
        self,
        *,
        surface: TaskSurface,
        source_path: str,
        selected_paths: list[str] | None = None,
        options: TaskOptions | None = None,
    ) -> dict[str, Any]:
        normalized_surface = _normalize_surface(surface)
        root = Path(source_path).expanduser().resolve()
        if not root.exists():
            raise TaskInputError(f"Source path does not exist: {root}")

        selected_options = options or TaskOptions()
        settings = self._settings_loader().model_copy(deep=True)
        activate_translation_surface(settings, normalized_surface)
        default_surface_source = getattr(
            settings,
            f"{normalized_surface}_source_lang",
            settings.source_lang,
        )
        source_selection = normalize_source_selection(
            selected_options.source_lang
            if selected_options.source_lang is not None
            else default_surface_source
        )
        if source_selection is None:
            raise TaskInputError("源语言必须是内置语言或自动识别；自定义语言只能作为目标语言。")
        if selected_options.target_lang:
            if normalized_surface == "pdf":
                settings.pdf.target_lang = selected_options.target_lang
            else:
                settings.target_lang = selected_options.target_lang
        elif normalized_surface in {"excel", "word"}:
            settings.target_lang = getattr(
                settings,
                f"{normalized_surface}_target_lang",
                settings.target_lang,
            )
        files = self._scan(root, normalized_surface, selected_options)
        selected = {
            str(Path(path).expanduser().resolve())
            for path in (selected_paths or [])
            if str(path or "").strip()
        }
        if selected:
            files = [
                item
                for item in files
                if str(Path(item.path).expanduser().resolve()) in selected
            ]
        if not files:
            raise TaskInputError(
                f"No supported {normalized_surface} files were found at: {root}"
            )
        if normalized_surface in {"excel", "word"}:
            tm_manager.init_db()

        page_key = _PAGE_BY_SURFACE[normalized_surface]
        api_context = task_api_context_for_page(settings, page_key)
        task_id = uuid.uuid4().hex
        lease = self._registry.acquire(
            owner_key=task_id,
            owner_label=_LABEL_BY_SURFACE[normalized_surface],
            resources=api_context.api_groups,
        )
        if lease is None:
            raise TaskConflictError(
                "Another running translation task uses the same upstream API."
            )

        try:
            prompt_source = "|".join(
                (
                    str(getattr(settings, "domain_preset", "") or ""),
                    str(getattr(settings, "custom_prompt", "") or ""),
                    str(getattr(settings, "domain_prompt_overrides", {}) or {}),
                )
            )
            task_snapshot = {
                "surface": normalized_surface,
                "source_lang": source_selection,
                "target_lang": (
                    settings.pdf.target_lang
                    if normalized_surface == "pdf"
                    else settings.target_lang
                ),
                "domain_preset": (
                    getattr(settings, f"{normalized_surface}_domain_preset", "")
                    if normalized_surface in {"excel", "word"}
                    else ""
                ),
                "prompt_signature": hashlib.sha256(
                    prompt_source.encode("utf-8")
                ).hexdigest()[:12],
            }
            runner = self._build_runner(
                surface=normalized_surface,
                files=files,
                settings=settings,
                source_root=root if root.is_dir() else root.parent,
                options=selected_options,
                source_lang=source_selection,
                key_overrides=api_context.key_overrides,
            )
            task = ApiTask(
                task_id=task_id,
                surface=normalized_surface,
                source_path=str(root),
                runner=runner,
                lease=lease,
                created_at=time.time(),
                model_snapshot=dict(api_context.model_snapshot or {}),
                task_snapshot=task_snapshot,
            )
            with self._lock:
                self._tasks[task_id] = task
            self._append_event(
                task,
                "start",
                {
                    "state": "running",
                    "model_snapshot": _json_safe(task.model_snapshot),
                    "task_snapshot": _json_safe(task.task_snapshot),
                },
            )
            runner.start()
            threading.Thread(
                target=self._pump_runner,
                args=(task,),
                daemon=True,
                name=f"api-task-{task_id[:8]}",
            ).start()
            return self.task_status(task_id)
        except Exception:
            lease.release()
            with self._lock:
                self._tasks.pop(task_id, None)
            raise

    def task_status(self, task_id: str) -> dict[str, Any]:
        task = self._get_task(task_id)
        with task.condition:
            return {
                "task_id": task.task_id,
                "surface": task.surface,
                "source_path": task.source_path,
                "state": task.state,
                "terminal": task.terminal,
                "created_at": task.created_at,
                "model_snapshot": _json_safe(task.model_snapshot),
                "task_snapshot": _json_safe(task.task_snapshot),
                "result": task.result,
            }

    def stop_task(self, task_id: str) -> dict[str, Any]:
        task = self._get_task(task_id)
        with task.condition:
            if task.terminal:
                return self.task_status(task_id)
            task.state = "stopping"
        task.runner.stop()
        self._append_event(task, "stopping", {"state": "stopping"})
        return self.task_status(task_id)

    def reservations(self) -> list[dict[str, Any]]:
        return [
            {
                "owner_key": item.owner_key,
                "owner_label": item.owner_label,
                "resources": [list(resource) if isinstance(resource, tuple) else str(resource) for resource in item.resources],
                "conservative": item.conservative,
            }
            for item in self._registry.reservations()
        ]

    def iter_sse(
        self,
        task_id: str,
        *,
        after_event_id: int = 0,
    ) -> Generator[str, None, None]:
        task = self._get_task(task_id)
        last_id = max(0, int(after_event_id or 0))
        while True:
            with task.condition:
                pending = [event for event in task.events if event["id"] > last_id]
                terminal = task.terminal
                if not pending and not terminal:
                    task.condition.wait(timeout=15)
                    pending = [event for event in task.events if event["id"] > last_id]
                    terminal = task.terminal
            if not pending:
                if terminal:
                    return
                yield ": keepalive\n\n"
                continue
            for event in pending:
                last_id = event["id"]
                payload = json.dumps(event["data"], ensure_ascii=False, separators=(",", ":"))
                yield f"id: {event['id']}\nevent: {event['type']}\ndata: {payload}\n\n"
            if terminal:
                return

    def shutdown(self) -> None:
        with self._lock:
            tasks = list(self._tasks.values())
        for task in tasks:
            with task.condition:
                if task.terminal:
                    continue
                task.state = "stopping"
            task.runner.stop()

    def _scan(
        self,
        root: Path,
        surface: TaskSurface,
        options: TaskOptions,
    ) -> list[Any]:
        if surface == "excel":
            return scan_path(root)
        if surface == "word":
            return scan_word_path(root)
        return scan_pdf_path(root, include_images=options.include_images)

    def _build_runner(
        self,
        *,
        surface: TaskSurface,
        files: list[Any],
        settings: AppSettings,
        source_root: Path,
        options: TaskOptions,
        source_lang: str,
        key_overrides: dict[str, str],
    ) -> Runner:
        if surface == "excel":
            return TaskRunner(
                files,
                settings,
                source_root=source_root,
                allow_xls_fallback=options.allow_xls_fallback,
                source_lang=source_lang,
                key_overrides=key_overrides,
                untranslated_only=options.untranslated_only,
            )
        if surface == "word":
            return WordTaskRunner(
                files,
                settings,
                source_root=source_root,
                source_lang=source_lang,
                key_overrides=key_overrides,
                untranslated_only=options.untranslated_only,
                protect_scheme_cover=options.protect_scheme_cover,
            )
        return PdfImageTranslationRunner(
            files,
            settings,
            source_root=source_root,
            key_overrides=key_overrides,
        )

    def _pump_runner(self, task: ApiTask) -> None:
        try:
            while task.runner.needs_poll():
                message = task.runner.get_message(timeout=0.1)
                if message is not None:
                    self._handle_message(task, message)
                with task.condition:
                    if task.terminal:
                        return
            self._finish_if_needed(
                task,
                state="error",
                event_type="error",
                result={"message": "Translation runner ended without a terminal message."},
            )
        except Exception as exc:  # noqa: BLE001 - task errors must be delivered to SSE.
            self._finish_if_needed(
                task,
                state="error",
                event_type="error",
                result={"message": str(exc) or exc.__class__.__name__},
            )

    def _handle_message(self, task: ApiTask, message: Any) -> None:
        event_type = _event_type_for_message(message)
        payload = _json_safe(asdict(message) if is_dataclass(message) else message)
        if isinstance(message, DoneMsg):
            self._finish_if_needed(task, "done", event_type, payload)
            return
        if isinstance(message, ErrorMsg):
            self._finish_if_needed(task, "error", event_type, payload)
            return
        if isinstance(message, StoppedMsg):
            self._finish_if_needed(task, "stopped", event_type, payload)
            return
        self._append_event(task, event_type, payload)

    def _finish_if_needed(
        self,
        task: ApiTask,
        state: str,
        event_type: str,
        result: dict[str, Any],
    ) -> None:
        with task.condition:
            if task.terminal:
                return
            task.state = state
            task.terminal = True
            task.result = result
        task.lease.release()
        self._append_event(task, event_type, result)

    @staticmethod
    def _append_event(task: ApiTask, event_type: str, data: Any) -> None:
        with task.condition:
            task.events.append(
                {
                    "id": task.next_event_id,
                    "type": event_type,
                    "data": _json_safe(data),
                }
            )
            task.next_event_id += 1
            task.condition.notify_all()

    def _get_task(self, task_id: str) -> ApiTask:
        with self._lock:
            task = self._tasks.get(str(task_id or ""))
        if task is None:
            raise TaskNotFoundError(task_id)
        return task


def _normalize_surface(surface: str) -> TaskSurface:
    normalized = str(surface or "").strip().lower()
    if normalized not in _PAGE_BY_SURFACE:
        raise TaskInputError(f"Unsupported translation surface: {surface}")
    return normalized  # type: ignore[return-value]


def _event_type_for_message(message: Any) -> str:
    mapping = (
        (ProgressMsg, "progress"),
        (StatusMsg, "status"),
        (LogMsg, "log"),
        (WordRecoveryStatusMsg, "word_recovery"),
        (PdfReviewStatusMsg, "pdf_review"),
        (PdfPageRecoveryStatusMsg, "pdf_page_recovery"),
        (DoneMsg, "done"),
        (ErrorMsg, "error"),
        (StoppedMsg, "stopped"),
    )
    for cls, event_type in mapping:
        if isinstance(message, cls):
            return event_type
    return "message"


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return _json_safe(asdict(value))
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)
