"""Generate tab: start form, segment table, speed preview and a Generate button.

The button calls generate_to_disk, the same function the CLI uses. Units: positions in cm,
speeds in cm/s, durations and time in s, sizes in bytes.
"""

import math
from pathlib import Path
from typing import Any

import numpy as np
import plotly.graph_objects as go
import streamlit as st
from pydantic import ValidationError

from vl_data.core.config import Config
from vl_data.core.manifest import read_manifest
from vl_data.generator.drive import check_batch_fits
from vl_data.generator.models import MoveSegment, Scenario, load_scenario
from vl_data.generator.motion import frame_count, speed_profile
from vl_data.generator.writer import generate_to_disk

DEMO_SCENARIO = Path(__file__).resolve().parents[1] / "scenarios" / "demo.yaml"
DURATION_STEP_S = 0.0625  # s, one frame at 16 frames/s
_START_FIELDS = ("x_cm", "y_cm", "z_cm", "speed_cm_s")  # cm, cm, cm, cm/s


def _number(value: Any, what: str) -> float:
    """float(value), or ValueError naming `what` when the cell is blank or not a finite number."""
    if value is None or isinstance(value, str):
        raise ValueError(f"{what} is blank or not a number")
    number = float(value)
    if math.isnan(number):
        raise ValueError(f"{what} is blank or not a number")
    return number


def rows_to_scenario(start: dict[str, float], rows: list[dict[str, Any]], seed: int) -> Scenario:
    """Turn the start values (cm, cm/s) and segment-table rows into a validated Scenario.

    Every number goes through float() and the seed through int(), because pydantic is strict
    and a table gives numpy numbers. end_speed_cm_s is dropped for stop rows. Raises ValueError
    for an empty table or a blank, NaN or unknown cell, and pydantic ValidationError (a
    ValueError) for an invalid scenario. duration_s is in s, end_speed_cm_s in cm/s.
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
    return Scenario.model_validate({"seed": int(seed), "start": start_values, "segments": segments})


def preview_figure(scenario: Scenario, config: Config) -> go.Figure:
    """Speed (cm/s) against time (s) for the scenario, one point per frame at config.fps."""
    speed = speed_profile(scenario, config.fps)  # cm/s
    time_s = np.arange(len(speed)) / config.fps  # s
    figure = go.Figure(go.Scatter(x=time_s, y=speed, mode="lines", name="speed"))
    figure.update_layout(xaxis_title="time (s)", yaxis_title="speed (cm/s)", margin={"t": 20})
    return figure


def _demo_table() -> list[dict[str, Any]]:
    """The demo scenario's segments as the editor's starting rows (stop rows have no speed)."""
    demo = load_scenario(DEMO_SCENARIO)
    return [
        {
            "type": seg.type,
            "duration_s": seg.duration_s,
            "end_speed_cm_s": seg.end_speed_cm_s if isinstance(seg, MoveSegment) else None,
        }
        for seg in demo.segments
    ]


def _generate(scenario: Scenario, config: Config) -> None:
    """Warn about the size, write the drive inside a status block, and show the outcome."""
    try:
        frame_bytes = check_batch_fits(config)  # bytes per frame
    except ValueError as exc:
        st.error(f"Cannot generate: {exc}")
        return
    frames = frame_count(scenario, config.fps)
    st.warning(
        f"This drive has {frames} frames, about {frames * frame_bytes:,} bytes "
        "(uncompressed point data)."
    )
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
    st.markdown(f"drive_dir: `{drive_dir}`")
    st.markdown(f"scenario_hash: `{manifest.scenario_hash}`")
    st.markdown(f"frames: {manifest.frame_count}")


def render_generate_tab(config: Config) -> None:
    """Draw the form, preview and Generate button; Generate calls generate_to_disk.

    The form has the start state (x, y, z in cm, speed in cm/s) and the seed; the table has
    one row per segment (duration_s in s, end_speed_cm_s in cm/s). Invalid input is shown as an
    error and never raises.
    """
    st.subheader("Generate a drive")
    columns = st.columns(4)
    start = {
        name: columns[i].number_input(name, value=0.0, step=1.0, format="%.1f")
        for i, name in enumerate(_START_FIELDS)
    }
    seed = st.number_input("seed", value=0, step=1, min_value=0)
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
        scenario = rows_to_scenario(start, list(table), int(seed))
    except ValueError as exc:  # includes pydantic ValidationError
        st.error(f"Invalid scenario: {exc}")
        return
    st.plotly_chart(preview_figure(scenario, config))
    if st.button("Generate"):
        _generate(scenario, config)
