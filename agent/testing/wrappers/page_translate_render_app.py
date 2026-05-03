"""AppTest wrapper for the translate page.

Load this file with streamlit.testing.v1.AppTest.from_file(...) to render
ui.page_translate directly without booting the full app shell.
"""

import streamlit as st

from settings import AppSettings
from ui.page_translate import render_page


settings = st.session_state["settings"] if "settings" in st.session_state else AppSettings()
st.session_state["settings"] = render_page(settings)
