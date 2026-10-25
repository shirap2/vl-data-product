"""Evaluate a drive's motion table against its ground truth. The only reader of ground truth.

Units: positions in cm, velocity in cm/s, acceleration in cm/s^2, windows in frames.
"""

import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pyarrow as pa
from numpy.typing import NDArray
from pyarrow.parquet import ParquetFile, read_table

from vl_data.core.config import Config
from vl_data.core.manifest import Manifest, read_manifest, verify_drive
from vl_data.core.schemas import GROUND_TRUTH_SCHEMA
from vl_data.sdk.client import VLClient
from vl_data.usecase.config import MotionConfig

_TRUTH_NAME = "ground_truth.parquet"
_ACC_CHANGE_REL = 1e-9  # relative to max(1, largest true |acceleration|); smaller is no change
_MAX_PROBLEMS_SHOWN = 5  # verify_drive problems named in the message
_MAX_TEXT = 100  # characters kept of each outside text in a message


@dataclass(frozen=True)
class MetricErrors:
    """Error of one metric: largest absolute error and RMS error, in the metric's unit.

    The unit is that of the metric (cm, cm/s or cm/s^2). Both are NaN when no frame has both
    an estimate and a true value.
    """

    max_abs: float  # in the metric's unit (cm, cm/s or cm/s^2)
    rms: float  # in the metric's unit


@dataclass(frozen=True)
class EvalReport:
    """Result of evaluate_drive. Counts are frames; errors are estimate minus truth."""

    scenario_hash: str
    frames_compared: int  # frames present in both the motion table and the ground truth
    frames_null: int  # compared frames whose velocity or acceleration is null
    frames_interior: int  # compared frames that are interior
    position_cm: MetricErrors  # cm, interior frames
    velocity_cm_s: MetricErrors  # cm/s, interior frames with an estimate
    acceleration_cm_s2: MetricErrors  # cm/s^2, interior frames with an estimate
    velocity_all_cm_s: MetricErrors  # cm/s, all compared frames with an estimate
    acceleration_all_cm_s2: MetricErrors  # cm/s^2, all compared frames with an estimate
    passed: bool  # at least one interior frame and interior max errors within tolerances
    # One row per compared frame, ordered by frame_id: frame_id, interior, x_est_cm, x_true_cm,
    # velocity_est_cm_s (nullable), velocity_true_cm_s, acceleration_est_cm_s2 (nullable),
    # acceleration_true_cm_s2. Positions cm, velocities cm/s, accelerations cm/s^2. Not stored.
    frames: pa.Table


class EvaluationError(Exception):
    """The drive cannot be evaluated.

    Causes: the drive fails verify_drive; the lineage row is missing, duplicated, not "ok" or its
    scenario_hash or ground_truth_sha256 differs from the manifest; the motion table is missing;
    the ground truth or motion row count differs from the manifest frame count; their frame ids
    differ; the ground truth has the wrong columns; or reading a file fails.
    """


def max_abs_rms(estimate: NDArray[np.float64], truth: NDArray[np.float64]) -> MetricErrors:
    """Max and RMS of estimate minus truth, in the unit of the inputs (cm, cm/s or cm/s^2).

    NaN for both when there are no frames.
    """
    if estimate.size == 0:
        return MetricErrors(float("nan"), float("nan"))
    diff = estimate - truth
    return MetricErrors(float(np.max(np.abs(diff))), float(np.sqrt(np.mean(diff**2))))


def _short(problem: str) -> str:
    """A verify_drive problem as a short quoted name, without detail text (which may hold paths)."""
    head = problem.split(" (")[0]
    if head.startswith("cannot read manifest.json"):
        head = "cannot read manifest.json"
    return repr(head[:_MAX_TEXT])


def _check_lineage(
    client: VLClient, drive_id: str, scenario_hash: str, truth_sha256: str | None
) -> None:
    """Raise EvaluationError unless one "ok" lineage row matches the manifest hashes."""
    lineage = client.lineage(drive_id)
    if lineage.num_rows == 0:
        raise EvaluationError(f"drive {drive_id!r} has no lineage row; ingest it first")
    if lineage.num_rows != 1:
        raise EvaluationError(
            f"drive {drive_id!r} has {lineage.num_rows} lineage rows; exactly one is required"
        )
    status = lineage.column("status")[0].as_py()
    if status != "ok":
        raise EvaluationError(f"drive {drive_id!r} has lineage status {status!r}, not 'ok'")
    if lineage.column("scenario_hash")[0].as_py() != scenario_hash:
        raise EvaluationError(f"drive {drive_id!r}: lineage scenario_hash differs from manifest")
    if truth_sha256 is None or lineage.column("ground_truth_sha256")[0].as_py() != truth_sha256:
        raise EvaluationError(
            f"drive {drive_id!r}: lineage ground_truth_sha256 differs from manifest"
        )


def _load_truth(path: Path, expected_rows: int) -> pa.Table:
    """Read the ground truth file (cm, cm/s, cm/s^2), ordered by frame_id, after checks.

    The footer row count must equal expected_rows (frames) and the columns must match
    GROUND_TRUTH_SCHEMA by name and type; otherwise EvaluationError.
    """
    handle = ParquetFile(path)
    try:
        rows = int(handle.metadata.num_rows)
    finally:
        handle.close()
    if rows != expected_rows:
        raise EvaluationError(f"ground truth has {rows} rows; the manifest implies {expected_rows}")
    table = read_table(path)
    for field in GROUND_TRUTH_SCHEMA:
        if (
            field.name not in table.schema.names
            or table.schema.field(field.name).type != field.type
        ):
            raise EvaluationError(f"ground truth column {field.name!r} is missing or wrong type")
    return table.select(GROUND_TRUTH_SCHEMA.names).sort_by("frame_id")


def _interior_mask(
    frame_id: NDArray[np.int64],
    truth_ids: NDArray[np.int64],
    truth_acc: NDArray[np.float64],
    half_window: float,
) -> NDArray[np.bool_]:
    """True for frames whose window holds no change of true acceleration; never the last frame.

    frame_id: ids of the compared frames; truth_ids and truth_acc (cm/s^2) cover all truth
    frames, ordered. A change is at id k when acceleration differs from that of the previous
    frame by more than 1e-9 * max(1, largest |acceleration|) cm/s^2. The window of frame f holds
    the ids within half_window frames either side of f.
    """
    threshold = _ACC_CHANGE_REL * max(1.0, float(np.max(np.abs(truth_acc))))  # cm/s^2
    changed = np.abs(np.diff(truth_acc)) > threshold
    interior = np.ones(frame_id.size, dtype=bool)
    for k in truth_ids[1:][changed]:
        interior &= np.abs(frame_id - k) > half_window
    interior &= frame_id != truth_ids[-1]  # the truth repeats the previous acceleration here
    return interior


def interior_mask(
    truth_acceleration: NDArray[np.float64], half_window_frames: float
) -> NDArray[np.bool_]:
    """Interior frames of a drive whose frame ids are 0, 1, 2, ... (the sweep's definition).

    truth_acceleration: true acceleration in cm/s^2, one value per frame, in frame order.
    half_window_frames: half the window length, in frames. The result is the same mask
    evaluate_drive uses (no change of true acceleration within half_window_frames of the
    frame; the last frame is never interior), so the sweep and the evaluation agree.
    """
    ids = np.arange(truth_acceleration.size, dtype=np.int64)
    return _interior_mask(ids, ids, truth_acceleration, half_window_frames)


def _estimate(table: pa.Table, name: str) -> tuple[NDArray[np.float64], NDArray[np.bool_]]:
    """Values of a motion column and a mask of valid ones (not null and not NaN)."""
    column = table.column(name)
    values = column.fill_null(np.nan).to_numpy()
    valid = column.is_valid().to_numpy(zero_copy_only=False) & np.isfinite(values)
    return values, valid


def _report(
    manifest: Manifest, truth: pa.Table, motion: pa.Table, config: Config, cfg: MotionConfig
) -> EvalReport:
    """Build the report from the checked truth and motion tables (same frame ids, ordered)."""
    truth_ids = truth.column("frame_id").to_numpy()
    truth_acc = truth.column("acceleration_cm_s2").to_numpy()
    offset_cm = (manifest.body.size_cm - 1) / 2  # cm
    x_est = motion.column("x_cm").to_numpy() - offset_cm
    x_true = truth.column("x_cm").to_numpy()
    v_est, v_ok = _estimate(motion, "velocity_cm_s")
    a_est, a_ok = _estimate(motion, "acceleration_cm_s2")
    v_true = truth.column("velocity_cm_s").to_numpy()

    interior = _interior_mask(truth_ids, truth_ids, truth_acc, cfg.window_frames / 2)
    v_in = interior & v_ok
    a_in = interior & a_ok
    position = max_abs_rms(x_est[interior], x_true[interior])
    velocity = max_abs_rms(v_est[v_in], v_true[v_in])
    acceleration = max_abs_rms(a_est[a_in], truth_acc[a_in])
    frames_interior = int(interior.sum())
    passed = bool(
        frames_interior > 0
        and position.max_abs <= config.tolerances.position_cm
        and velocity.max_abs <= cfg.tolerances.velocity_cm_s
        and acceleration.max_abs <= cfg.tolerances.acceleration_cm_s2
    )
    frames = pa.table(
        {
            "frame_id": pa.array(truth_ids, pa.int64()),
            "interior": pa.array(interior, pa.bool_()),
            "x_est_cm": pa.array(x_est, pa.float64()),
            "x_true_cm": pa.array(x_true, pa.float64()),
            "velocity_est_cm_s": pa.array(v_est, pa.float64(), mask=~v_ok),
            "velocity_true_cm_s": pa.array(v_true, pa.float64()),
            "acceleration_est_cm_s2": pa.array(a_est, pa.float64(), mask=~a_ok),
            "acceleration_true_cm_s2": pa.array(truth_acc, pa.float64()),
        }
    )
    return EvalReport(
        scenario_hash=manifest.scenario_hash,
        frames_compared=int(truth_ids.size),
        frames_null=int(np.count_nonzero(~(v_ok & a_ok))),
        frames_interior=frames_interior,
        position_cm=position,
        velocity_cm_s=velocity,
        acceleration_cm_s2=acceleration,
        velocity_all_cm_s=max_abs_rms(v_est[v_ok], v_true[v_ok]),
        acceleration_all_cm_s2=max_abs_rms(a_est[a_ok], truth_acc[a_ok]),
        passed=passed,
        frames=frames,
    )


def _compare(
    drive_dir: Path,
    drive_id: str,
    manifest: Manifest,
    client: VLClient,
    config: Config,
    cfg: MotionConfig,
) -> EvalReport:
    """Everything after verify_drive; may raise EvaluationError or a read error."""
    truth_entry = manifest.files.get(_TRUTH_NAME)
    _check_lineage(
        client, drive_id, manifest.scenario_hash, truth_entry.sha256 if truth_entry else None
    )
    motion = client.motion(drive_id)
    if motion.num_rows == 0:
        raise EvaluationError(f"drive {drive_id!r} has no motion rows; run motion first")
    if motion.num_rows != manifest.frame_count:
        raise EvaluationError(
            f"motion has {motion.num_rows} rows; the manifest implies {manifest.frame_count}"
        )
    truth = _load_truth(drive_dir / _TRUTH_NAME, manifest.frame_count)
    motion = motion.sort_by("frame_id")
    truth_ids = truth.column("frame_id").to_numpy()
    motion_ids = motion.column("frame_id").to_numpy()
    strictly_increasing = bool(np.all(np.diff(truth_ids) > 0) and np.all(np.diff(motion_ids) > 0))
    if not strictly_increasing or not np.array_equal(truth_ids, motion_ids):
        raise EvaluationError(
            f"motion ({motion_ids.size} rows) and ground truth ({truth_ids.size} rows) "
            "have different frame ids"
        )
    return _report(manifest, truth, motion, config, cfg)


def evaluate_drive(
    drive_dir: Path, client: VLClient, config: Config, cfg: MotionConfig
) -> EvalReport:
    """Compare the motion table of a drive with its ground truth. Errors are estimate - truth.

    Order: verify_drive (files and hashes), then the lineage checks (one "ok" row, scenario_hash
    and ground_truth_sha256 equal to the manifest), then the motion table, and only then the
    ground truth file is opened. Any failure, including a read error, raises EvaluationError
    (messages hold the exception type name, never paths or manifest text).
    The drive id is the folder name of the absolute drive_dir. The motion table and the ground
    truth must both have manifest.frame_count rows and the same frame ids; nothing is dropped.
    The stored motion x_cm is the raw centroid, which sits (body.size_cm - 1) / 2 cm (the
    centroid offset) ahead of the truth position along +x, so x_est_cm = x_cm - offset is
    compared with the true x (cm).
    A frame is interior when no change of true acceleration lies within cfg.window_frames / 2
    frames of it, and the last frame never is. Position errors use interior frames; velocity and
    acceleration errors use frames with an estimate (not null, not NaN), interior
    (velocity_cm_s, acceleration_cm_s2) and all (velocity_all_cm_s, acceleration_all_cm_s2).
    passed needs frames_interior > 0 and interior max errors within config.tolerances.position_cm
    (cm), cfg.tolerances.velocity_cm_s (cm/s) and cfg.tolerances.acceleration_cm_s2 (cm/s^2).
    Nothing is stored.
    """
    drive_id = "?"
    try:
        drive_id = Path(os.path.abspath(drive_dir)).name
        verdict = verify_drive(drive_dir)
        manifest = read_manifest(drive_dir) if verdict.ok else None
    except (OSError, ValueError) as exc:
        raise EvaluationError(f"cannot verify drive {drive_id!r}: {type(exc).__name__}") from exc
    if manifest is None:
        shown = "; ".join(_short(p) for p in verdict.problems[:_MAX_PROBLEMS_SHOWN])
        raise EvaluationError(f"drive {drive_id!r} failed verification: {shown}")
    try:
        return _compare(drive_dir, drive_id, manifest, client, config, cfg)
    except (OSError, ValueError, KeyError, pa.ArrowException, MemoryError) as exc:
        raise EvaluationError(
            f"drive {drive_id!r}: evaluation failed ({type(exc).__name__})"
        ) from exc
