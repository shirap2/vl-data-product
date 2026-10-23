"""Tests for canonical JSON and hashing."""

import hashlib
from pathlib import Path

import pytest
from pydantic import BaseModel

from vl_data.core.hashing import canonical_json, file_sha256, scenario_hash, sha256_hex


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
