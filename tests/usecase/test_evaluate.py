"""evaluate_drive on a small generated, ingested drive. Units: cm, cm/s, cm/s^2, frames."""

import json
import math
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from vl_data.core.config import ToleranceConfig
from vl_data.core.lake_schemas import LINEAGE_SCHEMA, MOTION_SCHEMA
from vl_data.usecase import evaluate
from vl_data.usecase.config import MotionConfig, MotionTolerances
from vl_data.usecase.evaluate import EvalReport, EvaluationError, evaluate_drive

from .helpers import MadeDrive, interior_from_truth, max_rms

MakeDrive = Callable[[], MadeDrive]
SRC = Path(__file__).resolve().parents[2] / "src" / "vl_data"
OFFSET_CM = 49.5  # (size_cm - 1) / 2 for the 100 cm body
TRUTH = "ground_truth.parquet"


def _run(d: MadeDrive) -> EvalReport:
    return evaluate_drive(d.drive_dir, d.client, d.config, d.motion_cfg)


def _truth(d: MadeDrive) -> pa.Table:
    return pq.read_table(d.drive_dir / TRUTH)


def _interior(d: MadeDrive) -> np.ndarray[Any, np.dtype[np.bool_]]:
    truth = _truth(d)
    return interior_from_truth(
        truth.column("frame_id").to_numpy(),
        truth.column("acceleration_cm_s2").to_numpy(),
        d.motion_cfg.window_frames / 2,
    )


def _swap_lineage(d: MadeDrive, **changes: object) -> None:
    row = d.client.lineage(d.drive_id).to_pylist()[0]
    row.update(changes)
    table = pa.Table.from_pylist([row], schema=LINEAGE_SCHEMA)
    d.lake.swap_partition("lineage", d.drive_id, table.to_batches())


def _swap_motion(d: MadeDrive, table: pa.Table) -> None:
    d.lake.swap_partition("motion", d.drive_id, table.cast(MOTION_SCHEMA).to_batches())


def _set_motion_column(d: MadeDrive, name: str, values: pa.Array) -> None:
    motion = d.client.motion(d.drive_id)
    idx = motion.schema.get_field_index(name)
    _swap_motion(d, motion.set_column(idx, MOTION_SCHEMA.field(name), values))


def _with_nulls(d: MadeDrive, name: str, rows: list[int], shift: float = 0.0) -> None:
    """Add shift to a motion column and make the given row positions null."""
    values = d.client.motion(d.drive_id).column(name).to_numpy(zero_copy_only=False) + shift
    values[rows] = np.nan
    _set_motion_column(d, name, pa.array(values, mask=np.isnan(values)))


def _fail_if_called(*args: object, **kwargs: object) -> pa.Table:
    raise AssertionError("ground truth was read")


@pytest.fixture
def no_truth_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any way of opening the ground truth file in evaluate.py fail the test."""
    monkeypatch.setattr(evaluate, "read_table", _fail_if_called)
    monkeypatch.setattr(evaluate, "ParquetFile", _fail_if_called)


def test_clean_drive_passes(shared_drive: MadeDrive) -> None:
    d = shared_drive
    report = _run(d)
    assert report.frames_compared == d.frame_count
    assert report.frames_interior > 0
    assert report.position_cm.max_abs <= d.config.tolerances.position_cm
    assert report.velocity_cm_s.max_abs <= d.motion_cfg.tolerances.velocity_cm_s
    assert report.acceleration_cm_s2.max_abs <= d.motion_cfg.tolerances.acceleration_cm_s2
    assert report.acceleration_all_cm_s2.max_abs > report.acceleration_cm_s2.max_abs
    assert report.velocity_all_cm_s.max_abs > report.velocity_cm_s.max_abs
    assert report.passed is True


def test_metrics_match_independent_calculation(shared_drive: MadeDrive) -> None:
    report = _run(shared_drive)
    f = report.frames
    interior = f.column("interior").to_numpy(zero_copy_only=False)

    def col(name: str) -> np.ndarray[Any, np.dtype[np.float64]]:
        return f.column(name).fill_null(np.nan).to_numpy()

    def check(got: Any, est: str, true: str, mask: Any) -> None:
        e, t = col(est), col(true)
        keep = mask & ~np.isnan(e)
        want_max, want_rms = max_rms(e[keep], t[keep])
        assert got.max_abs == pytest.approx(want_max)
        assert got.rms == pytest.approx(want_rms)

    everything = np.ones(f.num_rows, dtype=bool)
    check(report.position_cm, "x_est_cm", "x_true_cm", interior)
    check(report.velocity_cm_s, "velocity_est_cm_s", "velocity_true_cm_s", interior)
    check(report.acceleration_cm_s2, "acceleration_est_cm_s2", "acceleration_true_cm_s2", interior)
    check(report.velocity_all_cm_s, "velocity_est_cm_s", "velocity_true_cm_s", everything)
    check(
        report.acceleration_all_cm_s2,
        "acceleration_est_cm_s2",
        "acceleration_true_cm_s2",
        everything,
    )


def test_null_velocities_are_left_out_of_both_error_sets(make_drive: MakeDrive) -> None:
    d = make_drive()
    nulled = [0, 1, 60, 61, 62]  # edge frames and interior frames
    _with_nulls(d, "velocity_cm_s", nulled)
    report = _run(d)
    f = report.frames
    interior = f.column("interior").to_numpy(zero_copy_only=False)
    est = f.column("velocity_est_cm_s").fill_null(np.nan).to_numpy()
    true = f.column("velocity_true_cm_s").to_numpy()
    assert interior[60:63].all()
    ok = ~np.isnan(est)
    assert not ok[nulled].any()
    want_in = max_rms(est[ok & interior], true[ok & interior])
    want_all = max_rms(est[ok], true[ok])
    assert report.velocity_cm_s.max_abs == pytest.approx(want_in[0])
    assert report.velocity_cm_s.rms == pytest.approx(want_in[1])
    assert report.velocity_all_cm_s.max_abs == pytest.approx(want_all[0])
    assert report.velocity_all_cm_s.rms == pytest.approx(want_all[1])


def test_null_counts_are_exact(make_drive: MakeDrive) -> None:
    d = make_drive()
    before = d.client.motion(d.drive_id)
    already = before.column("velocity_cm_s").is_null().to_numpy(
        zero_copy_only=False
    ) | before.column("acceleration_cm_s2").is_null().to_numpy(zero_copy_only=False)
    vel_rows, acc_rows = [0, 1, 62], [60, 61, 62]  # frame 62 has both estimates null
    assert not already[[0, 1, 60, 61, 62]].any()
    _with_nulls(d, "velocity_cm_s", vel_rows)
    _with_nulls(d, "acceleration_cm_s2", acc_rows)
    report = _run(d)
    assert report.frames_null == int(already.sum()) + 5
    assert report.frames_compared == d.frame_count
    assert report.frames.column("velocity_est_cm_s").null_count == (
        before.column("velocity_cm_s").null_count + len(vel_rows)
    )
    assert report.frames.column("acceleration_est_cm_s2").null_count == (
        before.column("acceleration_cm_s2").null_count + len(acc_rows)
    )


def test_interior_count_matches_truth_file(shared_drive: MadeDrive) -> None:
    d = shared_drive
    mask = _interior(d)
    report = _run(d)
    assert report.frames_interior == int(np.count_nonzero(mask))
    assert report.frames.column("interior").to_pylist() == mask.tolist()


def test_altered_motion_fails(make_drive: MakeDrive) -> None:
    d = make_drive()
    vel = d.client.motion(d.drive_id).column("velocity_cm_s").to_numpy(zero_copy_only=False)
    _set_motion_column(d, "velocity_cm_s", pa.array(vel + 100.0))
    report = _run(d)
    assert report.velocity_cm_s.max_abs > 90.0
    assert report.passed is False


def test_position_gate(shared_drive: MadeDrive) -> None:
    d = shared_drive
    tight = d.config.model_copy(update={"tolerances": ToleranceConfig(position_cm=1e-6)})
    report = evaluate_drive(d.drive_dir, d.client, tight, d.motion_cfg)
    assert report.position_cm.max_abs > 1e-6
    assert report.passed is False


def test_tolerances_gate_the_result(shared_drive: MadeDrive) -> None:
    d = shared_drive
    tight = d.motion_cfg.model_copy(
        update={"tolerances": MotionTolerances(velocity_cm_s=1e-6, acceleration_cm_s2=1e-6)}
    )
    assert evaluate_drive(d.drive_dir, d.client, d.config, tight).passed is False


def test_no_interior_frames_fails(shared_drive: MadeDrive) -> None:
    d = shared_drive
    wide = MotionConfig(window_frames=1000, min_points=5)
    report = evaluate_drive(d.drive_dir, d.client, d.config, wide)
    assert report.frames_interior == 0
    assert report.passed is False


def test_all_interior_velocities_null_fails(make_drive: MakeDrive) -> None:
    d = make_drive()
    _with_nulls(d, "velocity_cm_s", np.flatnonzero(_interior(d)).tolist())
    report = _run(d)
    assert report.frames_interior > 0
    assert math.isnan(report.velocity_cm_s.max_abs)
    assert report.passed is False


def test_stored_nan_counts_as_null(make_drive: MakeDrive) -> None:
    d = make_drive()
    vel = d.client.motion(d.drive_id).column("velocity_cm_s").to_numpy(zero_copy_only=False).copy()
    vel[[60, 61]] = np.nan
    _set_motion_column(d, "velocity_cm_s", pa.array(vel))  # NaN values, no validity mask
    report = _run(d)
    assert report.frames_null == 2
    assert report.frames.column("velocity_est_cm_s").null_count == 2
    assert math.isfinite(report.velocity_cm_s.max_abs)


def test_relative_drive_path(shared_drive: MadeDrive, monkeypatch: pytest.MonkeyPatch) -> None:
    d = shared_drive
    monkeypatch.chdir(d.drive_dir)
    report = evaluate_drive(Path("."), d.client, d.config, d.motion_cfg)
    assert report.frames_compared == d.frame_count


def test_hash_mismatch_raises(make_drive: MakeDrive, no_truth_read: None) -> None:
    d = make_drive()
    _swap_lineage(d, scenario_hash="0" * 64)
    with pytest.raises(EvaluationError, match="scenario_hash"):
        _run(d)


def test_ground_truth_hash_mismatch_raises(make_drive: MakeDrive, no_truth_read: None) -> None:
    d = make_drive()
    _swap_lineage(d, ground_truth_sha256="f" * 64)
    with pytest.raises(EvaluationError, match="ground_truth_sha256"):
        _run(d)


def test_rejected_lineage_raises(make_drive: MakeDrive, no_truth_read: None) -> None:
    d = make_drive()
    _swap_lineage(d, status="rejected")
    with pytest.raises(EvaluationError, match="status 'rejected'"):
        _run(d)


def test_changed_file_raises(make_drive: MakeDrive, no_truth_read: None) -> None:
    d = make_drive()
    path = d.drive_dir / TRUTH
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0xFF
    path.write_bytes(bytes(data))
    with pytest.raises(EvaluationError, match="changed") as info:
        _run(d)
    assert str(d.drive_dir) not in str(info.value)


def test_manifest_without_truth_entry_raises(make_drive: MakeDrive, no_truth_read: None) -> None:
    d = make_drive()
    path = d.drive_dir / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    del manifest["files"][TRUTH]
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(EvaluationError):
        _run(d)


def test_only_evaluate_mentions_ground_truth() -> None:
    offenders = [
        str(p.relative_to(SRC))
        for folder in ("usecase", "sdk")
        for p in (SRC / folder).rglob("*.py")
        if p.name != "evaluate.py" and "ground_truth" in p.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_interior_excludes_speed_changes(shared_drive: MadeDrive) -> None:
    d = shared_drive
    report = _run(d)
    half = d.motion_cfg.window_frames / 2
    frame_id = report.frames.column("frame_id").to_numpy()
    interior = report.frames.column("interior").to_numpy(zero_copy_only=False)
    far = frame_id < d.frame_count - 1
    for kink in d.kink_frames:
        near = np.abs(frame_id - kink) <= half
        assert not interior[near].any()
        far &= ~near
    assert interior[far].all()
    assert not interior[-1]
    assert int(interior.sum()) == report.frames_interior


def test_report_has_per_frame_table(shared_drive: MadeDrive) -> None:
    d = shared_drive
    table = _run(d).frames
    assert table.num_rows == d.frame_count
    assert table.column_names == [
        "frame_id",
        "interior",
        "x_est_cm",
        "x_true_cm",
        "velocity_est_cm_s",
        "velocity_true_cm_s",
        "acceleration_est_cm_s2",
        "acceleration_true_cm_s2",
    ]
    assert table.schema.field("frame_id").type == pa.int64()
    assert table.schema.field("interior").type == pa.bool_()
    truth = _truth(d)
    motion = d.client.motion(d.drive_id)
    assert table.column("frame_id").to_pylist() == truth.column("frame_id").to_pylist()
    assert table.column("x_true_cm").to_pylist() == truth.column("x_cm").to_pylist()
    for got, want in [
        ("velocity_true_cm_s", "velocity_cm_s"),
        ("acceleration_true_cm_s2", "acceleration_cm_s2"),
    ]:
        assert table.column(got).to_pylist() == truth.column(want).to_pylist()
    est = motion.column("x_cm").to_numpy() - OFFSET_CM
    assert np.allclose(table.column("x_est_cm").to_numpy(), est)
    for got, want in [
        ("velocity_est_cm_s", "velocity_cm_s"),
        ("acceleration_est_cm_s2", "acceleration_cm_s2"),
    ]:
        assert table.column(got).to_pylist() == motion.column(want).to_pylist()


def test_missing_lineage_raises(make_drive: MakeDrive) -> None:
    d = make_drive()
    d.lake.swap_partition("lineage", d.drive_id, [])
    with pytest.raises(EvaluationError, match="lineage"):
        _run(d)


def test_two_lineage_rows_raise(make_drive: MakeDrive) -> None:
    d = make_drive()
    row = d.client.lineage(d.drive_id).to_pylist()[0]
    table = pa.Table.from_pylist([row, row], schema=LINEAGE_SCHEMA)
    d.lake.swap_partition("lineage", d.drive_id, table.to_batches())
    with pytest.raises(EvaluationError, match="lineage"):
        _run(d)


def test_missing_motion_raises(make_drive: MakeDrive) -> None:
    d = make_drive()
    d.lake.swap_partition("motion", d.drive_id, [])
    with pytest.raises(EvaluationError, match="motion"):
        _run(d)


def test_missing_frame_raises(make_drive: MakeDrive) -> None:
    d = make_drive()
    motion = d.client.motion(d.drive_id)
    _swap_motion(d, motion.filter(pa.compute.not_equal(motion.column("frame_id"), 100)))
    with pytest.raises(EvaluationError, match="motion has 287 rows"):
        _run(d)


def test_extra_frame_raises(make_drive: MakeDrive) -> None:
    d = make_drive()
    motion = d.client.motion(d.drive_id)
    extra = pa.Table.from_pylist([{**motion.slice(0, 1).to_pylist()[0], "frame_id": 288}])
    _swap_motion(d, pa.concat_tables([motion, extra.cast(motion.schema)]))
    with pytest.raises(EvaluationError, match="motion has 289 rows"):
        _run(d)


def test_different_frame_ids_raise(make_drive: MakeDrive) -> None:
    d = make_drive()
    ids = d.client.motion(d.drive_id).column("frame_id").to_numpy() + 1000
    _set_motion_column(d, "frame_id", pa.array(ids))
    with pytest.raises(EvaluationError, match="different frame ids"):
        _run(d)


def test_truth_row_count_mismatch_raises(
    make_drive: MakeDrive, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = make_drive()
    stub = SimpleNamespace(metadata=SimpleNamespace(num_rows=5), close=lambda: None)
    monkeypatch.setattr(evaluate, "ParquetFile", lambda path: stub)
    with pytest.raises(EvaluationError, match="ground truth has 5 rows"):
        _run(d)


def test_truth_with_other_columns_raises(
    make_drive: MakeDrive, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = make_drive()
    other = _truth(d).rename_columns(["frame_id", "timestamp_ns", "x_cm", "v", "a"])
    monkeypatch.setattr(evaluate, "read_table", lambda path: other)
    with pytest.raises(EvaluationError, match="velocity_cm_s"):
        _run(d)


def test_truth_with_wrong_type_raises(
    make_drive: MakeDrive, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = make_drive()
    truth = _truth(d)
    idx = truth.schema.get_field_index("x_cm")
    wrong = truth.set_column(idx, "x_cm", truth.column("x_cm").cast(pa.float32()))
    monkeypatch.setattr(evaluate, "read_table", lambda path: wrong)
    with pytest.raises(EvaluationError, match="x_cm"):
        _run(d)


def test_read_error_becomes_evaluation_error(
    make_drive: MakeDrive, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = make_drive()

    def broken(path: Path) -> pa.Table:
        raise OSError(f"cannot open {path}")

    monkeypatch.setattr(evaluate, "read_table", broken)
    with pytest.raises(EvaluationError, match="OSError") as info:
        _run(d)
    assert str(d.drive_dir) not in str(info.value)


def test_unreadable_manifest_raises(make_drive: MakeDrive) -> None:
    d = make_drive()
    (d.drive_dir / "manifest.json").write_text("not json", encoding="utf-8")
    with pytest.raises(EvaluationError, match="cannot read manifest.json") as info:
        _run(d)
    assert str(d.drive_dir) not in str(info.value)


def test_interior_mask_public_helper_matches_independent_definition() -> None:
    # Acceleration steps at frames 10 and 20 (a difference from the previous frame).
    acc = np.zeros(30)
    acc[10:20] = 50.0
    for half in (1.0, 4.0, 8.0):
        got = evaluate.interior_mask(acc, half)
        expected = interior_from_truth(np.arange(30), acc, half)
        assert got.dtype == np.bool_ and got.shape == (30,)
        assert np.array_equal(got, expected)
    mask = evaluate.interior_mask(acc, 4.0)
    assert not mask[-1]  # the last frame is never interior
    assert not mask[10] and not mask[14] and mask[25] and not mask[16]
    assert not mask[6] and mask[5]  # |6 - 10| = 4 is within the half window of 4; |5 - 10| is not


def test_interior_mask_edge_cases() -> None:
    flat = evaluate.interior_mask(np.zeros(5), 2.0)
    assert flat.tolist() == [True, True, True, True, False]  # no change; only the last excluded
    one = evaluate.interior_mask(np.array([3.0]), 2.0)
    assert one.tolist() == [False]
