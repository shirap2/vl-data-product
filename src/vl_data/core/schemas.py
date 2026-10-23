"""Arrow schemas for the drive files and the point-cloud byte encoding."""

from typing import Any

import numpy as np
import pyarrow as pa
from numpy.typing import NDArray

SCHEMA_VERSION: int = 1

# One row per frame; points is the encode_points bytes of that frame.
FRAMES_SCHEMA: pa.Schema = pa.schema(
    [("frame_id", pa.int64()), ("timestamp_ns", pa.int64()), ("points", pa.binary())]
)

# One row per frame; x in cm, velocity in cm/s, acceleration in cm/s^2.
GROUND_TRUTH_SCHEMA: pa.Schema = pa.schema(
    [
        ("frame_id", pa.int64()),
        ("timestamp_ns", pa.int64()),
        ("x_cm", pa.float64()),
        ("velocity_cm_s", pa.float64()),
        ("acceleration_cm_s2", pa.float64()),
    ]
)

_BYTES_PER_POINT = 12  # three little-endian int32 values


def encode_points(points: NDArray[np.int32]) -> bytes:
    """Encode an (n, 3) integer array (cm) as little-endian int32 row-major bytes.

    Raises ValueError for a wrong shape, a non-integer dtype (floats are never
    cast silently) or values outside the int32 range.
    """
    arr: Any = np.asarray(points)
    if arr.ndim != 2 or arr.shape[1] != 3:
        raise ValueError(f"points must have shape (n, 3), got {arr.shape}")
    if not np.issubdtype(arr.dtype, np.integer):
        raise ValueError(f"points must have an integer dtype, got {arr.dtype}")
    info = np.iinfo(np.int32)
    if arr.size and (arr.min() < info.min or arr.max() > info.max):
        raise ValueError("points do not fit in int32")
    return np.ascontiguousarray(arr, dtype="<i4").tobytes()


def decode_points(buf: bytes) -> NDArray[np.int32]:
    """Decode bytes from encode_points into a writable (n, 3) int32 array (cm).

    Raises ValueError if len(buf) is not a multiple of 12. Checking n against the
    expected points per frame is the caller's job.
    """
    if len(buf) % _BYTES_PER_POINT != 0:
        raise ValueError(f"buffer length {len(buf)} is not a multiple of 12 bytes")
    arr = np.frombuffer(buf, dtype="<i4").reshape(-1, 3)
    return arr.astype(np.int32)
