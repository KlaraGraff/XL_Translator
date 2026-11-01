"""Task lifecycle and SSE event storage for the local API."""

from __future__ import annotations

import json
import hashlib
import os
from contextlib import contextmanager
import re
import threading
import time
import uuid
from collections.abc import Callable, Generator
from dataclasses import asdict, dataclass, field, is_dataclass, replace
from pathlib import Path
from typing import Any

from core import bilingual_writer, diagnostics, tm_manager
from core.file_scanner import GENERATED_OUTPUT_DIR_MARKER
from core.file_progress import FileProgressMsg, file_progress_entry, file_progress_id
from core.model_api_identity import task_api_context_for_page
from core.engine_dispatcher import activate_translation_surface, core_prompt_for
from core.language_registry import normalize_source_selection
from core.pdf_image_translation import (
    PdfPageActionError,
)
from core.pdf_page_snapshot import restored_page_image_path, restored_pdf_page_review
from core.task_logger import redact_absolute_paths, sanitize_task_log_message
from core.path_utils import normalize_user_path
from core.surface_settings import surface_setting
from core.task_resources import ScheduledTaskLease, TaskResourceRegistry
from core.task_history import MAX_TASK_LOG_LINES, TaskHistoryError, TaskHistoryStore
from loguru import logger
from core.tm_cleaning_task_runner import TmCleaningTaskRunner
from core.cad_task_runner import CAD_PAUSED_STATUS_PREFIX
from core.task_runner import (
    DoneMsg,
    ErrorMsg,
    LogMsg,
    PdfPageRecoveryStatusMsg,
    PdfReviewStatusMsg,
    ProgressMsg,
    StatusMsg,
    StoppedMsg,
    WordRecoveryStatusMsg,
    user_facing_reason,
)
from api.surfaces import RunnerRequest, get_surface
from api.task_contracts import (  # noqa: F401 - re-exported
    Runner,
    TaskConflictError,
    TaskInputError,
    TaskNotFoundError,
    TaskOptions,
)
from settings import AppSettings, load_settings

# A surface key ("excel", "word", "pdf", "cad", "tm_clean").  What each
# surface does differently lives in api/surfaces/, not in this module.
TaskSurface = str

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


# Runner, the task errors and TaskOptions live in api/task_contracts.py
# (surfaces need them too); they are re-exported from here.


# 诊断记录只收 ASCII 类别；阶段名是中文（读取文件 / 翻译 / …），先换成固定代号。
_DIAGNOSTIC_PHASES = {
    "读取文件": "read",
    "翻译": "translate",
    "调整行高": "adjust",
    "生成文件": "generate",
    "记忆库清洗": "tm_clean",
}


# 见 _iter_task_sse：把 WebKit 攒着不交出的小块事件推出去。2 KB 在本机回环上可以忽略。
SSE_FLUSH_PADDING = ":" + " " * 2048 + "\n\n"


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
    # 「翻译」页一键开始的一批任务共用的输出文件夹；单独开始的任务为空。
    batch_output_dir: str = ""
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
    # 用户通过接口按了停止（或暂停后点「结束」）。runner 也会因故自行收尾成
    # stopped（Word 持续限流），界面靠这个分清「用户停的」和「被迫停的」：
    # 后者要把后端说明的原因原样给用户看，不能换成「任务已停止」。
    stop_requested: bool = False


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
        # 维护页的清理正在进行时 >0：检查「没有活动任务」和清理本身之间，新任务和单页操作
        # 一律不让登记，否则清理会在一个刚开始的任务脚下删掉它要写的数据。
        self._maintenance_holds = 0
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
                "同类翻译已有任务正在进行（包括已暂停和正在停止的任务），请待其结束后再开始。",
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

    def saved_request_defaults(self, surface: str, *, target_lang: str | None = None) -> dict[str, Any]:
        """What the page's workspace would fill into a start request (``apply_saved_defaults``)."""
        spec = get_surface(surface)
        settings = self._settings_loader().model_copy(deep=True)
        activate_translation_surface(settings, spec.key)
        target = _apply_target_lang(settings, spec, target_lang)
        return spec.saved_request_defaults(settings, target_lang=target)

    def saved_scan_defaults(
        self,
        surface: str,
        *,
        source_lang: str | None = None,
        target_lang: str | None = None,
    ) -> dict[str, Any]:
        """What the page's workspace would fill into a scan request (``apply_saved_defaults``)."""
        spec = get_surface(surface)
        settings = self._settings_loader().model_copy(deep=True)
        activate_translation_surface(settings, spec.key)
        target = _apply_target_lang(settings, spec, target_lang)
        source = (
            source_lang
            if source_lang is not None
            else str(surface_setting(settings, spec.key, "source_lang", "auto") or "auto")
        )
        return spec.saved_scan_defaults(settings, source_lang=source, target_lang=target)

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
                    "本次确认已过期，请重新点击开始。",
                    reason="expired_or_consumed",
                )
            if token.prepared.fingerprint != prepared.fingerprint:
                raise TaskConflictError(
                    "确认后任务设置已更改，请重新点击开始。",
                    reason="stale",
                )
            expected_revision = token.registry_revision
        else:
            risk = self._registry.scheduling_risk(
                task_type=prepared.surface,
                group_capacities=prepared.group_capacities,
            )
            if bool(risk["surface_busy"]):
                raise TaskConflictError("同类翻译已有任务正在进行，请待其结束后再开始。", reason="surface_busy")
            if risk["shared_groups"]:
                raise TaskConflictError(
                    "此任务将与正在运行的任务共用同一条模型连接，请确认后再开始。",
                    reason="confirmation_required",
                )
            expected_revision = int(risk["revision"])
        return self._start_prepared(prepared, expected_revision=expected_revision)

    def start_batch(
        self,
        items: list[tuple[TaskSurface, str, list[str], TaskOptions]],
        *,
        allow_shared: bool = False,
    ) -> dict[str, Any]:
        """「翻译」页拖入混合文件夹后的「全部开始」：几种类型一起开始，写进同一个输出文件夹。

        每种类型的设置都是它自己页面上的（请求里各带各的），这里只做三件事：开始前看一眼
        会不会和这一批之外正在跑的任务共用连接（要确认）；建一个共用的输出文件夹；逐个
        准备并开始——逐个是为了「分散到不同连接」打开时，后开始的能看到前面已经占了哪条。
        同一批内部共用连接不再问：同一条连接上的总并发由调度器封顶（见 FairApiGroupScheduler）。
        某一类没能开始不影响其他类，结果里列出原因。
        """
        if not items:
            raise TaskInputError("没有要开始的翻译。")
        seen: set[str] = set()
        for surface, _, _, _ in items:
            key = get_surface(surface).key
            if key in seen:
                raise TaskInputError("同一批里同一种类型只能有一个任务。")
            seen.add(key)
        roots: list[Path] = []
        for surface, source_path, _, _ in items:
            normalized = str(normalize_user_path(str(source_path or "")) or "").strip()
            if not normalized:
                raise TaskInputError("请先选择要翻译的文件或文件夹。")
            source = Path(normalized).expanduser().resolve()
            roots.append(source if source.is_dir() else source.parent)
        share_batch_output = True
        external_owners: list[str] = []
        for surface, _, _, options in items:
            spec = get_surface(surface)
            settings = self._settings_loader().model_copy(deep=True)
            activate_translation_surface(settings, spec.key)
            _apply_target_lang(settings, spec, options.target_lang)
            if surface_setting(settings, spec.key, "output_mode", "task_folder") != "task_folder":
                share_batch_output = False
            context = task_api_context_for_page(
                settings, spec.page_key, busy_connection_ids=self._busy_connection_ids()
            )
            risk = self._registry.scheduling_risk(
                task_type=spec.key,
                group_capacities=dict(getattr(context, "group_concurrency", {}) or {}),
            )
            if bool(risk["surface_busy"]):
                raise TaskConflictError(
                    "同类翻译已有任务正在进行，请待其结束后再开始。", reason="surface_busy"
                )
            for shared in risk["shared_groups"]:
                external_owners.extend(str(key) for key in shared.get("active_owner_keys") or ())
        if external_owners and not allow_shared:
            with self._lock:
                labels = sorted(
                    {
                        get_surface(task.surface).owner_label(task.task_snapshot)
                        for key, task in self._tasks.items()
                        if key in set(external_owners)
                    }
                )
            return {"requires_confirmation": True, "shared_with": labels}

        output_dir: Path | None = None
        if share_batch_output:
            common = Path(os.path.commonpath([str(root) for root in roots]))
            output_dir = bilingual_writer.resolve_output_directory(
                common,
                output_mode=bilingual_writer.OUTPUT_MODE_TASK_FOLDER,
            ).output_dir
        started: list[dict[str, Any]] = []
        failed: list[dict[str, str]] = []
        for surface, source_path, selected_paths, options in items:
            try:
                prepared = self._prepare_task(
                    surface=surface,
                    source_path=source_path,
                    selected_paths=selected_paths,
                    options=replace(
                        options,
                        batch_output_dir=str(output_dir) if output_dir is not None else "",
                    ),
                )
                started.append(self._start_prepared(prepared, expected_revision=None))
            except (TaskInputError, TaskConflictError) as exc:
                failed.append(
                    {
                        "surface": get_surface(surface).key,
                        "message": str(exc),
                        "reason": str(getattr(exc, "reason", "") or ""),
                    }
                )
            except Exception as exc:  # noqa: BLE001 - 一类出意外不拖累其他类
                logger.exception(f"[批量] {surface} 未能开始")
                failed.append(
                    {
                        "surface": get_surface(surface).key,
                        "message": user_facing_reason(exc, fallback="任务因意外问题未能启动，请重试。"),
                        "reason": "",
                    }
                )
        if not started and output_dir is not None:
            try:
                output_dir.rmdir()
            except OSError:
                pass
        return {
            "requires_confirmation": False,
            "output_dir": str(output_dir) if started and output_dir is not None else "",
            "tasks": started,
            "failed": failed,
        }

    def _prepare_task(
        self,
        *,
        surface: TaskSurface,
        source_path: str,
        selected_paths: list[str] | None,
        options: TaskOptions | None,
    ) -> PreparedTask:
        spec = get_surface(surface)
        normalized_surface = spec.key
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
        if selected_options.batch_output_dir:
            # 只认批量接口建的那个文件夹：已存在、名字带「_翻译输出_」。不让请求把结果写到任意目录。
            batch_dir = Path(str(normalize_user_path(selected_options.batch_output_dir))).expanduser().resolve()
            if not batch_dir.is_dir() or GENERATED_OUTPUT_DIR_MARKER not in batch_dir.name:
                raise TaskInputError("这一批的输出文件夹不可用，请重新开始。")
            selected_options = replace(selected_options, batch_output_dir=str(batch_dir))
        settings = self._settings_loader().model_copy(deep=True)
        activate_translation_surface(settings, normalized_surface)

        if not spec.requires_source:
            lang_pair = str(selected_options.lang_pair or source_path or "").strip()
            if "-" not in lang_pair or not all(part.strip() for part in lang_pair.split("-", 1)):
                raise TaskInputError("请先选择要清洗的语言方向。")
            tm_manager.init_db()
            context = task_api_context_for_page(
                settings,
                spec.page_key,
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
            raise TaskInputError("请先选择要翻译的文件或文件夹。")
        root = Path(source_path).expanduser().resolve()
        if not root.exists():
            raise TaskInputError(f"找不到「{root.name or root}」，可能已被移动或删除。")
        # 没有自己源语言设置的页面（PDF：按每页识别原文语种）是自动识别。以前退回全局的
        # source_lang（默认中文），任务详情就写成「中文 → 中文」。
        default_surface_source = surface_setting(
            settings, normalized_surface, "source_lang", "auto"
        )
        source_selection = normalize_source_selection(
            selected_options.source_lang
            if selected_options.source_lang is not None
            else default_surface_source
        )
        if source_selection is None:
            raise TaskInputError("源语言必须是内置语言或自动识别；自定义语言只能作为目标语言。")
        _apply_target_lang(settings, spec, selected_options.target_lang)

        selected = {
            str(Path(path).expanduser().resolve())
            for path in (selected_paths or [])
            if str(path or "").strip()
        }
        # 跨文件夹多选/拖入：界面把第一个文件的上级目录当来源，其他文件夹里的文件不在
        # 它下面，只重扫这一个根再按 selected_paths 收窄，它们就被无声丢掉了。不在根下
        # 的那几个各扫一次补进来。来源根不换：换成共同上级会让窗口重载后的找回去扫整个
        # 主目录甚至整块盘、输出文件夹也跟着挪过去，跨盘符时还根本算不出共同上级。
        # 根外的文件译文按文件名放在输出文件夹顶层，所以同名的要先拦下来。
        outside = sorted(
            path for path in selected if not _path_is_within(Path(path), root)
        )
        scan_roots = [root, *(Path(path) for path in outside)]
        files = []
        seen_files: set[str] = set()
        for scan_root in scan_roots:
            if spec.scan_honours_selection:
                found = self._scan(
                    scan_root,
                    normalized_surface,
                    selected_options,
                    selected_paths=selected if selected else None,
                )
            else:
                # Keep the established call shape for other surfaces; tests and
                # integrations may provide a narrow scan adapter for them.
                found = self._scan(scan_root, normalized_surface, selected_options)
            if not outside:
                files.extend(found)
                continue
            for item in found:
                key = str(Path(item.path).expanduser().resolve())
                if key not in seen_files:
                    seen_files.add(key)
                    files.append(item)
        if selected:
            files = [
                item
                for item in files
                if str(Path(item.path).expanduser().resolve()) in selected
            ]
        if outside:
            _raise_on_output_name_clash(files, root)
        if not files:
            logger.info(f"[任务] 没有找到可翻译的 {normalized_surface} 文件：{root}")
            raise TaskInputError("未找到可翻译的文件，请选择其他文件或文件夹，或检查文件格式。")
        self._validate_preflight(
            normalized_surface, files=files, settings=settings, options=selected_options
        )
        if spec.uses_translation_memory:
            tm_manager.init_db()

        context = task_api_context_for_page(
            settings,
            spec.page_key,
            busy_connection_ids=self._busy_connection_ids(),
        )
        prompt_source = "|".join(
            (
                core_prompt_for(settings, normalized_surface, spec.default_prompt),
                str(surface_setting(settings, normalized_surface, "smart_context", "")),
                str(surface_setting(settings, normalized_surface, "domain_preset", "") or ""),
                str(
                    surface_setting(settings, normalized_surface, "domain_custom_prompts", {})
                    or {}
                ),
            )
        )
        task_snapshot = {
            "surface": normalized_surface,
            "source_lang": source_selection,
            "target_lang": (
                settings.pdf.target_lang
                if spec.target_lang_setting == "pdf"
                else settings.target_lang
            ),
            "domain_preset": (
                surface_setting(settings, normalized_surface, "domain_preset", "")
                if spec.domain_preset_in_snapshot
                else ""
            ),
            "smart_context": (
                bool(surface_setting(settings, normalized_surface, "smart_context", True))
                if spec.domain_preset_in_snapshot
                else None
            ),
            "prompt_signature": hashlib.sha256(prompt_source.encode("utf-8")).hexdigest()[:12],
            "connections": self._connection_summaries(context),
        }
        task_snapshot.update(
            spec.snapshot(settings=settings, files=files, options=selected_options)
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
        if files:
            extras = get_surface(surface).fingerprint_extras(files=files, options=options)
            if extras:
                fingerprint_payload["surface_extras"] = extras
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
        spec = get_surface(prepared.surface)
        owner_label = spec.owner_label(prepared.task_snapshot)
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
                "surface_busy": "同类翻译已有任务正在进行，请待其结束后再开始。",
                "stale": "确认后正在运行的任务发生了变化，请重新点击开始。",
            }
            raise TaskConflictError(
                messages.get(reason, "任务未能开始，请稍后重新点击开始。"), reason=reason
            )
        lease = attempted.lease
        try:
            api_schedulers = {
                role: lease.scheduler_for(group)
                for role, group in prepared.role_groups.items()
            }
            if not spec.requires_source:
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
                batch_output_dir=str(prepared.options.batch_output_dir or ""),
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
                self._raise_if_maintenance_active()
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
                        task,
                        {
                            "message": user_facing_reason(
                                exc, fallback="任务因意外问题未能启动，请重试。"
                            )
                        },
                        "error",
                    )
                    task.state = "error"
                    task.terminal = True
                    task.updated_at = time.time()
                    task.result = _sanitize_task_data(failure_result)
                    # Terminal flag and terminal event must be visible together.
                    self._append_event(task, "error", task.result)
            raise

    def _batch_dir_in_use(self, task: ApiTask) -> bool:
        if not task.batch_output_dir:
            return False
        with self._lock:
            return any(
                other is not task and not other.terminal and other.batch_output_dir == task.batch_output_dir
                for other in self._tasks.values()
            )

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
                ["两个任务共用同一条模型连接，可能出现变慢、排队或超时，且均会计费。"]
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

    def task_source(self, task_id: str) -> dict[str, Any]:
        """Return the source location of a task still held by this sidecar.

        Task-center payloads and history never carry source paths.  The
        workspace keeps them in window memory instead, which a reload of the
        window loses: re-adopting a running or paused task then showed an
        empty source bar and "this task kept no source path".  Only live
        in-memory tasks answer here; nothing is read from or written to
        history, so persisted records stay path-free.
        """
        task = self._get_task(task_id)
        with task.condition:
            return {
                "task_id": task.task_id,
                "surface": task.surface,
                "source_path": task.source_path,
                "files": [
                    {"file_id": file_id, "path": path}
                    for file_id, path in task.file_sources.items()
                ],
            }

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
            "stop_requested": task.stop_requested,
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
        # 任务列表每 4 秒轮询一次，界面上只用摘要；日志只在单个任务的状态里返回。
        # 以前每条记录都带整份日志，轮询报文随历史线性膨胀（审计 高-4）。
        recent_records = sorted(
            recent.values(), key=lambda record: record.get("updated_at", 0), reverse=True
        )[:200]
        return {"active": [_without_logs(record) for record in active],
                "recent": [_without_logs(record) for record in recent_records],
                "active_work_count": self.active_task_count()}

    @contextmanager
    def maintenance_window(self) -> Generator[int, None, None]:
        """Hold off new work while a maintenance clean-up runs.

        Yields the number of active tasks seen at entry.  When it is 0 no task
        or page operation can register until the block exits, so the caller's
        "no active tasks" check stays true for the whole clean-up.
        """
        with self._lock:
            active = self.active_task_count()
            if not active:
                self._maintenance_holds += 1
        try:
            yield active
        finally:
            if not active:
                with self._lock:
                    self._maintenance_holds -= 1

    def _raise_if_maintenance_active(self) -> None:
        """Caller holds ``self._lock``."""
        if self._maintenance_holds:
            raise TaskConflictError(
                "正在清理本地数据，请稍候再开始。",
                reason="maintenance_active",
            )

    def active_task_count(self) -> int:
        """Count all work that can write data, including terminal page operations."""
        with self._lock:
            return sum(1 for task in self._tasks.values() if not task.terminal or task.rerun_active)

    def active_surfaces(self) -> frozenset[str]:
        """Keys of the types with a task still running or paused (an extension cannot be replaced then)."""
        with self._lock:
            return frozenset(task.surface for task in self._tasks.values() if not task.terminal or task.rerun_active)

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
            raise TaskHistoryError("任务历史无法写入，暂时无法清空记录。") from exc

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
                    "任务仍在运行，请先停止，再删除这条记录。",
                    reason="task_active",
                )
            if live is not None and live.rerun_active:
                # Terminal, but still writing: the rerun (or restore) rebuilds
                # the output file and the report under this record.
                raise TaskConflictError(
                    "此任务正在将一页切换回上一版，请稍候几秒再删除此记录。"
                    if live.rerun_kind == "restore"
                    else "此任务正在重新生成某一页，请待其完成后再删除此记录。",
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
            raise TaskHistoryError("任务历史无法写入，暂时无法删除记录。") from exc
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
                    "message": "应用中途关闭，任务未完成。已生成的文件和报告仍保留在输出目录中，可重新开始翻译。",
                    "recovery": {"can_resume": False, "reason": "sidecar_restarted"},
                }, "interrupted")
                task.state = "interrupted"
                task.result = result
                self._archive_diagnostics(task)
                task.terminal = True
                task.updated_at = time.time()
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
            task.stop_requested = True
        task.runner.stop()
        self._append_event(task, "stopping", {"state": "stopping"})
        return self.task_status(task_id)

    def pause_task(self, task_id: str) -> dict[str, Any]:
        """Pause PDF page submission while its sidecar and snapshot remain alive."""
        task = self._get_task(task_id)
        if not get_surface(task.surface).can_pause:
            raise TaskInputError("此任务不支持暂停。")
        with task.condition:
            if task.terminal:
                return self.task_status(task_id)
            if not hasattr(task.runner, "pause"):
                raise TaskInputError("此任务不支持暂停。")
            if task.state == "paused":
                return self.task_status(task_id)
            if task.state != "running":
                raise TaskInputError("任务当前无法暂停。")
            # A surface that pauses at its next safe point (CAD: between
            # drawings or between request batches) keeps the intermediate
            # state visible until the runner acknowledges it.
            task.state = (
                "pausing" if get_surface(task.surface).pause_mode == "file_boundary" else "paused"
            )
            self._append_event(task, task.state, {"state": task.state})
        task.runner.pause()  # type: ignore[attr-defined]
        return self.task_status(task_id)

    def resume_task(self, task_id: str) -> dict[str, Any]:
        """Resume a same-sidecar PDF task with its frozen settings."""
        task = self._get_task(task_id)
        if not get_surface(task.surface).can_resume:
            raise TaskInputError("当前任务不支持继续。")
        with task.condition:
            if task.terminal:
                raise TaskInputError("任务已结束，无法继续。")
            if not hasattr(task.runner, "resume"):
                raise TaskInputError("当前任务不支持继续。")
            if task.state != "paused":
                raise TaskInputError("只有已暂停的任务可以继续。")
            task.state = "running"
            self._append_event(task, "resumed", {"state": "running"})
        task.runner.resume()  # type: ignore[attr-defined]
        return self.task_status(task_id)

    def end_paused_task(self, task_id: str) -> dict[str, Any]:
        """End an intentionally paused PDF task and retain its partial evidence."""
        task = self._get_task(task_id)
        if not get_surface(task.surface).can_end_paused:
            raise TaskInputError("此任务暂停后无法直接结束，请点击「停止」。")
        with task.condition:
            if task.terminal:
                return self.task_status(task_id)
            if task.state != "paused":
                raise TaskInputError("只有已暂停的任务可以结束。")
            task.state = "stopping"
            task.stop_requested = True
            self._append_event(task, "stopping", {"state": "stopping", "reason": "end_paused"})
        if hasattr(task.runner, "end_paused"):
            task.runner.end_paused()  # type: ignore[attr-defined]
        else:
            task.runner.stop()
        return self.task_status(task_id)

    def pdf_page_review(self, task_id: str) -> dict[str, Any]:
        """Return the pull-only per-page snapshot backing the review panel."""
        try:
            task = self._get_task(task_id)
        except TaskNotFoundError:
            # 重启后历史任务没有 runner，逐页记录只剩运行结束时那份清单：把它读回来，
            # 而不是让界面只能写「此历史任务的逐页记录已不可用」。
            restored = restored_pdf_page_review(self._history_record(task_id))
            if restored is None:
                raise
            return restored
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
        try:
            task = self._get_task(task_id)
        except TaskNotFoundError:
            # 同上：重启后的历史任务只能从清单和页图存档里找这一页，没有任何操作入口
            # （快照里 actionable / rerun_actionable 都是假），只回答「当时长什么样」。
            try:
                return restored_page_image_path(
                    self._history_record(task_id),
                    relative_path=relative_path,
                    page_number=page_number,
                    kind=kind,
                )
            except PdfPageActionError as exc:
                raise TaskInputError(str(exc)) from exc
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
        if normalized not in {"regenerate", "skip", "cancel"}:
            raise TaskInputError("此页只能选择「重新生成」或「跳过」。")
        task = self._get_task(task_id)
        runner = self._pdf_review_runner(task)
        if normalized == "regenerate":
            method = runner.request_page_regenerate
        elif normalized == "skip":
            method = runner.request_page_skip
        else:
            method = getattr(runner, "cancel_page_action", None)
            if not callable(method):
                raise TaskInputError("此 PDF 任务不支持撤回已安排的单页操作。")
        # The state check and the queueing happen under the same task condition
        # that ``resume_task`` uses to flip the state.  Either this request is
        # queued while the runner still waits inside its pause loop, or the task
        # is already running again and the request is rejected outright.
        with task.condition:
            if task.terminal:
                raise TaskConflictError("任务已结束，无法再进行单页操作。", reason="task_terminal")
            if task.state != "paused":
                raise TaskConflictError(
                    "只有已暂停的 PDF 与图片任务可以单独处理某一页。",
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
            raise TaskInputError("此任务的逐页记录已释放，无法再重新生成单页。")
        with task.condition:
            if self._shutdown.is_set():
                raise TaskConflictError("应用正在关闭，请重新打开后再操作。", reason="shutting_down")
            if not task.terminal:
                raise TaskConflictError(
                    "任务尚未结束；运行中的任务请先暂停，再进行单页操作。",
                    reason="task_not_terminal",
                )
            if task.rerun_active:
                raise TaskConflictError(
                    "此任务正在将一页切换回上一版，即将完成，请稍候再重新生成。"
                    if task.rerun_kind == "restore"
                    else "此任务已有一页正在重新生成，请待其完成后再操作下一页。",
                    reason="page_rerun_active",
                )
            with self._lock:
                self._raise_if_maintenance_active()
                task.rerun_active = True
            task.rerun_kind = "rerun"
        lease: ScheduledTaskLease | None = None
        try:
            # A rerun spends real API budget, so it reserves the shared groups
            # exactly like a task does.  That also gives the right answer for
            # free when a real PDF task is running: the same-type slot is taken.
            attempted = self._registry.reserve_task(
                owner_key=f"{task.task_id}:page-rerun",
                owner_label=get_surface("pdf").label,
                task_type="pdf",
                group_capacities=task.group_capacities,
            )
            if attempted.lease is None:
                reason = attempted.reason or "conflict"
                messages = {
                    "surface_busy": "有 PDF 与图片翻译任务正在进行，请待其结束后再重新生成此页。",
                }
                raise TaskConflictError(
                    messages.get(reason, "此页未能开始重新生成，请稍后重试。"),
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
            raise TaskInputError("此任务的逐页记录已释放，无法再切换回上一版。")
        with task.condition:
            if self._shutdown.is_set():
                raise TaskConflictError("应用正在关闭，请重新打开后再操作。", reason="shutting_down")
            if not task.terminal:
                raise TaskConflictError(
                    "任务尚未结束；运行中的任务请先暂停，再进行单页操作。",
                    reason="task_not_terminal",
                )
            if task.rerun_active:
                raise TaskConflictError(
                    "此任务正在将另一页切换回上一版，即将完成，请稍候再操作。"
                    if task.rerun_kind == "restore"
                    else "此任务有一页正在重新生成，请待其完成后再切换回上一版。",
                    reason="page_rerun_active",
                )
            with self._lock:
                self._raise_if_maintenance_active()
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
            raise TaskInputError("只有 PDF 与图片翻译任务有逐页审核记录。")
        runner = task.runner
        required = (
            "pdf_page_snapshot",
            "resolve_page_image_path",
            "request_page_regenerate",
            "request_page_skip",
        )
        if not all(callable(getattr(runner, name, None)) for name in required):
            raise TaskInputError("此 PDF 任务没有逐页审核记录。")
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
            # 每批事件后补一段 SSE 注释。macOS 的 WebView（WebKit）会把流里零碎的小块
            # 攒着，等后面再来数据才交给页面：一批事件里最后那条（常常是文件进度）就一直
            # 慢一拍，界面上计数比运行日志少一批。注释行界面直接忽略，只用来把前面的
            # 事件「推」出去。
            yield SSE_FLUSH_PADDING
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
                                "message": "等待关闭超时，单页操作尚未确认结束。",
                            })
                        break
                    task.condition.wait(timeout=min(remaining, 0.25))
        _log_threads_still_running(tasks)
        self.mark_active_tasks_interrupted()
        self.flush_history()

    # The three seams below are where the task manager hands over to a
    # surface (api/surfaces/).  Tests replace them on an instance to run the
    # lifecycle without real files, models or converters.

    def _scan(
        self,
        root: Path,
        surface: TaskSurface,
        options: TaskOptions,
        *,
        selected_paths: set[str] | None = None,
    ) -> list[Any]:
        return get_surface(surface).scan(root, options, selected_paths=selected_paths)

    def _validate_preflight(
        self,
        surface: TaskSurface,
        *,
        files: list[Any],
        settings: AppSettings,
        options: TaskOptions,
    ) -> None:
        get_surface(surface).validate_preflight(files=files, settings=settings, options=options)

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
        return get_surface(surface).build_runner(
            RunnerRequest(
                files=files,
                settings=settings,
                source_root=source_root,
                options=options,
                source_lang=source_lang,
                key_overrides=key_overrides,
                api_schedulers=dict(api_schedulers or {}),
                connection_chains=dict(connection_chains or {}),
            )
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
                result={
                    "message": user_facing_reason(
                        "Translation runner ended without a terminal message.",
                        fallback="翻译意外中止，请重试。",
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
                        exc, fallback="任务运行时发生意外问题，请重试或查看运行日志。"
                    )
                },
            )

    def _handle_message(self, task: ApiTask, message: Any) -> None:
        event_type = _event_type_for_message(message)
        payload = _json_safe(asdict(message) if is_dataclass(message) else message)
        if isinstance(message, ProgressMsg):
            phase_total = int(message.phase_total)
            phase_index = max(1, int(message.phase_index))
            if phase_total > 0:
                phase_index = min(phase_total, phase_index)
            stage_percent = min(100.0, max(0.0, 100.0 * message.step_done / max(1, message.step_total)))
            overall_percent = overall_progress_percent(
                task.surface, phase_index, phase_total, stage_percent
            )
            # 事件本身也带上总进度：界面直接用它画进度条，不再各自按「阶段平均分」折算。
            if isinstance(payload, dict):
                payload["overall_percent"] = round(overall_percent, 1)
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
            if get_surface(task.surface).pause_mode == "file_boundary":
                # The file-boundary pause is acknowledged through a status line,
                # and the page reads this surface's progress off the snapshot.
                with task.condition:
                    task.task_snapshot.update(task.progress)
        if isinstance(message, StatusMsg) and get_surface(task.surface).pause_mode == "file_boundary":
            if str(message.phase_desc).startswith(CAD_PAUSED_STATUS_PREFIX):
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
            if state in {"error", "stopped"} and not self._batch_dir_in_use(task):
                # 完成态不动：PDF 完成后还能单页重跑，会往同一个目录里写。同一批里别的
                # 任务还在往这个文件夹写时也不动（它可能还没写出第一个文件）。
                result = _drop_empty_output_dir(result)
            task.state = state
            task.result = _sanitize_task_data(result)
            # 赶在终态事件之前写好：界面一收到终态就会问「有没有诊断记录」，晚一步
            # 它就记成「没有」。也要赶在 runner 换成 RetiredRunner 之前（连接摘要
            # 用它冻结的那份设置）。
            self._archive_diagnostics(task)
            task.terminal = True
            task.updated_at = time.time()
            # Append while still holding the condition (it is reentrant): a
            # reader that observes terminal=True must also see the terminal
            # event, or an SSE stream ends cleanly without ever carrying it.
            self._append_event(task, event_type, task.result)
        task.lease.release()
        self._retire_terminal_task(task)

    def _archive_diagnostics(self, task: ApiTask) -> None:
        """Leave the anonymous support record that 「导出诊断」 exports.

        ``core.diagnostics`` 早就能写匿名诊断记录，任务中心也有导出按钮，但以前
        没有任何任务结束时真的去写，按钮对所有任务都是灰的。每个结束的任务都留
        一份（完成、出错、停止、中断），按诊断模块自己的上限滚动清理。记录里只有
        类型、阶段、计数和服务商类别，不含文件名、路径、原文译文或密钥。写失败
        只记日志：诊断是辅助信息，不能因为它让任务收不了尾。

        PDF 的清单/报告路径不用单独转交：终态消息本来就带着，而 ``_sanitize_task_data``
        把 ``output_dir``/``manifest_path``/``report_path`` 归在 ``_ARTIFACT_PATH_KEYS``
        里原样保留，诊断模块直接从严脱敏后的 result 取即可。
        """
        try:
            with task.condition:
                state = task.state
                result = dict(task.result or {})
                logs = list(task.logs)
                files = list(task.file_progress.values())
                progress = dict(task.progress)
            runner = task.runner
            settings = getattr(runner, "_settings", None) or getattr(runner, "settings", None)
            if settings is None:
                settings = self._settings_loader()
            diagnostics.archive_task_diagnostics(
                surface=str(task.surface),
                # CAD 的阶段名带后半句（「翻译 · 第 1 / 3 个文件」），只取「 · 」前面。
                phase=_DIAGNOSTIC_PHASES.get(str(progress.get("phase_name") or "").split(" · ")[0].strip(), "start"),
                task_id=task.task_id,
                settings=settings,
                selected_files=files,
                logs=logs,
                done=result,
                error_message=str(result.get("message") or "") if state in {"error", "interrupted"} else "",
                status=state,
                progress=progress or None,
            )
        except Exception:  # noqa: BLE001 - diagnostics must never break task teardown
            logger.opt(exception=True).warning("任务诊断记录写入失败；任务本身不受影响。")

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
            if not get_surface(task.surface).keeps_runner_after_finish:
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
        if event_type == "log" and isinstance(data, dict) and data.get("visible") is False:
            # 运行器标了 visible=False 的行（逐页「已渲染 / 已提交」、像素尺寸细节等）
            # 只给排查用：运行器自己已经写进任务日志和 app.log，这里不再推到界面的
            # 运行日志里。以前这个标记没人读，这些行照样显示给用户。
            return
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
                # 只留最近的一段：这份列表会整份写进 task_history.json，也会随任务
                # 状态返回。不封顶时长任务能把历史文件撑到几十 MB（审计 高-4）。
                # 完整日志另有任务日志文件和诊断包，这里只供界面回看。
                if len(task.logs) > MAX_TASK_LOG_LINES:
                    del task.logs[: len(task.logs) - MAX_TASK_LOG_LINES]
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


def _apply_target_lang(settings: AppSettings, spec: Any, requested: str | None) -> str:
    """Put the task's target language where the surface reads it; return it.

    A requested language wins; otherwise a page with its own saved target
    (``per_surface``) copies it into the shared field.
    """
    if requested:
        if spec.target_lang_setting == "pdf":
            settings.pdf.target_lang = requested
        else:
            settings.target_lang = requested
    elif spec.target_lang_setting == "per_surface":
        settings.target_lang = surface_setting(settings, spec.key, "target_lang", settings.target_lang)
    return str(settings.pdf.target_lang if spec.target_lang_setting == "pdf" else settings.target_lang)


def _scanned_file_name(item: Any) -> str:
    """Return one scanned entry's file name without assuming a scanner type.

    Each surface returns its own item class (``FileItem`` / ``WordFileItem`` /
    the PDF scanner entry), and task-manager tests hand in bare objects.  Only
    the file name is taken: the task title must not leak a full local path.
    """
    raw = getattr(item, "path", None) or getattr(item, "name", "")
    value = str(raw or "").strip()
    return Path(value).name if value else ""


def _raise_on_output_name_clash(files: list[Any], root: Path) -> None:
    """根外的文件按文件名落在输出顶层：两份落到同一个位置时，后一份会盖掉前一份。"""
    seen: dict[str, Path] = {}
    for item in files:
        path = Path(item.path).expanduser().resolve()
        relative = path.relative_to(root) if _path_is_within(path, root) else Path(path.name)
        key = str(relative).casefold()
        if key in seen and seen[key] != path:
            raise TaskInputError(
                f"选中的文件里有两份「{path.name}」来自不同文件夹，译文会互相覆盖。"
                "请分两次翻译，或先给其中一份改名。"
            )
        seen[key] = path


def _path_is_within(path: Path, root: Path) -> bool:
    """``path`` 是 ``root`` 本身或在它下面（两边都已 resolve）。"""
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


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



# 各页面每个阶段在总进度条上占多少，见各 Surface 的 ``phase_weights``。阶段耗时差别
# 很大：PDF 的「翻译页面」要等图像模型一页页画，占了整个任务的绝大部分时间，按阶段
# 平均分的话进度条会在 25%~50% 之间爬完整个任务，最后一秒从 50% 冲到 100%。


def overall_progress_percent(
    surface: str, phase_index: int, phase_total: int, stage_percent: float
) -> float:
    """Fold one phase's progress into the whole task's 0-100 progress.

    ``phase_total <= 0`` means the runner reports whole-batch progress directly
    (CAD), so the stage percentage already is the overall one. Weights that do
    not line up with the reported phase count (a phase was added or dropped)
    fall back to equal weights rather than guessing.
    """
    if phase_total <= 0:
        return stage_percent
    try:
        weights = tuple(get_surface(surface).phase_weights)[:phase_total]
    except TaskInputError:
        weights = ()
    if len(weights) != phase_total:
        weights = (1.0,) * phase_total
    total = sum(weights) or 1.0
    index = max(1, min(phase_total, phase_index))
    done = sum(weights[: index - 1]) + weights[index - 1] * stage_percent / 100.0
    return min(100.0, max(0.0, 100.0 * done / total))


def _without_logs(record: dict[str, Any]) -> dict[str, Any]:
    if "logs" not in record:
        return record
    return {key: value for key, value in record.items() if key != "logs"}


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


_IGNORABLE_OUTPUT_LEFTOVERS = {".DS_Store", "Thumbs.db", "desktop.ini"}


def _drop_empty_output_dir(result: dict[str, Any]) -> dict[str, Any]:
    """Remove the timestamped output folder when the task wrote nothing into it.

    Runners create ``…_翻译输出_<时间>`` up front.  A task that failed or was
    stopped before its first file used to leave that empty folder next to the
    user's sources, and the banner still offered "打开输出目录" onto it.  Only
    a folder this app generated (the name marker) with no file anywhere inside
    is removed; anything else stays exactly as it is.
    """
    raw = result.get("output_dir") if isinstance(result, dict) else None
    if not raw:
        return result
    folder = Path(str(raw))
    if GENERATED_OUTPUT_DIR_MARKER not in folder.name:
        return result
    try:
        if not folder.is_dir():
            return result
        entries = list(folder.rglob("*"))
        if any(item.is_file() and item.name not in _IGNORABLE_OUTPUT_LEFTOVERS for item in entries):
            return result
        for item in sorted(entries, key=lambda path: len(path.parts), reverse=True):
            if item.is_dir():
                item.rmdir()
            else:
                item.unlink()
        folder.rmdir()
    except OSError:
        # 删不掉（权限、正好有人在往里写）就原样留着：多一个空文件夹不碍事，误删才碍事。
        return result
    cleaned = dict(result)
    cleaned["output_dir"] = ""
    for key in ("translated_dir", "publication_dir", "review_dir"):
        value = cleaned.get(key)
        if value and Path(str(value)).is_relative_to(folder):
            cleaned[key] = ""
    return cleaned


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


def _log_threads_still_running(tasks: list[Any]) -> None:
    """When a task did not stop in time, write down where its thread is waiting.

    A shutdown that uses its whole budget is otherwise a black box: the log
    only shows that it took 12 seconds, not which converter, model request or
    lock held it.  Only runs when something actually overstayed.
    """
    stuck = [task for task in tasks if not task.terminal or task.rerun_active]
    if not stuck:
        return
    import sys
    import traceback

    names = {thread.ident: thread.name for thread in threading.enumerate()}
    # The installed app records paths relative to its bundle ("core/x.py"), a
    # source run absolute ones; compare both the same way.
    markers = ("/core/", "/engines/", "/adapters/")
    for ident, frame in sys._current_frames().items():
        # The pump threads only wait on their runner's queue; the runner is what to show.
        if ident == threading.get_ident() or str(names.get(ident, "")).startswith(("api-task-", "api-rerun-")):
            continue
        frames = traceback.extract_stack(frame)
        # Only threads that are inside the app's own work, not uvicorn or idle pools.
        if not any(
            marker in "/" + entry.filename.replace("\\", "/") for entry in frames for marker in markers
        ):
            continue
        stack = "".join(traceback.format_list(frames[-12:]))
        logger.warning(f"关闭时任务未在时限内停下，线程 {names.get(ident, ident)} 停在：\n{stack}")
