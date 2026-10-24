"""Generator commands: generate, verify and roundtrip."""

from pathlib import Path
from typing import Annotated, NoReturn

import typer
import yaml

from vl_data.core.config import load_config
from vl_data.core.manifest import read_manifest, verify_drive
from vl_data.generator.drive import generate as generate_drive
from vl_data.generator.inverse import (
    MIN_RELIABLE_SEGMENT_FRAMES,
    compare_scenarios,
    infer_scenario,
    short_segments,
)
from vl_data.generator.models import load_scenario
from vl_data.generator.writer import generate_to_disk


def register(app: typer.Typer) -> None:
    """Add the generate, verify and roundtrip commands to the root app."""
    app.command("generate")(generate)
    app.command("verify")(verify)
    app.command("roundtrip")(roundtrip)


def _exit_with_error(message: str) -> NoReturn:
    """Print a one-line error to stderr and exit with code 1."""
    typer.echo(f"error: {' '.join(message.split())}", err=True)
    raise typer.Exit(code=1)


def generate(
    scenario: Annotated[Path, typer.Option("--scenario", help="Scenario YAML file.")],
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Output root; default is raw_dir from the config (VL_RAW_DIR)."),
    ] = None,
) -> None:
    """Generate a drive from a scenario YAML and print its summary.

    Units: frames: count; timestamps ns; positions cm.
    """
    config = load_config()
    try:
        loaded = load_scenario(scenario)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        _exit_with_error(f"cannot load scenario {scenario}: {exc}")
    try:
        drive_dir = generate_to_disk(loaded, config, out if out is not None else config.raw_dir)
    except (ValueError, OSError, RuntimeError) as exc:
        notes = " ".join(getattr(exc, "__notes__", []))
        _exit_with_error(f"cannot write drive: {exc} {notes}")
    manifest = read_manifest(drive_dir)
    typer.echo(f"frames: {manifest.frame_count}")
    typer.echo(f"timestamp_spacing_ns: {config.frame_interval_ns}")
    typer.echo(f"points_per_frame: {manifest.points_per_frame}")
    typer.echo(f"scenario_hash: {manifest.scenario_hash}")
    typer.echo(f"drive_dir: {drive_dir}")
    typer.echo("manifest: verified")


def verify(drive_dir: Annotated[Path, typer.Argument(help="Drive folder to check.")]) -> None:
    """Verify a drive's manifest; exit non-zero on any problem. No physical quantity."""
    result = verify_drive(drive_dir)
    if not result.ok:
        for problem in result.problems:
            typer.echo(problem, err=True)
        raise typer.Exit(code=1)
    typer.echo("manifest: verified")


def roundtrip(
    scenario: Annotated[Path, typer.Option("--scenario", help="Scenario YAML file.")],
) -> None:
    """Generate in memory, infer the scenario back, compare to tolerances; exit 1 on failure.

    Writes nothing to disk. Units: boundary and duration errors in frames, end speed in cm/s.
    """
    config = load_config()
    try:
        loaded = load_scenario(scenario)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        _exit_with_error(f"cannot load scenario {scenario}: {exc}")
    for index, frames in short_segments(loaded, config.fps):
        typer.echo(
            f"warning: segment {index} is {frames} frames; the round trip is only tested for "
            f"segments of {MIN_RELIABLE_SEGMENT_FRAMES} frames or more, so a result for this "
            "segment is outside the tested range (ASSUMPTIONS A14)",
            err=True,
        )
    try:
        drive = generate_drive(loaded, config)
        inferred = infer_scenario(drive.frames, config)
    except ValueError as exc:
        _exit_with_error(f"cannot round-trip scenario: {exc}")
    report = compare_scenarios(drive.scenario, inferred, config.tolerances, config.fps)
    typer.echo(f"segments: expected {len(drive.scenario.segments)}, found {len(inferred.segments)}")
    if not report.ok:
        for problem in report.problems:
            typer.echo(problem, err=True)
        raise typer.Exit(code=1)
    typer.echo(f"max_boundary_error_frames: {report.max_boundary_error_frames}")
    typer.echo(f"max_duration_error_frames: {report.max_duration_error_frames}")
    typer.echo(f"max_end_speed_error_cm_s: {report.max_end_speed_error_cm_s:.1f}")
    typer.echo("round trip OK")
