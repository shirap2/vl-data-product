"""Figure tests for the BI app. Units: cm, cm/s, cm/s^2, frames, s."""

import numpy as np
import plotly.graph_objects as go
import pyarrow as pa
import pytest

from vl_data.bi.data import DriveQuality, load_quality
from vl_data.bi.figures import (
    codes_figure,
    error_figure,
    motion_figure,
    quality_figure,
    timeline_figure,
)
from vl_data.core.config import Config
from vl_data.usecase.config import MotionConfig
from vl_data.usecase.evaluate import evaluate_drive

from .helpers import BiDrive

FPS = 16


def _arr(values: object) -> np.ndarray:
    return np.asarray(values, dtype=float)


def _col(table: pa.Table, name: str) -> np.ndarray:
    """A column as floats; nulls become NaN."""
    return table.column(name).cast(pa.float64()).fill_null(np.nan).to_numpy().astype(float)


def _frames(
    ids: list[int], est: list[float | None], true: list[float], acc_est: list[float | None]
) -> pa.Table:
    n = len(ids)
    return pa.table(
        {
            "frame_id": pa.array(ids, pa.int64()),
            "interior": pa.array([True] * n),
            "x_est_cm": pa.array([0.0] * n),
            "x_true_cm": pa.array([0.0] * n),
            "velocity_est_cm_s": pa.array(est, pa.float64()),
            "velocity_true_cm_s": pa.array(true, pa.float64()),
            "acceleration_est_cm_s2": pa.array(acc_est, pa.float64()),
            "acceleration_true_cm_s2": pa.array([1.0] * n),
        }
    )


def _report_frames(d: BiDrive, config: Config, cfg: MotionConfig) -> pa.Table:
    return evaluate_drive(d.drive_dir, d.client, config, cfg).frames


def test_timeline_x_is_seconds_and_values_match(
    clean: BiDrive, bi_config: Config, motion_cfg: MotionConfig
) -> None:
    for d in (clean,):  # a dropped-frame drive cannot be evaluated (truth has all frames)
        frames = _report_frames(d, bi_config, motion_cfg)
        ids = _col(frames, "frame_id")
        for kind, unit, suffix in (
            ("velocity", "cm/s", "cm_s"),
            ("acceleration", "cm/s^2", "cm_s2"),
        ):
            fig = timeline_figure(frames, FPS, kind)
            assert [t.name for t in fig.data] == ["estimate", "truth"]
            for t in fig.data:
                np.testing.assert_allclose(_arr(t.x), ids / FPS)
            np.testing.assert_allclose(
                _arr(fig.data[0].y), _col(frames, f"{kind}_est_{suffix}"), equal_nan=True
            )
            np.testing.assert_allclose(
                _arr(fig.data[1].y), _col(frames, f"{kind}_true_{suffix}"), equal_nan=True
            )
            assert fig.layout.xaxis.title.text == "time (s)"
            assert unit in fig.layout.yaxis.title.text


def test_timeline_gaps_and_sparse_frame_ids() -> None:
    frames = _frames([0, 1, 3, 4], [None, 2.0, 3.0, 4.0], [1.0, 2.0, 3.0, 4.0], [None, 1, 1, 1])
    vel = timeline_figure(frames, 4, "velocity")
    np.testing.assert_allclose(_arr(vel.data[0].x), [0.0, 0.25, 0.75, 1.0])  # not the row index
    np.testing.assert_allclose(_arr(vel.data[0].y), [np.nan, 2, 3, 4], equal_nan=True)
    np.testing.assert_allclose(_arr(vel.data[1].y), [1, 2, 3, 4])
    acc = timeline_figure(frames, 4, "acceleration")
    np.testing.assert_allclose(_arr(acc.data[0].y), [np.nan, 1, 1, 1], equal_nan=True)
    np.testing.assert_allclose(_arr(acc.data[1].y), [1, 1, 1, 1])
    assert not vel.data[0].connectgaps
    assert "markers" in vel.data[0].mode and "lines" in vel.data[0].mode


def test_error_figure_is_estimate_minus_truth_with_tolerance_lines(
    clean: BiDrive, bi_config: Config, motion_cfg: MotionConfig
) -> None:
    frames = _report_frames(clean, bi_config, motion_cfg)
    ids = _col(frames, "frame_id")
    for kind, tol, unit, est, true in (
        ("velocity", 5.0, "(cm/s)", "velocity_est_cm_s", "velocity_true_cm_s"),
        ("acceleration", 30.0, "(cm/s^2)", "acceleration_est_cm_s2", "acceleration_true_cm_s2"),
    ):
        fig = error_figure(frames, FPS, kind, tol)
        assert len(fig.data) == 1
        np.testing.assert_allclose(_arr(fig.data[0].x), ids / FPS)
        np.testing.assert_allclose(
            _arr(fig.data[0].y), _col(frames, est) - _col(frames, true), equal_nan=True
        )
        lines = [s.y0 for s in fig.layout.shapes if s.type == "line"]
        assert len(fig.layout.shapes) == 2
        assert sorted(lines) == [-tol, tol]
        assert fig.layout.xaxis.title.text == "time (s)"
        assert fig.layout.yaxis.title.text.endswith(f"error {unit}")


def test_error_null_estimate_is_a_gap_not_zero() -> None:
    frames = _frames([0, 2, 3], [None, 5.0, 2.0], [1.0, 3.0, 2.5], [None, 4.0, 3.0])
    fig = error_figure(frames, 2, "velocity", 1.0)
    np.testing.assert_allclose(_arr(fig.data[0].y), [np.nan, 2.0, -0.5], equal_nan=True)
    np.testing.assert_allclose(_arr(fig.data[0].x), [0.0, 1.0, 1.5])
    acc = error_figure(frames, 2, "acceleration", 1.0)
    np.testing.assert_allclose(_arr(acc.data[0].y), [np.nan, 3.0, 2.0], equal_nan=True)


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
    frames = _frames([0, 1], [1.0, 2.0], [1.0, 2.0], [1.0, 2.0])
    motion = pa.table({"frame_id": [0], "x_cm": [0.0]})  # raises before any column is read
    with pytest.raises(ValueError, match="kind"):
        motion_figure(motion, FPS, "speed")
    with pytest.raises(ValueError, match="kind"):
        motion_figure(motion, FPS, "")
    for bad in ("position", "speed"):
        with pytest.raises(ValueError, match="kind"):
            timeline_figure(frames, FPS, bad)
        with pytest.raises(ValueError, match="kind"):
            error_figure(frames, FPS, bad, 1.0)
