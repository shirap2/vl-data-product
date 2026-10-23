"""Tests for the Arrow schemas and point encoding."""

import numpy as np
import pyarrow as pa
import pytest

from vl_data.core.schemas import (
    FRAMES_SCHEMA,
    GROUND_TRUTH_SCHEMA,
    SCHEMA_VERSION,
    decode_points,
    encode_points,
)


def test_points_roundtrip() -> None:
    points = np.arange(30, dtype=np.int32).reshape(10, 3) - 15
    buf = encode_points(points)
    assert len(buf) == 10 * 3 * 4
    out = decode_points(buf)
    assert out.dtype == np.int32
    assert out.shape == (10, 3)
    assert np.array_equal(out, points)
    out[0, 0] = 99  # the result must be writable
    assert encode_points(points) == buf


def test_encoding_is_little_endian_row_major() -> None:
    buf = encode_points(np.array([[1, 2, 3]], dtype=np.int32))
    assert buf == bytes([1, 0, 0, 0, 2, 0, 0, 0, 3, 0, 0, 0])
    assert encode_points(np.array([[-1, 0, 0]], dtype=np.int64)) == bytes([255] * 4 + [0] * 8)


def test_empty_points_roundtrip() -> None:
    assert decode_points(encode_points(np.zeros((0, 3), dtype=np.int32))).shape == (0, 3)


def test_encode_rejects_bad_shape_and_dtype() -> None:
    with pytest.raises(ValueError):
        encode_points(np.zeros((4, 2), dtype=np.int32))
    with pytest.raises(ValueError):
        encode_points(np.zeros(6, dtype=np.int32))
    with pytest.raises(ValueError):
        encode_points(np.zeros((2, 3), dtype=np.float64))
    with pytest.raises(ValueError):
        encode_points(np.array([[2**40, 0, 0]], dtype=np.int64))


def test_decode_rejects_bad_length() -> None:
    with pytest.raises(ValueError, match="multiple of 12"):
        decode_points(b"\x00" * 13)


def test_schemas() -> None:
    assert SCHEMA_VERSION == 1
    assert FRAMES_SCHEMA.names == ["frame_id", "timestamp_ns", "points"]
    assert FRAMES_SCHEMA.field("points").type == pa.binary()
    assert FRAMES_SCHEMA.field("frame_id").type == pa.int64()
    assert GROUND_TRUTH_SCHEMA.names == [
        "frame_id",
        "timestamp_ns",
        "x_cm",
        "velocity_cm_s",
        "acceleration_cm_s2",
    ]
    assert GROUND_TRUTH_SCHEMA.field("x_cm").type == pa.float64()
    assert GROUND_TRUTH_SCHEMA.field("timestamp_ns").type == pa.int64()


def test_encode_points_layout_independent() -> None:
    base = np.arange(60, dtype=np.int32).reshape(20, 3) - 30
    expected_all = encode_points(np.ascontiguousarray(base))
    sliced = base[::2]
    assert not sliced.flags.c_contiguous
    fortran = np.asfortranarray(base)
    big = base.astype(">i4")
    assert encode_points(sliced) == encode_points(np.ascontiguousarray(sliced))
    assert encode_points(fortran) == expected_all
    assert encode_points(big) == expected_all
    for arr in (sliced, fortran, big):
        assert np.array_equal(decode_points(encode_points(arr)), arr)
