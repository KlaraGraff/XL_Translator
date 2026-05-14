"""Word translation page."""

from __future__ import annotations

import html
from pathlib import Path

import streamlit as st

from config import (
    WORD_BATCH_CHARS_MAX,
    WORD_BATCH_CHARS_MIN,
    WORD_BATCH_PARAGRAPHS_MAX,
    WORD_BATCH_PARAGRAPHS_MIN,
    WORD_BATCH_SPLIT_CHARS_MAX,
    WORD_BATCH_SPLIT_CHARS_MIN,
    WORD_REVIEW_HIGHLIGHT_COLOR_DEFAULT,
    WORD_STRICT_RETRY_ATTEMPTS_MAX,
    WORD_STRICT_RETRY_ATTEMPTS_MIN,
)
from core.bilingual_writer import (
    custom_output_dir_will_be_created,
    get_custom_output_dir_error,
    resolve_custom_output_dir,
)
from core.language_registry import (
    get_default_source_lang,
    get_default_target_lang,
    get_source_lang_codes,
    get_source_lang_display,
    get_target_lang_display,
    is_supported_source_lang,
    is_supported_target_lang,
)
from core.task_runner import DoneMsg, ErrorMsg, LogMsg, ProgressMsg, StatusMsg, StoppedMsg
from core.word_document import is_supported_word_file, scan_word_path
from core.word_task_runner import WordTaskRunner
from settings import AppSettings
from ui.components import (
    render_checkbox_tooltip_row,
    render_field_group,
    render_log_area,
    render_main_tooltip_support,
    render_setting_card,
    render_stat_cards,
)
from ui.native_dialogs import pick_folder, pick_word_file
from ui.target_language_selector import render_target_lang_selectbox

_PHASE = "word_translate_phase"
_RUNNER = "word_translate_runner"
_LOGS = "word_translate_logs"
_PROGRESS = "word_translate_progress"
_STATUS = "word_translate_status"
_DONE = "word_translate_done"
_FILES = "word_translate_file_items"
_STOP_PENDING = "word_translate_stop_pending"
_STOP_MESSAGE = "word_translate_stop_message"
_SOURCE_ROOT = "word_translate_source_root"
_LANG_SELECTED = "word_translate_selected_target_lang"
_LANG_SELECTBOX_KEY = "word_translate_target_lang_select"
_SOURCE_LANG_SELECTED = "word_translate_selected_source_lang"
_SOURCE_LANG_SELECTBOX_KEY = "word_translate_source_lang_select"
_BROWSE_DIALOG_OPEN = "word_translate_browse_dialog_open"
_PENDING_SCAN_PATH = "word_translate_pending_scan_path"
_PENDING_SOURCE_INPUT_VALUE = "word_translate_pending_source_input_value"
_BROWSE_ERROR = "word_translate_browse_error"
_FOLDER_INPUT_KEY = "word_translate_source_input"
_WORD_HIGHLIGHT_ENABLED_KEY = "word_highlight_unresolved_checkbox"
_WORD_HIGHLIGHT_COLOR_KEY = "word_highlight_color_select"
_WORD_HIGHLIGHT_COLORS = {
    "浅黄": "FFF2CC",
    "浅蓝": "DDEBFF",
    "浅红": "F4CCCC",
    "浅紫": "EADCF8",
    "浅灰": "E7E6E6",
}


def _normalize_word_highlight_color(value: str | None) -> str:
    color = str(value or "").strip().lstrip("#").upper()
    if color in _WORD_HIGHLIGHT_COLORS.values():
        return color
    return WORD_REVIEW_HIGHLIGHT_COLOR_DEFAULT


def _format_word_highlight_color(value: str) -> str:
    color = _normalize_word_highlight_color(value)
    for label, candidate in _WORD_HIGHLIGHT_COLORS.items():
        if candidate == color:
            return f"{label}  #{color}"
    return f"#{color}"


def _init(settings: AppSettings | None = None) -> None:
    for key, value in [
        (_PHASE, "idle"),
        (_RUNNER, None),
        (_LOGS, []),
        (_PROGRESS, None),
        (_STATUS, None),
        (_DONE, None),
        (_FILES, []),
        (_STOP_PENDING, False),
        (_STOP_MESSAGE, ""),
        (_BROWSE_DIALOG_OPEN, False),
        (_PENDING_SCAN_PATH, None),
        (_PENDING_SOURCE_INPUT_VALUE, None),
        (_BROWSE_ERROR, ""),
    ]:
        if key not in st.session_state:
            st.session_state[key] = value
    if not st.session_state.get(_SOURCE_ROOT):
        st.session_state[_SOURCE_ROOT] = settings.last_source_folder if settings else ""
    if _FOLDER_INPUT_KEY not in st.session_state:
        st.session_state[_FOLDER_INPUT_KEY] = settings.last_source_folder if settings else ""
    if _LANG_SELECTED not in st.session_state and settings is not None:
        initial_target_lang = (
            settings.target_lang
            if is_supported_target_lang(
                settings.target_lang,
                settings.custom_target_langs,
                include_optional=True,
            )
            else get_default_target_lang()
        )
        st.session_state[_LANG_SELECTED] = initial_target_lang
    if _SOURCE_LANG_SELECTED not in st.session_state:
        st.session_state[_SOURCE_LANG_SELECTED] = ""
    if _SOURCE_LANG_SELECTBOX_KEY not in st.session_state:
        st.session_state[_SOURCE_LANG_SELECTBOX_KEY] = ""
    if "word_cb_task_log" not in st.session_state:
        st.session_state["word_cb_task_log"] = settings.output.enable_task_log if settings else False
    if "word_output_mode_radio" not in st.session_state:
        st.session_state["word_output_mode_radio"] = (
            "自定义目录" if settings and settings.output.use_custom_output_dir else "源目录内"
        )
    if "word_custom_output_dir_input" not in st.session_state:
        st.session_state["word_custom_output_dir_input"] = (
            settings.output.custom_output_dir if settings else ""
        )
    if "word_batch_paragraphs_input" not in st.session_state:
        st.session_state["word_batch_paragraphs_input"] = (
            settings.word_batch.max_paragraphs_per_batch if settings else 4
        )
    if "word_batch_chars_input" not in st.session_state:
        st.session_state["word_batch_chars_input"] = (
            settings.word_batch.max_chars_per_batch if settings else 3000
        )
    if "word_split_chars_input" not in st.session_state:
        st.session_state["word_split_chars_input"] = (
            settings.word_batch.split_paragraph_chars if settings else 6000
        )
    if "word_retry_attempts_input" not in st.session_state:
        st.session_state["word_retry_attempts_input"] = (
            settings.word_batch.strict_retry_attempts if settings else 3
        )
    if _WORD_HIGHLIGHT_ENABLED_KEY not in st.session_state:
        st.session_state[_WORD_HIGHLIGHT_ENABLED_KEY] = (
            settings.word_review.highlight_unresolved if settings else False
        )
    if _WORD_HIGHLIGHT_COLOR_KEY not in st.session_state:
        st.session_state[_WORD_HIGHLIGHT_COLOR_KEY] = _normalize_word_highlight_color(
            settings.word_review.highlight_color if settings else WORD_REVIEW_HIGHLIGHT_COLOR_DEFAULT
        )


def _get_selected_files() -> list:
    files = st.session_state.get(_FILES, [])
    return [
        file_item
        for index, file_item in enumerate(files)
        if st.session_state.get(f"word_file_check_{index}", True)
    ]


def _sync_word_target_lang_from_widget(settings: AppSettings) -> None:
    widget_target_lang = st.session_state.get(_LANG_SELECTBOX_KEY)
    if not is_supported_target_lang(
        widget_target_lang,
        settings.custom_target_langs,
        include_optional=True,
    ):
        return
    st.session_state[_LANG_SELECTED] = widget_target_lang
    settings.target_lang = widget_target_lang


def _sync_word_source_lang_from_widget(settings: AppSettings) -> None:
    widget_source_lang = st.session_state.get(_SOURCE_LANG_SELECTBOX_KEY, "")
    if not widget_source_lang:
        st.session_state[_SOURCE_LANG_SELECTED] = ""
        return
    if not is_supported_source_lang(widget_source_lang):
        return
    st.session_state[_SOURCE_LANG_SELECTED] = widget_source_lang
    settings.source_lang = widget_source_lang


def _get_selected_target_lang(settings: AppSettings) -> str:
    selected_target_lang = (
        st.session_state.get(_LANG_SELECTBOX_KEY)
        or st.session_state.get(_LANG_SELECTED)
        or settings.target_lang
    )
    if is_supported_target_lang(
        selected_target_lang,
        settings.custom_target_langs,
        include_optional=True,
    ):
        return selected_target_lang
    return get_default_target_lang()


def _get_selected_source_lang() -> str:
    selected_source_lang = (
        st.session_state.get(_SOURCE_LANG_SELECTBOX_KEY)
        or st.session_state.get(_SOURCE_LANG_SELECTED)
        or ""
    )
    if is_supported_source_lang(selected_source_lang):
        return selected_source_lang
    return ""


def _render_word_page_header(settings: AppSettings, phase: str) -> None:
    phase_map = {
        "idle": "待执行",
        "running": "执行中",
        "done": "已完成",
        "error": "异常",
        "stopped": "已中止",
    }
    selected_files = _get_selected_files()
    current_root = (st.session_state.get(_SOURCE_ROOT) or settings.last_source_folder or "").strip()
    target_label = get_target_lang_display(
        _get_selected_target_lang(settings),
        settings.custom_target_langs,
        include_optional=True,
    )
    st.markdown(
        '<div class="page-head">'
        '  <div class="page-head__eyebrow">Translate Workspace</div>'
        '  <div class="page-head__title-row">'
        '    <div class="page-head__title-block">'
        '      <div class="page-head__title">Word 翻译</div>'
        '    </div>'
        '    <div class="page-head__status-cluster">'
        f'      <span class="meta-pill"><span class="meta-pill__label">目标语言</span><span class="meta-pill__value">{html.escape(target_label)}</span></span>'
        f'      <span class="meta-pill"><span class="meta-pill__label">已选文件</span><span class="meta-pill__value">{len(selected_files)} 个</span></span>'
        f'      <span class="meta-pill meta-pill--mono meta-pill--path" title="{html.escape(current_root or "尚未选择源路径", quote=True)}"><span class="meta-pill__label">源路径</span><span class="meta-pill__value">{html.escape(current_root or "尚未选择")}</span></span>'
        f'      <div class="phase-badge">{phase_map.get(phase, "待执行")}</div>'
        '    </div>'
        '  </div>'
        '</div>',
        unsafe_allow_html=True,
    )


def _set_scanned_files(items: list) -> None:
    st.session_state[_FILES] = items
    st.session_state[_PHASE] = "idle"
    st.session_state[_DONE] = None
    st.session_state[_LOGS] = []
    st.session_state[_PROGRESS] = None
    st.session_state[_STATUS] = None
    st.session_state[_STOP_PENDING] = False
    st.session_state[_STOP_MESSAGE] = ""
    for index in range(len(items)):
        st.session_state[f"word_file_check_{index}"] = True
    st.session_state["word_file_check_all"] = True


def _scan_source_path(raw_path: str, settings: AppSettings) -> bool:
    candidate = (raw_path or "").strip().strip('"')
    if not candidate:
        st.error("请输入文件夹或文件路径。")
        return False

    input_path = Path(candidate)
    if not input_path.exists():
        st.error(f"路径不存在：{candidate}")
        return False
    if input_path.is_file() and not is_supported_word_file(input_path):
        st.error("不支持的文件类型：仅支持 .docx 文件。")
        return False

    normalized_path = str(input_path)
    settings.last_source_folder = normalized_path
    st.session_state["settings"] = settings
    st.session_state[_SOURCE_ROOT] = str(input_path if input_path.is_dir() else input_path.parent)
    with st.spinner("扫描中..."):
        items = scan_word_path(input_path)
    _set_scanned_files(items)
    return True


def _get_source_input_candidate(settings: AppSettings) -> str:
    return (
        st.session_state.get(_FOLDER_INPUT_KEY)
        or settings.last_source_folder
        or ""
    ).strip().strip('"')


def _close_browse_dialog(*, clear_error: bool = True) -> None:
    st.session_state[_BROWSE_DIALOG_OPEN] = False
    if clear_error:
        st.session_state[_BROWSE_ERROR] = ""


def _pick_source_path(mode: str, settings: AppSettings) -> None:
    current_path = _get_source_input_candidate(settings)
    try:
        selected_path = pick_folder(current_path) if mode == "folder" else pick_word_file(current_path)
    except RuntimeError as exc:
        st.session_state[_BROWSE_ERROR] = str(exc)
        return

    if not selected_path:
        _close_browse_dialog()
        st.rerun()

    st.session_state[_PENDING_SOURCE_INPUT_VALUE] = selected_path
    st.session_state[_PENDING_SCAN_PATH] = selected_path
    _close_browse_dialog()
    st.rerun()


@st.dialog("浏览源路径")
def _render_source_browse_dialog(settings: AppSettings) -> None:
    st.markdown("请选择要扫描的源路径类型。")
    st.caption("选择文件夹会递归扫描其中所有 Word 文件；选择 Word 文件则只扫描该文件。")

    browse_error = st.session_state.get(_BROWSE_ERROR, "")
    if browse_error:
        st.error(f"无法打开系统选择窗口：{browse_error}")

    col_folder, col_file = st.columns(2, gap="small")
    with col_folder:
        if st.button("选择文件夹", use_container_width=True, key="word_browse_folder_button"):
            _pick_source_path("folder", settings)
    with col_file:
        if st.button("选择 Word 文件", type="secondary", use_container_width=True, key="word_browse_file_button"):
            _pick_source_path("file", settings)

    if st.button("取消", use_container_width=True, key="word_browse_cancel_button"):
        _close_browse_dialog()
        st.rerun()


def _sync_word_inspector_state(settings: AppSettings) -> AppSettings:
    settings.output.enable_task_log = st.session_state.get(
        "word_cb_task_log",
        settings.output.enable_task_log,
    )
    output_mode = st.session_state.get(
        "word_output_mode_radio",
        "自定义目录" if settings.output.use_custom_output_dir else "源目录内",
    )
    settings.output.use_custom_output_dir = output_mode == "自定义目录"
    if settings.output.use_custom_output_dir:
        settings.output.custom_output_dir = (
            st.session_state.get("word_custom_output_dir_input", settings.output.custom_output_dir)
            .strip()
            .strip('"')
        )
    else:
        settings.output.custom_output_dir = ""
    settings.word_batch.max_paragraphs_per_batch = int(
        st.session_state.get(
            "word_batch_paragraphs_input",
            settings.word_batch.max_paragraphs_per_batch,
        )
    )
    settings.word_batch.max_chars_per_batch = int(
        st.session_state.get(
            "word_batch_chars_input",
            settings.word_batch.max_chars_per_batch,
        )
    )
    settings.word_batch.split_paragraph_chars = max(
        settings.word_batch.max_chars_per_batch,
        int(
            st.session_state.get(
                "word_split_chars_input",
                settings.word_batch.split_paragraph_chars,
            )
        ),
    )
    settings.word_batch.strict_retry_attempts = int(
        st.session_state.get(
            "word_retry_attempts_input",
            settings.word_batch.strict_retry_attempts,
        )
    )
    settings.word_review.highlight_unresolved = bool(
        st.session_state.get(
            _WORD_HIGHLIGHT_ENABLED_KEY,
            settings.word_review.highlight_unresolved,
        )
    )
    settings.word_review.highlight_color = _normalize_word_highlight_color(
        st.session_state.get(
            _WORD_HIGHLIGHT_COLOR_KEY,
            settings.word_review.highlight_color,
        )
    )
    return settings


def _render_word_output_card(settings: AppSettings, phase: str) -> AppSettings:
    output_card = render_setting_card(
        st,
        key="word-inspector-output",
        title="输出位置",
        density="compact",
    )
    with output_card:
        output_mode = st.radio(
            "输出位置选择",
            options=["源目录内", "自定义目录"],
            index=1 if settings.output.use_custom_output_dir else 0,
            horizontal=True,
            label_visibility="collapsed",
            disabled=phase == "running",
            key="word_output_mode_radio",
        )
        settings.output.use_custom_output_dir = output_mode == "自定义目录"

        if settings.output.use_custom_output_dir:
            custom_output_field = render_field_group(
                output_card,
                key="word-custom-output",
                label="自定义输出目录",
            )
            with custom_output_field:
                custom_dir = st.text_input(
                    "自定义输出目录",
                    value=settings.output.custom_output_dir,
                    placeholder="输入输出目录绝对路径",
                    label_visibility="collapsed",
                    disabled=phase == "running",
                    key="word_custom_output_dir_input",
                )
            settings.output.custom_output_dir = custom_dir.strip().strip('"')

            custom_output_error = get_custom_output_dir_error(settings.output.custom_output_dir)
            custom_output_root = resolve_custom_output_dir(settings.output.custom_output_dir)
            if custom_output_error is not None:
                st.error(custom_output_error)
                if st.button("使用默认位置", key="word_use_default_output", use_container_width=True):
                    settings.output.use_custom_output_dir = False
                    st.session_state["word_output_mode_radio"] = "源目录内"
                    st.rerun()
            elif custom_output_dir_will_be_created(settings.output.custom_output_dir):
                st.info(f"目录将在执行时自动创建：{custom_output_root}")
            else:
                st.success("自定义输出目录可用。")
        else:
            settings.output.custom_output_dir = ""

    return settings


def _render_word_inspector(settings: AppSettings, phase: str) -> AppSettings:
    params_card = render_setting_card(
        st,
        key="word-inspector-settings",
        title="任务参数",
        density="compact",
    )
    with params_card:
        target_lang_field = render_field_group(
            params_card,
            key="word-target-lang",
            label="目标语言",
        )
        with target_lang_field:
            render_target_lang_selectbox(
                settings,
                state_prefix="word_translate",
                label="目标语言",
                disabled=phase == "running",
                include_optional_target_langs=True,
            )

        if _get_selected_target_lang(settings) == "zh":
            source_lang_field = render_field_group(
                params_card,
                key="word-source-lang",
                label="源语言",
            )
            with source_lang_field:
                source_lang_options = ["", *get_source_lang_codes()]
                selected_source_lang = _get_selected_source_lang()
                if st.session_state.get(_SOURCE_LANG_SELECTBOX_KEY) not in source_lang_options:
                    st.session_state[_SOURCE_LANG_SELECTBOX_KEY] = selected_source_lang
                chosen_source_lang = st.selectbox(
                    "源语言",
                    options=source_lang_options,
                    index=source_lang_options.index(
                        st.session_state.get(_SOURCE_LANG_SELECTBOX_KEY, "")
                    ),
                    format_func=lambda option: (
                        "请选择源语言" if not option else get_source_lang_display(option)
                    ),
                    label_visibility="collapsed",
                    disabled=phase == "running",
                    key=_SOURCE_LANG_SELECTBOX_KEY,
                )
                if chosen_source_lang and is_supported_source_lang(chosen_source_lang):
                    st.session_state[_SOURCE_LANG_SELECTED] = chosen_source_lang
                    settings.source_lang = chosen_source_lang
                else:
                    st.session_state[_SOURCE_LANG_SELECTED] = ""

        batch_section = st.container(key="word-batch-strategy")
        with batch_section:
            st.markdown(
                '<div class="word-batch-section-title">高级批次策略</div>',
                unsafe_allow_html=True,
            )
            batch_col1, batch_col2 = st.columns(2, gap="small")
            with batch_col1:
                paragraphs_field = render_field_group(
                    batch_col1,
                    key="word-batch-paragraphs",
                    label="每批最多段落",
                    hint=f"{WORD_BATCH_PARAGRAPHS_MIN} - {WORD_BATCH_PARAGRAPHS_MAX}",
                )
                with paragraphs_field:
                    st.number_input(
                        "每批最多段落",
                        min_value=WORD_BATCH_PARAGRAPHS_MIN,
                        max_value=WORD_BATCH_PARAGRAPHS_MAX,
                        step=1,
                        format="%d",
                        disabled=phase == "running",
                        label_visibility="collapsed",
                        key="word_batch_paragraphs_input",
                    )
            with batch_col2:
                chars_field = render_field_group(
                    batch_col2,
                    key="word-batch-chars",
                    label="每批字符上限",
                    hint=f"{WORD_BATCH_CHARS_MIN} - {WORD_BATCH_CHARS_MAX}",
                )
                with chars_field:
                    st.number_input(
                        "每批字符上限",
                        min_value=WORD_BATCH_CHARS_MIN,
                        max_value=WORD_BATCH_CHARS_MAX,
                        step=100,
                        format="%d",
                        disabled=phase == "running",
                        label_visibility="collapsed",
                        key="word_batch_chars_input",
                    )
            batch_col3, batch_col4 = st.columns(2, gap="small")
            with batch_col3:
                split_field = render_field_group(
                    batch_col3,
                    key="word-split-chars",
                    label="长段拆分阈值",
                    hint=f"{WORD_BATCH_SPLIT_CHARS_MIN} - {WORD_BATCH_SPLIT_CHARS_MAX}",
                )
                with split_field:
                    st.number_input(
                        "长段拆分阈值",
                        min_value=WORD_BATCH_SPLIT_CHARS_MIN,
                        max_value=WORD_BATCH_SPLIT_CHARS_MAX,
                        step=500,
                        format="%d",
                        disabled=phase == "running",
                        label_visibility="collapsed",
                        key="word_split_chars_input",
                    )
            with batch_col4:
                retry_field = render_field_group(
                    batch_col4,
                    key="word-retry-attempts",
                    label="失败重试次数",
                    hint=f"{WORD_STRICT_RETRY_ATTEMPTS_MIN} - {WORD_STRICT_RETRY_ATTEMPTS_MAX}",
                )
                with retry_field:
                    st.number_input(
                        "失败重试次数",
                        min_value=WORD_STRICT_RETRY_ATTEMPTS_MIN,
                        max_value=WORD_STRICT_RETRY_ATTEMPTS_MAX,
                        step=1,
                        format="%d",
                        disabled=phase == "running",
                        label_visibility="collapsed",
                        key="word_retry_attempts_input",
                    )

            highlight_enabled = render_checkbox_tooltip_row(
                params_card,
                "高亮需复核内容",
                "高亮需人工复核内容",
                "在输出 Word 中标记重试后仍未获得有效译文的位置，便于快速定位。",
                [
                    "默认关闭，只影响新生成的双语 Word 文件。",
                    "只标记最终仍需人工复核的原文段落或表格单元格。",
                    "若检测到原位置已有底纹或文本高亮，将跳过，不覆盖原样式。",
                ],
                key=_WORD_HIGHLIGHT_ENABLED_KEY,
                checkbox_label="高亮需复核内容开关",
                disabled=phase == "running",
            )
            if highlight_enabled:
                highlight_color_field = render_field_group(
                    params_card,
                    key="word-review-highlight-color",
                    label="复核高亮颜色",
                )
                with highlight_color_field:
                    st.selectbox(
                        "复核高亮颜色",
                        options=list(_WORD_HIGHLIGHT_COLORS.values()),
                        format_func=_format_word_highlight_color,
                        disabled=phase == "running",
                        label_visibility="collapsed",
                        key=_WORD_HIGHLIGHT_COLOR_KEY,
                    )

        render_checkbox_tooltip_row(
            params_card,
            "启用任务日志",
            "启用任务日志",
            "记录每次 Word 翻译任务的关键过程与异常信息，便于定位问题。",
            [
                "开启后会将任务运行日志持续写入本地文件。",
                "日志路径沿用当前项目配置。",
                "日志不会写入 GitHub 仓库。",
            ],
            key="word_cb_task_log",
            checkbox_label="启用任务日志开关",
            disabled=phase == "running",
        )

    settings.output.enable_task_log = st.session_state.get("word_cb_task_log", False)
    return settings


def render_page(settings: AppSettings) -> AppSettings:
    _init(settings)
    _sync_word_target_lang_from_widget(settings)
    _sync_word_source_lang_from_widget(settings)
    render_main_tooltip_support()
    phase = st.session_state[_PHASE]
    settings = _sync_word_inspector_state(settings)

    _render_word_page_header(settings, phase)

    command_shell = st.container(key="word-command-shell")
    with command_shell:
        st.markdown('<div class="toolbar-headline">源路径</div>', unsafe_allow_html=True)
        col_path, col_browse, col_scan = st.columns(
            [5.8, 1.15, 1.05],
            gap="small",
            vertical_alignment="bottom",
        )
        with col_path:
            pending_source_input = st.session_state.pop(_PENDING_SOURCE_INPUT_VALUE, None)
            if pending_source_input is None:
                pending_source_input = st.session_state.get(_PENDING_SCAN_PATH)
            if pending_source_input is not None:
                st.session_state[_FOLDER_INPUT_KEY] = pending_source_input
            source_path_field = render_field_group(
                col_path,
                key="word-source-path",
                variant="single-layer",
            )
            with source_path_field:
                folder = st.text_input(
                    "源路径（文件夹或单文件）",
                    placeholder="可手动输入文件夹或 Word 文件绝对路径，也可点击“浏览”选择",
                    label_visibility="collapsed",
                    disabled=phase == "running",
                    key=_FOLDER_INPUT_KEY,
                )
        with col_browse:
            if st.button(
                "浏览",
                use_container_width=True,
                disabled=phase == "running",
                key="word_browse_button",
            ) and phase != "running":
                st.session_state[_BROWSE_DIALOG_OPEN] = True
                st.session_state[_BROWSE_ERROR] = ""
        with col_scan:
            scan_clicked = st.button(
                "扫描",
                type="secondary",
                use_container_width=True,
                disabled=phase == "running",
                key="word_scan_button",
            )

    if st.session_state.get(_BROWSE_DIALOG_OPEN) and phase != "running":
        _render_source_browse_dialog(settings)

    pending_scan_path = st.session_state.pop(_PENDING_SCAN_PATH, None)
    if pending_scan_path and phase != "running":
        if _scan_source_path(pending_scan_path, settings):
            st.rerun()

    if scan_clicked and phase != "running":
        if _scan_source_path(folder, settings):
            st.rerun()

    main_col, side_col = st.columns([2.16, 0.94], gap="medium")
    with main_col:
        if phase == "idle":
            _render_idle_content()
        elif phase == "running":
            _render_running_fragment()
        elif phase == "done":
            _render_done_content()
        elif phase == "stopped":
            _render_stopped_content()
        elif phase == "error":
            _render_error_content()

    with side_col:
        _render_action_buttons(settings, phase)
        settings = _render_word_output_card(settings, phase)
        settings = _render_word_inspector(settings, phase)

    return settings


def _render_idle_content() -> None:
    files = st.session_state[_FILES]
    workspace = st.container(key="word-workspace-shell")
    with workspace:
        st.markdown(
            '<div class="workspace-shell__header">'
            '  <div><div class="workspace-shell__title">任务清单</div></div>'
            '</div>',
            unsafe_allow_html=True,
        )
        if not files:
            st.markdown(
                '<div class="content-placeholder content-placeholder--workbench">可手动输入文件夹或单个 Word 文件路径后点击“扫描”，也可点击“浏览”选择并自动扫描，即可在此查看可处理文件列表。</div>',
                unsafe_allow_html=True,
            )
            return
        _render_word_scope_summary(files)
        _render_word_file_table(files)


def _render_word_scope_summary(files: list) -> None:
    selected_n = sum(
        1 for index in range(len(files))
        if st.session_state.get(f"word_file_check_{index}", True)
    )
    paragraph_count = sum(file_item.paragraph_count for file_item in files)
    table_count = sum(file_item.table_count for file_item in files)
    st.markdown(
        '<div class="kpi-strip">'
        f'  <div class="kpi-tile"><span class="kpi-tile__label">已扫描文件</span><span class="kpi-tile__value">{len(files)}</span></div>'
        f'  <div class="kpi-tile"><span class="kpi-tile__label">已选任务</span><span class="kpi-tile__value">{selected_n}</span></div>'
        f'  <div class="kpi-tile"><span class="kpi-tile__label">正文段落</span><span class="kpi-tile__value">{paragraph_count}</span></div>'
        f'  <div class="kpi-tile"><span class="kpi-tile__label">表格数</span><span class="kpi-tile__value">{table_count}</span></div>'
        '</div>',
        unsafe_allow_html=True,
    )


def _render_word_file_table(file_items: list) -> None:
    if not file_items:
        st.info("未发现 Word 文件，请检查文件夹路径。")
        return

    st.session_state["_word_file_count"] = len(file_items)

    def _sync_all() -> None:
        value = st.session_state.get("word_file_check_all", True)
        for index in range(st.session_state.get("_word_file_count", 0)):
            st.session_state[f"word_file_check_{index}"] = value

    widths = [0.7, 4.3, 1.1, 1.0, 1.0]
    st.markdown('<span class="file-table-header-anchor"></span>', unsafe_allow_html=True)
    hcols = st.columns(widths, vertical_alignment="center")
    hcols[0].checkbox(
        "全选文件",
        key="word_file_check_all",
        on_change=_sync_all,
        label_visibility="collapsed",
    )
    hcols[1].markdown('<div class="file-table-head">文件名</div>', unsafe_allow_html=True)
    hcols[2].markdown('<div class="file-table-center"><span class="file-table-head">大小</span></div>', unsafe_allow_html=True)
    hcols[3].markdown('<div class="file-table-center"><span class="file-table-head">段落</span></div>', unsafe_allow_html=True)
    hcols[4].markdown('<div class="file-table-center"><span class="file-table-head">表格</span></div>', unsafe_allow_html=True)

    with st.container():
        for index, file_item in enumerate(file_items):
            st.markdown('<span class="file-table-row-anchor"></span>', unsafe_allow_html=True)
            rcols = st.columns(widths, vertical_alignment="center")
            rcols[0].checkbox(
                f"选择 Word 文件 {index + 1}",
                key=f"word_file_check_{index}",
                label_visibility="collapsed",
            )
            rcols[1].markdown(
                f'<div class="file-cell file-cell--name" title="{html.escape(file_item.name, quote=True)}">{html.escape(file_item.name)}</div>',
                unsafe_allow_html=True,
            )
            rcols[2].markdown(
                f'<div class="file-table-center"><span class="file-cell file-cell--metric">{file_item.size_kb:.1f} KB</span></div>',
                unsafe_allow_html=True,
            )
            rcols[3].markdown(
                f'<div class="file-table-center"><span class="file-cell file-cell--metric">{file_item.paragraph_count}</span></div>',
                unsafe_allow_html=True,
            )
            rcols[4].markdown(
                f'<div class="file-table-center"><span class="file-cell file-cell--metric">{file_item.table_count}</span></div>',
                unsafe_allow_html=True,
            )


def _get_word_phase_layout(phase_total: int) -> tuple[dict[int, float], dict[int, float]]:
    phase_weights = {1: 0.12, 2: 0.70, 3: 0.18}
    phase_offsets: dict[int, float] = {}
    cumulative = 0.0
    for phase_idx in range(1, phase_total + 1):
        phase_offsets[phase_idx] = cumulative
        cumulative += phase_weights.get(phase_idx, 0.0)
    return phase_weights, phase_offsets


def _calc_word_overall_progress(progress: ProgressMsg) -> float:
    phase_weights, phase_offsets = _get_word_phase_layout(progress.phase_total)
    step_pct = progress.step_done / max(progress.step_total, 1)
    overall = phase_offsets.get(progress.phase_index, 0.0) + step_pct * phase_weights.get(progress.phase_index, 0.0)
    return min(overall, 1.0)


def _drain_word_runner_messages(runner: WordTaskRunner) -> None:
    while True:
        msg = runner.get_message(timeout=0.0)
        if msg is None:
            break
        if isinstance(msg, LogMsg):
            st.session_state[_LOGS].append(
                {"level": msg.level, "message": msg.message, "ts": msg.ts}
            )
        elif isinstance(msg, ProgressMsg):
            st.session_state[_PROGRESS] = msg
        elif isinstance(msg, StatusMsg):
            st.session_state[_STATUS] = msg.phase_desc
        elif isinstance(msg, DoneMsg):
            st.session_state[_DONE] = msg
            st.session_state[_RUNNER] = None
            st.session_state[_PHASE] = "done"
            return
        elif isinstance(msg, ErrorMsg):
            st.session_state[_LOGS].append({"level": "ERROR", "message": msg.message, "ts": ""})
            st.session_state[_RUNNER] = None
            st.session_state[_PHASE] = "error"
            return
        elif isinstance(msg, StoppedMsg):
            st.session_state[_LOGS].append({"level": "WARN", "message": msg.message, "ts": ""})
            st.session_state[_RUNNER] = None
            st.session_state[_STOP_MESSAGE] = msg.message
            st.session_state[_PHASE] = "stopped"
            return


def _render_running_status_block() -> bool:
    runner: WordTaskRunner | None = st.session_state.get(_RUNNER)
    if runner is None:
        st.error("任务状态异常：运行器不存在。")
        return False

    _drain_word_runner_messages(runner)
    if st.session_state.get(_PHASE) != "running":
        return False

    progress: ProgressMsg | None = st.session_state.get(_PROGRESS)
    status_desc = (st.session_state.get(_STATUS) or "").replace("状态：", "", 1).strip()
    stop_hint = runner.stop_requested()

    st.markdown(
        '<div class="workspace-shell__header workspace-shell__header--running">'
        '  <div>'
        '    <div class="workspace-shell__title">执行监控</div>'
        '    <div class="workspace-shell__caption">当前任务会持续刷新阶段进度与运行日志。</div>'
        '  </div>'
        '</div>',
        unsafe_allow_html=True,
    )

    if progress:
        overall = _calc_word_overall_progress(progress)
        st.markdown(
            '<div class="status-kpi-row">'
            f'  <div class="status-kpi-tile"><span>阶段</span><strong>{progress.phase_index} / {progress.phase_total}</strong></div>'
            f'  <div class="status-kpi-tile"><span>当前步骤</span><strong>{html.escape(progress.phase_name)}</strong></div>'
            f'  <div class="status-kpi-tile"><span>步数</span><strong>{progress.step_done} / {progress.step_total}</strong></div>'
            '</div>',
            unsafe_allow_html=True,
        )
        st.progress(overall, text=f"总进度 {int(overall * 100)}%")
        if status_desc:
            st.markdown(
                f'<div class="subtle-note subtle-note--status">{html.escape(status_desc)}</div>',
                unsafe_allow_html=True,
            )
        if stop_hint:
            st.warning("正在停止任务，等待当前批次安全结束...")
    elif status_desc:
        st.info(status_desc)
        if stop_hint:
            st.warning("已收到停止请求，等待后台任务退出...")
    else:
        st.info("初始化中，请稍候...")
        if stop_hint:
            st.warning("已收到停止请求，等待后台任务退出...")

    render_log_area(st.session_state[_LOGS], container_id="word-translate-log-container")
    return True


def _render_running_fragment() -> None:
    _word_running_fragment()


@st.fragment(run_every=0.3)
def _word_running_fragment() -> None:
    phase = st.session_state.get(_PHASE, "idle")
    if phase != "running":
        st.rerun()
        return
    if not _render_running_status_block():
        st.rerun()


def _render_done_content() -> None:
    done: DoneMsg = st.session_state[_DONE]
    success_n = sum(1 for item in done.file_results if item.get("success"))
    failure_n = len(done.file_results) - success_n
    issues = list(done.issues or [])
    resolved_issues = [issue for issue in issues if issue.get("severity") == "resolved"]
    review_issues = [issue for issue in issues if issue.get("severity") != "resolved"]

    workspace = st.container(key="word-done-shell")
    with workspace:
        st.markdown(
            '<div class="workspace-shell__header">'
            '  <div>'
            '    <div class="workspace-shell__title">任务结果</div>'
            '    <div class="workspace-shell__caption">Word 翻译任务已经完成，下面保留本次执行的关键结果与输出目录。</div>'
            '  </div>'
            '</div>',
            unsafe_allow_html=True,
        )
        if failure_n == 0 and not issues:
            st.success("翻译成功。")
        elif issues and failure_n:
            st.warning("翻译完成，但有内容需复核，且有文件未成功。")
        elif issues:
            st.warning("翻译完成，但有内容需复核。")
        else:
            st.warning("翻译完成，但有文件未成功。")
        st.markdown(
            '<div class="kpi-strip kpi-strip--done">'
            f'  <div class="kpi-tile"><span class="kpi-tile__label">成功文件</span><span class="kpi-tile__value">{success_n}</span></div>'
            f'  <div class="kpi-tile"><span class="kpi-tile__label">失败文件</span><span class="kpi-tile__value">{failure_n}</span></div>'
            f'  <div class="kpi-tile kpi-tile--wide"><span class="kpi-tile__label">输出目录</span><span class="kpi-tile__value">{html.escape(done.output_dir)}</span></div>'
            '</div>',
            unsafe_allow_html=True,
        )
        render_stat_cards(
            done.elapsed_sec,
            len(done.file_results),
            done.tm_hit_count,
            done.api_call_count,
        )
        st.markdown(f'<div class="mono-block">{html.escape(done.output_dir)}</div>', unsafe_allow_html=True)
        if done.report_path:
            st.markdown(
                f'<div class="subtle-note">质量报告：{html.escape(done.report_path)}</div>',
                unsafe_allow_html=True,
            )

        _render_word_quality_issues(review_issues, resolved_issues)

        for result in done.file_results:
            modifier = " translate-result-row--error" if not result.get("success") else ""
            detail_html = ""
            issue_count = len(result.get("issues") or [])
            if result.get("error"):
                detail_html = f'<div class="translate-result-row__detail">{html.escape(result["error"])}</div>'
            elif issue_count:
                detail_html = (
                    f'<div class="translate-result-row__detail translate-result-row__detail--muted">'
                    f'本文件有 {issue_count} 条质量提示，详情见上方复核清单。</div>'
                )
            st.markdown(
                f'<div class="translate-result-row{modifier}">'
                f'  <div class="translate-result-row__title">{html.escape(result["name"])}</div>'
                f'  <div class="translate-result-row__meta">{_format_word_result_status(result, issue_count)}</div>'
                f'  {detail_html}'
                '</div>',
                unsafe_allow_html=True,
            )


def _render_word_quality_issues(
    review_issues: list[dict],
    resolved_issues: list[dict],
) -> None:
    if not review_issues and not resolved_issues:
        return

    st.markdown(
        '<div class="word-quality-panel">'
        '  <div class="word-quality-panel__title">质量提示</div>'
        '  <div class="word-quality-panel__caption">以下段落在翻译过程中触发了质量校验或自动重试，建议按位置回看输出文件。</div>'
        '</div>',
        unsafe_allow_html=True,
    )
    _render_word_quality_group("需人工复核", review_issues)
    _render_word_quality_group("已自动处理", resolved_issues)


def _render_word_quality_group(title: str, issues: list[dict]) -> None:
    if not issues:
        return
    st.markdown(
        f'<div class="word-quality-group-title">{html.escape(title)}（{len(issues)}）</div>',
        unsafe_allow_html=True,
    )
    for issue in issues:
        severity = str(issue.get("severity") or "")
        modifier = " word-quality-issue--review" if severity == "needs_review" else " word-quality-issue--resolved"
        fragments = issue.get("review_fragments") or []
        fragment_html = ""
        if fragments:
            fragment_text = "、".join(str(fragment) for fragment in fragments)
            fragment_html = (
                f'  <div class="word-quality-issue__detail">'
                f'    <span>问题片段：{html.escape(fragment_text)}</span>'
                f'  </div>'
            )
        st.markdown(
            f'<div class="word-quality-issue{modifier}">'
            f'  <div class="word-quality-issue__title">{html.escape(issue.get("snippet") or "未记录段落内容")}</div>'
            f'  <div class="word-quality-issue__meta">{html.escape(issue.get("file") or "未知文件")}</div>'
            f'  <div class="word-quality-issue__detail">'
            f'    <span>{html.escape(issue.get("section_path") or "正文")}</span>'
            f'    <span>{html.escape(issue.get("location_label") or "未知位置")}</span>'
            f'  </div>'
            f'  <div class="word-quality-issue__detail">'
            f'    <span>问题：{html.escape(issue.get("problem") or "未记录")}</span>'
            f'    <span>处理：{html.escape(issue.get("status") or "未记录")}</span>'
            f'  </div>'
            f'{fragment_html}'
            '</div>',
            unsafe_allow_html=True,
        )


def _format_word_result_status(result: dict, issue_count: int) -> str:
    if not result.get("success"):
        return "失败"
    if issue_count:
        return f"成功 / {issue_count} 条提示"
    return "成功"


def _render_error_content() -> None:
    workspace = st.container(key="word-error-shell")
    with workspace:
        st.markdown(
            '<div class="workspace-shell__header">'
            '  <div>'
            '    <div class="workspace-shell__title">任务异常</div>'
            '    <div class="workspace-shell__caption">后台执行中断，请结合日志定位失败位置。</div>'
            '  </div>'
            '</div>',
            unsafe_allow_html=True,
        )
        st.error("任务执行出错，请查看日志。")
        render_log_area(st.session_state[_LOGS], container_id="word-translate-log-container")


def _render_stopped_content() -> None:
    workspace = st.container(key="word-stopped-shell")
    with workspace:
        st.markdown(
            '<div class="workspace-shell__header">'
            '  <div>'
            '    <div class="workspace-shell__title">任务已中止</div>'
            '    <div class="workspace-shell__caption">翻译流程已安全停止，日志仍保留以便回看中断位置。</div>'
            '  </div>'
            '</div>',
            unsafe_allow_html=True,
        )
        st.warning(st.session_state.get(_STOP_MESSAGE) or "任务已中止。")
        render_log_area(st.session_state[_LOGS], container_id="word-translate-log-container")


def _start_word_translation_task(
    selected_files,
    settings,
    source_root,
    target_lang,
    source_lang="zh",
) -> None:
    settings.target_lang = target_lang
    settings.source_lang = str(source_lang or get_default_source_lang()).strip() or get_default_source_lang()
    runner = WordTaskRunner(
        selected_files,
        settings,
        source_root=source_root,
        source_lang=settings.source_lang,
    )
    runner.start()
    st.session_state[_RUNNER] = runner
    st.session_state[_LOGS] = []
    st.session_state[_PROGRESS] = None
    st.session_state[_STATUS] = None
    st.session_state[_PHASE] = "running"
    st.session_state[_STOP_PENDING] = False
    st.session_state[_STOP_MESSAGE] = ""
    st.rerun()


def _render_action_buttons(settings: AppSettings, phase: str) -> None:
    action_shell = st.container(key="word-action-shell")
    with action_shell:
        st.markdown(
            '<div class="workspace-shell__header workspace-shell__header--compact">'
            '  <div><div class="workspace-shell__title">执行操作</div></div>'
            '</div>',
            unsafe_allow_html=True,
        )

        selected_target_lang = _get_selected_target_lang(settings)
        selected_source_lang = (
            _get_selected_source_lang()
            if selected_target_lang == "zh"
            else get_default_source_lang()
        )
        lang_label = (
            get_target_lang_display(
                selected_target_lang,
                settings.custom_target_langs,
                include_optional=True,
            )
            if selected_target_lang
            else "未选择"
        )

        if phase == "idle":
            files = st.session_state[_FILES]
            selected = _get_selected_files()
            output_dir_valid = True
            if settings.output.use_custom_output_dir:
                output_dir_valid = get_custom_output_dir_error(settings.output.custom_output_dir) is None

            st.markdown(
                '<div class="ui-field-note ui-field-note--block">'
                f'当前目标语言：{html.escape(lang_label)}；可执行文件：{len(selected)} / {len(files)}'
                '</div>',
                unsafe_allow_html=True,
            )
            if st.button(
                f"开始翻译（{lang_label}）",
                type="primary",
                use_container_width=True,
                disabled=(
                    (not selected)
                    or (not output_dir_valid)
                    or (selected_target_lang == "zh" and not selected_source_lang)
                ),
                key="word_start_button",
            ) and selected and output_dir_valid:
                if not is_supported_target_lang(
                    selected_target_lang,
                    settings.custom_target_langs,
                    include_optional=True,
                ):
                    st.error("请先选择目标语言后再开始翻译。")
                    return
                if selected_target_lang == "zh" and not selected_source_lang:
                    st.error("目标语言为中文时，请先选择源语言后再开始翻译。")
                    return
                if selected_source_lang == selected_target_lang:
                    st.error("源语言与目标语言不能相同，请重新选择后再开始翻译。")
                    return

                source_root = (st.session_state.get(_SOURCE_ROOT) or settings.last_source_folder or "").strip().strip('"')
                _start_word_translation_task(
                    selected,
                    settings,
                    source_root if source_root else None,
                    selected_target_lang,
                    source_lang=selected_source_lang,
                )

        elif phase == "running":
            runner: WordTaskRunner | None = st.session_state.get(_RUNNER)
            st.markdown(
                '<div class="ui-field-note ui-field-note--block">运行期间会锁定参数；终止操作采用二次确认，防止误触。</div>',
                unsafe_allow_html=True,
            )
            if runner is None:
                st.button("任务状态异常", type="primary", use_container_width=True, disabled=True, key="word_runner_missing")
                return

            if st.session_state.get(_STOP_PENDING):
                st.warning("再次点击后将请求停止当前任务。")
                col_confirm, col_cancel = st.columns(2, gap="small")
                with col_confirm:
                    if st.button("确认终止", type="primary", use_container_width=True, key="word_confirm_stop"):
                        runner.stop()
                        st.session_state[_STOP_PENDING] = False
                        st.session_state[_STATUS] = "状态：正在停止任务，请等待当前批次结束..."
                        st.rerun()
                with col_cancel:
                    if st.button("继续翻译", use_container_width=True, key="word_keep_running"):
                        st.session_state[_STOP_PENDING] = False
                        st.rerun()
            elif runner.stop_requested():
                st.button("正在停止...", type="primary", use_container_width=True, disabled=True, key="word_stopping")
                st.markdown(
                    '<div class="ui-field-note ui-field-note--block">等待当前批次安全结束后会自动切回结果状态。</div>',
                    unsafe_allow_html=True,
                )
            else:
                if st.button("终止翻译", type="primary", use_container_width=True, key="word_stop_button"):
                    st.session_state[_STOP_PENDING] = True
                    st.rerun()
                st.markdown(
                    '<div class="ui-field-note ui-field-note--block">点击后需再次确认，避免误触。</div>',
                    unsafe_allow_html=True,
                )

        elif phase in ("done", "error", "stopped"):
            if st.button("返回并开始新任务", type="primary", use_container_width=True, key="word_reset_button"):
                st.session_state[_PHASE] = "idle"
                st.session_state[_FILES] = []
                st.session_state[_DONE] = None
                st.session_state[_RUNNER] = None
                st.session_state[_STOP_PENDING] = False
                st.session_state[_STOP_MESSAGE] = ""
                st.rerun()
