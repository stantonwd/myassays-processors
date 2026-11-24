"""Settings shared by every page: where the data folder is, and loaders."""
import os
import tomllib
from pathlib import Path
import pandas as pd
import streamlit as st
import records as rec

REPO = Path(__file__).parent
SERIES, MUTED = "#2a78d6", "#898781"     # charts: one series colour + reference lines


def secret(key):
    """st.secrets only looks in the folder streamlit was started from, so also
    read .streamlit/secrets.toml in the repo directly."""
    try:
        val = st.secrets.get(key)
        if val:
            return val
    except Exception:            # no secrets.toml where streamlit looked
        pass
    try:
        with open(REPO / ".streamlit" / "secrets.toml", "rb") as fh:
            return tomllib.load(fh).get(key)
    except Exception:
        return None


DATA_DIR = rec.data_dir(secret("data_dir"))
REC_DIR = rec.records_dir(DATA_DIR)
DATA_DIR_SET = bool(secret("data_dir") or os.environ.get("MYASSAYS_DATA_DIR"))


def load_db() -> dict:
    return rec.load_all(REC_DIR)


@st.cache_data(show_spinner=False)
def _collection(path: str, mtime: float) -> pd.DataFrame:
    return rec.load_collection(path)


def load_collection():
    """(collection DataFrame or None, file path or None, error text or None)."""
    f = rec.find_collection_file(DATA_DIR)
    if not f:
        return None, None, None
    try:
        return _collection(str(f), f.stat().st_mtime), f, None
    except Exception as e:
        return None, f, str(e)


def sidebar_data_info(coll_file, coll_err):
    with st.sidebar:
        st.caption(f"Data folder: `{DATA_DIR}`")
        if not DATA_DIR_SET:
            st.warning("No data folder is set, so the default above is being used. Set "
                       "data_dir in .streamlit/secrets.toml (see README).")
        if coll_err:
            st.warning(f"Couldn't read {coll_file.name}: {coll_err}")
        elif coll_file:
            st.caption(f"Collection file: `{coll_file.name}`")
        else:
            st.warning("No fecal_hormone_collection_*.csv in the data folder, so pg/g "
                       "can't be calculated.")
