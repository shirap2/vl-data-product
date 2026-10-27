"""Tests for the ingest orchestration. Units: cm, ns; fps in frames/s.

Drives use a 10 x 10 x 10 body (1,000 points per frame) so the 1,000-frame demo stays small.
"""

import json
import re
import shutil
import subprocess
import sys
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from vl_data.core.config import BodyConfig, Config
from vl_data.core.hashing import file_sha256
from vl_data.core.lake import SWAP_DIR, Lake
from vl_data.core.lake_schemas import LINEAGE_SCHEMA
from vl_data.core.manifest import read_manifest
from vl_data.core.schemas import decode_points
from vl_data.etl import ingest as ingest_module
from vl_data.etl.config import EtlConfig, KeypointSpec
from vl_data.etl.ingest import (
    IngestResult,
    _check_footer,
    _clean,
    _int_text,
    _Reject,
    ingest_drive,
)
from vl_data.generator.drive import Drive, generate
from vl_data.generator.faults import (
    BreakSchema,
    CorruptPoints,
    DropFrame,
    DuplicateFrame,
    Fault,
    ShiftTimestamp,
    Teleport,
    inject_faults,
)
from vl_data.generator.models import Scenario
from vl_data.generator.writer import write_drive

from .helpers import build_table, columns

FRAMES = "frames.parquet"


@pytest.fixture(scope="session")
def body10() -> Config:
    """A 10 x 10 x 10 body, 40 cm cube."""
    return Config(body=BodyConfig(size_cm=40, points_per_axis=10))


@pytest.fixture(scope="session")
def drive10(demo_scenario: Scenario, body10: Config) -> Drive:
    """The unmodified demo drive on the 10-per-axis body."""
    return generate(demo_scenario, body10)


@pytest.fixture(scope="session")
def clean_dir10(tmp_path_factory: pytest.TempPathFactory, drive10: Drive, body10: Config) -> Path:
    """The demo drive written once per session."""
    return write_drive(drive10, tmp_path_factory.mktemp("clean10"), body10)


@pytest.fixture
def clean_copy(tmp_path: Path, clean_dir10: Path) -> Path:
    """A private copy of the clean drive folder (same folder name)."""
    target = tmp_path / "copy" / clean_dir10.name
    shutil.copytree(clean_dir10, target)
    return target


@pytest.fixture
def write10(tmp_path: Path, drive10: Drive, body10: Config) -> Callable[..., Path]:
    """Return write(faults, table): inject faults or use a table, and write a drive folder."""
    counter = 0

    def write(faults: Sequence[Fault] = (), table: pa.Table | None = None) -> Path:
        nonlocal counter
        counter += 1
        drive, _ = inject_faults(drive10, faults)
        if table is not None:
            drive = replace(drive, frames=table)
        return write_drive(drive, tmp_path / f"w{counter}", body10)

    return write


@pytest.fixture
def lake(tmp_path: Path) -> Lake:
    return Lake(tmp_path / "lake")


@pytest.fixture
def etl() -> EtlConfig:
    return EtlConfig()


def _edit_manifest(drive_dir: Path, edit: Callable[[dict[str, Any]], None]) -> None:
    path = drive_dir / "manifest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    edit(data)
    path.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _refresh_entry(drive_dir: Path, name: str) -> None:
    """Make the manifest agree with the file on disk (so only the content is bad)."""

    def edit(data: dict[str, Any]) -> None:
        path = drive_dir / name
        data["files"][name] = {"sha256": file_sha256(path), "bytes": path.stat().st_size}

    _edit_manifest(drive_dir, edit)


def _lake_bytes(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and SWAP_DIR not in p.relative_to(root).parts
    }


def _quarantine(lake: Lake, drive: str) -> list[dict[str, Any]]:
    return lake.read("quarantine", drive).to_pylist()


def _assert_rejected(result: IngestResult, lake: Lake, code: str, rows: int = 1) -> None:
    assert result.status == "rejected"
    assert result.frames_kept == 0
    assert result.keypoint_rows == 0
    assert result.quarantine_by_code == {code: rows}
    assert result.frames_quarantined == rows
    assert lake.list_drives("keypoints") == []
    q = _quarantine(lake, result.drive)
    assert [r["code"] for r in q] == [code] * rows
    assert all(r["frame_id"] is None and r["row_index"] is None for r in q)
    lineage = lake.read("lineage", result.drive).to_pylist()
    assert len(lineage) == 1
    assert lineage[0]["status"] == "rejected"
    assert (lineage[0]["frames_in"], lineage[0]["frames_kept"], lineage[0]["keypoint_rows"]) == (
        0,
        0,
        0,
    )


def test_clean_drive_ingested(clean_copy: Path, lake: Lake, etl: EtlConfig) -> None:
    result = ingest_drive(clean_copy, lake, etl)
    manifest = read_manifest(clean_copy)
    assert result == IngestResult(
        drive=clean_copy.name,
        status="ok",
        frames_expected=1000,
        frames_in=1000,
        frames_kept=1000,
        frames_rejected=0,
        frames_missing=0,
        keypoint_rows=9000,
        quarantine_by_code={},
        frames_quarantined=0,
        points_per_frame_in=1000,
        keypoints_per_frame_out=9,
    )
    assert clean_copy.name == manifest.scenario_hash[:12]
    kp = lake.read("keypoints", clean_copy.name)
    assert kp.num_rows == 9000
    assert lake.read("quarantine", clean_copy.name).num_rows == 0
    names = [s.name for s in etl.keypoints]
    first = kp.slice(0, 9).column("keypoint").to_pylist()
    assert first == names
    con = lake.connect()
    try:
        rows = con.execute(
            "SELECT keypoint, count(*) FROM keypoints GROUP BY 1 ORDER BY 1"
        ).fetchall()
    finally:
        con.close()
    assert [r[1] for r in rows] == [1000] * 9


@pytest.mark.parametrize(
    ("fault", "code", "kept", "frames_in"),
    [
        (DropFrame(100), "VL-001", 999, 999),
        (ShiftTimestamp(100, 1000), "VL-002", 999, 1000),
        (DuplicateFrame(100), "VL-003", 1000, 1001),
        (CorruptPoints(100), "VL-004", 999, 1000),
        (Teleport(100, 1000), "VL-006", 999, 1000),
    ],
)
def test_each_frame_fault_one_quarantine_row(
    write10: Callable[..., Path],
    lake: Lake,
    etl: EtlConfig,
    fault: Fault,
    code: str,
    kept: int,
    frames_in: int,
) -> None:
    drive_dir = write10([fault])
    result = ingest_drive(drive_dir, lake, etl)
    assert result.status == "ok"
    assert result.frames_expected == 1000
    assert result.frames_in == frames_in
    assert result.frames_kept == kept
    assert result.keypoint_rows == kept * 9
    assert result.quarantine_by_code == {code: 1}
    assert result.frames_quarantined == 1
    assert result.frames_missing == (1 if code == "VL-001" else 0)
    assert result.frames_rejected == (0 if code == "VL-001" else 1)
    q = _quarantine(lake, drive_dir.name)
    assert len(q) == 1
    assert q[0]["code"] == code
    assert q[0]["frame_id"] == 100
    assert (q[0]["row_index"] is None) == (code == "VL-001")
    ids = lake.read("keypoints", drive_dir.name).column("frame_id").to_pylist()
    assert len(ids) == kept * 9
    assert (100 in ids) == (code == "VL-003")
    lineage = lake.read("lineage", drive_dir.name).to_pylist()[0]
    assert lineage["frames_rejected"] == result.frames_rejected
    assert lineage["frames_missing"] == result.frames_missing


def test_empty_frames_file_quarantines_every_frame(
    write10: Callable[..., Path], drive10: Drive, lake: Lake, etl: EtlConfig
) -> None:
    drive_dir = write10(table=drive10.frames.slice(0, 0))
    result = ingest_drive(drive_dir, lake, etl)
    assert result.status == "ok"
    assert (result.frames_in, result.frames_kept, result.keypoint_rows) == (0, 0, 0)
    assert result.quarantine_by_code == {"VL-001": 1000}
    assert lake.read("keypoints", drive_dir.name).num_rows == 0
    assert lake.read("lineage", drive_dir.name).to_pylist()[0]["frames_missing"] == 1000


def test_tampered_file_rejects_drive(clean_copy: Path, lake: Lake, etl: EtlConfig) -> None:
    path = clean_copy / FRAMES
    raw = bytearray(path.read_bytes())
    raw[len(raw) // 2] ^= 0xFF
    path.write_bytes(bytes(raw))
    result = ingest_drive(clean_copy, lake, etl)
    _assert_rejected(result, lake, "INTEGRITY")
    assert "changed: frames.parquet" in _quarantine(lake, result.drive)[0]["message"]
    assert result.drive == clean_copy.name
    assert result.points_per_frame_in == 1000


def test_extra_file_and_two_problems_give_two_rows(
    clean_copy: Path, lake: Lake, etl: EtlConfig
) -> None:
    (clean_copy / "notes.txt").write_text("x", encoding="utf-8")
    with (clean_copy / FRAMES).open("ab") as f:
        f.write(b"x")  # size and hash both differ: two problems
    result = ingest_drive(clean_copy, lake, etl)
    _assert_rejected(result, lake, "INTEGRITY", rows=3)
    text = " ".join(r["message"] for r in _quarantine(lake, result.drive))
    assert "extra: notes.txt" in text


def test_unreadable_manifest_rejects_drive(clean_copy: Path, lake: Lake, etl: EtlConfig) -> None:
    (clean_copy / "manifest.json").unlink()
    result = ingest_drive(clean_copy, lake, etl)
    _assert_rejected(result, lake, "INTEGRITY")
    assert result.drive == clean_copy.name
    assert result.points_per_frame_in == 0
    lineage = lake.read("lineage", clean_copy.name).to_pylist()[0]
    assert lineage["scenario_hash"] is None
    assert lineage["schema_version"] is None
    assert lineage["frames_sha256"] is None


def test_schema_one_manifest_without_body_rejects(
    clean_copy: Path, lake: Lake, etl: EtlConfig
) -> None:
    def edit(data: dict[str, Any]) -> None:
        del data["body"]
        data["schema_version"] = 1

    _edit_manifest(clean_copy, edit)
    result = ingest_drive(clean_copy, lake, etl)
    _assert_rejected(result, lake, "INTEGRITY")
    assert result.drive == clean_copy.name
    assert result.points_per_frame_in == 0


def test_unsupported_schema_version_rejects(clean_copy: Path, lake: Lake, etl: EtlConfig) -> None:
    _edit_manifest(clean_copy, lambda data: data.update(schema_version=3))
    result = ingest_drive(clean_copy, lake, etl)
    _assert_rejected(result, lake, "INTEGRITY")
    assert "schema_version 3" in _quarantine(lake, result.drive)[0]["message"]
    lineage = lake.read("lineage", result.drive).to_pylist()[0]
    assert lineage["schema_version"] == 3
    assert lineage["scenario_hash"] == read_manifest(clean_copy).scenario_hash


def test_folder_name_mismatch_rejects(
    clean_copy: Path, lake: Lake, etl: EtlConfig, tmp_path: Path
) -> None:
    renamed = clean_copy.parent / "renamed_drive"
    clean_copy.rename(renamed)
    result = ingest_drive(renamed, lake, etl)
    _assert_rejected(result, lake, "INTEGRITY")
    assert result.drive == "renamed_drive"
    assert lake.list_drives("lineage") == ["renamed_drive"]


def test_invalid_folder_name_raises_and_writes_nothing(
    clean_copy: Path, lake: Lake, etl: EtlConfig
) -> None:
    bad = clean_copy.parent / "bad name"
    clean_copy.rename(bad)
    with pytest.raises(ValueError, match="drive"):
        ingest_drive(bad, lake, etl)
    assert not lake.root.exists() or _lake_bytes(lake.root) == {}


def test_fps_not_dividing_a_second_rejects(clean_copy: Path, lake: Lake, etl: EtlConfig) -> None:
    _edit_manifest(clean_copy, lambda data: data.update(fps=7))
    result = ingest_drive(clean_copy, lake, etl)
    _assert_rejected(result, lake, "INTEGRITY")
    assert "fps" in _quarantine(lake, result.drive)[0]["message"]


def test_invalid_scenario_or_body_in_manifest_rejects(
    clean_copy: Path, lake: Lake, etl: EtlConfig
) -> None:
    _edit_manifest(clean_copy, lambda data: data.update(scenario={}))
    result = ingest_drive(clean_copy, lake, etl)
    _assert_rejected(result, lake, "INTEGRITY")

    def one_point(data: dict[str, Any]) -> None:
        data["body"]["points_per_axis"] = 1
        data["points_per_frame"] = 1

    _edit_manifest(clean_copy, one_point)
    result = ingest_drive(clean_copy, lake, etl)
    _assert_rejected(result, lake, "INTEGRITY")


def test_frames_file_not_in_manifest_rejects(clean_copy: Path, lake: Lake, etl: EtlConfig) -> None:
    _edit_manifest(clean_copy, lambda data: data["files"].pop(FRAMES))
    result = ingest_drive(clean_copy, lake, etl)
    _assert_rejected(result, lake, "INTEGRITY")
    assert FRAMES in _quarantine(lake, result.drive)[0]["message"]


def test_frame_id_out_of_range_rejects(
    write10: Callable[..., Path], drive10: Drive, lake: Lake, etl: EtlConfig
) -> None:
    ids, ts, pts = columns(drive10)
    ids[10] = 5000
    ids[11] = -1
    drive_dir = write10(table=build_table(ids, ts, pts))
    result = ingest_drive(drive_dir, lake, etl)
    _assert_rejected(result, lake, "INTEGRITY")
    assert "5000" in _quarantine(lake, result.drive)[0]["message"]


def test_schema_mismatch_rejects_drive(
    write10: Callable[..., Path], lake: Lake, etl: EtlConfig
) -> None:
    drive_dir = write10([BreakSchema()])
    result = ingest_drive(drive_dir, lake, etl)
    _assert_rejected(result, lake, "VL-005")
    lineage = lake.read("lineage", result.drive).to_pylist()[0]
    manifest = read_manifest(drive_dir)
    assert lineage["frames_sha256"] == manifest.files[FRAMES].sha256  # hashes were verified


def test_unreadable_frames_file_with_matching_hash_rejects(
    clean_copy: Path, lake: Lake, etl: EtlConfig
) -> None:
    (clean_copy / FRAMES).write_bytes(b"this is not a parquet file")
    _refresh_entry(clean_copy, FRAMES)
    result = ingest_drive(clean_copy, lake, etl)
    _assert_rejected(result, lake, "INTEGRITY")


def test_null_frame_id_rejects(
    write10: Callable[..., Path], drive10: Drive, lake: Lake, etl: EtlConfig
) -> None:
    ids: list[int | None] = list(columns(drive10)[0])
    _, ts, pts = columns(drive10)
    ids[5] = None
    table = pa.Table.from_arrays(
        [pa.array(ids, pa.int64()), pa.array(ts), pa.array(pts, pa.binary())],
        schema=drive10.frames.schema,
    )
    result = ingest_drive(write10(table=table), lake, etl)
    _assert_rejected(result, lake, "INTEGRITY")
    assert "frame_id" in _quarantine(lake, result.drive)[0]["message"]


def test_row_group_above_cap_rejects(clean_copy: Path, drive10: Drive, lake: Lake) -> None:
    pq.write_table(drive10.frames, clean_copy / FRAMES, row_group_size=400)  # about 4.8 MB each
    _refresh_entry(clean_copy, FRAMES)
    result = ingest_drive(clean_copy, lake, EtlConfig(max_row_group_mib=2))
    _assert_rejected(result, lake, "INTEGRITY")
    assert "row group" in _quarantine(lake, result.drive)[0]["message"]
    ok = ingest_drive(clean_copy, lake, EtlConfig(max_row_group_mib=64))
    assert ok.status == "ok"
    assert ok.frames_kept == 1000


def test_rejected_after_ok_removes_keypoints(clean_copy: Path, lake: Lake, etl: EtlConfig) -> None:
    assert ingest_drive(clean_copy, lake, etl).status == "ok"
    assert lake.list_drives("keypoints") == [clean_copy.name]
    (clean_copy / FRAMES).write_bytes(b"changed")
    result = ingest_drive(clean_copy, lake, etl)
    _assert_rejected(result, lake, "INTEGRITY", rows=2)
    assert lake.read("lineage", clean_copy.name).num_rows == 1


def test_reingest_is_byte_identical(clean_copy: Path, lake: Lake, etl: EtlConfig) -> None:
    ingest_drive(clean_copy, lake, etl)
    first = _lake_bytes(lake.root)
    assert len(first) == 3
    ingest_drive(clean_copy, lake, etl)
    assert _lake_bytes(lake.root) == first


def test_reingest_after_fix_replaces_quarantine(
    write10: Callable[..., Path], clean_dir10: Path, lake: Lake, etl: EtlConfig
) -> None:
    bad = write10([Teleport(100, 1000)])
    assert bad.name == clean_dir10.name  # same scenario, same folder name
    assert ingest_drive(bad, lake, etl).frames_quarantined == 1
    assert len(_quarantine(lake, bad.name)) == 1
    result = ingest_drive(clean_dir10, lake, etl)
    assert result.frames_kept == 1000
    assert _quarantine(lake, bad.name) == []
    assert lake.read("keypoints", bad.name).num_rows == 9000


def test_lineage_row_content(clean_copy: Path, lake: Lake, etl: EtlConfig) -> None:
    ingest_drive(clean_copy, lake, etl)
    table = lake.read("lineage", clean_copy.name)
    assert table.schema.names == [*LINEAGE_SCHEMA.names, "drive"]
    row = table.to_pylist()[0]
    manifest = read_manifest(clean_copy)
    assert row == {
        "scenario_hash": manifest.scenario_hash,
        "schema_version": 2,
        "status": "ok",
        "frames_expected": 1000,
        "frames_in": 1000,
        "frames_kept": 1000,
        "frames_rejected": 0,
        "frames_missing": 0,
        "keypoint_rows": 9000,
        "frames_sha256": manifest.files[FRAMES].sha256,
        "drive": clean_copy.name,
    }


def test_drive_without_ground_truth_file_ingests(
    clean_copy: Path, lake: Lake, etl: EtlConfig
) -> None:
    name = "ground_truth.parquet"  # Module 1 may stop writing it; ingest never depends on it
    (clean_copy / name).unlink(missing_ok=True)
    _edit_manifest(clean_copy, lambda data: data["files"].pop(name, None))
    result = ingest_drive(clean_copy, lake, etl)
    assert result.status == "ok"
    assert result.frames_kept == 1000


def test_keypoint_values_match_source_points(clean_copy: Path, lake: Lake, etl: EtlConfig) -> None:
    ingest_drive(clean_copy, lake, etl)
    frames = pq.read_table(clean_copy / FRAMES).to_pandas()
    kp = lake.read("keypoints", clean_copy.name).to_pandas()
    centroid = kp[kp["keypoint"] == "centroid"].sort_values("frame_id")
    assert centroid["frame_id"].tolist() == list(range(1000))
    source_x = [decode_points(p)[:, 0].mean() for p in frames.sort_values("frame_id")["points"]]
    assert np.abs(centroid["x_cm"].to_numpy() - np.array(source_x)).max() < 1e-6
    low = kp[kp["keypoint"] == "corner_000"].sort_values("frame_id")
    high = kp[kp["keypoint"] == "corner_111"].sort_values("frame_id")
    assert (np.abs(high["x_cm"].to_numpy() - low["x_cm"].to_numpy() - 39) <= 1).all()  # 1 cm wobble
    stamps = kp.drop_duplicates("frame_id").sort_values("frame_id")["timestamp_ns"]
    assert stamps.tolist() == [i * 62_500_000 for i in range(1000)]


def test_keypoint_rows_sorted_by_frame_then_yaml_order(
    write10: Callable[..., Path], lake: Lake, etl: EtlConfig
) -> None:
    drive_dir = write10([DuplicateFrame(100), DropFrame(7)])
    ingest_drive(drive_dir, lake, etl)
    kp = lake.read("keypoints", drive_dir.name)
    ids = kp.column("frame_id").to_pylist()
    assert ids == sorted(ids)
    assert 7 not in ids
    names = [s.name for s in etl.keypoints]
    assert kp.column("keypoint").to_pylist()[:18] == names + names


def test_chunk_size_does_not_change_output(
    clean_copy: Path, tmp_path: Path, etl: EtlConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    outputs = []
    for frames in (1, 256):
        monkeypatch.setattr(EtlConfig, "chunk_frames", lambda self, ppf, n=frames: n)
        lake = Lake(tmp_path / f"lake{frames}")
        assert ingest_drive(clean_copy, lake, etl).frames_kept == 1000
        outputs.append(_lake_bytes(lake.root))
    assert outputs[0] == outputs[1]
    assert len(outputs[0]) == 3


def test_bad_keypoint_spec_is_a_config_error(clean_copy: Path, lake: Lake) -> None:
    bad = EtlConfig(keypoints=[KeypointSpec(name="far", kind="index", index=5000)])
    with pytest.raises(ValueError, match="far"):
        ingest_drive(clean_copy, lake, bad)
    assert not lake.root.exists() or _lake_bytes(lake.root) == {}


def test_schema_version_beyond_int32_is_stored_as_null(
    clean_copy: Path, lake: Lake, etl: EtlConfig
) -> None:
    _edit_manifest(clean_copy, lambda data: data.update(schema_version=2**40))
    result = ingest_drive(clean_copy, lake, etl)
    _assert_rejected(result, lake, "INTEGRITY")
    assert lake.read("lineage", result.drive).to_pylist()[0]["schema_version"] is None


def test_unverifiable_drive_rejects(
    clean_copy: Path, lake: Lake, etl: EtlConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(drive_dir: Path) -> None:
        raise OSError(5, "disk went away")

    monkeypatch.setattr("vl_data.etl.ingest.verify_drive", broken)
    result = ingest_drive(clean_copy, lake, etl)
    _assert_rejected(result, lake, "INTEGRITY")
    assert "disk went away" in _quarantine(lake, result.drive)[0]["message"]


# ---- repair pass: hostile drive folder (hand-made manifest or Parquet) ----


def _forbid(monkeypatch: pytest.MonkeyPatch, name: str) -> list[str]:
    """Make vl_data.etl.ingest.<name> fail the test if it is called; returns the call log."""
    calls: list[str] = []

    def guard(*args: Any, **kwargs: Any) -> None:
        calls.append(name)
        raise AssertionError(f"{name} must not run here")

    monkeypatch.setattr(f"vl_data.etl.ingest.{name}", guard)
    return calls


def _has_control(text: str) -> bool:
    return any(unicodedata.category(c).startswith("C") for c in text)


def test_points_per_axis_one_million_rejects_before_allocating(
    clean_copy: Path, lake: Lake, etl: EtlConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def huge(data: dict[str, Any]) -> None:
        data["body"]["points_per_axis"] = 1_000_000
        data["points_per_frame"] = 10**18

    _edit_manifest(clean_copy, huge)
    calls = [
        _forbid(monkeypatch, "cube_lattice"),
        _forbid(monkeypatch, "check_keypoint_specs"),
        _forbid(monkeypatch, "verify_drive"),
    ]
    result = ingest_drive(clean_copy, lake, etl)
    assert calls == [[], [], []]
    _assert_rejected(result, lake, "INTEGRITY")
    assert "max_points_per_frame" in _quarantine(lake, result.drive)[0]["message"]


def test_point_cap_is_a_boundary(clean_copy: Path, lake: Lake) -> None:
    assert ingest_drive(clean_copy, lake, EtlConfig(max_points_per_frame=1000)).status == "ok"
    result = ingest_drive(clean_copy, lake, EtlConfig(max_points_per_frame=999))
    _assert_rejected(result, lake, "INTEGRITY")


def test_lattice_is_built_after_verify_drive(
    clean_copy: Path, lake: Lake, etl: EtlConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    (clean_copy / FRAMES).write_bytes(b"changed")  # verify_drive fails
    calls = _forbid(monkeypatch, "cube_lattice")
    result = ingest_drive(clean_copy, lake, etl)
    _assert_rejected(result, lake, "INTEGRITY", rows=2)
    assert calls == []


def test_memory_error_building_the_lattice_rejects(
    clean_copy: Path, lake: Lake, etl: EtlConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: Any, **kwargs: Any) -> None:
        raise MemoryError

    monkeypatch.setattr("vl_data.etl.ingest.cube_lattice", boom)
    _assert_rejected(ingest_drive(clean_copy, lake, etl), lake, "INTEGRITY")


def test_memory_error_in_scan_rejects(
    clean_copy: Path, lake: Lake, etl: EtlConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: Any, **kwargs: Any) -> None:
        raise MemoryError

    monkeypatch.setattr("vl_data.etl.ingest.scan_frames", boom)
    _assert_rejected(ingest_drive(clean_copy, lake, etl), lake, "INTEGRITY")


def test_footer_with_too_many_rows_rejects(clean_copy: Path, lake: Lake, etl: EtlConfig) -> None:
    n = 3001  # cap is 2 * 1000 expected frames + 1000
    table = build_table(list(range(n)), [i * 62_500_000 for i in range(n)], [b""] * n)
    pq.write_table(table, clean_copy / FRAMES)
    _refresh_entry(clean_copy, FRAMES)
    result = ingest_drive(clean_copy, lake, etl)
    _assert_rejected(result, lake, "INTEGRITY")
    assert "3001 rows" in _quarantine(lake, result.drive)[0]["message"]


def test_footer_check_row_counts() -> None:
    def meta(total: int, groups: list[int]) -> Any:
        return SimpleNamespace(
            num_rows=total,
            num_row_groups=len(groups),
            row_group=lambda i: SimpleNamespace(num_rows=groups[i]),
        )

    _check_footer(meta(3000, [1500, 1500]), 1000)  # exactly at the cap
    with pytest.raises(_Reject, match="3001 rows"):
        _check_footer(meta(3001, [1500, 1501]), 1000)
    with pytest.raises(_Reject, match="row group 1 .* 4000"):
        _check_footer(meta(10, [5, 4000]), 1000)  # a footer that lies about its total
    with pytest.raises(_Reject, match="row groups"):
        _check_footer(meta(10, [0] * 3001), 1000)


def test_many_problems_are_capped_at_twenty_plus_a_summary(
    clean_copy: Path, lake: Lake, etl: EtlConfig
) -> None:
    for i in range(10_000):
        (clean_copy / f"x{i}.txt").write_bytes(b"")
    result = ingest_drive(clean_copy, lake, etl)
    q = _quarantine(lake, result.drive)
    assert len(q) == 21
    assert q[-1]["message"] == "and 9980 more problems"
    assert result.frames_quarantined == 21
    assert result.quarantine_by_code == {"INTEGRITY": 21}


def test_exactly_twenty_problems_have_no_summary(
    clean_copy: Path, lake: Lake, etl: EtlConfig
) -> None:
    for i in range(20):
        (clean_copy / f"x{i}.txt").write_bytes(b"")
    q = _quarantine(lake, ingest_drive(clean_copy, lake, etl).drive)
    assert len(q) == 20
    assert not any("more problems" in r["message"] for r in q)


def test_control_characters_in_a_listed_name_are_removed(
    clean_copy: Path, lake: Lake, etl: EtlConfig
) -> None:
    evil = "evil\x07\x1b[2J\x00name‮ x" + "y" * 1000
    entry = {"sha256": "0" * 64, "bytes": 1}
    _edit_manifest(clean_copy, lambda data: data["files"].update({evil: entry}))
    result = ingest_drive(clean_copy, lake, etl)
    assert result.status == "rejected"
    messages = [r["message"] for r in _quarantine(lake, result.drive)]
    assert messages
    assert all(not _has_control(m) and len(m) <= 300 for m in messages)
    assert any("evil" in m for m in messages)


def test_clean_text_helper() -> None:
    assert _clean("a\tb\n c\x00d\x1be f") == "a b cde f"
    assert len(_clean("z" * 1000)) == 300
    assert _clean("\ud800ok") == "ok"


def test_huge_scenario_hash_rejects_without_echo_and_stores_null(
    clean_copy: Path, lake: Lake, etl: EtlConfig
) -> None:
    _edit_manifest(clean_copy, lambda data: data.update(scenario_hash="a" * 1_000_000))
    result = ingest_drive(clean_copy, lake, etl)
    _assert_rejected(result, lake, "INTEGRITY")
    message = _quarantine(lake, result.drive)[0]["message"]
    assert "aaaa" not in message
    assert len(message) < 100
    assert lake.read("lineage", result.drive).to_pylist()[0]["scenario_hash"] is None


@pytest.mark.parametrize("bad", ["A" * 64, "g" * 64, "a" * 63, "a" * 65, "a" * 64 + "\n", ""])
def test_scenario_hash_must_be_64_lowercase_hex(
    clean_copy: Path, lake: Lake, etl: EtlConfig, bad: str
) -> None:
    _edit_manifest(clean_copy, lambda data: data.update(scenario_hash=bad))
    result = ingest_drive(clean_copy, lake, etl)
    _assert_rejected(result, lake, "INTEGRITY")
    assert lake.read("lineage", result.drive).to_pylist()[0]["scenario_hash"] is None


def test_drive_id_comes_from_the_path_given_even_for_a_relative_path(
    clean_copy: Path, lake: Lake, etl: EtlConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(clean_copy.parent)
    result = ingest_drive(Path(clean_copy.name), lake, etl)
    assert result.status == "ok"
    assert result.drive == clean_copy.name


def _make_dir_link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        pass
    if sys.platform == "win32":
        done = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, check=False
        )
        if done.returncode == 0:
            return
    pytest.skip("cannot create a directory symlink or junction without privileges here")


def test_drive_id_is_the_link_name_not_the_target_name(
    clean_copy: Path, lake: Lake, etl: EtlConfig
) -> None:
    link = clean_copy.parent / "linked_drive"
    _make_dir_link(link, clean_copy)
    result = ingest_drive(link, lake, etl)
    assert result.drive == "linked_drive"
    _assert_rejected(result, lake, "INTEGRITY")  # the name is not the hash prefix
    assert lake.list_drives("lineage") == ["linked_drive"]


def test_differing_duplicate_of_a_frame_quarantines_both_copies(
    write10: Callable[..., Path], drive10: Drive, lake: Lake, etl: EtlConfig
) -> None:
    ids, ts, pts = columns(drive10)
    ids.append(100)
    ts.append(ts[100])
    pts.append(pts[101])  # same frame number and time, different cloud
    result = ingest_drive(write10(table=build_table(ids, ts, pts)), lake, etl)
    assert result.status == "ok"
    assert result.frames_in == 1001
    assert result.frames_kept == 999
    assert result.quarantine_by_code.get("VL-003") == 2
    q = [r for r in _quarantine(lake, result.drive) if r["code"] == "VL-003"]
    assert sorted(r["row_index"] for r in q) == [100, 1000]
    kp_ids = lake.read("keypoints", result.drive).column("frame_id").to_pylist()
    assert 100 not in kp_ids
    assert len(set(kp_ids)) == 999


def test_shuffled_rows_with_identical_duplicate_keep_generator_keypoints(
    write10: Callable[..., Path], clean_dir10: Path, drive10: Drive, tmp_path: Path, etl: EtlConfig
) -> None:
    reference = Lake(tmp_path / "reference")
    ingest_drive(clean_dir10, reference, etl)
    ids, ts, pts = columns(drive10)
    ids.append(ids[100])
    ts.append(ts[100])
    pts.append(pts[100])  # identical repeat
    order = np.random.default_rng(7).permutation(len(ids)).tolist()
    table = build_table([ids[i] for i in order], [ts[i] for i in order], [pts[i] for i in order])
    lake = Lake(tmp_path / "shuffled")
    result = ingest_drive(write10(table=table), lake, etl)
    assert result.frames_kept == 1000
    assert result.quarantine_by_code == {"VL-003": 1}
    got = lake.read("keypoints", result.drive).to_pandas()
    want = reference.read("keypoints", result.drive).to_pandas()
    assert len(got) == 9000
    pd.testing.assert_frame_equal(got.reset_index(drop=True), want.reset_index(drop=True))


# ---- second repair pass ----


@pytest.mark.parametrize("error", [ValueError("bad"), RecursionError("deep"), MemoryError()])
def test_verify_drive_errors_reject_the_drive(
    clean_copy: Path,
    lake: Lake,
    etl: EtlConfig,
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
) -> None:
    def broken(drive_dir: Path) -> None:
        raise error

    monkeypatch.setattr("vl_data.etl.ingest.verify_drive", broken)
    result = ingest_drive(clean_copy, lake, etl)
    _assert_rejected(result, lake, "INTEGRITY")
    assert "cannot verify the drive" in _quarantine(lake, result.drive)[0]["message"]


@pytest.mark.parametrize("stage", ["drive", "frame", "keypoints"])
def test_memory_error_after_the_scan_rejects_and_drops_keypoints(
    clean_copy: Path, lake: Lake, etl: EtlConfig, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    assert ingest_drive(clean_copy, lake, etl).status == "ok"
    real_rules = ingest_module.run_rules

    def rules(ctx: Any, scope: str) -> Any:
        if scope == stage:
            raise MemoryError
        return real_rules(ctx, scope)

    def boom(*args: Any, **kwargs: Any) -> None:
        raise MemoryError

    monkeypatch.setattr("vl_data.etl.ingest.run_rules", rules)
    if stage == "keypoints":
        monkeypatch.setattr("vl_data.etl.ingest._keypoint_batches", boom)
    result = ingest_drive(clean_copy, lake, etl)
    _assert_rejected(result, lake, "INTEGRITY")
    assert lake.read("lineage", result.drive).to_pylist()[0]["status"] == "rejected"


@pytest.mark.parametrize("field", ["schema_version", "fps"])
def test_huge_integer_in_the_manifest_rejects_without_crashing(
    clean_copy: Path, lake: Lake, etl: EtlConfig, field: str
) -> None:
    path = clean_copy / "manifest.json"
    text = path.read_text(encoding="utf-8")
    new = re.sub(rf'"{field}"\s*:\s*\d+', f'"{field}": 1' + "0" * 5000, text)
    assert new != text
    path.write_text(new, encoding="utf-8")
    result = ingest_drive(clean_copy, lake, etl)
    _assert_rejected(result, lake, "INTEGRITY")
    assert len(_quarantine(lake, result.drive)[0]["message"]) <= 300


def test_int_text_never_raises_for_a_huge_integer() -> None:
    assert _int_text(7) == "7"
    assert _int_text(-12) == "-12"
    assert _int_text(10**5000) == "<huge integer>"


@pytest.mark.parametrize("field", ["schema_version", "fps"])
def test_huge_integer_that_parses_gives_a_safe_message(
    clean_copy: Path, lake: Lake, etl: EtlConfig, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    real = ingest_module.read_manifest

    def huge(drive_dir: Path) -> Any:
        return real(drive_dir).model_copy(update={field: 10**5000})

    monkeypatch.setattr("vl_data.etl.ingest.read_manifest", huge)
    result = ingest_drive(clean_copy, lake, etl)
    _assert_rejected(result, lake, "INTEGRITY")
    assert field in _quarantine(lake, result.drive)[0]["message"]
    if field == "schema_version":
        assert lake.read("lineage", result.drive).to_pylist()[0]["schema_version"] is None


@pytest.mark.parametrize("where", ["verify", "open", "scan"])
def test_stored_rows_contain_no_local_path(
    tmp_path: Path,
    clean_copy: Path,
    lake: Lake,
    etl: EtlConfig,
    monkeypatch: pytest.MonkeyPatch,
    where: str,
) -> None:
    frames = clean_copy / FRAMES
    texts = [str(frames), frames.as_posix(), str(clean_copy)]

    def verify(drive_dir: Path) -> None:
        raise OSError(2, "gone", str(frames))

    real_open = pq.ParquetFile

    def opener(source: Any, *args: Any, **kwargs: Any) -> Any:
        if Path(source) == frames:  # the lake's own files still open
            raise pa.ArrowInvalid("Could not open " + " and ".join(texts))
        return real_open(source, *args, **kwargs)

    def scan(*args: Any, **kwargs: Any) -> None:
        raise ValueError("bad " + " and ".join(texts))

    if where == "verify":
        monkeypatch.setattr("vl_data.etl.ingest.verify_drive", verify)
    elif where == "open":
        monkeypatch.setattr("vl_data.etl.ingest.pq.ParquetFile", opener)
    else:
        monkeypatch.setattr("vl_data.etl.ingest.scan_frames", scan)
    result = ingest_drive(clean_copy, lake, etl)
    _assert_rejected(result, lake, "INTEGRITY")
    stored = json.dumps(_quarantine(lake, result.drive))
    # the name alone also catches a path that was repr-escaped (doubled backslashes)
    for forbidden in (str(tmp_path), tmp_path.as_posix(), tmp_path.name):
        assert forbidden not in stored
    assert "gone" in stored or "<drive>" in stored
