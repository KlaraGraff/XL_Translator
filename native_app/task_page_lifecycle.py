"""Lifecycle guards shared by native translation task pages."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import Thread

from PySide6.QtCore import QObject, QTimer
from shiboken6 import isValid


_WORKSPACE_GENERATION_ATTR = "_workspace_generation"
RUNNER_MESSAGE_DRAIN_LIMIT = 500


@dataclass(frozen=True)
class SilentRunnerExit:
    """Terminal state used when a runner exits without a terminal message."""

    phase: str
    message: str


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


def request_background_stop(
    task: object | None,
    *method_names: str,
) -> bool:
    """Request cancellation without assuming a specific worker implementation."""

    if task is None:
        return False
    names = method_names or ("stop", "cancel", "requestInterruption", "quit")
    for name in names:
        method = getattr(task, name, None)
        if not callable(method):
            continue
        try:
            method()
        except Exception:  # noqa: BLE001 - shutdown must remain best effort.
            return False
        return True
    return False


def wait_for_background_task(task: object | None, timeout_ms: int = 250) -> bool:
    """Wait at most ``timeout_ms`` for a Qt worker or Python runner to finish."""

    if task is None:
        return True
    timeout_ms = max(0, int(timeout_ms))
    wait = getattr(task, "wait", None)
    if callable(wait):
        try:
            result = wait(timeout_ms)
            if isinstance(result, bool):
                return result
        except (RuntimeError, TypeError):
            pass

    thread = getattr(task, "_thread", None)
    if isinstance(thread, Thread):
        try:
            thread.join(timeout_ms / 1000)
            return not thread.is_alive()
        except RuntimeError:
            return False

    is_running = getattr(task, "is_running", None)
    if not callable(is_running):
        is_running = getattr(task, "isRunning", None)
    if callable(is_running):
        try:
            return not bool(is_running())
        except (RuntimeError, TypeError):
            return False
    return True


def detach_running_qobject(task: object | None) -> None:
    """Remove a still-running QObject from a page that is about to be destroyed."""

    if not isinstance(task, QObject) or not isValid(task):
        return
    try:
        task.setParent(None)
    except RuntimeError:
        pass


def silent_runner_exit(
    runner: object | None,
    poll_error: Exception | None = None,
) -> SilentRunnerExit | None:
    """Detect a runner that ended (or broke) without Done/Error/Stopped output."""

    if runner is None:
        return None
    if poll_error is not None:
        return SilentRunnerExit("error", f"读取后台任务消息失败：{poll_error}")
    needs_poll = getattr(runner, "needs_poll", None)
    if not callable(needs_poll):
        return SilentRunnerExit("error", "后台任务接口异常，无法确认运行状态。")
    try:
        if bool(needs_poll()):
            return None
    except Exception as exc:  # noqa: BLE001 - convert broken runners to UI state.
        return SilentRunnerExit("error", f"后台任务状态检查失败：{exc}")

    stop_requested = getattr(runner, "stop_requested", None)
    try:
        stopped = bool(stop_requested()) if callable(stop_requested) else False
    except Exception:  # noqa: BLE001 - an unreadable flag is not a clean stop.
        stopped = False
    if stopped:
        return SilentRunnerExit("stopped", "任务已中止，后台任务未返回结束详情。")
    return SilentRunnerExit("error", "后台任务意外结束，未返回完成结果。请检查诊断记录后重试。")
