"""Data layer of the BI app: drive list, quality, motion hint, SQL outcome, commands.

Pure functions over Arrow tables and the SDK; no Streamlit import and no lake write.
Units: frames and rows are counts; positions cm; velocities cm/s; accelerations cm/s^2.
"""

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import duckdb
import pyarrow as pa

from vl_data.core.lake import Lake
from vl_data.sdk.client import VLClient

QUARANTINE_ROWS_SHOWN: int = 200  # rows
SQL_ROWS_SHOWN: int = 10_000  # rows; display cap of the SQL tab, not a memory limit

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
class MotionHint:
    """Why a drive has no stored motion, with the PowerShell command that fixes it (if any)."""

    problem: str  # plain text, never a file path
    command: str | None  # PowerShell command that fixes the problem, when there is one
    can_compute: bool = False  # True only for exactly one lineage row with status ok


@dataclass(frozen=True)
class QueryOutcome:
    """Result of one SQL run: a table (at most SQL_ROWS_SHOWN rows), or a problem text."""

    table: pa.Table | None
    total_rows: int  # rows the query returned, before the display cap; 0 on a problem
    problem: str | None  # plain text, never a file path


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


def motion_hint(client: VLClient, raw_dir: Path, lake_dir: Path, drive: str) -> MotionHint:
    """Explain a drive with no stored motion; only an ingested ok drive is fixed by motion.

    Reads the lineage only (not the motion table) and writes nothing; never raises. The problem
    text holds no file path. The command is the ingest command (drive not ingested, or rejected
    at ingest), the motion command (ingested ok, no motion yet), or None (invalid drive id,
    unreadable lineage, or unexpected lineage rows).
    """
    try:
        _check_drive(drive)
    except ValueError as error:
        return MotionHint(str(error), None)
    try:
        lineage = client.lineage(drive)
        statuses = lineage.column("status").to_pylist()
    except (OSError, ValueError, KeyError, pa.ArrowException) as error:
        return MotionHint(f"cannot read the lineage: {_kind(error)}", None)
    if lineage.num_rows > 1:
        return MotionHint(f"drive {drive!r} has unexpected lineage rows ({lineage.num_rows})", None)
    ingest = ingest_command(raw_dir, drive, lake_dir)
    if lineage.num_rows == 0:
        return MotionHint(f"drive {drive!r} is not ingested", ingest)
    if statuses != ["ok"]:
        problem = f"drive {drive!r} was rejected at ingest; fix the raw data and re-ingest"
        return MotionHint(problem, ingest)
    problem = f"drive {drive!r} has no motion table; compute it first"
    return MotionHint(problem, motion_command(drive, lake_dir), can_compute=True)


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


def run_query(client: VLClient, query: str) -> QueryOutcome:
    """Run one SELECT through the SDK; every failure becomes a problem text without a file path.

    The first SQL_ROWS_SHOWN rows are kept for display (a slice after the query ran, not a
    memory limit); total_rows counts all returned rows. A zero-row result is a valid table.
    """
    try:
        result = client.sql(query)
    except duckdb.IOException as error:
        return QueryOutcome(None, 0, f"{_kind(error)}: cannot read a file")  # text can hold paths
    except duckdb.Error as error:
        first_line = str(error).splitlines()[0] if str(error) else ""
        if first_line and "/" not in first_line and "\\" not in first_line:
            return QueryOutcome(None, 0, first_line)
        return QueryOutcome(None, 0, _kind(error))
    except (ValueError, OSError, pa.ArrowException) as error:
        return QueryOutcome(None, 0, read_problem(error))
    shown = result.slice(0, SQL_ROWS_SHOWN).rename_columns(_unique_names(result.column_names))
    return QueryOutcome(shown, result.num_rows, None)


def _unique_names(names: list[str]) -> list[str]:
    """Column names without repeats: the first keeps its name, later ones get _1, _2, ... .

    A suffix that another column already uses is skipped, so no two names are equal.
    """
    taken = set(names)
    seen: set[str] = set()
    unique = []
    for name in names:
        if name in seen:
            number = 1
            while f"{name}_{number}" in taken:
                number += 1
            name = f"{name}_{number}"
            taken.add(name)
        seen.add(name)
        unique.append(name)
    return unique


def example_queries(drive: str) -> dict[str, str]:
    """Five example SELECTs for a drive, label to SQL, in a fixed order; ValueError for a bad id.

    Units sit in the column aliases: cm, cm/s. The drive id is checked before it enters the SQL.
    """
    _check_drive(drive)
    return {
        "Quarantine rows per code": (
            "SELECT code, count(*) AS rows_quarantined FROM quarantine "
            f"WHERE drive = '{drive}' GROUP BY code ORDER BY code"
        ),
        "Peak speed (cm/s)": (
            f"SELECT max(abs(velocity_cm_s)) AS peak_speed_cm_s FROM motion WHERE drive = '{drive}'"
        ),
        "Kept vs rejected frames": (
            "SELECT frames_expected, frames_in, frames_kept, frames_rejected, frames_missing "
            f"FROM lineage WHERE drive = '{drive}'"
        ),
        "First 20 centroid positions (cm)": (
            "SELECT frame_id, x_cm, y_cm, z_cm FROM keypoints "
            f"WHERE drive = '{drive}' AND keypoint = 'centroid' ORDER BY frame_id LIMIT 20"
        ),
        "All drives with status": (
            "SELECT drive, status, frames_expected, frames_kept, frames_rejected, frames_missing "
            "FROM lineage ORDER BY drive"
        ),
    }
