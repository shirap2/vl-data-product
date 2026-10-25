"""Tests for the cube body. Units: cm."""

import numpy as np
import pytest

from vl_data.core.schemas import ARROW_BINARY_LIMIT, BYTES_PER_POINT
from vl_data.generator.body import cube_lattice, place_body


def test_lattice_shape_and_corners() -> None:
    lat = cube_lattice(100, 10)
    assert lat.shape == (1000, 3)
    axis = [0, 11, 22, 33, 44, 55, 66, 77, 88, 99]
    # x-major: flat index = ix*100 + iy*10 + iz
    for flat in (0, 1, 10, 100, 123, 999):
        ix, iy, iz = flat // 100, (flat // 10) % 10, flat % 10
        assert lat[flat].tolist() == [axis[ix], axis[iy], axis[iz]]
    assert set(np.unique(lat).tolist()) == set(axis)
    corner_idx = [0, 9, 90, 99, 900, 909, 990, 999]
    corners = {tuple(lat[i].tolist()) for i in corner_idx}
    assert corners == {(x, y, z) for x in (0, 99) for y in (0, 99) for z in (0, 99)}
    assert len(corners) == 8
    # all 1000 rows are distinct
    assert len({tuple(row) for row in lat.tolist()}) == 1000


def test_lattice_dtype_and_contiguity() -> None:
    lat = cube_lattice(100, 10)
    assert lat.dtype == np.int32
    assert lat.flags["C_CONTIGUOUS"]


def test_lattice_small_cube() -> None:
    lat = cube_lattice(2, 2)
    assert lat.tolist() == [
        [0, 0, 0],
        [0, 0, 1],
        [0, 1, 0],
        [0, 1, 1],
        [1, 0, 0],
        [1, 0, 1],
        [1, 1, 0],
        [1, 1, 1],
    ]


@pytest.mark.parametrize(("size", "n"), [(0, 10), (-5, 10), (100, 1), (100, 0)])
def test_lattice_argument_errors(size: int, n: int) -> None:
    with pytest.raises(ValueError):
        cube_lattice(size, n)


def test_one_cm_shift_is_exact() -> None:
    lat = cube_lattice(100, 10)
    for start in (0.0, 7.0, -3.0, 100.0, 12345.0, -99999.0):
        a = place_body(lat, start, 5.0, -2.0)
        b = place_body(lat, start + 1.0, 5.0, -2.0)
        assert a.dtype == np.int32 and b.dtype == np.int32
        diff = b - a
        assert diff.shape == (1000, 3), f"start={start}"
        assert (diff == np.array([1, 0, 0], dtype=np.int32)).all(), f"start={start}"


def test_place_body_values_and_rounding() -> None:
    lat = cube_lattice(2, 2)
    out = place_body(lat, 10.0, 20.0, 30.0)
    assert out[0].tolist() == [10, 20, 30]
    assert out[-1].tolist() == [11, 21, 31]
    # rint rounds half to even
    assert place_body(lat, 0.5, 1.5, 2.5)[0].tolist() == [0, 2, 2]
    assert place_body(lat, 0.4, 0.0, 0.0)[0].tolist() == [0, 0, 0]


def test_place_body_does_not_mutate_lattice() -> None:
    lat = cube_lattice(100, 10)
    before = lat.copy()
    place_body(lat, 50.0, 0.0, 0.0)
    assert (lat == before).all()


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_place_body_rejects_non_finite(bad: float) -> None:
    lat = cube_lattice(100, 10)
    for args in ((bad, 0.0, 0.0), (0.0, bad, 0.0), (0.0, 0.0, bad)):
        with pytest.raises(ValueError):
            place_body(lat, *args)


def test_place_body_rejects_int32_overflow() -> None:
    lat = cube_lattice(100, 10)
    with pytest.raises(ValueError):
        place_body(lat, 3.0e9, 0.0, 0.0)
    with pytest.raises(ValueError):
        place_body(lat, 0.0, -3.0e9, 0.0)
    with pytest.raises(ValueError):
        place_body(lat, 0.0, 0.0, float(2**31))
    # the largest legal value still works
    out = place_body(cube_lattice(2, 2), float(2**31 - 2), 0.0, 0.0)
    assert out[:, 0].max() == 2**31 - 1


def test_lattice_rejects_repeated_points() -> None:
    with pytest.raises(ValueError, match="repeated"):
        cube_lattice(2, 5)
    lat = cube_lattice(10, 10)
    assert lat.shape == (1000, 3)
    assert len({tuple(row) for row in lat.tolist()}) == 1000


def test_lattice_rejects_a_cube_past_the_int32_range() -> None:
    """Axis values reach size_cm - 1; above int32 they used to wrap silently."""
    assert int(cube_lattice(2**31, 2).max()) == 2**31 - 1  # the last size that fits
    with pytest.raises(ValueError, match="int32"):
        cube_lattice(2**31 + 1, 2)
    with pytest.raises(ValueError, match="int32"):
        cube_lattice(10**12, 5)


def test_lattice_rejects_too_many_points_before_allocating() -> None:
    """points_per_axis above 1,000,000 raises at once (a cube of it would be 10**18 points)."""
    with pytest.raises(ValueError, match="1000000"):
        cube_lattice(2_000_000, 1_000_001)


def test_lattice_rejects_a_frame_too_big_for_arrow_before_allocating() -> None:
    """points_per_axis 1,000,000 passed the old bound and would try 10**18 points."""
    with pytest.raises(ValueError, match="points_per_axis"):
        cube_lattice(2_000_000, 1_000_000)
    # the Arrow boundary, pinned without allocating: 563 fits, 564 does not
    assert 563**3 * BYTES_PER_POINT < ARROW_BINARY_LIMIT <= 564**3 * BYTES_PER_POINT
    with pytest.raises(ValueError, match="points_per_axis"):
        cube_lattice(1000, 564)


def test_lattice_still_builds_the_shipped_full_density() -> None:
    assert cube_lattice(100, 100).shape == (1_000_000, 3)
