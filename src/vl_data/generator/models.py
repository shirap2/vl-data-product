"""Scenario model: a start state and a list of move/stop segments.

Units: positions in cm, speeds in cm/s (signed, along +x), durations in s.
Floats are strict (a YAML ``yes`` is rejected, an int is accepted and stored as a float)
and must be finite.
"""

import math
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

FRAMES_PER_SECOND = 16  # frame rate of the assignment, frames/s
_FRAME_TOLERANCE = 1e-9  # frames; slack when checking duration * fps against an integer
MAX_FRAMES = 2_000  # frames; cap per scenario, 2,000 frames = 125 s at 16 frames/s

_Cm = Annotated[float, Field(strict=True, allow_inf_nan=False)]
_CmPerS = Annotated[float, Field(strict=True, allow_inf_nan=False)]
_Seconds = Annotated[float, Field(strict=True, allow_inf_nan=False)]


def _check_whole_frames(duration_s: float) -> float:
    """Return duration_s (s) if it is positive, whole frames and at most MAX_FRAMES, else raise.

    The check is on duration_s * FRAMES_PER_SECOND, in frames.
    """
    if not math.isfinite(duration_s) or duration_s <= 0:
        raise ValueError(f"duration_s must be a positive number of seconds, got {duration_s!r}")
    scaled = duration_s * FRAMES_PER_SECOND  # frames; may overflow to inf for huge durations
    if not math.isfinite(scaled):
        raise ValueError(f"duration_s {duration_s!r} s is too long (over MAX_FRAMES frames)")
    frames = round(scaled)
    if frames < 1 or abs(scaled - frames) > _FRAME_TOLERANCE:
        raise ValueError(
            f"duration_s {duration_s!r} s is not a whole number of frames "
            f"(multiple of 1/{FRAMES_PER_SECOND} s)"
        )
    if frames > MAX_FRAMES:
        raise ValueError(
            f"duration_s {duration_s!r} s is {frames} frames, more than MAX_FRAMES ({MAX_FRAMES})"
        )
    return duration_s


class Start(BaseModel):
    """Start state of the vehicle."""

    model_config = ConfigDict(extra="forbid")

    x_cm: _Cm = 0.0  # cm
    y_cm: _Cm = 0.0  # cm
    z_cm: _Cm = 0.0  # cm
    speed_cm_s: _CmPerS = 0.0  # cm/s, signed, along +x


class MoveSegment(BaseModel):
    """Speed changes linearly to end_speed_cm_s over duration_s."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["move"] = "move"
    duration_s: _Seconds  # s, whole frames
    end_speed_cm_s: _CmPerS  # cm/s, signed

    @field_validator("duration_s")
    @classmethod
    def _whole_frames(cls, value: float) -> float:
        return _check_whole_frames(value)


class StopSegment(BaseModel):
    """The vehicle stands still (speed 0) for duration_s."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["stop"] = "stop"
    duration_s: _Seconds  # s, whole frames

    @field_validator("duration_s")
    @classmethod
    def _whole_frames(cls, value: float) -> float:
        return _check_whole_frames(value)


Segment = Annotated[MoveSegment | StopSegment, Field(discriminator="type")]


class Scenario(BaseModel):
    """A drive: seed, start state, segments.

    The seed is internal: reserved for future noise, not shown to users (not in the Generate
    tab), default 0. It stays an accepted optional key in scenario files and is part of the
    scenario hash (the model dump always includes it), so omitting it equals `seed: 0`.
    """

    model_config = ConfigDict(extra="forbid")

    seed: Annotated[int, Field(strict=True)] = 0
    start: Start = Field(default_factory=Start)
    segments: list[Segment] = Field(min_length=1)

    @model_validator(mode="after")
    def _speed_never_jumps(self) -> "Scenario":
        """A move needs a nonzero start or end speed; a stop needs the speed to be 0 (cm/s).

        The total length is also capped at MAX_FRAMES frames.
        """
        total = sum(round(seg.duration_s * FRAMES_PER_SECOND) for seg in self.segments)  # frames
        if total > MAX_FRAMES:
            raise ValueError(f"scenario has {total} frames, more than the limit of {MAX_FRAMES}")
        speed = self.start.speed_cm_s  # cm/s, running speed at the segment start
        for index, seg in enumerate(self.segments):
            if isinstance(seg, MoveSegment):
                if speed == 0.0 and seg.end_speed_cm_s == 0.0:
                    raise ValueError(f"segment {index}: move from 0 to 0 cm/s, use stop")
                speed = seg.end_speed_cm_s
            else:
                if speed != 0.0:
                    raise ValueError(
                        f"segment {index}: stop at {speed} cm/s, slow to 0 with a move first"
                    )
        return self


def load_scenario(path: Path) -> Scenario:
    """Read and validate a scenario YAML file.

    Raises FileNotFoundError for a missing file, ValueError for an empty or non-mapping
    file, and pydantic ValidationError for invalid content. Malformed YAML raises
    yaml.YAMLError and a file that is not UTF-8 raises UnicodeDecodeError.
    """
    if not path.is_file():
        raise FileNotFoundError(f"scenario file not found: {path}")
    data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("scenario file must contain a mapping at the top level")
    return Scenario.model_validate(data)
