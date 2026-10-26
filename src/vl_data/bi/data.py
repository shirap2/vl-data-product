"""Data layer of the BI app: drive list, quality, evaluation outcome, metric rows, commands.

Pure functions over Arrow tables and the SDK; no Streamlit import and no lake write.
Units: frames and rows are counts; positions cm; velocities cm/s; accelerations cm/s^2.
"""

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa

from vl_data.core.config import Config
from vl_data.core.lake import Lake
from vl_data.sdk.client import VLClient
from vl_data.usecase.config import MotionConfig
from vl_data.usecase.evaluate import EvalReport, EvaluationError, MetricErrors, evaluate_drive

QUARANTINE_ROWS_SHOWN: int = 200  # rows

# Same rule as the lake's private _DRIVE_RE (core/lake.py): checked before any path is built.
_DRIVE_RE = re.compile(r"[A-Za-z0-9_-]+")


@dataclass(frozen=True)
class DriveQuality:
    """Lineage and quarantine summary of one drive. Counts are frames or rows."""

    drive: str
    scenario_hash: str | None
    status: str  # "ok" or "rejected"
    frames_expected: int
    frames_in: int
    frames_kept: int
    frames_rejected: int  # quarantine rows that name a file row
    frames_missing: int  # VL-001 rows
    keypoint_rows: int
    quarantine_by_code: dict[str, int]  # quarantine rows per code, keys sorted


@dataclass(frozen=True)
class AnalyzeOutcome:
    """Result of one Analyze click: a report, or a problem with the command that fixes it."""

    drive: str
    report: EvalReport | None
    problem: str | None  # plain text, never a file path
    command: str | None  # PowerShell command that fixes the problem, when there is one


def _check_drive(drive: str) -> None:
    """Raise ValueError unless drive matches the lake's drive-id rule [A-Za-z0-9_-]+."""
    if not _DRIVE_RE.fullmatch(drive):
        raise ValueError("invalid drive id: use letters, digits, '_' and '-' only")


def list_lake_drives(lake: Lake) -> list[str]:
    """Drive ids that have a lineage row, sorted (includes rejected drives)."""
    return lake.list_drives("lineage")


def load_quality(client: VLClient, drive: str) -> DriveQuality | None:
    """Return the drive's quality, or None when it has no lineage row; ValueError if more than one.

    Counts are frames or rows. quarantine_by_code counts quarantine rows per code (keys sorted);
    a rejected drive can carry codes outside VL-001 to VL-006 (for example INTEGRITY).
    """
    _check_drive(drive)
    lineage = client.lineage(drive)
    if lineage.num_rows == 0:
        return None
    if lineage.num_rows > 1:
        raise ValueError(f"drive {drive!r} has {lineage.num_rows} lineage rows, expected one")
    row = lineage.to_pylist()[0]
    codes = Counter(client.quarantine(drive).column("code").to_pylist())
    return DriveQuality(
        drive=drive,
        scenario_hash=row["scenario_hash"],
        status=row["status"],
        frames_expected=row["frames_expected"],
        frames_in=row["frames_in"],
        frames_kept=row["frames_kept"],
        frames_rejected=row["frames_rejected"],
        frames_missing=row["frames_missing"],
        keypoint_rows=row["keypoint_rows"],
        quarantine_by_code={code: codes[code] for code in sorted(codes)},
    )


def _ps_quote(path: Path) -> str:
    """PowerShell single-quoted literal of a path: no expansion; quote characters doubled.

    PowerShell treats the straight quote and U+2018, U+2019, U+201A and U+201B as single quotes.
    """
    text = str(path)
    for quote in ("'", "‘", "’", "‚", "‛"):
        text = text.replace(quote, quote + quote)
    return "'" + text + "'"


def ingest_command(raw_dir: Path, drive: str, lake_dir: Path) -> str:
    """Text of the PowerShell command that ingests the raw drive folder into the given lake."""
    _check_drive(drive)
    return f"python -m vl_data ingest {_ps_quote(raw_dir / drive)} --lake-dir {_ps_quote(lake_dir)}"


def motion_command(drive: str, lake_dir: Path) -> str:
    """Text of the PowerShell command that computes the motion table of a drive in the lake."""
    _check_drive(drive)
    return f"python -m vl_data motion {drive} --lake-dir {_ps_quote(lake_dir)}"


def _is_link(entry: Path) -> bool:
    """True for a symlink, or a Windows junction (junction detection needs Python 3.12+).

    Repeats the rule of the evaluate command (usecase/cli.py), whose helper is private.
    """
    is_junction = getattr(entry, "is_junction", None)
    return entry.is_symlink() or bool(is_junction and is_junction())


def analyze_drive(
    client: VLClient,
    config: Config,
    cfg: MotionConfig,
    raw_dir: Path,
    lake_dir: Path,
    drive: str,
) -> AnalyzeOutcome:
    """Evaluate a drive against its ground truth; every failure becomes a problem text.

    Needs a non-empty motion table (else the problem names motion_command) and a real raw
    folder raw_dir/drive that is not, and holds no, symlink or junction (else ingest_command).
    Problem texts never hold a file path. Nothing is written to the lake.
    """
    try:
        _check_drive(drive)
    except ValueError as error:
        return AnalyzeOutcome(drive, None, str(error), None)
    try:
        has_motion = client.motion(drive).num_rows > 0
    except (OSError, pa.ArrowException) as error:
        return AnalyzeOutcome(drive, None, f"cannot read the motion table: {_kind(error)}", None)
    command = ingest_command(raw_dir, drive, lake_dir)
    if not has_motion:
        return _without_motion(client, lake_dir, drive, command)
    drive_dir = raw_dir / drive
    if not drive_dir.is_dir():
        return AnalyzeOutcome(drive, None, f"raw folder of drive {drive!r} not found", command)
    try:
        linked = _is_link(drive_dir) or any(_is_link(entry) for entry in drive_dir.iterdir())
    except OSError as error:
        problem = f"cannot list the raw folder of drive {drive!r}: {_kind(error)}"
        return AnalyzeOutcome(drive, None, problem, command)
    if linked:
        problem = f"the raw folder of drive {drive!r} is, or holds, a link; use the real folder"
        return AnalyzeOutcome(drive, None, problem, command)
    try:
        report = evaluate_drive(drive_dir, client, config, cfg)
    except EvaluationError as error:
        return AnalyzeOutcome(drive, None, str(error), None)
    except (OSError, ValueError, pa.ArrowException) as error:
        problem = f"drive {drive!r}: evaluation failed ({_kind(error)})"
        return AnalyzeOutcome(drive, None, problem, None)
    return AnalyzeOutcome(drive, report, None, None)


def _without_motion(client: VLClient, lake_dir: Path, drive: str, ingest: str) -> AnalyzeOutcome:
    """Outcome for a drive with no motion table; only an ingested ok drive is fixed by motion."""
    try:
        lineage = client.lineage(drive)
    except (OSError, ValueError, pa.ArrowException) as error:
        return AnalyzeOutcome(drive, None, f"cannot read the lineage: {_kind(error)}", None)
    if lineage.num_rows > 1:
        problem = f"drive {drive!r} has unexpected lineage rows ({lineage.num_rows})"
        return AnalyzeOutcome(drive, None, problem, None)
    if lineage.num_rows == 0:
        return AnalyzeOutcome(drive, None, f"drive {drive!r} is not ingested", ingest)
    if lineage.column("status").to_pylist() != ["ok"]:
        problem = f"drive {drive!r} was rejected at ingest; fix the raw data and re-ingest"
        return AnalyzeOutcome(drive, None, problem, ingest)
    problem = f"drive {drive!r} has no motion table; compute it first"
    return AnalyzeOutcome(drive, None, problem, motion_command(drive, lake_dir))


def _kind(error: Exception) -> str:
    """Exception type name only: messages of file errors can hold paths."""
    return type(error).__name__


def read_problem(error: Exception) -> str:
    """Safe text for a failed read: the fixed message of a ValueError, else the type name only.

    pa.ArrowInvalid is also a ValueError but can hold a file path, so it gets the type name.
    """
    if isinstance(error, ValueError) and not isinstance(error, pa.ArrowException):
        return str(error)
    return _kind(error)


def _row(
    metric: str,
    unit: str,
    interior: MetricErrors,
    overall: MetricErrors | None,
    tolerance: float,
) -> dict[str, object]:
    return {
        "metric": metric,
        "unit": unit,
        "interior_max": interior.max_abs,
        "interior_rms": interior.rms,
        "all_max": None if overall is None else overall.max_abs,
        "all_rms": None if overall is None else overall.rms,
        "tolerance": tolerance,
        "within": bool(interior.max_abs <= tolerance),  # False when NaN (no interior frame)
    }


def metric_rows(report: EvalReport, config: Config, cfg: MotionConfig) -> list[dict[str, object]]:
    """Rows for the metrics table: metric, unit, interior and all-frame max and RMS, tolerance.

    Each row also has within (interior max <= tolerance).

    Errors are estimate minus truth in the row's unit (cm, cm/s or cm/s^2). Position has
    interior frames only (all_max and all_rms are None). within is interior max <= tolerance,
    False when the error is NaN. The overall PASS or FAIL is report.passed (it also needs at
    least one interior frame).
    """
    return [
        _row("position", "cm", report.position_cm, None, config.tolerances.position_cm),
        _row(
            "velocity",
            "cm/s",
            report.velocity_cm_s,
            report.velocity_all_cm_s,
            cfg.tolerances.velocity_cm_s,
        ),
        _row(
            "acceleration",
            "cm/s^2",
            report.acceleration_cm_s2,
            report.acceleration_all_cm_s2,
            cfg.tolerances.acceleration_cm_s2,
        ),
    ]
