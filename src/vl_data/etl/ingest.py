"""Ingest one drive: verify, validate, reduce to keypoints and swap the tables into the lake.

Units: cm, ns; fps in frames/s; sizes in bytes or MiB as named. The frames file is read once.
"""

import os
import re
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from numpy.typing import NDArray

from vl_data.core.config import NS_PER_SECOND
from vl_data.core.lake import Lake
from vl_data.core.lake_schemas import KEYPOINTS_SCHEMA, LINEAGE_SCHEMA, QUARANTINE_SCHEMA
from vl_data.core.manifest import Manifest, read_manifest, verify_drive
from vl_data.etl.body import drive_body, expected_frame_count
from vl_data.etl.config import EtlConfig
from vl_data.etl.facts import FACT_COLUMNS, check_keypoint_specs, scan_frames
from vl_data.etl.rules import RuleContext, run_rules
from vl_data.generator.body import cube_lattice

FRAMES_NAME = "frames.parquet"
INTEGRITY = "INTEGRITY"  # quarantine code of whole-drive problems that are not VL codes
_INT32_MAX = 2**31 - 1
_MAX_TEXT = 300  # characters of any outside text kept in a quarantine message
_SCAN_TEXT = 4096  # characters of outside text looked at before cleaning (bounds the work)
_MAX_PROBLEMS = 20  # whole-drive quarantine rows stored; more become one summary row
_HASH_RE = re.compile(r"[0-9a-f]{64}")  # a scenario hash: 64 lowercase hex characters
_ROWS_BASE = 1000  # rows; slack on top of 2 x the expected frames for the footer row-count cap


@dataclass(frozen=True)
class IngestResult:
    """What one ingest did. Counts are frames or rows."""

    drive: str  # folder name, the first 12 hex of the scenario hash for a good drive
    status: str  # "ok" or "rejected"
    frames_expected: int  # frames the scenario implies (0 when rejected)
    frames_in: int  # rows in the frames file (0 when rejected)
    frames_kept: int  # frames written to keypoints
    # Quarantine rows that name a file row (a row_index). A frame that breaks two rules has two
    # such rows and counts twice, so in general frames_in != frames_kept + frames_rejected.
    frames_rejected: int
    frames_missing: int  # VL-001 rows
    keypoint_rows: int  # rows written to keypoints
    quarantine_by_code: dict[str, int]  # quarantine rows per code
    frames_quarantined: int  # all quarantine rows
    points_per_frame_in: int  # points per frame in the raw data (0 before the manifest is read)
    keypoints_per_frame_out: int  # keypoints written per frame


class _Reject(Exception):
    """A whole-drive problem: carries the (code, message) quarantine rows."""

    def __init__(self, rows: list[tuple[str, str]]) -> None:
        super().__init__("; ".join(message for _, message in rows))
        self.rows = rows


def _reject(message: str) -> _Reject:
    """A whole-drive rejection with one INTEGRITY quarantine row; use as `raise _reject(...)`."""
    return _Reject([(INTEGRITY, message)])


@dataclass
class _Gate:
    """What is known about the drive when a rejection happens (for the lineage row)."""

    manifest: Manifest | None = None
    verified: bool = False


def ingest_drive(drive_dir: Path, lake: Lake, etl: EtlConfig) -> IngestResult:
    """Verify, validate and reduce one drive and swap its partitions into the lake.

    The drive folder is untrusted (a hand-made manifest or Parquet file must not exhaust memory)
    and is assumed not to change during the ingest (frames.parquet is hashed by verify_drive and
    opened again by the scan).

    Gate order (cheap checks first, so a bad manifest never costs a full read): see
    assumption M2-A34, then verify_drive, the lattice, the frames-file footer (row counts
    at most 2 * expected frames + 1000 rows; each row group at most etl.max_row_group_mib MiB as
    the footer claims) and the schema (VL-005); then one chunked scan and the frame rules.

    Raises ValueError for an invalid drive id or a keypoint spec that does not fit the drive's
    body (config errors; nothing is written). Any other failure is a rejection, returned not
    raised: one INTEGRITY (or VL-005) quarantine row per problem (text cleaned and cut to 300
    characters; the first 20 problems, then one "and K more problems" row), a lineage row with
    status "rejected" and zero counts, and the drive's keypoints removed. Read, scan or
    allocation failures (OSError, ValueError, RecursionError, Arrow error, MemoryError) are
    rejections too.

    A good drive writes keypoints (kept frames sorted by frame_id, then YAML keypoint order),
    then quarantine, then lineage last, each as one partition swap. Every file in the manifest
    is hashed by verify_drive; only frames.parquet is opened, and its manifest hash goes into
    the lineage row. frames_rejected counts quarantine rows that name a file row, so a frame that
    breaks two rules counts twice. Returns an IngestResult.
    """
    drive = Path(os.path.abspath(drive_dir)).name  # the name given, links not followed
    lake.read("lineage", drive)  # raises ValueError for an invalid drive id, before any write
    gate = _Gate()
    try:
        return _ingest(drive_dir, drive, lake, etl, gate)
    except _Reject as reject:
        return _write_rejected(lake, etl, drive, gate, reject.rows)


def _clean(text: str) -> str:
    """Text from outside, safe to store: control characters removed, whitespace collapsed to
    single spaces, at most _MAX_TEXT characters. Only the first _SCAN_TEXT characters are read."""
    kept = "".join(
        " " if c.isspace() else c
        for c in text[:_SCAN_TEXT]
        if c.isspace() or not unicodedata.category(c).startswith("C")
    )
    return " ".join(kept.split())[:_MAX_TEXT]


def _int_text(value: int) -> str:
    """str(value) for a sane integer; a placeholder for one too large to print safely.

    Python refuses to print an int of over 4300 digits (ValueError), and a manifest is untrusted.
    """
    if abs(value) >= 10**18:
        return "<huge integer>"
    return str(value)


def _short(error: BaseException, drive_dir: Path) -> str:
    """One-line cleaned text of an exception (see _clean), without the drive's local path.

    An OSError keeps only its type name and strerror (its text holds file names). In any text
    the drive folder's path, in the forms Python and Arrow print it, becomes "<drive>".
    """
    if isinstance(error, OSError):
        text = f"{type(error).__name__}: {error.strerror or ''}"
    else:
        text = f"{type(error).__name__}: {error}"
    text = text[: _SCAN_TEXT * 4]  # bounds the work; the path is replaced before the final cut
    absolute = os.path.abspath(drive_dir)
    forms = {str(drive_dir), Path(drive_dir).as_posix(), absolute, Path(absolute).as_posix()}
    forms |= {form.replace("\\", "\\\\") for form in forms}  # as printed inside a repr
    for form in sorted(forms, key=len, reverse=True):
        if form:
            text = text.replace(form, "<drive>")
    return _clean(text)


def _limit_rows(rows: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Clean every message, keep the first _MAX_PROBLEMS rows and add an "and K more" row."""
    cleaned = [(code, _clean(message)) for code, message in rows]
    if len(cleaned) <= _MAX_PROBLEMS:
        return cleaned
    more = len(cleaned) - _MAX_PROBLEMS
    return [*cleaned[:_MAX_PROBLEMS], (INTEGRITY, f"and {more} more problems")]


def _row_cap(frames_expected: int) -> int:
    """Most rows a frames file may claim: 2 * frames_expected + 1000 rows (M2-A47)."""
    return 2 * frames_expected + _ROWS_BASE


def _check_footer(metadata: Any, frames_expected: int) -> None:
    """Raise _Reject if the footer claims implausibly many rows for the expected frames.

    The cap (_row_cap) is applied to the total, to each row group and to the number of row
    groups, because the footer is controlled by the file's author.
    """
    cap = _row_cap(frames_expected)  # rows
    if metadata.num_rows > cap:
        raise _reject(f"footer claims {metadata.num_rows} rows, above the cap {cap}")
    if metadata.num_row_groups > cap:
        raise _reject(f"footer lists {metadata.num_row_groups} row groups, above the cap {cap}")
    for group in range(metadata.num_row_groups):
        rows = metadata.row_group(group).num_rows
        if rows > cap:
            raise _reject(f"row group {group} claims {rows} rows, above the cap {cap}")


@dataclass(frozen=True)
class _Checked:
    """A drive that passed the gates before any big read."""

    manifest: Manifest
    points_per_frame: int  # points in one raw frame (= the lattice count)
    frames_expected: int  # frames the scenario implies
    lattice: NDArray[np.int32]  # (points_per_frame, 3) cm


@dataclass(frozen=True)
class _Scanned:
    """The frames file after the scan and the frame rules."""

    facts: pd.DataFrame  # one row per file row
    violations: pd.DataFrame  # frame-rule violations (the quarantine rows)
    n_kept: int  # frames that broke no rule
    keypoint_batches: list[pa.RecordBatch]  # the kept frames reduced to keypoints


def _ingest(drive_dir: Path, drive: str, lake: Lake, etl: EtlConfig, gate: _Gate) -> IngestResult:
    """Gates, footer check, scan with rules, then the writes; _Reject on a whole-drive failure."""
    checked = _check_drive(drive_dir, drive, etl, gate)
    schema = _open_frames_file(drive_dir, checked.frames_expected, etl)
    scanned = _scan_and_validate(drive_dir, checked, schema, etl)
    return _write_ok(lake, drive, etl, checked, scanned)


def _check_drive(drive_dir: Path, drive: str, etl: EtlConfig, gate: _Gate) -> _Checked:
    """The gates in the order of M2-A34, then verify_drive and the lattice (no frames read)."""
    try:
        manifest = read_manifest(drive_dir)
    except (OSError, ValueError) as error:
        raise _reject(f"cannot read manifest.json: {_short(error, drive_dir)}") from error
    gate.manifest = manifest
    if not _HASH_RE.fullmatch(manifest.scenario_hash):
        raise _reject("scenario_hash is not 64 lowercase hex characters")
    if drive != manifest.scenario_hash[:12]:
        raise _reject(
            f"folder name {drive} is not the first 12 hex of scenario_hash {manifest.scenario_hash}"
        )
    if manifest.schema_version not in etl.supported_schema_versions:
        raise _reject(
            f"schema_version {_int_text(manifest.schema_version)} is not supported "
            f"(supported: {etl.supported_schema_versions})"
        )
    if manifest.fps < 1 or NS_PER_SECOND % manifest.fps != 0:
        raise _reject(f"fps {_int_text(manifest.fps)} does not divide 1,000,000,000 ns evenly")
    # The Manifest validator forces points_per_frame == body.points_per_axis ** 3, so this one
    # number is also the lattice count the scan checks, and the keypoint-spec check below uses it.
    points_per_frame = manifest.points_per_frame
    if points_per_frame > etl.max_points_per_frame:
        raise _reject(
            f"points per frame {_int_text(points_per_frame)} exceeds max_points_per_frame "
            f"{etl.max_points_per_frame}"
        )
    try:
        body = drive_body(manifest)
        frames_expected = expected_frame_count(manifest)
    except ValueError as error:
        raise _reject(f"invalid body or scenario: {_short(error, drive_dir)}") from error
    if FRAMES_NAME not in manifest.files:
        raise _reject(f"{FRAMES_NAME} is not listed in the manifest")
    check_keypoint_specs(points_per_frame, etl.keypoints)  # a config error raises, not rejects
    try:
        verified = verify_drive(drive_dir)
    except (OSError, ValueError, RecursionError, MemoryError) as error:
        raise _reject(f"cannot verify the drive: {_short(error, drive_dir)}") from error
    if not verified.ok:
        raise _Reject([(INTEGRITY, problem) for problem in verified.problems])
    gate.verified = True
    try:  # built only for a verified drive, and its size is capped above
        lattice = cube_lattice(body.size_cm, body.points_per_axis)
    except (ValueError, MemoryError) as error:
        raise _reject(f"cannot build the lattice: {_short(error, drive_dir)}") from error
    return _Checked(manifest, points_per_frame, frames_expected, lattice)


def _open_frames_file(drive_dir: Path, frames_expected: int, etl: EtlConfig) -> pa.Schema:
    """Open frames.parquet, check its footer (_check_footer, row-group size), return its schema."""
    try:
        with pq.ParquetFile(drive_dir / FRAMES_NAME) as parquet:
            schema = parquet.schema_arrow
            metadata = parquet.metadata
            _check_footer(metadata, frames_expected)
            for group in range(metadata.num_row_groups):
                mib = metadata.row_group(group).total_byte_size / 2**20
                if mib > etl.max_row_group_mib:
                    raise _reject(
                        f"row group {group} holds {mib:.0f} MiB uncompressed, above "
                        f"max_row_group_mib {etl.max_row_group_mib}"
                    )
    except (OSError, ValueError, MemoryError, pa.ArrowException) as error:
        raise _reject(f"cannot open {FRAMES_NAME}: {_short(error, drive_dir)}") from error
    return schema


def _check_frame_ids(facts: pd.DataFrame, frames_expected: int) -> None:
    """Raise _Reject if any frame_id is outside 0..frames_expected-1 (frames)."""
    ids = facts["frame_id"].to_numpy(dtype=np.int64)
    outside = (ids < 0) | (ids >= frames_expected)
    if outside.any():
        first = int(np.flatnonzero(outside)[0])
        raise _reject(
            f"{int(outside.sum())} row(s) have a frame_id outside "
            f"0..{frames_expected - 1}, "
            f"the first is {int(ids[first])} at row {first}"
        )


def _scan_and_validate(
    drive_dir: Path, checked: _Checked, schema: pa.Schema, etl: EtlConfig
) -> _Scanned:
    """Drive rules, one scan of the frames, frame rules, then keypoints of the kept frames."""
    manifest = checked.manifest
    points_per_frame = checked.points_per_frame
    frames_expected = checked.frames_expected
    ctx = RuleContext(
        facts=pd.DataFrame({name: [] for name in FACT_COLUMNS}),
        schema=schema,
        frame_count=frames_expected,
        points_per_frame=points_per_frame,
        fps=manifest.fps,
        frame_interval_ns=NS_PER_SECOND // manifest.fps,
        limits=etl.limits,
    )
    try:
        schema_violations = run_rules(ctx, "drive")
    except MemoryError as error:
        raise _reject(f"cannot run the drive rules: {_short(error, drive_dir)}") from error
    if len(schema_violations):
        raise _Reject(
            [
                (str(code), str(message))
                for code, message in zip(
                    schema_violations["code"], schema_violations["message"], strict=True
                )
            ]
        )

    lattice = checked.lattice
    offsets = (lattice - lattice[0]).astype(np.int32)  # cm
    try:
        facts, keypoints = scan_frames(
            drive_dir / FRAMES_NAME,
            offsets,
            etl.chunk_frames(points_per_frame),
            etl.keypoints,
            _row_cap(frames_expected),
        )
    except (OSError, ValueError, MemoryError, pa.ArrowException) as error:
        raise _reject(f"cannot read {FRAMES_NAME}: {_short(error, drive_dir)}") from error
    _check_frame_ids(facts, frames_expected)

    try:
        violations = run_rules(replace(ctx, facts=facts), "frame")
        flagged = violations["row_index"].dropna().astype("int64")
        kept = facts[~facts["row_index"].isin(flagged)].sort_values("frame_id", kind="stable")
        kept_rows = kept["row_index"].to_numpy(dtype=np.int64)
        keypoint_batches = _keypoint_batches(kept, keypoints[kept_rows], etl)
    except MemoryError as error:
        raise _reject(f"cannot run the frame rules: {_short(error, drive_dir)}") from error
    return _Scanned(facts, violations, len(kept), keypoint_batches)


def _write_ok(
    lake: Lake, drive: str, etl: EtlConfig, checked: _Checked, scanned: _Scanned
) -> IngestResult:
    """Swap keypoints, quarantine and (last) lineage into the lake; return the "ok" result."""
    facts, violations = scanned.facts, scanned.violations
    n_keypoints = len(etl.keypoints)
    codes = violations["code"].tolist()
    by_code = {code: codes.count(code) for code in sorted(set(codes))}
    frames_missing = by_code.get("VL-001", 0)
    frames_rejected = int(violations["row_index"].notna().sum())
    keypoint_rows = scanned.n_kept * n_keypoints

    lake.swap_partition("keypoints", drive, scanned.keypoint_batches)
    lake.swap_partition("quarantine", drive, _quarantine_batches(violations))
    lineage = _lineage_batch(
        checked.manifest,
        status="ok",
        verified=True,
        counts=(
            checked.frames_expected,
            len(facts),
            scanned.n_kept,
            frames_rejected,
            frames_missing,
            keypoint_rows,
        ),
    )
    lake.swap_partition("lineage", drive, [lineage])
    return IngestResult(
        drive=drive,
        status="ok",
        frames_expected=checked.frames_expected,
        frames_in=len(facts),
        frames_kept=scanned.n_kept,
        frames_rejected=frames_rejected,
        frames_missing=frames_missing,
        keypoint_rows=keypoint_rows,
        quarantine_by_code=by_code,
        frames_quarantined=len(violations),
        points_per_frame_in=checked.points_per_frame,
        keypoints_per_frame_out=n_keypoints,
    )


def _write_rejected(
    lake: Lake, etl: EtlConfig, drive: str, gate: _Gate, rows: list[tuple[str, str]]
) -> IngestResult:
    """Drop the drive's keypoints, then write its quarantine rows and a rejected lineage row."""
    lake.drop_partition("keypoints", drive)
    rows = _limit_rows(rows)
    n = len(rows)
    batch = pa.RecordBatch.from_arrays(
        [
            pa.array([None] * n, pa.int64()),
            pa.array([None] * n, pa.int64()),
            pa.array([code for code, _ in rows], pa.string()),
            pa.array([message for _, message in rows], pa.string()),
        ],
        schema=QUARANTINE_SCHEMA,
    )
    lake.swap_partition("quarantine", drive, [batch])
    manifest = gate.manifest
    lineage = _lineage_batch(manifest, "rejected", gate.verified, (0, 0, 0, 0, 0, 0))
    lake.swap_partition("lineage", drive, [lineage])
    by_code: dict[str, int] = {}
    for code, _ in rows:
        by_code[code] = by_code.get(code, 0) + 1
    return IngestResult(
        drive=drive,
        status="rejected",
        frames_expected=0,
        frames_in=0,
        frames_kept=0,
        frames_rejected=0,
        frames_missing=0,
        keypoint_rows=0,
        quarantine_by_code=dict(sorted(by_code.items())),
        frames_quarantined=n,
        points_per_frame_in=manifest.points_per_frame if manifest else 0,
        keypoints_per_frame_out=len(etl.keypoints),
    )


def _lineage_batch(
    manifest: Manifest | None,
    status: str,
    verified: bool,
    counts: tuple[int, int, int, int, int, int],
) -> pa.RecordBatch:
    """One lineage row. counts: expected, in, kept, rejected, missing, keypoint rows.

    The file hashes come from the manifest and are stored only when verify_drive passed.
    """
    frames_sha = None
    if manifest is not None and verified:
        entry = manifest.files.get(FRAMES_NAME)
        frames_sha = entry.sha256 if entry else None
    version = manifest.schema_version if manifest is not None else None
    if version is not None and not 0 <= version <= _INT32_MAX:
        version = None
    scenario_hash = None  # NULL unless the manifest hash is well formed (M2-A46)
    if manifest is not None and _HASH_RE.fullmatch(manifest.scenario_hash):
        scenario_hash = manifest.scenario_hash
    values: list[object] = [
        scenario_hash,
        version,
        status,
        *counts,
        frames_sha,
    ]
    arrays = [
        pa.array([value], type=field.type)
        for value, field in zip(values, LINEAGE_SCHEMA, strict=True)
    ]
    return pa.RecordBatch.from_arrays(arrays, schema=LINEAGE_SCHEMA)


def _quarantine_batches(violations: pd.DataFrame) -> list[pa.RecordBatch]:
    """The violations of the frame rules as one quarantine batch (none when empty)."""
    if not len(violations):
        return []
    batch = pa.RecordBatch.from_arrays(
        [
            pa.array([None if pd.isna(v) else int(v) for v in violations["frame_id"]], pa.int64()),
            pa.array([None if pd.isna(v) else int(v) for v in violations["row_index"]], pa.int64()),
            pa.array(violations["code"].tolist(), pa.string()),
            pa.array(violations["message"].tolist(), pa.string()),
        ],
        schema=QUARANTINE_SCHEMA,
    )
    return [batch]


def _keypoint_batches(
    kept: pd.DataFrame, points: NDArray[np.float64], etl: EtlConfig
) -> list[pa.RecordBatch]:
    """One keypoints batch: per kept frame (given order), one row per keypoint in YAML order.

    points is (kept frames, keypoints, 3) in cm. Returns no batch when no frame was kept.
    """
    n_keypoints = len(etl.keypoints)
    if not len(kept):
        return []
    names = [spec.name for spec in etl.keypoints]
    flat = points.reshape(-1, 3)
    batch = pa.RecordBatch.from_arrays(
        [
            pa.array(np.repeat(kept["frame_id"].to_numpy(dtype=np.int64), n_keypoints)),
            pa.array(np.repeat(kept["timestamp_ns"].to_numpy(dtype=np.int64), n_keypoints)),
            pa.array(names * len(kept), pa.string()),
            pa.array(flat[:, 0]),
            pa.array(flat[:, 1]),
            pa.array(flat[:, 2]),
        ],
        schema=KEYPOINTS_SCHEMA,
    )
    return [batch]
