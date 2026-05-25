"""Main window and shared sidebar for the native Qt interface."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
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
    IMAGE_GENERATION_MODEL_PROVIDERS,
    OLLAMA_RECOMMENDED_MODELS,
    VISION_TEXT_MODEL_PROVIDERS,
    get_concurrency_bounds,
    is_valid_concurrency_unlock_code,
)
from core.connectivity_check import check_connectivity
from core.image_generation import check_image_generation_connectivity
from core.model_catalog import build_model_catalog_signature, fetch_openai_compatible_models
from core.model_roles import (
    FOLLOW_SOURCE_LABELS,
    ROLE_CLEANER,
    ROLE_IMAGE,
    ROLE_PDF_REVIEW,
    ROLE_TRANSLATION,
    SOURCE_INDEPENDENT,
    ChainedModelFollowError,
    allowed_source_roles,
    get_role_settings,
    provider_supports_capability,
    resolve_effective_model_config,
    role_label,
    settings_for_text_role,
)
from core.pdf_review import check_pdf_review_connectivity
from core.update_checker import check_for_updates
from native_app.pages.excel_translate import ExcelTranslatePage
from native_app.pages.pdf_translate import PdfTranslatePage
from native_app.pages.tm_manager import TmManagerPage
from native_app.pages.word_translate import WordTranslatePage
from native_app.widgets import (
    build_app_tooltip_html,
    create_centered_option_combo,
    create_editable_combo,
    create_option_combo,
    install_in_app_tooltips,
    refresh_combo_completer,
    select_combo_text_match,
)
from settings import AppSettings, get_key, save_key, save_settings


def _section_title(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("SectionTitle")
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
    widget.setToolTipDuration(5200)


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


BRAND_TOOLTIP = {
    "title": APP_NAME,
    "summary": (
        f"{APP_NAME} 是一个面向 Excel、Word 和 PDF 文档的本地翻译器，"
        "左侧完成配置，右侧执行任务并维护统一记忆库。"
    ),
    "items": [
        "Excel 翻译页用于扫描表格文件、执行批量翻译和查看结果。",
        "Word 翻译页用于扫描 DOCX 文件并生成双语 Word。",
        "PDF 翻译页用于执行版式图像翻译并生成可审阅输出包。",
        "记忆库管理页用于搜索、新增、固定和清理共享词条。",
    ],
}

DOMAIN_TOOLTIP = {
    "title": "专业领域",
    "summary": "先选最接近当前资料的领域预设，再决定是否细调 Prompt。",
    "items": [
        "预设会带入该领域常用术语、语气和翻译侧重。",
        "常规任务优先直接使用预设，只有特殊要求时再改 Prompt。",
        "同一批文件尽量保持同一领域，结果通常更稳定。",
    ],
}

PROMPT_TOOLTIP = {
    "title": "Prompt",
    "summary": "这是本次翻译的工作指令，会直接影响术语、语气和约束。",
    "items": [
        "跟随领域预设时，可以在默认内容上小幅微调。",
        "清空修改内容会恢复为当前预设的默认值。",
        "选择“自定义”后，这里的内容会完整作为本次任务 Prompt。",
        "建议只保留必要规则，避免重复和过长。",
    ],
}

ENGINE_TOOLTIP = {
    "title": "模型配置",
    "summary": "按模型用途切换翻译、深度清洗和图像生成配置。",
    "items": [
        "翻译模型可使用云端 API 或本地 Ollama。",
        "深度清洗模型和图像生成模型使用云端配置，可跟随上游模型的服务商凭据。",
        "获取模型和测试连接始终作用于当前选中的模型用途。",
    ],
}

CLOUD_SETTINGS_TOOLTIP = {
    "title": "云端 API 设置",
    "summary": "这组配置决定请求会发送到哪个云端服务，以及由哪个模型完成翻译。",
    "items": [
        "服务商用于切换当前接入渠道。",
        "API Key 用于身份认证。",
        "Base URL 主要用于兼容接口或自定义网关。",
        "模型名称决定本次实际调用的云端模型。",
    ],
}

OLLAMA_TOOLTIP = {
    "title": "Ollama 模型",
    "summary": "本地模型运行在当前设备上，适合对数据不出本机有要求的翻译任务。",
    "items": [
        "推荐列表适合快速选择常用模型，也可以手动填写本机已安装的其他模型名。",
        "模型越大，通常效果更好，但也会占用更多本机资源。",
        "只要使用本地引擎，翻译内容就不会发送到外部云端服务。",
    ],
}

TUNING_TOOLTIP = {
    "title": "吞吐调优",
    "summary": "批次大小和并发数一起决定速度、稳定性和资源占用。",
    "items": [
        "批次越大通常越快，但更容易超时或带来上下文压力。",
        "并发越高整体吞吐越高，但也更容易限流或占满本机资源。",
        "遇到超时、失败重试或机器负载偏高时，优先把这两项调低。",
    ],
}


class Sidebar(QFrame):
    """Left navigation and global settings panel."""

    navigateRequested = Signal(str)
    settingsChanged = Signal()

    def __init__(self, settings: AppSettings):
        super().__init__()
        self.settings = settings
        self.setObjectName("Sidebar")
        self.setFixedWidth(330)
        self._current_model_role = ROLE_TRANSLATION
        self._model_catalog_signature = ""
        self._model_catalog_models: list[str] = []
        self._updating_prompt_edit = False

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(14)

        brand = QLabel(APP_NAME)
        brand.setObjectName("BrandTitle")
        _set_tooltip(
            brand,
            BRAND_TOOLTIP["title"],
            BRAND_TOOLTIP["summary"],
            BRAND_TOOLTIP["items"],
        )
        version = QLabel(f"by OA | {APP_VERSION_LABEL}")
        version.setObjectName("BrandMeta")
        root.addWidget(brand)
        root.addWidget(version)

        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)
        nav_items = [
            ("excel_translate", "Excel 翻译"),
            ("word_translate", "Word 翻译"),
            ("pdf_translate", "PDF 翻译"),
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
                    "pdf_translate": "执行 PDF 版式图像翻译。",
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
        self._refresh_source_role_options()
        self._sync_model_role_fields()
        self._sync_engine_visibility()
        self._form.addStretch(1)

    def set_active_page(self, page: str) -> None:
        page_to_index = {"excel_translate": 0, "word_translate": 1, "pdf_translate": 2, "tm": 3}
        button = self._nav_group.buttons()[page_to_index.get(page, 0)]
        button.setChecked(True)

    def select_model_role(self, role: str) -> None:
        if not hasattr(self, "model_role_combo"):
            return
        index = self.model_role_combo.findData(role)
        if index >= 0:
            self.model_role_combo.setCurrentIndex(index)

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
        title = _section_title("专业领域")
        _set_tooltip(
            title,
            DOMAIN_TOOLTIP["title"],
            DOMAIN_TOOLTIP["summary"],
            DOMAIN_TOOLTIP["items"],
        )
        layout.addWidget(title)

        self.domain_combo = create_option_combo()
        self.domain_combo.addItems(list(DOMAIN_PRESETS.keys()))
        _set_tooltip(
            self.domain_combo,
            DOMAIN_TOOLTIP["title"],
            DOMAIN_TOOLTIP["summary"],
            DOMAIN_TOOLTIP["items"],
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
        self.prompt_edit.setPlaceholderText("输入专属 System Prompt...")
        self.prompt_edit.setPlainText(self._domain_prompt_value(self.domain_combo.currentText()))
        _set_tooltip(
            self.prompt_edit,
            PROMPT_TOOLTIP["title"],
            PROMPT_TOOLTIP["summary"],
            PROMPT_TOOLTIP["items"],
        )
        self.prompt_edit.textChanged.connect(self._on_prompt_changed)
        layout.addWidget(self.prompt_edit)

    def _domain_default_prompt(self, preset: str) -> str:
        preset_value = DOMAIN_PRESETS.get(preset, "")
        if isinstance(preset_value, dict):
            return preset_value.get("_base", "")
        return str(preset_value)

    def _domain_prompt_value(self, preset: str) -> str:
        if preset == "自定义":
            return self.settings.custom_prompt
        return self.settings.domain_prompt_overrides.get(
            preset,
            self._domain_default_prompt(preset),
        )

    def _refresh_domain_prompt_editor(self) -> None:
        self._updating_prompt_edit = True
        try:
            self.prompt_edit.setPlainText(
                self._domain_prompt_value(self.settings.domain_preset)
            )
        finally:
            self._updating_prompt_edit = False

    def _build_engine_section(self) -> None:
        _, layout = self._build_card()
        title = _section_title("模型配置")
        _set_tooltip(
            title,
            ENGINE_TOOLTIP["title"],
            ENGINE_TOOLTIP["summary"],
            ENGINE_TOOLTIP["items"],
        )
        layout.addWidget(title)

        self.model_role_combo = create_centered_option_combo()
        self.model_role_combo.setObjectName("ModelRoleCombo")
        self.model_role_combo.addItem(role_label(ROLE_TRANSLATION), ROLE_TRANSLATION)
        self.model_role_combo.addItem(role_label(ROLE_CLEANER), ROLE_CLEANER)
        self.model_role_combo.addItem("PDF 图像翻译模型", ROLE_IMAGE)
        self.model_role_combo.currentIndexChanged.connect(self._on_model_role_changed)
        _set_tooltip(
            self.model_role_combo,
            "模型用途",
            "切换当前正在配置的模型用途。",
            ["下方服务商、模型名和共用操作会随用途切换。"],
        )
        layout.addWidget(self.model_role_combo)

        self.pdf_image_generation_hint = QLabel("PDF 生图模型")
        self.pdf_image_generation_hint.setObjectName("FieldHint")
        self.pdf_image_generation_hint.setWordWrap(True)
        layout.addWidget(self.pdf_image_generation_hint)

        self.source_role_label = _field_label(
            "配置来源",
            "配置来源",
            "选择独立配置，或跟随上游模型的服务商、API Key 与 Base URL。",
            ["不允许链式跟随；模型名称仍可单独填写。"],
        )
        layout.addWidget(self.source_role_label)
        self.source_role_combo = create_option_combo()
        self.source_role_combo.currentIndexChanged.connect(self._on_source_role_changed)
        layout.addWidget(self.source_role_combo)

        self.mode_combo = create_option_combo()
        self.mode_combo.addItem("云端 API", "cloud")
        self.mode_combo.addItem("本地 Ollama", "local")
        self.mode_combo.setCurrentIndex(0 if self.settings.engine.mode == "cloud" else 1)
        _set_tooltip(
            self.mode_combo,
            ENGINE_TOOLTIP["title"],
            ENGINE_TOOLTIP["summary"],
            ENGINE_TOOLTIP["items"],
        )
        self.mode_combo.currentIndexChanged.connect(self._on_engine_mode_changed)
        layout.addWidget(self.mode_combo)

        layout.addWidget(
            _field_label(
                "服务商",
                "服务商",
                "切换当前接入渠道，决定 API Key、Base URL 和模型名称的实际用法。",
                CLOUD_SETTINGS_TOOLTIP["items"],
            )
        )
        self.provider_combo = create_option_combo()
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
        _set_tooltip(
            self.api_key_input,
            "API Key",
            "用于当前云端服务商的身份认证，密钥会保存到本机 keys.json，不显示明文。",
            ["更换 API Key 后，已获取的模型列表会自动失效，需要重新获取。"],
        )
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
            [
                "主要用于 OpenAI 兼容接口或自定义网关。",
                "更换 Base URL 后，已获取的模型列表会自动失效，需要重新获取。",
            ],
        )
        self.base_url_input.editingFinished.connect(self._on_base_url_changed)
        layout.addWidget(self.base_url_input)

        layout.addWidget(
            _field_label(
                "模型名称",
                "模型名称",
                "决定本次实际调用的云端模型。",
                [
                    "可手动输入模型名，也可点击“获取模型”后从列表选择。",
                    "输入部分模型名后按回车，会优先匹配已加载列表中的模型。",
                    "只要 API Key、Base URL 和服务商不变，已获取列表会保留在下拉框中。",
                ],
            )
        )
        self.model_combo = create_editable_combo()
        self.model_combo.addItem(self.settings.engine.cloud_model)
        self.model_combo.setCurrentText(self.settings.engine.cloud_model)
        refresh_combo_completer(self.model_combo)
        _set_tooltip(
            self.model_combo,
            "模型名称",
            "可手动输入模型名，也可点击“获取模型”后从列表选择。",
            [
                "输入部分模型名后按回车，会优先匹配已加载列表中的模型。",
                "API 配置未变化时，模型列表会继续保留，方便反复切换。",
            ],
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
            [
                "成功后会把模型名称加载到上方下拉框，便于直接选择。",
                "后续只要服务商、API Key、Base URL 不变，列表会继续保留。",
            ],
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

        self._build_pdf_review_model_section(layout)

        layout.addWidget(
            _field_label(
                "Ollama 模型",
                OLLAMA_TOOLTIP["title"],
                OLLAMA_TOOLTIP["summary"],
                OLLAMA_TOOLTIP["items"],
            )
        )
        self.ollama_combo = create_editable_combo()
        self.ollama_combo.addItems(OLLAMA_RECOMMENDED_MODELS)
        self.ollama_combo.setCurrentText(self.settings.engine.ollama_model)
        refresh_combo_completer(self.ollama_combo)
        _set_tooltip(
            self.ollama_combo,
            OLLAMA_TOOLTIP["title"],
            OLLAMA_TOOLTIP["summary"],
            OLLAMA_TOOLTIP["items"],
        )
        self.ollama_combo.currentTextChanged.connect(self._on_ollama_changed)
        layout.addWidget(self.ollama_combo)

        update_button = QPushButton("检查更新")
        _set_tooltip(update_button, "检查更新", "检查 GitHub 发布页是否存在新版安装包。")
        update_button.clicked.connect(self._check_updates)
        layout.addWidget(update_button)

    def _build_pdf_review_model_section(self, parent_layout: QVBoxLayout) -> None:
        self.pdf_review_frame = QFrame()
        self.pdf_review_frame.setObjectName("RecoveryCard")
        layout = QVBoxLayout(self.pdf_review_frame)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        title_row = QHBoxLayout()
        title_row.addWidget(_section_title("PDF 翻译审核模型"))
        optional = QLabel("可选")
        optional.setObjectName("RecoveryBadge")
        title_row.addStretch(1)
        title_row.addWidget(optional)
        layout.addLayout(title_row)

        hint = QLabel("仅在 PDF 翻译页启用“翻译审核”后使用；未启用时无需配置。")
        hint.setObjectName("FieldHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        layout.addWidget(
            _field_label(
                "配置来源",
                "配置来源",
                "审核模型可独立配置，也可跟随翻译模型的服务商、API Key 与 Base URL。",
                ["模型名称仍可单独填写。"],
            )
        )
        self.review_source_role_combo = create_option_combo()
        self.review_source_role_combo.currentIndexChanged.connect(
            self._on_review_source_role_changed
        )
        layout.addWidget(self.review_source_role_combo)

        layout.addWidget(_field_label("服务商", "服务商", "PDF 翻译审核模型使用图像理解 + 文本输出能力。"))
        self.review_provider_combo = create_option_combo()
        self.review_provider_combo.addItems(list(VISION_TEXT_MODEL_PROVIDERS.keys()))
        self.review_provider_combo.currentTextChanged.connect(self._on_review_provider_changed)
        layout.addWidget(self.review_provider_combo)

        layout.addWidget(_field_label("API Key", "API Key", "用于审核模型服务商的身份认证。"))
        self.review_api_key_input = QLineEdit()
        self.review_api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.review_api_key_input.editingFinished.connect(self._on_review_api_key_changed)
        layout.addWidget(self.review_api_key_input)

        layout.addWidget(_field_label("Base URL", "Base URL", "审核模型的 OpenAI 兼容接口基础地址。"))
        self.review_base_url_input = QLineEdit()
        self.review_base_url_input.editingFinished.connect(self._on_review_base_url_changed)
        layout.addWidget(self.review_base_url_input)

        layout.addWidget(_field_label("模型名称", "模型名称", "填写具备图像理解能力的审核模型。"))
        self.review_model_combo = create_editable_combo()
        self.review_model_combo.currentTextChanged.connect(self._on_review_model_changed)
        layout.addWidget(self.review_model_combo)

        self.review_model_status = QLabel("")
        self.review_model_status.setObjectName("FieldHint")
        self.review_model_status.setWordWrap(True)
        layout.addWidget(self.review_model_status)

        buttons = QHBoxLayout()
        fetch_models = QPushButton("获取审核模型")
        fetch_models.clicked.connect(self._fetch_review_models)
        buttons.addWidget(fetch_models)
        test_conn = QPushButton("测试审核连接")
        test_conn.clicked.connect(self._test_review_connectivity)
        buttons.addWidget(test_conn)
        layout.addLayout(buttons)

        parent_layout.addWidget(self.pdf_review_frame)

    def _build_tuning_section(self) -> None:
        _, layout = self._build_card()
        title = _section_title("吞吐调优")
        _set_tooltip(
            title,
            TUNING_TOOLTIP["title"],
            TUNING_TOOLTIP["summary"],
            TUNING_TOOLTIP["items"],
        )
        layout.addWidget(title)

        layout.addWidget(
            _field_label(
                "批次大小",
                "批次大小",
                "每次提交给模型的一组文本数量。",
                TUNING_TOOLTIP["items"],
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
                TUNING_TOOLTIP["items"],
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
        self._refresh_domain_prompt_editor()
        self._persist()

    def _on_prompt_changed(self) -> None:
        if self._updating_prompt_edit:
            return
        prompt = self.prompt_edit.toPlainText()
        preset = self.settings.domain_preset
        if preset == "自定义":
            self.settings.custom_prompt = prompt
        else:
            default_prompt = self._domain_default_prompt(preset)
            if not prompt.strip() or prompt.strip() == default_prompt.strip():
                self.settings.domain_prompt_overrides.pop(preset, None)
            else:
                self.settings.domain_prompt_overrides[preset] = prompt
        self._persist()

    def _current_role(self) -> str:
        return str(
            self.model_role_combo.currentData()
            if hasattr(self, "model_role_combo")
            else self._current_model_role
        ) or ROLE_TRANSLATION

    def _role_settings(self):
        return get_role_settings(self.settings, self._current_role())

    def _is_following_current_role(self) -> bool:
        role_settings = self._role_settings()
        return bool(role_settings and role_settings.source_role != SOURCE_INDEPENDENT)

    def _on_model_role_changed(self) -> None:
        self._current_model_role = self._current_role()
        self._refresh_source_role_options()
        self._sync_model_role_fields()
        self._sync_engine_visibility()

    def _refresh_source_role_options(self) -> None:
        role = self._current_role()
        self.source_role_combo.blockSignals(True)
        self.source_role_combo.clear()
        for source in allowed_source_roles(role):
            self.source_role_combo.addItem(FOLLOW_SOURCE_LABELS.get(source, source), source)
        role_settings = self._role_settings()
        current = role_settings.source_role if role_settings else SOURCE_INDEPENDENT
        index = self.source_role_combo.findData(current)
        self.source_role_combo.setCurrentIndex(index if index >= 0 else 0)
        self.source_role_combo.blockSignals(False)
        visible = role != ROLE_TRANSLATION
        self.source_role_label.setVisible(visible)
        self.source_role_combo.setVisible(visible)

    def _on_source_role_changed(self) -> None:
        role_settings = self._role_settings()
        if role_settings is None:
            return
        previous = role_settings.source_role
        selected = str(self.source_role_combo.currentData() or SOURCE_INDEPENDENT)
        role_settings.source_role = selected
        try:
            resolve_effective_model_config(self.settings, self._current_role())
        except ChainedModelFollowError as exc:
            role_settings.source_role = previous
            QMessageBox.warning(self, APP_NAME, str(exc))
        self._refresh_source_role_options()
        self._sync_model_role_fields()
        self._sync_engine_visibility()
        self._persist()

    def _on_review_source_role_changed(self) -> None:
        role_settings = self.settings.pdf_review_model_role
        previous = role_settings.source_role
        selected = str(self.review_source_role_combo.currentData() or SOURCE_INDEPENDENT)
        role_settings.source_role = selected
        try:
            resolve_effective_model_config(self.settings, ROLE_PDF_REVIEW)
        except ChainedModelFollowError as exc:
            role_settings.source_role = previous
            QMessageBox.warning(self, APP_NAME, str(exc))
        self._sync_review_model_fields()
        self._persist()

    def _on_review_provider_changed(self, label: str) -> None:
        role_settings = self.settings.pdf_review_model_role
        if role_settings.source_role != SOURCE_INDEPENDENT:
            return
        role_settings.cloud_provider = VISION_TEXT_MODEL_PROVIDERS.get(
            label,
            role_settings.cloud_provider,
        )
        self.review_api_key_input.setText(get_key(role_settings.cloud_provider))
        self._refresh_review_role_status()
        self._persist()

    def _on_review_api_key_changed(self) -> None:
        config = resolve_effective_model_config(self.settings, ROLE_PDF_REVIEW)
        save_key(config.provider, self.review_api_key_input.text().strip())
        self.settingsChanged.emit()

    def _on_review_base_url_changed(self) -> None:
        role_settings = self.settings.pdf_review_model_role
        if role_settings.source_role == SOURCE_INDEPENDENT:
            role_settings.cloud_base_url = self.review_base_url_input.text().strip()
        self._refresh_review_role_status()
        self._persist()

    def _on_review_model_changed(self) -> None:
        self.settings.pdf_review_model_role.cloud_model = self.review_model_combo.currentText().strip()
        self._refresh_review_role_status()
        self._persist()

    def _sync_model_role_fields(self) -> None:
        role = self._current_role()
        previous_signature = self._current_model_catalog_signature()
        try:
            config = resolve_effective_model_config(self.settings, role)
        except ChainedModelFollowError:
            role_settings = self._role_settings()
            if role_settings is not None:
                role_settings.source_role = SOURCE_INDEPENDENT
            config = resolve_effective_model_config(self.settings, role)

        self._reload_provider_options(config.provider)
        self.pdf_image_generation_hint.setVisible(role == ROLE_IMAGE)
        if role == ROLE_IMAGE:
            self.pdf_image_generation_hint.setText("PDF 生图模型（必填）")
        self.mode_combo.blockSignals(True)
        self.mode_combo.setCurrentIndex(0 if self.settings.engine.mode == "cloud" else 1)
        self.mode_combo.blockSignals(False)
        self.provider_combo.blockSignals(True)
        self.provider_combo.setCurrentText(_cloud_provider_label(config.provider))
        self.provider_combo.blockSignals(False)
        self.api_key_input.setText(get_key(config.provider))
        self.base_url_input.setText(config.base_url)
        self._set_model_combo_items([config.model])
        self._clear_model_catalog_if_signature_changed(previous_signature)
        self._refresh_role_status()
        self._sync_review_model_fields()

    def _reload_provider_options(self, provider: str) -> None:
        role = self._current_role()
        follows = self._is_following_current_role()
        provider_items = (
            IMAGE_GENERATION_MODEL_PROVIDERS
            if role == ROLE_IMAGE and not follows
            else CLOUD_ENGINES
        )
        current_label = _cloud_provider_label(provider)
        self.provider_combo.blockSignals(True)
        self.provider_combo.clear()
        self.provider_combo.addItems(list(provider_items.keys()))
        if self.provider_combo.findText(current_label) < 0:
            self.provider_combo.addItem(current_label)
        self.provider_combo.setCurrentText(current_label)
        self.provider_combo.blockSignals(False)

    def _sync_review_model_fields(self) -> None:
        if not hasattr(self, "review_source_role_combo"):
            return
        role_settings = self.settings.pdf_review_model_role
        self.review_source_role_combo.blockSignals(True)
        self.review_source_role_combo.clear()
        for source in allowed_source_roles(ROLE_PDF_REVIEW):
            self.review_source_role_combo.addItem(FOLLOW_SOURCE_LABELS.get(source, source), source)
        source_index = self.review_source_role_combo.findData(role_settings.source_role)
        self.review_source_role_combo.setCurrentIndex(source_index if source_index >= 0 else 0)
        self.review_source_role_combo.blockSignals(False)

        try:
            config = resolve_effective_model_config(self.settings, ROLE_PDF_REVIEW)
        except ChainedModelFollowError:
            role_settings.source_role = SOURCE_INDEPENDENT
            config = resolve_effective_model_config(self.settings, ROLE_PDF_REVIEW)
        except Exception:
            config = None

        provider = config.provider if config is not None else role_settings.cloud_provider
        self.review_provider_combo.blockSignals(True)
        self.review_provider_combo.clear()
        self.review_provider_combo.addItems(list(VISION_TEXT_MODEL_PROVIDERS.keys()))
        provider_label = _cloud_provider_label(provider)
        if self.review_provider_combo.findText(provider_label) < 0:
            self.review_provider_combo.addItem(provider_label)
        self.review_provider_combo.setCurrentText(provider_label)
        self.review_provider_combo.blockSignals(False)

        self.review_api_key_input.setText(get_key(provider))
        self.review_base_url_input.setText(config.base_url if config is not None else role_settings.cloud_base_url)
        self._set_review_model_combo_items([
            config.model if config is not None else role_settings.cloud_model
        ])
        self._refresh_review_role_status()
        self._sync_review_model_visibility()

    def _set_review_model_combo_items(self, models: list[str]) -> None:
        try:
            fallback_model = resolve_effective_model_config(
                self.settings,
                ROLE_PDF_REVIEW,
            ).model
        except Exception:
            fallback_model = self.settings.pdf_review_model_role.cloud_model
        current = self.review_model_combo.currentText().strip() or fallback_model
        options: list[str] = []
        for model in [current, *models]:
            value = str(model or "").strip()
            if value and value not in options:
                options.append(value)
        if not options:
            options.append("")
        self.review_model_combo.blockSignals(True)
        self.review_model_combo.clear()
        self.review_model_combo.addItems(options)
        self.review_model_combo.setCurrentText(current)
        refresh_combo_completer(self.review_model_combo)
        self.review_model_combo.blockSignals(False)

    def _sync_review_model_visibility(self) -> None:
        if not hasattr(self, "review_provider_combo"):
            return
        follows = self.settings.pdf_review_model_role.source_role != SOURCE_INDEPENDENT
        access_editable = not follows
        for widget in (
            self.review_provider_combo,
            self.review_api_key_input,
            self.review_base_url_input,
        ):
            widget.setEnabled(access_editable)
        self.review_model_combo.setEnabled(True)

    def _refresh_review_role_status(self) -> None:
        if not hasattr(self, "review_model_status"):
            return
        try:
            config = resolve_effective_model_config(self.settings, ROLE_PDF_REVIEW)
        except Exception as exc:  # noqa: BLE001 - status only.
            self.review_model_status.setText(str(exc))
            return
        status = self.settings.pdf_review_model_role.availability_status
        message = self.settings.pdf_review_model_role.availability_message
        capability_hint = (
            "图像理解审核能力可用。"
            if provider_supports_capability(config.provider, "vision_text")
            else "当前服务商不在图像理解审核能力列表中。"
        )
        state_text = {
            "available": "上次审核连接测试：可用",
            "unavailable": "上次审核连接测试：不可用",
        }.get(status, "尚未完成审核模型可用性校验")
        optional_hint = "可选项：未启用 PDF 翻译审核时可留空。"
        follow_hint = (
            f"\n当前正在{FOLLOW_SOURCE_LABELS.get(config.source_role)}，服务商/API Key/Base URL 只读。"
            if config.follows
            else ""
        )
        self.review_model_status.setText(
            f"{optional_hint}\n{state_text}。{capability_hint}"
            + (f"\n{message}" if message else "")
            + follow_hint
        )

    def _refresh_role_status(self) -> None:
        try:
            config = resolve_effective_model_config(self.settings, self._current_role())
        except Exception as exc:  # noqa: BLE001 - status only.
            self.model_catalog_status.setText(str(exc))
            return
        if config.role == ROLE_IMAGE:
            status = self.settings.image_model_role.availability_status
            message = self.settings.image_model_role.availability_message
            capability_hint = (
                "图像生成能力可用。"
                if provider_supports_capability(config.provider, "image")
                else "当前服务商不在图像生成能力列表中。"
            )
            state_text = {
                "available": "上次图像连接测试：可用",
                "unavailable": "上次图像连接测试：不可用",
            }.get(status, "尚未完成图像生成可用性校验")
            self.model_catalog_status.setText(
                f"{state_text}。{capability_hint}" + (f"\n{message}" if message else "")
            )
            return
        if config.follows:
            self.model_catalog_status.setText(
                f"当前{config.label}正在{FOLLOW_SOURCE_LABELS.get(config.source_role)}，服务商/API Key/Base URL 只读。"
            )

    def _on_engine_mode_changed(self) -> None:
        if self._current_role() != ROLE_TRANSLATION:
            return
        self.settings.engine.mode = self.mode_combo.currentData()
        self._sync_engine_visibility()
        self._persist()

    def _on_provider_changed(self, label: str) -> None:
        previous_signature = self._current_model_catalog_signature()
        provider = _cloud_provider_value(label)
        role_settings = self._role_settings()
        if self._current_role() == ROLE_TRANSLATION:
            self.settings.engine.cloud_provider = provider
        elif role_settings is not None and role_settings.source_role == SOURCE_INDEPENDENT:
            role_settings.cloud_provider = provider
        self.api_key_input.setText(get_key(provider))
        self._clear_model_catalog_if_signature_changed(previous_signature)
        self._refresh_role_status()
        self._persist()

    def _on_api_key_changed(self) -> None:
        previous_signature = self._current_model_catalog_signature()
        config = resolve_effective_model_config(self.settings, self._current_role())
        save_key(config.provider, self.api_key_input.text().strip())
        self._clear_model_catalog_if_signature_changed(previous_signature)
        self.settingsChanged.emit()

    def _on_base_url_changed(self) -> None:
        previous_signature = self._current_model_catalog_signature()
        role_settings = self._role_settings()
        if self._current_role() == ROLE_TRANSLATION:
            self.settings.engine.cloud_base_url = self.base_url_input.text().strip()
        elif role_settings is not None and role_settings.source_role == SOURCE_INDEPENDENT:
            role_settings.cloud_base_url = self.base_url_input.text().strip()
        self._clear_model_catalog_if_signature_changed(previous_signature)
        self._refresh_role_status()
        self._persist()

    def _on_model_changed(self) -> None:
        role_settings = self._role_settings()
        if self._current_role() == ROLE_TRANSLATION:
            self.settings.engine.cloud_model = self.model_combo.currentText().strip()
        elif role_settings is not None:
            role_settings.cloud_model = self.model_combo.currentText().strip()
        self._refresh_role_status()
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
        role = self._current_role()
        follows = self._is_following_current_role()
        is_translation = role == ROLE_TRANSLATION
        is_cloud = self.settings.engine.mode == "cloud" if is_translation else True
        self.mode_combo.setVisible(is_translation)
        cloud_widgets = (
            self.provider_combo,
            self.api_key_input,
            self.base_url_input,
            self.model_combo,
        )
        for widget in cloud_widgets:
            widget.setVisible(is_cloud)
        access_editable = is_cloud and not follows
        self.provider_combo.setEnabled(access_editable)
        self.api_key_input.setEnabled(access_editable)
        self.base_url_input.setEnabled(access_editable)
        self.model_combo.setEnabled(is_cloud)
        self.ollama_combo.setVisible(is_translation)
        self.ollama_combo.setEnabled(is_translation and not is_cloud)
        self.pdf_review_frame.setVisible(role == ROLE_IMAGE)
        self._sync_review_model_visibility()
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
            role = self._current_role()
            if role == ROLE_IMAGE:
                result = check_image_generation_connectivity(self.settings)
                save_settings(self.settings)
                return result.message if result.ok else f"{result.message}\n{result.detail}"
            result = check_connectivity(settings_for_text_role(self.settings, role))
            return result.message if result.ok else f"{result.message}\n{result.detail}"

        self._with_busy_cursor(run)
        self._refresh_role_status()

    def _test_review_connectivity(self) -> None:
        def run() -> str:
            result = check_pdf_review_connectivity(self.settings)
            save_settings(self.settings)
            return result.message if result.ok else f"{result.message}\n{result.detail}"

        self._with_busy_cursor(run)
        self._sync_review_model_fields()

    def _current_model_catalog_signature(self) -> str:
        try:
            config = resolve_effective_model_config(self.settings, self._current_role())
            provider = config.provider
            api_key = config.api_key
            base_url = config.base_url
        except Exception:
            provider = self.settings.engine.cloud_provider
            api_key = get_key(provider)
            base_url = self.settings.engine.cloud_base_url
        return build_model_catalog_signature(
            provider=provider,
            api_key=api_key,
            base_url=base_url,
        )

    def _clear_model_catalog_if_signature_changed(self, previous_signature: str) -> None:
        if self._current_model_catalog_signature() == previous_signature:
            return
        self._model_catalog_signature = ""
        self._model_catalog_models = []
        self._set_model_combo_items([self.model_combo.currentText().strip()])
        self.model_catalog_status.setText("API 配置已变化，请重新获取模型列表。")

    def _set_model_combo_items(
        self,
        models: list[str],
        *,
        include_current: bool = True,
    ) -> None:
        try:
            config = resolve_effective_model_config(self.settings, self._current_role())
            fallback_model = config.model
        except Exception:
            fallback_model = self.settings.engine.cloud_model
        current = self.model_combo.currentText().strip() or fallback_model
        options: list[str] = []
        seed_models = [current, *models] if include_current else models
        for model in seed_models:
            value = str(model or "").strip()
            if value and value not in options:
                options.append(value)
        if not options:
            options.append(fallback_model)

        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        self.model_combo.addItems(options)
        if current:
            self.model_combo.setCurrentText(current)
        refresh_combo_completer(self.model_combo)
        self.model_combo.blockSignals(False)

    def _fetch_models(self) -> None:
        try:
            config = resolve_effective_model_config(self.settings, self._current_role())
        except Exception as exc:  # noqa: BLE001 - converted to UI message.
            QMessageBox.warning(self, APP_NAME, str(exc))
            return
        if not provider_supports_capability(config.provider, config.capability):
            message = f"当前服务商不支持{config.label}所需的{config.capability}能力，请手动填写或更换服务商。"
            self.model_catalog_status.setText(message)
            QMessageBox.warning(self, APP_NAME, message)
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            result = fetch_openai_compatible_models(
                provider=config.provider,
                api_key=config.api_key,
                base_url=config.base_url,
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
        self._model_catalog_signature = self._current_model_catalog_signature()
        self._model_catalog_models = list(result.models)
        self._set_model_combo_items(result.models, include_current=False)
        self.model_combo.setCurrentText(selected_model)
        select_combo_text_match(self.model_combo)
        self._on_model_changed()
        self.model_catalog_status.setText(f"{result.message} 可从下拉列表选择。")
        self.model_combo.showPopup()

    def _fetch_review_models(self) -> None:
        try:
            config = resolve_effective_model_config(self.settings, ROLE_PDF_REVIEW)
        except Exception as exc:  # noqa: BLE001 - converted to UI message.
            QMessageBox.warning(self, APP_NAME, str(exc))
            return
        if not provider_supports_capability(config.provider, config.capability):
            message = f"当前服务商不支持{config.label}所需的图像理解审核能力，请手动填写或更换服务商。"
            self.review_model_status.setText(message)
            QMessageBox.warning(self, APP_NAME, message)
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            result = fetch_openai_compatible_models(
                provider=config.provider,
                api_key=config.api_key,
                base_url=config.base_url,
            )
        finally:
            QApplication.restoreOverrideCursor()

        if not result.ok or not result.models:
            message = f"{result.message}\n{result.detail}".strip()
            self.review_model_status.setText(message)
            QMessageBox.warning(self, APP_NAME, message)
            return

        previous_model = self.review_model_combo.currentText().strip()
        selected_model = previous_model if previous_model in result.models else result.models[0]
        self._set_review_model_combo_items(result.models)
        self.review_model_combo.setCurrentText(selected_model)
        select_combo_text_match(self.review_model_combo)
        self._on_review_model_changed()
        self.review_model_status.setText(f"{result.message} 可从下拉列表选择。")
        self.review_model_combo.showPopup()

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
            "pdf_translate": PdfTranslatePage(settings),
            "tm": TmManagerPage(settings),
        }
        for page in self.pages.values():
            self.stack.addWidget(page)

        self.sidebar.navigateRequested.connect(self._navigate)
        self.sidebar.settingsChanged.connect(self.pages["excel_translate"].refresh_settings)
        self.sidebar.settingsChanged.connect(self.pages["word_translate"].refresh_settings)
        self.sidebar.settingsChanged.connect(self.pages["pdf_translate"].refresh_settings)
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
        self._sync_page_activation("excel_translate")

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("文件")
        quit_action = QAction("退出", self)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

    def _navigate(self, page: str) -> None:
        page_order = ["excel_translate", "word_translate", "pdf_translate", "tm"]
        self.stack.setCurrentIndex(page_order.index(page))
        self.sidebar.set_active_page(page)
        self._sync_page_activation(page)

    def _sync_page_activation(self, active_page: str) -> None:
        for page_key, page in self.pages.items():
            if hasattr(page, "set_page_active"):
                page.set_page_active(page_key == active_page)

    def _sync_tm_language_from_translation(
        self,
        target_lang: str,
        source_lang: str,
    ) -> None:
        self.pages["tm"].sync_language_from_translation(target_lang, source_lang)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API name.
        save_settings(self.settings)
        super().closeEvent(event)
