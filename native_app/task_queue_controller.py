"""Qt-facing translation queue controller."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, Signal

from core.task_queue import (
    TASK_STATUS_FAILED,
    TranslationTask,
    TranslationTaskQueue,
)


class NativeTranslationQueueController(QObject):
    """Bridge the core translation queue to page-owned runners."""

    changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.queue = TranslationTaskQueue()
        self._starters: dict[str, Callable[[TranslationTask], None]] = {}
        self._stoppers: dict[str, Callable[[], None]] = {}

    def arrange(
        self,
        task: TranslationTask,
        *,
        starter: Callable[[TranslationTask], None],
    ) -> TranslationTask:
        arranged = self.queue.arrange(task)
        self._starters[arranged.task_id] = starter
        self.changed.emit()
        self.evaluate()
        return arranged

    def evaluate(self) -> None:
        changed = False
        while True:
            task = self.queue.next_startable()
            if task is None:
                break
            starter = self._starters.get(task.task_id)
            if starter is None:
                self.queue.finish(
                    task.task_id,
                    TASK_STATUS_FAILED,
                    message="任务启动器不可用",
                    error_message="任务启动器不可用",
                )
                changed = True
                continue
            try:
                starter(task)
            except Exception as exc:  # noqa: BLE001 - converted to task failure.
                self.queue.finish(
                    task.task_id,
                    TASK_STATUS_FAILED,
                    message=str(exc),
                    error_message=str(exc),
                )
            changed = True
        if changed:
            self.changed.emit()

    def register_stopper(self, task_id: str, stopper: Callable[[], None]) -> None:
        self._stoppers[task_id] = stopper

    def unregister_runtime(self, task_id: str) -> None:
        self._stoppers.pop(task_id, None)

    def request_stop(self, task_id: str) -> bool:
        stopper = self._stoppers.get(task_id)
        if stopper is None:
            return False
        stopper()
        return True

    def cancel(self, task_id: str) -> None:
        self.queue.cancel(task_id)
        self.changed.emit()
        self.evaluate()

    def move(self, task_id: str, direction: int) -> None:
        if self.queue.move_queued(task_id, direction):
            self.changed.emit()
            self.evaluate()

    def clear_history(self) -> None:
        self.queue.clear_history()
        self.changed.emit()

    def finish_task(
        self,
        task_id: str,
        status: str,
        *,
        message: str = "",
        output_path: str = "",
        error_message: str = "",
        block_api_groups: bool = False,
    ) -> None:
        if block_api_groups:
            task = self.queue.task(task_id)
            if task is not None:
                self.queue.block_groups(
                    [requirement.key for requirement in task.group_requirements],
                    error_message or message or "API 配置不可用",
                )
        self.unregister_runtime(task_id)
        self.queue.finish(
            task_id,
            status,
            message=message,
            output_path=output_path,
            error_message=error_message,
        )
        self.changed.emit()
        self.evaluate()

    def update_progress(
        self,
        task_id: str,
        *,
        progress_label: str = "",
        status_message: str = "",
        output_path: str = "",
    ) -> None:
        self.queue.update_progress(
            task_id,
            progress_label=progress_label,
            status_message=status_message,
            output_path=output_path,
        )
        self.changed.emit()
