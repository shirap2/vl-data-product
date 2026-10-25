"""Tests for the config loader."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from vl_data.core.config import Config, load_config


@pytest.fixture(autouse=True)
def _no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make sure VL_RAW_DIR from the outside never leaks into a test."""
    monkeypatch.delenv("VL_RAW_DIR", raising=False)


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "cfg.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_load() -> None:
    config = load_config()
    assert config.fps == 16
    assert config.frame_interval_ns == 62_500_000
    assert config.raw_dir == Path("data/raw")
    assert config.body.size_cm == 100
    assert config.body.points_per_axis == 100
    assert config.tolerances.velocity_cm_s == 17.0
    assert config.fit.penalty == 40.0
    # The shipped default.yaml and the model defaults must agree.
    assert config == Config()


def test_env_override_raw_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _write(tmp_path, "raw_dir: from/file\n")
    assert load_config(path).raw_dir == Path("from/file")
    monkeypatch.setenv("VL_RAW_DIR", str(tmp_path / "elsewhere"))
    assert load_config(path).raw_dir == tmp_path / "elsewhere"
    monkeypatch.setenv("VL_RAW_DIR", "")
    assert load_config(path).raw_dir == Path("from/file")


def test_bad_config_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        load_config(_write(tmp_path, "bogus_key: 1\n"))
    with pytest.raises(ValidationError):
        load_config(_write(tmp_path, "fps: -16\n"))
    with pytest.raises(ValidationError):
        load_config(_write(tmp_path, "body:\n  extra: 1\n"))


def test_other_invalid_values_rejected() -> None:
    with pytest.raises(ValidationError):
        Config(fps=0)
    with pytest.raises(ValidationError):
        Config(fps=7)  # 1e9 / 7 is not an integer number of ns
    with pytest.raises(ValidationError):
        Config.model_validate({"body": {"size_cm": 0}})
    with pytest.raises(ValidationError):
        Config.model_validate({"body": {"points_per_axis": 1}})
    with pytest.raises(ValidationError):
        Config.model_validate({"tolerances": {"position_cm": 0.0}})
    with pytest.raises(ValidationError):
        Config.model_validate({"fit": {"penalty": -1.0}})
    with pytest.raises(ValidationError):
        Config.model_validate({"fit": {"penalty": float("inf")}})


def test_int_given_to_float_field_becomes_float() -> None:
    config = Config.model_validate({"fit": {"penalty": 40}})
    assert isinstance(config.fit.penalty, float)


def test_empty_yaml_gives_defaults(tmp_path: Path) -> None:
    assert load_config(_write(tmp_path, "")) == Config()


def test_non_mapping_yaml_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        load_config(_write(tmp_path, "- 1\n- 2\n"))


def test_explicit_missing_path_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.yaml")


def test_default_file_missing_gives_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vl_data.core.config as module

    monkeypatch.setattr(module, "DEFAULT_CONFIG_PATH", tmp_path / "does_not_exist.yaml")
    assert load_config() == Config()


def test_frame_interval_for_other_fps() -> None:
    assert Config(fps=10).frame_interval_ns == 100_000_000


def test_body_config_bounds_match_the_manifest() -> None:
    """BodyConfig rejects what BodyInfo would reject later; the limits themselves are accepted."""
    from vl_data.core.config import BodyConfig

    assert BodyConfig(size_cm=2_147_483_647, points_per_axis=1_000_000).size_cm == 2_147_483_647
    with pytest.raises(ValidationError):
        BodyConfig(size_cm=2_147_483_648, points_per_axis=10)
    with pytest.raises(ValidationError):
        BodyConfig(size_cm=100, points_per_axis=1_000_001)
    with pytest.raises(ValidationError):
        Config.model_validate({"body": {"size_cm": 100, "points_per_axis": 10**9}})
