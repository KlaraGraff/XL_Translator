"""AppTest wrapper for the Word translation page."""

import streamlit as st

from settings import AppSettings
from ui.page_word_translate import render_page


settings = st.session_state["settings"] if "settings" in st.session_state else AppSettings()
st.session_state["settings"] = render_page(settings)
