"""Shared weighted API request scheduling helpers."""

from __future__ import annotations

import math
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator


API_REQUEST_CATEGORY_NORMAL = "normal"
API_REQUEST_CATEGORY_RECOVERY = "recovery"


@dataclass(frozen=True)
class ApiSchedulerSnapshot:
    capacity: int
    active_total_weight: int
    active_normal_weight: int
    active_recovery_weight: int
    waiting_recovery_count: int


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
        ratio = min(max(float(normal_soft_ratio or 0.8), 0.1), 1.0)
        self.normal_soft_limit = max(1, int(math.floor(self.capacity * ratio)))
        self._active_total_weight = 0
        self._active_normal_weight = 0
        self._active_recovery_weight = 0
        self._waiting_recovery_count = 0
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
    ) -> Iterator[None]:
        normalized_weight = self.acquire(weight, category=category)
        try:
            yield
        finally:
            self.release(normalized_weight, category=category)

    def acquire(
        self,
        weight: int | float | None = 1,
        *,
        category: str = API_REQUEST_CATEGORY_NORMAL,
    ) -> int:
        normalized_weight = self.normalize_weight(weight)
        normalized_category = _normalize_category(category)

        with self._condition:
            if normalized_category == API_REQUEST_CATEGORY_RECOVERY:
                self._waiting_recovery_count += 1
            try:
                while not self._can_acquire(normalized_weight, normalized_category):
                    self._condition.wait()
                self._active_total_weight += normalized_weight
                if normalized_category == API_REQUEST_CATEGORY_NORMAL:
                    self._active_normal_weight += normalized_weight
                else:
                    self._active_recovery_weight += normalized_weight
                return normalized_weight
            finally:
                if normalized_category == API_REQUEST_CATEGORY_RECOVERY:
                    self._waiting_recovery_count = max(0, self._waiting_recovery_count - 1)

    def release(
        self,
        weight: int | float | None = 1,
        *,
        category: str = API_REQUEST_CATEGORY_NORMAL,
    ) -> None:
        normalized_weight = self.normalize_weight(weight)
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
                active_total_weight=self._active_total_weight,
                active_normal_weight=self._active_normal_weight,
                active_recovery_weight=self._active_recovery_weight,
                waiting_recovery_count=self._waiting_recovery_count,
            )

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
