"""Ingest panel of the BI app: pick a raw drive folder and ingest it into the lake on a click.

The only lake write of the app. It goes through etl.ingest.ingest_drive (the function the
`ingest` command calls), one drive per click, and only after a click. The lake has a single
writer (core/lake.py): do not run the command line ingest at the same time.
Units: frames and rows are counts; folder modification times are seconds (st_mtime).
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

import pyarrow as pa
import streamlit as st
import yaml

from vl_data.bi.lake_lock import try_lake_lock
from vl_data.core.config import Config
from vl_data.core.lake import Lake
from vl_data.etl.config import load_etl_config
from vl_data.etl.ingest import IngestResult, ingest_drive

# Same folder rule as etl/cli.py: the first 12 hex characters of the scenario hash.
_DRIVE_FOLDER = re.compile(r"[0-9a-f]{12}")
_REINGEST = " (re-ingest)"
_BUTTON = "Ingest this drive"
LAST_INGESTED_KEY = "bi_last_ingested"  # session key: the shell selects this drive next run
_BUSY = "Another ingest is running. Try again when it has finished."
_NOT_AVAILABLE = "That drive folder is not available."
_UNEXPECTED = "Ingest failed unexpectedly."
_SLOW_NOTE = "Ingesting reads the whole drive; a large drive can take minutes. Keep this page open."


@dataclass(frozen=True)
class RawDrives:
    """Raw drive folders split by whether the lake has them. Newest folder first."""

    pending: list[str] = field(default_factory=list)  # not in the lake
    ingested: list[str] = field(default_factory=list)  # already in the lake (re-ingest)
    missing: bool = False  # the raw folder does not exist
    problem: str | None = None  # plain text, never a file path; the raw folder cannot be read


def _is_link(entry: Path) -> bool:
    """True for a symlink, or (Python 3.12 and later) a Windows junction."""
    is_junction = getattr(entry, "is_junction", None)
    return entry.is_symlink() or bool(is_junction and is_junction())


def pending_raw_drives(raw_dir: Path, lake_drives: list[str]) -> RawDrives:
    """List the drive folders under raw_dir; never raises.

    A drive folder is a real directory (no symlink or junction) named by 12 lowercase hex
    characters; the writer's .tmp-* and .old-* folders fail the name rule. Both groups are
    ordered newest folder first (st_mtime of the folder, ties by name). An entry that cannot be
    examined is skipped. A missing raw_dir gives missing=True; an unreadable one gives a problem.
    """
    try:
        entries = list(raw_dir.iterdir())
    except FileNotFoundError:
        return RawDrives(missing=True)
    except OSError:
        return RawDrives(problem="Cannot read the raw folder.")
    found: list[tuple[float, str]] = []
    for entry in entries:
        try:
            if _is_link(entry) or not _DRIVE_FOLDER.fullmatch(entry.name) or not entry.is_dir():
                continue
            found.append((entry.stat().st_mtime, entry.name))
        except OSError:
            continue
    found.sort(key=lambda item: (-item[0], item[1]))
    names = [name for _, name in found]
    return RawDrives(
        pending=[name for name in names if name not in lake_drives],
        ingested=[name for name in names if name in lake_drives],
    )


def _problem_text(error: Exception) -> str:
    """Fixed text per error kind: messages can hold file paths or config values."""
    if isinstance(error, yaml.YAMLError):
        return "The ETL config file is not valid YAML."
    if isinstance(error, pa.ArrowException):
        return f"Cannot read a table ({type(error).__name__})."
    if isinstance(error, OSError):
        return f"Cannot read or write a file ({type(error).__name__})."
    return "The drive id or the ETL settings are not valid."


def run_ingest(raw_dir: Path, drive: str, lake_dir: Path) -> IngestResult | str:
    """Ingest raw_dir/drive into the lake at lake_dir; return the result, or a problem text.

    A drive the ETL rejects is a normal IngestResult (status "rejected"). A drive name that is
    not 12 lowercase hex, or a folder that is missing, a link or not a directory, gives a fixed
    text. Config errors, write failures and any other failure become a short text with no file
    path and no config value.

    The lake has a single writer. The shared lake lock (bi/lake_lock.py, taken without waiting)
    allows one lake write (ingest or motion) at a time inside this server process; a second
    call returns a "busy" text and does not call ingest_drive. The lock does NOT cover the
    command line `ingest`, nor a second server process: do not run those at the same time.

    An unexpected exception is turned into a fixed text on purpose and is not logged: its type
    name and message can hold file paths or config values.
    """
    folder = raw_dir / drive
    try:
        usable = bool(_DRIVE_FOLDER.fullmatch(drive)) and not _is_link(folder) and folder.is_dir()
    except OSError:
        usable = False
    if not usable:
        return _NOT_AVAILABLE
    with try_lake_lock() as acquired:
        if not acquired:
            return _BUSY
        try:
            etl = load_etl_config()
            return ingest_drive(folder, Lake(lake_dir), etl)
        except (ValueError, OSError, yaml.YAMLError, pa.ArrowException) as error:
            return _problem_text(error)
        except Exception:  # noqa: BLE001 - never show str(error): it can hold paths or values
            return _UNEXPECTED


def _draw_outcome(drive: str, outcome: IngestResult | str) -> None:
    """Draw the stored result of the last click: a rejection is a warning, not an error."""
    if isinstance(outcome, str):
        st.error(outcome)
    elif outcome.status == "rejected":
        st.warning(
            f"Drive {drive} was rejected by the ETL. Open the Clean tab to see the INTEGRITY "
            f"rows (quarantine {outcome.frames_quarantined} rows)."
        )
    else:
        st.success(
            f"Drive {drive}: status ok, {outcome.frames_kept} frames kept, "
            f"{outcome.frames_rejected} rejected, {outcome.frames_missing} missing."
        )


def render_ingest_panel(
    config: Config, lake_dir: Path, lake_drives: list[str], key_prefix: str
) -> None:
    """Draw the ingest panel; never raises. key_prefix keeps widget keys unique per placement.

    Pending drives come first (newest folder preselected), then ingested ones labelled
    "re-ingest". A click runs one ingest inside st.status, stores the result in session state
    and reruns, so the sidebar re-reads the lake; the result is drawn on the next run.
    """
    st.subheader("Ingest a raw drive")
    result_key = f"{key_prefix}_ingest_outcome"
    raw = pending_raw_drives(config.raw_dir, lake_drives)
    if raw.problem is not None:
        st.error(raw.problem)
    elif not raw.pending and not raw.ingested:
        st.info(
            "No raw drive folder found. Generate a drive in the Generate tab; the raw folder "
            "is created when the first drive is written."
            if raw.missing
            else "The raw folder holds no drive folder yet. Generate a drive in the Generate tab."
        )
    else:
        options = raw.pending + raw.ingested
        select_key = f"{key_prefix}_ingest_drive"
        if st.session_state.get(select_key) not in options:
            st.session_state.pop(select_key, None)  # a folder removed since the last run
        chosen = st.selectbox(
            "Raw drive",
            options,
            format_func=lambda name: name + _REINGEST if name in raw.ingested else name,
            key=select_key,
        )
        st.caption(_SLOW_NOTE)  # shown before the click; the status block does not repeat it
        if (
            st.button(_BUTTON, key=f"{key_prefix}_ingest_button") and chosen is not None
        ):  # None only for mypy: options are not empty
            with st.status(f"Ingesting drive {chosen} ...", expanded=True) as status:
                outcome = run_ingest(config.raw_dir, chosen, lake_dir)
                status.update(label=f"Finished drive {chosen}", state="complete")
            st.session_state[result_key] = (chosen, outcome)
            if isinstance(outcome, IngestResult):  # ok and rejected drives both have a lineage row
                st.session_state[LAST_INGESTED_KEY] = chosen
            st.rerun()
    stored = st.session_state.pop(result_key, None)
    if stored is not None:
        _draw_outcome(*stored)
