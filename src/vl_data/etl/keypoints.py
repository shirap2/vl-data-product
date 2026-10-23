"""Keypoint extraction from body point clouds. Units: cm."""

import numpy as np
from numpy.typing import NDArray

from vl_data.etl.config import KeypointSpec


def extract_keypoints(
    points: NDArray[np.integer], specs: list[KeypointSpec]
) -> NDArray[np.float64]:
    """Return the keypoints of each frame as a (frames, len(specs), 3) float64 array in cm.

    points is (frames, n_points, 3) of an integer dtype, in cm. Output follows the order
    of specs: an "index" spec picks points[:, index, :]; "centroid" is the float64 mean
    over points (computed once per call). Raises ValueError if points has a non-integer
    dtype, is not 3-D with last dimension 3, has no points, or a spec index is outside
    [0, n_points).
    """
    if not np.issubdtype(points.dtype, np.integer):
        raise ValueError(f"points must have an integer dtype, got {points.dtype}")
    if points.ndim != 3 or points.shape[2] != 3:
        raise ValueError(f"points must have shape (frames, n_points, 3), got {points.shape}")
    n_points = points.shape[1]
    if n_points == 0:
        raise ValueError("points has no points on axis 1")
    out = np.empty((points.shape[0], len(specs), 3), dtype=np.float64)
    centroid: NDArray[np.float64] | None = None
    for k, spec in enumerate(specs):
        if spec.kind == "centroid":
            if centroid is None:
                centroid = points.mean(axis=1, dtype=np.float64)
            out[:, k, :] = centroid
        else:
            index = spec.index
            if index is None or not 0 <= index < n_points:
                raise ValueError(
                    f"keypoint {spec.name!r}: index {index} out of range for {n_points} points"
                )
            out[:, k, :] = points[:, index, :]
    return out
