"""Cross-translation task queue and API concurrency group helpers."""

from __future__ import annotations

import hashlib
import itertools
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Iterable

from config import normalize_cloud_base_url
from core.api_scheduler import WeightedApiScheduler

TRANSLATION_TYPE_EXCEL = "excel"
TRANSLATION_TYPE_WORD = "word"
TRANSLATION_TYPE_PDF = "pdf"

TASK_STATUS_QUEUED = "queued"
TASK_STATUS_RUNNING = "running"
TASK_STATUS_COMPLETED = "completed"
TASK_STATUS_FAILED = "failed"
TASK_STATUS_STOPPED = "stopped"
TASK_STATUS_CANCELED = "canceled"

ACTIVE_TASK_STATUSES = {TASK_STATUS_QUEUED, TASK_STATUS_RUNNING}
HISTORICAL_TASK_STATUSES = {
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_STOPPED,
    TASK_STATUS_CANCELED,
}

_TASK_COUNTER = itertools.count(1)


@dataclass(frozen=True)
class ApiConcurrencyGroupKey:
    """Stable in-memory identity for one cloud API concurrency group."""

    mode: str
    base_url: str
    api_key_hash: str

    def as_display_id(self) -> str:
        return f"{self.mode}:{self.base_url}:{self.api_key_hash[:8]}"


@dataclass(frozen=True)
class ApiConcurrencyRequirement:
    key: ApiConcurrencyGroupKey
    declared_concurrency: int
    provider: str = ""
    role: str = ""
    role_label: str = ""
    key_fingerprint: str = ""


@dataclass(frozen=True)
class TranslationTaskSnapshot:
    """Immutable user-facing task snapshot captured at arrangement time."""

    title: str
    translation_type: str
    file_count: int
    target_language: str
    source_language: str = ""
    source_path: str = ""
    output_policy: str = ""
    output_path: str = ""
    domain: str = ""
    prompt_summary: str = ""
    model_role: str = ""
    provider: str = ""
    model: str = ""
    api_key_fingerprint: str = ""
    concurrency_label: str = ""
    params: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class TranslationTask:
    snapshot: TranslationTaskSnapshot
    group_requirements: tuple[ApiConcurrencyRequirement, ...]
    task_id: str = ""
    status: str = TASK_STATUS_QUEUED
    arranged_at: datetime = field(default_factory=datetime.now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    status_message: str = ""
    progress_label: str = ""
    output_path: str = ""
    error_message: str = ""
    block_reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.task_id:
            object.__setattr__(
                self,
                "task_id",
                f"translation-task-{next(_TASK_COUNTER):06d}",
            )

    @property
    def translation_type(self) -> str:
        return self.snapshot.translation_type

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_TASK_STATUSES

    @property
    def is_historical(self) -> bool:
        return self.status in HISTORICAL_TASK_STATUSES


def hash_secret(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def mask_secret(value: str, *, visible: int = 4) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= visible * 2:
        return text[0:visible] + "..." if len(text) > visible else text
    return f"{text[:visible]}...{text[-visible:]}"


def is_api_group_blocking_error(message: str) -> bool:
    """Return True for model/access errors that should pause the same API group."""
    text = str(message or "").strip().lower()
    if not text:
        return False
    patterns = (
        "invalid api key",
        "incorrect api key",
        "unauthorized",
        "forbidden",
        "permission denied",
        "401",
        "403",
        "api key 无效",
        "api key不可用",
        "api key 暂时不可用",
        "缺少 api key",
        "尚未填写 api key",
        "模型配置不可用",
        "模型名称不能为空",
        "缺少 base url",
        "服务商不支持",
    )
    return any(pattern in text for pattern in patterns)


def api_group_key_from_config(config: Any) -> ApiConcurrencyGroupKey | None:
    """Build a concurrency group key from an EffectiveModelConfig-like object."""
    mode = str(getattr(config, "mode", "") or "").strip()
    if mode != "cloud":
        return None
    provider = str(getattr(config, "provider", "") or "").strip()
    base_url = normalize_cloud_base_url(
        provider,
        str(getattr(config, "base_url", "") or ""),
    ).rstrip("/")
    api_key = str(getattr(config, "api_key", "") or "").strip()
    return ApiConcurrencyGroupKey(
        mode="cloud",
        base_url=base_url,
        api_key_hash=hash_secret(api_key),
    )


def api_requirement_from_config(
    config: Any,
    *,
    declared_concurrency: int,
) -> ApiConcurrencyRequirement | None:
    key = api_group_key_from_config(config)
    if key is None:
        return None
    return ApiConcurrencyRequirement(
        key=key,
        declared_concurrency=max(1, int(declared_concurrency or 1)),
        provider=str(getattr(config, "provider", "") or "").strip(),
        role=str(getattr(config, "role", "") or "").strip(),
        role_label=str(getattr(config, "label", "") or "").strip(),
        key_fingerprint=mask_secret(str(getattr(config, "api_key", "") or "")),
    )


class TranslationTaskQueue:
    """Own the active translation queue, history, and shared API schedulers."""

    def __init__(self, *, history_limit: int = 20) -> None:
        self.history_limit = max(1, int(history_limit or 20))
        self._tasks: list[TranslationTask] = []
        self._schedulers: dict[ApiConcurrencyGroupKey, WeightedApiScheduler] = {}
        self._blocked_groups: dict[ApiConcurrencyGroupKey, str] = {}

    def tasks(self) -> list[TranslationTask]:
        return list(self._tasks)

    def active_tasks(self) -> list[TranslationTask]:
        return [task for task in self._tasks if task.is_active]

    def historical_tasks(self) -> list[TranslationTask]:
        return [task for task in self._tasks if task.is_historical]

    def task(self, task_id: str) -> TranslationTask | None:
        for task in self._tasks:
            if task.task_id == task_id:
                return task
        return None

    def arrange(self, task: TranslationTask) -> TranslationTask:
        arranged = replace(task, status=TASK_STATUS_QUEUED)
        self._tasks.append(arranged)
        self._recalculate_group_capacities()
        self._refresh_block_reasons()
        return arranged

    def cancel(self, task_id: str, message: str = "已取消") -> TranslationTask | None:
        task = self.task(task_id)
        if task is None or task.status != TASK_STATUS_QUEUED:
            return None
        updated = replace(
            task,
            status=TASK_STATUS_CANCELED,
            finished_at=datetime.now(),
            status_message=message,
            block_reason="",
        )
        self._replace_task(updated)
        self._trim_history()
        self._recalculate_group_capacities()
        return updated

    def mark_running(self, task_id: str) -> TranslationTask | None:
        task = self.task(task_id)
        if task is None:
            return None
        updated = replace(
            task,
            status=TASK_STATUS_RUNNING,
            started_at=task.started_at or datetime.now(),
            status_message=task.status_message or "正在执行",
            block_reason="",
        )
        self._replace_task(updated)
        self._recalculate_group_capacities()
        return updated

    def finish(
        self,
        task_id: str,
        status: str,
        *,
        message: str = "",
        output_path: str = "",
        error_message: str = "",
    ) -> TranslationTask | None:
        if status not in HISTORICAL_TASK_STATUSES:
            raise ValueError(f"Unsupported terminal task status: {status}")
        task = self.task(task_id)
        if task is None:
            return None
        updated = replace(
            task,
            status=status,
            finished_at=datetime.now(),
            status_message=message,
            output_path=output_path or task.output_path,
            error_message=error_message,
            block_reason="",
        )
        self._replace_task(updated)
        self._trim_history()
        self._recalculate_group_capacities()
        return updated

    def update_progress(
        self,
        task_id: str,
        *,
        progress_label: str = "",
        status_message: str = "",
        output_path: str = "",
    ) -> TranslationTask | None:
        task = self.task(task_id)
        if task is None:
            return None
        updated = replace(
            task,
            progress_label=progress_label or task.progress_label,
            status_message=status_message or task.status_message,
            output_path=output_path or task.output_path,
        )
        self._replace_task(updated)
        return updated

    def block_groups(
        self,
        group_keys: Iterable[ApiConcurrencyGroupKey],
        reason: str,
    ) -> None:
        message = str(reason or "").strip() or "API 配置不可用"
        for key in group_keys:
            self._blocked_groups[key] = message
        self._refresh_block_reasons()

    def clear_group_blocks(
        self,
        group_keys: Iterable[ApiConcurrencyGroupKey] | None = None,
    ) -> None:
        if group_keys is None:
            self._blocked_groups.clear()
        else:
            for key in group_keys:
                self._blocked_groups.pop(key, None)
        self._refresh_block_reasons()

    def next_startable(self) -> TranslationTask | None:
        self._refresh_block_reasons()
        for task in self._tasks:
            if task.status != TASK_STATUS_QUEUED:
                continue
            if self._can_start(task):
                return self.mark_running(task.task_id)
        return None

    def scheduler_for(
        self,
        key: ApiConcurrencyGroupKey,
        *,
        fallback_capacity: int = 1,
    ) -> WeightedApiScheduler:
        capacity = max(self._group_capacity(key), int(fallback_capacity or 1), 1)
        scheduler = self._schedulers.get(key)
        if scheduler is None:
            scheduler = WeightedApiScheduler(capacity)
            self._schedulers[key] = scheduler
        else:
            scheduler.set_capacity(capacity)
        return scheduler

    def active_count(self, translation_type: str | None = None) -> int:
        return len(self._active_for_type(translation_type))

    def active_position(
        self,
        task_id: str,
        *,
        translation_type: str | None = None,
    ) -> tuple[int, int]:
        active = self._active_for_type(translation_type)
        total = len(active)
        for index, task in enumerate(active, start=1):
            if task.task_id == task_id:
                return index, total
        return 0, total

    def move_queued(self, task_id: str, direction: int) -> bool:
        if direction == 0:
            return False
        index = next(
            (
                i
                for i, task in enumerate(self._tasks)
                if task.task_id == task_id and task.status == TASK_STATUS_QUEUED
            ),
            None,
        )
        if index is None:
            return False
        step = -1 if direction < 0 else 1
        target = index + step
        while 0 <= target < len(self._tasks):
            candidate = self._tasks[target]
            if candidate.status == TASK_STATUS_RUNNING:
                return False
            if candidate.status == TASK_STATUS_QUEUED:
                self._tasks[index], self._tasks[target] = (
                    self._tasks[target],
                    self._tasks[index],
                )
                return True
            target += step
        return False

    def clear_history(self) -> None:
        self._tasks = [task for task in self._tasks if not task.is_historical]

    def _can_start(self, task: TranslationTask) -> bool:
        if self._translation_type_running(task.translation_type):
            return False
        for requirement in task.group_requirements:
            if requirement.key in self._blocked_groups:
                return False
            scheduler = self.scheduler_for(
                requirement.key,
                fallback_capacity=requirement.declared_concurrency,
            )
            snapshot = scheduler.snapshot()
            if snapshot.capacity - snapshot.active_total_weight < 1:
                return False
        return True

    def _translation_type_running(self, translation_type: str) -> bool:
        return any(
            task.status == TASK_STATUS_RUNNING
            and task.translation_type == translation_type
            for task in self._tasks
        )

    def _replace_task(self, updated: TranslationTask) -> None:
        for index, task in enumerate(self._tasks):
            if task.task_id == updated.task_id:
                self._tasks[index] = updated
                return

    def _active_for_type(self, translation_type: str | None) -> list[TranslationTask]:
        return [
            task
            for task in self._tasks
            if task.is_active
            and (
                translation_type is None
                or task.translation_type == translation_type
            )
        ]

    def _group_capacity(self, key: ApiConcurrencyGroupKey) -> int:
        capacity = 1
        for task in self._tasks:
            if not task.is_active:
                continue
            for requirement in task.group_requirements:
                if requirement.key == key:
                    capacity = max(capacity, requirement.declared_concurrency)
        return capacity

    def _recalculate_group_capacities(self) -> None:
        active_keys = {
            requirement.key
            for task in self._tasks
            if task.is_active
            for requirement in task.group_requirements
        }
        for key in active_keys:
            self.scheduler_for(key, fallback_capacity=self._group_capacity(key))

    def _refresh_block_reasons(self) -> None:
        for task in list(self._tasks):
            if task.status != TASK_STATUS_QUEUED:
                continue
            reason = ""
            for requirement in task.group_requirements:
                reason = self._blocked_groups.get(requirement.key, "")
                if reason:
                    break
            if task.block_reason != reason:
                self._replace_task(replace(task, block_reason=reason))

    def _trim_history(self) -> None:
        history = self.historical_tasks()
        if len(history) <= self.history_limit:
            return
        remove_ids = {
            task.task_id
            for task in history[: max(0, len(history) - self.history_limit)]
        }
        self._tasks = [task for task in self._tasks if task.task_id not in remove_ids]
