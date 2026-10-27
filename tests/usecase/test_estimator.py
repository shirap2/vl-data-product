"""Tests for the sliding-window polynomial motion estimator (cm, s, cm/s, cm/s^2)."""

from typing import Any

import numpy as np
import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from vl_data.core.config import BodyConfig, Config
from vl_data.usecase.config import MotionConfig, load_motion_config
from vl_data.usecase.estimator import estimate_motion

from .helpers import load_sweep_tool

FPS = 100
CFG = MotionConfig(window_frames=16, poly_order=2, min_points=5, max_gap_frames=4)


def _times(n: int) -> np.ndarray:
    return np.arange(n, dtype=np.float64) / FPS


def test_constant_speed_exact() -> None:
    t = _times(60)
    x = 10.0 + 100.0 * t
    est = estimate_motion(t, x, FPS, CFG)
    assert np.allclose(est.velocity_cm_s, 100.0, atol=1e-6)
    assert np.allclose(est.acceleration_cm_s2, 0.0, atol=1e-6)
    assert np.allclose(est.x_smooth_cm, x, atol=1e-6)


def test_constant_acceleration_exact() -> None:
    t = _times(60)
    x0, v0, a = 5.0, 20.0, 300.0
    x = x0 + v0 * t + 0.5 * a * t**2
    est = estimate_motion(t, x, FPS, CFG)
    full = slice(8, 60 - 8)
    assert np.allclose(est.velocity_cm_s[full], v0 + a * t[full], atol=1e-6)
    assert np.allclose(est.acceleration_cm_s2[full], a, atol=1e-6)
    assert np.all(est.n_points[full] == 17)
    assert est.n_points.dtype == np.int32


def test_signed_backward() -> None:
    t = _times(60)
    est = estimate_motion(t, 500.0 - 150.0 * t, FPS, CFG)
    assert np.allclose(est.velocity_cm_s, -150.0, atol=1e-6)
    # Reversal: v = 100 - 400 t crosses zero at t = 0.25 s.
    x = 100.0 * t - 200.0 * t**2
    rev = estimate_motion(t, x, FPS, CFG)
    assert rev.velocity_cm_s[5] > 0
    assert rev.velocity_cm_s[55] < 0
    assert np.allclose(rev.velocity_cm_s, 100.0 - 400.0 * t, atol=1e-6)
    assert np.allclose(rev.acceleration_cm_s2, -400.0, atol=1e-6)


def test_missing_frames_use_timestamps() -> None:
    t_full = _times(60)
    keep = np.ones(60, dtype=bool)
    keep[30] = False
    t = t_full[keep]
    x = 5.0 + 20.0 * t + 150.0 * t**2
    est = estimate_motion(t, x, FPS, CFG)
    assert np.allclose(est.velocity_cm_s, 20.0 + 300.0 * t, atol=1e-6)
    assert np.allclose(est.acceleration_cm_s2, 300.0, atol=1e-6)
    near = int(np.searchsorted(t, t_full[29]))
    assert est.n_points[near] == 16


def test_sparse_window_is_nan_by_min_points_only() -> None:
    # Gap limit is large, so only min_points can reject: stride 5 leaves 3 points per window.
    cfg = MotionConfig(window_frames=16, poly_order=2, min_points=5, max_gap_frames=10)
    t = _times(60)[::5]
    est = estimate_motion(t, 2.0 * t, FPS, cfg)
    assert est.n_points.max() == 3
    assert np.all(est.n_points < cfg.min_points)
    assert np.all(np.isnan(est.velocity_cm_s))
    assert np.all(np.isnan(est.acceleration_cm_s2))
    assert np.all(np.isnan(est.x_smooth_cm))
    # Same data with min_points lowered to 3: interior windows (3 points) now fit.
    ok = estimate_motion(t, 2.0 * t, FPS, cfg.model_copy(update={"min_points": 3}))
    assert np.array_equal(ok.n_points, est.n_points)
    interior = ok.n_points == 3
    assert interior.any()
    assert np.all(np.isfinite(ok.velocity_cm_s[interior]))


def test_min_points_boundary_at_edge_frames() -> None:
    t = _times(60)
    x = 3.0 + 40.0 * t
    at = MotionConfig(window_frames=16, poly_order=2, min_points=9, max_gap_frames=4)
    over = MotionConfig(window_frames=16, poly_order=2, min_points=10, max_gap_frames=4)
    est_at = estimate_motion(t, x, FPS, at)
    est_over = estimate_motion(t, x, FPS, over)
    edges = [0, 59]
    assert np.all(est_at.n_points[edges] == 9)
    assert np.all(np.isfinite(est_at.velocity_cm_s))
    assert np.all(np.isnan(est_over.velocity_cm_s[edges]))
    assert np.all(np.isnan(est_over.x_smooth_cm[edges]))
    assert np.all(np.isfinite(est_over.velocity_cm_s[1:59][est_over.n_points[1:59] >= 10]))
    assert np.all(est_over.n_points[edges] == 9)


def _gap_run(gap_frames: int) -> np.ndarray:
    # Frames 0..19 then 19 + gap_frames .. 38 + gap_frames; spacing across the hole = gap_frames.
    frames = np.concatenate([np.arange(0, 20), np.arange(19 + gap_frames, 39 + gap_frames)])
    tt = frames / FPS
    return estimate_motion(tt, 10.0 * tt, FPS, CFG).velocity_cm_s


def test_gap_at_limit_is_accepted() -> None:
    assert np.all(np.isfinite(_gap_run(4)))


def test_gap_over_limit_is_nan_exactly_where_window_spans_it() -> None:
    over = _gap_run(5)
    # Window is +-8 frames; it spans the hole when it holds frame 19 and frame 24:
    # frames 16..19 (indices 16..19) and 24..27 (indices 20..23).
    expected_nan = np.zeros(40, dtype=bool)
    expected_nan[16:24] = True
    assert np.array_equal(np.isnan(over), expected_nan)
    assert np.all(np.isfinite(over[~expected_nan]))


def test_large_time_offset_keeps_window_and_gap_rules() -> None:
    fps = 16
    frames = np.concatenate([np.arange(0, 20), np.arange(23, 43)])  # spacing 4 = the limit
    s = frames / fps
    t = 1e6 + s
    x = 5.0 + 20.0 * s + 150.0 * s**2
    cfg = MotionConfig(window_frames=16, poly_order=2, min_points=5, max_gap_frames=4)
    est = estimate_motion(t, x, fps, cfg)
    assert np.all(np.isfinite(est.velocity_cm_s))
    # Loose atol 1e-4: t has ~1e-10 s resolution at 1e6 s.
    assert np.allclose(est.velocity_cm_s, 20.0 + 300.0 * s, atol=1e-4)
    assert np.allclose(est.acceleration_cm_s2, 300.0, atol=1e-4)
    base = estimate_motion(s, x, fps, cfg)
    assert np.array_equal(est.n_points, base.n_points)


def test_timestamp_jitter_within_tolerance_keeps_window_edges_and_gap_limit() -> None:
    # Every time is 3e-8 too large, so a window edge or a gap of exactly the limit lies about
    # 2e-9 s past its bound; the tolerance of 1e-6 frame intervals (1e-8 s at 100 fps) absorbs it.
    stretch = 1.0 + 3e-8
    frames = np.concatenate([np.arange(0, 20), np.arange(23, 43)])  # spacing 4 = the limit
    t = frames / FPS * stretch
    est = estimate_motion(t, 10.0 * t, FPS, CFG)
    exact = estimate_motion(frames / FPS, 10.0 * (frames / FPS), FPS, CFG)
    assert np.array_equal(est.n_points, exact.n_points)
    assert np.all(np.isfinite(est.velocity_cm_s))


def test_edges_use_one_sided_window() -> None:
    t = _times(60)
    x = 3.0 + 40.0 * t + 100.0 * t**2
    est = estimate_motion(t, x, FPS, CFG)
    for i in (0, 59):
        assert np.isfinite(est.velocity_cm_s[i])
    assert est.n_points[0] == 9
    assert est.n_points[59] == 9
    assert est.n_points[30] == 17
    assert est.velocity_cm_s[0] == pytest.approx(40.0, abs=1e-6)
    assert est.velocity_cm_s[59] == pytest.approx(40.0 + 200.0 * t[59], abs=1e-6)


def test_cubic_with_order_three() -> None:
    cfg = MotionConfig(window_frames=16, poly_order=3, min_points=6, max_gap_frames=4)
    t = _times(60)
    x = 1.0 + 2.0 * t + 30.0 * t**2 + 80.0 * t**3
    est = estimate_motion(t, x, FPS, cfg)
    assert np.allclose(est.velocity_cm_s, 2.0 + 60.0 * t + 240.0 * t**2, atol=1e-5)
    assert np.allclose(est.acceleration_cm_s2, 60.0 + 480.0 * t, atol=1e-4)


def test_length_mismatch_rejected() -> None:
    with pytest.raises(ValueError):
        estimate_motion(_times(10), np.zeros(9), FPS, CFG)


def test_non_increasing_times_rejected() -> None:
    t = _times(10)
    t[5] = t[4]
    with pytest.raises(ValueError):
        estimate_motion(t, np.zeros(10), FPS, CFG)


def test_non_1d_rejected() -> None:
    with pytest.raises(ValueError):
        estimate_motion(np.zeros((2, 2)), np.zeros((2, 2)), FPS, CFG)


@pytest.mark.parametrize("fps", [0, -5])
def test_fps_below_one_rejected(fps: int) -> None:
    with pytest.raises(ValueError, match="fps"):
        estimate_motion(_times(10), np.zeros(10), fps, CFG)


@pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf])
def test_non_finite_times_rejected(bad: float) -> None:
    t = _times(10)
    t[3] = bad
    with pytest.raises(ValueError, match="t_s"):
        estimate_motion(t, np.zeros(10), FPS, CFG)


@pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf])
def test_non_finite_positions_rejected(bad: float) -> None:
    x = np.zeros(10)
    x[3] = bad
    with pytest.raises(ValueError, match="x_cm"):
        estimate_motion(_times(10), x, FPS, CFG)


def test_empty_input_returns_empty() -> None:
    est = estimate_motion(np.array([]), np.array([]), FPS, CFG)
    assert est.velocity_cm_s.shape == (0,)
    assert est.acceleration_cm_s2.shape == (0,)
    assert est.x_smooth_cm.shape == (0,)
    assert est.n_points.shape == (0,)


sweep: Any = load_sweep_tool()
_SMALL = Config(body=BodyConfig(size_cm=100, points_per_axis=10))
# Legs (is_stop, frames, end speed cm/s): the same rules as the sweep's random search.
_legs = st.lists(
    st.tuples(
        st.booleans(),  # True: stop (after slowing to 0 if moving), False: move
        st.integers(min_value=sweep.LEG_MIN_FRAMES, max_value=sweep.LEG_MAX_FRAMES),  # frames
        st.integers(min_value=-sweep.LEG_MAX_SPEED_CM_S, max_value=sweep.LEG_MAX_SPEED_CM_S),
    ),
    min_size=1,
    max_size=sweep.MAX_LEGS,
)


# Regression bounds for the property test, from the 2026-10-25 development measurement (1.5 x the
# largest interior max error of the shipped window and order over the sweep drives and a
# 1,502-scenario random search, rounded up to 0.1). They guard the estimator against drift; the
# pipeline does not use them.
_VELOCITY_BOUND_CM_S = 19.4  # cm/s
_ACCELERATION_BOUND_CM_S2 = 31.9  # cm/s^2


def _check_within_yaml(legs: list[tuple[bool, int, int]]) -> None:
    drive = sweep.drive_from_scenario("random", sweep.scenario_from_legs(legs), _SMALL)
    cfg = load_motion_config()
    errors = sweep.measure_drive(drive, cfg)
    assert errors.frames_interior > 0
    assert errors.velocity_interior.max_abs <= _VELOCITY_BOUND_CM_S
    assert errors.acceleration_interior.max_abs <= _ACCELERATION_BOUND_CM_S2


@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@example(legs=[(False, 48, 2)])  # first counterexample: slow motion, 12.9 cm/s^2 error
@example(legs=[(False, 96, -68)] * 5)  # worst of a 1,500-example search, 19.53 cm/s^2
@given(legs=_legs)
def test_property_matches_truth_within_bound(legs: list[tuple[bool, int, int]]) -> None:
    _check_within_yaml(legs)


@pytest.mark.parametrize("legs", [list(p) for p in sweep.PINNED_LEGS])
def test_pinned_counterexamples_within_the_regression_bounds(
    legs: list[tuple[bool, int, int]],
) -> None:
    _check_within_yaml(legs)
    # The slow-motion case really is above the bound the two sweep drives alone would give.
    drive = sweep.drive_from_scenario("pinned", sweep.scenario_from_legs(legs), _SMALL)
    assert sweep.measure_drive(drive, load_motion_config()).acceleration_interior.max_abs > 6.6
