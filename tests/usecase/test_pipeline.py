"""compute_motion on a stub-ingested drive (hand-made keypoints and lineage partitions)."""

from pathlib import Path

import numpy as np
import pyarrow as pa
import pytest

from vl_data.core.config import NS_PER_SECOND, Config
from vl_data.core.lake import PART_NAME, Lake
from vl_data.core.lake_schemas import KEYPOINTS_SCHEMA, LINEAGE_SCHEMA, MOTION_SCHEMA
from vl_data.sdk.client import VLClient
from vl_data.usecase import pipeline
from vl_data.usecase.config import MotionConfig
from vl_data.usecase.estimator import MotionEstimate, estimate_motion
from vl_data.usecase.pipeline import MotionError, MotionResult, compute_motion

FPS = 16
STEP_NS = 62_500_000
X0, V0, A0 = 100.0, 20.0, 8.0  # cm, cm/s, cm/s^2


def _x(t: float) -> float:
    return X0 + V0 * t + 0.5 * A0 * t * t


def _ingest(
    lake: Lake,
    drive: str = "d1",
    n: int = 40,
    skip: frozenset[int] = frozenset(),
    status: str | None = "ok",
    centroid: bool = True,
) -> None:
    rows: list[tuple[int, int, str, float, float, float]] = []
    for f in range(n):
        if f in skip:
            continue
        ts = f * STEP_NS
        if centroid:
            rows.append((f, ts, "centroid", _x(ts / NS_PER_SECOND), 1.0, 2.0))
        rows.append((f, ts, "corner_a", 5000.0 + f, 1.0, 2.0))
        rows.append((f, ts, "corner_b", -5000.0 - f, 1.0, 2.0))
    cols = list(zip(*rows, strict=True))
    kp = pa.table(
        {
            name: pa.array(col, type=KEYPOINTS_SCHEMA.field(name).type)
            for name, col in zip(KEYPOINTS_SCHEMA.names, cols, strict=True)
        },
        schema=KEYPOINTS_SCHEMA,
    )
    lake.swap_partition("keypoints", drive, kp.to_batches())
    if status is not None:
        lin = pa.table(
            {
                "scenario_hash": ["abc"],
                "schema_version": pa.array([1], pa.int32()),
                "status": [status],
                "frames_expected": [n],
                "frames_in": [n],
                "frames_kept": [n - len(skip)],
                "frames_rejected": [0],
                "frames_missing": [len(skip)],
                "keypoint_rows": [len(rows)],
                "frames_sha256": ["f"],
                "ground_truth_sha256": ["g"],
            },
            schema=LINEAGE_SCHEMA,
        )
        lake.swap_partition("lineage", drive, lin.to_batches())


@pytest.fixture
def lake(tmp_path: Path) -> Lake:
    return Lake(tmp_path / "lake")


def _run(lake: Lake, drive: str = "d1") -> MotionResult:
    return compute_motion(VLClient(lake), lake, drive, Config(), MotionConfig())


def test_motion_partition_written(lake: Lake) -> None:
    _ingest(lake)
    res = _run(lake)
    assert res.frames_read == 40
    assert res.frames_null == 0
    assert res.frames_estimated == 40
    tbl = VLClient(lake).motion("d1")
    assert tbl.num_rows == 40
    v = tbl.column("velocity_cm_s").to_numpy()
    a = tbl.column("acceleration_cm_s2").to_numpy()
    for i in range(10, 30):
        t = i / FPS
        assert v[i] == pytest.approx(V0 + A0 * t, abs=1e-6)
        assert a[i] == pytest.approx(A0, abs=1e-6)


def test_nan_is_written_as_null(lake: Lake) -> None:
    # every 5th frame only in 10..29: gaps of 5 frames exceed max_gap_frames (4)
    _ingest(lake, n=40, skip=frozenset(f for f in range(10, 30) if f % 5))
    res = _run(lake)
    tbl = VLClient(lake).motion("d1")
    assert res.frames_null > 0
    vel = tbl.column("velocity_cm_s")
    acc = tbl.column("acceleration_cm_s2")
    either = np.asarray(vel.is_null().to_numpy(zero_copy_only=False)) | np.asarray(
        acc.is_null().to_numpy(zero_copy_only=False)
    )
    assert int(either.sum()) == res.frames_null
    assert vel.null_count > 0
    for col in (vel, acc):
        vals = col.drop_null().to_numpy()
        assert not np.isnan(vals).any()
    assert res.frames_estimated + res.frames_null == res.frames_read


def test_rerun_is_byte_identical(lake: Lake) -> None:
    _ingest(lake)
    _run(lake)
    path = lake.root / "motion" / "drive=d1" / PART_NAME
    first = path.read_bytes()
    _run(lake)
    assert path.read_bytes() == first


@pytest.mark.parametrize("case", ["no_lineage", "rejected", "no_centroid"])
def test_refuses_unprocessable_drive(lake: Lake, case: str) -> None:
    if case == "no_lineage":
        _ingest(lake, status=None)
    elif case == "rejected":
        _ingest(lake, status="rejected")
    else:
        _ingest(lake, centroid=False)
    with pytest.raises(MotionError):
        _run(lake)
    assert lake.list_drives("motion") == []
    with pytest.raises(MotionError):
        _run(lake, "nosuch")
    assert lake.list_drives("motion") == []


def test_missing_frame_gives_null_counts(lake: Lake) -> None:
    _ingest(lake, skip=frozenset({20}))
    res = _run(lake)
    assert res.frames_read == 39
    assert res.frames_estimated + res.frames_null == res.frames_read
    # the 2-frame gap is under max_gap_frames (4) and every window keeps >= min_points (5)
    assert res.frames_null == 0
    assert res.frames_estimated == 39
    tbl = VLClient(lake).motion("d1")
    assert tbl.num_rows == 39
    frames = tbl.column("frame_id").to_numpy()
    v = tbl.column("velocity_cm_s").to_numpy()
    for frame, vel in zip(frames, v, strict=True):
        if 10 <= frame < 30:
            assert vel == pytest.approx(V0 + A0 * frame / FPS, abs=1e-6)


def test_stored_x_is_raw_centroid(lake: Lake) -> None:
    _ingest(lake)
    _run(lake)
    client = VLClient(lake)
    kp = client.keypoints("d1", "centroid")
    mo = client.motion("d1")
    assert mo.column("x_cm").to_pylist() == kp.column("x_cm").to_pylist()
    assert mo.column("frame_id").to_pylist() == kp.column("frame_id").to_pylist()
    assert mo.column("timestamp_ns").to_pylist() == kp.column("timestamp_ns").to_pylist()


def test_motion_readable_through_client_with_schema(lake: Lake) -> None:
    _ingest(lake)
    _run(lake)
    tbl = VLClient(lake).motion("d1")
    assert tbl.schema.equals(MOTION_SCHEMA)


def _replace_centroid(
    lake: Lake, ts: dict[int, int] | None = None, x: dict[int, float] | None = None
) -> None:
    """Overwrite timestamp_ns / x_cm of centroid rows (keyed by row index among centroid rows)."""
    kp = VLClient(lake).keypoints("d1")
    names = kp.column("keypoint").to_pylist()
    cols = {n: kp.column(n).to_pylist() for n in KEYPOINTS_SCHEMA.names}
    idx = [i for i, k in enumerate(names) if k == "centroid"]
    for j, v in (ts or {}).items():
        cols["timestamp_ns"][idx[j]] = v
    for j, xv in (x or {}).items():
        cols["x_cm"][idx[j]] = xv
    tbl = pa.table(
        {n: pa.array(c, type=KEYPOINTS_SCHEMA.field(n).type) for n, c in cols.items()},
        schema=KEYPOINTS_SCHEMA,
    )
    lake.swap_partition("keypoints", "d1", tbl.to_batches())


def test_refuses_duplicate_timestamp(lake: Lake) -> None:
    _ingest(lake)
    _replace_centroid(lake, ts={6: 5 * STEP_NS})
    with pytest.raises(MotionError, match="increasing"):
        _run(lake)
    assert lake.list_drives("motion") == []


def test_refuses_out_of_order_timestamp(lake: Lake) -> None:
    _ingest(lake)
    _replace_centroid(lake, ts={6: 4 * STEP_NS})
    with pytest.raises(MotionError, match="increasing"):
        _run(lake)
    assert lake.list_drives("motion") == []


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_refuses_non_finite_x(lake: Lake, bad: float) -> None:
    _ingest(lake)
    _replace_centroid(lake, x={3: bad})
    with pytest.raises(MotionError, match="finite"):
        _run(lake)
    assert lake.list_drives("motion") == []


def test_refuses_two_lineage_rows(lake: Lake) -> None:
    _ingest(lake)
    lin = VLClient(lake).lineage("d1")
    two = pa.concat_tables([lin, lin]).cast(LINEAGE_SCHEMA)
    lake.swap_partition("lineage", "d1", two.to_batches())
    with pytest.raises(MotionError, match="exactly one"):
        _run(lake)
    assert lake.list_drives("motion") == []


def test_null_masks_are_per_column(lake: Lake, monkeypatch: pytest.MonkeyPatch) -> None:
    _ingest(lake)
    n = 40

    def fake(t_s: object, x_cm: object, fps: int, cfg: MotionConfig) -> MotionEstimate:
        vel = np.arange(n, dtype=np.float64)
        acc = np.arange(n, dtype=np.float64) + 100.0
        vel[5] = np.nan
        acc[9] = np.nan
        return MotionEstimate(vel, acc, np.zeros(n), np.full(n, 7, dtype=np.int32))

    monkeypatch.setattr(pipeline, "estimate_motion", fake)
    res = _run(lake)
    tbl = VLClient(lake).motion("d1")
    vel_col = tbl.column("velocity_cm_s")
    acc_col = tbl.column("acceleration_cm_s2")
    assert vel_col[5].as_py() is None
    assert acc_col[5].as_py() == 105.0
    assert vel_col[9].as_py() == 9.0
    assert acc_col[9].as_py() is None
    assert vel_col.null_count == 1
    assert acc_col.null_count == 1
    assert res.frames_null == 2
    assert res.frames_estimated == 38


def test_refusal_leaves_existing_motion_untouched(lake: Lake) -> None:
    _ingest(lake)
    _run(lake)
    path = lake.root / "motion" / "drive=d1" / PART_NAME
    before = path.read_bytes()
    lin = VLClient(lake).lineage("d1").to_pydict()
    lin["status"] = ["rejected"]
    lake.swap_partition("lineage", "d1", pa.table(lin, schema=LINEAGE_SCHEMA).to_batches())
    with pytest.raises(MotionError):
        _run(lake)
    assert path.read_bytes() == before


def test_n_points_matches_estimator(lake: Lake) -> None:
    _ingest(lake)
    _run(lake)
    tbl = VLClient(lake).motion("d1")
    ts = tbl.column("timestamp_ns").to_numpy()
    t_s = (ts - ts[0]) / NS_PER_SECOND
    cfg = MotionConfig()
    expected = estimate_motion(t_s, tbl.column("x_cm").to_numpy(), FPS, cfg).n_points
    got = tbl.column("n_points").to_numpy()
    assert got.tolist() == expected.tolist()
    assert cfg.window_frames % 2 == 0
    assert got[20] == cfg.window_frames + 1
    assert (got[8:32] == cfg.window_frames + 1).all()
