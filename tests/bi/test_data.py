"""Data-layer tests for the BI app. Units: cm, cm/s, cm/s^2, frames, s."""

import dataclasses
import os
import shutil
from collections import Counter
from pathlib import Path, PureWindowsPath

import numpy as np
import pyarrow as pa
import pytest

from vl_data.bi import data
from vl_data.bi.data import (
    AnalyzeOutcome,
    _is_link,
    _ps_quote,
    analyze_drive,
    ingest_command,
    list_lake_drives,
    load_quality,
    metric_rows,
    motion_command,
)
from vl_data.core.config import Config
from vl_data.core.lake import Lake
from vl_data.sdk.client import VLClient
from vl_data.usecase.config import MotionConfig
from vl_data.usecase.evaluate import EvalReport, MetricErrors, evaluate_drive

from .helpers import DROPPED_FRAME, BiDrive

UNKNOWN_DRIVE = "0" * 12  # a valid id with no rows in any lake here


def _snapshot(root: Path) -> dict[str, int]:
    """Relative path to size in bytes of every file under root."""
    return {str(p.relative_to(root)): p.stat().st_size for p in root.rglob("*") if p.is_file()}


def _analyze(
    d: BiDrive, config: Config, cfg: MotionConfig, raw_dir: Path | None = None
) -> AnalyzeOutcome:
    return analyze_drive(
        d.client, config, cfg, raw_dir or d.drive_dir.parent, d.root / "lake", d.drive_id
    )


def _analyze_id(d: BiDrive, config: Config, cfg: MotionConfig, drive: str) -> AnalyzeOutcome:
    return analyze_drive(d.client, config, cfg, d.drive_dir.parent, d.root / "lake", drive)


def test_rejected_drive_is_listed(clean: BiDrive, rejected: BiDrive) -> None:
    assert rejected.ingest.status == "rejected"
    assert rejected.client.drives() == []  # no keypoints
    assert list_lake_drives(rejected.lake) == [rejected.drive_id]
    assert list_lake_drives(clean.lake) == [clean.drive_id]


def test_missing_lake_lists_nothing(tmp_path: Path) -> None:
    lake_dir = tmp_path / "nolake"
    assert list_lake_drives(Lake(lake_dir)) == []
    assert not lake_dir.exists()


def test_quality_counts_match_ingest_result(clean: BiDrive) -> None:
    quality = load_quality(clean.client, clean.drive_id)
    assert quality is not None
    result = clean.ingest
    assert quality.drive == clean.drive_id
    assert quality.status == result.status == "ok"
    assert quality.frames_expected == result.frames_expected == 288
    assert quality.frames_in == result.frames_in
    assert quality.frames_kept == result.frames_kept
    assert quality.frames_rejected == result.frames_rejected == 0
    assert quality.frames_missing == result.frames_missing == 0
    assert quality.keypoint_rows == result.keypoint_rows
    assert quality.quarantine_by_code == result.quarantine_by_code == {}
    assert quality.scenario_hash is not None
    assert quality.scenario_hash.startswith(clean.drive_id)


def test_quality_dropped_frame_shows_vl001(dropped: BiDrive) -> None:
    quality = load_quality(dropped.client, dropped.drive_id)
    assert quality is not None
    assert quality.quarantine_by_code == {"VL-001": 1}
    assert quality.frames_missing == 1
    assert quality.frames_expected == quality.frames_in + 1
    assert quality.quarantine_by_code == dropped.ingest.quarantine_by_code
    frame_ids = dropped.client.quarantine(dropped.drive_id).column("frame_id").to_pylist()
    assert frame_ids == [DROPPED_FRAME]


def test_quality_of_rejected_drive_accepts_non_vl_codes(rejected: BiDrive) -> None:
    quality = load_quality(rejected.client, rejected.drive_id)
    assert quality is not None
    assert quality.status == "rejected"
    assert (quality.frames_in, quality.frames_kept, quality.keypoint_rows) == (0, 0, 0)
    assert list(quality.quarantine_by_code) == sorted(quality.quarantine_by_code)
    assert quality.quarantine_by_code == rejected.ingest.quarantine_by_code
    assert sum(quality.quarantine_by_code.values()) >= 1


def test_quality_unknown_drive_is_none(clean: BiDrive) -> None:
    assert load_quality(clean.client, UNKNOWN_DRIVE) is None


def test_quality_two_lineage_rows_raises(clean: BiDrive) -> None:
    class TwoRows(VLClient):
        def lineage(self, drive: str) -> pa.Table:
            one = super().lineage(drive)
            return pa.concat_tables([one, one])

    with pytest.raises(ValueError, match="lineage"):
        load_quality(TwoRows(clean.lake), clean.drive_id)


def test_analyze_ok_equals_evaluate_drive(
    clean: BiDrive, bi_config: Config, motion_cfg: MotionConfig
) -> None:
    outcome = _analyze(clean, bi_config, motion_cfg)
    direct = evaluate_drive(clean.drive_dir, clean.client, bi_config, motion_cfg)
    assert outcome.drive == clean.drive_id
    assert outcome.problem is None
    assert outcome.command is None
    assert outcome.report is not None
    assert outcome.report.passed is True
    assert outcome.report.frames.equals(direct.frames)


def test_analyze_missing_motion_of_ok_drive_gives_motion_command(
    unmotioned: BiDrive, bi_config: Config, motion_cfg: MotionConfig
) -> None:
    assert unmotioned.ingest.status == "ok" and not unmotioned.has_motion
    outcome = _analyze(unmotioned, bi_config, motion_cfg)
    assert outcome.report is None
    assert outcome.problem is not None and unmotioned.drive_id in outcome.problem
    assert outcome.command == motion_command(unmotioned.drive_id, unmotioned.root / "lake")


def test_analyze_rejected_drive_gives_ingest_command_not_motion(
    rejected: BiDrive, bi_config: Config, motion_cfg: MotionConfig
) -> None:
    assert not rejected.has_motion
    outcome = _analyze(rejected, bi_config, motion_cfg)
    assert outcome.report is None
    assert outcome.problem is not None and "rejected" in outcome.problem
    expected = ingest_command(rejected.drive_dir.parent, rejected.drive_id, rejected.root / "lake")
    assert outcome.command == expected
    assert " motion " not in expected


def test_analyze_unknown_drive_gives_ingest_command(
    clean: BiDrive, bi_config: Config, motion_cfg: MotionConfig
) -> None:
    outcome = _analyze_id(clean, bi_config, motion_cfg, UNKNOWN_DRIVE)
    assert outcome.report is None
    assert outcome.problem is not None and "not ingested" in outcome.problem
    expected = ingest_command(clean.drive_dir.parent, UNKNOWN_DRIVE, clean.root / "lake")
    assert outcome.command == expected
    assert " motion " not in expected


def test_analyze_unreadable_lineage_is_problem(
    unmotioned: BiDrive, bi_config: Config, motion_cfg: MotionConfig
) -> None:
    class Broken(VLClient):
        def lineage(self, drive: str) -> pa.Table:
            raise pa.ArrowInvalid("C:/secret/path")

    outcome = analyze_drive(
        Broken(unmotioned.lake),
        bi_config,
        motion_cfg,
        unmotioned.drive_dir.parent,
        unmotioned.root / "lake",
        unmotioned.drive_id,
    )
    assert outcome.report is None and outcome.command is None
    assert outcome.problem is not None and "secret" not in outcome.problem
    assert "ArrowInvalid" in outcome.problem


def test_analyze_missing_or_linked_raw_folder(
    clean: BiDrive, bi_config: Config, motion_cfg: MotionConfig, tmp_path: Path
) -> None:
    empty_raw = tmp_path / "empty_raw"
    empty_raw.mkdir()
    missing = _analyze(clean, bi_config, motion_cfg, empty_raw)
    assert missing.report is None and missing.problem is not None
    assert missing.command == ingest_command(empty_raw, clean.drive_id, clean.root / "lake")
    assert str(empty_raw) not in missing.problem

    link_raw = tmp_path / "link_raw"
    link_raw.mkdir()
    try:
        os.symlink(clean.drive_dir, link_raw / clean.drive_id, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("no privilege to create symlinks")
    linked = _analyze(clean, bi_config, motion_cfg, link_raw)
    assert linked.report is None and linked.problem is not None
    assert "link" in linked.problem
    assert linked.command == ingest_command(link_raw, clean.drive_id, clean.root / "lake")


def test_analyze_link_inside_drive_folder_is_refused(
    clean: BiDrive, bi_config: Config, motion_cfg: MotionConfig, tmp_path: Path
) -> None:
    copy = tmp_path / "raw" / clean.drive_id
    shutil.copytree(clean.drive_dir, copy)
    (copy / "ground_truth.parquet").unlink()
    try:
        os.symlink(clean.drive_dir / "ground_truth.parquet", copy / "ground_truth.parquet")
    except (OSError, NotImplementedError):
        pytest.skip("no privilege to create symlinks")
    outcome = _analyze(clean, bi_config, motion_cfg, tmp_path / "raw")
    assert outcome.report is None and outcome.problem is not None
    assert "link" in outcome.problem
    assert outcome.command == ingest_command(tmp_path / "raw", clean.drive_id, clean.root / "lake")


def test_analyze_raw_path_that_is_a_file_is_missing_folder(
    clean: BiDrive, bi_config: Config, motion_cfg: MotionConfig, tmp_path: Path
) -> None:
    (tmp_path / clean.drive_id).write_text("not a folder")
    outcome = _analyze(clean, bi_config, motion_cfg, tmp_path)
    assert outcome.report is None and outcome.problem is not None
    assert outcome.command == ingest_command(tmp_path, clean.drive_id, clean.root / "lake")


def test_analyze_unreadable_folder_is_problem(
    clean: BiDrive, bi_config: Config, motion_cfg: MotionConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(self: Path) -> object:
        raise PermissionError(13, "secret detail")

    monkeypatch.setattr(Path, "iterdir", boom)
    outcome = _analyze(clean, bi_config, motion_cfg)
    assert outcome.report is None and outcome.problem is not None
    assert "secret detail" not in outcome.problem
    assert outcome.command == ingest_command(
        clean.drive_dir.parent, clean.drive_id, clean.root / "lake"
    )


def test_analyze_tampered_drive_is_problem(
    clean: BiDrive, bi_config: Config, motion_cfg: MotionConfig, tmp_path: Path
) -> None:
    raw = tmp_path / "raw"
    copy = raw / clean.drive_id
    shutil.copytree(clean.drive_dir, copy)
    with (copy / "frames.parquet").open("ab") as handle:
        handle.write(b"x")
    outcome = _analyze(clean, bi_config, motion_cfg, raw)
    assert outcome.report is None
    assert outcome.problem is not None and clean.drive_id in outcome.problem
    assert str(tmp_path) not in outcome.problem
    assert os.sep not in outcome.problem


@pytest.mark.parametrize("error", [OSError("C:\\secret\\path"), ValueError("C:\\secret\\path")])
def test_analyze_other_errors_are_problems_without_paths(
    clean: BiDrive,
    bi_config: Config,
    motion_cfg: MotionConfig,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    def boom(*args: object, **kwargs: object) -> EvalReport:
        raise error

    monkeypatch.setattr(data, "evaluate_drive", boom)
    outcome = _analyze(clean, bi_config, motion_cfg)
    assert outcome.report is None and outcome.problem is not None
    assert "secret" not in outcome.problem
    assert type(error).__name__ in outcome.problem


def test_analyze_never_writes_lake(
    clean: BiDrive, rejected: BiDrive, bi_config: Config, motion_cfg: MotionConfig
) -> None:
    before = {d.root: _snapshot(d.root / "lake") for d in (clean, rejected)}
    raw_before = _snapshot(clean.drive_dir)
    _analyze(clean, bi_config, motion_cfg)
    _analyze(rejected, bi_config, motion_cfg)
    assert {d.root: _snapshot(d.root / "lake") for d in (clean, rejected)} == before
    assert _snapshot(clean.drive_dir) == raw_before


def test_metric_rows_match_independent_recompute(
    clean: BiDrive, bi_config: Config, motion_cfg: MotionConfig
) -> None:
    report = _analyze(clean, bi_config, motion_cfg).report
    assert report is not None
    frames = report.frames
    interior = np.asarray(frames.column("interior").to_pylist(), dtype=bool)

    def diff(est: str, true: str) -> np.ndarray:
        e = frames.column(est).to_numpy(zero_copy_only=False).astype(np.float64)
        t = frames.column(true).to_numpy(zero_copy_only=False).astype(np.float64)
        return e - t  # NaN where the estimate is null

    def stats(d: np.ndarray, mask: np.ndarray) -> tuple[float, float]:
        d = d[mask & ~np.isnan(d)]
        return float(np.abs(d).max()), float(np.sqrt(np.mean(d**2)))

    everything = np.ones_like(interior)
    expected = {
        "position": (diff("x_est_cm", "x_true_cm"), "cm", bi_config.tolerances.position_cm, False),
        "velocity": (
            diff("velocity_est_cm_s", "velocity_true_cm_s"),
            "cm/s",
            motion_cfg.tolerances.velocity_cm_s,
            True,
        ),
        "acceleration": (
            diff("acceleration_est_cm_s2", "acceleration_true_cm_s2"),
            "cm/s^2",
            motion_cfg.tolerances.acceleration_cm_s2,
            True,
        ),
    }
    rows = metric_rows(report, bi_config, motion_cfg)
    assert [r["metric"] for r in rows] == list(expected)
    for row in rows:
        d, unit, tolerance, has_all = expected[str(row["metric"])]
        imax, irms = stats(d, interior)
        assert row["unit"] == unit
        assert row["interior_max"] == pytest.approx(imax)
        assert row["interior_rms"] == pytest.approx(irms)
        assert row["tolerance"] == tolerance
        assert row["within"] is (imax <= tolerance)
        if has_all:
            amax, arms = stats(d, everything)
            assert row["all_max"] == pytest.approx(amax)
            assert row["all_rms"] == pytest.approx(arms)
        else:
            assert row["all_max"] is None and row["all_rms"] is None
    assert report.passed == all(bool(r["within"]) for r in rows)


def test_metric_rows_nan_is_not_within(
    clean: BiDrive, bi_config: Config, motion_cfg: MotionConfig
) -> None:
    report = _analyze(clean, bi_config, motion_cfg).report
    assert report is not None
    nan = MetricErrors(float("nan"), float("nan"))
    empty = dataclasses.replace(report, velocity_cm_s=nan)
    rows = metric_rows(empty, bi_config, motion_cfg)
    velocity = next(r for r in rows if r["metric"] == "velocity")
    assert velocity["within"] is False


def test_invalid_drive_id_rejected(
    clean: BiDrive, bi_config: Config, motion_cfg: MotionConfig, tmp_path: Path
) -> None:
    for bad in ("../x", "a/b", "", "a b", "x\\y", "x'; DROP"):
        with pytest.raises(ValueError, match="drive id"):
            load_quality(clean.client, bad)
        with pytest.raises(ValueError, match="drive id"):
            ingest_command(tmp_path, bad, tmp_path)
        with pytest.raises(ValueError, match="drive id"):
            motion_command(bad, tmp_path)
        outcome = analyze_drive(clean.client, bi_config, motion_cfg, tmp_path, tmp_path, bad)
        assert outcome.report is None and outcome.problem is not None
        assert outcome.command is None
        assert outcome.drive == bad


def test_command_texts() -> None:
    raw, lake = PureWindowsPath("C:/data/raw"), PureWindowsPath("D:/my lake")
    assert _ps_quote(raw / "abc123") == "'C:\\data\\raw\\abc123'"
    assert ingest_command(raw, "abc123", lake) == (  # type: ignore[arg-type]
        "python -m vl_data ingest 'C:\\data\\raw\\abc123' --lake-dir 'D:\\my lake'"
    )
    assert motion_command("abc123", lake) == (  # type: ignore[arg-type]
        "python -m vl_data motion abc123 --lake-dir 'D:\\my lake'"
    )


def test_ingest_command_names_the_lake(tmp_path: Path) -> None:
    text = ingest_command(tmp_path / "raw", "abc123", tmp_path / "other_lake")
    assert text.endswith(f"--lake-dir {_ps_quote(tmp_path / 'other_lake')}")
    assert str(tmp_path / "other_lake") in text


@pytest.mark.parametrize(
    "name, quoted",
    [
        ("a$(x)b", "'a$(x)b'"),
        ("a`b", "'a`b'"),
        ("it's", "'it''s'"),
        ("a\u201db", "'a\u201db'"),
        ("a\u2018b\u2019c", "'a\u2018\u2018b\u2019\u2019c'"),
        ("$env:X", "'$env:X'"),
        ("a‘b", "'a‘‘b'"),
        ("a’b", "'a’’b'"),
        ("a‚b", "'a‚‚b'"),
        ("a‛b", "'a‛‛b'"),
        ("a‚‛b'c", "'a‚‚‛‛b''c'"),
        ("$(calc)`x;y", "'$(calc)`x;y'"),
    ],
)
def test_commands_quote_paths_as_single_quoted_literals(name: str, quoted: str) -> None:
    folder = PureWindowsPath("C:/x") / name
    prefix = "C:\\x\\"
    assert _ps_quote(folder) == "'" + prefix + quoted[1:]  # type: ignore[arg-type]
    assert (
        motion_command("abc123", folder)
        == (  # type: ignore[arg-type]
            "python -m vl_data motion abc123 --lake-dir '" + prefix + quoted[1:]
        )
    )
    assert '"' not in ingest_command(folder, "abc123", folder)  # type: ignore[arg-type]


def test_quote_keeps_hostile_path_inside_one_literal() -> None:
    folder = PureWindowsPath("C:/x") / "a$(calc)`b;c\u201a\u201b'd"
    text = _ps_quote(folder)  # type: ignore[arg-type]
    quotes = ("'", "\u2018", "\u2019", "\u201a", "\u201b")
    inner = text[1:-1]
    assert text[0] == "'" and text[-1] == "'"
    for quote in quotes:
        assert inner.count(quote) % 2 == 0  # every quote character is doubled
        assert quote + quote in inner or quote not in inner
    stripped = inner
    for quote in quotes:
        stripped = stripped.replace(quote + quote, "")
    assert not any(quote in stripped for quote in quotes)  # nothing can close the literal
    assert "$(calc)`b;c" in inner


class _LineageStub(VLClient):
    """Client with an empty motion table and a canned lineage."""

    def __init__(self, lake: Lake, lineage: pa.Table | Exception) -> None:
        super().__init__(lake)
        self._lineage = lineage

    def motion(self, drive: str) -> pa.Table:
        return pa.table({"frame": pa.array([], pa.int64())})

    def lineage(self, drive: str) -> pa.Table:
        if isinstance(self._lineage, Exception):
            raise self._lineage
        return self._lineage


def test_analyze_two_lineage_rows_is_unexpected_not_rejected(
    clean: BiDrive, bi_config: Config, motion_cfg: MotionConfig
) -> None:
    stub = _LineageStub(clean.lake, pa.table({"status": ["ok", "ok"]}))
    outcome = analyze_drive(
        stub, bi_config, motion_cfg, clean.drive_dir.parent, clean.root / "lake", clean.drive_id
    )
    assert outcome.report is None and outcome.command is None
    assert outcome.problem is not None
    assert "unexpected lineage rows" in outcome.problem
    assert "rejected" not in outcome.problem
    assert clean.drive_id in outcome.problem
    assert str(clean.root) not in outcome.problem


def test_analyze_lineage_value_error_is_problem(
    clean: BiDrive, bi_config: Config, motion_cfg: MotionConfig
) -> None:
    stub = _LineageStub(clean.lake, ValueError("C:/secret/path"))
    outcome = analyze_drive(
        stub, bi_config, motion_cfg, clean.drive_dir.parent, clean.root / "lake", clean.drive_id
    )
    assert outcome.report is None and outcome.command is None
    assert outcome.problem is not None
    assert "ValueError" in outcome.problem
    assert "secret" not in outcome.problem


def test_analyze_unreadable_motion_table_is_problem(
    clean: BiDrive, bi_config: Config, motion_cfg: MotionConfig
) -> None:
    class Broken(VLClient):
        def motion(self, drive: str) -> pa.Table:
            raise pa.ArrowInvalid("C:/secret/path")

    outcome = analyze_drive(
        Broken(clean.lake),
        bi_config,
        motion_cfg,
        clean.drive_dir.parent,
        clean.root,
        clean.drive_id,
    )
    assert outcome.report is None and outcome.problem is not None
    assert "secret" not in outcome.problem
    assert "ArrowInvalid" in outcome.problem


def test_analyze_link_detected_without_link_privilege(
    clean: BiDrive, bi_config: Config, motion_cfg: MotionConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda self: self.name == clean.drive_id or real(self))
    outcome = _analyze(clean, bi_config, motion_cfg)
    assert outcome.report is None and outcome.problem is not None
    assert "link" in outcome.problem
    assert outcome.command == ingest_command(
        clean.drive_dir.parent, clean.drive_id, clean.root / "lake"
    )


def test_quality_mapping_with_two_fault_kinds(multi_fault: BiDrive) -> None:
    quality = load_quality(multi_fault.client, multi_fault.drive_id)
    assert quality is not None
    quarantine = multi_fault.client.quarantine(multi_fault.drive_id)
    codes = quarantine.column("code").to_pylist()
    row_index = quarantine.column("row_index").to_pylist()
    with_row = sum(1 for value in row_index if value is not None)
    missing = sum(1 for code in codes if code == "VL-001")
    assert quality.frames_missing == missing
    assert quality.frames_rejected == with_row
    assert quality.frames_missing != quality.frames_rejected
    assert with_row >= 1 and missing >= 1
    assert quality.quarantine_by_code == dict(Counter(codes))
    assert list(quality.quarantine_by_code) == sorted(set(codes))
    assert len(quality.quarantine_by_code) >= 2


def test_metric_rows_within_boundary(
    clean: BiDrive, bi_config: Config, motion_cfg: MotionConfig
) -> None:
    report = _analyze(clean, bi_config, motion_cfg).report
    assert report is not None
    tolerance = motion_cfg.tolerances.velocity_cm_s

    def within(max_abs: float) -> object:
        edited = dataclasses.replace(report, velocity_cm_s=MetricErrors(max_abs, 0.0))
        rows = metric_rows(edited, bi_config, motion_cfg)
        return next(r for r in rows if r["metric"] == "velocity")["within"]

    assert within(tolerance) is True
    assert within(tolerance + 1e-6) is False


def test_is_link_detects_junction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = tmp_path / "junction_like"
    folder.mkdir()
    assert _is_link(folder) is False
    monkeypatch.setattr(Path, "is_junction", lambda self: self.name == folder.name, raising=False)
    assert _is_link(folder) is True


def test_read_problem_hides_paths_of_file_errors() -> None:
    secret = r"C:\secret\x.parquet"
    assert data.read_problem(ValueError("invalid drive id")) == "invalid drive id"
    for error in (OSError(secret), pa.ArrowInvalid(f"Failed to open local file '{secret}'")):
        text = data.read_problem(error)
        assert text == type(error).__name__
        assert "secret" not in text
