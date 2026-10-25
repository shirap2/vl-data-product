"""The cube body: a point lattice. Units: cm."""

import numpy as np
from numpy.typing import NDArray

from vl_data.core.schemas import ARROW_BINARY_LIMIT, BYTES_PER_POINT, MAX_POINTS_PER_AXIS

_INT32_MIN = float(np.iinfo(np.int32).min)
_INT32_MAX = float(np.iinfo(np.int32).max)
_MAX_POINTS_PER_AXIS = MAX_POINTS_PER_AXIS  # count; shared with BodyConfig and BodyInfo


def cube_lattice(size_cm: int, points_per_axis: int) -> NDArray[np.int32]:
    """Return the (points_per_axis**3, 3) cube lattice in cm, x-major, from 0 to size_cm - 1.

    Axis values are rint(linspace(0, size_cm - 1, points_per_axis)); the flat index is
    ix * n * n + iy * n + iz. Raises ValueError if size_cm < 1 or points_per_axis < 2 or
    points_per_axis > size_cm (distinct integer values need size_cm >= points_per_axis), if
    points_per_axis > 1,000,000, or if the largest value size_cm - 1 is above the int32 range
    (it used to wrap silently), or if one frame of points_per_axis**3 points at 12 bytes each
    would reach the Arrow single-binary limit (2**31 bytes, so points_per_axis is at most 563).
    All bounds are checked before anything is allocated.
    """
    if size_cm < 1:
        raise ValueError(f"size_cm must be at least 1 cm, got {size_cm}")
    if points_per_axis < 2:
        raise ValueError(f"points_per_axis must be at least 2, got {points_per_axis}")
    if points_per_axis > _MAX_POINTS_PER_AXIS:
        raise ValueError(
            f"points_per_axis {points_per_axis} is above the limit {_MAX_POINTS_PER_AXIS}"
        )
    frame_bytes = int(points_per_axis) ** 3 * BYTES_PER_POINT  # bytes, exact Python int
    if frame_bytes >= ARROW_BINARY_LIMIT:
        raise ValueError(
            f"points_per_axis {points_per_axis} would give a frame of {frame_bytes} bytes, "
            f"at or above the Arrow limit of {ARROW_BINARY_LIMIT} bytes"
        )
    if size_cm - 1 > _INT32_MAX:
        raise ValueError(f"size_cm {size_cm} cm does not fit the int32 range (limit 2**31 cm)")
    if points_per_axis > size_cm:
        raise ValueError(
            f"points_per_axis {points_per_axis} > size_cm {size_cm} would give repeated points"
        )
    axis = np.rint(np.linspace(0, size_cm - 1, points_per_axis)).astype(np.int32)
    grid = np.stack(np.meshgrid(axis, axis, axis, indexing="ij"), axis=-1)
    return np.ascontiguousarray(grid.reshape(-1, 3), dtype=np.int32)


def place_body(
    lattice: NDArray[np.int32], x_cm: float, y_cm: float, z_cm: float
) -> NDArray[np.int32]:
    """Return rint(lattice + [x, y, z]) as int32 cm (the body placed at the given offset).

    Raises ValueError for a non-finite offset or a result outside the int32 range.
    """
    offset = np.array([x_cm, y_cm, z_cm], dtype=np.float64)  # cm
    if not np.isfinite(offset).all():
        raise ValueError(f"position must be finite, got {x_cm}, {y_cm}, {z_cm} cm")
    placed = np.rint(lattice.astype(np.float64) + offset)
    if placed.min() < _INT32_MIN or placed.max() > _INT32_MAX:
        raise ValueError("body position is outside the int32 cm range")
    return np.ascontiguousarray(placed.astype(np.int32))
