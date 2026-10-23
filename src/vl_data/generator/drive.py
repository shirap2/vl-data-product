"""In-memory drive: frames and ground truth tables. Units: cm, cm/s, cm/s^2, s, ns."""

from dataclasses import dataclass

import numpy as np
import pyarrow as pa

from vl_data.core.config import Config
from vl_data.core.hashing import scenario_hash as compute_scenario_hash
from vl_data.core.schemas import FRAMES_SCHEMA, GROUND_TRUTH_SCHEMA, encode_points
from vl_data.generator.body import cube_lattice, place_body
from vl_data.generator.models import Scenario
from vl_data.generator.motion import ground_truth_arrays, integrate_position, speed_profile

# frames per RecordBatch: limits per-batch Python objects; the finished table is full size
_BATCH_FRAMES = 1000
_HASH_DIR_CHARS = 12  # hex characters of the scenario hash used as the folder name


@dataclass(frozen=True)
class Drive:
    """A generated drive: the scenario, its hash and the two tables."""

    scenario: Scenario
    scenario_hash: str
    frames: pa.Table  # frame_id, timestamp_ns (ns), points (int32 cm, 12 bytes per point)
    ground_truth: pa.Table  # x in cm, velocity in cm/s, acceleration in cm/s^2


def drive_dir_name(scenario_hash: str) -> str:
    """First 12 hex characters of the scenario hash."""
    return scenario_hash[:_HASH_DIR_CHARS]


def generate(scenario: Scenario, config: Config) -> Drive:
    """Build frames and ground truth for a scenario; no file access.

    Frame k is at timestamp k * config.frame_interval_ns (ns); the cube body is placed at
    the integrated x position (cm) with the scenario's start y and z (cm). Frames are built
    in batches of 1000 to limit per-batch Python objects; the finished table is still full size.
    """
    fps = config.fps  # frames/s
    interval_ns = config.frame_interval_ns  # ns
    speed = speed_profile(scenario, fps)  # cm/s
    x_cm = integrate_position(scenario.start.x_cm, speed, fps)
    velocity, acceleration = ground_truth_arrays(x_cm, speed, fps)
    count = len(x_cm)  # frames
    frame_ids = np.arange(count, dtype=np.int64)
    timestamps = frame_ids * np.int64(interval_ns)  # ns
    lattice = cube_lattice(config.body.size_cm, config.body.points_per_axis)

    batches: list[pa.RecordBatch] = []
    for first in range(0, count, _BATCH_FRAMES):
        last = min(first + _BATCH_FRAMES, count)
        points = [
            encode_points(
                place_body(lattice, float(x_cm[k]), scenario.start.y_cm, scenario.start.z_cm)
            )
            for k in range(first, last)
        ]
        batches.append(
            pa.RecordBatch.from_arrays(
                [
                    pa.array(frame_ids[first:last]),
                    pa.array(timestamps[first:last]),
                    pa.array(points, type=pa.binary()),
                ],
                schema=FRAMES_SCHEMA,
            )
        )
    frames = pa.Table.from_batches(batches, schema=FRAMES_SCHEMA)
    ground_truth = pa.Table.from_arrays(
        [
            pa.array(frame_ids),
            pa.array(timestamps),
            pa.array(x_cm),
            pa.array(velocity),
            pa.array(acceleration),
        ],
        schema=GROUND_TRUTH_SCHEMA,
    )
    return Drive(scenario, compute_scenario_hash(scenario), frames, ground_truth)
