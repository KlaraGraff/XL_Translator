"""Native translation-memory management page."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app_meta import APP_NAME
from core import tm_manager
from core.language_registry import (
    build_lang_pair,
    get_ordered_target_lang_codes,
    get_target_lang_display,
    remember_recent_target_lang,
)
from core.tm_cleaner import (
    CleanSuggestion,
    apply_suggestions,
    build_clean_system_prompt,
    get_clean_builtin_system_prompt,
)
from core.model_roles import (
    ROLE_CLEANER,
    LocalModelFollowNotAllowedError,
    resolve_effective_model_config,
)
from native_app.workers import TmCleanWorker
from native_app.widgets import (
    build_app_tooltip_html,
    configure_app_table,
    create_check_table_item,
    create_option_combo,
    create_searchable_combo,
    refresh_combo_completer,
    select_combo_text_match,
)
from settings import AppSettings, save_settings


HEADER_TILE_HEIGHT = 48
HEADER_TILE_MIN_WIDTH = 86
COMPACT_CONTROL_HEIGHT = 32
COMPACT_BUTTON_HEIGHT = 34
TM_SCOPE_CARD_MIN_WIDTH = 300
TM_OVERVIEW_CARD_MIN_WIDTH = 380
TM_CLEANER_CARD_MIN_WIDTH = 340
WORKSPACE_ACTION_BUTTON_WIDTH = 96
WORKSPACE_ACTION_BUTTON_HEIGHT = 38
ENTRY_DIALOG_WIDTH = 860
ENTRY_DIALOG_MIN_WIDTH = 760
ENTRY_DIALOG_FIELD_HEIGHT = 72
PAGE_SIZE = 10
CLEAN_NOTICE_VISIBLE_MS = 8000
ENTRY_ID_ROLE = int(Qt.ItemDataRole.UserRole) + 1
ENTRY_SOURCE_ROLE = int(Qt.ItemDataRole.UserRole) + 2
ENTRY_TARGET_ROLE = int(Qt.ItemDataRole.UserRole) + 3
ENTRY_PINNED_ROLE = int(Qt.ItemDataRole.UserRole) + 4
DIFF_TARGET_ROLE = int(Qt.ItemDataRole.UserRole) + 11


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
    del title, summary, items
    label = QLabel(text)
    label.setProperty("tmFieldLabel", "true")
    return label


def _module_title(
    text: str,
    title: str,
    summary: str,
    items: list[str] | None = None,
) -> QLabel:
    label = _label(text, "SectionTitle")
    _set_tooltip(label, title, summary, items)
    return label


def _card() -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("Card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(12, 9, 12, 9)
    layout.setSpacing(8)
    layout.setAlignment(Qt.AlignmentFlag.AlignTop)
    return frame, layout


def _compact_control(widget: QWidget, height: int = COMPACT_CONTROL_HEIGHT) -> QWidget:
    widget.setProperty("compact", "true")
    widget.setFixedHeight(height)
    return widget


def _workspace_action_button(text: str) -> QPushButton:
    button = QPushButton(text)
    button.setFixedSize(WORKSPACE_ACTION_BUTTON_WIDTH, WORKSPACE_ACTION_BUTTON_HEIGHT)
    return button


def _metric_pair(label: str, value: str) -> QWidget:
    widget = QWidget()
    widget.setProperty("tmMetricPair", "true")
    layout = QHBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(5)
    label_widget = QLabel(label)
    label_widget.setObjectName("TmMetricLabel")
    value_widget = QLabel(value)
    value_widget.setObjectName("TmMetricValue")
    layout.addWidget(label_widget)
    layout.addWidget(value_widget)
    layout.addStretch(1)
    return widget


def _normalize_clean_progress_payload(payload):
    if payload is None:
        return None

    if isinstance(payload, dict):
        total_entries = int(payload.get("total_entries") or payload.get("total") or 0)
        completed_entries = int(payload.get("completed_entries") or payload.get("done") or 0)
        total_batches = int(payload.get("total_batches") or 0)
        completed_batches = int(payload.get("completed_batches") or 0)
        submitted_batches = int(payload.get("submitted_batches") or completed_batches or 0)
        stage = str(payload.get("stage") or "prepared")
    elif isinstance(payload, (tuple, list)) and len(payload) >= 2:
        completed_entries = int(payload[0] or 0)
        total_entries = int(payload[1] or 0)
        total_batches = 0
        completed_batches = 0
        submitted_batches = 0
        stage = "processing" if completed_entries else "prepared"
    else:
        return None

    total_entries = max(0, total_entries)
    completed_entries = min(max(0, completed_entries), total_entries)
    total_batches = max(0, total_batches)
    completed_batches = min(max(0, completed_batches), total_batches)
    if total_batches:
        submitted_batches = min(max(completed_batches, submitted_batches), total_batches)
    else:
        submitted_batches = max(0, submitted_batches)

    return {
        "stage": stage,
        "total_entries": total_entries,
        "completed_entries": completed_entries,
        "total_batches": total_batches,
        "completed_batches": completed_batches,
        "submitted_batches": submitted_batches,
    }


def _build_clean_progress_display(progress: dict) -> tuple[float, str, str]:
    stage = progress["stage"]
    total_entries = progress["total_entries"]
    completed_entries = progress["completed_entries"]
    total_batches = progress["total_batches"]
    completed_batches = progress["completed_batches"]
    submitted_batches = progress["submitted_batches"]

    if stage == "prepared":
        detail = (
            f"已切分为 {total_batches} 个批次"
            if total_batches
            else "正在初始化清洗任务"
        )
        return 0.0, f"已装载 {total_entries} 条词条", detail

    if stage == "waiting_first_result":
        detail = (
            f"已调度 {submitted_batches} / {total_batches} 个批次"
            if total_batches
            else "正在等待首批结果返回"
        )
        return 0.02, "深度清洗已开始", detail

    ratio = completed_entries / max(total_entries, 1)
    detail = (
        f"完成 {completed_batches} / {total_batches} 个批次"
        if total_batches
        else ""
    )
    if stage == "completed":
        return 1.0, f"清洗完成：{completed_entries} / {total_entries} 条", detail
    return ratio, f"清洗中：{completed_entries} / {total_entries} 条", detail


def _update_lang_prompt_map(prompt_map: dict[str, str], lang_pair: str, prompt: str) -> dict[str, str]:
    updated = dict(prompt_map)
    value = str(prompt or "").strip()
    if value:
        updated[lang_pair] = value
    else:
        updated.pop(lang_pair, None)
    return updated


class TmManagerPage(QWidget):
    """Qt implementation of the translation-memory workbench."""

    def __init__(self, settings: AppSettings):
        super().__init__()
        self.settings = settings
        self.keyword = ""
        self.current_page = 1
        self.phase = "idle"
        self.clean_worker: TmCleanWorker | None = None
        self.clean_progress: dict | None = None
        self.clean_suggestions: list[CleanSuggestion] = []
        self._updating_entry_table = False
        self._updating_diff_table = False
        self._clean_notice_text = ""
        self._clean_notice_seen = False
        self._is_page_active = False
        self._pending_language_sync: tuple[str, str] | None = None
        self._clean_session_lang_pair = ""
        self._clean_session_target_lang = ""
        self._clean_session_source_lang = ""
        self._clean_notice_timer = QTimer(self)
        self._clean_notice_timer.setSingleShot(True)
        self._clean_notice_timer.timeout.connect(self._hide_clean_notice)

        tm_manager.init_db()
        self._build_ui()
        self._refresh_all()

    @property
    def lang_pair(self) -> str:
        return build_lang_pair(self._selected_target_lang(), self._selected_source_lang())

    def showEvent(self, event) -> None:  # noqa: N802 - Qt API name.
        super().showEvent(event)
        self.set_page_active(True)

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt API name.
        self.set_page_active(False)
        super().hideEvent(event)

    def set_page_active(self, active: bool) -> None:
        self._is_page_active = active
        if active:
            self._arm_clean_notice_timer()
        else:
            self._clean_notice_timer.stop()

    def refresh_settings(self) -> None:
        self._refresh_language_options()
        self._refresh_clean_model_options()
        self._refresh_all()

    def sync_language_from_translation(self, target_lang: str, source_lang: str) -> None:
        target_lang = str(target_lang or "").strip()
        source_lang = str(source_lang or "").strip()
        if not target_lang or not source_lang:
            return
        if self._is_clean_session_locked():
            self._pending_language_sync = (target_lang, source_lang)
            return

        self._apply_synced_language(target_lang, source_lang)

    def _apply_synced_language(self, target_lang: str, source_lang: str) -> None:
        old_lang_pair = self.lang_pair
        self.settings.target_lang = target_lang
        self.settings.source_lang = source_lang
        if build_lang_pair(target_lang, source_lang) != old_lang_pair:
            self.current_page = 1
            self.keyword = ""
        self._refresh_language_options()
        self._refresh_all()

    def _apply_pending_language_sync(self) -> bool:
        pending = self._pending_language_sync
        self._pending_language_sync = None
        if pending is None:
            return False
        target_lang, source_lang = pending
        self._apply_synced_language(target_lang, source_lang)
        return True

    def _is_clean_session_locked(self) -> bool:
        return self.phase in {"cleaning", "review"}

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 18, 24, 18)
        root.setSpacing(12)

        self.header_layout = QVBoxLayout()
        self.header_layout.setSpacing(6)
        root.addLayout(self.header_layout)

        top_row = QHBoxLayout()
        top_row.setSpacing(12)
        root.addLayout(top_row)
        self._build_overview_card(top_row)
        self._build_scope_card(top_row)
        self._build_cleaner_card(top_row)

        self.workspace = QFrame()
        self.workspace.setObjectName("Workspace")
        self.workspace_layout = QVBoxLayout(self.workspace)
        self.workspace_layout.setContentsMargins(18, 18, 18, 18)
        self.workspace_layout.setSpacing(14)
        root.addWidget(self.workspace, 1)

    def _build_scope_controls(self, layout: QVBoxLayout) -> None:
        form_grid = QGridLayout()
        form_grid.setHorizontalSpacing(10)
        form_grid.setVerticalSpacing(8)
        for column in range(3):
            form_grid.setColumnMinimumWidth(column, 96)
            form_grid.setColumnStretch(column, 1)
        layout.addLayout(form_grid)

        form_grid.addWidget(
            _field_label("源语言", "源语言", "选择词库原文语言。"),
            0,
            0,
        )
        self.source_combo = create_searchable_combo()
        if self.source_combo.lineEdit() is not None:
            self.source_combo.lineEdit().setPlaceholderText("筛选源语言")
        self.source_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        _compact_control(self.source_combo)
        form_grid.addWidget(self.source_combo, 1, 0)

        form_grid.addWidget(
            _field_label("目标语言", "目标语言", "选择词库译文语言。"),
            0,
            1,
        )
        self.target_combo = create_searchable_combo()
        if self.target_combo.lineEdit() is not None:
            self.target_combo.lineEdit().setPlaceholderText("筛选目标语言")
        self.target_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        _compact_control(self.target_combo)
        form_grid.addWidget(self.target_combo, 1, 1)

        form_grid.addWidget(
            _field_label(
                "词长上限",
                "自动入库上限",
                "设置自动入库的最长词条长度。",
                [
                    "超过上限的长句不会自动入库。",
                    "手动新增不受限制。",
                ],
            ),
            0,
            2,
        )
        self.max_len_spin = QSpinBox()
        self.max_len_spin.setRange(1, 200)
        self.max_len_spin.setValue(self.settings.tm.max_len)
        self.max_len_spin.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        _compact_control(self.max_len_spin)
        self.max_len_spin.valueChanged.connect(self._on_max_len_changed)
        form_grid.addWidget(self.max_len_spin, 1, 2)

        self._refresh_language_options()
        self.source_combo.currentIndexChanged.connect(self._on_source_changed)
        self.target_combo.currentIndexChanged.connect(self._on_target_changed)

        self.lang_pair_hint = QLabel("", self)
        self.lang_pair_hint.setObjectName("FieldHint")
        self.lang_pair_hint.hide()

    def _build_scope_card(self, row: QHBoxLayout) -> None:
        frame, layout = _card()
        frame.setProperty("tmTopCard", "true")
        frame.setMinimumWidth(TM_SCOPE_CARD_MIN_WIDTH)
        frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        row.addWidget(frame, 4)
        layout.addWidget(
            _module_title(
                "语言与规则",
                "语言与规则",
                "设置当前语言对和自动入库规则。",
                [
                    "语言对决定当前词库范围。",
                    "词长上限仅影响自动入库。",
                ],
            )
        )
        self._build_scope_controls(layout)

    def _build_overview_card(self, row: QHBoxLayout) -> None:
        frame, layout = _card()
        frame.setProperty("tmTopCard", "true")
        frame.setMinimumWidth(TM_OVERVIEW_CARD_MIN_WIDTH)
        frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        row.addWidget(frame, 3)
        layout.addWidget(
            _module_title(
                "词库概况",
                "词库概况",
                "查看统计并管理词库文件。",
                [
                    "JSON 用于备份和迁移。",
                    "CSV 用于查看和表格处理。",
                    "导入时按原文判断重复项。",
                ],
            )
        )

        self.stats_layout = QHBoxLayout()
        self.stats_layout.setSpacing(10)
        layout.addLayout(self.stats_layout)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.import_button = QPushButton("导入词库")
        _compact_control(self.import_button, COMPACT_BUTTON_HEIGHT)
        self.import_button.clicked.connect(self._import_entries)
        self.import_button.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        buttons.addWidget(self.import_button)

        self.export_json_button = QPushButton("导出 JSON")
        _compact_control(self.export_json_button, COMPACT_BUTTON_HEIGHT)
        self.export_json_button.clicked.connect(lambda: self._export_entries("json"))
        self.export_json_button.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        buttons.addWidget(self.export_json_button)

        self.export_csv_button = QPushButton("导出 CSV")
        _compact_control(self.export_csv_button, COMPACT_BUTTON_HEIGHT)
        self.export_csv_button.clicked.connect(lambda: self._export_entries("csv"))
        self.export_csv_button.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        buttons.addWidget(self.export_csv_button)
        layout.addLayout(buttons)

    def _build_cleaner_card(self, row: QHBoxLayout) -> None:
        frame, layout = _card()
        frame.setProperty("tmTopCard", "true")
        frame.setMinimumWidth(TM_CLEANER_CARD_MIN_WIDTH)
        frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        row.addWidget(frame, 3)

        title_row = QHBoxLayout()
        title_row.setSpacing(10)
        self.cleaner_title_label = _module_title(
            "维护与清洗",
            "维护与清洗",
            "清洗当前语言对的未固定词条。",
            [
                "差异确认模式会先生成建议，再由用户确认写入。",
                "直接覆写模式会自动写入结果。",
                "固定词条不参与清洗。",
            ],
        )
        title_row.addWidget(self.cleaner_title_label)
        title_row.addStretch(1)
        self.auto_pin_check = QCheckBox("清洗后自动固定")
        self.auto_pin_check.setChecked(self.settings.auto_pin_after_clean)
        self.auto_pin_check.toggled.connect(self._on_cleaner_settings_changed)
        title_row.addWidget(self.auto_pin_check)
        layout.addLayout(title_row)

        self.clean_mode_combo = create_option_combo()
        self.clean_mode_combo.addItem("差异确认模式", "diff")
        self.clean_mode_combo.addItem("直接覆写模式", "overwrite")
        self.clean_mode_combo.setCurrentIndex(
            1 if self.settings.cleaner_mode == "overwrite" else 0
        )
        self.clean_mode_combo.currentIndexChanged.connect(self._on_cleaner_settings_changed)
        self.clean_mode_combo.hide()
        layout.addWidget(self.clean_mode_combo)

        self.clean_summary_layout = QHBoxLayout()
        self.clean_summary_layout.setSpacing(8)
        layout.addLayout(self.clean_summary_layout)

        action_row = QHBoxLayout()
        action_row.setSpacing(8)
        self.clean_button = QPushButton("启动深度清洗")
        self.clean_button.setObjectName("PrimaryButton")
        _compact_control(self.clean_button, COMPACT_BUTTON_HEIGHT)
        self.clean_button.clicked.connect(self._toggle_cleaning)
        action_row.addWidget(self.clean_button, 1)

        self.prompt_button = QPushButton("编辑提示词")
        _compact_control(self.prompt_button, COMPACT_BUTTON_HEIGHT)
        self.prompt_button.clicked.connect(self._open_cleaner_prompt_dialog)
        action_row.addWidget(self.prompt_button, 1)
        layout.addLayout(action_row)

        self.clean_progress_panel = QWidget()
        progress_row = QHBoxLayout(self.clean_progress_panel)
        progress_row.setContentsMargins(0, 0, 0, 0)
        progress_row.setSpacing(10)
        self.clean_progress_bar = QProgressBar()
        self.clean_progress_bar.setRange(0, 100)
        progress_row.addWidget(self.clean_progress_bar, 1)

        self.clean_status = QLabel("清洗任务未启动。")
        self.clean_status.setWordWrap(False)
        self.clean_status.setObjectName("FieldHint")
        progress_row.addWidget(self.clean_status)
        layout.addWidget(self.clean_progress_panel)

    def _refresh_language_options(self) -> None:
        if not hasattr(self, "target_combo"):
            return
        current_source = (
            self._clean_session_source_lang
            if self._is_clean_session_locked() and self._clean_session_source_lang
            else self.settings.source_lang
        )
        current_target = (
            self._clean_session_target_lang
            if self._is_clean_session_locked() and self._clean_session_target_lang
            else self.settings.target_lang
        )
        source_codes = get_ordered_target_lang_codes(
            [current_source],
            self.settings.custom_target_langs,
            include_optional=True,
        )
        target_codes = get_ordered_target_lang_codes(
            [current_target],
            self.settings.custom_target_langs,
            include_optional=True,
        )

        self.source_combo.blockSignals(True)
        self.source_combo.clear()
        for code in source_codes:
            self.source_combo.addItem(
                get_target_lang_display(
                    code,
                    self.settings.custom_target_langs,
                    include_optional=True,
                ),
                code,
            )
        source_index = self.source_combo.findData(current_source)
        self.source_combo.setCurrentIndex(source_index if source_index >= 0 else 0)
        refresh_combo_completer(self.source_combo)
        self.source_combo.blockSignals(False)

        self.target_combo.blockSignals(True)
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
        index = self.target_combo.findData(current_target)
        self.target_combo.setCurrentIndex(index if index >= 0 else 0)
        refresh_combo_completer(self.target_combo)
        self.target_combo.blockSignals(False)

    def _refresh_clean_model_options(self) -> None:
        if not hasattr(self, "clean_model_input"):
            return
        current = str(self.settings.cleaner_model or "").strip()
        cloud_model = str(self.settings.engine.cloud_model or "").strip()
        options = ["跟随当前翻译模型"]
        if cloud_model:
            options.append(cloud_model)
        if current and current not in options:
            options.append(current)
        self.clean_model_input.blockSignals(True)
        self.clean_model_input.clear()
        self.clean_model_input.addItems(options)
        if current:
            self.clean_model_input.setCurrentText(current)
        else:
            self.clean_model_input.setCurrentIndex(0)
        refresh_combo_completer(self.clean_model_input)
        self.clean_model_input.blockSignals(False)

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
            hasattr(self, "source_combo")
            and self.source_combo.currentIndex() >= 0
            and self.source_combo.currentText().strip()
            != self.source_combo.itemText(self.source_combo.currentIndex()).strip()
        ):
            select_combo_text_match(self.source_combo)
        return str(self.source_combo.currentData() or self.settings.source_lang or "")

    def _selected_target_label(self) -> str:
        target_lang = self._selected_target_lang() if hasattr(self, "target_combo") else self.settings.target_lang
        return get_target_lang_display(
            target_lang,
            self.settings.custom_target_langs,
            include_optional=True,
        )

    def _refresh_all(self) -> None:
        self._refresh_header()
        self._refresh_stats()
        self._refresh_cleaner_controls()
        self._refresh_workspace()

    def _refresh_header(self) -> None:
        _clear_layout(self.header_layout)
        self.header_layout.addWidget(_label("TM Workbench", "PageEyebrow"))

        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        title_row.addWidget(_label("记忆库管理", "PageTitle"))
        title_row.addStretch(1)
        stats = tm_manager.get_stats(self.lang_pair)
        title_row.addWidget(self._pill("语言对", self.lang_pair), alignment=Qt.AlignmentFlag.AlignTop)
        title_row.addWidget(self._pill("总词条", f"{stats['total']} 条"), alignment=Qt.AlignmentFlag.AlignTop)
        title_row.addWidget(self._phase_badge(), alignment=Qt.AlignmentFlag.AlignTop)
        self.header_layout.addLayout(title_row)
        self.lang_pair_hint.setText(f"当前范围：{self.lang_pair}")

    def _pill(self, label: str, value: str) -> QFrame:
        frame = QFrame()
        frame.setObjectName("Pill")
        frame.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        frame.setFixedHeight(HEADER_TILE_HEIGHT)
        frame.setMinimumWidth(HEADER_TILE_MIN_WIDTH)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(8, 3, 8, 3)
        layout.setSpacing(0)
        label_widget = QLabel(label)
        label_widget.setObjectName("PillLabel")
        value_widget = QLabel(value)
        value_widget.setObjectName("PillValue")
        value_widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(label_widget)
        layout.addWidget(value_widget)
        return frame

    def _phase_badge(self) -> QFrame:
        text = {
            "idle": "词库就绪",
            "cleaning": "清洗执行中",
            "review": "等待确认",
        }.get(self.phase, "词库就绪")
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

    def _refresh_stats(self) -> None:
        _clear_layout(self.stats_layout)
        stats = tm_manager.get_stats(self.lang_pair)
        items = [
            ("总词条", f"{stats['total']:,}"),
            ("已固定", f"{stats['pinned']:,}"),
            ("手动", f"{stats['manual']:,}"),
            ("自动", f"{stats['auto']:,}"),
        ]
        for label, value in items:
            self.stats_layout.addWidget(_metric_pair(label, value), 1)

    def _refresh_cleaner_controls(self) -> None:
        running = self.phase == "cleaning"
        locked = self._is_clean_session_locked()
        for widget in (
            self.auto_pin_check,
            self.clean_mode_combo,
            self.prompt_button,
        ):
            widget.setEnabled(not locked)
        for widget in (self.source_combo, self.target_combo):
            widget.setEnabled(not locked)
        self.clean_button.setEnabled(self.phase != "review")
        self.clean_button.setText("中止清洗" if running else "启动深度清洗")
        self.clean_button.setObjectName("DangerButton" if running else "PrimaryButton")
        self.clean_button.style().unpolish(self.clean_button)
        self.clean_button.style().polish(self.clean_button)
        self._refresh_clean_summary()
        self._sync_clean_progress_panel()

    def _refresh_clean_summary(self) -> None:
        _clear_layout(self.clean_summary_layout)
        state = {
            "idle": "未开始",
            "cleaning": "清洗中",
            "review": "待确认",
        }.get(self.phase, "未开始")
        pending = len(self.clean_suggestions) if self.phase == "review" else 0
        items = [
            ("状态", state),
            ("待确认", f"{pending:,}"),
            ("冲突", "0"),
        ]
        for label, value in items:
            self.clean_summary_layout.addWidget(_metric_pair(label, value), 1)

    def _sync_clean_progress_panel(self) -> None:
        running = self.phase == "cleaning"
        has_notice = bool(self._clean_notice_text)
        self.clean_progress_panel.setVisible(running or has_notice)
        self.clean_progress_bar.setVisible(running)
        if running:
            if self.clean_progress:
                ratio, text, detail = _build_clean_progress_display(self.clean_progress)
                self.clean_progress_bar.setValue(int(ratio * 100))
                self.clean_status.setText(f"{text}\n{detail}".strip())
            else:
                self.clean_progress_bar.setValue(0)
                self.clean_status.setText("清洗任务正在启动...")
            return
        self.clean_progress_bar.setValue(0)
        self.clean_status.setText(self._clean_notice_text or "清洗任务未启动。")

    def _set_clean_notice(self, message: str) -> None:
        self._clean_notice_text = str(message or "").strip()
        self._clean_notice_seen = False
        self._clean_notice_timer.stop()
        self._sync_clean_progress_panel()
        self._arm_clean_notice_timer()

    def _clear_clean_notice(self) -> None:
        self._clean_notice_text = ""
        self._clean_notice_seen = False
        self._clean_notice_timer.stop()
        self._sync_clean_progress_panel()

    def _arm_clean_notice_timer(self) -> None:
        if (
            not self._clean_notice_text
            or not self._is_page_active
            or self._clean_notice_timer.isActive()
        ):
            return
        self._clean_notice_seen = True
        self._clean_notice_timer.start(CLEAN_NOTICE_VISIBLE_MS)

    def _hide_clean_notice(self) -> None:
        if not self._clean_notice_seen or self.phase == "cleaning":
            return
        self._clean_notice_text = ""
        self._clean_notice_seen = False
        self._sync_clean_progress_panel()

    def _refresh_workspace(self) -> None:
        self.diff_table = None
        _clear_layout(self.workspace_layout)
        if self.phase == "review" and self.clean_suggestions:
            self._render_diff_workspace()
            return
        self._render_entry_workspace()

    def _render_entry_workspace(self) -> None:
        self.workspace_layout.addWidget(_label("词条工作区", "SectionTitle"))
        if self.phase == "cleaning":
            note = QLabel("清洗执行中。词库可浏览，编辑操作暂时锁定。")
            note.setWordWrap(True)
            note.setObjectName("MutedText")
            self.workspace_layout.addWidget(note)

        search_row = QHBoxLayout()
        self.search_input = QLineEdit(self.keyword)
        self.search_input.setPlaceholderText("输入原文、译文或关键词")
        self.search_input.returnPressed.connect(self._apply_search)
        search_row.addWidget(self.search_input, 1)

        search_button = _workspace_action_button("搜索")
        search_button.clicked.connect(self._apply_search)
        search_row.addWidget(search_button)

        add_button = _workspace_action_button("新增词条")
        add_button.setEnabled(self.phase != "cleaning")
        add_button.clicked.connect(self._add_entry)
        search_row.addWidget(add_button)
        self.workspace_layout.addLayout(search_row)

        rows, total = self._load_current_rows()
        pin_stats = tm_manager.get_pin_count(self.lang_pair, self.keyword)
        all_pinned = pin_stats["unpinned"] == 0 and pin_stats["pinned"] > 0

        scope_row = QHBoxLayout()
        scope = QLabel(
            f"当前范围：{total:,} 条；已固定 {pin_stats['pinned']:,}；"
            f"可编辑 {pin_stats['unpinned']:,}"
        )
        scope.setObjectName("MutedText")
        scope_row.addWidget(scope, 1)

        bulk_delete = _workspace_action_button("批量删除")
        bulk_delete.setEnabled(total > 0 and self.phase != "cleaning")
        bulk_delete.clicked.connect(lambda: self._bulk_delete(total, pin_stats))
        scope_row.addWidget(bulk_delete)

        bulk_pin = _workspace_action_button("全部解锁" if all_pinned else "全部固定")
        bulk_pin.setEnabled(total > 0 and self.phase != "cleaning")
        bulk_pin.clicked.connect(lambda: self._bulk_pin(not all_pinned))
        scope_row.addWidget(bulk_pin)
        self.workspace_layout.addLayout(scope_row)

        self._render_entries_table(rows)
        self._render_pager(total)

    def _load_current_rows(self) -> tuple[list[dict], int]:
        rows, total = tm_manager.search_entries(
            self.lang_pair,
            self.keyword,
            page=self.current_page,
            page_size=PAGE_SIZE,
        )
        total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        if self.current_page > total_pages:
            self.current_page = total_pages
            rows, total = tm_manager.search_entries(
                self.lang_pair,
                self.keyword,
                page=self.current_page,
                page_size=PAGE_SIZE,
            )
        return rows, total

    def _render_entries_table(self, rows: list[dict]) -> None:
        table = QTableWidget(len(rows), 3)
        table.setObjectName("TmEntryTable")
        table.setHorizontalHeaderLabels(["原文", "译文", "操作"])
        configure_app_table(table, editable=True, row_height=58, word_wrap=True)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(2, 252)
        table.setWordWrap(True)
        table.blockSignals(True)
        for row_index, row in enumerate(rows):
            is_pinned = bool(row.get("pinned", 0))
            values = [
                str(row.get("source_text") or ""),
                str(row.get("target_text") or ""),
            ]
            for col_index, value in enumerate(values):
                item = QTableWidgetItem(value)
                flags = item.flags()
                if is_pinned or self.phase == "cleaning":
                    flags &= ~Qt.ItemFlag.ItemIsEditable
                    item.setToolTip("固定词条需先解锁后修改。")
                else:
                    flags |= Qt.ItemFlag.ItemIsEditable
                    item.setToolTip("点击后编辑，回车或失焦保存。")
                item.setFlags(flags)
                item.setData(ENTRY_ID_ROLE, int(row["id"]))
                item.setData(ENTRY_SOURCE_ROLE, values[0])
                item.setData(ENTRY_TARGET_ROLE, values[1])
                item.setData(ENTRY_PINNED_ROLE, is_pinned)
                table.setItem(row_index, col_index, item)
            table.setCellWidget(row_index, 2, self._build_row_actions(row, is_pinned))
        table.blockSignals(False)
        table.cellClicked.connect(self._edit_entry_cell_on_click)
        table.itemChanged.connect(self._on_entry_item_changed)
        table.resizeRowsToContents()
        for row_index in range(len(rows)):
            table.setRowHeight(row_index, max(table.rowHeight(row_index), 58))
        self.workspace_layout.addWidget(table, 1)

    def _edit_entry_cell_on_click(self, row: int, column: int) -> None:
        if column not in (0, 1) or self.phase == "cleaning":
            return
        table = self.sender()
        if not isinstance(table, QTableWidget):
            return
        item = table.item(row, column)
        if item is None or not (item.flags() & Qt.ItemFlag.ItemIsEditable):
            return
        table.editItem(item)

    def _on_entry_item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating_entry_table or item.column() not in (0, 1):
            return
        table = item.tableWidget()
        if table is None:
            return
        entry_id = item.data(ENTRY_ID_ROLE)
        if entry_id is None or item.data(ENTRY_PINNED_ROLE):
            return

        row = item.row()
        source_item = table.item(row, 0)
        target_item = table.item(row, 1)
        if source_item is None or target_item is None:
            return

        old_source = str(source_item.data(ENTRY_SOURCE_ROLE) or "")
        old_target = str(target_item.data(ENTRY_TARGET_ROLE) or "")
        new_source = source_item.text().strip()
        new_target = target_item.text().strip()
        if new_source == old_source and new_target == old_target:
            return

        if not new_source or not new_target:
            self._restore_entry_row_items(table, row, old_source, old_target)
            QMessageBox.warning(self, APP_NAME, "保存失败：原文和译文均不能为空。")
            return

        if not tm_manager.update_entry_full(int(entry_id), new_source, new_target):
            self._restore_entry_row_items(table, row, old_source, old_target)
            QMessageBox.warning(self, APP_NAME, "保存失败：原文或译文为空，或与已有词条冲突。")
            return
        self._commit_entry_row_items(table, row, new_source, new_target)

    def _commit_entry_row_items(
        self,
        table: QTableWidget,
        row: int,
        source: str,
        target: str,
    ) -> None:
        self._updating_entry_table = True
        try:
            source_item = table.item(row, 0)
            target_item = table.item(row, 1)
            if source_item is not None:
                source_item.setText(source)
                source_item.setData(ENTRY_SOURCE_ROLE, source)
                source_item.setData(ENTRY_TARGET_ROLE, target)
            if target_item is not None:
                target_item.setText(target)
                target_item.setData(ENTRY_SOURCE_ROLE, source)
                target_item.setData(ENTRY_TARGET_ROLE, target)
        finally:
            self._updating_entry_table = False

    def _restore_entry_row_items(
        self,
        table: QTableWidget,
        row: int,
        source: str,
        target: str,
    ) -> None:
        self._updating_entry_table = True
        try:
            source_item = table.item(row, 0)
            target_item = table.item(row, 1)
            if source_item is not None:
                source_item.setText(source)
            if target_item is not None:
                target_item.setText(target)
        finally:
            self._updating_entry_table = False

    def _build_row_actions(self, row: dict, is_pinned: bool) -> QWidget:
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(4)
        disabled = self.phase == "cleaning"

        edit = QPushButton("编辑")
        edit.setMinimumWidth(66)
        edit.setEnabled(not is_pinned and not disabled)
        edit.clicked.connect(lambda _=False, entry=dict(row): self._edit_entry(entry))
        layout.addWidget(edit)

        delete = QPushButton("删除")
        delete.setMinimumWidth(66)
        delete.setEnabled(not is_pinned and not disabled)
        delete.clicked.connect(lambda _=False, entry_id=int(row["id"]): self._delete_entry(entry_id))
        layout.addWidget(delete)

        pin = QPushButton("解锁" if is_pinned else "固定")
        pin.setMinimumWidth(66)
        pin.setEnabled(not disabled)
        pin.clicked.connect(
            lambda _=False, entry_id=int(row["id"]), pinned=not is_pinned: self._pin_entry(
                entry_id,
                pinned,
            )
        )
        layout.addWidget(pin)
        return container

    def _render_pager(self, total: int) -> None:
        total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        row = QHBoxLayout()
        prev_button = QPushButton("上一页")
        prev_button.setEnabled(self.current_page > 1)
        prev_button.clicked.connect(lambda: self._change_page(self.current_page - 1))
        row.addWidget(prev_button)

        summary = QLabel(f"第 {self.current_page} / {total_pages} 页，共 {total:,} 条词条")
        summary.setAlignment(Qt.AlignmentFlag.AlignCenter)
        summary.setObjectName("MutedText")
        row.addWidget(summary, 1)

        next_button = QPushButton("下一页")
        next_button.setEnabled(self.current_page < total_pages)
        next_button.clicked.connect(lambda: self._change_page(self.current_page + 1))
        row.addWidget(next_button)
        self.workspace_layout.addLayout(row)

    def _render_diff_workspace(self) -> None:
        self.workspace_layout.addWidget(_label("清洗差异确认", "SectionTitle"))
        note = QLabel(f"发现 {len(self.clean_suggestions)} 条建议。请选择需要写入的结果。")
        note.setWordWrap(True)
        note.setObjectName("MutedText")
        self.workspace_layout.addWidget(note)

        table = QTableWidget(len(self.clean_suggestions), 4)
        table.setHorizontalHeaderLabels(["写入", "原文", "当前译文", "建议译文"])
        configure_app_table(table, editable=True, row_height=58, word_wrap=True)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(0, 64)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.diff_table = table
        table.blockSignals(True)
        for row, suggestion in enumerate(self.clean_suggestions):
            check = create_check_table_item()
            check.setCheckState(
                Qt.CheckState.Checked
                if suggestion.accepted
                else Qt.CheckState.Unchecked
            )
            table.setItem(row, 0, check)
            for col, value in enumerate(
                [
                    suggestion.source_text,
                    suggestion.old_target,
                    suggestion.new_target,
                ],
                start=1,
            ):
                item = QTableWidgetItem(value)
                if col == 3:
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
                    item.setData(DIFF_TARGET_ROLE, suggestion.new_target)
                    item.setToolTip("点击后修改本次建议译文。")
                else:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                table.setItem(row, col, item)
        table.blockSignals(False)
        table.cellClicked.connect(self._edit_clean_suggestion_on_click)
        table.itemChanged.connect(self._on_clean_suggestion_item_changed)
        table.resizeRowsToContents()
        self.workspace_layout.addWidget(table, 1)

        actions = QHBoxLayout()
        apply_button = QPushButton("写入已勾选建议")
        apply_button.setObjectName("PrimaryButton")
        apply_button.clicked.connect(self._apply_clean_suggestions)
        actions.addWidget(apply_button)

        discard_button = QPushButton("放弃本次结果")
        discard_button.clicked.connect(self._discard_clean_suggestions)
        actions.addWidget(discard_button)
        actions.addStretch(1)
        self.workspace_layout.addLayout(actions)

    def _edit_clean_suggestion_on_click(self, row: int, column: int) -> None:
        if column != 3:
            return
        table = self.sender()
        if not isinstance(table, QTableWidget):
            return
        item = table.item(row, column)
        if item is None or not (item.flags() & Qt.ItemFlag.ItemIsEditable):
            return
        table.editItem(item)

    def _on_clean_suggestion_item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating_diff_table or item.column() != 3:
            return
        table = item.tableWidget()
        if table is None:
            return
        row = item.row()
        if row < 0 or row >= len(self.clean_suggestions):
            return
        previous = str(item.data(DIFF_TARGET_ROLE) or "")
        new_target = item.text().strip()
        if not new_target:
            self._restore_clean_suggestion_item(item, previous)
            QMessageBox.warning(self, APP_NAME, "建议译文不能为空。")
            return
        if new_target == previous:
            return
        self.clean_suggestions[row].new_target = new_target
        item.setData(DIFF_TARGET_ROLE, new_target)
        check = table.item(row, 0)
        if check is not None:
            self._updating_diff_table = True
            try:
                check.setCheckState(Qt.CheckState.Checked)
            finally:
                self._updating_diff_table = False

    def _restore_clean_suggestion_item(
        self,
        item: QTableWidgetItem,
        value: str,
    ) -> None:
        self._updating_diff_table = True
        try:
            item.setText(value)
        finally:
            self._updating_diff_table = False

    def _apply_search(self) -> None:
        self.keyword = self.search_input.text().strip()
        self.current_page = 1
        self._refresh_all()

    def _change_page(self, page: int) -> None:
        self.current_page = max(1, page)
        self._refresh_workspace()

    def _on_max_len_changed(self, value: int) -> None:
        self.settings.tm.max_len = value
        save_settings(self.settings)

    def _on_target_changed(self) -> None:
        if self._is_clean_session_locked():
            self._refresh_language_options()
            return
        target_lang = self._selected_target_lang()
        self.settings.target_lang = target_lang
        self.settings.recent_target_langs = remember_recent_target_lang(
            self.settings.recent_target_langs,
            target_lang,
            self.settings.custom_target_langs,
            include_optional=True,
        )
        self.current_page = 1
        self.keyword = ""
        save_settings(self.settings)
        self._refresh_all()

    def _on_source_changed(self) -> None:
        if self._is_clean_session_locked():
            self._refresh_language_options()
            return
        self.settings.source_lang = self._selected_source_lang()
        self.current_page = 1
        self.keyword = ""
        save_settings(self.settings)
        self._refresh_all()

    def _on_cleaner_settings_changed(self) -> None:
        self.settings.cleaner_mode = str(self.clean_mode_combo.currentData() or "diff")
        self.settings.auto_pin_after_clean = self.auto_pin_check.isChecked()
        save_settings(self.settings)

    def _add_entry(self) -> None:
        ok, source, target = self._entry_dialog("新增词条")
        if not ok:
            return
        if not tm_manager.insert_manual_entry(source, target, self.lang_pair):
            QMessageBox.warning(self, APP_NAME, "新增失败：请确认原文和译文均已填写。")
            return
        self._refresh_all()

    def _edit_entry(self, entry: dict) -> None:
        ok, source, target = self._entry_dialog(
            "编辑词条",
            source=str(entry.get("source_text") or ""),
            target=str(entry.get("target_text") or ""),
        )
        if not ok:
            return
        if not tm_manager.update_entry_full(int(entry["id"]), source, target):
            QMessageBox.warning(self, APP_NAME, "保存失败：原文为空、译文为空或与已有词条冲突。")
            return
        self._refresh_all()

    def _entry_dialog(
        self,
        title: str,
        *,
        source: str = "",
        target: str = "",
    ) -> tuple[bool, str, str]:
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.setMinimumWidth(ENTRY_DIALOG_MIN_WIDTH)
        dialog.resize(ENTRY_DIALOG_WIDTH, 260)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(8)
        layout.addWidget(_label("原文", "SectionTitle"))
        source_edit = QTextEdit()
        source_edit.setFixedHeight(ENTRY_DIALOG_FIELD_HEIGHT)
        source_edit.setPlainText(source)
        layout.addWidget(source_edit)
        layout.addWidget(_label("译文", "SectionTitle"))
        target_edit = QTextEdit()
        target_edit.setFixedHeight(ENTRY_DIALOG_FIELD_HEIGHT)
        target_edit.setPlainText(target)
        layout.addWidget(target_edit)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False, "", ""
        return True, source_edit.toPlainText().strip(), target_edit.toPlainText().strip()

    def _delete_entry(self, entry_id: int) -> None:
        answer = QMessageBox.question(
            self,
            APP_NAME,
            "确认删除该词条？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        tm_manager.delete_entry(entry_id)
        self._refresh_all()

    def _pin_entry(self, entry_id: int, pinned: bool) -> None:
        tm_manager.pin_entry(entry_id, pinned)
        self._refresh_all()

    def _bulk_pin(self, pinned: bool) -> None:
        tm_manager.set_all_pinned(self.lang_pair, pinned, self.keyword)
        self._refresh_all()

    def _bulk_delete(self, total: int, pin_stats: dict) -> None:
        keyword_hint = f"\n当前关键词：{self.keyword}" if self.keyword else ""
        if pin_stats["pinned"] > 0:
            box = QMessageBox(self)
            box.setWindowTitle(APP_NAME)
            box.setText(
                f"当前范围共 {total} 条词条，其中 {pin_stats['pinned']} 条已固定。"
                f"{keyword_hint}\n请选择删除方式。"
            )
            unpinned_button = box.addButton("仅删除未固定", QMessageBox.ButtonRole.AcceptRole)
            all_button = box.addButton("全部删除", QMessageBox.ButtonRole.DestructiveRole)
            box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            clicked = box.clickedButton()
            if clicked == unpinned_button:
                deleted = tm_manager.delete_unpinned_entries(self.lang_pair, self.keyword)
            elif clicked == all_button:
                deleted = tm_manager.delete_all_entries(self.lang_pair, self.keyword)
            else:
                return
        else:
            answer = QMessageBox.question(
                self,
                APP_NAME,
                f"确认删除当前范围内的 {total} 条词条？{keyword_hint}",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            deleted = tm_manager.delete_all_entries(self.lang_pair, self.keyword)
        QMessageBox.information(self, APP_NAME, f"已删除 {deleted} 条词条。")
        self.current_page = 1
        self._refresh_all()

    def _export_entries(self, fmt: str) -> None:
        entries = tm_manager.get_all_entries_for_export(self.lang_pair)
        if fmt == "json":
            data = json.dumps(entries, ensure_ascii=False, indent=2)
            filename = f"tm_{self.lang_pair}.json"
            file_filter = "JSON 文件 (*.json)"
        else:
            buf = io.StringIO()
            fieldnames = ["source_text", "target_text", "word_type", "pinned", "updated_at"]
            writer = csv.DictWriter(buf, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(entries)
            data = buf.getvalue()
            filename = f"tm_{self.lang_pair}.csv"
            file_filter = "CSV 文件 (*.csv)"

        target, _ = QFileDialog.getSaveFileName(self, "导出词库", filename, file_filter)
        if not target:
            return
        Path(target).write_text(data, encoding="utf-8-sig" if fmt == "csv" else "utf-8")
        QMessageBox.information(self, APP_NAME, f"已导出：{target}")

    def _import_entries(self) -> None:
        source, _ = QFileDialog.getOpenFileName(
            self,
            "导入词库",
            "",
            "词库文件 (*.json *.csv)",
        )
        if not source:
            return
        try:
            entries = self._parse_import_file(Path(source))
        except Exception as exc:  # noqa: BLE001 - converted to UI message.
            QMessageBox.warning(self, APP_NAME, f"导入失败：{exc}")
            return

        answer = QMessageBox.question(
            self,
            APP_NAME,
            "如遇重复原文，是否覆盖本地译文？\n选择“否”将跳过重复项。",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No
            | QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Cancel:
            return
        mode = "overwrite" if answer == QMessageBox.StandardButton.Yes else "skip"
        result = tm_manager.import_entries(entries, self.lang_pair, mode)
        QMessageBox.information(
            self,
            APP_NAME,
            f"导入完成：新增或更新 {result['inserted']} 条；"
            f"重复 {result['duplicates']} 条；跳过 {result['skipped']} 条。",
        )
        self._refresh_all()

    @staticmethod
    def _parse_import_file(path: Path) -> list[dict]:
        content = path.read_text(encoding="utf-8-sig")
        if path.suffix.lower() == ".json":
            data = json.loads(content)
            if not isinstance(data, list):
                raise ValueError("JSON 顶层应为数组")
            return data
        if path.suffix.lower() == ".csv":
            return list(csv.DictReader(io.StringIO(content)))
        raise ValueError(f"不支持的文件类型：{path.name}")

    def _toggle_cleaning(self) -> None:
        if self.phase == "cleaning":
            if self.clean_worker is not None:
                self.clean_worker.cancel()
                self.clean_status.setText("已发送中止请求，等待当前批次结束...")
            return
        if self.phase == "review":
            QMessageBox.information(
                self,
                APP_NAME,
                "请先处理当前清洗结果，再启动新的清洗。",
            )
            return
        self._start_cleaning()

    def _start_cleaning(self) -> None:
        stats = tm_manager.get_stats(self.lang_pair)
        if stats["unpinned"] <= 0:
            QMessageBox.information(self, APP_NAME, "当前语言对没有可清洗的未固定词条。")
            return
        try:
            resolve_effective_model_config(self.settings, ROLE_CLEANER)
        except LocalModelFollowNotAllowedError as exc:
            QMessageBox.warning(self, APP_NAME, str(exc))
            return
        self._on_cleaner_settings_changed()
        self._clear_clean_notice()
        self.phase = "cleaning"
        self.clean_progress = None
        self.clean_suggestions = []
        self._clean_session_target_lang = self._selected_target_lang()
        self._clean_session_source_lang = self._selected_source_lang()
        self._clean_session_lang_pair = self.lang_pair
        self.clean_progress_bar.setValue(0)
        self.clean_status.setText("清洗任务正在启动...")
        self.clean_worker = TmCleanWorker(
            lang_pair=self._clean_session_lang_pair,
            settings=self.settings.model_copy(deep=True),
            overwrite=self.settings.cleaner_mode == "overwrite",
            parent=self,
        )
        self.clean_worker.progress.connect(self._on_clean_progress)
        self.clean_worker.finished.connect(self._on_clean_finished)
        self.clean_worker.start()
        self._refresh_all()

    def _on_clean_progress(self, payload: object) -> None:
        progress = _normalize_clean_progress_payload(payload)
        if progress is None:
            return
        self.clean_progress = progress
        ratio, text, detail = _build_clean_progress_display(progress)
        self.clean_progress_bar.setValue(int(ratio * 100))
        self.clean_status.setText(f"{text}\n{detail}".strip())

    def _on_clean_finished(self, suggestions: object, message: str, ok: bool) -> None:
        self.clean_worker = None
        self.clean_progress_bar.setValue(0)
        if not ok:
            self.phase = "idle"
            self.clean_suggestions = []
            self._set_clean_notice(message or "清洗任务已结束。")
            if message and "中止" not in message:
                QMessageBox.warning(self, APP_NAME, f"清洗失败：{message}")
            self._finish_clean_session()
            return

        suggestions_list = list(suggestions)
        if self.settings.cleaner_mode == "overwrite":
            self.phase = "idle"
            self.clean_suggestions = []
            self._set_clean_notice(message or "清洗完成。")
            self._finish_clean_session()
            return

        if not suggestions_list:
            self.phase = "idle"
            self.clean_suggestions = []
            self._set_clean_notice("清洗完成，未发现需要修改的词条。")
            self._finish_clean_session()
            return

        self.phase = "review"
        self.clean_suggestions = suggestions_list
        self._set_clean_notice(f"清洗完成，发现 {len(suggestions_list)} 条建议，等待确认。")
        self._refresh_all()

    def _apply_clean_suggestions(self) -> None:
        table = getattr(self, "diff_table", None)
        if table is not None:
            for row, suggestion in enumerate(self.clean_suggestions):
                target_item = table.item(row, 3)
                if target_item is not None:
                    new_target = target_item.text().strip()
                    if not new_target:
                        QMessageBox.warning(self, APP_NAME, "建议译文不能为空。")
                        table.setCurrentCell(row, 3)
                        table.editItem(target_item)
                        return
                    suggestion.new_target = new_target
                item = table.item(row, 0)
                suggestion.accepted = (
                    item is not None and item.checkState() == Qt.CheckState.Checked
                )
        applied = apply_suggestions(
            self.clean_suggestions,
            auto_pin=self.auto_pin_check.isChecked(),
        )
        self.phase = "idle"
        self.clean_suggestions = []
        self._set_clean_notice(f"已写入 {applied} 条清洗建议。")
        self._finish_clean_session()

    def _discard_clean_suggestions(self) -> None:
        self.phase = "idle"
        self.clean_suggestions = []
        self._set_clean_notice("已放弃本次清洗建议。")
        self._finish_clean_session()

    def _finish_clean_session(self) -> None:
        self._clean_session_lang_pair = ""
        self._clean_session_target_lang = ""
        self._clean_session_source_lang = ""
        if not self._apply_pending_language_sync():
            self._refresh_all()

    def _open_cleaner_prompt_dialog(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("自定义深度清洗提示词")
        dialog.resize(760, 760)
        layout = QVBoxLayout(dialog)
        layout.setSpacing(10)

        intro = QLabel(f"语言对：{self.lang_pair}。内置规则只读，可追加或覆盖提示词。")
        intro.setWordWrap(True)
        intro.setObjectName("MutedText")
        layout.addWidget(intro)

        layout.addWidget(_label("内置提示词（只读）", "SectionTitle"))
        builtin_edit = QTextEdit()
        builtin_edit.setReadOnly(True)
        builtin_edit.setMinimumHeight(150)
        builtin_edit.setPlainText(
            get_clean_builtin_system_prompt(
                self.lang_pair,
                self.settings.custom_target_langs,
            )
        )
        layout.addWidget(builtin_edit)

        layout.addWidget(_label("当前语言的补充提示词", "SectionTitle"))
        extra_edit = QTextEdit()
        extra_edit.setMinimumHeight(100)
        extra_edit.setPlaceholderText("例如：统一项目缩写，保持译文简洁。")
        extra_edit.setPlainText(self.settings.cleaner_prompt_extras.get(self.lang_pair, ""))
        layout.addWidget(extra_edit)

        full_check = QCheckBox("启用完整提示词覆盖（高级）")
        full_override = self.settings.cleaner_full_prompt_overrides.get(self.lang_pair, "")
        full_check.setChecked(bool(full_override))
        _set_tooltip(
            full_check,
            "完整提示词覆盖",
            "启用后仅使用下方 Prompt。",
        )
        layout.addWidget(full_check)

        full_edit = QTextEdit()
        full_edit.setMinimumHeight(120)
        full_edit.setPlaceholderText("输入完整清洗 Prompt")
        full_edit.setPlainText(full_override)
        full_edit.setEnabled(full_check.isChecked())
        layout.addWidget(full_edit)

        layout.addWidget(_label("实际发送预览", "SectionTitle"))
        preview_edit = QTextEdit()
        preview_edit.setReadOnly(True)
        preview_edit.setMinimumHeight(150)
        layout.addWidget(preview_edit)

        def refresh_preview() -> None:
            preview_edit.setPlainText(
                build_clean_system_prompt(
                    lang_pair=self.lang_pair,
                    extra_prompt=extra_edit.toPlainText(),
                    full_override_prompt=(
                        full_edit.toPlainText()
                        if full_check.isChecked()
                        else ""
                    ),
                    custom_target_langs=self.settings.custom_target_langs,
                )
            )

        full_check.toggled.connect(full_edit.setEnabled)
        full_check.toggled.connect(refresh_preview)
        extra_edit.textChanged.connect(refresh_preview)
        full_edit.textChanged.connect(refresh_preview)
        refresh_preview()

        buttons = QDialogButtonBox()
        reset_button = buttons.addButton("恢复默认", QDialogButtonBox.ButtonRole.ResetRole)
        save_button = buttons.addButton("保存", QDialogButtonBox.ButtonRole.AcceptRole)
        cancel_button = buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        save_button.clicked.connect(dialog.accept)
        cancel_button.clicked.connect(dialog.reject)

        def reset_prompt() -> None:
            extra_edit.clear()
            full_check.setChecked(False)
            full_edit.clear()
            refresh_preview()

        reset_button.clicked.connect(reset_prompt)
        layout.addWidget(buttons)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        full_text = full_edit.toPlainText().strip() if full_check.isChecked() else ""
        if full_check.isChecked() and not full_text:
            QMessageBox.warning(self, APP_NAME, "请填写完整 Prompt，或关闭完整覆盖。")
            return
        self.settings.cleaner_prompt_extras = _update_lang_prompt_map(
            self.settings.cleaner_prompt_extras,
            self.lang_pair,
            extra_edit.toPlainText(),
        )
        self.settings.cleaner_full_prompt_overrides = _update_lang_prompt_map(
            self.settings.cleaner_full_prompt_overrides,
            self.lang_pair,
            full_text,
        )
        save_settings(self.settings)
        QMessageBox.information(self, APP_NAME, "清洗 Prompt 已更新。")
