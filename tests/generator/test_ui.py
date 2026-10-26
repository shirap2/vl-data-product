"""Tests for the Generate tab. Units: cm, cm/s, s, bytes. Drives go to tmp_path only."""

import math
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
import yaml
from streamlit.testing.v1 import AppTest
from typer.testing import CliRunner

from vl_data.cli import app as cli_app
from vl_data.core.config import Config, load_config
from vl_data.core.hashing import scenario_hash
from vl_data.core.manifest import VerifyResult, read_manifest, verify_drive
from vl_data.generator import generate_to_disk
from vl_data.generator.models import MoveSegment, Scenario, StopSegment, load_scenario
from vl_data.generator.motion import speed_profile
from vl_data.generator.ui import generate_tab
from vl_data.generator.ui.generate_tab import preview_figure, rows_to_scenario

DEMO = Path(__file__).resolve().parents[2] / "src/vl_data/generator/scenarios/demo.yaml"
START = {"x_cm": 0.0, "y_cm": 0.0, "z_cm": 0.0, "speed_cm_s": 0.0}
RUN_TIMEOUT_S = 120  # s, AppTest default is 3 s; the write needs longer


def _tab_app(config: "Config") -> None:
    """AppTest entry point: draw the Generate tab (AppTest copies this function's source)."""
    from vl_data.generator.ui.generate_tab import render_generate_tab

    render_generate_tab(config)


def _small(small_config: Config, raw_dir: Path) -> Config:
    """Small-body config (10 points per axis) that writes into raw_dir."""
    return small_config.model_copy(update={"raw_dir": raw_dir})


def _run(config: Config) -> AppTest:
    """Load the tab with the default demo rows and run it once."""
    return AppTest.from_function(_tab_app, args=(config,), default_timeout=RUN_TIMEOUT_S).run()


def test_app_renders_without_exception(small_config: Config, tmp_path: Path) -> None:
    """The app loads and shows the form, the table and the preview."""
    at = _run(_small(small_config, tmp_path))
    assert not at.exception
    labels = [n.label for n in at.number_input]
    assert labels == ["x_cm", "y_cm", "z_cm", "speed_cm_s"]  # no seed box: the seed is hidden
    assert at.button[0].label == "Generate"
    assert len(at.get("plotly_chart")) == 1
    assert len(at.dataframe) == 1
    assert not at.error
    assert list(tmp_path.iterdir()) == []  # nothing is written before the click


def test_rows_to_scenario_validates() -> None:
    """Valid rows give a Scenario; invalid rows give a clear ValueError."""
    rows = [
        {"type": "move", "duration_s": 2.0, "end_speed_cm_s": 100.0},
        {"type": "move", "duration_s": 1.0, "end_speed_cm_s": 0.0},
    ]
    scenario = rows_to_scenario(START, rows)
    assert scenario.seed == 0  # the hidden default
    assert [type(s) for s in scenario.segments] == [MoveSegment, MoveSegment]
    with pytest.raises(ValueError, match="duration_s"):
        rows_to_scenario(START, [{"type": "move", "duration_s": 1.03, "end_speed_cm_s": 5.0}])
    with pytest.raises(ValueError, match="more than"):
        rows_to_scenario(START, [{"type": "stop", "duration_s": 200.0}])


def test_rows_to_scenario_stop_rows_drop_end_speed() -> None:
    """A stop row ignores end_speed_cm_s, whether NaN or a number."""
    rows = [
        {"type": "stop", "duration_s": 1.0, "end_speed_cm_s": math.nan},
        {"type": "stop", "duration_s": 1.0, "end_speed_cm_s": 50.0},
    ]
    scenario = rows_to_scenario(START, rows)
    assert all(isinstance(s, StopSegment) for s in scenario.segments)
    assert [s.duration_s for s in scenario.segments] == [1.0, 1.0]


@pytest.mark.parametrize(
    ("rows", "text"),
    [
        ([], "at least one segment"),
        ([{"type": "move", "duration_s": 1.0, "end_speed_cm_s": math.nan}], "end_speed_cm_s"),
        ([{"type": "move", "duration_s": 1.0, "end_speed_cm_s": None}], "end_speed_cm_s"),
        ([{"type": "move", "duration_s": math.nan, "end_speed_cm_s": 5.0}], "duration_s"),
        ([{"type": "move", "duration_s": None, "end_speed_cm_s": 5.0}], "duration_s"),
        ([{"type": None, "duration_s": 1.0, "end_speed_cm_s": 5.0}], "type"),
        ([{"type": "jump", "duration_s": 1.0, "end_speed_cm_s": 5.0}], "type"),
        ([{"type": "stop", "end_speed_cm_s": 5.0}], "duration_s"),
        ([{"type": "move", "duration_s": math.inf, "end_speed_cm_s": 5.0}], "segment 0 duration_s"),
        ([{"type": "move", "duration_s": 1.0, "end_speed_cm_s": -math.inf}], "end_speed_cm_s"),
        ([{"type": "move", "duration_s": True, "end_speed_cm_s": 5.0}], "segment 0 duration_s"),
        ([{"type": "move", "duration_s": 1.0, "end_speed_cm_s": np.bool_(True)}], "end_speed_cm_s"),
        ([{"type": "move", "duration_s": 10**400, "end_speed_cm_s": 5.0}], "segment 0 duration_s"),
        ([{"type": "move", "duration_s": [1.0], "end_speed_cm_s": 5.0}], "segment 0 duration_s"),
    ],
)
def test_rows_to_scenario_rejects_blank_and_empty(rows: list[dict[str, Any]], text: str) -> None:
    """Blank, NaN, unknown and missing cells and an empty table raise a clear ValueError."""
    with pytest.raises(ValueError, match=text):
        rows_to_scenario(START, rows)


def test_rows_to_scenario_rejects_blank_start() -> None:
    """A blank start value raises a clear ValueError."""
    rows = [{"type": "stop", "duration_s": 1.0, "end_speed_cm_s": math.nan}]
    with pytest.raises(ValueError, match="x_cm"):
        rows_to_scenario({**START, "x_cm": math.nan}, rows)


def test_rows_to_scenario_accepts_numpy_numbers() -> None:
    """numpy floats and ints (what a DataFrame gives) are converted; pydantic is strict."""
    rows = [
        {"type": "move", "duration_s": np.float64(2.0), "end_speed_cm_s": np.float32(100.0)},
    ]
    start = {k: np.float64(v) for k, v in START.items()}
    scenario = rows_to_scenario(start, rows)
    assert scenario.seed == 0
    assert type(scenario.start.x_cm) is float
    assert type(scenario.segments[0].duration_s) is float


def test_tab_has_no_seed_widget(small_config: Config, tmp_path: Path) -> None:
    """Only the 4 start fields are number inputs; no widget is labelled seed."""
    at = _run(_small(small_config, tmp_path))
    assert not at.exception
    assert len(at.number_input) == 4
    assert all("seed" not in str(n.label).lower() for n in at.number_input)
    assert "seed" not in " ".join(str(el.value) for el in at.markdown).lower()
    assert len(at.text_input) == 0
    assert len(at.slider) == 0
    assert len(at.checkbox) == 0
    assert len(at.selectbox) == 0
    for kind in ("text_area", "radio", "multiselect", "date_input", "number_input", "button"):
        assert all("seed" not in str(getattr(w, "label", "")).lower() for w in at.get(kind))


def test_scenario_without_seed_defaults_to_zero_and_same_hash(
    small_config: Config, tmp_path: Path
) -> None:
    """A file with no seed loads with seed 0 and hashes like the same file with `seed: 0`."""
    body = (
        "start: {x_cm: 0.0, y_cm: 0.0, z_cm: 0.0, speed_cm_s: 0.0}\n"
        "segments:\n"
        "  - {type: stop, duration_s: 2.0}\n"
    )
    without = tmp_path / "without.yaml"
    with_zero = tmp_path / "with_zero.yaml"
    without.write_text(body, encoding="utf-8")
    with_zero.write_text("seed: 0\n" + body, encoding="utf-8")
    a, b = load_scenario(without), load_scenario(with_zero)
    assert a.seed == 0
    assert b.seed == 0
    assert scenario_hash(a, small_config.body) == scenario_hash(b, small_config.body)


def test_demo_yaml_has_no_seed_key_and_golden_hash_unchanged() -> None:
    """demo.yaml has no seed key and its hash prefix stays bf394b9bfb42 (seed 0 by default)."""
    assert "seed" not in yaml.safe_load(DEMO.read_text(encoding="utf-8"))
    demo = load_scenario(DEMO)
    assert demo.seed == 0
    assert scenario_hash(demo, Config().body).startswith("bf394b9bfb42")


def test_preview_figure_values(small_config: Config) -> None:
    """The plot is speed (cm/s) against time (s) at config.fps."""
    scenario = load_scenario(DEMO)
    figure = preview_figure(scenario, small_config)
    trace = figure.data[0]
    speed = speed_profile(scenario, small_config.fps)
    assert np.array_equal(np.asarray(trace.y), speed)
    assert len(trace.x) == 1000
    assert trace.x[0] == 0.0
    assert trace.x[16] == pytest.approx(1.0)
    # By hand from demo.yaml: a 2 s stop is frames 0 to 31 at 0 cm/s; the 6 s move (96 frames) to
    # 600 cm/s adds 600 / 96 = 6.25 cm/s per frame, so frame 33 is 1 x 6.25 and 33 / 16 s in.
    assert trace.y[31] == 0.0
    assert trace.y[33] == pytest.approx(6.25)
    assert trace.x[33] == pytest.approx(2.0625)
    assert "(s)" in figure.layout.xaxis.title.text
    assert "(cm/s)" in figure.layout.yaxis.title.text


def test_generate_warns_before_writing_and_uses_status(
    small_config: Config, tmp_path: Path
) -> None:
    """The click shows the size warning and an st.status block, then the drive details."""
    config = _small(small_config, tmp_path)
    at = _run(config).button[0].click().run(timeout=RUN_TIMEOUT_S)
    assert not at.exception
    assert len(at.warning) == 1
    text = at.warning[0].value
    assert "1000 frames" in text
    assert "about 12,000,000 bytes (uncompressed point data)" in text  # 1000 x 1000 x 12
    assert len(at.status) == 1
    assert at.status[0].state == "complete"
    assert not at.error
    folder = next(tmp_path.iterdir())
    manifest = read_manifest(folder)
    shown = " ".join(str(el.value) for el in at.markdown)
    assert str(folder) in shown
    assert manifest.scenario_hash in shown
    assert f"frames: {manifest.frame_count}" in shown


def test_generate_shows_write_error(
    small_config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing write is shown as st.error and the status block ends in the error state."""

    def boom(scenario: Scenario, config: Config, out_root: Path) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr("vl_data.generator.ui.generate_tab.generate_to_disk", boom)
    at = _run(_small(small_config, tmp_path)).button[0].click().run(timeout=RUN_TIMEOUT_S)
    assert not at.exception
    assert len(at.warning) == 1  # the warning still comes before the write
    assert at.status[0].state == "error"
    assert "disk full" in at.error[0].value


def test_generate_shows_batch_size_error(small_config: Config, tmp_path: Path) -> None:
    """A body whose batch cannot fit an Arrow array is shown as st.error, not a crash."""
    body = small_config.body.model_copy(update={"size_cm": 2_000, "points_per_axis": 1_000})
    config = _small(small_config, tmp_path).model_copy(update={"body": body})
    at = _run(config).button[0].click().run(timeout=RUN_TIMEOUT_S)
    assert not at.exception
    assert "too large" in at.error[0].value
    assert not at.status
    assert list(tmp_path.iterdir()) == []


def test_bad_table_shows_error(
    small_config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ValueError from rows_to_scenario (patched here) is shown as st.error, no plot, no write.

    The real parsing is covered by the rows tests and by test_real_bad_row_shows_error.
    """

    def bad(start: dict[str, float], rows: list[dict[str, Any]]) -> Scenario:
        raise ValueError("segment 0: end_speed_cm_s is blank")

    monkeypatch.setattr("vl_data.generator.ui.generate_tab.rows_to_scenario", bad)
    at = _run(_small(small_config, tmp_path))
    assert not at.exception
    assert "end_speed_cm_s is blank" in at.error[0].value
    assert len(at.get("plotly_chart")) == 0
    assert list(tmp_path.iterdir()) == []


def test_real_bad_row_shows_error(
    small_config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real inf cell (through _demo_table, nothing else patched) reaches st.error by row."""
    rows = [{"type": "move", "duration_s": math.inf, "end_speed_cm_s": 5.0}]
    monkeypatch.setattr("vl_data.generator.ui.generate_tab._demo_table", lambda: rows)
    at = _run(_small(small_config, tmp_path))
    assert not at.exception
    assert len(at.error) == 1
    assert "segment 0 duration_s must be a finite number" in at.error[0].value
    assert len(at.get("plotly_chart")) == 0
    assert list(tmp_path.iterdir()) == []


def _patch_disk(monkeypatch: pytest.MonkeyPatch, free: int) -> list[Path]:
    """Make generate_tab's shutil.disk_usage report `free` bytes; return the paths asked."""
    asked: list[Path] = []

    def fake(path: Path) -> SimpleNamespace:
        asked.append(Path(path))
        return SimpleNamespace(free=free)

    monkeypatch.setattr(generate_tab, "shutil", SimpleNamespace(disk_usage=fake))
    return asked


def _count_writes(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Wrap generate_tab.generate_to_disk so each call is recorded; return the call list."""
    calls: list[Path] = []
    real = generate_tab.generate_to_disk

    def counting(scenario: Scenario, config: Config, out_root: Path) -> Path:
        calls.append(out_root)
        return real(scenario, config, out_root)

    monkeypatch.setattr(generate_tab, "generate_to_disk", counting)
    return calls


def test_disk_guard_refuses_when_not_enough_space(
    small_config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Needed bytes over free bytes: st.error with both numbers, no status, nothing written."""
    _patch_disk(monkeypatch, 1)
    calls = _count_writes(monkeypatch)
    at = _run(_small(small_config, tmp_path)).button[0].click().run(timeout=RUN_TIMEOUT_S)
    assert not at.exception
    assert len(at.error) == 1
    text = at.error[0].value
    assert "disk" in text
    assert "12,000,000" in text  # needed bytes, 1000 frames x 12,000 bytes
    assert "1 bytes" in text  # free bytes
    assert "upper bound" in text
    assert not at.status
    assert not at.warning
    assert list(tmp_path.iterdir()) == []
    assert calls == []


def test_disk_guard_passes_with_enough_space(
    small_config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Free space equal to the estimate is enough: the drive is written."""
    _patch_disk(monkeypatch, 12_000_000)
    at = _run(_small(small_config, tmp_path)).button[0].click().run(timeout=RUN_TIMEOUT_S)
    assert not at.exception
    assert not at.error
    assert at.status[0].state == "complete"
    assert len(list(tmp_path.iterdir())) == 1


def test_disk_guard_uses_nearest_existing_parent(
    small_config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raw_dir that does not exist yet is checked through its nearest existing parent."""
    asked = _patch_disk(monkeypatch, 10**12)
    config = _small(small_config, tmp_path / "not" / "yet")
    at = _run(config).button[0].click().run(timeout=RUN_TIMEOUT_S)
    assert not at.exception
    assert not at.error
    assert asked == [tmp_path]
    assert len(list((tmp_path / "not" / "yet").iterdir())) == 1


def test_disk_guard_shows_oserror(
    small_config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An OSError from disk_usage is shown as st.error and nothing is written."""

    def broken(path: Path) -> SimpleNamespace:
        raise OSError("no such device")

    monkeypatch.setattr(generate_tab, "shutil", SimpleNamespace(disk_usage=broken))
    at = _run(_small(small_config, tmp_path)).button[0].click().run(timeout=RUN_TIMEOUT_S)
    assert not at.exception
    assert "no such device" in at.error[0].value
    assert not at.status
    assert list(tmp_path.iterdir()) == []


def test_non_whole_number_body_shows_error(small_config: Config, tmp_path: Path) -> None:
    """A body size that is not a whole number cannot be hashed: st.error, nothing written."""
    body = small_config.body.model_copy(update={"size_cm": 100.5})
    config = _small(small_config, tmp_path).model_copy(update={"body": body})
    at = _run(config).button[0].click().run(timeout=RUN_TIMEOUT_S)
    assert not at.exception
    assert len(at.error) == 1
    assert "whole number" in at.error[0].value
    assert "size_cm" in at.error[0].value
    assert not at.status
    assert list(tmp_path.iterdir()) == []


def test_existing_verified_drive_is_skipped(
    small_config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A verified drive with this hash is reported as already generated; nothing is written."""
    config = _small(small_config, tmp_path)
    folder = generate_to_disk(load_scenario(DEMO), config, tmp_path)
    before = read_manifest(folder)
    calls = _count_writes(monkeypatch)
    _patch_disk(monkeypatch, 1)  # 1 byte free: the skip must run before the disk guard
    at = _run(config).button[0].click().run(timeout=RUN_TIMEOUT_S)
    assert not at.exception
    assert not at.error
    assert calls == []
    assert not at.warning
    assert len(at.status) == 1  # only the check, no write block
    shown = " ".join(str(el.value) for el in at.markdown)
    assert "already generated" in shown
    assert str(folder) in shown
    assert before.scenario_hash in shown
    assert f"frames: {before.frame_count}" in shown
    assert read_manifest(folder) == before


def test_corrupt_existing_drive_is_replaced(
    small_config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A folder that fails verify_drive falls through to a normal write that replaces it."""
    config = _small(small_config, tmp_path)
    folder = generate_to_disk(load_scenario(DEMO), config, tmp_path)
    (folder / "frames.parquet").unlink()
    assert not verify_drive(folder).ok
    calls = _count_writes(monkeypatch)
    at = _run(config).button[0].click().run(timeout=RUN_TIMEOUT_S)
    assert not at.exception
    assert not at.error
    assert len(calls) == 1
    assert len(at.warning) == 1
    assert "already generated" not in " ".join(str(el.value) for el in at.markdown)
    assert verify_drive(folder).ok
    assert (folder / "frames.parquet").is_file()


def test_unreadable_existing_folder_falls_through(
    small_config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An OSError while verifying the old folder does not stop a normal write."""
    config = _small(small_config, tmp_path)
    folder = generate_to_disk(load_scenario(DEMO), config, tmp_path)

    def broken(path: Path) -> None:
        raise OSError("cannot list")

    monkeypatch.setattr(generate_tab, "verify_drive", broken)
    calls = _count_writes(monkeypatch)
    at = _run(config).button[0].click().run(timeout=RUN_TIMEOUT_S)
    assert not at.exception
    assert not at.error
    assert len(calls) == 1
    assert verify_drive(folder).ok


def test_existing_drive_with_other_hash_is_not_skipped(
    small_config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A folder that verifies but whose manifest hash differs from the target is rewritten."""
    config = _small(small_config, tmp_path)
    folder = generate_to_disk(load_scenario(DEMO), config, tmp_path)
    other = read_manifest(folder).model_copy(update={"scenario_hash": "0" * 64})
    monkeypatch.setattr(generate_tab, "read_manifest", lambda d: other)
    monkeypatch.setattr(generate_tab, "verify_drive", lambda d: VerifyResult(True, []))
    calls = _count_writes(monkeypatch)
    at = _run(config).button[0].click().run(timeout=RUN_TIMEOUT_S)
    assert not at.exception
    assert len(calls) == 1


def _target(config: Config) -> Path:
    """The folder the tab writes the demo drive to: raw_dir / first 12 hash characters."""
    full_hash = read_hash(config)
    return config.raw_dir / full_hash[:12]


def read_hash(config: Config) -> str:
    """Scenario hash of the demo scenario for this config's body."""
    return scenario_hash(load_scenario(DEMO), config.body)


def test_symlink_at_target_is_refused_not_skipped(
    small_config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symlink to a good same-hash drive is not "already generated": the writer refuses it."""
    config = _small(small_config, tmp_path / "raw")
    real = generate_to_disk(load_scenario(DEMO), config, tmp_path / "elsewhere")
    before = read_manifest(real)
    target = _target(config)
    target.parent.mkdir()
    try:
        os.symlink(real, target, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"cannot create a symlink on this machine: {exc}")
    calls = _count_writes(monkeypatch)
    at = _run(config).button[0].click().run(timeout=RUN_TIMEOUT_S)
    assert not at.exception
    assert len(at.error) == 1
    assert "Cannot write drive" in at.error[0].value
    assert "already generated" not in " ".join(str(el.value) for el in at.markdown)
    assert len(calls) == 1  # the writer was asked and refused
    assert target.is_symlink()
    assert read_manifest(real) == before
    assert verify_drive(real).ok
    assert sorted(p.name for p in (tmp_path / "raw").iterdir()) == [target.name]


def test_target_reported_as_symlink_is_refused_not_skipped(
    small_config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same as the symlink test but needs no rights: is_symlink answers True for a good drive."""
    config = _small(small_config, tmp_path)
    target = generate_to_disk(load_scenario(DEMO), config, tmp_path)
    before = read_manifest(target)
    real_is_symlink = Path.is_symlink

    def linked(self: Path) -> bool:
        return self == target or real_is_symlink(self)

    monkeypatch.setattr(Path, "is_symlink", linked)
    calls = _count_writes(monkeypatch)
    at = _run(config).button[0].click().run(timeout=RUN_TIMEOUT_S)
    assert not at.exception
    assert len(at.error) == 1
    assert "not a folder" in at.error[0].value
    assert "already generated" not in " ".join(str(el.value) for el in at.markdown)
    assert len(calls) == 1  # the writer was asked and refused
    assert read_manifest(target) == before
    assert [p.name for p in tmp_path.iterdir()] == [target.name]


def test_file_at_target_is_refused(small_config: Config, tmp_path: Path) -> None:
    """A plain file where the drive folder should be gives st.error and stays untouched."""
    config = _small(small_config, tmp_path)
    target = _target(config)
    target.write_text("keep me", encoding="utf-8")
    at = _run(config).button[0].click().run(timeout=RUN_TIMEOUT_S)
    assert not at.exception
    assert len(at.error) == 1
    assert "not a folder" in at.error[0].value
    assert target.read_text(encoding="utf-8") == "keep me"
    assert [p.name for p in tmp_path.iterdir()] == [target.name]


def test_raw_dir_that_is_a_file_shows_error(small_config: Config, tmp_path: Path) -> None:
    """A raw_dir that is a file gives st.error and nothing is created or changed."""
    raw = tmp_path / "raw_is_a_file"
    raw.write_text("keep me", encoding="utf-8")
    at = _run(_small(small_config, raw)).button[0].click().run(timeout=RUN_TIMEOUT_S)
    assert not at.exception
    assert len(at.error) == 1
    assert raw.read_text(encoding="utf-8") == "keep me"
    assert [p.name for p in tmp_path.iterdir()] == [raw.name]


def test_relative_raw_dir_works_for_disk_guard(
    small_config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative raw_dir is checked through its nearest existing parent (the current folder)."""
    monkeypatch.chdir(tmp_path)
    asked = _patch_disk(monkeypatch, 10**12)
    config = _small(small_config, Path("rel") / "raw")
    at = _run(config).button[0].click().run(timeout=RUN_TIMEOUT_S)
    assert not at.exception
    assert not at.error
    assert asked == [Path(".")]
    assert len(list((tmp_path / "rel" / "raw").iterdir())) == 1


def test_recursion_error_while_verifying_falls_through(
    small_config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A RecursionError (deeply nested manifest) in the check does not stop a normal write."""
    config = _small(small_config, tmp_path)
    folder = generate_to_disk(load_scenario(DEMO), config, tmp_path)

    def deep(path: Path) -> None:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(generate_tab, "verify_drive", deep)
    calls = _count_writes(monkeypatch)
    at = _run(config).button[0].click().run(timeout=RUN_TIMEOUT_S)
    assert not at.exception
    assert not at.error
    assert len(calls) == 1
    assert verify_drive(folder).ok


def test_permission_error_from_is_dir_falls_through(
    small_config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PermissionError from is_dir on the target is an OSError: normal write, no crash."""
    config = _small(small_config, tmp_path)
    target = _target(config)
    real_is_dir = Path.is_dir

    def denied(self: Path) -> bool:
        if self == target:
            raise PermissionError("access denied")
        return real_is_dir(self)

    monkeypatch.setattr(Path, "is_dir", denied)
    calls = _count_writes(monkeypatch)
    at = _run(config).button[0].click().run(timeout=RUN_TIMEOUT_S)
    assert not at.exception
    assert not at.error
    assert len(calls) == 1
    assert any(target.iterdir())  # the drive was written


def test_generate_button_matches_cli(
    small_config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    """The button writes the same drive (hash, file hashes) as generate_to_disk and the CLI."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        f"raw_dir: {(tmp_path / 'ui').as_posix()}\nbody: {{size_cm: 100, points_per_axis: 10}}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("vl_data.core.config.DEFAULT_CONFIG_PATH", config_file)
    monkeypatch.delenv("VL_RAW_DIR", raising=False)
    config = load_config()
    assert config.body == small_config.body

    at = _run(config).button[0].click().run(timeout=RUN_TIMEOUT_S)
    assert not at.exception
    assert not at.error
    ui_dir = next((tmp_path / "ui").iterdir())

    direct_dir = generate_to_disk(load_scenario(DEMO), config, tmp_path / "direct")
    result = runner.invoke(
        cli_app, ["generate", "--scenario", str(DEMO), "--out", str(tmp_path / "cli")]
    )
    assert result.exit_code == 0, result.output
    cli_dir = next((tmp_path / "cli").iterdir())

    ui, direct, cli = (read_manifest(d) for d in (ui_dir, direct_dir, cli_dir))
    assert ui.scenario_hash == direct.scenario_hash == cli.scenario_hash
    assert ui_dir.name == direct_dir.name == cli_dir.name == ui.scenario_hash[:12]
    assert ui.files == direct.files == cli.files  # sha256 and bytes of every file


def test_generate_app_script_imports_and_runs_in_bare_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running the runner file as a plain script (no Streamlit server) does not raise.

    Run in-process under the module name so that coverage sees its three lines. It stays
    before the AppTest run of the same file, which would make coverage skip the file.
    Measured 2026-10-25 (these two tests, --cov=vl_data): this test first gives 3 of 3 lines
    (100%) for generate_app.py; the AppTest test first gives 0 of 3 (0%, lines 6-9 missed).
    Log: .claude/run-proofs/TASK-7-1-iter2-cov-order.log (local, git-ignored, A28).
    """
    monkeypatch.setenv("VL_RAW_DIR", str(tmp_path))
    script = Path(generate_tab.__file__).with_name("generate_app.py")
    namespace = {"__name__": "vl_data.generator.ui.generate_app"}  # coverage matches on this
    exec(compile(script.read_text(encoding="utf-8"), str(script), "exec"), namespace)
    assert list(tmp_path.iterdir()) == []


def test_generate_app_script_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The standalone runner file loads the config and draws the tab (no click, no write)."""
    monkeypatch.setenv("VL_RAW_DIR", str(tmp_path))
    script = Path(generate_tab.__file__).with_name("generate_app.py")
    at = AppTest.from_file(str(script), default_timeout=RUN_TIMEOUT_S).run()
    assert not at.exception
    assert at.button[0].label == "Generate"
    assert list(tmp_path.iterdir()) == []
