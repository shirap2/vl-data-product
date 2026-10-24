"""Tests for the inverse functions: infer_track, fit_segments, compare_scenarios.

Units: cm, cm/s, s; fps in frames/s; boundaries and durations in frames.
"""

from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from vl_data.core.config import Config, load_config
from vl_data.core.schemas import FRAMES_SCHEMA, encode_points
from vl_data.generator.drive import Drive, generate
from vl_data.generator.inverse import (
    MIN_RELIABLE_SEGMENT_FRAMES,
    Track,
    compare_scenarios,
    fit_segments,
    infer_scenario,
    infer_track,
    short_segments,
)
from vl_data.generator.models import MoveSegment, Scenario, StopSegment, load_scenario

DEMO = Path(__file__).resolve().parents[2] / "src/vl_data/generator/scenarios/demo.yaml"
DEMO_BOUNDARIES = [32, 128, 288, 352, 480, 576, 656]  # frames, true segment starts after the first


@pytest.fixture(scope="module")
def config() -> Config:
    return load_config()


@pytest.fixture(scope="module")
def demo_drive(config: Config) -> Drive:
    return generate(load_scenario(DEMO), config)


def _scenario(start_speed: float, segments: list[dict[str, Any]], start_x: float = 0.0) -> Scenario:
    return Scenario.model_validate(
        {
            "seed": 0,
            "start": {"x_cm": start_x, "y_cm": 0.0, "z_cm": 0.0, "speed_cm_s": start_speed},
            "segments": segments,
        }
    )


def _move(frames: int, end: float) -> dict[str, Any]:
    return {"type": "move", "duration_s": frames / 16, "end_speed_cm_s": end}


def _stop(frames: int) -> dict[str, Any]:
    return {"type": "stop", "duration_s": frames / 16}


def _track(velocity: list[float]) -> Track:
    v = np.asarray(velocity, dtype=np.float64)
    return Track(x_cm=np.zeros(len(v) + 1), velocity_cm_s=v)


def _starts(scenario: Scenario, fps: int = 16) -> list[int]:
    """Start frame of every segment after the first."""
    out: list[int] = []
    total = 0
    for seg in scenario.segments[:-1]:
        total += round(seg.duration_s * fps)
        out.append(total)
    return out


# ---------------------------------------------------------------- infer_track


def test_infer_track_within_tolerance(demo_drive: Drive, config: Config) -> None:
    """Centroid x error at most 0.6 cm, velocity error at most 17 cm/s."""
    track = infer_track(demo_drive.frames, config)
    gt_x = demo_drive.ground_truth.column("x_cm").to_numpy()
    gt_v = demo_drive.ground_truth.column("velocity_cm_s").to_numpy()
    assert len(track.x_cm) == 1000
    assert len(track.velocity_cm_s) == 999
    assert np.max(np.abs(track.x_cm - gt_x)) <= config.tolerances.position_cm
    assert np.max(np.abs(track.velocity_cm_s - gt_v[:-1])) <= config.tolerances.velocity_cm_s


def test_infer_track_multi_chunk_equals_single_chunk(demo_drive: Drive, config: Config) -> None:
    """A table made of several uneven chunks gives the same track as one chunk."""
    parts = [demo_drive.frames.slice(0, 1), demo_drive.frames.slice(1, 299)]
    parts.append(demo_drive.frames.slice(300))
    chunked = pa.concat_tables(parts)
    assert chunked.column("points").num_chunks > 1
    single = infer_track(demo_drive.frames.combine_chunks(), config)
    multi = infer_track(chunked, config)
    assert np.array_equal(single.x_cm, multi.x_cm)
    assert np.array_equal(single.velocity_cm_s, multi.velocity_cm_s)


def test_infer_track_one_frame_has_empty_velocity(demo_drive: Drive, config: Config) -> None:
    track = infer_track(demo_drive.frames.slice(0, 1), config)
    assert len(track.x_cm) == 1
    assert len(track.velocity_cm_s) == 0


def test_infer_track_empty_table_rejected(config: Config) -> None:
    with pytest.raises(ValueError, match="no frames"):
        infer_track(FRAMES_SCHEMA.empty_table(), config)


def test_infer_track_frame_without_points_rejected(config: Config) -> None:
    empty = encode_points(np.zeros((0, 3), dtype=np.int32))
    table = pa.table(
        {"frame_id": [0], "timestamp_ns": [0], "points": pa.array([empty], type=pa.binary())},
        schema=FRAMES_SCHEMA,
    )
    with pytest.raises(ValueError, match="no points"):
        infer_track(table, config)


# ---------------------------------------------------------------- fit_segments


def test_fit_segments_demo(demo_drive: Drive, config: Config) -> None:
    """The demo gives 8 segments with the right types and boundaries."""
    segments = fit_segments(infer_track(demo_drive.frames, config), config.fit, config.fps)
    assert [s.type for s in segments] == ["stop"] + ["move"] * 6 + ["stop"]
    starts = np.cumsum([round(s.duration_s * config.fps) for s in segments])[:-1]
    assert len(starts) == len(DEMO_BOUNDARIES)
    assert np.max(np.abs(starts - np.array(DEMO_BOUNDARIES))) <= config.tolerances.boundary_frames
    # constant-speed moves stay moves with a matching speed; the reversal is one move
    moves = [s for s in segments if isinstance(s, MoveSegment)]
    assert abs(moves[1].end_speed_cm_s - 600.0) <= config.tolerances.end_speed_cm_s
    assert abs(moves[4].end_speed_cm_s + 300.0) <= config.tolerances.end_speed_cm_s
    assert moves[-1].end_speed_cm_s == 0.0  # snapped: the move before the final stop ends at rest


def test_fit_segments_single_piece_when_series_is_short(config: Config) -> None:
    segments = fit_segments(_track([300.0] * 5), config.fit, config.fps)
    assert len(segments) == 1
    assert isinstance(segments[0], MoveSegment)
    assert segments[0].end_speed_cm_s == pytest.approx(300.0)
    assert segments[0].duration_s * config.fps == pytest.approx(6)  # 5 velocities + last frame


def test_fit_segments_single_frame_pair_rejected(config: Config) -> None:
    with pytest.raises(ValueError, match="at least 2 frames"):
        fit_segments(_track([]), config.fit, config.fps)


def test_fit_segments_one_velocity_is_one_piece(config: Config) -> None:
    segments = fit_segments(_track([100.0]), config.fit, config.fps)
    assert [s.type for s in segments] == ["move"]


def test_fit_segments_all_stop(config: Config) -> None:
    segments = fit_segments(_track([0.0] * 40), config.fit, config.fps)
    assert len(segments) == 1
    assert isinstance(segments[0], StopSegment)
    assert segments[0].duration_s * config.fps == pytest.approx(41)


def test_snap_to_rest_merges_move_zero_to_zero_into_stops(config: Config) -> None:
    """A flat 40 cm/s piece between two stops would become a move 0->0; it is merged."""
    velocity = [0.0] * 40 + [40.0] * 20 + [0.0] * 40
    segments = fit_segments(_track(velocity), config.fit, config.fps)
    assert len(segments) == 1
    assert isinstance(segments[0], StopSegment)
    assert segments[0].duration_s * config.fps == pytest.approx(101)


def test_snap_to_rest_forces_end_speed_before_stop_to_zero(config: Config) -> None:
    """A move that ends beside a stop ends at exactly 0 cm/s, whatever the fit said."""
    velocity = [0.0] * 40 + list(np.linspace(0.0, 300.0, 40, endpoint=False))
    velocity += list(np.linspace(300.0, 40.0, 40, endpoint=False)) + [0.0] * 40
    segments = fit_segments(_track(velocity), config.fit, config.fps)
    assert [s.type for s in segments] == ["stop", "move", "move", "stop"]
    assert isinstance(segments[2], MoveSegment)
    assert segments[2].end_speed_cm_s == 0.0
    assert isinstance(segments[1], MoveSegment)
    assert segments[1].end_speed_cm_s == pytest.approx(300.0, abs=1.0)


def test_snapped_scenario_always_validates(config: Config) -> None:
    """Whatever the fit finds, the result passes the Scenario validator."""
    rng = np.random.default_rng(1)
    for _ in range(20):
        pieces = rng.integers(2, 6)
        velocity: list[float] = []
        for _p in range(pieces):
            level = float(rng.choice([0.0, 20.0, 45.0, 200.0, -150.0]))
            velocity += [level] * int(rng.integers(10, 30))
        segments = fit_segments(_track(velocity), config.fit, config.fps)
        assert segments  # fit_segments returns segments; Scenario below must accept them
        start = 0.0 if isinstance(segments[0], StopSegment) else float(velocity[0])
        Scenario.model_validate(
            {"start": {"speed_cm_s": start}, "segments": [s.model_dump() for s in segments]}
        )


# ----------------------------------------------------------- infer_scenario


def test_infer_scenario_start_state_and_seed(demo_drive: Drive, config: Config) -> None:
    inferred = infer_scenario(demo_drive.frames, config)
    assert inferred.seed == 0
    assert inferred.start.x_cm == 0.0
    assert inferred.start.y_cm == 0.0
    assert inferred.start.z_cm == 0.0
    assert inferred.start.speed_cm_s == 0.0


def test_infer_scenario_recovers_start_x_and_start_speed(config: Config) -> None:
    scenario = _scenario(200.0, [_move(64, 600.0), _move(96, 0.0), _stop(64)], start_x=50.0)
    drive = generate(scenario, config)
    inferred = infer_scenario(drive.frames, config)
    assert inferred.start.x_cm == 50.0
    assert inferred.start.speed_cm_s == pytest.approx(200.0, abs=config.tolerances.end_speed_cm_s)
    report = compare_scenarios(scenario, inferred, config.tolerances, config.fps)
    assert report.ok, report.problems


def test_roundtrip_demo_within_tolerance(demo_drive: Drive, config: Config) -> None:
    """Boundary, duration and end-speed errors are within the configured tolerances."""
    inferred = infer_scenario(demo_drive.frames, config)
    report = compare_scenarios(demo_drive.scenario, inferred, config.tolerances, config.fps)
    assert report.ok, report.problems
    assert report.problems == []
    assert report.max_boundary_error_frames <= config.tolerances.boundary_frames
    assert report.max_duration_error_frames <= config.tolerances.duration_frames
    assert report.max_end_speed_error_cm_s <= config.tolerances.end_speed_cm_s


# ---------------------------------------------------------- compare_scenarios


def _two(a: list[dict[str, Any]], b: list[dict[str, Any]]) -> tuple[Scenario, Scenario]:
    return _scenario(0.0, a), _scenario(0.0, b)


def test_compare_identical_is_ok(config: Config) -> None:
    a, b = _two([_stop(20), _move(40, 100.0)], [_stop(20), _move(40, 100.0)])
    report = compare_scenarios(a, b, config.tolerances, config.fps)
    assert report.ok
    assert (report.max_boundary_error_frames, report.max_duration_error_frames) == (0, 0)
    assert report.max_end_speed_error_cm_s == 0.0


def test_compare_type_mismatch(config: Config) -> None:
    a, b = _two([_stop(20), _move(40, 100.0)], [_stop(20), _stop(40)])
    report = compare_scenarios(a, b, config.tolerances, config.fps)
    assert not report.ok
    assert report.problems == ["segment 1: expected move, found stop"]


def test_compare_count_mismatch(config: Config) -> None:
    a, b = _two([_stop(20), _move(40, 100.0)], [_stop(20), _move(20, 100.0), _move(20, 200.0)])
    report = compare_scenarios(a, b, config.tolerances, config.fps)
    assert not report.ok
    assert report.problems == ["segment count: expected 2, found 3"]


def test_compare_boundary_and_duration_errors(config: Config) -> None:
    a, b = _two(
        [_move(20, 100.0), _move(40, 0.0), _stop(40)],
        [_move(40, 100.0), _move(30, 0.0), _stop(30)],
    )
    report = compare_scenarios(a, b, config.tolerances, config.fps)
    assert not report.ok
    assert report.max_boundary_error_frames == 20
    assert report.max_duration_error_frames == 20
    problems = report.problems
    assert any(p.startswith("segment 0 boundary: expected 20 frames, found 40") for p in problems)
    assert any(p.startswith("segment 1 boundary: expected 60 frames, found 70") for p in problems)
    assert any(p.startswith("segment 0 duration: expected 20 frames, found 40") for p in problems)
    assert any(p.startswith("segment 2 duration: expected 40 frames, found 30") for p in problems)


def test_compare_end_speed_error(config: Config) -> None:
    a, b = _two([_move(40, 100.0)], [_move(40, 100.0 + 50.0)])
    report = compare_scenarios(a, b, config.tolerances, config.fps)
    assert not report.ok
    assert report.max_end_speed_error_cm_s == 50.0
    assert len(report.problems) == 1
    assert report.problems[0].startswith("segment 0 end speed: expected 100.0 cm/s, found 150.0")


def test_compare_within_tolerance_edges_pass(config: Config) -> None:
    tol = config.tolerances
    a, b = _two(
        [_move(40, 100.0), _move(40, 400.0)],
        [
            _move(40 + tol.boundary_frames, 100.0 + tol.end_speed_cm_s),
            _move(40 - tol.boundary_frames, 400.0),
        ],
    )
    report = compare_scenarios(a, b, tol, config.fps)
    assert report.ok, report.problems
    assert report.max_boundary_error_frames == tol.boundary_frames


# ------------------------------------------------------------------ property


@st.composite
def _scenarios(draw: st.DrawFn) -> Scenario:
    """Valid scenarios (search domain, ASSUMPTIONS A14). Up to 7 draws, each adding one segment
    or being skipped; at most 1,600 frames in total; 32 to 160 frames per segment; move end
    speed a multiple of 10 cm/s in +-600 cm/s, nonzero end speed at least 100 cm/s in size;
    a stop only at speed 0 and not after a stop; no 0 -> 0 move; a slope change of at least
    50 cm/s^2 from the previous segment (a stop has slope 0). The running slope starts at 0, so
    a first move also needs a slope of at least 50 cm/s^2 in size, while a first stop is not
    checked. If every draw is skipped, the fallback is one 16-frame stop, which is below the
    32-frame minimum.
    """
    segments: list[dict[str, Any]] = []
    speed = 0.0  # cm/s, running speed
    prev_slope = 0.0  # cm/s^2
    total = 0  # frames
    for _ in range(draw(st.integers(min_value=1, max_value=7))):
        frames = draw(st.integers(min_value=MIN_RELIABLE_SEGMENT_FRAMES, max_value=160))
        end_choice = draw(st.integers(min_value=-60, max_value=60)) * 10.0  # cm/s
        make_stop = draw(st.booleans())
        if total + frames > 1600:
            break
        if make_stop:
            if speed != 0.0 or prev_slope == 0.0 and segments and segments[-1]["type"] == "stop":
                continue
            if abs(0.0 - prev_slope) < 50.0 and segments:
                continue
            segments.append(_stop(frames))
            prev_slope = 0.0
        else:
            end = end_choice
            if abs(end) < 100.0 and end != 0.0:
                continue
            if speed == 0.0 and end == 0.0:
                continue
            slope = (end - speed) / (frames / 16)  # cm/s^2
            if abs(slope - prev_slope) < 50.0:
                continue
            segments.append(_move(frames, end))
            speed, prev_slope = end, slope
        total += frames
    if not segments:
        segments.append(_stop(16))
    return _scenario(0.0, segments)


@settings(
    max_examples=40,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(scenario=_scenarios())
def test_roundtrip_property(scenario: Scenario) -> None:
    """Hypothesis scenarios round-trip within tolerance."""
    config = load_config()
    drive = generate(scenario, config)
    inferred = infer_scenario(drive.frames, config)
    report = compare_scenarios(scenario, inferred, config.tolerances, config.fps)
    assert report.ok, f"{report.problems}; inferred={inferred.model_dump()}"


def test_short_segments_lists_segments_under_32_frames() -> None:
    """Segments under 32 frames (2 s) are listed as (index, frames); 32 frames is not short."""
    scenario = _scenario(0.0, [_stop(32), _move(16, 100.0), _move(31, 0.0), _stop(32)])
    assert short_segments(scenario, 16) == [(1, 16), (2, 31)]


def test_short_segments_demo_has_none() -> None:
    """The demo's shortest segment is exactly 32 frames, so nothing is listed."""
    assert short_segments(load_scenario(DEMO), 16) == []
