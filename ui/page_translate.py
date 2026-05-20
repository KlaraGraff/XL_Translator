"""
翻译任务主页面。
布局：顶部命令条 + 左侧工作面 + 右侧任务检视器。

状态机：idle → running → done | stopped | error
"""

import html
from pathlib import Path

import streamlit as st

from core.api_config_check import check_translation_api_config
from core.bilingual_writer import (
    custom_output_dir_will_be_created,
    get_custom_output_dir_error,
    resolve_custom_output_dir,
)
from core.diagnostics import archive_task_diagnostics
from core.file_scanner import is_supported_excel_file, scan_path
from core.language_registry import (
    get_default_source_lang,
    get_default_target_lang,
    get_source_lang_codes,
    get_source_lang_display,
    get_target_lang_display,
    is_supported_source_lang,
    is_supported_target_lang,
)
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
from settings import AppSettings
from ui.components import (
    build_tooltip_label_html,
    render_checkbox_tooltip_row,
    render_field_group,
    render_file_table,
    render_log_area,
    render_main_tooltip_support,
    render_radio_option_tooltips,
    render_setting_card,
    render_stat_cards,
)
from ui.native_dialogs import pick_excel_file, pick_folder
from ui.diagnostics_panel import (
    render_current_diagnostic_download,
    render_history_diagnostic_export,
)
from ui.target_language_selector import (
    render_target_lang_selectbox,
    was_target_lang_manually_selected,
)

# ── session_state 键名 ────────────────────────────────────
_PHASE = "translate_phase"
_RUNNER = "translate_runner"
_LOGS = "translate_logs"
_PROGRESS = "translate_progress"
_STATUS = "translate_status"
_DONE = "translate_done"
_FILES = "translate_file_items"
_STOP_PENDING = "translate_stop_pending"
_STOP_MESSAGE = "translate_stop_message"
_SOURCE_ROOT = "translate_source_root"
_LANG_SELECTED = "translate_selected_target_lang"
_LANG_SELECTBOX_KEY = "translate_target_lang_select"
_SOURCE_LANG_SELECTED = "translate_selected_source_lang"
_SOURCE_LANG_SELECTBOX_KEY = "translate_source_lang_select"
_BROWSE_DIALOG_OPEN = "translate_browse_dialog_open"
_PENDING_SCAN_PATH = "translate_pending_scan_path"
_PENDING_SOURCE_INPUT_VALUE = "translate_pending_source_input_value"
_BROWSE_ERROR = "translate_browse_error"
_FOLDER_INPUT_KEY = "folder_input"
_TASK_ID = "translate_task_id"
_DIAGNOSTIC_RECORD_DIR = "translate_diagnostic_record_dir"
_DIAGNOSTIC_ERROR = "translate_diagnostic_error"


def _init(settings: AppSettings | None = None) -> None:
    for k, v in [
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
        (_TASK_ID, ""),
        (_DIAGNOSTIC_RECORD_DIR, ""),
        (_DIAGNOSTIC_ERROR, ""),
    ]:
        if k not in st.session_state:
            st.session_state[k] = v
    if not st.session_state.get(_SOURCE_ROOT):
        st.session_state[_SOURCE_ROOT] = (settings.last_source_folder if settings else "")
    if _FOLDER_INPUT_KEY not in st.session_state:
        st.session_state[_FOLDER_INPUT_KEY] = (settings.last_source_folder if settings else "")
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


def _get_selected_files() -> list:
    files = st.session_state.get(_FILES, [])
    return [
        f for i, f in enumerate(files)
        if st.session_state.get(f"file_check_{i}", True)
    ]


def _get_selected_target_label(settings: AppSettings) -> str:
    selected_target_lang = (
        st.session_state.get(_LANG_SELECTBOX_KEY)
        or st.session_state.get(_LANG_SELECTED)
        or settings.target_lang
    )
    if not is_supported_target_lang(
        selected_target_lang,
        settings.custom_target_langs,
        include_optional=True,
    ):
        return "未选择"
    selected_label = get_target_lang_display(
        selected_target_lang,
        settings.custom_target_langs,
        include_optional=True,
    )
    if (
        not was_target_lang_manually_selected("translate")
        and selected_target_lang == get_default_target_lang()
    ):
        return f"{selected_label}（默认）"
    return selected_label


def _sync_translate_target_lang_from_widget(settings: AppSettings) -> None:
    widget_target_lang = st.session_state.get(_LANG_SELECTBOX_KEY)
    if not is_supported_target_lang(
        widget_target_lang,
        settings.custom_target_langs,
        include_optional=True,
    ):
        return
    st.session_state[_LANG_SELECTED] = widget_target_lang
    settings.target_lang = widget_target_lang


def _sync_translate_source_lang_from_widget(settings: AppSettings) -> None:
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


def _build_translate_meta_pill(
    label: str,
    value: str,
    *,
    monospace: bool = False,
    title: str | None = None,
    extra_class: str = "",
) -> str:
    pill_classes = "meta-pill"
    if monospace:
        pill_classes += " meta-pill--mono"
    if extra_class:
        pill_classes += f" {extra_class}"

    title_value = title or value
    title_attr = f' title="{html.escape(title_value, quote=True)}"' if title_value else ""
    return (
        f'<span class="{pill_classes}"{title_attr}>'
        f'  <span class="meta-pill__label">{html.escape(label)}</span>'
        f'  <span class="meta-pill__value">{html.escape(value)}</span>'
        '</span>'
    )


def _render_translate_page_header(settings: AppSettings, phase: str) -> None:
    phase_map = {
        "idle": "待执行",
        "running": "执行中",
        "done": "已完成",
        "error": "异常",
        "stopped": "已中止",
    }
    selected_files = _get_selected_files()
    current_root = (st.session_state.get(_SOURCE_ROOT) or settings.last_source_folder or "").strip()
    target_pill = _build_translate_meta_pill("目标语言", _get_selected_target_label(settings))
    selected_pill = _build_translate_meta_pill("已选文件", f"{len(selected_files)} 个")
    source_value = current_root or "尚未选择"
    source_pill = _build_translate_meta_pill(
        "源路径",
        source_value,
        monospace=True,
        title=current_root or "尚未选择源路径",
        extra_class="meta-pill--path",
    )
    st.markdown(
        '<div class="page-head">'
        '  <div class="page-head__eyebrow">Translate Workspace</div>'
        '  <div class="page-head__title-row">'
        '    <div class="page-head__title-block">'
        '      <div class="page-head__title">翻译任务</div>'
        '    </div>'
        '    <div class="page-head__status-cluster">'
        f'      {target_pill}'
        f'      {selected_pill}'
        f'      {source_pill}'
        f'      <div class="phase-badge">{phase_map.get(phase, "待执行")}</div>'
        '    </div>'
        '  </div>'
        '</div>',
        unsafe_allow_html=True,
    )


def _render_translate_scope_summary(files: list) -> None:
    selected_n = sum(
        1 for i in range(len(files))
        if st.session_state.get(f"file_check_{i}", True)
    )
    total_sheets = sum(len(f.sheets) for f in files)
    xls_n = sum(1 for f in files if f.path.suffix.lower() == ".xls")
    st.markdown(
        '<div class="kpi-strip">'
        f'  <div class="kpi-tile"><span class="kpi-tile__label">已扫描文件</span><span class="kpi-tile__value">{len(files)}</span></div>'
        f'  <div class="kpi-tile"><span class="kpi-tile__label">已选任务</span><span class="kpi-tile__value">{selected_n}</span></div>'
        f'  <div class="kpi-tile"><span class="kpi-tile__label">总分表数</span><span class="kpi-tile__value">{total_sheets}</span></div>'
        f'  <div class="kpi-tile"><span class="kpi-tile__label">.xls 文件</span><span class="kpi-tile__value">{xls_n}</span></div>'
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
    for i in range(len(items)):
        st.session_state[f"file_check_{i}"] = True
    st.session_state["file_check_all"] = True


def _scan_source_path(raw_path: str, settings: AppSettings) -> bool:
    candidate = (raw_path or "").strip().strip('"')
    if not candidate:
        st.error("请输入文件夹或文件路径。")
        return False

    input_path = Path(candidate)
    if not input_path.exists():
        st.error(f"路径不存在：{candidate}")
        return False
    if input_path.is_file() and not is_supported_excel_file(input_path):
        st.error("不支持的文件类型：仅支持 .xlsx / .xls 文件。")
        return False

    normalized_path = str(input_path)
    settings.last_source_folder = normalized_path
    st.session_state["settings"] = settings
    st.session_state[_SOURCE_ROOT] = str(input_path if input_path.is_dir() else input_path.parent)
    with st.spinner("扫描中..."):
        items = scan_path(input_path)
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
        selected_path = (
            pick_folder(current_path)
            if mode == "folder"
            else pick_excel_file(current_path)
        )
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
    st.caption("选择文件夹会递归扫描其中所有 Excel 文件；选择 Excel 文件则只扫描该文件。")

    browse_error = st.session_state.get(_BROWSE_ERROR, "")
    if browse_error:
        st.error(f"无法打开系统选择窗口：{browse_error}")

    col_folder, col_file = st.columns(2, gap="small")
    with col_folder:
        if st.button(
            "选择文件夹",
            use_container_width=True,
            key="translate_browse_folder_button",
        ):
            _pick_source_path("folder", settings)
    with col_file:
        if st.button(
            "选择 Excel 文件",
            type="secondary",
            use_container_width=True,
            key="translate_browse_file_button",
        ):
            _pick_source_path("file", settings)

    if st.button(
        "取消",
        use_container_width=True,
        key="translate_browse_cancel_button",
    ):
        _close_browse_dialog()
        st.rerun()


def _sync_translate_inspector_state(settings: AppSettings) -> AppSettings:
    settings.output.keep_original_sheets = st.session_state.get(
        "cb_keep_original",
        settings.output.keep_original_sheets,
    )
    settings.output.formula_display_value_backfill = st.session_state.get(
        "cb_formula_display_value_backfill",
        settings.output.formula_display_value_backfill,
    )
    settings.output.enable_excel_autofit = st.session_state.get(
        "cb_excel_autofit",
        settings.output.enable_excel_autofit,
    )
    settings.output.lock_row_height = st.session_state.get(
        "cb_lock_row_height",
        settings.output.lock_row_height,
    )
    settings.output.enable_task_log = st.session_state.get(
        "cb_task_log",
        settings.output.enable_task_log,
    )

    output_mode = st.session_state.get(
        "output_mode_radio",
        "自定义目录" if settings.output.use_custom_output_dir else "源目录内",
    )
    settings.output.use_custom_output_dir = (output_mode == "自定义目录")
    if settings.output.use_custom_output_dir:
        settings.output.custom_output_dir = (
            st.session_state.get("custom_output_dir_input", settings.output.custom_output_dir)
            .strip()
            .strip('"')
        )
    else:
        settings.output.custom_output_dir = ""

    return settings


def _render_translate_inspector(settings: AppSettings, phase: str) -> AppSettings:
    params_card = render_setting_card(
        st,
        key="translate-inspector-settings",
        title="任务参数",
        density="compact",
    )
    with params_card:
        target_lang_field = render_field_group(
            params_card,
            key="translate-target-lang",
            label="目标语言",
        )
        with target_lang_field:
            # KNOWN-ISSUE-UI-001:
            # The target-language field uses a dedicated selectbox component whose
            # vertical alignment is still tracked in docs/KNOWN_ISSUES.md.
            render_target_lang_selectbox(
                settings,
                state_prefix="translate",
                label="目标语言",
                disabled=(phase == "running"),
                include_optional_target_langs=True,
            )

        if _get_selected_target_lang(settings) == "zh":
            source_lang_field = render_field_group(
                params_card,
                key="translate-source-lang",
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
                        "请选择源语言"
                        if not option
                        else get_source_lang_display(option)
                    ),
                    label_visibility="collapsed",
                    disabled=(phase == "running"),
                    key=_SOURCE_LANG_SELECTBOX_KEY,
                )
                if chosen_source_lang and is_supported_source_lang(chosen_source_lang):
                    st.session_state[_SOURCE_LANG_SELECTED] = chosen_source_lang
                    settings.source_lang = chosen_source_lang
                else:
                    st.session_state[_SOURCE_LANG_SELECTED] = ""

        def _on_excel_autofit_change() -> None:
            if st.session_state.get("cb_excel_autofit", False):
                st.session_state["cb_lock_row_height"] = False

        def _on_lock_row_height_change() -> None:
            if st.session_state.get("cb_lock_row_height", False):
                st.session_state["cb_excel_autofit"] = False

        settings.output.keep_original_sheets = render_checkbox_tooltip_row(
            params_card,
            "保留原始表格",
            "保留原始表格",
            "控制输出文件是否同时保留原始中文工作表。",
            [
                "开启后：输出文件会包含原始中文分表与翻译结果分表。",
                "便于对照审校、回溯字段来源与排查差异。",
                "关闭后：仅保留翻译结果，文件更精简。",
            ],
            key="cb_keep_original",
            checkbox_label="保留原始表格开关",
            disabled=(phase == "running"),
        )

        settings.output.formula_display_value_backfill = render_checkbox_tooltip_row(
            params_card,
            "公式文本按显示值回填",
            "公式文本按显示值回填",
            "当单元格显示内容来自公式时，按公式计算结果匹配翻译并覆盖写入。",
            [
                "开启后：公式生成的中文或短语会按显示结果参与回填。",
                "命中后：输出分表中的对应单元格会被静态文本覆盖，不再保留原公式。",
                "关闭后：保持旧行为，更保守，但这类公式文本可能无法正确回填。",
            ],
            key="cb_formula_display_value_backfill",
            checkbox_label="公式文本按显示值回填开关",
            disabled=(phase == "running"),
        )

        render_checkbox_tooltip_row(
            params_card,
            "Excel 精调行高",
            "Excel 精调行高",
            "翻译完成后调用本地 Excel 进行更精确的行高自适应。",
            [
                "用于修正 Python 估算行高在复杂单元格下可能出现的偏差。",
                "通常能提升多行文本与长句的显示完整性。",
                "与“锁定行高，缩小字号”互斥，二者不能同时开启。",
            ],
            key="cb_excel_autofit",
            on_change=_on_excel_autofit_change,
            checkbox_label="Excel 精调行高开关",
            disabled=(phase == "running"),
        )

        render_checkbox_tooltip_row(
            params_card,
            "锁定行高，缩小字号",
            "锁定行高，缩小字号",
            "保持现有行高不变，通过递减字体来提升文本容纳能力。",
            [
                "适合版式必须固定、不能拉伸行高的表格模板。",
                "推荐含有浮动图片的表格开启，可有效防止行高拉伸导致的图片错位或遮挡。",
                "程序会在最小字号范围内自动尝试完整展示内容。",
                "文本过长时仍可能出现截断，请结合审校确认。",
                "与“Excel 精调行高”互斥，二者不能同时开启。",
            ],
            key="cb_lock_row_height",
            on_change=_on_lock_row_height_change,
            checkbox_label="锁定行高缩小字号开关",
            disabled=(phase == "running"),
        )

        if "cb_task_log" not in st.session_state:
            st.session_state["cb_task_log"] = settings.output.enable_task_log
        settings.output.enable_task_log = render_checkbox_tooltip_row(
            params_card,
            "启用任务日志",
            "启用任务日志",
            "记录每次翻译任务的关键过程与异常信息，便于定位问题。",
            [
                "开启后会将任务运行日志持续写入本地文件。",
                "适合排查失败重试、格式异常与性能波动问题。",
                "默认日志路径：~/.xl_translator/app.log。",
            ],
            key="cb_task_log",
            checkbox_label="启用任务日志开关",
            disabled=(phase == "running"),
        )

    settings.output.enable_excel_autofit = st.session_state.get("cb_excel_autofit", False)
    settings.output.lock_row_height = st.session_state.get("cb_lock_row_height", False)

    return settings


def _render_translate_output_card(settings: AppSettings, phase: str) -> AppSettings:

    output_card = render_setting_card(
        st,
        key="translate-inspector-output",
        title="输出位置",
        density="compact",
    )
    with output_card:
        output_mode_shell = output_card.container(key="translate-output-mode-shell")
        with output_mode_shell:
            output_mode = st.radio(
                "输出位置选择",
                options=["源目录内", "自定义目录"],
                index=1 if settings.output.use_custom_output_dir else 0,
                horizontal=True,
                label_visibility="collapsed",
                disabled=(phase == "running"),
                key="output_mode_radio",
            )

        render_radio_option_tooltips(
            container_key="translate-output-mode-shell",
            option_markup={
                "源目录内": build_tooltip_label_html(
                    "源目录内",
                    "源目录内",
                    "输出目录会创建在源路径同级位置，目录名自动附带时间戳。",
                    [
                        "无需额外填写目录，适合按原文件路径就地整理结果。",
                        "执行时会自动生成新的输出目录，避免覆盖历史翻译文件。",
                        "适合希望结果与原文保持邻近管理的场景。",
                    ],
                ),
                "自定义目录": build_tooltip_label_html(
                    "自定义目录",
                    "自定义目录",
                    "将翻译结果集中写入你指定的输出目录。",
                    [
                        "选中后可在下方输入一个输出目录绝对路径。",
                        "目录不存在时会在执行阶段自动创建。",
                        "适合把多个翻译任务统一归档到固定位置。",
                    ],
                ),
            },
        )
        settings.output.use_custom_output_dir = (output_mode == "自定义目录")

        if settings.output.use_custom_output_dir:
            custom_output_field = render_field_group(
                output_card,
                key="translate-custom-output",
                label="自定义输出目录",
            )
            with custom_output_field:
                custom_dir = st.text_input(
                    "自定义输出目录",
                    value=settings.output.custom_output_dir,
                    placeholder="输入输出目录绝对路径，如 C:\\Users\\xxx\\Output",
                    label_visibility="collapsed",
                    disabled=(phase == "running"),
                    key="custom_output_dir_input",
                )
            settings.output.custom_output_dir = custom_dir.strip().strip('"')

            custom_output_error = get_custom_output_dir_error(settings.output.custom_output_dir)
            custom_output_root = resolve_custom_output_dir(settings.output.custom_output_dir)
            if custom_output_error is not None:
                st.error(custom_output_error)
                col_use_default, col_retry = st.columns(2, gap="small")
                with col_use_default:
                    if st.button("使用默认位置", key="translate_use_default_output", use_container_width=True):
                        settings.output.use_custom_output_dir = False
                        st.session_state["output_mode_radio"] = "源目录内"
                        st.rerun()
                with col_retry:
                    if st.button("重新输入", key="translate_retry_output", use_container_width=True):
                        st.session_state["custom_output_dir_input"] = ""
                        st.rerun()
            elif custom_output_dir_will_be_created(settings.output.custom_output_dir):
                st.info(f"目录将在执行时自动创建：{custom_output_root}")
            else:
                st.success("自定义输出目录可用。")
        else:
            settings.output.custom_output_dir = ""

    return settings


def render_page(settings: AppSettings) -> AppSettings:
    _init(settings)
    _sync_translate_target_lang_from_widget(settings)
    _sync_translate_source_lang_from_widget(settings)
    render_main_tooltip_support()
    phase = st.session_state[_PHASE]

    if "cb_keep_original" not in st.session_state:
        st.session_state["cb_keep_original"] = settings.output.keep_original_sheets
    if "cb_formula_display_value_backfill" not in st.session_state:
        st.session_state["cb_formula_display_value_backfill"] = settings.output.formula_display_value_backfill
    if "cb_excel_autofit" not in st.session_state:
        st.session_state["cb_excel_autofit"] = settings.output.enable_excel_autofit
    if "cb_lock_row_height" not in st.session_state:
        st.session_state["cb_lock_row_height"] = settings.output.lock_row_height
    if "cb_task_log" not in st.session_state:
        st.session_state["cb_task_log"] = settings.output.enable_task_log
    if "output_mode_radio" not in st.session_state:
        st.session_state["output_mode_radio"] = (
            "自定义目录" if settings.output.use_custom_output_dir else "源目录内"
        )
    if "custom_output_dir_input" not in st.session_state:
        st.session_state["custom_output_dir_input"] = settings.output.custom_output_dir

    settings = _sync_translate_inspector_state(settings)

    _render_translate_page_header(settings, phase)

    command_shell = st.container(key="translate-command-shell")
    with command_shell:
        st.markdown(
            '<div class="toolbar-headline">源路径</div>',
            unsafe_allow_html=True,
        )
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
                key="translate-source-path",
                variant="single-layer",
            )
            with source_path_field:
                folder = st.text_input(
                    "源路径（文件夹或单文件）",
                    placeholder="可手动输入文件夹或 Excel 文件绝对路径，也可点击“浏览”选择，如 C:\\Users\\xxx\\Documents\\待翻译 或 C:\\Users\\xxx\\Documents\\A.xlsx",
                    label_visibility="collapsed",
                    disabled=(phase == "running"),
                    key=_FOLDER_INPUT_KEY,
                )
        with col_browse:
            if st.button(
                "浏览",
                use_container_width=True,
                disabled=(phase == "running"),
                key="translate_browse_button",
            ) and phase != "running":
                st.session_state[_BROWSE_DIALOG_OPEN] = True
                st.session_state[_BROWSE_ERROR] = ""
        with col_scan:
            scan_clicked = st.button(
                "扫描",
                type="secondary",
                use_container_width=True,
                disabled=(phase == "running"),
                key="translate_scan_button",
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

    if False and scan_clicked and phase != "running":
        folder = folder.strip().strip('"')
        if not folder:
            st.error("请输入文件夹或文件路径。")
        else:
            input_path = Path(folder)
            if not input_path.exists():
                st.error(f"路径不存在：{folder}")
            elif input_path.is_file() and not is_supported_excel_file(input_path):
                st.error("不支持的文件类型：仅支持 .xlsx / .xls 文件。")
            else:
                settings.last_source_folder = folder
                st.session_state["settings"] = settings
                st.session_state[_SOURCE_ROOT] = str(input_path if input_path.is_dir() else input_path.parent)
                with st.spinner("扫描中..."):
                    items = scan_path(input_path)
                st.session_state[_FILES] = items
                st.session_state[_PHASE] = "idle"
                st.session_state[_DONE] = None
                st.session_state[_LOGS] = []
                st.session_state[_PROGRESS] = None
                st.session_state[_STATUS] = None
                st.session_state[_STOP_PENDING] = False
                st.session_state[_STOP_MESSAGE] = ""
                for i in range(len(items)):
                    st.session_state[f"file_check_{i}"] = True
                st.session_state["file_check_all"] = True
                st.rerun()

    main_col, side_col = st.columns([2.16, 0.94], gap="medium")
    with main_col:
        if phase == "idle":
            _render_idle_content()
        elif phase == "running":
            _render_running_fragment()
        elif phase == "done":
            _render_done_content(settings)
        elif phase == "stopped":
            _render_stopped_content()
        elif phase == "error":
            _render_error_content(settings)

    with side_col:
        _render_action_buttons(settings, phase)
        settings = _render_translate_output_card(settings, phase)
        settings = _render_translate_inspector(settings, phase)

    return settings


def _render_idle_content() -> None:
    files = st.session_state[_FILES]
    workspace = st.container(key="translate-workspace-shell")
    with workspace:
        st.markdown(
            '<div class="workspace-shell__header">'
            '  <div>'
            '    <div class="workspace-shell__title">任务清单</div>'
            '  </div>'
            '</div>',
            unsafe_allow_html=True,
        )
        if not files:
            st.markdown(
                '<div class="content-placeholder content-placeholder--workbench">可手动输入文件夹或单个 Excel 文件路径后点击“扫描”，也可点击“浏览”选择并自动扫描，即可在此查看可处理文件列表。</div>',
                unsafe_allow_html=True,
            )
            return
        _render_translate_scope_summary(files)
        render_file_table(files)


def _get_translate_phase_layout(phase_total: int) -> tuple[dict[int, float], dict[int, float]]:
    if phase_total == 4:
        phase_weights = {1: 0.05, 2: 0.70, 3: 0.15, 4: 0.10}
    else:
        phase_weights = {1: 0.05, 2: 0.80, 3: 0.15}

    phase_offsets: dict[int, float] = {}
    cumulative = 0.0
    for phase_idx in range(1, phase_total + 1):
        phase_offsets[phase_idx] = cumulative
        cumulative += phase_weights.get(phase_idx, 0.0)

    return phase_weights, phase_offsets


def _calc_translate_overall_progress(progress: ProgressMsg) -> float:
    phase_weights, phase_offsets = _get_translate_phase_layout(progress.phase_total)
    step_pct = progress.step_done / max(progress.step_total, 1)
    overall = phase_offsets.get(progress.phase_index, 0.0) + step_pct * phase_weights.get(progress.phase_index, 0.0)
    return min(overall, 1.0)


def _drain_translate_runner_messages(runner: TaskRunner) -> None:
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
            st.session_state[_LOGS].append(
                {"level": "ERROR", "message": msg.message, "ts": ""}
            )
            st.session_state[_RUNNER] = None
            st.session_state[_PHASE] = "error"
            return
        elif isinstance(msg, StoppedMsg):
            st.session_state[_LOGS].append(
                {"level": "WARN", "message": msg.message, "ts": ""}
            )
            st.session_state[_RUNNER] = None
            st.session_state[_STOP_MESSAGE] = msg.message
            st.session_state[_PHASE] = "stopped"
            return


def _render_running_status_block() -> bool:
    runner: TaskRunner | None = st.session_state.get(_RUNNER)
    if runner is None:
        st.error("任务状态异常：运行器不存在。")
        return False

    _drain_translate_runner_messages(runner)
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
        overall = _calc_translate_overall_progress(progress)
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
            st.warning("正在停止任务，等待当前批次安全结束...", icon="⚠️")
    elif status_desc:
        st.info(f"🔄 {status_desc}")
        if stop_hint:
            st.warning("已收到停止请求，等待后台任务退出...", icon="⚠️")
    else:
        st.info("初始化中，请稍候...")
        if stop_hint:
            st.warning("已收到停止请求，等待后台任务退出...", icon="⚠️")

    render_log_area(st.session_state[_LOGS], container_id="translate-log-container")
    return True


def _render_running_fragment() -> None:
    _translate_running_fragment()


@st.fragment(run_every=0.3)
def _translate_running_fragment() -> None:
    phase = st.session_state.get(_PHASE, "idle")
    if phase != "running":
        st.rerun()
        return

    if not _render_running_status_block():
        st.rerun()


def _latest_translate_error_message() -> str:
    error_messages = [
        str(item.get("message") or "")
        for item in st.session_state.get(_LOGS, [])
        if item.get("level") == "ERROR"
    ]
    return error_messages[-1] if error_messages else ""


def _done_needs_translate_diagnostics(done: DoneMsg) -> bool:
    failure_n = sum(1 for item in done.file_results if not item.get("success"))
    return failure_n > 0 or bool(done.issues)


def _ensure_translate_diagnostic_archive(settings: AppSettings, phase: str) -> None:
    if st.session_state.get(_DIAGNOSTIC_RECORD_DIR) or st.session_state.get(_DIAGNOSTIC_ERROR):
        return

    done = st.session_state.get(_DONE)
    if phase == "done":
        if not isinstance(done, DoneMsg) or not _done_needs_translate_diagnostics(done):
            return
        error_message = ""
    elif phase == "error":
        error_message = _latest_translate_error_message() or "任务执行出错，请查看日志。"
    else:
        return

    try:
        record_dir = archive_task_diagnostics(
            surface="excel",
            phase=phase,
            task_id=st.session_state.get(_TASK_ID, ""),
            settings=settings,
            selected_files=st.session_state.get(_FILES, []),
            logs=st.session_state.get(_LOGS, []),
            done=done if isinstance(done, DoneMsg) else None,
            error_message=error_message,
            source_root=st.session_state.get(_SOURCE_ROOT, ""),
            status=st.session_state.get(_STATUS, ""),
            progress=st.session_state.get(_PROGRESS),
        )
        st.session_state[_DIAGNOSTIC_RECORD_DIR] = str(record_dir)
    except Exception as exc:  # noqa: BLE001 - show recoverable UI notice
        st.session_state[_DIAGNOSTIC_ERROR] = str(exc)


def _render_translate_diagnostic_panel() -> None:
    diagnostic_error = st.session_state.get(_DIAGNOSTIC_ERROR)
    if diagnostic_error:
        st.warning(f"诊断归档失败：{diagnostic_error}")
        return
    render_current_diagnostic_download(
        st.session_state.get(_DIAGNOSTIC_RECORD_DIR),
        key_prefix="translate",
    )


def _render_done_content(settings: AppSettings) -> None:
    done: DoneMsg = st.session_state[_DONE]
    success_n = sum(1 for item in done.file_results if item.get("success"))
    failure_n = len(done.file_results) - success_n
    issues = list(done.issues or [])
    _ensure_translate_diagnostic_archive(settings, "done")

    workspace = st.container(key="translate-done-shell")
    with workspace:
        st.markdown(
            '<div class="workspace-shell__header">'
            '  <div>'
            '    <div class="workspace-shell__title">任务结果</div>'
            '    <div class="workspace-shell__caption">翻译任务已经完成，下面保留本次执行的关键结果与输出目录。</div>'
            '  </div>'
            '</div>',
            unsafe_allow_html=True,
        )
        if issues and failure_n:
            st.warning("翻译完成，但有 API 请求或文件处理问题需要检查。")
        elif issues:
            st.warning("翻译完成，但有 API 请求未成功，部分内容可能保留为原文。")
        elif failure_n:
            st.warning("翻译完成，但有文件未成功。")
        else:
            st.success("翻译任务已完成。")
        for issue in issues:
            message = str(issue.get("message") or "").strip()
            if message:
                st.warning(message)
        _render_translate_diagnostic_panel()
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
        st.markdown(
            f'<div class="mono-block">{html.escape(done.output_dir)}</div>',
            unsafe_allow_html=True,
        )

        for result in done.file_results:
            modifier = " translate-result-row--error" if not result.get("success") else ""
            detail_html = ""
            if result.get("error"):
                detail_html = (
                    f'<div class="translate-result-row__detail">{html.escape(result["error"])}</div>'
                )
            st.markdown(
                f'<div class="translate-result-row{modifier}">'
                f'  <div class="translate-result-row__title">{html.escape(result["name"])}</div>'
                f'  <div class="translate-result-row__meta">{"成功" if result.get("success") else "失败"}</div>'
                f'  {detail_html}'
                '</div>',
                unsafe_allow_html=True,
            )


def _render_error_content(settings: AppSettings) -> None:
    _ensure_translate_diagnostic_archive(settings, "error")
    workspace = st.container(key="translate-error-shell")
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
        error_messages = [
            str(item.get("message") or "")
            for item in st.session_state.get(_LOGS, [])
            if item.get("level") == "ERROR"
        ]
        st.error(error_messages[-1] if error_messages else "任务执行出错，请查看日志。")
        _render_translate_diagnostic_panel()
        render_log_area(st.session_state[_LOGS], container_id="translate-log-container")


def _render_stopped_content() -> None:
    workspace = st.container(key="translate-stopped-shell")
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
        render_log_area(st.session_state[_LOGS], container_id="translate-log-container")


def _start_translation_task(
    selected_files,
    settings,
    source_root,
    target_lang,
    allow_xls_fallback=False,
    source_lang="zh",
) -> None:
    settings.target_lang = target_lang
    settings.source_lang = str(source_lang or get_default_source_lang()).strip() or get_default_source_lang()
    runner = TaskRunner(
        selected_files,
        settings,
        source_root=source_root,
        allow_xls_fallback=allow_xls_fallback,
        source_lang=settings.source_lang,
    )
    runner.start()
    st.session_state[_RUNNER] = runner
    st.session_state[_TASK_ID] = runner.task_id
    st.session_state[_LOGS] = []
    st.session_state[_PROGRESS] = None
    st.session_state[_STATUS] = None
    st.session_state[_PHASE] = "running"
    st.session_state[_DONE] = None
    st.session_state[_STOP_PENDING] = False
    st.session_state[_STOP_MESSAGE] = ""
    st.session_state[_DIAGNOSTIC_RECORD_DIR] = ""
    st.session_state[_DIAGNOSTIC_ERROR] = ""
    st.rerun()


@st.dialog("⚠️ 发现旧版 .xls 格式 - 需要兼容模式")
def _xls_fallback_dialog(
    selected_files,
    root,
    settings,
    selected_target_lang,
    selected_source_lang="zh",
    unavailable_reason="",
) -> None:
    st.warning(
        "您的列表中包含旧版 `.xls` 文件，且当前环境未检测到可用的本地 Excel 自动化支持。\n\n"
        "**需要使用纯代码兼容模式处理：**\n"
        "- 纯代码模式不可避免地会**丢失一些复杂的单元格格式**（如合并单元格的样式）、图片和图表等。\n"
        "- 翻译的基础文本数据不受影响。\n\n"
        "您是否同意使用兼容模式继续本次翻译？\n"
        "*(如果不同意，请关闭本窗口，并在列表中取消勾选 .xls 文件后再试)*"
    )
    if unavailable_reason:
        st.caption(f"检测详情：{unavailable_reason}")
    c1, c2 = st.columns(2)
    if c1.button("同意并继续", type="secondary", key="translate_xls_accept", use_container_width=True):
        st.session_state.pop("show_xls_fallback_dialog", None)
        _start_translation_task(
            selected_files,
            settings,
            root,
            selected_target_lang,
            True,
            source_lang=selected_source_lang,
        )
    if c2.button("取消", key="translate_xls_cancel", use_container_width=True):
        st.session_state.pop("show_xls_fallback_dialog", None)
        st.rerun()


def _render_action_buttons(settings: AppSettings, phase: str) -> None:
    action_shell = st.container(key="translate-action-shell")
    with action_shell:
        st.markdown(
            '<div class="workspace-shell__header workspace-shell__header--compact">'
            '  <div>'
            '    <div class="workspace-shell__title">执行操作</div>'
            '  </div>'
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

            if st.session_state.get("show_xls_fallback_dialog"):
                dlg_state = st.session_state["show_xls_fallback_dialog"]
                _xls_fallback_dialog(
                    dlg_state["items"],
                    dlg_state["root"],
                    settings,
                    dlg_state["target_lang"],
                    dlg_state.get("source_lang", get_default_source_lang()),
                    dlg_state.get("reason", ""),
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
                key="translate_start_button",
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
                api_config_check = check_translation_api_config(settings)
                if not api_config_check.ok:
                    st.error(api_config_check.message)
                    if api_config_check.detail:
                        st.caption(api_config_check.detail)
                    return

                source_root = (st.session_state.get(_SOURCE_ROOT) or settings.last_source_folder or "").strip().strip('"')
                resolved_root = source_root if source_root else None

                has_xls = any(f.path.suffix.lower() == ".xls" for f in selected)
                excel_automation_available, unavailable_reason = get_local_excel_availability()
                if has_xls and not excel_automation_available:
                    st.session_state["show_xls_fallback_dialog"] = {
                        "items": selected,
                        "root": resolved_root,
                        "target_lang": selected_target_lang,
                        "source_lang": selected_source_lang,
                        "reason": unavailable_reason,
                    }
                    st.rerun()
                else:
                    _start_translation_task(
                        selected,
                        settings,
                        resolved_root,
                        selected_target_lang,
                        allow_xls_fallback=False,
                        source_lang=selected_source_lang,
                    )

        elif phase == "running":
            runner: TaskRunner | None = st.session_state.get(_RUNNER)
            st.markdown(
                '<div class="ui-field-note ui-field-note--block">运行期间会锁定参数；终止操作采用二次确认，防止误触。</div>',
                unsafe_allow_html=True,
            )
            if runner is None:
                st.button("任务状态异常", type="primary", use_container_width=True, disabled=True, key="translate_runner_missing")
                return

            if st.session_state.get(_STOP_PENDING):
                st.warning("再次点击后将请求停止当前任务。", icon="⚠️")
                col_confirm, col_cancel = st.columns(2, gap="small")
                with col_confirm:
                    if st.button("确认终止", type="primary", use_container_width=True, key="translate_confirm_stop"):
                        runner.stop()
                        st.session_state[_STOP_PENDING] = False
                        st.session_state[_STATUS] = "状态：正在停止任务，请等待当前批次结束..."
                        st.rerun()
                with col_cancel:
                    if st.button("继续翻译", use_container_width=True, key="translate_keep_running"):
                        st.session_state[_STOP_PENDING] = False
                        st.rerun()
            elif runner.stop_requested():
                st.button("正在停止...", type="primary", use_container_width=True, disabled=True, key="translate_stopping")
                st.markdown(
                    '<div class="ui-field-note ui-field-note--block">等待当前批次安全结束后会自动切回结果状态。</div>',
                    unsafe_allow_html=True,
                )
            else:
                if st.button("终止翻译", type="primary", use_container_width=True, key="translate_stop_button"):
                    st.session_state[_STOP_PENDING] = True
                    st.rerun()
                st.markdown(
                    '<div class="ui-field-note ui-field-note--block">点击后需再次确认，避免误触。</div>',
                    unsafe_allow_html=True,
                )

        elif phase in ("done", "error", "stopped"):
            if st.button("返回并开始新任务", type="primary", use_container_width=True, key="translate_reset_button"):
                st.session_state[_PHASE] = "idle"
                st.session_state[_FILES] = []
                st.session_state[_DONE] = None
                st.session_state[_RUNNER] = None
                st.session_state[_STOP_PENDING] = False
                st.session_state[_STOP_MESSAGE] = ""
                st.session_state[_TASK_ID] = ""
                st.session_state[_DIAGNOSTIC_RECORD_DIR] = ""
                st.session_state[_DIAGNOSTIC_ERROR] = ""
                st.rerun()

        render_history_diagnostic_export(
            key_prefix="translate",
            disabled=(phase == "running"),
        )
