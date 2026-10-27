"""Sliding-window polynomial estimate of velocity and acceleration from positions.

Units: time in seconds, position in cm, velocity in cm/s, acceleration in cm/s^2.
"""

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from vl_data.usecase.config import MotionConfig

_EPS_FRAMES = 1e-6  # fraction of a frame interval; tolerance on window edges and gaps


@dataclass(frozen=True)
class MotionEstimate:
    """Per-frame motion estimates: velocity cm/s, acceleration cm/s^2, x cm.

    NaN where the window is too sparse or gappy.
    """

    velocity_cm_s: NDArray[np.float64]  # cm/s, signed
    acceleration_cm_s2: NDArray[np.float64]  # cm/s^2, signed
    x_smooth_cm: NDArray[np.float64]  # cm, fitted position at each frame time
    n_points: NDArray[np.int32]  # samples in each window (counted even when NaN)


def estimate_motion(
    t_s: NDArray[np.float64],
    x_cm: NDArray[np.float64],
    fps: int,
    cfg: MotionConfig,
) -> MotionEstimate:
    """Fit a local polynomial around each sample and read off position, speed and acceleration.

    t_s: sample times in seconds, strictly increasing (frames may be missing).
    x_cm: positions in cm, same length as t_s.
    fps: frame rate in frames/s; one frame interval is 1/fps seconds.
    cfg: window size, order, minimum points and gap limit (all in frames).

    The window for sample i holds the samples within window_frames/2 frame intervals either
    side of t_i, so it is centred, and one-sided (about half the points) at the ends.
    Outputs are NaN when the window has fewer than cfg.min_points points or any gap between
    consecutive window points exceeds cfg.max_gap_frames frame intervals.
    Raises ValueError for fps < 1, non-1-D input, mismatched lengths, non-finite values or
    non-increasing times.
    """
    if fps < 1:
        raise ValueError("fps must be at least 1")
    t = np.asarray(t_s, dtype=np.float64)
    x = np.asarray(x_cm, dtype=np.float64)
    if t.ndim != 1 or x.ndim != 1:
        raise ValueError("t_s and x_cm must be 1-D")
    if t.shape != x.shape:
        raise ValueError("t_s and x_cm must have the same length")
    if not np.all(np.isfinite(t)):
        raise ValueError("t_s must be finite")
    if not np.all(np.isfinite(x)):
        raise ValueError("x_cm must be finite")
    if t.size > 1 and not np.all(np.diff(t) > 0):
        raise ValueError("t_s must be strictly increasing")

    n = t.size
    velocity = np.full(n, np.nan)
    acceleration = np.full(n, np.nan)
    x_smooth = np.full(n, np.nan)
    counts = np.zeros(n, dtype=np.int32)

    eps_s = _EPS_FRAMES / fps  # seconds
    half_s = cfg.window_frames / 2 / fps + eps_s  # seconds
    max_gap_s = cfg.max_gap_frames / fps + eps_s  # seconds
    for i in range(n):
        lo = int(np.searchsorted(t, t[i] - half_s, side="left"))
        hi = int(np.searchsorted(t, t[i] + half_s, side="right"))
        counts[i] = hi - lo
        if counts[i] < cfg.min_points:
            continue
        if np.any(np.diff(t[lo:hi]) > max_gap_s):
            continue
        offsets = (t[lo:hi] - t[i]) * fps  # frame intervals, keeps the fit well conditioned
        design = np.vander(offsets, N=cfg.poly_order + 1, increasing=True)
        coef, *_ = np.linalg.lstsq(design, x[lo:hi], rcond=None)
        x_smooth[i] = coef[0]
        velocity[i] = coef[1] * fps
        acceleration[i] = 2.0 * coef[2] * fps**2
    return MotionEstimate(velocity, acceleration, x_smooth, counts)
