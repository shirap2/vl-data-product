"""Shell of the BI app: drive selector in the sidebar, four tabs, scenario-hash badge.

Units: none (the tabs carry cm, cm/s, cm/s^2, frames, rows and s).
"""

from pathlib import Path

import pyarrow as pa
import streamlit as st

from vl_data.bi.analyze_tab import render_analyze_tab
from vl_data.bi.clean_tab import render_clean_tab
from vl_data.bi.data import list_lake_drives, read_problem
from vl_data.bi.ingest_panel import LAST_INGESTED_KEY, render_ingest_panel
from vl_data.bi.sql_tab import render_sql_tab
from vl_data.core.config import Config
from vl_data.core.lake import Lake
from vl_data.generator.ui.generate_tab import render_generate_tab
from vl_data.sdk.client import VLClient

_DRIVE_KEY = "bi_drive"
_HASH_CHARS = 12  # hex characters in the badge; the caption shows all 64
_NO_DRIVE_HINT = (
    "No drive in the lake yet. Generate one in the Generate tab, then click 'Ingest this drive' "
    "in the Generate or Clean tab. Command line alternative: "
    "python -m vl_data ingest <drive folder> --lake-dir <lake folder>, then reload this page."
)


def hash_badge(scenario_hash: str | None, label: str) -> None:
    """Draw the scenario-hash badge (first 12 hex) and the full hash in a caption.

    With None (the drive has no hash) a gray "none" badge and a short caption are drawn.
    """
    if scenario_hash is None:
        st.badge(f"{label}: none", color="gray")
        st.caption("scenario_hash: not available for this drive")
        return
    st.badge(f"{label}: {scenario_hash[:_HASH_CHARS]}", color="blue")
    st.caption(f"scenario_hash: {scenario_hash}")


def _selected_hash(client: VLClient, drive: str) -> str | None:
    """Scenario hash from the drive's one lineage row; None when it cannot be read.

    Silent on a read problem: the Clean tab and the other tabs report it themselves.
    """
    try:
        lineage = client.lineage(drive)
    except (ValueError, OSError, pa.ArrowException):
        return None
    if lineage.num_rows != 1:
        return None
    value: str | None = lineage.column("scenario_hash").to_pylist()[0]
    return value


def _choose_drive(drives: list[str]) -> None:
    """Set the selector state before it is drawn: the drive just ingested, else a valid drive.

    A selector that was empty keeps the value None in session state when drives appear, so a
    drive that was never chosen is replaced by the first one.
    """
    last = st.session_state.pop(LAST_INGESTED_KEY, None)
    if last in drives:
        st.session_state[_DRIVE_KEY] = last
    elif drives and st.session_state.get(_DRIVE_KEY) not in drives:
        st.session_state[_DRIVE_KEY] = drives[0]


def render_app(config: Config, lake_dir: Path) -> None:
    """Draw the sidebar drive selector and the four tabs; never raises.

    The lake is read here; it is written only by the ingest panel, through ingest_drive, after a
    click (nothing is created when the folder is missing or empty). The hash of the selected
    drive is read once per run and shown at the top of every tab. All four tab bodies run on
    every rerun.
    """
    lake = Lake(lake_dir)
    client = VLClient(lake)
    lake_readable = True
    try:
        drives = list_lake_drives(lake)
    except (OSError, pa.ArrowException) as error:
        st.error(read_problem(error))
        drives = []
        lake_readable = False  # no ingest panel: a click would write into a lake we cannot read
    _choose_drive(drives)
    drive = st.sidebar.selectbox("Drive", drives, key=_DRIVE_KEY)
    scenario_hash = _selected_hash(client, drive) if drive is not None else None
    generate, clean, analyze, sql = st.tabs(["Generate", "Clean", "Analyze", "SQL"])
    with generate:
        if drive is not None:
            hash_badge(scenario_hash, "selected drive")
        render_generate_tab(config)
        if lake_readable:
            render_ingest_panel(config, lake_dir, drives, "generate")
    for tab in (clean, analyze, sql):
        with tab:
            if drive is None:
                st.info(_NO_DRIVE_HINT)
            else:
                hash_badge(scenario_hash, "scenario hash")
    if drive is None:
        # an empty lake: Generate and Clean get the panel; Analyze and SQL keep the hint
        with clean:
            if lake_readable:
                render_ingest_panel(config, lake_dir, drives, "clean")
        return
    with clean:
        render_ingest_panel(config, lake_dir, drives, "clean")
        render_clean_tab(client, drive, config.raw_dir, lake_dir)
    with analyze:
        render_analyze_tab(client, config, lake_dir, drive)
    with sql:
        render_sql_tab(client, drive)
