"""Tests for VLClient over a small hand-made lake in tmp_path."""

from pathlib import Path

import duckdb
import pyarrow as pa
import pytest

import vl_data.sdk
from vl_data.core.lake import Lake
from vl_data.core.lake_schemas import (
    KEYPOINTS_SCHEMA,
    LINEAGE_SCHEMA,
    MOTION_SCHEMA,
    QUARANTINE_SCHEMA,
    TABLE_SCHEMAS,
)
from vl_data.sdk import VLClient


def kp_batch(frames: list[int], keypoint: str = "centroid") -> pa.RecordBatch:
    n = len(frames)
    return pa.record_batch(
        [
            pa.array(frames, pa.int64()),
            pa.array([f * 1000 for f in frames], pa.int64()),
            pa.array([keypoint] * n, pa.string()),
            pa.array([float(f) for f in frames], pa.float64()),
            pa.array([0.0] * n, pa.float64()),
            pa.array([0.0] * n, pa.float64()),
        ],
        schema=KEYPOINTS_SCHEMA,
    )


def motion_batch(frames: list[int]) -> pa.RecordBatch:
    n = len(frames)
    return pa.record_batch(
        [
            pa.array(frames, pa.int64()),
            pa.array([f * 1000 for f in frames], pa.int64()),
            pa.array([float(f) for f in frames], pa.float64()),
            pa.array([None] + [1.5] * (n - 1), pa.float64()),
            pa.array([None] * n, pa.float64()),
            pa.array([3] * n, pa.int32()),
        ],
        schema=MOTION_SCHEMA,
    )


def lineage_batch() -> pa.RecordBatch:
    return pa.record_batch(
        [
            pa.array(["h"], pa.string()),
            pa.array([1], pa.int32()),
            pa.array(["ok"], pa.string()),
            *[pa.array([n], pa.int64()) for n in (3, 3, 3, 0, 0, 3)],
            pa.array(["f"], pa.string()),
            pa.array(["g"], pa.string()),
        ],
        schema=LINEAGE_SCHEMA,
    )


def quarantine_batch() -> pa.RecordBatch:
    return pa.record_batch(
        [
            pa.array([2], pa.int64()),
            pa.array([None], pa.int64()),
            pa.array(["VL-001"], pa.string()),
            pa.array(["bad"], pa.string()),
        ],
        schema=QUARANTINE_SCHEMA,
    )


@pytest.fixture
def lake(tmp_path: Path) -> Lake:
    lk = Lake(tmp_path)
    lk.swap_partition("keypoints", "b", [kp_batch([2, 0, 1]), kp_batch([1, 0], "nose")])
    lk.swap_partition("keypoints", "a", [kp_batch([0])])
    lk.swap_partition("motion", "b", [motion_batch([0, 1, 2])])
    lk.swap_partition("lineage", "b", [lineage_batch()])
    lk.swap_partition("quarantine", "b", [quarantine_batch()])
    return lk


def test_drives_and_keypoints(lake: Lake) -> None:
    client = VLClient(lake)
    assert client.drives() == ["a", "b"]
    t = client.keypoints("b", "centroid")
    assert t.schema.equals(KEYPOINTS_SCHEMA)
    assert t.column("frame_id").to_pylist() == [0, 1, 2]
    assert set(t.column("keypoint").to_pylist()) == {"centroid"}
    all_b = client.keypoints("b")
    assert all_b.num_rows == 5
    assert all_b.column("frame_id").to_pylist() == [0, 0, 1, 1, 2]
    assert all_b.column("keypoint").to_pylist() == [
        "centroid",
        "nose",
        "centroid",
        "nose",
        "centroid",
    ]
    assert all_b.column("timestamp_ns").to_pylist() == [0, 0, 1000, 1000, 2000]
    assert all_b.column("x_cm").to_pylist() == [0.0, 0.0, 1.0, 1.0, 2.0]
    assert client.keypoints("b", "tail").num_rows == 0
    assert client.motion("b").schema.equals(MOTION_SCHEMA)
    assert client.lineage("b").column("status").to_pylist() == ["ok"]
    assert client.quarantine("b").column("code").to_pylist() == ["VL-001"]


def test_keypoints_ties_ordered_by_keypoint_name(tmp_path: Path) -> None:
    lk = Lake(tmp_path)
    lk.swap_partition(
        "keypoints",
        "d",
        [
            kp_batch([5], "tail"),
            kp_batch([5], "head"),
            kp_batch([4], "tail"),
            kp_batch([5], "nose"),
        ],
    )
    t = VLClient(lk).keypoints("d")
    assert t.column("frame_id").to_pylist() == [4, 5, 5, 5]
    assert t.column("keypoint").to_pylist() == ["tail", "head", "nose", "tail"]


def test_sql_over_views(lake: Lake) -> None:
    client = VLClient(lake)
    m = client.sql("SELECT frame_id, x_cm FROM motion ORDER BY frame_id")
    assert isinstance(m, pa.Table)
    assert m.column("frame_id").to_pylist() == [0, 1, 2]
    c = client.sql("SELECT keypoint, count(*) AS n FROM keypoints GROUP BY keypoint ORDER BY 1")
    assert c.to_pydict() == {"keypoint": ["centroid", "nose"], "n": [4, 2]}
    assert client.sql("  SELECT 1 AS one;  ").to_pydict() == {"one": [1]}


def test_unknown_drive_is_empty(lake: Lake) -> None:
    client = VLClient(lake)
    for table, method in [
        ("keypoints", client.keypoints),
        ("motion", client.motion),
        ("lineage", client.lineage),
        ("quarantine", client.quarantine),
    ]:
        t = method("nope")
        assert t.num_rows == 0
        assert t.schema.equals(TABLE_SCHEMAS[table])
    for bad in ["a/b", ""]:
        for method in (client.keypoints, client.motion, client.lineage, client.quarantine):
            with pytest.raises(ValueError):
                method(bad)
    empty = VLClient(Lake(lake.root / "empty"))
    assert empty.drives() == []
    assert empty.keypoints("x").schema.equals(KEYPOINTS_SCHEMA)
    assert empty.keypoints("x", "centroid").num_rows == 0


@pytest.mark.parametrize(
    "query",
    [
        "COPY keypoints TO 'x.csv'",
        "CREATE TABLE t AS SELECT 1",
        "INSTALL httpfs",
        "INSERT INTO keypoints SELECT * FROM keypoints",
        "DROP VIEW keypoints",
        "SELECT 1; SELECT 2",
        "SELECT 1; DROP VIEW keypoints",
        "",
        "SELEC nonsense",
        "PRAGMA version",
        "EXPLAIN SELECT 1",
    ],
)
def test_sql_rejects_non_select(lake: Lake, query: str) -> None:
    with pytest.raises(ValueError):
        VLClient(lake).sql(query)
    assert lake.list_drives("keypoints") == ["a", "b"]


def test_sql_accepts_with_select(lake: Lake) -> None:
    t = VLClient(lake).sql("WITH x AS (SELECT 1 AS a) SELECT * FROM x")
    assert t.to_pydict() == {"a": [1]}


def test_sql_closes_every_connection_it_opens(lake: Lake, monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[duckdb.DuckDBPyConnection] = []
    real_connect = lake.connect

    def spy() -> duckdb.DuckDBPyConnection:
        con = real_connect()
        opened.append(con)
        return con

    monkeypatch.setattr(lake, "connect", spy)
    client = VLClient(lake)
    assert client.sql("SELECT count(*) AS n FROM keypoints").column("n").to_pylist() == [6]
    with pytest.raises(duckdb.Error):
        client.sql("SELECT * FROM no_such_table")
    assert len(opened) == 2
    for con in opened:
        with pytest.raises(duckdb.Error):
            con.execute("SELECT 1")


def test_partition_swap_works_after_sql(lake: Lake) -> None:
    client = VLClient(lake)
    assert client.sql("SELECT count(*) AS n FROM keypoints").column("n").to_pylist() == [6]
    lake.swap_partition("keypoints", "b", [kp_batch([0])])
    assert client.keypoints("b").num_rows == 1
    with pytest.raises(duckdb.Error):
        client.sql("SELECT * FROM no_such_table")
    lake.swap_partition("keypoints", "b", [kp_batch([0, 1])])
    assert client.keypoints("b").num_rows == 2


def test_sdk_never_mentions_ground_truth() -> None:
    folder = Path(vl_data.sdk.__file__).parent
    files = list(folder.glob("*.py"))
    assert files
    for f in files:
        assert "ground_truth" not in f.read_text(encoding="utf-8")
