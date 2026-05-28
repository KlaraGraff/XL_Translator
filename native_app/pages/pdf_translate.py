"""Native PDF image-layout translation page."""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QTimer, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app_meta import APP_NAME
from config import (
    PDF_PAGE_CONCURRENCY_SAFETY_CAP,
    PDF_PAGE_RETRY_ATTEMPTS_MAX,
    PDF_PAGE_RETRY_ATTEMPTS_MIN,
)
from core.bilingual_writer import (
    custom_output_dir_will_be_created,
    get_custom_output_dir_error,
    resolve_custom_output_dir,
)
from core.diagnostics import (
    archive_task_diagnostics,
    build_diagnostics_history_zip_bytes,
    count_diagnostic_records,
)
from core.image_generation import check_image_generation_connectivity
from core.language_registry import (
    get_ordered_target_lang_codes,
    get_target_lang_display,
    is_supported_target_lang,
    remember_recent_target_lang,
)
from core.model_roles import (
    ROLE_IMAGE,
    ROLE_PDF_REVIEW,
    image_model_signature,
    pdf_review_model_signature,
    provider_supports_capability,
    resolve_effective_model_config,
)
from core.pdf_image_translation import (
    PDF_MANIFEST_FILENAME,
    PDF_PAGES_ROOT,
    PDF_REPORT_FILENAME,
    PDF_OUTPUT_STATE_FAILED,
    PDF_OUTPUT_STATE_STOPPED,
    SOURCE_TYPE_IMAGE,
    SUPPORTED_IMAGE_SUFFIXES,
    TRANSLATED_PAGES_DIRNAME,
    PdfFileItem,
    PdfImageTranslationRunner,
    is_supported_pdf_or_image_file,
)
from core.pdf_review import check_pdf_review_connectivity
from core.task_runner import (
    DoneMsg,
    ErrorMsg,
    LogMsg,
    PdfPageRecoveryStatusMsg,
    PdfReviewStatusMsg,
    ProgressMsg,
    StatusMsg,
    StoppedMsg,
)
from core.task_queue import (
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_STOPPED,
    TRANSLATION_TYPE_PDF,
    TranslationTask,
    TranslationTaskSnapshot,
    api_requirement_from_config,
    is_api_group_blocking_error,
)
from native_app.result_view import ResultIssueRow, format_elapsed, render_translation_result
from native_app.task_queue_view import (
    clear_layout as clear_queue_layout,
    render_selected_task_snapshot,
    render_translation_list,
)
from native_app.widgets import (
    MiddleElideLabel,
    MiddleElideLineEdit,
    build_app_tooltip_html,
    configure_app_table,
    configure_file_result_table,
    configure_file_selection_table,
    create_check_table_item,
    create_elide_table_item,
    create_searchable_combo,
    create_table_item,
    is_live_widget,
    refresh_combo_completer,
    select_combo_text_match,
)
from native_app.workers import PdfScanWorker
from settings import AppSettings, save_settings


HEADER_TILE_HEIGHT = 48
HEADER_TILE_MIN_WIDTH = 86
HEADER_SOURCE_MIN_WIDTH = 300
HEADER_SOURCE_MAX_WIDTH = 430
KPI_TILE_HEIGHT = 76
VISIBLE_LOG_ENTRY_LIMIT = 300
VISIBLE_LOG_CHAR_LIMIT = 140_000
DIAGNOSTIC_LOG_ENTRY_LIMIT = 5000
DIAGNOSTIC_LOG_CHAR_LIMIT = 2_000_000
PDF_STOP_SUBMISSION_BUTTON_TEXT = "停止提交新页"
PDF_RESUME_TRANSLATION_BUTTON_TEXT = "继续翻译"
IMAGE_FILE_FILTER = "图片文件 (" + " ".join(f"*{suffix}" for suffix in sorted(SUPPORTED_IMAGE_SUFFIXES)) + ")"


def _clear_layout(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        child_layout = item.layout()
        if widget is not None:
            widget.setParent(None)
            widget.deleteLater()
        elif child_layout is not None:
            _clear_layout(child_layout)


def _label(text: str, object_name: str | None = None) -> QLabel:
    label = QLabel(text)
    if object_name:
        label.setObjectName(object_name)
    return label


def _tooltip(title: str, summary: str, items: list[str] | None = None) -> str:
    return build_app_tooltip_html(title, summary, items)


def _set_tooltip(
    widget: QWidget,
    title: str,
    summary: str,
    items: list[str] | None = None,
) -> None:
    widget.setToolTip(_tooltip(title, summary, items))
    widget.setToolTipDuration(4200)


def _field_label(
    text: str,
    title: str,
    summary: str,
    items: list[str] | None = None,
) -> QLabel:
    label = QLabel(text)
    _set_tooltip(label, title, summary, items)
    return label


def _card() -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("Card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(16, 16, 16, 16)
    layout.setSpacing(12)
    return frame, layout


class PdfTranslatePage(QWidget):
    """Qt implementation of the PDF image-layout translation workspace."""

    def __init__(self, settings: AppSettings):
        super().__init__()
        self.settings = settings
        self.phase = "idle"
        self.files: list[PdfFileItem] = []
        self.source_root = settings.last_pdf_source_folder
        self.runner: PdfImageTranslationRunner | None = None
        self.scan_worker: PdfScanWorker | None = None
        self.log_entries: list[dict[str, str]] = []
        self.diagnostic_log_entries: list[dict[str, str]] = []
        self._visible_log_chars = 0
        self._diagnostic_log_chars = 0
        self.progress: ProgressMsg | None = None
        self.page_recovery_status: PdfPageRecoveryStatusMsg | None = None
        self.review_status: PdfReviewStatusMsg | None = None
        self.status_text = ""
        self.done: DoneMsg | None = None
        self.stop_message = ""
        self.task_files: list[PdfFileItem] = []
        self.current_task_source_root = ""
        self.current_task_id = ""
        self.current_queue_task_id = ""
        self.queue_controller = None
        self.translation_list_open = False
        self.selected_queue_task_id = ""
        self.preparing_next_task = False
        self.deferred_terminal_phase = ""
        self.deferred_done: DoneMsg | None = None
        self.deferred_stop_message = ""
        self._task_diagnostics_archived = False
        self._terminal_output_dir = ""
        self._terminal_report_path = ""
        self._terminal_manifest_path = ""
        self._workspace_render_phase = self.phase

        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(800)
        self.poll_timer.timeout.connect(self._poll_runner)
        self.ui_sync_timer = QTimer(self)
        self.ui_sync_timer.setInterval(500)
        self.ui_sync_timer.timeout.connect(self._sync_action_card_with_workspace)

        self._build_ui()
        self._render_workspace()
        self._render_action_card()

    def refresh_settings(self) -> None:
        self._refresh_header()
        self._render_action_card()

    def set_queue_controller(self, controller) -> None:
        self.queue_controller = controller
        controller.changed.connect(self._on_queue_changed)

    def selected_queue_task(self):
        if not self.translation_list_open or self.queue_controller is None:
            return None
        return self.queue_controller.queue.task(self.selected_queue_task_id)

    def _on_queue_changed(self) -> None:
        had_open_list = self.translation_list_open
        has_tasks = (
            self.queue_controller is not None
            and bool(self.queue_controller.queue.tasks())
        )
        if not has_tasks:
            self.translation_list_open = False
            self.selected_queue_task_id = ""
        if self.translation_list_open or had_open_list:
            self._ensure_selected_queue_task()
            self._render_workspace()
        else:
            self._render_action_card()
        self._sync_window_sidebar_task_snapshot()

    def _ensure_selected_queue_task(self) -> None:
        if self.queue_controller is None:
            self.selected_queue_task_id = ""
            return
        tasks = self.queue_controller.queue.tasks()
        if any(task.task_id == self.selected_queue_task_id for task in tasks):
            return
        self.selected_queue_task_id = tasks[0].task_id if tasks else ""

    def _sync_window_sidebar_task_snapshot(self) -> None:
        window = self.window()
        if hasattr(window, "_sync_sidebar_task_snapshot"):
            window._sync_sidebar_task_snapshot()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt API name.
        super().showEvent(event)
        self.set_page_active(True)

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt API name.
        self.set_page_active(False)
        super().hideEvent(event)

    def set_page_active(self, active: bool) -> None:
        if active:
            if self.runner is not None or self._is_preparing_next_task():
                self._start_ui_sync_guard()
            self._refresh_header()
            self._render_action_card()
            self._sync_window_sidebar_task_snapshot()
        else:
            self._stop_ui_sync_guard()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(26, 22, 26, 22)
        root.setSpacing(16)
        self.header_layout = QVBoxLayout()
        self.header_layout.setSpacing(8)
        root.addLayout(self.header_layout)
        self._build_command_bar(root)

        body = QHBoxLayout()
        body.setSpacing(16)
        root.addLayout(body, 1)

        self.workspace_scroll = QScrollArea()
        self.workspace_scroll.setWidgetResizable(True)
        self.workspace_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        self.workspace_layout = QVBoxLayout(content)
        self.workspace_layout.setContentsMargins(0, 0, 0, 0)
        self.workspace_layout.setSpacing(0)
        self.workspace_scroll.setWidget(content)
        body.addWidget(self.workspace_scroll, 1)

        side = QScrollArea()
        side.setWidgetResizable(True)
        side.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        side.setFixedWidth(360)
        side_content = QWidget()
        side_layout = QVBoxLayout(side_content)
        side_layout.setContentsMargins(0, 0, 0, 0)
        side_layout.setSpacing(12)
        self.side_layout = side_layout
        self.action_card, self.action_layout = _card()
        side_layout.addWidget(self.action_card)
        self.queue_snapshot_card, self.queue_snapshot_layout = _card()
        self.queue_snapshot_card.hide()
        side_layout.addWidget(self.queue_snapshot_card)
        self._build_output_card(side_layout)
        self._build_params_card(side_layout)
        side_layout.addStretch(1)
        side.setWidget(side_content)
        body.addWidget(side)

    def _refresh_header(self) -> None:
        _clear_layout(self.header_layout)
        self.header_layout.addWidget(_label("PDF / Image Workspace", "PageEyebrow"))
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        title_row.addWidget(_label("PDF/图片翻译", "PageTitle"))
        title_row.addStretch(1)
        title_row.addWidget(self._pill("目标语言", self._selected_target_label()), alignment=Qt.AlignmentFlag.AlignTop)
        title_row.addWidget(self._pill("已选文件", f"{len(self._selected_files())} 个"), alignment=Qt.AlignmentFlag.AlignTop)
        title_row.addWidget(self._pill("源路径", self.source_root or "尚未选择", min_width=HEADER_SOURCE_MIN_WIDTH, max_width=HEADER_SOURCE_MAX_WIDTH), alignment=Qt.AlignmentFlag.AlignTop)
        title_row.addWidget(self._phase_badge(), alignment=Qt.AlignmentFlag.AlignTop)
        self.header_layout.addLayout(title_row)

    def _build_command_bar(self, root: QVBoxLayout) -> None:
        frame = QFrame()
        frame.setObjectName("CommandBar")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)
        layout.addWidget(_label("源路径", "SectionTitle"))
        row = QHBoxLayout()
        row.setSpacing(10)
        self.source_input = MiddleElideLineEdit(self.settings.last_pdf_source_folder)
        self._refresh_source_placeholder()
        row.addWidget(self.source_input, 1)
        self.browse_button = QPushButton("浏览")
        self.browse_button.clicked.connect(self._browse_source)
        row.addWidget(self.browse_button)
        self.scan_button = QPushButton("扫描")
        self.scan_button.clicked.connect(self._scan_source)
        row.addWidget(self.scan_button)
        layout.addLayout(row)
        root.addWidget(frame)

    def _refresh_source_placeholder(self) -> None:
        if not hasattr(self, "source_input"):
            return
        if self.settings.pdf.image_translation_enabled:
            self.source_input.setPlaceholderText("输入文件夹、PDF 或图片文件路径")
        else:
            self.source_input.setPlaceholderText("输入文件夹或 PDF 文件路径")

    def _build_output_card(self, side_layout: QVBoxLayout) -> None:
        frame, layout = _card()
        self.output_card = frame
        side_layout.addWidget(frame)
        layout.addWidget(_label("输出位置", "SectionTitle"))
        self.output_default_radio = QRadioButton("源目录内")
        self.output_custom_radio = QRadioButton("自定义目录")
        self.output_custom_radio.setChecked(self.settings.output.use_custom_output_dir)
        self.output_default_radio.setChecked(not self.settings.output.use_custom_output_dir)
        self.output_default_radio.toggled.connect(self._on_output_changed)
        self.output_custom_radio.toggled.connect(self._on_output_changed)
        layout.addWidget(self.output_default_radio)
        layout.addWidget(self.output_custom_radio)
        self.custom_output_input = MiddleElideLineEdit(self.settings.output.custom_output_dir)
        self.custom_output_input.setPlaceholderText("输入输出目录")
        self.custom_output_input.editingFinished.connect(self._on_output_changed)
        layout.addWidget(self.custom_output_input)
        self.output_status = QLabel("")
        self.output_status.setWordWrap(True)
        self.output_status.setObjectName("FieldHint")
        layout.addWidget(self.output_status)
        self._refresh_output_controls()

    def _build_params_card(self, side_layout: QVBoxLayout) -> None:
        frame, layout = _card()
        self.params_card = frame
        side_layout.addWidget(frame)
        layout.addWidget(_label("任务参数", "SectionTitle"))
        layout.addWidget(_field_label("目标语言", "目标语言", "选择译文语言。"))
        self.target_combo = create_searchable_combo()
        if self.target_combo.lineEdit() is not None:
            self.target_combo.lineEdit().setPlaceholderText("筛选语言")
        self._load_target_options()
        self.target_combo.currentIndexChanged.connect(self._on_target_changed)
        layout.addWidget(self.target_combo)
        layout.addWidget(
            _field_label(
                "页级重试次数",
                "页级重试次数",
                "设置单页失败后的重试次数。",
            )
        )
        self.retry_spin = QSpinBox()
        self.retry_spin.setRange(PDF_PAGE_RETRY_ATTEMPTS_MIN, PDF_PAGE_RETRY_ATTEMPTS_MAX)
        self.retry_spin.setValue(self.settings.pdf.page_retry_attempts)
        self.retry_spin.valueChanged.connect(self._on_params_changed)
        layout.addWidget(self.retry_spin)
        layout.addWidget(_field_label("PDF 页生成并发数", "PDF 页生成并发数", "留空时跟随云端并发数。"))
        self.pdf_concurrency_input = QLineEdit(
            "" if self.settings.pdf.page_generation_concurrency is None else str(self.settings.pdf.page_generation_concurrency)
        )
        self.pdf_concurrency_input.setPlaceholderText("留空")
        self.pdf_concurrency_input.editingFinished.connect(self._on_params_changed)
        layout.addWidget(self.pdf_concurrency_input)
        self.image_translation_checkbox = QCheckBox("启用图片翻译")
        self.image_translation_checkbox.setChecked(self.settings.pdf.image_translation_enabled)
        self.image_translation_checkbox.toggled.connect(self._on_image_translation_enabled_changed)
        _set_tooltip(
            self.image_translation_checkbox,
            "启用图片翻译",
            "允许扫描和翻译图片文件。",
            [
                "支持 PNG、JPG、JPEG、WebP、BMP、TIFF。",
                "每张图片按单页任务处理。",
            ],
        )
        layout.addWidget(self.image_translation_checkbox)
        self.pdf_compression_checkbox = QCheckBox("同时生成压缩 PDF（推荐）")
        self.pdf_compression_checkbox.setChecked(self.settings.pdf.generate_compressed_pdf)
        self.pdf_compression_checkbox.toggled.connect(self._on_params_changed)
        _set_tooltip(
            self.pdf_compression_checkbox,
            "同时生成压缩 PDF",
            "同时输出高清版和压缩版 PDF。",
            [
                "关闭后仅输出高清版。",
                "页面素材仍保留原质量。",
            ],
        )
        layout.addWidget(self.pdf_compression_checkbox)
        self.pdf_review_checkbox = QCheckBox("启用翻译审核")
        self.pdf_review_checkbox.setChecked(self.settings.pdf.review_enabled)
        self.pdf_review_checkbox.toggled.connect(self._on_review_enabled_changed)
        _set_tooltip(
            self.pdf_review_checkbox,
            "启用翻译审核",
            "使用审核模型检查候选译图。",
            [
                "未启用时无需配置审核模型。",
                "启用后会增加请求和耗时。",
                "审核模型在左侧模型配置中设置。",
            ],
        )
        layout.addWidget(self.pdf_review_checkbox)
        fixed = QLabel("PDF 按 300 DPI 渲染；源页面保存为 PNG，图片译图按模型返回格式保存。")
        fixed.setObjectName("FieldHint")
        fixed.setWordWrap(True)
        layout.addWidget(fixed)

    def _load_target_options(self) -> None:
        target_codes = get_ordered_target_lang_codes(
            self.settings.recent_target_langs,
            self.settings.custom_target_langs,
            include_optional=True,
        )
        self.target_combo.clear()
        for code in target_codes:
            self.target_combo.addItem(
                get_target_lang_display(code, self.settings.custom_target_langs, include_optional=True),
                code,
            )
        index = self.target_combo.findData(self.settings.target_lang)
        self.target_combo.setCurrentIndex(index if index >= 0 else 0)
        refresh_combo_completer(self.target_combo)

    def _pill(
        self,
        label: str,
        value: str,
        *,
        min_width: int = HEADER_TILE_MIN_WIDTH,
        max_width: int | None = None,
    ) -> QFrame:
        frame = QFrame()
        frame.setObjectName("Pill")
        frame.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        frame.setFixedHeight(HEADER_TILE_HEIGHT)
        frame.setMinimumWidth(min_width)
        if max_width is not None:
            frame.setMaximumWidth(max_width)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(8, 3, 8, 3)
        layout.setSpacing(0)
        layout.addWidget(_label(label, "PillLabel"))
        value_widget = MiddleElideLabel(value) if max_width is not None else QLabel(value)
        value_widget.setObjectName("PillValue")
        value_widget.setWordWrap(False)
        if max_width is not None:
            value_widget.setMaximumWidth(max(1, max_width - 16))
        value_widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(value_widget)
        return frame

    def _phase_badge(self) -> QFrame:
        text = {
            "idle": "待执行",
            "running": "执行中",
            "done": "已完成",
            "error": "异常",
            "stopped": "已中止",
        }.get(self.phase, "待执行")
        frame = QFrame()
        frame.setObjectName("PhaseBadge")
        frame.setFixedHeight(HEADER_TILE_HEIGHT)
        frame.setMinimumWidth(HEADER_TILE_MIN_WIDTH)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(8, 3, 8, 3)
        label = QLabel(text)
        label.setObjectName("PhaseBadgeText")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(label)
        return frame

    def _render_workspace(self) -> None:
        self._normalize_terminal_state()
        self._workspace_render_phase = self.phase
        _clear_layout(self.workspace_layout)
        frame = QFrame()
        frame.setObjectName("Workspace")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(14)
        self.workspace_frame = frame
        self.workspace_layout.addWidget(frame, 1 if self.phase == "idle" and self.files else 0)
        if self.translation_list_open and self.queue_controller is not None:
            self._render_translation_list_workspace(layout)
        elif self.phase == "idle":
            self._render_idle_workspace(layout)
        elif self.phase == "running":
            self._render_running_workspace(layout)
        elif self.phase == "done":
            self._render_done_workspace(layout)
        elif self.phase == "error":
            self._render_error_workspace(layout)
        elif self.phase == "stopped":
            self._render_stopped_workspace(layout)
        self.workspace_layout.addStretch(1)
        self._refresh_header()
        self._render_action_card()

    def _render_translation_list_workspace(self, layout: QVBoxLayout) -> None:
        self._ensure_selected_queue_task()
        tasks = self.queue_controller.queue.tasks() if self.queue_controller is not None else []
        render_translation_list(
            layout,
            tasks=tasks,
            selected_task_id=self.selected_queue_task_id,
            on_select=self._select_queue_task,
            on_move=self._move_queue_task,
            on_cancel=self._cancel_queue_task,
            on_open_output=self._open_queue_output,
            on_clear_history=self._clear_queue_history,
        )

    def _render_idle_workspace(self, layout: QVBoxLayout) -> None:
        if not self.files:
            layout.addWidget(_label("任务清单", "SectionTitle"))
            if self.settings.pdf.image_translation_enabled:
                placeholder_text = "输入或选择文件夹、PDF 或图片文件后扫描，生成任务清单。"
            else:
                placeholder_text = "输入或选择文件夹、PDF 文件后扫描，生成任务清单。"
            placeholder = QLabel(placeholder_text)
            placeholder.setWordWrap(True)
            placeholder.setObjectName("MutedText")
            layout.addWidget(placeholder)
            return
        title_row = QHBoxLayout()
        title_row.addWidget(_label("任务清单", "SectionTitle"))
        self.selection_status_label = QLabel("")
        self.selection_status_label.setObjectName("FieldHint")
        title_row.addWidget(self.selection_status_label)
        title_row.addStretch(1)
        select_all = QPushButton("全选")
        select_all.clicked.connect(lambda: self._set_all_file_selection(True))
        title_row.addWidget(select_all)
        deselect_all = QPushButton("全不选")
        deselect_all.clicked.connect(lambda: self._set_all_file_selection(False))
        title_row.addWidget(deselect_all)
        layout.addLayout(title_row)
        layout.addLayout(
            self._build_result_kpis(
                [
                    ("已扫描文件", str(len(self.files))),
                    ("已选任务", str(len(self._selected_files()))),
                    ("总页数", str(sum(item.page_count for item in self.files))),
                    ("图片文件", str(self._image_file_count(self.files))),
                ]
            )
        )
        self.table = QTableWidget(len(self.files), 5)
        self.table.setHorizontalHeaderLabels(["选择", "类型", "文件名", "大小", "页数"])
        configure_app_table(self.table, row_height=38)
        self.table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        configure_file_selection_table(self.table, fixed_column_widths={1: 78, 3: 112, 4: 86})
        for row, item in enumerate(self.files):
            self.table.setItem(row, 0, create_check_table_item())
            self.table.setItem(row, 1, create_table_item(self._file_type_label(item)))
            self.table.setItem(row, 2, create_elide_table_item(item.path.name))
            self.table.setItem(row, 3, create_table_item(f"{item.size_kb:.1f} KB"))
            self.table.setItem(row, 4, create_table_item(item.page_count))
        self.table.itemChanged.connect(self._on_file_selection_changed)
        self._refresh_selection_summary()
        layout.addWidget(self.table, 1)
        self._schedule_file_table_height_refresh()

    def _render_running_workspace(self, layout: QVBoxLayout) -> None:
        layout.addWidget(_label("执行监控", "SectionTitle"))
        self.running_status = QLabel("正在初始化...")
        self.running_status.setObjectName("MutedText")
        layout.addWidget(self.running_status)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        layout.addWidget(self.progress_bar)
        self.page_recovery_summary = self._build_page_recovery_summary()
        layout.addWidget(self.page_recovery_summary)
        self.review_summary = self._build_pdf_review_summary()
        layout.addWidget(self.review_summary)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(360)
        layout.addWidget(self.log_view, 1)
        self._refresh_running_widgets()

    def _build_recovery_card(
        self,
        title: str,
        metric_labels: tuple[str, str, str],
    ) -> tuple[QFrame, QLabel, tuple[QLabel, QLabel, QLabel]]:
        card = QFrame()
        card.setObjectName("RecoveryCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 9, 12, 10)
        layout.setSpacing(7)
        header = QHBoxLayout()
        header.setSpacing(8)
        title_label = _label(title, "RecoveryTitle")
        badge = _label("未启用", "RecoveryBadge")
        header.addWidget(title_label)
        header.addStretch(1)
        header.addWidget(badge)
        layout.addLayout(header)

        metric_row = QHBoxLayout()
        metric_row.setSpacing(6)
        value_labels: list[QLabel] = []
        tones = ("active", "success", "warn")
        for label_text, tone in zip(metric_labels, tones):
            metric = QFrame()
            metric.setObjectName("RecoveryMetric")
            metric_layout = QVBoxLayout(metric)
            metric_layout.setContentsMargins(8, 5, 8, 5)
            metric_layout.setSpacing(1)
            metric_layout.addWidget(_label(label_text, "RecoveryMetricLabel"))
            value_label = _label("0", f"RecoveryMetricValue_{tone}")
            metric_layout.addWidget(value_label)
            value_labels.append(value_label)
            metric_row.addWidget(metric, 1)
        layout.addLayout(metric_row)
        return card, badge, tuple(value_labels)

    def _build_pdf_review_summary(self) -> QWidget:
        summary = QWidget()
        summary.setObjectName("RecoverySummary")
        layout = QHBoxLayout(summary)
        layout.setContentsMargins(0, 0, 0, 0)
        card, self.review_badge, values = self._build_recovery_card(
            "翻译审核",
            ("审核中", "已通过", "需复核"),
        )
        (
            self.review_processing_value,
            self.review_passed_value,
            self.review_failed_value,
        ) = values
        layout.addWidget(card, 1)
        summary.setVisible(False)
        return summary

    def _build_page_recovery_summary(self) -> QWidget:
        summary = QWidget()
        summary.setObjectName("RecoverySummary")
        layout = QHBoxLayout(summary)
        layout.setContentsMargins(0, 0, 0, 0)
        card, self.page_recovery_badge, values = self._build_recovery_card(
            "页级重试",
            ("重试中", "已恢复", "占位页"),
        )
        (
            self.page_retrying_value,
            self.page_recovered_value,
            self.page_placeholder_value,
        ) = values
        layout.addWidget(card, 1)
        summary.setVisible(False)
        return summary

    def _render_done_workspace(self, layout: QVBoxLayout) -> None:
        done = self.done
        if done is None:
            render_translation_result(
                layout,
                empty_message="PDF/图片翻译任务已完成。",
                done=None,
                summary_text="",
                summary_success=True,
                kpi_items=[],
                file_status_formatter=self._format_file_result_status,
            )
            return
        pdf_generated_count = sum(
            1
            for item in done.file_results
            if item.get("success") and item.get("source_type") != SOURCE_TYPE_IMAGE
        )
        image_generated_count = sum(
            1
            for item in done.file_results
            if item.get("success") and item.get("source_type") == SOURCE_TYPE_IMAGE
        )
        generated_count = pdf_generated_count + image_generated_count
        compressed_count = sum(1 for item in done.file_results if item.get("compressed_output"))
        failure_count = len(done.file_results) - generated_count
        placeholder_count = sum(int(item.get("placeholder_page_count") or 0) for item in done.file_results)
        emergency_count = sum(int(item.get("emergency_ratio_normalized_count") or 0) for item in done.file_results)
        review_passed_count = sum(int(item.get("review_passed_page_count") or 0) for item in done.file_results)
        review_repaired_count = sum(int(item.get("review_repaired_page_count") or 0) for item in done.file_results)
        review_failed_count = sum(int(item.get("review_failed_page_count") or 0) for item in done.file_results)
        review_enabled = any(item.get("review_enabled") for item in done.file_results)
        summary_success = (
            failure_count == 0
            and placeholder_count == 0
            and emergency_count == 0
            and review_failed_count == 0
        )
        if summary_success:
            summary_text = f"翻译成功：高清 PDF {pdf_generated_count} 个，压缩 PDF {compressed_count} 个，译图 {image_generated_count} 张。"
        else:
            summary_text = f"任务完成：高清 PDF {pdf_generated_count} 个，压缩 PDF {compressed_count} 个，译图 {image_generated_count} 张；需复核 {placeholder_count + emergency_count + review_failed_count} 页。"
        kpi_items = [
            ("高清 PDF", str(pdf_generated_count)),
            ("压缩 PDF", str(compressed_count)),
            ("译图", str(image_generated_count)),
            ("生成失败", str(failure_count)),
            ("失败占位页", str(placeholder_count)),
            ("应急归一化", str(emergency_count)),
        ]
        if review_enabled:
            kpi_items.extend(
                [
                    ("审核通过页", str(review_passed_count)),
                    ("审核修复页", str(review_repaired_count)),
                    ("审核未通过页", str(review_failed_count)),
                ]
            )
        kpi_items.extend(
            [
                ("耗时", format_elapsed(done.elapsed_sec)),
                ("图像请求", str(done.api_call_count)),
            ]
        )
        render_translation_result(
            layout,
            empty_message="PDF/图片翻译任务已完成。",
            done=done,
            summary_text=summary_text,
            summary_success=summary_success,
            kpi_items=kpi_items,
            issue_rows=self._build_result_issue_rows(done.issues),
            file_status_formatter=self._format_file_result_status,
            file_status_width=220,
            file_detail_width=220,
        )

    def _render_error_workspace(self, layout: QVBoxLayout) -> None:
        layout.addWidget(_label("任务异常", "SectionTitle"))
        message = QLabel(self._latest_error_message() or "PDF/图片翻译任务执行出错，请查看日志。")
        message.setWordWrap(True)
        layout.addWidget(message)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(360)
        layout.addWidget(self.log_view, 1)
        self._refresh_log_view()

    def _render_stopped_workspace(self, layout: QVBoxLayout) -> None:
        layout.addWidget(_label("任务已中止", "SectionTitle"))
        message = QLabel(self.stop_message or "任务已中止。已保留现有产物、页面素材和报告。")
        message.setWordWrap(True)
        layout.addWidget(message)

        manifest = self._load_terminal_manifest()
        file_records = [
            item for item in manifest.get("files", []) if isinstance(item, dict)
        ]
        if file_records:
            completed_pdf_count = sum(1 for item in file_records if item.get("translated_pdf_path"))
            completed_image_count = sum(1 for item in file_records if item.get("translated_image_path"))
            completed_count = completed_pdf_count + completed_image_count
            unfinished_count = len(file_records) - completed_count
            total_pages = str(manifest.get("total_page_count") or "")
            elapsed = self._format_manifest_elapsed(manifest.get("elapsed_sec"))
            kpis = [
                ("已完成 PDF", str(completed_pdf_count)),
                ("已完成图片", str(completed_image_count)),
                ("未完成文件", str(unfinished_count)),
            ]
            if total_pages:
                kpis.append(("总页数", total_pages))
            if elapsed:
                kpis.append(("耗时", elapsed))
            layout.addLayout(self._build_result_kpis(kpis))
            self._add_stopped_file_table(layout, file_records)

        paths = self._stopped_artifact_paths()
        self._add_path_field(layout, "输出目录", paths.get("output_dir", ""))
        self._add_path_field(layout, "报告位置", paths.get("report_path", ""))
        self._add_path_field(layout, "清单位置", paths.get("manifest_path", ""))
        self._add_path_field(layout, "页面素材", paths.get("page_archive_dir", ""))

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(360)
        layout.addWidget(self.log_view, 1)
        self._refresh_log_view()

    def _load_terminal_manifest(self) -> dict:
        manifest_path = self._terminal_manifest_path
        if not manifest_path and self._terminal_output_dir:
            manifest_path = str(Path(self._terminal_output_dir) / PDF_MANIFEST_FILENAME)
        if not manifest_path:
            return {}
        path = Path(manifest_path)
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - result page should still render.
            self._append_runtime_log("WARN", f"读取 PDF/图片翻译清单失败：{exc}")
            return {}
        return payload if isinstance(payload, dict) else {}

    def _stopped_artifact_paths(self) -> dict[str, str]:
        output_dir = self._terminal_output_dir
        report_path = self._terminal_report_path
        manifest_path = self._terminal_manifest_path
        if output_dir:
            root = Path(output_dir)
            report_path = report_path or str(root / PDF_REPORT_FILENAME)
            manifest_path = manifest_path or str(root / PDF_MANIFEST_FILENAME)
            page_archive_dir = str(root / PDF_PAGES_ROOT)
        else:
            page_archive_dir = ""
        return {
            "output_dir": output_dir,
            "report_path": report_path,
            "manifest_path": manifest_path,
            "page_archive_dir": page_archive_dir,
        }

    def _add_path_field(self, layout: QVBoxLayout, label: str, value: str) -> None:
        value = str(value or "").strip()
        if not value:
            return
        layout.addWidget(_label(label, "PillLabel"))
        row = QHBoxLayout()
        row.setSpacing(8)
        field = MiddleElideLineEdit(value)
        field.setReadOnly(True)
        row.addWidget(field, 1)
        copy_button = QPushButton("复制")
        copy_button.setProperty("compact", "true")
        copy_button.clicked.connect(lambda _checked=False, text=value: self._copy_text(text))
        row.addWidget(copy_button)
        layout.addLayout(row)

    def _copy_text(self, text: str) -> None:
        app = QApplication.instance()
        if app is not None:
            app.clipboard().setText(text)

    def _add_stopped_file_table(
        self,
        layout: QVBoxLayout,
        file_records: list[dict],
    ) -> None:
        layout.addWidget(_label("文件状态", "SectionTitle"))
        table = QTableWidget(len(file_records), 3)
        table.setHorizontalHeaderLabels(["文件名", "状态", "产物 / 素材"])
        configure_app_table(table, row_height=42)
        configure_file_result_table(table, status_width=128, detail_width=360)
        for row, record in enumerate(file_records):
            table.setItem(row, 0, create_elide_table_item(record.get("name") or ""))
            table.setItem(
                row,
                1,
                create_elide_table_item(
                    self._stopped_file_status_text(record),
                    alignment=Qt.AlignmentFlag.AlignCenter,
                ),
            )
            table.setItem(row, 2, create_elide_table_item(self._stopped_file_detail(record)))
        layout.addWidget(table)

    def _stopped_file_status_text(self, record: dict) -> str:
        if str(record.get("source_type") or "") == SOURCE_TYPE_IMAGE:
            if record.get("translated_image_path"):
                return "已完成译图"
            status = str(record.get("status") or "")
            if status == PDF_OUTPUT_STATE_FAILED:
                return "生成失败"
            if status == PDF_OUTPUT_STATE_STOPPED:
                return "未完成图片"
            return "未完成图片"
        if record.get("translated_pdf_path"):
            return "已完成 PDF"
        status = str(record.get("status") or "")
        if status == PDF_OUTPUT_STATE_FAILED:
            return "生成失败"
        if status == PDF_OUTPUT_STATE_STOPPED:
            return "未完成 PDF"
        return "未完成 PDF"

    def _stopped_file_detail(self, record: dict) -> str:
        outputs = [
            str(record.get("translated_pdf_path") or "").strip(),
            str(record.get("compressed_pdf_path") or "").strip(),
            str(record.get("translated_image_path") or "").strip(),
        ]
        completed_outputs = [item for item in outputs if item]
        if completed_outputs:
            return "；".join(completed_outputs)
        material_dir = self._material_dir_for_record(record)
        if material_dir:
            return f"页面素材：{material_dir}"
        return str(record.get("error") or "")

    def _material_dir_for_record(self, record: dict) -> str:
        if not self._terminal_output_dir:
            return ""
        relative = str(record.get("relative_path") or record.get("name") or "").strip()
        if not relative:
            return str(Path(self._terminal_output_dir) / PDF_PAGES_ROOT)
        return str(
            Path(self._terminal_output_dir)
            / PDF_PAGES_ROOT
            / TRANSLATED_PAGES_DIRNAME
            / Path(relative).with_suffix("")
        )

    @staticmethod
    def _format_manifest_elapsed(value: object) -> str:
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            return ""
        return format_elapsed(seconds)

    def _build_result_kpis(self, items: list[tuple[str, str]]) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)
        for label, value in items:
            tile = QFrame()
            tile.setObjectName("KpiTile")
            tile.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            tile.setFixedHeight(KPI_TILE_HEIGHT)
            layout = QVBoxLayout(tile)
            layout.setContentsMargins(12, 8, 12, 8)
            layout.setSpacing(4)
            layout.addWidget(_label(label, "PillLabel"))
            value_label = _label(value, "PillValue")
            value_label.setWordWrap(False)
            layout.addWidget(value_label)
            row.addWidget(tile, 1)
        return row

    def _build_result_issue_rows(self, issues: list[dict]) -> list[ResultIssueRow]:
        return [
            ResultIssueRow(
                issue_type="需复核",
                file_name=str(issue.get("file") or ""),
                position=str(issue.get("location_label") or ""),
                problem=str(issue.get("problem") or ""),
                status=str(issue.get("status") or ""),
            )
            for issue in issues
        ]

    def _format_file_result_status(self, result: dict) -> str:
        if not result.get("success"):
            return "生成失败"
        if result.get("source_type") == SOURCE_TYPE_IMAGE:
            parts = ["译图"]
            output_format = str(result.get("translated_image_format") or "").strip()
            if output_format:
                parts.append(output_format)
            review_failed = int(result.get("review_failed_page_count") or 0)
            review_repaired = int(result.get("review_repaired_page_count") or 0)
            if review_failed:
                parts.append(f"审核未通过 {review_failed} 页")
            if review_repaired:
                parts.append(f"审核修复 {review_repaired} 页")
            if not review_failed:
                parts.append("成功")
            return " / ".join(parts)
        parts = ["高清版"]
        if result.get("compressed_output"):
            parts.append("压缩版")
        placeholder = int(result.get("placeholder_page_count") or 0)
        emergency = int(result.get("emergency_ratio_normalized_count") or 0)
        if placeholder:
            parts.append(f"失败占位 {placeholder} 页")
        if emergency:
            parts.append(f"应急归一化 {emergency} 页")
        review_failed = int(result.get("review_failed_page_count") or 0)
        review_repaired = int(result.get("review_repaired_page_count") or 0)
        if review_failed:
            parts.append(f"审核未通过 {review_failed} 页")
        if review_repaired:
            parts.append(f"审核修复 {review_repaired} 页")
        if not placeholder and not emergency and not review_failed:
            parts.append("成功")
        return " / ".join(parts)

    def _render_action_card(self) -> None:
        self._normalize_terminal_state()
        _clear_layout(self.action_layout)
        self.action_layout.addWidget(_label("执行操作", "SectionTitle"))
        self._sync_queue_side_cards()

        if self.translation_list_open:
            close = QPushButton("关闭翻译列表")
            close.clicked.connect(self._toggle_translation_list)
            self.action_layout.addWidget(close)
            self.action_layout.addStretch(1)
            self.action_card.updateGeometry()
            self.action_card.update()
            self._render_queue_snapshot_card()
            return

        queue_label = self._queue_entry_text()
        if queue_label:
            queue_button = QPushButton(queue_label)
            queue_button.clicked.connect(self._toggle_translation_list)
            self.action_layout.addWidget(queue_button)
        deferred_label = self._deferred_terminal_entry_text()
        if deferred_label:
            deferred_button = QPushButton(deferred_label)
            deferred_button.setObjectName("PrimaryButton")
            deferred_button.clicked.connect(self._show_deferred_terminal_result)
            self.action_layout.addWidget(deferred_button)

        if self._is_preparing_next_task():
            selected = self._selected_files()
            lang_label = self._selected_target_label()
            review_label = "已启用翻译审核" if self.settings.pdf.review_enabled else "未启用翻译审核"
            image_count = self._image_file_count(selected)
            pdf_count = len(selected) - image_count
            note = QLabel(f"目标语言：{lang_label}；PDF {pdf_count} 个，图片 {image_count} 个；{review_label}")
            note.setWordWrap(True)
            note.setObjectName("MutedText")
            self.action_layout.addWidget(note)
            start = QPushButton(f"开始翻译（{lang_label}）")
            start.setObjectName("PrimaryButton")
            start.setEnabled(self._can_start())
            start.clicked.connect(self._start_translation)
            self.action_layout.addWidget(start)

            cancel = QPushButton("取消安排")
            cancel.clicked.connect(self._cancel_prepare_next_task)
            self.action_layout.addWidget(cancel)
        elif self.phase == "idle":
            selected = self._selected_files()
            lang_label = self._selected_target_label()
            review_label = "已启用翻译审核" if self.settings.pdf.review_enabled else "未启用翻译审核"
            image_count = self._image_file_count(selected)
            pdf_count = len(selected) - image_count
            note = QLabel(f"目标语言：{lang_label}；PDF {pdf_count} 个，图片 {image_count} 个；{review_label}")
            note.setWordWrap(True)
            note.setObjectName("MutedText")
            self.action_layout.addWidget(note)
            start = QPushButton(f"开始翻译（{lang_label}）")
            start.setObjectName("PrimaryButton")
            start.setEnabled(self._can_start())
            start.clicked.connect(self._start_translation)
            self.action_layout.addWidget(start)
        elif self._has_running_task():
            running_actions = QHBoxLayout()
            arrange_next = QPushButton("安排新任务")
            arrange_next.setObjectName("PrimaryButton")
            arrange_next.clicked.connect(self._prepare_next_task)
            running_actions.addWidget(arrange_next)
            if self._is_stop_requested():
                resume = QPushButton(PDF_RESUME_TRANSLATION_BUTTON_TEXT)
                resume.setObjectName("PrimaryButton")
                resume.clicked.connect(self._resume_translation)
                running_actions.addWidget(resume)
                self.action_layout.addLayout(running_actions)
                note = QLabel("已停止提交新页，可继续翻译。")
                note.setWordWrap(True)
                note.setObjectName("MutedText")
                self.action_layout.addWidget(note)
            else:
                stop = QPushButton(PDF_STOP_SUBMISSION_BUTTON_TEXT)
                stop.setObjectName("DangerButton")
                stop.clicked.connect(self._confirm_stop)
                running_actions.addWidget(stop)
                self.action_layout.addLayout(running_actions)
        else:
            reset = QPushButton("返回并开始新任务")
            reset.setObjectName("PrimaryButton")
            reset.clicked.connect(self._reset_task)
            self.action_layout.addWidget(reset)
        history = QPushButton("导出历史诊断归档" if count_diagnostic_records() > 0 else "暂无历史诊断")
        history.setObjectName("PdfHistoryDiagnosticsButton")
        history.setEnabled(count_diagnostic_records() > 0 and not self._has_running_task())
        history.clicked.connect(self._export_history_diagnostics)
        self.action_layout.addWidget(history)
        self.action_layout.addStretch(1)
        self.action_card.updateGeometry()
        self.action_card.update()
        self._render_queue_snapshot_card()

    def _queue_entry_text(self) -> str:
        if self.queue_controller is None:
            return ""
        tasks = self.queue_controller.queue.tasks()
        if len(tasks) <= 1:
            return ""
        active = self.queue_controller.queue.active_count()
        if active:
            running = next(
                (
                    task
                    for task in tasks
                    if task.status == "running"
                ),
                None,
            )
            position, total = (
                self.queue_controller.queue.active_position(
                    running.task_id,
                )
                if running is not None
                else (1, active)
            )
            return f"查看翻译列表（{position}/{total}）"
        return "查看翻译列表"

    def _deferred_terminal_entry_text(self) -> str:
        if self.deferred_terminal_phase == "done":
            return "查看上一轮结果"
        if self.deferred_terminal_phase == "error":
            return "查看上一轮异常"
        if self.deferred_terminal_phase == "stopped":
            return "查看上一轮中止结果"
        return ""

    def _defer_terminal_result(
        self,
        phase: str,
        *,
        done: DoneMsg | None = None,
        stop_message: str = "",
    ) -> None:
        self.deferred_terminal_phase = phase
        self.deferred_done = done
        self.deferred_stop_message = stop_message

    def _clear_deferred_terminal_result(self) -> None:
        self.deferred_terminal_phase = ""
        self.deferred_done = None
        self.deferred_stop_message = ""

    def _show_deferred_terminal_result(self) -> None:
        phase = self.deferred_terminal_phase
        if not phase:
            return
        self.preparing_next_task = False
        self.phase = phase
        self._workspace_render_phase = phase
        if phase == "done":
            self.done = self.deferred_done
        elif phase == "stopped":
            self.stop_message = self.deferred_stop_message
        self.translation_list_open = False
        self.runner = None
        self.poll_timer.stop()
        self._lock_inputs(False)
        self._clear_deferred_terminal_result()
        self._render_workspace()
        self._sync_window_sidebar_task_snapshot()

    def _sync_queue_side_cards(self) -> None:
        if hasattr(self, "output_card"):
            self.output_card.setVisible(not self.translation_list_open)
        if hasattr(self, "params_card"):
            self.params_card.setVisible(not self.translation_list_open)
        if hasattr(self, "queue_snapshot_card"):
            self.queue_snapshot_card.setVisible(self.translation_list_open)

    def _render_queue_snapshot_card(self) -> None:
        if not hasattr(self, "queue_snapshot_layout"):
            return
        clear_queue_layout(self.queue_snapshot_layout)
        if not self.translation_list_open:
            return
        render_selected_task_snapshot(
            self.queue_snapshot_layout,
            task=self.selected_queue_task(),
            on_stop=self._stop_queue_task,
            on_open_output=self._open_queue_output,
        )

    def _toggle_translation_list(self) -> None:
        if (
            self.queue_controller is None
            or len(self.queue_controller.queue.tasks()) <= 1
        ):
            self.translation_list_open = False
            self.selected_queue_task_id = ""
            self._render_workspace()
            self._sync_window_sidebar_task_snapshot()
            return
        self.translation_list_open = not self.translation_list_open
        self._ensure_selected_queue_task()
        self._render_workspace()
        self._sync_window_sidebar_task_snapshot()

    def _select_queue_task(self, task_id: str) -> None:
        self.selected_queue_task_id = task_id
        self._render_workspace()
        self._sync_window_sidebar_task_snapshot()

    def _move_queue_task(self, task_id: str, direction: int) -> None:
        if self.queue_controller is not None:
            self.queue_controller.move(task_id, direction)

    def _cancel_queue_task(self, task_id: str) -> None:
        if self.queue_controller is not None:
            self.queue_controller.cancel(task_id)

    def _clear_queue_history(self) -> None:
        if self.queue_controller is not None:
            self.queue_controller.clear_history()

    def _clear_queue_history_if_idle(self) -> None:
        if (
            self.queue_controller is not None
            and not self.queue_controller.queue.active_tasks()
        ):
            self.queue_controller.clear_history()

    def _stop_queue_task(self, task_id: str) -> None:
        if self.queue_controller is not None:
            self.queue_controller.request_stop(task_id)

    def _open_queue_output(self, task) -> None:
        path = str(task.output_path or task.snapshot.output_path or "").strip()
        if not path:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _prepare_next_task(self) -> None:
        self.preparing_next_task = True
        self.translation_list_open = False
        self.phase = "idle"
        self._workspace_render_phase = self.phase
        self.files = []
        self._lock_inputs(False)
        self._render_workspace()
        self._sync_window_sidebar_task_snapshot()

    def _is_preparing_next_task(self) -> bool:
        return bool(
            self.preparing_next_task
            or (
                self.phase == "idle"
                and self.runner is not None
                and self.done is None
            )
        )

    def _cancel_prepare_next_task(self) -> None:
        self.preparing_next_task = False
        self.translation_list_open = False
        if self.deferred_terminal_phase:
            self._show_deferred_terminal_result()
            return
        if self.runner is not None and self.done is None:
            self.phase = "running"
            self._workspace_render_phase = self.phase
            self._lock_inputs(True)
        else:
            self.phase = "idle" if self.done is None else self.phase
            self._workspace_render_phase = self.phase
            self._lock_inputs(False)
        self._render_workspace()
        self._sync_window_sidebar_task_snapshot()

    def _visible_action_button_texts(self) -> list[str]:
        return [
            button.text()
            for button in self.action_card.findChildren(QPushButton)
            if button.parent() is not None
        ]

    def _sync_action_card_with_workspace(self) -> None:
        if not hasattr(self, "action_card"):
            return
        if self._is_preparing_next_task():
            button_texts = self._visible_action_button_texts()
            if (
                "取消安排" not in button_texts
                or not any(text.startswith("开始翻译（") for text in button_texts)
            ):
                self._render_action_card()
            if self.runner is None:
                self._stop_ui_sync_guard()
            return
        terminal_phase = ""
        if self.done is not None:
            terminal_phase = "done"
        elif (
            self.phase in {"running", "done", "error", "stopped"}
            and self._workspace_render_phase in {"done", "error", "stopped"}
        ):
            terminal_phase = self._workspace_render_phase
        if not terminal_phase:
            if self.phase != "running":
                self._stop_ui_sync_guard()
            return
        self.phase = terminal_phase
        self.runner = None
        self.poll_timer.stop()
        self._lock_inputs(False)
        button_texts = self._visible_action_button_texts()
        if (
            PDF_STOP_SUBMISSION_BUTTON_TEXT in button_texts
            or PDF_RESUME_TRANSLATION_BUTTON_TEXT in button_texts
            or "返回并开始新任务" not in button_texts
        ):
            self._render_action_card()
            button_texts = self._visible_action_button_texts()
        if (
            PDF_STOP_SUBMISSION_BUTTON_TEXT not in button_texts
            and PDF_RESUME_TRANSLATION_BUTTON_TEXT not in button_texts
            and "返回并开始新任务" in button_texts
        ):
            self._stop_ui_sync_guard()

    def _start_ui_sync_guard(self) -> None:
        if not self.ui_sync_timer.isActive():
            self.ui_sync_timer.start()

    def _stop_ui_sync_guard(self) -> None:
        if self.ui_sync_timer.isActive():
            self.ui_sync_timer.stop()

    def _has_running_task(self) -> bool:
        self._normalize_terminal_state()
        return self.done is None and self.runner is not None

    def _is_stop_requested(self) -> bool:
        runner = self.runner
        return bool(
            runner is not None
            and hasattr(runner, "stop_requested")
            and runner.stop_requested()
        )

    def _normalize_terminal_state(self) -> None:
        if self.phase in {"done", "error", "stopped"}:
            self.runner = None
            self.poll_timer.stop()
            self._lock_inputs(False)
            return
        if self.done is not None:
            self.runner = None
            self.phase = "done"
            self.poll_timer.stop()
            self._lock_inputs(False)

    def _browse_source(self) -> None:
        current = self.source_input.text().strip().strip('"')
        base_path = Path(current).expanduser() if current else Path.home()
        base = str(base_path if base_path.is_dir() else base_path.parent)
        choice = QMessageBox(self)
        choice.setWindowTitle("浏览源路径")
        choice.setText("请选择源路径类型。")
        file_button = choice.addButton("选择 PDF 文件", QMessageBox.ButtonRole.ActionRole)
        image_button = None
        if self.settings.pdf.image_translation_enabled:
            image_button = choice.addButton("选择图片文件", QMessageBox.ButtonRole.ActionRole)
        folder_button = choice.addButton("选择文件夹", QMessageBox.ButtonRole.AcceptRole)
        choice.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        choice.exec()
        selected = ""
        clicked = choice.clickedButton()
        if clicked == file_button:
            selected, _ = QFileDialog.getOpenFileName(self, "选择 PDF 文件", base, "PDF 文件 (*.pdf)")
        elif image_button is not None and clicked == image_button:
            selected, _ = QFileDialog.getOpenFileName(self, "选择图片文件", base, IMAGE_FILE_FILTER)
        elif clicked == folder_button:
            selected = QFileDialog.getExistingDirectory(self, "选择源文件夹", base)
        else:
            return
        if selected:
            self.source_input.setText(selected)
            self._scan_source()

    def _scan_source(self) -> None:
        raw_path = self.source_input.text().strip().strip('"')
        if not raw_path:
            QMessageBox.warning(self, APP_NAME, "请输入文件夹或文件路径。")
            return
        input_path = Path(raw_path).expanduser()
        if not input_path.exists():
            QMessageBox.warning(self, APP_NAME, f"路径不存在：{raw_path}")
            return
        if input_path.is_file() and not is_supported_pdf_or_image_file(
            input_path,
            include_images=self.settings.pdf.image_translation_enabled,
        ):
            message = "不支持的文件类型：仅支持 .pdf 文件。"
            if self.settings.pdf.image_translation_enabled:
                message = "不支持的文件类型：仅支持 .pdf 或已启用的图片格式。"
            QMessageBox.warning(self, APP_NAME, message)
            return
        self.scan_button.setEnabled(False)
        self.scan_button.setText("扫描中...")
        self.scan_worker = PdfScanWorker(
            raw_path,
            self,
            include_images=self.settings.pdf.image_translation_enabled,
        )
        self.scan_worker.finished.connect(self._on_scan_finished)
        self.scan_worker.start()

    def _on_scan_finished(self, items: object, source_root: str, error: str) -> None:
        self.scan_button.setEnabled(True)
        self.scan_button.setText("扫描")
        if error:
            QMessageBox.warning(self, APP_NAME, f"扫描失败：{error}")
            return
        self.files = list(items)
        self.source_root = source_root
        self.settings.last_pdf_source_folder = self.source_input.text().strip().strip('"')
        self.phase = "idle"
        self.translation_list_open = False
        if not self._is_preparing_next_task():
            self.done = None
            self.task_files = []
            self.current_task_id = ""
            self.current_queue_task_id = ""
            self.selected_queue_task_id = ""
            self._clear_deferred_terminal_result()
            self._task_diagnostics_archived = False
            self._reset_runtime_logs()
            self._clear_queue_history_if_idle()
        save_settings(self.settings)
        self._render_workspace()
        self._render_action_card()

    def _selected_files(self) -> list[PdfFileItem]:
        table = getattr(self, "table", None)
        if table is None or table.rowCount() != len(self.files):
            return list(self.files)
        selected: list[PdfFileItem] = []
        for row, item in enumerate(self.files):
            check = table.item(row, 0)
            if check is None or check.checkState() == Qt.CheckState.Checked:
                selected.append(item)
        return selected

    @staticmethod
    def _file_type_label(item: PdfFileItem) -> str:
        return "图片" if item.source_type == SOURCE_TYPE_IMAGE else "PDF"

    @staticmethod
    def _image_file_count(items: list[PdfFileItem]) -> int:
        return sum(1 for item in items if item.source_type == SOURCE_TYPE_IMAGE)

    def _set_all_file_selection(self, checked: bool) -> None:
        table = getattr(self, "table", None)
        if table is None:
            return
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        table.blockSignals(True)
        for row in range(table.rowCount()):
            item = table.item(row, 0)
            if item is not None:
                item.setCheckState(state)
        table.blockSignals(False)
        self._on_file_selection_changed()

    def _on_file_selection_changed(self) -> None:
        self._refresh_selection_summary()
        self._refresh_header()
        self._render_action_card()

    def _refresh_selection_summary(self) -> None:
        label = getattr(self, "selection_status_label", None)
        if label is not None:
            label.setText(f"已选 {len(self._selected_files())} / {len(self.files)}")

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API name.
        super().resizeEvent(event)
        QTimer.singleShot(0, self._refresh_file_table_height)

    def _schedule_file_table_height_refresh(self) -> None:
        self._refresh_file_table_height()
        QTimer.singleShot(0, self._refresh_file_table_height)

    def _refresh_file_table_height(self) -> None:
        table = getattr(self, "table", None)
        if table is None:
            return
        header_height = max(table.horizontalHeader().height(), table.horizontalHeader().sizeHint().height(), 34)
        row_height = max(table.verticalHeader().defaultSectionSize(), 34)
        full_height = header_height + row_height * table.rowCount() + 2 * table.frameWidth() + 6
        viewport_height = self.workspace_scroll.viewport().height() if hasattr(self, "workspace_scroll") else self.height()
        target_height = min(full_height, max(220, int(viewport_height * 0.8)))
        table.setMinimumHeight(target_height)
        table.setMaximumHeight(target_height)

    def _selected_target_lang(self) -> str:
        if (
            hasattr(self, "target_combo")
            and self.target_combo.currentIndex() >= 0
            and self.target_combo.currentText().strip()
            != self.target_combo.itemText(self.target_combo.currentIndex()).strip()
        ):
            select_combo_text_match(self.target_combo)
        return str(self.target_combo.currentData() or self.settings.target_lang or "")

    def _selected_target_label(self) -> str:
        target_lang = str(self.target_combo.currentData()) if hasattr(self, "target_combo") else self.settings.target_lang
        if not is_supported_target_lang(target_lang, self.settings.custom_target_langs, include_optional=True):
            return "未选择"
        return get_target_lang_display(target_lang, self.settings.custom_target_langs, include_optional=True)

    def _can_start(self) -> bool:
        if self.phase != "idle" or not self._selected_files():
            return False
        if not self._selected_target_lang():
            return False
        if self.settings.output.use_custom_output_dir:
            return get_custom_output_dir_error(self.settings.output.custom_output_dir) is None
        return True

    def _is_pdf_review_model_configured(self) -> bool:
        try:
            config = resolve_effective_model_config(self.settings, ROLE_PDF_REVIEW)
        except Exception:
            return False
        return bool(
            config.model
            and config.api_key
            and (config.provider == "openai" or config.base_url)
            and provider_supports_capability(config.provider, "vision_text")
        )

    def _prompt_configure_pdf_review_model(self) -> None:
        box = QMessageBox(self)
        box.setWindowTitle(APP_NAME)
        box.setText("PDF 翻译审核模型未配置。")
        box.setInformativeText("请在左侧“模型配置”中完成审核模型设置。")
        configure_button = box.addButton("前往配置", QMessageBox.ButtonRole.ActionRole)
        box.addButton("取消启用", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() == configure_button:
            window = self.window()
            sidebar = getattr(window, "sidebar", None)
            if sidebar is not None and hasattr(sidebar, "select_model_role"):
                sidebar.select_model_role(ROLE_IMAGE)

    def _on_target_changed(self) -> None:
        target_lang = self._selected_target_lang()
        if target_lang:
            self.settings.target_lang = target_lang
            self.settings.recent_target_langs = remember_recent_target_lang(
                self.settings.recent_target_langs,
                target_lang,
                self.settings.custom_target_langs,
                include_optional=True,
            )
        save_settings(self.settings)
        self._refresh_header()
        self._render_action_card()

    def _on_params_changed(self) -> None:
        self.settings.pdf.page_retry_attempts = self.retry_spin.value()
        self.settings.pdf.generate_compressed_pdf = self.pdf_compression_checkbox.isChecked()
        raw = self.pdf_concurrency_input.text().strip()
        if not raw:
            self.settings.pdf.page_generation_concurrency = None
        else:
            try:
                value = max(1, min(PDF_PAGE_CONCURRENCY_SAFETY_CAP, int(raw)))
                self.settings.pdf.page_generation_concurrency = value
                self.pdf_concurrency_input.setText(str(value))
            except ValueError:
                self.pdf_concurrency_input.setText("")
                self.settings.pdf.page_generation_concurrency = None
        save_settings(self.settings)

    def _on_image_translation_enabled_changed(self, checked: bool) -> None:
        self.settings.pdf.image_translation_enabled = checked
        save_settings(self.settings)
        self._refresh_source_placeholder()
        self._render_workspace()

    def _on_review_enabled_changed(self, checked: bool) -> None:
        if checked and not self._is_pdf_review_model_configured():
            self.pdf_review_checkbox.blockSignals(True)
            self.pdf_review_checkbox.setChecked(False)
            self.pdf_review_checkbox.blockSignals(False)
            self.settings.pdf.review_enabled = False
            save_settings(self.settings)
            self._prompt_configure_pdf_review_model()
            return
        self.settings.pdf.review_enabled = checked
        save_settings(self.settings)

    def _on_output_changed(self) -> None:
        self.settings.output.use_custom_output_dir = self.output_custom_radio.isChecked()
        self.settings.output.custom_output_dir = (
            self.custom_output_input.text().strip().strip('"')
            if self.settings.output.use_custom_output_dir
            else ""
        )
        save_settings(self.settings)
        self._refresh_output_controls()
        self._render_action_card()

    def _refresh_output_controls(self) -> None:
        use_custom = self.output_custom_radio.isChecked()
        self.custom_output_input.setVisible(use_custom)
        self.custom_output_input.setEnabled(use_custom and self.phase != "running")
        if not use_custom:
            self.output_status.setText("输出目录将创建在源路径同级位置。")
            return
        custom_dir = self.custom_output_input.text().strip().strip('"')
        error = get_custom_output_dir_error(custom_dir)
        if error:
            self.output_status.setText(error)
            return
        custom_output_root = resolve_custom_output_dir(custom_dir)
        if custom_output_dir_will_be_created(custom_dir):
            self.output_status.setText(f"执行时创建目录：{custom_output_root}")
        else:
            self.output_status.setText("自定义输出目录可用。")

    def _lock_inputs(self, locked: bool) -> None:
        for widget in (
            self.source_input,
            self.browse_button,
            self.scan_button,
            self.target_combo,
            self.output_default_radio,
            self.output_custom_radio,
            self.custom_output_input,
            self.retry_spin,
            self.pdf_concurrency_input,
            self.image_translation_checkbox,
            self.pdf_compression_checkbox,
            self.pdf_review_checkbox,
        ):
            widget.setEnabled(not locked)

    def _start_translation(self) -> None:
        selected = self._selected_files()
        if not selected:
            QMessageBox.warning(self, APP_NAME, "请先扫描并选择至少一个 PDF 或图片文件。")
            return
        target_lang = self._selected_target_lang()
        if not target_lang:
            QMessageBox.warning(self, APP_NAME, "请先选择目标语言。")
            return
        if self.runner is not None and self.queue_controller is None:
            QMessageBox.warning(self, APP_NAME, "任务正在运行，暂不能直接启动新的 PDF/图片翻译。")
            return
        try:
            image_config = resolve_effective_model_config(self.settings, ROLE_IMAGE)
        except Exception as exc:  # noqa: BLE001 - converted to UI message.
            QMessageBox.warning(self, APP_NAME, f"图像生成模型配置不可用：{exc}")
            return
        if not provider_supports_capability(image_config.provider, "image"):
            QMessageBox.warning(self, APP_NAME, f"图像生成服务商不支持该能力：{image_config.provider}")
            return
        if not image_config.model:
            QMessageBox.warning(self, APP_NAME, "请先填写图像生成模型名称。")
            return
        if not self._handle_image_model_history_prompt():
            return
        if self.settings.pdf.review_enabled:
            if not self._is_pdf_review_model_configured():
                self._prompt_configure_pdf_review_model()
                return
            if not self._handle_pdf_review_model_history_prompt():
                return

        self.settings.target_lang = target_lang
        self._on_output_changed()
        self._on_params_changed()
        settings_snapshot = self.settings.model_copy(deep=True)
        if self.queue_controller is not None:
            try:
                image_config = resolve_effective_model_config(settings_snapshot, ROLE_IMAGE)
                pdf_concurrency = self._queue_pdf_concurrency(settings_snapshot)
                image_requirement = api_requirement_from_config(
                    image_config,
                    declared_concurrency=pdf_concurrency,
                )
                review_config = None
                review_requirement = None
                if settings_snapshot.pdf.review_enabled:
                    review_config = resolve_effective_model_config(
                        settings_snapshot,
                        ROLE_PDF_REVIEW,
                    )
                    review_requirement = api_requirement_from_config(
                        review_config,
                        declared_concurrency=1,
                    )
            except Exception as exc:  # noqa: BLE001 - converted to UI warning.
                QMessageBox.warning(self, APP_NAME, f"PDF 模型配置不可用：{exc}")
                return

            requirements = tuple(
                requirement
                for requirement in (image_requirement, review_requirement)
                if requirement is not None
            )
            if requirements:
                key_overrides = {image_config.provider: image_config.api_key}
                if review_config is not None:
                    key_overrides[review_config.provider] = review_config.api_key
                total_pages = sum(item.page_count for item in selected)
                selected_image_count = self._image_file_count(selected)
                selected_pdf_count = len(selected) - selected_image_count
                mixed_title = (
                    f"PDF/图片翻译 · {len(selected)} 个文件"
                    if selected_image_count
                    else f"PDF 翻译 · {len(selected)} 个文件"
                )
                task = TranslationTask(
                    snapshot=TranslationTaskSnapshot(
                        title=mixed_title,
                        translation_type=TRANSLATION_TYPE_PDF,
                        file_count=len(selected),
                        target_language=self._selected_target_label(),
                        source_path=self.source_root or "",
                        output_policy=(
                            "自定义目录"
                            if settings_snapshot.output.use_custom_output_dir
                            else "源目录内"
                        ),
                        domain=settings_snapshot.domain_preset,
                        prompt_summary=(
                            "启用翻译审核"
                            if settings_snapshot.pdf.review_enabled
                            else "未启用翻译审核"
                        ),
                        model_role=(
                            f"{image_config.label} / {review_config.label}"
                            if review_config is not None
                            else image_config.label
                        ),
                        provider=(
                            f"{image_config.provider} / {review_config.provider}"
                            if review_config is not None
                            else image_config.provider
                        ),
                        model=(
                            f"{image_config.model} / {review_config.model}"
                            if review_config is not None
                            else image_config.model
                        ),
                        api_key_fingerprint=self._queue_api_key_fingerprint(requirements),
                        concurrency_label=str(pdf_concurrency),
                        params=(
                            ("PDF 文件数", str(selected_pdf_count)),
                            ("图片文件数", str(selected_image_count)),
                            ("总页数", str(total_pages)),
                            ("页生成并发", str(pdf_concurrency)),
                            ("页级重试次数", str(settings_snapshot.pdf.page_retry_attempts)),
                            (
                                "压缩 PDF",
                                "生成" if settings_snapshot.pdf.generate_compressed_pdf else "不生成",
                            ),
                            (
                                "翻译审核",
                                "启用" if settings_snapshot.pdf.review_enabled else "未启用",
                            ),
                        ),
                    ),
                    group_requirements=requirements,
                    metadata={
                        "files": list(selected),
                        "settings": settings_snapshot,
                        "source_root": self.source_root or None,
                        "key_overrides": key_overrides,
                        "image_requirement": image_requirement,
                        "review_requirement": review_requirement,
                    },
                )
                arranged = self.queue_controller.arrange(
                    task,
                    starter=self._start_queued_translation,
                )
                self.preparing_next_task = False
                self.translation_list_open = False
                self.selected_queue_task_id = arranged.task_id
                if self.runner is not None and self.done is None:
                    self.phase = "running"
                    self._workspace_render_phase = self.phase
                self._render_workspace()
                self._sync_window_sidebar_task_snapshot()
                return

        self.preparing_next_task = False
        self._begin_runner(
            selected,
            self.settings,
            source_root=self.source_root or None,
        )

    def _queue_pdf_concurrency(self, settings: AppSettings) -> int:
        raw = settings.pdf.page_generation_concurrency
        if raw is None:
            raw = settings.engine.concurrency
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = 1
        return max(1, min(PDF_PAGE_CONCURRENCY_SAFETY_CAP, value))

    def _queue_api_key_fingerprint(self, requirements) -> str:
        fingerprints: list[str] = []
        for requirement in requirements:
            value = str(getattr(requirement, "key_fingerprint", "") or "")
            if value and value not in fingerprints:
                fingerprints.append(value)
        return " / ".join(fingerprints)

    def _start_queued_translation(self, task: TranslationTask) -> None:
        metadata = task.metadata
        image_requirement = metadata.get("image_requirement")
        review_requirement = metadata.get("review_requirement")
        image_scheduler = (
            self.queue_controller.queue.scheduler_for(
                image_requirement.key,
                fallback_capacity=image_requirement.declared_concurrency,
            )
            if self.queue_controller is not None and image_requirement is not None
            else None
        )
        review_scheduler = None
        if self.queue_controller is not None and review_requirement is not None:
            if (
                image_requirement is not None
                and review_requirement.key == image_requirement.key
            ):
                review_scheduler = image_scheduler
            else:
                review_scheduler = self.queue_controller.queue.scheduler_for(
                    review_requirement.key,
                    fallback_capacity=review_requirement.declared_concurrency,
                )
        self._begin_runner(
            list(metadata.get("files") or []),
            metadata.get("settings") or self.settings,
            source_root=metadata.get("source_root"),
            queue_task=task,
            api_scheduler=image_scheduler,
            review_api_scheduler=review_scheduler,
            key_overrides=dict(metadata.get("key_overrides") or {}),
        )

    def _begin_runner(
        self,
        selected: list[PdfFileItem],
        settings: AppSettings,
        *,
        source_root=None,
        queue_task: TranslationTask | None = None,
        api_scheduler=None,
        review_api_scheduler=None,
        key_overrides: dict[str, str] | None = None,
    ) -> None:
        effective_source_root = source_root if source_root is not None else self.source_root or None
        self.runner = PdfImageTranslationRunner(
            selected,
            settings,
            source_root=effective_source_root,
            key_overrides=key_overrides,
            api_scheduler=api_scheduler,
            review_api_scheduler=review_api_scheduler,
        )
        self.task_files = list(selected)
        self.current_task_source_root = str(effective_source_root or "")
        self.current_task_id = self.runner.task_id
        self.current_queue_task_id = queue_task.task_id if queue_task is not None else ""
        if queue_task is not None and self.queue_controller is not None:
            self.queue_controller.register_stopper(
                queue_task.task_id,
                self.runner.stop,
            )
        self._task_diagnostics_archived = False
        self._reset_runtime_logs()
        self.phase = "running"
        self._workspace_render_phase = self.phase
        self.progress = ProgressMsg(1, 4, "预处理 PDF/图片", 0, max(1, len(selected)))
        self.page_recovery_status = None
        self.review_status = None
        self.status_text = "状态：正在初始化任务..."
        self.done = None
        self.stop_message = ""
        self._terminal_output_dir = ""
        self._terminal_report_path = ""
        self._terminal_manifest_path = ""
        self._append_runtime_log("INFO", "正在初始化任务...")
        self._start_ui_sync_guard()
        self._lock_inputs(True)
        self._render_workspace()
        self._render_action_card()
        self.poll_timer.start()
        self.runner.start()

    def _handle_image_model_history_prompt(self) -> bool:
        role_settings = self.settings.image_model_role
        signature = image_model_signature(self.settings)
        if (
            role_settings.availability_status == "available"
            and role_settings.availability_signature == signature
        ):
            return True
        box = QMessageBox(self)
        box.setWindowTitle(APP_NAME)
        if (
            role_settings.availability_status == "unavailable"
            and role_settings.availability_signature == signature
        ):
            box.setText("图像生成模型上次校验不可用。")
            if role_settings.availability_message:
                box.setInformativeText(role_settings.availability_message)
        else:
            box.setText("图像生成模型尚未校验。")
            box.setInformativeText("建议先测试连接，也可继续执行。")
        test_button = box.addButton("测试连接", QMessageBox.ButtonRole.ActionRole)
        continue_button = box.addButton("继续生成", QMessageBox.ButtonRole.AcceptRole)
        cancel_button = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked == cancel_button:
            return False
        if clicked == continue_button:
            return True
        if clicked == test_button:
            result = check_image_generation_connectivity(self.settings)
            save_settings(self.settings)
            if not result.ok:
                QMessageBox.warning(self, APP_NAME, result.message)
                return False
        return True

    def _handle_pdf_review_model_history_prompt(self) -> bool:
        role_settings = self.settings.pdf_review_model_role
        signature = pdf_review_model_signature(self.settings)
        if (
            role_settings.availability_status == "available"
            and role_settings.availability_signature == signature
        ):
            return True
        box = QMessageBox(self)
        box.setWindowTitle(APP_NAME)
        if (
            role_settings.availability_status == "unavailable"
            and role_settings.availability_signature == signature
        ):
            box.setText("PDF 翻译审核模型上次校验不可用。")
            if role_settings.availability_message:
                box.setInformativeText(role_settings.availability_message)
        else:
            box.setText("PDF 翻译审核模型尚未校验。")
            box.setInformativeText("建议先测试审核连接，也可继续执行。")
        test_button = box.addButton("测试审核连接", QMessageBox.ButtonRole.ActionRole)
        continue_button = box.addButton("继续生成", QMessageBox.ButtonRole.AcceptRole)
        cancel_button = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked == cancel_button:
            return False
        if clicked == continue_button:
            return True
        if clicked == test_button:
            result = check_pdf_review_connectivity(self.settings)
            save_settings(self.settings)
            if not result.ok:
                QMessageBox.warning(self, APP_NAME, result.message)
                return False
        return True

    def _persist_runtime_settings(self) -> None:
        try:
            save_settings(self.settings)
        except Exception as exc:  # noqa: BLE001 - diagnostics should continue.
            self._append_runtime_log("WARN", f"运行状态保存失败：{exc}")

    def _confirm_stop(self) -> None:
        if self.runner is None:
            return
        if self._is_stop_requested():
            self._render_action_card()
            return
        answer = QMessageBox.question(
            self,
            APP_NAME,
            "确认停止提交新的 PDF/图片页面？\n\n已提交页面会继续完成；未完成文件仅保留页面素材和报告。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.runner.stop()
            self.status_text = self._stop_wait_status_text()
            self._refresh_running_widgets()
            self._render_action_card()

    def _resume_translation(self) -> None:
        if self.runner is None or not hasattr(self.runner, "resume"):
            return
        self.runner.resume()
        self.status_text = "状态：已继续翻译，正在提交后续页面..."
        self._refresh_running_widgets()
        self._render_action_card()

    def _poll_runner(self) -> None:
        runner = self.runner
        if runner is None:
            self.poll_timer.stop()
            return
        while True:
            msg = runner.get_message(timeout=0.0)
            if msg is None:
                break
            if isinstance(msg, LogMsg):
                self._append_runtime_log(
                    msg.level,
                    msg.message,
                    msg.ts,
                    visible=getattr(msg, "visible", True),
                )
            elif isinstance(msg, ProgressMsg):
                self.progress = msg
                if self.current_queue_task_id and self.queue_controller is not None:
                    self.queue_controller.update_progress(
                        self.current_queue_task_id,
                        progress_label=f"{msg.step_done}/{msg.step_total}",
                        status_message=msg.phase_name,
                    )
            elif isinstance(msg, PdfPageRecoveryStatusMsg):
                self.page_recovery_status = msg
            elif isinstance(msg, PdfReviewStatusMsg):
                self.review_status = msg
            elif isinstance(msg, StatusMsg):
                self.status_text = msg.phase_desc
                if self.current_queue_task_id and self.queue_controller is not None:
                    self.queue_controller.update_progress(
                        self.current_queue_task_id,
                        status_message=msg.phase_desc,
                    )
            elif isinstance(msg, DoneMsg):
                finished_queue_task_id = self.current_queue_task_id
                show_terminal = self.phase == "running" or not finished_queue_task_id
                self.done = msg if show_terminal else None
                self._terminal_output_dir = msg.output_dir
                self._terminal_report_path = msg.report_path
                self._terminal_manifest_path = str(Path(msg.output_dir) / "pdf_translation_manifest.json") if msg.output_dir else ""
                self.runner = None
                self.phase = "done" if show_terminal else "idle"
                self.poll_timer.stop()
                self._lock_inputs(False)
                self._persist_runtime_settings()
                self._archive_current_task(phase="done", done=msg)
                if finished_queue_task_id and self.queue_controller is not None:
                    self.queue_controller.finish_task(
                        finished_queue_task_id,
                        TASK_STATUS_COMPLETED,
                        message="已完成",
                        output_path=msg.output_dir,
                    )
                    if self.current_queue_task_id == finished_queue_task_id:
                        self.current_queue_task_id = ""
                if not show_terminal:
                    self._defer_terminal_result("done", done=msg)
                    self.done = None
                self._render_workspace()
                self._render_action_card()
                self._schedule_action_card_resync()
                return
            elif isinstance(msg, ErrorMsg):
                finished_queue_task_id = self.current_queue_task_id
                show_terminal = self.phase == "running" or not finished_queue_task_id
                self._append_runtime_log("ERROR", msg.message)
                self._terminal_output_dir = msg.output_dir
                self._terminal_report_path = msg.report_path
                self._terminal_manifest_path = msg.manifest_path
                self.runner = None
                self.phase = "error" if show_terminal else "idle"
                self.poll_timer.stop()
                self._lock_inputs(False)
                self._persist_runtime_settings()
                self._archive_current_task(
                    phase="error",
                    error_message=msg.message,
                    status=self.status_text or "任务异常",
                )
                if finished_queue_task_id and self.queue_controller is not None:
                    self.queue_controller.finish_task(
                        finished_queue_task_id,
                        TASK_STATUS_FAILED,
                        message=msg.message,
                        output_path=msg.output_dir,
                        error_message=msg.message,
                        block_api_groups=is_api_group_blocking_error(msg.message),
                    )
                    if self.current_queue_task_id == finished_queue_task_id:
                        self.current_queue_task_id = ""
                if not show_terminal:
                    self._defer_terminal_result("error")
                self._render_workspace()
                self._render_action_card()
                self._schedule_action_card_resync()
                return
            elif isinstance(msg, StoppedMsg):
                finished_queue_task_id = self.current_queue_task_id
                show_terminal = self.phase == "running" or not finished_queue_task_id
                self._append_runtime_log("WARN", msg.message)
                self.stop_message = msg.message
                self._terminal_output_dir = msg.output_dir
                self._terminal_report_path = msg.report_path
                self._terminal_manifest_path = msg.manifest_path
                self.runner = None
                self.phase = "stopped" if show_terminal else "idle"
                self.poll_timer.stop()
                self._lock_inputs(False)
                self._persist_runtime_settings()
                self._archive_current_task(
                    phase="stopped",
                    error_message=msg.message,
                    status=self.status_text or "任务已中止",
                )
                if finished_queue_task_id and self.queue_controller is not None:
                    self.queue_controller.finish_task(
                        finished_queue_task_id,
                        TASK_STATUS_STOPPED,
                        message=msg.message,
                        output_path=msg.output_dir,
                        error_message=msg.message,
                    )
                    if self.current_queue_task_id == finished_queue_task_id:
                        self.current_queue_task_id = ""
                if not show_terminal:
                    self._defer_terminal_result("stopped", stop_message=msg.message)
                self._render_workspace()
                self._render_action_card()
                self._schedule_action_card_resync()
                return
        if not runner.needs_poll():
            self.runner = None
            self.poll_timer.stop()
        self._refresh_running_widgets()

    def _schedule_action_card_resync(self) -> None:
        self._start_ui_sync_guard()
        QTimer.singleShot(0, self._render_action_card)
        QTimer.singleShot(150, self._render_action_card)

    def _archive_current_task(
        self,
        *,
        phase: str,
        done: DoneMsg | None = None,
        error_message: str = "",
        status: str = "",
    ) -> None:
        if self._task_diagnostics_archived:
            return
        task_id = self.current_task_id
        if not task_id and self.runner is not None:
            task_id = self.runner.task_id
        try:
            archive_task_diagnostics(
                surface="pdf",
                phase=phase,
                task_id=task_id or "runtime",
                settings=self.settings,
                selected_files=self.task_files or self._selected_files(),
                logs=list(self.diagnostic_log_entries),
                done=done,
                error_message=error_message,
                source_root=self.current_task_source_root or self.source_root or "",
                status=status or self.status_text or phase,
                progress=self.progress,
                task_artifacts={
                    "output_dir": self._terminal_output_dir,
                    "report_path": self._terminal_report_path,
                    "manifest_path": self._terminal_manifest_path,
                },
            )
            self._task_diagnostics_archived = True
        except Exception as exc:  # noqa: BLE001 - diagnostics must not block UI transition.
            self._append_runtime_log("WARN", f"诊断归档写入失败：{exc}")

    def _refresh_running_widgets(self) -> None:
        if not is_live_widget(getattr(self, "running_status", None)) or not is_live_widget(
            getattr(self, "progress_bar", None)
        ):
            return
        progress = self.progress
        if progress is None:
            self.running_status.setText(self.status_text or "正在初始化...")
            self.progress_bar.setValue(0)
        else:
            overall = self._calc_overall_progress(progress)
            self.progress_bar.setValue(int(overall * 100))
            status = self.status_text.replace("状态：", "", 1).strip()
            if self._is_stop_requested():
                status = self._stop_wait_status_text().replace("状态：", "", 1).strip()
            page_hint = ""
            if progress.phase_name == "翻译页面":
                page_hint = f" | 总页进度 {progress.step_done} / {progress.step_total}"
            self.running_status.setText(
                f"阶段 {progress.phase_index} / {progress.phase_total} | "
                f"{progress.phase_name} | {progress.step_done} / {progress.step_total}"
                f"{page_hint}"
                + (f"\n{status}" if status else "")
            )
        self._refresh_page_recovery_summary()
        self._refresh_review_summary()
        self._refresh_log_view()

    def _stop_wait_status_text(self) -> str:
        summary = self.page_recovery_status
        if summary is None:
            return "状态：正在停止任务：不再提交新页，等待已提交页面完成；可继续翻译。"
        submitted = getattr(summary, "submitted_page_count", 0)
        pending = getattr(summary, "pending_submitted_page_count", 0)
        return (
            f"状态：正在停止任务：已提交 {submitted} 页，等待 {pending} 页完成；"
            "可继续翻译。"
        )

    def _calc_overall_progress(self, progress: ProgressMsg) -> float:
        if progress.phase_total == 4:
            weights = {1: 0.06, 2: 0.82, 3: 0.10, 4: 0.02}
        else:
            weights = {index: 1.0 / max(progress.phase_total, 1) for index in range(1, progress.phase_total + 1)}
        offset = 0.0
        for phase_index in range(1, progress.phase_index):
            offset += weights.get(phase_index, 0.0)
        current = progress.step_done / max(progress.step_total, 1)
        return min(offset + current * weights.get(progress.phase_index, 0.0), 1.0)

    def _refresh_page_recovery_summary(self) -> None:
        summary_widget = getattr(self, "page_recovery_summary", None)
        if not is_live_widget(summary_widget):
            return
        summary = self.page_recovery_status
        if summary is None:
            summary_widget.setVisible(False)
            return
        visible = bool(
            summary.retrying_page_count
            or summary.retried_page_count
            or summary.recovered_page_count
            or summary.placeholder_page_count
        )
        summary_widget.setVisible(visible)
        if not visible:
            return
        if summary.retrying_page_count:
            self.page_recovery_badge.setText("处理中")
        elif summary.placeholder_page_count:
            self.page_recovery_badge.setText("需复核")
        else:
            self.page_recovery_badge.setText("已恢复")
        self.page_retrying_value.setText(str(summary.retrying_page_count))
        self.page_recovered_value.setText(str(summary.recovered_page_count))
        self.page_placeholder_value.setText(str(summary.placeholder_page_count))

    def _refresh_review_summary(self) -> None:
        summary_widget = getattr(self, "review_summary", None)
        if not is_live_widget(summary_widget):
            return
        if not self.settings.pdf.review_enabled:
            summary_widget.setVisible(False)
            return
        summary_widget.setVisible(True)
        summary = self.review_status
        if summary is None:
            self.review_badge.setText("等待候选")
            self.review_processing_value.setText("0")
            self.review_passed_value.setText("0")
            self.review_failed_value.setText("0")
            return
        if summary.review_processing_count:
            self.review_badge.setText("处理中")
        elif summary.review_round and summary.review_total:
            self.review_badge.setText(f"第 {summary.review_round}/{summary.review_total} 轮")
        else:
            self.review_badge.setText("等待候选")
        if (
            summary.review_processing_count == 0
            and summary.review_round
            and (summary.review_passed_count or summary.review_failed_count)
        ):
            self.review_badge.setText("已完成")
        self.review_processing_value.setText(str(summary.review_processing_count))
        self.review_passed_value.setText(str(summary.review_passed_count))
        self.review_failed_value.setText(str(summary.review_failed_count))

    def _reset_runtime_logs(self) -> None:
        self.log_entries = []
        self.diagnostic_log_entries = []
        self._visible_log_chars = 0
        self._diagnostic_log_chars = 0

    def _append_runtime_log(
        self,
        level: str,
        message: str,
        ts: str = "",
        *,
        visible: bool = True,
    ) -> None:
        entry = {"level": str(level or ""), "message": str(message or ""), "ts": str(ts or "")}
        if visible:
            self._append_bounded_log(self.log_entries, entry, "_visible_log_chars", VISIBLE_LOG_ENTRY_LIMIT, VISIBLE_LOG_CHAR_LIMIT)
        self._append_bounded_log(self.diagnostic_log_entries, entry, "_diagnostic_log_chars", DIAGNOSTIC_LOG_ENTRY_LIMIT, DIAGNOSTIC_LOG_CHAR_LIMIT)

    def _append_bounded_log(
        self,
        entries: list[dict[str, str]],
        entry: dict[str, str],
        char_attr: str,
        entry_limit: int,
        char_limit: int,
    ) -> None:
        entries.append(dict(entry))
        setattr(self, char_attr, getattr(self, char_attr) + self._log_entry_size(entry))
        while entries and (len(entries) > entry_limit or getattr(self, char_attr) > char_limit):
            removed = entries.pop(0)
            setattr(self, char_attr, max(0, getattr(self, char_attr) - self._log_entry_size(removed)))

    @staticmethod
    def _log_entry_size(entry: dict[str, str]) -> int:
        return sum(len(str(value or "")) for value in entry.values()) + 16

    def _refresh_log_view(self) -> None:
        if not is_live_widget(getattr(self, "log_view", None)):
            return
        lines = []
        for item in self.log_entries:
            prefix = f"[{item.get('ts')}]" if item.get("ts") else ""
            lines.append(f"{prefix} {item.get('level', '')} {item.get('message', '')}".strip())
        self.log_view.setPlainText("\n".join(lines))
        self.log_view.moveCursor(QTextCursor.MoveOperation.End)

    def _latest_error_message(self) -> str:
        errors = [
            item.get("message", "")
            for item in (self.diagnostic_log_entries or self.log_entries)
            if item.get("level") == "ERROR"
        ]
        return errors[-1] if errors else ""

    def _reset_task(self) -> None:
        self.phase = "idle"
        self.translation_list_open = False
        self._workspace_render_phase = self.phase
        self._stop_ui_sync_guard()
        self.files = []
        self.done = None
        self.runner = None
        self.progress = None
        self.page_recovery_status = None
        self.review_status = None
        self.status_text = ""
        self.stop_message = ""
        self.task_files = []
        self.current_task_source_root = ""
        self.current_task_id = ""
        self.current_queue_task_id = ""
        self.selected_queue_task_id = ""
        self.preparing_next_task = False
        self._clear_deferred_terminal_result()
        self._task_diagnostics_archived = False
        self._terminal_output_dir = ""
        self._terminal_report_path = ""
        self._terminal_manifest_path = ""
        self._reset_runtime_logs()
        self._lock_inputs(False)
        self._clear_queue_history_if_idle()
        self._render_workspace()
        self._render_action_card()

    def _export_history_diagnostics(self) -> None:
        try:
            data, filename, _ = build_diagnostics_history_zip_bytes()
        except Exception as exc:  # noqa: BLE001 - converted to UI message.
            QMessageBox.warning(self, APP_NAME, f"读取历史诊断失败：{exc}")
            return
        target, _ = QFileDialog.getSaveFileName(self, "导出历史诊断归档", filename, "Zip 文件 (*.zip)")
        if target:
            Path(target).write_bytes(data)
            QMessageBox.information(self, APP_NAME, f"已导出：{target}")
