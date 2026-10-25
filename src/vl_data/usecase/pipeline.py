"""Compute per-frame motion of a drive's centroid and store it in the lake.

Units: timestamps in ns, positions in cm, velocity in cm/s, acceleration in cm/s^2.
"""

from dataclasses import dataclass

import numpy as np
import pyarrow as pa

from vl_data.core.config import NS_PER_SECOND, Config
from vl_data.core.lake import Lake
from vl_data.core.lake_schemas import MOTION_SCHEMA
from vl_data.sdk.client import VLClient
from vl_data.usecase.config import MotionConfig
from vl_data.usecase.estimator import estimate_motion


@dataclass(frozen=True)
class MotionResult:
    """Counts of frames (not rows of other tables) for one drive."""

    drive: str
    frames_read: int  # centroid frames read
    frames_estimated: int  # frames with both velocity and acceleration
    frames_null: int  # frames where velocity or acceleration is null


class MotionError(Exception):
    """The drive cannot be processed (bad lineage, no or invalid position rows)."""


def compute_motion(
    client: VLClient, lake: Lake, drive: str, config: Config, cfg: MotionConfig
) -> MotionResult:
    """Estimate motion for one drive and replace its motion partition.

    Reads the drive's lineage and the cfg.position_keypoint rows, and writes
    frame_id, timestamp_ns (ns), x_cm (raw keypoint x, cm), velocity_cm_s (cm/s),
    acceleration_cm_s2 (cm/s^2) and n_points. NaN estimates are stored as null.
    Raises MotionError, writing nothing, when the lineage table does not have exactly one
    row, its status is not "ok", the drive has no rows for the position keypoint, the
    timestamps are not strictly increasing, or any x is not finite.
    """
    lineage = client.lineage(drive)
    if lineage.num_rows == 0:
        raise MotionError(f"drive {drive!r} has no lineage row; ingest it first")
    if lineage.num_rows != 1:
        raise MotionError(
            f"drive {drive!r} has {lineage.num_rows} lineage rows; exactly one is required"
        )
    status = lineage.column("status")[0].as_py()
    if status != "ok":
        raise MotionError(f"drive {drive!r} has status {status!r}; only status 'ok' is processed")
    rows = client.keypoints(drive, cfg.position_keypoint)
    if rows.num_rows == 0:
        raise MotionError(f"drive {drive!r} has no {cfg.position_keypoint!r} keypoint rows")

    frame_id = rows.column("frame_id").to_numpy()
    ts_ns = rows.column("timestamp_ns").to_numpy()
    x_cm = rows.column("x_cm").to_numpy()
    if ts_ns.size > 1 and not np.all(np.diff(ts_ns) > 0):
        raise MotionError(f"drive {drive!r} timestamps are not strictly increasing")
    if not np.all(np.isfinite(x_cm)):
        raise MotionError(f"drive {drive!r} has non-finite x values")
    # Subtract the first timestamp for accuracy; the estimates do not depend on the offset.
    t_s = (ts_ns - ts_ns[0]) / NS_PER_SECOND
    est = estimate_motion(t_s, x_cm, config.fps, cfg)

    vel_nan = np.isnan(est.velocity_cm_s)
    acc_nan = np.isnan(est.acceleration_cm_s2)
    batch = pa.RecordBatch.from_arrays(
        [
            pa.array(frame_id, pa.int64()),
            pa.array(ts_ns, pa.int64()),
            pa.array(x_cm, pa.float64()),
            pa.array(est.velocity_cm_s, pa.float64(), mask=vel_nan),
            pa.array(est.acceleration_cm_s2, pa.float64(), mask=acc_nan),
            pa.array(est.n_points, pa.int32()),
        ],
        schema=MOTION_SCHEMA,
    )
    lake.swap_partition("motion", drive, [batch])

    frames_read = int(rows.num_rows)
    frames_null = int(np.count_nonzero(vel_nan | acc_nan))
    return MotionResult(drive, frames_read, frames_read - frames_null, frames_null)
