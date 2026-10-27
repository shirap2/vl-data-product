"""Figure tests for the BI app. Units: cm, cm/s, cm/s^2, frames, s."""

import numpy as np
import plotly.graph_objects as go
import pyarrow as pa
import pytest

from vl_data.bi.data import DriveQuality, load_quality
from vl_data.bi.figures import codes_figure, motion_figure, quality_figure

from .helpers import BiDrive

FPS = 16


def _arr(values: object) -> np.ndarray:
    return np.asarray(values, dtype=float)


def _col(table: pa.Table, name: str) -> np.ndarray:
    """A column as floats; nulls become NaN."""
    return table.column(name).cast(pa.float64()).fill_null(np.nan).to_numpy().astype(float)


def test_quality_and_codes_figures(dropped: BiDrive) -> None:
    quality = load_quality(dropped.client, dropped.drive_id)
    assert quality is not None
    fig = quality_figure(quality)
    assert len(fig.data) == 1
    assert [str(v) for v in fig.data[0].x] == ["kept", "rejected", "missing"]
    assert list(fig.data[0].y) == [quality.frames_kept, quality.frames_rejected, 1]
    assert fig.layout.yaxis.title.text == "frames"

    other = DriveQuality("d", None, "ok", 10, 9, 7, 2, 1, 5, {})
    assert list(quality_figure(other).data[0].y) == [7, 2, 1]

    codes = codes_figure({"INTEGRITY": 3, "VL-001": 1})
    assert [str(v) for v in codes.data[0].x] == ["INTEGRITY", "VL-001"]
    assert list(codes.data[0].y) == [3, 1]
    assert codes.layout.yaxis.title.text == "quarantine rows"

    empty = codes_figure({})
    assert isinstance(empty, go.Figure)
    assert sum(len(t.y) for t in empty.data if t.y is not None) == 0


def test_motion_figure_matches_motion_table(clean: BiDrive, dropped: BiDrive) -> None:
    for d in (clean, dropped):
        motion = d.client.motion(d.drive_id)
        order = np.argsort(_col(motion, "frame_id"), kind="stable")
        ids = _col(motion, "frame_id")[order]
        for kind, col, label in (
            ("position", "x_cm", "centroid x (cm)"),
            ("velocity", "velocity_cm_s", "velocity (cm/s)"),
            ("acceleration", "acceleration_cm_s2", "acceleration (cm/s^2)"),
        ):
            fig = motion_figure(motion, FPS, kind)
            assert len(fig.data) == 1
            np.testing.assert_allclose(_arr(fig.data[0].x), ids / FPS)
            np.testing.assert_allclose(
                _arr(fig.data[0].y), _col(motion, col)[order], equal_nan=True
            )
            assert fig.layout.yaxis.title.text == label
            assert fig.layout.xaxis.title.text == "time (s)"


def test_motion_figure_sorts_and_keeps_gaps() -> None:
    motion = pa.table(
        {
            "frame_id": pa.array([3, 0, 1], pa.int64()),
            "timestamp_ns": pa.array([3, 0, 1], pa.int64()),
            "x_cm": pa.array([30.0, 0.0, 10.0]),
            "velocity_cm_s": pa.array([3.0, None, 1.0]),
            "acceleration_cm_s2": pa.array([None, None, 0.5]),
            "n_points": pa.array([1, 1, 1], pa.int64()),
        }
    )
    pos = motion_figure(motion, 2, "position")
    np.testing.assert_allclose(_arr(pos.data[0].x), [0.0, 0.5, 1.5])
    np.testing.assert_allclose(_arr(pos.data[0].y), [0, 10, 30])
    vel = motion_figure(motion, 2, "velocity")
    np.testing.assert_allclose(_arr(vel.data[0].y), [np.nan, 1, 3], equal_nan=True)
    acc = motion_figure(motion, 2, "acceleration")
    np.testing.assert_allclose(_arr(acc.data[0].y), [np.nan, 0.5, np.nan], equal_nan=True)


def test_unknown_kind_raises() -> None:
    motion = pa.table({"frame_id": [0], "x_cm": [0.0]})  # raises before any column is read
    with pytest.raises(ValueError, match="kind"):
        motion_figure(motion, FPS, "speed")
    with pytest.raises(ValueError, match="kind"):
        motion_figure(motion, FPS, "")
