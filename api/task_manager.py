"""Task lifecycle and SSE event storage for the local API."""

from __future__ import annotations

import json
import hashlib
import re
import threading
import time
import uuid
from collections.abc import Callable, Generator
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from core import tm_manager
from core import bilingual_writer
from core.api_config_check import check_translation_api_config
from core.file_scanner import scan_path
from core.model_api_identity import task_api_context_for_page
from core.engine_dispatcher import activate_translation_surface
from core.language_registry import normalize_source_selection
from core.model_roles import (
    ROLE_IMAGE,
    ROLE_PDF_REVIEW,
    provider_supports_capability,
    resolve_effective_model_config,
)
from core.pdf_image_translation import PdfImageTranslationRunner, scan_pdf_path
from core.task_resources import ScheduledTaskLease, TaskResourceRegistry
from core.task_history import TaskHistoryStore
from core.tm_cleaning_task_runner import TmCleaningTaskRunner
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
from core.word_converter import get_local_word_automation_availability
from core.xls_converter import get_local_excel_availability
from settings import AppSettings, load_settings

TaskSurface = Literal["excel", "word", "pdf", "tm_clean"]

_PAGE_BY_SURFACE = {
    "excel": "excel_translate",
    "word": "word_translate",
    "pdf": "pdf_translate",
    "tm_clean": "tm_clean",
}
_LABEL_BY_SURFACE = {
    "excel": "Excel translation",
    "word": "Word translation",
    "pdf": "PDF translation",
    "tm_clean": "TM cleaning",
}


class Runner(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...

    def needs_poll(self) -> bool: ...

    def get_message(self, timeout: float = 0.05) -> Any: ...


class TaskNotFoundError(KeyError):
    """Raised when a task ID does not belong to this sidecar."""


class TaskConflictError(RuntimeError):
    """Raised when the scheduler cannot admit a candidate task."""

    def __init__(self, message: str, *, reason: str = "conflict") -> None:
        super().__init__(message)
        self.reason = reason


class TaskInputError(ValueError):
    """Raised when a source path has no usable files for the selected surface."""


@dataclass(frozen=True)
class TaskOptions:
    untranslated_only: bool = False
    protect_scheme_cover: bool = False
    allow_xls_fallback: bool = False
    allow_doc_fallback: bool = False
    include_images: bool = False
    source_lang: str | None = None
    target_lang: str | None = None
    allow_known_review_failure: bool = False
    lang_pair: str | None = None

    @property
    def xls_conversion_mode(self) -> str:
        return "compatibility" if self.allow_xls_fallback else "high_fidelity"

    @property
    def doc_conversion_mode(self) -> str:
        return "compatibility" if self.allow_doc_fallback else "high_fidelity"


@dataclass
class ApiTask:
    task_id: str
    surface: TaskSurface
    source_path: str
    source_label: str
    runner: Runner
    lease: ScheduledTaskLease
    created_at: float
    model_snapshot: dict[str, dict[str, object]] = field(default_factory=dict)
    role_groups: dict[str, object] = field(default_factory=dict)
    task_snapshot: dict[str, object] = field(default_factory=dict)
    state: str = "running"
    result: dict[str, Any] | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    next_event_id: int = 1
    terminal: bool = False
    updated_at: float = field(default_factory=time.time)
    logs: list[dict[str, Any]] = field(default_factory=list)
    condition: threading.Condition = field(default_factory=threading.Condition)


@dataclass(frozen=True)
class PreparedTask:
    """Validated, immutable start input used by preflight and atomic start."""

    surface: TaskSurface
    source_path: str
    source_label: str
    files: list[Any]
    settings: AppSettings
    options: TaskOptions
    source_lang: str
    task_snapshot: dict[str, object]
    model_snapshot: dict[str, dict[str, object]]
    key_overrides: dict[str, str]
    role_groups: dict[str, object]
    group_capacities: dict[object, int]
    fingerprint: str


@dataclass(frozen=True)
class ConfirmationToken:
    token: str
    prepared: PreparedTask
    registry_revision: int
    expires_at: float


class TranslationTaskManager:
    """Owns active runners, upstream locks, and replayable SSE event streams."""

    def __init__(
        self,
        *,
        settings_loader: Callable[[], AppSettings] = load_settings,
        registry: TaskResourceRegistry | None = None,
        history_store: TaskHistoryStore | None = None,
    ) -> None:
        self._settings_loader = settings_loader
        self._registry = registry or TaskResourceRegistry()
        self._history = history_store or TaskHistoryStore()
        self._tasks: dict[str, ApiTask] = {}
        self._confirmation_tokens: dict[str, ConfirmationToken] = {}
        self._lock = threading.RLock()
        # A complete sidecar restart cannot safely resume a frozen runner.  A
        # prior process may have recorded an active summary, so close that
        # state before this manager accepts fresh work.
        self._history.mark_active_interrupted()

    def preflight_task(
        self,
        *,
        surface: TaskSurface,
        source_path: str = "",
        selected_paths: list[str] | None = None,
        options: TaskOptions | None = None,
    ) -> dict[str, Any]:
        """Validate a candidate and issue a short-lived shared-risk token."""
        prepared = self._prepare_task(
            surface=surface,
            source_path=source_path,
            selected_paths=selected_paths,
            options=options,
        )
        risk = self._registry.scheduling_risk(
            task_type=prepared.surface,
            group_capacities=prepared.group_capacities,
        )
        if bool(risk["surface_busy"]):
            raise TaskConflictError(
                "同类型任务仍在运行、暂停或安全停止中。",
                reason="surface_busy",
            )
        response: dict[str, Any] = {
            "requires_confirmation": bool(risk["shared_groups"]),
            "risk": self._risk_payload(prepared, risk),
            "candidate_snapshot": _json_safe(prepared.task_snapshot),
        }
        if response["requires_confirmation"]:
            token = uuid.uuid4().hex
            with self._lock:
                self._purge_expired_tokens_locked()
                self._confirmation_tokens[token] = ConfirmationToken(
                    token=token,
                    prepared=prepared,
                    registry_revision=int(risk["revision"]),
                    expires_at=time.time() + 120,
                )
            response["confirmation_token"] = token
        return response

    def start_task(
        self,
        *,
        surface: TaskSurface,
        source_path: str = "",
        selected_paths: list[str] | None = None,
        options: TaskOptions | None = None,
        confirmation_token: str | None = None,
    ) -> dict[str, Any]:
        prepared = self._prepare_task(
            surface=surface,
            source_path=source_path,
            selected_paths=selected_paths,
            options=options,
        )
        expected_revision: int | None = None
        if confirmation_token:
            with self._lock:
                self._purge_expired_tokens_locked()
                token = self._confirmation_tokens.pop(str(confirmation_token), None)
            if token is None:
                raise TaskConflictError(
                    "风险确认令牌已过期或已使用，请重新预检。",
                    reason="expired_or_consumed",
                )
            if token.prepared.fingerprint != prepared.fingerprint:
                raise TaskConflictError(
                    "预检后的任务设置已变化，请重新确认风险。",
                    reason="stale",
                )
            expected_revision = token.registry_revision
        else:
            risk = self._registry.scheduling_risk(
                task_type=prepared.surface,
                group_capacities=prepared.group_capacities,
            )
            if bool(risk["surface_busy"]):
                raise TaskConflictError("同类型任务仍处于活动状态。", reason="surface_busy")
            if risk["shared_groups"]:
                raise TaskConflictError(
                    "此任务与活动任务共用模型/API，请先完成风险确认。",
                    reason="confirmation_required",
                )
            expected_revision = int(risk["revision"])
        return self._start_prepared(prepared, expected_revision=expected_revision)

    def _prepare_task(
        self,
        *,
        surface: TaskSurface,
        source_path: str,
        selected_paths: list[str] | None,
        options: TaskOptions | None,
    ) -> PreparedTask:
        normalized_surface = _normalize_surface(surface)
        selected_options = options or TaskOptions()
        settings = self._settings_loader().model_copy(deep=True)
        activate_translation_surface(settings, normalized_surface)

        if normalized_surface == "tm_clean":
            lang_pair = str(selected_options.lang_pair or source_path or "").strip()
            if "-" not in lang_pair or not all(part.strip() for part in lang_pair.split("-", 1)):
                raise TaskInputError("TM 清洗任务必须选择有效的定向语言对。")
            tm_manager.init_db()
            context = task_api_context_for_page(settings, _PAGE_BY_SURFACE[normalized_surface])
            task_snapshot: dict[str, object] = {
                "surface": normalized_surface,
                "lang_pair": lang_pair,
                "tm": {"mode": "suggestion_only", "writes_on_start": False},
                "selected_file_count": 0,
                "connections": self._connection_summaries(context),
            }
            return self._prepared_from_context(
                surface=normalized_surface,
                source_path="",
                source_label="TM language-pair task",
                files=[],
                settings=settings,
                options=TaskOptions(**{**selected_options.__dict__, "lang_pair": lang_pair}),
                source_lang="",
                task_snapshot=task_snapshot,
                context=context,
            )

        root = Path(source_path).expanduser().resolve()
        if not root.exists():
            raise TaskInputError(f"Source path does not exist: {root}")
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
        if normalized_surface == "excel":
            self._validate_excel_preflight(files=files, settings=settings, options=selected_options)
        elif normalized_surface == "word":
            self._validate_word_preflight(files=files, settings=settings, options=selected_options)
        else:
            self._validate_pdf_preflight(files=files, settings=settings, options=selected_options)
        if normalized_surface in {"excel", "word"}:
            tm_manager.init_db()

        context = task_api_context_for_page(settings, _PAGE_BY_SURFACE[normalized_surface])
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
            "target_lang": settings.pdf.target_lang if normalized_surface == "pdf" else settings.target_lang,
            "domain_preset": (
                getattr(settings, f"{normalized_surface}_domain_preset", "")
                if normalized_surface in {"excel", "word"}
                else ""
            ),
            "prompt_signature": hashlib.sha256(prompt_source.encode("utf-8")).hexdigest()[:12],
            "connections": self._connection_summaries(context),
        }
        if normalized_surface == "excel":
            task_snapshot.update(
                {
                    "excel_output": settings.excel_output.model_dump(mode="json"),
                    "excel_review": settings.excel_review.model_dump(mode="json"),
                    "tm": {"max_len": settings.tm.max_len},
                    "xls_conversion_mode": selected_options.xls_conversion_mode,
                    "selected_file_count": len(files),
                    "xls_file_count": sum(1 for item in files if _excel_file_format(item) == "xls"),
                }
            )
        elif normalized_surface == "word":
            task_snapshot.update(
                {
                    "word_output": settings.word_output.model_dump(mode="json"),
                    "word_batch": settings.word_batch.model_dump(mode="json"),
                    "word_review": settings.word_review.model_dump(mode="json"),
                    "word_conversion": settings.word_conversion.model_dump(mode="json"),
                    "tm": {"max_len": settings.tm.max_len},
                    "doc_conversion_mode": selected_options.doc_conversion_mode,
                    "selected_file_count": len(files),
                    "doc_file_count": sum(1 for item in files if _word_file_format(item) == "doc"),
                }
            )
        else:
            task_snapshot.update(
                {
                    "pdf": settings.pdf.model_dump(mode="json"),
                    "pdf_output": settings.pdf_output.model_dump(mode="json"),
                    "selected_file_count": len(files),
                    "pdf_file_count": sum(1 for item in files if getattr(item, "source_type", "pdf") == "pdf"),
                    "image_file_count": sum(1 for item in files if getattr(item, "source_type", "pdf") == "image"),
                    "tm": {"enabled": False},
                }
            )
        return self._prepared_from_context(
            surface=normalized_surface,
            source_path=str(root),
            source_label=f"{len(files)} selected input file(s)",
            files=files,
            settings=settings,
            options=selected_options,
            source_lang=source_selection,
            task_snapshot=task_snapshot,
            context=context,
        )

    def _prepared_from_context(
        self,
        *,
        surface: TaskSurface,
        source_path: str,
        source_label: str,
        files: list[Any],
        settings: AppSettings,
        options: TaskOptions,
        source_lang: str,
        task_snapshot: dict[str, object],
        context: Any,
    ) -> PreparedTask:
        role_groups = dict(getattr(context, "role_groups", {}) or {})
        group_capacities = dict(getattr(context, "group_concurrency", {}) or {})
        if not group_capacities:
            groups = tuple(getattr(context, "api_groups", ()) or ())
            group_capacities = {group: 1 for group in groups}
        # A configuration that cannot identify a connection must never be
        # silently considered independent of all other unknown connections.
        if not group_capacities:
            group_capacities = {("unknown", "connection"): 1}
        fingerprint_payload = {
            "surface": surface,
            "snapshot": task_snapshot,
            "models": dict(getattr(context, "model_snapshot", {}) or {}),
            "groups": sorted((repr(key), value) for key, value in group_capacities.items()),
            "options": {
                "untranslated_only": options.untranslated_only,
                "protect_scheme_cover": options.protect_scheme_cover,
                "allow_xls_fallback": options.allow_xls_fallback,
                "allow_doc_fallback": options.allow_doc_fallback,
                "include_images": options.include_images,
                "source_lang": source_lang,
                "target_lang": options.target_lang,
                "lang_pair": options.lang_pair,
            },
        }
        fingerprint = hashlib.sha256(
            json.dumps(fingerprint_payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        return PreparedTask(
            surface=surface,
            source_path=source_path,
            source_label=source_label,
            files=files,
            settings=settings,
            options=options,
            source_lang=source_lang,
            task_snapshot=task_snapshot,
            model_snapshot=dict(getattr(context, "model_snapshot", {}) or {}),
            key_overrides=dict(getattr(context, "key_overrides", {}) or {}),
            role_groups=role_groups,
            group_capacities=group_capacities,
            fingerprint=fingerprint,
        )

    def _start_prepared(
        self,
        prepared: PreparedTask,
        *,
        expected_revision: int | None,
    ) -> dict[str, Any]:
        task_id = uuid.uuid4().hex
        attempted = self._registry.reserve_task(
            owner_key=task_id,
            owner_label=_LABEL_BY_SURFACE[prepared.surface],
            task_type=prepared.surface,
            group_capacities=prepared.group_capacities,
            expected_revision=expected_revision,
        )
        if attempted.lease is None:
            reason = attempted.reason or "conflict"
            messages = {
                "surface_busy": "同类型任务仍处于活动状态。",
                "stale": "风险确认期间任务资源已变化，请重新预检。",
            }
            raise TaskConflictError(messages.get(reason, "任务资源预约失败。"), reason=reason)
        lease = attempted.lease
        try:
            api_schedulers = {
                role: lease.scheduler_for(group)
                for role, group in prepared.role_groups.items()
            }
            if prepared.surface == "tm_clean":
                runner = self._build_clean_runner(
                    lang_pair=str(prepared.options.lang_pair or ""),
                    settings=prepared.settings,
                    key_overrides=prepared.key_overrides,
                    api_scheduler=api_schedulers.get("cleaner"),
                )
            else:
                source = Path(prepared.source_path)
                runner = self._build_runner(
                    surface=prepared.surface,
                    files=prepared.files,
                    settings=prepared.settings,
                    source_root=source if source.is_dir() else source.parent,
                    options=prepared.options,
                    source_lang=prepared.source_lang,
                    key_overrides=prepared.key_overrides,
                    api_schedulers=api_schedulers,
                )
            task = ApiTask(
                task_id=task_id,
                surface=prepared.surface,
                source_path=prepared.source_path,
                source_label=prepared.source_label,
                runner=runner,
                lease=lease,
                created_at=time.time(),
                model_snapshot=prepared.model_snapshot,
                role_groups=prepared.role_groups,
                task_snapshot=prepared.task_snapshot,
            )
            with self._lock:
                self._tasks[task_id] = task
            self._append_event(
                task,
                "start",
                {
                    "state": "running",
                    "model_snapshot": task.model_snapshot,
                    "task_snapshot": task.task_snapshot,
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

    def _build_clean_runner(self, **kwargs: Any) -> Runner:
        return TmCleaningTaskRunner(**kwargs)

    def _connection_summaries(self, context: Any) -> list[dict[str, object]]:
        values: list[dict[str, object]] = []
        for role, snapshot in dict(getattr(context, "model_snapshot", {}) or {}).items():
            if not isinstance(snapshot, dict):
                continue
            values.append(
                {
                    "role": role,
                    "connection_id": str(snapshot.get("connection_id") or "unknown"),
                    "mode": str(snapshot.get("mode") or ""),
                    "provider": str(snapshot.get("provider") or ""),
                    "base_url": str(snapshot.get("base_url") or ""),
                    "throughput": _json_safe(snapshot.get("throughput") or {}),
                }
            )
        return values

    def _risk_payload(self, prepared: PreparedTask, risk: dict[str, object]) -> dict[str, object]:
        active_by_id = {task.task_id: task for task in self._tasks.values() if not task.terminal}
        shared_connections: list[dict[str, object]] = []
        for item in list(risk.get("shared_groups") or []):
            if not isinstance(item, dict):
                continue
            resource = item.get("resource")
            roles = [role for role, group in prepared.role_groups.items() if group == resource]
            matching = [
                snapshot
                for role, snapshot in prepared.model_snapshot.items()
                if role in roles and isinstance(snapshot, dict)
            ]
            snapshot = matching[0] if matching else {}
            shared_connections.append(
                {
                    "connection_id": str(snapshot.get("connection_id") or "unknown"),
                    "summary": {
                        "mode": str(snapshot.get("mode") or "unknown"),
                        "provider": str(snapshot.get("provider") or "unknown"),
                        "base_url": str(snapshot.get("base_url") or ""),
                    },
                    "roles": roles,
                    "active_concurrency": int(item.get("active_capacity") or 0),
                    "candidate_concurrency": int(item.get("candidate_capacity") or 0),
                    "total_potential_concurrency": int(item.get("total_potential_capacity") or 0),
                }
            )
        active_tasks = [
            {
                "task_id": task.task_id,
                "surface": task.surface,
                "state": task.state,
                "source_label": task.source_label,
                "frozen_connections": task.task_snapshot.get("connections", []),
            }
            for task in active_by_id.values()
        ]
        return {
            "active_tasks": active_tasks,
            "shared_connections": shared_connections,
            "warnings": (
                ["共享 API 可能触发 429、排队、超时、失败或额外费用。"]
                if shared_connections
                else []
            ),
        }

    def _purge_expired_tokens_locked(self) -> None:
        now = time.time()
        self._confirmation_tokens = {
            token: value
            for token, value in self._confirmation_tokens.items()
            if value.expires_at > now
        }

    def task_status(self, task_id: str) -> dict[str, Any]:
        task = self._get_task(task_id)
        with task.condition:
            return self._status_payload(task, include_result=True)

    def _status_payload(self, task: ApiTask, *, include_result: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "task_id": task.task_id,
            "surface": task.surface,
            # Source paths are never exposed through task-center APIs.  The
            # UI receives an anonymous count/label and can use output refs for
            # local operations after completion.
            "source_label": task.source_label,
            "state": task.state,
            "terminal": task.terminal,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
            "model_snapshot": task.model_snapshot,
            "task_snapshot": task.task_snapshot,
            "logs": list(task.logs),
        }
        if include_result:
            result = _sanitize_task_data(task.result or {})
            if isinstance(result, dict):
                result["local_operations"] = _local_operation_descriptors(result)
            payload["result"] = result
        return _sanitize_task_data(payload)

    def _persist_task(self, task: ApiTask) -> None:
        with task.condition:
            record = self._status_payload(task, include_result=True)
        self._history.upsert(record)

    def list_tasks(self) -> dict[str, list[dict[str, Any]]]:
        with self._lock:
            active = [
                self._status_payload(task, include_result=False)
                for task in self._tasks.values()
                if not task.terminal
            ]
        return {"active": active, "recent": self._history.records()}

    def task_results(self, task_id: str) -> dict[str, Any]:
        try:
            task = self._get_task(task_id)
        except TaskNotFoundError:
            for item in self._history.records():
                if str(item.get("task_id") or "") == str(task_id):
                    return item
            raise
        with task.condition:
            return self._status_payload(task, include_result=True)

    def mark_active_tasks_interrupted(self) -> list[str]:
        """Mark live sidecar state non-resumable before a hard restart."""
        with self._lock:
            tasks = [task for task in self._tasks.values() if not task.terminal]
        interrupted: list[str] = []
        for task in tasks:
            with task.condition:
                if task.terminal:
                    continue
                task.state = "interrupted"
                task.terminal = True
                task.updated_at = time.time()
                task.result = {
                    "message": "应用或 sidecar 已中断；请依据已有产物或报告新建任务。",
                    "recovery": {"can_resume": False, "reason": "sidecar_restarted"},
                }
            task.lease.release()
            self._append_event(task, "interrupted", task.result)
            interrupted.append(task.task_id)
        return interrupted

    def stop_task(self, task_id: str) -> dict[str, Any]:
        task = self._get_task(task_id)
        with task.condition:
            if task.terminal:
                return self.task_status(task_id)
            task.state = "stopping"
        task.runner.stop()
        self._append_event(task, "stopping", {"state": "stopping"})
        return self.task_status(task_id)

    def pause_task(self, task_id: str) -> dict[str, Any]:
        """Pause PDF page submission while its sidecar and snapshot remain alive."""
        task = self._get_task(task_id)
        if task.surface != "pdf":
            raise TaskInputError("只有 PDF/图片任务支持暂停提交。")
        with task.condition:
            if task.terminal:
                return self.task_status(task_id)
            if not hasattr(task.runner, "pause"):
                raise TaskInputError("当前 PDF 任务不支持暂停提交。")
            if task.state == "paused":
                return self.task_status(task_id)
            if task.state != "running":
                raise TaskInputError("当前 PDF/图片任务不处于可暂停状态。")
            task.state = "paused"
            self._append_event(task, "paused", {"state": "paused"})
        task.runner.pause()  # type: ignore[attr-defined]
        return self.task_status(task_id)

    def resume_task(self, task_id: str) -> dict[str, Any]:
        """Resume a same-sidecar PDF task with its frozen settings."""
        task = self._get_task(task_id)
        if task.surface != "pdf":
            raise TaskInputError("只有 PDF/图片任务支持继续。")
        with task.condition:
            if task.terminal:
                raise TaskInputError("任务已经结束，不能继续。")
            if not hasattr(task.runner, "resume"):
                raise TaskInputError("当前 PDF 任务不支持继续。")
            if task.state != "paused":
                raise TaskInputError("只有暂停提交的 PDF/图片任务可以继续。")
            task.state = "running"
            self._append_event(task, "resumed", {"state": "running"})
        task.runner.resume()  # type: ignore[attr-defined]
        return self.task_status(task_id)

    def end_paused_task(self, task_id: str) -> dict[str, Any]:
        """End an intentionally paused PDF task and retain its partial evidence."""
        task = self._get_task(task_id)
        if task.surface != "pdf":
            raise TaskInputError("只有 PDF/图片任务支持结束暂停任务。")
        with task.condition:
            if task.terminal:
                return self.task_status(task_id)
            if task.state != "paused":
                raise TaskInputError("只有暂停提交的 PDF/图片任务可以结束。")
            task.state = "stopping"
            self._append_event(task, "stopping", {"state": "stopping", "reason": "end_paused"})
        if hasattr(task.runner, "end_paused"):
            task.runner.end_paused()  # type: ignore[attr-defined]
        else:
            task.runner.stop()
        return self.task_status(task_id)

    def reservations(self) -> list[dict[str, Any]]:
        return self.resource_groups()

    def resource_groups(self) -> list[dict[str, Any]]:
        """Expose live group budgets without raw tuples or credential hashes."""
        groups: dict[object, dict[str, Any]] = {}
        with self._lock:
            active = [task for task in self._tasks.values() if not task.terminal]
        for task in active:
            for role, snapshot in task.model_snapshot.items():
                if not isinstance(snapshot, dict):
                    continue
                connection_id = str(snapshot.get("connection_id") or "unknown")
                key = connection_id
                entry = groups.setdefault(
                    key,
                    {
                        "connection_id": connection_id,
                        "summary": {
                            "mode": str(snapshot.get("mode") or "unknown"),
                            "provider": str(snapshot.get("provider") or "unknown"),
                            "base_url": str(snapshot.get("base_url") or ""),
                        },
                        "capacity": 0,
                        "active_weight": 0,
                        "tasks": [],
                    },
                )
                scheduler = task.lease.scheduler_for(task.role_groups.get(role))
                if scheduler is not None:
                    snapshot_value = scheduler.snapshot()
                    entry["capacity"] = snapshot_value.capacity
                    entry["active_weight"] = snapshot_value.active_total_weight
                entry["tasks"].append({"task_id": task.task_id, "surface": task.surface, "role": role})
            if not task.model_snapshot:
                # Test doubles and malformed legacy configurations can lack a
                # resolved role snapshot.  They remain conservative/visible
                # without publishing the underlying opaque resource tuple.
                for scheduler in task.lease._schedulers.values():  # noqa: SLF001
                    snapshot_value = scheduler.snapshot()
                    key = f"unknown-{task.task_id[:12]}"
                    entry = groups.setdefault(
                        key,
                        {
                            "connection_id": "unknown",
                            "summary": {"mode": "unknown", "provider": "unknown", "base_url": ""},
                            "capacity": snapshot_value.capacity,
                            "active_weight": snapshot_value.active_total_weight,
                            "tasks": [],
                        },
                    )
                    entry["tasks"].append({"task_id": task.task_id, "surface": task.surface, "role": "unknown"})
        return list(groups.values())

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
        api_schedulers: dict[str, Any] | None = None,
    ) -> Runner:
        api_schedulers = dict(api_schedulers or {})
        if surface == "excel":
            return TaskRunner(
                files,
                settings,
                source_root=source_root,
                allow_xls_fallback=options.allow_xls_fallback,
                source_lang=source_lang,
                key_overrides=key_overrides,
                api_scheduler=api_schedulers.get("translation"),
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
                allow_doc_fallback=options.allow_doc_fallback,
                api_scheduler=api_schedulers.get("translation"),
            )
        return PdfImageTranslationRunner(
            files,
            settings,
            source_root=source_root,
            key_overrides=key_overrides,
            api_scheduler=api_schedulers.get("image"),
            review_api_scheduler=api_schedulers.get("pdf_review"),
        )

    @staticmethod
    def _validate_excel_preflight(
        *,
        files: list[Any],
        settings: AppSettings,
        options: TaskOptions,
    ) -> None:
        """Fail before task creation for deterministic Excel input/settings issues."""
        if not str(settings.target_lang or "").strip():
            raise TaskInputError("请先选择 Excel 目标语言。")
        model_check = check_translation_api_config(settings)
        if not model_check.ok:
            detail = f"（{model_check.detail}）" if model_check.detail else ""
            raise TaskInputError(f"{model_check.message}{detail}")
        excel_output = settings.excel_output
        if excel_output.use_custom_output_dir:
            output_error = bilingual_writer.get_custom_output_dir_error(
                excel_output.custom_output_dir
            )
            if output_error:
                raise TaskInputError(output_error)

        xls_files = [
            item
            for item in files
            if _excel_file_format(item) == "xls"
        ]
        if not xls_files or options.allow_xls_fallback:
            return
        available, reason = get_local_excel_availability()
        if available:
            return
        raise TaskInputError(
            "检测到 "
            f"{len(xls_files)} 个 .xls 文件，但本机 Microsoft Excel 高保真自动化不可用：{reason}。"
            "请取消任务，安装/授权 Microsoft Excel 后重试，或明确确认兼容转换；"
            "兼容转换可能损失复杂样式、合并单元格、图片、图表和宏。"
        )

    @staticmethod
    def _validate_word_preflight(
        *,
        files: list[Any],
        settings: AppSettings,
        options: TaskOptions,
    ) -> None:
        """Fail Word startup before allocating a task for known bad input.

        A selected legacy document is never silently routed through a
        compatibility converter.  The task either has a native Word path or a
        task-level, explicit ``allow_doc_fallback`` confirmation captured in
        its frozen snapshot.
        """
        if not str(settings.target_lang or "").strip():
            raise TaskInputError("请先选择 Word 目标语言。")
        model_check = check_translation_api_config(settings)
        if not model_check.ok:
            detail = f"（{model_check.detail}）" if model_check.detail else ""
            raise TaskInputError(f"{model_check.message}{detail}")
        word_output = settings.word_output
        if word_output.use_custom_output_dir:
            output_error = bilingual_writer.get_custom_output_dir_error(
                word_output.custom_output_dir
            )
            if output_error:
                raise TaskInputError(output_error)

        doc_files = [item for item in files if _word_file_format(item) == "doc"]
        if not doc_files or options.allow_doc_fallback:
            return
        available, reason = get_local_word_automation_availability()
        if available:
            return
        raise TaskInputError(
            "检测到 "
            f"{len(doc_files)} 个 .doc 文件，但本机 Microsoft Word 高保真自动化不可用：{reason}。"
            "请取消任务，安装/授权 Microsoft Word 后重试，或明确确认兼容转换；"
            "兼容转换可能改变版式、域、图文和宏。"
        )

    @staticmethod
    def _validate_pdf_preflight(
        *,
        files: list[Any],
        settings: AppSettings,
        options: TaskOptions,
    ) -> None:
        """Validate PDF/image-specific settings before allocating a task lease."""
        if not files:
            raise TaskInputError("请至少选择一个 PDF 或图片文件。")
        if not str(settings.pdf.target_lang or "").strip():
            raise TaskInputError("请先选择 PDF/图片目标语言。")
        pdf_output = settings.pdf_output
        if pdf_output.use_custom_output_dir:
            output_error = bilingual_writer.get_custom_output_dir_error(
                pdf_output.custom_output_dir
            )
            if output_error:
                raise TaskInputError(output_error)
        try:
            image_model = resolve_effective_model_config(settings, ROLE_IMAGE)
        except Exception as exc:  # noqa: BLE001 - give UI the resolved contract error.
            raise TaskInputError(f"PDF 翻译模型配置不可用：{exc}") from exc
        if not image_model.model:
            raise TaskInputError("请先填写 PDF 翻译模型名称。")
        if not provider_supports_capability(image_model.provider, "image"):
            raise TaskInputError(
                f"当前 PDF 翻译模型服务商不支持图像生成能力：{image_model.provider}"
            )
        if image_model.mode == "cloud" and not image_model.api_key:
            raise TaskInputError("PDF 翻译模型尚未配置 API Key。")
        if not settings.pdf.review_enabled:
            return
        try:
            review_model = resolve_effective_model_config(settings, ROLE_PDF_REVIEW)
        except Exception as exc:  # noqa: BLE001
            raise TaskInputError(f"PDF 翻译审核模型配置不可用：{exc}") from exc
        if not review_model.model:
            raise TaskInputError("已启用逐页审核，请先填写 PDF 翻译审核模型名称。")
        if not provider_supports_capability(review_model.provider, "vision_text"):
            raise TaskInputError(
                f"当前 PDF 翻译审核模型服务商不支持视觉理解能力：{review_model.provider}"
            )
        if review_model.mode == "cloud" and not review_model.api_key:
            raise TaskInputError("已启用逐页审核，请先配置审核模型 API Key。")
        availability = str(settings.pdf_review_model_role.availability_status or "unknown")
        if availability == "unavailable" and not options.allow_known_review_failure:
            raise TaskInputError(
                "PDF 翻译审核模型当前配置已测试失败；请重新测试、关闭审核，或明确确认继续。"
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
            issues = payload.get("issues") if isinstance(payload, dict) else None
            has_issues = bool(issues)
            self._finish_if_needed(
                task,
                "completed_with_issues" if has_issues else "done",
                "completed_with_issues" if has_issues else event_type,
                payload,
            )
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
            task.updated_at = time.time()
            task.result = _sanitize_task_data(result)
        task.lease.release()
        self._append_event(task, event_type, task.result)

    def _append_event(self, task: ApiTask, event_type: str, data: Any) -> None:
        safe_data = _sanitize_task_data(data)
        if event_type == "log":
            level = "INFO"
            if isinstance(safe_data, dict):
                level = str(safe_data.get("level") or level)
            # A runner log line may contain a filename or model-supplied text.
            # Do not expose it via the replayable SSE buffer at all.
            safe_data = {
                "level": level,
                "stage": "runner",
                "message": "任务运行日志已脱敏。",
            }
        with task.condition:
            task.events.append(
                {
                    "id": task.next_event_id,
                    "type": event_type,
                    "data": safe_data,
                }
            )
            if event_type == "log":
                # Runner strings may contain file names, source fragments or
                # provider output.  Keep only structured observability here.
                task.logs.append(
                    {
                        "event_id": task.next_event_id,
                        "level": str(safe_data.get("level") or "INFO")
                        if isinstance(safe_data, dict)
                        else "INFO",
                        "stage": "runner",
                        "message": "任务运行日志已脱敏。",
                    }
                )
            task.updated_at = time.time()
            task.next_event_id += 1
            task.condition.notify_all()
        self._persist_task(task)

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


def _excel_file_format(item: Any) -> str:
    """Return a normalized Excel format without assuming a concrete item class.

    The public scanner returns ``FileItem`` instances, but task-manager unit
    tests and future adapters may provide a minimal file-like object.  The
    startup preflight must therefore treat absent metadata as non-legacy
    rather than crash before acquiring the task resource.
    """
    explicit = str(getattr(item, "format", "") or "").strip().lower()
    if explicit:
        return explicit.lstrip(".")
    path = getattr(item, "path", None)
    return Path(path).suffix.lower().lstrip(".") if path else ""


def _word_file_format(item: Any) -> str:
    """Return a normalized Word format without assuming a concrete scanner type."""
    explicit = str(getattr(item, "format", "") or "").strip().lower()
    if explicit:
        return explicit.lstrip(".")
    path = getattr(item, "path", None)
    return Path(path).suffix.lower().lstrip(".") if path else ""


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


_SENSITIVE_VALUE_KEYS = {
    "api_key",
    "key_overrides",
    "source_text",
    "target_text",
    "old_target",
    "new_target",
    "raw_text",
    "raw_response",
    "model_response",
    "prompt",
    "system_prompt",
    "user_prompt",
    "source_path",
    "original_path",
    "input_path",
    "source_file",
}
_ARTIFACT_PATH_KEYS = {
    "output_dir",
    "output_path",
    "translated_path",
    "report_path",
    "manifest_path",
    "custom_output_dir",
}
_ABSOLUTE_PATH_RE = re.compile(r"(?:(?:[A-Za-z]:)?[/\\])(?:[^\s'\"<>]+[/\\])*[^\s'\"<>]+")
_API_SECRET_RE = re.compile(r"(?i)(?:bearer\s+|sk-[a-z0-9_-]{8,}|api[_ -]?key\s*[:=]\s*)[^\s,;]+")


def _sanitize_task_data(value: Any, *, key: str = "") -> Any:
    """Remove content/credential/source-path fields from task-center data."""
    normalized_key = str(key or "").strip().lower()
    if normalized_key in _SENSITIVE_VALUE_KEYS:
        return None
    if isinstance(value, Path):
        return str(value) if normalized_key in _ARTIFACT_PATH_KEYS else None
    if is_dataclass(value):
        return _sanitize_task_data(asdict(value), key=key)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for child_key, child_value in value.items():
            child_name = str(child_key)
            lowered = child_name.lower()
            if lowered in _SENSITIVE_VALUE_KEYS:
                continue
            if lowered in {"source", "original", "translation", "translated", "content", "text"}:
                continue
            if "prompt" in lowered or "response" in lowered and lowered != "response_status":
                continue
            if lowered == "local_operations":
                result[child_name] = _sanitize_local_operations(child_value)
                continue
            sanitized = _sanitize_task_data(child_value, key=lowered)
            if sanitized is not None:
                result[child_name] = sanitized
        return result
    if isinstance(value, (list, tuple, set)):
        return [
            item
            for item in (_sanitize_task_data(item, key=key) for item in value)
            if item is not None
        ]
    if isinstance(value, str):
        if normalized_key in _ARTIFACT_PATH_KEYS:
            return value
        text = _API_SECRET_RE.sub("[redacted]", value)
        text = _ABSOLUTE_PATH_RE.sub("[path]", text)
        return text[:300]
    return _json_safe(value)


def _local_operation_descriptors(result: dict[str, Any]) -> list[dict[str, str]]:
    """Return declarative operations for the Tauri shell; never execute them."""
    operations: list[dict[str, str]] = []
    mappings = (
        ("open_output", "output_dir"),
        ("reveal_output", "output_path"),
        ("open_report", "report_path"),
        ("open_manifest", "manifest_path"),
        ("copy_output_path", "output_dir"),
    )
    for action, field_name in mappings:
        path = str(result.get(field_name) or "").strip()
        if path:
            operations.append({"action": action, "path": path})
    return operations


def _sanitize_local_operations(value: Any) -> list[dict[str, str]]:
    """Preserve only task-generated output artifact refs for Tauri actions."""
    if not isinstance(value, (list, tuple)):
        return []
    allowed_actions = {
        "open_output",
        "reveal_output",
        "open_report",
        "open_manifest",
        "copy_output_path",
    }
    operations: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        action = str(item.get("action") or "")
        path = str(item.get("path") or "")
        if action in allowed_actions and path:
            operations.append({"action": action, "path": path})
    return operations
