"""Tests for the rule registry VL-001 to VL-006. Units: cm, cm/s, ns; fps in frames/s."""

from collections.abc import Callable
from dataclasses import replace

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vl_data.core.config import Config
from vl_data.core.manifest import read_manifest
from vl_data.core.schemas import FRAMES_SCHEMA, encode_points
from vl_data.etl.body import expected_frame_count
from vl_data.etl.config import LimitsConfig
from vl_data.etl.rules import RULES, VIOLATION_COLUMNS, RuleContext, run_rules
from vl_data.generator import faults as fault_module
from vl_data.generator.body import cube_lattice
from vl_data.generator.drive import Drive, generate
from vl_data.generator.faults import (
    BreakSchema,
    CorruptPoints,
    DropFrame,
    DuplicateFrame,
    Fault,
    ShiftTimestamp,
    Teleport,
    inject_faults,
)
from vl_data.generator.models import MoveSegment, Scenario, Start, StopSegment
from vl_data.generator.writer import write_drive

from .helpers import Written, build_table, columns, scan_facts

MakeCtx = Callable[..., RuleContext]
WriteFaulty = Callable[..., Written]


def _pairs(violations: pd.DataFrame) -> list[tuple[str, int | None]]:
    """(code, frame_id or None) per violation row, in order."""
    return [
        (str(code), None if pd.isna(frame_id) else int(frame_id))
        for code, frame_id in zip(violations["code"], violations["frame_id"], strict=True)
    ]


def _frame_scope(
    write_faulty: WriteFaulty, make_ctx: MakeCtx, faults: list[object]
) -> pd.DataFrame:
    return run_rules(make_ctx(write_faulty(faults)), "frame")


def test_violation_frame_dtypes(write_faulty: WriteFaulty, make_ctx: MakeCtx) -> None:
    out = _frame_scope(write_faulty, make_ctx, [DropFrame(100), CorruptPoints(200)])
    assert list(out.columns) == VIOLATION_COLUMNS
    assert str(out["row_index"].dtype) == "Int64"
    assert str(out["frame_id"].dtype) == "Int64"
    empty = _frame_scope(write_faulty, make_ctx, [])
    assert list(empty.columns) == VIOLATION_COLUMNS
    assert str(empty["row_index"].dtype) == "Int64"
    assert str(empty["frame_id"].dtype) == "Int64"


def test_vl001_missing_frame(write_faulty: WriteFaulty, make_ctx: MakeCtx) -> None:
    out = _frame_scope(write_faulty, make_ctx, [DropFrame(100)])
    assert _pairs(out) == [("VL-001", 100)]
    assert pd.isna(out.loc[0, "row_index"])
    assert out.loc[0, "message"] == "frame 100 missing"


def test_vl001_two_missing_two_rows(write_faulty: WriteFaulty, make_ctx: MakeCtx) -> None:
    out = _frame_scope(write_faulty, make_ctx, [DropFrame(100), DropFrame(500)])
    assert _pairs(out) == [("VL-001", 100), ("VL-001", 500)]


def test_vl002_shifted_timestamp(write_faulty: WriteFaulty, make_ctx: MakeCtx) -> None:
    out = _frame_scope(write_faulty, make_ctx, [ShiftTimestamp(200, 1_000_000)])
    assert _pairs(out) == [("VL-002", 200)]
    assert out.loc[0, "row_index"] == 200
    # a shift of more than five frame intervals puts the row after later frames in time
    big = 5 * 62_500_000 + 7
    out = _frame_scope(write_faulty, make_ctx, [ShiftTimestamp(200, big)])
    assert _pairs(out) == [("VL-002", 200)]
    out = _frame_scope(write_faulty, make_ctx, [ShiftTimestamp(200, -big)])
    assert _pairs(out) == [("VL-002", 200)]


def test_rows_out_of_order_not_flagged(
    write_faulty: WriteFaulty, make_ctx: MakeCtx, clean_drive: Drive
) -> None:
    table = clean_drive.frames.take(list(range(999, -1, -1)))
    out = run_rules(make_ctx(write_faulty(table=table)), "frame")
    assert len(out) == 0


def test_vl003_identical_duplicate_keeps_first(
    write_faulty: WriteFaulty, make_ctx: MakeCtx
) -> None:
    out = _frame_scope(write_faulty, make_ctx, [DuplicateFrame(300)])
    assert _pairs(out) == [("VL-003", 300)]
    assert out.loc[0, "row_index"] == 301  # the repeat, not the original at row 300


def test_vl003_differing_duplicates_quarantine_all(
    write_faulty: WriteFaulty, make_ctx: MakeCtx, clean_drive: Drive
) -> None:
    ids, ts, pts = columns(clean_drive)
    moved = np.frombuffer(pts[300] or b"", dtype="<i4").reshape(-1, 3) + np.array([1, 0, 0])
    ids.insert(301, ids[300])
    ts.insert(301, ts[300])
    pts.insert(301, encode_points(moved.astype(np.int32)))
    out = run_rules(make_ctx(write_faulty(table=build_table(ids, ts, pts))), "frame")
    assert _pairs(out) == [("VL-003", 300), ("VL-003", 300)]
    assert out["row_index"].tolist() == [300, 301]


def test_vl003_same_points_different_timestamp_quarantine_all(
    write_faulty: WriteFaulty, make_ctx: MakeCtx, clean_drive: Drive
) -> None:
    ids, ts, pts = columns(clean_drive)
    ids.insert(301, ids[300])
    ts.insert(301, ts[300] + 1)
    pts.insert(301, pts[300])
    out = run_rules(make_ctx(write_faulty(table=build_table(ids, ts, pts))), "frame")
    assert sorted(_pairs(out)) == [("VL-002", 300), ("VL-003", 300), ("VL-003", 300)]


def test_vl004_corrupt_points(write_faulty: WriteFaulty, make_ctx: MakeCtx) -> None:
    out = _frame_scope(write_faulty, make_ctx, [CorruptPoints(400)])
    assert _pairs(out) == [("VL-004", 400)]
    assert out.loc[0, "row_index"] == 400
    message = out.loc[0, "message"]
    assert "1488" in message
    assert "1500" in message


def test_vl004_moved_point_message(
    write_faulty: WriteFaulty, make_ctx: MakeCtx, clean_drive: Drive
) -> None:
    ids, ts, pts = columns(clean_drive)
    moved = np.frombuffer(pts[40] or b"", dtype="<i4").reshape(-1, 3).copy()
    moved[3, 2] += 5
    pts[40] = encode_points(moved)
    out = run_rules(make_ctx(write_faulty(table=build_table(ids, ts, pts))), "frame")
    assert _pairs(out) == [("VL-004", 40)]
    assert "lattice" in out.loc[0, "message"]


def test_vl005_schema_mismatch(write_faulty: WriteFaulty, make_ctx: MakeCtx) -> None:
    written = write_faulty([BreakSchema()])
    ctx = make_ctx(written, facts=pd.DataFrame())  # a broken file is never scanned
    out = run_rules(ctx, "drive")
    assert _pairs(out) == [("VL-005", None)]
    assert pd.isna(out.loc[0, "row_index"])
    message = out.loc[0, "message"]
    assert "points" in message
    assert "pts" in message


def test_vl005_clean_schema_and_metadata_ignored(
    write_faulty: WriteFaulty, make_ctx: MakeCtx
) -> None:
    written = write_faulty()
    ctx = make_ctx(written, facts=pd.DataFrame())
    with_meta = replace(ctx, schema=ctx.schema.with_metadata({"k": "v"}))
    assert len(run_rules(with_meta, "drive")) == 0


def test_drive_scope_does_not_touch_facts(write_faulty: WriteFaulty, make_ctx: MakeCtx) -> None:
    ctx = make_ctx(write_faulty(), facts=pd.DataFrame())
    out = run_rules(ctx, "drive")
    assert list(out.columns) == VIOLATION_COLUMNS
    assert len(out) == 0


def test_vl006_teleport_flags_one_frame(write_faulty: WriteFaulty, make_ctx: MakeCtx) -> None:
    out = _frame_scope(write_faulty, make_ctx, [Teleport(500, 1000)])
    assert _pairs(out) == [("VL-006", 500)]
    assert out.loc[0, "row_index"] == 500


def test_vl006_after_dropped_frame_uses_gap(write_faulty: WriteFaulty, make_ctx: MakeCtx) -> None:
    # 700 cm/s with no margin: 43.75 cm across one frame, 87.5 cm across two. Frames 128 to 288
    # of the demo move at 600 cm/s (37.5 cm per frame, 75 cm across the gap of two).
    limits = LimitsConfig(max_speed_cm_s=700.0, rounding_margin_cm=0.0)
    written = write_faulty([DropFrame(200)])
    out = run_rules(make_ctx(written, limits), "frame")
    assert _pairs(out) == [("VL-001", 200)]
    # the same drive with a one-frame limit would flag frame 201 if the gap were ignored
    assert limits.max_jump_cm(16, 1) < 75.0 < limits.max_jump_cm(16, 2)


def test_vl006_just_inside_limit_passes(write_faulty: WriteFaulty, make_ctx: MakeCtx) -> None:
    # frame 10 is inside the first stop (speed 0); the limit at 16 fps is 564.5 cm
    inside = _frame_scope(write_faulty, make_ctx, [Teleport(10, 560)])
    assert len(inside) == 0
    outside = _frame_scope(write_faulty, make_ctx, [Teleport(10, 570)])
    assert _pairs(outside) == [("VL-006", 10)]


def test_vl006_skips_other_failed_frames(
    write_faulty: WriteFaulty, make_ctx: MakeCtx, clean_drive: Drive
) -> None:
    drive, _ = inject_faults(clean_drive, [CorruptPoints(600), Teleport(700, 1000)])
    ids, ts, pts = columns(drive)
    ts[700] += 3  # frame 700 now also fails VL-002, so it must not be the baseline
    out = run_rules(make_ctx(write_faulty(table=build_table(ids, ts, pts))), "frame")
    assert sorted(_pairs(out)) == [("VL-002", 700), ("VL-004", 600)]


def test_clean_demo_has_no_violations(write_faulty: WriteFaulty, make_ctx: MakeCtx) -> None:
    ctx = make_ctx(write_faulty())
    assert len(run_rules(ctx, "frame")) == 0
    assert len(run_rules(ctx, "drive")) == 0


def test_input_facts_not_modified(write_faulty: WriteFaulty, make_ctx: MakeCtx) -> None:
    ctx = make_ctx(write_faulty([Teleport(500, 1000), CorruptPoints(10)]))
    before = ctx.facts.copy()
    run_rules(ctx, "frame")
    pd.testing.assert_frame_equal(ctx.facts, before)


@st.composite
def _scenarios(draw: st.DrawFn) -> Scenario:
    count = draw(st.integers(min_value=1, max_value=4))
    segments: list[MoveSegment | StopSegment] = [
        MoveSegment(
            duration_s=draw(st.integers(min_value=1, max_value=40)) / 16,
            end_speed_cm_s=float(
                draw(st.integers(min_value=-8900, max_value=8900).filter(lambda v: v != 0))
            ),
        )
        for _ in range(count)
    ]
    return Scenario(start=Start(), segments=segments)


@settings(max_examples=15, deadline=None)
@given(scenario=_scenarios())
def test_property_clean_drives_never_flagged(
    scenario: Scenario, tmp_path_factory: pytest.TempPathFactory
) -> None:
    config = Config.model_validate({"body": {"size_cm": 40, "points_per_axis": 5}})
    drive = generate(scenario, config)
    out = write_drive(drive, tmp_path_factory.mktemp("prop"), config)
    path = out / "frames.parquet"
    lattice = cube_lattice(40, 5)
    offsets = (lattice - lattice[0]).astype(np.int32)
    ctx = RuleContext(
        facts=scan_facts(path, offsets, 64),
        schema=pq.ParquetFile(path).schema_arrow,
        frame_count=expected_frame_count(read_manifest(out)),
        points_per_frame=offsets.shape[0],
        fps=config.fps,
        frame_interval_ns=config.frame_interval_ns,
        limits=LimitsConfig(),
    )
    assert ctx.frame_count == drive.frames.num_rows
    assert len(run_rules(ctx, "frame")) == 0
    assert len(run_rules(ctx, "drive")) == 0


_EXAMPLES: dict[str, Fault] = {
    "DropFrame": DropFrame(100),
    "ShiftTimestamp": ShiftTimestamp(100, 1000),
    "DuplicateFrame": DuplicateFrame(100),
    "CorruptPoints": CorruptPoints(100),
    "BreakSchema": BreakSchema(),
    "Teleport": Teleport(100, 1000),
}


def test_registry_order_and_scopes() -> None:
    assert [r.code for r in RULES] == [f"VL-00{n}" for n in range(1, 7)]
    assert [r.scope for r in RULES] == ["frame", "frame", "frame", "frame", "drive", "frame"]
    assert [r.needs_clean_frames for r in RULES] == [False] * 5 + [True]
    for rule in RULES:
        assert rule.name
        assert rule.meaning
        assert rule.detection


def test_each_rule_has_real_example_fault(
    clean_drive: Drive, write_faulty: WriteFaulty, make_ctx: MakeCtx
) -> None:
    for rule in RULES:
        cls = getattr(fault_module, rule.example_fault)
        assert isinstance(cls, type)
        fault = _EXAMPLES[rule.example_fault]
        assert isinstance(fault, cls)
        _, counts = inject_faults(clean_drive, [fault])
        assert counts == {rule.code: 1}
        written = write_faulty([fault])
        assert written.counts == {rule.code: 1}
        # a broken schema is never scanned, so the drive-scope rule gets empty facts
        ctx = make_ctx(written, facts=pd.DataFrame() if rule.scope == "drive" else None)
        found = run_rules(ctx, rule.scope)
        assert set(found["code"]) == {rule.code}


def test_run_rules_orders_and_combines(write_faulty: WriteFaulty, make_ctx: MakeCtx) -> None:
    faults = [
        DropFrame(100),
        ShiftTimestamp(200, 1_000_000),
        DuplicateFrame(300),
        CorruptPoints(400),
        Teleport(500, 1000),
    ]
    out = _frame_scope(write_faulty, make_ctx, faults)
    assert [c for c, _ in _pairs(out)] == ["VL-002", "VL-003", "VL-004", "VL-006", "VL-001"]
    assert _pairs(out)[-1] == ("VL-001", 100)
    assert out["row_index"].iloc[:-1].is_monotonic_increasing
    assert pd.isna(out["row_index"].iloc[-1])


def test_vl001_last_frame_duplicate_and_drop(write_faulty: WriteFaulty, make_ctx: MakeCtx) -> None:
    out = _frame_scope(write_faulty, make_ctx, [DuplicateFrame(999)])
    assert _pairs(out) == [("VL-003", 999)]  # 1001 rows must not hide or invent a frame
    out = _frame_scope(write_faulty, make_ctx, [DropFrame(999)])
    assert _pairs(out) == [("VL-001", 999)]


def test_vl001_counts_cancel(write_faulty: WriteFaulty, make_ctx: MakeCtx) -> None:
    out = _frame_scope(write_faulty, make_ctx, [DropFrame(5), DuplicateFrame(9)])
    assert sorted(_pairs(out)) == [("VL-001", 5), ("VL-003", 9)]


def test_vl002_overflow_safe(write_faulty: WriteFaulty, make_ctx: MakeCtx) -> None:
    ctx = make_ctx(write_faulty())
    facts = ctx.facts.head(4).copy()
    wrapped = int((np.array([2**62], dtype=np.int64) * np.int64(ctx.frame_interval_ns))[0])
    facts.loc[1, "frame_id"] = 2**62
    facts.loc[1, "timestamp_ns"] = wrapped  # what an overflowing multiply would call correct
    facts.loc[2, "frame_id"] = -5
    out = RULES[1].check(replace(ctx, facts=facts))
    assert out["frame_id"].tolist() == [2**62, -5]
    assert out["row_index"].tolist() == [1, 2]


def test_vl003_three_identical_copies(
    write_faulty: WriteFaulty, make_ctx: MakeCtx, clean_drive: Drive
) -> None:
    ids, ts, pts = columns(clean_drive)
    for _ in range(2):
        ids.insert(301, ids[300])
        ts.insert(301, ts[300])
        pts.insert(301, pts[300])
    out = run_rules(make_ctx(write_faulty(table=build_table(ids, ts, pts))), "frame")
    assert _pairs(out) == [("VL-003", 300), ("VL-003", 300)]
    assert out["row_index"].tolist() == [301, 302]


def test_vl003_three_copies_one_differs(
    write_faulty: WriteFaulty, make_ctx: MakeCtx, clean_drive: Drive
) -> None:
    ids, ts, pts = columns(clean_drive)
    moved = np.frombuffer(pts[300] or b"", dtype="<i4").reshape(-1, 3) + np.array([1, 0, 0])
    ids[301:301] = [ids[300], ids[300]]
    ts[301:301] = [ts[300], ts[300]]
    pts[301:301] = [pts[300], encode_points(moved.astype(np.int32))]
    out = run_rules(make_ctx(write_faulty(table=build_table(ids, ts, pts))), "frame")
    assert out["row_index"].tolist() == [300, 301, 302]
    assert set(out["code"]) == {"VL-003"}


def test_vl003_non_adjacent_copies(
    write_faulty: WriteFaulty, make_ctx: MakeCtx, clean_drive: Drive
) -> None:
    ids, ts, pts = columns(clean_drive)
    ids.insert(500, ids[300])
    ts.insert(500, ts[300])
    pts.insert(500, pts[300])
    out = run_rules(make_ctx(write_faulty(table=build_table(ids, ts, pts))), "frame")
    assert _pairs(out) == [("VL-003", 300)]
    assert out["row_index"].tolist() == [500]  # the later copy; the first row is kept


def _vl006(out: pd.DataFrame) -> list[int]:
    return [int(f) for c, f in zip(out["code"], out["frame_id"], strict=True) if c == "VL-006"]


def test_vl006_teleported_first_frame(write_faulty: WriteFaulty, make_ctx: MakeCtx) -> None:
    out = _frame_scope(write_faulty, make_ctx, [Teleport(0, 1000)])
    assert _pairs(out) == [("VL-006", 0)]


def test_vl006_teleported_second_frame(write_faulty: WriteFaulty, make_ctx: MakeCtx) -> None:
    out = _frame_scope(write_faulty, make_ctx, [Teleport(1, 1000)])
    assert _pairs(out) == [("VL-006", 1)]


def test_vl006_drop_first_then_teleport_next(write_faulty: WriteFaulty, make_ctx: MakeCtx) -> None:
    out = _frame_scope(write_faulty, make_ctx, [DropFrame(0), Teleport(1, 1000)])
    assert sorted(_pairs(out)) == [("VL-001", 0), ("VL-006", 1)]


def test_vl006_corrupt_first_then_teleport_next(
    write_faulty: WriteFaulty, make_ctx: MakeCtx
) -> None:
    out = _frame_scope(write_faulty, make_ctx, [CorruptPoints(0), Teleport(1, 1000)])
    assert sorted(_pairs(out)) == [("VL-004", 0), ("VL-006", 1)]


def test_vl006_teleported_last_frame(write_faulty: WriteFaulty, make_ctx: MakeCtx) -> None:
    out = _frame_scope(write_faulty, make_ctx, [Teleport(999, 1000)])
    assert _pairs(out) == [("VL-006", 999)]


def test_vl006_tiny_drives(
    write_faulty: WriteFaulty, make_ctx: MakeCtx, clean_drive: Drive
) -> None:
    for rows in (0, 1, 2):
        table = clean_drive.frames.slice(0, rows)
        out = run_rules(make_ctx(write_faulty(table=table)), "frame")
        assert _vl006(out) == []


def test_vl005_variants(write_faulty: WriteFaulty, make_ctx: MakeCtx) -> None:
    ctx = make_ctx(write_faulty(), facts=pd.DataFrame())
    base = FRAMES_SCHEMA

    def message(schema: pa.Schema) -> str:
        out = run_rules(replace(ctx, schema=schema), "drive")
        assert _pairs(out) == [("VL-005", None)]
        return str(out.loc[0, "message"])

    retyped = pa.schema([base.field(0), pa.field("timestamp_ns", pa.int32()), base.field(2)])
    assert "different ['timestamp_ns']" in message(retyped)
    assert "unexpected ['extra']" in message(base.append(pa.field("extra", pa.int64())))
    reordered = pa.schema([base.field(1), base.field(0), base.field(2)])
    assert "column order differs" in message(reordered)
    required = pa.schema(
        [pa.field("frame_id", pa.int64(), nullable=False), base.field(1), base.field(2)]
    )
    assert "different ['frame_id']" in message(required)
    assert "missing ['points']" in message(pa.schema([base.field(0), base.field(1)]))


def test_vl006_teleport_between_gap_one_and_gap_two_limits(
    write_faulty: WriteFaulty, make_ctx: MakeCtx
) -> None:
    # 1100 cm is above the gap-1 limit (564.5) and below the gap-2 limit (1127)
    for frame in (0, 1):
        out = _frame_scope(write_faulty, make_ctx, [Teleport(frame, 1100)])
        assert _pairs(out) == [("VL-006", frame)]


def test_vl006_teleport_beyond_gap_two_limit(write_faulty: WriteFaulty, make_ctx: MakeCtx) -> None:
    out = _frame_scope(write_faulty, make_ctx, [Teleport(0, 2000)])
    assert _pairs(out) == [("VL-006", 0)]


def test_vl006_teleport_inside_stationary_start(
    write_faulty: WriteFaulty, make_ctx: MakeCtx
) -> None:
    for frame in (2, 3, 20):
        out = _frame_scope(write_faulty, make_ctx, [Teleport(frame, 1000)])
        assert _pairs(out) == [("VL-006", frame)]


def test_vl006_first_frames_dropped_then_teleport(
    write_faulty: WriteFaulty, make_ctx: MakeCtx
) -> None:
    faults = [DropFrame(i) for i in range(5)] + [Teleport(5, 1000)]
    out = _frame_scope(write_faulty, make_ctx, faults)
    assert sorted(_pairs(out)) == [("VL-001", i) for i in range(5)] + [("VL-006", 5)]
