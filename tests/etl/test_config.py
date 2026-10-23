"""Tests for the ETL config models and loader."""

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from vl_data.etl import config as etl_config
from vl_data.etl.config import EtlConfig, KeypointSpec, LimitsConfig, load_etl_config


@pytest.fixture(autouse=True)
def _no_lake_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VL_LAKE_DIR", raising=False)


CORNERS = {
    "corner_000": 0,
    "corner_001": 9,
    "corner_010": 90,
    "corner_011": 99,
    "corner_100": 900,
    "corner_101": 909,
    "corner_110": 990,
    "corner_111": 999,
}


def _check_defaults(cfg: EtlConfig) -> None:
    assert len(cfg.keypoints) == 9
    assert cfg.limits.max_speed_cm_s == 9000.0
    assert cfg.limits.rounding_margin_cm == 2.0
    assert cfg.chunk_frames == 256
    assert cfg.supported_schema_versions == [1]
    assert cfg.lake_dir == Path("warehouse")
    for spec, (name, index) in zip(cfg.keypoints[:8], CORNERS.items(), strict=True):
        assert (spec.name, spec.kind, spec.index) == (name, "index", index)
    assert (cfg.keypoints[8].name, cfg.keypoints[8].kind) == ("centroid", "centroid")
    assert cfg.keypoints[8].index is None


def test_defaults_load(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VL_LAKE_DIR", raising=False)
    assert etl_config.DEFAULT_ETL_CONFIG_PATH.is_file()
    _check_defaults(load_etl_config())


def test_builtin_defaults_equal_yaml(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VL_LAKE_DIR", raising=False)
    assert EtlConfig() == load_etl_config(etl_config.DEFAULT_ETL_CONFIG_PATH)


def test_env_override_lake_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VL_LAKE_DIR", "somewhere/else")
    assert load_etl_config().lake_dir == Path("somewhere/else")
    monkeypatch.setenv("VL_LAKE_DIR", "")
    assert load_etl_config().lake_dir == Path("warehouse")


def test_env_wins_over_explicit_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "e.yaml"
    path.write_text("lake_dir: other\n", encoding="utf-8")
    assert load_etl_config(path).lake_dir == Path("other")
    monkeypatch.setenv("VL_LAKE_DIR", "from_env")
    assert load_etl_config(path).lake_dir == Path("from_env")


@pytest.mark.parametrize(
    "bad",
    [
        {"name": "a"},
        {"name": "a", "index": -1},
        {"name": "a", "kind": "centroid", "index": 3},
        {"name": "", "index": 0},
        {"name": "a", "kind": "median"},
        {"name": "a", "index": 0, "extra": 1},
        {"name": "   ", "index": 0},
        {"name": " a ", "index": 0},
        {"name": "../x", "index": 0},
        {"name": "a/b", "index": 0},
        {"name": "x" * 65, "index": 0},
        {"name": "a", "index": True},
        {"name": "a", "index": 9.0},
    ],
)
def test_bad_keypoint_spec_rejected(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        KeypointSpec.model_validate(bad)


def test_max_jump_scales_with_gap() -> None:
    cfg = EtlConfig()
    assert cfg.max_jump_cm(16, 1) == pytest.approx(564.5)
    assert cfg.max_jump_cm(16, 2) == pytest.approx(1127.0)
    with pytest.raises(ValueError):
        cfg.max_jump_cm(0, 1)
    with pytest.raises(ValueError):
        cfg.max_jump_cm(16, 0)
    with pytest.raises(ValueError):
        cfg.max_jump_cm(-1, 1)
    with pytest.raises(ValueError):
        cfg.max_jump_cm(16, -1)


@pytest.mark.parametrize("fps", [7, 30])
def test_max_jump_other_fps(fps: int) -> None:
    cfg = EtlConfig()
    assert cfg.max_jump_cm(fps, 1) == pytest.approx(9000.0 / fps + 2.0)
    assert cfg.max_jump_cm(fps, 3) == pytest.approx(9000.0 * 3 / fps + 2.0)
    assert cfg.max_jump_cm(fps, fps) == pytest.approx(9000.0 + 2.0)


def test_max_jump_custom_limits() -> None:
    cfg = EtlConfig(limits=LimitsConfig(rounding_margin_cm=0.0))
    assert cfg.max_jump_cm(16, 1) == pytest.approx(562.5)
    assert cfg.max_jump_cm(16, 16) == pytest.approx(9000.0)


def test_duplicate_names_rejected() -> None:
    with pytest.raises(ValidationError):
        EtlConfig(keypoints=[KeypointSpec(name="a", index=0), KeypointSpec(name="a", index=1)])


def test_empty_keypoints_rejected() -> None:
    with pytest.raises(ValidationError):
        EtlConfig(keypoints=[])


@pytest.mark.parametrize("versions", [[], [0], [-1], list(range(1, 18))])
def test_bad_schema_versions_rejected(versions: list[int]) -> None:
    with pytest.raises(ValidationError):
        EtlConfig(supported_schema_versions=versions)


def test_chunk_frames_zero_rejected() -> None:
    with pytest.raises(ValidationError):
        EtlConfig(chunk_frames=0)


def test_chunk_frames_above_cap_rejected() -> None:
    assert EtlConfig(chunk_frames=100_000).chunk_frames == 100_000
    with pytest.raises(ValidationError):
        EtlConfig(chunk_frames=100_001)


def test_too_many_keypoints_rejected() -> None:
    many = [KeypointSpec(name=f"k{i}", index=i) for i in range(257)]
    assert len(EtlConfig(keypoints=many[:256]).keypoints) == 256
    with pytest.raises(ValidationError):
        EtlConfig(keypoints=many)


def test_max_speed_above_cap_rejected() -> None:
    assert LimitsConfig(max_speed_cm_s=100_000.0).max_speed_cm_s == 100_000.0
    with pytest.raises(ValidationError):
        LimitsConfig(max_speed_cm_s=100_001.0)


@pytest.mark.parametrize(
    "text",
    [
        "keypoints: [{name: a, index: true}]\n",
        "keypoints: [{name: a, index: 9.0}]\n",
        "supported_schema_versions: [true]\n",
        "supported_schema_versions: [1.0]\n",
    ],
)
def test_non_int_yaml_values_rejected(tmp_path: Path, text: str) -> None:
    path = tmp_path / "e.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValidationError):
        load_etl_config(path)


def test_ordinary_int_values_accepted(tmp_path: Path) -> None:
    path = tmp_path / "e.yaml"
    path.write_text(
        "supported_schema_versions: [1, 2]\nkeypoints: [{name: a, index: 9}]\n",
        encoding="utf-8",
    )
    cfg = load_etl_config(path)
    assert cfg.supported_schema_versions == [1, 2]
    assert cfg.keypoints[0].index == 9


def test_broken_yaml_raises_value_error(tmp_path: Path) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text("a: [1, 2\nb: {", encoding="utf-8")
    with pytest.raises(ValueError, match="broken.yaml"):
        load_etl_config(path)


@pytest.mark.parametrize("value", [0.0, -1.0, float("inf"), float("nan")])
def test_bad_max_speed_rejected(value: float) -> None:
    with pytest.raises(ValidationError):
        LimitsConfig(max_speed_cm_s=value)


@pytest.mark.parametrize("value", [-0.1, float("inf"), float("nan")])
def test_bad_margin_rejected(value: float) -> None:
    with pytest.raises(ValidationError):
        LimitsConfig(rounding_margin_cm=value)


def test_unknown_top_level_key_rejected(tmp_path: Path) -> None:
    path = tmp_path / "e.yaml"
    path.write_text("bogus: 1\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_etl_config(path)


def test_missing_explicit_path_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_etl_config(tmp_path / "nope.yaml")


def test_missing_default_uses_builtin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VL_LAKE_DIR", raising=False)
    monkeypatch.setattr(etl_config, "DEFAULT_ETL_CONFIG_PATH", tmp_path / "nope.yaml")
    _check_defaults(load_etl_config())


def test_empty_file_gives_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VL_LAKE_DIR", raising=False)
    path = tmp_path / "e.yaml"
    path.write_text("", encoding="utf-8")
    assert load_etl_config(path) == EtlConfig()


def test_non_mapping_raises(tmp_path: Path) -> None:
    path = tmp_path / "e.yaml"
    path.write_text("- 1\n- 2\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_etl_config(path)
