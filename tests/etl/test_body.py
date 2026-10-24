"""Tests for the drive-body adapter."""

from collections.abc import Callable

import pytest

from vl_data.core.config import BodyConfig
from vl_data.core.manifest import Manifest, read_manifest
from vl_data.etl.body import drive_body, expected_frame_count
from vl_data.generator.faults import DropFrame, DuplicateFrame

from .helpers import Written


def _manifest(points_per_frame: int) -> Manifest:
    return Manifest(
        schema_version=1,
        scenario_hash="0" * 64,
        seed=0,
        fps=16,
        frame_count=1,
        points_per_frame=points_per_frame,
        scenario={},
        files={},
    )


def test_body_from_points_per_frame() -> None:
    fallback = BodyConfig(size_cm=150, points_per_axis=10)
    for points, axis in ((1_000, 10), (1_000_000, 100), (8, 2)):
        body = drive_body(_manifest(points), fallback)
        assert body.points_per_axis == axis
        assert body.size_cm == 150


@pytest.mark.parametrize("points", [999, 1001, 0, 1, -8, 10**400])
def test_body_rejects_non_cube(points: int) -> None:
    with pytest.raises(ValueError):
        drive_body(_manifest(points), BodyConfig())


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
        expected_frame_count(_manifest(8))  # scenario={} does not validate
