"""Inverse of the generator. Units: cm, cm/s, s; fps in frames/s; boundaries in frames."""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

import numpy as np
import pyarrow as pa
from numpy.typing import NDArray

from vl_data.core.config import Config, FitConfig, ToleranceConfig
from vl_data.core.schemas import BYTES_PER_POINT
from vl_data.generator.body import cube_lattice
from vl_data.generator.drive import check_batch_fits
from vl_data.generator.models import MoveSegment, Scenario, Segment, StopSegment

_DECIMALS = 2  # speeds are rounded to 0.01 cm/s so that "exactly 0" is well defined
# Shortest segment, in frames (2 s at 16 frames/s), that the round trip is tested for (A14).
# Shorter segments can merge into one fitted piece or give an end speed beyond tolerance.
MIN_RELIABLE_SEGMENT_FRAMES = 32


@dataclass(frozen=True)
class Track:
    """Per-frame track recovered from the frames."""

    x_cm: NDArray[np.float64]  # body origin x per frame, cm
    velocity_cm_s: NDArray[np.float64]  # forward difference, cm/s, length n - 1
    y_cm: NDArray[np.float64]  # body origin y per frame, cm
    z_cm: NDArray[np.float64]  # body origin z per frame, cm


@dataclass(frozen=True)
class RoundTripReport:
    """Result of comparing an expected and an inferred scenario."""

    ok: bool
    problems: list[str]
    max_boundary_error_frames: int  # frames
    max_duration_error_frames: int  # frames
    max_end_speed_error_cm_s: float  # cm/s


@dataclass
class _Piece:
    """One fitted piece. frames is a count; speeds are cm/s at the piece boundaries."""

    frames: int
    is_stop: bool
    start_cm_s: float
    end_cm_s: float


def _points_arrays(frames: pa.Table | Iterable[pa.RecordBatch]) -> Iterator[pa.Array]:
    """Yield the chunks of the points column of a table, or of each record batch.

    Each yielded array holds one frame per entry (12 bytes per point, int32 cm). Raises
    ValueError if the column is missing or its type is not binary or large_binary.
    """
    if isinstance(frames, pa.Table):
        if "points" not in frames.column_names:
            raise ValueError("the table has no points column")
        for chunk in frames.column("points").chunks:
            _check_binary(chunk)
            yield chunk
        return
    for batch in frames:
        if "points" not in batch.schema.names:
            raise ValueError("the batch has no points column")
        column = batch.column(batch.schema.get_field_index("points"))
        _check_binary(column)
        yield column


def _check_binary(array: pa.Array) -> None:
    if not (pa.types.is_binary(array.type) or pa.types.is_large_binary(array.type)):
        raise ValueError(f"the points column must be binary or large_binary, not {array.type}")


def infer_track(frames: pa.Table | Iterable[pa.RecordBatch], config: Config) -> Track:
    """Recover body x, y, z (cm) and x velocity (cm/s) from the frames alone.

    Each coordinate is the mean of a frame's points minus the lattice centre, so it is the body
    origin in cm, the same reference as the generator's position. Velocity is the forward
    difference of x times fps (frames/s), length n - 1. frames is a table (any chunks) or an
    iterable of record batches; frames are read one row at a time, never as a whole chunk.
    Raises ValueError for no frames, a missing points column, a null points entry, a frame
    with no points, or a frame whose point count is not points_per_axis cubed, and also for a
    config whose frames would be too large for one Arrow binary array (checked first, so a huge
    points_per_axis cannot run out of memory).
    """
    check_batch_fits(config)  # before the lattice: a frame too big for Arrow cannot exist
    lattice = cube_lattice(config.body.size_cm, config.body.points_per_axis)
    centre = lattice.mean(axis=0, dtype=np.float64)  # cm, (x, y, z)
    expected = len(lattice)  # points per frame
    centroids: list[Any] = []  # cm, one (x, y, z) per frame
    for array in _points_arrays(frames):
        for i in range(len(array)):
            value = array[i]
            if not value.is_valid:
                raise ValueError(f"frame {len(centroids)} has a null points entry")
            buf = value.as_buffer()
            if buf.size == 0:
                raise ValueError("a frame has no points")
            if buf.size != expected * BYTES_PER_POINT:
                raise ValueError(
                    f"frame {len(centroids)} has {buf.size} bytes of points, expected "
                    f"{expected} points ({expected * BYTES_PER_POINT} bytes)"
                )
            points = np.frombuffer(buf, dtype="<i4").reshape(-1, 3)
            centroids.append(points.mean(axis=0, dtype=np.float64))
    if not centroids:
        raise ValueError("the table has no frames")
    xyz = np.asarray(centroids, dtype=np.float64) - centre
    return Track(
        x_cm=xyz[:, 0].copy(),
        velocity_cm_s=np.diff(xyz[:, 0]) * config.fps,
        y_cm=xyz[:, 1].copy(),
        z_cm=xyz[:, 2].copy(),
    )


def _prefix(values: NDArray[np.float64]) -> NDArray[np.float64]:
    return np.concatenate(([0.0], np.cumsum(values)))


def _partition(velocity: NDArray[np.float64], fit: FitConfig) -> list[tuple[int, int]]:
    """Penalised optimal partition of the velocity series into linear pieces.

    Cost of a piece = squared residual of its least-squares line / sigma^2 (sigma in cm/s)
    plus a penalty per piece; pieces are at least min_segment_frames long unless the whole
    series is shorter, which gives one piece. Returns (start, end) index pairs, end exclusive.
    The search is O(n^2) in the number of velocities n (fine for thousands of frames).
    """
    count = len(velocity)
    min_len = min(fit.min_segment_frames, count)
    t = np.arange(count, dtype=np.float64)
    p_t, p_v = _prefix(t), _prefix(velocity)
    p_tt, p_tv, p_vv = _prefix(t * t), _prefix(t * velocity), _prefix(velocity * velocity)
    best = np.full(count + 1, np.inf)
    best[0] = 0.0
    previous = np.zeros(count + 1, dtype=np.int64)
    sigma2 = fit.noise_sigma_cm_s**2
    for end in range(min_len, count + 1):
        starts = np.arange(0, end - min_len + 1)
        n = (end - starts).astype(np.float64)
        s_t = p_t[end] - p_t[starts]
        s_v = p_v[end] - p_v[starts]
        s_xx = (p_tt[end] - p_tt[starts]) - s_t * s_t / n
        s_xy = (p_tv[end] - p_tv[starts]) - s_t * s_v / n
        s_yy = (p_vv[end] - p_vv[starts]) - s_v * s_v / n
        flat = s_xx < 1e-9
        ssr = np.where(flat, s_yy, s_yy - s_xy * s_xy / np.where(flat, 1.0, s_xx))
        cost = best[starts] + np.maximum(ssr, 0.0) / sigma2 + fit.penalty
        pick = int(np.argmin(cost))
        best[end] = cost[pick]
        previous[end] = starts[pick]
    spans: list[tuple[int, int]] = []
    end = count
    while end > 0:
        start = int(previous[end])
        spans.append((start, end))
        end = start
    return spans[::-1]


def _fit_pieces(track: Track, fit: FitConfig) -> list[_Piece]:
    """Fit a line to every piece; speeds are extrapolated to the piece boundaries.

    A move reaches its end speed only on the first frame of the next segment, so the end
    speed is the line value at the boundary frame (the last piece ends one frame after
    its last velocity). A piece is a stop when its fitted speed stays under
    stop_speed_cm_s at both boundaries.
    """
    velocity = track.velocity_cm_s
    spans = _partition(velocity, fit)
    pieces: list[_Piece] = []
    for index, (start, end) in enumerate(spans):
        boundary = end + 1 if index == len(spans) - 1 else end  # frame index, end of piece
        if end - start >= 2:
            ts = np.arange(start, end, dtype=np.float64)
            slope, intercept = np.polyfit(ts, velocity[start:end], 1)
        else:
            slope, intercept = 0.0, float(velocity[start])
        first = round(float(intercept + slope * start), _DECIMALS)  # cm/s
        last = round(float(intercept + slope * boundary), _DECIMALS)  # cm/s
        is_stop = max(abs(first), abs(last)) < fit.stop_speed_cm_s
        pieces.append(_Piece(boundary - start, is_stop, first, last))
    return pieces


def _snap_to_rest(pieces: list[_Piece]) -> list[_Piece]:
    """Make the pieces satisfy the Scenario rules (see ASSUMPTIONS A53).

    Every stop has speed 0; the piece before a stop ends at 0 and the piece after a stop
    starts at 0 (cm/s). A move that would go from 0 to 0 becomes a stop. Repeats until
    nothing changes, then adjacent stops are merged by summing their frames.
    """
    changed = True
    while changed:
        changed = False
        for i, piece in enumerate(pieces):
            if piece.is_stop:
                piece.start_cm_s = piece.end_cm_s = 0.0
                if i > 0 and not pieces[i - 1].is_stop:
                    pieces[i - 1].end_cm_s = 0.0
                if i + 1 < len(pieces) and not pieces[i + 1].is_stop:
                    pieces[i + 1].start_cm_s = 0.0
        running = 0.0 if pieces[0].is_stop else pieces[0].start_cm_s  # cm/s
        for piece in pieces:
            if not piece.is_stop:
                if running == 0.0 and piece.end_cm_s == 0.0:
                    piece.is_stop = True
                    changed = True
                running = piece.end_cm_s
    return _merge_adjacent_stops(pieces)


def _merge_adjacent_stops(pieces: list[_Piece]) -> list[_Piece]:
    """Merge each run of consecutive stops into one stop (frames are summed)."""
    merged: list[_Piece] = []
    for piece in pieces:
        if piece.is_stop and merged and merged[-1].is_stop:
            merged[-1].frames += piece.frames
        else:
            merged.append(piece)
    return merged


def _fit(track: Track, fit: FitConfig, fps: int) -> tuple[float, list[Segment]]:
    """Return (start speed in cm/s, segments) after the snap rule."""
    if len(track.velocity_cm_s) < 1:
        raise ValueError("fitting needs at least 2 frames")
    pieces = _snap_to_rest(_fit_pieces(track, fit))
    start_speed = 0.0 if pieces[0].is_stop else pieces[0].start_cm_s  # cm/s
    segments: list[Segment] = [
        StopSegment(duration_s=p.frames / fps)
        if p.is_stop
        else MoveSegment(duration_s=p.frames / fps, end_speed_cm_s=p.end_cm_s)
        for p in pieces
    ]
    return start_speed, segments


def fit_segments(track: Track, fit: FitConfig, fps: int) -> list[Segment]:
    """Fit piecewise-linear speed pieces and return typed move/stop segments.

    Durations are whole frames (frames / fps, in s); end speeds are in cm/s, rounded to
    0.01 cm/s. Stops have speed 0 and the move before a stop ends at 0 (snap rule).
    Raises ValueError if the track has fewer than 2 frames.
    """
    return _fit(track, fit, fps)[1]


def infer_scenario(frames: pa.Table | Iterable[pa.RecordBatch], config: Config) -> Scenario:
    """infer_track then fit_segments, rebuilt as a Scenario (seed 0).

    Start: x, y and z = the recovered body origin of frame 0, rounded to whole cm (so the
    original start is recovered when it was in whole cm), speed = the first piece's start
    speed (cm/s) after the snap rule (0 for a first stop).
    """
    track = infer_track(frames, config)
    start_speed, segments = _fit(track, config.fit, config.fps)
    return Scenario.model_validate(
        {
            "seed": 0,
            "start": {
                "x_cm": float(round(track.x_cm[0])),
                "y_cm": float(round(track.y_cm[0])),
                "z_cm": float(round(track.z_cm[0])),
                "speed_cm_s": start_speed,
            },
            "segments": [s.model_dump() for s in segments],
        }
    )


def _frames(scenario: Scenario, fps: int) -> list[int]:
    """Whole-frame duration of each segment (frames); fps in frames/s."""
    return [round(seg.duration_s * fps) for seg in scenario.segments]


def short_segments(scenario: Scenario, fps: int) -> list[tuple[int, int]]:
    """List (segment index, frames) for segments under MIN_RELIABLE_SEGMENT_FRAMES frames.

    fps is in frames/s. The inverse is not guaranteed for these segments (ASSUMPTIONS A14).
    """
    return [
        (index, frames)
        for index, frames in enumerate(_frames(scenario, fps))
        if frames < MIN_RELIABLE_SEGMENT_FRAMES
    ]


def compare_scenarios(
    expected: Scenario, inferred: Scenario, tol: ToleranceConfig, fps: int
) -> RoundTripReport:
    """Compare segment types, count, boundaries, durations and end speeds to tolerances.

    Boundaries are segment end frames and durations are in frames (fps in frames/s);
    end speeds are in cm/s. Only segments are compared (not the start state). A different
    segment count stops the comparison, with all maxima reported as 0.
    """
    problems: list[str] = []
    if len(expected.segments) != len(inferred.segments):
        problems.append(
            f"segment count: expected {len(expected.segments)}, found {len(inferred.segments)}"
        )
        return RoundTripReport(False, problems, 0, 0, 0.0)
    exp_frames, inf_frames = _frames(expected, fps), _frames(inferred, fps)
    exp_ends = np.cumsum(exp_frames).tolist()  # frames
    inf_ends = np.cumsum(inf_frames).tolist()  # frames
    max_boundary, max_duration, max_speed = 0, 0, 0.0
    last = len(exp_frames) - 1
    for i, (exp, inf) in enumerate(zip(expected.segments, inferred.segments, strict=True)):
        if exp.type != inf.type:
            problems.append(f"segment {i}: expected {exp.type}, found {inf.type}")
            continue
        if i < last:
            error = abs(exp_ends[i] - inf_ends[i])  # frames
            max_boundary = max(max_boundary, error)
            if error > tol.boundary_frames:
                problems.append(
                    f"segment {i} boundary: expected {exp_ends[i]} frames, found {inf_ends[i]} "
                    f"(error {error} > limit {tol.boundary_frames})"
                )
        d_error = abs(exp_frames[i] - inf_frames[i])  # frames
        max_duration = max(max_duration, d_error)
        if d_error > tol.duration_frames:
            problems.append(
                f"segment {i} duration: expected {exp_frames[i]} frames, found {inf_frames[i]} "
                f"(error {d_error} > limit {tol.duration_frames})"
            )
        if isinstance(exp, MoveSegment) and isinstance(inf, MoveSegment):
            s_error = abs(exp.end_speed_cm_s - inf.end_speed_cm_s)  # cm/s
            max_speed = max(max_speed, s_error)
            if s_error > tol.end_speed_cm_s:
                problems.append(
                    f"segment {i} end speed: expected {exp.end_speed_cm_s:.1f} cm/s, "
                    f"found {inf.end_speed_cm_s:.1f} cm/s "
                    f"(error {s_error:.1f} > limit {tol.end_speed_cm_s:.1f})"
                )
    return RoundTripReport(not problems, problems, max_boundary, max_duration, max_speed)
