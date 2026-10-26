"""Plotly figures for the BI app. Units: cm, cm/s, cm/s^2, frames, s. No Streamlit import."""

from collections.abc import Mapping

import numpy as np
import plotly.graph_objects as go
import pyarrow as pa

from vl_data.bi.data import DriveQuality

_TIME_TITLE = "time (s)"
_MODE = "lines+markers"  # markers keep isolated points visible next to gaps

# kind to (motion column, y axis title) for the stored motion table
_MOTION_KINDS = {
    "position": ("x_cm", "centroid x (cm)"),
    "velocity": ("velocity_cm_s", "velocity (cm/s)"),
    "acceleration": ("acceleration_cm_s2", "acceleration (cm/s^2)"),
}


def _check_kind(kind: str, allowed: Mapping[str, object]) -> None:
    if kind not in allowed:
        raise ValueError(f"unknown kind {kind!r}; expected one of {sorted(allowed)}")


def _values(table: pa.Table, column: str) -> np.ndarray:
    """Column as float64; nulls become NaN so they stay gaps in the plot."""
    return np.asarray(
        table.column(column).cast(pa.float64()).fill_null(np.nan).to_numpy(), dtype=float
    )


def _seconds(table: pa.Table, fps: int) -> np.ndarray:
    """Time in s of each row: frame_id / fps (a dropped frame keeps its original id)."""
    return _values(table, "frame_id") / fps


def _bars(labels: list[str], counts: list[int], y_title: str) -> go.Figure:
    fig = go.Figure(go.Bar(x=labels, y=counts))
    fig.update_layout(yaxis_title=y_title)
    return fig


def quality_figure(quality: DriveQuality) -> go.Figure:
    """Bar chart of kept, rejected and missing frames (counts)."""
    counts = [quality.frames_kept, quality.frames_rejected, quality.frames_missing]
    return _bars(["kept", "rejected", "missing"], counts, "frames")


def codes_figure(by_code: dict[str, int]) -> go.Figure:
    """Bar chart of quarantine rows per error code (counts); empty dict gives an empty chart."""
    return _bars(list(by_code), list(by_code.values()), "quarantine rows")


def motion_figure(motion: pa.Table, fps: int, kind: str) -> go.Figure:
    """Stored motion against time in s; kind is "position", "velocity" or "acceleration".

    Units: y is cm (centroid x), cm/s or cm/s^2.

    Position is the raw centroid x, which runs ahead of the true position by (size_cm - 1) / 2 cm.
    """
    _check_kind(kind, _MOTION_KINDS)
    column, title = _MOTION_KINDS[kind]
    ordered = motion.sort_by("frame_id")  # VLClient.motion does not sort
    fig = go.Figure(
        go.Scatter(x=_seconds(ordered, fps), y=_values(ordered, column), mode=_MODE, name=kind)
    )
    fig.update_layout(xaxis_title=_TIME_TITLE, yaxis_title=title)
    return fig
