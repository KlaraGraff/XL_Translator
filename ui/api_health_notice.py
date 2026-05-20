"""Top-center API health notice for app startup checks."""

from __future__ import annotations

import html
import time
from datetime import date

import streamlit as st

from core.api_health import (
    API_HEALTH_FAILED,
    API_HEALTH_NOTICE_TTL_SECONDS,
    ApiHealthRecord,
    build_connectivity_signature,
    load_api_health_record,
    run_api_health_check,
    should_check_api_health_on_startup,
)
from settings import AppSettings


_AUTO_CHECKED_KEY = "_api_health_auto_checked_decisions"
_NOTICE_ID_KEY = "_api_health_notice_id"
_NOTICE_SHOWN_AT_KEY = "_api_health_notice_shown_at"
_NOTICE_DISMISSED_ID_KEY = "_api_health_notice_dismissed_id"


def _get_auto_checked_decisions() -> dict[str, bool]:
    checked = st.session_state.get(_AUTO_CHECKED_KEY)
    if not isinstance(checked, dict):
        checked = {}
        st.session_state[_AUTO_CHECKED_KEY] = checked
    return checked


def _decision_id(signature: str, today: date) -> str:
    return f"{signature}|{today.isoformat()}"


def _mark_checked_for_session(signature: str, today: date) -> None:
    checked = _get_auto_checked_decisions()
    checked[_decision_id(signature, today)] = True


def _maybe_run_startup_check(settings: AppSettings, today: date) -> ApiHealthRecord | None:
    record = load_api_health_record()
    signature = build_connectivity_signature(settings)
    decision = _decision_id(signature, today)

    if (
        should_check_api_health_on_startup(settings, record=record, today=today)
        and not _get_auto_checked_decisions().get(decision)
    ):
        record = run_api_health_check(settings, today=today)
        _mark_checked_for_session(signature, today)

    return record


def _notice_id(record: ApiHealthRecord) -> str:
    return "|".join(
        [
            record.signature,
            record.checked_at,
            record.result_status,
            record.message,
        ]
    )


def _prepare_notice_state(record: ApiHealthRecord) -> str:
    current_id = _notice_id(record)
    if st.session_state.get(_NOTICE_ID_KEY) != current_id:
        st.session_state[_NOTICE_ID_KEY] = current_id
        st.session_state[_NOTICE_SHOWN_AT_KEY] = time.monotonic()
        if st.session_state.get(_NOTICE_DISMISSED_ID_KEY) != current_id:
            st.session_state.pop(_NOTICE_DISMISSED_ID_KEY, None)
    return current_id


def _should_show_notice(record: ApiHealthRecord, signature: str) -> bool:
    if record.signature != signature or record.last_status != API_HEALTH_FAILED:
        return False

    current_id = _prepare_notice_state(record)
    if st.session_state.get(_NOTICE_DISMISSED_ID_KEY) == current_id:
        return False

    shown_at = st.session_state.get(_NOTICE_SHOWN_AT_KEY)
    if isinstance(shown_at, (int, float)):
        if time.monotonic() - shown_at >= API_HEALTH_NOTICE_TTL_SECONDS:
            st.session_state[_NOTICE_DISMISSED_ID_KEY] = current_id
            return False

    return True


def _notice_detail(record: ApiHealthRecord) -> str:
    if record.message:
        return record.message
    return "请检查 API Key、Base URL、模型或本地 Ollama 服务后重新检测。"


def _render_notice_card(record: ApiHealthRecord, settings: AppSettings, today: date) -> None:
    title = "当前翻译引擎不可用"
    detail = _notice_detail(record)

    notice = st.container(key="api-health-notice")
    with notice:
        st.markdown(
            (
                '<div class="api-health-notice__body">'
                '  <div class="api-health-notice__icon">!</div>'
                '  <div class="api-health-notice__copy">'
                f'    <div class="api-health-notice__title">{html.escape(title)}</div>'
                f'    <div class="api-health-notice__detail" title="{html.escape(detail)}">'
                f"{html.escape(detail)}"
                "    </div>"
                "  </div>"
                "</div>"
            ),
            unsafe_allow_html=True,
        )

        actions = st.container(key="api-health-notice-actions")
        with actions:
            spacer_col, retry_col, dismiss_col = st.columns([1.8, 0.9, 0.9], gap="small")
            with spacer_col:
                st.markdown('<div class="api-health-notice__hint">请重新配置后再试</div>', unsafe_allow_html=True)
            with retry_col:
                retry_clicked = st.button(
                    "重新检测",
                    key="api_health_retry_button",
                    use_container_width=True,
                )
            with dismiss_col:
                dismiss_clicked = st.button(
                    "我知道了",
                    key="api_health_dismiss_button",
                    use_container_width=True,
                )

    current_id = _notice_id(record)
    if dismiss_clicked:
        st.session_state[_NOTICE_DISMISSED_ID_KEY] = current_id
        st.rerun()

    if retry_clicked:
        updated_record = run_api_health_check(settings, today=today)
        _mark_checked_for_session(build_connectivity_signature(settings), today)
        st.session_state.pop(_NOTICE_ID_KEY, None)
        st.session_state.pop(_NOTICE_SHOWN_AT_KEY, None)
        if updated_record.last_status != API_HEALTH_FAILED:
            st.session_state[_NOTICE_DISMISSED_ID_KEY] = _notice_id(updated_record)
        else:
            st.session_state.pop(_NOTICE_DISMISSED_ID_KEY, None)
        st.rerun()


def render_api_health_monitor(settings: AppSettings) -> None:
    """Run startup API health checks and show a cc-switch-style top notice."""
    today = date.today()
    record = _maybe_run_startup_check(settings, today)
    if record is None:
        return

    signature = build_connectivity_signature(settings)
    if _should_show_notice(record, signature):
        _render_notice_card(record, settings, today)
