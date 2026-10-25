"""Measure the motion estimator on generated drives and pick its defaults.

Run from the repo root (the package must be importable, for example PYTHONPATH=src):

    python tools/motion_sweep.py --write-yaml [--full-density-check 300]

What it does. It generates, in memory, two drives with a 100 cm cube of 10 points per axis:
the demo scenario (1000 frames) and a harder drive (stop, forward ramp, reversal, stop; 624
frames). Per frame it takes the centroid x (mean of the points' x, cm) and the true velocity
(cm/s) and acceleration (cm/s^2) from the in-memory ground truth; it never reads a file or the
lake. It runs estimate_motion for every window length (frames) and polynomial order, and also a
per-frame-difference baseline (central differences of the centroid x). Errors are estimate minus
truth, as maximum absolute and RMS (vl_data.usecase.evaluate.max_abs_rms), over interior frames
(vl_data.usecase.evaluate.interior_mask, the definition evaluate_drive uses, with half a window
each side) and over all frames that have an estimate.

Derived settings. min_points = default_min_points(window) = window // 2 + 1, so the one-sided
window at the first and last frame (half a window plus the frame itself) still fits.
max_gap_frames is the MotionConfig default (4: bridges three missing frames); the sweep drives
have no missing frames, so it does not change the numbers.

Selection rule. Among the window x order cells (the baseline is never chosen), choose the one
with the lowest worst-case (largest over both drives) interior acceleration max error; ties are
broken by the lower worst-case interior velocity max error, then the smaller window, then the
lower order. One eligibility condition is added because the interior set shrinks as the window
grows (a 48-frame window leaves only about half the frames "interior", so its small interior
error hides large errors near every speed change): a cell is eligible only if at least
MIN_INTERIOR_FRACTION (0.8) of the frames of every sweep drive are interior. With
--min-interior-fraction 0 the rule is the plain one; the run prints what that would choose.

Random-scenario search. The two sweep drives start and end at rest, so they miss slow motion
(integer-cm rounding of the centroid is a staircase) and clips that end while moving. The tool
therefore also measures the chosen cell on random scenarios: --random-search N scenarios drawn
with numpy's RNG from --seed (move and stop legs of 48 to 96 frames, end speeds up to 400 cm/s,
up to 5 legs, 10 points per axis), plus the PINNED_LEGS scenarios, which are always included
(the counterexamples that the Hypothesis test found). The same leg rules build the scenarios of
the property test in tests/usecase/test_estimator.py (scenario_from_legs).

Tolerances. The velocity (cm/s) and acceleration (cm/s^2) tolerances are the largest measured
interior max error of the chosen cell over the sweep drives and the random-scenario search,
times 1.5, rounded up to 1 decimal (never below the measured value, always above zero).
--write-yaml updates only the window, order, min_points and tolerance fields of
config/motion.yaml, atomically, and rewrites the header note.

--full-density-check N (at most 600) generates N frames at the real density (100 points per
axis) to a temp folder with generate_to_disk, ingests them into a temp lake, runs
compute_motion and evaluate_drive, prints the interior errors beside the small-lattice errors of
the same scenario, and deletes the temp folders. It refuses to start when the free disk space is
under twice the estimated size (12 bytes per point, uncompressed).

Units: positions cm, velocity cm/s, acceleration cm/s^2, windows and gaps in frames, time s.
"""

import argparse
import math
import os
import re
import shutil
import sys
import tempfile
import textwrap
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import yaml
from numpy.typing import NDArray

import vl_data.generator as _generator_package
from vl_data.core.config import NS_PER_SECOND, BodyConfig, Config
from vl_data.core.lake import Lake
from vl_data.core.schemas import BYTES_PER_POINT
from vl_data.etl.config import EtlConfig
from vl_data.etl.ingest import ingest_drive
from vl_data.generator.drive import generate
from vl_data.generator.models import Scenario, load_scenario
from vl_data.generator.writer import generate_to_disk
from vl_data.sdk.client import VLClient
from vl_data.usecase.config import DEFAULT_MOTION_CONFIG_PATH, MotionConfig, load_motion_config
from vl_data.usecase.estimator import estimate_motion
from vl_data.usecase.evaluate import (
    EvalReport,
    MetricErrors,
    evaluate_drive,
    interior_mask,
    max_abs_rms,
)
from vl_data.usecase.pipeline import compute_motion

ErrorStats = MetricErrors  # max_abs and rms, in the metric's unit (cm/s or cm/s^2)

DEFAULT_WINDOWS = (8, 12, 16, 24, 32, 48)  # frames
DEFAULT_ORDERS = (2, 3)  # polynomial order
DEFAULT_MAX_GAP_FRAMES = MotionConfig().max_gap_frames  # frame intervals
TOLERANCE_MARGIN = 1.5  # factor on the measured worst-case interior max error
MIN_TOLERANCE = 0.1  # the smallest tolerance written (cm/s or cm/s^2); a tolerance must be > 0
MIN_INTERIOR_FRACTION = 0.8  # share of frames of every sweep drive that must be interior
DEFAULT_RANDOM_SEARCH = 1500  # random scenarios per run
DEFAULT_SEED = 0
MAX_FULL_DENSITY_FRAMES = 600  # frames; 600 x 12 MB = 7.2 GB uncompressed
BASELINE_LABEL = "per-frame differences"
_TIE_DIGITS = 6  # decimals; errors closer than this are tied in choose_default
_BASELINE_HALF_WINDOW = 1.0  # frames; a central difference looks one frame either side
_DEMO_YAML = Path(_generator_package.__file__).resolve().parent / "scenarios" / "demo.yaml"
_SWEEP_BODY = BodyConfig(size_cm=100, points_per_axis=10)

# A leg is (is_stop, frames, end speed in cm/s); see scenario_from_legs.
Leg = tuple[bool, int, int]
LEG_MIN_FRAMES = 48  # frames (3 s at 16 fps): with speeds up to 400 cm/s, |a| is at most 267
LEG_MAX_FRAMES = 96  # frames
LEG_MAX_SPEED_CM_S = 400  # cm/s
MAX_LEGS = 5
# Counterexamples found by the Hypothesis property test, always part of the random search:
# one 48-frame move from 0 to 2 cm/s (slow motion, the first found), and five 96-frame moves
# towards -68 cm/s (the worst found in a 1,500-example search).
PINNED_LEGS: tuple[tuple[Leg, ...], ...] = (
    ((False, 48, 2),),
    ((False, 96, -68),) * 5,
)


@dataclass(frozen=True)
class SweepDrive:
    """One generated drive with its centroid x and true motion, one value per frame."""

    name: str
    fps: int  # frames/s
    t_s: NDArray[np.float64]  # s, from the first frame
    centroid_x_cm: NDArray[np.float64]  # cm, mean x of the frame's points
    true_x_cm: NDArray[np.float64]  # cm, ground-truth position of the body origin
    true_velocity_cm_s: NDArray[np.float64]  # cm/s
    true_acceleration_cm_s2: NDArray[np.float64]  # cm/s^2


@dataclass(frozen=True)
class DriveErrors:
    """Errors of one estimator setting on one drive."""

    frames_interior: int  # frames
    frames_total: int  # frames in the drive
    velocity_interior: ErrorStats  # cm/s
    acceleration_interior: ErrorStats  # cm/s^2
    velocity_all: ErrorStats  # cm/s, all frames with an estimate
    acceleration_all: ErrorStats  # cm/s^2


def _worst(values: Sequence[float]) -> float:
    """Largest of the values, or inf if any is not finite."""
    result = 0.0
    for value in values:
        if not math.isfinite(value):
            return math.inf
        result = max(result, float(value))
    return result


@dataclass(frozen=True)
class SweepRow:
    """One table row: a window x order cell, or the baseline (window and order None)."""

    label: str
    window_frames: int | None  # frames
    poly_order: int | None
    errors: dict[str, DriveErrors]  # by drive name

    def worst_interior_acceleration_max(self) -> float:
        """Largest interior acceleration max error over the drives, cm/s^2 (inf if not finite)."""
        return _worst([e.acceleration_interior.max_abs for e in self.errors.values()])

    def min_interior_fraction(self) -> float:
        """Smallest share of a drive's frames that are interior, over the drives (0 to 1)."""
        return min(e.frames_interior / e.frames_total for e in self.errors.values())

    def worst_interior_velocity_max(self) -> float:
        """Largest interior velocity max error over the drives, cm/s (inf if not finite)."""
        return _worst([e.velocity_interior.max_abs for e in self.errors.values()])


@dataclass(frozen=True)
class RandomSearchResult:
    """Worst interior errors of one estimator setting over the pinned and random scenarios."""

    examples: int  # scenarios measured (pinned plus random)
    seed: int
    worst_velocity_cm_s: float  # cm/s, largest interior velocity max error
    worst_acceleration_cm_s2: float  # cm/s^2, largest interior acceleration max error
    worst_velocity_legs: tuple[Leg, ...]
    worst_acceleration_legs: tuple[Leg, ...]


@dataclass(frozen=True)
class FullDensityResult:
    """Outcome of the full-density confirmation."""

    frames: int  # frames
    report: EvalReport  # evaluation of the full-density drive
    small_errors: DriveErrors  # same scenario, 10 points per axis, same estimator
    max_centroid_difference_cm: float  # cm, largest |stored full centroid x - small centroid x|


def default_min_points(window_frames: int) -> int:
    """min_points for a window: window // 2 + 1 samples (the one-sided end window fits)."""
    return window_frames // 2 + 1


def centroid_x_cm(frames: pa.Table) -> NDArray[np.float64]:
    """Mean x (cm) of the points of each frame, ordered as the table (int32 xyz, 12 bytes)."""
    column = frames.column("points")
    out: list[float] = []
    for chunk in column.chunks:
        for item in chunk:
            xyz = np.frombuffer(item.as_buffer(), dtype="<i4").reshape(-1, 3)
            out.append(float(xyz[:, 0].mean(dtype=np.float64)))
    return np.asarray(out, dtype=np.float64)


def drive_from_scenario(name: str, scenario: Scenario, config: Config) -> SweepDrive:
    """Generate a drive in memory and keep its centroid x (cm) and in-memory ground truth.

    Positions are in cm, velocities in cm/s, accelerations in cm/s^2, times in s at
    config.fps frames/s. config.body must be small enough for generate() (at most 1 GiB).
    """
    drive = generate(scenario, config)
    truth = drive.ground_truth
    ts = drive.frames.column("timestamp_ns").to_numpy()  # ns
    return SweepDrive(
        name=name,
        fps=config.fps,
        t_s=(ts - ts[0]) / NS_PER_SECOND,
        centroid_x_cm=centroid_x_cm(drive.frames),
        true_x_cm=truth.column("x_cm").to_numpy(),
        true_velocity_cm_s=truth.column("velocity_cm_s").to_numpy(),
        true_acceleration_cm_s2=truth.column("acceleration_cm_s2").to_numpy(),
    )


def demo_scenario() -> Scenario:
    """The demo scenario (1000 frames at 16 fps), loaded from the generator's demo.yaml."""
    return load_scenario(_DEMO_YAML)


def _scenario(segments: list[dict[str, object]]) -> Scenario:
    """A scenario from segment dicts (durations s, speeds cm/s), starting at rest at x = 0 cm."""
    return Scenario.model_validate(
        {
            "seed": 0,
            "start": {"x_cm": 0.0, "y_cm": 0.0, "z_cm": 0.0, "speed_cm_s": 0.0},
            "segments": segments,
        }
    )


def hard_scenario() -> Scenario:
    """A harder drive, 624 frames: stop, forward ramp, hard reversal, slow to a stop, stop.

    Speeds in cm/s: 0 -> 600 over 3 s (200 cm/s^2), cruise 6 s, 600 -> -600 over 3 s
    (400 cm/s^2, twice the demo's largest), cruise 5 s, -600 -> 0 over 3 s, stop 15 s.
    """
    return _scenario(
        [
            {"type": "stop", "duration_s": 4.0},
            {"type": "move", "duration_s": 3.0, "end_speed_cm_s": 600.0},
            {"type": "move", "duration_s": 6.0, "end_speed_cm_s": 600.0},
            {"type": "move", "duration_s": 3.0, "end_speed_cm_s": -600.0},
            {"type": "move", "duration_s": 5.0, "end_speed_cm_s": -600.0},
            {"type": "move", "duration_s": 3.0, "end_speed_cm_s": 0.0},
            {"type": "stop", "duration_s": 15.0},
        ]
    )


def scenario_from_legs(legs: Sequence[Leg], fps: int = 16) -> Scenario:
    """Build a valid scenario from legs (is_stop, frames, end speed cm/s); frames / fps seconds.

    A stop leg slows to 0 cm/s first when moving. A move leg whose end speed equals the current
    speed ends at 1 cm/s instead (a move needs a speed change).
    """
    segments: list[dict[str, object]] = []
    speed = 0
    for is_stop, frames, end_speed in legs:
        duration = frames / fps  # s
        if is_stop:
            if speed != 0:
                segments.append({"type": "move", "duration_s": duration, "end_speed_cm_s": 0.0})
                speed = 0
            else:
                segments.append({"type": "stop", "duration_s": duration})
        elif end_speed != speed:
            segments.append(
                {"type": "move", "duration_s": duration, "end_speed_cm_s": float(end_speed)}
            )
            speed = end_speed
        else:
            segments.append({"type": "move", "duration_s": duration, "end_speed_cm_s": 1.0})
            speed = 1
    return _scenario(segments)


def build_sweep_drives(config: Config | None = None) -> list[SweepDrive]:
    """The two sweep drives, [demo, hard], in memory (small lattice unless config says else)."""
    cfg = config if config is not None else Config(body=_SWEEP_BODY)
    return [
        drive_from_scenario("demo", demo_scenario(), cfg),
        drive_from_scenario("hard", hard_scenario(), cfg),
    ]


def motion_config_for(window_frames: int, poly_order: int) -> MotionConfig:
    """Estimator settings (frames) for a grid cell: default_min_points, default max gap."""
    return MotionConfig(
        window_frames=window_frames,
        poly_order=poly_order,  # type: ignore[arg-type]
        min_points=default_min_points(window_frames),
        max_gap_frames=DEFAULT_MAX_GAP_FRAMES,
    )


def difference_estimate(
    x_cm: NDArray[np.float64], fps: int
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Per-frame differences: (velocity cm/s, acceleration cm/s^2) from positions in cm.

    Velocity is the central difference (x[i+1] - x[i-1]) * fps / 2, forward at the first frame
    and backward at the last. Acceleration is (x[i+1] - 2 x[i] + x[i-1]) * fps^2; NaN at the
    first and last frame. Frames are assumed evenly spaced at 1/fps s.
    """
    n = x_cm.size
    velocity = np.full(n, np.nan)
    acceleration = np.full(n, np.nan)
    if n >= 2:
        velocity[1:-1] = (x_cm[2:] - x_cm[:-2]) * fps / 2
        velocity[0] = (x_cm[1] - x_cm[0]) * fps
        velocity[-1] = (x_cm[-1] - x_cm[-2]) * fps
    if n >= 3:
        acceleration[1:-1] = (x_cm[2:] - 2 * x_cm[1:-1] + x_cm[:-2]) * fps**2
    return velocity, acceleration


def measure_drive(drive: SweepDrive, cfg: MotionConfig | None) -> DriveErrors:
    """Errors of estimate_motion with cfg on a drive; cfg None means per-frame differences.

    Interior frames use half of cfg.window_frames either side (one frame for the baseline).
    Velocity in cm/s, acceleration in cm/s^2; frames with no estimate are left out.
    """
    if cfg is None:
        velocity, acceleration = difference_estimate(drive.centroid_x_cm, drive.fps)
        half_window = _BASELINE_HALF_WINDOW
    else:
        est = estimate_motion(drive.t_s, drive.centroid_x_cm, drive.fps, cfg)
        velocity, acceleration = est.velocity_cm_s, est.acceleration_cm_s2
        half_window = cfg.window_frames / 2
    interior = interior_mask(drive.true_acceleration_cm_s2, half_window)
    everything = np.ones(interior.size, dtype=bool)

    def errors(estimate: NDArray[np.float64], truth: NDArray[np.float64], keep: Any) -> ErrorStats:
        use = keep & np.isfinite(estimate)
        return max_abs_rms(estimate[use], truth[use])

    truth_v, truth_a = drive.true_velocity_cm_s, drive.true_acceleration_cm_s2
    return DriveErrors(
        frames_interior=int(interior.sum()),
        frames_total=int(interior.size),
        velocity_interior=errors(velocity, truth_v, interior),
        acceleration_interior=errors(acceleration, truth_a, interior),
        velocity_all=errors(velocity, truth_v, everything),
        acceleration_all=errors(acceleration, truth_a, everything),
    )


def run_sweep(
    drives: Sequence[SweepDrive],
    windows: Sequence[int] = DEFAULT_WINDOWS,
    orders: Sequence[int] = DEFAULT_ORDERS,
) -> list[SweepRow]:
    """One row per window (frames) x order (windows outer, orders inner), then the baseline row.

    Each row holds the errors on every drive: velocity in cm/s, acceleration in cm/s^2.
    """
    rows: list[SweepRow] = []
    for window in windows:
        for order in orders:
            cfg = motion_config_for(window, order)
            errors = {d.name: measure_drive(d, cfg) for d in drives}
            rows.append(SweepRow(f"window {window}, order {order}", window, order, errors))
    rows.append(
        SweepRow(BASELINE_LABEL, None, None, {d.name: measure_drive(d, None) for d in drives})
    )
    return rows


def choose_default(
    rows: Sequence[SweepRow], min_interior_fraction: float = MIN_INTERIOR_FRACTION
) -> SweepRow:
    """The selection rule: lowest worst-case interior acceleration max error, then lowest
    worst-case interior velocity max error, then smaller window, then lower order.

    Only cells with at least min_interior_fraction (0 to 1) of the frames of every drive
    interior are eligible; 0 makes every cell eligible. The baseline row is never chosen.
    Errors are compared rounded to 6 decimals (1e-6 cm/s^2 or cm/s), so differences at float
    round-off level count as ties. Raises ValueError if no window x order row is eligible.
    """
    cells = [
        r
        for r in rows
        if r.window_frames is not None
        and r.poly_order is not None
        and r.min_interior_fraction() >= min_interior_fraction
    ]
    if not cells:
        raise ValueError("no eligible window x order rows to choose from")
    return min(
        cells,
        key=lambda r: (
            round(r.worst_interior_acceleration_max(), _TIE_DIGITS),
            round(r.worst_interior_velocity_max(), _TIE_DIGITS),
            r.window_frames or 0,
            r.poly_order or 0,
        ),
    )


def _draw_legs(rng: np.random.Generator) -> tuple[Leg, ...]:
    """Draw 1 to MAX_LEGS legs; edge values (shortest, longest, fastest, near rest) are favoured."""
    legs: list[Leg] = []
    for _ in range(int(rng.integers(1, MAX_LEGS + 1))):
        frames = (
            int(rng.choice([LEG_MIN_FRAMES, LEG_MAX_FRAMES]))
            if rng.random() < 0.3
            else int(rng.integers(LEG_MIN_FRAMES, LEG_MAX_FRAMES + 1))
        )
        kind = rng.random()
        if kind < 0.3:
            speed = int(rng.integers(-10, 11))  # slow: the centroid is a rounding staircase
        elif kind < 0.5:
            speed = int(rng.choice([-LEG_MAX_SPEED_CM_S, LEG_MAX_SPEED_CM_S]))
        else:
            speed = int(rng.integers(-LEG_MAX_SPEED_CM_S, LEG_MAX_SPEED_CM_S + 1))
        legs.append((bool(rng.random() < 0.25), frames, speed))
    return tuple(legs)


def random_search(
    cfg: MotionConfig, count: int, seed: int = DEFAULT_SEED, config: Config | None = None
) -> RandomSearchResult:
    """Worst interior errors of cfg on the PINNED_LEGS scenarios plus `count` random ones.

    The random scenarios come from numpy's default_rng(seed), so a run is reproducible. The
    pinned scenarios are always measured, also for count 0. config.body defaults to the sweep
    body (10 points per axis, 100 cm cube). Errors: velocity in cm/s, acceleration in cm/s^2;
    a scenario with no interior frame with an estimate is skipped.
    """
    body = config if config is not None else Config(body=_SWEEP_BODY)
    rng = np.random.default_rng(seed)
    all_legs = [*PINNED_LEGS, *(_draw_legs(rng) for _ in range(count))]
    worst_v, worst_a = 0.0, 0.0
    legs_v: tuple[Leg, ...] = PINNED_LEGS[0]
    legs_a: tuple[Leg, ...] = PINNED_LEGS[0]
    for legs in all_legs:
        drive = drive_from_scenario("random", scenario_from_legs(legs, body.fps), body)
        errors = measure_drive(drive, cfg)
        v = errors.velocity_interior.max_abs
        a = errors.acceleration_interior.max_abs
        if math.isfinite(v) and v > worst_v:
            worst_v, legs_v = v, tuple(legs)
        if math.isfinite(a) and a > worst_a:
            worst_a, legs_a = a, tuple(legs)
    return RandomSearchResult(len(all_legs), seed, worst_v, worst_a, legs_v, legs_a)


def tolerance_from_measured(measured: float) -> float:
    """measured * 1.5 rounded up to 1 decimal, at least 0.1; never below measured (same unit).

    Raises ValueError if measured is negative or not finite.
    """
    if not math.isfinite(measured) or measured < 0:
        raise ValueError(f"measured error must be a finite number >= 0, got {measured}")
    return max(MIN_TOLERANCE, math.ceil(round(measured * TOLERANCE_MARGIN * 10, 6)) / 10)


def _fmt(value: float, width: int = 8) -> str:
    return f"{value:{width}.3f}"


def format_table(rows: Sequence[SweepRow]) -> str:
    """Text tables: the worst-case selection table, then one error table per drive.

    Velocity in cm/s, acceleration in cm/s^2; 'int' is interior frames, 'all' every frame with
    an estimate; max is the largest absolute error, rms the root mean square.
    """
    names = list(rows[0].errors)
    lines = [
        "Selection table: worst case over drives {}, interior frames".format(", ".join(names)),
        f"{'row':<24}{'window':>7}{'order':>6}{'min_pts':>8}{'interior':>9}"
        f"{'int vel max (cm/s)':>20}{'int acc max (cm/s^2)':>22}",
    ]
    for r in rows:
        window = "-" if r.window_frames is None else str(r.window_frames)
        order = "-" if r.poly_order is None else str(r.poly_order)
        min_pts = "-" if r.window_frames is None else str(default_min_points(r.window_frames))
        lines.append(
            f"{r.label:<24}{window:>7}{order:>6}{min_pts:>8}{r.min_interior_fraction():>9.1%}"
            f"{_fmt(r.worst_interior_velocity_max(), 20)}"
            f"{_fmt(r.worst_interior_acceleration_max(), 22)}"
        )
    for name in names:
        lines += [
            "",
            f"Drive {name}: velocity in cm/s, acceleration in cm/s^2",
            f"{'row':<24}{'frames':>7}{'int vel max':>12}{'int vel rms':>12}{'int acc max':>12}"
            f"{'int acc rms':>12}{'all vel max':>12}{'all vel rms':>12}{'all acc max':>12}"
            f"{'all acc rms':>12}",
        ]
        for r in rows:
            e = r.errors[name]
            lines.append(
                f"{r.label:<24}{e.frames_interior:>7}"
                + "".join(
                    _fmt(v, 12)
                    for v in (
                        e.velocity_interior.max_abs,
                        e.velocity_interior.rms,
                        e.acceleration_interior.max_abs,
                        e.acceleration_interior.rms,
                        e.velocity_all.max_abs,
                        e.velocity_all.rms,
                        e.acceleration_all.max_abs,
                        e.acceleration_all.rms,
                    )
                )
            )
    return "\n".join(lines)


_TOP_FIELDS = ("window_frames", "poly_order", "min_points")
_TOLERANCE_FIELDS = ("velocity_cm_s", "acceleration_cm_s2")
_HEADER_WIDTH = 100  # characters per line of the generated header comment
_LINE = re.compile(r"^(\s*)(\w+):(\s*)([^\s#]+)(.*)$")


def write_yaml(
    path: Path,
    row: SweepRow,
    velocity_tol: float,
    acceleration_tol: float,
    stamp: str,
    basis: str = "",
) -> None:
    """Update window_frames (frames), poly_order, min_points (frames) and the two tolerances
    (velocity_cm_s in cm/s, acceleration_cm_s2 in cm/s^2) in a motion YAML.

    Other lines, the file's line endings and every field comment stay as they are. The
    tolerance keys are replaced only inside the `tolerances:` block. Any '# PROVISIONAL' line
    and any earlier '# Measured by' note are removed and a new note (stamp is a date string;
    basis names the random-scenario search that fed the tolerances) is written after the first
    line, wrapped at 100 characters. The rewritten text is parsed and checked (only the five
    fields differ) and then written to a temp file in the same folder and moved over the
    original with os.replace, so a failure leaves the original unchanged. Raises ValueError for
    the baseline row, a missing field or a failed check.
    """
    if row.window_frames is None or row.poly_order is None:
        raise ValueError("the baseline row has no window or order to write")
    top = {
        "window_frames": str(row.window_frames),
        "poly_order": str(row.poly_order),
        "min_points": str(default_min_points(row.window_frames)),
    }
    tolerances = {
        "velocity_cm_s": repr(float(velocity_tol)),
        "acceleration_cm_s2": repr(float(acceleration_tol)),
    }
    with open(path, encoding="utf-8", newline="") as handle:
        original = handle.read()
    newline = "\r\n" if "\r\n" in original else "\n"
    lines = original.splitlines()
    kept: list[str] = []
    skipping_note = False
    for line in lines:
        if line.startswith("# PROVISIONAL"):
            continue
        if line.startswith("# Measured by"):
            skipping_note = True
            continue
        if skipping_note and line.startswith("#   "):
            continue
        skipping_note = False
        kept.append(line)
    note = textwrap.wrap(
        f"Measured by tools/motion_sweep.py on {stamp}. Window and order: lowest worst-case "
        "interior acceleration error over the sweep drives (demo, hard). Tolerances: 1.5 x the "
        "largest measured interior max error over the sweep drives and "
        f"{basis or 'no extra search'}, rounded up to 0.1.",
        width=_HEADER_WIDTH,
        initial_indent="# ",
        subsequent_indent="#   ",
    )
    kept[1 if kept else 0 : 1 if kept else 0] = note
    remaining = {*top, *tolerances}
    section = ""
    for index, line in enumerate(kept):
        top_level = re.match(r"^(\w+):", line)
        if top_level:
            section = top_level.group(1)
        match = _LINE.match(line)
        if not match:
            continue
        indent, key = match.group(1), match.group(2)
        if indent == "":
            if key in top:
                kept[index] = f"{key}:{match.group(3)}{top[key]}{match.group(5)}"
                remaining.discard(key)
        elif section == "tolerances" and key in tolerances:
            kept[index] = f"{indent}{key}:{match.group(3)}{tolerances[key]}{match.group(5)}"
            remaining.discard(key)
    if remaining:
        raise ValueError(f"fields not found in {path.name}: {sorted(remaining)}")
    text = newline.join(kept) + newline
    expected: Any = yaml.safe_load(original) or {}
    expected = {**expected, "tolerances": dict(expected.get("tolerances", {}))}
    for key, value in top.items():
        expected[key] = int(value)
    for key, value in tolerances.items():
        expected["tolerances"][key] = float(value)
    if yaml.safe_load(text) != expected:
        raise ValueError(f"rewritten {path.name} differs from the original in other fields")
    handle_fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8", newline="") as out:
            out.write(text)
        os.replace(temp_name, path)
    except BaseException:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
        raise


def full_density_scenario(frames: int, fps: int = 16) -> Scenario:
    """A scenario of exactly `frames` frames (above 11 * fps; 177 at 16 fps): stop 2 s, ramp
    0 -> 600 cm/s over 6 s, cruise, then slow to 0 over 3 s. fps is in frames/s.
    """
    fixed = 11 * fps  # frames: stop 2 s, ramp 6 s, slow-down 3 s
    if frames <= fixed:
        raise ValueError(f"frames must be above {fixed} for the full-density scenario")
    return _scenario(
        [
            {"type": "stop", "duration_s": 2.0},
            {"type": "move", "duration_s": 6.0, "end_speed_cm_s": 600.0},
            {"type": "move", "duration_s": (frames - fixed) / fps, "end_speed_cm_s": 600.0},
            {"type": "move", "duration_s": 3.0, "end_speed_cm_s": 0.0},
        ]
    )


def estimated_bytes(frames: int, config: Config) -> int:
    """Uncompressed bytes of the points of `frames` frames (points_per_axis^3 x 12 bytes)."""
    return frames * config.body.points_per_axis**3 * BYTES_PER_POINT


def full_density_check(
    frames: int, base_dir: Path | None = None, config: Config | None = None
) -> FullDensityResult:
    """Generate `frames` frames at the real density, ingest, compute motion, evaluate, clean up.

    Uses generate_to_disk into a temp folder under base_dir (the system temp folder when None)
    and a temp lake, then evaluate_drive with the YAML motion config. Also runs the same
    scenario at 10 points per axis in memory and returns the largest difference (cm) between
    the stored centroid x (motion table x_cm) and the small-lattice centroid x. Raises
    RuntimeError, before writing anything, when the free disk space is under twice
    estimated_bytes (uncompressed size). The temp folders are deleted before returning, also on
    error; a warning with the path goes to stderr if one is left behind. config defaults to
    Config() (100 points per axis); tests pass a small one.
    """
    full = config if config is not None else Config()
    small = Config(body=BodyConfig(size_cm=full.body.size_cm, points_per_axis=10), fps=full.fps)
    scenario = full_density_scenario(frames, full.fps)
    cfg = load_motion_config()
    need = 2 * estimated_bytes(frames, full)  # bytes
    free = shutil.disk_usage(base_dir if base_dir is not None else tempfile.gettempdir()).free
    if free < need:
        raise RuntimeError(f"not enough free disk space: {free} bytes free, {need} bytes needed")
    work = Path(tempfile.mkdtemp(prefix="motion_sweep_", dir=base_dir))
    try:
        drive_dir = generate_to_disk(scenario, full, work / "raw")
        lake = Lake(work / "lake")
        result = ingest_drive(drive_dir, lake, EtlConfig())
        if result.status != "ok":
            raise RuntimeError(f"ingest status {result.status!r}")
        client = VLClient(lake)
        compute_motion(client, lake, drive_dir.name, full, cfg)
        report = evaluate_drive(drive_dir, client, full, cfg)
        stored_x = client.motion(drive_dir.name).sort_by("frame_id").column("x_cm").to_numpy()
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if work.exists():
            print(f"warning: temp folder was not fully deleted: {work}", file=sys.stderr)
    reference = drive_from_scenario("small", scenario, small)
    difference = float(np.abs(stored_x - reference.centroid_x_cm).max())
    return FullDensityResult(frames, report, measure_drive(reference, cfg), difference)


def _format_full_density(result: FullDensityResult) -> str:
    """Text block of the full-density check (velocity cm/s, acceleration cm/s^2, position cm)."""
    r = result.report
    s = result.small_errors
    return "\n".join(
        [
            f"Full-density check: {result.frames} frames at 100 points per axis, "
            f"{r.frames_interior} interior, {r.frames_null} null; passed={r.passed}",
            f"{'':<22}{'int vel max':>12}{'int vel rms':>12}{'int acc max':>12}{'int acc rms':>12}",
            f"{'full density':<22}"
            f"{_fmt(r.velocity_cm_s.max_abs, 12)}{_fmt(r.velocity_cm_s.rms, 12)}"
            f"{_fmt(r.acceleration_cm_s2.max_abs, 12)}{_fmt(r.acceleration_cm_s2.rms, 12)}",
            f"{'10 points per axis':<22}"
            f"{_fmt(s.velocity_interior.max_abs, 12)}{_fmt(s.velocity_interior.rms, 12)}"
            f"{_fmt(s.acceleration_interior.max_abs, 12)}{_fmt(s.acceleration_interior.rms, 12)}",
            f"Largest centroid x difference, full vs small lattice: "
            f"{result.max_centroid_difference_cm:.6f} cm",
            f"Interior position error (full density): {r.position_cm.max_abs:.4f} cm max",
            "Units: velocity cm/s, acceleration cm/s^2.",
        ]
    )


def _ints(text: str) -> tuple[int, ...]:
    """Parse a comma list of integers (argparse type)."""
    try:
        return tuple(int(part) for part in text.split(",") if part.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a comma list of integers: {text!r}") from exc


def _frames_arg(text: str) -> int:
    """Parse --full-density-check: 0 (off) to MAX_FULL_DENSITY_FRAMES frames (argparse type)."""
    try:
        value = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an integer: {text!r}") from exc
    if not 0 <= value <= MAX_FULL_DENSITY_FRAMES:
        raise argparse.ArgumentTypeError(f"must be 0 to {MAX_FULL_DENSITY_FRAMES} frames")
    return value


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Parse and validate the command line (windows and orders against MotionConfig)."""
    parser = argparse.ArgumentParser(description="Sweep the motion estimator and pick defaults.")
    parser.add_argument("--windows", type=_ints, default=DEFAULT_WINDOWS, help="frames, comma list")
    parser.add_argument("--orders", type=_ints, default=DEFAULT_ORDERS, help="2 and/or 3")
    parser.add_argument("--write-yaml", action="store_true", help="update the motion YAML")
    parser.add_argument(
        "--yaml", type=Path, default=DEFAULT_MOTION_CONFIG_PATH, help="motion YAML to write"
    )
    parser.add_argument(
        "--min-interior-fraction",
        type=float,
        default=MIN_INTERIOR_FRACTION,
        help="share of frames of every drive that must be interior (0 to 1)",
    )
    parser.add_argument(
        "--random-search",
        type=int,
        default=DEFAULT_RANDOM_SEARCH,
        metavar="N",
        help="random scenarios measured for the tolerances (the pinned ones are always added)",
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="seed of the random search")
    parser.add_argument(
        "--full-density-check",
        type=_frames_arg,
        default=0,
        metavar="N",
        help=f"frames at the real density, 0 to {MAX_FULL_DENSITY_FRAMES}",
    )
    args = parser.parse_args(argv)
    if not args.windows or not args.orders:
        parser.error("--windows and --orders need at least one value")
    if args.random_search < 0:
        parser.error("--random-search must be 0 or more")
    if not 0.0 <= args.min_interior_fraction <= 1.0:
        parser.error("--min-interior-fraction must be between 0 and 1")
    for window in args.windows:
        for order in args.orders:
            try:
                motion_config_for(window, order)
            except ValueError as exc:
                parser.error(f"window {window}, order {order} is not a valid setting: {exc}")
    return args


def main(argv: Sequence[str] | None = None, drives: Sequence[SweepDrive] | None = None) -> int:
    """Run the sweep, print the tables and the chosen cell; optionally write the YAML.

    drives replaces the two generated sweep drives (tests pass cached ones). Velocity is
    printed in cm/s, acceleration in cm/s^2. Returns the exit code (0).
    """
    args = _parse_args(argv)
    sweep_drives = list(drives) if drives is not None else build_sweep_drives()
    rows = run_sweep(sweep_drives, args.windows, args.orders)
    print(format_table(rows))
    chosen = choose_default(rows, args.min_interior_fraction)
    plain = choose_default(rows, 0.0)
    assert chosen.window_frames is not None and chosen.poly_order is not None
    search = random_search(
        motion_config_for(chosen.window_frames, chosen.poly_order), args.random_search, args.seed
    )
    velocity_measured = max(chosen.worst_interior_velocity_max(), search.worst_velocity_cm_s)
    acceleration_measured = max(
        chosen.worst_interior_acceleration_max(), search.worst_acceleration_cm_s2
    )
    velocity_tol = tolerance_from_measured(velocity_measured)  # cm/s
    acceleration_tol = tolerance_from_measured(acceleration_measured)  # cm/s^2
    print()
    print(
        f"Chosen: window {chosen.window_frames} frames, order {chosen.poly_order}, "
        f"min_points {default_min_points(chosen.window_frames)}, "
        f"max_gap_frames {DEFAULT_MAX_GAP_FRAMES}"
    )
    if plain is not chosen:
        print(
            f"Without the interior-fraction requirement ({args.min_interior_fraction:.0%}) the "
            f"plain rule would choose window {plain.window_frames}, order {plain.poly_order} "
            f"({plain.min_interior_fraction():.1%} of frames interior in the worst drive)"
        )
    print(
        f"Measured worst-case interior max error, sweep drives: velocity "
        f"{chosen.worst_interior_velocity_max():.3f} cm/s, acceleration "
        f"{chosen.worst_interior_acceleration_max():.3f} cm/s^2"
    )
    print(
        f"Measured worst-case interior max error, {search.examples} random and pinned scenarios "
        f"(seed {search.seed}): velocity {search.worst_velocity_cm_s:.3f} cm/s, acceleration "
        f"{search.worst_acceleration_cm_s2:.3f} cm/s^2"
    )
    print(
        f"Tolerances (x{TOLERANCE_MARGIN} of the larger, rounded up to 0.1): velocity "
        f"{velocity_tol} cm/s, acceleration {acceleration_tol} cm/s^2"
    )
    if args.write_yaml:
        basis = (
            f"a random-scenario search ({search.examples} scenarios, {len(PINNED_LEGS)} pinned, "
            f"seed {search.seed})"
        )
        write_yaml(
            args.yaml, chosen, velocity_tol, acceleration_tol, date.today().isoformat(), basis
        )
        print(f"Wrote {args.yaml}")
    if args.full_density_check > 0:
        frames = args.full_density_check
        size = estimated_bytes(frames, Config())
        print()
        print(f"Estimated size: {frames} frames x 12 MB = {size / 1e9:.1f} GB uncompressed")
        print(_format_full_density(full_density_check(frames)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
