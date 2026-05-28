"""Shared widgets for the translation-list UI."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from core.task_queue import (
    TASK_STATUS_CANCELED,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_QUEUED,
    TASK_STATUS_RUNNING,
    TASK_STATUS_STOPPED,
    TRANSLATION_TYPE_EXCEL,
    TRANSLATION_TYPE_PDF,
    TRANSLATION_TYPE_WORD,
    TranslationTask,
)
from native_app.widgets import MiddleElideLabel


def clear_layout(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        child = item.layout()
        if widget is not None:
            widget.setParent(None)
            widget.deleteLater()
        elif child is not None:
            clear_layout(child)


def render_translation_list(
    layout: QVBoxLayout,
    *,
    tasks: list[TranslationTask],
    selected_task_id: str,
    on_select: Callable[[str], None],
    on_move: Callable[[str, int], None],
    on_cancel: Callable[[str], None],
    on_open_output: Callable[[TranslationTask], None],
    on_clear_history: Callable[[], None],
) -> None:
    header = QHBoxLayout()
    header.addWidget(_title("翻译列表"))
    header.addStretch(1)
    clear_history = QPushButton("清空历史")
    clear_history.clicked.connect(on_clear_history)
    header.addWidget(clear_history)
    layout.addLayout(header)

    active = [task for task in tasks if task.status in {TASK_STATUS_RUNNING, TASK_STATUS_QUEUED}]
    position = _selected_active_position(active, selected_task_id)
    count_label = QLabel(f"当前 {position}/{len(active)}" if active else "当前 0/0")
    count_label.setObjectName("MutedText")
    layout.addWidget(count_label)

    _render_group(
        layout,
        "运行中",
        [task for task in tasks if task.status == TASK_STATUS_RUNNING],
        selected_task_id=selected_task_id,
        on_select=on_select,
        on_move=on_move,
        on_cancel=on_cancel,
        on_open_output=on_open_output,
    )
    _render_group(
        layout,
        "排队中",
        [task for task in tasks if task.status == TASK_STATUS_QUEUED],
        selected_task_id=selected_task_id,
        on_select=on_select,
        on_move=on_move,
        on_cancel=on_cancel,
        on_open_output=on_open_output,
    )
    _render_group(
        layout,
        "历史",
        [
            task
            for task in tasks
            if task.status
            in {
                TASK_STATUS_COMPLETED,
                TASK_STATUS_FAILED,
                TASK_STATUS_STOPPED,
                TASK_STATUS_CANCELED,
            }
        ],
        selected_task_id=selected_task_id,
        on_select=on_select,
        on_move=on_move,
        on_cancel=on_cancel,
        on_open_output=on_open_output,
    )
    layout.addStretch(1)


def render_selected_task_snapshot(
    layout: QVBoxLayout,
    *,
    task: TranslationTask | None,
    on_stop: Callable[[str], None],
    on_open_output: Callable[[TranslationTask], None],
) -> None:
    if task is None:
        layout.addWidget(_title("所选任务"))
        empty = QLabel("请选择一个翻译任务。")
        empty.setWordWrap(True)
        empty.setObjectName("MutedText")
        layout.addWidget(empty)
        return

    title_row = QHBoxLayout()
    title_row.addWidget(_title("所选任务"))
    title_row.addStretch(1)
    title_row.addWidget(_status_badge(task.status, task.block_reason))
    layout.addLayout(title_row)

    layout.addWidget(_body(task.snapshot.title, bold=True))
    layout.addWidget(_muted(_task_summary(task)))
    if task.status == TASK_STATUS_RUNNING:
        progress = QProgressBar()
        progress.setRange(0, 100)
        progress.setValue(_progress_percent(task.progress_label))
        layout.addWidget(progress)

    action_row = QHBoxLayout()
    if task.status == TASK_STATUS_RUNNING:
        stop = QPushButton("终止翻译")
        stop.setObjectName("DangerButton")
        stop.clicked.connect(lambda: on_stop(task.task_id))
        action_row.addWidget(stop)
    if task.output_path or task.snapshot.output_path:
        open_output = QPushButton("打开当前输出" if task.status == TASK_STATUS_RUNNING else "打开输出")
        open_output.clicked.connect(lambda: on_open_output(task))
        action_row.addWidget(open_output)
    if action_row.count():
        layout.addLayout(action_row)

    layout.addSpacing(8)
    layout.addWidget(_title("输出位置快照"))
    layout.addWidget(_muted("输出目录"))
    layout.addWidget(_path_body(_display_path(task.output_path or task.snapshot.output_path)))
    layout.addWidget(_muted("输出策略"))
    layout.addWidget(_body(task.snapshot.output_policy or "按任务安排时设置"))

    layout.addSpacing(8)
    layout.addWidget(_title("任务参数快照"))
    _add_field(layout, "目标语言", task.snapshot.target_language)
    if task.snapshot.source_language:
        _add_field(layout, "源语言", task.snapshot.source_language)
    for label, value in task.snapshot.params:
        _add_field(layout, label, value)


def _render_group(
    layout: QVBoxLayout,
    title: str,
    tasks: list[TranslationTask],
    *,
    selected_task_id: str,
    on_select: Callable[[str], None],
    on_move: Callable[[str, int], None],
    on_cancel: Callable[[str], None],
    on_open_output: Callable[[TranslationTask], None],
) -> None:
    if not tasks:
        return
    layout.addSpacing(8)
    layout.addWidget(_title(title))
    for task in tasks:
        layout.addWidget(
            _task_card(
                task,
                selected=task.task_id == selected_task_id,
                on_select=on_select,
                on_move=on_move,
                on_cancel=on_cancel,
                on_open_output=on_open_output,
            )
        )


def _task_card(
    task: TranslationTask,
    *,
    selected: bool,
    on_select: Callable[[str], None],
    on_move: Callable[[str, int], None],
    on_cancel: Callable[[str], None],
    on_open_output: Callable[[TranslationTask], None],
) -> QFrame:
    frame = QFrame()
    frame.setObjectName("QueueTaskCard")
    frame.setProperty("selected", selected)
    frame.setProperty("blocked", bool(task.block_reason))
    frame.setProperty("running", task.status == TASK_STATUS_RUNNING)
    layout = QHBoxLayout(frame)
    layout.setContentsMargins(12, 10, 12, 10)
    layout.setSpacing(10)

    select = QPushButton("选中" if selected else "查看")
    select.clicked.connect(lambda: on_select(task.task_id))
    layout.addWidget(select)

    body = QVBoxLayout()
    body.setSpacing(4)
    body.addWidget(_body(task.snapshot.title, bold=True))
    body.addWidget(_muted(_task_summary(task)))
    if task.status == TASK_STATUS_RUNNING and task.progress_label:
        body.addWidget(_muted(task.progress_label))
    elif task.block_reason:
        body.addWidget(_muted(task.block_reason))
    elif task.error_message:
        body.addWidget(_muted(task.error_message))
    layout.addLayout(body, 1)

    actions = QHBoxLayout()
    actions.setSpacing(6)
    if task.status == TASK_STATUS_QUEUED:
        up = QPushButton("↑")
        up.clicked.connect(lambda: on_move(task.task_id, -1))
        actions.addWidget(up)
        down = QPushButton("↓")
        down.clicked.connect(lambda: on_move(task.task_id, 1))
        actions.addWidget(down)
        cancel = QPushButton("取消")
        cancel.clicked.connect(lambda: on_cancel(task.task_id))
        actions.addWidget(cancel)
    elif task.status in {TASK_STATUS_COMPLETED, TASK_STATUS_FAILED, TASK_STATUS_STOPPED}:
        if task.output_path or task.snapshot.output_path:
            open_output = QPushButton("打开输出")
            open_output.clicked.connect(lambda: on_open_output(task))
            actions.addWidget(open_output)
    layout.addLayout(actions)
    return frame


def _selected_active_position(tasks: list[TranslationTask], selected_task_id: str) -> int:
    for index, task in enumerate(tasks, start=1):
        if task.task_id == selected_task_id:
            return index
    return 1 if tasks else 0


def _task_summary(task: TranslationTask) -> str:
    parts = [_translation_type_label(task.translation_type), _status_text(task)]
    if task.snapshot.target_language:
        parts.append(task.snapshot.target_language)
    parts.append(task.arranged_at.strftime("%H:%M"))
    return " · ".join(part for part in parts if part)


def _status_text(task: TranslationTask) -> str:
    if task.block_reason:
        return "阻塞"
    return {
        TASK_STATUS_QUEUED: "等待执行",
        TASK_STATUS_RUNNING: "正在翻译",
        TASK_STATUS_COMPLETED: "已完成",
        TASK_STATUS_FAILED: "失败",
        TASK_STATUS_STOPPED: "已中止",
        TASK_STATUS_CANCELED: "已取消",
    }.get(task.status, task.status)


def _translation_type_label(translation_type: str) -> str:
    return {
        TRANSLATION_TYPE_EXCEL: "Excel",
        TRANSLATION_TYPE_WORD: "Word",
        TRANSLATION_TYPE_PDF: "PDF",
    }.get(translation_type, translation_type)


def _progress_percent(progress_label: str) -> int:
    text = str(progress_label or "")
    if "/" not in text:
        return 0
    left, right = text.split("/", 1)
    try:
        done = int("".join(ch for ch in left if ch.isdigit()))
        total = int("".join(ch for ch in right if ch.isdigit()))
    except ValueError:
        return 0
    if total <= 0:
        return 0
    return max(0, min(100, int(done * 100 / total)))


def _status_badge(status: str, block_reason: str) -> QLabel:
    label = QLabel("阻塞" if block_reason else _status_text(_StatusPlaceholder(status)))
    label.setObjectName("ResultWarning" if block_reason else "ResultSuccess")
    return label


class _StatusPlaceholder:
    def __init__(self, status: str) -> None:
        self.status = status
        self.block_reason = ""


def _display_path(path: str) -> str:
    text = str(path or "").strip()
    if not text:
        return "任务启动后生成"
    try:
        return str(Path(text))
    except Exception:
        return text


def _add_field(layout: QVBoxLayout, label: str, value: str) -> None:
    layout.addWidget(_muted(label))
    value_label = _body(str(value or ""))
    value_label.setObjectName("ReadonlyField")
    layout.addWidget(value_label)


def _title(text_value: str) -> QLabel:
    label = QLabel(text_value)
    label.setObjectName("SectionTitle")
    return label


def _body(text_value: str, *, bold: bool = False) -> QLabel:
    label = QLabel(text_value)
    label.setWordWrap(True)
    if bold:
        label.setObjectName("SectionTitle")
    return label


def _path_body(text_value: str) -> MiddleElideLabel:
    label = MiddleElideLabel(text_value)
    label.setObjectName("ReadonlyField")
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


def _muted(text_value: str) -> QLabel:
    label = QLabel(text_value)
    label.setWordWrap(True)
    label.setObjectName("MutedText")
    return label
