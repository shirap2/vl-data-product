"""Tests for keypoint extraction."""

import numpy as np
import pytest
from numpy.typing import NDArray

from vl_data.etl.config import EtlConfig, KeypointSpec
from vl_data.etl.keypoints import extract_keypoints
from vl_data.generator.body import cube_lattice

SPECS = EtlConfig().keypoints


def _frames(lattice: NDArray[np.int32], n: int = 1) -> NDArray[np.int32]:
    return np.repeat(lattice[None, :, :], n, axis=0)


def test_corners_by_index() -> None:
    out = extract_keypoints(_frames(cube_lattice(100, 10)), SPECS)
    for k, spec in enumerate(SPECS[:8]):
        x, y, z = (int(c) for c in spec.name.removeprefix("corner_"))
        np.testing.assert_array_equal(out[0, k], [x * 99, y * 99, z * 99])


def test_centroid_is_mean() -> None:
    shifted = cube_lattice(100, 10) + np.array([10, 20, 30], dtype=np.int32)
    out = extract_keypoints(_frames(shifted), SPECS)
    np.testing.assert_allclose(out[0, 8], [59.5, 69.5, 79.5])


def test_shift_moves_all_keypoints() -> None:
    base = _frames(cube_lattice(100, 10))
    moved = base + np.array([1, 0, 0], dtype=np.int32)
    delta = extract_keypoints(moved, SPECS) - extract_keypoints(base, SPECS)
    np.testing.assert_array_equal(delta, np.tile([1.0, 0.0, 0.0], (1, 9, 1)))


def test_frame_order_kept() -> None:
    lat = cube_lattice(100, 10)
    pts = np.stack([lat, lat + 5, lat + 7]).astype(np.int32)
    out = extract_keypoints(pts, SPECS)
    assert out.shape == (3, 9, 3)
    np.testing.assert_array_equal(out[1, 0], [5, 5, 5])
    np.testing.assert_array_equal(out[2, 0], [7, 7, 7])


def test_dtype_and_shape() -> None:
    out = extract_keypoints(_frames(cube_lattice(100, 10), 4), SPECS[:2])
    assert out.dtype == np.float64
    assert out.shape == (4, 2, 3)


def test_spec_order_respected() -> None:
    specs = [SPECS[8], SPECS[3]]
    out = extract_keypoints(_frames(cube_lattice(100, 10)), specs)
    np.testing.assert_allclose(out[0, 0], [49.5, 49.5, 49.5])
    np.testing.assert_array_equal(out[0, 1], [0, 99, 99])


def test_centroid_no_int32_overflow() -> None:
    # numpy's mean already accumulates in float64; this pins that large values stay exact.
    big = np.full((1, 4, 3), np.iinfo(np.int32).max, dtype=np.int32)
    out = extract_keypoints(big, [KeypointSpec(name="c", kind="centroid")])
    assert out[0, 0, 0] == float(np.iinfo(np.int32).max)


@pytest.mark.parametrize("shape", [(10, 3), (1, 10, 2), (1, 1, 10, 3)])
def test_bad_shapes_rejected(shape: tuple[int, ...]) -> None:
    with pytest.raises(ValueError):
        extract_keypoints(np.zeros(shape, dtype=np.int32), SPECS)


def test_zero_points_rejected() -> None:
    with pytest.raises(ValueError):
        extract_keypoints(np.zeros((2, 0, 3), dtype=np.int32), SPECS[8:])


def test_index_out_of_range_rejected() -> None:
    with pytest.raises(ValueError):
        extract_keypoints(np.zeros((1, 10, 3), dtype=np.int32), [SPECS[2]])


def test_index_boundary() -> None:
    pts = np.arange(30, dtype=np.int32).reshape(1, 10, 3)
    last = KeypointSpec(name="last", index=9)
    np.testing.assert_array_equal(extract_keypoints(pts, [last])[0, 0], [27, 28, 29])
    with pytest.raises(ValueError):
        extract_keypoints(pts, [KeypointSpec(name="past", index=10)])


def test_negative_index_rejected() -> None:
    spec = KeypointSpec.model_construct(name="neg", kind="index", index=-1)
    with pytest.raises(ValueError):
        extract_keypoints(np.zeros((1, 10, 3), dtype=np.int32), [spec])


def test_zero_frames() -> None:
    out = extract_keypoints(np.zeros((0, 1000, 3), dtype=np.int32), SPECS)
    assert out.shape == (0, 9, 3)


def test_empty_specs() -> None:
    out = extract_keypoints(np.zeros((2, 10, 3), dtype=np.int32), [])
    assert out.shape == (2, 0, 3)
    assert out.dtype == np.float64


def test_non_contiguous_and_read_only_input() -> None:
    base = np.arange(2 * 10 * 6, dtype=np.int32).reshape(2, 10, 6)
    view = base[:, :, ::2]
    assert not view.flags["C_CONTIGUOUS"]
    view.flags.writeable = False
    specs = [KeypointSpec(name="p", index=1), KeypointSpec(name="c", kind="centroid")]
    out = extract_keypoints(view, specs)
    np.testing.assert_array_equal(out[0, 0], [6, 8, 10])
    np.testing.assert_allclose(out[1, 1], view[1].mean(axis=0))


@pytest.mark.parametrize("dtype", [np.float32, np.float64])
def test_float_input_rejected(dtype: type) -> None:
    with pytest.raises(ValueError):
        extract_keypoints(np.zeros((1, 10, 3), dtype=dtype), SPECS[:1])  # type: ignore[arg-type]


def test_nan_input_rejected() -> None:
    with pytest.raises(ValueError):
        extract_keypoints(np.full((1, 10, 3), np.nan), SPECS[8:])  # type: ignore[arg-type]


def test_int64_input_accepted() -> None:
    pts = np.arange(30, dtype=np.int64).reshape(1, 10, 3)
    out = extract_keypoints(pts, [KeypointSpec(name="p", index=2)])  # type: ignore[arg-type]
    np.testing.assert_array_equal(out[0, 0], [6, 7, 8])


def test_two_centroid_specs_identical() -> None:
    specs = [
        KeypointSpec(name="c1", kind="centroid"),
        KeypointSpec(name="c2", kind="centroid"),
    ]
    out = extract_keypoints(_frames(cube_lattice(100, 10), 2), specs)
    np.testing.assert_array_equal(out[:, 0], out[:, 1])
