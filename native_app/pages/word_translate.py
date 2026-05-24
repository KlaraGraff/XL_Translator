"""Native Word translation page."""

from __future__ import annotations

import html
from pathlib import Path

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QFileDialog,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QHeaderView,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app_meta import APP_NAME
from config import (
    WORD_BATCH_CHARS_MAX,
    WORD_BATCH_CHARS_MIN,
    WORD_BATCH_PARAGRAPHS_MAX,
    WORD_BATCH_PARAGRAPHS_MIN,
    WORD_BATCH_SPLIT_CHARS_MAX,
    WORD_BATCH_SPLIT_CHARS_MIN,
    WORD_STRICT_RETRY_ATTEMPTS_MAX,
    WORD_STRICT_RETRY_ATTEMPTS_MIN,
)
from core.api_config_check import check_translation_api_config
from core.bilingual_writer import (
    custom_output_dir_will_be_created,
    get_custom_output_dir_error,
    resolve_custom_output_dir,
)
from core.diagnostics import build_diagnostics_history_zip_bytes, count_diagnostic_records
from core.language_registry import (
    get_ordered_target_lang_codes,
    get_target_lang_display,
    is_supported_target_lang,
    remember_recent_target_lang,
)
from core.task_runner import DoneMsg, ErrorMsg, LogMsg, ProgressMsg, StatusMsg, StoppedMsg
from core.word_document import WordFileItem, is_supported_word_file
from core.word_task_runner import WordTaskRunner
from native_app.workers import WordScanWorker
from native_app.widgets import (
    configure_searchable_combo,
    refresh_combo_completer,
    select_combo_text_match,
)
from settings import AppSettings, save_settings


HEADER_TILE_HEIGHT = 48
HEADER_TILE_MIN_WIDTH = 86
HEADER_SOURCE_MIN_WIDTH = 300
HEADER_SOURCE_MAX_WIDTH = 430


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
    item_html = "".join(f"<li>{html.escape(item)}</li>" for item in items or [])
    body = f"<b>{html.escape(title)}</b><br>{html.escape(summary)}"
    if item_html:
        body += f"<ul>{item_html}</ul>"
    return body


def _set_tooltip(
    widget: QWidget,
    title: str,
    summary: str,
    items: list[str] | None = None,
) -> None:
    widget.setToolTip(_tooltip(title, summary, items))
    widget.setToolTipDuration(3600)


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


class WordTranslatePage(QWidget):
    """Qt implementation of the Word translation workspace."""

    languageChanged = Signal(str, str)

    def __init__(self, settings: AppSettings):
        super().__init__()
        self.settings = settings
        self.phase = "idle"
        self.files: list[WordFileItem] = []
        self.source_root = settings.last_source_folder
        self.runner: WordTaskRunner | None = None
        self.scan_worker: WordScanWorker | None = None
        self.log_entries: list[dict[str, str]] = []
        self.progress: ProgressMsg | None = None
        self.status_text = ""
        self.done: DoneMsg | None = None
        self.stop_message = ""

        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(800)
        self.poll_timer.timeout.connect(self._poll_runner)

        self._build_ui()
        self._render_workspace()
        self._render_action_card()

    def refresh_settings(self) -> None:
        self._refresh_header()
        self._render_action_card()

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

        scroll = QScrollArea()
        self.workspace_scroll = scroll
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        self.workspace_layout = QVBoxLayout(content)
        self.workspace_layout.setContentsMargins(0, 0, 0, 0)
        self.workspace_layout.setSpacing(0)
        scroll.setWidget(content)
        body.addWidget(scroll, 1)

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
        self.header_layout.addWidget(_label("Word Workspace", "PageEyebrow"))

        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        title_row.addWidget(_label("Word 翻译", "PageTitle"))
        title_row.addStretch(1)
        title_row.addWidget(
            self._pill("目标语言", self._selected_target_label()),
            alignment=Qt.AlignmentFlag.AlignTop,
        )
        title_row.addWidget(
            self._pill("已选文件", f"{len(self._selected_files())} 个"),
            alignment=Qt.AlignmentFlag.AlignTop,
        )
        title_row.addWidget(
            self._pill(
                "源路径",
                self.source_root or "尚未选择",
                min_width=HEADER_SOURCE_MIN_WIDTH,
                max_width=HEADER_SOURCE_MAX_WIDTH,
            ),
            alignment=Qt.AlignmentFlag.AlignTop,
        )
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
        self.source_input.setPlaceholderText("可手动输入文件夹或 Word 文件绝对路径")
        _set_tooltip(
            self.source_input,
            "源路径",
            "可输入一个文件夹或单个 DOCX 文件的绝对路径。",
            ["文件夹会递归扫描其中所有 .docx 文件。"],
        )
        row.addWidget(self.source_input, 1)

        self.browse_button = QPushButton("浏览")
        _set_tooltip(self.browse_button, "浏览", "选择源文件夹或单个 Word 文件。")
        self.browse_button.clicked.connect(self._browse_source)
        row.addWidget(self.browse_button)

        self.scan_button = QPushButton("扫描")
        _set_tooltip(self.scan_button, "扫描", "读取源路径中的 Word 文件并生成任务清单。")
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
        _set_tooltip(
            self.output_default_radio,
            "源目录内",
            "输出目录会创建在源路径同级位置，目录名自动附带时间戳。",
        )
        _set_tooltip(
            self.output_custom_radio,
            "自定义目录",
            "将翻译结果集中写入指定输出目录。",
        )
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

        self.target_lang_label = _field_label(
            "目标语言",
            "目标语言",
            "选择本次要翻译成的语言。",
            ["可与源语言独立选择；新配置默认目标语言为英文。"],
        )
        layout.addWidget(self.target_lang_label)
        self.target_combo = QComboBox()
        configure_searchable_combo(self.target_combo)
        if self.target_combo.lineEdit() is not None:
            self.target_combo.lineEdit().setPlaceholderText("输入语言名称筛选")
        self._load_target_options()
        _set_tooltip(
            self.target_combo,
            "目标语言",
            "默认目标语言为英文，默认源语言为中文；输入前几个字可快速匹配目标语言。",
            ["点击右侧箭头可展开候选列表。"],
        )
        self.target_combo.currentIndexChanged.connect(self._on_target_changed)
        layout.addWidget(self.target_combo)

        self.source_lang_label = _field_label(
            "源语言",
            "源语言",
            "选择原文语言，可与目标语言独立设置。",
        )
        layout.addWidget(self.source_lang_label)
        self.source_lang_combo = QComboBox()
        configure_searchable_combo(self.source_lang_combo)
        if self.source_lang_combo.lineEdit() is not None:
            self.source_lang_combo.lineEdit().setPlaceholderText("输入语言名称筛选")
        self._load_source_options()
        _set_tooltip(self.source_lang_combo, "源语言", "选择原文语言；输入前几个字可快速匹配。")
        self.source_lang_combo.currentIndexChanged.connect(self._on_source_lang_changed)
        layout.addWidget(self.source_lang_combo)

        self.highlight_check = QCheckBox("高亮需复核原文")
        self.highlight_check.setChecked(self.settings.word_review.highlight_unresolved)
        _set_tooltip(
            self.highlight_check,
            "高亮需复核原文",
            "在生成的双语 Word 中标记仍需人工确认的段落或表格单元格。",
        )
        self.highlight_check.toggled.connect(self._on_params_changed)
        layout.addWidget(self.highlight_check)

        layout.addWidget(_field_label("每批最多段落", "每批最多段落", "控制 Word 段落打包数量。"))
        self.batch_paragraphs_spin = QSpinBox()
        self.batch_paragraphs_spin.setRange(WORD_BATCH_PARAGRAPHS_MIN, WORD_BATCH_PARAGRAPHS_MAX)
        self.batch_paragraphs_spin.setValue(self.settings.word_batch.max_paragraphs_per_batch)
        self.batch_paragraphs_spin.valueChanged.connect(self._on_params_changed)
        layout.addWidget(self.batch_paragraphs_spin)

        layout.addWidget(_field_label("每批最多字符", "每批最多字符", "控制 Word 批次字符预算。"))
        self.batch_chars_spin = QSpinBox()
        self.batch_chars_spin.setRange(WORD_BATCH_CHARS_MIN, WORD_BATCH_CHARS_MAX)
        self.batch_chars_spin.setValue(self.settings.word_batch.max_chars_per_batch)
        self.batch_chars_spin.valueChanged.connect(self._on_params_changed)
        layout.addWidget(self.batch_chars_spin)

        layout.addWidget(_field_label("长段拆分阈值", "长段拆分阈值", "超过该字符数的段落会尝试拆分。"))
        self.split_chars_spin = QSpinBox()
        self.split_chars_spin.setRange(WORD_BATCH_SPLIT_CHARS_MIN, WORD_BATCH_SPLIT_CHARS_MAX)
        self.split_chars_spin.setValue(self.settings.word_batch.split_paragraph_chars)
        self.split_chars_spin.valueChanged.connect(self._on_params_changed)
        layout.addWidget(self.split_chars_spin)

        layout.addWidget(_field_label("严格重试次数", "严格重试次数", "首轮失败后单段严格重试的次数。"))
        self.retry_spin = QSpinBox()
        self.retry_spin.setRange(WORD_STRICT_RETRY_ATTEMPTS_MIN, WORD_STRICT_RETRY_ATTEMPTS_MAX)
        self.retry_spin.setValue(self.settings.word_batch.strict_retry_attempts)
        self.retry_spin.valueChanged.connect(self._on_params_changed)
        layout.addWidget(self.retry_spin)
        self._sync_source_lang_visibility()

    def _load_target_options(self) -> None:
        target_codes = get_ordered_target_lang_codes(
            self.settings.recent_target_langs,
            self.settings.custom_target_langs,
            include_optional=True,
        )
        self.target_combo.clear()
        for code in target_codes:
            self.target_combo.addItem(
                get_target_lang_display(
                    code,
                    self.settings.custom_target_langs,
                    include_optional=True,
                ),
                code,
            )
        index = self.target_combo.findData(self.settings.target_lang)
        self.target_combo.setCurrentIndex(index if index >= 0 else 0)
        refresh_combo_completer(self.target_combo)

    def _load_source_options(self) -> None:
        source_codes = get_ordered_target_lang_codes(
            [self.settings.source_lang],
            self.settings.custom_target_langs,
            include_optional=True,
        )
        self.source_lang_combo.clear()
        for code in source_codes:
            self.source_lang_combo.addItem(
                get_target_lang_display(
                    code,
                    self.settings.custom_target_langs,
                    include_optional=True,
                ),
                code,
            )
        index = self.source_lang_combo.findData(self.settings.source_lang)
        self.source_lang_combo.setCurrentIndex(index if index >= 0 else 0)
        refresh_combo_completer(self.source_lang_combo)

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
        label_widget = QLabel(label)
        label_widget.setObjectName("PillLabel")
        value_widget = QLabel(value)
        value_widget.setObjectName("PillValue")
        value_widget.setWordWrap(False)
        value_widget.setMaximumWidth(260)
        value_widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(label_widget)
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
        frame.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
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

    def _render_idle_workspace(self, layout: QVBoxLayout) -> None:
        if not self.files:
            layout.addWidget(_label("任务清单", "SectionTitle"))
            placeholder = QLabel(
                "可手动输入文件夹或单个 Word 文件路径后点击“扫描”，"
                "也可点击“浏览”选择并自动扫描，即可在此查看可处理文件列表。"
            )
            placeholder.setWordWrap(True)
            placeholder.setObjectName("MutedText")
            layout.addWidget(placeholder)
            return

        title_row = QHBoxLayout()
        title_row.setSpacing(10)
        title_row.addWidget(_label("任务清单", "SectionTitle"))
        self.selection_status_label = QLabel("")
        self.selection_status_label.setObjectName("FieldHint")
        title_row.addWidget(self.selection_status_label)
        title_row.addStretch(1)
        select_all = QPushButton("全选")
        _set_tooltip(select_all, "全选", "勾选当前任务清单中的全部 Word 文件。")
        select_all.clicked.connect(lambda: self._set_all_file_selection(True))
        title_row.addWidget(select_all)
        deselect_all = QPushButton("全不选")
        _set_tooltip(deselect_all, "全不选", "取消勾选当前任务清单中的全部 Word 文件。")
        deselect_all.clicked.connect(lambda: self._set_all_file_selection(False))
        title_row.addWidget(deselect_all)
        layout.addLayout(title_row)

        layout.addLayout(
            self._build_result_kpis(
                [
                    ("已扫描文件", str(len(self.files))),
                    ("已选任务", str(len(self._selected_files()))),
                    ("正文段落", str(sum(item.paragraph_count for item in self.files))),
                    ("表格数", str(sum(item.table_count for item in self.files))),
                ]
            )
        )
        self.table = QTableWidget(len(self.files), 5)
        self.table.setHorizontalHeaderLabels(["选择", "文件名", "大小", "段落", "表格"])
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        self._configure_file_table_columns()
        for row, item in enumerate(self.files):
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            check.setCheckState(Qt.CheckState.Checked)
            check.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, 0, check)
            self.table.setItem(row, 1, QTableWidgetItem(item.name))
            self.table.setItem(row, 2, QTableWidgetItem(f"{item.size_kb:.1f} KB"))
            self.table.setItem(row, 3, QTableWidgetItem(str(item.paragraph_count)))
            self.table.setItem(row, 4, QTableWidgetItem(str(item.table_count)))
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
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(360)
        layout.addWidget(self.log_view, 1)
        self._refresh_running_widgets()

    def _render_done_workspace(self, layout: QVBoxLayout) -> None:
        done = self.done
        layout.addWidget(_label("任务结果", "SectionTitle"))
        if done is None:
            layout.addWidget(QLabel("Word 翻译任务已完成。"))
            return
        success_count = sum(1 for item in done.file_results if item.get("success"))
        failure_count = len(done.file_results) - success_count
        layout.addLayout(
            self._build_result_kpis(
                [
                    ("成功文件", str(success_count)),
                    ("失败文件", str(failure_count)),
                    ("输出目录", done.output_dir),
                    ("耗时", f"{done.elapsed_sec:.1f}s"),
                    ("TM 命中", str(done.tm_hit_count)),
                    ("API 调用", str(done.api_call_count)),
                ]
            )
        )
        output = QLabel(done.output_dir)
        output.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        output.setObjectName("MutedText")
        layout.addWidget(output)
        table = QTableWidget(len(done.file_results), 3)
        table.setHorizontalHeaderLabels(["文件名", "状态", "详情"])
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setStretchLastSection(True)
        for row, result in enumerate(done.file_results):
            table.setItem(row, 0, QTableWidgetItem(str(result.get("name") or "")))
            table.setItem(row, 1, QTableWidgetItem("成功" if result.get("success") else "失败"))
            table.setItem(row, 2, QTableWidgetItem(str(result.get("error") or "")))
        table.resizeColumnsToContents()
        layout.addWidget(table, 1)

    def _render_error_workspace(self, layout: QVBoxLayout) -> None:
        layout.addWidget(_label("任务异常", "SectionTitle"))
        message = QLabel(self._latest_error_message() or "任务执行出错，请查看日志。")
        message.setWordWrap(True)
        layout.addWidget(message)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(360)
        layout.addWidget(self.log_view, 1)
        self._refresh_log_view()

    def _render_stopped_workspace(self, layout: QVBoxLayout) -> None:
        layout.addWidget(_label("任务已中止", "SectionTitle"))
        message = QLabel(self.stop_message or "任务已中止。")
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

    def _render_action_card(self) -> None:
        _clear_layout(self.action_layout)
        self.action_layout.addWidget(_label("执行操作", "SectionTitle"))
        if self.phase == "idle":
            selected = self._selected_files()
            lang_label = self._selected_target_label()
            note = QLabel(f"当前目标语言：{lang_label}；可执行文件：{len(selected)} / {len(self.files)}")
            note.setWordWrap(True)
            note.setObjectName("MutedText")
            self.action_layout.addWidget(note)
            start = QPushButton(f"开始翻译（{lang_label}）")
            start.setObjectName("PrimaryButton")
            _set_tooltip(start, "开始翻译", "按当前任务清单和 Word 参数启动翻译。")
            start.setEnabled(self._can_start())
            start.clicked.connect(self._start_translation)
            self.action_layout.addWidget(start)
        elif self.phase == "running":
            stop = QPushButton("终止翻译")
            stop.setObjectName("DangerButton")
            stop.clicked.connect(self._confirm_stop)
            self.action_layout.addWidget(stop)
        else:
            reset = QPushButton("返回并开始新任务")
            reset.setObjectName("PrimaryButton")
            reset.clicked.connect(self._reset_task)
            self.action_layout.addWidget(reset)
        history = QPushButton(
            "导出历史诊断归档" if count_diagnostic_records() > 0 else "暂无历史诊断"
        )
        history.setEnabled(count_diagnostic_records() > 0 and self.phase != "running")
        history.clicked.connect(self._export_history_diagnostics)
        self.action_layout.addWidget(history)
        self.action_layout.addStretch(1)

    def _browse_source(self) -> None:
        current = self.source_input.text().strip().strip('"')
        base_path = Path(current).expanduser() if current else Path.home()
        base = str(base_path if base_path.is_dir() else base_path.parent)
        choice = QMessageBox(self)
        choice.setWindowTitle("浏览源路径")
        choice.setText("请选择要扫描的源路径类型。")
        folder_button = choice.addButton("选择文件夹", QMessageBox.ButtonRole.AcceptRole)
        file_button = choice.addButton("选择 Word 文件", QMessageBox.ButtonRole.ActionRole)
        choice.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        choice.exec()
        selected = ""
        clicked = choice.clickedButton()
        if clicked == folder_button:
            selected = QFileDialog.getExistingDirectory(self, "选择源文件夹", base)
        elif clicked == file_button:
            selected, _ = QFileDialog.getOpenFileName(
                self,
                "选择 Word 文件",
                base,
                "Word 文件 (*.docx)",
            )
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
        if input_path.is_file() and not is_supported_word_file(input_path):
            QMessageBox.warning(self, APP_NAME, "不支持的文件类型：仅支持 .docx 文件。")
            return
        self.scan_button.setEnabled(False)
        self.scan_button.setText("扫描中...")
        self.scan_worker = WordScanWorker(raw_path, self)
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
        self.log_entries = []
        save_settings(self.settings)
        self._render_workspace()
        self._render_action_card()

    def _selected_files(self) -> list[WordFileItem]:
        table = getattr(self, "table", None)
        if table is None or table.rowCount() != len(self.files):
            return list(self.files)
        selected: list[WordFileItem] = []
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

    def _refresh_selection_summary(self) -> None:
        label = getattr(self, "selection_status_label", None)
        if label is not None:
            label.setText(f"已选 {len(self._selected_files())} / {len(self.files)}")

    def _configure_file_table_columns(self) -> None:
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for column in (2, 3, 4):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setColumnWidth(0, 58)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.table.verticalHeader().setDefaultSectionSize(34)

    def _schedule_file_table_height_refresh(self) -> None:
        self._refresh_file_table_height()
        QTimer.singleShot(0, self._refresh_file_table_height)

    def _refresh_file_table_height(self) -> None:
        table = getattr(self, "table", None)
        if table is None:
            return

        header_height = max(
            table.horizontalHeader().height(),
            table.horizontalHeader().sizeHint().height(),
            34,
        )
        row_height = max(table.verticalHeader().defaultSectionSize(), 34)
        full_height = header_height + row_height * table.rowCount() + 2 * table.frameWidth() + 6

        scroll = getattr(self, "workspace_scroll", None)
        viewport_height = scroll.viewport().height() if scroll is not None else self.height()
        frame = getattr(self, "workspace_frame", None)
        layout = frame.layout() if frame is not None else None
        reserved_height = 0
        spacing_count = 0
        if layout is not None:
            margins = layout.contentsMargins()
            reserved_height += margins.top() + margins.bottom()
            for index in range(layout.count()):
                item = layout.itemAt(index)
                widget = item.widget()
                child_layout = item.layout()
                if widget is table:
                    continue
                if widget is not None:
                    reserved_height += widget.sizeHint().height()
                    spacing_count += 1
                elif child_layout is not None:
                    reserved_height += child_layout.sizeHint().height()
                    spacing_count += 1
            reserved_height += max(0, spacing_count) * layout.spacing()

        available = viewport_height - reserved_height
        if available < 220:
            available = max(220, int(self.height() * 0.45))
        target_height = full_height if full_height <= available else max(220, int(available * 0.9))
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

    def _selected_source_lang(self) -> str:
        if (
            hasattr(self, "source_lang_combo")
            and self.source_lang_combo.currentIndex() >= 0
            and self.source_lang_combo.currentText().strip()
            != self.source_lang_combo.itemText(self.source_lang_combo.currentIndex()).strip()
        ):
            select_combo_text_match(self.source_lang_combo)
        return str(self.source_lang_combo.currentData() or self.settings.source_lang or "")

    def _selected_target_label(self) -> str:
        target_lang = (
            str(self.target_combo.currentData())
            if hasattr(self, "target_combo")
            else self.settings.target_lang
        )
        if not is_supported_target_lang(
            target_lang,
            self.settings.custom_target_langs,
            include_optional=True,
        ):
            return "未选择"
        return get_target_lang_display(
            target_lang,
            self.settings.custom_target_langs,
            include_optional=True,
        )

    def _can_start(self) -> bool:
        if self.phase != "idle" or not self._selected_files():
            return False
        if not self._selected_target_lang() or not self._selected_source_lang():
            return False
        if self.settings.output.use_custom_output_dir:
            return get_custom_output_dir_error(self.settings.output.custom_output_dir) is None
        return True

    def _on_file_selection_changed(self) -> None:
        self._refresh_selection_summary()
        self._refresh_header()
        self._render_action_card()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API name.
        super().resizeEvent(event)
        QTimer.singleShot(0, self._refresh_file_table_height)

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
        self._sync_source_lang_visibility()
        save_settings(self.settings)
        self._refresh_header()
        self._render_action_card()
        self._emit_language_changed()

    def _on_source_lang_changed(self) -> None:
        source_lang = self._selected_source_lang()
        if source_lang:
            self.settings.source_lang = source_lang
            save_settings(self.settings)
        self._render_action_card()
        self._emit_language_changed()

    def _emit_language_changed(self) -> None:
        target_lang = self._selected_target_lang()
        source_lang = self._selected_source_lang()
        if target_lang and source_lang:
            self.languageChanged.emit(target_lang, source_lang)

    def _on_params_changed(self) -> None:
        self.settings.word_review.highlight_unresolved = self.highlight_check.isChecked()
        self.settings.word_batch.max_paragraphs_per_batch = self.batch_paragraphs_spin.value()
        self.settings.word_batch.max_chars_per_batch = self.batch_chars_spin.value()
        self.settings.word_batch.split_paragraph_chars = self.split_chars_spin.value()
        self.settings.word_batch.strict_retry_attempts = self.retry_spin.value()
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

    def _sync_source_lang_visibility(self) -> None:
        self.source_lang_label.setVisible(True)
        self.source_lang_combo.setVisible(True)
        self.source_lang_combo.setEnabled(self.phase != "running")

    def _lock_inputs(self, locked: bool) -> None:
        for widget in (
            self.source_input,
            self.browse_button,
            self.scan_button,
            self.target_combo,
            self.source_lang_combo,
            self.output_default_radio,
            self.output_custom_radio,
            self.custom_output_input,
            self.highlight_check,
            self.batch_paragraphs_spin,
            self.batch_chars_spin,
            self.split_chars_spin,
            self.retry_spin,
        ):
            widget.setEnabled(not locked)
        self._sync_source_lang_visibility()

    def _start_translation(self) -> None:
        selected = self._selected_files()
        if not selected:
            QMessageBox.warning(self, APP_NAME, "请先扫描并选择至少一个 Word 文件。")
            return
        target_lang = self._selected_target_lang()
        source_lang = self._selected_source_lang()
        if not target_lang or not source_lang:
            QMessageBox.warning(self, APP_NAME, "请先选择目标语言和源语言。")
            return
        config_check = check_translation_api_config(self.settings)
        if not config_check.ok:
            detail = f"\n{config_check.detail}" if config_check.detail else ""
            QMessageBox.warning(self, APP_NAME, f"{config_check.message}{detail}")
            return
        self.settings.target_lang = target_lang
        self.settings.source_lang = source_lang
        self._on_output_changed()
        self._on_params_changed()
        self.runner = WordTaskRunner(
            selected,
            self.settings,
            source_root=self.source_root or None,
            source_lang=source_lang,
        )
        self.runner.start()
        self.phase = "running"
        self.log_entries = []
        self.progress = None
        self.status_text = ""
        self.done = None
        self.stop_message = ""
        self._lock_inputs(True)
        self._render_workspace()
        self._render_action_card()
        self.poll_timer.start()

    def _confirm_stop(self) -> None:
        if self.runner is None:
            return
        answer = QMessageBox.question(
            self,
            APP_NAME,
            "确认终止当前 Word 翻译任务？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.runner.stop()
            self.status_text = "正在停止任务，请等待当前批次结束..."
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
                self.log_entries.append(
                    {"level": msg.level, "message": msg.message, "ts": msg.ts}
                )
            elif isinstance(msg, ProgressMsg):
                self.progress = msg
            elif isinstance(msg, StatusMsg):
                self.status_text = msg.phase_desc
            elif isinstance(msg, DoneMsg):
                self.done = msg
                self.runner = None
                self.phase = "done"
                self.poll_timer.stop()
                self._lock_inputs(False)
                self._render_workspace()
                self._render_action_card()
                return
            elif isinstance(msg, ErrorMsg):
                self.log_entries.append({"level": "ERROR", "message": msg.message, "ts": ""})
                self.runner = None
                self.phase = "error"
                self.poll_timer.stop()
                self._lock_inputs(False)
                self._render_workspace()
                self._render_action_card()
                return
            elif isinstance(msg, StoppedMsg):
                self.log_entries.append({"level": "WARN", "message": msg.message, "ts": ""})
                self.stop_message = msg.message
                self.runner = None
                self.phase = "stopped"
                self.poll_timer.stop()
                self._lock_inputs(False)
                self._render_workspace()
                self._render_action_card()
                return
        if not runner.needs_poll():
            self.runner = None
            self.poll_timer.stop()
        self._refresh_running_widgets()

    def _refresh_running_widgets(self) -> None:
        if not hasattr(self, "running_status"):
            return
        progress = self.progress
        if progress is None:
            self.running_status.setText(self.status_text or "初始化中，请稍候...")
            self.progress_bar.setValue(0)
        else:
            overall = self._calc_overall_progress(progress)
            self.progress_bar.setValue(int(overall * 100))
            status = self.status_text.replace("状态：", "", 1).strip()
            self.running_status.setText(
                f"阶段 {progress.phase_index} / {progress.phase_total} | "
                f"{progress.phase_name} | {progress.step_done} / {progress.step_total}"
                + (f"\n{status}" if status else "")
            )
        self._refresh_log_view()

    def _refresh_log_view(self) -> None:
        if not hasattr(self, "log_view"):
            return
        lines = []
        for item in self.log_entries[-300:]:
            prefix = f"[{item.get('ts')}]" if item.get("ts") else ""
            lines.append(f"{prefix} {item.get('level', '')} {item.get('message', '')}".strip())
        self.log_view.setPlainText("\n".join(lines))
        self.log_view.moveCursor(QTextCursor.MoveOperation.End)

    def _calc_overall_progress(self, progress: ProgressMsg) -> float:
        weights = {1: 0.12, 2: 0.70, 3: 0.18}
        offset = 0.0
        for phase_index in range(1, progress.phase_index):
            offset += weights.get(phase_index, 0.0)
        current = progress.step_done / max(progress.step_total, 1)
        return min(offset + current * weights.get(progress.phase_index, 0.0), 1.0)

    def _latest_error_message(self) -> str:
        errors = [
            item.get("message", "")
            for item in self.log_entries
            if item.get("level") == "ERROR"
        ]
        return errors[-1] if errors else ""

    def _reset_task(self) -> None:
        self.phase = "idle"
        self.files = []
        self.done = None
        self.runner = None
        self.progress = None
        self.status_text = ""
        self.stop_message = ""
        self.log_entries = []
        self._lock_inputs(False)
        self._render_workspace()
        self._render_action_card()

    def _export_history_diagnostics(self) -> None:
        try:
            data, filename, _ = build_diagnostics_history_zip_bytes()
        except Exception as exc:  # noqa: BLE001 - converted to UI message.
            QMessageBox.warning(self, APP_NAME, f"读取历史诊断失败：{exc}")
            return
        target, _ = QFileDialog.getSaveFileName(
            self,
            "导出历史诊断归档",
            filename,
            "Zip 文件 (*.zip)",
        )
        if target:
            Path(target).write_bytes(data)
            QMessageBox.information(self, APP_NAME, f"已导出：{target}")
