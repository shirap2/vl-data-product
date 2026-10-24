"""Configuration models and the YAML loader.

Units: lengths in cm, speeds in cm/s, times in s or ns, boundaries in frames.
"""

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

NS_PER_SECOND = 1_000_000_000

# Default config file of a source checkout: <repo root>/config/default.yaml.
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "default.yaml"


class BodyConfig(BaseModel):
    """The simulated vehicle body: a cube sampled on a point lattice."""

    model_config = ConfigDict(extra="forbid")

    size_cm: int = Field(default=100, gt=0)  # cube edge, cm
    points_per_axis: int = Field(default=100, ge=2)  # lattice points per axis


class ToleranceConfig(BaseModel):
    """Limits for the round-trip comparison of a scenario against its inverse."""

    model_config = ConfigDict(extra="forbid")

    position_cm: float = Field(default=0.6, gt=0, allow_inf_nan=False)  # cm
    velocity_cm_s: float = Field(default=17.0, gt=0, allow_inf_nan=False)  # cm/s
    boundary_frames: int = Field(default=6, gt=0)  # frames
    duration_frames: int = Field(default=8, gt=0)  # frames
    end_speed_cm_s: float = Field(default=40.0, gt=0, allow_inf_nan=False)  # cm/s


class FitConfig(BaseModel):
    """Parameters of the piecewise-linear segment fit."""

    model_config = ConfigDict(extra="forbid")

    min_segment_frames: int = Field(default=8, gt=0)  # frames
    noise_sigma_cm_s: float = Field(default=12.0, gt=0, allow_inf_nan=False)  # cm/s
    penalty: float = Field(default=40.0, gt=0, allow_inf_nan=False)  # unitless
    stop_speed_cm_s: float = Field(default=30.0, gt=0, allow_inf_nan=False)  # cm/s


class Config(BaseModel):
    """Top-level configuration."""

    model_config = ConfigDict(extra="forbid")

    fps: int = Field(default=16, gt=0)  # frames per second
    raw_dir: Path = Path("data/raw")  # output root for drives
    body: BodyConfig = BodyConfig()
    tolerances: ToleranceConfig = ToleranceConfig()
    fit: FitConfig = FitConfig()

    @model_validator(mode="after")
    def _fps_divides_second(self) -> "Config":
        """Reject an fps whose frame spacing is not a whole number of ns."""
        if NS_PER_SECOND % self.fps != 0:
            raise ValueError(f"fps {self.fps} does not divide 1_000_000_000 ns evenly")
        return self

    @property
    def frame_interval_ns(self) -> int:
        """Frame spacing in ns (62,500,000 at 16 fps)."""
        return NS_PER_SECOND // self.fps


def load_config(path: Path | None = None) -> Config:
    """Load and validate config YAML; VL_RAW_DIR (non-empty) overrides raw_dir.

    With no path, the repo's config/default.yaml is used, or the built-in defaults
    when that file does not exist. An explicit path that does not exist raises
    FileNotFoundError. An empty file means all defaults. Units: see the models.
    """
    data: Any = {}
    if path is None:
        if DEFAULT_CONFIG_PATH.is_file():
            data = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    else:
        if not path.is_file():
            raise FileNotFoundError(f"config file not found: {path}")
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError("config file must contain a mapping at the top level")
    env_raw_dir = os.environ.get("VL_RAW_DIR")
    if env_raw_dir:
        data["raw_dir"] = env_raw_dir
    return Config.model_validate(data)
