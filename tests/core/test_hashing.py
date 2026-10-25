"""Tests for canonical JSON and hashing."""

import hashlib
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest
from pydantic import BaseModel

from vl_data.core.config import BodyConfig
from vl_data.core.hashing import (
    canonical_json,
    drive_hash,
    file_sha256,
    scenario_hash,
    sha256_hex,
)
from vl_data.generator.drive import drive_dir_name


class _Seg(BaseModel):
    type: str
    duration_s: float


class _Model(BaseModel):
    seed: int
    speed_cm_s: float
    segments: list[_Seg]


def _model() -> _Model:
    return _Model(
        seed=7,
        speed_cm_s=600.0,
        segments=[_Seg(type="stop", duration_s=0.0625), _Seg(type="move", duration_s=2.0)],
    )


# Pinned literals, written down by hand (not produced by the code under test).
GOLDEN_JSON = (
    '{"seed":7,"segments":[{"duration_s":0.0625,"type":"stop"},'
    '{"duration_s":2,"type":"move"}],"speed_cm_s":600}'
)
GOLDEN_SHA256 = "ece52cbe0f5086b302f2b7a07b42b703f3a7561c974c54f526fe9fa2774137a1"


def test_canonical_json_sorted_compact() -> None:
    assert canonical_json({"b": 1, "a": [1, 2], "c": {"z": 1, "y": 2}}) == (
        '{"a":[1,2],"b":1,"c":{"y":2,"z":1}}'
    )
    assert canonical_json({"v": 600}) == canonical_json({"v": 600.0})
    assert canonical_json({"v": -0.0}) == '{"v":0}'


def test_canonical_json_keeps_non_integral_floats_and_bools() -> None:
    assert canonical_json({"v": 0.1, "w": 1.5e-7}) == '{"v":0.1,"w":1.5e-07}'
    assert canonical_json({"t": True, "f": False, "n": None}) == '{"f":false,"n":null,"t":true}'
    assert canonical_json((1.0, 2.5, "é")) == '[1,2.5,"\\u00e9"]'


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_canonical_json_rejects_non_finite(bad: float) -> None:
    with pytest.raises(ValueError):
        canonical_json({"v": bad})


def test_canonical_json_rejects_non_string_keys() -> None:
    with pytest.raises(TypeError):
        canonical_json({1: "a"})


def test_scenario_hash_golden() -> None:
    model = _model()
    assert canonical_json(model.model_dump(mode="json")) == GOLDEN_JSON
    assert scenario_hash(model) == GOLDEN_SHA256
    # The pinned hash really is the SHA-256 of the pinned string.
    assert hashlib.sha256(GOLDEN_JSON.encode("utf-8")).hexdigest() == GOLDEN_SHA256


def test_scenario_hash_ignores_key_order() -> None:
    a = _Model.model_validate(
        {"seed": 7, "speed_cm_s": 600, "segments": [{"type": "stop", "duration_s": 1}]}
    )
    b = _Model.model_validate(
        {"segments": [{"duration_s": 1.0, "type": "stop"}], "speed_cm_s": 600.0, "seed": 7}
    )
    assert scenario_hash(a) == scenario_hash(b)
    assert scenario_hash(a) != scenario_hash(_model())


def test_sha256_hex_is_lowercase_64() -> None:
    digest = sha256_hex(b"abc")
    assert digest == hashlib.sha256(b"abc").hexdigest()
    assert len(digest) == 64 and digest == digest.lower()


def test_file_sha256_matches_hashlib(tmp_path: Path) -> None:
    data = bytes(range(256)) * 1000
    path = tmp_path / "f.bin"
    path.write_bytes(data)
    assert file_sha256(path) == hashlib.sha256(data).hexdigest()


# With a body the hash covers {"body": ..., "scenario": ...}; the string is written by hand.
GOLDEN_BODY_JSON = '{"body":{"points_per_axis":100,"size_cm":100},"scenario":' + GOLDEN_JSON + "}"
GOLDEN_BODY_SHA256 = "e8f216e4a0a12afb5eea257f10ab78067e7325e6cbef829651f4052d926697b3"


def test_scenario_hash_with_body_golden() -> None:
    body = BodyConfig(size_cm=100, points_per_axis=100)
    assert scenario_hash(_model(), body) == GOLDEN_BODY_SHA256
    assert hashlib.sha256(GOLDEN_BODY_JSON.encode("utf-8")).hexdigest() == GOLDEN_BODY_SHA256
    # a plain dict body gives the same hash
    assert scenario_hash(_model(), {"size_cm": 100, "points_per_axis": 100}) == GOLDEN_BODY_SHA256


def test_drive_hash_from_plain_dicts_matches_scenario_hash() -> None:
    body = {"size_cm": 100, "points_per_axis": 100}
    assert drive_hash(_model().model_dump(mode="json"), body) == GOLDEN_BODY_SHA256


def test_scenario_hash_without_body_is_unchanged() -> None:
    assert scenario_hash(_model(), None) == GOLDEN_SHA256


def test_density_changes_hash_and_folder() -> None:
    small = scenario_hash(_model(), BodyConfig(size_cm=100, points_per_axis=10))
    full = scenario_hash(_model(), BodyConfig(size_cm=100, points_per_axis=100))
    assert small != full
    assert small[:12] != full[:12]
    assert drive_dir_name(small) != drive_dir_name(full)
    # the size also counts
    assert scenario_hash(_model(), BodyConfig(size_cm=200, points_per_axis=10)) != small


def test_drive_hash_coerces_integral_floats_in_the_body() -> None:
    """100.0 and 100 give the same drive hash (body values are int-coerced)."""
    scenario = _model().model_dump(mode="json")
    as_float = drive_hash(scenario, {"size_cm": 100.0, "points_per_axis": 100.0})
    assert as_float == drive_hash(scenario, {"size_cm": 100, "points_per_axis": 100})
    assert as_float == GOLDEN_BODY_SHA256


@pytest.mark.parametrize(
    "body",
    [
        {"size_cm": 100.5, "points_per_axis": 100},
        {"size_cm": 100, "points_per_axis": 99.9},
        {"size_cm": float("nan"), "points_per_axis": 100},
        {"size_cm": 100, "points_per_axis": float("inf")},
    ],
)
def test_drive_hash_rejects_a_non_integral_body(body: dict[str, float]) -> None:
    """int() used to truncate 100.5 to 100, a collision with the 100 folder."""
    scenario = _model().model_dump(mode="json")
    with pytest.raises(ValueError, match="whole number"):
        drive_hash(scenario, body)
    with pytest.raises(ValueError, match="whole number"):
        scenario_hash(_model(), body)


@pytest.mark.parametrize(
    "bad",
    [
        True,
        False,
        np.bool_(True),
        np.bool_(False),
        "100",
        np.float32(100.5),
        Decimal("100.5"),
        None,
        [100],
        Decimal("NaN"),
    ],
)
def test_drive_hash_rejects_bool_str_and_non_integral_numeric_types(bad: object) -> None:
    scenario = _model().model_dump(mode="json")
    with pytest.raises(ValueError, match="whole number"):
        drive_hash(scenario, {"size_cm": bad, "points_per_axis": 100})
    with pytest.raises(ValueError, match="whole number"):
        drive_hash(scenario, {"size_cm": 100, "points_per_axis": bad})


@pytest.mark.parametrize(
    "good", [np.int64(100), np.float64(100.0), np.float32(100.0), Decimal("100")]
)
def test_drive_hash_accepts_integral_numpy_and_decimal_values(good: object) -> None:
    scenario = _model().model_dump(mode="json")
    assert drive_hash(scenario, {"size_cm": good, "points_per_axis": good}) == GOLDEN_BODY_SHA256
