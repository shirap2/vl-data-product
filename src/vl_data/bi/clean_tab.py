"""Clean tab of the BI app: ingest quality of one lake drive. Reads the lake.

Units: frames and rows are counts; no other physical quantity is shown.
"""

from pathlib import Path

import pyarrow as pa
import streamlit as st

from vl_data.bi.data import QUARANTINE_ROWS_SHOWN, ingest_command, load_quality, read_problem
from vl_data.bi.figures import codes_figure, quality_figure
from vl_data.sdk.client import VLClient


def render_clean_tab(client: VLClient, drive: str, raw_dir: Path, lake_dir: Path) -> None:
    """Draw the quality view of a drive (reads the lake, writes nothing); never raises.

    Shows counts (frames, rows), two bar charts and the first QUARANTINE_ROWS_SHOWN (200)
    quarantine rows. A drive without a lineage row gets the exact ingest command for
    raw_dir/drive into lake_dir. Bad drive ids and unreadable tables become st.error messages.
    """
    try:
        quality = load_quality(client, drive)
    except (ValueError, OSError, pa.ArrowException) as error:
        st.error(f"Cannot read the quality of drive {drive!r}: {read_problem(error)}")
        return
    if quality is None:
        st.info(f"Drive {drive!r} is not ingested. Run this command in PowerShell:")
        st.code(ingest_command(raw_dir, drive, lake_dir), language="powershell")
        return
    st.subheader(f"Quality of drive {drive} ({quality.status})")
    columns = st.columns(6)
    counts = [
        ("expected (frames)", quality.frames_expected),
        ("in (frames)", quality.frames_in),
        ("kept (frames)", quality.frames_kept),
        ("rejected (frames)", quality.frames_rejected),
        ("missing (frames)", quality.frames_missing),
        ("keypoint rows (rows)", quality.keypoint_rows),
    ]
    for column, (label, value) in zip(columns, counts, strict=True):
        column.metric(label, value)
    try:
        st.plotly_chart(quality_figure(quality), width="stretch", key="clean_quality")
        codes = codes_figure(quality.quarantine_by_code)
        st.plotly_chart(codes, width="stretch", key="clean_codes")
    except (KeyError, ValueError, pa.ArrowException) as error:
        st.error(f"Cannot draw the charts of drive {drive!r}: {type(error).__name__}")
        return
    try:
        quarantine = client.quarantine(drive)
    except (ValueError, OSError, pa.ArrowException) as error:
        st.error(f"Cannot read the quarantine rows of drive {drive!r}: {read_problem(error)}")
        return
    try:
        shown = quarantine.slice(0, QUARANTINE_ROWS_SHOWN)
        st.caption(f"showing {shown.num_rows} of {quarantine.num_rows} quarantine rows")
        st.dataframe(shown, width="stretch", hide_index=True)
    except (KeyError, ValueError, pa.ArrowException) as error:
        st.error(f"Cannot show the quarantine rows of drive {drive!r}: {type(error).__name__}")
