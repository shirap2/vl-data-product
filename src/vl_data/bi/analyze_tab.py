"""Analyze tab of the BI app: stored motion, and an evaluation against ground truth on click.

Units: position cm, velocity cm/s, acceleration cm/s^2, time s, counts in frames.
"""

from pathlib import Path

import pyarrow as pa
import streamlit as st

from vl_data.bi.data import AnalyzeOutcome, analyze_drive, metric_rows, read_problem
from vl_data.bi.figures import error_figure, motion_figure, timeline_figure
from vl_data.core.config import Config
from vl_data.sdk.client import VLClient
from vl_data.usecase.config import MotionConfig
from vl_data.usecase.evaluate import EvalReport

_OUTCOME_KEY = "analyze_outcome"  # session_state entry: the one stored AnalyzeOutcome
_KINDS = ("velocity", "acceleration")


def render_analyze_tab(
    client: VLClient, config: Config, cfg: MotionConfig, lake_dir: Path, drive: str
) -> None:
    """Draw the stored motion, the Evaluate button and, once clicked, the stored report.

    The motion charts (cm, cm/s, cm/s^2 against s) need only the lake. Only a click on Evaluate
    runs the evaluation (config.raw_dir/drive is the raw folder); its outcome is kept in
    st.session_state and redrawn on later reruns without evaluating again. Never raises.
    """
    try:
        motion = client.motion(drive)
    except (ValueError, OSError, pa.ArrowException) as error:
        st.error(f"Cannot read the motion of drive {drive!r}: {read_problem(error)}")
        return
    if motion.num_rows == 0:
        st.info(f"Drive {drive!r} has no stored motion to show.")
        # Without a motion table analyze_drive stops at the problem: it never evaluates.
        _draw_outcome(
            analyze_drive(client, config, cfg, config.raw_dir, lake_dir, drive), config, cfg
        )
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
    st.subheader("Evaluation")
    if st.button("Evaluate against ground truth", key="analyze_evaluate"):
        with st.spinner("Evaluating the drive (verifies and re-hashes its raw files)..."):
            fresh = analyze_drive(client, config, cfg, config.raw_dir, lake_dir, drive)
        st.session_state[_OUTCOME_KEY] = fresh
    stored: AnalyzeOutcome | None = st.session_state.get(_OUTCOME_KEY)
    if stored is not None and stored.drive == drive:
        _draw_outcome(stored, config, cfg)


def _draw_outcome(outcome: AnalyzeOutcome, config: Config, cfg: MotionConfig) -> None:
    """Draw a problem with its command, or the PASS or FAIL line, metric table and charts."""
    if outcome.problem is not None:
        st.error(outcome.problem)
        if outcome.command is not None:
            st.code(outcome.command, language="powershell")
        return
    if outcome.report is not None:
        try:
            _draw_report(outcome.report, config, cfg)
        except (KeyError, ValueError, pa.ArrowException) as error:
            st.error(f"Cannot draw the report of drive {outcome.drive!r}: {type(error).__name__}")


def _draw_report(report: EvalReport, config: Config, cfg: MotionConfig) -> None:
    """Draw the PASS or FAIL line, the metric table (cm, cm/s, cm/s^2) and four charts."""
    if report.passed:
        st.success("PASS: interior errors are within the tolerances")
    else:
        st.error("FAIL: an interior error exceeds its tolerance, or no interior frame")
    st.caption(
        "Errors are estimate minus truth in cm, cm/s or cm/s^2. The true values come from the "
        "ground truth, which only this evaluation uses."
    )
    st.dataframe(metric_rows(report, config, cfg), width="stretch", hide_index=True)
    tolerances = {
        "velocity": cfg.tolerances.velocity_cm_s,
        "acceleration": cfg.tolerances.acceleration_cm_s2,
    }
    for kind in _KINDS:
        figure = timeline_figure(report.frames, config.fps, kind)
        st.plotly_chart(figure, width="stretch", key=f"eval_timeline_{kind}")
    for kind in _KINDS:
        figure = error_figure(report.frames, config.fps, kind, tolerances[kind])
        st.plotly_chart(figure, width="stretch", key=f"eval_error_{kind}")
