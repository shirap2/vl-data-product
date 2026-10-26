"""Data-layer tests for the BI app. Units: cm, cm/s, cm/s^2, frames, s."""

from collections import Counter
from pathlib import Path, PureWindowsPath

import pyarrow as pa
import pytest

from vl_data.bi import data
from vl_data.bi.data import (
    SQL_ROWS_SHOWN,
    MotionHint,
    QueryOutcome,
    _ps_quote,
    example_queries,
    ingest_command,
    list_lake_drives,
    load_quality,
    motion_command,
    motion_hint,
    run_query,
)
from vl_data.core.lake import Lake
from vl_data.sdk.client import VLClient

from .helpers import DROPPED_FRAME, BiDrive

UNKNOWN_DRIVE = "0" * 12  # a valid id with no rows in any lake here


def _snapshot(root: Path) -> dict[str, int]:
    """Relative path to size in bytes of every file under root."""
    return {str(p.relative_to(root)): p.stat().st_size for p in root.rglob("*") if p.is_file()}


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
    """Client with a canned lineage (a table or an exception to raise)."""

    def __init__(self, lake: Lake, lineage: pa.Table | Exception) -> None:
        super().__init__(lake)
        self._lineage = lineage

    def lineage(self, drive: str) -> pa.Table:
        if isinstance(self._lineage, Exception):
            raise self._lineage
        return self._lineage


def test_invalid_drive_id_rejected(clean: BiDrive, tmp_path: Path) -> None:
    for bad in ("../x", "a/b", "", "a b", "x\\y", "x'; DROP"):
        with pytest.raises(ValueError, match="drive id"):
            load_quality(clean.client, bad)
        with pytest.raises(ValueError, match="drive id"):
            ingest_command(tmp_path, bad, tmp_path)
        with pytest.raises(ValueError, match="drive id"):
            motion_command(bad, tmp_path)
        hint = motion_hint(clean.client, tmp_path, tmp_path, bad)
        assert isinstance(hint, MotionHint)
        assert "invalid drive id" in hint.problem
        assert hint.command is None


def _hint(d: BiDrive, drive: str | None = None, client: VLClient | None = None) -> MotionHint:
    return motion_hint(client or d.client, d.drive_dir.parent, d.root / "lake", drive or d.drive_id)


def test_hint_missing_motion_of_ok_drive_gives_motion_command(unmotioned: BiDrive) -> None:
    assert unmotioned.ingest.status == "ok" and not unmotioned.has_motion
    hint = _hint(unmotioned)
    assert unmotioned.drive_id in hint.problem
    assert "no motion table" in hint.problem
    assert hint.command == motion_command(unmotioned.drive_id, unmotioned.root / "lake")


def test_hint_rejected_drive_gives_ingest_command_not_motion(rejected: BiDrive) -> None:
    assert not rejected.has_motion
    hint = _hint(rejected)
    assert "rejected" in hint.problem
    expected = ingest_command(rejected.drive_dir.parent, rejected.drive_id, rejected.root / "lake")
    assert hint.command == expected
    assert " motion " not in expected


def test_hint_unknown_drive_gives_ingest_command(clean: BiDrive) -> None:
    hint = _hint(clean, UNKNOWN_DRIVE)
    assert "not ingested" in hint.problem
    expected = ingest_command(clean.drive_dir.parent, UNKNOWN_DRIVE, clean.root / "lake")
    assert hint.command == expected
    assert " motion " not in expected


@pytest.mark.parametrize("error", [pa.ArrowInvalid("C:/secret/path"), ValueError("C:/secret/path")])
def test_hint_unreadable_lineage_is_path_free(clean: BiDrive, error: Exception) -> None:
    hint = _hint(clean, client=_LineageStub(clean.lake, error))
    assert hint.problem == f"cannot read the lineage: {type(error).__name__}"
    assert hint.command is None
    assert "secret" not in hint.problem


def test_hint_lineage_without_status_column_is_path_free_and_does_not_raise(clean: BiDrive) -> None:
    stub = _LineageStub(clean.lake, pa.table({"drive_id": [clean.drive_id]}))
    hint = _hint(clean, client=stub)
    assert "cannot read the lineage" in hint.problem
    assert "KeyError" in hint.problem
    assert hint.command is None


def test_hint_two_lineage_rows_is_unexpected_not_rejected(clean: BiDrive) -> None:
    stub = _LineageStub(clean.lake, pa.table({"status": ["ok", "ok"]}))
    hint = _hint(clean, client=stub)
    assert hint.command is None
    assert "unexpected lineage rows (2)" in hint.problem
    assert "rejected" not in hint.problem
    assert clean.drive_id in hint.problem
    assert str(clean.root) not in hint.problem


def test_hint_never_writes_lake(clean: BiDrive, rejected: BiDrive, unmotioned: BiDrive) -> None:
    drives = (clean, rejected, unmotioned)
    before = {d.root: _snapshot(d.root / "lake") for d in drives}
    for d in drives:
        _hint(d)
    _hint(clean, UNKNOWN_DRIVE)
    assert {d.root: _snapshot(d.root / "lake") for d in drives} == before


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


def test_read_problem_hides_paths_of_file_errors() -> None:
    secret = r"C:\secret\x.parquet"
    assert data.read_problem(ValueError("invalid drive id")) == "invalid drive id"
    for error in (OSError(secret), pa.ArrowInvalid(f"Failed to open local file '{secret}'")):
        text = data.read_problem(error)
        assert text == type(error).__name__
        assert "secret" not in text


# --- SQL tab ---------------------------------------------------------------------------------


class RaisingSqlClient:
    """Client double whose sql() raises a chosen error."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def sql(self, query: str) -> pa.Table:
        raise self._error


def test_run_query_select_and_cap(dropped: BiDrive) -> None:
    d = dropped
    ids = sorted(d.client.keypoints(d.drive_id, "centroid").column("frame_id").to_pylist())
    outcome = run_query(
        d.client,
        f"SELECT frame_id FROM keypoints WHERE drive = '{d.drive_id}' AND keypoint = 'centroid' "
        "ORDER BY frame_id",
    )
    assert outcome.problem is None
    assert outcome.table is not None
    assert outcome.total_rows == len(ids) == 287  # 288 frames, frame 5 dropped
    assert outcome.table.column("frame_id").to_pylist() == ids
    assert SQL_ROWS_SHOWN == 10_000

    capped = run_query(d.client, "SELECT * FROM generate_series(1, 12345) AS t(n)")
    assert capped.problem is None
    assert capped.table is not None
    assert capped.table.num_rows == 10_000
    assert capped.total_rows == 12_345
    assert capped.table.column("n").to_pylist() == list(range(1, 10_001))

    empty = run_query(d.client, "SELECT frame_id FROM quarantine WHERE code = 'NO-SUCH-CODE'")
    assert empty == QueryOutcome(empty.table, 0, None)
    assert empty.table is not None
    assert empty.table.num_rows == 0


def test_run_query_refuses_non_select_and_bad_sql(dropped: BiDrive, tmp_path: Path) -> None:
    d = dropped
    before = _snapshot(d.root / "lake")
    missing = tmp_path / "secret_folder_xyz" / "missing.parquet"
    queries = {
        "drop": "DROP VIEW keypoints",
        "copy": f"COPY (SELECT 1) TO '{(tmp_path / 'out.csv').as_posix()}'",
        "two": "SELECT 1; SELECT 2",
        "invalid": "SELEC nonsense (",
        "binder": "SELECT no_such_column FROM lineage",
        "io": f"SELECT * FROM read_parquet('{missing.as_posix()}')",
        "catalog_path": 'SELECT * FROM "a/b"',
    }
    problems = {}
    for name, query in queries.items():
        outcome = run_query(d.client, query)
        assert outcome.table is None, name
        assert outcome.total_rows == 0, name
        assert outcome.problem, name
        problems[name] = outcome.problem
    assert "only a SELECT statement is allowed" in problems["drop"]
    assert "only a SELECT statement is allowed" in problems["copy"]
    assert "expected exactly one SELECT statement, got 2" in problems["two"]
    assert "cannot parse the query" in problems["invalid"]
    assert "no_such_column" in problems["binder"]
    assert problems["catalog_path"] == "CatalogException"  # first line holds a separator
    assert "IOException" in problems["io"]
    for text in problems.values():
        for part in ("secret_folder_xyz", "missing.parquet", str(tmp_path), "\\"):
            assert part not in text
    assert not (tmp_path / "out.csv").exists()
    assert _snapshot(d.root / "lake") == before


def test_run_query_reader_errors_give_type_name_only() -> None:
    cases = {
        pa.ArrowInvalid(r"bad C:\secret\file.parquet"): "ArrowInvalid",
        FileNotFoundError(r"C:\secret\file.parquet"): "FileNotFoundError",
        pa.ArrowIOError(r"C:\secret\file.parquet"): "OSError",
    }
    for error, name in cases.items():
        outcome = run_query(RaisingSqlClient(error), "SELECT 1")  # type: ignore[arg-type]
        assert outcome == QueryOutcome(None, 0, name)


def test_example_queries_run_and_check_drive_id(dropped: BiDrive) -> None:
    d = dropped
    examples = example_queries(d.drive_id)
    assert list(examples) == [
        "Quarantine rows per code",
        "Peak speed (cm/s)",
        "Kept vs rejected frames",
        "First 20 centroid positions (cm)",
        "All drives with status",
    ]
    results = {}
    for label, query in examples.items():
        outcome = run_query(d.client, query)
        assert outcome.problem is None, label
        assert outcome.table is not None, label
        results[label] = outcome.table
    quarantine = results["Quarantine rows per code"].to_pylist()
    assert quarantine == [{"code": "VL-001", "rows_quarantined": 1}]
    expected_peak = max(
        abs(v)
        for v in d.client.motion(d.drive_id).column("velocity_cm_s").to_pylist()
        if v is not None
    )
    peak = results["Peak speed (cm/s)"].to_pylist()
    assert len(peak) == 1
    assert peak[0]["peak_speed_cm_s"] == pytest.approx(expected_peak)
    lineage = d.client.lineage(d.drive_id).to_pylist()[0]
    kept = results["Kept vs rejected frames"].to_pylist()
    assert kept == [
        {
            k: lineage[k]
            for k in (
                "frames_expected",
                "frames_in",
                "frames_kept",
                "frames_rejected",
                "frames_missing",
            )
        }
    ]
    first = results["First 20 centroid positions (cm)"]
    assert first.column_names == ["frame_id", "x_cm", "y_cm", "z_cm"]
    centroid = d.client.keypoints(d.drive_id, "centroid")
    ids = sorted(centroid.column("frame_id").to_pylist())[:20]
    assert first.column("frame_id").to_pylist() == ids
    assert DROPPED_FRAME not in ids
    assert len(ids) == 20
    everything = results["All drives with status"].to_pylist()
    assert [r["drive"] for r in everything] == [d.drive_id]
    assert everything[0]["status"] == lineage["status"]
    assert everything[0]["frames_missing"] == 1
    assert d.drive_id in examples["Quarantine rows per code"]
    assert f"'{d.drive_id}'" in examples["Peak speed (cm/s)"]


@pytest.mark.parametrize("bad", ["", "a'b", "x y", "a;b", r"..\x", "a/b"])
def test_example_queries_reject_bad_drive_id(bad: str) -> None:
    with pytest.raises(ValueError, match="invalid drive id"):
        example_queries(bad)


@pytest.mark.parametrize(
    ("count", "total"), [(0, 0), (9_999, 9_999), (10_000, 10_000), (10_001, 10_001)]
)
def test_run_query_cap_boundary(dropped: BiDrive, count: int, total: int) -> None:
    outcome = run_query(dropped.client, f"SELECT * FROM range({count}) AS t(n)")
    assert outcome.problem is None
    assert outcome.table is not None
    assert outcome.total_rows == total
    assert outcome.table.num_rows == min(count, 10_000)
    assert outcome.table.column("n").to_pylist() == list(range(min(count, 10_000)))


def test_run_query_makes_column_names_unique(dropped: BiDrive) -> None:
    outcome = run_query(dropped.client, "SELECT 1 AS a, 2 AS a, 3 AS a, 4 AS b, 5 AS a_1")
    assert outcome.table is not None
    assert outcome.table.column_names == ["a", "a_2", "a_3", "b", "a_1"]
    assert [outcome.table.column(i).to_pylist() for i in range(5)] == [[1], [2], [3], [4], [5]]
    assert outcome.total_rows == 1
