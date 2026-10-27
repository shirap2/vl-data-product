"""Fault injection for generated drives. Units: cm, ns."""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import cast

import numpy as np
import pyarrow as pa
from numpy.typing import NDArray

from vl_data.core.schemas import BYTES_PER_POINT, decode_points, encode_points
from vl_data.generator.drive import Drive


@dataclass(frozen=True)
class DropFrame:
    """VL-001 missing frame: delete the row with this frame_id (no renumbering)."""

    frame_id: int


@dataclass(frozen=True)
class ShiftTimestamp:
    """VL-002 timestamp gap: add delta_ns (ns) to this frame's timestamp.

    delta_ns must be an int (not a float or bool), nonzero, and smaller in size than one frame
    interval (|delta_ns| < frame_interval_ns), so the frame never passes its neighbour.
    """

    frame_id: int
    delta_ns: int  # ns


@dataclass(frozen=True)
class DuplicateFrame:
    """VL-003 duplicate frame: insert a copy of the row right after the original."""

    frame_id: int


@dataclass(frozen=True)
class CorruptPoints:
    """VL-004 corrupt point cloud: remove the last point (12 bytes) of this frame."""

    frame_id: int


@dataclass(frozen=True)
class BreakSchema:
    """VL-005 schema mismatch: rename the points column."""


@dataclass(frozen=True)
class Teleport:
    """VL-006 physically impossible motion: shift this frame's points by dx_cm (cm) in x."""

    frame_id: int
    dx_cm: int  # cm


Fault = DropFrame | ShiftTimestamp | DuplicateFrame | CorruptPoints | BreakSchema | Teleport


_CODES: dict[type, str] = {
    DropFrame: "VL-001",
    ShiftTimestamp: "VL-002",
    DuplicateFrame: "VL-003",
    CorruptPoints: "VL-004",
    BreakSchema: "VL-005",
    Teleport: "VL-006",
}
DEFAULT_FRAME_INTERVAL_NS = 62_500_000  # ns: 16 fps
_INT64_MIN, _INT64_MAX = -(2**63), 2**63 - 1
_RENAMED_POINTS = "pts"  # the name BreakSchema gives the points column


def _validate(
    faults: Sequence[Fault],
    ids: list[int],
    timestamps: list[int],
    points: list[bytes],
    frame_interval_ns: int,
) -> None:
    """Raise ValueError for bad input.

    Covers repeated frame_ids or non-increasing timestamps (ns) in the table, unknown or shared
    fault frame_ids, a second BreakSchema, a delta_ns that is not an int, is 0, leaves int64 or
    is not smaller than frame_interval_ns (ns) in size, a dx_cm (cm) of 0, and CorruptPoints on
    a frame with under 12 bytes.
    """
    row_of = {frame_id: row for row, frame_id in enumerate(ids)}
    if len(row_of) != len(ids):
        raise ValueError("frame_id values are not unique")
    for row in range(1, len(ids)):
        if timestamps[row] <= timestamps[row - 1]:
            raise ValueError(
                "input timestamps are not strictly increasing: frame_id "
                f"{ids[row - 1]} and frame_id {ids[row]} are in the wrong order or equal"
            )
    seen: set[int] = set()
    schema_breaks = 0
    for fault in faults:
        if isinstance(fault, BreakSchema):
            schema_breaks += 1
            if schema_breaks > 1:
                raise ValueError("at most one BreakSchema is allowed")
            continue
        if fault.frame_id not in row_of:
            raise ValueError(f"unknown frame_id {fault.frame_id}")
        if fault.frame_id in seen:
            raise ValueError(f"more than one fault for frame_id {fault.frame_id}")
        seen.add(fault.frame_id)
        if isinstance(fault, ShiftTimestamp):
            _validate_shift(fault, timestamps[row_of[fault.frame_id]], frame_interval_ns)
        elif isinstance(fault, Teleport) and fault.dx_cm == 0:
            raise ValueError("dx_cm must not be 0")
        elif (
            isinstance(fault, CorruptPoints)
            and len(points[row_of[fault.frame_id]]) < BYTES_PER_POINT
        ):
            raise ValueError(f"frame_id {fault.frame_id} has fewer than 12 bytes of points")


def _validate_shift(fault: ShiftTimestamp, timestamp: int, frame_interval_ns: int) -> None:
    """Raise ValueError unless delta_ns is a nonzero int, inside int64 and under one interval."""
    delta = fault.delta_ns  # ns
    if isinstance(delta, bool) or not isinstance(delta, int):
        raise ValueError(f"delta_ns must be an int, got {delta!r}")
    if delta == 0:
        raise ValueError("delta_ns must not be 0")
    if not _INT64_MIN <= timestamp + delta <= _INT64_MAX:
        raise ValueError(f"delta_ns {delta} moves the timestamp out of int64")
    if abs(delta) >= frame_interval_ns:
        raise ValueError(
            f"delta_ns {delta} ns is too large: the shift must be smaller than one frame "
            f"interval ({frame_interval_ns} ns)"
        )


def inject_faults(
    drive: Drive,
    faults: Sequence[Fault],
    frame_interval_ns: int = DEFAULT_FRAME_INTERVAL_NS,
) -> tuple[Drive, dict[str, int]]:
    """Apply faults to the frames table; return the new drive and injected counts per code.

    Each fault targets the row with its frame_id in the original drive (timestamps in ns,
    point coordinates in cm). The scenario hash is unchanged; the input drive is not
    modified. frame_interval_ns (ns, default 62,500,000) is the bound for ShiftTimestamp.
    Raises ValueError for repeated frame_ids or non-increasing timestamps in the input table,
    an unknown frame_id, two faults on one frame_id, more than one BreakSchema, a delta_ns that
    is not an int, is 0, leaves int64 or has |delta_ns| >= frame_interval_ns, dx_cm == 0,
    final timestamps that are not strictly increasing in original row order (rows dropped and
    DuplicateFrame copies are left out; the check compares adjacent surviving rows),
    CorruptPoints on a frame with under 12 bytes, or a Teleport that pushes a coordinate out
    of int32.
    """
    frames = drive.frames
    ids: list[int] = frames.column("frame_id").to_pylist()
    timestamps: list[int] = frames.column("timestamp_ns").to_pylist()  # ns
    points: list[bytes] = frames.column("points").to_pylist()
    _validate(faults, ids, timestamps, points, frame_interval_ns)

    by_id: dict[int, Fault] = {f.frame_id: f for f in faults if not isinstance(f, BreakSchema)}
    out_ids: list[int] = []
    out_ts: list[int] = []
    out_points: list[bytes] = []
    out_is_copy: list[bool] = []
    for frame_id, ts, pts in zip(ids, timestamps, points, strict=True):
        fault = by_id.get(frame_id)
        copies = 1
        if isinstance(fault, DropFrame):
            continue
        if isinstance(fault, ShiftTimestamp):
            ts += fault.delta_ns
        elif isinstance(fault, DuplicateFrame):
            copies = 2
        elif isinstance(fault, CorruptPoints):
            pts = pts[:-BYTES_PER_POINT]
        elif isinstance(fault, Teleport):
            # int64 so the sum cannot wrap; encode_points range-checks it against int32
            moved: NDArray[np.int64] = decode_points(pts).astype(np.int64)
            moved[:, 0] += fault.dx_cm  # cm
            pts = encode_points(cast("NDArray[np.int32]", moved))
        for copy in range(copies):
            out_ids.append(frame_id)
            out_ts.append(ts)
            out_points.append(pts)
            out_is_copy.append(copy > 0)
    survivors = [
        (i, t) for i, t, is_copy in zip(out_ids, out_ts, out_is_copy, strict=True) if not is_copy
    ]
    for (prev_id, prev_ts), (next_id, next_ts) in zip(survivors, survivors[1:], strict=False):
        if next_ts <= prev_ts:
            raise ValueError(
                "faults would make timestamps not strictly increasing: frame_id "
                f"{prev_id} ({prev_ts} ns) is followed by frame_id {next_id} ({next_ts} ns)"
            )

    schema = frames.schema
    table = pa.Table.from_arrays(
        [
            pa.array(out_ids, type=schema.field("frame_id").type),
            pa.array(out_ts, type=schema.field("timestamp_ns").type),
            pa.array(out_points, type=schema.field("points").type),
        ],
        schema=schema,
    )
    if any(isinstance(f, BreakSchema) for f in faults):
        names = [_RENAMED_POINTS if n == "points" else n for n in table.schema.names]
        table = table.rename_columns(names)

    counts: dict[str, int] = {}
    for fault in faults:
        code = _CODES[type(fault)]
        counts[code] = counts.get(code, 0) + 1
    return replace(drive, frames=table), counts
