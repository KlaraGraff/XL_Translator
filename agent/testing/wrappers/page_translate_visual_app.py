"""Visual wrapper for the translate page with global CSS and sidebar shell."""

from pathlib import Path

import streamlit as st

from settings import AppSettings
from ui.sidebar import render_sidebar
import ui.page_translate as page_translate


def _inject_css() -> None:
    css_path = Path(__file__).resolve().parents[3] / "ui" / "styles.css"
    st.markdown(f"<style>{css_path.read_text(encoding='utf-8')}</style>", unsafe_allow_html=True)


st.set_page_config(
    page_title="Translator",
    page_icon="T",
    layout="wide",
    initial_sidebar_state="expanded",
)

_inject_css()

settings = st.session_state["settings"] if "settings" in st.session_state else AppSettings()
st.session_state["settings"] = settings

settings, _ = render_sidebar(settings, "translate", False)
st.session_state["settings"] = page_translate.render_page(settings)
