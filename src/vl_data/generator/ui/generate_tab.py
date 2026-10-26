"""Generate tab: start form, segment table, speed preview and a Generate button.

The button calls generate_to_disk, the same function the CLI uses. Units: positions in cm,
speeds in cm/s, durations and time in s, sizes in bytes.
"""

import math
import shutil
from pathlib import Path
from typing import Any

import numpy as np
import plotly.graph_objects as go
import streamlit as st
from pydantic import ValidationError

from vl_data.core.config import Config
from vl_data.core.hashing import scenario_hash
from vl_data.core.manifest import Manifest, read_manifest, verify_drive
from vl_data.generator.drive import check_batch_fits, drive_dir_name
from vl_data.generator.models import MoveSegment, Scenario, load_scenario
from vl_data.generator.motion import frame_count, speed_profile
from vl_data.generator.writer import generate_to_disk

DEMO_SCENARIO = Path(__file__).resolve().parents[1] / "scenarios" / "demo.yaml"
DURATION_STEP_S = 0.0625  # s, one frame at 16 frames/s
_START_FIELDS = ("x_cm", "y_cm", "z_cm", "speed_cm_s")  # cm, cm, cm, cm/s


def _number(value: Any, what: str) -> float:
    """float(value), or ValueError naming `what` unless the cell is a finite number.

    Blank (None), text, bool, NaN and infinity are rejected with "<what> must be a finite number".
    """
    if value is None or isinstance(value, str | bool | np.bool_):
        raise ValueError(f"{what} must be a finite number")
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        raise ValueError(f"{what} must be a finite number") from None
    if not math.isfinite(number):
        raise ValueError(f"{what} must be a finite number")
    return number


def rows_to_scenario(start: dict[str, float], rows: list[dict[str, Any]]) -> Scenario:
    """Turn the start values (cm, cm/s) and segment-table rows into a validated Scenario.

    Every number goes through float(), because pydantic is strict and a table gives numpy
    numbers. end_speed_cm_s is dropped for stop rows. The seed is not an input: the Scenario
    default (0) is used. The only error raised is ValueError (pydantic's ValidationError is
    one): for an empty table, a blank, NaN, infinite, bool or unknown cell, or an invalid
    scenario. duration_s is in s, end_speed_cm_s in cm/s.
    """
    if not rows:
        raise ValueError("the segment table is empty; add at least one segment")
    start_values = {name: _number(start.get(name), f"start {name}") for name in _START_FIELDS}
    segments: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        where = f"segment {index}"
        kind = row.get("type")
        if kind not in ("move", "stop"):
            raise ValueError(f"{where}: type must be 'move' or 'stop', got {kind!r}")
        segment: dict[str, Any] = {
            "type": kind,
            "duration_s": _number(row.get("duration_s"), f"{where} duration_s"),
        }
        if kind == "move":
            segment["end_speed_cm_s"] = _number(
                row.get("end_speed_cm_s"), f"{where} end_speed_cm_s"
            )
        segments.append(segment)
    return Scenario.model_validate({"start": start_values, "segments": segments})


def preview_figure(scenario: Scenario, config: Config) -> go.Figure:
    """Speed (cm/s) against time (s) for the scenario, one point per frame at config.fps."""
    speed = speed_profile(scenario, config.fps)  # cm/s
    time_s = np.arange(len(speed)) / config.fps  # s
    figure = go.Figure(go.Scatter(x=time_s, y=speed, mode="lines", name="speed"))
    figure.update_layout(xaxis_title="time (s)", yaxis_title="speed (cm/s)", margin={"t": 20})
    return figure


def _demo_table() -> list[dict[str, Any]]:
    """The demo scenario's segments as the editor's starting rows (stop rows have no speed).

    Each row has type, duration_s (s) and end_speed_cm_s (cm/s, None for a stop).
    """
    demo = load_scenario(DEMO_SCENARIO)
    return [
        {
            "type": seg.type,
            "duration_s": seg.duration_s,
            "end_speed_cm_s": seg.end_speed_cm_s if isinstance(seg, MoveSegment) else None,
        }
        for seg in demo.segments
    ]


def _free_bytes(path: Path) -> int:
    """Free bytes on the disk holding `path`; a path not created yet uses its nearest parent.

    Raises OSError if shutil.disk_usage fails.
    """
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


def _verified_manifest(drive_dir: Path, expected_hash: str) -> Manifest | None:
    """The manifest of drive_dir if it is a folder that passes verify_drive and has this hash.

    The check runs inside an st.status block because verify_drive re-hashes every file (it
    can take a while for a large drive). Returns None if the path is missing, a symlink (the
    writer refuses those, so its error is shown), not a folder, unreadable, fails verification
    or holds another hash; the caller then writes normally.
    """
    manifest: Manifest | None = None
    try:
        if drive_dir.is_symlink() or not drive_dir.is_dir():
            return None
    except OSError:
        return None
    with st.status("Checking existing drive...", expanded=True) as status:
        try:
            if verify_drive(drive_dir).ok:
                found = read_manifest(drive_dir)
                if found.scenario_hash == expected_hash:
                    manifest = found
        except (OSError, ValueError, RecursionError):
            manifest = None
        if manifest is None:
            status.update(label="Existing drive is not usable; writing again", state="error")
        else:
            status.update(label="Existing drive verified", state="complete")
    return manifest


def _show_drive(drive_dir: Path, manifest: Manifest) -> None:
    """Show the folder, the scenario hash and the frame count (frames) of a drive."""
    st.markdown(f"drive_dir: `{drive_dir}`")
    st.markdown(f"scenario_hash: `{manifest.scenario_hash}`")
    st.markdown(f"frames: {manifest.frame_count}")


def _generate(scenario: Scenario, config: Config) -> None:
    """Skip, refuse or write the drive for `scenario` and show the outcome with st.

    Sizes are in bytes (frames x bytes per frame, an uncompressed upper bound); fps is in frames/s.
    """
    try:
        frame_bytes = check_batch_fits(config)  # bytes per frame
        full_hash = scenario_hash(scenario, config.body)
        frames = frame_count(scenario, config.fps)  # frames
    except ValueError as exc:
        st.error(f"Cannot generate: {exc}")
        return
    target = config.raw_dir / drive_dir_name(full_hash)
    existing = _verified_manifest(target, full_hash)
    if existing is not None:
        st.markdown("This drive is already generated; nothing was written.")
        _show_drive(target, existing)
        return
    needed = frames * frame_bytes  # bytes, uncompressed upper bound
    try:
        free = _free_bytes(config.raw_dir)  # bytes
    except OSError as exc:
        st.error(f"Cannot check the free disk space: {exc}")
        return
    if needed > free:
        st.error(
            f"Not enough disk space: the drive needs up to {needed:,} bytes (an upper bound, "
            f"uncompressed point data) and the disk has {free:,} bytes free. Nothing was written."
        )
        return
    st.warning(f"This drive has {frames} frames, about {needed:,} bytes (uncompressed point data).")
    problem: Exception | None = None
    with st.status("Writing drive...", expanded=True) as status:
        try:
            drive_dir = generate_to_disk(scenario, config, config.raw_dir)
            manifest = read_manifest(drive_dir)
        except (ValidationError, ValueError, OSError, RuntimeError) as exc:
            problem = exc
            status.update(label="Writing failed", state="error")
        else:
            status.update(label="Drive written and verified", state="complete")
    if problem is not None:
        notes = " ".join(getattr(problem, "__notes__", []))
        st.error(f"Cannot write drive: {problem} {notes}".strip())
        return
    _show_drive(drive_dir, manifest)


def render_generate_tab(config: Config) -> None:
    """Draw the form, preview and Generate button; Generate calls generate_to_disk.

    The form has the start state (x, y, z in cm, speed in cm/s); the table has
    one row per segment (duration_s in s, end_speed_cm_s in cm/s). Invalid input is shown as an
    error and never raises.
    """
    st.subheader("Generate a drive")
    columns = st.columns(4)
    start = {
        name: columns[i].number_input(name, value=0.0, step=1.0, format="%.1f")
        for i, name in enumerate(_START_FIELDS)
    }
    table = st.data_editor(
        _demo_table(),
        num_rows="dynamic",
        column_config={
            "type": st.column_config.SelectboxColumn("type", options=["move", "stop"]),
            "duration_s": st.column_config.NumberColumn(
                "duration_s", step=DURATION_STEP_S, format="%.4f"
            ),
            "end_speed_cm_s": st.column_config.NumberColumn("end_speed_cm_s", step=1.0),
        },
    )
    try:
        scenario = rows_to_scenario(start, list(table))
    except ValueError as exc:  # includes pydantic ValidationError
        st.error(f"Invalid scenario: {exc}")
        return
    st.plotly_chart(preview_figure(scenario, config))
    if st.button("Generate"):
        _generate(scenario, config)
