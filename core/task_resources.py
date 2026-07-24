"""Shared reservations for upstream model API resources."""

from __future__ import annotations

import math
import threading
import uuid
from collections import deque
from collections.abc import Callable, Hashable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass

from core.api_scheduler import (
    API_CONCURRENCY_ACTION_REDUCED,
    API_CONCURRENCY_ACTION_RETRY_CURRENT,
    API_CONCURRENCY_ACTION_UNAVAILABLE,
    API_REQUEST_CATEGORY_NORMAL,
    ApiConcurrencyLimitDecision,
    ApiSchedulerAcquireCancelled,
    ApiSchedulerLease,
    ApiSchedulerSnapshot,
)


@dataclass(frozen=True)
class TaskResourceReservation:
    """Immutable view of one task's reserved upstream resources."""

    owner_key: str
    owner_label: str
    resources: frozenset[Hashable]
    conservative: bool = False


@dataclass(frozen=True)
class ScheduledTaskReservation:
    """One active task in the Phase 7 scheduler.

    ``resources`` are opaque connection identities.  They deliberately retain
    no API keys; callers may only turn them into a user-facing summary through
    the frozen model snapshot kept by the task manager.
    """

    owner_key: str
    owner_label: str
    task_type: str
    resources: frozenset[Hashable]
    group_capacities: tuple[tuple[Hashable, int], ...]

    def capacity_for(self, resource: Hashable) -> int:
        return dict(self.group_capacities).get(resource, 1)


@dataclass(frozen=True)
class TaskReservationAttempt:
    lease: "ScheduledTaskLease | None"
    reason: str | None
    revision: int


class TaskResourceLease:
    """Idempotent handle returned by :class:`TaskResourceRegistry`."""

    def __init__(self, registry: TaskResourceRegistry, token: str) -> None:
        self._registry = registry
        self._token = token
        self._release_lock = threading.Lock()
        self._released = False

    @property
    def released(self) -> bool:
        with self._release_lock:
            return self._released

    def release(self) -> bool:
        """Release once and report whether this call owned the release."""
        with self._release_lock:
            if self._released:
                return False
            self._released = True
        self._registry._release(self._token)
        return True


class TaskGroupScheduler:
    """Per-task facade over a shared, fair upstream connection scheduler.

    Runners already depend on the small ``WeightedApiScheduler`` protocol.
    This facade intentionally exposes that protocol so Excel, Word, PDF and
    TM cleaning can use the same connection group without knowing about one
    another.
    """

    def __init__(self, group: "FairApiGroupScheduler", owner_key: str) -> None:
        self._group = group
        self._owner_key = owner_key

    @property
    def capacity(self) -> int:
        return self._group.capacity

    def normalize_weight(self, weight: int | float | None) -> int:
        return self._group.normalize_weight(weight)

    @contextmanager
    def slot(
        self,
        weight: int | float | None = 1,
        *,
        category: str = API_REQUEST_CATEGORY_NORMAL,
        should_stop: Callable[[], bool] | None = None,
    ) -> Iterator[ApiSchedulerLease]:
        lease = self.acquire_lease(weight, category=category, should_stop=should_stop)
        try:
            yield lease
        finally:
            self.release(lease, category=category)

    def acquire(
        self,
        weight: int | float | None = 1,
        *,
        category: str = API_REQUEST_CATEGORY_NORMAL,
    ) -> int:
        return self.acquire_lease(weight, category=category).weight

    def acquire_lease(
        self,
        weight: int | float | None = 1,
        *,
        category: str = API_REQUEST_CATEGORY_NORMAL,
        should_stop: Callable[[], bool] | None = None,
    ) -> ApiSchedulerLease:
        return self._group.acquire_lease(
            self._owner_key,
            weight,
            category=category,
            should_stop=should_stop,
        )

    def release(
        self,
        weight: int | float | ApiSchedulerLease | None = 1,
        *,
        category: str = API_REQUEST_CATEGORY_NORMAL,
    ) -> None:
        self._group.release(weight, category=category)

    def snapshot(self) -> ApiSchedulerSnapshot:
        return self._group.snapshot()

    def set_capacity(self, capacity: int) -> None:
        self._group.set_capacity(capacity)

    def register_concurrency_limit_hit(
        self,
        request_generation: int | None,
    ) -> ApiConcurrencyLimitDecision:
        return self._group.register_concurrency_limit_hit(request_generation)


class FairApiGroupScheduler:
    """One connection-group budget shared fairly among active tasks.

    The capacity is the sum of the frozen per-task throughput values.  A
    simple task FIFO sits in front of the weighted capacity check, so a busy
    task cannot continuously re-acquire slots ahead of another task waiting on
    the same API connection.  429 feedback changes only this in-memory group.
    """

    def __init__(self) -> None:
        self.capacity = 1
        self.initial_capacity = 1
        self.minimum_capacity = 1
        self._generation = 0
        self._active_total_weight = 0
        self._active_normal_weight = 0
        self._active_recovery_weight = 0
        self._waiting_recovery_count = 0
        self._task_capacities: dict[str, int] = {}
        self._waiting_tasks: deque[str] = deque()
        self._waiting_task_set: set[str] = set()
        self._condition = threading.Condition()

    def add_task(self, owner_key: str, capacity: int) -> TaskGroupScheduler:
        with self._condition:
            self._task_capacities[owner_key] = max(1, int(capacity or 1))
            self._reset_capacity_locked()
        return TaskGroupScheduler(self, owner_key)

    def remove_task(self, owner_key: str) -> None:
        with self._condition:
            self._task_capacities.pop(owner_key, None)
            self._remove_waiter_locked(owner_key)
            if self._task_capacities:
                self._reset_capacity_locked()
            self._condition.notify_all()

    @property
    def empty(self) -> bool:
        with self._condition:
            return not self._task_capacities

    def normalize_weight(self, weight: int | float | None) -> int:
        try:
            normalized = int(math.ceil(float(weight or 1)))
        except (TypeError, ValueError, OverflowError):
            normalized = 1
        with self._condition:
            return min(max(1, normalized), max(1, self.capacity))

    def acquire_lease(
        self,
        owner_key: str,
        weight: int | float | None = 1,
        *,
        category: str = API_REQUEST_CATEGORY_NORMAL,
        should_stop: Callable[[], bool] | None = None,
    ) -> ApiSchedulerLease:
        normalized_weight = self.normalize_weight(weight)
        normalized_category = _normalize_category(category)
        with self._condition:
            if owner_key not in self._task_capacities:
                raise ApiSchedulerAcquireCancelled("任务资源预约已释放")
            if normalized_category != API_REQUEST_CATEGORY_NORMAL:
                self._waiting_recovery_count += 1
            self._add_waiter_locked(owner_key)
            try:
                while not self._can_acquire_locked(owner_key, normalized_weight):
                    if should_stop is not None and should_stop():
                        raise ApiSchedulerAcquireCancelled("API 请求在等待并发槽位期间已取消")
                    self._condition.wait(timeout=0.1 if should_stop is not None else None)
                    if owner_key not in self._task_capacities:
                        raise ApiSchedulerAcquireCancelled("任务资源预约已释放")
                if should_stop is not None and should_stop():
                    raise ApiSchedulerAcquireCancelled("API 请求在获得并发槽位前已取消")
                self._remove_waiter_locked(owner_key)
                self._active_total_weight += normalized_weight
                if normalized_category == API_REQUEST_CATEGORY_NORMAL:
                    self._active_normal_weight += normalized_weight
                else:
                    self._active_recovery_weight += normalized_weight
                return ApiSchedulerLease(weight=normalized_weight, generation=self._generation)
            finally:
                if normalized_category != API_REQUEST_CATEGORY_NORMAL:
                    self._waiting_recovery_count = max(0, self._waiting_recovery_count - 1)
                self._remove_waiter_locked(owner_key)

    def release(
        self,
        weight: int | float | ApiSchedulerLease | None = 1,
        *,
        category: str = API_REQUEST_CATEGORY_NORMAL,
    ) -> None:
        if isinstance(weight, ApiSchedulerLease):
            normalized_weight = max(1, int(weight.weight))
        else:
            normalized_weight = _normalize_weight(weight)
        normalized_category = _normalize_category(category)
        with self._condition:
            self._active_total_weight = max(0, self._active_total_weight - normalized_weight)
            if normalized_category == API_REQUEST_CATEGORY_NORMAL:
                self._active_normal_weight = max(0, self._active_normal_weight - normalized_weight)
            else:
                self._active_recovery_weight = max(0, self._active_recovery_weight - normalized_weight)
            self._condition.notify_all()

    def snapshot(self) -> ApiSchedulerSnapshot:
        with self._condition:
            return ApiSchedulerSnapshot(
                capacity=self.capacity,
                initial_capacity=self.initial_capacity,
                minimum_capacity=self.minimum_capacity,
                generation=self._generation,
                active_total_weight=self._active_total_weight,
                active_normal_weight=self._active_normal_weight,
                active_recovery_weight=self._active_recovery_weight,
                waiting_recovery_count=self._waiting_recovery_count,
            )

    def set_capacity(self, capacity: int) -> None:
        with self._condition:
            self.capacity = max(int(capacity or 1), self._active_total_weight, 1)
            self.initial_capacity = max(self.initial_capacity, self.capacity)
            self.minimum_capacity = _minimum_capacity(self.initial_capacity)
            self._condition.notify_all()

    def register_concurrency_limit_hit(
        self,
        request_generation: int | None,
    ) -> ApiConcurrencyLimitDecision:
        with self._condition:
            previous_capacity = self.capacity
            if request_generation is not None and request_generation != self._generation:
                return ApiConcurrencyLimitDecision(
                    action=API_CONCURRENCY_ACTION_RETRY_CURRENT,
                    previous_capacity=previous_capacity,
                    current_capacity=previous_capacity,
                    minimum_capacity=self.minimum_capacity,
                    generation=self._generation,
                    request_generation=request_generation,
                )
            if previous_capacity <= self.minimum_capacity:
                return ApiConcurrencyLimitDecision(
                    action=API_CONCURRENCY_ACTION_UNAVAILABLE,
                    previous_capacity=previous_capacity,
                    current_capacity=previous_capacity,
                    minimum_capacity=self.minimum_capacity,
                    generation=self._generation,
                    request_generation=request_generation,
                )
            self.capacity = max(self.minimum_capacity, previous_capacity - 1)
            self._generation += 1
            self._condition.notify_all()
            return ApiConcurrencyLimitDecision(
                action=API_CONCURRENCY_ACTION_REDUCED,
                previous_capacity=previous_capacity,
                current_capacity=self.capacity,
                minimum_capacity=self.minimum_capacity,
                generation=self._generation,
                request_generation=request_generation,
            )

    def _reset_capacity_locked(self) -> None:
        # A membership change creates a new runtime group.  Resetting here is
        # deliberate: a 429 reduction must not leak into a later task group.
        total = max(1, sum(self._task_capacities.values()))
        self.capacity = max(total, self._active_total_weight)
        self.initial_capacity = total
        self.minimum_capacity = _minimum_capacity(total)
        self._generation += 1
        self._condition.notify_all()

    def _can_acquire_locked(self, owner_key: str, weight: int) -> bool:
        if self._active_total_weight + weight > self.capacity:
            return False
        return bool(self._waiting_tasks and self._waiting_tasks[0] == owner_key)

    def _add_waiter_locked(self, owner_key: str) -> None:
        if owner_key not in self._waiting_task_set:
            self._waiting_tasks.append(owner_key)
            self._waiting_task_set.add(owner_key)

    def _remove_waiter_locked(self, owner_key: str) -> None:
        if owner_key not in self._waiting_task_set:
            return
        self._waiting_task_set.discard(owner_key)
        try:
            self._waiting_tasks.remove(owner_key)
        except ValueError:
            pass
        self._condition.notify_all()


class ScheduledTaskLease:
    """Idempotent ownership handle for an active scheduled task."""

    def __init__(
        self,
        registry: "TaskResourceRegistry",
        token: str,
        schedulers: Mapping[Hashable, TaskGroupScheduler],
    ) -> None:
        self._registry = registry
        self._token = token
        self._schedulers = dict(schedulers)
        self._released = False
        self._release_lock = threading.Lock()

    @property
    def released(self) -> bool:
        with self._release_lock:
            return self._released

    def scheduler_for(self, resource: Hashable) -> TaskGroupScheduler | None:
        return self._schedulers.get(resource)

    def release(self) -> bool:
        with self._release_lock:
            if self._released:
                return False
            self._released = True
        self._registry._release_scheduled(self._token)
        return True


class TaskResourceRegistry:
    """Atomically reserve model API groups for background translation tasks.

    An empty or unknown resource set is treated conservatively: it conflicts
    with every other task. Known disjoint API groups may run concurrently.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._reservations: dict[str, TaskResourceReservation] = {}
        self._scheduled: dict[str, ScheduledTaskReservation] = {}
        self._groups: dict[Hashable, FairApiGroupScheduler] = {}
        self._revision = 0

    def acquire(
        self,
        *,
        owner_key: str,
        owner_label: str,
        resources: Iterable[Hashable] | None,
    ) -> TaskResourceLease | None:
        resource_set = frozenset(resources or ())
        candidate = TaskResourceReservation(
            owner_key=str(owner_key or "").strip(),
            owner_label=str(owner_label or "").strip() or "Other task",
            resources=resource_set,
            conservative=not resource_set,
        )
        with self._lock:
            if any(
                self._conflicts(candidate, held)
                for held in self._reservations.values()
            ):
                return None
            token = uuid.uuid4().hex
            self._reservations[token] = candidate
        return TaskResourceLease(self, token)

    def reservations(self) -> tuple[TaskResourceReservation, ...]:
        with self._lock:
            return tuple(self._reservations.values())

    def release_all(self) -> None:
        with self._lock:
            self._reservations.clear()
            self._scheduled.clear()
            self._groups.clear()
            self._revision += 1

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    def scheduled_reservations(self) -> tuple[ScheduledTaskReservation, ...]:
        with self._lock:
            return tuple(self._scheduled.values())

    def reserve_task(
        self,
        *,
        owner_key: str,
        owner_label: str,
        task_type: str,
        group_capacities: Mapping[Hashable, int] | None,
        expected_revision: int | None = None,
    ) -> TaskReservationAttempt:
        """Atomically enforce the same-type slot and reserve shared groups."""
        normalized_type = str(task_type or "").strip() or "other"
        capacities = {
            resource: max(1, int(capacity or 1))
            for resource, capacity in (group_capacities or {}).items()
        }
        candidate = ScheduledTaskReservation(
            owner_key=str(owner_key or "").strip(),
            owner_label=str(owner_label or "").strip() or "Other task",
            task_type=normalized_type,
            resources=frozenset(capacities),
            group_capacities=tuple(capacities.items()),
        )
        with self._lock:
            if expected_revision is not None and expected_revision != self._revision:
                return TaskReservationAttempt(None, "stale", self._revision)
            if any(item.task_type == normalized_type for item in self._scheduled.values()):
                return TaskReservationAttempt(None, "surface_busy", self._revision)
            token = uuid.uuid4().hex
            schedulers: dict[Hashable, TaskGroupScheduler] = {}
            self._scheduled[token] = candidate
            for resource, capacity in capacities.items():
                group = self._groups.setdefault(resource, FairApiGroupScheduler())
                schedulers[resource] = group.add_task(candidate.owner_key, capacity)
            self._revision += 1
            return TaskReservationAttempt(
                ScheduledTaskLease(self, token, schedulers),
                None,
                self._revision,
            )

    def scheduling_risk(
        self,
        *,
        task_type: str,
        group_capacities: Mapping[Hashable, int] | None,
    ) -> dict[str, object]:
        """Return a non-secret view of active shared groups for preflight."""
        capacities = dict(group_capacities or {})
        with self._lock:
            busy = any(
                item.task_type == str(task_type or "").strip()
                for item in self._scheduled.values()
            )
            shared: list[dict[str, object]] = []
            for resource, candidate_capacity in capacities.items():
                participants = [
                    item
                    for item in self._scheduled.values()
                    if resource in item.resources
                ]
                if not participants:
                    continue
                active_capacity = sum(item.capacity_for(resource) for item in participants)
                shared.append(
                    {
                        "resource": resource,
                        "active_owner_keys": [item.owner_key for item in participants],
                        "active_capacity": active_capacity,
                        "candidate_capacity": max(1, int(candidate_capacity or 1)),
                        "total_potential_capacity": active_capacity
                        + max(1, int(candidate_capacity or 1)),
                    }
                )
            return {
                "surface_busy": busy,
                "shared_groups": shared,
                "revision": self._revision,
            }

    def _release(self, token: str) -> None:
        with self._lock:
            self._reservations.pop(token, None)

    def _release_scheduled(self, token: str) -> None:
        with self._lock:
            reservation = self._scheduled.pop(token, None)
            if reservation is None:
                return
            for resource in reservation.resources:
                group = self._groups.get(resource)
                if group is None:
                    continue
                group.remove_task(reservation.owner_key)
                if group.empty:
                    self._groups.pop(resource, None)
            self._revision += 1

    @staticmethod
    def _conflicts(
        candidate: TaskResourceReservation,
        held: TaskResourceReservation,
    ) -> bool:
        return bool(
            candidate.conservative
            or held.conservative
            or candidate.resources.intersection(held.resources)
        )


def _normalize_category(category: str) -> str:
    return API_REQUEST_CATEGORY_NORMAL if category == API_REQUEST_CATEGORY_NORMAL else "recovery"


def _normalize_weight(weight: int | float | None) -> int:
    try:
        return max(1, int(math.ceil(float(weight or 1))))
    except (TypeError, ValueError, OverflowError):
        return 1


def _minimum_capacity(initial_capacity: int) -> int:
    return max(1, int(math.floor(max(1, initial_capacity) * 0.2)))
