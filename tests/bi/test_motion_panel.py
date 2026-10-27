"""Tests of the motion panel, the shared lake lock and the Analyze tab states.

Units: frames and rows are counts; positions cm; velocities cm/s; accelerations cm/s^2; time s.
"""

import shutil
from pathlib import Path

import pyarrow as pa
import pytest
import yaml
from streamlit.testing.v1 import AppTest

import vl_data.bi.lake_lock as lake_lock_module
from vl_data.bi.ingest_panel import run_ingest
from vl_data.bi.motion_panel import run_motion
from vl_data.core.config import Config
from vl_data.core.lake import Lake
from vl_data.etl.ingest import IngestResult
from vl_data.generator.models import Scenario
from vl_data.generator.writer import generate_to_disk
from vl_data.sdk.client import VLClient
from vl_data.usecase.pipeline import MotionError, MotionResult

from .helpers import SCENARIO, BiDrive

MOTION_KEYS = ["motion_position", "motion_velocity", "motion_acceleration"]
BUSY = "Another write to the lake is running. Try again when it has finished."
INGEST_BUSY = "Another ingest is running. Try again when it has finished."
OUTCOME_KEY = "analyze_motion_outcome"


def analyze_script(client, config, lake_dir, drive):  # noqa: ANN001, ANN201
    from vl_data.bi.analyze_tab import render_analyze_tab

    render_analyze_tab(client, config, lake_dir, drive)


def switch_script(config, lake_dir, drives):  # noqa: ANN001, ANN201
    import streamlit as st

    from vl_data.bi.analyze_tab import render_analyze_tab
    from vl_data.core.lake import Lake
    from vl_data.sdk.client import VLClient

    drive = st.selectbox("d", drives, key="sel")
    render_analyze_tab(VLClient(Lake(lake_dir)), config, lake_dir, drive)


def _copy(d: BiDrive, tmp_path: Path) -> Path:
    lake_dir = tmp_path / "lake"
    shutil.copytree(d.root / "lake", lake_dir)
    return lake_dir


def _app(lake_dir: Path, config: Config, drive: str, tmp_path: Path) -> AppTest:
    cfg = config.model_copy(update={"raw_dir": tmp_path / "raw"})
    args = (VLClient(Lake(lake_dir)), cfg, lake_dir, drive)
    return AppTest.from_function(analyze_script, args=args, default_timeout=60)


def _keys(at: AppTest) -> list[str]:
    return [element.key for element in at.get("plotly_chart")]


def _boom(error: Exception):  # noqa: ANN202
    def raiser(*args: object, **kwargs: object) -> object:
        raise error

    return raiser


def _motion_rows(lake_dir: Path, drive: str) -> int:
    return Lake(lake_dir).read("motion", drive).num_rows


# ---------------------------------------------------------------- lake_lock


def test_lake_lock_helper_acquires_and_releases() -> None:
    with lake_lock_module.try_lake_lock() as got:
        assert got
        assert lake_lock_module.LAKE_LOCK.locked()
        with lake_lock_module.try_lake_lock() as second:
            assert not second
        assert lake_lock_module.LAKE_LOCK.locked()  # the loser did not release it
    assert not lake_lock_module.LAKE_LOCK.locked()


def test_lake_lock_is_released_after_an_exception() -> None:
    with pytest.raises(RuntimeError), lake_lock_module.try_lake_lock():
        raise RuntimeError("x")
    assert not lake_lock_module.LAKE_LOCK.locked()


# ---------------------------------------------------------------- run_motion


def test_run_motion_writes_one_row_per_kept_frame(
    unmotioned: BiDrive, bi_config: Config, tmp_path: Path
) -> None:
    lake_dir = _copy(unmotioned, tmp_path)
    assert _motion_rows(lake_dir, unmotioned.drive_id) == 0
    result = run_motion(bi_config, lake_dir, unmotioned.drive_id)
    assert isinstance(result, MotionResult)
    assert result.frames_read == unmotioned.ingest.frames_kept
    assert _motion_rows(lake_dir, unmotioned.drive_id) == unmotioned.ingest.frames_kept
    assert not lake_lock_module.LAKE_LOCK.locked()


def test_run_motion_not_ingested_gives_motion_error_text(
    unmotioned: BiDrive, bi_config: Config, tmp_path: Path
) -> None:
    result = run_motion(bi_config, _copy(unmotioned, tmp_path), "0" * 12)
    assert isinstance(result, str)
    assert "0" * 12 in result
    assert len(result) <= 200
    assert not lake_lock_module.LAKE_LOCK.locked()


def test_run_motion_rejected_drive_gives_motion_error_text(
    rejected: BiDrive, bi_config: Config, tmp_path: Path
) -> None:
    lake_dir = _copy(rejected, tmp_path)
    result = run_motion(bi_config, lake_dir, rejected.drive_id)
    assert isinstance(result, str)
    assert "rejected" in result
    assert _motion_rows(lake_dir, rejected.drive_id) == 0


def test_run_motion_error_text_is_capped(
    unmotioned: BiDrive, bi_config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("vl_data.bi.motion_panel.compute_motion", _boom(MotionError("x" * 500)))
    result = run_motion(bi_config, _copy(unmotioned, tmp_path), unmotioned.drive_id)
    assert result == "x" * 200


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (pa.ArrowInvalid("C:/secret value=42"), "Cannot read a table (ArrowInvalid)."),
        (ValueError("C:/secret value=42"), "The motion settings or the drive id are not valid."),
        (OSError("C:/secret value=42"), "Cannot read or write a file (OSError)."),
        (yaml.YAMLError("C:/secret value=42"), "The motion config file is not valid YAML."),
        (RuntimeError("C:/secret value=42"), "Motion computation failed unexpectedly."),
    ],
)
@pytest.mark.parametrize("where", ["config", "compute"])
def test_run_motion_failure_texts_are_fixed(
    unmotioned: BiDrive,
    bi_config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected: str,
    where: str,
) -> None:
    target = "load_motion_config" if where == "config" else "compute_motion"
    monkeypatch.setattr(f"vl_data.bi.motion_panel.{target}", _boom(error))
    result = run_motion(bi_config, _copy(unmotioned, tmp_path), unmotioned.drive_id)
    assert result == expected
    assert not lake_lock_module.LAKE_LOCK.locked()


def test_motion_is_blocked_while_an_ingest_holds_the_lock(
    unmotioned: BiDrive, bi_config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[str] = []
    monkeypatch.setattr(
        "vl_data.bi.motion_panel.compute_motion", lambda *a, **k: called.append("x")
    )
    assert lake_lock_module.LAKE_LOCK.acquire(blocking=False)  # what an ingest holds
    try:
        assert run_motion(bi_config, _copy(unmotioned, tmp_path), unmotioned.drive_id) == BUSY
        assert lake_lock_module.LAKE_LOCK.locked()
    finally:
        lake_lock_module.LAKE_LOCK.release()
    assert called == []


def test_ingest_is_blocked_while_a_motion_run_holds_the_lock(
    unmotioned: BiDrive, bi_config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[object] = []
    raw = tmp_path / "raw"
    (raw / ("a" * 12)).mkdir(parents=True)

    def during(*args: object, **kwargs: object) -> MotionResult:
        seen.append(run_ingest(raw, "a" * 12, tmp_path / "lake2"))
        return MotionResult("d", 0, 0, 0)

    monkeypatch.setattr("vl_data.bi.motion_panel.compute_motion", during)
    result = run_motion(bi_config, _copy(unmotioned, tmp_path), unmotioned.drive_id)
    assert isinstance(result, MotionResult)
    assert seen == [INGEST_BUSY]
    assert not lake_lock_module.LAKE_LOCK.locked()


# ---------------------------------------------------------------- Analyze tab states


def test_no_motion_ok_drive_shows_info_button_and_command(
    unmotioned: BiDrive, bi_config: Config, tmp_path: Path
) -> None:
    at = _app(_copy(unmotioned, tmp_path), bi_config, unmotioned.drive_id, tmp_path).run()
    assert not at.exception
    assert not at.error
    assert [i.value for i in at.info] == ["Motion is not computed yet"]
    assert [b.label for b in at.button] == ["Compute motion"]
    assert at.button[0].key == "analyze_compute_motion"
    assert len(at.code) == 1
    assert "python -m vl_data motion" in at.code[0].value
    assert _keys(at) == []


def test_click_compute_shows_the_three_charts_and_the_result(
    unmotioned: BiDrive, bi_config: Config, tmp_path: Path
) -> None:
    lake_dir = _copy(unmotioned, tmp_path)
    at = _app(lake_dir, bi_config, unmotioned.drive_id, tmp_path).run()
    at.button(key="analyze_compute_motion").click()
    at.run()
    assert not at.exception
    assert not at.error
    assert _keys(at) == MOTION_KEYS
    assert [b.key for b in at.button] == ["analyze_recompute_motion"]
    frames = unmotioned.ingest.frames_kept
    assert _motion_rows(lake_dir, unmotioned.drive_id) == frames
    shown = [s.value for s in at.success]
    assert len(shown) == 1
    assert f"{frames} frames read" in shown[0]
    at.run()  # drawn once: the next run does not show it again
    assert not at.success


def test_recompute_keeps_the_row_count(
    unmotioned: BiDrive, bi_config: Config, tmp_path: Path
) -> None:
    lake_dir = _copy(unmotioned, tmp_path)
    assert isinstance(run_motion(bi_config, lake_dir, unmotioned.drive_id), MotionResult)
    at = _app(lake_dir, bi_config, unmotioned.drive_id, tmp_path).run()
    assert [b.label for b in at.button] == ["Recompute motion"]
    assert _keys(at) == MOTION_KEYS
    at.button(key="analyze_recompute_motion").click()
    at.run()
    assert not at.exception
    assert len(at.success) == 1
    assert _motion_rows(lake_dir, unmotioned.drive_id) == unmotioned.ingest.frames_kept


def test_failed_compute_shows_error_text_and_no_charts(
    unmotioned: BiDrive, bi_config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("vl_data.bi.motion_panel.run_motion", lambda *a, **k: "fixed problem")
    at = _app(_copy(unmotioned, tmp_path), bi_config, unmotioned.drive_id, tmp_path).run()
    at.button(key="analyze_compute_motion").click()
    at.run()
    assert [e.value for e in at.error] == ["fixed problem"]
    assert [i.value for i in at.info] == ["Motion is not computed yet"]
    assert _keys(at) == []


def test_ok_drive_with_motion_shows_recompute_button(
    clean: BiDrive, bi_config: Config, tmp_path: Path
) -> None:
    at = _app(_copy(clean, tmp_path), bi_config, clean.drive_id, tmp_path).run()
    assert not at.exception
    assert not at.error
    assert not at.warning
    assert _keys(at) == MOTION_KEYS
    assert [b.label for b in at.button] == ["Recompute motion"]
    assert not at.code


def test_rejected_drive_without_motion_has_no_button(
    rejected: BiDrive, bi_config: Config, tmp_path: Path
) -> None:
    at = _app(_copy(rejected, tmp_path), bi_config, rejected.drive_id, tmp_path).run()
    assert not at.button
    assert "rejected" in at.error[0].value


def test_rejected_drive_with_old_motion_shows_warning_and_no_button(
    unmotioned: BiDrive, bi_config: Config, tmp_path: Path
) -> None:
    lake_dir = _copy(unmotioned, tmp_path)
    assert isinstance(run_motion(bi_config, lake_dir, unmotioned.drive_id), MotionResult)
    lake = Lake(lake_dir)
    table = lake.read("lineage", unmotioned.drive_id).drop_columns(["drive"])
    rows = table.to_pylist()
    rows[0]["status"] = "rejected"
    changed = pa.Table.from_pylist(rows, schema=table.schema)
    lake.swap_partition("lineage", unmotioned.drive_id, changed.to_batches())
    at = _app(lake_dir, bi_config, unmotioned.drive_id, tmp_path).run()
    assert not at.exception
    assert not at.error
    assert _keys(at) == MOTION_KEYS
    assert not at.button
    warnings = [w.value for w in at.warning]
    assert len(warnings) == 1
    assert "not ok" in warnings[0]
    assert "earlier run" in warnings[0]


def test_stored_result_is_shown_only_for_the_selected_drive(
    unmotioned: BiDrive, bi_config: Config, tmp_path: Path
) -> None:
    lake_dir = _copy(unmotioned, tmp_path)
    scenario = Scenario.model_validate({**SCENARIO, "seed": 5})
    second = generate_to_disk(scenario, bi_config, tmp_path / "src")
    assert isinstance(run_ingest(tmp_path / "src", second.name, lake_dir), IngestResult)
    assert second.name != unmotioned.drive_id
    drives = sorted([unmotioned.drive_id, second.name])
    cfg = bi_config.model_copy(update={"raw_dir": tmp_path / "raw"})
    at = AppTest.from_function(switch_script, args=(cfg, lake_dir, drives), default_timeout=60)
    at.run()
    selected = at.selectbox(key="sel").value
    other = drives[1] if selected == drives[0] else drives[0]
    at.session_state[OUTCOME_KEY] = (other, "stale text")
    at.run()
    assert not at.exception
    assert "stale text" not in [e.value for e in at.error]
    assert OUTCOME_KEY not in at.session_state
    at.session_state[OUTCOME_KEY] = (selected, "own text")
    at.run()
    assert "own text" in [e.value for e in at.error]
