"""Main window and shared sidebar for the native Qt interface."""

from __future__ import annotations

import html
from collections.abc import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app_meta import APP_NAME, APP_VERSION_LABEL
from config import (
    CHUNK_CLOUD_MAX,
    CHUNK_CLOUD_MIN,
    CHUNK_LOCAL_MAX,
    CHUNK_LOCAL_MIN,
    CLOUD_ENGINES,
    DOMAIN_PRESETS,
    OLLAMA_RECOMMENDED_MODELS,
    get_concurrency_bounds,
    is_valid_concurrency_unlock_code,
)
from core.connectivity_check import check_connectivity
from core.model_catalog import fetch_openai_compatible_models
from core.update_checker import check_for_updates
from native_app.pages.excel_translate import ExcelTranslatePage
from native_app.pages.tm_manager import TmManagerPage
from native_app.pages.word_translate import WordTranslatePage
from native_app.widgets import install_in_app_tooltips
from settings import AppSettings, get_key, save_key, save_settings


def _section_title(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("SectionTitle")
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
    widget.setToolTipDuration(3200)


def _field_label(
    text: str,
    title: str,
    summary: str,
    items: list[str] | None = None,
) -> QLabel:
    label = QLabel(text)
    _set_tooltip(label, title, summary, items)
    return label


def _cloud_provider_label(provider: str) -> str:
    for label, value in CLOUD_ENGINES.items():
        if value == provider:
            return label
    return next(iter(CLOUD_ENGINES.keys()))


def _cloud_provider_value(label: str) -> str:
    return CLOUD_ENGINES.get(label, next(iter(CLOUD_ENGINES.values())))


class Sidebar(QFrame):
    """Left navigation and global settings panel."""

    navigateRequested = Signal(str)
    settingsChanged = Signal()

    def __init__(self, settings: AppSettings):
        super().__init__()
        self.settings = settings
        self.setObjectName("Sidebar")
        self.setFixedWidth(330)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(14)

        brand = QLabel(APP_NAME)
        brand.setObjectName("BrandTitle")
        _set_tooltip(
            brand,
            APP_NAME,
            "Excel、Word 与记忆库管理入口。",
        )
        version = QLabel(f"by OA | {APP_VERSION_LABEL}")
        version.setObjectName("BrandMeta")
        root.addWidget(brand)
        root.addWidget(version)

        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)
        nav_items = [
            ("excel_translate", "表格翻译"),
            ("word_translate", "Word 翻译"),
            ("tm", "记忆库管理"),
        ]
        for index, (page, title) in enumerate(nav_items):
            button = QPushButton(title)
            button.setObjectName("NavButton")
            button.setCheckable(True)
            _set_tooltip(
                button,
                title,
                {
                    "excel_translate": "扫描 Excel 文件并执行批量翻译。",
                    "word_translate": "扫描 Word 文件并生成双语文档。",
                    "tm": "搜索、新增、固定和清理翻译记忆库。",
                }[page],
            )
            button.clicked.connect(lambda _=False, key=page: self.navigateRequested.emit(key))
            self._nav_group.addButton(button)
            root.addWidget(button)
            if index == 0:
                button.setChecked(True)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        self._form = QVBoxLayout(body)
        self._form.setContentsMargins(0, 4, 0, 0)
        self._form.setSpacing(12)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        self._build_domain_section()
        self._build_engine_section()
        self._build_tuning_section()
        self._sync_engine_visibility()
        self._form.addStretch(1)

    def set_active_page(self, page: str) -> None:
        page_to_index = {"excel_translate": 0, "word_translate": 1, "tm": 2}
        button = self._nav_group.buttons()[page_to_index.get(page, 0)]
        button.setChecked(True)

    def _build_card(self) -> tuple[QFrame, QVBoxLayout]:
        frame = QFrame()
        frame.setObjectName("Card")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)
        self._form.addWidget(frame)
        return frame, layout

    def _persist(self) -> None:
        save_settings(self.settings)
        self.settingsChanged.emit()

    def _build_domain_section(self) -> None:
        _, layout = self._build_card()
        layout.addWidget(_section_title("专业领域"))

        self.domain_combo = QComboBox()
        self.domain_combo.addItems(list(DOMAIN_PRESETS.keys()))
        _set_tooltip(
            self.domain_combo,
            "专业领域",
            "先选最接近当前资料的领域预设，再决定是否细调 Prompt。",
            [
                "预设会带入该领域常用术语、语气和翻译侧重。",
                "同一批文件尽量保持同一领域，结果通常更稳定。",
            ],
        )
        if self.settings.domain_preset in DOMAIN_PRESETS:
            self.domain_combo.setCurrentText(self.settings.domain_preset)
        self.domain_combo.currentTextChanged.connect(self._on_domain_changed)
        layout.addWidget(self.domain_combo)

        prompt_hint = QLabel("Prompt")
        prompt_hint.setObjectName("FieldHint")
        layout.addWidget(prompt_hint)

        self.prompt_edit = QTextEdit()
        self.prompt_edit.setMinimumHeight(112)
        self.prompt_edit.setPlainText(self.settings.custom_prompt)
        _set_tooltip(
            self.prompt_edit,
            "Prompt",
            "这是本次翻译的工作指令，会直接影响术语、语气和约束。",
            [
                "跟随领域预设时，可以在默认内容上小幅微调。",
                "建议只保留必要规则，避免重复和过长。",
            ],
        )
        self.prompt_edit.textChanged.connect(self._on_prompt_changed)
        layout.addWidget(self.prompt_edit)

    def _build_engine_section(self) -> None:
        _, layout = self._build_card()
        layout.addWidget(_section_title("翻译引擎"))

        self.mode_combo = QComboBox()
        self.mode_combo.addItem("云端 API", "cloud")
        self.mode_combo.addItem("本地 Ollama", "local")
        self.mode_combo.setCurrentIndex(0 if self.settings.engine.mode == "cloud" else 1)
        _set_tooltip(
            self.mode_combo,
            "翻译引擎",
            "选择本次任务使用云端 API 还是本地 Ollama。",
            [
                "云端 API 适合模型选择更多、通用质量更高的场景。",
                "本地 Ollama 适合隐私敏感文件，翻译内容不上传云端。",
            ],
        )
        self.mode_combo.currentIndexChanged.connect(self._on_engine_mode_changed)
        layout.addWidget(self.mode_combo)

        layout.addWidget(
            _field_label(
                "服务商",
                "服务商",
                "切换当前接入渠道。",
                ["不同服务商会影响 Base URL、模型名称和 API Key 的实际用法。"],
            )
        )
        self.provider_combo = QComboBox()
        self.provider_combo.addItems(list(CLOUD_ENGINES.keys()))
        self.provider_combo.setCurrentText(
            _cloud_provider_label(self.settings.engine.cloud_provider)
        )
        self.provider_combo.currentTextChanged.connect(self._on_provider_changed)
        layout.addWidget(self.provider_combo)

        layout.addWidget(
            _field_label(
                "API Key",
                "API Key",
                "用于当前云端服务商的身份认证。",
                ["密钥会保存到本机 keys.json，不显示明文。"],
            )
        )
        self.api_key_input = QLineEdit()
        self.api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_input.setText(get_key(self.settings.engine.cloud_provider))
        _set_tooltip(self.api_key_input, "API Key", "填写当前服务商的访问密钥。")
        self.api_key_input.editingFinished.connect(self._on_api_key_changed)
        layout.addWidget(self.api_key_input)

        layout.addWidget(
            _field_label(
                "Base URL",
                "Base URL",
                "云端接口地址，主要用于 OpenAI 兼容接口或自定义网关。",
            )
        )
        self.base_url_input = QLineEdit(self.settings.engine.cloud_base_url)
        _set_tooltip(
            self.base_url_input,
            "Base URL",
            "填写服务商提供的接口基础地址，例如 https://.../v1。",
        )
        self.base_url_input.editingFinished.connect(self._on_base_url_changed)
        layout.addWidget(self.base_url_input)

        layout.addWidget(
            _field_label(
                "模型名称",
                "模型名称",
                "决定本次实际调用的云端模型。",
                ["点击“获取模型”后，会把可用模型加载到下拉列表中。"],
            )
        )
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.addItem(self.settings.engine.cloud_model)
        self.model_combo.setCurrentText(self.settings.engine.cloud_model)
        _set_tooltip(
            self.model_combo,
            "模型名称",
            "可手动输入模型名，也可点击“获取模型”后从列表选择。",
        )
        self.model_combo.currentTextChanged.connect(self._on_model_changed)
        layout.addWidget(self.model_combo)

        self.model_catalog_status = QLabel("")
        self.model_catalog_status.setObjectName("FieldHint")
        self.model_catalog_status.setWordWrap(True)
        layout.addWidget(self.model_catalog_status)

        model_buttons = QHBoxLayout()
        fetch_models = QPushButton("获取模型")
        _set_tooltip(
            fetch_models,
            "获取模型",
            "向当前 OpenAI 兼容服务商请求模型列表。",
            ["成功后会把模型名称加载到上方下拉框，便于直接选择。"],
        )
        fetch_models.clicked.connect(self._fetch_models)
        model_buttons.addWidget(fetch_models)
        test_conn = QPushButton("测试连接")
        _set_tooltip(
            test_conn,
            "测试连接",
            "按当前服务商、Base URL、API Key 和模型配置发起连通性检查。",
        )
        test_conn.clicked.connect(self._test_connectivity)
        model_buttons.addWidget(test_conn)
        layout.addLayout(model_buttons)

        layout.addWidget(
            _field_label(
                "Ollama 模型",
                "Ollama 模型",
                "本地模型运行在当前设备上，适合对数据不出本机有要求的翻译任务。",
            )
        )
        self.ollama_combo = QComboBox()
        self.ollama_combo.setEditable(True)
        self.ollama_combo.addItems(OLLAMA_RECOMMENDED_MODELS)
        self.ollama_combo.setCurrentText(self.settings.engine.ollama_model)
        _set_tooltip(self.ollama_combo, "Ollama 模型", "选择或输入本机已安装的 Ollama 模型名。")
        self.ollama_combo.currentTextChanged.connect(self._on_ollama_changed)
        layout.addWidget(self.ollama_combo)

        update_button = QPushButton("检查更新")
        _set_tooltip(update_button, "检查更新", "检查 GitHub 发布页是否存在新版安装包。")
        update_button.clicked.connect(self._check_updates)
        layout.addWidget(update_button)

    def _build_tuning_section(self) -> None:
        _, layout = self._build_card()
        layout.addWidget(_section_title("吞吐调优"))

        layout.addWidget(
            _field_label(
                "批次大小",
                "批次大小",
                "每次提交给模型的一组文本数量。",
                ["批次越大通常越快，但更容易超时或带来上下文压力。"],
            )
        )
        self.batch_spin = QSpinBox()
        self.batch_spin.setRange(CHUNK_CLOUD_MIN, CHUNK_CLOUD_MAX)
        self.batch_spin.setValue(self.settings.engine.batch_size)
        self.batch_spin.valueChanged.connect(self._on_batch_changed)
        layout.addWidget(self.batch_spin)

        layout.addWidget(
            _field_label(
                "并发数",
                "并发数",
                "同时发起的翻译请求数量。",
                ["并发越高吞吐越高，但也更容易触发限流或占用本机资源。"],
            )
        )
        self.concurrency_input = QLineEdit(
            str(
                self.settings.engine.ollama_concurrency
                if self.settings.engine.mode == "local"
                else self.settings.engine.concurrency
            )
        )
        self.concurrency_input.editingFinished.connect(self._on_concurrency_changed)
        layout.addWidget(self.concurrency_input)

    def _on_domain_changed(self, value: str) -> None:
        self.settings.domain_preset = value
        self._persist()

    def _on_prompt_changed(self) -> None:
        self.settings.custom_prompt = self.prompt_edit.toPlainText()
        self._persist()

    def _on_engine_mode_changed(self) -> None:
        self.settings.engine.mode = self.mode_combo.currentData()
        self._sync_engine_visibility()
        self._persist()

    def _on_provider_changed(self, label: str) -> None:
        provider = _cloud_provider_value(label)
        self.settings.engine.cloud_provider = provider
        self.api_key_input.setText(get_key(provider))
        self._persist()

    def _on_api_key_changed(self) -> None:
        save_key(self.settings.engine.cloud_provider, self.api_key_input.text().strip())
        self.settingsChanged.emit()

    def _on_base_url_changed(self) -> None:
        self.settings.engine.cloud_base_url = self.base_url_input.text().strip()
        self._persist()

    def _on_model_changed(self) -> None:
        self.settings.engine.cloud_model = self.model_combo.currentText().strip()
        self._persist()

    def _on_ollama_changed(self, value: str) -> None:
        self.settings.engine.ollama_model = value.strip()
        self._persist()

    def _on_batch_changed(self, value: int) -> None:
        self.settings.engine.batch_size = value
        self._persist()

    def _on_concurrency_changed(self) -> None:
        raw = self.concurrency_input.text().strip()
        if is_valid_concurrency_unlock_code(raw):
            self.settings.engine.concurrency_unlocked = True
        minimum, maximum = get_concurrency_bounds(
            self.settings.engine.mode,
            self.settings.engine.concurrency_unlocked,
        )
        try:
            value = int(raw)
        except ValueError:
            value = (
                self.settings.engine.ollama_concurrency
                if self.settings.engine.mode == "local"
                else self.settings.engine.concurrency
            )
        value = max(minimum, min(maximum, value))
        if self.settings.engine.mode == "local":
            self.settings.engine.ollama_concurrency = value
        else:
            self.settings.engine.concurrency = value
        self.concurrency_input.setText(str(value))
        self._persist()

    def _sync_engine_visibility(self) -> None:
        is_cloud = self.settings.engine.mode == "cloud"
        cloud_widgets = (
            self.provider_combo,
            self.api_key_input,
            self.base_url_input,
            self.model_combo,
        )
        for widget in cloud_widgets:
            widget.setEnabled(is_cloud)
        self.ollama_combo.setEnabled(not is_cloud)
        if self.settings.engine.mode == "local":
            self.batch_spin.setRange(CHUNK_LOCAL_MIN, CHUNK_LOCAL_MAX)
            self.batch_spin.setValue(
                max(CHUNK_LOCAL_MIN, min(CHUNK_LOCAL_MAX, self.settings.engine.batch_size))
            )
        else:
            self.batch_spin.setRange(CHUNK_CLOUD_MIN, CHUNK_CLOUD_MAX)
            self.batch_spin.setValue(
                max(CHUNK_CLOUD_MIN, min(CHUNK_CLOUD_MAX, self.settings.engine.batch_size))
            )

    def _with_busy_cursor(self, fn: Callable[[], str]) -> None:
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            message = fn()
        finally:
            QApplication.restoreOverrideCursor()
        QMessageBox.information(self, APP_NAME, message)

    def _test_connectivity(self) -> None:
        def run() -> str:
            result = check_connectivity(self.settings)
            return result.message if result.ok else f"{result.message}\n{result.detail}"

        self._with_busy_cursor(run)

    def _fetch_models(self) -> None:
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            result = fetch_openai_compatible_models(
                provider=self.settings.engine.cloud_provider,
                api_key=get_key(self.settings.engine.cloud_provider),
                base_url=self.settings.engine.cloud_base_url,
            )
        finally:
            QApplication.restoreOverrideCursor()

        if not result.ok or not result.models:
            message = f"{result.message}\n{result.detail}".strip()
            self.model_catalog_status.setText(message)
            QMessageBox.warning(self, APP_NAME, message)
            return

        previous_model = self.model_combo.currentText().strip()
        selected_model = previous_model if previous_model in result.models else result.models[0]
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        self.model_combo.addItems(result.models)
        self.model_combo.setCurrentText(selected_model)
        self.model_combo.blockSignals(False)
        self._on_model_changed()
        self.model_catalog_status.setText(f"{result.message} 可从下拉列表选择。")
        self.model_combo.showPopup()

    def _check_updates(self) -> None:
        def run() -> str:
            result = check_for_updates()
            if result.has_update:
                return f"{result.message}\n{result.release_url}"
            return result.message

        self._with_busy_cursor(run)


class NativeMainWindow(QMainWindow):
    """Top-level native desktop window."""

    def __init__(self, settings: AppSettings):
        super().__init__()
        app = QApplication.instance()
        if app is not None:
            install_in_app_tooltips(app)
        self.settings = settings
        self.setWindowTitle(APP_NAME)

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.sidebar = Sidebar(settings)
        self.stack = QStackedWidget()
        self.pages = {
            "excel_translate": ExcelTranslatePage(settings),
            "word_translate": WordTranslatePage(settings),
            "tm": TmManagerPage(settings),
        }
        for page in self.pages.values():
            self.stack.addWidget(page)

        self.sidebar.navigateRequested.connect(self._navigate)
        self.sidebar.settingsChanged.connect(self.pages["excel_translate"].refresh_settings)
        self.sidebar.settingsChanged.connect(self.pages["word_translate"].refresh_settings)
        self.sidebar.settingsChanged.connect(self.pages["tm"].refresh_settings)
        self.pages["excel_translate"].languageChanged.connect(
            self._sync_tm_language_from_translation
        )
        self.pages["word_translate"].languageChanged.connect(
            self._sync_tm_language_from_translation
        )

        layout.addWidget(self.sidebar)
        layout.addWidget(self.stack, 1)
        self.setCentralWidget(central)
        self._build_menu()

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("文件")
        quit_action = QAction("退出", self)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

    def _navigate(self, page: str) -> None:
        page_order = ["excel_translate", "word_translate", "tm"]
        self.stack.setCurrentIndex(page_order.index(page))
        self.sidebar.set_active_page(page)

    def _sync_tm_language_from_translation(
        self,
        target_lang: str,
        source_lang: str,
    ) -> None:
        self.pages["tm"].sync_language_from_translation(target_lang, source_lang)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API name.
        save_settings(self.settings)
        super().closeEvent(event)
