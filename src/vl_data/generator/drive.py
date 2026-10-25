"""In-memory drive: frames and ground truth tables. Units: cm, cm/s, cm/s^2, s, ns."""

from collections.abc import Iterator
from dataclasses import dataclass

import numpy as np
import pyarrow as pa
from numpy.typing import NDArray

from vl_data.core.config import Config
from vl_data.core.hashing import scenario_hash as compute_scenario_hash
from vl_data.core.schemas import (
    ARROW_BINARY_LIMIT,
    BYTES_PER_POINT,
    FRAMES_SCHEMA,
    GROUND_TRUTH_SCHEMA,
)
from vl_data.generator.body import cube_lattice, place_body
from vl_data.generator.models import Scenario
from vl_data.generator.motion import (
    frame_count,
    ground_truth_arrays,
    integrate_position,
    speed_profile,
)

# Frames per RecordBatch and per parquet row group. One Arrow binary array holds at most
# 2**31 - 1 bytes; 50 full frames (1,000,000 points x 12 bytes) are 600,000,000 bytes.
BATCH_FRAMES = 50
_ARROW_BINARY_LIMIT = ARROW_BINARY_LIMIT  # bytes: offsets of an Arrow binary array are int32
# Most bytes of points that generate() or write_drive() may hold in memory: 1 GiB, which is
# 89 full-density frames (12,000,000 bytes each) or 89,478 frames at 10 points per axis. The
# 2,000-frame cap at full density would be 24 GB; use generate_to_disk, which streams.
MAX_IN_MEMORY_BYTES = 2**30  # bytes
_HASH_DIR_CHARS = 12  # hex characters of the scenario hash used as the folder name


@dataclass(frozen=True)
class Drive:
    """A generated drive: the scenario, its hash and the two tables."""

    scenario: Scenario
    scenario_hash: str
    frames: pa.Table  # frame_id, timestamp_ns (ns), points (int32 cm, 12 bytes per point)
    ground_truth: pa.Table  # x in cm, velocity in cm/s, acceleration in cm/s^2


def check_batch_fits(config: Config) -> int:
    """Return the bytes of one frame (points_per_axis cubed x 12) if a batch of BATCH_FRAMES fits.

    Raises ValueError (before any allocation) when BATCH_FRAMES frames would not fit one Arrow
    binary array, whose size is limited to 2**31 - 1 bytes.
    """
    frame_bytes = config.body.points_per_axis**3 * BYTES_PER_POINT  # bytes
    if BATCH_FRAMES * frame_bytes >= _ARROW_BINARY_LIMIT:
        raise ValueError(
            f"a batch of {BATCH_FRAMES} frames of {frame_bytes} bytes is too large for one "
            f"Arrow binary array (limit {_ARROW_BINARY_LIMIT - 1} bytes)"
        )
    return frame_bytes


def check_in_memory_size(frames: int, frame_bytes: int) -> None:
    """Raise ValueError if frames x frame_bytes (bytes of points) is over MAX_IN_MEMORY_BYTES."""
    total = frames * frame_bytes  # bytes
    if total > MAX_IN_MEMORY_BYTES:
        raise ValueError(
            f"{frames} frames of {frame_bytes} bytes are {total} bytes of points, over the "
            f"in-memory limit of {MAX_IN_MEMORY_BYTES} bytes; use generate_to_disk, which streams"
        )


def drive_dir_name(scenario_hash: str) -> str:
    """First 12 hex characters of the scenario hash."""
    return scenario_hash[:_HASH_DIR_CHARS]


def _positions(
    scenario: Scenario, config: Config
) -> tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.float64]]:
    """Return (x in cm, velocity in cm/s, acceleration in cm/s^2), one value per frame."""
    fps = config.fps  # frames/s
    speed = speed_profile(scenario, fps)  # cm/s
    x_cm = integrate_position(scenario.start.x_cm, speed, fps)
    velocity, acceleration = ground_truth_arrays(x_cm, speed, fps)
    return x_cm, velocity, acceleration


def build_ground_truth(scenario: Scenario, config: Config) -> pa.Table:
    """Ground truth table (GROUND_TRUTH_SCHEMA) of a scenario; one row per frame, so the
    frame count is num_rows. Small (about 40 bytes per frame); needs no frames.

    x in cm, velocity in cm/s, acceleration in cm/s^2, timestamps in ns.
    """
    x_cm, velocity, acceleration = _positions(scenario, config)
    frame_ids = np.arange(len(x_cm), dtype=np.int64)
    timestamps = frame_ids * np.int64(config.frame_interval_ns)  # ns
    return pa.Table.from_arrays(
        [
            pa.array(frame_ids),
            pa.array(timestamps),
            pa.array(x_cm),
            pa.array(velocity),
            pa.array(acceleration),
        ],
        schema=GROUND_TRUTH_SCHEMA,
    )


def iter_frame_batches(scenario: Scenario, config: Config) -> Iterator[pa.RecordBatch]:
    """Yield the frames of a scenario as RecordBatches (FRAMES_SCHEMA) of at most BATCH_FRAMES.

    Only one batch is in memory at a time (600 MB at full density). Frame k is at timestamp
    k * config.frame_interval_ns (ns); the cube body is placed at the integrated x position
    (cm) with the scenario's start y and z (cm). Raises ValueError (on the first next) when a
    batch would not fit one Arrow binary array, when the lattice is invalid, or when a position
    is outside the int32 cm range (then after the earlier batches were yielded).
    """
    frame_bytes = check_batch_fits(config)  # bytes
    x_cm = _positions(scenario, config)[0]
    count = len(x_cm)  # frames
    lattice = cube_lattice(config.body.size_cm, config.body.points_per_axis)
    for first in range(0, count, BATCH_FRAMES):
        last = min(first + BATCH_FRAMES, count)
        rows = last - first
        data = np.empty((rows, len(lattice), 3), dtype="<i4")  # cm, same bytes as encode_points
        for row in range(rows):
            data[row] = place_body(
                lattice, float(x_cm[first + row]), scenario.start.y_cm, scenario.start.z_cm
            )
        offsets = np.arange(rows + 1, dtype=np.int32) * np.int32(frame_bytes)
        points = pa.Array.from_buffers(
            pa.binary(), rows, [None, pa.py_buffer(offsets), pa.py_buffer(data)]
        )
        frame_ids = np.arange(first, last, dtype=np.int64)
        yield pa.RecordBatch.from_arrays(
            [
                pa.array(frame_ids),
                pa.array(frame_ids * np.int64(config.frame_interval_ns)),  # ns
                points,
            ],
            schema=FRAMES_SCHEMA,
        )


def generate(scenario: Scenario, config: Config) -> Drive:
    """Build frames and ground truth for a scenario in memory; no file access.

    Frame k is at timestamp k * config.frame_interval_ns (ns); the cube body is placed at
    the integrated x position (cm) with the scenario's start y and z (cm). The frames come
    from iter_frame_batches (batches of BATCH_FRAMES = 50) and are collected into one table, so
    this is for small drives and tests: a full-density frame is 12 MB, a 1,000-frame drive 12 GB.
    Use generate_to_disk to stream a large drive. The hash covers the scenario and config.body.
    Raises ValueError, before building anything, when the points would be over
    MAX_IN_MEMORY_BYTES (1 GiB).
    """
    frame_bytes = check_batch_fits(config)  # bytes
    check_in_memory_size(frame_count(scenario, config.fps), frame_bytes)
    frames = pa.Table.from_batches(list(iter_frame_batches(scenario, config)), schema=FRAMES_SCHEMA)
    ground_truth = build_ground_truth(scenario, config)
    return Drive(scenario, compute_scenario_hash(scenario, config.body), frames, ground_truth)
