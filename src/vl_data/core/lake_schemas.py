"""Arrow schemas for the lake tables (keypoints, quarantine, lineage, motion)."""

import pyarrow as pa

# One row per (frame, keypoint); timestamp in ns, coordinates in cm.
KEYPOINTS_SCHEMA: pa.Schema = pa.schema(
    [
        pa.field("frame_id", pa.int64(), nullable=False),
        pa.field("timestamp_ns", pa.int64(), nullable=False),
        pa.field("keypoint", pa.string(), nullable=False),
        pa.field("x_cm", pa.float64(), nullable=False),
        pa.field("y_cm", pa.float64(), nullable=False),
        pa.field("z_cm", pa.float64(), nullable=False),
    ]
)

# One row per rejected frame or row; row_index is a 0-based row position in the source file.
QUARANTINE_SCHEMA: pa.Schema = pa.schema(
    [
        pa.field("frame_id", pa.int64(), nullable=True),
        pa.field("row_index", pa.int64(), nullable=True),
        pa.field("code", pa.string(), nullable=False),
        pa.field("message", pa.string(), nullable=False),
    ]
)

# One row per load; every frames_* and keypoint_rows value is a count (frames or rows).
LINEAGE_SCHEMA: pa.Schema = pa.schema(
    [
        pa.field("scenario_hash", pa.string(), nullable=True),
        pa.field("schema_version", pa.int32(), nullable=True),
        pa.field("status", pa.string(), nullable=False),
        pa.field("frames_expected", pa.int64(), nullable=False),
        pa.field("frames_in", pa.int64(), nullable=False),
        pa.field("frames_kept", pa.int64(), nullable=False),
        pa.field("frames_rejected", pa.int64(), nullable=False),
        pa.field("frames_missing", pa.int64(), nullable=False),
        pa.field("keypoint_rows", pa.int64(), nullable=False),
        pa.field("frames_sha256", pa.string(), nullable=True),
        pa.field("ground_truth_sha256", pa.string(), nullable=True),
    ]
)

# One row per frame of a drive's centroid; timestamp in ns, x in cm, velocity in cm/s,
# acceleration in cm/s^2. Velocity and acceleration are null where there is no estimate.
# n_points is the number of samples in the estimator window (a count).
MOTION_SCHEMA: pa.Schema = pa.schema(
    [
        pa.field("frame_id", pa.int64(), nullable=False),
        pa.field("timestamp_ns", pa.int64(), nullable=False),
        pa.field("x_cm", pa.float64(), nullable=False),
        pa.field("velocity_cm_s", pa.float64(), nullable=True),
        pa.field("acceleration_cm_s2", pa.float64(), nullable=True),
        pa.field("n_points", pa.int32(), nullable=False),
    ]
)

# Table name -> schema. A plain dict so later modules can add entries.
TABLE_SCHEMAS: dict[str, pa.Schema] = {
    "keypoints": KEYPOINTS_SCHEMA,
    "quarantine": QUARANTINE_SCHEMA,
    "lineage": LINEAGE_SCHEMA,
    "motion": MOTION_SCHEMA,
}
