"""Tests for the drive-body adapter."""

import pytest

from vl_data.core.config import BodyConfig
from vl_data.core.manifest import Manifest
from vl_data.etl.body import drive_body


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
