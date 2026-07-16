"""Shared reservations for upstream model API resources."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Hashable, Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class TaskResourceReservation:
    """Immutable view of one task's reserved upstream resources."""

    owner_key: str
    owner_label: str
    resources: frozenset[Hashable]
    conservative: bool = False


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


class TaskResourceRegistry:
    """Atomically reserve model API groups for background translation tasks.

    An empty or unknown resource set is treated conservatively: it conflicts
    with every other task. Known disjoint API groups may run concurrently.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._reservations: dict[str, TaskResourceReservation] = {}

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

    def _release(self, token: str) -> None:
        with self._lock:
            self._reservations.pop(token, None)

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
