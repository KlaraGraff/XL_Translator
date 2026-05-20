"""
记忆库管理页面（原 TM 管理）。
布局（一页内完成，无需翻动）：
  顶部行：单一入库规则 | 语言对切换 | 词库概览统计 + 导入/导出
  深度清洗区（固定展示，置于阈值与词表之间）
  搜索框 + 录入按钮
  主体：词条表格（固定高度，内部可滚动）
  底部：翻页
"""
import csv
import html
import io
import json
import queue as _queue
import threading

import streamlit as st

from config import CLOUD_ENGINES
from core import tm_manager
from core.language_registry import (
    build_lang_pair,
)
from settings import AppSettings
from ui.components import (
    build_tooltip_label_html,
    render_field_group,
    render_main_tooltip_support,
    render_tooltip_label,
)
from ui.target_language_selector import render_target_lang_selectbox

# ── 清洗状态机 session_state 键 ──────────────────────────────────────────────
_CLEAN_PHASE   = "clean_phase"      # "idle" | "running"
_CLEAN_THREAD  = "clean_thread"
_CLEAN_PROG_Q  = "clean_prog_q"
_CLEAN_RES_Q   = "clean_res_q"
_CLEAN_PROG    = "clean_progress"   # structured dict, legacy tuple supported
_CLEAN_RESULT  = "clean_result"     # ("ok", suggestions) | ("err", msg)
_CLEAN_CANCEL  = "clean_cancel_event"
_TM_TABLE_COLS = [4.75, 4.75, 0.64, 0.64, 0.64]
_TM_TABLE_BODY_HEIGHT = 336
_CLEANER_PROMPT_EXTRA_KEY = "_tm_cleaner_prompt_extra"
_CLEANER_PROMPT_FULL_KEY = "_tm_cleaner_prompt_full"
_CLEANER_PROMPT_FULL_ENABLED_KEY = "_tm_cleaner_prompt_full_enabled"
_CLEANER_PROMPT_OPEN_KEY = "_tm_cleaner_prompt_open"
_CLEANER_PROMPT_RESET_PENDING_KEY = "_tm_cleaner_prompt_reset_pending"
_DIFF_CHECKBOX_PREFIX = "_tm_clean_diff_accept_"


def _tm_search_applied_key(lang_pair: str) -> str:
    return f"tm_search_{lang_pair}"


def _tm_search_input_key(lang_pair: str) -> str:
    return f"tm_search_input_{lang_pair}"


def _tm_page_state_key(lang_pair: str) -> str:
    return f"tm_page_{lang_pair}"


def _apply_tm_search(lang_pair: str) -> None:
    input_key = _tm_search_input_key(lang_pair)
    applied_key = _tm_search_applied_key(lang_pair)
    st.session_state[applied_key] = str(st.session_state.get(input_key, "")).strip()
    st.session_state[_tm_page_state_key(lang_pair)] = 1


def _clean_state_init():
    for k, v in [
        (_CLEAN_PHASE,  "idle"),
        (_CLEAN_THREAD, None),
        (_CLEAN_PROG_Q, None),
        (_CLEAN_RES_Q,  None),
        (_CLEAN_PROG,   None),
        (_CLEAN_RESULT, None),
        (_CLEAN_CANCEL, None),
    ]:
        if k not in st.session_state:
            st.session_state[k] = v


def _get_cleaner_prompt_extra(settings: AppSettings, lang_pair: str) -> str:
    return str(settings.cleaner_prompt_extras.get(lang_pair, "")).strip()


def _get_cleaner_full_prompt_override(settings: AppSettings, lang_pair: str) -> str:
    return str(settings.cleaner_full_prompt_overrides.get(lang_pair, "")).strip()


def _update_lang_prompt_map(prompt_map: dict[str, str], lang_pair: str, prompt: str) -> dict[str, str]:
    updated = dict(prompt_map)
    value = str(prompt or "").strip()
    if value:
        updated[lang_pair] = value
    else:
        updated.pop(lang_pair, None)
    return updated


def _open_cleaner_prompt_dialog(settings: AppSettings, lang_pair: str) -> None:
    full_override = _get_cleaner_full_prompt_override(settings, lang_pair)
    st.session_state[_CLEANER_PROMPT_EXTRA_KEY] = _get_cleaner_prompt_extra(settings, lang_pair)
    st.session_state[_CLEANER_PROMPT_FULL_KEY] = full_override
    st.session_state[_CLEANER_PROMPT_FULL_ENABLED_KEY] = bool(full_override)
    st.session_state.pop(_CLEANER_PROMPT_RESET_PENDING_KEY, None)
    st.session_state[_CLEANER_PROMPT_OPEN_KEY] = True


def _close_cleaner_prompt_dialog() -> None:
    st.session_state[_CLEANER_PROMPT_OPEN_KEY] = False
    st.session_state.pop(_CLEANER_PROMPT_EXTRA_KEY, None)
    st.session_state.pop(_CLEANER_PROMPT_FULL_KEY, None)
    st.session_state.pop(_CLEANER_PROMPT_FULL_ENABLED_KEY, None)
    st.session_state.pop(_CLEANER_PROMPT_RESET_PENDING_KEY, None)


def _reset_cleaner_prompt_dialog_state() -> None:
    st.session_state[_CLEANER_PROMPT_EXTRA_KEY] = ""
    st.session_state[_CLEANER_PROMPT_FULL_KEY] = ""
    st.session_state[_CLEANER_PROMPT_FULL_ENABLED_KEY] = False


def _queue_cleaner_prompt_reset() -> None:
    st.session_state[_CLEANER_PROMPT_RESET_PENDING_KEY] = True


def _consume_cleaner_prompt_reset_if_needed() -> None:
    if st.session_state.pop(_CLEANER_PROMPT_RESET_PENDING_KEY, False):
        _reset_cleaner_prompt_dialog_state()


def _diff_checkbox_key(lang_pair: str, entry_id: int) -> str:
    return f"{_DIFF_CHECKBOX_PREFIX}{lang_pair}_{entry_id}"


def _clear_diff_checkbox_state(lang_pair: str) -> None:
    prefix = f"{_DIFF_CHECKBOX_PREFIX}{lang_pair}_"
    for key in list(st.session_state.keys()):
        if key.startswith(prefix):
            st.session_state.pop(key, None)


def _normalize_clean_progress_payload(payload):
    if payload is None:
        return None

    stage = "prepared"
    total_entries = 0
    completed_entries = 0
    total_batches = 0
    completed_batches = 0
    submitted_batches = 0

    if isinstance(payload, dict):
        stage = str(payload.get("stage") or "prepared")
        total_entries = int(payload.get("total_entries") or payload.get("total") or 0)
        completed_entries = int(payload.get("completed_entries") or payload.get("done") or 0)
        total_batches = int(payload.get("total_batches") or 0)
        completed_batches = int(payload.get("completed_batches") or 0)
        submitted_batches = int(payload.get("submitted_batches") or completed_batches or 0)
    elif isinstance(payload, (tuple, list)) and len(payload) >= 2:
        completed_entries = int(payload[0] or 0)
        total_entries = int(payload[1] or 0)
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


def _build_clean_progress_display(progress: dict) -> tuple[float, str, str | None]:
    stage = progress["stage"]
    total_entries = progress["total_entries"]
    completed_entries = progress["completed_entries"]
    total_batches = progress["total_batches"]
    completed_batches = progress["completed_batches"]
    submitted_batches = progress["submitted_batches"]

    batch_summary = None
    if total_batches:
        batch_summary = f"{completed_batches} / {total_batches} 个批次"

    if stage == "prepared":
        text = f"深度清洗准备中... 已装载 {total_entries} 条词条"
        if total_batches:
            text += f"，已切分为 {total_batches} 个批次"
        return 0.0, text, "正在初始化清洗任务。"

    if stage == "waiting_first_result":
        text = "深度清洗已开始，正在等待首批结果返回..."
        if total_batches:
            return 0.02, text, f"已调度 {submitted_batches} / {total_batches} 个批次。"
        return 0.02, text, None

    if total_entries:
        progress_value = completed_entries / total_entries
    else:
        progress_value = 1.0 if stage == "completed" else 0.0

    if stage == "completed":
        text = f"深度清洗即将完成... 已处理 {completed_entries} / {total_entries} 条词条"
        if batch_summary:
            text += f"，完成 {batch_summary}"
        return 1.0 if total_entries else progress_value, text, None

    text = f"深度清洗进行中... 已处理 {completed_entries} / {total_entries} 条词条"
    if batch_summary:
        text += f"，完成 {batch_summary}"
    return progress_value, text, None


def _set_diff_checkbox_state(lang_pair: str, entry_ids: list[int], accepted: bool) -> None:
    for entry_id in entry_ids:
        st.session_state[_diff_checkbox_key(lang_pair, entry_id)] = accepted


# ── 全删确认弹窗（支持固定项冲突分流）────────────────────────────────────────

@st.dialog("⚠️ 确认全部删除")
def _bulk_delete_dialog(lang_pair: str, keyword: str):
    pin_stats = tm_manager.get_pin_count(lang_pair, keyword)
    total = pin_stats["pinned"] + pin_stats["unpinned"]
    filter_hint = f"（关键词：\"{keyword}\"）" if keyword.strip() else ""
    has_pinned = pin_stats["pinned"] > 0

    if has_pinned:
        st.warning(
            f"当前筛选范围内共 **{total}** 条词条{filter_hint}，"
            f"其中 **{pin_stats['pinned']}** 条已固定。\n\n"
            "**已固定词条受保护。请选择处理方式：**"
        )
        c1, c2, c3 = st.columns([2, 2, 1])
        if c1.button(
            f"解除固定并全部清空（{total} 条）",
            type="secondary",
            key=f"dlg_all_{lang_pair}",
            use_container_width=True,
        ):
            tm_manager.set_all_pinned(lang_pair, False, keyword)
            deleted = tm_manager.delete_all_entries(lang_pair, keyword)
            st.session_state.pop(f"show_bulk_delete_{lang_pair}", None)
            st.session_state["_pending_toast"] = (f"已删除全部 {deleted} 条词条", "🗑️")
            st.rerun()
        if c2.button(
            f"仅删除未固定词汇（{pin_stats['unpinned']} 条）",
            key=f"dlg_unpinned_{lang_pair}",
            use_container_width=True,
        ):
            deleted = tm_manager.delete_unpinned_entries(lang_pair, keyword)
            st.session_state.pop(f"show_bulk_delete_{lang_pair}", None)
            st.session_state["_pending_toast"] = (f"已删除 {deleted} 条未固定词条", "🗑️")
            st.rerun()
        if c3.button("取消", key=f"dlg_cancel_{lang_pair}", use_container_width=True):
            st.session_state.pop(f"show_bulk_delete_{lang_pair}", None)
            st.rerun()
    else:
        st.warning(
            f"即将删除当前筛选下的 **{total}** 条词条{filter_hint}。\n\n"
            "**此操作不可撤销，删除后无法恢复！**"
        )
        c1, c2 = st.columns(2)
        if c1.button("确认删除", type="secondary", key=f"dlg_confirm_{lang_pair}"):
            deleted = tm_manager.delete_all_entries(lang_pair, keyword)
            st.session_state.pop(f"show_bulk_delete_{lang_pair}", None)
            st.session_state["_pending_toast"] = (f"已删除 {deleted} 条词条", "🗑️")
            st.rerun()
        if c2.button("取消", key=f"dlg_cancel2_{lang_pair}"):
            st.session_state.pop(f"show_bulk_delete_{lang_pair}", None)
            st.rerun()


# ── 导入弹窗 ──────────────────────────────────────────────────────────────────

@st.dialog("📥 导入词库")
def _import_dialog(lang_pair: str):
    uploaded = st.file_uploader(
        "选择词库文件（JSON 或 CSV）",
        type=["json", "csv"],
        key=f"import_upload_{lang_pair}",
        label_visibility="collapsed",
    )
    st.caption("支持本工具导出的 JSON / CSV 文件")

    render_tooltip_label(
        "重复词条处理方式",
        "导入冲突处理策略",
        "当导入词条与本地已有原文冲突时，按设定规则处理。",
        [
            "跳过重复项：保留本地已有译文，仅写入新词条。",
            "覆盖重复项：以导入文件译文替换本地同源词条。",
            "建议先导出备份，再进行覆盖导入。",
        ],
    )
    conflict_mode = st.radio(
        "重复词条处理方式选择",
        options=["跳过重复项", "覆盖重复项"],
        key=f"import_mode_{lang_pair}",
        label_visibility="collapsed",
    )
    mode_map = {"跳过重复项": "skip", "覆盖重复项": "overwrite"}

    c1, c2 = st.columns(2)
    if c2.button("取消", key=f"cancel_import_{lang_pair}", use_container_width=True):
        st.rerun()
    if c1.button(
        "开始导入",
        type="secondary",
        key=f"do_import_{lang_pair}",
        disabled=not uploaded,
        use_container_width=True,
    ):
        if uploaded:
            try:
                entries = _parse_import_file(uploaded)
                result  = tm_manager.import_entries(
                    entries, lang_pair, mode_map[conflict_mode]
                )
                st.success(
                    f"导入完成：新增 **{result['inserted']}** 条，"
                    f"处理重复 {result['duplicates']} 条（{conflict_mode}）"
                )
                if st.button(
                    "关闭并刷新",
                    type="secondary",
                    key=f"close_import_{lang_pair}",
                    use_container_width=True,
                ):
                    st.rerun()
            except Exception as e:
                st.error(f"导入失败：{e}")


def _parse_import_file(uploaded) -> list[dict]:
    content = uploaded.read().decode("utf-8-sig")
    name = uploaded.name.lower()
    if name.endswith(".json"):
        data = json.loads(content)
        if not isinstance(data, list):
            raise ValueError("JSON 格式错误：顶层应为数组")
        return data
    if name.endswith(".csv"):
        reader = csv.DictReader(io.StringIO(content))
        return list(reader)
    raise ValueError(f"不支持的文件类型：{uploaded.name}")


def _build_export_data(lang_pair: str, fmt: str) -> tuple[str, str, str]:
    """返回 (数据字符串, 文件名, mime 类型)。"""
    entries = tm_manager.get_all_entries_for_export(lang_pair)
    if fmt == "json":
        data = json.dumps(entries, ensure_ascii=False, indent=2)
        return data, f"tm_{lang_pair}.json", "application/json"
    # csv
    buf = io.StringIO()
    if entries:
        writer = csv.DictWriter(buf, fieldnames=list(entries[0].keys()))
        writer.writeheader()
        writer.writerows(entries)
    return buf.getvalue(), f"tm_{lang_pair}.csv", "text/csv"


@st.dialog("自定义深度清洗提示词", on_dismiss=_close_cleaner_prompt_dialog)
def _render_cleaner_prompt_dialog_legacy_unused(settings: AppSettings):
    """Legacy placeholder kept only to preserve historical dialog naming."""
    _ = settings
    return None

@st.dialog("自定义深度清洗提示词", on_dismiss=_close_cleaner_prompt_dialog)
def _render_cleaner_prompt_dialog(settings: AppSettings, lang_pair: str):
    from core.tm_cleaner import (
        build_clean_system_prompt,
        get_clean_builtin_system_prompt,
        get_clean_target_lang_name,
    )

    if _CLEANER_PROMPT_EXTRA_KEY not in st.session_state:
        _open_cleaner_prompt_dialog(settings, lang_pair)
    _consume_cleaner_prompt_reset_if_needed()

    builtin_prompt = get_clean_builtin_system_prompt(
        lang_pair,
        settings.custom_target_langs,
    )
    target_lang_name = get_clean_target_lang_name(
        lang_pair,
        settings.custom_target_langs,
    )

    st.caption(
        f"内置清洗规则会根据当前语言自动切换。当前正在编辑：{target_lang_name} 方向的自定义清洗配置。"
    )
    st.markdown("**内置提示词（只读）**")
    st.text_area(
        "内置提示词预览",
        value=builtin_prompt,
        height=220,
        disabled=True,
        label_visibility="collapsed",
    )

    st.markdown("**当前语言的补充提示词**")
    st.caption("这里只用于补充当前语言的术语偏好和细则；留空时仅使用内置提示词。")
    st.text_area(
        "当前语言的补充提示词",
        key=_CLEANER_PROMPT_EXTRA_KEY,
        height=140,
        placeholder="例如：优先统一当前语言的工程缩写，保持语气简洁。",
        label_visibility="collapsed",
    )

    st.checkbox(
        "启用完整提示词覆盖（高级）",
        key=_CLEANER_PROMPT_FULL_ENABLED_KEY,
        help="开启后，当前语言将直接使用下方的完整 prompt，不再拼接内置规则与补充提示词。",
    )
    if st.session_state.get(_CLEANER_PROMPT_FULL_ENABLED_KEY):
        st.warning("高级覆盖已启用。保存后，当前语言将直接使用下方的完整 prompt。")
        st.text_area(
            "完整提示词覆盖",
            key=_CLEANER_PROMPT_FULL_KEY,
            height=220,
            placeholder="仅在需要完全接管清洗 prompt 时使用。",
            label_visibility="collapsed",
        )

    preview_prompt = build_clean_system_prompt(
        lang_pair=lang_pair,
        extra_prompt=st.session_state.get(_CLEANER_PROMPT_EXTRA_KEY, ""),
        full_override_prompt=(
            st.session_state.get(_CLEANER_PROMPT_FULL_KEY, "")
            if st.session_state.get(_CLEANER_PROMPT_FULL_ENABLED_KEY)
            else ""
        ),
        custom_target_langs=settings.custom_target_langs,
    )
    with st.expander("查看实际发送给模型的 prompt", expanded=False):
        st.text_area(
            "实际 prompt 预览",
            value=preview_prompt,
            height=260,
            disabled=True,
            label_visibility="collapsed",
        )

    bc1, bc2, bc3 = st.columns([1.1, 1.1, 0.9])
    if bc1.button("恢复默认", key="cleaner_prompt_reset", use_container_width=True):
        _queue_cleaner_prompt_reset()
        st.rerun()
    if bc2.button("保存", type="secondary", key="cleaner_prompt_save", use_container_width=True):
        extra_prompt = str(st.session_state.get(_CLEANER_PROMPT_EXTRA_KEY, "")).strip()
        full_enabled = bool(st.session_state.get(_CLEANER_PROMPT_FULL_ENABLED_KEY))
        full_override = (
            str(st.session_state.get(_CLEANER_PROMPT_FULL_KEY, "")).strip()
            if full_enabled
            else ""
        )
        if full_enabled and not full_override:
            st.toast("已启用完整覆盖，请填写完整 prompt 或关闭高级模式。", icon="⚠️")
            return

        settings.cleaner_prompt_extras = _update_lang_prompt_map(
            settings.cleaner_prompt_extras,
            lang_pair,
            extra_prompt,
        )
        settings.cleaner_full_prompt_overrides = _update_lang_prompt_map(
            settings.cleaner_full_prompt_overrides,
            lang_pair,
            full_override,
        )
        st.session_state["settings"] = settings
        _close_cleaner_prompt_dialog()
        st.toast("当前语言的清洗 prompt 已更新", icon="✅")
        st.rerun()
    if bc3.button("取消", key="cleaner_prompt_cancel", use_container_width=True):
        _close_cleaner_prompt_dialog()
        st.rerun()


def _render_tm_cell_text(container, value: str, *, max_chars: int = 40) -> None:
    clipped = value[:max_chars] + ("…" if len(value) > max_chars else "")
    container.markdown(
        f'<div class="tm-row-text">{html.escape(clipped)}</div>',
        unsafe_allow_html=True,
    )


def _render_tm_row_anchor(*classes: str) -> None:
    cls = " ".join(["tm-table-row-anchor", *classes]).strip()
    st.markdown(f'<span class="{cls}" style="display:none"></span>', unsafe_allow_html=True)


def _render_tm_diff_text(
    container,
    value: str,
    *,
    emphasize: bool = False,
    alternate: bool = False,
) -> None:
    classes = ["tm-diff-text"]
    if emphasize:
        classes.append("tm-diff-text--new")
    if alternate:
        classes.append("tm-diff-text--alt")
    container.markdown(
        f'<div class="{" ".join(classes)}">{html.escape(str(value or ""))}</div>',
        unsafe_allow_html=True,
    )


def _inject_tm_layout_styles() -> None:
    st.markdown(
        (
            "<style>"
            ":root{--tm-table-body-height:"
            f"{_TM_TABLE_BODY_HEIGHT}px;"
            "}"
            "</style>"
        ),
        unsafe_allow_html=True,
    )


def _render_tm_section_divider(*, tight: bool = False) -> None:
    cls = "tm-section-divider tm-section-divider--tight" if tight else "tm-section-divider"
    st.markdown(f'<div class="{cls}"></div>', unsafe_allow_html=True)


def _render_tm_page_header(clean_phase: str) -> None:
    phase_label = "清洗执行中" if clean_phase == "running" else "词库就绪"
    st.markdown(
        '<div class="page-head">'
        '  <div class="page-head__eyebrow">TM Workbench</div>'
        '  <div class="page-head__title-row">'
        '    <div>'
        '      <div class="page-head__title">记忆库管理</div>'
        '    </div>'
        f'    <div class="phase-badge">{phase_label}</div>'
        '  </div>'
        '</div>',
        unsafe_allow_html=True,
    )


def _render_tm_stat_tiles(items: list[tuple[str, str]], *, compact: bool = False) -> None:
    grid_class = "tm-insight-grid tm-insight-grid--compact" if compact else "tm-insight-grid"
    tiles_html = "".join(
        (
            '<div class="tm-insight-tile">'
            f'<span class="tm-insight-label">{html.escape(label)}</span>'
            f'<span class="tm-insight-value">{html.escape(value)}</span>'
            '</div>'
        )
        for label, value in items
    )
    st.markdown(f'<div class="{grid_class}">{tiles_html}</div>', unsafe_allow_html=True)


def _render_tm_scope_toolbar(
    lang_pair: str,
    keyword: str,
    total: int,
    pin_stats: dict,
    all_pinned: bool,
    *,
    disabled: bool = False,
) -> None:
    scope_title = "当前语言对搜索结果" if keyword.strip() else "当前语言对全部词条"
    chips = []
    if keyword.strip():
        chips.append(
            f'<span class="tm-scope-chip tm-scope-chip--keyword" title="{html.escape(keyword, quote=True)}">'
            f'搜索 {html.escape(keyword)}'
            '</span>'
        )
    chips.extend(
        [
            f'<span class="tm-scope-chip">范围 {total:,}</span>',
            f'<span class="tm-scope-chip">已固定 {pin_stats["pinned"]:,}</span>',
            f'<span class="tm-scope-chip">可编辑 {pin_stats["unpinned"]:,}</span>',
        ]
    )
    scope_band = st.container(key=f"tm-scope-band-{lang_pair}")
    with scope_band:
        summary_col, btn_del, btn_pin = st.columns([3.2, 1.05, 1.05], gap="small", vertical_alignment="top")
        with summary_col:
            st.markdown(
                '<div class="tm-scope-toolbar">'
                '  <div class="tm-scope-toolbar__main">'
                f'    <div class="tm-scope-toolbar__title">{scope_title}</div>'
                f'    <div class="tm-scope-toolbar__meta">{"".join(chips)}</div>'
                '  </div>'
                '</div>',
                unsafe_allow_html=True,
            )
        with btn_del:
            if st.button(
                "批量删除",
                key=f"tm_bulk_delete_{lang_pair}",
                use_container_width=True,
                disabled=disabled,
            ):
                st.session_state[f"show_bulk_delete_{lang_pair}"] = True
                st.rerun()
        with btn_pin:
            if st.button(
                "全部解锁" if all_pinned else "全部固定",
                key=f"tm_bulk_pin_{lang_pair}",
                use_container_width=True,
                disabled=disabled,
            ):
                tm_manager.set_all_pinned(
                    lang_pair,
                    not all_pinned,
                    st.session_state.get(_tm_search_applied_key(lang_pair), ""),
                )
                st.rerun()


def _render_tm_running_workspace(stats: dict, lang_pair: str) -> None:
    workspace = st.container(key="tm-running-workspace")
    with workspace:
        st.markdown(
            '<div class="workspace-shell__header">'
            '  <div>'
            '    <div class="workspace-shell__title">深度清洗执行中</div>'
            '    <div class="workspace-shell__caption">右侧正在执行词条治理；当前主工作面保持只读，等待清洗结果返回。</div>'
            '  </div>'
            '</div>',
            unsafe_allow_html=True,
        )
        _render_tm_stat_tiles(
            [
                ("当前语言对", lang_pair),
                ("总词条", f"{stats['total']:,}"),
                ("已固定", f"{stats['pinned']:,}"),
                ("可清洗", f"{stats['unpinned']:,}"),
            ]
        )
        st.markdown(
            '<div class="content-placeholder content-placeholder--workbench">清洗完成后，这里会自动切回词条工作区，并展示结果或待审建议。</div>',
            unsafe_allow_html=True,
        )


# ── 主页面 ────────────────────────────────────────────────────────────────────

def render_page(settings: AppSettings) -> AppSettings:
    _clean_state_init()
    _inject_tm_layout_styles()
    render_main_tooltip_support()
    clean_phase = st.session_state[_CLEAN_PHASE]
    is_cleaning = clean_phase == "running"

    # ── 跨 rerun toast 消息显示 ──────────────────────────
    if st.session_state.get("_pending_toast"):
        msg, icon = st.session_state.pop("_pending_toast")
        st.toast(msg, icon=icon)

    _render_tm_page_header(clean_phase)

    top_band = st.container(key="tm-top-band-shell")
    with top_band:
        top_row = st.columns([1.0, 1.02, 1.18], gap="small")

        with top_row[0]:
            control_shell = st.container(key="tm-control-shell")
            with control_shell:
                st.markdown(
                    '<div class="workspace-shell__header workspace-shell__header--compact">'
                    '  <div>'
                    '    <div class="workspace-shell__title">词库规则与语言范围</div>'
                    '  </div>'
                    '</div>',
                    unsafe_allow_html=True,
                )
                rule_col, lang_col = st.columns([1.0, 1.0], gap="small")
                with rule_col:
                    max_len_field = render_field_group(
                        rule_col,
                        key="tm-max-len",
                        label_html=build_tooltip_label_html(
                            "最长词上限",
                            "自动入库上限",
                            "定义允许自动进入记忆库的最长词条长度。",
                            [
                                "超过该值的长句将直接跳过，不再区分短词或长词。",
                                "建议按业务文本平均长度设置，优先保证词库可复用性。",
                                "手动新增词条不受这项自动入库规则影响。",
                            ],
                            trigger_class="ui-setting-subfield-label",
                        ),
                        hint="超过上限的文本不会自动写入记忆库。",
                    )
                    with max_len_field:
                        settings.tm.max_len = st.number_input(
                            "最长词上限输入",
                            min_value=1,
                            max_value=200,
                            value=settings.tm.max_len,
                            label_visibility="collapsed",
                        )

                with lang_col:
                    lang_scope_field = render_field_group(
                        lang_col,
                        key="tm-lang-scope",
                        label="工作语言对",
                    )
                    with lang_scope_field:
                        selected_lang = render_target_lang_selectbox(
                            settings,
                            state_prefix="tm",
                            label="语言对",
                        )
                    lang_pair = build_lang_pair(selected_lang)
                    with lang_scope_field:
                        st.markdown(
                            '<div class="ui-field-group__hint">'
                            f'当前检视范围：<span class="mono-inline">{html.escape(lang_pair)}</span>'
                            '</div>',
                            unsafe_allow_html=True,
                        )

        stats = tm_manager.get_stats(lang_pair)

        with top_row[1]:
            overview_shell = st.container(key="tm-overview-shell")
            with overview_shell:
                st.markdown(
                    '<div class="workspace-shell__header workspace-shell__header--compact">'
                    '  <div>'
                    '    <div class="workspace-shell__title">词库概览与交换</div>'
                    '  </div>'
                    '</div>',
                    unsafe_allow_html=True,
                )
                overview_body = st.container(key=f"tm-overview-body-{lang_pair}")
                with overview_body:
                    export_data, export_fn, export_mime = _build_export_data(lang_pair, "json")
                    actions_row = st.container(key=f"tm-overview-actions-{lang_pair}")
                    with actions_row:
                        exp_c, imp_c = st.columns(2, gap="small")
                        exp_c.download_button(
                            "导出词库",
                            data=export_data,
                            file_name=export_fn,
                            mime=export_mime,
                            key=f"export_btn_{lang_pair}",
                            use_container_width=True,
                        )
                        if imp_c.button(
                            "导入词库",
                            key=f"import_btn_{lang_pair}",
                            use_container_width=True,
                        ):
                            _import_dialog(lang_pair)

                    stats_row = st.container(key=f"tm-overview-stats-{lang_pair}")
                    with stats_row:
                        _render_tm_stat_tiles(
                            [
                                ("总词条", f"{stats['total']:,}"),
                                ("自动入库", f"{stats['auto']:,}"),
                                ("手动录入", f"{stats['manual']:,}"),
                                ("已固定", f"{stats['pinned']:,}"),
                            ],
                            compact=True,
                        )

        with top_row[2]:
            cleaner_shell = st.container(key="tm-cleaner-shell")
            settings = _render_cleaner_section(cleaner_shell, lang_pair, settings, is_running=is_cleaning)

    if is_cleaning:
        progress_shell = st.container(key="tm-progress-shell")
        with progress_shell:
            _render_cleaning_progress_fragment(lang_pair, settings)

    workspace = st.container(key="tm-workspace-shell")
    with workspace:
        st.markdown(
            '<div class="workspace-shell__header workspace-shell__header--compact">'
            '  <div>'
            '    <div class="workspace-shell__title">词条工作区</div>'
            '  </div>'
            '</div>',
            unsafe_allow_html=True,
        )
        if is_cleaning:
            st.markdown(
                '<div class="subtle-note subtle-note--status">深度清洗执行中，词库仍可浏览，但新增、删除、编辑和批量写入操作会暂时锁定。</div>',
                unsafe_allow_html=True,
            )

        applied_search_key = _tm_search_applied_key(lang_pair)
        input_search_key = _tm_search_input_key(lang_pair)
        page_key = _tm_page_state_key(lang_pair)

        if applied_search_key not in st.session_state:
            st.session_state[applied_search_key] = ""
        if input_search_key not in st.session_state:
            st.session_state[input_search_key] = st.session_state[applied_search_key]

        search_band = st.container(key=f"tm-search-band-{lang_pair}")
        with search_band:
            search_col, search_btn_col, add_col = st.columns([3.2, 1.05, 1.05], gap="small", vertical_alignment="top")
            with search_col:
                search_field = render_field_group(
                    search_col,
                    key=f"tm-search-{lang_pair}",
                    variant="single-layer",
                )
                with search_field:
                    st.text_input(
                        "搜索词条",
                        placeholder="输入原文、译文或关键词",
                        label_visibility="collapsed",
                        key=input_search_key,
                        on_change=_apply_tm_search,
                        args=(lang_pair,),
                    )
            with search_btn_col:
                if st.button(
                    "搜索",
                    key=f"search_btn_{lang_pair}",
                    use_container_width=True,
                ):
                    _apply_tm_search(lang_pair)
                    st.rerun()
            with add_col:
                if st.button(
                    "新增词条",
                    key=f"add_btn_{lang_pair}",
                    use_container_width=True,
                    disabled=is_cleaning,
                ):
                    st.session_state[f"tm_add_mode_{lang_pair}"] = True
                    st.rerun()

        if page_key not in st.session_state:
            st.session_state[page_key] = 1

        keyword = str(st.session_state.get(applied_search_key, "")).strip()

        PAGE_SIZE = 10
        rows, total = tm_manager.search_entries(
            lang_pair,
            keyword,
            page=st.session_state[page_key],
            page_size=PAGE_SIZE,
        )
        total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        pin_stats = tm_manager.get_pin_count(lang_pair, keyword)
        all_pinned = pin_stats["unpinned"] == 0 and pin_stats["pinned"] > 0

        _render_tm_scope_toolbar(
            lang_pair,
            keyword,
            total,
            pin_stats,
            all_pinned,
            disabled=is_cleaning,
        )

        if st.session_state.get(f"show_bulk_delete_{lang_pair}"):
            _bulk_delete_dialog(lang_pair, keyword)

        diff_key = f"clean_diff_{lang_pair}"
        if st.session_state.get(diff_key):
            _render_diff_confirm(diff_key, lang_pair)
        else:
            _render_tm_table(lang_pair, rows, disabled=is_cleaning)

            pager_shell = st.container(key="tm-pager-shell")
            with pager_shell:
                pg1, pg2, pg3 = st.columns([1.0, 2.6, 1.0], gap="small")
                with pg1:
                    if st.button(
                        "← 上页",
                        key=f"prev_{lang_pair}",
                        disabled=(st.session_state[page_key] <= 1),
                        use_container_width=True,
                    ):
                        st.session_state[page_key] -= 1
                        st.rerun()
                with pg2:
                    st.markdown(
                        '<div class="tm-pager-summary">'
                        f'第 {st.session_state[page_key]} / {total_pages} 页<span>共 {total:,} 条词条</span>'
                        '</div>',
                        unsafe_allow_html=True,
                    )
                with pg3:
                    if st.button(
                        "下页 →",
                        key=f"next_{lang_pair}",
                        disabled=(st.session_state[page_key] >= total_pages),
                        use_container_width=True,
                    ):
                        st.session_state[page_key] += 1
                        st.rerun()

    return settings


# ── 新增词条行 ────────────────────────────────────────────────────────────────

def _render_new_entry_row(lang_pair: str, *, disabled: bool = False):
    _render_tm_row_anchor("tm-table-row--new")
    nc1, nc2, nc3, nc4, nc5 = st.columns(_TM_TABLE_COLS, vertical_alignment="center")
    new_src = nc1.text_input(
        "新原文",
        placeholder="输入原文",
        key=f"new_src_{lang_pair}",
        label_visibility="collapsed",
    )
    new_tgt = nc2.text_input(
        "新译文",
        placeholder="输入译文",
        key=f"new_tgt_{lang_pair}",
        label_visibility="collapsed",
    )
    if nc3.button("✓", key=f"new_save_{lang_pair}",
                  use_container_width=True,
                  disabled=disabled):
        src = new_src.strip()
        tgt = new_tgt.strip()
        if src and tgt:
            tm_manager.insert_manual_entry(src, tgt, lang_pair)
            st.session_state.pop(f"tm_add_mode_{lang_pair}", None)
            st.session_state.pop(f"new_src_{lang_pair}", None)
            st.session_state.pop(f"new_tgt_{lang_pair}", None)
            st.rerun()
        else:
            st.toast("请同时填写原文和译文", icon="⚠️")
    if nc4.button("✕", key=f"new_cancel_{lang_pair}",
                  use_container_width=True,
                  disabled=disabled):
        st.session_state.pop(f"tm_add_mode_{lang_pair}", None)
        st.rerun()
    nc5.markdown("")


# ── 词条行 ────────────────────────────────────────────────────────────────────

def _render_entry_row(r: dict, lang_pair: str, *, disabled: bool = False):
    edit_key  = f"edit_{lang_pair}_{r['id']}"
    is_pinned = bool(r.get("pinned", 0))
    is_editing = bool(st.session_state.get(edit_key)) and not disabled
    row_class = "tm-table-row--edit" if is_editing else "tm-table-row--display"
    _render_tm_row_anchor(row_class)
    rc1, rc2, rc3, rc4, rc5 = st.columns(_TM_TABLE_COLS, vertical_alignment="center")

    if is_editing:
        # ── 编辑模式：原文和译文均可修改 ──
        new_src = rc1.text_input(
            "原文",
            value=r["source_text"],
            key=f"inp_src_{edit_key}",
            label_visibility="collapsed",
        )
        new_val = rc2.text_input(
            "译文",
            value=r["target_text"],
            key=f"inp_{edit_key}",
            label_visibility="collapsed",
        )
        if rc3.button("💾", key=f"save_{edit_key}",
                      use_container_width=True,
                      disabled=disabled):
            success = tm_manager.update_entry_full(r["id"], new_src, new_val)
            if success:
                del st.session_state[edit_key]
                st.rerun()
            else:
                st.toast("保存失败：原文与已有词条冲突", icon="❌")
        if rc4.button("✕", key=f"cancel_{edit_key}",
                      use_container_width=True,
                      disabled=disabled):
            del st.session_state[edit_key]
            st.rerun()
        rc5.markdown("")
    else:
        # ── 展示模式 ──
        _render_tm_cell_text(rc1, r["source_text"])
        _render_tm_cell_text(rc2, r["target_text"])

        # 编辑按钮：固定时禁用
        if rc3.button(
            "✎",
            key=f"btn_edit_{edit_key}",
            disabled=is_pinned or disabled,
            use_container_width=True,
        ):
            st.session_state[edit_key] = True
            st.rerun()

        # 删除按钮：固定时禁用
        if rc4.button(
            "🗑",
            key=f"btn_del_{lang_pair}_{r['id']}",
            disabled=is_pinned or disabled,
            use_container_width=True,
        ):
            tm_manager.delete_entry(r["id"])
            st.rerun()

        # 固定按钮：始终可操作
        pin_icon = "📌" if is_pinned else "📍"
        if rc5.button(pin_icon, key=f"btn_pin_{lang_pair}_{r['id']}",
                      use_container_width=True,
                      disabled=disabled):
            tm_manager.pin_entry(r["id"], not is_pinned)
            st.rerun()


def _render_tm_header_row() -> None:
    _render_tm_row_anchor("tm-table-row--header")
    hc1, hc2, hc3, hc4, hc5 = st.columns(_TM_TABLE_COLS, vertical_alignment="center")
    hc1.markdown('<p class="tm-hdr">原文</p>', unsafe_allow_html=True)
    hc2.markdown('<p class="tm-hdr">译文</p>', unsafe_allow_html=True)
    hc3.markdown('<p class="tm-hdr tm-hdr-action">编辑</p>', unsafe_allow_html=True)
    hc4.markdown('<p class="tm-hdr tm-hdr-action">删除</p>', unsafe_allow_html=True)
    hc5.markdown('<p class="tm-hdr tm-hdr-action">固定</p>', unsafe_allow_html=True)


def _render_tm_table(lang_pair: str, rows: list[dict], *, disabled: bool = False) -> None:
    with st.container(key="tm-table-shell"):
        st.markdown('<span class="tm-table-scroll-anchor" style="display:none"></span>', unsafe_allow_html=True)
        with st.container():
            _render_tm_header_row()

            if st.session_state.get(f"tm_add_mode_{lang_pair}") and not disabled:
                _render_new_entry_row(lang_pair, disabled=disabled)

            if rows:
                for r in rows:
                    _render_entry_row(r, lang_pair, disabled=disabled)
            elif not st.session_state.get(f"tm_add_mode_{lang_pair}") or disabled:
                st.markdown(
                    '<div class="content-placeholder tm-table-empty">暂无词条。翻译完成后会自动积累。</div>',
                    unsafe_allow_html=True,
                )


# ── 深度清洗设置（固定显示）──────────────────────────────

def _render_cleaner_section(
    container,
    lang_pair: str,
    settings: AppSettings,
    is_running: bool = False,
) -> AppSettings:
    mode_map = {"差异确认模式": "diff", "直接覆写模式": "overwrite"}
    mode_rev = {v: k for k, v in mode_map.items()}
    eng_labels = list(CLOUD_ENGINES.keys())
    eng_keys = list(CLOUD_ENGINES.values())
    eng_idx = eng_keys.index(settings.cleaner_engine) if settings.cleaner_engine in eng_keys else 0

    cleaner_body = container.container(key=f"tm-cleaner-body-{lang_pair}")
    with cleaner_body:
        auto_pin_key = f"auto_pin_{lang_pair}"
        if auto_pin_key not in st.session_state:
            st.session_state[auto_pin_key] = settings.auto_pin_after_clean

        cleaner_header_row = cleaner_body.container(key=f"tm-cleaner-header-row-{lang_pair}")
        with cleaner_header_row:
            title_col, toggle_col = st.columns(
                [1.02, 1.28],
                gap="small",
                vertical_alignment="center",
            )
            with title_col:
                st.markdown(
                    '<div class="workspace-shell__title">深度清洗</div>',
                    unsafe_allow_html=True,
                )
            with toggle_col:
                toggle_label_col, toggle_switch_col = st.columns(
                    [1.75, 0.65],
                    gap="small",
                    vertical_alignment="center",
                )
                with toggle_label_col:
                    st.markdown(
                        '<div class="tm-cleaner-inline-toggle__label">清洗后自动固定</div>',
                        unsafe_allow_html=True,
                    )
                with toggle_switch_col:
                    settings.auto_pin_after_clean = st.toggle(
                        "清洗后自动固定开关",
                        key=auto_pin_key,
                        disabled=is_running,
                        label_visibility="collapsed",
                        help="将本次已写入的清洗结果立即设为固定词条，减少重复清洗并保护已确认译文。",
                    )

        cleaner_fields = cleaner_body.container(key=f"tm-cleaner-fields-{lang_pair}")
        with cleaner_fields:
            hl1, hl2, hl3 = st.columns([1.18, 1.08, 0.82], gap="small")
            with hl1:
                clean_mode_field = render_field_group(
                    hl1,
                    key=f"tm-clean-mode-{lang_pair}",
                    label_html=build_tooltip_label_html(
                        "清洗模式",
                        "深度清洗模式",
                        "定义 AI 清洗建议的落库方式，平衡风险与效率。",
                        [
                            "差异确认模式：逐条审核后再写入，适合谨慎校对。",
                            "直接覆写模式：自动写入建议，效率更高但不可撤销。",
                            "建议先从差异确认模式开始，稳定后再切换覆写。",
                        ],
                        trigger_class="ui-setting-subfield-label",
                    ),
                )
                with clean_mode_field:
                    mode_sel = st.selectbox(
                        "清洗模式选择",
                        list(mode_map.keys()),
                        index=list(mode_map.keys()).index(mode_rev.get(settings.cleaner_mode, "差异确认模式")),
                        key=f"cmode_{lang_pair}",
                        disabled=is_running,
                        label_visibility="collapsed",
                    )
                settings.cleaner_mode = mode_map[mode_sel]

            with hl2:
                cleaner_engine_field = render_field_group(
                    hl2,
                    key=f"tm-clean-engine-{lang_pair}",
                    label="清洗引擎",
                )
                with cleaner_engine_field:
                    eng_sel = st.selectbox(
                        "清洗引擎",
                        eng_labels,
                        index=eng_idx,
                        key=f"ceng_{lang_pair}",
                        disabled=is_running,
                        label_visibility="collapsed",
                    )
                settings.cleaner_engine = CLOUD_ENGINES[eng_sel]

            with hl3:
                cleaner_model_field = render_field_group(
                    hl3,
                    key=f"tm-clean-model-{lang_pair}",
                    label="模型",
                )
                with cleaner_model_field:
                    settings.cleaner_model = st.text_input(
                        "模型",
                        value=settings.cleaner_model,
                        placeholder="同翻译引擎",
                        key=f"cmodel_{lang_pair}",
                        disabled=is_running,
                        label_visibility="collapsed",
                    )

        cleaner_actions = cleaner_body.container(key=f"tm-cleaner-actions-{lang_pair}")
        with cleaner_actions:
            btn_run, btn_prompt = st.columns(2, gap="small")
            with btn_run:
                if is_running:
                    if st.button("中止清洗", type="secondary", key=f"clean_stop_{lang_pair}", use_container_width=True):
                        cancel_event = st.session_state.get(_CLEAN_CANCEL)
                        if cancel_event is not None:
                            cancel_event.set()
                        st.session_state["_pending_toast"] = ("已发送中止请求，等待当前批次结束...", "⚠️")
                        st.rerun()
                else:
                    if st.button("启动深度清洗", type="secondary", key=f"clean_{lang_pair}", use_container_width=True):
                        _launch_cleaning(lang_pair, settings)
            with btn_prompt:
                if st.button(
                    "编辑清洗提示词",
                    key=f"cleaner_prompt_btn_{lang_pair}",
                    use_container_width=True,
                    disabled=is_running,
                    help="查看并编辑深度清洗时发送给模型的提示词",
                ):
                    _open_cleaner_prompt_dialog(settings, lang_pair)

    if st.session_state.get(_CLEANER_PROMPT_OPEN_KEY):
        _render_cleaner_prompt_dialog(settings, lang_pair)

    return settings


def _launch_cleaning(lang_pair: str, settings: AppSettings):
    """启动清洗后台线程，立即切换到 running 状态并 rerun。
    引擎初始化失败时也切换状态，错误通过 res_q 传递给进度渲染器显示。
    """
    from core.engine_dispatcher import build_engine
    from core.tm_cleaner import run_cleaning

    prog_q = _queue.Queue()
    res_q  = _queue.Queue()
    cancel_event = threading.Event()

    # 先切换状态、保存队列，无论引擎是否初始化成功
    st.session_state[_CLEAN_THREAD] = None
    st.session_state[_CLEAN_PROG_Q] = prog_q
    st.session_state[_CLEAN_RES_Q]  = res_q
    st.session_state[_CLEAN_PROG]   = None
    st.session_state[_CLEAN_RESULT] = None
    st.session_state[_CLEAN_PHASE]  = "running"
    st.session_state[_CLEAN_CANCEL] = cancel_event

    temp = settings.model_copy(deep=True)
    temp.engine.cloud_provider = settings.cleaner_engine
    temp.engine.cloud_model    = settings.cleaner_model or settings.engine.cloud_model
    temp.engine.mode           = "cloud"

    try:
        engine = build_engine(temp)
    except Exception as e:
        # 引擎初始化失败：写入 res_q，进度渲染器会显示错误
        res_q.put(("err", f"清洗引擎初始化失败：{e}"))
        st.rerun()
        return

    def _worker():
        try:
            def _cb(*args):
                if not args:
                    return
                prog_q.put(args[0] if len(args) == 1 else tuple(args))
            suggestions = run_cleaning(
                lang_pair, engine,
                batch_size=settings.engine.batch_size,
                concurrency=settings.engine.concurrency,
                progress_callback=_cb,
                extra_prompt=_get_cleaner_prompt_extra(settings, lang_pair),
                full_override_prompt=_get_cleaner_full_prompt_override(settings, lang_pair),
                custom_target_langs=settings.custom_target_langs,
                cancel_event=cancel_event,
            )
            if cancel_event.is_set():
                res_q.put(("stopped", None))
            else:
                res_q.put(("ok", suggestions))
        except Exception as e:
            res_q.put(("err", str(e)))

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    st.session_state[_CLEAN_THREAD] = t
    st.rerun()


def _run_cleaning(lang_pair: str, settings: AppSettings):
    """向后兼容保留，直接委托给 _launch_cleaning。"""
    _launch_cleaning(lang_pair, settings)


@st.fragment(run_every=1.0)
def _cleaning_progress_frag():
    """模块级 fragment，每 0.5 秒自动轮询清洗进度。
    参数通过 session_state 传入：_clean_frag_lang_pair / _clean_frag_mode / _clean_frag_auto_pin
    """
    phase = st.session_state.get(_CLEAN_PHASE, "idle")
    if phase != "running":
        # 清洗已结束，触发整页 rerun 切回 idle 界面
        st.rerun()
        return

    prog_q: _queue.Queue    = st.session_state.get(_CLEAN_PROG_Q)
    res_q:  _queue.Queue    = st.session_state.get(_CLEAN_RES_Q)

    if prog_q is None or res_q is None:
        st.warning("清洗队列异常，请重试。")
        return

    lang_pair = st.session_state.get("_clean_frag_lang_pair", "")
    cleaner_mode  = st.session_state.get("_clean_frag_mode", "diff")
    auto_pin      = st.session_state.get("_clean_frag_auto_pin", False)

    # 排空进度队列，取最新进度
    latest_prog = None
    while not prog_q.empty():
        try:
            latest_prog = prog_q.get_nowait()
        except _queue.Empty:
            break
    if latest_prog is not None:
        st.session_state[_CLEAN_PROG] = latest_prog

    # 检查结果队列
    result = None
    try:
        result = res_q.get_nowait()
        st.session_state[_CLEAN_RESULT] = result
        st.session_state[_CLEAN_PHASE]  = "idle"
    except _queue.Empty:
        pass

    # ── 渲染进度 UI ──
    st.markdown(
        '<div class="workspace-shell__header workspace-shell__header--compact">'
        '  <div>'
        '    <div class="workspace-shell__title">清洗进度</div>'
        '    <div class="workspace-shell__caption">进度会持续轮询；完成后会自动切回主词表工作区。</div>'
        '  </div>'
        '</div>',
        unsafe_allow_html=True,
    )
    prog = _normalize_clean_progress_payload(st.session_state.get(_CLEAN_PROG))
    if prog:
        progress_value, progress_text, progress_caption = _build_clean_progress_display(prog)
        st.progress(progress_value, text=progress_text)
        mc1, mc2, mc3, mc4 = st.columns(4, gap="small")
        mc1.metric("已处理词条", f"{prog['completed_entries']}")
        mc2.metric("总词条数", f"{prog['total_entries']}")
        mc3.metric("已完成批次", f"{prog['completed_batches']}")
        mc4.metric("总批次数", f"{prog['total_batches']}")
        if progress_caption:
            st.caption(progress_caption)
    else:
        st.progress(0.0, text="初始化清洗任务，请稍候...")

    st.markdown(
        '<div class="subtle-note">清洗执行期间请保持在当前页面，结果会在完成后自动回填。</div>',
        unsafe_allow_html=True,
    )

    # 如果已拿到结果，处理后续逻辑并触发整页 rerun
    # 不使用 time.sleep() 阻塞渲染线程，改用 _pending_toast 机制跨 rerun 传递消息
    if result is not None:
        from core.tm_cleaner import apply_suggestions
        status, payload = result
        if status == "err":
            st.session_state["_pending_toast"] = (f"清洗失败：{payload}", "❌")
        elif status == "stopped":
            st.session_state["_pending_toast"] = ("清洗已中止，未写入任何建议", "⚠️")
        else:
            suggestions = payload
            if not suggestions:
                _clear_diff_checkbox_state(lang_pair)
                st.session_state["_pending_toast"] = ("清洗完成，未发现需要修改的词条", "✅")
            elif cleaner_mode == "overwrite":
                _clear_diff_checkbox_state(lang_pair)
                count = apply_suggestions(suggestions, auto_pin=auto_pin)
                st.session_state["_pending_toast"] = (f"清洗完成，已直接覆写 {count} 条词条", "✅")
            else:
                _clear_diff_checkbox_state(lang_pair)
                st.session_state[f"clean_diff_{lang_pair}"]     = suggestions
                st.session_state[f"clean_auto_pin_{lang_pair}"] = auto_pin
                st.session_state["_pending_toast"] = (
                    f"清洗完成，发现 {len(suggestions)} 处建议修改，请在下方确认", "🔍"
                )
        st.rerun()


def _render_cleaning_progress_fragment(lang_pair: str, settings: AppSettings):
    """将 lang_pair / settings 关键字段存入 session_state，然后调用模块级 fragment。"""
    # 把渲染所需参数写入 session_state，供模块级 fragment 读取
    st.session_state["_clean_frag_lang_pair"]  = lang_pair
    st.session_state["_clean_frag_mode"]       = settings.cleaner_mode
    st.session_state["_clean_frag_auto_pin"]   = settings.auto_pin_after_clean
    _cleaning_progress_frag()


def _render_diff_confirm(diff_key: str, lang_pair: str):
    from core.tm_cleaner import apply_suggestions, CleanSuggestion
    suggestions: list[CleanSuggestion] = st.session_state[diff_key]
    auto_pin = st.session_state.get(f"clean_auto_pin_{lang_pair}", False)

    st.markdown(
        '<div class="workspace-shell__header">'
        '  <div>'
        f'    <div class="workspace-shell__title">清洗建议审阅</div>'
        f'    <div class="workspace-shell__caption">当前语言对共发现 {len(suggestions)} 处建议修改。请在写入前确认需要保留的变更。</div>'
        '  </div>'
        '</div>',
        unsafe_allow_html=True,
    )
    h1, h2, h3, h4 = st.columns([1, 3, 3, 3])
    h1.markdown("**接受**")
    h2.markdown("**原文**")
    h3.markdown("**旧译文**")
    h4.markdown("**建议译文**")

    for i, s in enumerate(suggestions):
        row_alt = (i % 2) == 1
        checkbox_key = _diff_checkbox_key(lang_pair, s.entry_id)
        if checkbox_key not in st.session_state:
            st.session_state[checkbox_key] = s.accepted
        c1, c2, c3, c4 = st.columns([1, 3, 3, 3])
        s.accepted = c1.checkbox("", key=checkbox_key)
        _render_tm_diff_text(c2, s.source_text, alternate=row_alt)
        _render_tm_diff_text(c3, s.old_target, alternate=row_alt)
        _render_tm_diff_text(c4, s.new_target, emphasize=True, alternate=row_alt)

    accepted_n = sum(1 for s in suggestions if s.accepted)
    entry_ids = [s.entry_id for s in suggestions]
    ba, bn, bc = st.columns([1, 1, 2])
    ba.button(
        "全选",
        key=f"all_{lang_pair}",
        on_click=_set_diff_checkbox_state,
        args=[lang_pair, entry_ids, True],
    )
    bn.button(
        "全不选",
        key=f"none_{lang_pair}",
        on_click=_set_diff_checkbox_state,
        args=[lang_pair, entry_ids, False],
    )
    if bc.button(f"确认写入（{accepted_n} 条）", type="secondary", key=f"confirm_{lang_pair}"):
        count = apply_suggestions(suggestions, auto_pin=auto_pin)
        pin_note = "，并已自动固定" if auto_pin and count else ""
        st.success(f"已写入 {count} 条{pin_note}。")
        _clear_diff_checkbox_state(lang_pair)
        del st.session_state[diff_key]
        st.session_state.pop(f"clean_auto_pin_{lang_pair}", None)
        st.rerun()
