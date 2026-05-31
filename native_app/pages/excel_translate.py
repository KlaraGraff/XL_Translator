"""Native Excel translation page."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QTextCursor
from PySide6.QtWidgets import (
    QFileDialog,
    QCheckBox,
    QColorDialog,
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
    QSizePolicy,
    QTableWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app_meta import APP_NAME
from config import (
    REVIEW_MARK_COLOR_FOREIGN_NOISE_DEFAULT,
    REVIEW_MARK_COLOR_SEMANTIC_DEFAULT,
    REVIEW_MARK_COLOR_UNRESOLVED_DEFAULT,
)
from core.api_config_check import check_translation_api_config
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
from core.file_scanner import FileItem, is_supported_excel_file
from core.language_registry import (
    get_ordered_target_lang_codes,
    get_target_lang_display,
    is_supported_target_lang,
    remember_recent_target_lang,
)
from core.mixed_language import (
    MIXED_MARK_FOREIGN_NOISE,
    MIXED_MARK_SEMANTIC,
    MIXED_MARK_UNRESOLVED,
)
from core.model_api_identity import ApiGroupSignature, task_api_context_for_page
from core.task_runner import (
    DoneMsg,
    ErrorMsg,
    LogMsg,
    ProgressMsg,
    StatusMsg,
    StoppedMsg,
    TaskRunner,
)
from core.xls_converter import get_local_excel_availability
from native_app.workers import ScanWorker
from native_app.result_view import (
    build_done_summary,
    format_elapsed,
    render_translation_result,
)
from native_app.widgets import (
    build_app_tooltip_html,
    configure_app_table,
    configure_file_selection_table,
    create_check_table_item,
    create_elide_table_item,
    create_searchable_combo,
    create_table_item,
    MiddleElideLabel,
    MiddleElideLineEdit,
    is_live_widget,
    refresh_combo_completer,
    select_combo_text_match,
)
from settings import AppSettings, save_settings

_CUSTOM_COLOR_VALUE = "__custom__"
_REVIEW_COLOR_PRESETS = (
    ("浅黄", "FFF2CC"),
    ("浅红", "F4CCCC"),
    ("浅橙", "FCE4D6"),
    ("浅蓝", "DDEBFF"),
    ("浅绿", "D9EAD3"),
)
_REVIEW_COLOR_ROWS = (
    (MIXED_MARK_SEMANTIC, "语义校验接受", REVIEW_MARK_COLOR_SEMANTIC_DEFAULT),
    (MIXED_MARK_UNRESOLVED, "保留原文复核", REVIEW_MARK_COLOR_UNRESOLVED_DEFAULT),
    (MIXED_MARK_FOREIGN_NOISE, "疑似原文异常", REVIEW_MARK_COLOR_FOREIGN_NOISE_DEFAULT),
)


def _normalize_color_hex(value: str, fallback: str) -> str:
    cleaned = str(value or "").strip().lstrip("#").upper()
    if len(cleaned) == 6 and all(char in "0123456789ABCDEF" for char in cleaned):
        return cleaned
    return fallback


def _review_default_color(mark: str) -> str:
    for row_mark, _label, default_color in _REVIEW_COLOR_ROWS:
        if row_mark == mark:
            return default_color
    return REVIEW_MARK_COLOR_SEMANTIC_DEFAULT


def _contrast_text_color(color: str) -> str:
    normalized = _normalize_color_hex(color, "FFF2CC")
    red = int(normalized[0:2], 16)
    green = int(normalized[2:4], 16)
    blue = int(normalized[4:6], 16)
    luminance = (red * 299 + green * 587 + blue * 114) / 1000
    return "#111827" if luminance >= 150 else "#FFFFFF"


HEADER_TILE_HEIGHT = 48
HEADER_TILE_MIN_WIDTH = 86
HEADER_SOURCE_MIN_WIDTH = 300
HEADER_SOURCE_MAX_WIDTH = 430
KPI_TILE_HEIGHT = 76
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


class ExcelTranslatePage(QWidget):
    """Qt implementation of the Excel translation workspace."""

    languageChanged = Signal(str, str)

    def __init__(self, settings: AppSettings):
        super().__init__()
        self.settings = settings
        self.phase = "idle"
        self.files: list[FileItem] = []
        self.source_root = settings.last_excel_source_folder
        self.runner: TaskRunner | None = None
        self.scan_worker: ScanWorker | None = None
        self.log_entries: list[dict[str, str]] = []
        self.diagnostic_log_entries: list[dict[str, str]] = []
        self._visible_log_chars = 0
        self._diagnostic_log_chars = 0
        self.progress: ProgressMsg | None = None
        self.status_text = ""
        self.done: DoneMsg | None = None
        self.stop_message = ""
        self.task_files: list[FileItem] = []
        self.current_task_source_root = ""
        self.current_task_id = ""
        self.current_task_api_groups: set[ApiGroupSignature] = set()
        self._task_diagnostics_archived = False
        self._workspace_render_phase = self.phase
        self.external_task_lock = False
        self.external_task_owner_label = ""
        self.external_task_lock_reason = ""

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

    def showEvent(self, event) -> None:  # noqa: N802 - Qt API name.
        super().showEvent(event)
        self.set_page_active(True)

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt API name.
        self.set_page_active(False)
        super().hideEvent(event)

    def set_page_active(self, active: bool) -> None:
        if active:
            if self.runner is not None:
                self._start_ui_sync_guard()
            self._refresh_header()
            self._render_action_card()
        else:
            self._stop_ui_sync_guard()

    def set_external_task_lock(
        self,
        locked: bool,
        owner_label: str = "",
        reason: str = "",
    ) -> None:
        locked = bool(locked)
        owner_label = str(owner_label or "").strip()
        reason = str(reason or "").strip()
        if (
            self.external_task_lock == locked
            and self.external_task_owner_label == owner_label
            and self.external_task_lock_reason == reason
        ):
            return
        self.external_task_lock = locked
        self.external_task_owner_label = owner_label
        self.external_task_lock_reason = reason
        self._lock_inputs(self.phase == "running")
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
        self.side_layout = side_layout
        self.action_card, self.action_layout = _card()
        side_layout.addWidget(self.action_card)
        self._build_output_card(side_layout)
        self._build_params_card(side_layout)
        side_layout.addStretch(1)
        side.setWidget(side_content)
        body.addWidget(side)

    def _refresh_header(self) -> None:
        _clear_layout(self.header_layout)
        self.header_layout.addWidget(_label("Excel Workspace", "PageEyebrow"))

        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        title_row.addWidget(_label("Excel 翻译", "PageTitle"))
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
        title_row.addWidget(
            self._phase_badge(),
            alignment=Qt.AlignmentFlag.AlignTop,
        )
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
        self.source_input = MiddleElideLineEdit(self.settings.last_excel_source_folder)
        self.source_input.setPlaceholderText("输入文件夹或 Excel 文件绝对路径")
        _set_tooltip(
            self.source_input,
            "源路径",
            "指定待翻译的 Excel 文件或文件夹。",
            [
                "文件夹会递归扫描 .xlsx 和 .xls 文件。",
                "单个文件仅扫描该文件。",
            ],
        )
        row.addWidget(self.source_input, 1)

        self.browse_button = QPushButton("浏览")
        _set_tooltip(
            self.browse_button,
            "浏览",
            "选择源文件夹或 Excel 文件。",
        )
        self.browse_button.clicked.connect(self._browse_source)
        row.addWidget(self.browse_button)

        self.scan_button = QPushButton("扫描")
        _set_tooltip(
            self.scan_button,
            "扫描",
            "扫描源路径并生成任务清单。",
        )
        self.scan_button.clicked.connect(self._scan_source)
        row.addWidget(self.scan_button)
        layout.addLayout(row)
        root.addWidget(frame)

    def _build_output_card(self, side_layout: QVBoxLayout) -> None:
        frame, layout = _card()
        self.output_card = frame
        side_layout.addWidget(frame)
        layout.addWidget(_label("输出位置", "SectionTitle"))

        self.output_default_radio = QRadioButton("源目录内")
        self.output_custom_radio = QRadioButton("自定义目录")
        _set_tooltip(
            self.output_default_radio,
            "源目录内",
            "在源路径同级位置创建输出目录。",
            ["目录名自动附加时间戳。"],
        )
        _set_tooltip(
            self.output_custom_radio,
            "自定义目录",
            "将结果写入指定目录。",
            ["目录不存在时会自动创建。"],
        )
        self.output_custom_radio.setChecked(self.settings.output.use_custom_output_dir)
        self.output_default_radio.setChecked(not self.settings.output.use_custom_output_dir)
        self.output_default_radio.toggled.connect(self._on_output_changed)
        self.output_custom_radio.toggled.connect(self._on_output_changed)
        layout.addWidget(self.output_default_radio)
        layout.addWidget(self.output_custom_radio)

        self.custom_output_input = MiddleElideLineEdit(self.settings.output.custom_output_dir)
        self.custom_output_input.setPlaceholderText("输入输出目录")
        _set_tooltip(
            self.custom_output_input,
            "自定义输出目录",
            "指定翻译结果保存位置。",
        )
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

        self.target_lang_label = _field_label(
            "目标语言",
            "目标语言",
            "选择译文语言。",
            ["可与源语言独立设置。"],
        )
        layout.addWidget(self.target_lang_label)
        self.target_combo = create_searchable_combo()
        if self.target_combo.lineEdit() is not None:
            self.target_combo.lineEdit().setPlaceholderText("筛选语言")
        self._load_target_options()
        _set_tooltip(
            self.target_combo,
            "目标语言",
            "选择输出语言。",
            ["可输入名称快速筛选。"],
        )
        self.target_combo.currentIndexChanged.connect(self._on_target_changed)
        layout.addWidget(self.target_combo)

        self.source_lang_label = _field_label(
            "源语言",
            "源语言",
            "选择原文语言。",
        )
        layout.addWidget(self.source_lang_label)
        self.source_lang_combo = create_searchable_combo()
        if self.source_lang_combo.lineEdit() is not None:
            self.source_lang_combo.lineEdit().setPlaceholderText("筛选语言")
        self._load_source_options()
        _set_tooltip(
            self.source_lang_combo,
            "源语言",
            "选择源文件的主要语言。",
        )
        self.source_lang_combo.currentIndexChanged.connect(self._on_source_lang_changed)
        layout.addWidget(self.source_lang_combo)

        self.keep_original_check = self._checkbox(
            "保留原始表格",
            self.settings.output.keep_original_sheets,
            layout,
        )
        self.formula_backfill_check = self._checkbox(
            "公式文本按显示值回填",
            self.settings.output.formula_display_value_backfill,
            layout,
        )
        self.excel_autofit_check = self._checkbox(
            "Excel 精调行高",
            self.settings.output.enable_excel_autofit,
            layout,
        )
        self.lock_row_height_check = self._checkbox(
            "锁定行高，缩小字号",
            self.settings.output.lock_row_height,
            layout,
        )
        self.review_color_combos: dict[str, QComboBox] = {}
        self.review_color_inputs: dict[str, QLineEdit] = {}
        self.review_color_buttons: dict[str, QPushButton] = {}
        self._build_review_color_controls(layout)
        layout.addWidget(_field_label(
            "已有底色处理",
            "已有底色处理",
            "设置风险标记遇到原单元格已有底色时的处理方式。",
        ))
        self.existing_fill_policy_combo = QComboBox()
        self.existing_fill_policy_combo.addItem("不覆盖原有底色", "skip")
        self.existing_fill_policy_combo.addItem("覆盖原有底色", "overwrite")
        self.existing_fill_policy_combo.addItem("保留原有底色并使用红字", "red_font")
        policy_index = self.existing_fill_policy_combo.findData(
            self.settings.excel_review.existing_fill_policy
        )
        self.existing_fill_policy_combo.setCurrentIndex(max(0, policy_index))
        _set_tooltip(
            self.existing_fill_policy_combo,
            "已有底色处理",
            "风险单元格无底色时会直接填充风险色；已有底色时按此策略处理。",
        )
        self.existing_fill_policy_combo.currentIndexChanged.connect(self._on_params_changed)
        layout.addWidget(self.existing_fill_policy_combo)
        self._sync_source_lang_visibility()

    def _build_review_color_controls(self, layout: QVBoxLayout) -> None:
        layout.addWidget(
            _field_label(
                "标记颜色",
                "标记颜色",
                "设置输出表格中复核和异常标记使用的填充颜色。",
                [
                    "下拉选项会直接显示对应高亮底色。",
                    "选择自定义后可点击选色按钮，也可输入 #RRGGBB 色值。",
                ],
            )
        )
        for mark, label, default_color in _REVIEW_COLOR_ROWS:
            row = QHBoxLayout()
            row.setSpacing(8)

            name_label = QLabel(label)
            name_label.setObjectName("FieldHint")
            name_label.setMinimumWidth(88)
            row.addWidget(name_label)

            current_color = self.settings.word_review.mark_colors.get(mark, default_color)
            combo = QComboBox()
            combo.setMinimumWidth(116)
            self._populate_review_color_combo(combo, default_color, current_color)
            combo.currentIndexChanged.connect(
                lambda _index=0, key=mark: self._on_review_color_combo_changed(key)
            )
            _set_tooltip(
                combo,
                label,
                "选择该类标记写入输出文件时使用的填充颜色。",
            )
            row.addWidget(combo, 1)

            custom_input = QLineEdit()
            custom_input.setPlaceholderText("#RRGGBB")
            custom_input.setMaxLength(7)
            custom_input.editingFinished.connect(
                lambda key=mark: self._on_review_custom_color_finished(key)
            )
            _set_tooltip(
                custom_input,
                "自定义色值",
                "输入 6 位十六进制颜色，例如 #DDEBFF。",
            )
            row.addWidget(custom_input, 1)

            pick_button = QPushButton("选色")
            pick_button.setMinimumWidth(56)
            pick_button.clicked.connect(
                lambda _checked=False, key=mark: self._choose_review_custom_color(key)
            )
            _set_tooltip(
                pick_button,
                "选色",
                "打开系统颜色选择器设置自定义标记颜色。",
            )
            row.addWidget(pick_button)

            self.review_color_combos[mark] = combo
            self.review_color_inputs[mark] = custom_input
            self.review_color_buttons[mark] = pick_button
            self._refresh_review_color_control(mark)
            layout.addLayout(row)

    def _populate_review_color_combo(
        self,
        combo: QComboBox,
        default_color: str,
        current_color: str,
    ) -> None:
        combo.clear()
        normalized_default = _normalize_color_hex(default_color, default_color)

        for label, color in _REVIEW_COLOR_PRESETS:
            normalized = _normalize_color_hex(color, normalized_default)
            default_suffix = "（默认）" if normalized == normalized_default else ""
            combo.addItem(f"{label}{default_suffix} #{normalized}", normalized)
            self._set_combo_item_highlight(combo, combo.count() - 1, normalized)

        normalized_current = _normalize_color_hex(current_color, normalized_default)
        combo.addItem(f"自定义 #{normalized_current}", _CUSTOM_COLOR_VALUE)
        self._set_combo_item_highlight(combo, combo.count() - 1, normalized_current)

        for index in range(combo.count()):
            if combo.itemData(index) == normalized_current:
                combo.setCurrentIndex(index)
                return
        combo.setCurrentIndex(combo.count() - 1)

    def _set_combo_item_highlight(
        self,
        combo: QComboBox,
        index: int,
        color: str,
    ) -> None:
        normalized = _normalize_color_hex(color, "FFF2CC")
        qcolor = QColor(f"#{normalized}")
        combo.setItemData(index, QBrush(qcolor), Qt.ItemDataRole.BackgroundRole)
        combo.setItemData(
            index,
            QBrush(QColor(_contrast_text_color(normalized))),
            Qt.ItemDataRole.ForegroundRole,
        )

    def _on_review_color_combo_changed(self, mark: str) -> None:
        self._refresh_review_color_control(mark)
        self._on_params_changed()

    def _on_review_custom_color_finished(self, mark: str) -> None:
        combo = self.review_color_combos.get(mark)
        if combo is not None and combo.currentData() != _CUSTOM_COLOR_VALUE:
            custom_index = combo.findData(_CUSTOM_COLOR_VALUE)
            if custom_index >= 0:
                combo.setCurrentIndex(custom_index)
        self._on_params_changed()

    def _choose_review_custom_color(self, mark: str) -> None:
        combo = self.review_color_combos.get(mark)
        custom_input = self.review_color_inputs.get(mark)
        if combo is None or custom_input is None:
            return

        initial = QColor(f"#{self._selected_review_color(mark)}")
        selected = QColorDialog.getColor(initial, self, "选择标记颜色")
        if not selected.isValid():
            return

        custom_index = combo.findData(_CUSTOM_COLOR_VALUE)
        if custom_index >= 0 and combo.currentData() != _CUSTOM_COLOR_VALUE:
            combo.blockSignals(True)
            combo.setCurrentIndex(custom_index)
            combo.blockSignals(False)
        custom_input.setText(selected.name().upper())
        self._refresh_review_color_control(mark)
        self._on_params_changed()

    def _refresh_review_color_control(self, mark: str) -> None:
        combo = self.review_color_combos.get(mark)
        custom_input = self.review_color_inputs.get(mark)
        pick_button = self.review_color_buttons.get(mark)
        if combo is None or custom_input is None or pick_button is None:
            return

        color = self._selected_review_color(mark)
        is_custom = combo.currentData() == _CUSTOM_COLOR_VALUE
        controls_enabled = self.phase != "running" and not self.external_task_lock
        custom_input.setVisible(is_custom)
        custom_input.setEnabled(is_custom and controls_enabled)
        pick_button.setVisible(is_custom)
        pick_button.setEnabled(is_custom and controls_enabled)
        custom_input.setText(f"#{color}")
        custom_index = combo.findData(_CUSTOM_COLOR_VALUE)
        if custom_index >= 0:
            combo.setItemText(custom_index, f"自定义 #{color}")
            self._set_combo_item_highlight(combo, custom_index, color)
        self._apply_review_color_preview(combo, color)
        self._apply_review_color_preview(custom_input, color)
        self._apply_review_color_preview(pick_button, color)

    def _selected_review_color(self, mark: str) -> str:
        default_color = _review_default_color(mark)
        combo = self.review_color_combos.get(mark)
        custom_input = self.review_color_inputs.get(mark)
        if combo is None:
            return default_color
        if combo.currentData() == _CUSTOM_COLOR_VALUE:
            raw_color = custom_input.text() if custom_input is not None else ""
            return _normalize_color_hex(raw_color, default_color)
        return _normalize_color_hex(str(combo.currentData() or ""), default_color)

    def _sync_review_color_settings(self) -> None:
        if not getattr(self, "review_color_combos", None):
            return
        for mark, _label, default_color in _REVIEW_COLOR_ROWS:
            self.settings.word_review.mark_colors[mark] = _normalize_color_hex(
                self._selected_review_color(mark),
                default_color,
            )
            self._refresh_review_color_control(mark)

    def _apply_review_color_preview(self, widget: QWidget, color: str) -> None:
        normalized = _normalize_color_hex(color, "FFF2CC")
        text_color = _contrast_text_color(normalized)
        widget.setStyleSheet(
            f"background-color: #{normalized}; "
            f"color: {text_color}; "
            f"selection-background-color: #{normalized};"
        )

    def _checkbox(self, text: str, checked: bool, layout: QVBoxLayout) -> QCheckBox:
        checkbox = QCheckBox(text)
        checkbox.setToolTip(
            {
                "保留原始表格": _tooltip(
                    "保留原始表格",
                    "在输出文件中保留原始工作表。",
                    [
                        "便于对照审校。",
                        "关闭后仅保留翻译结果。",
                    ],
                ),
                "公式文本按显示值回填": _tooltip(
                    "公式文本按显示值回填",
                    "按公式显示值匹配并写入译文。",
                    [
                        "命中后会以静态文本替换原公式。",
                        "关闭后保留原公式。",
                    ],
                ),
                "Excel 精调行高": _tooltip(
                    "Excel 精调行高",
                    "翻译完成后调用本地 Excel 调整行高。",
                    ["与“锁定行高，缩小字号”互斥。"],
                ),
                "锁定行高，缩小字号": _tooltip(
                    "锁定行高，缩小字号",
                    "保持行高不变，通过缩小字号容纳译文。",
                    [
                        "与“Excel 精调行高”互斥。",
                    ],
                ),
            }.get(text, "")
        )
        checkbox.setChecked(checked)
        checkbox.toggled.connect(self._on_params_changed)
        layout.addWidget(checkbox)
        return checkbox

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
        value_widget = MiddleElideLabel(value) if max_width is not None else QLabel(value)
        value_widget.setObjectName("PillValue")
        value_widget.setWordWrap(False)
        if max_width is not None:
            value_widget.setMaximumWidth(max(1, max_width - 16))
        else:
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
            placeholder = QLabel(
                "输入或选择源路径后扫描，生成可处理文件列表。"
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
        _set_tooltip(select_all, "全选", "选择全部 Excel 文件。")
        select_all.clicked.connect(lambda: self._set_all_file_selection(True))
        title_row.addWidget(select_all)
        deselect_all = QPushButton("全不选")
        _set_tooltip(deselect_all, "全不选", "取消选择全部 Excel 文件。")
        deselect_all.clicked.connect(lambda: self._set_all_file_selection(False))
        title_row.addWidget(deselect_all)
        layout.addLayout(title_row)

        layout.addLayout(self._build_kpi_strip())
        self.table = QTableWidget(len(self.files), 4)
        self.table.setHorizontalHeaderLabels(["选择", "文件名", "大小", "分表数"])
        configure_app_table(self.table, row_height=38)
        self.table.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        self._configure_file_table_columns()
        for row, item in enumerate(self.files):
            self.table.setItem(row, 0, create_check_table_item())
            self.table.setItem(row, 1, create_elide_table_item(item.name))
            self.table.setItem(row, 2, create_table_item(f"{item.size_kb:.1f} KB"))
            self.table.setItem(row, 3, create_table_item(len(item.sheets)))
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
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(360)
        layout.addWidget(self.log_view, 1)
        self._refresh_running_widgets()

    def _render_done_workspace(self, layout: QVBoxLayout) -> None:
        done = self.done
        if done is None:
            render_translation_result(
                layout,
                empty_message="Excel 翻译任务已完成。",
                done=None,
                summary_text="",
                summary_success=True,
                kpi_items=[],
                file_status_formatter=self._format_file_result_status,
            )
            return

        generated_count = sum(1 for item in done.file_results if item.get("success"))
        failure_count = len(done.file_results) - generated_count
        summary_success = failure_count == 0
        summary_text = (
            "翻译成功。"
            if summary_success
            else build_done_summary(
                generated_count=generated_count,
                failed_count=failure_count,
            )
        )
        render_translation_result(
            layout,
            empty_message="Excel 翻译任务已完成。",
            done=done,
            summary_text=summary_text,
            summary_success=summary_success,
            kpi_items=[
                ("已生成文件", str(generated_count)),
                ("生成失败", str(failure_count)),
                ("耗时", format_elapsed(done.elapsed_sec)),
                ("TM 命中", str(done.tm_hit_count)),
                ("API 翻译", str(done.api_call_count)),
            ],
            file_status_formatter=self._format_file_result_status,
            file_status_width=112,
            file_detail_width=180,
        )

    def _format_file_result_status(self, result: dict) -> str:
        return "已生成 / 成功" if result.get("success") else "生成失败"

    def _render_error_workspace(self, layout: QVBoxLayout) -> None:
        layout.addWidget(_label("任务异常", "SectionTitle"))
        error = self._latest_error_message() or "任务执行出错，请查看日志。"
        message = QLabel(error)
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

    def _build_kpi_strip(self) -> QHBoxLayout:
        selected = len(self._selected_files())
        total_sheets = sum(len(item.sheets) for item in self.files)
        xls_count = sum(1 for item in self.files if item.path.suffix.lower() == ".xls")
        return self._build_result_kpis(
            [
                ("已扫描文件", str(len(self.files))),
                ("已选任务", str(selected)),
                ("总分表数", str(total_sheets)),
                (".xls 文件", str(xls_count)),
            ]
        )

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

    def _render_action_card(self) -> None:
        self._normalize_terminal_state()
        _clear_layout(self.action_layout)
        self.action_layout.addWidget(_label("执行操作", "SectionTitle"))

        if self.phase == "idle":
            selected = self._selected_files()
            lang_label = self._selected_target_label()
            if self.external_task_lock:
                owner = self.external_task_owner_label or "其他翻译"
                note_text = (
                    self.external_task_lock_reason
                    or f"{owner}任务正在运行，请先完成或终止当前任务。"
                )
            else:
                note_text = f"目标语言：{lang_label}；可执行文件：{len(selected)} / {len(self.files)}"
            note = QLabel(note_text)
            note.setWordWrap(True)
            note.setObjectName("MutedText")
            self.action_layout.addWidget(note)

            start = QPushButton(f"开始翻译（{lang_label}）")
            start.setObjectName("PrimaryButton")
            _set_tooltip(
                start,
                "开始翻译",
                "按当前设置启动翻译。",
                ["启动前会检查模型配置和 .xls 兼容条件。"],
            )
            start.setEnabled(self._can_start())
            start.clicked.connect(self._start_translation)
            self.action_layout.addWidget(start)
        elif self._has_running_task():
            note = QLabel("任务运行中，参数已锁定。")
            note.setWordWrap(True)
            note.setObjectName("MutedText")
            self.action_layout.addWidget(note)
            stop = QPushButton("终止翻译")
            stop.setObjectName("DangerButton")
            _set_tooltip(
                stop,
                "终止翻译",
                "停止当前后台任务。",
                ["当前批次结束后退出。"],
            )
            stop.clicked.connect(self._confirm_stop)
            self.action_layout.addWidget(stop)
        else:
            reset = QPushButton("返回并开始新任务")
            reset.setObjectName("PrimaryButton")
            _set_tooltip(reset, "返回并开始新任务", "返回待执行状态。")
            reset.clicked.connect(self._reset_task)
            self.action_layout.addWidget(reset)

        history = QPushButton(
            "导出历史诊断归档"
            if count_diagnostic_records() > 0
            else "暂无历史诊断"
        )
        history.setEnabled(count_diagnostic_records() > 0 and not self._has_running_task())
        _set_tooltip(
            history,
            "历史诊断归档",
            "导出已保存的诊断包。",
            ["不包含原始文件和 API Key。"],
        )
        history.clicked.connect(self._export_history_diagnostics)
        self.action_layout.addWidget(history)
        self.action_layout.addStretch(1)
        self.action_card.updateGeometry()
        self.action_card.update()

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
        return self.done is None and self.runner is not None

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

    def _schedule_action_card_resync(self) -> None:
        self._start_ui_sync_guard()
        QTimer.singleShot(0, self._render_action_card)
        QTimer.singleShot(150, self._render_action_card)

    def _browse_source(self) -> None:
        current = self.source_input.text().strip().strip('"')
        base_path = Path(current).expanduser() if current else Path.home()
        base = str(base_path if base_path.is_dir() else base_path.parent)

        choice = QMessageBox(self)
        choice.setWindowTitle("浏览源路径")
        choice.setText("请选择要扫描的源路径类型。")
        folder_button = choice.addButton("选择文件夹", QMessageBox.ButtonRole.AcceptRole)
        file_button = choice.addButton("选择 Excel 文件", QMessageBox.ButtonRole.ActionRole)
        choice.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        choice.exec()

        selected = ""
        clicked = choice.clickedButton()
        if clicked == folder_button:
            selected = QFileDialog.getExistingDirectory(self, "选择源文件夹", base)
        elif clicked == file_button:
            selected, _ = QFileDialog.getOpenFileName(
                self,
                "选择 Excel 文件",
                base,
                "Excel 文件 (*.xlsx *.xls)",
            )
        else:
            return
        if selected:
            self.source_input.setText(selected)
            self._scan_source()

    def _on_file_selection_changed(self) -> None:
        self._refresh_selection_summary()
        self._refresh_header()
        self._render_action_card()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API name.
        super().resizeEvent(event)
        QTimer.singleShot(0, self._refresh_file_table_height)

    def _scan_source(self) -> None:
        raw_path = self.source_input.text().strip().strip('"')
        if not raw_path:
            QMessageBox.warning(self, APP_NAME, "请输入文件夹或文件路径。")
            return
        input_path = Path(raw_path).expanduser()
        if not input_path.exists():
            QMessageBox.warning(self, APP_NAME, f"路径不存在：{raw_path}")
            return
        if input_path.is_file() and not is_supported_excel_file(input_path):
            QMessageBox.warning(self, APP_NAME, "不支持的文件类型：仅支持 .xlsx / .xls 文件。")
            return

        self.scan_button.setEnabled(False)
        self.scan_button.setText("扫描中...")
        self.scan_worker = ScanWorker(raw_path, self)
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
        self.settings.last_excel_source_folder = self.source_input.text().strip().strip('"')
        self.phase = "idle"
        self.done = None
        self.task_files = []
        self.current_task_id = ""
        self._task_diagnostics_archived = False
        self._reset_runtime_logs()
        save_settings(self.settings)
        self._render_workspace()
        self._render_action_card()

    def _selected_files(self) -> list[FileItem]:
        table = getattr(self, "table", None)
        if table is None or table.rowCount() != len(self.files):
            return list(self.files)
        selected: list[FileItem] = []
        for row, item in enumerate(self.files):
            check = table.item(row, 0)
            if check is None or check.checkState() == Qt.CheckState.Checked:
                selected.append(item)
        return selected

    def _reset_runtime_logs(self) -> None:
        self.log_entries = []
        self.diagnostic_log_entries = []
        self._visible_log_chars = 0
        self._diagnostic_log_chars = 0

    def _append_runtime_log(self, level: str, message: str, ts: str = "") -> None:
        entry = {"level": str(level or ""), "message": str(message or ""), "ts": str(ts or "")}
        self._append_bounded_log(
            self.log_entries,
            entry,
            "_visible_log_chars",
            VISIBLE_LOG_ENTRY_LIMIT,
            VISIBLE_LOG_CHAR_LIMIT,
        )
        self._append_bounded_log(
            self.diagnostic_log_entries,
            entry,
            "_diagnostic_log_chars",
            DIAGNOSTIC_LOG_ENTRY_LIMIT,
            DIAGNOSTIC_LOG_CHAR_LIMIT,
        )

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
            setattr(
                self,
                char_attr,
                max(0, getattr(self, char_attr) - self._log_entry_size(removed)),
            )

    @staticmethod
    def _log_entry_size(entry: dict[str, str]) -> int:
        return sum(len(str(value or "")) for value in entry.values()) + 16

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
        configure_file_selection_table(
            self.table,
            fixed_column_widths={2: 112, 3: 86},
        )
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
        if self.external_task_lock:
            return False
        if self.phase != "idle" or not self._selected_files():
            return False
        if not self._selected_target_lang() or not self._selected_source_lang():
            return False
        if self.settings.output.use_custom_output_dir:
            return get_custom_output_dir_error(self.settings.output.custom_output_dir) is None
        return True

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
        if self.excel_autofit_check.isChecked() and self.lock_row_height_check.isChecked():
            sender = self.sender()
            if sender == self.excel_autofit_check:
                self.lock_row_height_check.setChecked(False)
            else:
                self.excel_autofit_check.setChecked(False)
        self.settings.output.keep_original_sheets = self.keep_original_check.isChecked()
        self.settings.output.formula_display_value_backfill = (
            self.formula_backfill_check.isChecked()
        )
        self.settings.output.enable_excel_autofit = self.excel_autofit_check.isChecked()
        self.settings.output.lock_row_height = self.lock_row_height_check.isChecked()
        self.settings.excel_review.existing_fill_policy = (
            self.existing_fill_policy_combo.currentData() or "red_font"
        )
        self._sync_review_color_settings()
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

    def _sync_source_lang_visibility(self) -> None:
        self.source_lang_label.setVisible(True)
        self.source_lang_combo.setVisible(True)
        self.source_lang_combo.setEnabled(
            self.phase != "running" and not self.external_task_lock
        )

    def _lock_inputs(self, locked: bool) -> None:
        effective_locked = bool(locked or self.external_task_lock)
        for widget in (
            self.source_input,
            self.browse_button,
            self.scan_button,
            self.target_combo,
            self.source_lang_combo,
            self.output_default_radio,
            self.output_custom_radio,
            self.custom_output_input,
            self.keep_original_check,
            self.formula_backfill_check,
            self.excel_autofit_check,
            self.lock_row_height_check,
            self.existing_fill_policy_combo,
        ):
            widget.setEnabled(not effective_locked)
        for widget in (
            *getattr(self, "review_color_combos", {}).values(),
            *getattr(self, "review_color_inputs", {}).values(),
            *getattr(self, "review_color_buttons", {}).values(),
        ):
            widget.setEnabled(not effective_locked)
        for mark in getattr(self, "review_color_combos", {}):
            self._refresh_review_color_control(mark)
        self._sync_source_lang_visibility()

    def _start_translation(self) -> None:
        selected = self._selected_files()
        if not selected:
            QMessageBox.warning(self, APP_NAME, "请先扫描并选择至少一个文件。")
            return

        target_lang = self._selected_target_lang()
        source_lang = self._selected_source_lang()
        if not target_lang or not source_lang:
            QMessageBox.warning(self, APP_NAME, "请先选择目标语言和源语言。")
            return
        if self.runner is not None:
            QMessageBox.warning(self, APP_NAME, "任务正在运行，请先完成或终止当前任务。")
            return

        config_check = check_translation_api_config(self.settings)
        if not config_check.ok:
            detail = f"\n{config_check.detail}" if config_check.detail else ""
            QMessageBox.warning(self, APP_NAME, f"{config_check.message}{detail}")
            return

        has_xls = any(item.path.suffix.lower() == ".xls" for item in selected)
        excel_available, unavailable_reason = get_local_excel_availability()
        allow_xls_fallback = False
        if has_xls and not excel_available:
            message = (
                "当前列表包含 .xls 文件，但未检测到可用的本地 Excel 自动化。\n\n"
                "是否使用兼容模式继续？\n\n"
                f"检测详情：{unavailable_reason}"
            )
            answer = QMessageBox.warning(
                self,
                APP_NAME,
                message,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            allow_xls_fallback = True

        self.settings.target_lang = target_lang
        self.settings.source_lang = source_lang
        self._on_output_changed()
        self._on_params_changed()

        self._begin_runner(
            selected,
            self.settings,
            source_lang=source_lang,
            allow_xls_fallback=allow_xls_fallback,
        )

    def _begin_runner(
        self,
        selected: list[FileItem],
        settings: AppSettings,
        *,
        source_lang: str,
        source_root=None,
        allow_xls_fallback: bool = False,
    ) -> None:
        effective_source_root = source_root if source_root is not None else self.source_root or None
        task_settings = settings.model_copy(deep=True)
        try:
            api_context = task_api_context_for_page(task_settings, "excel_translate")
        except Exception as exc:  # noqa: BLE001 - converted to UI message.
            QMessageBox.warning(self, APP_NAME, f"Excel 翻译 API 配置不可用：{exc}")
            return
        self.runner = TaskRunner(
            selected,
            task_settings,
            source_root=effective_source_root,
            allow_xls_fallback=allow_xls_fallback,
            source_lang=source_lang,
            key_overrides=api_context.key_overrides,
        )
        self.task_files = list(selected)
        self.current_task_source_root = str(effective_source_root or "")
        self.current_task_id = self.runner.task_id
        self.current_task_api_groups = set(api_context.api_groups)
        self._task_diagnostics_archived = False
        self._reset_runtime_logs()
        self.runner.start()
        self.phase = "running"
        self._workspace_render_phase = self.phase
        self.progress = None
        self.status_text = ""
        self.done = None
        self.stop_message = ""
        self._start_ui_sync_guard()
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
            "确认终止当前 Excel 翻译任务？",
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
                self._append_runtime_log(msg.level, msg.message, msg.ts)
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
                self._archive_current_task(phase="done", done=msg)
                self._render_workspace()
                self._render_action_card()
                self._schedule_action_card_resync()
                return
            elif isinstance(msg, ErrorMsg):
                self._append_runtime_log("ERROR", msg.message)
                self.runner = None
                self.phase = "error"
                self.poll_timer.stop()
                self._lock_inputs(False)
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
                self.runner = None
                self.phase = "stopped"
                self.poll_timer.stop()
                self._lock_inputs(False)
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
                surface="excel",
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
            self.running_status.setText(
                f"阶段 {progress.phase_index} / {progress.phase_total} | "
                f"{progress.phase_name} | {progress.step_done} / {progress.step_total}"
                + (f"\n{status}" if status else "")
            )
        self._refresh_log_view()

    def _refresh_log_view(self) -> None:
        if not is_live_widget(getattr(self, "log_view", None)):
            return
        lines = []
        for item in self.log_entries:
            prefix = f"[{item.get('ts')}]" if item.get("ts") else ""
            lines.append(f"{prefix} {item.get('level', '')} {item.get('message', '')}".strip())
        self.log_view.setPlainText("\n".join(lines))
        self.log_view.moveCursor(QTextCursor.MoveOperation.End)

    def _calc_overall_progress(self, progress: ProgressMsg) -> float:
        if progress.phase_total == 4:
            weights = {1: 0.05, 2: 0.70, 3: 0.15, 4: 0.10}
        else:
            weights = {1: 0.05, 2: 0.80, 3: 0.15}
        offset = 0.0
        for phase_index in range(1, progress.phase_index):
            offset += weights.get(phase_index, 0.0)
        current = progress.step_done / max(progress.step_total, 1)
        return min(offset + current * weights.get(progress.phase_index, 0.0), 1.0)

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
        self.current_task_api_groups = set()
        self.current_task_source_root = ""
        self.current_task_id = ""
        self._task_diagnostics_archived = False
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
        target, _ = QFileDialog.getSaveFileName(
            self,
            "导出历史诊断归档",
            filename,
            "Zip 文件 (*.zip)",
        )
        if target:
            Path(target).write_bytes(data)
            QMessageBox.information(self, APP_NAME, f"已导出：{target}")
