"""Use-case command: motion.

Exit codes follow the ingest command: 0 = done; 1 = the work was refused or failed (MotionError);
2 = usage or configuration error; 3 = a file could not be read or written (through the lake read
or write). Any other unexpected failure exits 1 with the exception type name and no traceback.
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
from vl_data.usecase.pipeline import MotionError, compute_motion

EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_IO = 3
_TEXT_LIMIT = 300
# A drive id the lake can store: the same rule as the Lake (a test keeps the two equal).
_DRIVE_ID = re.compile(r"[A-Za-z0-9_-]+")


def register(app: typer.Typer) -> None:
    """Add the motion command to the root app."""
    app.command("motion")(motion)


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
