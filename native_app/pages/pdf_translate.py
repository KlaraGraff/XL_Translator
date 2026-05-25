"""Native PDF image-layout translation page."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
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
    PdfFileItem,
    PdfImageTranslationRunner,
    is_supported_pdf_file,
)
from core.pdf_review import check_pdf_review_connectivity
from core.task_runner import (
    DoneMsg,
    ErrorMsg,
    LogMsg,
    PdfReviewStatusMsg,
    ProgressMsg,
    StatusMsg,
    StoppedMsg,
)
from native_app.result_view import ResultIssueRow, format_elapsed, render_translation_result
from native_app.widgets import (
    MiddleElideLabel,
    build_app_tooltip_html,
    configure_app_table,
    configure_file_selection_table,
    create_check_table_item,
    create_elide_table_item,
    create_searchable_combo,
    create_table_item,
    refresh_combo_completer,
    select_combo_text_match,
)
from native_app.workers import PdfScanWorker
from settings import AppSettings, save_settings


HEADER_TILE_HEIGHT = 48
HEADER_TILE_MIN_WIDTH = 86
HEADER_SOURCE_MIN_WIDTH = 300
HEADER_SOURCE_MAX_WIDTH = 430
VISIBLE_LOG_ENTRY_LIMIT = 300
VISIBLE_LOG_CHAR_LIMIT = 140_000
DIAGNOSTIC_LOG_ENTRY_LIMIT = 5000
DIAGNOSTIC_LOG_CHAR_LIMIT = 2_000_000


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
        self.source_root = settings.last_source_folder
        self.runner: PdfImageTranslationRunner | None = None
        self.scan_worker: PdfScanWorker | None = None
        self.log_entries: list[dict[str, str]] = []
        self.diagnostic_log_entries: list[dict[str, str]] = []
        self._visible_log_chars = 0
        self._diagnostic_log_chars = 0
        self.progress: ProgressMsg | None = None
        self.review_status: PdfReviewStatusMsg | None = None
        self.status_text = ""
        self.done: DoneMsg | None = None
        self.stop_message = ""
        self.task_files: list[PdfFileItem] = []
        self.current_task_id = ""
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

    def set_page_active(self, _active: bool) -> None:
        return

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
        self.action_card, self.action_layout = _card()
        side_layout.addWidget(self.action_card)
        self._build_output_card(side_layout)
        self._build_params_card(side_layout)
        side_layout.addStretch(1)
        side.setWidget(side_content)
        body.addWidget(side)

    def _refresh_header(self) -> None:
        _clear_layout(self.header_layout)
        self.header_layout.addWidget(_label("PDF Workspace", "PageEyebrow"))
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        title_row.addWidget(_label("PDF 翻译", "PageTitle"))
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
        self.source_input = QLineEdit(self.settings.last_source_folder)
        self.source_input.setPlaceholderText("可手动输入文件夹或 PDF 文件绝对路径")
        row.addWidget(self.source_input, 1)
        self.browse_button = QPushButton("浏览")
        self.browse_button.clicked.connect(self._browse_source)
        row.addWidget(self.browse_button)
        self.scan_button = QPushButton("扫描")
        self.scan_button.clicked.connect(self._scan_source)
        row.addWidget(self.scan_button)
        layout.addLayout(row)
        root.addWidget(frame)

    def _build_output_card(self, side_layout: QVBoxLayout) -> None:
        frame, layout = _card()
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
        self.custom_output_input = QLineEdit(self.settings.output.custom_output_dir)
        self.custom_output_input.setPlaceholderText("输入输出目录绝对路径")
        self.custom_output_input.editingFinished.connect(self._on_output_changed)
        layout.addWidget(self.custom_output_input)
        self.output_status = QLabel("")
        self.output_status.setWordWrap(True)
        self.output_status.setObjectName("FieldHint")
        layout.addWidget(self.output_status)
        self._refresh_output_controls()

    def _build_params_card(self, side_layout: QVBoxLayout) -> None:
        frame, layout = _card()
        side_layout.addWidget(frame)
        layout.addWidget(_label("任务参数", "SectionTitle"))
        layout.addWidget(_field_label("目标语言", "目标语言", "PDF 图像路线只需要选择目标语言。"))
        self.target_combo = create_searchable_combo()
        if self.target_combo.lineEdit() is not None:
            self.target_combo.lineEdit().setPlaceholderText("输入语言名称筛选")
        self._load_target_options()
        self.target_combo.currentIndexChanged.connect(self._on_target_changed)
        layout.addWidget(self.target_combo)
        layout.addWidget(_field_label("页级重试次数", "页级重试次数", "单页生成或质检失败后的重试次数。"))
        self.retry_spin = QSpinBox()
        self.retry_spin.setRange(PDF_PAGE_RETRY_ATTEMPTS_MIN, PDF_PAGE_RETRY_ATTEMPTS_MAX)
        self.retry_spin.setValue(self.settings.pdf.page_retry_attempts)
        self.retry_spin.valueChanged.connect(self._on_params_changed)
        layout.addWidget(self.retry_spin)
        layout.addWidget(_field_label("PDF 页生成并发数", "PDF 页生成并发数", "留空表示跟随云端并发数。"))
        self.pdf_concurrency_input = QLineEdit(
            "" if self.settings.pdf.page_generation_concurrency is None else str(self.settings.pdf.page_generation_concurrency)
        )
        self.pdf_concurrency_input.setPlaceholderText("留空")
        self.pdf_concurrency_input.editingFinished.connect(self._on_params_changed)
        layout.addWidget(self.pdf_concurrency_input)
        self.pdf_compression_checkbox = QCheckBox("同时生成压缩 PDF（推荐）")
        self.pdf_compression_checkbox.setChecked(self.settings.pdf.generate_compressed_pdf)
        self.pdf_compression_checkbox.toggled.connect(self._on_params_changed)
        _set_tooltip(
            self.pdf_compression_checkbox,
            "同时生成压缩 PDF",
            "勾选后会保留高清版，并额外生成带“压缩”标注的 PDF。",
            ["归档页面素材仍保留原质量。"],
        )
        layout.addWidget(self.pdf_compression_checkbox)
        compression_hint = QLabel("开启后会同时输出高清版和压缩版；关闭时只输出高清版。")
        compression_hint.setObjectName("FieldHint")
        compression_hint.setWordWrap(True)
        layout.addWidget(compression_hint)
        self.pdf_review_checkbox = QCheckBox("启用翻译审核")
        self.pdf_review_checkbox.setChecked(self.settings.pdf.review_enabled)
        self.pdf_review_checkbox.toggled.connect(self._on_review_enabled_changed)
        _set_tooltip(
            self.pdf_review_checkbox,
            "启用翻译审核",
            "启用后每页候选译图会先交给 PDF 翻译审核模型判断，通过后再采用。",
            [
                "这是可选增强流程，会增加审核请求和耗时。",
                "审核模型在左侧“模型配置”中的“PDF 图像翻译模型”下配置。",
            ],
        )
        layout.addWidget(self.pdf_review_checkbox)
        review_hint = QLabel("可选项：未启用时无需配置审核模型；启用后会保留候选图和审核记录。")
        review_hint.setObjectName("FieldHint")
        review_hint.setWordWrap(True)
        layout.addWidget(review_hint)
        fixed = QLabel("固定 300 DPI 渲染，页面素材统一保存为 PNG。")
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
        if self.phase == "idle":
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

    def _render_idle_workspace(self, layout: QVBoxLayout) -> None:
        if not self.files:
            layout.addWidget(_label("任务清单", "SectionTitle"))
            placeholder = QLabel("可手动输入文件夹或单个 PDF 文件路径后点击“扫描”，即可在此查看可处理文件列表。")
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
                    ("PDF 路线", "版式图像"),
                ]
            )
        )
        self.table = QTableWidget(len(self.files), 4)
        self.table.setHorizontalHeaderLabels(["选择", "文件名", "大小", "页数"])
        configure_app_table(self.table, row_height=38)
        self.table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        configure_file_selection_table(self.table, fixed_column_widths={2: 112, 3: 86})
        for row, item in enumerate(self.files):
            self.table.setItem(row, 0, create_check_table_item())
            self.table.setItem(row, 1, create_elide_table_item(item.path.name))
            self.table.setItem(row, 2, create_table_item(f"{item.size_kb:.1f} KB"))
            self.table.setItem(row, 3, create_table_item(item.page_count))
        self.table.itemChanged.connect(self._on_file_selection_changed)
        self._refresh_selection_summary()
        layout.addWidget(self.table, 1)
        self._schedule_file_table_height_refresh()

    def _render_running_workspace(self, layout: QVBoxLayout) -> None:
        layout.addWidget(_label("执行监控", "SectionTitle"))
        self.running_status = QLabel("初始化中，请稍候...")
        self.running_status.setObjectName("MutedText")
        layout.addWidget(self.running_status)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        layout.addWidget(self.progress_bar)
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

    def _render_done_workspace(self, layout: QVBoxLayout) -> None:
        done = self.done
        if done is None:
            render_translation_result(
                layout,
                empty_message="PDF 翻译任务已完成。",
                done=None,
                summary_text="",
                summary_success=True,
                kpi_items=[],
                file_status_formatter=self._format_file_result_status,
            )
            return
        generated_count = sum(1 for item in done.file_results if item.get("success"))
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
            summary_text = f"翻译成功：已生成 {generated_count} 个高清 PDF，{compressed_count} 个压缩 PDF。"
        else:
            summary_text = f"任务完成：已生成 {generated_count} 个高清 PDF，{compressed_count} 个压缩 PDF，需复核页面 {placeholder_count + emergency_count + review_failed_count} 页。"
        kpi_items = [
            ("高清 PDF", str(generated_count)),
            ("压缩 PDF", str(compressed_count)),
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
            empty_message="PDF 翻译任务已完成。",
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
        message = QLabel(self._latest_error_message() or "PDF 翻译任务执行出错，请查看日志。")
        message.setWordWrap(True)
        layout.addWidget(message)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(360)
        layout.addWidget(self.log_view, 1)
        self._refresh_log_view()

    def _render_stopped_workspace(self, layout: QVBoxLayout) -> None:
        layout.addWidget(_label("任务已中止", "SectionTitle"))
        message = QLabel(self.stop_message or "任务已中止，已保留页面素材和报告。")
        message.setWordWrap(True)
        layout.addWidget(message)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(360)
        layout.addWidget(self.log_view, 1)
        self._refresh_log_view()

    def _build_result_kpis(self, items: list[tuple[str, str]]) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)
        for label, value in items:
            tile = QFrame()
            tile.setObjectName("KpiTile")
            layout = QVBoxLayout(tile)
            layout.setContentsMargins(12, 10, 12, 10)
            layout.addWidget(_label(label, "PillLabel"))
            value_label = _label(value, "PillValue")
            value_label.setWordWrap(True)
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
        if self.phase == "idle":
            selected = self._selected_files()
            lang_label = self._selected_target_label()
            review_label = "已启用翻译审核" if self.settings.pdf.review_enabled else "未启用翻译审核"
            note = QLabel(
                f"当前目标语言：{lang_label}；可执行 PDF：{len(selected)} / {len(self.files)}；{review_label}"
            )
            note.setWordWrap(True)
            note.setObjectName("MutedText")
            self.action_layout.addWidget(note)
            start = QPushButton(f"开始翻译（{lang_label}）")
            start.setObjectName("PrimaryButton")
            start.setEnabled(self._can_start())
            start.clicked.connect(self._start_translation)
            self.action_layout.addWidget(start)
        elif self._has_running_task():
            stop = QPushButton("终止翻译")
            stop.setObjectName("DangerButton")
            stop.clicked.connect(self._confirm_stop)
            self.action_layout.addWidget(stop)
        else:
            reset = QPushButton("返回并开始新任务")
            reset.setObjectName("PrimaryButton")
            reset.clicked.connect(self._reset_task)
            self.action_layout.addWidget(reset)
        history = QPushButton("导出历史诊断归档" if count_diagnostic_records() > 0 else "暂无历史诊断")
        history.setEnabled(count_diagnostic_records() > 0 and not self._has_running_task())
        history.clicked.connect(self._export_history_diagnostics)
        self.action_layout.addWidget(history)
        self.action_layout.addStretch(1)

    def _visible_action_button_texts(self) -> list[str]:
        return [
            button.text()
            for button in self.action_card.findChildren(QPushButton)
            if button.parent() is not None
        ]

    def _sync_action_card_with_workspace(self) -> None:
        if not hasattr(self, "action_card"):
            return
        terminal_phase = ""
        if self.done is not None:
            terminal_phase = "done"
        elif self._workspace_render_phase in {"done", "error", "stopped"}:
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
        if "终止翻译" in button_texts or "返回并开始新任务" not in button_texts:
            self._render_action_card()
            button_texts = self._visible_action_button_texts()
        if "终止翻译" not in button_texts and "返回并开始新任务" in button_texts:
            self._stop_ui_sync_guard()

    def _start_ui_sync_guard(self) -> None:
        if not self.ui_sync_timer.isActive():
            self.ui_sync_timer.start()

    def _stop_ui_sync_guard(self) -> None:
        if self.ui_sync_timer.isActive():
            self.ui_sync_timer.stop()

    def _has_running_task(self) -> bool:
        self._normalize_terminal_state()
        return self.phase == "running" and self.done is None and self.runner is not None

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
        choice.setText("请选择要扫描的源路径类型。")
        folder_button = choice.addButton("选择文件夹", QMessageBox.ButtonRole.AcceptRole)
        file_button = choice.addButton("选择 PDF 文件", QMessageBox.ButtonRole.ActionRole)
        choice.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        choice.exec()
        selected = ""
        clicked = choice.clickedButton()
        if clicked == folder_button:
            selected = QFileDialog.getExistingDirectory(self, "选择源文件夹", base)
        elif clicked == file_button:
            selected, _ = QFileDialog.getOpenFileName(self, "选择 PDF 文件", base, "PDF 文件 (*.pdf)")
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
        if input_path.is_file() and not is_supported_pdf_file(input_path):
            QMessageBox.warning(self, APP_NAME, "不支持的文件类型：仅支持 .pdf 文件。")
            return
        self.scan_button.setEnabled(False)
        self.scan_button.setText("扫描中...")
        self.scan_worker = PdfScanWorker(raw_path, self)
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
        self.settings.last_source_folder = self.source_input.text().strip().strip('"')
        self.phase = "idle"
        self.done = None
        self.task_files = []
        self.current_task_id = ""
        self._task_diagnostics_archived = False
        self._reset_runtime_logs()
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
        box.setText("当前尚未配置 PDF 翻译审核模型。")
        box.setInformativeText("请前往左侧“模型配置”-“PDF 图像翻译模型”中配置“PDF 翻译审核模型”。")
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
            self.output_status.setText("输出目录会创建在源路径同级位置，并自动附带时间戳。")
            return
        custom_dir = self.custom_output_input.text().strip().strip('"')
        error = get_custom_output_dir_error(custom_dir)
        if error:
            self.output_status.setText(error)
            return
        custom_output_root = resolve_custom_output_dir(custom_dir)
        if custom_output_dir_will_be_created(custom_dir):
            self.output_status.setText(f"目录将在执行时自动创建：{custom_output_root}")
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
            self.pdf_compression_checkbox,
            self.pdf_review_checkbox,
        ):
            widget.setEnabled(not locked)

    def _start_translation(self) -> None:
        selected = self._selected_files()
        if not selected:
            QMessageBox.warning(self, APP_NAME, "请先扫描并选择至少一个 PDF 文件。")
            return
        target_lang = self._selected_target_lang()
        if not target_lang:
            QMessageBox.warning(self, APP_NAME, "请先选择目标语言。")
            return
        try:
            image_config = resolve_effective_model_config(self.settings, ROLE_IMAGE)
        except Exception as exc:  # noqa: BLE001 - converted to UI message.
            QMessageBox.warning(self, APP_NAME, f"图像生成模型配置不可用：{exc}")
            return
        if not provider_supports_capability(image_config.provider, "image"):
            QMessageBox.warning(self, APP_NAME, f"当前图像生成模型服务商不支持图像生成能力：{image_config.provider}")
            return
        if not image_config.model:
            QMessageBox.warning(self, APP_NAME, "请先在左侧“模型配置”中填写图像生成模型名称。")
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
        self.runner = PdfImageTranslationRunner(
            selected,
            self.settings,
            source_root=self.source_root or None,
        )
        self.task_files = list(selected)
        self.current_task_id = self.runner.task_id
        self._task_diagnostics_archived = False
        self._reset_runtime_logs()
        self.runner.start()
        self.phase = "running"
        self._workspace_render_phase = self.phase
        self.progress = None
        self.review_status = None
        self.status_text = ""
        self.done = None
        self.stop_message = ""
        self._terminal_output_dir = ""
        self._terminal_report_path = ""
        self._terminal_manifest_path = ""
        self._start_ui_sync_guard()
        self._lock_inputs(True)
        self._render_workspace()
        self._render_action_card()
        self.poll_timer.start()

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
            box.setText("当前图像生成模型上次记录为不可用。")
            if role_settings.availability_message:
                box.setInformativeText(role_settings.availability_message)
        else:
            box.setText("当前图像生成模型尚未完成可用性校验。")
            box.setInformativeText("建议先测试连接；也可以继续生成并让真实任务记录可用状态。")
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
            box.setText("当前 PDF 翻译审核模型上次记录为不可用。")
            if role_settings.availability_message:
                box.setInformativeText(role_settings.availability_message)
        else:
            box.setText("当前 PDF 翻译审核模型尚未完成可用性校验。")
            box.setInformativeText("建议先测试审核连接；也可以继续生成并让真实任务记录可用状态。")
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
        answer = QMessageBox.question(
            self,
            APP_NAME,
            "确认终止当前 PDF 翻译任务？\n\n已提交页面会等待结束或超时，任务不会合成最终 PDF。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.runner.stop()
            self.status_text = "正在停止任务：不再提交新页，等待已提交页面结束..."
            self._refresh_running_widgets()

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
                self._append_runtime_log(msg.level, msg.message, msg.ts)
            elif isinstance(msg, ProgressMsg):
                self.progress = msg
            elif isinstance(msg, PdfReviewStatusMsg):
                self.review_status = msg
            elif isinstance(msg, StatusMsg):
                self.status_text = msg.phase_desc
            elif isinstance(msg, DoneMsg):
                self.done = msg
                self._terminal_output_dir = msg.output_dir
                self._terminal_report_path = msg.report_path
                self._terminal_manifest_path = str(Path(msg.output_dir) / "pdf_translation_manifest.json") if msg.output_dir else ""
                self.runner = None
                self.phase = "done"
                self.poll_timer.stop()
                self._lock_inputs(False)
                self._persist_runtime_settings()
                self._archive_current_task(phase="done", done=msg)
                self._render_workspace()
                self._render_action_card()
                self._schedule_action_card_resync()
                return
            elif isinstance(msg, ErrorMsg):
                self._append_runtime_log("ERROR", msg.message)
                self._terminal_output_dir = msg.output_dir
                self._terminal_report_path = msg.report_path
                self._terminal_manifest_path = msg.manifest_path
                self.runner = None
                self.phase = "error"
                self.poll_timer.stop()
                self._lock_inputs(False)
                self._persist_runtime_settings()
                self._archive_current_task(
                    phase="error",
                    error_message=msg.message,
                    status=self.status_text or "任务异常",
                )
                self._render_workspace()
                self._render_action_card()
                self._schedule_action_card_resync()
                return
            elif isinstance(msg, StoppedMsg):
                self._append_runtime_log("WARN", msg.message)
                self.stop_message = msg.message
                self._terminal_output_dir = msg.output_dir
                self._terminal_report_path = msg.report_path
                self._terminal_manifest_path = msg.manifest_path
                self.runner = None
                self.phase = "stopped"
                self.poll_timer.stop()
                self._lock_inputs(False)
                self._persist_runtime_settings()
                self._archive_current_task(
                    phase="stopped",
                    error_message=msg.message,
                    status=self.status_text or "任务已中止",
                )
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
                source_root=self.source_root or "",
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
        if not hasattr(self, "running_status"):
            return
        progress = self.progress
        if progress is None:
            self.running_status.setText(self.status_text or "初始化中，请稍候...")
            self.progress_bar.setValue(0)
        else:
            overall = min(progress.step_done / max(progress.step_total, 1), 1.0)
            if progress.phase_total:
                overall = min((progress.phase_index - 1 + overall) / progress.phase_total, 1.0)
            self.progress_bar.setValue(int(overall * 100))
            status = self.status_text.replace("状态：", "", 1).strip()
            self.running_status.setText(
                f"阶段 {progress.phase_index} / {progress.phase_total} | "
                f"{progress.phase_name} | {progress.step_done} / {progress.step_total}"
                + (f"\n{status}" if status else "")
            )
        self._refresh_review_summary()
        self._refresh_log_view()

    def _refresh_review_summary(self) -> None:
        summary_widget = getattr(self, "review_summary", None)
        if summary_widget is None:
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

    def _append_runtime_log(self, level: str, message: str, ts: str = "") -> None:
        entry = {"level": str(level or ""), "message": str(message or ""), "ts": str(ts or "")}
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
        if not hasattr(self, "log_view"):
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
        self._workspace_render_phase = self.phase
        self._stop_ui_sync_guard()
        self.files = []
        self.done = None
        self.runner = None
        self.progress = None
        self.status_text = ""
        self.stop_message = ""
        self.task_files = []
        self.current_task_id = ""
        self._task_diagnostics_archived = False
        self._terminal_output_dir = ""
        self._terminal_report_path = ""
        self._terminal_manifest_path = ""
        self._reset_runtime_logs()
        self._lock_inputs(False)
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
