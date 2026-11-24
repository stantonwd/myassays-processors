"""MyAssays processor.  Run with:  streamlit run app.py  (or double-click run_app.bat)

Three pages:
  Process plate   upload one MyAssays export, check it, save it to the records
  Issues          samples that still need a decision (reruns, binding, failed runs)
  Levey-Jennings  control lots across plates; click a point to accept/reject it
"""
import streamlit as st
import common
import records as rec

st.set_page_config(page_title="MyAssays Processor", layout="wide")

try:
    n_open = len(rec.issues(common.load_db()["hormone_results"]))
except Exception:
    n_open = 0

nav = st.navigation([
    st.Page("views/process.py", title="Process plate", icon="🧪", default=True),
    st.Page("views/issues.py", title=f"Issues ({n_open})" if n_open else "Issues",
            icon="🚩", url_path="issues"),
    st.Page("views/levey_jennings.py", title="Levey-Jennings", icon="📈",
            url_path="levey-jennings"),
])
nav.run()
