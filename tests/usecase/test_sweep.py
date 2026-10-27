"""Tests for tools/motion_sweep.py and the measured defaults in config/motion.yaml.

Units: cm, cm/s, cm/s^2, frames, s.
"""

import os
import shutil
from collections import namedtuple
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pytest

from vl_data.core.config import BodyConfig, Config
from vl_data.generator.body import cube_lattice, place_body
from vl_data.generator.models import Scenario
from vl_data.usecase.config import MotionConfig, load_motion_config

from .helpers import load_sweep_tool

sweep: Any = load_sweep_tool()

YAML_PATH = Path(__file__).resolve().parents[2] / "config" / "motion.yaml"
SMALL = Config(body=BodyConfig(size_cm=100, points_per_axis=10))
SHORT = Scenario.model_validate(
    {
        "seed": 0,
        "start": {"x_cm": 0.0, "y_cm": 0.0, "z_cm": 0.0, "speed_cm_s": 0.0},
        "segments": [
            {"type": "stop", "duration_s": 2.0},
            {"type": "move", "duration_s": 3.0, "end_speed_cm_s": 300.0},
            {"type": "move", "duration_s": 3.0, "end_speed_cm_s": -200.0},
            {"type": "move", "duration_s": 2.0, "end_speed_cm_s": 0.0},
            {"type": "stop", "duration_s": 2.0},
        ],
    }
)  # 192 frames at 16 fps


@pytest.fixture(scope="module")
def short_drive() -> Any:
    return sweep.drive_from_scenario("short", SHORT, SMALL)


@pytest.fixture(scope="module")
def sweep_drives() -> list[Any]:
    """The demo and hard drives, generated once for the whole module."""
    return sweep.build_sweep_drives(SMALL)


@pytest.fixture
def yaml_copy(tmp_path: Path) -> Path:
    target = tmp_path / "motion.yaml"
    shutil.copyfile(YAML_PATH, target)
    return target


def _stats(value: float) -> Any:
    return sweep.ErrorStats(max_abs=value, rms=value / 2)


def _errors(vel_max: float, acc_max: float, interior: int = 10) -> Any:
    return sweep.DriveErrors(
        frames_interior=interior,
        frames_total=10,
        velocity_interior=_stats(vel_max),
        acceleration_interior=_stats(acc_max),
        velocity_all=_stats(vel_max * 2),
        acceleration_all=_stats(acc_max * 2),
    )


def _row(window: int, order: int, per_drive: list[tuple[float, float]]) -> Any:
    errors = {f"d{i}": _errors(v, a) for i, (v, a) in enumerate(per_drive)}
    return sweep.SweepRow(f"w{window}o{order}", window, order, errors)


def test_sweep_reports_every_cell(short_drive: Any) -> None:
    rows = sweep.run_sweep([short_drive], windows=(8, 12), orders=(2, 3))
    cells = [(r.window_frames, r.poly_order) for r in rows if r.window_frames is not None]
    assert cells == [(8, 2), (8, 3), (12, 2), (12, 3)]
    assert len(rows) == 5
    assert rows[-1].label == "per-frame differences" and rows[-1].window_frames is None
    for row in rows:
        stats = row.errors["short"]
        assert stats.frames_interior > 0
        assert stats.acceleration_all.max_abs >= stats.acceleration_interior.max_abs


def test_chosen_default_beats_baseline_on_every_drive(sweep_drives: list[Any]) -> None:
    cfg = load_motion_config()
    assert len(sweep_drives) == 2
    for drive in sweep_drives:
        chosen = sweep.measure_drive(drive, cfg)
        base = sweep.measure_drive(drive, None)
        for name in ("acceleration_interior", "acceleration_all", "velocity_interior"):
            assert getattr(chosen, name).max_abs < getattr(base, name).max_abs, (drive.name, name)
            assert getattr(chosen, name).rms < getattr(base, name).rms, (drive.name, name)
        # All-frame velocity is NOT better than differences at the clip ends and at speed
        # changes (measured: window 16 smooths them), so it is deliberately not asserted.


def test_yaml_window_and_order_are_the_selection_rule_result(sweep_drives: list[Any]) -> None:
    cfg = load_motion_config()
    rows = sweep.run_sweep(sweep_drives, sweep.DEFAULT_WINDOWS, sweep.DEFAULT_ORDERS)
    chosen = sweep.choose_default(rows)
    assert (chosen.window_frames, chosen.poly_order) == (cfg.window_frames, cfg.poly_order)
    assert cfg.min_points == sweep.default_min_points(cfg.window_frames)


def test_sweep_drive_lengths_and_interior_shares(sweep_drives: list[Any]) -> None:
    demo, hard = sweep_drives
    assert (demo.name, len(demo.t_s)) == ("demo", 1000)
    assert (hard.name, len(hard.t_s)) == ("hard", 624)
    for drive in sweep_drives:
        assert drive.centroid_x_cm.shape == drive.t_s.shape == drive.true_velocity_cm_s.shape
        assert np.all(np.diff(drive.t_s) > 0)
    assert hard.true_velocity_cm_s.min() < 0 < hard.true_velocity_cm_s.max()  # a reversal
    # Interior frames of the chosen cell (window 16, order 2), as documented in ARCHITECTURE.md.
    cfg = load_motion_config()
    assert (cfg.window_frames, cfg.poly_order) == (16, 2)
    shares = [sweep.measure_drive(d, cfg) for d in sweep_drives]
    assert [(s.frames_interior, s.frames_total) for s in shares] == [(880, 1000), (521, 624)]


def test_centroid_is_truth_plus_offset(short_drive: Any) -> None:
    assert short_drive.centroid_x_cm[0] == pytest.approx(49.5)
    assert short_drive.centroid_x_cm[-1] == pytest.approx(49.5 + short_drive.true_x_cm[-1], abs=0.5)


@pytest.mark.parametrize("points_per_axis", [2, 10, 100])
@pytest.mark.parametrize("x_cm", [0.0, 12.3, -7.7, 1000.49, 33.01])
def test_centroid_identity_for_any_lattice_density(points_per_axis: int, x_cm: float) -> None:
    """Unit check on one frame: centroid x = (size_cm - 1) / 2 + rint(x), for any density.

    This is why the sweep may use 10 points per axis. It holds for positions that are not an
    exact half cm (rint rounds a tie to the even integer, which can depend on the point).
    """
    lattice = cube_lattice(100, points_per_axis)
    centroid = float(place_body(lattice, x_cm, 0.0, 0.0)[:, 0].mean(dtype=np.float64))
    assert centroid == pytest.approx(49.5 + np.rint(x_cm), abs=1e-9)


def test_max_abs_rms_of_known_errors() -> None:
    got = sweep.max_abs_rms(np.array([1.0, 2.0, 5.0]), np.array([1.0, 4.0, 5.0]))
    assert got.max_abs == 2.0 and got.rms == pytest.approx(np.sqrt(4.0 / 3.0))
    empty = sweep.max_abs_rms(np.array([]), np.array([]))
    assert np.isnan(empty.max_abs) and np.isnan(empty.rms)


def test_interior_mask_excludes_half_a_window_around_each_change() -> None:
    acceleration = np.zeros(20)
    acceleration[10:] = 5.0  # a change at frame 10
    mask = sweep.interior_mask(acceleration, 3.0)
    assert not mask[7:14].any()  # frames 10 +/- 3 are not interior
    assert mask[:7].all() and mask[14:19].all()
    assert not mask[19]  # the last frame is never interior
    assert sweep.interior_mask(np.zeros(5), 2.0).tolist() == [True, True, True, True, False]


def test_true_motion_of_a_ramp_and_a_stop() -> None:
    scenario = Scenario.model_validate(
        {
            "seed": 0,
            "start": {"x_cm": 10.0, "y_cm": 0.0, "z_cm": 0.0, "speed_cm_s": 0.0},
            "segments": [
                {"type": "move", "duration_s": 1.0, "end_speed_cm_s": 160.0},
                {"type": "move", "duration_s": 1.0, "end_speed_cm_s": 0.0},
                {"type": "stop", "duration_s": 1.0},
            ],
        }
    )
    x, velocity, acceleration = sweep.true_motion(scenario, 16)  # 48 frames; speed 10 cm/s * frame
    assert x.size == velocity.size == acceleration.size == 48
    assert x[0] == 10.0 and x[16] == pytest.approx(85.0)  # 10 + 10 * (0 + ... + 15) / 16
    assert velocity[:16] == pytest.approx(10.0 * np.arange(16))  # velocity equals the speed
    assert acceleration[:16] == pytest.approx(160.0)  # 10 cm/s per frame at 16 fps
    assert acceleration[16:32] == pytest.approx(-160.0)
    assert np.all(velocity[32:] == 0.0) and np.all(acceleration[32:] == 0.0)


def test_baseline_exact_on_a_parabola() -> None:
    fps = 16
    t = np.arange(20) / fps
    x = 3.0 + 40.0 * t + 100.0 * t**2
    velocity, acceleration = sweep.difference_estimate(x, fps)
    assert velocity[5] == pytest.approx(40.0 + 200.0 * t[5])
    assert acceleration[5] == pytest.approx(200.0)
    assert np.isnan(acceleration[0]) and np.isnan(acceleration[-1])
    assert velocity[0] == pytest.approx((x[1] - x[0]) * fps)  # forward at the first frame
    assert velocity[-1] == pytest.approx((x[-1] - x[-2]) * fps)  # backward at the last


def test_selection_rule_orders_by_acc_then_vel_then_window() -> None:
    a = _row(8, 2, [(5.0, 30.0), (4.0, 20.0)])  # worst acc 30
    b = _row(12, 2, [(9.0, 10.0), (3.0, 25.0)])  # worst acc 25, worst vel 9
    c = _row(16, 3, [(2.0, 25.0), (1.0, 10.0)])  # worst acc 25, worst vel 2: wins on velocity
    d = _row(24, 2, [(2.0, 25.0), (1.0, 10.0)])  # ties c exactly: the smaller window wins
    baseline = sweep.SweepRow("per-frame differences", None, None, c.errors)
    assert sweep.choose_default([a, b, c, d, baseline]) is c
    assert sweep.choose_default([a, b, d, c, baseline]) is c
    assert sweep.choose_default([a, b, baseline]) is b
    assert b.worst_interior_acceleration_max() == 25.0
    assert b.worst_interior_velocity_max() == 9.0


def test_selection_rule_treats_round_off_as_a_tie() -> None:
    order2 = _row(48, 2, [(3.1, 0.2857474904897117)])
    order3 = _row(48, 3, [(3.2, 0.28574749048425474)])  # 5e-12 lower: float noise, not a win
    assert sweep.choose_default([order3, order2], 0.0) is order2


def test_selection_rule_requires_interior_share() -> None:
    thin = sweep.SweepRow(
        "w48", 48, 3, {"d0": _errors(1.0, 0.1, interior=5), "d1": _errors(1.0, 0.1)}
    )
    fine = _row(16, 2, [(9.0, 4.0), (9.0, 4.0)])
    assert thin.min_interior_fraction() == 0.5
    assert sweep.choose_default([thin, fine]) is fine  # 80% of every drive by default
    assert sweep.choose_default([thin, fine], 0.0) is thin  # the plain rule
    assert sweep.choose_default([thin, fine], 0.5) is thin
    with pytest.raises(ValueError):
        sweep.choose_default([thin], 0.9)


def test_selection_rule_needs_a_cell() -> None:
    baseline = sweep.SweepRow("per-frame differences", None, None, {"d": _errors(1.0, 1.0)})
    with pytest.raises(ValueError):
        sweep.choose_default([baseline])


def test_derived_settings_are_valid_for_the_whole_grid() -> None:
    for window in sweep.DEFAULT_WINDOWS:
        for order in sweep.DEFAULT_ORDERS:
            cfg = sweep.motion_config_for(window, order)
            assert isinstance(cfg, MotionConfig)
            assert order + 1 <= cfg.min_points <= cfg.window_frames
            assert cfg.max_gap_frames == MotionConfig().max_gap_frames


def test_random_search_is_seeded_and_always_includes_pinned_scenarios() -> None:
    cfg = load_motion_config()
    none = sweep.random_search(cfg, 0, seed=1)
    assert none.examples == len(sweep.PINNED_LEGS)
    assert none.worst_acceleration_cm_s2 > 19.0  # the pinned -68 cm/s case alone
    assert none.worst_velocity_cm_s > 0
    first = sweep.random_search(cfg, 4, seed=7)
    again = sweep.random_search(cfg, 4, seed=7)
    assert first == again and first.examples == len(sweep.PINNED_LEGS) + 4
    assert first.worst_acceleration_cm_s2 >= none.worst_acceleration_cm_s2


def test_random_search_scenarios_follow_the_leg_rules() -> None:
    rng = np.random.default_rng(3)
    for _ in range(50):
        legs = sweep._draw_legs(rng)
        assert 1 <= len(legs) <= sweep.MAX_LEGS
        for _is_stop, frames, speed in legs:
            assert sweep.LEG_MIN_FRAMES <= frames <= sweep.LEG_MAX_FRAMES
            assert abs(speed) <= sweep.LEG_MAX_SPEED_CM_S
        sweep.scenario_from_legs(legs)  # always a valid scenario


def test_write_yaml_updates_only_measured_fields(tmp_path: Path) -> None:
    before = YAML_PATH.read_text(encoding="utf-8")
    target = tmp_path / "motion.yaml"
    target.write_text("# PROVISIONAL: placeholder values\n" + before, encoding="utf-8")
    row = _row(24, 3, [(5.0, 30.0)])
    sweep.write_yaml(target, row, stamp="2026-01-01", basis="a random-scenario search (seed 9)")
    cfg = load_motion_config(target)
    assert (cfg.window_frames, cfg.poly_order, cfg.min_points) == (24, 3, 13)
    assert cfg.position_keypoint == "centroid" and cfg.max_gap_frames == 4
    text = target.read_text(encoding="utf-8")
    assert "PROVISIONAL" not in text and "2026-01-01" in text
    assert "random-scenario search (seed 9)" in text
    assert all(len(line) <= 100 for line in text.splitlines() if line.startswith("#"))
    assert "# frames in the sliding fit window" in text  # field comments stay
    assert "position_keypoint: centroid" in text
    # A second write replaces the note instead of stacking another one.
    sweep.write_yaml(target, row, stamp="2026-02-02", basis="x")
    again = target.read_text(encoding="utf-8")
    assert again.count("Measured by") == 1 and "2026-01-01" not in again


def test_write_yaml_keeps_crlf_line_endings(tmp_path: Path) -> None:
    target = tmp_path / "motion.yaml"
    target.write_bytes(
        YAML_PATH.read_text(encoding="utf-8").replace("\r\n", "\n").encode().replace(b"\n", b"\r\n")
    )
    sweep.write_yaml(target, _row(16, 2, [(1.0, 1.0)]), stamp="s")
    raw = target.read_bytes()
    assert raw.count(b"\r\n") == raw.count(b"\n") > 5


def test_write_yaml_changes_only_top_level_fields(tmp_path: Path) -> None:
    target = tmp_path / "motion.yaml"
    target.write_text(
        "window_frames: 16\npoly_order: 2\nmin_points: 9\nother:\n  window_frames: 1\n",
        encoding="utf-8",
    )
    sweep.write_yaml(target, _row(24, 2, [(1.0, 1.0)]), stamp="s")
    text = target.read_text(encoding="utf-8")
    assert "window_frames: 24" in text
    assert "other:\n  window_frames: 1\n" in text


def test_write_yaml_is_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "motion.yaml"
    shutil.copyfile(YAML_PATH, target)
    before = target.read_bytes()

    def fail(*_: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        sweep.write_yaml(target, _row(24, 2, [(1.0, 1.0)]), stamp="s")
    assert target.read_bytes() == before  # the original is untouched
    assert [p.name for p in tmp_path.iterdir()] == ["motion.yaml"]  # no temp file left


def test_write_yaml_rejects_the_baseline_row_and_missing_fields(tmp_path: Path) -> None:
    target = tmp_path / "motion.yaml"
    target.write_text("window_frames: 16\n", encoding="utf-8")
    baseline = sweep.SweepRow("per-frame differences", None, None, {"d": _errors(1.0, 1.0)})
    with pytest.raises(ValueError):
        sweep.write_yaml(target, baseline, stamp="x")
    with pytest.raises(ValueError, match="not found"):
        sweep.write_yaml(target, _row(16, 2, [(1.0, 1.0)]), stamp="x")
    assert target.read_text(encoding="utf-8") == "window_frames: 16\n"


def test_main_prints_table_and_writes_yaml(
    yaml_copy: Path, sweep_drives: list[Any], capsys: pytest.CaptureFixture[str]
) -> None:
    code = sweep.main(
        ["--windows", "8,16", "--orders", "2,3", "--random-search", "3", "--write-yaml"]
        + ["--yaml", str(yaml_copy)],
        drives=sweep_drives,
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "per-frame differences" in out and "cm/s^2" in out
    assert "Chosen: window 16 frames, order 2, min_points 9" in out
    assert "scenarios (seed 0)" in out
    cfg = load_motion_config(yaml_copy)
    assert (cfg.window_frames, cfg.poly_order) == (16, 2)
    assert "tolerance" not in out.lower()  # the sweep reports measurements, not limits
    written = yaml_copy.read_text(encoding="utf-8")
    assert "tolerances" not in written and "developer measurement" in written


def test_main_without_write_leaves_the_yaml_alone(
    yaml_copy: Path, sweep_drives: list[Any], capsys: pytest.CaptureFixture[str]
) -> None:
    before = yaml_copy.read_bytes()
    assert (
        sweep.main(
            ["--windows", "16", "--orders", "2", "--random-search", "0", "--yaml", str(yaml_copy)],
            drives=sweep_drives,
        )
        == 0
    )
    assert "Chosen: window 16 frames, order 2" in capsys.readouterr().out
    assert yaml_copy.read_bytes() == before


@pytest.mark.parametrize(
    "args",
    [
        ["--full-density-check", "601"],
        ["--full-density-check", "-1"],
        ["--windows", "2"],
        ["--windows", "x"],
        ["--orders", "4"],
        ["--min-interior-fraction", "2"],
        ["--random-search", "-3"],
    ],
)
def test_main_rejects_bad_arguments(args: list[str]) -> None:
    with pytest.raises(SystemExit) as info:
        sweep.main(args, drives=[])
    assert info.value.code == 2


def test_compare_stored_motion_counts_nulls_and_measures_errors() -> None:
    scenario = Scenario.model_validate(
        {
            "seed": 0,
            "start": {"x_cm": 0.0, "y_cm": 0.0, "z_cm": 0.0, "speed_cm_s": 0.0},
            "segments": [{"type": "stop", "duration_s": 2.0}],
        }
    )  # 32 frames at 16 fps, always at x = 0: the true motion is all zeros
    offset = 49.5  # (100 - 1) / 2 cm, the centroid of the 100 cm body above its origin
    x = np.full(32, offset)
    x[3] += 0.4
    x[31] += 100.0  # the last frame is never interior, so this must not count
    velocity = np.zeros(32)
    velocity[5], velocity[10], velocity[31] = 2.0, -3.0, 900.0
    acceleration = np.zeros(32)
    acceleration[12], acceleration[31] = 4.0, 900.0
    motion = pa.table(
        {
            "x_cm": pa.array(x),
            "velocity_cm_s": pa.array(velocity, mask=np.isin(np.arange(32), [7, 8])),
            "acceleration_cm_s2": pa.array(acceleration, mask=np.isin(np.arange(32), [8, 20])),
        }
    )
    got = sweep.compare_stored_motion(motion, scenario, SMALL, MotionConfig())
    assert got.frames_interior == 31
    assert got.frames_null == 3  # frames 7, 8 and 20: a null velocity or acceleration
    assert got.position_cm.max_abs == pytest.approx(0.4)
    assert got.position_cm.rms == pytest.approx(np.sqrt(0.16 / 31))
    assert got.velocity_cm_s.max_abs == pytest.approx(3.0)
    assert got.velocity_cm_s.rms == pytest.approx(np.sqrt(13.0 / 29))  # 2 nulls left out
    assert got.acceleration_cm_s2.max_abs == pytest.approx(4.0)
    assert got.acceleration_cm_s2.rms == pytest.approx(np.sqrt(16.0 / 29))


def test_full_density_check_refuses_when_disk_is_too_small(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    free = namedtuple("usage", "total used free")(10, 10, 1000)
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: free)
    with pytest.raises(RuntimeError, match="free disk space"):
        sweep.full_density_check(frames=190, base_dir=tmp_path, config=SMALL)
    assert list(tmp_path.iterdir()) == []  # nothing was written


def test_full_density_check_cleans_up_and_reports(tmp_path: Path) -> None:
    """Pipeline plumbing on a SMALL lattice (config=SMALL): the small lattice is compared with
    itself, so the zero centroid difference here does not prove that 100 points per axis give
    the same centroid. That is measured once on 300 frames (docs) and checked as a unit
    identity in test_centroid_identity_for_any_lattice_density.
    """
    result = sweep.full_density_check(frames=190, base_dir=tmp_path, config=SMALL)
    assert result.report.frames_interior > 0
    assert np.isfinite(result.report.velocity_cm_s.max_abs)
    assert np.isfinite(result.report.acceleration_cm_s2.max_abs)
    assert result.report.position_cm.max_abs < 1.0  # cm: the stored centroid tracks the truth
    assert result.max_centroid_difference_cm == pytest.approx(0.0, abs=1e-9)
    assert list(tmp_path.iterdir()) == []  # temp folders are deleted


def test_full_density_scenario_has_exactly_the_requested_frames() -> None:
    from vl_data.generator.motion import frame_count

    for frames in (177, 300):
        assert frame_count(sweep.full_density_scenario(frames), 16) == frames
    with pytest.raises(ValueError):
        sweep.full_density_scenario(176)
