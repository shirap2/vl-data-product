"""Analyze tab of the BI app: the stored motion of the selected drive.

Units: position cm, velocity cm/s, acceleration cm/s^2, time s.
"""

from pathlib import Path

import pyarrow as pa
import streamlit as st

from vl_data.bi.data import motion_hint, read_problem
from vl_data.bi.figures import motion_figure
from vl_data.core.config import Config
from vl_data.sdk.client import VLClient


def render_analyze_tab(client: VLClient, config: Config, lake_dir: Path, drive: str) -> None:
    """Draw the stored motion: centroid x, velocity and acceleration against time in s.

    Units: cm, cm/s and cm/s^2; time is frame_id / config.fps. Only the lake is read. A drive
    with no stored motion gets the reason and, when one exists, the command that fixes it
    (the ingest command uses config.raw_dir). Never raises.
    """
    try:
        motion = client.motion(drive)
    except (ValueError, OSError, pa.ArrowException) as error:
        st.error(f"Cannot read the motion of drive {drive!r}: {read_problem(error)}")
        return
    if motion.num_rows == 0:
        st.info(f"Drive {drive!r} has no stored motion to show.")
        hint = motion_hint(client, config.raw_dir, lake_dir, drive)
        st.error(hint.problem)
        if hint.command is not None:
            st.code(hint.command, language="powershell")
        return
    st.subheader("Stored motion")
    st.caption("Centroid x in cm, velocity in cm/s and acceleration in cm/s^2 against time in s.")
    try:
        for kind in ("position", "velocity", "acceleration"):
            figure = motion_figure(motion, config.fps, kind)
            st.plotly_chart(figure, width="stretch", key=f"motion_{kind}")
    except (KeyError, ValueError, pa.ArrowException) as error:
        st.error(f"Cannot draw the motion of drive {drive!r}: {type(error).__name__}")
