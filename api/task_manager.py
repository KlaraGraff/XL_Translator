"""Task lifecycle and SSE event storage for the local API."""

from __future__ import annotations

import json
import hashlib
import re
import threading
import time
import uuid
from collections.abc import Callable, Generator
from dataclasses import asdict, dataclass, field, is_dataclass, replace
from pathlib import Path
from typing import Any, Literal, Protocol

from core import tm_manager
from core import bilingual_writer
from core.api_config_check import check_translation_api_config
from core.file_scanner import scan_path
from core.file_progress import FileProgressMsg, file_progress_entry, file_progress_id
from core.model_api_identity import task_api_context_for_page
from core.engine_dispatcher import activate_translation_surface
from core.language_registry import get_target_lang_display, normalize_source_selection
from core.model_roles import (
    ROLE_IMAGE,
    ROLE_PDF_REVIEW,
    provider_supports_capability,
    resolve_effective_model_config,
)
from core.engine_dispatcher import build_role_engine
from core.pdf_image_translation import (
    PdfImageTranslationRunner,
    PdfPageActionError,
    scan_pdf_path,
)
from core.task_logger import redact_absolute_paths, sanitize_task_log_message
from core.path_utils import normalize_user_path
from core.task_resources import ScheduledTaskLease, TaskResourceRegistry
from core.task_history import TaskHistoryError, TaskHistoryStore
from loguru import logger
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
    user_facing_reason,
)
from core.word_document import scan_word_path
from core.word_task_runner import WordTaskRunner
from core.cad_task_runner import CadTaskRunner
from core.cad_translation import CadPipelineOptions, SubprocessCadConverter, scan_cad_paths
from api.cad_plugin import probe_status
from core.word_converter import get_local_word_automation_availability
from core.xls_converter import (
    describe_xls_compatibility_consequence,
    get_local_excel_availability,
    libreoffice_xls_conversion_available,
)
from settings import AppSettings, load_settings

TaskSurface = Literal["excel", "word", "pdf", "cad", "tm_clean"]

_PAGE_BY_SURFACE = {
    "excel": "excel_translate",
    "word": "word_translate",
    "pdf": "pdf_translate",
    "cad": "cad_translate",
    "tm_clean": "tm_clean",
}
_LABEL_BY_SURFACE = {
    "excel": "Excel translation",
    "word": "Word translation",
    "pdf": "PDF translation",
    "cad": "CAD drawing translation",
    "tm_clean": "TM cleaning",
}

# Shortest gap between two history writes for the same task while it runs.
# Every event used to trigger a full sanitize + read + rewrite of the history
# file, so a task emitting thousands of events paid O(n^2) for a summary that
# only has to be roughly current until the task ends.
HISTORY_WRITE_INTERVAL_SECONDS = 1.0
# How many finished tasks keep an in-memory record.  Older ones are dropped:
# their summary, result and logs are already on disk, and the task center
# falls back to that record.
MAX_RETAINED_TERMINAL_TASKS = 12
# Events kept for replay after a task ends.  The terminal event is the last
# one, so a late SSE consumer still sees how the task finished.
TERMINAL_EVENT_TAIL = 300


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

    def __init__(self, message: str, *, reason: str = "invalid_input") -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class TaskOptions:
    untranslated_only: bool = False
    protect_front_matter: bool = False
    translate_headers_footers: bool = False
    allow_xls_fallback: bool = False
    allow_doc_fallback: bool = False
    include_images: bool = False
    source_lang: str | None = None
    target_lang: str | None = None
    allow_known_review_failure: bool = False
    lang_pair: str | None = None
    # 「接着上次继续」时上一次任务的输出目录；runner 只读它，不往里写。
    resume_output_dir: str | None = None
    # CAD-only task settings. These are deliberately separate from the
    # document output suffix settings: CAD always creates a sibling output
    # file/directory and never exposes a filename-suffix option.
    cad_output_dir: str | None = None
    cad_use_terminology: bool = True
    cad_keep_work_dxf: bool = True
    cad_copy_related_files: bool = False
    cad_verify_roundtrip: bool = True
    cad_scan_replacement_chars: bool = True
    cad_include_block_text: bool = True
    cad_glossary_path: str | None = None
    cad_use_memory: bool = True
    cad_check_entity_counts: bool = True
    cad_scan_residual: bool = True
    cad_translate_output_filename: bool = False

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
    # Kept so a finished PDF task can reserve the same shared API groups again
    # for a single-page rerun.  The lease taken at start time is released the
    # moment the task ends, and its schedulers stop working with it.
    group_capacities: dict[object, int] = field(default_factory=dict)
    task_snapshot: dict[str, object] = field(default_factory=dict)
    file_progress: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Used only to match runner results before sanitization; never serialized.
    file_sources: dict[str, str] = field(default_factory=dict)
    state: str = "running"
    result: dict[str, Any] | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    next_event_id: int = 1
    terminal: bool = False
    updated_at: float = field(default_factory=time.time)
    logs: list[dict[str, Any]] = field(default_factory=list)
    progress: dict[str, Any] = field(default_factory=dict)
    condition: threading.Condition = field(default_factory=threading.Condition)
    # History throttling bookkeeping; only touched under ``condition``.
    last_persisted_at: float = 0.0
    last_persisted_state: str = ""
    history_dirty: bool = False
    # A finished PDF task regenerating one page.  Deliberately *not* a task
    # state: every active-task query filters on ``terminal``, and flipping this
    # task back to running would put it into the task center's concurrency
    # accounting, the busy-connection set and the risk payload all over again.
    rerun_active: bool = False
    # 与 ``rerun_active`` 同生同灭：占着锁的是哪种操作（"rerun" / "restore"）。
    # 只用来把 409 文案说准——「有一页在重新生成」和「有一页在换回上一版」
    # 对用户是两回事，等待时长也差一个量级。
    rerun_kind: str = ""


class RetiredRunner:
    """Stand-in left behind when a finished task's runner is released.

    Keeping the real runner alive is what made ``_tasks`` leak: it holds the
    scanned file list, a deep copy of settings and every PDF page record.
    Nothing reads a runner once a task is terminal, but the attribute must
    stay callable so a stray ``stop()`` cannot raise.
    """

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def needs_poll(self) -> bool:
        return False

    def get_message(self, timeout: float = 0.05) -> Any:
        return None


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
    # Frozen per-role fallback chain handed to the runner.
    connection_chains: dict[str, tuple[str, ...]] = field(default_factory=dict)


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
        # Set once the process is on its way out; SSE loops check it so an
        # open stream cannot hold up a graceful shutdown.
        self._shutdown = threading.Event()
        # A complete sidecar restart cannot safely resume a frozen runner.  A
        # prior process may have recorded an active summary, so close that
        # state before this manager accepts fresh work.
        self._history_warning_tasks: set[str] = set()
        self._pending_history: dict[str, dict[str, Any]] = {}
        self._pending_history_lock = threading.RLock()
        try:
            self._history.mark_active_interrupted()
        except (TaskHistoryError, OSError):
            logger.warning("任务历史暂时无法保存；本次任务仍可正常执行。")

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
        source_path = str(normalize_user_path(str(source_path or "")) or "")
        selected_paths = [
            str(normalize_user_path(str(path)))
            for path in (selected_paths or [])
            if str(path or "").strip()
        ]
        selected_options = options or TaskOptions()
        option_paths = {
            "resume_output_dir": normalize_user_path(selected_options.resume_output_dir),
            "cad_output_dir": normalize_user_path(selected_options.cad_output_dir),
            "cad_glossary_path": normalize_user_path(selected_options.cad_glossary_path),
        }
        if any(option_paths[key] != getattr(selected_options, key) for key in option_paths):
            selected_options = replace(selected_options, **option_paths)
        settings = self._settings_loader().model_copy(deep=True)
        activate_translation_surface(settings, normalized_surface)

        if normalized_surface == "tm_clean":
            lang_pair = str(selected_options.lang_pair or source_path or "").strip()
            if "-" not in lang_pair or not all(part.strip() for part in lang_pair.split("-", 1)):
                raise TaskInputError("TM 清洗任务必须选择有效的定向语言对。")
            tm_manager.init_db()
            context = task_api_context_for_page(
                settings,
                _PAGE_BY_SURFACE[normalized_surface],
                busy_connection_ids=self._busy_connection_ids(),
            )
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
                source_label=f"语言对 {lang_pair}",
                files=[],
                settings=settings,
                options=TaskOptions(**{**selected_options.__dict__, "lang_pair": lang_pair}),
                source_lang="",
                task_snapshot=task_snapshot,
                context=context,
            )

        if not str(source_path or "").strip():
            # Path("").resolve() is the process cwd; never scan that.
            raise TaskInputError("Source path is required.")
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

        selected = {
            str(Path(path).expanduser().resolve())
            for path in (selected_paths or [])
            if str(path or "").strip()
        }
        if normalized_surface == "cad":
            files = self._scan(
                root,
                normalized_surface,
                selected_options,
                selected_paths=selected if selected else None,
            )
        else:
            # Keep the established call shape for other surfaces; tests and
            # integrations may provide a narrow scan adapter for them.
            files = self._scan(root, normalized_surface, selected_options)
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
        elif normalized_surface == "cad":
            self._validate_cad_preflight(files=files, settings=settings)
        else:
            self._validate_pdf_preflight(files=files, settings=settings, options=selected_options)
        if normalized_surface in {"excel", "word"}:
            tm_manager.init_db()
        elif normalized_surface == "cad":
            tm_manager.init_db()

        context = task_api_context_for_page(
                settings,
                _PAGE_BY_SURFACE[normalized_surface],
                busy_connection_ids=self._busy_connection_ids(),
            )
        prompt_source = "|".join(
            (
                str(getattr(settings, "domain_preset", "") or ""),
                str(getattr(settings, "custom_prompt", "") or ""),
                str(getattr(settings, "domain_prompt_overrides", {}) or {}),
                str(
                    getattr(
                        settings,
                        f"{normalized_surface}_domain_custom_prompts",
                        {},
                    )
                    or {}
                ),
                str(
                    getattr(
                        settings,
                        f"{normalized_surface}_domain_disabled_presets",
                        [],
                    )
                    or []
                ),
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
        elif normalized_surface == "cad":
            glossary_signature = ""
            if selected_options.cad_glossary_path:
                glossary_path = Path(selected_options.cad_glossary_path).expanduser()
                try:
                    glossary_signature = hashlib.sha256(glossary_path.read_bytes()).hexdigest()
                except OSError:
                    glossary_signature = "missing"
            task_snapshot.update(
                {
                    "selected_file_count": len(files),
                    "cad_output_dir": selected_options.cad_output_dir or "",
                    "cad_checks": {
                        "verify_roundtrip": selected_options.cad_verify_roundtrip,
                        "scan_replacement_chars": selected_options.cad_scan_replacement_chars,
                        "check_entity_counts": selected_options.cad_check_entity_counts,
                        "scan_residual": selected_options.cad_scan_residual,
                    },
                    "cad_options": {
                        "use_terminology": selected_options.cad_use_terminology,
                        "keep_work_dxf": selected_options.cad_keep_work_dxf,
                        "copy_related_files": selected_options.cad_copy_related_files,
                        "include_block_text": selected_options.cad_include_block_text,
                        "glossary_path": selected_options.cad_glossary_path or "",
                        "glossary_sha256": glossary_signature,
                    },
                    "tm": {"enabled": selected_options.cad_use_memory, "priority": "user_memory_first"},
                }
            )
            task_snapshot["target_lang_display"] = get_target_lang_display(settings.target_lang)
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
            source_label=_selected_files_label(files),
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
                "protect_front_matter": options.protect_front_matter,
                "translate_headers_footers": options.translate_headers_footers,
                "allow_xls_fallback": options.allow_xls_fallback,
                "allow_doc_fallback": options.allow_doc_fallback,
                "include_images": options.include_images,
                "source_lang": source_lang,
                "target_lang": options.target_lang,
                "lang_pair": options.lang_pair,
                # 续译任务和全量任务即使源相同也不算重复提交；指纹整体会被
                # sha256 掉，这里的路径不会以明文进历史。
                "resume_output_dir": options.resume_output_dir,
            },
        }
        if surface == "cad":
            cad_files = []
            for item in files:
                path = Path(getattr(item, "path", ""))
                try:
                    stat = path.stat()
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                    cad_files.append({"path": str(path.resolve()), "sha256": digest, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
                except OSError:
                    cad_files.append({"path": str(path.resolve()), "missing": True})
            fingerprint_payload["cad_content_identity"] = cad_files
            fingerprint_payload["options"]["cad"] = {
                key: getattr(options, key)
                for key in ("cad_use_terminology", "cad_keep_work_dxf", "cad_copy_related_files", "cad_verify_roundtrip", "cad_scan_replacement_chars", "cad_include_block_text", "cad_glossary_path", "cad_use_memory", "cad_check_entity_counts", "cad_scan_residual", "cad_translate_output_filename")
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
            connection_chains={
                role: tuple(chain)
                for role, chain in dict(
                    getattr(context, "role_connection_chains", {}) or {}
                ).items()
            },
        )

    def _start_prepared(
        self,
        prepared: PreparedTask,
        *,
        expected_revision: int | None,
    ) -> dict[str, Any]:
        task_id = uuid.uuid4().hex
        owner_label = _LABEL_BY_SURFACE[prepared.surface]
        if prepared.surface == "cad":
            owner_label = f"{owner_label} → {get_target_lang_display(str(prepared.task_snapshot.get('target_lang') or ''))}"
        attempted = self._registry.reserve_task(
            owner_key=task_id,
            owner_label=owner_label,
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
                    connection_chains=prepared.connection_chains,
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
                group_capacities=dict(prepared.group_capacities),
                task_snapshot=prepared.task_snapshot,
            )
            source = Path(prepared.source_path)
            source_root = source if source.is_dir() else source.parent
            for item in prepared.files:
                if getattr(item, "path", None) is None:
                    continue
                entry = file_progress_entry(item, source_root)
                task.file_progress[entry["file_id"]] = entry
                task.file_sources[entry["file_id"]] = str(item.path)
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
        except Exception as exc:
            lease.release()
            with self._lock:
                task = self._tasks.pop(task_id, None)
            if task is not None:
                # The "start" event has already been persisted with
                # state="running"; leave a terminal record instead of a ghost
                # forever-running task in the history.
                with task.condition:
                    failure_result = self._complete_file_progress(
                        task, {"message": str(exc) or exc.__class__.__name__}, "error",
                    )
                    task.state = "error"
                    task.terminal = True
                    task.updated_at = time.time()
                    task.result = _sanitize_task_data(failure_result)
                    # Terminal flag and terminal event must be visible together.
                    self._append_event(task, "error", task.result)
            raise

    def _build_clean_runner(self, **kwargs: Any) -> Runner:
        return TmCleaningTaskRunner(**kwargs)

    def _busy_connection_ids(self) -> frozenset[str]:
        """Return the pool entries active tasks are already running on.

        Read from the frozen snapshots rather than from settings, so a task
        that started before the pool was edited still counts as occupying the
        connection it actually uses.
        """
        busy: set[str] = set()
        with self._lock:
            active = [task for task in self._tasks.values() if not task.terminal]
        for task in active:
            for snapshot in (task.model_snapshot or {}).values():
                if not isinstance(snapshot, dict):
                    continue
                connection_id = str(snapshot.get("pool_connection_id") or "").strip()
                if connection_id:
                    busy.add(connection_id)
        return frozenset(busy)

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
                    # Which configured entry this run took, so a finished task
                    # can still say which connection produced its output.
                    "pool_connection_id": str(snapshot.get("pool_connection_id") or ""),
                    "pool_connection_label": str(
                        snapshot.get("pool_connection_label") or ""
                    ),
                    "pool_connection_count": len(
                        list(snapshot.get("pool_connection_chain") or [])
                    ),
                    "throughput": _json_safe(snapshot.get("throughput") or {}),
                }
            )
        return values

    def _risk_payload(self, prepared: PreparedTask, risk: dict[str, object]) -> dict[str, object]:
        with self._lock:
            active_by_id = {
                task.task_id: task for task in self._tasks.values() if not task.terminal
            }
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
        try:
            task = self._get_task(task_id)
        except TaskNotFoundError:
            # The in-memory record of a long-finished task is dropped to bound
            # memory; its persisted summary still answers the task center.
            record = self._history_record(task_id)
            if record is None:
                raise
            return record
        with task.condition:
            return self._status_payload(task, include_result=True)

    def _history_record(self, task_id: str) -> dict[str, Any] | None:
        wanted = str(task_id or "")
        with self._pending_history_lock:
            pending = self._pending_history.get(wanted)
            if pending is not None:
                return dict(pending)
        for item in self._history.records():
            if str(item.get("task_id") or "") == wanted:
                return item
        return None

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
            "file_progress": {
                "revision": max((entry.get("revision", 0) for entry in task.file_progress.values()), default=0),
                "files": list(task.file_progress.values()),
            },
            "progress": dict(task.progress),
        }
        if include_result:
            result = _sanitize_task_data(task.result or {})
            if isinstance(result, dict):
                result["local_operations"] = _local_operation_descriptors(result)
            payload["result"] = result
        return _sanitize_task_data(payload)

    def _write_task_history(self, task: ApiTask, record: dict[str, Any]) -> None:
        """History is optional bookkeeping, never a translation prerequisite."""
        failed = False
        # Serialize only history I/O and its retry cache. Never acquire a task
        # condition while holding this lock: event writers may already own it.
        with self._pending_history_lock:
            try:
                self._history.upsert(record)
            except (TaskHistoryError, OSError):
                failed = True
                pending = self._pending_history.get(task.task_id)
                if pending is None or pending.get("updated_at", 0) <= record.get("updated_at", 0):
                    self._pending_history.pop(task.task_id, None)
                    self._pending_history[task.task_id] = record
                while len(self._pending_history) > 200:
                    oldest = next(iter(self._pending_history))
                    self._pending_history.pop(oldest)
                    self._history_warning_tasks.discard(oldest)
                if task.task_id not in self._history_warning_tasks:
                    self._history_warning_tasks.add(task.task_id)
                    logger.warning("任务历史暂时无法保存；任务继续执行，结果保留在本次运行中。")
            else:
                pending = self._pending_history.get(task.task_id)
                if pending is not None and pending.get("updated_at", 0) <= record.get("updated_at", 0):
                    self._pending_history.pop(task.task_id, None)
                self._history_warning_tasks.discard(task.task_id)
        with task.condition:
            if failed:
                task.history_dirty = True
            else:
                task.last_persisted_at = time.time()
                task.last_persisted_state = str(record["state"])
                task.history_dirty = task.updated_at != record["updated_at"]

    def _retry_pending_history(self, key: str, record: dict[str, Any]) -> None:
        with self._pending_history_lock:
            # A successful newer write may have removed this snapshot while
            # flush was waiting, or a newer failed write may have replaced it.
            if self._pending_history.get(key) is not record:
                return
            try:
                self._history.upsert(record)
            except (TaskHistoryError, OSError):
                return
            self._pending_history.pop(key, None)
            self._history_warning_tasks.discard(key)

    def _persist_task(self, task: ApiTask) -> None:
        with task.condition:
            record = self._status_payload(task, include_result=True)
            task.history_dirty = True
        self._write_task_history(task, record)

    def list_tasks(self) -> dict[str, Any]:
        with self._lock:
            active = [
                self._status_payload(task, include_result=False)
                for task in self._tasks.values()
                if not task.terminal
            ]
        with self._pending_history_lock:
            unsaved = [dict(record) for record in self._pending_history.values() if record["terminal"]]
        recent = {record["task_id"]: record for record in self._history.records()}
        recent.update({record["task_id"]: record for record in unsaved})
        return {"active": active, "recent": sorted(recent.values(),
                key=lambda record: record.get("updated_at", 0), reverse=True)[:200],
                "active_work_count": self.active_task_count()}

    def active_task_count(self) -> int:
        """Count all work that can write data, including terminal page operations."""
        with self._lock:
            return sum(1 for task in self._tasks.values() if not task.terminal or task.rerun_active)

    def clear_history(self) -> int:
        """Clear persisted task summaries only after the caller enforces its guard."""
        try:
            with self._pending_history_lock:
                removed = self._history.clear()
                self._pending_history.clear()
                self._history_warning_tasks.clear()
            with self._lock:
                tasks = list(self._tasks.values())
            for task in tasks:
                with task.condition:
                    if task.terminal:
                        task.history_dirty = False
            return removed
        except OSError as exc:
            raise TaskHistoryError("任务历史无法写入，暂时不能清空记录。") from exc

    def delete_task_record(self, task_id: str) -> dict[str, Any]:
        """删除单条任务记录本身。

        只动任务中心的记录：已经生成的译文、报告和诊断包都不属于这里，
        任何情况下都不删。运行中的任务不能删——它的记录还在被事件流改写，
        删掉只会在下一个事件里重新长出来。
        """
        key = str(task_id or "").strip()
        with self._lock:
            live = self._tasks.get(key)
            if live is not None and not live.terminal:
                raise TaskConflictError(
                    "任务仍在运行，请先安全停止，再删除这条记录。",
                    reason="task_active",
                )
            if live is not None and live.rerun_active:
                # Terminal, but still writing: the rerun (or restore) rebuilds
                # the output file and the report under this record.
                raise TaskConflictError(
                    "这个任务正在把一页换回上一版，稍等几秒再删除这条记录。"
                    if live.rerun_kind == "restore"
                    else "这个任务正在重新生成某一页，等它跑完再删除这条记录。",
                    reason="page_rerun_active",
                )
        # 其它任务可能仍在运行并写历史，先把内存里挂着的脏记录落盘，
        # 免得下面的整表重写把它们回退成更旧的版本。
        self.flush_history()
        # 一次加锁内读改写，历史文件不会出现「只剩一半」的中间态：以前是先 clear 再
        # 逐条 upsert 回填，整表最多 200 条就是 201 次落盘（实测约 0.4 秒），这段时间里
        # 别的任务写进来的记录要么被挤到表尾，要么直接丢失。
        try:
            with self._pending_history_lock:
                removed_from_history = self._history.remove(key)
                removed_from_pending = self._pending_history.pop(key, None) is not None
                self._history_warning_tasks.discard(key)
        except OSError as exc:
            raise TaskHistoryError("任务历史无法写入，暂时不能删除记录。") from exc
        with self._lock:
            removed_from_memory = self._tasks.pop(key, None) is not None or removed_from_pending
        if not removed_from_history and not removed_from_memory:
            raise TaskNotFoundError(key)
        return {
            "task_id": key,
            "removed_count": 1,
            "outputs_affected": False,
            "restart_required": False,
        }

    def task_results(self, task_id: str) -> dict[str, Any]:
        try:
            task = self._get_task(task_id)
        except TaskNotFoundError:
            record = self._history_record(task_id)
            if record is None:
                raise
            return record
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
                result = self._complete_file_progress(task, {
                    "message": "应用或 sidecar 已中断；请依据已有产物或报告新建任务。",
                    "recovery": {"can_resume": False, "reason": "sidecar_restarted"},
                }, "interrupted")
                task.state = "interrupted"
                task.terminal = True
                task.updated_at = time.time()
                task.result = result
                # Terminal flag and terminal event must become visible together.
                self._append_event(task, "interrupted", task.result)
            task.lease.release()
            self._retire_terminal_task(task)
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
        if task.surface not in {"pdf", "cad"}:
            raise TaskInputError("当前任务不支持暂停提交。")
        with task.condition:
            if task.terminal:
                return self.task_status(task_id)
            if not hasattr(task.runner, "pause"):
                raise TaskInputError("当前任务不支持暂停提交。")
            if task.state == "paused":
                return self.task_status(task_id)
            if task.state != "running":
                raise TaskInputError("当前任务不处于可暂停状态。")
            # CAD pauses at the current-file boundary. Keep the intermediate
            # state visible until the runner acknowledges that boundary.
            task.state = "pausing" if task.surface == "cad" else "paused"
            self._append_event(task, "pausing" if task.surface == "cad" else "paused", {"state": task.state})
        task.runner.pause()  # type: ignore[attr-defined]
        return self.task_status(task_id)

    def resume_task(self, task_id: str) -> dict[str, Any]:
        """Resume a same-sidecar PDF task with its frozen settings."""
        task = self._get_task(task_id)
        if task.surface not in {"pdf", "cad"}:
            raise TaskInputError("当前任务不支持继续。")
        with task.condition:
            if task.terminal:
                raise TaskInputError("任务已经结束，不能继续。")
            if not hasattr(task.runner, "resume"):
                raise TaskInputError("当前任务不支持继续。")
            if task.state != "paused":
                raise TaskInputError("只有暂停提交的任务可以继续。")
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

    def pdf_page_review(self, task_id: str) -> dict[str, Any]:
        """Return the pull-only per-page snapshot backing the review panel."""
        task = self._get_task(task_id)
        runner = self._pdf_review_runner(task)
        snapshot = runner.pdf_page_snapshot()
        with task.condition:
            state = task.state
            terminal = task.terminal
            rerun_active = task.rerun_active
        rerun = dict(snapshot.get("rerun") or {})
        # The panel polls this endpoint while a rerun runs, so the two flags
        # have to agree even in the window between the runner's thread finishing
        # and the pump clearing the task flag.
        rerun["active"] = bool(rerun.get("active")) or rerun_active
        return {
            "task_id": task.task_id,
            "state": state,
            "terminal": terminal,
            "actionable": state == "paused" and not terminal,
            "review_enabled": bool(snapshot.get("review_enabled")),
            # 终态任务的单页重生成走另一条路（不排队、立刻跑），可用性也另算：
            # actionable 说的是「暂停中可以排队页操作」，这一条说的是「已结束，
            # 但还能把某一页重跑一遍」。
            "rerun_actionable": bool(
                terminal and snapshot.get("can_rerun") and not rerun["active"]
            ),
            "rerun": _json_safe(rerun),
            "files": _json_safe(snapshot.get("files") or []),
        }

    def pdf_page_image_path(
        self,
        task_id: str,
        *,
        relative_path: str,
        page_number: int,
        kind: str,
    ) -> Path | None:
        """Resolve one page image; comparison stays available after the run."""
        task = self._get_task(task_id)
        runner = self._pdf_review_runner(task)
        try:
            return runner.resolve_page_image_path(
                relative_path=relative_path,
                page_number=page_number,
                kind=kind,
            )
        except PdfPageActionError as exc:
            raise TaskInputError(str(exc)) from exc

    def request_pdf_page_action(
        self,
        task_id: str,
        *,
        action: str,
        relative_path: str,
        page_number: int,
    ) -> dict[str, Any]:
        """Queue a single-page action; only a paused task may accept one."""
        normalized = str(action or "").strip().lower()
        if normalized not in {"regenerate", "skip"}:
            raise TaskInputError("单页操作只支持 regenerate 或 skip。")
        task = self._get_task(task_id)
        runner = self._pdf_review_runner(task)
        method = (
            runner.request_page_regenerate
            if normalized == "regenerate"
            else runner.request_page_skip
        )
        # The state check and the queueing happen under the same task condition
        # that ``resume_task`` uses to flip the state.  Either this request is
        # queued while the runner still waits inside its pause loop, or the task
        # is already running again and the request is rejected outright.
        with task.condition:
            if task.terminal:
                raise TaskConflictError("任务已经结束，不能再做单页操作。", reason="task_terminal")
            if task.state != "paused":
                raise TaskConflictError(
                    "只有暂停中的 PDF/图片任务可以做单页操作。",
                    reason="task_not_paused",
                )
            try:
                accepted = method(relative_path=relative_path, page_number=page_number)
            except PdfPageActionError as exc:
                raise TaskInputError(str(exc)) from exc
            self._append_event(
                task,
                "pdf_page_action",
                {
                    "action": normalized,
                    "page_number": int(accepted.get("page_number") or page_number),
                    "name": str(accepted.get("name") or ""),
                },
            )
        return {"task_id": task.task_id, "state": "paused", "accepted": accepted}

    def rerun_pdf_page(
        self,
        task_id: str,
        *,
        relative_path: str,
        page_number: int,
    ) -> dict[str, Any]:
        """Regenerate one page of a task that already ended, output file included.

        The task stays terminal throughout.  A rerun is not the task running
        again — it is a bounded piece of extra work on an archived task, and
        every "is anything active?" query in this file keys off ``terminal``.
        """
        task = self._get_task(task_id)
        runner = self._pdf_review_runner(task)
        if not callable(getattr(runner, "rerun_page", None)):
            raise TaskInputError("这个任务的逐页记录已经释放，不能再重新生成单页。")
        with task.condition:
            if self._shutdown.is_set():
                raise TaskConflictError("应用正在关闭，请重新打开后再操作。", reason="shutting_down")
            if not task.terminal:
                raise TaskConflictError(
                    "任务还没结束；运行中的任务请先暂停再做单页操作。",
                    reason="task_not_terminal",
                )
            if task.rerun_active:
                raise TaskConflictError(
                    "这个任务正在把一页换回上一版，马上就好，稍等一下再重新生成。"
                    if task.rerun_kind == "restore"
                    else "这个任务已经有一页在重新生成，等它跑完再操作下一页。",
                    reason="page_rerun_active",
                )
            task.rerun_active = True
            task.rerun_kind = "rerun"
        lease: ScheduledTaskLease | None = None
        try:
            # A rerun spends real API budget, so it reserves the shared groups
            # exactly like a task does.  That also gives the right answer for
            # free when a real PDF task is running: the same-type slot is taken.
            attempted = self._registry.reserve_task(
                owner_key=f"{task.task_id}:page-rerun",
                owner_label=_LABEL_BY_SURFACE["pdf"],
                task_type="pdf",
                group_capacities=task.group_capacities,
            )
            if attempted.lease is None:
                reason = attempted.reason or "conflict"
                messages = {
                    "surface_busy": "有 PDF/图片任务正在进行，等它结束后再重新生成这一页。",
                }
                raise TaskConflictError(
                    messages.get(reason, "任务资源预约失败。"),
                    reason=reason,
                )
            lease = attempted.lease
            api_schedulers = {
                role: lease.scheduler_for(group)
                for role, group in task.role_groups.items()
            }
            try:
                accepted = runner.rerun_page(
                    relative_path=relative_path,
                    page_number=page_number,
                    api_scheduler=api_schedulers.get("image"),
                    review_api_scheduler=api_schedulers.get("pdf_review"),
                )
            except PdfPageActionError as exc:
                raise TaskInputError(str(exc)) from exc
        except Exception:
            if lease is not None:
                lease.release()
            with task.condition:
                task.rerun_active = False
                task.rerun_kind = ""
            raise
        self._append_event(
            task,
            "pdf_page_rerun",
            {
                "phase": "started",
                "page_number": int(accepted.get("page_number") or page_number),
                "name": str(accepted.get("name") or ""),
            },
        )
        threading.Thread(
            target=self._pump_page_rerun,
            args=(task, runner, lease),
            daemon=True,
            name=f"api-rerun-{task.task_id[:8]}",
        ).start()
        return {"task_id": task.task_id, "state": task.state, "accepted": accepted}

    def restore_pdf_page_previous(
        self,
        task_id: str,
        *,
        relative_path: str,
        page_number: int,
    ) -> dict[str, Any]:
        """把一页换回上一版译文，并按新的页图重新装配输出文件。

        和 ``rerun_pdf_page`` 共用同一把忙锁（``rerun_active``）：同一份输出文件
        不能同时被两件事重装。区别是这件事不调用模型、不花钱，所以既不预约资源
        组（``reserve_task``），也不需要后台线程＋轮询——同步做完就回，界面拿到
        响应时输出文件已经换好了。
        """
        task = self._get_task(task_id)
        runner = self._pdf_review_runner(task)
        if not callable(getattr(runner, "restore_previous_page", None)):
            raise TaskInputError("这个任务的逐页记录已经释放，不能再换回上一版。")
        with task.condition:
            if self._shutdown.is_set():
                raise TaskConflictError("应用正在关闭，请重新打开后再操作。", reason="shutting_down")
            if not task.terminal:
                raise TaskConflictError(
                    "任务还没结束；运行中的任务请先暂停再做单页操作。",
                    reason="task_not_terminal",
                )
            if task.rerun_active:
                raise TaskConflictError(
                    "这个任务正在把另一页换回上一版，马上就好，稍等一下再操作。"
                    if task.rerun_kind == "restore"
                    else "这个任务有一页正在重新生成，等它跑完再换回上一版。",
                    reason="page_rerun_active",
                )
            task.rerun_active = True
            task.rerun_kind = "restore"
        try:
            accepted = runner.restore_previous_page(
                relative_path=relative_path,
                page_number=page_number,
            )
        except PdfPageActionError as exc:
            self._settle_page_restore(task, runner, apply_patch=False)
            raise TaskInputError(str(exc)) from exc
        except Exception:
            self._settle_page_restore(task, runner, apply_patch=False)
            raise
        self._settle_page_restore(task, runner, apply_patch=True, closing_data={
            "phase": "finished",
            "page_number": int(accepted.get("page_number") or page_number),
            "name": str(accepted.get("name") or ""),
        })
        self._retire_terminal_task(task)
        return {"task_id": task.task_id, "state": task.state, "accepted": accepted}

    def _settle_page_restore(
        self, task: ApiTask, runner: Any, *, apply_patch: bool,
        closing_data: dict[str, Any] | None = None,
    ) -> None:
        """换回结束后放锁：把 runner 攒下的日志收走，成功时刷新任务结果。

        这里是换回路径上 ``rerun_active`` 唯一的释放点：排空消息要写任务历史，
        哪一步抛出去而锁没放掉，这个任务之后所有单页重生成/换回都会被 409 挡死，
        只能重启 sidecar。所以排空和取结果各自吞异常（最多丢几行日志或一次结果
        刷新），锁无条件释放——与 ``_pump_page_rerun`` 的 finally 收尾纪律对齐。
        """
        try:
            while True:
                message = runner.get_message(timeout=0.0)
                if message is None:
                    break
                self._handle_message(task, message)
        except Exception:  # noqa: BLE001 - 排空失败只损失日志，锁必须照常放。
            pass
        patch: dict[str, Any] = {}
        if apply_patch:
            try:
                patch = dict(runner.result_patch() or {})
            except Exception:  # noqa: BLE001 - 结果刷新失败不该让换回本身算失败。
                patch = {}
        try:
            with task.condition:
                if patch and isinstance(task.result, dict):
                    task.result.update(_sanitize_task_data(patch))
            if closing_data is not None:
                self._append_event(task, "pdf_page_restore", closing_data)
            self._persist_task(task)
        finally:
            with task.condition:
                task.rerun_active = False
                task.rerun_kind = ""
                task.condition.notify_all()

    def _pump_page_rerun(
        self,
        task: ApiTask,
        runner: Any,
        lease: ScheduledTaskLease,
    ) -> None:
        """Drain a rerun's messages without ever touching the terminal record.

        ``_pump_runner`` cannot be reused: it exits the moment it sees
        ``terminal``, and its finish path would try to give an already-finished
        task a second terminal state.
        """
        error = ""
        try:
            while True:
                message = runner.get_message(timeout=0.1)
                if message is not None:
                    self._handle_message(task, message)
                    continue
                if not runner.page_rerun_state().get("active"):
                    # Drain what settled between the last read and this check.
                    while True:
                        message = runner.get_message(timeout=0.05)
                        if message is None:
                            break
                        self._handle_message(task, message)
                    break
            error = str(runner.page_rerun_state().get("error") or "")
        except Exception as exc:  # noqa: BLE001 - a broken pump must not wedge the flag.
            error = str(exc) or exc.__class__.__name__
        finally:
            patch = {}
            if not error:
                try:
                    patch = dict(runner.result_patch() or {})
                except Exception:  # noqa: BLE001 - a stale result is not worth failing on.
                    patch = {}
            try:
                with task.condition:
                    if patch and isinstance(task.result, dict):
                        task.result.update(_sanitize_task_data(patch))
                self._append_event(
                    task,
                    "pdf_page_rerun",
                    {"phase": "failed" if error else "finished", "message": error},
                )
                self._persist_task(task)
            finally:
                with task.condition:
                    task.rerun_active = False
                    task.rerun_kind = ""
                    task.condition.notify_all()
                lease.release()
            self._retire_terminal_task(task)

    @staticmethod
    def _pdf_review_runner(task: ApiTask) -> Any:
        if task.surface != "pdf":
            raise TaskInputError("只有 PDF/图片任务提供逐页审核数据。")
        runner = task.runner
        required = (
            "pdf_page_snapshot",
            "resolve_page_image_path",
            "request_page_regenerate",
            "request_page_skip",
        )
        if not all(callable(getattr(runner, name, None)) for name in required):
            raise TaskInputError("当前 PDF 任务不支持逐页审核。")
        return runner

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
        # Look up the task eagerly: inside the generator the failure would only
        # surface after the 200 response headers are already on the wire, where
        # the TaskNotFoundError -> 404 handler can no longer run.
        task = self._get_task(task_id)
        return self._iter_task_sse(task, after_event_id=after_event_id)

    def _iter_task_sse(
        self,
        task: ApiTask,
        *,
        after_event_id: int,
    ) -> Generator[str, None, None]:
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
                if terminal or self._shutdown.is_set():
                    return
                yield ": keepalive\n\n"
                continue
            for event in pending:
                last_id = event["id"]
                payload = json.dumps(event["data"], ensure_ascii=False, separators=(",", ":"))
                yield f"id: {event['id']}\nevent: {event['type']}\ndata: {payload}\n\n"
            if terminal:
                return

    def begin_shutdown(self) -> None:
        """Ask every live runner to stop, without waiting for any of them.

        Safe to call from a signal handler: it only flips flags, wakes the SSE
        loops so a graceful HTTP shutdown is not blocked by an open stream, and
        signals the runners.  The waiting happens in ``shutdown``.
        """
        self._shutdown.set()
        with self._lock:
            tasks = list(self._tasks.values())
        for task in tasks:
            with task.condition:
                terminal = task.terminal
                if not terminal and task.state != "stopping":
                    task.state = "stopping"
                task.condition.notify_all()
            if terminal and not task.rerun_active:
                continue
            try:
                task.runner.stop()
            except Exception:
                # A runner that cannot be asked to stop is handled by the
                # interrupted bookkeeping in ``shutdown``.
                pass

    def shutdown(self, *, timeout: float = 12.0) -> None:
        """Let live tasks unwind before the process goes away.

        Runners delete their own scratch state on the way out — the
        LibreOffice profile, the ``word_translator_temp`` docx directory, the
        PDF page workspaces — but only if they are allowed to unwind instead
        of being killed mid-run.  Whatever is still alive at the deadline is
        recorded as interrupted, so the task center never keeps showing a task
        that is running in no process.
        """
        self.begin_shutdown()
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._lock:
            tasks = list(self._tasks.values())
        for task in tasks:
            with task.condition:
                while not task.terminal or task.rerun_active:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        if task.rerun_active:
                            self._append_event(task, "pdf_page_shutdown_pending", {
                                "message": "关闭等待已超时，单页操作尚未确认安全结束。",
                            })
                        break
                    task.condition.wait(timeout=min(remaining, 0.25))
        self.mark_active_tasks_interrupted()
        self.flush_history()

    def _scan(
        self,
        root: Path,
        surface: TaskSurface,
        options: TaskOptions,
        *,
        selected_paths: set[str] | None = None,
    ) -> list[Any]:
        if surface == "excel":
            return scan_path(root)
        if surface == "word":
            return scan_word_path(root)
        if surface == "cad":
            status = probe_status()
            converter_path = str(status.get("converter") or "").strip()
            converter = SubprocessCadConverter(converter_path) if converter_path else None
            scan_options = CadPipelineOptions(include_block_text=options.cad_include_block_text)
            output_root = Path(options.cad_output_dir).expanduser().resolve() if options.cad_output_dir else None

            def under(path: Path, parent: Path | None) -> bool:
                if parent is None:
                    return False
                try:
                    path.resolve().relative_to(parent)
                    return True
                except ValueError:
                    return False

            if selected_paths:
                scan_inputs = [Path(path) for path in sorted(selected_paths)]
            elif root.is_dir():
                scan_inputs = [
                    candidate
                    for candidate in sorted(root.rglob("*"))
                    if candidate.is_file()
                    and candidate.suffix.lower() in {".dwg", ".dxf"}
                    and not under(candidate, output_root)
                    and not any(
                        part in {"CAD翻译输出", ".cad-work", ".cad-review"}
                        or (part.endswith("_翻译输出") or "_翻译输出_" in part)
                        for part in candidate.relative_to(root).parts
                    )
                ]
            else:
                scan_inputs = [root]

            summaries = []
            for candidate in scan_inputs:
                if under(candidate, output_root):
                    continue
                try:
                    summaries.extend(scan_cad_paths([candidate], converter=converter, options=scan_options))
                except Exception as exc:  # one malformed drawing must not abort the batch preflight
                    logger.warning("CAD 预检跳过文件 {}：{}", candidate, exc)
            return [
                type("CadFile", (), {"path": summary.path, "name": summary.filename, "format": summary.format})()
                for summary in summaries
            ]
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
        connection_chains: dict[str, tuple[str, ...]] | None = None,
    ) -> Runner:
        api_schedulers = dict(api_schedulers or {})
        chains = dict(connection_chains or {})
        translation_chain = tuple(chains.get("translation") or ())
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
                connection_chain=translation_chain,
                resume_output_dir=options.resume_output_dir,
            )
        if surface == "word":
            return WordTaskRunner(
                files,
                settings,
                source_root=source_root,
                source_lang=source_lang,
                key_overrides=key_overrides,
                untranslated_only=options.untranslated_only,
                protect_front_matter=options.protect_front_matter,
                translate_headers_footers=options.translate_headers_footers,
                allow_doc_fallback=options.allow_doc_fallback,
                api_scheduler=api_schedulers.get("translation"),
                resume_output_dir=options.resume_output_dir,
            )
        if surface == "cad":
            status = probe_status()
            converter_path = str(status.get("converter") or "").strip()
            has_dwg = any(Path(getattr(item, "path", "")).suffix.lower() == ".dwg" for item in files)
            if has_dwg and not converter_path:
                raise TaskInputError("CAD 翻译需要先连接 ODA 转换器。", reason="cad_converter_missing")
            # All document surfaces publish into one unique, timestamped
            # task root.  CAD used to keep a permanent ``CAD翻译输出`` folder
            # with nested translated/review/run directories, which made a
            # second run overwrite or mix with the first one.  Reuse the
            # shared builder used by Excel, Word and PDF instead.
            cad_output = getattr(settings, "cad_output", None)
            configured_output_dir = options.cad_output_dir
            if not configured_output_dir and cad_output is not None and getattr(cad_output, "use_custom_output_dir", False):
                configured_output_dir = str(getattr(cad_output, "custom_output_dir", "") or "").strip() or None
            output_dir = bilingual_writer.build_output_dir(source_root, configured_output_dir)
            engine = build_role_engine(
                settings,
                "translation",
                connection_ids=tuple(chains.get("translation") or ()),
            )
            from core.engine_dispatcher import get_system_prompt, translate_texts
            system_prompt = get_system_prompt(settings, target_lang=settings.target_lang, source_lang=source_lang, page_key="cad")

            def translate_batch(texts: list[str], glossary: dict[str, str]) -> dict[str, str]:
                prompt = system_prompt
                if glossary:
                    prompt += "\n固定术语（必须遵守）：\n" + "\n".join(f"{k} = {v}" for k, v in glossary.items())
                return translate_texts(
                    texts,
                    engine,
                    settings.target_lang,
                    prompt,
                    max(1, int(settings.engine.batch_size)),
                    max(1, int(settings.engine.concurrency)),
                    # CAD preflight may keep mixed English/French text under
                    # automatic detection.  Passing ``zh`` here silently
                    # changes the model instruction and can produce a wrong
                    # translation; the engine accepts ``auto`` and should
                    # receive the user's actual selection.
                    source_lang=source_lang,
                    api_scheduler=api_schedulers.get("translation"),
                )

            source_pair = str(source_lang or "").strip()
            memory_pairs = (
                [f"{source_pair}-{settings.target_lang}"]
                if source_pair and source_pair != "auto"
                else [f"{candidate}-{settings.target_lang}" for candidate in ("en", "fr", "zh")]
            )

            def memory_lookup(texts: list[str]) -> dict[str, str | None]:
                hits: dict[str, str | None] = {text: None for text in texts}
                remaining = list(texts)
                for pair in memory_pairs:
                    if not remaining:
                        break
                    current = tm_manager.lookup_batch(remaining, pair)
                    for text, value in current.items():
                        if value and hits.get(text) in (None, ""):
                            hits[text] = value
                    remaining = [text for text in remaining if not hits.get(text)]
                return hits

            return CadTaskRunner(
                files,
                source_root=source_root,
                output_dir=output_dir,
                converter=SubprocessCadConverter(converter_path) if converter_path else None,
                memory_lookup=memory_lookup if options.cad_use_memory else None,
                translator=translate_batch,
                filename_translator=engine,
                glossary=_load_cad_glossary(options.cad_glossary_path) if options.cad_use_terminology else {},
                options=CadPipelineOptions(
                    source_lang=source_lang,
                    target_lang=settings.target_lang,
                    skip_target_language=options.untranslated_only,
                    keep_work_dxf=options.cad_keep_work_dxf,
                    include_block_text=options.cad_include_block_text,
                    verify_roundtrip=options.cad_verify_roundtrip,
                    scan_replacement_chars=options.cad_scan_replacement_chars,
                    copy_related_files=options.cad_copy_related_files,
                    check_entity_counts=options.cad_check_entity_counts,
                    scan_residual=options.cad_scan_residual,
                ),
                resume_output_dir=options.resume_output_dir,
                translate_output_filename=options.cad_translate_output_filename,
                translation_identity={
                    "model": getattr(settings.engine, "cloud_model", ""),
                    "provider": getattr(settings.engine, "cloud_provider", ""),
                    "target_lang": settings.target_lang,
                    "source_lang": source_lang,
                },
            )
        return PdfImageTranslationRunner(
            files,
            settings,
            source_root=source_root,
            key_overrides=key_overrides,
            api_scheduler=api_schedulers.get("image"),
            review_api_scheduler=api_schedulers.get("pdf_review"),
            resume_output_dir=options.resume_output_dir,
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
        consequence = describe_xls_compatibility_consequence(
            has_libreoffice=libreoffice_xls_conversion_available()
        )
        raise TaskInputError(
            "检测到 "
            f"{len(xls_files)} 个 .xls 文件，但本机 Microsoft Excel 高保真自动化不可用：{reason}。"
            f"请取消任务，安装/授权 Microsoft Excel 后重试，或明确确认兼容转换{consequence}"
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
            # 这一条是有出路的拦截（allow_known_review_failure 就是那条出路），界面必须
            # 拿它开一个带「仍要继续」按钮的弹窗，而不是弹一句两秒就没的提示。靠比对中文
            # 句子来认这种情况太脆，给它一个稳定的 reason。
            raise TaskInputError(
                "PDF 翻译审核模型当前配置已测试失败；请重新测试、关闭审核，或明确确认继续。",
                reason="pdf_review_model_unavailable",
            )

    @staticmethod
    def _validate_cad_preflight(*, files: list[Any], settings: AppSettings) -> None:
        if not files:
            raise TaskInputError("请至少选择一个 DWG 或 DXF 文件。")
        status = probe_status()
        has_dwg = any(Path(getattr(item, "path", "")).suffix.lower() == ".dwg" for item in files)
        if has_dwg and not bool(status.get("enabled")):
            raise TaskInputError(
                "CAD 插件或 ODA 转换器未就绪，请先安装 CAD Support 并连接 ODA。",
                reason="cad_capability_missing",
            )
        if not str(getattr(settings, "target_lang", "") or "").strip():
            raise TaskInputError("请先选择 CAD 目标语言。")
        model_check = check_translation_api_config(settings)
        if not model_check.ok:
            detail = f"（{model_check.detail}）" if model_check.detail else ""
            raise TaskInputError(f"{model_check.message}{detail}")

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
                result={
                    "message": user_facing_reason(
                        "Translation runner ended without a terminal message.",
                        fallback="翻译流程异常终止，未收到结束信号，请重试。",
                    )
                },
            )
        except Exception as exc:  # noqa: BLE001 - task errors must be delivered to SSE.
            # 裸异常名（如 "RuntimeError"）和未识别的英文内部串一样，不能直接端给
            # 用户；user_facing_reason 只放行真正的中文说明，其余一律换成兜底句。
            self._finish_if_needed(
                task,
                state="error",
                event_type="error",
                result={
                    "message": user_facing_reason(
                        exc, fallback="任务执行时出现未知错误，请重试或查看日志。"
                    )
                },
            )

    def _handle_message(self, task: ApiTask, message: Any) -> None:
        event_type = _event_type_for_message(message)
        payload = _json_safe(asdict(message) if is_dataclass(message) else message)
        if isinstance(message, ProgressMsg):
            stage_weights = (15, 10, 45, 20, 10)
            phase_index = max(1, min(len(stage_weights), int(message.phase_index)))
            stage_percent = min(100.0, max(0.0, 100.0 * message.step_done / max(1, message.step_total)))
            completed_weight = sum(stage_weights[: phase_index - 1])
            overall_percent = completed_weight + stage_weights[phase_index - 1] * stage_percent / 100.0
            with task.condition:
                task.progress = {
                    "phase_index": phase_index,
                    "phase_total": int(message.phase_total),
                    "phase_name": str(message.phase_name),
                    "stage_percent": round(stage_percent, 1),
                    "overall_percent": round(overall_percent, 1),
                    "step_done": int(message.step_done),
                    "step_total": int(message.step_total),
                }
            if task.surface == "cad":
                with task.condition:
                    task.task_snapshot.update(task.progress)
        if isinstance(message, StatusMsg) and task.surface == "cad":
            if str(message.phase_desc).startswith("任务已暂停"):
                with task.condition:
                    if not task.terminal and task.state == "pausing":
                        task.state = "paused"
                        self._append_event(task, "paused", {"state": "paused"})
        if isinstance(message, FileProgressMsg):
            with task.condition:
                if task.terminal:
                    return
                payload["revision"] = task.next_event_id
                task.file_progress[message.file_id] = _sanitize_task_data(payload)
                self._append_event(task, "file_progress", payload)
            return
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
            result = self._complete_file_progress(task, result, state)
            task.state = state
            task.terminal = True
            task.updated_at = time.time()
            task.result = _sanitize_task_data(result)
            # Append while still holding the condition (it is reentrant): a
            # reader that observes terminal=True must also see the terminal
            # event, or an SSE stream ends cleanly without ever carrying it.
            self._append_event(task, event_type, task.result)
        task.lease.release()
        self._retire_terminal_task(task)

    def _complete_file_progress(self, task: ApiTask, result: dict[str, Any], state: str) -> dict[str, Any]:
        """Reconcile each file against its output, never blanket-mark a task done."""
        result = dict(result)
        matched: dict[str, dict[str, Any]] = {}
        for key in ("file_results", "files", "file_records"):
            entries = result.get(key)
            if not isinstance(entries, list):
                continue
            tagged = []
            for raw in entries:
                if not isinstance(raw, dict):
                    tagged.append(raw)
                    continue
                entry = dict(raw)
                file_id = str(entry.get("file_id") or "")
                source_path = entry.get("source_path")
                if not file_id and source_path:
                    candidate = file_progress_id(source_path)
                    if candidate in task.file_progress:
                        file_id = candidate
                if not file_id:
                    relative = str(entry.get("source_relative_path") or entry.get("relative_path") or "")
                    candidates = [
                        fid for fid, descriptor in task.file_progress.items()
                        if relative and descriptor["relative_path"] == relative
                    ]
                    if len(candidates) != 1:
                        name = str(entry.get("name") or "")
                        candidates = [
                            fid for fid, descriptor in task.file_progress.items()
                            if name and name in {descriptor["name"], Path(descriptor["name"]).stem}
                        ]
                    if len(candidates) == 1:
                        file_id = candidates[0]
                if file_id:
                    entry["file_id"] = file_id
                    matched[file_id] = {**matched.get(file_id, {}), **entry}
                tagged.append(entry)
            result[key] = tagged
        for file_id, progress in task.file_progress.items():
            entry = matched.get(file_id, {})
            output = any(entry.get(key) for key in ("output", "output_path", "translated_image_path"))
            status = str(entry.get("status") or "")
            previous = progress["state"]
            if output:
                file_state = "generated"
            elif status == "unstarted":
                file_state = "unstarted"
            elif status in {"failed", "error"}:
                file_state = "failed"
            elif status == "stopped":
                file_state = "stopped"
            elif previous in {"generated", "failed", "unstarted", "stopped", "interrupted"}:
                file_state = previous
            elif state == "interrupted":
                file_state = "interrupted"
            elif state == "stopped":
                file_state = "unstarted" if progress["phase"] == "prepare" and previous == "waiting" else "stopped"
            else:
                file_state = "failed"
            progress.update(
                state=file_state,
                result=_sanitize_task_data(entry or progress.get("result") or {}),
                revision=task.next_event_id,
            )
            self._append_event(task, "file_progress", progress)
        return result

    def _retire_terminal_task(self, task: ApiTask) -> None:
        """Release a finished task's heavy references and bound how many stay.

        ``_tasks`` never dropped a finished entry, and every entry pinned its
        runner — the scanned file list, a deep copy of settings and, for PDF,
        one record per page.  A day of ordinary use grew the sidecar
        monotonically.  What the task center still needs (summary, result,
        logs, the tail of the event stream) is kept here and on disk.
        """
        with task.condition:
            if task.surface != "pdf":
                # PDF is the exception: page comparison images stay browsable
                # after the run and are resolved through the runner.  Those
                # runners are bounded by the retention cap below instead.
                task.runner = RetiredRunner()
            if len(task.events) > TERMINAL_EVENT_TAIL:
                del task.events[: len(task.events) - TERMINAL_EVENT_TAIL]
        self._evict_retired_tasks()

    def _evict_retired_tasks(self) -> None:
        """Keep only the most recent finished tasks in memory."""
        with self._lock:
            terminal = [item for item in self._tasks.values()
                        if item.terminal and not item.rerun_active]
            excess = len(terminal) - MAX_RETAINED_TERMINAL_TASKS
            if excess <= 0:
                return
            terminal.sort(key=lambda item: item.updated_at)
            for stale in terminal[:excess]:
                self._tasks.pop(stale.task_id, None)

    def _append_event(self, task: ApiTask, event_type: str, data: Any) -> None:
        safe_data = _sanitize_task_data(data)
        if event_type == "log":
            level = "INFO"
            raw_message = ""
            if isinstance(data, dict):
                level = str(data.get("level") or level)
                raw_message = str(data.get("message") or "")
            # 逐条脱敏，而不是整条丢弃：凭据、绝对路径和引用了原文/译文/模型输出
            # 的行会被替换掉，阶段、文件名、计数和耗时保留下来，运行面板才有进度可看。
            safe_data = {
                "level": level,
                "stage": "runner",
                "message": sanitize_task_log_message(raw_message),
            }
        pending_record: dict[str, Any] | None = None
        with task.condition:
            task.events.append(
                {
                    "id": task.next_event_id,
                    "type": event_type,
                    "data": safe_data,
                }
            )
            if event_type == "log":
                # 任务中心的历史日志与 SSE 事件用同一条脱敏结果，两处不会不一致。
                task.logs.append(
                    {
                        "event_id": task.next_event_id,
                        "level": str(safe_data.get("level") or "INFO"),
                        "stage": "runner",
                        "message": str(safe_data.get("message") or ""),
                    }
                )
            now = time.time()
            task.updated_at = now
            task.next_event_id += 1
            if task.terminal and task.rerun_active:
                # A single-page rerun emits hundreds of log lines on a task that
                # is already terminal.  Without this the branch below would do a
                # full sanitize + read + rewrite of the history file for each
                # one; the page-operation settlement persists once before
                # releasing its busy flag.
                task.history_dirty = True
            elif task.terminal:
                # Persist before anyone can observe the event (the condition is
                # an RLock, so the nested acquire in _persist_task is fine).
                # Waking readers first let an SSE consumer see the terminal
                # event, tear down, and race the history write still in flight
                # on this thread.  This happens once per task, so the cost the
                # throttle below exists to avoid does not apply.
                self._persist_task(task)
            elif self._history_write_due(task, now):
                # Build the record under the condition so it is a consistent
                # snapshot, but keep the file read/rewrite outside: that is the
                # part that blocks this task's SSE stream and status endpoint.
                pending_record = self._status_payload(task, include_result=True)
                task.history_dirty = True
            else:
                task.history_dirty = True
            task.condition.notify_all()
        if pending_record is not None:
            self._write_task_history(task, pending_record)

    @staticmethod
    def _history_write_due(task: ApiTask, now: float) -> bool:
        """Decide whether this event is worth a full history rewrite.

        A state change is always worth it — the task center reads state from
        the persisted record.  Everything else (progress ticks, log lines) can
        wait for the interval, because it only makes the summary slightly
        stale, never wrong.
        """
        if task.state != task.last_persisted_state:
            return True
        return (now - task.last_persisted_at) >= HISTORY_WRITE_INTERVAL_SECONDS

    def flush_history(self) -> None:
        """Write out summaries whose last events were held back by the throttle."""
        with self._pending_history_lock:
            pending = list(self._pending_history.items())
        for key, record in pending:
            self._retry_pending_history(key, record)
        with self._lock:
            tasks = list(self._tasks.values())
        for task in tasks:
            with task.condition:
                if not task.history_dirty:
                    continue
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


def _load_cad_glossary(path: str | None) -> dict[str, str]:
    """Load an optional simple JSON/CSV terminology map."""
    if not str(path or "").strip():
        return {}
    candidate = Path(str(path)).expanduser()
    if not candidate.is_file():
        raise TaskInputError(f"CAD 术语库不存在：{candidate}")
    try:
        if candidate.suffix.lower() == ".json":
            value = json.loads(candidate.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("JSON 术语库必须是原文到译文的对象。")
            return {
                str(key): str(item)
                for key, item in value.items()
                if str(key).strip() and str(item).strip()
            }
        glossary: dict[str, str] = {}
        for row in candidate.read_text(encoding="utf-8-sig").splitlines():
            if not row.strip() or row.lstrip().startswith("#"):
                continue
            source, separator, target = row.partition("\t")
            if not separator:
                source, separator, target = row.partition(",")
            if separator and source.strip() and target.strip():
                glossary[source.strip()] = target.strip()
        return glossary
    except (OSError, UnicodeError, ValueError) as exc:
        raise TaskInputError(f"CAD 术语库无法读取：{candidate}") from exc


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


def _scanned_file_name(item: Any) -> str:
    """Return one scanned entry's file name without assuming a scanner type.

    Each surface returns its own item class (``FileItem`` / ``WordFileItem`` /
    the PDF scanner entry), and task-manager tests hand in bare objects.  Only
    the file name is taken: the task title must not leak a full local path.
    """
    raw = getattr(item, "path", None) or getattr(item, "name", "")
    value = str(raw or "").strip()
    return Path(value).name if value else ""


def _selected_files_label(files: list[Any]) -> str:
    """任务标题里的来源说明：单文件报文件名，多文件报「首个文件名 等 N 个文件」。"""
    names = [name for name in (_scanned_file_name(item) for item in files) if name]
    if not names:
        # 扫描条目没有可读路径（测试替身或今后新增的条目形状）时退回计数说明，
        # 标题宁可粗略，也不能报错或落回英文占位串。
        return f"{len(files)} 个文件"
    if len(files) == 1:
        return names[0]
    return f"{names[0]} 等 {len(files)} 个文件"


def _event_type_for_message(message: Any) -> str:
    mapping = (
        (FileProgressMsg, "file_progress"),
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
    "translated_dir",
    "publication_dir",
    "review_dir",
    "summary_path",
    "checkpoint_path",
    # Word and PDF name their per-file artifact "output" (Excel uses "output_path").
    # Without it the redactor rewrote every Word output path to the literal
    # "[path]", and the task center printed that placeholder as if it were a path.
    "output",
    "output_path",
    # PDF 每份文件会写出两个产物：高清版走 "output"，压缩版走这个键。漏掉它的话产物表里
    # 压缩版那一行会显示成 "[path]"（界面把这个占位符当空值，等于压缩版从来没生成过）。
    "compressed_output",
    "translated_path",
    "report_path",
    "manifest_path",
    "custom_output_dir",
}
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
            if (
                normalized_key != "model_snapshot"
                and lowered in {"source", "original", "translation", "translated", "content", "text"}
            ):
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
        text = redact_absolute_paths(text)
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
        ("open_summary", "summary_path"),
        ("open_review", "review_dir"),
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
        "open_summary",
        "open_review",
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
