"""Main window and shared sidebar for the native Qt interface."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
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

from app_meta import APP_NAME, APP_VERSION, APP_VERSION_LABEL
from config import (
    CLOUD_ENGINES,
    DISABLED_BASE_URL_PLACEHOLDER,
    DOMAIN_PRESETS,
    IMAGE_GENERATION_MODEL_PROVIDERS,
    LOCAL_MODEL_PROVIDERS,
    LM_STUDIO_BASE_URL,
    OLLAMA_BASE_URL,
    VISION_TEXT_MODEL_PROVIDERS,
    cloud_provider_base_url_default,
    cloud_provider_uses_base_url,
    normalize_cloud_base_url,
)
from core.connectivity_check import check_connectivity
from core.image_generation import check_image_generation_connectivity
from core.model_catalog import build_model_catalog_signature, fetch_openai_compatible_models
from core.model_config import (
    ImportedModelConfig,
    apply_model_config_import,
    build_model_config_export_payload,
    parse_model_config_import,
)
from core.model_roles import (
    FOLLOW_SOURCE_LABELS,
    ROLE_CLEANER,
    ROLE_IMAGE,
    ROLE_PDF_REVIEW,
    ROLE_TRANSLATION,
    SOURCE_INDEPENDENT,
    ChainedModelFollowError,
    LocalModelFollowNotAllowedError,
    allowed_source_roles,
    get_role_settings,
    provider_supports_capability,
    resolve_effective_model_config,
    role_label,
    settings_for_text_role,
)
from core.model_api_identity import task_api_groups_for_page
from core.model_throughput import (
    batch_size_bounds,
    concurrency_bounds,
    get_model_throughput,
    set_model_throughput,
    supports_batch_size,
)
from core.pdf_review import check_pdf_review_connectivity
from core.update_checker import (
    GITHUB_REPO,
    UpdateCheckResult,
    is_major_upgrade,
    major_version,
)
from native_app.pages.excel_translate import ExcelTranslatePage
from native_app.pages.pdf_translate import PdfTranslatePage
from native_app.pages.tm_manager import TmManagerPage
from native_app.pages.word_translate import WordTranslatePage
from native_app.task_page_lifecycle import (
    detach_running_qobject,
    request_background_stop,
    wait_for_background_task,
)
from native_app.widgets import (
    build_app_tooltip_html,
    create_centered_option_combo,
    create_editable_combo,
    create_option_combo,
    install_in_app_tooltips,
    refresh_combo_completer,
    select_combo_text_match,
)
from native_app.workers import (
    CallableWorker,
    TaskResourceRegistry,
    UpdateCheckWorker,
)
from settings import (
    AppSettings,
    api_key_scope,
    get_cloud_provider_config,
    get_key,
    parse_api_key_scope,
    save_key,
    save_settings,
    select_cloud_provider_config,
    set_cloud_provider_config,
    write_private_text_file,
)


def _section_title(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("SectionTitle")
    return label


def _tooltip(
    title: str,
    summary: str,
    items: list[str] | None = None,
    *,
    title_meta: str = "",
) -> str:
    return build_app_tooltip_html(title, summary, items, title_meta=title_meta)


def _set_tooltip(
    widget: QWidget,
    title: str,
    summary: str,
    items: list[str] | None = None,
    *,
    title_meta: str = "",
) -> None:
    widget.setToolTip(_tooltip(title, summary, items, title_meta=title_meta))
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


RELEASE_NOTES_MAX_CHARS = 1200
GITHUB_PROJECT_PAGE_URL = f"https://github.com/{GITHUB_REPO}"
SIDEBAR_WIDTH = 330
COMPACT_NAV_RAIL_WIDTH = 72
COMPACT_SHELL_SCREEN_WIDTH = 1360
DEFAULT_WINDOW_WIDTH = 1320
DEFAULT_WINDOW_HEIGHT = 880


def _release_notes_preview(notes: str) -> str:
    value = str(notes or "").strip()
    if not value:
        return "本次发布未填写更新说明。"
    if len(value) <= RELEASE_NOTES_MAX_CHARS:
        return value
    return f"{value[:RELEASE_NOTES_MAX_CHARS].rstrip()}\n\n内容较长，完整说明请查看发布页。"


def _local_provider_label(provider: str) -> str:
    for label, value in LOCAL_MODEL_PROVIDERS.items():
        if value == provider:
            return label
    return next(iter(LOCAL_MODEL_PROVIDERS.keys()))


def _local_provider_value(label: str) -> str:
    return LOCAL_MODEL_PROVIDERS.get(label, next(iter(LOCAL_MODEL_PROVIDERS.values())))


def _local_provider_default_base_url(provider: str) -> str:
    if provider == "ollama":
        return OLLAMA_BASE_URL
    if provider == "lm_studio":
        return LM_STUDIO_BASE_URL
    return ""


def _flow_step(title: str, detail: str) -> QFrame:
    frame = QFrame()
    frame.setObjectName("RecoveryCard")
    frame.setFixedWidth(128)
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(8, 7, 8, 7)
    layout.setSpacing(2)

    title_label = QLabel(title)
    title_label.setObjectName("FieldHint")
    title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    title_label.setWordWrap(True)
    layout.addWidget(title_label)

    detail_label = QLabel(detail)
    detail_label.setObjectName("SectionTitle")
    detail_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    detail_label.setWordWrap(True)
    layout.addWidget(detail_label)
    return frame


def _show_local_follow_warning(parent: QWidget, message: str) -> None:
    dialog = QDialog(parent)
    dialog.setWindowTitle(APP_NAME)
    dialog.setModal(True)
    dialog.setMinimumWidth(520)

    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(18, 16, 18, 14)
    layout.setSpacing(12)

    title = _section_title("跟随来源不可用")
    layout.addWidget(title)

    text = QLabel(str(message or "").strip())
    text.setWordWrap(True)
    text.setObjectName("FieldHint")
    layout.addWidget(text)

    flow = QHBoxLayout()
    flow.setSpacing(8)
    flow.addWidget(_flow_step("翻译模型", "本地模型"))
    for label in ("跟随", "改为"):
        arrow = QLabel(f"{label}\n→")
        arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
        arrow.setObjectName("FieldHint")
        arrow.setFixedWidth(34)
        flow.addWidget(arrow)
        if label == "跟随":
            flow.addWidget(_flow_step("当前用途", "需要云端能力"))
    flow.addWidget(_flow_step("处理方式", "独立云端配置"))
    layout.addLayout(flow)

    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
    buttons.accepted.connect(dialog.accept)
    layout.addWidget(buttons)
    dialog.exec()


BRAND_TOOLTIP = {
    "title": APP_NAME,
    "title_meta": f"by OA | {APP_VERSION_LABEL}",
    "summary": f"{APP_NAME} 用于本地处理 Excel、Word 与 PDF 翻译任务。",
    "items": [
        "左侧设置模型、领域和吞吐参数。",
        "右侧执行翻译、查看结果并维护记忆库。",
    ],
}

DOMAIN_TOOLTIP = {
    "title": "专业领域",
    "summary": "选择与资料内容匹配的领域预设。",
    "items": [
        "预设会影响术语、语气和翻译侧重。",
        "同一批文件建议使用同一领域。",
    ],
}

PROMPT_TOOLTIP = {
    "title": "Prompt",
    "summary": "设置本次翻译的补充指令。",
    "items": [
        "留空时使用当前领域默认指令。",
        "自定义内容应简短、明确、可执行。",
    ],
}

ENGINE_TOOLTIP = {
    "title": "模型配置",
    "summary": "配置各类任务使用的模型。",
    "items": [
        "模型用途：翻译模型处理 Excel/Word，清洗模型维护记忆库，PDF 翻译模型生成页图。",
        "接入方式：云端 API 使用服务商接口，本地模型使用本机服务。",
        "PDF 翻译审核模型可独立配置，也可跟随 PDF 翻译模型。",
    ],
}

MODEL_ROLE_TOOLTIP = {
    "title": "模型用途",
    "summary": "选择当前要配置的模型用途。",
    "items": [
        "翻译模型：用于 Excel 和 Word 文本翻译。",
        "清洗模型：用于记忆库深度清洗。",
        "PDF 翻译模型：用于 PDF 页图和图片翻译。",
    ],
}

ENGINE_MODE_TOOLTIP = {
    "title": "接入方式",
    "summary": "选择模型的调用方式。",
    "items": [
        "云端 API：通过服务商接口调用模型。",
        "本地模型：通过本机 Ollama、LM Studio 或自定义地址调用模型。",
    ],
}

CLOUD_SETTINGS_TOOLTIP = {
    "title": "云端 API 设置",
    "summary": "设置云端请求所需的服务商、密钥、地址和模型。",
    "items": [
        "Base URL 仅在兼容接口或自定义网关中使用。",
        "API Key 保存在本机密钥文件中。",
    ],
}

TUNING_TOOLTIP = {
    "title": "吞吐调优",
    "summary": "调整批次大小和并发数。",
    "items": [
        "数值越高，速度可能更快，限流和超时风险也更高。",
        "出现失败、超时或负载过高时，请适当调低。",
    ],
}

MODEL_CONFIG_EXPORT_TYPE = "translator_model_config"
MODEL_CONFIG_EXPORT_VERSION = 2
MODEL_CONFIG_SETTING_KEYS = (
    "engine",
    "cleaner_model_role",
    "image_model_role",
    "pdf_review_model_role",
)
MODEL_CONFIG_CLOUD_FIELDS = (
    "cloud_provider",
    "cloud_model",
    "cloud_base_url",
    "cloud_provider_configs",
)
MODEL_CONFIG_ROLE_CLOUD_FIELDS = (
    "source_role",
    *MODEL_CONFIG_CLOUD_FIELDS,
)
MODEL_PROFILE_ROLES = (
    ("translation", ROLE_TRANSLATION),
    ("cleaner", ROLE_CLEANER),
    ("pdf_translation", ROLE_IMAGE),
    ("pdf_review", ROLE_PDF_REVIEW),
)
MODEL_PROFILE_ROLE_BY_KEY = {
    profile_key: role for profile_key, role in MODEL_PROFILE_ROLES
}
MODEL_PROFILE_KEY_BY_ROLE = {
    role: profile_key for profile_key, role in MODEL_PROFILE_ROLES
}
MODEL_PROFILE_SETTING_KEY_BY_ROLE = {
    ROLE_TRANSLATION: "engine",
    ROLE_CLEANER: "cleaner_model_role",
    ROLE_IMAGE: "image_model_role",
    ROLE_PDF_REVIEW: "pdf_review_model_role",
}


class Sidebar(QFrame):
    """Left navigation and global settings panel."""

    navigateRequested = Signal(str)
    settingsChanged = Signal()
    updateCheckRequested = Signal()
    globalUpdateIgnoreToggled = Signal()
    currentUpdateIgnored = Signal()
    updatePromptRequested = Signal()

    def __init__(self, settings: AppSettings):
        super().__init__()
        self.settings = settings
        self.setObjectName("Sidebar")
        self.setFixedWidth(SIDEBAR_WIDTH)
        self._current_model_role = ROLE_TRANSLATION
        self._model_catalog_signature = ""
        self._model_catalog_models: list[str] = []
        self._updating_prompt_edit = False
        self._update_notice_result: UpdateCheckResult | None = None
        self._connectivity_worker: CallableWorker | None = None
        self._model_fetch_worker: CallableWorker | None = None
        self._review_model_fetch_worker: CallableWorker | None = None
        self._model_fetch_generation = 0
        self._review_model_fetch_generation = 0
        self._background_closing = False
        self._connectivity_on_finished: Callable[[], None] | None = None
        self._model_fetch_context: tuple[int, str, str, str] | None = None
        self._review_model_fetch_context: tuple[int, str, str] | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(14)

        brand_row = QHBoxLayout()
        brand_row.setSpacing(8)
        brand = QLabel(APP_NAME)
        brand.setObjectName("BrandTitle")
        _set_tooltip(
            brand,
            BRAND_TOOLTIP["title"],
            BRAND_TOOLTIP["summary"],
            BRAND_TOOLTIP["items"],
            title_meta=BRAND_TOOLTIP["title_meta"],
        )
        brand_row.addWidget(brand, 1)
        self.update_notice_button = QPushButton("更新")
        self.update_notice_button.setObjectName("UpdateNoticeButton")
        self.update_notice_button.setProperty("compact", True)
        self.update_notice_button.clicked.connect(self.updatePromptRequested.emit)
        self.update_notice_button.hide()
        brand_row.addWidget(self.update_notice_button)
        self.ignore_notice_button = QPushButton("忽略")
        self.ignore_notice_button.setObjectName("UpdateNoticeButton")
        self.ignore_notice_button.setProperty("compact", True)
        self.ignore_notice_button.clicked.connect(self.currentUpdateIgnored.emit)
        self.ignore_notice_button.hide()
        brand_row.addWidget(self.ignore_notice_button)
        root.addLayout(brand_row)

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
                    "pdf_translate": "执行 PDF 翻译。",
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
        self._build_update_footer()
        self._refresh_source_role_options()
        self._sync_model_role_fields()
        self._sync_engine_visibility()
        self.sync_update_ignore_button()
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

    def _build_update_footer(self) -> None:
        row = QHBoxLayout()
        row.setSpacing(8)
        self.update_check_button = QPushButton("检查更新")
        self.update_check_button.clicked.connect(self.updateCheckRequested.emit)
        _set_tooltip(
            self.update_check_button,
            "检查更新",
            "检查是否有可用新版本。",
        )
        row.addWidget(self.update_check_button, 1)

        self.update_ignore_button = QPushButton("忽略更新")
        self.update_ignore_button.clicked.connect(self.globalUpdateIgnoreToggled.emit)
        row.addWidget(self.update_ignore_button, 1)
        self._form.addLayout(row)

        self.github_project_button = QPushButton("打开 GitHub 仓库")
        self.github_project_button.clicked.connect(self._open_github_project_page)
        _set_tooltip(
            self.github_project_button,
            "GitHub 仓库",
            "打开 Translator 的 GitHub 代码仓库。",
        )
        self._form.addWidget(self.github_project_button)

    def _open_github_project_page(self) -> None:
        if not QDesktopServices.openUrl(QUrl(GITHUB_PROJECT_PAGE_URL)):
            QMessageBox.warning(
                self,
                APP_NAME,
                f"无法打开链接：\n{GITHUB_PROJECT_PAGE_URL}",
            )

    def _refresh_widget_style(self, widget: QWidget) -> None:
        widget.style().unpolish(widget)
        widget.style().polish(widget)
        widget.update()

    def sync_update_ignore_button(self) -> None:
        ignored = self.settings.update.ignore_updates
        self.update_ignore_button.setText("已忽略更新" if ignored else "忽略更新")
        self.update_ignore_button.setProperty("updateIgnored", ignored)
        _set_tooltip(
            self.update_ignore_button,
            "已忽略更新" if ignored else "忽略更新",
            "常规更新提醒已暂停。"
            if ignored
            else "暂停常规更新提醒；大版本仍会提示。",
        )
        self._refresh_widget_style(self.update_ignore_button)

    def set_update_checking(self, checking: bool) -> None:
        self.update_check_button.setEnabled(not checking)
        self.update_check_button.setText("检查中..." if checking else "检查更新")

    def set_update_notice(self, result: UpdateCheckResult | None) -> None:
        self._update_notice_result = result
        visible = result is not None and result.has_update
        if result is not None:
            _set_tooltip(
                self.update_notice_button,
                "更新",
                f"发现新版 V{result.latest_version}。",
            )
            _set_tooltip(
                self.ignore_notice_button,
                "忽略",
                f"不再提示 V{result.latest_version}。",
            )
        self.update_notice_button.setVisible(visible)
        self.ignore_notice_button.setVisible(visible)

    def _persist(self) -> None:
        save_settings(self.settings)
        self.settingsChanged.emit()

    def _set_model_catalog_status(self, text: str) -> None:
        value = str(text or "").strip()
        self.model_catalog_status.setText(value)
        self.model_catalog_status.setVisible(bool(value))

    def _set_review_model_status(self, text: str) -> None:
        if not hasattr(self, "review_model_status"):
            return
        value = str(text or "").strip()
        self.review_model_status.setText(value)
        self.review_model_status.setVisible(bool(value))

    def _cloud_model_config_payload(self) -> dict:
        payload = self.settings.model_dump(mode="json")
        cloud_config = {}
        for key in MODEL_CONFIG_SETTING_KEYS:
            source = payload.get(key, {})
            if not isinstance(source, dict):
                continue
            allowed_fields = (
                MODEL_CONFIG_CLOUD_FIELDS
                if key == "engine"
                else MODEL_CONFIG_ROLE_CLOUD_FIELDS
            )
            cloud_config[key] = {
                field: source[field]
                for field in allowed_fields
                if field in source
            }
        return cloud_config

    def _cloud_api_keys_for_export(self, cloud_config: dict) -> dict[str, str]:
        cloud_providers = set(CLOUD_ENGINES.values())
        configured_providers: set[str] = set()
        for owner in cloud_config.values():
            if not isinstance(owner, dict):
                continue
            provider = str(owner.get("cloud_provider") or "").strip()
            if provider in cloud_providers:
                configured_providers.add(provider)
            provider_configs = owner.get("cloud_provider_configs")
            if isinstance(provider_configs, dict):
                configured_providers.update(
                    provider
                    for provider in provider_configs
                    if str(provider or "").strip() in cloud_providers
                )
        return {
            provider: api_key
            for provider in sorted(configured_providers)
            if (api_key := str(get_key(provider) or "").strip())
        }

    def _cloud_api_key_scopes_for_export(self, cloud_config: dict) -> list[dict[str, str]]:
        cloud_providers = set(CLOUD_ENGINES.values())
        configured_scopes: set[tuple[str, str]] = set()
        for owner in cloud_config.values():
            if not isinstance(owner, dict):
                continue
            provider = str(owner.get("cloud_provider") or "").strip()
            if provider in cloud_providers:
                base_url = normalize_cloud_base_url(
                    provider,
                    str(owner.get("cloud_base_url") or "").strip(),
                )
                configured_scopes.add((provider, base_url))
            provider_configs = owner.get("cloud_provider_configs")
            if isinstance(provider_configs, dict):
                for provider, raw_config in provider_configs.items():
                    provider = str(provider or "").strip()
                    if provider not in cloud_providers or not isinstance(raw_config, dict):
                        continue
                    base_url = normalize_cloud_base_url(
                        provider,
                        str(raw_config.get("cloud_base_url") or "").strip(),
                    )
                    configured_scopes.add((provider, base_url))

        entries: list[dict[str, str]] = []
        for provider, base_url in sorted(configured_scopes):
            api_key = str(get_key(provider, base_url) or "").strip()
            if not api_key:
                continue
            entries.append(
                {
                    "scope": api_key_scope(provider, base_url),
                    "provider": provider,
                    "base_url": base_url,
                    "api_key": api_key,
                }
            )
        return entries

    def _cloud_provider_config_profiles_for_export(
        self,
        owner: dict,
    ) -> dict[str, dict[str, str]]:
        cloud_providers = set(CLOUD_ENGINES.values())
        provider_configs = dict(owner.get("cloud_provider_configs") or {})
        current_provider = str(owner.get("cloud_provider") or "").strip()
        if current_provider in cloud_providers:
            provider_configs.setdefault(
                current_provider,
                {
                    "cloud_model": str(owner.get("cloud_model") or "").strip(),
                    "cloud_base_url": str(owner.get("cloud_base_url") or "").strip(),
                },
            )

        profiles: dict[str, dict[str, str]] = {}
        for provider, raw_config in sorted(provider_configs.items()):
            provider = str(provider or "").strip()
            if provider not in cloud_providers or not isinstance(raw_config, dict):
                continue
            base_url = normalize_cloud_base_url(
                provider,
                str(raw_config.get("cloud_base_url") or "").strip(),
            )
            entry = {
                "model": str(raw_config.get("cloud_model") or "").strip(),
                "base_url": base_url,
            }
            api_key = str(get_key(provider, base_url) or "").strip()
            if api_key:
                entry["api_key"] = api_key
            profiles[provider] = entry
        return profiles

    def _cloud_profile_for_export(self, owner: dict) -> dict[str, object]:
        provider = str(owner.get("cloud_provider") or "").strip()
        base_url = normalize_cloud_base_url(
            provider,
            str(owner.get("cloud_base_url") or "").strip(),
        )
        profile: dict[str, object] = {
            "provider": provider,
            "model": str(owner.get("cloud_model") or "").strip(),
            "base_url": base_url,
            "provider_configs": self._cloud_provider_config_profiles_for_export(owner),
        }
        api_key = str(get_key(provider, base_url) or "").strip()
        if api_key:
            profile["api_key"] = api_key
        return profile

    def _local_profile_for_export(self, engine: dict) -> dict[str, str]:
        return {
            "provider": str(engine.get("local_provider") or "").strip(),
            "model": str(engine.get("local_model") or engine.get("ollama_model") or "").strip(),
            "base_url": str(engine.get("local_base_url") or "").strip(),
        }

    def _throughput_profile_for_export(self, role: str) -> dict[str, object]:
        try:
            config = resolve_effective_model_config(self.settings, role)
            throughput = get_model_throughput(self.settings, config)
        except Exception:
            return {}
        profile: dict[str, object] = {
            "profile_key": throughput.profile_key,
            "concurrency": throughput.concurrency,
        }
        if throughput.batch_size is not None:
            profile["batch_size"] = throughput.batch_size
        return profile

    def _effective_profile_for_export(self, role: str) -> dict[str, object]:
        try:
            config = resolve_effective_model_config(self.settings, role)
        except Exception:
            return {}
        profile: dict[str, object] = {
            "mode": config.mode,
            "provider": config.provider,
            "model": config.model,
            "base_url": config.base_url,
            "source_role": config.source_role,
            "follows": config.follows,
        }
        if config.api_key:
            profile["api_key"] = config.api_key
        return profile

    def _model_profiles_for_export(self) -> dict[str, dict[str, object]]:
        payload = self.settings.model_dump(mode="json")
        profiles: dict[str, dict[str, object]] = {}
        for profile_key, role in MODEL_PROFILE_ROLES:
            setting_key = MODEL_PROFILE_SETTING_KEY_BY_ROLE[role]
            owner = dict(payload.get(setting_key) or {})
            profile: dict[str, object] = {
                "role": role,
                "label": role_label(role),
                "cloud": self._cloud_profile_for_export(owner),
                "effective": self._effective_profile_for_export(role),
                "throughput": self._throughput_profile_for_export(role),
            }
            if role == ROLE_TRANSLATION:
                profile["mode"] = str(owner.get("mode") or "cloud").strip() or "cloud"
                profile["source_role"] = SOURCE_INDEPENDENT
                profile["local"] = self._local_profile_for_export(owner)
            else:
                profile["source_role"] = str(
                    owner.get("source_role") or SOURCE_INDEPENDENT
                ).strip()
            profiles[profile_key] = profile
        return profiles

    def _build_model_config_export_payload(self) -> dict:
        self._save_current_model_role_fields()
        self._save_review_model_fields()
        return build_model_config_export_payload(
            self.settings,
            get_api_key=get_key,
        )

    def _extract_imported_model_config(
        self,
        raw: object,
    ) -> tuple[dict, dict[str, str], list[dict[str, str]], dict, dict[str, dict]]:
        imported = parse_model_config_import(raw)
        return (
            imported.model_config,
            imported.api_keys,
            imported.scoped_api_keys,
            imported.throughput_profiles,
            imported.profile_throughputs,
        )

    def _extract_imported_model_profiles(
        self,
        raw: dict,
    ) -> tuple[dict, dict[str, str], list[dict[str, str]], dict, dict[str, dict]]:
        profiles_raw = raw.get("model_profiles")
        if not isinstance(profiles_raw, dict):
            raise ValueError("model_profiles 必须是 JSON 对象。")

        model_config: dict[str, dict] = {}
        scoped_api_keys: list[dict[str, str]] = []
        profile_throughputs: dict[str, dict] = {}

        def add_key(provider: str, base_url: str, api_key: str) -> None:
            provider = str(provider or "").strip()
            if provider not in set(CLOUD_ENGINES.values()):
                return
            api_key = str(api_key or "").strip()
            if not api_key:
                return
            scoped_api_keys.append(
                {
                    "provider": provider,
                    "base_url": normalize_cloud_base_url(provider, base_url),
                    "api_key": api_key,
                }
            )

        def cloud_values(profile: dict) -> dict:
            cloud = profile.get("cloud", {})
            if not isinstance(cloud, dict):
                cloud = {}
            provider = str(
                cloud.get("provider") or cloud.get("cloud_provider") or ""
            ).strip()
            model = str(cloud.get("model") or cloud.get("cloud_model") or "").strip()
            base_url = normalize_cloud_base_url(
                provider,
                str(cloud.get("base_url") or cloud.get("cloud_base_url") or "").strip(),
            )
            add_key(provider, base_url, str(cloud.get("api_key") or ""))

            provider_configs: dict[str, dict[str, str]] = {}
            configs_raw = cloud.get("provider_configs") or cloud.get(
                "cloud_provider_configs"
            )
            if isinstance(configs_raw, dict):
                for raw_provider, raw_config in configs_raw.items():
                    config_provider = str(raw_provider or "").strip()
                    if (
                        config_provider not in set(CLOUD_ENGINES.values())
                        or not isinstance(raw_config, dict)
                    ):
                        continue
                    config_base_url = normalize_cloud_base_url(
                        config_provider,
                        str(
                            raw_config.get("base_url")
                            or raw_config.get("cloud_base_url")
                            or ""
                        ).strip(),
                    )
                    provider_configs[config_provider] = {
                        "cloud_model": str(
                            raw_config.get("model")
                            or raw_config.get("cloud_model")
                            or ""
                        ).strip(),
                        "cloud_base_url": config_base_url,
                    }
                    add_key(
                        config_provider,
                        config_base_url,
                        str(raw_config.get("api_key") or ""),
                    )
            values = {
                "cloud_provider": provider,
                "cloud_model": model,
                "cloud_base_url": base_url,
                "cloud_provider_configs": provider_configs,
            }
            return values

        for profile_key, profile in profiles_raw.items():
            role = MODEL_PROFILE_ROLE_BY_KEY.get(str(profile_key or "").strip())
            if role is None and str(profile_key or "").strip() == ROLE_IMAGE:
                role = ROLE_IMAGE
            if role is None or not isinstance(profile, dict):
                continue

            setting_key = MODEL_PROFILE_SETTING_KEY_BY_ROLE[role]
            values = cloud_values(profile)
            if role == ROLE_TRANSLATION:
                mode = str(profile.get("mode") or "cloud").strip()
                values["mode"] = mode if mode in {"cloud", "local"} else "cloud"
                local = profile.get("local", {})
                if isinstance(local, dict):
                    values.update(
                        {
                            "local_provider": str(local.get("provider") or "").strip(),
                            "local_model": str(local.get("model") or "").strip(),
                            "local_base_url": str(local.get("base_url") or "").strip(),
                        }
                    )
                    values["ollama_model"] = values["local_model"]
            else:
                source_role = str(
                    profile.get("source_role") or SOURCE_INDEPENDENT
                ).strip()
                if source_role == "pdf_translation":
                    source_role = ROLE_IMAGE
                values["source_role"] = source_role
            model_config[setting_key] = values

            throughput = profile.get("throughput")
            if isinstance(throughput, dict):
                profile_throughputs[role] = throughput

        if not model_config:
            raise ValueError("未找到可导入的模型配置。")
        return model_config, {}, scoped_api_keys, {}, profile_throughputs

    def _extract_imported_throughput_profiles(self, raw: dict) -> dict:
        profiles = raw.get("model_throughput_profiles", {})
        if profiles in (None, ""):
            return {}
        if not isinstance(profiles, dict):
            raise ValueError("model_throughput_profiles 必须是 JSON 对象。")
        return {
            str(key): value
            for key, value in profiles.items()
            if str(key).strip() and isinstance(value, dict)
        }

    def _extract_imported_scoped_api_keys(self, raw: dict) -> list[dict[str, str]]:
        scoped_raw = raw.get("scoped_api_keys", [])
        if scoped_raw in (None, ""):
            scoped_raw = []
        cloud_providers = set(CLOUD_ENGINES.values())
        entries: list[dict[str, str]] = []

        def add_entry(provider: str, base_url: str, api_key: str) -> None:
            provider = str(provider or "").strip()
            if provider not in cloud_providers:
                return
            api_key = str(api_key or "").strip()
            if not api_key:
                return
            entries.append(
                {
                    "provider": provider,
                    "base_url": normalize_cloud_base_url(provider, base_url),
                    "api_key": api_key,
                }
            )

        if isinstance(scoped_raw, list):
            for entry in scoped_raw:
                if not isinstance(entry, dict):
                    continue
                provider = str(entry.get("provider") or "").strip()
                base_url = str(entry.get("base_url") or "").strip()
                if not provider:
                    provider, parsed_base_url = parse_api_key_scope(entry.get("scope", ""))
                    base_url = base_url or parsed_base_url
                add_entry(provider, base_url, str(entry.get("api_key") or ""))
            return entries

        if isinstance(scoped_raw, dict):
            for scope, value in scoped_raw.items():
                provider, base_url = parse_api_key_scope(scope)
                if isinstance(value, dict):
                    provider = str(value.get("provider") or provider).strip()
                    base_url = str(value.get("base_url") or base_url).strip()
                    api_key = str(value.get("api_key") or "").strip()
                else:
                    api_key = str(value or "").strip()
                add_entry(provider, base_url, api_key)
            return entries

        raise ValueError("scoped_api_keys 必须是数组或 JSON 对象。")

    def _apply_imported_model_config(
        self,
        model_config: dict,
        api_keys: dict[str, str],
        scoped_api_keys: list[dict[str, str]],
        throughput_profiles: dict,
        profile_throughputs: dict[str, dict],
    ) -> None:
        updated = apply_model_config_import(
            self.settings,
            ImportedModelConfig(
                model_config=model_config,
                api_keys=api_keys,
                scoped_api_keys=scoped_api_keys,
                throughput_profiles=throughput_profiles,
                profile_throughputs=profile_throughputs,
            ),
            save_api_key=save_key,
        )
        for key in MODEL_CONFIG_SETTING_KEYS:
            setattr(self.settings, key, getattr(updated, key))
        self.settings.model_throughput_profiles = updated.model_throughput_profiles

        self._model_catalog_signature = ""
        self._model_catalog_models = []
        self._refresh_source_role_options()
        self._sync_model_role_fields()
        self._sync_engine_visibility()
        self._persist()

    def _export_model_config(self) -> None:
        target, _ = QFileDialog.getSaveFileName(
            self,
            "导出模型配置",
            "translator-model-config.json",
            "JSON 文件 (*.json)",
        )
        if not target:
            return

        path = Path(target)
        if path.suffix.lower() != ".json":
            path = path.with_suffix(".json")
        try:
            write_private_text_file(
                path,
                json.dumps(
                    self._build_model_config_export_payload(),
                    indent=2,
                    ensure_ascii=False,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - converted to UI message.
            QMessageBox.warning(self, APP_NAME, f"导出配置失败：\n{exc}")
            return

        QMessageBox.information(
            self,
            APP_NAME,
            f"模型配置已导出：\n{path}\n\n"
            "导出文件包含云端 API Key，请妥善保存。",
        )

    def _import_model_config(self) -> None:
        source, _ = QFileDialog.getOpenFileName(
            self,
            "导入模型配置",
            "",
            "JSON 文件 (*.json)",
        )
        if not source:
            return

        try:
            raw = json.loads(Path(source).read_text(encoding="utf-8"))
            (
                model_config,
                api_keys,
                scoped_api_keys,
                throughput_profiles,
                profile_throughputs,
            ) = self._extract_imported_model_config(raw)
            self._apply_imported_model_config(
                model_config,
                api_keys,
                scoped_api_keys,
                throughput_profiles,
                profile_throughputs,
            )
        except Exception as exc:  # noqa: BLE001 - converted to UI message.
            QMessageBox.warning(self, APP_NAME, f"导入配置失败：\n{exc}")
            return

        QMessageBox.information(self, APP_NAME, "模型配置已导入。")

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
        if self.settings.domain_preset in DOMAIN_PRESETS:
            self.domain_combo.setCurrentText(self.settings.domain_preset)
        self.domain_combo.currentTextChanged.connect(self._on_domain_changed)
        layout.addWidget(self.domain_combo)

        prompt_hint = QLabel("Prompt")
        prompt_hint.setObjectName("FieldHint")
        _set_tooltip(
            prompt_hint,
            PROMPT_TOOLTIP["title"],
            PROMPT_TOOLTIP["summary"],
            PROMPT_TOOLTIP["items"],
        )
        layout.addWidget(prompt_hint)

        self.prompt_edit = QTextEdit()
        self.prompt_edit.setMinimumHeight(112)
        self.prompt_edit.setPlaceholderText("输入补充 Prompt")
        self.prompt_edit.setPlainText(self._domain_prompt_value(self.domain_combo.currentText()))
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

        self._build_model_config_actions(layout)

        self.model_role_label = _field_label(
            "模型用途",
            MODEL_ROLE_TOOLTIP["title"],
            MODEL_ROLE_TOOLTIP["summary"],
            MODEL_ROLE_TOOLTIP["items"],
        )
        layout.addWidget(self.model_role_label)
        self.model_role_combo = create_centered_option_combo()
        self.model_role_combo.setObjectName("ModelRoleCombo")
        self.model_role_combo.addItem(role_label(ROLE_TRANSLATION), ROLE_TRANSLATION)
        self.model_role_combo.addItem(role_label(ROLE_CLEANER), ROLE_CLEANER)
        self.model_role_combo.addItem(role_label(ROLE_IMAGE), ROLE_IMAGE)
        self.model_role_combo.currentIndexChanged.connect(self._on_model_role_changed)
        layout.addWidget(self.model_role_combo)

        self.pdf_image_generation_hint = QLabel(role_label(ROLE_IMAGE))
        self.pdf_image_generation_hint.setObjectName("FieldHint")
        self.pdf_image_generation_hint.setWordWrap(True)
        layout.addWidget(self.pdf_image_generation_hint)

        self.source_role_label = _field_label(
            "配置来源",
            "配置来源",
            "选择独立配置或跟随上游模型。",
            ["跟随时共用服务商、API Key 与 Base URL。"],
        )
        layout.addWidget(self.source_role_label)
        self.source_role_combo = create_option_combo()
        self.source_role_combo.currentIndexChanged.connect(self._on_source_role_changed)
        layout.addWidget(self.source_role_combo)

        self.mode_label = _field_label(
            "接入方式",
            ENGINE_MODE_TOOLTIP["title"],
            ENGINE_MODE_TOOLTIP["summary"],
            ENGINE_MODE_TOOLTIP["items"],
        )
        layout.addWidget(self.mode_label)
        self.mode_combo = create_option_combo()
        self.mode_combo.addItem("云端 API", "cloud")
        self.mode_combo.addItem("本地模型", "local")
        self.mode_combo.setCurrentIndex(0 if self.settings.engine.mode == "cloud" else 1)
        self.mode_combo.currentIndexChanged.connect(self._on_engine_mode_changed)
        layout.addWidget(self.mode_combo)

        self.provider_label = _field_label(
            "服务商",
            "服务商",
            "选择当前模型的接入渠道。",
            CLOUD_SETTINGS_TOOLTIP["items"],
        )
        layout.addWidget(self.provider_label)
        self.provider_combo = create_option_combo()
        self.provider_combo.addItems(list(CLOUD_ENGINES.keys()))
        self.provider_combo.setCurrentText(
            _cloud_provider_label(self.settings.engine.cloud_provider)
        )
        self.provider_combo.currentTextChanged.connect(self._on_provider_changed)
        layout.addWidget(self.provider_combo)

        self.api_key_label = _field_label(
            "API Key",
            "API Key",
            "当前云端服务商的访问密钥。",
            [
                "密钥保存在本机，不显示明文。",
                "更换后需重新获取模型列表。",
            ],
        )
        layout.addWidget(self.api_key_label)
        self.api_key_input = QLineEdit()
        self.api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        initial_provider = str(self.settings.engine.cloud_provider or "").strip()
        initial_provider_config = get_cloud_provider_config(
            self.settings.engine,
            initial_provider,
        )
        self.api_key_input.setText(
            get_key(initial_provider, initial_provider_config.cloud_base_url)
        )
        self.api_key_input.editingFinished.connect(self._on_api_key_changed)
        layout.addWidget(self.api_key_input)

        self.base_url_label = _field_label(
            "Base URL",
            "Base URL",
            "接口基础地址。",
            [
                "常见格式：https://.../v1。",
                "更换后需重新获取模型列表。",
            ],
        )
        layout.addWidget(self.base_url_label)
        self.base_url_input = QLineEdit(self.settings.engine.cloud_base_url)
        self.base_url_input.editingFinished.connect(self._on_base_url_changed)
        layout.addWidget(self.base_url_input)

        self.model_label = _field_label(
            "模型名称",
            "模型名称",
            "设置实际调用的模型。",
            [
                "可输入模型名，也可先获取模型列表后选择。",
                "输入部分名称后按回车可匹配候选项。",
                "服务商、密钥和地址不变时，列表会保留。",
            ],
        )
        layout.addWidget(self.model_label)
        self.model_combo = create_editable_combo()
        self.model_combo.addItem(self.settings.engine.cloud_model)
        self.model_combo.setCurrentText(self.settings.engine.cloud_model)
        refresh_combo_completer(self.model_combo)
        self.model_combo.currentTextChanged.connect(self._on_model_changed)
        layout.addWidget(self.model_combo)

        self.model_catalog_status = QLabel("")
        self.model_catalog_status.setObjectName("FieldHint")
        self.model_catalog_status.setWordWrap(True)
        self.model_catalog_status.hide()
        layout.addWidget(self.model_catalog_status)

        model_buttons = QHBoxLayout()
        self.fetch_models_button = QPushButton("获取模型")
        _set_tooltip(
            self.fetch_models_button,
            "获取模型",
            "从当前服务商读取模型列表。",
            [
                "成功后可在模型名称下拉框中选择。",
                "仅支持兼容模型列表接口的服务商。",
            ],
        )
        self.fetch_models_button.clicked.connect(self._fetch_models)
        model_buttons.addWidget(self.fetch_models_button)
        test_conn = QPushButton("测试连接")
        _set_tooltip(
            test_conn,
            "测试连接",
            "测试当前模型配置是否可用。",
        )
        test_conn.clicked.connect(self._test_connectivity)
        model_buttons.addWidget(test_conn)
        layout.addLayout(model_buttons)

        self._build_model_throughput_fields(layout)
        self._build_pdf_review_model_section(layout)

    def _build_model_throughput_fields(self, parent_layout: QVBoxLayout) -> None:
        row = QHBoxLayout()
        row.setSpacing(8)

        self.batch_field = QWidget()
        batch_layout = QVBoxLayout(self.batch_field)
        batch_layout.setContentsMargins(0, 0, 0, 0)
        batch_layout.setSpacing(4)

        self.batch_label = _field_label(
            "批次大小",
            "批次大小",
            "每次请求包含的文本数量。",
            TUNING_TOOLTIP["items"],
        )
        batch_layout.addWidget(self.batch_label)
        self.batch_spin = QSpinBox()
        self.batch_spin.valueChanged.connect(self._on_batch_changed)
        batch_layout.addWidget(self.batch_spin)
        row.addWidget(self.batch_field, 1)

        self.concurrency_field = QWidget()
        concurrency_layout = QVBoxLayout(self.concurrency_field)
        concurrency_layout.setContentsMargins(0, 0, 0, 0)
        concurrency_layout.setSpacing(4)

        self.concurrency_label = _field_label(
            "并发数",
            "并发数",
            "同时发起的请求数量。",
            TUNING_TOOLTIP["items"],
        )
        concurrency_layout.addWidget(self.concurrency_label)
        self.concurrency_input = QSpinBox()
        self.concurrency_input.valueChanged.connect(self._on_concurrency_changed)
        concurrency_layout.addWidget(self.concurrency_input)
        row.addWidget(self.concurrency_field, 1)

        parent_layout.addLayout(row)

        self.throughput_status = QLabel("")
        self.throughput_status.setObjectName("FieldHint")
        self.throughput_status.setWordWrap(True)
        self.throughput_status.hide()
        parent_layout.addWidget(self.throughput_status)

    def _build_model_config_actions(self, parent_layout: QVBoxLayout) -> None:
        config_buttons = QHBoxLayout()
        config_buttons.setSpacing(8)
        self.export_model_config_button = QPushButton("导出配置")
        _set_tooltip(
            self.export_model_config_button,
            "导出配置",
            "导出当前模型配置及相关 API Key。",
            ["JSON 文件含云端 API Key，请妥善保管。"],
        )
        self.export_model_config_button.clicked.connect(self._export_model_config)
        config_buttons.addWidget(self.export_model_config_button)

        self.import_model_config_button = QPushButton("导入配置")
        _set_tooltip(
            self.import_model_config_button,
            "导入配置",
            "导入模型配置并刷新当前设置。",
        )
        self.import_model_config_button.clicked.connect(self._import_model_config)
        config_buttons.addWidget(self.import_model_config_button)
        parent_layout.addLayout(config_buttons)

    def _build_pdf_review_model_section(self, parent_layout: QVBoxLayout) -> None:
        self.pdf_review_frame = QFrame()
        self.pdf_review_frame.setObjectName("RecoveryCard")
        layout = QVBoxLayout(self.pdf_review_frame)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        title_row = QHBoxLayout()
        review_title = _section_title("PDF 翻译审核模型")
        _set_tooltip(
            review_title,
            "PDF 翻译审核模型",
            "配置用于检查候选译图的审核模型。",
            [
                "可独立配置，也可跟随 PDF 翻译模型。",
                "启用 PDF 翻译审核后才会调用。",
            ],
        )
        title_row.addWidget(review_title)
        optional = QLabel("可选")
        optional.setObjectName("RecoveryBadge")
        title_row.addStretch(1)
        title_row.addWidget(optional)
        layout.addLayout(title_row)

        layout.addWidget(
            _field_label(
                "配置来源",
                "配置来源",
                "选择独立配置或跟随上游模型。",
                ["跟随时模型名称仍可单独填写。"],
            )
        )
        self.review_source_role_combo = create_option_combo()
        self.review_source_role_combo.currentIndexChanged.connect(
            self._on_review_source_role_changed
        )
        layout.addWidget(self.review_source_role_combo)

        layout.addWidget(_field_label("服务商", "服务商", "选择审核模型服务商。"))
        self.review_provider_combo = create_option_combo()
        self.review_provider_combo.addItems(list(VISION_TEXT_MODEL_PROVIDERS.keys()))
        self.review_provider_combo.currentTextChanged.connect(self._on_review_provider_changed)
        layout.addWidget(self.review_provider_combo)

        layout.addWidget(_field_label("API Key", "API Key", "审核模型的访问密钥。"))
        self.review_api_key_input = QLineEdit()
        self.review_api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.review_api_key_input.editingFinished.connect(self._on_review_api_key_changed)
        layout.addWidget(self.review_api_key_input)

        layout.addWidget(_field_label("Base URL", "Base URL", "审核模型的接口地址。"))
        self.review_base_url_input = QLineEdit()
        self.review_base_url_input.editingFinished.connect(self._on_review_base_url_changed)
        layout.addWidget(self.review_base_url_input)

        layout.addWidget(_field_label("模型名称", "模型名称", "填写审核模型名称。"))
        self.review_model_combo = create_editable_combo()
        self.review_model_combo.currentTextChanged.connect(self._on_review_model_changed)
        layout.addWidget(self.review_model_combo)

        self.review_model_status = QLabel("")
        self.review_model_status.setObjectName("FieldHint")
        self.review_model_status.setWordWrap(True)
        self.review_model_status.hide()
        layout.addWidget(self.review_model_status)

        buttons = QHBoxLayout()
        self.fetch_review_models_button = QPushButton("获取审核模型")
        self.fetch_review_models_button.clicked.connect(self._fetch_review_models)
        buttons.addWidget(self.fetch_review_models_button)
        test_conn = QPushButton("测试审核连接")
        test_conn.clicked.connect(self._test_review_connectivity)
        buttons.addWidget(test_conn)
        layout.addLayout(buttons)

        self.review_concurrency_field = QWidget()
        review_concurrency_layout = QVBoxLayout(self.review_concurrency_field)
        review_concurrency_layout.setContentsMargins(0, 0, 0, 0)
        review_concurrency_layout.setSpacing(4)

        self.review_concurrency_label = _field_label(
            "并发数",
            "并发数",
            "审核模型同时发起的请求数量。",
            TUNING_TOOLTIP["items"],
        )
        review_concurrency_layout.addWidget(self.review_concurrency_label)
        self.review_concurrency_spin = QSpinBox()
        self.review_concurrency_spin.valueChanged.connect(
            self._on_review_concurrency_changed
        )
        review_concurrency_layout.addWidget(self.review_concurrency_spin)

        self.review_concurrency_shared_input = QLineEdit("共用页生成并发")
        self.review_concurrency_shared_input.setReadOnly(True)
        self.review_concurrency_shared_input.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.review_concurrency_shared_input.setProperty("readonlyHint", True)
        self.review_concurrency_shared_input.hide()
        review_concurrency_layout.addWidget(self.review_concurrency_shared_input)

        layout.addWidget(self.review_concurrency_field)

        parent_layout.addWidget(self.pdf_review_frame)

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

    def _provider_owner_for_role(self, role: str):
        if role == ROLE_TRANSLATION:
            return self.settings.engine
        return get_role_settings(self.settings, role)

    def _normalized_base_url_input(
        self,
        provider: str,
        raw_value: str,
        previous_value: str,
    ) -> str:
        if not cloud_provider_uses_base_url(provider):
            return ""
        value = str(raw_value or "").strip()
        if value:
            return normalize_cloud_base_url(provider, value)
        default_url = cloud_provider_base_url_default(provider)
        if default_url:
            return default_url
        return str(previous_value or "").strip()

    def _set_base_url_field(self, field: QLineEdit, provider: str, value: str) -> None:
        if cloud_provider_uses_base_url(provider):
            field.setPlaceholderText(
                cloud_provider_base_url_default(provider) or "https://.../v1"
            )
            field.setText(str(value or ""))
        else:
            field.setText("")
            field.setPlaceholderText(DISABLED_BASE_URL_PLACEHOLDER)

    def _save_model_role_fields(self, role: str) -> None:
        if not hasattr(self, "model_combo"):
            return
        model = self.model_combo.currentText().strip()
        if role == ROLE_TRANSLATION and self.settings.engine.mode == "local":
            self.settings.engine.local_model = model
            if self.settings.engine.local_provider == "ollama":
                self.settings.engine.ollama_model = model
            if hasattr(self, "base_url_input"):
                self.settings.engine.local_base_url = self.base_url_input.text().strip()
            return

        owner = self._provider_owner_for_role(role)
        if owner is None:
            return
        if role != ROLE_TRANSLATION and owner.source_role != SOURCE_INDEPENDENT:
            owner.cloud_model = model
            return

        provider = str(owner.cloud_provider or "").strip()
        previous = get_cloud_provider_config(owner, provider)
        base_url = self._normalized_base_url_input(
            provider,
            self.base_url_input.text() if hasattr(self, "base_url_input") else "",
            previous.cloud_base_url,
        )
        set_cloud_provider_config(
            owner,
            provider,
            cloud_model=model,
            cloud_base_url=base_url,
        )

    def _save_current_model_role_fields(self) -> None:
        self._save_model_role_fields(self._current_model_role)

    def _save_review_model_fields(self) -> None:
        if not hasattr(self, "review_model_combo"):
            return
        role_settings = self.settings.pdf_review_model_role
        model = self.review_model_combo.currentText().strip()
        if role_settings.source_role != SOURCE_INDEPENDENT:
            role_settings.cloud_model = model
            return
        provider = str(role_settings.cloud_provider or "").strip()
        previous = get_cloud_provider_config(role_settings, provider)
        base_url = self._normalized_base_url_input(
            provider,
            self.review_base_url_input.text(),
            previous.cloud_base_url,
        )
        set_cloud_provider_config(
            role_settings,
            provider,
            cloud_model=model,
            cloud_base_url=base_url,
        )

    def _on_model_role_changed(self) -> None:
        self._save_current_model_role_fields()
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
        self._save_current_model_role_fields()
        previous = role_settings.source_role
        selected = str(self.source_role_combo.currentData() or SOURCE_INDEPENDENT)
        role_settings.source_role = selected
        try:
            resolve_effective_model_config(self.settings, self._current_role())
        except ChainedModelFollowError as exc:
            role_settings.source_role = previous
            QMessageBox.warning(self, APP_NAME, str(exc))
        except LocalModelFollowNotAllowedError as exc:
            role_settings.source_role = previous
            _show_local_follow_warning(self, str(exc))
        self._refresh_source_role_options()
        self._sync_model_role_fields()
        self._sync_engine_visibility()
        self._persist()

    def _on_review_source_role_changed(self) -> None:
        role_settings = self.settings.pdf_review_model_role
        self._save_review_model_fields()
        previous = role_settings.source_role
        selected = str(self.review_source_role_combo.currentData() or SOURCE_INDEPENDENT)
        role_settings.source_role = selected
        try:
            resolve_effective_model_config(self.settings, ROLE_PDF_REVIEW)
        except ChainedModelFollowError as exc:
            role_settings.source_role = previous
            QMessageBox.warning(self, APP_NAME, str(exc))
        except LocalModelFollowNotAllowedError as exc:
            role_settings.source_role = previous
            _show_local_follow_warning(self, str(exc))
        self._sync_review_model_fields()
        self._persist()

    def _on_review_provider_changed(self, label: str) -> None:
        role_settings = self.settings.pdf_review_model_role
        if role_settings.source_role != SOURCE_INDEPENDENT:
            return
        self._save_review_model_fields()
        provider = VISION_TEXT_MODEL_PROVIDERS.get(
            label,
            role_settings.cloud_provider,
        )
        select_cloud_provider_config(role_settings, provider)
        provider_config = get_cloud_provider_config(role_settings, provider)
        self.review_api_key_input.setText(
            get_key(role_settings.cloud_provider, provider_config.cloud_base_url)
        )
        self._sync_review_model_fields()
        self._refresh_review_role_status()
        self._persist()

    def _on_review_api_key_changed(self) -> None:
        config = resolve_effective_model_config(self.settings, ROLE_PDF_REVIEW)
        save_key(config.provider, self.review_api_key_input.text().strip(), config.base_url)
        self.settingsChanged.emit()

    def _on_review_base_url_changed(self) -> None:
        role_settings = self.settings.pdf_review_model_role
        if role_settings.source_role == SOURCE_INDEPENDENT:
            provider = str(role_settings.cloud_provider or "").strip()
            previous = get_cloud_provider_config(role_settings, provider)
            base_url = self._normalized_base_url_input(
                provider,
                self.review_base_url_input.text(),
                previous.cloud_base_url,
            )
            set_cloud_provider_config(
                role_settings,
                provider,
                cloud_base_url=base_url,
            )
            self.review_base_url_input.setText(base_url)
            self.review_api_key_input.setText(get_key(provider, base_url))
        self._refresh_review_role_status()
        self._persist()

    def _on_review_model_changed(self) -> None:
        role_settings = self.settings.pdf_review_model_role
        model = self.review_model_combo.currentText().strip()
        if role_settings.source_role == SOURCE_INDEPENDENT:
            set_cloud_provider_config(
                role_settings,
                role_settings.cloud_provider,
                cloud_model=model,
            )
        else:
            role_settings.cloud_model = model
        self._refresh_review_role_status()
        self._sync_review_throughput_fields()
        self._persist()

    def _sync_model_role_fields(self) -> None:
        role = self._current_role()
        previous_signature = self._current_model_catalog_signature()
        local_follow_error = ""
        try:
            config = resolve_effective_model_config(self.settings, role)
        except ChainedModelFollowError:
            role_settings = self._role_settings()
            if role_settings is not None:
                role_settings.source_role = SOURCE_INDEPENDENT
            config = resolve_effective_model_config(self.settings, role)
        except LocalModelFollowNotAllowedError as exc:
            local_follow_error = str(exc)
            role_settings = self._role_settings()
            if role_settings is not None:
                previous_source = role_settings.source_role
                role_settings.source_role = SOURCE_INDEPENDENT
                config = resolve_effective_model_config(self.settings, role)
                role_settings.source_role = previous_source
            else:
                config = resolve_effective_model_config(self.settings, ROLE_TRANSLATION)

        self._reload_provider_options(config.provider)
        self.pdf_image_generation_hint.setVisible(role == ROLE_IMAGE)
        if role == ROLE_IMAGE:
            self.pdf_image_generation_hint.setText(f"{role_label(ROLE_IMAGE)}（必填）")
        self.mode_combo.blockSignals(True)
        self.mode_combo.setCurrentIndex(0 if self.settings.engine.mode == "cloud" else 1)
        self.mode_combo.blockSignals(False)
        self.provider_combo.blockSignals(True)
        provider_label = (
            _local_provider_label(config.provider)
            if config.mode == "local"
            else _cloud_provider_label(config.provider)
        )
        self.provider_combo.setCurrentText(provider_label)
        self.provider_combo.blockSignals(False)
        self.api_key_input.setText("" if config.mode == "local" else config.api_key)
        self._set_base_url_field(self.base_url_input, config.provider, config.base_url)
        self._set_model_combo_items([config.model], include_current=False, current_text=config.model)
        self._clear_model_catalog_if_signature_changed(previous_signature)
        self._refresh_role_status()
        if local_follow_error:
            self._set_model_catalog_status(local_follow_error)
        self._sync_review_model_fields()

    def _reload_provider_options(self, provider: str) -> None:
        role = self._current_role()
        follows = self._is_following_current_role()
        provider_items = (
            LOCAL_MODEL_PROVIDERS
            if role == ROLE_TRANSLATION and self.settings.engine.mode == "local"
            else IMAGE_GENERATION_MODEL_PROVIDERS
            if role == ROLE_IMAGE and not follows
            else CLOUD_ENGINES
        )
        current_label = (
            _local_provider_label(provider)
            if role == ROLE_TRANSLATION and self.settings.engine.mode == "local"
            else _cloud_provider_label(provider)
        )
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

        review_api_key = (
            config.api_key
            if config is not None
            else get_key(provider, role_settings.cloud_base_url)
        )
        self.review_api_key_input.setText(review_api_key)
        review_base_url = config.base_url if config is not None else role_settings.cloud_base_url
        self._set_base_url_field(self.review_base_url_input, provider, review_base_url)
        self._set_review_model_combo_items([
            config.model if config is not None else role_settings.cloud_model
        ], current_text=config.model if config is not None else role_settings.cloud_model)
        self._refresh_review_role_status()
        self._sync_review_model_visibility()
        self._sync_review_throughput_fields()

    def _set_review_model_combo_items(
        self,
        models: list[str],
        *,
        current_text: str | None = None,
    ) -> None:
        try:
            fallback_model = resolve_effective_model_config(
                self.settings,
                ROLE_PDF_REVIEW,
            ).model
        except Exception:
            fallback_model = self.settings.pdf_review_model_role.cloud_model
        current = (
            str(current_text or "").strip()
            if current_text is not None
            else self.review_model_combo.currentText().strip() or fallback_model
        )
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
        try:
            config = resolve_effective_model_config(self.settings, ROLE_PDF_REVIEW)
            base_url_editable = cloud_provider_uses_base_url(config.provider)
        except Exception:
            base_url_editable = True
        for widget in (
            self.review_provider_combo,
            self.review_api_key_input,
        ):
            widget.setEnabled(access_editable)
        self.review_base_url_input.setEnabled(access_editable and base_url_editable)
        self.review_model_combo.setEnabled(True)

    def _refresh_review_role_status(self) -> None:
        if not hasattr(self, "review_model_status"):
            return
        try:
            config = resolve_effective_model_config(self.settings, ROLE_PDF_REVIEW)
        except Exception as exc:  # noqa: BLE001 - status only.
            self._set_review_model_status(str(exc))
            return
        status = self.settings.pdf_review_model_role.availability_status
        message = self.settings.pdf_review_model_role.availability_message
        status_lines: list[str] = []
        if status == "available":
            status_lines.append("上次审核连接测试：可用。")
        elif status == "unavailable":
            status_lines.append("上次审核连接测试：不可用。")
        if message:
            status_lines.append(message)
        if not provider_supports_capability(config.provider, "vision_text"):
            status_lines.append("当前服务商未标记为支持图像理解审核。")
        self._set_review_model_status("\n".join(status_lines))

    def _refresh_role_status(self) -> None:
        try:
            config = resolve_effective_model_config(self.settings, self._current_role())
        except Exception as exc:  # noqa: BLE001 - status only.
            self._set_model_catalog_status(str(exc))
            return
        if config.mode == "local":
            self._set_model_catalog_status(
                "当前使用本地翻译模型。可获取本机模型列表。"
            )
            return
        if config.role == ROLE_IMAGE:
            status = self.settings.image_model_role.availability_status
            message = self.settings.image_model_role.availability_message
            capability_hint = (
                "服务商支持图像生成。"
                if provider_supports_capability(config.provider, "image")
                else "服务商暂未标记为支持图像生成。"
            )
            state_text = {
                "available": "上次 PDF 翻译模型测试：可用",
                "unavailable": "上次 PDF 翻译模型测试：不可用",
            }.get(status, "PDF 翻译模型尚未校验")
            self._set_model_catalog_status(
                f"{state_text}。{capability_hint}" + (f"\n{message}" if message else "")
            )
            return
        if config.follows:
            self._set_model_catalog_status(
                f"{config.label}正在{FOLLOW_SOURCE_LABELS.get(config.source_role)}，服务商/API Key/Base URL 只读。"
            )
            return
        self._set_model_catalog_status("")

    def _on_engine_mode_changed(self) -> None:
        if self._current_role() != ROLE_TRANSLATION:
            return
        self._save_current_model_role_fields()
        self.settings.engine.mode = self.mode_combo.currentData()
        if self.settings.engine.mode == "local" and not self.settings.engine.local_base_url:
            self.settings.engine.local_base_url = _local_provider_default_base_url(
                self.settings.engine.local_provider,
            )
        self._sync_model_role_fields()
        self._sync_engine_visibility()
        self._persist()

    def _on_provider_changed(self, label: str) -> None:
        previous_signature = self._current_model_catalog_signature()
        self._save_current_model_role_fields()
        local_translation = (
            self._current_role() == ROLE_TRANSLATION
            and self.settings.engine.mode == "local"
        )
        provider = (
            _local_provider_value(label)
            if local_translation
            else _cloud_provider_value(label)
        )
        role_settings = self._role_settings()
        if local_translation:
            self.settings.engine.local_provider = provider
            self.settings.engine.local_base_url = _local_provider_default_base_url(provider)
            self.base_url_input.setText(self.settings.engine.local_base_url)
            self.api_key_input.setText("")
        elif self._current_role() == ROLE_TRANSLATION:
            select_cloud_provider_config(self.settings.engine, provider)
            provider_config = get_cloud_provider_config(self.settings.engine, provider)
            self.api_key_input.setText(get_key(provider, provider_config.cloud_base_url))
        elif role_settings is not None and role_settings.source_role == SOURCE_INDEPENDENT:
            select_cloud_provider_config(role_settings, provider)
            provider_config = get_cloud_provider_config(role_settings, provider)
            self.api_key_input.setText(get_key(provider, provider_config.cloud_base_url))
        self._sync_model_role_fields()
        self._sync_engine_visibility()
        self._clear_model_catalog_if_signature_changed(previous_signature)
        self._refresh_role_status()
        self._persist()

    def _on_api_key_changed(self) -> None:
        previous_signature = self._current_model_catalog_signature()
        config = resolve_effective_model_config(self.settings, self._current_role())
        save_key(config.provider, self.api_key_input.text().strip(), config.base_url)
        self._clear_model_catalog_if_signature_changed(previous_signature)
        self.settingsChanged.emit()

    def _on_base_url_changed(self) -> None:
        previous_signature = self._current_model_catalog_signature()
        role_settings = self._role_settings()
        if self._current_role() == ROLE_TRANSLATION and self.settings.engine.mode == "local":
            self.settings.engine.local_base_url = self.base_url_input.text().strip()
        elif self._current_role() == ROLE_TRANSLATION:
            provider = str(self.settings.engine.cloud_provider or "").strip()
            previous = get_cloud_provider_config(self.settings.engine, provider)
            base_url = self._normalized_base_url_input(
                provider,
                self.base_url_input.text(),
                previous.cloud_base_url,
            )
            set_cloud_provider_config(
                self.settings.engine,
                provider,
                cloud_base_url=base_url,
            )
            self._set_base_url_field(self.base_url_input, provider, base_url)
        elif role_settings is not None and role_settings.source_role == SOURCE_INDEPENDENT:
            provider = str(role_settings.cloud_provider or "").strip()
            previous = get_cloud_provider_config(role_settings, provider)
            base_url = self._normalized_base_url_input(
                provider,
                self.base_url_input.text(),
                previous.cloud_base_url,
            )
            set_cloud_provider_config(
                role_settings,
                provider,
                cloud_base_url=base_url,
            )
            self._set_base_url_field(self.base_url_input, provider, base_url)
        try:
            config = resolve_effective_model_config(self.settings, self._current_role())
            if config.mode != "local":
                self.api_key_input.setText(config.api_key)
        except Exception:
            pass
        self._clear_model_catalog_if_signature_changed(previous_signature)
        self._refresh_role_status()
        self._persist()

    def _on_model_changed(self) -> None:
        role_settings = self._role_settings()
        if self._current_role() == ROLE_TRANSLATION and self.settings.engine.mode == "local":
            value = self.model_combo.currentText().strip()
            self.settings.engine.local_model = value
            if self.settings.engine.local_provider == "ollama":
                self.settings.engine.ollama_model = value
        elif self._current_role() == ROLE_TRANSLATION:
            set_cloud_provider_config(
                self.settings.engine,
                self.settings.engine.cloud_provider,
                cloud_model=self.model_combo.currentText().strip(),
            )
        elif role_settings is not None:
            if role_settings.source_role == SOURCE_INDEPENDENT:
                set_cloud_provider_config(
                    role_settings,
                    role_settings.cloud_provider,
                    cloud_model=self.model_combo.currentText().strip(),
                )
            else:
                role_settings.cloud_model = self.model_combo.currentText().strip()
        self._refresh_role_status()
        self._sync_throughput_fields()
        self._persist()

    def _on_batch_changed(self, value: int) -> None:
        try:
            config = resolve_effective_model_config(self.settings, self._current_role())
        except Exception:
            return
        if not supports_batch_size(config):
            return
        set_model_throughput(self.settings, config, batch_size=value)
        self._persist()
        self._sync_throughput_fields()

    def _on_concurrency_changed(self, value: int | None = None) -> None:
        try:
            config = resolve_effective_model_config(self.settings, self._current_role())
        except Exception:
            return
        if config.role not in {ROLE_TRANSLATION, ROLE_CLEANER, ROLE_IMAGE}:
            return
        selected = self.concurrency_input.value() if value is None else int(value)
        set_model_throughput(self.settings, config, concurrency=selected)
        self._persist()
        self._sync_throughput_fields()

    def _on_review_concurrency_changed(self, value: int | None = None) -> None:
        try:
            config = resolve_effective_model_config(self.settings, ROLE_PDF_REVIEW)
        except Exception:
            return
        selected = self.review_concurrency_spin.value() if value is None else int(value)
        set_model_throughput(self.settings, config, concurrency=selected)
        self._persist()
        self._sync_review_throughput_fields()

    def _sync_throughput_fields(self) -> None:
        if not hasattr(self, "concurrency_input"):
            return
        try:
            config = resolve_effective_model_config(self.settings, self._current_role())
        except Exception as exc:  # noqa: BLE001 - UI status only.
            self.batch_field.setVisible(False)
            self.concurrency_field.setVisible(False)
            self.throughput_status.setText(str(exc))
            self.throughput_status.show()
            return

        show_batch = supports_batch_size(config)
        self.batch_field.setVisible(show_batch)
        throughput = get_model_throughput(self.settings, config)
        if show_batch:
            minimum, maximum = batch_size_bounds(config) or (1, throughput.batch_size or 1)
            batch_summary = (
                "控制记忆库深度清洗每次请求的文本数量。"
                if config.role == ROLE_CLEANER
                else "控制 Excel/Word 文本翻译每次请求的文本数量。"
            )
            self.batch_label.setText("批次大小")
            _set_tooltip(
                self.batch_label,
                "批次大小",
                batch_summary,
                TUNING_TOOLTIP["items"],
            )
            self.batch_spin.blockSignals(True)
            self.batch_spin.setRange(minimum, maximum)
            self.batch_spin.setValue(throughput.batch_size or minimum)
            self.batch_spin.blockSignals(False)

        minimum, maximum = concurrency_bounds(config)
        if config.role == ROLE_IMAGE:
            concurrency_summary = "控制 PDF 页图像生成同时发起的请求数量。"
        elif config.role == ROLE_CLEANER:
            concurrency_summary = "控制记忆库深度清洗同时发起的请求数量。"
        else:
            concurrency_summary = "控制 Excel/Word 文本翻译同时发起的请求数量。"
        self.concurrency_label.setText("并发数")
        _set_tooltip(
            self.concurrency_label,
            "并发数",
            concurrency_summary,
            TUNING_TOOLTIP["items"],
        )
        self.concurrency_input.blockSignals(True)
        self.concurrency_input.setRange(minimum, maximum)
        self.concurrency_input.setValue(throughput.concurrency)
        self.concurrency_input.blockSignals(False)
        self.concurrency_field.setVisible(True)
        self.throughput_status.clear()
        self.throughput_status.hide()

    def _sync_review_throughput_fields(self) -> None:
        if not hasattr(self, "review_concurrency_spin"):
            return
        follows_image_model = self.settings.pdf_review_model_role.source_role == ROLE_IMAGE
        if follows_image_model:
            self.review_concurrency_spin.hide()
            self.review_concurrency_shared_input.show()
            self.review_concurrency_label.setText("并发数")
            _set_tooltip(
                self.review_concurrency_label,
                "并发数",
                "跟随 PDF 翻译模型时共用页生成并发，审核请求会按实际进度动态占用，无需单独设置。",
                TUNING_TOOLTIP["items"],
            )
            try:
                image_config = resolve_effective_model_config(self.settings, ROLE_IMAGE)
                image_throughput = get_model_throughput(self.settings, image_config)
                minimum, maximum = concurrency_bounds(image_config)
                self.review_concurrency_spin.blockSignals(True)
                self.review_concurrency_spin.setRange(minimum, maximum)
                self.review_concurrency_spin.setValue(image_throughput.concurrency)
                self.review_concurrency_spin.blockSignals(False)
            except Exception:
                self.review_concurrency_spin.setEnabled(False)
            return

        self.review_concurrency_shared_input.hide()
        self.review_concurrency_spin.show()
        _set_tooltip(
            self.review_concurrency_label,
            "并发数",
            "审核模型同时发起的请求数量。",
            TUNING_TOOLTIP["items"],
        )
        try:
            config = resolve_effective_model_config(self.settings, ROLE_PDF_REVIEW)
        except Exception:
            self.review_concurrency_spin.setEnabled(False)
            return
        throughput = get_model_throughput(self.settings, config)
        minimum, maximum = concurrency_bounds(config)
        self.review_concurrency_spin.blockSignals(True)
        self.review_concurrency_spin.setRange(minimum, maximum)
        self.review_concurrency_spin.setValue(throughput.concurrency)
        self.review_concurrency_spin.blockSignals(False)
        self.review_concurrency_spin.setEnabled(True)

    def _sync_engine_visibility(self) -> None:
        role = self._current_role()
        follows = self._is_following_current_role()
        is_translation = role == ROLE_TRANSLATION
        is_local_translation = is_translation and self.settings.engine.mode == "local"
        self.mode_label.setVisible(is_translation)
        self.mode_combo.setVisible(is_translation)
        always_model_widgets = (
            self.provider_label,
            self.provider_combo,
            self.base_url_label,
            self.base_url_input,
            self.model_label,
            self.model_combo,
        )
        for widget in always_model_widgets:
            widget.setVisible(True)
        self.api_key_label.setVisible(not is_local_translation)
        self.api_key_input.setVisible(not is_local_translation)
        access_editable = not follows
        try:
            config = resolve_effective_model_config(self.settings, role)
            base_url_editable = config.mode == "local" or cloud_provider_uses_base_url(config.provider)
        except Exception:
            base_url_editable = True
        self.provider_combo.setEnabled(access_editable)
        self.api_key_input.setEnabled(access_editable and not is_local_translation)
        self.base_url_input.setEnabled(access_editable and base_url_editable)
        self.model_combo.setEnabled(True)
        self.pdf_review_frame.setVisible(role == ROLE_IMAGE)
        self._sync_review_model_visibility()
        self._sync_throughput_fields()

    def _with_busy_cursor(
        self,
        fn: Callable[[], str],
        on_finished: Callable[[], None] | None = None,
    ) -> None:
        if self._background_closing or self._connectivity_worker is not None:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        worker = CallableWorker(fn, self)
        self._connectivity_worker = worker
        self._connectivity_on_finished = on_finished
        worker.resultReady.connect(self._on_connectivity_result)
        worker.threadFinished.connect(worker.deleteLater)
        worker.start()

    def _on_connectivity_result(self, result: object) -> None:
        if self._connectivity_worker is None:
            return
        self._connectivity_worker = None
        on_finished = self._connectivity_on_finished
        self._connectivity_on_finished = None
        QApplication.restoreOverrideCursor()
        if self._background_closing:
            return
        message = str(result) if isinstance(result, Exception) else str(result)
        QMessageBox.information(self, APP_NAME, message)
        if on_finished is not None:
            on_finished()

    def shutdown_background_tasks(self, timeout_ms: int = 100) -> None:
        self._background_closing = True
        self._model_fetch_generation += 1
        self._review_model_fetch_generation += 1
        self._connectivity_on_finished = None
        self._model_fetch_context = None
        self._review_model_fetch_context = None
        had_connectivity_worker = self._connectivity_worker is not None
        for attr_name in (
            "_connectivity_worker",
            "_model_fetch_worker",
            "_review_model_fetch_worker",
        ):
            worker = getattr(self, attr_name, None)
            setattr(self, attr_name, None)
            if worker is None:
                continue
            try:
                worker.resultReady.disconnect()
            except (RuntimeError, TypeError):
                pass
            request_background_stop(worker, "cancel")
            wait_for_background_task(worker, timeout_ms)
            if worker.isRunning():
                detach_running_qobject(worker)
                try:
                    worker.threadFinished.connect(worker.deleteLater)
                except (RuntimeError, TypeError):
                    pass
            else:
                worker.deleteLater()
        if had_connectivity_worker:
            QApplication.restoreOverrideCursor()

    def _test_connectivity(self) -> None:
        def run() -> str:
            try:
                role = self._current_role()
                if role == ROLE_IMAGE:
                    result = check_image_generation_connectivity(self.settings)
                    save_settings(self.settings)
                    return result.message if result.ok else f"{result.message}\n{result.detail}"
                result = check_connectivity(settings_for_text_role(self.settings, role))
                return result.message if result.ok else f"{result.message}\n{result.detail}"
            except LocalModelFollowNotAllowedError as exc:
                return str(exc)

        self._with_busy_cursor(run, self._refresh_role_status)

    def _test_review_connectivity(self) -> None:
        def run() -> str:
            try:
                result = check_pdf_review_connectivity(self.settings)
                save_settings(self.settings)
                return result.message if result.ok else f"{result.message}\n{result.detail}"
            except LocalModelFollowNotAllowedError as exc:
                return str(exc)

        self._with_busy_cursor(run, self._sync_review_model_fields)

    def _current_model_catalog_signature(self) -> str:
        try:
            config = resolve_effective_model_config(self.settings, self._current_role())
            provider = config.provider
            api_key = config.api_key
            base_url = config.base_url
        except Exception:
            provider = self.settings.engine.cloud_provider
            base_url = self.settings.engine.cloud_base_url
            api_key = get_key(provider, base_url)
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
        self._set_model_catalog_status("API 配置已变化，请重新获取模型列表。")

    def _set_model_combo_items(
        self,
        models: list[str],
        *,
        include_current: bool = True,
        current_text: str | None = None,
    ) -> None:
        try:
            config = resolve_effective_model_config(self.settings, self._current_role())
            fallback_model = config.model
        except Exception:
            fallback_model = self.settings.engine.cloud_model
        current = (
            str(current_text or "").strip()
            if current_text is not None
            else self.model_combo.currentText().strip() or fallback_model
        )
        options: list[str] = []
        seed_models = [current, *models] if include_current else models
        for model in seed_models:
            value = str(model or "").strip()
            if value and value not in options:
                options.append(value)
        if not options:
            options.append(fallback_model if current_text is None else current)

        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        self.model_combo.addItems(options)
        if current:
            self.model_combo.setCurrentText(current)
        refresh_combo_completer(self.model_combo)
        self.model_combo.blockSignals(False)

    def _fetch_models(self) -> None:
        if self._background_closing or self._model_fetch_worker is not None:
            return
        try:
            config = resolve_effective_model_config(self.settings, self._current_role())
        except Exception as exc:  # noqa: BLE001 - converted to UI message.
            QMessageBox.warning(self, APP_NAME, str(exc))
            return
        if config.mode != "local" and not provider_supports_capability(config.provider, config.capability):
            message = f"服务商不支持{config.label}所需能力，请更换服务商或手动填写模型。"
            self._set_model_catalog_status(message)
            QMessageBox.warning(self, APP_NAME, message)
            return
        role = config.role
        signature = build_model_catalog_signature(
            provider=config.provider,
            api_key=config.api_key,
            base_url=config.base_url,
        )
        previous_model = self.model_combo.currentText().strip()
        self._model_fetch_generation += 1
        generation = self._model_fetch_generation
        self._model_fetch_context = (generation, role, signature, previous_model)
        self.fetch_models_button.setEnabled(False)
        self._set_model_catalog_status("正在获取模型列表...")
        worker = CallableWorker(
            lambda: fetch_openai_compatible_models(
                provider=config.provider,
                api_key=config.api_key,
                base_url=config.base_url,
            ),
            self,
        )
        self._model_fetch_worker = worker
        worker.resultReady.connect(self._on_model_fetch_result)
        worker.threadFinished.connect(worker.deleteLater)
        worker.start()

    def _on_model_fetch_result(self, result: object) -> None:
        if self._model_fetch_worker is None:
            return
        self._model_fetch_worker = None
        context = self._model_fetch_context
        self._model_fetch_context = None
        self.fetch_models_button.setEnabled(True)
        if self._background_closing or context is None:
            return
        generation, role, signature, previous_model = context
        if generation != self._model_fetch_generation:
            return
        if role != self._current_role() or signature != self._current_model_catalog_signature():
            self._set_model_catalog_status("API 配置已变化，请重新获取模型列表。")
            return
        if isinstance(result, Exception):
            message = str(result)
            self._set_model_catalog_status(message)
            QMessageBox.warning(self, APP_NAME, message)
            return
        if not result.ok or not result.models:
            message = f"{result.message}\n{result.detail}".strip()
            self._set_model_catalog_status(message)
            QMessageBox.warning(self, APP_NAME, message)
            return
        if role == ROLE_IMAGE and "gpt-image-2" in result.models:
            selected_model = "gpt-image-2"
        else:
            selected_model = previous_model if previous_model in result.models else result.models[0]
        self._model_catalog_signature = signature
        self._model_catalog_models = list(result.models)
        self._set_model_combo_items(result.models, include_current=False)
        self.model_combo.setCurrentText(selected_model)
        select_combo_text_match(self.model_combo)
        self._on_model_changed()
        self._set_model_catalog_status(f"{result.message} 可从下拉列表选择。")
        self.model_combo.showPopup()

    def _fetch_review_models(self) -> None:
        if self._background_closing or self._review_model_fetch_worker is not None:
            return
        try:
            config = resolve_effective_model_config(self.settings, ROLE_PDF_REVIEW)
        except Exception as exc:  # noqa: BLE001 - converted to UI message.
            QMessageBox.warning(self, APP_NAME, str(exc))
            return
        if not provider_supports_capability(config.provider, config.capability):
            message = f"服务商不支持{config.label}所需审核能力，请更换服务商或手动填写模型。"
            self._set_review_model_status(message)
            QMessageBox.warning(self, APP_NAME, message)
            return
        signature = build_model_catalog_signature(
            provider=config.provider,
            api_key=config.api_key,
            base_url=config.base_url,
        )
        previous_model = self.review_model_combo.currentText().strip()
        self._review_model_fetch_generation += 1
        generation = self._review_model_fetch_generation
        self._review_model_fetch_context = (generation, signature, previous_model)
        self.fetch_review_models_button.setEnabled(False)
        self._set_review_model_status("正在获取审核模型列表...")
        worker = CallableWorker(
            lambda: fetch_openai_compatible_models(
                provider=config.provider,
                api_key=config.api_key,
                base_url=config.base_url,
            ),
            self,
        )
        self._review_model_fetch_worker = worker
        worker.resultReady.connect(self._on_review_model_fetch_result)
        worker.threadFinished.connect(worker.deleteLater)
        worker.start()

    def _on_review_model_fetch_result(self, result: object) -> None:
        if self._review_model_fetch_worker is None:
            return
        self._review_model_fetch_worker = None
        context = self._review_model_fetch_context
        self._review_model_fetch_context = None
        self.fetch_review_models_button.setEnabled(True)
        if self._background_closing or context is None:
            return
        generation, signature, previous_model = context
        if generation != self._review_model_fetch_generation:
            return
        try:
            current = resolve_effective_model_config(self.settings, ROLE_PDF_REVIEW)
            current_signature = build_model_catalog_signature(
                provider=current.provider,
                api_key=current.api_key,
                base_url=current.base_url,
            )
        except Exception:
            self._set_review_model_status("审核模型配置已变化，请重新获取模型列表。")
            return
        if signature != current_signature:
            self._set_review_model_status("审核模型配置已变化，请重新获取模型列表。")
            return
        if isinstance(result, Exception):
            message = str(result)
            self._set_review_model_status(message)
            QMessageBox.warning(self, APP_NAME, message)
            return
        if not result.ok or not result.models:
            message = f"{result.message}\n{result.detail}".strip()
            self._set_review_model_status(message)
            QMessageBox.warning(self, APP_NAME, message)
            return
        selected_model = previous_model if previous_model in result.models else result.models[0]
        self._set_review_model_combo_items(result.models)
        self.review_model_combo.setCurrentText(selected_model)
        select_combo_text_match(self.review_model_combo)
        self._on_review_model_changed()
        self._set_review_model_status(f"{result.message} 可从下拉列表选择。")
        self.review_model_combo.showPopup()

class CompactNavRail(QFrame):
    """Small-screen navigation rail that keeps the work area wide."""

    navigateRequested = Signal(str)
    settingsRequested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("CompactNavRail")
        self.setFixedWidth(COMPACT_NAV_RAIL_WIDTH)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 12, 8, 12)
        root.setSpacing(8)

        brand = QLabel("OA")
        brand.setObjectName("SectionTitle")
        brand.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(brand)

        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)
        self._buttons: dict[str, QPushButton] = {}
        nav_items = [
            ("excel_translate", "Excel", "Excel 翻译"),
            ("word_translate", "Word", "Word 翻译"),
            ("pdf_translate", "PDF", "PDF 翻译"),
            ("tm", "TM", "记忆库管理"),
        ]
        for index, (page, short_title, title) in enumerate(nav_items):
            button = QPushButton(short_title)
            button.setObjectName("RailButton")
            button.setCheckable(True)
            _set_tooltip(button, title, f"切换到{title}。")
            button.clicked.connect(lambda _=False, key=page: self.navigateRequested.emit(key))
            self._nav_group.addButton(button)
            self._buttons[page] = button
            root.addWidget(button)
            if index == 0:
                button.setChecked(True)

        root.addStretch(1)

        settings_button = QPushButton("设置")
        settings_button.setObjectName("RailButton")
        _set_tooltip(settings_button, "设置", "打开完整模型、领域和 Prompt 设置。")
        settings_button.clicked.connect(self.settingsRequested.emit)
        root.addWidget(settings_button)

    def set_active_page(self, page: str) -> None:
        button = self._buttons.get(page)
        if button is not None:
            button.setChecked(True)


class NativeMainWindow(QMainWindow):
    """Top-level native desktop window."""

    TRANSLATION_PAGE_LABELS = {
        "excel_translate": "Excel 翻译",
        "word_translate": "Word 翻译",
        "pdf_translate": "PDF 翻译",
    }

    def __init__(self, settings: AppSettings):
        super().__init__()
        app = QApplication.instance()
        if app is not None:
            install_in_app_tooltips(app)
        self.settings = settings
        self.setWindowTitle(APP_NAME)
        self._update_worker: UpdateCheckWorker | None = None
        self._update_check_source = ""
        self._latest_update_result: UpdateCheckResult | None = None
        self._closing = False
        self._current_page = "excel_translate"
        self._compact_shell = False
        self._task_resource_registry = TaskResourceRegistry()

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.compact_nav = CompactNavRail()
        self.compact_nav.hide()
        self.sidebar = Sidebar(settings)
        self.stack = QStackedWidget()
        self.pages = {
            "excel_translate": ExcelTranslatePage(settings, self._task_resource_registry),
            "word_translate": WordTranslatePage(settings, self._task_resource_registry),
            "pdf_translate": PdfTranslatePage(settings, self._task_resource_registry),
            "tm": TmManagerPage(settings, self._task_resource_registry),
        }
        for page in self.pages.values():
            self.stack.addWidget(page)
        self.compact_nav.navigateRequested.connect(self._navigate)
        self.compact_nav.settingsRequested.connect(self._open_compact_settings)
        self.sidebar.navigateRequested.connect(self._navigate)
        self.sidebar.updateCheckRequested.connect(lambda: self._start_update_check("manual"))
        self.sidebar.globalUpdateIgnoreToggled.connect(self._toggle_global_update_ignore)
        self.sidebar.currentUpdateIgnored.connect(self._ignore_current_update_version)
        self.sidebar.updatePromptRequested.connect(self._show_latest_update_dialog)
        self.sidebar.settingsChanged.connect(self._refresh_pages_from_settings)
        self.pages["excel_translate"].languageChanged.connect(
            self._sync_tm_language_from_translation
        )
        self.pages["word_translate"].languageChanged.connect(
            self._sync_tm_language_from_translation
        )
        layout.addWidget(self.compact_nav)
        layout.addWidget(self.sidebar)
        layout.addWidget(self.stack, 1)
        self.setCentralWidget(central)
        self._build_menu()
        self._sync_page_activation("excel_translate")
        self._task_lock_timer = QTimer(self)
        self._task_lock_timer.setInterval(500)
        self._task_lock_timer.timeout.connect(self._sync_translation_task_locks)
        self._task_lock_timer.start()
        self._sync_translation_task_locks()
        QTimer.singleShot(600, lambda: self._start_update_check("auto"))

    def apply_initial_window_layout(self) -> None:
        screen = self.screen() or QApplication.primaryScreen()
        available = screen.availableGeometry() if screen is not None else None
        available_width = available.width() if available is not None else DEFAULT_WINDOW_WIDTH
        available_height = available.height() if available is not None else DEFAULT_WINDOW_HEIGHT
        compact = available_width <= COMPACT_SHELL_SCREEN_WIDTH
        self.set_compact_shell(compact)

        width_ratio = 0.94 if compact else 0.86
        target_width = min(DEFAULT_WINDOW_WIDTH, int(available_width * width_ratio))
        target_width = max(self.minimumSizeHint().width(), target_width)
        target_width = min(target_width, available_width)
        target_height = min(DEFAULT_WINDOW_HEIGHT, int(available_height * 0.92))
        self.resize(target_width, target_height)

        if available is not None:
            x = available.x() + max(0, (available.width() - self.width()) // 2)
            y = available.y() + max(0, (available.height() - self.height()) // 2)
            self.move(x, y)

    def set_compact_shell(self, compact: bool) -> None:
        self._compact_shell = compact
        self.compact_nav.setVisible(compact)
        self.sidebar.setFixedWidth(0 if compact else SIDEBAR_WIDTH)
        self.sidebar.setVisible(not compact)
        tm_page = self.pages.get("tm")
        if hasattr(tm_page, "set_compact_layout"):
            tm_page.set_compact_layout(compact)
        self.compact_nav.set_active_page(self._current_page)
        self.sidebar.set_active_page(self._current_page)
        layout = self.centralWidget().layout()
        layout.invalidate()
        layout.activate()
        self.stack.updateGeometry()
        self.centralWidget().updateGeometry()
        self.updateGeometry()

    def _refresh_pages_from_settings(self) -> None:
        self.pages["excel_translate"].refresh_settings()
        self.pages["word_translate"].refresh_settings()
        self.pages["pdf_translate"].refresh_settings()
        self.pages["tm"].refresh_settings()

    def _open_compact_settings(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("设置")
        dialog.setModal(False)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        sidebar = Sidebar(self.settings)
        sidebar.set_active_page(self._current_page)
        if self._latest_update_result is not None:
            sidebar.set_update_notice(
                self._latest_update_result if self._latest_update_result.has_update else None
            )
        sidebar.sync_update_ignore_button()
        layout.addWidget(sidebar)

        def navigate_from_dialog(page: str) -> None:
            self._navigate(page)
            dialog.accept()

        sidebar.navigateRequested.connect(navigate_from_dialog)
        sidebar.updateCheckRequested.connect(lambda: self._start_update_check("manual"))
        sidebar.globalUpdateIgnoreToggled.connect(self._toggle_global_update_ignore)
        sidebar.currentUpdateIgnored.connect(self._ignore_current_update_version)
        sidebar.updatePromptRequested.connect(self._show_latest_update_dialog)
        sidebar.settingsChanged.connect(self._refresh_pages_from_settings)

        screen = self.screen() or QApplication.primaryScreen()
        available = screen.availableGeometry() if screen is not None else None
        dialog_width = SIDEBAR_WIDTH
        dialog_height = min(
            860,
            int((available.height() if available is not None else DEFAULT_WINDOW_HEIGHT) * 0.9),
        )
        dialog.resize(dialog_width, dialog_height)
        dialog.exec()

    def _start_update_check(self, source: str) -> None:
        if self._closing:
            return
        if self._update_worker is not None and self._update_worker.isRunning():
            if source != "auto":
                self._update_check_source = source
                self.sidebar.set_update_checking(True)
            return
        self._update_check_source = source
        self.sidebar.set_update_checking(True)
        worker = UpdateCheckWorker(self)
        self._update_worker = worker
        worker.resultReady.connect(self._on_update_check_finished)
        worker.threadFinished.connect(worker.deleteLater)
        worker.start()

    def _on_update_check_finished(self, result: object) -> None:
        if self._closing:
            return
        source = self._update_check_source or "auto"
        self._update_check_source = ""
        self._update_worker = None
        self.sidebar.set_update_checking(False)

        if not isinstance(result, UpdateCheckResult):
            self.sidebar.set_update_notice(None)
            if source != "auto":
                QMessageBox.warning(self, APP_NAME, "检查更新失败：返回结果异常。")
            return

        self._latest_update_result = result
        self._handle_update_check_result(result, source)

    def _handle_update_check_result(
        self,
        result: UpdateCheckResult,
        source: str,
    ) -> None:
        if not result.ok or result.status == "error":
            self.sidebar.set_update_notice(None)
            if source != "auto":
                QMessageBox.warning(self, APP_NAME, result.message)
            return

        if result.status == "unknown":
            self.sidebar.set_update_notice(None)
            if source != "auto":
                QMessageBox.information(self, APP_NAME, result.message)
            return

        if not result.has_update:
            self.sidebar.set_update_notice(None)
            if source != "auto":
                QMessageBox.information(self, APP_NAME, result.message)
            return

        if source == "manual":
            self._show_manual_update_result(result)
            return

        if source == "restore":
            self._show_update_notice_if_allowed(result)
            return

        self._handle_automatic_update(result)

    def _handle_automatic_update(self, result: UpdateCheckResult) -> None:
        if self._is_release_ignored(result):
            self.sidebar.set_update_notice(None)
            return

        is_major = is_major_upgrade(result.latest_version, result.current_version)
        if self.settings.update.ignore_updates:
            if is_major and not self._major_prompt_was_shown(result):
                self._show_update_dialog(result)
                self._record_major_prompt_shown(result)
            self.sidebar.set_update_notice(None)
            return

        if is_major:
            if not self._major_prompt_was_shown(result):
                self._show_update_dialog(result)
                self._record_major_prompt_shown(result)
            self.sidebar.set_update_notice(None)
            return

        self.sidebar.set_update_notice(result)

    def _show_update_notice_if_allowed(self, result: UpdateCheckResult) -> None:
        if (
            result.has_update
            and not self.settings.update.ignore_updates
            and not self._is_release_ignored(result)
        ):
            self.sidebar.set_update_notice(result)
            return
        self.sidebar.set_update_notice(None)

    def _show_manual_update_result(self, result: UpdateCheckResult) -> None:
        self._show_update_notice_if_allowed(result)
        if self.settings.update.ignore_updates:
            self._show_global_ignored_update_dialog(result)
            return
        if self._is_release_ignored(result):
            self._show_release_ignored_update_dialog(result)
            return
        self._show_update_dialog(result)

    def _show_latest_update_dialog(self) -> None:
        result = self._latest_update_result
        if result is None or not result.has_update:
            self._start_update_check("manual")
            return
        self._show_update_dialog(result)

    def _toggle_global_update_ignore(self) -> None:
        self.settings.update.ignore_updates = not self.settings.update.ignore_updates
        save_settings(self.settings)
        self.sidebar.sync_update_ignore_button()
        if self.settings.update.ignore_updates:
            self.sidebar.set_update_notice(None)
            return
        self._start_update_check("restore")

    def _ignore_current_update_version(self) -> None:
        result = self._latest_update_result
        if result is None or not result.has_update:
            self.sidebar.set_update_notice(None)
            return
        self.settings.update.ignored_release_version = result.latest_version
        save_settings(self.settings)
        self.sidebar.set_update_notice(None)

    def _is_release_ignored(self, result: UpdateCheckResult) -> bool:
        ignored = self.settings.update.ignored_release_version.strip()
        return bool(ignored and ignored == result.latest_version)

    def _major_prompt_was_shown(self, result: UpdateCheckResult) -> bool:
        latest_major = major_version(result.latest_version)
        return (
            latest_major is not None
            and self.settings.update.last_prompted_major_version == str(latest_major)
        )

    def _record_major_prompt_shown(self, result: UpdateCheckResult) -> None:
        latest_major = major_version(result.latest_version)
        if latest_major is None:
            return
        self.settings.update.last_prompted_major_version = str(latest_major)
        save_settings(self.settings)

    def _show_global_ignored_update_dialog(self, result: UpdateCheckResult) -> None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle(APP_NAME)
        box.setText(f"发现新版 V{result.latest_version}")
        box.setInformativeText("常规更新提醒已暂停。")
        restore_button = box.addButton("恢复更新", QMessageBox.ButtonRole.AcceptRole)
        release_button = box.addButton("查看发布页", QMessageBox.ButtonRole.ActionRole)
        box.addButton("关闭", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is restore_button:
            self.settings.update.ignore_updates = False
            save_settings(self.settings)
            self.sidebar.sync_update_ignore_button()
            self._show_update_notice_if_allowed(result)
        elif clicked is release_button:
            self._open_update_url(result.release_url)

    def _show_release_ignored_update_dialog(self, result: UpdateCheckResult) -> None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle(APP_NAME)
        box.setText(f"发现新版 V{result.latest_version}")
        box.setInformativeText("该版本已被忽略。")
        restore_button = box.addButton("取消忽略", QMessageBox.ButtonRole.AcceptRole)
        release_button = box.addButton("查看发布页", QMessageBox.ButtonRole.ActionRole)
        box.addButton("关闭", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is restore_button:
            self.settings.update.ignored_release_version = ""
            save_settings(self.settings)
            self._show_update_notice_if_allowed(result)
        elif clicked is release_button:
            self._open_update_url(result.release_url)

    def _show_update_dialog(self, result: UpdateCheckResult) -> None:
        notes = _release_notes_preview(result.release_notes)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle(APP_NAME)
        box.setText(f"发现新版 V{result.latest_version}")
        box.setInformativeText(
            f"当前版本：V{APP_VERSION}\n"
            f"最新版本：V{result.latest_version}\n\n"
            f"更新内容：\n{notes}"
        )
        install_button = None
        if result.asset_name and result.download_url:
            install_button = box.addButton("下载安装包", QMessageBox.ButtonRole.AcceptRole)
        release_button = box.addButton("查看发布页", QMessageBox.ButtonRole.ActionRole)
        box.addButton("稍后", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if install_button is not None and clicked is install_button:
            self._open_update_url(result.download_url)
        elif clicked is release_button:
            self._open_update_url(result.release_url)

    def _open_update_url(self, url: str) -> None:
        target = str(url or "").strip()
        if not target:
            QMessageBox.warning(self, APP_NAME, "没有可打开的更新链接。")
            return
        if not QDesktopServices.openUrl(QUrl(target)):
            QMessageBox.warning(self, APP_NAME, f"无法打开链接：\n{target}")

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("文件")
        quit_action = QAction("退出", self)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

    def _navigate(self, page: str) -> None:
        page_order = ["excel_translate", "word_translate", "pdf_translate", "tm"]
        self.stack.setCurrentIndex(page_order.index(page))
        self._current_page = page
        self.sidebar.set_active_page(page)
        self.compact_nav.set_active_page(page)
        self._sync_page_activation(page)
        self._sync_translation_task_locks()

    def _sync_page_activation(self, active_page: str) -> None:
        for page_key, page in self.pages.items():
            if hasattr(page, "set_page_active"):
                page.set_page_active(page_key == active_page)

    def _is_translation_page_running(self, page_key: str) -> bool:
        page = self.pages.get(page_key)
        if page is None:
            return False
        return (
            getattr(page, "runner", None) is not None
            and getattr(page, "done", None) is None
            and getattr(page, "phase", "") == "running"
        )

    def _running_translation_tasks(self) -> list[dict[str, object]]:
        tasks: list[dict[str, object]] = []
        reserved_owner_keys: set[str] = set()
        for reservation in self._task_resource_registry.reservations():
            reserved_owner_keys.add(reservation.owner_key)
            tasks.append(
                {
                    "page_key": reservation.owner_key,
                    "label": reservation.owner_label,
                    "api_groups": reservation.resources,
                    "api_error": "" if reservation.resources else "API 资源无法识别",
                }
            )
        for page_key in self.TRANSLATION_PAGE_LABELS:
            page = self.pages.get(page_key)
            if (
                page is None
                or page_key in reserved_owner_keys
                or not self._is_translation_page_running(page_key)
            ):
                continue
            groups = frozenset(
                getattr(page, "current_task_api_groups", set()) or set()
            )
            error = ""
            if not groups:
                try:
                    groups = task_api_groups_for_page(self.settings, page_key)
                except Exception as exc:  # noqa: BLE001 - used for conservative locking.
                    error = str(exc)
            tasks.append(
                {
                    "page_key": page_key,
                    "label": self.TRANSLATION_PAGE_LABELS.get(page_key, ""),
                    "api_groups": groups,
                    "api_error": error,
                }
            )
        return tasks

    def _sync_translation_task_locks(self) -> None:
        running_tasks = self._running_translation_tasks()
        for page_key, label in self.TRANSLATION_PAGE_LABELS.items():
            page = self.pages.get(page_key)
            if page is None or not hasattr(page, "set_external_task_lock"):
                continue
            if self._is_translation_page_running(page_key) or not running_tasks:
                page.set_external_task_lock(False, label, "")
                continue

            locked = False
            owner_label = ""
            reason = ""
            try:
                candidate_groups = task_api_groups_for_page(self.settings, page_key)
                candidate_error = ""
            except Exception as exc:  # noqa: BLE001 - avoid unsafe parallel start.
                candidate_groups = frozenset()
                candidate_error = str(exc)

            for task in running_tasks:
                if task.get("page_key") == page_key:
                    continue
                task_label = str(task.get("label") or "其他翻译")
                task_groups = frozenset(
                    task.get("api_groups") or frozenset()
                )
                if candidate_error:
                    locked = True
                    owner_label = task_label
                    reason = (
                        "已有翻译任务正在运行；当前模型配置无法判断 API，"
                        "请先完成模型配置或等待任务结束。"
                    )
                    break
                if not candidate_groups or not task_groups:
                    locked = True
                    owner_label = task_label
                    reason = (
                        f"{task_label}正在运行；无法确认当前 API 是否独立，"
                        "请切换到明确不同的 API 后再启动。"
                    )
                    break
                if candidate_groups & task_groups:
                    locked = True
                    owner_label = task_label
                    reason = (
                        f"{task_label}正在使用当前 API，请切换到不同 API 后再启动。"
                    )
                    break

            page.set_external_task_lock(locked, owner_label if locked else label, reason)

    def _sync_tm_language_from_translation(
        self,
        target_lang: str,
        source_lang: str,
    ) -> None:
        self.pages["tm"].sync_language_from_translation(target_lang, source_lang)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API name.
        self._closing = True
        self._task_lock_timer.stop()
        self.sidebar.shutdown_background_tasks(timeout_ms=100)
        for page in self.pages.values():
            shutdown = getattr(page, "shutdown_background_tasks", None)
            if callable(shutdown):
                try:
                    shutdown(timeout_ms=100)
                except Exception:  # noqa: BLE001 - one task must not block app exit.
                    pass
        self._task_resource_registry.release_all()
        worker = self._update_worker
        self._update_worker = None
        self._update_check_source = ""
        if worker is not None:
            try:
                worker.resultReady.disconnect(self._on_update_check_finished)
            except (RuntimeError, TypeError):
                pass
            request_background_stop(worker, "cancel", "requestInterruption", "quit")
            wait_for_background_task(worker, 100)
            if worker.isRunning():
                detach_running_qobject(worker)
                try:
                    worker.threadFinished.connect(worker.deleteLater)
                except (RuntimeError, TypeError):
                    pass
            else:
                worker.deleteLater()
        save_settings(self.settings)
        super().closeEvent(event)
