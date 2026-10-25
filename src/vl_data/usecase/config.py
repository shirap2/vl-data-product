"""Motion config for the use-case algorithm. Lengths cm, speeds cm/s, accelerations cm/s^2."""

from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

DEFAULT_MOTION_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "motion.yaml"


class MotionTolerances(BaseModel):
    """Agreement limits in cm/s and cm/s^2 used when comparing fitted motion with truth."""

    model_config = ConfigDict(extra="forbid")

    velocity_cm_s: float = Field(default=19.4, gt=0, allow_inf_nan=False)  # cm/s
    acceleration_cm_s2: float = Field(default=31.9, gt=0, allow_inf_nan=False)  # cm/s^2


class MotionConfig(BaseModel):
    """Sliding-window polynomial fit settings; window and gap sizes are in frames."""

    model_config = ConfigDict(extra="forbid")

    window_frames: StrictInt = Field(default=16, le=1000)  # frames
    poly_order: Literal[2, 3] = 2  # polynomial order of the local fit
    min_points: StrictInt = Field(default=9, ge=1, le=1000)  # samples needed in a window
    # largest allowed spacing between neighbouring window points, in frame intervals
    # (4 bridges 3 missing frames)
    max_gap_frames: StrictInt = Field(default=4, ge=1, le=1000)
    position_keypoint: str = Field(default="centroid", min_length=1)  # name in config/etl.yaml
    tolerances: MotionTolerances = Field(default_factory=MotionTolerances)

    @model_validator(mode="after")
    def _check_sizes(self) -> Self:
        needed = self.poly_order + 1
        if self.window_frames < needed:
            raise ValueError(f"window_frames must be at least poly_order + 1 ({needed})")
        if self.min_points < needed:
            raise ValueError(f"min_points must be at least poly_order + 1 ({needed})")
        if self.window_frames < self.min_points:
            raise ValueError("window_frames must be at least min_points")
        return self


def load_motion_config(path: Path | None = None) -> MotionConfig:
    """Load and validate the motion config YAML (frames, cm, cm/s, cm/s^2).

    With no path, the repo's config/motion.yaml is used, or the built-in defaults when that
    file does not exist. An explicit path that does not exist raises FileNotFoundError.
    An empty file means all defaults; a non-mapping top level raises ValueError.
    """
    data: Any = {}
    if path is None:
        if DEFAULT_MOTION_CONFIG_PATH.is_file():
            data = _parse(DEFAULT_MOTION_CONFIG_PATH)
    else:
        if not path.is_file():
            raise FileNotFoundError(f"config file not found: {path}")
        data = _parse(path)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError("config file must contain a mapping at the top level")
    return MotionConfig.model_validate(data)


def _parse(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise ValueError(f"cannot parse {path}: {error}") from error
