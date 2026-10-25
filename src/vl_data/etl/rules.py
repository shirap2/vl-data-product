"""The rule registry VL-001 to VL-006 and the runner. Units: cm, ns; fps in frames/s.

Every rule takes a RuleContext and returns violations: a DataFrame with the columns in
VIOLATION_COLUMNS. row_index and frame_id are nullable Int64 (NA for a missing-frame row or a
drive-level row); code and message are str.
"""

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal

import numpy as np
import pandas as pd
import pyarrow as pa

from vl_data.core.schemas import FRAMES_SCHEMA
from vl_data.etl.config import LimitsConfig

VIOLATION_COLUMNS: list[str] = ["row_index", "frame_id", "code", "message"]
_BYTES_PER_POINT = 12  # bytes; core.schemas keeps its constant private, so one local copy
_INT64_MAX = 2**63 - 1


@dataclass(frozen=True)
class RuleContext:
    """Everything a rule may look at."""

    facts: pd.DataFrame  # one row per file row, see etl.facts.FACT_COLUMNS
    schema: pa.Schema  # schema of the frames file
    frame_count: int  # frames the manifest promises
    points_per_frame: int
    fps: int  # frames/s
    frame_interval_ns: int  # ns
    limits: LimitsConfig


@dataclass(frozen=True)
class Rule:
    """One validation rule with the text the error-code document is built from."""

    code: str
    name: str
    meaning: str
    detection: str
    example_fault: str  # class name in vl_data.generator.faults
    scope: Literal["drive", "frame"]
    needs_clean_frames: bool  # True: runs only on rows no other rule flagged
    check: Callable[[RuleContext], pd.DataFrame]


def _violations(
    rows: list[tuple[int | None, int | None, str, str]],
) -> pd.DataFrame:
    """Build a violations frame from (row_index, frame_id, code, message) tuples."""
    return pd.DataFrame(
        {
            "row_index": pd.array([r[0] for r in rows], dtype="Int64"),
            "frame_id": pd.array([r[1] for r in rows], dtype="Int64"),
            "code": pd.Series([r[2] for r in rows], dtype=object),
            "message": pd.Series([r[3] for r in rows], dtype=object),
        },
        columns=VIOLATION_COLUMNS,
    )


def _check_missing_frames(ctx: RuleContext) -> pd.DataFrame:
    """VL-001: every frame_id in range(frame_count) with no row."""
    present = set(ctx.facts["frame_id"].tolist())
    return _violations(
        [
            (None, frame_id, "VL-001", f"frame {frame_id} missing")
            for frame_id in range(ctx.frame_count)
            if frame_id not in present
        ]
    )


def _check_timestamps(ctx: RuleContext) -> pd.DataFrame:
    """VL-002: rows whose timestamp_ns (ns) is not frame_id * frame_interval_ns (ns).

    Rows whose frame_id is so large that the product would overflow int64 are flagged without
    multiplying.
    """
    facts = ctx.facts
    ids = facts["frame_id"].to_numpy(dtype=np.int64)
    limit = _INT64_MAX // ctx.frame_interval_ns  # largest |frame_id| whose product fits
    overflow = (ids > limit) | (ids < -limit)
    expected = np.where(overflow, 0, ids) * np.int64(ctx.frame_interval_ns)  # ns
    mismatch = facts["timestamp_ns"].to_numpy(dtype=np.int64) != expected
    bad = facts[overflow | mismatch]
    return _violations(
        [
            (
                int(row),
                int(frame_id),
                "VL-002",
                f"timestamp {int(ts)} ns, expected {int(frame_id) * ctx.frame_interval_ns} ns",
            )
            for row, frame_id, ts in zip(
                bad["row_index"], bad["frame_id"], bad["timestamp_ns"], strict=True
            )
        ]
    )


def _check_duplicates(ctx: RuleContext) -> pd.DataFrame:
    """VL-003: repeated frame_id. Identical repeats keep the first row; differing ones lose all."""
    facts = ctx.facts
    repeated = facts[facts.duplicated("frame_id", keep=False)]
    rows: list[tuple[int | None, int | None, str, str]] = []
    for key, group in repeated.groupby("frame_id", sort=True):
        frame_id = int(str(key))
        group = group.sort_values("row_index")
        identical = group[["timestamp_ns", "payload_sha256"]].drop_duplicates().shape[0] == 1
        flagged = group.iloc[1:] if identical else group
        note = "identical repeat" if identical else "copies differ"
        rows.extend(
            (
                int(row),
                frame_id,
                "VL-003",
                f"frame {frame_id} appears {len(group)} times, {note}",
            )
            for row in flagged["row_index"]
        )
    return _violations(rows)


def _check_clouds(ctx: RuleContext) -> pd.DataFrame:
    """VL-004: rows whose payload is not the expected cloud (sizes in bytes, lattice in cm)."""
    bad = ctx.facts[~ctx.facts["cloud_ok"].to_numpy(dtype=bool)]
    expected = ctx.points_per_frame * _BYTES_PER_POINT  # bytes
    rows: list[tuple[int | None, int | None, str, str]] = []
    for row, frame_id, n_bytes in zip(
        bad["row_index"], bad["frame_id"], bad["n_bytes"], strict=True
    ):
        if int(n_bytes) != expected:
            message = f"payload is {int(n_bytes)} bytes, expected {expected} bytes"
        else:
            message = "points are not the expected lattice"
        rows.append((int(row), int(frame_id), "VL-004", message))
    return _violations(rows)


def _check_schema(ctx: RuleContext) -> pd.DataFrame:
    """VL-005: the frames file schema must equal FRAMES_SCHEMA (metadata ignored)."""
    actual = ctx.schema.remove_metadata()
    wanted = FRAMES_SCHEMA.remove_metadata()
    if actual.equals(wanted):
        return _violations([])
    have = {f.name: f for f in actual}
    want = {f.name: f for f in wanted}
    missing = [name for name in want if name not in have]
    extra = [name for name in have if name not in want]
    different = [name for name in want if name in have and not have[name].equals(want[name])]
    parts = []
    if missing:
        parts.append(f"missing {missing}")
    if extra:
        parts.append(f"unexpected {extra}")
    if different:
        parts.append(f"different {different}")
    if not parts:
        parts.append("column order differs")
    return _violations([(None, None, "VL-005", "columns differ: " + ", ".join(parts))])


def _check_jumps(ctx: RuleContext) -> pd.DataFrame:
    """VL-006: centroid moves (cm) larger than the speed limit allows between frames.

    Rows are walked in (frame_id, row_index) order. The baseline comes from the tightest pair
    among the first up to three rows: for each pair the centroid distance (cm) is divided by
    limits.max_jump_cm(fps, gap) (cm, gap = frame_id difference); of the pairs within the limit
    (ratio <= 1) the smallest ratio wins (ties: order (a,b), (a,c), (b,c)) and its earlier row
    is the baseline. Earlier rows are compared with the baseline and flagged if beyond the limit.
    With no pair within the limit the first row is the baseline (the walk then flags the rest).
    After that each row is compared with the last accepted one (limit scaled by the gap) and a
    flagged row never becomes the baseline. Rows with a NaN centroid are skipped. Raises
    ValueError for a repeated frame_id (run the other rules first, which remove them).
    """
    facts = ctx.facts.sort_values(["frame_id", "row_index"])
    facts = facts[facts[["cx_cm", "cy_cm", "cz_cm"]].notna().all(axis=1)]
    rows_idx = facts["row_index"].to_numpy(dtype=np.int64)
    ids = facts["frame_id"].to_numpy(dtype=np.int64)
    xyz = facts[["cx_cm", "cy_cm", "cz_cm"]].to_numpy(dtype=np.float64)  # cm
    if len(ids) > 1 and (np.diff(ids) < 1).any():
        raise ValueError("VL-006 needs unique frame_id values")

    def jump(a: int, b: int) -> tuple[float, float]:
        """(distance, limit) in cm between sorted rows a < b."""
        distance = float(np.linalg.norm(xyz[b] - xyz[a]))  # cm
        return distance, ctx.limits.max_jump_cm(ctx.fps, int(ids[b] - ids[a]))

    rows: list[tuple[int | None, int | None, str, str]] = []

    def flag(k: int, distance: float, limit: float, gap: int) -> None:
        message = f"moved {distance:.1f} cm in {gap} frame(s), limit {limit:.1f} cm"
        rows.append((int(rows_idx[k]), int(ids[k]), "VL-006", message))

    n = len(ids)
    head = range(min(n, 3))
    best: tuple[float, int] | None = None  # (distance / limit, earlier row of the pair)
    for a in head:
        for b in head:
            if b > a:
                distance, limit = jump(a, b)
                if distance <= limit and (best is None or distance / limit < best[0]):
                    best = (distance / limit, a)  # strict <: the first pair wins a tie
    base = 0 if best is None else best[1]
    for k in range(base):  # rows before the baseline are checked against it
        distance, limit = jump(k, base)
        if distance > limit:
            flag(k, distance, limit, int(ids[base] - ids[k]))
    last: int | None = base if n else None
    for k in range(base + 1, n):
        if last is None:
            break
        distance, limit = jump(last, k)
        if distance > limit:
            flag(k, distance, limit, int(ids[k] - ids[last]))
        else:
            last = k
    return _violations(rows)


RULES: list[Rule] = [
    Rule(
        code="VL-001",
        name="Missing frame",
        meaning="A frame_id between 0 and frame_count - 1 has no row in the frames file.",
        detection="Every id in range(frame_count) that is absent from the frame_id column.",
        example_fault="DropFrame",
        scope="frame",
        needs_clean_frames=False,
        check=_check_missing_frames,
    ),
    Rule(
        code="VL-002",
        name="Timestamp gap",
        meaning="A frame's timestamp is not frame_id times the frame interval.",
        detection="timestamp_ns differs from frame_id * frame_interval_ns, checked per row.",
        example_fault="ShiftTimestamp",
        scope="frame",
        needs_clean_frames=False,
        check=_check_timestamps,
    ),
    Rule(
        code="VL-003",
        name="Duplicate frame",
        meaning="More than one row carries the same frame_id.",
        detection=(
            "Rows are grouped by frame_id. If all rows of a group have the same timestamp and "
            "payload hash, the first is kept and the repeats are flagged; otherwise every row "
            "of the group is flagged."
        ),
        example_fault="DuplicateFrame",
        scope="frame",
        needs_clean_frames=False,
        check=_check_duplicates,
    ),
    Rule(
        code="VL-004",
        name="Corrupt point cloud",
        meaning="A frame's points are not the expected body: wrong length, count or shape.",
        detection=(
            "The payload must be a multiple of 12 bytes, decode to points_per_frame points, and "
            "each axis of (points - expected lattice) must spread at most 1 cm."
        ),
        example_fault="CorruptPoints",
        scope="frame",
        needs_clean_frames=False,
        check=_check_clouds,
    ),
    Rule(
        code="VL-005",
        name="Schema mismatch",
        meaning="The frames file does not have the columns and types of the frames schema.",
        detection="The file schema (metadata ignored) is compared with the frames schema.",
        example_fault="BreakSchema",
        scope="drive",
        needs_clean_frames=False,
        check=_check_schema,
    ),
    Rule(
        code="VL-006",
        name="Impossible motion",
        meaning="The body moved farther between frames than the speed limit allows.",
        detection=(
            "Among frames with no other violation, in frame_id order, a centroid farther from the "
            "last accepted centroid than max_speed * frame_gap / fps + margin is flagged and "
            "never becomes the baseline."
        ),
        example_fault="Teleport",
        scope="frame",
        needs_clean_frames=True,
        check=_check_jumps,
    ),
]


def run_rules(ctx: RuleContext, scope: Literal["drive", "frame"]) -> pd.DataFrame:
    """Run the rules of one scope and return their violations.

    Drive scope never reads ctx.facts. Frame scope first runs the rules that do not need clean
    frames, then the others on a copy of the context holding only rows with no violation so far.
    The result is sorted by row_index (NA last), then code, then frame_id.
    """
    selected = [r for r in RULES if r.scope == scope]
    parts = [r.check(ctx) for r in selected if not r.needs_clean_frames]
    later = [r for r in selected if r.needs_clean_frames]
    if later:
        flagged = pd.concat(parts, ignore_index=True)["row_index"].dropna().astype("int64")
        clean = replace(ctx, facts=ctx.facts[~ctx.facts["row_index"].isin(flagged)])
        parts.extend(r.check(clean) for r in later)
    parts = [p for p in parts if len(p)]
    if not parts:
        return _violations([])
    out = pd.concat(parts, ignore_index=True)
    out = out.sort_values(["row_index", "code", "frame_id"], na_position="last", kind="stable")
    out = out.reset_index(drop=True)
    out["row_index"] = out["row_index"].astype("Int64")
    out["frame_id"] = out["frame_id"].astype("Int64")
    return out
