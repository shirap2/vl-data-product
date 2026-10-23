"""Generator commands: generate and verify."""

from pathlib import Path
from typing import Annotated, NoReturn

import typer
import yaml

from vl_data.core.config import load_config
from vl_data.core.manifest import read_manifest, verify_drive
from vl_data.generator.models import load_scenario
from vl_data.generator.writer import generate_to_disk


def register(app: typer.Typer) -> None:
    """Add the generate and verify commands to the root app."""
    app.command("generate")(generate)
    app.command("verify")(verify)


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
