"""Use-case commands: motion and evaluate.

Exit codes follow the ingest command: 0 = done (evaluate: the drive passed); 1 = the work was
refused or failed its check (motion: MotionError; evaluate: EvaluationError or a tolerance
exceeded); 2 = usage or configuration error; 3 = a file could not be read or written
(motion: through the lake read or write; evaluate: only if evaluate_drive itself fails
unexpectedly, because it turns its own read failures into EvaluationError, exit 1).
Any other unexpected failure exits 1 with the exception type name and no traceback.
Units: frames are counts, positions cm, velocities cm/s, accelerations cm/s^2.
"""

import re
from pathlib import Path
from typing import Annotated, NoReturn

import pyarrow as pa
import typer
import yaml

from vl_data.core.config import Config, load_config
from vl_data.core.lake import Lake
from vl_data.etl.config import load_etl_config
from vl_data.sdk.client import VLClient
from vl_data.usecase.config import MotionConfig, load_motion_config
from vl_data.usecase.evaluate import EvalReport, EvaluationError, MetricErrors, evaluate_drive
from vl_data.usecase.pipeline import MotionError, compute_motion

EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_IO = 3
_TEXT_LIMIT = 300
# A drive id the lake can store: the same rule as the Lake (a test keeps the two equal).
_DRIVE_ID = re.compile(r"[A-Za-z0-9_-]+")


def register(app: typer.Typer) -> None:
    """Add the motion and evaluate commands to the root app."""
    app.command("motion")(motion)
    app.command("evaluate")(evaluate)


def _safe(text: str, limit: int = _TEXT_LIMIT) -> str:
    """One line for the terminal: characters outside 0x20-0x7e become `?`; cut to limit."""
    return "".join(c if " " <= c <= "~" else "?" for c in " ".join(text.split())[:limit])


def _fail(message: str, code: int) -> NoReturn:
    """Print a one-line ASCII error to stderr and exit with the given code."""
    typer.echo(f"error: {_safe(message)}", err=True)
    raise typer.Exit(code=code)


def _load_settings() -> tuple[Config, MotionConfig, Path]:
    """Core config, motion config and the ETL lake folder; exit 2 if any cannot be loaded."""
    try:
        return load_config(), load_motion_config(), load_etl_config().lake_dir
    except (OSError, ValueError, yaml.YAMLError) as error:
        _fail(f"cannot load the configuration: {error}", EXIT_USAGE)


def _open_lake(lake_dir: Path | None, default: Path) -> Lake:
    """The lake at lake_dir (else the configured folder); exit 2 if it is not a folder."""
    root = lake_dir if lake_dir is not None else default
    if not root.is_dir():
        _fail(f"lake folder not found: {root}; run ingest first or give --lake-dir", EXIT_USAGE)
    return Lake(root)


def _is_link(entry: Path) -> bool:
    """True for a symlink, or (Python 3.12 and later) a Windows junction."""
    is_junction = getattr(entry, "is_junction", None)
    return entry.is_symlink() or bool(is_junction and is_junction())


def motion(
    drive_id: Annotated[str, typer.Argument(help="Drive id (the 12-hex drive folder name).")],
    lake_dir: Annotated[
        Path | None,
        typer.Option("--lake-dir", help="Lake root; else VL_LAKE_DIR, config/etl.yaml, warehouse."),
    ] = None,
) -> None:
    """Compute the centroid velocity and acceleration of an ingested drive into the lake.

    Prints frames read, frames estimated and frames null (counts of frames; velocity in cm/s,
    acceleration in cm/s^2). Exit codes as for ingest: 0 done, 1 the drive cannot be processed
    (not ingested, status not ok, bad rows), 2 usage or configuration error, 3 a file could
    not be read or written (reached through the lake read). Any other failure exits 1 with the
    exception type name.
    """
    if not _DRIVE_ID.fullmatch(drive_id):
        _fail("a drive id holds only letters, digits, '_' and '-'", EXIT_USAGE)
    config, cfg, default_lake = _load_settings()
    lake = _open_lake(lake_dir, default_lake)
    try:
        result = compute_motion(VLClient(lake), lake, drive_id, config, cfg)
    except MotionError as error:
        _fail(str(error), EXIT_FAILED)
    except (OSError, pa.ArrowException) as error:  # ArrowException first: it is a ValueError too
        _fail(f"cannot read or write the lake: {type(error).__name__}", EXIT_IO)
    except Exception as error:  # last resort: type name only, no traceback
        _fail(f"cannot process {drive_id!r}: {type(error).__name__}", EXIT_FAILED)
    typer.echo(f"drive: {result.drive}")
    typer.echo(f"frames_read: {result.frames_read} frames")
    typer.echo(f"frames_estimated: {result.frames_estimated} frames")
    typer.echo(f"frames_null: {result.frames_null} frames (velocity or acceleration is null)")


def _metric_line(name: str, unit: str, interior: MetricErrors, tolerance: float) -> str:
    """One report line: interior max and RMS error and the tolerance, all in the given unit.

    unit is cm, cm/s or cm/s^2; interior and tolerance are in that unit.
    """
    return (
        f"{name} ({unit}): interior max {interior.max_abs:.3f} rms {interior.rms:.3f}"
        f" | tolerance {tolerance:g}"
    )


def _echo_report(report: EvalReport, config: Config, cfg: MotionConfig) -> None:
    """Print the evaluation as key: value lines, then PASS or FAIL.

    Counts are frames; errors and tolerances are cm (position), cm/s (velocity) and cm/s^2
    (acceleration).
    """
    tol, cfg_tol = config.tolerances, cfg.tolerances
    typer.echo(f"scenario_hash: {report.scenario_hash}")
    typer.echo(f"frames_compared: {report.frames_compared} frames")
    typer.echo(f"frames_interior: {report.frames_interior} frames")
    typer.echo(f"frames_null: {report.frames_null} frames")
    typer.echo(_metric_line("position", "cm", report.position_cm, tol.position_cm))
    typer.echo(
        _metric_line("velocity", "cm/s", report.velocity_cm_s, cfg_tol.velocity_cm_s)
        + f" | all frames max {report.velocity_all_cm_s.max_abs:.3f}"
        f" rms {report.velocity_all_cm_s.rms:.3f}"
    )
    typer.echo(
        _metric_line(
            "acceleration", "cm/s^2", report.acceleration_cm_s2, cfg_tol.acceleration_cm_s2
        )
        + f" | all frames max {report.acceleration_all_cm_s2.max_abs:.3f}"
        f" rms {report.acceleration_all_cm_s2.rms:.3f}"
    )
    typer.echo("result: PASS" if report.passed else "result: FAIL")


def evaluate(
    drive_dir: Annotated[Path, typer.Argument(help="Drive folder with its ground truth.")],
    lake_dir: Annotated[
        Path | None,
        typer.Option("--lake-dir", help="Lake root; else VL_LAKE_DIR, config/etl.yaml, warehouse."),
    ] = None,
) -> None:
    """Compare a drive's stored motion with its ground truth and print the errors.

    Prints the scenario hash, frames compared, interior and null, and for position (cm),
    velocity (cm/s) and acceleration (cm/s^2) the interior and all-frame max and RMS errors
    with the tolerances, then PASS or FAIL. Exit codes as for ingest: 0 pass, 1 a tolerance is
    exceeded or the drive cannot be evaluated (a read failure inside the evaluation is an
    evaluation failure), 2 usage or configuration error (a drive folder that is, or holds, a
    link is refused), 3 only if the evaluation itself fails unexpectedly with a file error.
    """
    config, cfg, default_lake = _load_settings()
    if not drive_dir.is_dir():
        _fail(f"not a folder: {drive_dir}", EXIT_USAGE)
    try:
        linked = _is_link(drive_dir) or any(_is_link(entry) for entry in drive_dir.iterdir())
    except OSError as error:
        _fail(f"cannot list the drive folder: {type(error).__name__}", EXIT_USAGE)
    if linked:
        _fail("the drive folder is, or holds, a link; give the real folder", EXIT_USAGE)
    lake = _open_lake(lake_dir, default_lake)
    try:
        report = evaluate_drive(drive_dir, VLClient(lake), config, cfg)
    except EvaluationError as error:
        _fail(str(error), EXIT_FAILED)
    except (OSError, pa.ArrowException) as error:
        _fail(f"cannot read the lake or drive: {type(error).__name__}", EXIT_IO)
    except Exception as error:  # last resort: type name only, no traceback
        _fail(f"evaluation failed: {type(error).__name__}", EXIT_FAILED)
    _echo_report(report, config, cfg)
    if not report.passed:
        raise typer.Exit(code=EXIT_FAILED)
