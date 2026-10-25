"""Standalone runner for the Generate tab.

Run with: streamlit run src/vl_data/generator/ui/generate_app.py
"""

from vl_data.core.config import load_config
from vl_data.generator.ui.generate_tab import render_generate_tab

render_generate_tab(load_config())
