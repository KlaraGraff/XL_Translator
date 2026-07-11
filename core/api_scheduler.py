"""Shared weighted API request scheduling helpers."""

from __future__ import annotations

import math
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator


API_REQUEST_CATEGORY_NORMAL = "normal"
API_REQUEST_CATEGORY_RECOVERY = "recovery"
API_CONCURRENCY_ACTION_REDUCED = "reduced"
API_CONCURRENCY_ACTION_RETRY_CURRENT = "retry_current"
API_CONCURRENCY_ACTION_UNAVAILABLE = "unavailable"


class ApiSchedulerAcquireCancelled(RuntimeError):
    """Raised when a queued request is cancelled before acquiring a slot."""


@dataclass(frozen=True)
class ApiSchedulerLease:
    weight: int
    generation: int


@dataclass(frozen=True)
class ApiSchedulerSnapshot:
    capacity: int
    initial_capacity: int
    minimum_capacity: int
    generation: int
    active_total_weight: int
    active_normal_weight: int
    active_recovery_weight: int
    waiting_recovery_count: int


@dataclass(frozen=True)
class ApiConcurrencyLimitDecision:
    action: str
    previous_capacity: int
    current_capacity: int
    minimum_capacity: int
    generation: int
    request_generation: int | None = None

    @property
    def should_retry(self) -> bool:
        return self.action in {
            API_CONCURRENCY_ACTION_REDUCED,
            API_CONCURRENCY_ACTION_RETRY_CURRENT,
        }


class WeightedApiScheduler:
    """Limit concurrent API pressure by weighted request slots.

    The user-facing concurrency value remains the global capacity. Regular
    translation requests can use the full capacity while no recovery work is
    waiting; once recovery starts, new regular requests respect a soft cap so
    retry and semantic review work can make progress.
    """

    def __init__(
        self,
        capacity: int,
        *,
        normal_soft_ratio: float = 0.8,
    ) -> None:
        self.capacity = max(1, int(capacity or 1))
        self.initial_capacity = self.capacity
        self._adaptive_capacity_levels = _build_adaptive_capacity_levels(self.initial_capacity)
        self.minimum_capacity = self._adaptive_capacity_levels[-1]
        ratio = min(max(float(normal_soft_ratio or 0.8), 0.1), 1.0)
        self._normal_soft_ratio = ratio
        self.normal_soft_limit = max(1, int(math.floor(self.capacity * ratio)))
        self._active_total_weight = 0
        self._active_normal_weight = 0
        self._active_recovery_weight = 0
        self._waiting_recovery_count = 0
        self._generation = 0
        self._condition = threading.Condition()

    def normalize_weight(self, weight: int | float | None) -> int:
        try:
            normalized = int(math.ceil(float(weight or 1)))
        except (TypeError, ValueError, OverflowError):
            normalized = 1
        return min(max(1, normalized), self.capacity)

    @contextmanager
    def slot(
        self,
        weight: int | float | None = 1,
        *,
        category: str = API_REQUEST_CATEGORY_NORMAL,
        should_stop: Callable[[], bool] | None = None,
    ) -> Iterator[ApiSchedulerLease]:
        lease = self.acquire_lease(
            weight,
            category=category,
            should_stop=should_stop,
        )
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
        normalized_weight = self.normalize_weight(weight)
        normalized_category = _normalize_category(category)

        with self._condition:
            if normalized_category == API_REQUEST_CATEGORY_RECOVERY:
                self._waiting_recovery_count += 1
            try:
                while not self._can_acquire(normalized_weight, normalized_category):
                    if should_stop is not None and should_stop():
                        raise ApiSchedulerAcquireCancelled(
                            "API 请求在等待并发槽位期间已取消"
                        )
                    self._condition.wait(timeout=0.1 if should_stop is not None else None)
                if should_stop is not None and should_stop():
                    raise ApiSchedulerAcquireCancelled(
                        "API 请求在获得并发槽位前已取消"
                    )
                self._active_total_weight += normalized_weight
                if normalized_category == API_REQUEST_CATEGORY_NORMAL:
                    self._active_normal_weight += normalized_weight
                else:
                    self._active_recovery_weight += normalized_weight
                return ApiSchedulerLease(
                    weight=normalized_weight,
                    generation=self._generation,
                )
            finally:
                if normalized_category == API_REQUEST_CATEGORY_RECOVERY:
                    self._waiting_recovery_count = max(0, self._waiting_recovery_count - 1)

    def release(
        self,
        weight: int | float | ApiSchedulerLease | None = 1,
        *,
        category: str = API_REQUEST_CATEGORY_NORMAL,
    ) -> None:
        if isinstance(weight, ApiSchedulerLease):
            normalized_weight = max(1, int(weight.weight))
        else:
            normalized_weight = _normalize_release_weight(weight)
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
        """Update the group-level capacity while preserving active leases."""
        with self._condition:
            normalized = max(1, int(capacity or 1))
            self.capacity = max(normalized, self._active_total_weight)
            self.initial_capacity = max(self.initial_capacity, self.capacity)
            self._adaptive_capacity_levels = _build_adaptive_capacity_levels(
                self.initial_capacity
            )
            self.minimum_capacity = min(
                self.minimum_capacity,
                self._adaptive_capacity_levels[-1],
            )
            self.normal_soft_limit = max(
                1,
                int(math.floor(self.capacity * self._normal_soft_ratio)),
            )
            self._condition.notify_all()

    def register_concurrency_limit_hit(
        self,
        request_generation: int | None,
    ) -> ApiConcurrencyLimitDecision:
        """Adjust runtime capacity after an upstream concurrency-limit signal.

        A request carries the scheduler generation it acquired under. When a
        newer generation is already active, the signal belongs to an older
        burst and should only cause a retry under the current capacity.
        """
        with self._condition:
            previous_capacity = self.capacity
            if (
                request_generation is not None
                and request_generation != self._generation
            ):
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

            next_capacity = self._next_reduced_capacity_locked()
            self.capacity = next_capacity
            self.normal_soft_limit = max(
                1,
                int(math.floor(self.capacity * self._normal_soft_ratio)),
            )
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

    def _next_reduced_capacity_locked(self) -> int:
        for capacity in self._adaptive_capacity_levels:
            if capacity < self.capacity:
                return max(self.minimum_capacity, capacity)
        return max(self.minimum_capacity, self.capacity - 1)

    def _can_acquire(self, weight: int, category: str) -> bool:
        if self._active_total_weight + weight > self.capacity:
            return False

        if category != API_REQUEST_CATEGORY_NORMAL:
            return True

        recovery_pressure = (
            self._waiting_recovery_count > 0
            or self._active_recovery_weight > 0
        )
        if not recovery_pressure:
            return True

        if self._active_normal_weight + weight <= self.normal_soft_limit:
            return True

        # Allow one overweight regular request through if it can fit globally.
        # This avoids deadlock when a single large batch exceeds the soft cap.
        return self._active_normal_weight == 0


def _normalize_category(category: str) -> str:
    if category == API_REQUEST_CATEGORY_RECOVERY:
        return API_REQUEST_CATEGORY_RECOVERY
    return API_REQUEST_CATEGORY_NORMAL


def _normalize_release_weight(weight: int | float | None) -> int:
    try:
        normalized = int(math.ceil(float(weight or 1)))
    except (TypeError, ValueError, OverflowError):
        normalized = 1
    return max(1, normalized)


def _build_adaptive_capacity_levels(initial_capacity: int) -> tuple[int, ...]:
    capacity = max(1, int(initial_capacity or 1))
    levels: list[int] = []
    for ratio in (0.8, 0.6, 0.4, 0.2):
        level = max(1, int(math.floor(capacity * ratio)))
        if level >= capacity:
            level = capacity - 1
        if level < 1:
            level = 1
        if not levels or level < levels[-1]:
            levels.append(level)
    if not levels:
        levels.append(1)
    return tuple(levels)
