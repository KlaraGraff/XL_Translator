"""
Streamlit 应用入口。
导航：表格翻译 | Word 翻译 | 记忆库管理
"""
from functools import lru_cache
from pathlib import Path

import streamlit as st

from app_meta import APP_NAME
from core.tm_manager import init_db
from settings import load_settings, save_settings
from ui.api_health_notice import render_api_health_monitor
from ui.branding import get_page_icon_config
from ui.data_migration_gate import render_data_migration_gate
from ui.sidebar import render_sidebar
import ui.page_translate as page_translate
import ui.page_word_translate as page_word_translate
import ui.page_tm as page_tm


# ── CSS 注入 ──────────────────────────────────────────────

@lru_cache(maxsize=1)
def _load_css(css_path: str) -> str:
    return Path(css_path).read_text(encoding="utf-8")


def _inject_css():
    css_path = Path(__file__).parent / "ui" / "styles.css"
    if css_path.exists():
        st.markdown(
            f"<style>{_load_css(str(css_path))}</style>",
            unsafe_allow_html=True,
        )


# ── session_state 初始化 ──────────────────────────────────

def _init():
    if "settings"    not in st.session_state:
        st.session_state["settings"]    = load_settings()
    if "active_page" not in st.session_state:
        st.session_state["active_page"] = "excel_translate"
    elif st.session_state["active_page"] == "translate":
        st.session_state["active_page"] = "excel_translate"


def _persist_settings_if_changed(settings) -> None:
    """Persist settings whenever their serialized content changes."""
    settings_json = settings.model_dump_json()
    if settings_json != st.session_state.get("_settings_json"):
        save_settings(settings)
        st.session_state["_settings_json"] = settings_json


def _ensure_db_initialized() -> None:
    """Initialize the TM database once per Streamlit session."""
    if st.session_state.get("_tm_db_initialized"):
        return
    init_db()
    st.session_state["_tm_db_initialized"] = True


# ── 主入口 ────────────────────────────────────────────────

def main():
    st.set_page_config(
        page_title=APP_NAME,
        page_icon=get_page_icon_config(),
        layout="wide",
        initial_sidebar_state="expanded",
    )

    _inject_css()
    render_data_migration_gate()
    _init()
    _ensure_db_initialized()

    settings    = st.session_state["settings"]
    active_page = st.session_state["active_page"]
    is_running  = (
        st.session_state.get("translate_phase") == "running"
        or st.session_state.get("word_translate_phase") == "running"
    )

    # 侧边栏使用按钮 callback 在脚本主体执行前切换 active_page，
    # 避免导航高亮与主内容页落后一轮。
    settings, active_page = render_sidebar(settings, active_page, is_running)

    # 持久化侧边栏修改（仅在内容实际变更时写磁盘，避免每次 rerun 都 I/O）
    _persist_settings_if_changed(settings)
    st.session_state["settings"] = settings

    render_api_health_monitor(settings)

    # 主内容区路由
    if active_page in ("excel_translate", "translate"):
        updated = page_translate.render_page(settings)
    elif active_page == "word_translate":
        updated = page_word_translate.render_page(settings)
    else:
        updated = page_tm.render_page(settings)

    # 回写被页面修改的 settings（同样加比较保护，避免无变化时写盘）
    _persist_settings_if_changed(updated)
    st.session_state["settings"] = updated


if __name__ == "__main__":
    main()
