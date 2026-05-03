"""AppTest wrapper for the TM page.

Load this file with streamlit.testing.v1.AppTest.from_file(...) to render
ui.page_tm directly without booting the full app shell.
"""

import streamlit as st

from core import tm_manager
from settings import AppSettings
from ui.page_tm import render_page


tm_manager.init_db()
settings = st.session_state["settings"] if "settings" in st.session_state else AppSettings()
st.session_state["settings"] = render_page(settings)
