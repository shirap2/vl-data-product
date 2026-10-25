"""Tests for the drive-body adapter."""

from collections.abc import Callable

import pytest

from vl_data.core.manifest import BodyInfo, Manifest, read_manifest
from vl_data.etl.body import drive_body, expected_frame_count
from vl_data.generator.faults import DropFrame, DuplicateFrame

from .helpers import Written


def _manifest(points_per_axis: int, size_cm: int = 100) -> Manifest:
    return Manifest(
        schema_version=2,
        scenario_hash="0" * 64,
        seed=0,
        fps=16,
        frame_count=1,
        points_per_frame=points_per_axis**3,
        body=BodyInfo(size_cm=size_cm, points_per_axis=points_per_axis),
        scenario={},
        files={},
    )


@pytest.mark.parametrize("axis", [2, 10, 100])
def test_body_from_manifest(axis: int) -> None:
    body = drive_body(_manifest(axis, size_cm=150))
    assert body.points_per_axis == axis
    assert body.size_cm == 150


def test_body_rejects_single_point_lattice() -> None:
    with pytest.raises(ValueError):
        drive_body(_manifest(1))


def test_expected_frame_count_ignores_row_count(write_faulty: Callable[..., Written]) -> None:
    clean = read_manifest(write_faulty().drive_dir)
    assert expected_frame_count(clean) == 1000
    faulty = read_manifest(write_faulty([DuplicateFrame(5), DuplicateFrame(6)]).drive_dir)
    assert faulty.frame_count == 1002  # the file rows, not the frames
    assert expected_frame_count(faulty) == 1000
    dropped = read_manifest(write_faulty([DropFrame(5)]).drive_dir)
    assert dropped.frame_count == 999
    assert expected_frame_count(dropped) == 1000


def test_expected_frame_count_bad_scenario_raises() -> None:
    with pytest.raises(ValueError):
        expected_frame_count(_manifest(2))  # scenario={} does not validate
