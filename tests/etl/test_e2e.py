"""End to end: a drive written to disk, ingested through the CLI, queried with SQL. Small body."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from vl_data.cli import app
from vl_data.core.lake import Lake
from vl_data.core.manifest import read_manifest
from vl_data.etl.rules import RULES
from vl_data.generator import faults as faults_module
from vl_data.generator.faults import (
    BreakSchema,
    CorruptPoints,
    DropFrame,
    DuplicateFrame,
    Fault,
    ShiftTimestamp,
    Teleport,
)

from .helpers import Written

runner = CliRunner()

CASES: list[tuple[str, Fault, int]] = [
    ("VL-001", DropFrame(5), 0),
    ("VL-002", ShiftTimestamp(5, 1000), 0),
    ("VL-003", DuplicateFrame(5), 0),
    ("VL-004", CorruptPoints(5), 0),
    ("VL-005", BreakSchema(), 1),
    ("VL-006", Teleport(5, 1000), 0),
]


@pytest.fixture(autouse=True)
def _no_env_lake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VL_LAKE_DIR", raising=False)


def _query(lake: Lake, sql: str) -> list[tuple[Any, ...]]:
    con = lake.connect()
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def test_generate_ingest_query(tmp_path: Path, write_faulty: Callable[..., Written]) -> None:
    written = write_faulty([])
    lake_dir = tmp_path / "lake"
    result = runner.invoke(app, ["ingest", str(written.drive_dir), "--lake-dir", str(lake_dir)])
    assert result.exit_code == 0, result.output
    lake = Lake(lake_dir)
    per_frame = _query(lake, "SELECT count(*) AS n FROM keypoints GROUP BY frame_id")
    assert len(per_frame) == 1000
    assert {n for (n,) in per_frame} == {9}
    names = _query(lake, "SELECT DISTINCT keypoint FROM keypoints ORDER BY 1")
    assert len(names) == 9
    manifest = read_manifest(written.drive_dir)
    lineage = _query(
        lake,
        "SELECT drive, scenario_hash, status, frames_kept, keypoint_rows, frames_sha256, "
        "ground_truth_sha256 FROM lineage",
    )
    assert lineage == [
        (
            written.drive_dir.name,
            manifest.scenario_hash,
            "ok",
            1000,
            9000,
            manifest.files["frames.parquet"].sha256,
            manifest.files["ground_truth.parquet"].sha256,
        )
    ]
    assert _query(lake, "SELECT count(*) FROM quarantine") == [(0,)]


def test_example_faults_are_real(tmp_path: Path, write_faulty: Callable[..., Written]) -> None:
    """Each registry example fault exists in faults.py and, injected, reports exactly its code."""
    by_name = {type(fault).__name__: fault for _, fault, _ in CASES}
    assert set(by_name) == {rule.example_fault for rule in RULES}
    for rule in RULES:
        assert hasattr(faults_module, rule.example_fault)
        written = write_faulty([by_name[rule.example_fault]])
        lake_dir = tmp_path / f"lake-{rule.code}"
        result = runner.invoke(app, ["ingest", str(written.drive_dir), "--lake-dir", str(lake_dir)])
        assert result.exit_code in (0, 1), result.output
        codes = _query(Lake(lake_dir), "SELECT DISTINCT code FROM quarantine")
        assert codes == [(rule.code,)]


@pytest.mark.parametrize(("code", "fault", "exit_code"), CASES, ids=[c[0] for c in CASES])
def test_faulty_drives_end_to_end(
    tmp_path: Path,
    write_faulty: Callable[..., Written],
    code: str,
    fault: Fault,
    exit_code: int,
) -> None:
    written = write_faulty([fault])
    lake_dir = tmp_path / "lake"
    result = runner.invoke(app, ["ingest", str(written.drive_dir), "--lake-dir", str(lake_dir)])
    assert result.exit_code == exit_code, result.output
    assert f"quarantine_{code}: " in result.stdout
    codes = _query(Lake(lake_dir), "SELECT DISTINCT code FROM quarantine")
    assert codes == [(code,)]
    status = _query(Lake(lake_dir), "SELECT status FROM lineage")
    assert status == [("rejected" if exit_code else "ok",)]
