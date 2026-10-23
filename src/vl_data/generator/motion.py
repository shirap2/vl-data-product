"""Motion along x. Units: cm, cm/s, cm/s^2, s; fps in frames/s."""

import math

import numpy as np
from numpy.typing import NDArray

from vl_data.generator.models import MAX_FRAMES, MoveSegment, Scenario

_FRAME_TOLERANCE = 1e-9  # frames


def _segment_frames(scenario: Scenario, fps: int) -> list[int]:
    """Frames per segment (duration in s times fps in frames/s).

    ValueError if fps <= 0, a duration is not whole frames, or the total exceeds MAX_FRAMES.
    """
    if fps <= 0:
        raise ValueError(f"fps must be positive, got {fps}")
    counts: list[int] = []
    for index, seg in enumerate(scenario.segments):
        scaled = seg.duration_s * fps  # frames
        if not math.isfinite(scaled):
            raise ValueError(f"segment {index}: duration is not finite")
        frames = round(scaled)
        if frames < 1 or abs(scaled - frames) > _FRAME_TOLERANCE:
            raise ValueError(
                f"segment {index}: {seg.duration_s} s is not a whole number of frames at {fps} fps"
            )
        counts.append(frames)
    if sum(counts) > MAX_FRAMES:
        raise ValueError(
            f"scenario has {sum(counts)} frames at {fps} fps, more than MAX_FRAMES ({MAX_FRAMES})"
        )
    return counts


def frame_count(scenario: Scenario, fps: int) -> int:
    """Total frames: the sum of segment durations in frames (fps in frames/s)."""
    return sum(_segment_frames(scenario, fps))


def speed_profile(scenario: Scenario, fps: int) -> NDArray[np.float64]:
    """Signed speed in cm/s for every frame (linear ramp inside each segment).

    A move of n frames from a to b gives a + i * (b - a) / n at frame i, so b is reached
    on the first frame of the next segment. A stop gives 0.0.
    """
    pieces: list[NDArray[np.float64]] = []
    speed = scenario.start.speed_cm_s  # cm/s
    for seg, frames in zip(scenario.segments, _segment_frames(scenario, fps), strict=True):
        if isinstance(seg, MoveSegment):
            pieces.append(np.linspace(speed, seg.end_speed_cm_s, frames, endpoint=False))
            speed = seg.end_speed_cm_s
        else:
            pieces.append(np.zeros(frames, dtype=np.float64))
            speed = 0.0
    return np.concatenate(pieces).astype(np.float64)


def integrate_position(
    start_x_cm: float, speed_cm_s: NDArray[np.float64], fps: int
) -> NDArray[np.float64]:
    """Forward-Euler x position in cm: x[0] = start, x[k+1] = x[k] + v[k] / fps."""
    steps = speed_cm_s[:-1] / fps  # cm per frame
    return start_x_cm + np.concatenate(([0.0], np.cumsum(steps)))


def ground_truth_arrays(
    x_cm: NDArray[np.float64], speed_cm_s: NDArray[np.float64], fps: int
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Return (velocity_cm_s, acceleration_cm_s2) per frame, by forward differences.

    velocity[k] = (x[k+1] - x[k]) * fps and acceleration[k] = (speed[k+1] - speed[k]) * fps;
    the last frame repeats the previous value. One frame: velocity = speed, acceleration 0.
    """
    if len(x_cm) == 1:
        return np.array([speed_cm_s[0]], dtype=np.float64), np.array([0.0])
    velocity = np.diff(x_cm) * fps
    acceleration = np.diff(speed_cm_s) * fps
    return (
        np.concatenate((velocity, velocity[-1:])),
        np.concatenate((acceleration, acceleration[-1:])),
    )
