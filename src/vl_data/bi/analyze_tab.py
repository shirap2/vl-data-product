"""Analyze tab of the BI app: the stored motion of the selected drive.

Units: position cm, velocity cm/s, acceleration cm/s^2, time s.
"""

from pathlib import Path

import pyarrow as pa
import streamlit as st

from vl_data.bi.data import motion_hint, read_problem
from vl_data.bi.figures import motion_figure
from vl_data.bi.motion_panel import render_motion_panel
from vl_data.core.config import Config
from vl_data.sdk.client import VLClient


def render_analyze_tab(client: VLClient, config: Config, lake_dir: Path, drive: str) -> None:
    """Draw the stored motion: centroid x, velocity and acceleration against time in s.

    Units: cm, cm/s and cm/s^2; time is frame_id / config.fps. The lake is read here; the motion
    panel writes it only on a click. An ingested ok drive gets a Compute (no motion) or
    Recompute (motion present) button; a drive with no motion that cannot be computed gets the
    reason and the ingest command; motion of a drive whose lineage is not ok gets a warning
    and no button. Never raises.
    """
    try:
        motion = client.motion(drive)
    except (ValueError, OSError, pa.ArrowException) as error:
        st.error(f"Cannot read the motion of drive {drive!r}: {read_problem(error)}")
        return
    hint = motion_hint(client, config.raw_dir, lake_dir, drive)
    if motion.num_rows == 0:
        if hint.can_compute:
            st.info("Motion is not computed yet")
            render_motion_panel(config, lake_dir, drive)
            st.caption("Command line alternative:")
        else:
            st.info(f"Drive {drive!r} has no stored motion to show.")
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
        return
    if hint.can_compute:
        render_motion_panel(config, lake_dir, drive, recompute=True)
    else:
        st.warning(
            "The lineage of this drive is not ok (for example it was rejected at ingest); "
            "these charts are from an earlier run."
        )
