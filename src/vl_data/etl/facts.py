"""The single scan of the ETL: one small "fact" row and the keypoints of each frame.

Units: cm, ns.
"""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from numpy.typing import NDArray

from vl_data.core.schemas import FRAMES_SCHEMA
from vl_data.etl.config import KeypointSpec
from vl_data.etl.keypoints import extract_keypoints

FACT_COLUMNS: list[str] = [
    "row_index",  # position of the row in the file, from 0
    "frame_id",
    "timestamp_ns",  # ns
    "n_bytes",  # payload length in bytes, 0 for a null payload
    "payload_sha256",  # hex SHA-256 of the payload bytes
    "cloud_ok",  # payload is exactly the expected lattice
    "cx_cm",  # centroid x in cm, NaN unless cloud_ok
    "cy_cm",  # centroid y in cm, NaN unless cloud_ok
    "cz_cm",  # centroid z in cm, NaN unless cloud_ok
]
_BYTES_PER_POINT = 12  # bytes; schemas.py keeps this private, so one local copy
_MAX_SPREAD_CM = 1  # cm, rounding wobble allowed in the lattice shape
_INT32_MIN = int(np.iinfo(np.int32).min)
_INT32_MAX = int(np.iinfo(np.int32).max)


def check_keypoint_specs(n_points: int, specs: list[KeypointSpec]) -> None:
    """Raise ValueError if a keypoint spec cannot be applied to a cloud of n_points points.

    A spec index outside [0, n_points), or a corner spec when n_points is not a cube n**3 with
    n >= 2, is refused (the message names the keypoint). Does nothing when n_points < 1.
    """
    if n_points >= 1:
        extract_keypoints(np.zeros((1, n_points, 3), dtype=np.int32), specs)


def scan_frames(
    frames_path: Path,
    offsets: NDArray[np.int32],
    chunk_frames: int,
    keypoints: list[KeypointSpec],
    max_rows: int,
) -> tuple[pd.DataFrame, NDArray[np.float64]]:
    """Return (facts, keypoints) with one entry per row of the frames file, in file order.

    offsets is the expected lattice in cm relative to its first point, shape (n, 3), so
    offsets[0] is (0, 0, 0). A cloud is OK when the payload length is a multiple of 12 bytes,
    decodes to exactly n points and, per axis, the deviation d = points - offsets has a spread
    (max - min) of at most 1 cm. Whole-cm rounding in the generator (round half to even in
    place_body) makes a 1 cm shape wobble normal, so this deliberately does not catch a single
    point moved by exactly 1 cm; it does catch a wrong count, a length that is not a multiple
    of 12, a point moved by 2 cm or more and a scrambled point order. The deviation is computed
    in int32 when that cannot overflow (checked from the smallest and largest coordinate) and in
    int64 otherwise, so the answer never depends on the shortcut.

    The facts hold the centroid (cm, the mean of the decoded points when the cloud is OK, else
    NaN). The second result is a (rows, len(keypoints), 3) float64 array in cm in file-row order:
    the keypoints of each OK frame, NaN for the others (the centroid keypoint is that same mean).
    A bad, empty or null payload never raises. Only frames_path is opened; ground truth is never
    read.

    Raises ValueError if the footer's num_rows exceeds max_rows (rows; checked before the keypoint
    array is allocated, because that array is sized from the footer), if the streamed row count
    exceeds the footer's num_rows, if chunk_frames < 1, if a keypoint spec does not fit the cloud
    (see check_keypoint_specs), if frame_id, timestamp_ns or points is missing or has another
    type, or if a frame_id or timestamp_ns is null (the message names the column and row). The
    caller must gate on VL-005 (the schema rule) before scanning.

    Memory: the file is streamed in batches of chunk_frames rows, so at most one batch of
    payloads is held at a time. pyarrow reads whole row groups, so the file's row-group size
    also bounds memory (about 50 frames, 600 MB, at full density). The returned data is small:
    one fact row and len(keypoints) * 24 bytes per frame.
    """
    if chunk_frames < 1:
        raise ValueError(f"chunk_frames must be at least 1, got {chunk_frames}")
    n_points = offsets.shape[0]
    check_keypoint_specs(n_points, keypoints)
    expected = np.ascontiguousarray(offsets, dtype=np.int32)
    expected_bytes = n_points * _BYTES_PER_POINT
    offset_lo = int(np.min(expected)) if n_points else 0  # cm
    offset_hi = int(np.max(expected)) if n_points else 0  # cm
    other_specs = [s for s in keypoints if s.kind != "centroid"]
    other_slots = [k for k, s in enumerate(keypoints) if s.kind != "centroid"]
    centroid_slots = [k for k, s in enumerate(keypoints) if s.kind == "centroid"]

    row_index: list[int] = []
    frame_id: list[int] = []
    timestamp: list[int] = []
    n_bytes: list[int] = []
    sha: list[str] = []
    cloud_ok: list[bool] = []
    centroid: list[tuple[float, float, float]] = []

    next_row = 0
    # The executor is the inner context: it is shut down (hash futures are all resolved by then)
    # before the file closes, so no hash task can touch a closed file.
    with pq.ParquetFile(frames_path) as parquet, ThreadPoolExecutor(max_workers=1) as hasher:
        _check_columns(parquet.schema_arrow)
        total_rows = parquet.metadata.num_rows  # rows claimed by the footer
        if total_rows > max_rows:
            raise ValueError(f"the footer claims {total_rows} rows, above max_rows {max_rows}")
        picked = np.full((total_rows, len(keypoints), 3), np.nan, dtype=np.float64)
        for batch in parquet.iter_batches(
            batch_size=chunk_frames, columns=["frame_id", "timestamp_ns", "points"]
        ):
            for name in ("frame_id", "timestamp_ns"):
                column = batch.column(name)
                if column.null_count:
                    first_null = next(i for i in range(len(column)) if not column[i].is_valid)
                    raise ValueError(f"column {name} is null at row {next_row + first_null}")
            ids = batch.column("frame_id").to_pylist()
            stamps = batch.column("timestamp_ns").to_pylist()
            payloads = batch.column("points")
            if next_row + batch.num_rows > total_rows:
                raise ValueError(f"the file holds more rows than the footer claims ({total_rows})")
            for i in range(batch.num_rows):
                row_index.append(next_row + i)
                frame_id.append(ids[i])
                timestamp.append(stamps[i])
                scalar = payloads[i]
                buf = scalar.as_buffer() if scalar.is_valid else None
                if buf is None:  # null payload
                    buf = pa.py_buffer(b"")
                n_bytes.append(buf.size)
                digest = hasher.submit(_sha256_hex, buf)  # overlaps the numpy work below
                points = _checked_cloud(buf, expected, expected_bytes, offset_lo, offset_hi)
                sha.append(digest.result())
                if points is None:
                    cloud_ok.append(False)
                    centroid.append((np.nan, np.nan, np.nan))
                    continue
                mean = _centroid(points)
                cloud_ok.append(True)
                centroid.append(mean)
                row = next_row + i
                if other_specs:
                    picked[row, other_slots] = extract_keypoints(points[np.newaxis], other_specs)[0]
                for slot in centroid_slots:
                    picked[row, slot] = mean
            next_row += batch.num_rows

    cent = np.array(centroid, dtype=np.float64).reshape(-1, 3)
    facts = pd.DataFrame(
        {
            "row_index": np.array(row_index, dtype=np.int64),
            "frame_id": np.array(frame_id, dtype=np.int64),
            "timestamp_ns": np.array(timestamp, dtype=np.int64),
            "n_bytes": np.array(n_bytes, dtype=np.int64),
            "payload_sha256": pd.Series(sha, dtype=object),
            "cloud_ok": np.array(cloud_ok, dtype=bool),
            "cx_cm": cent[:, 0],
            "cy_cm": cent[:, 1],
            "cz_cm": cent[:, 2],
        },
        columns=FACT_COLUMNS,
    )
    return facts, picked[:next_row]


def _check_columns(schema: pa.Schema) -> None:
    """Raise ValueError unless the three scanned columns exist with their FRAMES_SCHEMA types."""
    for name in ("frame_id", "timestamp_ns", "points"):
        if name not in schema.names:
            raise ValueError(f"column {name} is missing from the frames file")
        actual = schema.field(name).type
        if not actual.equals(FRAMES_SCHEMA.field(name).type):
            raise ValueError(
                f"column {name} has type {actual}, expected {FRAMES_SCHEMA.field(name).type}"
            )


def _checked_cloud(
    buf: pa.Buffer,
    expected: NDArray[np.int32],
    expected_bytes: int,
    offset_lo: int,
    offset_hi: int,
) -> NDArray[np.int32] | None:
    """Return the (n, 3) int32 points (cm) of a good payload, else None; never raises on bytes.

    expected_bytes is the payload size of a good cloud; any other size fails before decoding.
    offset_lo and offset_hi (cm) are the smallest and largest value of expected. The returned
    array is a read-only view of buf.
    """
    if buf.size != expected_bytes or expected_bytes == 0:
        return None
    points = np.frombuffer(buf, dtype="<i4").reshape(-1, 3)
    flat = points.reshape(-1)
    low, high = int(flat.min()), int(flat.max())  # cm
    if low - offset_hi >= _INT32_MIN and high - offset_lo <= _INT32_MAX:
        deviation = np.subtract(points, expected)  # int32 cannot wrap here
    else:
        deviation = np.subtract(points, expected, dtype=np.int64)
    dev = deviation.reshape(-1)
    for axis in range(3):  # strided 1-D reductions are much faster than axis=0 on (n, 3)
        column = dev[axis::3]
        if int(column.max()) - int(column.min()) > _MAX_SPREAD_CM:
            return None
    return points.astype(np.int32, copy=False)


def _centroid(points: NDArray[np.int32]) -> tuple[float, float, float]:
    """Mean of the points per axis in cm (exact integer sum divided by the count)."""
    flat = points.reshape(-1)
    n = points.shape[0]
    return (
        float(flat[0::3].sum(dtype=np.int64)) / n,
        float(flat[1::3].sum(dtype=np.int64)) / n,
        float(flat[2::3].sum(dtype=np.int64)) / n,
    )


def _sha256_hex(buf: pa.Buffer) -> str:
    """Hex SHA-256 of the buffer's bytes (hashlib releases the GIL, so this can run in a thread)."""
    return hashlib.sha256(buf).hexdigest()
