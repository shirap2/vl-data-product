"""Runner of the BI app.

Run with: python -m streamlit run src/vl_data/bi/app.py
"""

import streamlit as st
import yaml

from vl_data.bi.shell import render_app
from vl_data.core.config import load_config
from vl_data.etl.config import load_etl_config

st.set_page_config(page_title="VL data product", layout="wide")
st.title("VL data product")
try:
    _config = load_config()
    _lake_dir = load_etl_config().lake_dir
except (OSError, ValueError, yaml.YAMLError):
    st.error("Cannot load the configuration files; check the config folder and the VL_* settings.")
else:
    render_app(_config, _lake_dir)
