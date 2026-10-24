"""Tests for the Module 3 motion config (lengths in cm, speeds in cm/s, accelerations in cm/s^2)."""

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from vl_data.usecase import config as motion_config
from vl_data.usecase.config import (
    DEFAULT_MOTION_CONFIG_PATH,
    MotionConfig,
    load_motion_config,
)


def _valid() -> dict[str, Any]:
    return {
        "window_frames": 16,
        "poly_order": 2,
        "min_points": 5,
        "max_gap_frames": 4,
        "position_keypoint": "centroid",
        "tolerances": {"velocity_cm_s": 20.0, "acceleration_cm_s2": 60.0},
    }


def _with(**changes: Any) -> dict[str, Any]:
    data = _valid()
    data.update(changes)
    return data


def _write(tmp_path: Path, data: Any) -> Path:
    path = tmp_path / "motion.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_default_yaml_loads() -> None:
    raw = yaml.safe_load(DEFAULT_MOTION_CONFIG_PATH.read_text(encoding="utf-8"))
    config = load_motion_config()
    assert set(raw) == {
        "window_frames",
        "poly_order",
        "min_points",
        "max_gap_frames",
        "position_keypoint",
        "tolerances",
    }
    assert config.window_frames == raw["window_frames"]
    assert config.poly_order == raw["poly_order"]
    assert config.min_points == raw["min_points"]
    assert config.max_gap_frames == raw["max_gap_frames"]
    assert config.position_keypoint == raw["position_keypoint"]
    assert config.tolerances.velocity_cm_s == raw["tolerances"]["velocity_cm_s"]
    assert config.tolerances.acceleration_cm_s2 == raw["tolerances"]["acceleration_cm_s2"]


@pytest.mark.parametrize(
    "data",
    [
        _with(window_frames=2),  # below poly_order + 1
        _with(poly_order=1),
        _with(poly_order=4),
        _with(min_points=2),  # below poly_order + 1
        _with(window_frames=6, min_points=7),  # window below min_points
        _with(max_gap_frames=0),
        _with(min_points=0),
        _with(window_frames=1001),
        _with(min_points=1001),
        _with(max_gap_frames=1001),
        _with(window_frames=16.0),  # strict int
        _with(min_points=5.0),
        _with(max_gap_frames=4.0),
        _with(window_frames=True),
        _with(min_points=True),
        _with(max_gap_frames=True),
        _with(tolerances={"velocity_cm_s": -1.0, "acceleration_cm_s2": 60.0}),
        _with(tolerances={"velocity_cm_s": 0.0, "acceleration_cm_s2": 60.0}),
        _with(tolerances={"velocity_cm_s": float("nan"), "acceleration_cm_s2": 60.0}),
        _with(tolerances={"velocity_cm_s": float("inf"), "acceleration_cm_s2": 60.0}),
        _with(tolerances={"velocity_cm_s": 20.0, "acceleration_cm_s2": -5.0}),
        _with(tolerances={"velocity_cm_s": 20.0, "acceleration_cm_s2": 0.0}),
        _with(tolerances={"velocity_cm_s": 20.0, "acceleration_cm_s2": float("nan")}),
        _with(position_keypoint=""),
        {**_valid(), "extra": 1},
        _with(tolerances={"velocity_cm_s": 20.0, "acceleration_cm_s2": 60.0, "extra": 1}),
    ],
)
def test_invalid_values_rejected(data: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        MotionConfig.model_validate(data)


def test_poly_order_three_accepted() -> None:
    config = MotionConfig.model_validate(_with(poly_order=3, min_points=4, window_frames=8))
    assert config.poly_order == 3


def test_minimum_sizes_accepted() -> None:
    config = MotionConfig.model_validate(_with(window_frames=3, min_points=3, max_gap_frames=1))
    assert (config.window_frames, config.min_points, config.max_gap_frames) == (3, 3, 1)
    config3 = MotionConfig.model_validate(_with(poly_order=3, window_frames=4, min_points=4))
    assert (config3.window_frames, config3.min_points) == (4, 4)


def test_upper_bounds_accepted_and_odd_window() -> None:
    config = MotionConfig.model_validate(
        _with(window_frames=1000, min_points=1000, max_gap_frames=1000)
    )
    assert config.window_frames == 1000
    assert MotionConfig.model_validate(_with(window_frames=15)).window_frames == 15


def test_explicit_missing_path_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_motion_config(tmp_path / "nope.yaml")


def test_empty_file_gives_defaults(tmp_path: Path) -> None:
    path = tmp_path / "motion.yaml"
    path.write_text("", encoding="utf-8")
    assert load_motion_config(path) == MotionConfig()


def test_non_mapping_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="mapping"):
        load_motion_config(_write(tmp_path, [1, 2, 3]))


def test_bad_yaml_raises_value_error(tmp_path: Path) -> None:
    path = tmp_path / "motion.yaml"
    path.write_text("window_frames: [unclosed", encoding="utf-8")
    with pytest.raises(ValueError, match="cannot parse"):
        load_motion_config(path)


def test_explicit_path_overrides(tmp_path: Path) -> None:
    config = load_motion_config(_write(tmp_path, _with(window_frames=20)))
    assert config.window_frames == 20


def test_builtin_defaults_when_default_file_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(motion_config, "DEFAULT_MOTION_CONFIG_PATH", tmp_path / "absent.yaml")
    config = load_motion_config()
    raw = yaml.safe_load(DEFAULT_MOTION_CONFIG_PATH.read_text(encoding="utf-8"))
    assert config == MotionConfig()
    assert config == MotionConfig.model_validate(raw)  # YAML and code defaults must not drift
