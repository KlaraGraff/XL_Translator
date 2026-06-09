"""Lifecycle guards shared by native translation task pages."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, QTimer
from shiboken6 import isValid


_WORKSPACE_GENERATION_ATTR = "_workspace_generation"


def current_workspace_generation(owner: object) -> int:
    return int(getattr(owner, _WORKSPACE_GENERATION_ATTR, 0) or 0)


def invalidate_workspace_generation(owner: object) -> int:
    generation = current_workspace_generation(owner) + 1
    setattr(owner, _WORKSPACE_GENERATION_ATTR, generation)
    return generation


def begin_workspace_render(owner: object, phase: str) -> int:
    generation = invalidate_workspace_generation(owner)
    setattr(owner, "_workspace_render_phase", phase)
    return generation


def schedule_workspace_callback(
    owner: object,
    delay_ms: int,
    callback: Callable[[], None],
) -> None:
    generation = current_workspace_generation(owner)

    def run_if_current() -> None:
        if isinstance(owner, QObject) and not isValid(owner):
            return
        if current_workspace_generation(owner) != generation:
            return
        callback()

    QTimer.singleShot(delay_ms, run_if_current)
