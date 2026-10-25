"""Tests for the motion model. Units: cm, cm/s, cm/s^2, s; fps in frames/s."""

from pathlib import Path
from typing import Any

import numpy as np
import pytest

import vl_data.generator
from vl_data.generator.models import Scenario, Start, StopSegment, load_scenario
from vl_data.generator.motion import (
    frame_count,
    ground_truth_arrays,
    integrate_position,
    speed_profile,
)

DEMO = Path(vl_data.generator.__file__).parent / "scenarios" / "demo.yaml"
FPS = 16


def make(*segments: dict[str, Any], **extra: Any) -> Scenario:
    return Scenario.model_validate({"segments": list(segments), **extra})


def test_speed_profile_ramp() -> None:
    v = speed_profile(load_scenario(DEMO), FPS)
    assert v.shape == (1000,) and v.dtype == np.float64
    # Segment frame counts: 32, 96, 160, 64, 128, 96, 80, 344 -> starts 0, 32, 128,
    # 288, 352, 480, 576, 656.
    # Stop: frames 0..31 are 0.
    assert v[0] == 0.0 and v[31] == 0.0
    # Move 1: 96 frames from 0 to 600, step 600/96 = 6.25 cm/s per frame.
    assert v[32] == 0.0
    assert v[33] == pytest.approx(6.25)
    assert v[64] == pytest.approx(32 * 6.25)  # 200.0
    assert v[127] == pytest.approx(95 * 6.25)  # 593.75, end speed not yet reached
    # Move 2 starts at 600 and holds it (600 -> 600) for 160 frames.
    assert v[128] == 600.0
    assert v[287] == 600.0
    # Move 3: 64 frames 600 -> 200, step -400/64 = -6.25.
    assert v[288] == 600.0
    assert v[289] == pytest.approx(593.75)
    assert v[351] == pytest.approx(600.0 - 63 * 6.25)  # 206.25
    # Move 4: 128 frames 200 -> -300, step -500/128 = -3.90625.
    assert v[352] == 200.0
    assert v[353] == pytest.approx(200.0 - 3.90625)
    # Reversal: 200 - k*3.90625 = 0 at k = 51.2, so it crosses 0 between frames 403 and 404.
    assert v[403] == pytest.approx(200.0 - 51 * 3.90625)  # 0.78125 > 0
    assert v[404] == pytest.approx(200.0 - 52 * 3.90625)  # -3.125 < 0
    assert v[403] > 0.0 > v[404]
    assert v[479] == pytest.approx(200.0 - 127 * 3.90625)  # -296.09375
    # Move 5: 96 frames holding -300.
    assert v[480] == -300.0 and v[575] == -300.0
    # Move 6: 80 frames -300 -> 0, step 3.75.
    assert v[576] == -300.0
    assert v[577] == pytest.approx(-296.25)
    assert v[655] == pytest.approx(-300.0 + 79 * 3.75)  # -3.75
    # Final stop: frames 656..999 are 0.
    assert v[656] == 0.0 and v[999] == 0.0
    assert (v[656:] == 0.0).all()


def test_speed_profile_start_speed_and_last_segment() -> None:
    sc = make(
        {"type": "move", "duration_s": 0.25, "end_speed_cm_s": 80.0},
        start={"speed_cm_s": 40.0},
    )
    # 4 frames from 40 to 80: 40, 50, 60, 70 (80 is never reached)
    assert speed_profile(sc, FPS).tolist() == [40.0, 50.0, 60.0, 70.0]


def test_frame_count_sums_segments() -> None:
    sc = make(
        {"type": "stop", "duration_s": 1.0},
        {"type": "move", "duration_s": 0.0625, "end_speed_cm_s": 1.0},
    )
    assert frame_count(sc, FPS) == 17
    assert frame_count(load_scenario(DEMO), FPS) == 1000


def test_frame_count_rejects_fps_without_whole_frames() -> None:
    sc = make({"type": "stop", "duration_s": 1.0625})  # 1.0625 * 7 = 7.4375
    with pytest.raises(ValueError):
        frame_count(sc, 7)
    with pytest.raises(ValueError):
        speed_profile(sc, 7)
    for bad_fps in (0, -16):
        with pytest.raises(ValueError):
            frame_count(sc, bad_fps)
    assert frame_count(sc, 32) == 34  # a different fps with whole frames works


def test_integrate_position_basics() -> None:
    v = np.array([16.0, 32.0, 48.0, 0.0])
    x = integrate_position(10.0, v, FPS)
    assert x.shape == (4,)
    assert x[0] == 10.0
    # x[k+1] = x[k] + v[k]/16: 10, 11, 13, 16 (the last speed is never integrated)
    assert x.tolist() == [10.0, 11.0, 13.0, 16.0]
    assert integrate_position(-5.0, np.array([123.0]), FPS).tolist() == [-5.0]


def test_demo_integrated_position_and_total_distance() -> None:
    sc = load_scenario(DEMO)
    v = speed_profile(sc, FPS)
    x = integrate_position(0.0, v, FPS)
    assert x.shape == (1000,) and x[0] == 0.0
    # Euler distance of a ramp a -> b over n frames is (n*a + (b-a)*(n-1)/2) / 16:
    #   move 1: (0 + 600*95/2)/16           =  28500/16 =  1781.25
    #   move 2: 160*600/16                  =               6000.0
    #   move 3: (64*600 + (-400)*63/2)/16   =  25800/16 =  1612.5
    #   move 4: (128*200 + (-500)*127/2)/16 =  -6150/16 =  -384.375
    #   move 5: 96*(-300)/16                =              -1800.0
    #   move 6: (80*(-300) + 300*79/2)/16   = -12150/16 =  -759.375
    # total = 6450.0 cm; both stops add 0.
    assert x[-1] == pytest.approx(6450.0, abs=1e-9)
    # the end of move 2 (frame 288): 1781.25 + 6000 = 7781.25
    assert x[288] == pytest.approx(7781.25, abs=1e-9)
    # a nonzero start offsets it exactly
    x2 = integrate_position(100.0, v, FPS)
    assert x2[-1] == pytest.approx(6550.0, abs=1e-9)


def test_ground_truth_matches_forward_difference() -> None:
    sc = load_scenario(DEMO)
    v = speed_profile(sc, FPS)
    x = integrate_position(0.0, v, FPS)
    vel, acc = ground_truth_arrays(x, v, FPS)
    assert vel.shape == acc.shape == (1000,)
    assert vel[-1] == vel[-2]
    assert acc[-1] == acc[-2]
    # velocity of Euler positions equals the speed profile itself
    np.testing.assert_allclose(vel[:-1], v[:-1], atol=1e-9)
    # hand values: ramp slope in move 1 is 6.25 cm/s per frame * 16 = 100 cm/s^2
    assert acc[40] == pytest.approx(100.0)
    # move 3 slope: -6.25 * 16 = -100
    assert acc[300] == pytest.approx(-100.0)
    # flat part and stop
    assert acc[200] == pytest.approx(0.0) and acc[900] == 0.0


def test_ground_truth_edge_cases() -> None:
    vel, acc = ground_truth_arrays(np.array([5.0]), np.array([7.0]), FPS)
    assert vel.tolist() == [7.0] and acc.tolist() == [0.0]
    vel, acc = ground_truth_arrays(np.array([0.0, 1.0]), np.array([16.0, 32.0]), FPS)
    assert vel.tolist() == [16.0, 16.0]
    assert acc.tolist() == [256.0, 256.0]  # (32-16)*16, repeated


def test_frame_count_rejects_unvalidated_non_finite_duration() -> None:
    # model_construct skips validation; the motion code must still fail cleanly.
    seg = StopSegment.model_construct(type="stop", duration_s=float("inf"))
    sc = Scenario.model_construct(seed=0, start=Start(), segments=[seg])
    with pytest.raises(ValueError, match="finite"):
        frame_count(sc, FPS)


def test_high_fps_cannot_defeat_frame_cap() -> None:
    sc = make({"type": "stop", "duration_s": 2.0})  # 2,500 frames at 1250 fps
    with pytest.raises(ValueError, match="MAX_FRAMES"):
        frame_count(sc, 1250)
    with pytest.raises(ValueError, match="MAX_FRAMES"):
        speed_profile(sc, 1250)
    assert frame_count(sc, 1000) == 2000  # exactly at the cap is fine
