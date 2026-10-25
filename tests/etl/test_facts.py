"""Tests for the frame scan (pass 1). Units: cm, ns."""

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from numpy.typing import NDArray

from vl_data.core.schemas import FRAMES_SCHEMA, encode_points
from vl_data.etl import facts as facts_module
from vl_data.etl.config import EtlConfig, KeypointSpec
from vl_data.etl.facts import FACT_COLUMNS, scan_frames
from vl_data.etl.keypoints import extract_keypoints
from vl_data.generator.body import cube_lattice
from vl_data.generator.drive import Drive
from vl_data.generator.faults import CorruptPoints

from .helpers import MAX_ROWS, Written, build_table, scan_facts


def test_fact_columns() -> None:
    assert FACT_COLUMNS == [
        "row_index",
        "frame_id",
        "timestamp_ns",
        "n_bytes",
        "payload_sha256",
        "cloud_ok",
        "cx_cm",
        "cy_cm",
        "cz_cm",
    ]


def test_one_fact_per_row(write_faulty: Callable[..., Written]) -> None:
    w = write_faulty()
    facts = scan_facts(w.frames_path, w.offsets, 256)
    assert list(facts.columns) == FACT_COLUMNS
    assert len(facts) == 1000
    assert facts["row_index"].tolist() == list(range(1000))
    assert facts["frame_id"].tolist() == list(range(1000))
    assert facts["frame_id"].dtype == np.int64
    assert facts["row_index"].dtype == np.int64
    # byte-identical (stationary) frames hash equally; VL-003 relies on this
    assert facts["payload_sha256"].iloc[:32].nunique() == 1
    assert facts["payload_sha256"].nunique() < 1000
    assert facts["cloud_ok"].all()
    assert (facts["n_bytes"] == 125 * 12).all()
    truth = pq.read_table(w.drive_dir / "ground_truth.parquet").to_pandas()
    half = (w.config.body.size_cm - 1) / 2
    assert np.abs(facts["cx_cm"] - (truth["x_cm"] + half)).max() < 0.6


def test_chunking_gives_same_facts(write_faulty: Callable[..., Written]) -> None:
    w = write_faulty()
    base = scan_facts(w.frames_path, w.offsets, 256)
    for chunk in (1, 7):
        pd.testing.assert_frame_equal(scan_facts(w.frames_path, w.offsets, chunk), base)


def test_hash_is_sha256_of_payload(
    write_faulty: Callable[..., Written], clean_drive: Drive
) -> None:
    w = write_faulty()
    facts = scan_facts(w.frames_path, w.offsets, 7)
    payloads = clean_drive.frames.column("points").to_pylist()
    assert facts["payload_sha256"].tolist() == [hashlib.sha256(p).hexdigest() for p in payloads]


def test_corrupt_payload_not_cloud_ok(
    write_faulty: Callable[..., Written], clean_drive: Drive
) -> None:
    ids = clean_drive.frames.column("frame_id").to_pylist()
    ts = clean_drive.frames.column("timestamp_ns").to_pylist()
    pts = clean_drive.frames.column("points").to_pylist()
    pts[10] = pts[10][:13]  # 13 bytes: not a multiple of 12
    moved = np.frombuffer(pts[20], dtype="<i4").reshape(-1, 3).copy()
    moved[7, 1] += 2  # one point 2 cm off the lattice
    pts[20] = encode_points(moved)
    pts[30] = b""  # empty
    pts[40] = None  # null
    table = pa.Table.from_arrays(
        [pa.array(ids), pa.array(ts), pa.array(pts, type=pa.binary())], schema=FRAMES_SCHEMA
    )
    w = write_faulty(table=table)
    facts = scan_facts(w.frames_path, w.offsets, 256)
    assert len(facts) == 1000
    for row, n_bytes in {10: 13, 20: 1500, 30: 0, 40: 0}.items():
        fact = facts.iloc[row]
        assert not fact["cloud_ok"]
        assert fact["n_bytes"] == n_bytes
        assert np.isnan(fact[["cx_cm", "cy_cm", "cz_cm"]].astype(float)).all()
    assert facts["cloud_ok"].sum() == 996

    w2 = write_faulty([CorruptPoints(50)])
    f2 = scan_facts(w2.frames_path, w2.offsets, 256)
    assert not f2.loc[50, "cloud_ok"]
    assert f2.loc[50, "n_bytes"] == 124 * 12
    assert np.isnan(f2.loc[50, "cx_cm"])
    assert f2["cloud_ok"].sum() == 999


def test_ground_truth_never_opened(write_faulty: Callable[..., Written]) -> None:
    w = write_faulty()
    (w.drive_dir / "ground_truth.parquet").unlink()
    facts = scan_facts(w.frames_path, w.offsets, 256)
    assert len(facts) == 1000
    assert facts["cloud_ok"].all()


def test_empty_file_gives_empty_facts(
    write_faulty: Callable[..., Written], clean_drive: Drive
) -> None:
    w = write_faulty(table=clean_drive.frames.slice(0, 0))
    facts = scan_facts(w.frames_path, w.offsets, 256)
    assert list(facts.columns) == FACT_COLUMNS
    assert len(facts) == 0


def _with_payload(clean_drive: Drive, row: int, points: NDArray[np.int32]) -> pa.Table:
    ids = clean_drive.frames.column("frame_id").to_pylist()
    ts = clean_drive.frames.column("timestamp_ns").to_pylist()
    pts = clean_drive.frames.column("points").to_pylist()
    pts[row] = encode_points(points.astype(np.int32))
    return pa.Table.from_arrays(
        [pa.array(ids), pa.array(ts), pa.array(pts, type=pa.binary())], schema=FRAMES_SCHEMA
    )


def _points(clean_drive: Drive, row: int) -> NDArray[np.int32]:
    raw = clean_drive.frames.column("points")[row].as_py()
    return np.frombuffer(raw, dtype="<i4").reshape(-1, 3).copy()


def test_demo_frames_with_half_cm_positions_are_ok(write_faulty: Callable[..., Written]) -> None:
    """Frames 96, 97, 320, 321 and 592 have x ending in .5 and a 1 cm shape wobble."""
    w = write_faulty()
    facts = scan_facts(w.frames_path, w.offsets, 256)
    assert facts["cloud_ok"].all()
    assert not facts[["cx_cm", "cy_cm", "cz_cm"]].isna().any().any()


def test_wobble_of_one_cm_is_accepted(
    write_faulty: Callable[..., Written], clean_drive: Drive
) -> None:
    """Accepted limit: one point moved by exactly 1 cm (spread stays at most 1) passes."""
    pts = _points(clean_drive, 20)
    pts[7, 1] += 1
    w = write_faulty(table=_with_payload(clean_drive, 20, pts))
    assert scan_facts(w.frames_path, w.offsets, 256).loc[20, "cloud_ok"]


def test_two_cm_move_rejected_on_every_axis(
    write_faulty: Callable[..., Written], clean_drive: Drive
) -> None:
    for axis in range(3):
        pts = _points(clean_drive, 20)
        pts[7, axis] -= 2
        w = write_faulty(table=_with_payload(clean_drive, 20, pts))
        fact = scan_facts(w.frames_path, w.offsets, 256).loc[20]
        assert not fact["cloud_ok"]
        assert np.isnan(fact["cx_cm"])


def test_swapped_points_rejected(write_faulty: Callable[..., Written], clean_drive: Drive) -> None:
    pts = _points(clean_drive, 20)
    pts[[0, 1]] = pts[[1, 0]]
    w = write_faulty(table=_with_payload(clean_drive, 20, pts))
    assert not scan_facts(w.frames_path, w.offsets, 256).loc[20, "cloud_ok"]


def _offsets() -> NDArray[np.int32]:
    lattice = cube_lattice(40, 5)
    return (lattice - lattice[0]).astype(np.int32)


def _write(table: pa.Table, path: Path, row_group_size: int = 1000) -> Path:
    pq.write_table(table, path, row_group_size=row_group_size)
    return path


def test_chunk_frames_below_one_raises(write_faulty: Callable[..., Written]) -> None:
    w = write_faulty()
    for bad in (0, -1):
        with pytest.raises(ValueError, match="chunk_frames"):
            scan_facts(w.frames_path, w.offsets, bad)


def test_null_frame_id_raises(tmp_path: Path) -> None:
    table = build_table([0, 1, 2], [0, 1, 2], [b"", b"", b""])
    table = table.set_column(0, "frame_id", pa.array([0, None, 2], type=pa.int64()))
    path = _write(table, tmp_path / "f.parquet")
    with pytest.raises(ValueError, match=r"frame_id.*row 1"):
        scan_facts(path, _offsets(), 2)


def test_null_timestamp_raises(tmp_path: Path) -> None:
    table = build_table([0, 1, 2], [0, 1, 2], [b"", b"", b""])
    table = table.set_column(1, "timestamp_ns", pa.array([0, 1, None], type=pa.int64()))
    path = _write(table, tmp_path / "f.parquet")
    with pytest.raises(ValueError, match=r"timestamp_ns.*row 2"):
        scan_facts(path, _offsets(), 1)


def test_missing_or_mistyped_column_raises(tmp_path: Path) -> None:
    table = build_table([0], [0], [b""])
    renamed = _write(table.rename_columns(["frame_id", "timestamp_ns", "pts"]), tmp_path / "a")
    with pytest.raises(ValueError, match="points"):
        scan_facts(renamed, _offsets(), 10)
    retyped = table.set_column(1, "timestamp_ns", pa.array([0], type=pa.int32()))
    with pytest.raises(ValueError, match="timestamp_ns"):
        scan_facts(_write(retyped, tmp_path / "b"), _offsets(), 10)


def test_chunking_across_row_groups(tmp_path: Path) -> None:
    offsets = np.array([[0, 0, 0], [1, 0, 0]], dtype=np.int32)
    n = 2500
    pts: list[bytes | None] = [
        encode_points(offsets + np.array([i, 0, 0], dtype=np.int32)) for i in range(n)
    ]
    pts[1234] = b"abc"  # one bad payload
    path = _write(build_table(list(range(n)), [i * 5 for i in range(n)], pts), tmp_path / "f")
    base = scan_facts(path, offsets, 1000)
    assert len(base) == n
    assert base["row_index"].tolist() == list(range(n))
    assert not base.loc[1234, "cloud_ok"]
    assert base["cloud_ok"].sum() == n - 1
    for chunk in (1, 7, 999, 1001, 5000):
        pd.testing.assert_frame_equal(scan_facts(path, offsets, chunk), base)


def test_more_rows_than_the_footer_claims_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    offsets = np.array([[0, 0, 0], [1, 0, 0]], dtype=np.int32)
    pts: list[bytes | None] = [encode_points(offsets)] * 10
    path = _write(build_table(list(range(10)), list(range(10)), pts), tmp_path / "f")

    class _Footer:
        """The real footer, except that it claims 4 rows."""

        def __init__(self, real: Any) -> None:
            self.real = real
            self.num_rows = 4

        def __getattr__(self, name: str) -> Any:
            return getattr(self.real, name)

    class Liar(pq.ParquetFile):
        @property
        def metadata(self) -> Any:  # type: ignore[override]
            return _Footer(super().metadata)

    monkeypatch.setattr(facts_module.pq, "ParquetFile", Liar)
    with pytest.raises(ValueError, match="more rows than the footer"):
        scan_frames(path, offsets, 3, [], MAX_ROWS)


def test_footer_rows_above_max_rows_raise_before_allocating(tmp_path: Path) -> None:
    offsets = np.array([[0, 0, 0], [1, 0, 0]], dtype=np.int32)
    pts: list[bytes | None] = [encode_points(offsets)] * 10
    path = _write(build_table(list(range(10)), list(range(10)), pts), tmp_path / "f")
    with pytest.raises(ValueError, match="max_rows"):
        scan_frames(path, offsets, 3, [], max_rows=9)
    facts, _ = scan_frames(path, offsets, 3, [], max_rows=10)  # exactly at the cap
    assert len(facts) == 10


def _decoded(clean_drive: Drive) -> NDArray[np.int32]:
    """All frames of the drive as a (frames, n, 3) int32 array."""
    raw = clean_drive.frames.column("points").to_pylist()
    return np.stack([np.frombuffer(p, dtype="<i4").reshape(-1, 3) for p in raw])


def test_keypoints_equal_extract_keypoints(
    write_faulty: Callable[..., Written], clean_drive: Drive
) -> None:
    w = write_faulty()
    specs = EtlConfig().keypoints
    facts, kp = scan_frames(w.frames_path, w.offsets, 100, specs, MAX_ROWS)
    expected = extract_keypoints(_decoded(clean_drive), specs)
    assert kp.shape == (1000, 9, 3)
    assert kp.dtype == np.float64
    np.testing.assert_allclose(kp, expected, rtol=0, atol=1e-9)
    # corners are whole numbers and exact
    np.testing.assert_array_equal(kp[:, :8], expected[:, :8])
    # the centroid column of the facts is the centroid keypoint
    np.testing.assert_array_equal(facts[["cx_cm", "cy_cm", "cz_cm"]].to_numpy(), kp[:, 8])


def test_keypoints_chunk_independent_and_nan_for_bad_rows(
    write_faulty: Callable[..., Written], clean_drive: Drive
) -> None:
    ids = clean_drive.frames.column("frame_id").to_pylist()
    ts = clean_drive.frames.column("timestamp_ns").to_pylist()
    pts = clean_drive.frames.column("points").to_pylist()
    pts[10] = pts[10][:13]
    pts[40] = None
    w = write_faulty(table=build_table(ids, ts, pts))
    specs = EtlConfig().keypoints
    _, base = scan_frames(w.frames_path, w.offsets, 256, specs, MAX_ROWS)
    for chunk in (1, 7):
        np.testing.assert_array_equal(
            scan_frames(w.frames_path, w.offsets, chunk, specs, MAX_ROWS)[1], base
        )
    assert np.isnan(base[[10, 40]]).all()
    assert not np.isnan(np.delete(base, [10, 40], axis=0)).any()


def test_no_keypoints_gives_empty_second_axis(write_faulty: Callable[..., Written]) -> None:
    w = write_faulty()
    facts, kp = scan_frames(w.frames_path, w.offsets, 256, [], MAX_ROWS)
    assert kp.shape == (1000, 0, 3)
    assert len(facts) == 1000


def test_only_centroid_spec(write_faulty: Callable[..., Written]) -> None:
    w = write_faulty()
    spec = [KeypointSpec(name="c", kind="centroid")]
    facts, kp = scan_frames(w.frames_path, w.offsets, 256, spec, MAX_ROWS)
    np.testing.assert_array_equal(kp[:, 0], facts[["cx_cm", "cy_cm", "cz_cm"]].to_numpy())


def test_bad_keypoint_spec_raises_value_error(write_faulty: Callable[..., Written]) -> None:
    w = write_faulty()
    with pytest.raises(ValueError, match="far"):
        scan_frames(w.frames_path, w.offsets, 256, [KeypointSpec(name="far", index=999)], MAX_ROWS)


def test_extreme_coordinates_use_exact_arithmetic(tmp_path: Path) -> None:
    """Values near the int32 limits give the same answer as int64 arithmetic."""
    off = np.array([[0, 0, 0], [3, 0, 0], [0, 3, 0], [0, 0, 3]], dtype=np.int32)
    low = np.iinfo(np.int32).min
    high = np.iinfo(np.int32).max
    clouds = [
        off.astype(np.int64) + low,  # every deviation is exactly INT32_MIN: ok
        np.full((4, 3), low, dtype=np.int64),  # deviations below INT32_MIN: not ok
        np.full((4, 3), high, dtype=np.int64),  # not ok
        off.astype(np.int64) + high - 3,  # ok
        off.astype(np.int64) + 7,  # ok
    ]
    path = _write(
        build_table(
            list(range(5)),
            [0, 1, 2, 3, 4],
            [encode_points(c.astype(np.int32)) for c in clouds],
        ),
        tmp_path / "f",
    )
    facts, _ = scan_frames(path, off, 2, [], MAX_ROWS)
    assert facts["cloud_ok"].tolist() == [True, False, False, True, True]
    # a negative offset lets the deviation overflow upwards
    neg = np.array([[0, 0, 0], [-5, 0, 0]], dtype=np.int32)
    cloud = np.array([[0, 0, 0], [high, 0, 0]], dtype=np.int64)
    path2 = _write(build_table([0], [0], [encode_points(cloud.astype(np.int32))]), tmp_path / "g")
    assert not scan_frames(path2, neg, 1, [], MAX_ROWS)[0].loc[0, "cloud_ok"]


def test_cloud_ok_matches_int64_reference(tmp_path: Path) -> None:
    """Random small wobbles give the same cloud_ok as a plain int64 spread computation."""
    rng = np.random.default_rng(7)
    off = _offsets()
    pts: list[bytes | None] = []
    want: list[bool] = []
    for _ in range(80):
        cloud = off.astype(np.int64) + rng.integers(-300, 300, size=3)
        wobble = rng.integers(-2, 3, size=off.shape) * (rng.random(off.shape) < 0.004)
        cloud = cloud + wobble
        dev = cloud - off.astype(np.int64)
        want.append(bool(((dev.max(axis=0) - dev.min(axis=0)) <= 1).all()))
        pts.append(encode_points(cloud.astype(np.int32)))
    path = _write(build_table(list(range(80)), list(range(80)), pts), tmp_path / "f")
    facts, _ = scan_frames(path, off, 16, [], MAX_ROWS)
    assert facts["cloud_ok"].tolist() == want
    assert 0 < sum(want) < 80
