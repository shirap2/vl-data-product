"""The cube body: a point lattice. Units: cm."""

import numpy as np
from numpy.typing import NDArray

_INT32_MIN = float(np.iinfo(np.int32).min)
_INT32_MAX = float(np.iinfo(np.int32).max)


def cube_lattice(size_cm: int, points_per_axis: int) -> NDArray[np.int32]:
    """Return the (points_per_axis**3, 3) cube lattice in cm, x-major, from 0 to size_cm - 1.

    Axis values are rint(linspace(0, size_cm - 1, points_per_axis)); the flat index is
    ix * n * n + iy * n + iz. Raises ValueError if size_cm < 1 or points_per_axis < 2 or
    points_per_axis > size_cm (distinct integer values need size_cm >= points_per_axis).
    """
    if size_cm < 1:
        raise ValueError(f"size_cm must be at least 1 cm, got {size_cm}")
    if points_per_axis < 2:
        raise ValueError(f"points_per_axis must be at least 2, got {points_per_axis}")
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
