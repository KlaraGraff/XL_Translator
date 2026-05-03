"""Shared target-language selectbox and custom-language management dialog."""

from __future__ import annotations

from typing import TYPE_CHECKING

import streamlit as st

from core import tm_manager
from core.language_registry import (
    CUSTOM_TARGET_LANG_ACTION,
    CUSTOM_TARGET_LANG_ACTION_LABEL,
    append_custom_target_lang,
    build_lang_pair,
    get_first_available_target_lang,
    get_ordered_target_lang_codes,
    get_saved_custom_target_lang_entries,
    get_target_lang_display,
    is_supported_target_lang,
    remember_recent_target_lang,
    remove_custom_target_lang,
    remove_recent_target_lang,
    update_custom_target_lang_description,
)

if TYPE_CHECKING:
    from settings import AppSettings


_DESCRIPTION_PLACEHOLDER = (
    "可选。用于说明这种语言主要在哪些地区使用、由哪些人使用，或它与常见别名的关系。"
    "例如：主要在柬埔寨使用，是当地通用语言。这里的“高棉语”对应柬埔寨官方语言。"
)


def _state_key(state_prefix: str, suffix: str) -> str:
    return f"{state_prefix}_{suffix}"


def _set_selected_target_lang(state_prefix: str, target_lang: str) -> None:
    st.session_state[_state_key(state_prefix, "selected_target_lang")] = target_lang


def _queue_selectbox_sync(state_prefix: str, target_lang: str) -> None:
    st.session_state[_state_key(state_prefix, "target_lang_select_pending")] = target_lang


def _mark_target_lang_user_selected(state_prefix: str) -> None:
    st.session_state[_state_key(state_prefix, "target_lang_user_selected")] = True


def was_target_lang_manually_selected(state_prefix: str) -> bool:
    return bool(st.session_state.get(_state_key(state_prefix, "target_lang_user_selected"), False))


def _queue_custom_target_lang_form_clear(state_prefix: str) -> None:
    st.session_state[_state_key(state_prefix, "custom_target_lang_input_pending_clear")] = True
    st.session_state[
        _state_key(state_prefix, "custom_target_lang_description_pending_clear")
    ] = True


def _consume_pending_clear(state_prefix: str, suffix: str, widget_key: str) -> None:
    pending_key = _state_key(state_prefix, suffix)
    if st.session_state.pop(pending_key, False):
        st.session_state[widget_key] = ""


def _seed_widget_value(widget_key: str, value: str) -> None:
    normalized_value = str(value or "")
    seed_key = f"{widget_key}__seed"
    if widget_key not in st.session_state or st.session_state.get(seed_key) != normalized_value:
        st.session_state[widget_key] = normalized_value
        st.session_state[seed_key] = normalized_value


def _open_custom_target_lang_dialog(state_prefix: str) -> None:
    st.session_state[_state_key(state_prefix, "custom_target_lang_dialog_open")] = True


def _close_custom_target_lang_dialog(state_prefix: str) -> None:
    _queue_custom_target_lang_form_clear(state_prefix)
    for suffix in (
        "custom_target_lang_dialog_open",
        "custom_target_lang_error",
        "custom_target_lang_pending_purge",
    ):
        st.session_state.pop(_state_key(state_prefix, suffix), None)


def _cleanup_custom_target_lang_entry_state(state_prefix: str, target_lang: str) -> None:
    for suffix in (
        f"custom_target_lang_desc_editor_{target_lang}",
        f"custom_target_lang_desc_editor_{target_lang}__seed",
    ):
        st.session_state.pop(_state_key(state_prefix, suffix), None)


def _select_target_lang(
    settings: "AppSettings",
    state_prefix: str,
    target_lang: str,
    *,
    sync_widget: bool = False,
    include_optional_target_langs: bool = False,
) -> None:
    settings.target_lang = target_lang
    settings.recent_target_langs = remember_recent_target_lang(
        settings.recent_target_langs,
        target_lang,
        settings.custom_target_langs,
        include_optional=include_optional_target_langs,
    )
    _set_selected_target_lang(state_prefix, target_lang)
    if sync_widget:
        _queue_selectbox_sync(state_prefix, target_lang)


def _reset_selected_target_lang_if_needed(
    settings: "AppSettings",
    state_prefix: str,
    *,
    sync_widget: bool = False,
    include_optional_target_langs: bool = False,
) -> None:
    if is_supported_target_lang(
        settings.target_lang,
        settings.custom_target_langs,
        include_optional=include_optional_target_langs,
    ):
        _set_selected_target_lang(state_prefix, settings.target_lang)
        if sync_widget:
            _queue_selectbox_sync(state_prefix, settings.target_lang)
        return

    fallback_target_lang = get_first_available_target_lang(
        settings.recent_target_langs,
        settings.custom_target_langs,
        include_optional=include_optional_target_langs,
    )
    settings.target_lang = fallback_target_lang
    settings.recent_target_langs = remember_recent_target_lang(
        settings.recent_target_langs,
        fallback_target_lang,
        settings.custom_target_langs,
        include_optional=include_optional_target_langs,
    )
    _select_target_lang(
        settings,
        state_prefix,
        fallback_target_lang,
        sync_widget=sync_widget,
        include_optional_target_langs=include_optional_target_langs,
    )


def _remove_custom_target_lang_entry(
    settings: "AppSettings",
    state_prefix: str,
    target_lang: str,
    *,
    purge: bool,
    include_optional_target_langs: bool = False,
) -> int:
    settings.custom_target_langs = remove_custom_target_lang(
        settings.custom_target_langs,
        target_lang,
    )
    settings.recent_target_langs = remove_recent_target_lang(
        settings.recent_target_langs,
        target_lang,
        settings.custom_target_langs,
        include_optional=include_optional_target_langs,
    )

    deleted_tm_entries = 0
    if purge:
        lang_pair = build_lang_pair(target_lang)
        deleted_tm_entries = tm_manager.delete_all_entries(lang_pair)
        settings.cleaner_prompt_extras.pop(lang_pair, None)
        settings.cleaner_full_prompt_overrides.pop(lang_pair, None)

    _cleanup_custom_target_lang_entry_state(state_prefix, target_lang)
    _reset_selected_target_lang_if_needed(
        settings,
        state_prefix,
        sync_widget=True,
        include_optional_target_langs=include_optional_target_langs,
    )
    st.session_state.pop(_state_key(state_prefix, "custom_target_lang_pending_purge"), None)
    return deleted_tm_entries


@st.dialog("自定义语言")
def _render_custom_target_lang_dialog(
    settings: "AppSettings",
    state_prefix: str,
    include_optional_target_langs: bool = False,
) -> None:
    error_key = _state_key(state_prefix, "custom_target_lang_error")
    name_input_key = _state_key(state_prefix, "custom_target_lang_input")
    description_input_key = _state_key(state_prefix, "custom_target_lang_description")
    pending_purge_key = _state_key(state_prefix, "custom_target_lang_pending_purge")

    st.warning(
        "自定义语言会直接把你输入的语言名称写入提示词。模型通常能处理很多语言，但对未内置语言的支持稳定性不做保证。",
        icon="⚠️",
    )
    st.caption("名称决定语种身份；语言说明仅用于帮助模型识别这种语言，可留空。")

    pending_purge_target_lang = st.session_state.get(pending_purge_key)
    if pending_purge_target_lang:
        pending_display = get_target_lang_display(
            pending_purge_target_lang,
            settings.custom_target_langs,
        )
        st.error(
            f"即将彻底删除“{pending_display}”。该操作会移除语言条目、相关 TM 词库，以及该语言对应的清洗提示词配置。",
            icon="🗑️",
        )
        confirm_col, cancel_col = st.columns(2)
        if confirm_col.button(
            "确认彻底删除",
            type="secondary",
            key=_state_key(state_prefix, "custom_target_lang_confirm_purge"),
            use_container_width=True,
        ):
            deleted_tm_entries = _remove_custom_target_lang_entry(
                settings,
                state_prefix,
                pending_purge_target_lang,
                purge=True,
                include_optional_target_langs=include_optional_target_langs,
            )
            st.session_state.pop(error_key, None)
            st.toast(
                f"已彻底删除“{pending_display}”，同步清理 {deleted_tm_entries} 条 TM 记录。",
                icon="✅",
            )
            st.rerun()
        if cancel_col.button(
            "取消",
            key=_state_key(state_prefix, "custom_target_lang_cancel_purge"),
            use_container_width=True,
        ):
            st.session_state.pop(pending_purge_key, None)
            st.rerun()
        st.markdown("---")

    if st.session_state.get(error_key):
        st.error(st.session_state[error_key])

    st.markdown("**新增自定义语言**")
    _consume_pending_clear(
        state_prefix,
        "custom_target_lang_input_pending_clear",
        name_input_key,
    )
    _consume_pending_clear(
        state_prefix,
        "custom_target_lang_description_pending_clear",
        description_input_key,
    )
    st.text_input(
        "自定义语言名称",
        key=name_input_key,
        placeholder="输入语言名称，例如：波斯尼亚语",
        label_visibility="collapsed",
    )
    st.text_area(
        "自定义语言说明",
        key=description_input_key,
        placeholder=_DESCRIPTION_PLACEHOLDER,
        height=110,
        label_visibility="collapsed",
    )

    add_col, close_col = st.columns([1.4, 1])
    if add_col.button(
        "添加并使用",
        type="secondary",
        key=_state_key(state_prefix, "custom_target_lang_add"),
        use_container_width=True,
    ):
        display_name = st.session_state.get(name_input_key, "")
        description = st.session_state.get(description_input_key, "")
        try:
            settings.custom_target_langs, target_lang = append_custom_target_lang(
                settings.custom_target_langs,
                display_name,
                description,
            )
        except ValueError as exc:
            st.session_state[error_key] = str(exc)
            st.rerun()
        else:
            st.session_state.pop(error_key, None)
            _select_target_lang(
                settings,
                state_prefix,
                target_lang,
                sync_widget=True,
                include_optional_target_langs=include_optional_target_langs,
            )
            _close_custom_target_lang_dialog(state_prefix)
            st.toast(f"已添加自定义语言：{get_target_lang_display(target_lang)}", icon="✅")
            st.rerun()
    if close_col.button(
        "关闭",
        key=_state_key(state_prefix, "custom_target_lang_close"),
        use_container_width=True,
    ):
        _close_custom_target_lang_dialog(state_prefix)
        st.rerun()

    st.markdown("---")
    st.markdown("**已保存语言**")
    saved_entries = get_saved_custom_target_lang_entries(settings.custom_target_langs)
    if not saved_entries:
        st.caption("还没有保存的自定义语言。")
        return

    for target_lang, display_name, description in saved_entries:
        is_selected = settings.target_lang == target_lang
        expander_label = display_name + ("  当前使用" if is_selected else "")
        with st.expander(expander_label, expanded=False):
            action_use_col, action_remove_col, action_purge_col = st.columns([1.1, 1, 1])
            if action_use_col.button(
                "使用",
                key=_state_key(state_prefix, f"custom_target_lang_use_{target_lang}"),
                use_container_width=True,
            ):
                st.session_state.pop(error_key, None)
                _select_target_lang(
                    settings,
                    state_prefix,
                    target_lang,
                    sync_widget=True,
                    include_optional_target_langs=include_optional_target_langs,
                )
                _close_custom_target_lang_dialog(state_prefix)
                st.rerun()

            if action_remove_col.button(
                "移出",
                key=_state_key(state_prefix, f"custom_target_lang_remove_{target_lang}"),
                use_container_width=True,
                help="仅从语言列表中移除，不删除该语言已有的 TM 或清洗配置。",
            ):
                st.session_state.pop(error_key, None)
                _remove_custom_target_lang_entry(
                    settings,
                    state_prefix,
                    target_lang,
                    purge=False,
                    include_optional_target_langs=include_optional_target_langs,
                )
                st.toast(f"已从列表移出“{display_name}”", icon="✅")
                st.rerun()

            if action_purge_col.button(
                "彻删",
                key=_state_key(state_prefix, f"custom_target_lang_purge_{target_lang}"),
                use_container_width=True,
                help="删除语言条目，并同步清理该语言相关 TM 与清洗配置。",
            ):
                st.session_state[pending_purge_key] = target_lang
                st.rerun()

            description_editor_key = _state_key(
                state_prefix,
                f"custom_target_lang_desc_editor_{target_lang}",
            )
            _seed_widget_value(description_editor_key, description)
            st.text_area(
                "语言说明",
                key=description_editor_key,
                placeholder=_DESCRIPTION_PLACEHOLDER,
                height=110,
                label_visibility="collapsed",
            )
            save_col, _ = st.columns([1.2, 2.8])
            if save_col.button(
                "保存说明",
                key=_state_key(state_prefix, f"custom_target_lang_save_desc_{target_lang}"),
                type="secondary",
                use_container_width=True,
            ):
                settings.custom_target_langs = update_custom_target_lang_description(
                    settings.custom_target_langs,
                    target_lang,
                    st.session_state.get(description_editor_key, ""),
                )
                st.session_state.pop(error_key, None)
                st.toast(f"已更新“{display_name}”的语言说明", icon="✅")
                st.rerun()


def render_target_lang_selectbox(
    settings: "AppSettings",
    *,
    state_prefix: str,
    label: str,
    disabled: bool = False,
    include_optional_target_langs: bool = False,
) -> str:
    selected_key = _state_key(state_prefix, "selected_target_lang")
    selectbox_key = _state_key(state_prefix, "target_lang_select")
    pending_selectbox_key = _state_key(state_prefix, "target_lang_select_pending")
    user_selected_key = _state_key(state_prefix, "target_lang_user_selected")

    default_target_lang = get_first_available_target_lang(
        settings.recent_target_langs,
        settings.custom_target_langs,
        include_optional=include_optional_target_langs,
    )

    if selected_key not in st.session_state:
        initial_target_lang = (
            settings.target_lang
            if is_supported_target_lang(
                settings.target_lang,
                settings.custom_target_langs,
                include_optional=include_optional_target_langs,
            )
            else default_target_lang
        )
        st.session_state[selected_key] = initial_target_lang

    if selectbox_key not in st.session_state:
        st.session_state[selectbox_key] = st.session_state[selected_key]
    if user_selected_key not in st.session_state:
        st.session_state[user_selected_key] = False

    if (
        is_supported_target_lang(
            settings.target_lang,
            settings.custom_target_langs,
            include_optional=include_optional_target_langs,
        )
        and settings.target_lang != st.session_state.get(selected_key)
    ):
        _set_selected_target_lang(state_prefix, settings.target_lang)
        _queue_selectbox_sync(state_prefix, settings.target_lang)

    ordered_codes = get_ordered_target_lang_codes(
        settings.recent_target_langs,
        settings.custom_target_langs,
        include_optional=include_optional_target_langs,
    )
    selected_target_lang = st.session_state.get(selected_key, default_target_lang)
    if selected_target_lang not in ordered_codes:
        _set_selected_target_lang(state_prefix, default_target_lang)
        _queue_selectbox_sync(state_prefix, default_target_lang)
        selected_target_lang = default_target_lang

    pending_selectbox_value = st.session_state.pop(pending_selectbox_key, None)
    if pending_selectbox_value in ordered_codes:
        st.session_state[selectbox_key] = pending_selectbox_value
    elif (
        selectbox_key not in st.session_state
        or st.session_state.get(selectbox_key)
        not in set(ordered_codes + [CUSTOM_TARGET_LANG_ACTION])
    ):
        st.session_state[selectbox_key] = selected_target_lang

    if st.session_state.get(selectbox_key) == CUSTOM_TARGET_LANG_ACTION:
        _open_custom_target_lang_dialog(state_prefix)
        st.session_state[selectbox_key] = selected_target_lang

    options = ordered_codes + [CUSTOM_TARGET_LANG_ACTION]
    # KNOWN-ISSUE-UI-001:
    # Alignment around this selectbox is tracked in docs/KNOWN_ISSUES.md.
    chosen_target_lang = st.selectbox(
        label,
        options=options,
        index=options.index(selected_target_lang),
        format_func=lambda option: (
            CUSTOM_TARGET_LANG_ACTION_LABEL
            if option == CUSTOM_TARGET_LANG_ACTION
            else get_target_lang_display(
                option,
                settings.custom_target_langs,
                include_optional=include_optional_target_langs,
            )
        ),
        label_visibility="collapsed",
        disabled=disabled,
        key=selectbox_key,
        on_change=_mark_target_lang_user_selected,
        args=(state_prefix,),
    )

    if chosen_target_lang == CUSTOM_TARGET_LANG_ACTION:
        _open_custom_target_lang_dialog(state_prefix)
        chosen_target_lang = selected_target_lang

    _select_target_lang(
        settings,
        state_prefix,
        chosen_target_lang,
        include_optional_target_langs=include_optional_target_langs,
    )

    if st.session_state.get(_state_key(state_prefix, "custom_target_lang_dialog_open")):
        _render_custom_target_lang_dialog(
            settings,
            state_prefix,
            include_optional_target_langs=include_optional_target_langs,
        )

    return chosen_target_lang
