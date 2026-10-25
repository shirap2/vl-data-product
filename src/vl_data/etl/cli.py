"""Loader commands: ingest and error-codes.

Exit codes: 0 = every drive ingested (even with quarantined frames); 1 = at least one drive
rejected (or, for error-codes --check, the file differs); 2 = usage or configuration error;
3 = at least one drive could not be read or written (an OSError). With --all the highest of
3, 1, 0 seen is the exit code.
"""

import contextlib
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Annotated, NoReturn

import typer
import yaml

from vl_data.core.lake import Lake
from vl_data.etl.config import load_etl_config
from vl_data.etl.errors_doc import render_error_codes
from vl_data.etl.ingest import IngestResult, ingest_drive

DEFAULT_ERROR_CODES_PATH = Path(__file__).resolve().parents[3] / "docs" / "ERROR_CODES.md"
_DRIVE_FOLDER = re.compile(r"[0-9a-f]{12}")  # a drive folder: first 12 hex of the scenario hash
EXIT_REJECTED = 1
EXIT_USAGE = 2
EXIT_IO = 3
_PATH_LIMIT = 200


def register(app: typer.Typer) -> None:
    """Add the ingest and error-codes commands to the root app."""
    app.command("ingest")(ingest)
    app.command("error-codes")(error_codes)


def _safe(text: str, limit: int = 64) -> str:
    """Text for the terminal: every character outside 0x20-0x7e becomes `?`; cut to limit."""
    return "".join(char if " " <= char <= "~" else "?" for char in text[:limit])


def _usage_error(message: str) -> NoReturn:
    """Print a one-line ASCII error to stderr and exit with code 2."""
    typer.echo(f"error: {_safe(' '.join(message.split()), 300)}", err=True)
    raise typer.Exit(code=EXIT_USAGE)


def _io_error(name: str, error: OSError) -> None:
    """Report an OS failure for one folder: its filtered name and the exception type only."""
    typer.echo(f"cannot ingest {_safe(name)}: {type(error).__name__}", err=True)


def _echo_summary(result: IngestResult, elapsed_s: float) -> None:
    """Print one drive's summary as key: value lines (counts are frames or rows; time in s)."""
    typer.echo(f"drive: {result.drive}")
    typer.echo(f"status: {result.status}")
    typer.echo(f"frames_expected: {result.frames_expected}")
    typer.echo(f"frames_in: {result.frames_in}")
    typer.echo(f"frames_kept: {result.frames_kept}")
    typer.echo(f"frames_quarantined: {result.frames_quarantined}")
    typer.echo(f"frames_missing: {result.frames_missing}")
    typer.echo(f"keypoint_rows: {result.keypoint_rows}")
    typer.echo(f"points_per_frame_in: {result.points_per_frame_in}")
    typer.echo(f"keypoints_per_frame_out: {result.keypoints_per_frame_out}")
    for code, count in sorted(result.quarantine_by_code.items()):
        typer.echo(f"quarantine_{code}: {count}")
    typer.echo(f"elapsed_s: {elapsed_s:.1f}")


def _is_link(entry: Path) -> bool:
    """True for a symlink, or (Python 3.12 and later) a Windows junction."""
    is_junction = getattr(entry, "is_junction", None)
    return entry.is_symlink() or bool(is_junction and is_junction())


def _drive_folders(root: Path) -> list[Path]:
    """Direct subfolders of root named by 12 lowercase hex characters, sorted.

    Every other entry is skipped with one `skipped: <name>` line (`(link)` after a symlink or
    junction, which is never followed). An entry that cannot be examined counts as skipped.
    Raises OSError if the folder cannot be listed.
    """
    found: list[Path] = []
    skipped: list[str] = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        name = _safe(entry.name)
        try:
            is_link = _is_link(entry)
            is_dir = not is_link and entry.is_dir()
        except OSError:
            skipped.append(name)
            continue
        if is_link:
            skipped.append(f"{name} (link)")
        elif is_dir and _DRIVE_FOLDER.fullmatch(entry.name):
            found.append(entry)
        else:
            skipped.append(name)
    for name in skipped:
        typer.echo(f"skipped: {name}")
    if skipped and found:
        typer.echo("")
    return found


def ingest(
    drive_dir: Annotated[Path | None, typer.Argument(help="One drive folder to ingest.")] = None,
    all_in: Annotated[
        Path | None,
        typer.Option("--all", help="Raw folder: ingest every 12-hex drive folder inside it."),
    ] = None,
    lake_dir: Annotated[
        Path | None,
        typer.Option("--lake-dir", help="Lake root; else VL_LAKE_DIR, config/etl.yaml, warehouse."),
    ] = None,
) -> None:
    """Ingest one drive folder, or every drive folder under --all, and print a summary.

    Exit 0 when every drive was ingested (quarantined frames are normal), 1 when at least one
    drive was rejected, 2 on a usage or configuration error, 3 when at least one drive could
    not be read or written. Times are in seconds.
    """
    if (drive_dir is None) == (all_in is None):
        _usage_error("give exactly one of a drive folder or --all <raw folder>")
    target = drive_dir if drive_dir is not None else all_in
    assert target is not None
    if not target.is_dir():
        _usage_error(f"not a folder: {_safe(str(target), _PATH_LIMIT)}")
    if drive_dir is not None and not _DRIVE_FOLDER.fullmatch(drive_dir.name):
        _usage_error(
            "a drive folder is named by 12 lowercase hex characters (the start of its "
            f"scenario hash), not {_safe(drive_dir.name)}"
        )
    try:
        etl = load_etl_config()
    except (OSError, ValueError, yaml.YAMLError) as error:
        _usage_error(f"cannot load the ETL config: {error}")
    lake = Lake(lake_dir if lake_dir is not None else etl.lake_dir)

    if drive_dir is not None:
        drives = [drive_dir]
    else:
        try:
            drives = _drive_folders(target)
        except OSError as error:
            _io_error(target.name, error)
            raise typer.Exit(code=EXIT_IO) from None
        if not drives:
            shown_root = _safe(str(target), _PATH_LIMIT)
            _usage_error(f"no drive folders (12 hex characters) in {shown_root}")
    exit_code = 0
    for position, folder in enumerate(drives):
        if position:
            typer.echo("")
        started = time.perf_counter()
        try:
            result = ingest_drive(folder, lake, etl)
        except ValueError as error:  # keypoint spec or drive id: nothing written for this drive
            _usage_error(f"cannot ingest {_safe(folder.name)}: {_safe(str(error), _PATH_LIMIT)}")
        except OSError as error:  # report it and go on with the remaining drives
            _io_error(folder.name, error)
            exit_code = max(exit_code, EXIT_IO)
            continue
        _echo_summary(result, time.perf_counter() - started)
        if result.status != "ok":
            exit_code = max(exit_code, EXIT_REJECTED)
    if exit_code:
        raise typer.Exit(code=exit_code)


def _is_source_checkout(document: Path) -> bool:
    """True when the default document's folder exists and pyproject.toml sits beside that folder."""
    return document.parent.is_dir() and (document.parent.parent / "pyproject.toml").is_file()


def error_codes(
    write: Annotated[bool, typer.Option("--write", help="Save the document.")] = False,
    check: Annotated[
        bool, typer.Option("--check", help="Exit 1 if the saved document differs.")
    ] = False,
    path: Annotated[
        Path | None, typer.Option("--path", help="Document path; default docs/ERROR_CODES.md.")
    ] = None,
) -> None:
    """Print the generated error-code Markdown; --write saves it, --check compares it.

    --check compares after CRLF to LF and exits 1 if the file differs or is missing.
    """
    if write and check:
        _usage_error("give only one of --write and --check")
    text = render_error_codes()
    if not (write or check):
        typer.echo(text, nl=False)
        return
    if path is not None:
        target = path
    elif _is_source_checkout(DEFAULT_ERROR_CODES_PATH):
        target = DEFAULT_ERROR_CODES_PATH
    else:
        _usage_error("not a source checkout (no docs folder beside pyproject.toml): give --path")
    shown = _safe(str(target), _PATH_LIMIT)
    if write:
        if target.is_symlink():
            _usage_error(f"{shown} is a link; give the real file with --path")
        temp_name: str | None = None
        try:
            handle, temp_name = tempfile.mkstemp(dir=target.parent, prefix=".error-codes-")
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
            os.replace(temp_name, target)
        except OSError as error:
            if temp_name is not None:
                with contextlib.suppress(OSError):
                    Path(temp_name).unlink()
            _usage_error(f"cannot write {shown}: {_safe(error.strerror or type(error).__name__)}")
        typer.echo(f"wrote {shown}")
    else:
        try:
            current = target.read_bytes().decode("utf-8").replace("\r\n", "\n")
        except (OSError, ValueError):
            current = None
        if current != text:
            typer.echo(f"error: {shown} is missing or differs; run error-codes --write", err=True)
            raise typer.Exit(code=EXIT_REJECTED)
        typer.echo(f"{shown} is up to date")
