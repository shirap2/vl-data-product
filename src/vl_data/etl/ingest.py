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
GROUND_TRUTH_NAME = "ground_truth.parquet"  # only its manifest hash is used, never the file
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

    Gates, cheapest first: the folder name must be a valid drive id (ValueError otherwise, nothing
    is written); the manifest must be readable (a missing, invalid or schema-1 manifest rejects the
    drive with the folder name as its id); scenario_hash must be 64 lowercase hex characters (the
    lineage then stores NULL for a bad one) and the folder name must equal scenario_hash[:12]; the
    schema_version must be in etl.supported_schema_versions; fps (frames/s) must divide 1e9 ns;
    points per frame (points, from the manifest and from the body) must not exceed
    etl.max_points_per_frame; the body and scenario must be valid; frames.parquet must be listed;
    verify_drive must pass; only then is the lattice built; the file must open, its footer row
    counts must be at most 2 * expected frames + 1000 rows (total, each row group, and the
    number of row groups), and no row group may exceed etl.max_row_group_mib (MiB, uncompressed,
    as the footer claims: a compression bomb can lie about it, a known limit); the schema must
    match (VL-005). Then the file is scanned once in chunks of etl.chunk_frames(points_per_frame)
    frames, a frame_id outside 0..expected-1 rejects the drive, and the frame rules run. Any
    rejection writes one INTEGRITY (or VL-005) quarantine row per problem (text cleaned of control
    characters and cut to 300 characters; the first 20 problems, then one "and K more problems"
    row), a lineage row with status "rejected" and zero counts, and removes the drive's
    keypoints. A read, scan or allocation failure (OSError, ValueError, RecursionError, Arrow
    error, MemoryError) is a drive-level INTEGRITY rejection. A keypoint spec that does not fit
    the drive's body raises ValueError (a config error, nothing is written).

    A good drive writes keypoints (kept frames sorted by frame_id, then YAML keypoint order),
    then quarantine, then lineage last, each as one partition swap. verify_drive hashes
    ground_truth.parquet in full but this function never opens or parses it; its manifest hash
    goes into the lineage row. frames_rejected in the result counts quarantine rows that name a
    file row, so a frame that breaks two rules counts twice. Returns an IngestResult.
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
        raise _Reject([(INTEGRITY, f"footer claims {metadata.num_rows} rows, above the cap {cap}")])
    if metadata.num_row_groups > cap:
        raise _Reject(
            [(INTEGRITY, f"footer lists {metadata.num_row_groups} row groups, above the cap {cap}")]
        )
    for group in range(metadata.num_row_groups):
        rows = metadata.row_group(group).num_rows
        if rows > cap:
            raise _Reject(
                [(INTEGRITY, f"row group {group} claims {rows} rows, above the cap {cap}")]
            )


def _ingest(drive_dir: Path, drive: str, lake: Lake, etl: EtlConfig, gate: _Gate) -> IngestResult:
    """Run gates, scan, rules and writes; raise _Reject for a whole-drive failure."""
    try:
        manifest = read_manifest(drive_dir)
    except (OSError, ValueError) as error:
        message = f"cannot read manifest.json: {_short(error, drive_dir)}"
        raise _Reject([(INTEGRITY, message)]) from error
    gate.manifest = manifest
    if not _HASH_RE.fullmatch(manifest.scenario_hash):
        raise _Reject([(INTEGRITY, "scenario_hash is not 64 lowercase hex characters")])
    if drive != manifest.scenario_hash[:12]:
        raise _Reject(
            [
                (
                    INTEGRITY,
                    f"folder name {drive} is not the first 12 hex of scenario_hash "
                    f"{manifest.scenario_hash}",
                )
            ]
        )
    if manifest.schema_version not in etl.supported_schema_versions:
        raise _Reject(
            [
                (
                    INTEGRITY,
                    f"schema_version {_int_text(manifest.schema_version)} is not supported "
                    f"(supported: {etl.supported_schema_versions})",
                )
            ]
        )
    if manifest.fps < 1 or NS_PER_SECOND % manifest.fps != 0:
        raise _Reject(
            [
                (
                    INTEGRITY,
                    f"fps {_int_text(manifest.fps)} does not divide 1,000,000,000 ns evenly",
                )
            ]
        )
    # The Manifest validator forces points_per_frame == body.points_per_axis ** 3, so this one
    # number is also the lattice count the scan checks, and the keypoint-spec check below uses it.
    points_per_frame = manifest.points_per_frame
    if points_per_frame > etl.max_points_per_frame:
        raise _Reject(
            [
                (
                    INTEGRITY,
                    f"points per frame {_int_text(points_per_frame)} exceeds max_points_per_frame "
                    f"{etl.max_points_per_frame}",
                )
            ]
        )
    try:
        body = drive_body(manifest)
        frames_expected = expected_frame_count(manifest)
    except ValueError as error:
        message = f"invalid body or scenario: {_short(error, drive_dir)}"
        raise _Reject([(INTEGRITY, message)]) from error
    if FRAMES_NAME not in manifest.files:
        raise _Reject([(INTEGRITY, f"{FRAMES_NAME} is not listed in the manifest")])
    check_keypoint_specs(points_per_frame, etl.keypoints)  # a config error raises, not rejects
    try:
        verified = verify_drive(drive_dir)
    except (OSError, ValueError, RecursionError, MemoryError) as error:
        message = f"cannot verify the drive: {_short(error, drive_dir)}"
        raise _Reject([(INTEGRITY, message)]) from error
    if not verified.ok:
        raise _Reject([(INTEGRITY, problem) for problem in verified.problems])
    gate.verified = True
    try:  # built only for a verified drive, and its size is capped above
        lattice = cube_lattice(body.size_cm, body.points_per_axis)
    except (ValueError, MemoryError) as error:
        message = f"cannot build the lattice: {_short(error, drive_dir)}"
        raise _Reject([(INTEGRITY, message)]) from error

    frames_path = drive_dir / FRAMES_NAME
    try:
        with pq.ParquetFile(frames_path) as parquet:
            schema = parquet.schema_arrow
            metadata = parquet.metadata
            _check_footer(metadata, frames_expected)
            for group in range(metadata.num_row_groups):
                mib = metadata.row_group(group).total_byte_size / 2**20
                if mib > etl.max_row_group_mib:
                    raise _Reject(
                        [
                            (
                                INTEGRITY,
                                f"row group {group} holds {mib:.0f} MiB uncompressed, above "
                                f"max_row_group_mib {etl.max_row_group_mib}",
                            )
                        ]
                    )
    except (OSError, ValueError, MemoryError, pa.ArrowException) as error:
        message = f"cannot open {FRAMES_NAME}: {_short(error, drive_dir)}"
        raise _Reject([(INTEGRITY, message)]) from error

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
        raise _Reject(
            [(INTEGRITY, f"cannot run the drive rules: {_short(error, drive_dir)}")]
        ) from error
    if len(schema_violations):
        raise _Reject(
            [
                (str(code), str(message))
                for code, message in zip(
                    schema_violations["code"], schema_violations["message"], strict=True
                )
            ]
        )

    offsets = (lattice - lattice[0]).astype(np.int32)  # cm
    try:
        facts, keypoints = scan_frames(
            frames_path,
            offsets,
            etl.chunk_frames(points_per_frame),
            etl.keypoints,
            _row_cap(frames_expected),
        )
    except (OSError, ValueError, MemoryError, pa.ArrowException) as error:
        message = f"cannot read {FRAMES_NAME}: {_short(error, drive_dir)}"
        raise _Reject([(INTEGRITY, message)]) from error
    ids = facts["frame_id"].to_numpy(dtype=np.int64)
    outside = (ids < 0) | (ids >= frames_expected)
    if outside.any():
        first = int(np.flatnonzero(outside)[0])
        raise _Reject(
            [
                (
                    INTEGRITY,
                    f"{int(outside.sum())} row(s) have a frame_id outside "
                    f"0..{frames_expected - 1}, "
                    f"the first is {int(ids[first])} at row {first}",
                )
            ]
        )

    try:
        violations = run_rules(replace(ctx, facts=facts), "frame")
        flagged = violations["row_index"].dropna().astype("int64")
        kept = facts[~facts["row_index"].isin(flagged)].sort_values("frame_id", kind="stable")
        kept_rows = kept["row_index"].to_numpy(dtype=np.int64)
        keypoint_batches = _keypoint_batches(kept, keypoints[kept_rows], etl)
    except MemoryError as error:
        message = f"cannot run the frame rules: {_short(error, drive_dir)}"
        raise _Reject([(INTEGRITY, message)]) from error
    n_kept = len(kept)
    n_keypoints = len(etl.keypoints)

    codes = violations["code"].tolist()
    by_code = {code: codes.count(code) for code in sorted(set(codes))}
    frames_missing = by_code.get("VL-001", 0)
    frames_rejected = int(violations["row_index"].notna().sum())
    keypoint_rows = n_kept * n_keypoints

    lake.swap_partition("keypoints", drive, keypoint_batches)
    lake.swap_partition("quarantine", drive, _quarantine_batches(violations))
    lineage = _lineage_batch(
        manifest,
        status="ok",
        verified=True,
        counts=(
            frames_expected,
            len(facts),
            n_kept,
            frames_rejected,
            frames_missing,
            keypoint_rows,
        ),
    )
    lake.swap_partition("lineage", drive, [lineage])
    return IngestResult(
        drive=drive,
        status="ok",
        frames_expected=frames_expected,
        frames_in=len(facts),
        frames_kept=n_kept,
        frames_rejected=frames_rejected,
        frames_missing=frames_missing,
        keypoint_rows=keypoint_rows,
        quarantine_by_code=by_code,
        frames_quarantined=len(violations),
        points_per_frame_in=points_per_frame,
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
    frames_sha = truth_sha = None
    if manifest is not None and verified:
        entry = manifest.files.get(FRAMES_NAME)
        frames_sha = entry.sha256 if entry else None
        truth = manifest.files.get(GROUND_TRUTH_NAME)
        truth_sha = truth.sha256 if truth else None
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
        truth_sha,
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
