"""Pass 1 of the ETL: one small "fact" row per row of frames.parquet. Units: cm, ns."""

import hashlib
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from numpy.typing import NDArray

from vl_data.core.schemas import FRAMES_SCHEMA, decode_points

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
_NO_CENTROID = (float("nan"), float("nan"), float("nan"))


def scan_frames(frames_path: Path, offsets: NDArray[np.int32], chunk_frames: int) -> pd.DataFrame:
    """Return one fact row per row of the frames file, in file order.

    offsets is the expected lattice in cm relative to its first point, shape (n, 3), so
    offsets[0] is (0, 0, 0). A cloud is OK when the payload length is a multiple of 12 bytes,
    decodes to exactly n points and, per axis, the deviation d = points - offsets has a spread
    (max - min) of at most 1 cm. Whole-cm rounding in the generator (round half to even in
    place_body) makes a 1 cm shape wobble normal, so this deliberately does not catch a single
    point moved by exactly 1 cm; it does catch a wrong count, a length that is not a multiple
    of 12, a point moved by 2 cm or more and a scrambled point order. The
    centroid (cm) is the mean of the decoded points when the cloud is OK, else NaN. A bad,
    empty or null payload never raises. Only frames_path is opened; ground truth is never read.
    Raises ValueError if chunk_frames < 1, if frame_id, timestamp_ns or points is missing or has
    another type, or if a frame_id or timestamp_ns is null (the message names the column and
    row). The caller must gate on VL-005 (the schema rule) before scanning.

    Memory: the file is streamed in batches of chunk_frames rows, so at most one batch of
    payloads is held at a time. pyarrow reads whole row groups, so the file's row-group size
    also bounds memory (the generator writes 1,000-row groups today and plans about 50 at
    full density). The returned facts are small: one row per frame.
    """
    if chunk_frames < 1:
        raise ValueError(f"chunk_frames must be at least 1, got {chunk_frames}")
    expected = offsets.astype(np.int64)
    expected_bytes = expected.shape[0] * _BYTES_PER_POINT

    row_index: list[int] = []
    frame_id: list[int] = []
    timestamp: list[int] = []
    n_bytes: list[int] = []
    sha: list[str] = []
    cloud_ok: list[bool] = []
    centroid: list[tuple[float, float, float]] = []

    next_row = 0
    with pq.ParquetFile(frames_path) as parquet:
        _check_columns(parquet.schema_arrow)
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
            for i in range(batch.num_rows):
                row_index.append(next_row + i)
                frame_id.append(ids[i])
                timestamp.append(stamps[i])
                scalar = payloads[i]
                buf = scalar.as_buffer() if scalar.is_valid else None
                if buf is None:  # null payload
                    buf = pa.py_buffer(b"")
                n_bytes.append(buf.size)
                sha.append(hashlib.sha256(buf).hexdigest())
                ok, mean = _check_cloud(buf, expected, expected_bytes)
                cloud_ok.append(ok)
                centroid.append(mean)
            next_row += batch.num_rows

    cent = np.array(centroid, dtype=np.float64).reshape(-1, 3)
    return pd.DataFrame(
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


def _check_cloud(
    buf: pa.Buffer, expected: NDArray[np.int64], expected_bytes: int
) -> tuple[bool, tuple[float, float, float]]:
    """Return (cloud_ok, centroid in cm) for one payload buffer; never raises on bad bytes.

    expected_bytes is the payload size of a good cloud; any other size fails before decoding.
    """
    if buf.size != expected_bytes or expected_bytes == 0:
        return False, _NO_CENTROID
    try:
        points = decode_points(buf)
    except ValueError:
        return False, _NO_CENTROID
    deviation = np.subtract(points, expected, dtype=np.int64)  # int64: cannot wrap
    spread = deviation.max(axis=0) - deviation.min(axis=0)  # cm per axis
    if (spread > _MAX_SPREAD_CM).any():
        return False, _NO_CENTROID
    mean = points.mean(axis=0, dtype=np.float64)  # cm
    return True, (float(mean[0]), float(mean[1]), float(mean[2]))
