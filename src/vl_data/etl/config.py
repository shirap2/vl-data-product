"""ETL configuration models and the YAML loader.

Units: lengths in cm, speeds in cm/s, frame rate in frames per second.
"""

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

# Default config file of a source checkout: <repo root>/config/etl.yaml.
DEFAULT_ETL_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "etl.yaml"


class KeypointSpec(BaseModel):
    """One keypoint: a lattice index, a cube corner, or the centroid of all points."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[A-Za-z0-9_]{1,64}$")  # keypoint name
    kind: Literal["index", "corner", "centroid"] = "index"
    index: StrictInt | None = None  # point index in the body lattice; only for kind "index"
    # (x, y, z) corner bits, each 0 (low end) or 1 (high end); only for kind "corner"
    bits: tuple[StrictInt, StrictInt, StrictInt] | None = None

    @model_validator(mode="after")
    def _check_spec(self) -> "KeypointSpec":
        """Require index for "index", bits for "corner", and neither for "centroid"."""
        if self.kind == "index":
            if self.index is None or self.index < 0:
                raise ValueError(f"keypoint {self.name!r}: kind 'index' needs an index >= 0")
            if self.bits is not None:
                raise ValueError(f"keypoint {self.name!r}: kind 'index' takes no bits")
        elif self.kind == "corner":
            if self.bits is None or any(b not in (0, 1) for b in self.bits):
                raise ValueError(f"keypoint {self.name!r}: kind 'corner' needs bits of 0 or 1")
            if self.index is not None:
                raise ValueError(f"keypoint {self.name!r}: kind 'corner' takes no index")
        elif self.index is not None or self.bits is not None:
            raise ValueError(f"keypoint {self.name!r}: kind 'centroid' takes no index or bits")
        return self


class LimitsConfig(BaseModel):
    """Plausibility limits on motion between frames."""

    model_config = ConfigDict(extra="forbid")

    max_speed_cm_s: float = Field(default=9000.0, gt=0, le=100_000, allow_inf_nan=False)  # cm/s
    rounding_margin_cm: float = Field(default=2.0, ge=0, allow_inf_nan=False)  # cm

    def max_jump_cm(self, fps: int, frame_gap: int) -> float:
        """Largest plausible keypoint move in cm across frame_gap frames at fps frames/s.

        max_speed_cm_s * frame_gap / fps + rounding_margin_cm (margin added once).
        Raises ValueError if fps < 1 or frame_gap < 1.
        """
        if fps < 1:
            raise ValueError(f"fps must be at least 1, got {fps}")
        if frame_gap < 1:
            raise ValueError(f"frame_gap must be at least 1, got {frame_gap}")
        return self.max_speed_cm_s * frame_gap / fps + self.rounding_margin_cm


def _default_keypoints() -> list[KeypointSpec]:
    """The eight lattice corners (binary x, y, z) and the centroid."""
    corners = [
        KeypointSpec(name=f"corner_{x}{y}{z}", kind="corner", bits=(x, y, z))
        for x in (0, 1)
        for y in (0, 1)
        for z in (0, 1)
    ]
    return [*corners, KeypointSpec(name="centroid", kind="centroid")]


class EtlConfig(BaseModel):
    """Top-level ETL configuration."""

    model_config = ConfigDict(extra="forbid")

    supported_schema_versions: list[StrictInt] = Field(
        default=[2], min_length=1, max_length=16
    )  # raw schema versions the loader accepts
    # MiB of raw int32 xyz data per chunk; peak memory is about 2-3x this (payload + decoded copy)
    chunk_mib: int = Field(default=128, ge=1, le=1024)
    lake_dir: Path = Path("warehouse")  # lake root
    limits: LimitsConfig = LimitsConfig()
    keypoints: list[KeypointSpec] = Field(
        default_factory=_default_keypoints, min_length=1, max_length=256
    )

    @model_validator(mode="after")
    def _check_lists(self) -> "EtlConfig":
        """Require non-empty positive schema versions and unique, non-empty keypoints."""
        if any(v < 1 for v in self.supported_schema_versions):
            raise ValueError("supported_schema_versions must be positive integers")
        names = [k.name for k in self.keypoints]
        if len(set(names)) != len(names):
            raise ValueError("keypoint names must be unique")
        return self

    def chunk_frames(self, points_per_frame: int) -> int:
        """Frames per processing chunk: chunk_mib MiB of int32 xyz data, at least 1.

        Raises ValueError if points_per_frame < 1.
        """
        if points_per_frame < 1:
            raise ValueError(f"points_per_frame must be at least 1, got {points_per_frame}")
        return max(1, (self.chunk_mib * 2**20) // (points_per_frame * 12))

    def max_jump_cm(self, fps: int, frame_gap: int) -> float:
        """Largest plausible keypoint move in cm across frame_gap frames at fps frames/s.

        Delegates to LimitsConfig.max_jump_cm. Raises ValueError if fps < 1 or frame_gap < 1.
        """
        return self.limits.max_jump_cm(fps, frame_gap)


def load_etl_config(path: Path | None = None) -> EtlConfig:
    """Load and validate ETL config YAML; VL_LAKE_DIR (non-empty) overrides lake_dir.

    With no path, the repo's config/etl.yaml is used, or the built-in defaults when that
    file does not exist. An explicit path that does not exist raises FileNotFoundError.
    An empty file means all defaults; a non-mapping top level raises ValueError.
    """
    data: Any = {}
    if path is None:
        if DEFAULT_ETL_CONFIG_PATH.is_file():
            data = yaml.safe_load(DEFAULT_ETL_CONFIG_PATH.read_text(encoding="utf-8"))
    else:
        if not path.is_file():
            raise FileNotFoundError(f"config file not found: {path}")
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as error:
            raise ValueError(f"cannot parse {path}: {error}") from error
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError("config file must contain a mapping at the top level")
    env_lake_dir = os.environ.get("VL_LAKE_DIR")
    if env_lake_dir:
        data["lake_dir"] = env_lake_dir
    return EtlConfig.model_validate(data)
