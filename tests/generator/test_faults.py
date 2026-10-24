"""Tests for fault injection. Units: cm, ns."""

from dataclasses import replace
from pathlib import Path

import numpy as np
import pyarrow as pa
import pytest

from vl_data.core.config import Config
from vl_data.core.manifest import verify_drive
from vl_data.core.schemas import decode_points
from vl_data.generator import Drive, generate, write_drive
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
from vl_data.generator.models import Scenario

FRAME = 5  # a frame with a neighbour on each side


@pytest.fixture(scope="module")
def config() -> Config:
    """Default config (16 fps, 100 cm cube, 10 points per axis)."""
    return Config()


@pytest.fixture(scope="module")
def drive(config: Config) -> Drive:
    """A small drive: 48 frames (3 s at 16 fps)."""
    scenario = Scenario.model_validate(
        {
            "segments": [
                {"type": "stop", "duration_s": 1.0},
                {"type": "move", "duration_s": 1.0, "end_speed_cm_s": 100.0},
                {"type": "move", "duration_s": 1.0, "end_speed_cm_s": 0.0},
            ]
        }
    )
    return generate(scenario, config)


def _col(drive: Drive, name: str) -> list[int]:
    return [int(v) for v in drive.frames.column(name).to_pylist()]


def test_vl001_drop_frame(drive: Drive) -> None:
    """One frame is missing and the count is 1 for VL-001."""
    new, counts = inject_faults(drive, [DropFrame(FRAME)])
    ids = _col(new, "frame_id")
    assert counts == {"VL-001": 1}
    assert len(ids) == drive.frames.num_rows - 1
    assert FRAME not in ids
    assert ids == [i for i in _col(drive, "frame_id") if i != FRAME]


def test_vl002_shift_timestamp(drive: Drive, config: Config) -> None:
    """One timestamp is moved and the count is 1 for VL-002."""
    delta = config.frame_interval_ns // 2  # ns
    new, counts = inject_faults(drive, [ShiftTimestamp(FRAME, delta)])
    old_ts, new_ts = _col(drive, "timestamp_ns"), _col(new, "timestamp_ns")
    assert counts == {"VL-002": 1}
    changed = [i for i, (a, b) in enumerate(zip(old_ts, new_ts, strict=True)) if a != b]
    assert changed == [FRAME]
    assert new_ts[FRAME] - old_ts[FRAME] == delta
    assert _col(new, "frame_id") == _col(drive, "frame_id")


def test_vl003_duplicate_frame(drive: Drive) -> None:
    """A frame_id appears twice and the count is 1 for VL-003."""
    new, counts = inject_faults(drive, [DuplicateFrame(FRAME)])
    ids = _col(new, "frame_id")
    assert counts == {"VL-003": 1}
    assert ids.count(FRAME) == 2
    assert ids[FRAME : FRAME + 2] == [FRAME, FRAME]
    assert len(ids) == drive.frames.num_rows + 1
    assert _col(new, "timestamp_ns")[FRAME] == _col(new, "timestamp_ns")[FRAME + 1]
    pts = new.frames.column("points").to_pylist()
    assert pts[FRAME] == pts[FRAME + 1]


def test_vl004_corrupt_points(drive: Drive, config: Config) -> None:
    """A frame has one point fewer (length (n-1)*12 bytes) and the count is 1 for VL-004."""
    new, counts = inject_faults(drive, [CorruptPoints(FRAME)])
    n = config.body.points_per_axis**3  # points per frame
    old_pts = drive.frames.column("points").to_pylist()
    new_pts = new.frames.column("points").to_pylist()
    assert counts == {"VL-004": 1}
    assert len(old_pts[FRAME]) == n * 12
    assert len(new_pts[FRAME]) == (n - 1) * 12
    assert new_pts[FRAME] == old_pts[FRAME][:-12]
    assert all(a == b for i, (a, b) in enumerate(zip(old_pts, new_pts, strict=True)) if i != FRAME)


def test_vl005_break_schema(drive: Drive) -> None:
    """The points column is renamed and the count is 1 for VL-005."""
    new, counts = inject_faults(drive, [BreakSchema()])
    assert counts == {"VL-005": 1}
    assert "points" not in new.frames.schema.names
    assert new.frames.schema.names == ["frame_id", "timestamp_ns", "pts"]
    assert new.frames.column("pts").to_pylist() == drive.frames.column("points").to_pylist()


def test_vl006_teleport(drive: Drive) -> None:
    """A frame jumps by dx_cm in x and the count is 1 for VL-006."""
    dx = 5000  # cm
    new, counts = inject_faults(drive, [Teleport(FRAME, dx)])
    assert counts == {"VL-006": 1}
    old_pts = drive.frames.column("points").to_pylist()
    new_pts = new.frames.column("points").to_pylist()
    for i, (a, b) in enumerate(zip(old_pts, new_pts, strict=True)):
        old_p, new_p = decode_points(a), decode_points(b)
        expected = np.zeros(3, dtype=np.int64)  # cm
        if i == FRAME:
            expected[0] = dx
        assert np.array_equal(new_p.astype(np.int64) - old_p, np.tile(expected, (len(old_p), 1)))


@pytest.mark.parametrize(
    "fault",
    [
        DropFrame(3),
        ShiftTimestamp(3, 1000),
        DuplicateFrame(3),
        CorruptPoints(7),
        BreakSchema(),
        Teleport(7, 5000),
    ],
    ids=["drop", "shift", "duplicate", "corrupt", "schema", "teleport"],
)
def test_faulty_drive_still_verifies(
    drive: Drive, config: Config, tmp_path: Path, fault: Fault
) -> None:
    """A faulty drive written to disk passes verify_drive, for every fault."""
    new, _ = inject_faults(drive, [fault])
    path = write_drive(new, tmp_path, config)
    assert verify_drive(path).ok


def test_ground_truth_and_hash_unchanged(drive: Drive) -> None:
    """Only the frames table changes."""
    new, _ = inject_faults(drive, [DropFrame(FRAME)])
    assert new.ground_truth is drive.ground_truth
    assert new.scenario_hash == drive.scenario_hash
    assert new.scenario == drive.scenario


def test_input_drive_not_mutated(drive: Drive) -> None:
    """The input drive's frames are unchanged after injection."""
    before = drive.frames
    snapshot = before.to_pylist()
    inject_faults(drive, [DropFrame(1), DuplicateFrame(2), BreakSchema()])
    assert before.to_pylist() == snapshot


def test_no_faults_gives_empty_counts(drive: Drive) -> None:
    """No faults: same frames, empty counts."""
    new, counts = inject_faults(drive, [])
    assert counts == {}
    assert new.frames.equals(drive.frames)


def test_multiple_faults_at_once(drive: Drive, config: Config) -> None:
    """Different frames, all codes: counts per code, targets found in the original drive."""
    faults: list[Fault] = [
        CorruptPoints(10),
        DropFrame(2),
        DuplicateFrame(8),
        ShiftTimestamp(20, 1000),
        Teleport(30, -700),
        BreakSchema(),
    ]
    new, counts = inject_faults(drive, faults)
    assert counts == {
        "VL-004": 1,
        "VL-001": 1,
        "VL-003": 1,
        "VL-002": 1,
        "VL-006": 1,
        "VL-005": 1,
    }
    ids = _col(new, "frame_id")
    assert 2 not in ids
    assert ids.count(8) == 2
    assert drive.frames.num_rows == len(ids)  # one dropped, one added
    n = config.body.points_per_axis**3  # points per frame
    pts = new.frames.column("pts").to_pylist()
    assert len(pts[ids.index(10)]) == (n - 1) * 12
    assert ids[7:9] == [8, 8]  # duplicate sits right after the original (frame 2 dropped)
    ts = _col(new, "timestamp_ns")
    assert ts[ids.index(20)] == _col(drive, "timestamp_ns")[20] + 1000
    old_pts = decode_points(drive.frames.column("points").to_pylist()[30])
    new_pts = decode_points(pts[ids.index(30)])
    assert np.array_equal(
        new_pts[:, 0].astype(np.int64) - old_pts[:, 0], np.full(len(old_pts), -700)
    )
    assert np.array_equal(new_pts[:, 1:], old_pts[:, 1:])


def test_repeated_code_on_different_frames_counts_objects(drive: Drive) -> None:
    """The count is the number of injected fault objects per code."""
    _, counts = inject_faults(drive, [DropFrame(1), DropFrame(3), DropFrame(9)])
    assert counts == {"VL-001": 3}


@pytest.mark.parametrize(
    "faults",
    [
        [DropFrame(9999)],
        [ShiftTimestamp(9999, 5)],
        [DuplicateFrame(-1)],
        [CorruptPoints(9999)],
        [Teleport(9999, 5)],
    ],
)
def test_unknown_frame_id_raises(drive: Drive, faults: list[Fault]) -> None:
    """A frame_id not in the original drive is a ValueError."""
    with pytest.raises(ValueError, match="unknown frame_id"):
        inject_faults(drive, faults)


@pytest.mark.parametrize(
    "faults",
    [
        [DropFrame(FRAME), DuplicateFrame(FRAME)],
        [DropFrame(FRAME), DropFrame(FRAME)],
        [CorruptPoints(FRAME), Teleport(FRAME, 5)],
    ],
)
def test_two_faults_on_same_frame_raise(drive: Drive, faults: list[Fault]) -> None:
    """Two faults on one frame_id (or a repeated fault) are a ValueError, not stacked."""
    with pytest.raises(ValueError, match="more than one fault"):
        inject_faults(drive, faults)


def test_two_break_schema_raise(drive: Drive) -> None:
    """At most one BreakSchema is allowed."""
    with pytest.raises(ValueError, match="BreakSchema"):
        inject_faults(drive, [BreakSchema(), BreakSchema()])


def test_zero_delta_raises(drive: Drive) -> None:
    """delta_ns == 0 is a ValueError."""
    with pytest.raises(ValueError, match="delta_ns"):
        inject_faults(drive, [ShiftTimestamp(FRAME, 0)])


def test_zero_dx_raises(drive: Drive) -> None:
    """dx_cm == 0 is a ValueError."""
    with pytest.raises(ValueError, match="dx_cm"):
        inject_faults(drive, [Teleport(FRAME, 0)])


@pytest.mark.parametrize(("frame_id", "sign"), [(0, 1), (FRAME, 1), (FRAME, -1), (47, -1)])
def test_shift_onto_neighbour_raises(
    drive: Drive, config: Config, frame_id: int, sign: int
) -> None:
    """A shift that makes the timestamp equal a neighbour's is a ValueError."""
    delta = sign * config.frame_interval_ns  # ns
    with pytest.raises(ValueError, match="neighbour"):
        inject_faults(drive, [ShiftTimestamp(frame_id, delta)])


def test_shift_onto_non_neighbour_raises(drive: Drive, config: Config) -> None:
    """A shift onto a non-adjacent row's timestamp is a ValueError."""
    with pytest.raises(ValueError, match="same timestamp"):
        inject_faults(drive, [ShiftTimestamp(3, 2 * config.frame_interval_ns)])


def test_two_shifts_colliding_raise(drive: Drive, config: Config) -> None:
    """Two shifts that land on the same timestamp are a ValueError."""
    iv = config.frame_interval_ns
    with pytest.raises(ValueError, match="same timestamp"):
        inject_faults(drive, [ShiftTimestamp(3, iv // 2), ShiftTimestamp(4, -(iv // 2))])


def test_shift_onto_duplicated_frame_raises(drive: Drive, config: Config) -> None:
    """A shift onto the timestamp of a duplicated frame is a ValueError."""
    with pytest.raises(ValueError, match="same timestamp"):
        inject_faults(drive, [DuplicateFrame(5), ShiftTimestamp(3, 2 * config.frame_interval_ns)])


def test_valid_pair_of_shifts_works(drive: Drive, config: Config) -> None:
    """Two shifts that keep all timestamps distinct are applied."""
    iv = config.frame_interval_ns
    new, counts = inject_faults(drive, [ShiftTimestamp(3, iv // 4), ShiftTimestamp(4, iv // 4)])
    old_ts, new_ts = _col(drive, "timestamp_ns"), _col(new, "timestamp_ns")
    assert counts == {"VL-002": 2}
    assert new_ts[3] == old_ts[3] + iv // 4
    assert new_ts[4] == old_ts[4] + iv // 4


def test_duplicate_frame_ids_in_original_raise(drive: Drive) -> None:
    """An original table with repeated frame_ids is a ValueError."""
    dup, _ = inject_faults(drive, [DuplicateFrame(2)])
    with pytest.raises(ValueError, match="frame_id values are not unique"):
        inject_faults(dup, [DropFrame(1)])


def test_shift_out_of_int64_raises(drive: Drive) -> None:
    """A shift whose result leaves int64 is a ValueError."""
    with pytest.raises(ValueError, match="int64"):
        inject_faults(drive, [ShiftTimestamp(FRAME, 2**63)])
    with pytest.raises(ValueError, match="int64"):
        inject_faults(drive, [ShiftTimestamp(FRAME, -(2**64))])


def test_teleport_int32_overflow_raises(drive: Drive) -> None:
    """A dx_cm that pushes a coordinate out of int32 is a ValueError."""
    with pytest.raises(ValueError):
        inject_faults(drive, [Teleport(FRAME, 2**31)])


def test_corrupt_points_on_empty_cell_raises(drive: Drive) -> None:
    """A frame with fewer than 12 bytes of points cannot be corrupted."""
    frames = drive.frames
    cols = [frames.column(n).to_pylist() for n in frames.schema.names]
    cols[2][FRAME] = b""
    table = pa.Table.from_arrays(
        [pa.array(c, type=f.type) for c, f in zip(cols, frames.schema, strict=True)],
        schema=frames.schema,
    )
    with pytest.raises(ValueError, match="12 bytes"):
        inject_faults(replace(drive, frames=table), [CorruptPoints(FRAME)])


def test_zero_row_drive_unknown_frame_id(drive: Drive) -> None:
    """On a drive with no rows every frame_id is unknown."""
    empty = replace(drive, frames=drive.frames.slice(0, 0))
    with pytest.raises(ValueError, match="unknown frame_id"):
        inject_faults(empty, [DropFrame(0)])


@pytest.mark.parametrize("row", [0, 47], ids=["first", "last"])
def test_edge_rows_drop_duplicate_corrupt(drive: Drive, row: int) -> None:
    """Drop, duplicate and corrupt work on the first and last frame."""
    ids = _col(drive, "frame_id")
    old_pts = drive.frames.column("points").to_pylist()
    new, _ = inject_faults(drive, [DropFrame(row)])
    assert _col(new, "frame_id") == ids[:row] + ids[row + 1 :]
    new, _ = inject_faults(drive, [DuplicateFrame(row)])
    assert _col(new, "frame_id") == ids[: row + 1] + ids[row:]
    new, _ = inject_faults(drive, [CorruptPoints(row)])
    assert new.frames.column("points").to_pylist()[row] == old_pts[row][:-12]


@pytest.mark.parametrize("row", [0, 47], ids=["first", "last"])
def test_edge_rows_teleport(drive: Drive, row: int) -> None:
    """Teleport works on the first and last frame."""
    new, _ = inject_faults(drive, [Teleport(row, 300)])
    old_p = decode_points(drive.frames.column("points").to_pylist()[row])
    new_p = decode_points(new.frames.column("points").to_pylist()[row])
    assert np.array_equal(new_p[:, 0].astype(np.int64) - old_p[:, 0], np.full(len(old_p), 300))


def test_teleport_negative_dx(drive: Drive) -> None:
    """A negative dx_cm moves x down by that many cm; y and z are unchanged."""
    new, _ = inject_faults(drive, [Teleport(FRAME, -250)])
    old_p = decode_points(drive.frames.column("points").to_pylist()[FRAME])
    new_p = decode_points(new.frames.column("points").to_pylist()[FRAME])
    assert np.array_equal(new_p[:, 0].astype(np.int64) - old_p[:, 0], np.full(len(old_p), -250))
    assert np.array_equal(new_p[:, 1:], old_p[:, 1:])
