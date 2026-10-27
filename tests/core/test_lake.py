"""Tests for the Parquet lake and its table schemas (all writes go to tmp_path)."""

import hashlib
import os
import shutil
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from vl_data.core.lake import Lake
from vl_data.core.lake_schemas import (
    KEYPOINTS_SCHEMA,
    LINEAGE_SCHEMA,
    QUARANTINE_SCHEMA,
    TABLE_SCHEMAS,
)

_DUCK_TYPES = {
    pa.int64(): "BIGINT",
    pa.int32(): "INTEGER",
    pa.string(): "VARCHAR",
    pa.float64(): "DOUBLE",
}
DRIVE = "123456789012"  # numeric-looking on purpose: DuckDB would infer BIGINT


def kp_batch(frame_ids: list[int], keypoint: str = "nose") -> pa.RecordBatch:
    """Build a keypoints batch; coordinates in cm, timestamps in ns."""
    n = len(frame_ids)
    return pa.record_batch(
        [
            pa.array(frame_ids, pa.int64()),
            pa.array([i * 1000 for i in frame_ids], pa.int64()),
            pa.array([keypoint] * n, pa.string()),
            pa.array([1.0] * n, pa.float64()),
            pa.array([2.0] * n, pa.float64()),
            pa.array([3.0] * n, pa.float64()),
        ],
        schema=KEYPOINTS_SCHEMA,
    )


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def part_file(root: Path, table: str, drive: str) -> Path:
    return root / table / f"drive={drive}" / "part-0.parquet"


def assert_no_swap_leftovers(root: Path) -> None:
    swap = root / "_swap"
    assert not swap.exists() or list(swap.iterdir()) == []


def test_table_schemas_match_plan() -> None:
    assert list(TABLE_SCHEMAS) == ["keypoints", "quarantine", "lineage"]
    assert TABLE_SCHEMAS["keypoints"] is KEYPOINTS_SCHEMA
    expected = {
        "keypoints": [
            ("frame_id", pa.int64(), False),
            ("timestamp_ns", pa.int64(), False),
            ("keypoint", pa.string(), False),
            ("x_cm", pa.float64(), False),
            ("y_cm", pa.float64(), False),
            ("z_cm", pa.float64(), False),
        ],
        "quarantine": [
            ("frame_id", pa.int64(), True),
            ("row_index", pa.int64(), True),
            ("code", pa.string(), False),
            ("message", pa.string(), False),
        ],
        "lineage": [
            ("scenario_hash", pa.string(), True),
            ("schema_version", pa.int32(), True),
            ("status", pa.string(), False),
            ("frames_expected", pa.int64(), False),
            ("frames_in", pa.int64(), False),
            ("frames_kept", pa.int64(), False),
            ("frames_rejected", pa.int64(), False),
            ("frames_missing", pa.int64(), False),
            ("keypoint_rows", pa.int64(), False),
            ("frames_sha256", pa.string(), True),
        ],
    }
    for name, fields in expected.items():
        schema = TABLE_SCHEMAS[name]
        assert [(f.name, f.type, f.nullable) for f in schema] == fields
    assert QUARANTINE_SCHEMA is TABLE_SCHEMAS["quarantine"]
    assert LINEAGE_SCHEMA is TABLE_SCHEMAS["lineage"]


def test_swap_creates_hive_partition(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    out = lake.swap_partition("keypoints", DRIVE, [kp_batch([0, 1])])
    assert out == tmp_path / "keypoints" / f"drive={DRIVE}"
    f = part_file(tmp_path, "keypoints", DRIVE)
    assert f.is_file()
    assert pq.read_schema(f).remove_metadata().equals(KEYPOINTS_SCHEMA)
    assert pq.read_table(f).num_rows == 2


def test_swap_replaces_and_is_idempotent(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    lake.swap_partition("keypoints", DRIVE, [kp_batch([0, 1, 2])])
    f = part_file(tmp_path, "keypoints", DRIVE)
    before = sha(f)
    lake.swap_partition("keypoints", DRIVE, [kp_batch([0, 1, 2])])
    assert sha(f) == before
    assert [p.name for p in (tmp_path / "keypoints").iterdir()] == [f"drive={DRIVE}"]
    lake.swap_partition("keypoints", DRIVE, [kp_batch([5])])
    assert lake.read("keypoints", DRIVE).num_rows == 1
    assert_no_swap_leftovers(tmp_path)


def test_failed_write_keeps_old_partition(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    lake.swap_partition("keypoints", DRIVE, [kp_batch([0, 1])])
    before = sha(part_file(tmp_path, "keypoints", DRIVE))

    def boom() -> Iterator[pa.RecordBatch]:
        yield kp_batch([9])
        raise RuntimeError("disk on fire")

    with pytest.raises(RuntimeError, match="disk on fire"):
        lake.swap_partition("keypoints", DRIVE, boom())
    assert sha(part_file(tmp_path, "keypoints", DRIVE)) == before
    assert lake.read("keypoints", DRIVE).num_rows == 2
    assert_no_swap_leftovers(tmp_path)


def test_failed_write_on_new_drive_leaves_nothing(tmp_path: Path) -> None:
    lake = Lake(tmp_path)

    def boom() -> Iterator[pa.RecordBatch]:
        raise RuntimeError("x")
        yield  # pragma: no cover

    with pytest.raises(RuntimeError):
        lake.swap_partition("keypoints", DRIVE, boom())
    assert lake.list_drives("keypoints") == []
    assert_no_swap_leftovers(tmp_path)


def test_failure_after_old_moved_aside_restores_old(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lake = Lake(tmp_path)
    lake.swap_partition("keypoints", DRIVE, [kp_batch([0, 1])])
    before = sha(part_file(tmp_path, "keypoints", DRIVE))
    real_rename = Path.rename
    calls: list[Path] = []

    def flaky(self: Path, target: str | Path) -> Path:
        calls.append(self)
        if len(calls) == 2:  # call 1 moves the old folder aside, call 2 moves the new one in
            raise OSError("rename failed")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", flaky)
    with pytest.raises(OSError, match="rename failed"):
        lake.swap_partition("keypoints", DRIVE, [kp_batch([7])])
    monkeypatch.undo()
    assert sha(part_file(tmp_path, "keypoints", DRIVE)) == before
    assert_no_swap_leftovers(tmp_path)


def test_swap_after_connection_closed(tmp_path: Path) -> None:
    """A swap after the DuckDB connection is closed succeeds and the new data is read."""
    lake = Lake(tmp_path)
    lake.swap_partition("keypoints", DRIVE, [kp_batch([0, 1])])
    con = lake.connect()
    assert con.execute("SELECT count(*) FROM keypoints").fetchone() == (2,)
    con.close()
    lake.swap_partition("keypoints", DRIVE, [kp_batch([0, 1, 2])])
    assert lake.read("keypoints", DRIVE).num_rows == 3


def test_wrong_schema_rejected(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    lake.swap_partition("keypoints", DRIVE, [kp_batch([0, 1])])
    before = sha(part_file(tmp_path, "keypoints", DRIVE))
    bad = pa.record_batch([pa.array([1], pa.int64())], names=["frame_id"])
    with pytest.raises(ValueError):
        lake.swap_partition("keypoints", DRIVE, [kp_batch([7]), bad])
    assert sha(part_file(tmp_path, "keypoints", DRIVE)) == before
    with pytest.raises(ValueError):
        lake.swap_partition("keypoints", "other", [bad])
    assert lake.list_drives("keypoints") == [DRIVE]
    assert_no_swap_leftovers(tmp_path)


def test_list_and_read(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    assert lake.list_drives("keypoints") == []
    lake.swap_partition("keypoints", "b", [kp_batch([0, 1])])
    lake.swap_partition("keypoints", "a", [kp_batch([0])])
    assert lake.list_drives("keypoints") == ["a", "b"]
    everything = lake.read("keypoints")
    assert everything.schema.names == [*KEYPOINTS_SCHEMA.names, "drive"]
    assert everything.schema.field("drive").type == pa.string()
    assert everything.num_rows == 3
    assert sorted(set(everything.column("drive").to_pylist())) == ["a", "b"]
    only_b = lake.read("keypoints", "b")
    assert only_b.num_rows == 2
    assert set(only_b.column("drive").to_pylist()) == {"b"}


def test_read_empty_and_missing(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    expected = KEYPOINTS_SCHEMA.append(pa.field("drive", pa.string()))
    for t in (lake.read("keypoints"), lake.read("keypoints", "nope")):
        assert t.num_rows == 0
        assert t.schema.equals(expected)
    lake.swap_partition("keypoints", "a", [kp_batch([0])])
    assert lake.read("keypoints", "nope").num_rows == 0


def test_list_drives_ignores_foreign_folders(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    lake.swap_partition("keypoints", "a", [kp_batch([0])])
    (tmp_path / "keypoints" / "drive=empty").mkdir()
    (tmp_path / "keypoints" / "other").mkdir()
    (tmp_path / "keypoints" / "stray.txt").write_text("x")
    assert lake.list_drives("keypoints") == ["a"]


def test_duckdb_views_query(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    lake.swap_partition("keypoints", DRIVE, [kp_batch([0, 1], "nose"), kp_batch([2], "tail")])
    lake.swap_partition("keypoints", "abc", [kp_batch([0], "nose")])
    con = lake.connect()
    rows = con.execute(
        "SELECT keypoint, count(*) FROM keypoints GROUP BY keypoint ORDER BY keypoint"
    ).fetchall()
    assert rows == [("nose", 3), ("tail", 1)]
    drives = con.execute("SELECT DISTINCT drive FROM keypoints ORDER BY drive").fetchall()
    assert drives == [(DRIVE,), ("abc",)]
    assert con.execute("SELECT typeof(drive) FROM keypoints LIMIT 1").fetchone() == ("VARCHAR",)
    con.close()


def test_duckdb_numeric_only_drives_stay_strings(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    lake.swap_partition("keypoints", DRIVE, [kp_batch([0])])
    con = lake.connect()
    assert con.execute("SELECT drive FROM keypoints").fetchone() == (DRIVE,)
    con.close()


def test_empty_table_view(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    con = lake.connect()
    for name, schema in TABLE_SCHEMAS.items():
        cur = con.execute(f"SELECT * FROM {name}")
        assert [d[0] for d in cur.description] == [*schema.names, "drive"]
        assert cur.fetchall() == []
        expected = [_DUCK_TYPES[f.type] for f in schema] + ["VARCHAR"]
        actual = con.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            f"WHERE table_name = '{name}' ORDER BY ordinal_position"
        ).fetchall()
        assert [t for _, t in actual] == expected
        assert [c for c, _ in actual] == [*schema.names, "drive"]
    con.close()


def test_zero_batches_writes_valid_empty_file(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    lake.swap_partition("keypoints", DRIVE, [])
    f = part_file(tmp_path, "keypoints", DRIVE)
    t = pq.read_table(f)
    assert t.num_rows == 0
    assert t.schema.remove_metadata().equals(KEYPOINTS_SCHEMA)
    assert lake.list_drives("keypoints") == [DRIVE]
    con = lake.connect()
    assert con.execute("SELECT count(*) FROM keypoints").fetchone() == (0,)
    con.close()


def test_view_over_zero_row_partition_mixed_with_data(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    lake.swap_partition("keypoints", "full", [kp_batch([0, 1])])
    lake.swap_partition("keypoints", "empty", [])
    con = lake.connect()
    rows = con.execute("SELECT drive, count(*) FROM keypoints GROUP BY drive").fetchall()
    assert rows == [("full", 2)]
    con.close()


def test_drop_partition(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    lake.swap_partition("keypoints", "a", [kp_batch([0])])
    lake.swap_partition("keypoints", "b", [kp_batch([0])])
    lake.drop_partition("keypoints", "a")
    assert lake.list_drives("keypoints") == ["b"]
    lake.drop_partition("keypoints", "a")
    lake.drop_partition("quarantine", "never")
    assert lake.list_drives("keypoints") == ["b"]


def test_invalid_table_and_drive_rejected(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    with pytest.raises(ValueError):
        lake.swap_partition("nope", "a", [])
    for bad in ("", "../x", "a/b", "a\\b", "a.b", ".."):
        with pytest.raises(ValueError):
            lake.swap_partition("keypoints", bad, [])
        with pytest.raises(ValueError):
            lake.drop_partition("keypoints", bad)
        with pytest.raises(ValueError):
            lake.read("keypoints", bad)
    with pytest.raises(ValueError):
        lake.list_drives("nope")
    with pytest.raises(ValueError):
        lake.read("nope")
    with pytest.raises(ValueError):
        lake.drop_partition("nope", "a")


def test_temp_folders_not_listed(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    seen: list[object] = []

    def gen() -> Iterator[pa.RecordBatch]:
        yield kp_batch([0])
        seen.append(lake.list_drives("keypoints"))
        con = lake.connect()
        seen.append(con.execute("SELECT count(*) FROM keypoints").fetchone())
        con.close()
        yield kp_batch([1])

    lake.swap_partition("keypoints", "a", gen())
    assert seen == [[], (0,)]
    lake.swap_partition("keypoints", "a", [kp_batch([2])])
    assert lake.list_drives("keypoints") == ["a"]
    con = lake.connect()
    assert con.execute("SELECT count(*) FROM keypoints").fetchone() == (1,)
    con.close()
    assert_no_swap_leftovers(tmp_path)


def test_quote_in_root_path(tmp_path: Path) -> None:
    lake = Lake(tmp_path / "it's")
    lake.swap_partition("keypoints", "a", [kp_batch([0])])
    con = lake.connect()
    assert con.execute("SELECT count(*) FROM keypoints").fetchone() == (1,)
    con.close()


def test_special_characters_in_root_path(tmp_path: Path) -> None:
    # '*' and '?' cannot be used in Windows file names, so only test them elsewhere
    special = "lake{x}[2]" if sys.platform == "win32" else "lake*x{y}?[2]"
    lake = Lake(tmp_path / "run[1]" / special)
    lake.swap_partition("keypoints", "a", [kp_batch([0, 1])])
    lake.swap_partition("keypoints", "b", [kp_batch([0])])
    con = lake.connect()
    assert con.execute("SELECT count(*) FROM keypoints").fetchone() == (3,)
    assert con.execute("SELECT DISTINCT drive FROM keypoints ORDER BY drive").fetchall() == [
        ("a",),
        ("b",),
    ]
    con.close()


def test_foreign_folder_with_part_file_is_invisible(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    lake.swap_partition("keypoints", "a", [kp_batch([0, 1])])
    other = tmp_path / "keypoints" / "other"
    other.mkdir()
    (other / "part-0.parquet").write_bytes(part_file(tmp_path, "keypoints", "a").read_bytes())
    spaced = tmp_path / "keypoints" / "drive=a b"
    spaced.mkdir()
    (spaced / "part-0.parquet").write_bytes(part_file(tmp_path, "keypoints", "a").read_bytes())
    assert lake.list_drives("keypoints") == ["a"]
    con = lake.connect()
    assert con.execute("SELECT count(*) FROM keypoints").fetchone() == (2,)
    assert con.execute("SELECT DISTINCT drive FROM keypoints").fetchall() == [("a",)]
    con.close()


def test_invalid_drive_folder_names_not_listed(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    lake.swap_partition("keypoints", "a", [kp_batch([0])])
    src = part_file(tmp_path, "keypoints", "a").read_bytes()
    for name in ("drive=a b", "drive="):
        d = tmp_path / "keypoints" / name
        d.mkdir()
        (d / "part-0.parquet").write_bytes(src)
    assert lake.list_drives("keypoints") == ["a"]
    assert lake.read("keypoints").num_rows == 1
    con = lake.connect()
    assert con.execute("SELECT count(*) FROM keypoints").fetchone() == (1,)
    con.close()


def test_drop_partition_propagates_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lake = Lake(tmp_path)
    lake.swap_partition("keypoints", "a", [kp_batch([0])])

    def deny(path: object, ignore_errors: bool = False, **kwargs: object) -> None:
        if ignore_errors:  # like the real rmtree, errors are swallowed only when asked
            return
        raise PermissionError("locked")

    monkeypatch.setattr(shutil, "rmtree", deny)
    with pytest.raises(PermissionError, match="locked"):
        lake.drop_partition("keypoints", "a")
    monkeypatch.undo()
    assert lake.list_drives("keypoints") == ["a"]


@pytest.mark.parametrize("exc", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_interrupt_in_batches_keeps_old_partition(tmp_path: Path, exc: type[BaseException]) -> None:
    lake = Lake(tmp_path)
    lake.swap_partition("keypoints", DRIVE, [kp_batch([0, 1])])
    before = sha(part_file(tmp_path, "keypoints", DRIVE))

    def boom() -> Iterator[pa.RecordBatch]:
        yield kp_batch([9])
        raise exc()

    with pytest.raises(exc):
        lake.swap_partition("keypoints", DRIVE, boom())
    assert sha(part_file(tmp_path, "keypoints", DRIVE)) == before
    assert_no_swap_leftovers(tmp_path)


def test_restore_failure_keeps_original_exception_with_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lake = Lake(tmp_path)
    lake.swap_partition("keypoints", DRIVE, [kp_batch([0, 1])])
    real_rename = Path.rename
    calls: list[Path] = []

    def flaky(self: Path, target: str | Path) -> Path:
        calls.append(self)
        if len(calls) == 2:
            raise OSError("rename failed")
        if len(calls) == 3:
            raise OSError("restore failed")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", flaky)
    with pytest.raises(OSError, match="rename failed") as info:
        lake.swap_partition("keypoints", DRIVE, [kp_batch([7])])
    monkeypatch.undo()
    notes = getattr(info.value, "__notes__", [])
    assert any("old-" in n for n in notes)
    assert any(p.name.startswith("old-") for p in (tmp_path / "_swap").iterdir())


def test_swap_sweeps_leftover_swap_folders(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    swap = tmp_path / "_swap"
    for name in ("old-deadbeef", "new-deadbeef", "new-cafebabe"):
        d = swap / name
        d.mkdir(parents=True)
        (d / "part-0.parquet").write_text("x")
    two_hours_ago = time.time() - 7200
    os.utime(swap / "new-deadbeef", (two_hours_ago, two_hours_ago))
    lake.swap_partition("keypoints", "a", [kp_batch([0])])
    assert sorted(p.name for p in swap.iterdir()) == ["new-cafebabe", "old-deadbeef"]
    assert lake.list_drives("keypoints") == ["a"]


def test_second_swap_does_not_destroy_unrestored_old_partition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lake = Lake(tmp_path)
    lake.swap_partition("keypoints", DRIVE, [kp_batch([0, 1])])
    real_rename = Path.rename
    calls: list[Path] = []

    def flaky(self: Path, target: str | Path) -> Path:
        calls.append(self)
        if len(calls) == 2:
            raise OSError("rename failed")
        if len(calls) == 3:
            raise OSError("restore failed")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", flaky)
    with pytest.raises(OSError, match="rename failed"):
        lake.swap_partition("keypoints", DRIVE, [kp_batch([7])])
    monkeypatch.undo()
    olds = [p for p in (tmp_path / "_swap").iterdir() if p.name.startswith("old-")]
    assert len(olds) == 1
    assert not (tmp_path / "keypoints" / f"drive={DRIVE}").exists()

    lake.swap_partition("keypoints", DRIVE, [kp_batch([5])])
    lake.swap_partition("keypoints", "other", [kp_batch([6])])
    assert olds[0].is_dir()
    assert pq.read_table(olds[0] / "part-0.parquet").column("frame_id").to_pylist() == [0, 1]


def test_view_with_hive_like_segments_in_root(tmp_path: Path) -> None:
    lake = Lake(tmp_path / "env=prod" / "drive=x" / "lake")
    lake.swap_partition("keypoints", "a", [kp_batch([0, 1])])
    lake.swap_partition("keypoints", "b", [kp_batch([2])])
    con = lake.connect()
    cur = con.execute("SELECT * FROM keypoints ORDER BY drive, frame_id")
    assert [d[0] for d in cur.description] == [*KEYPOINTS_SCHEMA.names, "drive"]
    assert [r[0] for r in cur.fetchall()] == [0, 1, 2]
    rows = con.execute("SELECT drive, typeof(drive) FROM keypoints ORDER BY frame_id").fetchall()
    assert rows == [("a", "VARCHAR"), ("a", "VARCHAR"), ("b", "VARCHAR")]
    con.close()


def test_many_drives_in_one_view(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    for i in range(500):
        lake.swap_partition("keypoints", f"d{i}", [kp_batch([i])])
    con = lake.connect()
    assert con.execute("SELECT count(*), count(DISTINCT drive) FROM keypoints").fetchone() == (
        500,
        500,
    )
    con.close()


def test_swap_with_open_connection_idle(tmp_path: Path) -> None:
    """Swap while a connection stays open after a query.

    The Windows branch records behaviour observed on this machine: the swap
    succeeds even though the idle connection is open. Elsewhere the swap may
    be refused by the OS, in which case the old partition must be intact.
    """
    lake = Lake(tmp_path)
    lake.swap_partition("keypoints", DRIVE, [kp_batch([0, 1])])
    before = sha(part_file(tmp_path, "keypoints", DRIVE))
    con = lake.connect()
    assert con.execute("SELECT count(*) FROM keypoints").fetchone() == (2,)
    try:
        lake.swap_partition("keypoints", DRIVE, [kp_batch([0, 1, 2])])
    except OSError:
        assert sys.platform != "win32"
        assert sha(part_file(tmp_path, "keypoints", DRIVE)) == before
    else:
        assert lake.read("keypoints", DRIVE).num_rows == 3
    finally:
        con.close()
