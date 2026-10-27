"""Motion panel of the Analyze tab: compute or recompute the motion table on a click.

It writes the lake only through usecase.pipeline.compute_motion (the function the `motion`
command calls), one drive per click. The write shares the lock of the ingest panel.
Units: frames are counts; the stored motion is cm, cm/s, cm/s^2 (shown by the Analyze tab).
"""

from pathlib import Path

import pyarrow as pa
import streamlit as st
import yaml

from vl_data.bi.lake_lock import try_lake_lock
from vl_data.core.config import Config
from vl_data.core.lake import Lake
from vl_data.sdk.client import VLClient
from vl_data.usecase.config import load_motion_config
from vl_data.usecase.pipeline import MotionError, MotionResult, compute_motion

OUTCOME_KEY = "analyze_motion_outcome"  # session key: (drive, MotionResult | str)
_BUSY = "Another write to the lake is running. Try again when it has finished."
_UNEXPECTED = "Motion computation failed unexpectedly."
_MAX_TEXT = 200  # characters of a MotionError text shown


def run_motion(config: Config, lake_dir: Path, drive: str) -> MotionResult | str:
    """Compute the motion table of a drive in the lake; return the result or a problem text.

    Loads the motion config at call time. A MotionError text (it names the drive, a status and
    counts, never a path) is shown cut to 200 characters; every other failure becomes a fixed
    text with no path and no config value. The shared lake lock is taken without waiting; when
    busy, compute_motion is not called. The command line `motion` is not covered by the lock.
    """
    with try_lake_lock() as acquired:
        if not acquired:
            return _BUSY
        try:
            cfg = load_motion_config()
            lake = Lake(lake_dir)
            return compute_motion(VLClient(lake), lake, drive, config, cfg)
        except MotionError as error:
            return str(error)[:_MAX_TEXT]
        except pa.ArrowException as error:
            return f"Cannot read a table ({type(error).__name__})."
        except ValueError:
            return "The motion settings or the drive id are not valid."
        except OSError as error:
            return f"Cannot read or write a file ({type(error).__name__})."
        except yaml.YAMLError:
            return "The motion config file is not valid YAML."
        except Exception:  # noqa: BLE001 - never show str(error): it can hold paths or values
            return _UNEXPECTED


def _draw_outcome(drive: str, outcome: MotionResult | str) -> None:
    if isinstance(outcome, str):
        st.error(outcome)
    else:
        st.success(
            f"Drive {drive}: motion computed, {outcome.frames_read} frames read, "
            f"{outcome.frames_estimated} estimated, {outcome.frames_null} null."
        )


def render_motion_panel(
    config: Config, lake_dir: Path, drive: str, *, recompute: bool = False
) -> None:
    """Draw the Compute (or Recompute) motion button and the result of the last click.

    A click runs run_motion inside st.status, stores (drive, outcome) in session state and
    reruns, so the charts appear at once; the outcome is drawn once, and only when the stored
    drive is the selected one. Never raises.
    """
    key = "analyze_recompute_motion" if recompute else "analyze_compute_motion"
    label = "Recompute motion" if recompute else "Compute motion"
    if st.button(label, key=key):
        with st.status(f"Computing motion of drive {drive} ...", expanded=True) as status:
            outcome = run_motion(config, lake_dir, drive)
            status.update(label=f"Finished drive {drive}", state="complete")
        st.session_state[OUTCOME_KEY] = (drive, outcome)
        st.rerun()
    stored = st.session_state.pop(OUTCOME_KEY, None)
    if stored is not None and stored[0] == drive:
        _draw_outcome(*stored)
