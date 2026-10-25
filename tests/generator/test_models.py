"""Tests for the scenario model. Units: cm, cm/s, s."""

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import vl_data.generator
from vl_data.core.hashing import scenario_hash
from vl_data.generator.models import (
    FRAMES_PER_SECOND,
    MAX_FRAMES,
    MoveSegment,
    Scenario,
    StopSegment,
    load_scenario,
)
from vl_data.generator.motion import frame_count

DEMO = Path(vl_data.generator.__file__).parent / "scenarios" / "demo.yaml"


def make(*segments: dict[str, Any], **extra: Any) -> Scenario:
    return Scenario.model_validate({"segments": list(segments), **extra})


def test_duration_must_be_whole_frames() -> None:
    with pytest.raises(ValidationError):
        make({"type": "stop", "duration_s": 1.03})
    with pytest.raises(ValidationError):
        make({"type": "move", "duration_s": 1.03, "end_speed_cm_s": 10.0})
    ok = make({"type": "stop", "duration_s": 1.0625})
    assert ok.segments[0].duration_s == 1.0625
    assert frame_count(ok, 16) == 17


@pytest.mark.parametrize("duration", [0.0, -1.0, float("nan"), float("inf")])
def test_duration_must_be_positive_and_finite(duration: float) -> None:
    with pytest.raises(ValidationError):
        make({"type": "stop", "duration_s": duration})


def test_move_with_zero_speeds_rejected() -> None:
    with pytest.raises(ValidationError, match="segment 0"):
        make({"type": "move", "duration_s": 1.0, "end_speed_cm_s": 0.0})
    # Moving from a nonzero start speed down to 0 is fine.
    make(
        {"type": "move", "duration_s": 1.0, "end_speed_cm_s": 0.0},
        start={"speed_cm_s": 50.0},
    )
    # A later zero-to-zero move (after a stop) is rejected too.
    with pytest.raises(ValidationError, match="segment 1"):
        make(
            {"type": "stop", "duration_s": 1.0},
            {"type": "move", "duration_s": 1.0, "end_speed_cm_s": 0.0},
        )


def test_stop_after_nonzero_speed_rejected() -> None:
    with pytest.raises(ValidationError, match="segment 1"):
        make(
            {"type": "move", "duration_s": 1.0, "end_speed_cm_s": 100.0},
            {"type": "stop", "duration_s": 1.0},
        )
    with pytest.raises(ValidationError, match="segment 0"):
        make({"type": "stop", "duration_s": 1.0}, start={"speed_cm_s": 5.0})
    # slowing to 0 first makes the stop legal
    make(
        {"type": "move", "duration_s": 1.0, "end_speed_cm_s": 100.0},
        {"type": "move", "duration_s": 1.0, "end_speed_cm_s": 0.0},
        {"type": "stop", "duration_s": 1.0},
    )


def test_extra_keys_rejected() -> None:
    with pytest.raises(ValidationError):
        make({"type": "stop", "duration_s": 1.0}, bogus=1)
    with pytest.raises(ValidationError):
        make({"type": "stop", "duration_s": 1.0, "end_speed_cm_s": 0.0})
    with pytest.raises(ValidationError):
        make({"type": "stop", "duration_s": 1.0}, start={"w_cm": 1.0})


def test_type_key_required_and_known() -> None:
    with pytest.raises(ValidationError):
        make({"duration_s": 1.0})
    with pytest.raises(ValidationError):
        make({"type": "turn", "duration_s": 1.0})


def test_empty_segments_rejected() -> None:
    with pytest.raises(ValidationError):
        Scenario.model_validate({"segments": []})
    with pytest.raises(ValidationError):
        Scenario.model_validate({})


def test_seed_and_bool_strictness() -> None:
    assert make({"type": "stop", "duration_s": 1.0}).seed == 0
    for bad in (1.0, True):
        with pytest.raises(ValidationError):
            make({"type": "stop", "duration_s": 1.0}, seed=bad)
    with pytest.raises(ValidationError):
        make({"type": "stop", "duration_s": True})
    with pytest.raises(ValidationError):
        make({"type": "move", "duration_s": 1.0, "end_speed_cm_s": True})
    with pytest.raises(ValidationError):
        make({"type": "stop", "duration_s": 1.0}, start={"x_cm": True})


def test_int_accepted_for_float_and_stored_as_float() -> None:
    a = make({"type": "move", "duration_s": 1, "end_speed_cm_s": 600})
    seg = a.segments[0]
    assert isinstance(seg, MoveSegment)
    assert seg.end_speed_cm_s == 600.0
    assert isinstance(seg.end_speed_cm_s, float)
    assert isinstance(seg.duration_s, float)
    b = make({"type": "move", "duration_s": 1.0, "end_speed_cm_s": 600.0})
    assert scenario_hash(a) == scenario_hash(b)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_rejected(bad: float) -> None:
    with pytest.raises(ValidationError):
        make({"type": "move", "duration_s": 1.0, "end_speed_cm_s": bad})
    with pytest.raises(ValidationError):
        make({"type": "stop", "duration_s": 1.0}, start={"x_cm": bad})
    with pytest.raises(ValidationError):
        make({"type": "stop", "duration_s": 1.0}, start={"speed_cm_s": bad})


def test_dump_includes_type_defaults() -> None:
    dumped = make({"type": "stop", "duration_s": 1.0}).model_dump(mode="json")
    assert dumped["segments"][0]["type"] == "stop"
    assert dumped["start"] == {"x_cm": 0.0, "y_cm": 0.0, "z_cm": 0.0, "speed_cm_s": 0.0}


def test_demo_yaml_loads() -> None:
    sc = load_scenario(DEMO)
    assert frame_count(sc, 16) == 1000
    assert sc.seed == 0
    assert sc.start.model_dump() == {"x_cm": 0.0, "y_cm": 0.0, "z_cm": 0.0, "speed_cm_s": 0.0}
    expected = [
        ("stop", 2.0, None),
        ("move", 6.0, 600.0),
        ("move", 10.0, 600.0),
        ("move", 4.0, 200.0),
        ("move", 8.0, -300.0),
        ("move", 6.0, -300.0),
        ("move", 5.0, 0.0),
        ("stop", 21.5, None),
    ]
    assert len(sc.segments) == 8
    for seg, (kind, dur, end) in zip(sc.segments, expected, strict=True):
        assert seg.type == kind
        assert seg.duration_s == dur
        if isinstance(seg, MoveSegment):
            assert seg.end_speed_cm_s == end
        else:
            assert isinstance(seg, StopSegment)


def test_load_scenario_errors(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_scenario(tmp_path / "missing.yaml")
    empty = tmp_path / "empty.yaml"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="mapping"):
        load_scenario(empty)
    listy = tmp_path / "list.yaml"
    listy.write_text("- 1\n- 2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="mapping"):
        load_scenario(listy)
    bad = tmp_path / "bad.yaml"
    bad.write_text("segments: []\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_scenario(bad)
    yes = tmp_path / "yes.yaml"
    yes.write_text("seed: yes\nsegments:\n  - {type: stop, duration_s: 1.0}\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_scenario(yes)


def test_huge_duration_gives_validation_error() -> None:
    # 1e308 * 16 overflows to inf; this must be a ValidationError, not OverflowError.
    with pytest.raises(ValidationError):
        make({"type": "stop", "duration_s": 1e308})
    with pytest.raises(ValidationError, match="MAX_FRAMES"):
        make({"type": "stop", "duration_s": 1e18})


def test_frame_cap_is_2000_frames_125_seconds() -> None:
    assert MAX_FRAMES == 2000
    assert MAX_FRAMES / FRAMES_PER_SECOND == 125.0  # s


def test_single_segment_frame_cap_boundary() -> None:
    ok = make({"type": "stop", "duration_s": MAX_FRAMES / FRAMES_PER_SECOND})  # 125.0 s
    assert frame_count(ok, FRAMES_PER_SECOND) == MAX_FRAMES
    with pytest.raises(ValidationError, match="MAX_FRAMES"):
        make({"type": "stop", "duration_s": (MAX_FRAMES + 1) / FRAMES_PER_SECOND})


def test_total_frames_cap_across_segments() -> None:
    half = (MAX_FRAMES // 2) / FRAMES_PER_SECOND  # 62.5 s, 1,000 frames
    ok = make(
        {"type": "move", "duration_s": half, "end_speed_cm_s": 100.0},
        {"type": "move", "duration_s": half, "end_speed_cm_s": 0.0},
    )
    assert frame_count(ok, FRAMES_PER_SECOND) == MAX_FRAMES
    with pytest.raises(ValidationError, match="limit of 2000"):
        make(
            {"type": "move", "duration_s": half, "end_speed_cm_s": 100.0},
            {"type": "move", "duration_s": half + 1 / FRAMES_PER_SECOND, "end_speed_cm_s": 0.0},
        )


def test_negative_zero_is_zero() -> None:
    with pytest.raises(ValidationError):
        make({"type": "stop", "duration_s": -0.0})
    sc = make(
        {"type": "move", "duration_s": 1.0, "end_speed_cm_s": -0.0},
        {"type": "stop", "duration_s": 1.0},
        start={"speed_cm_s": 5.0},
    )
    assert len(sc.segments) == 2
