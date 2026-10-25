"""Tests for the Generate tab. Units: cm, cm/s, s, bytes. Drives go to tmp_path only."""

import math
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from streamlit.testing.v1 import AppTest
from typer.testing import CliRunner

from vl_data.cli import app as cli_app
from vl_data.core.config import Config, load_config
from vl_data.core.manifest import read_manifest
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
    assert labels == ["x_cm", "y_cm", "z_cm", "speed_cm_s", "seed"]
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
    scenario = rows_to_scenario(START, rows, 3)
    assert scenario.seed == 3
    assert [type(s) for s in scenario.segments] == [MoveSegment, MoveSegment]
    with pytest.raises(ValueError, match="duration_s"):
        rows_to_scenario(START, [{"type": "move", "duration_s": 1.03, "end_speed_cm_s": 5.0}], 0)
    with pytest.raises(ValueError, match="more than"):
        rows_to_scenario(START, [{"type": "stop", "duration_s": 200.0}], 0)


def test_rows_to_scenario_stop_rows_drop_end_speed() -> None:
    """A stop row ignores end_speed_cm_s, whether NaN or a number."""
    rows = [
        {"type": "stop", "duration_s": 1.0, "end_speed_cm_s": math.nan},
        {"type": "stop", "duration_s": 1.0, "end_speed_cm_s": 50.0},
    ]
    scenario = rows_to_scenario(START, rows, 0)
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
    ],
)
def test_rows_to_scenario_rejects_blank_and_empty(rows: list[dict[str, Any]], text: str) -> None:
    """Blank, NaN, unknown and missing cells and an empty table raise a clear ValueError."""
    with pytest.raises(ValueError, match=text):
        rows_to_scenario(START, rows, 0)


def test_rows_to_scenario_rejects_blank_start() -> None:
    """A blank start value raises a clear ValueError."""
    rows = [{"type": "stop", "duration_s": 1.0, "end_speed_cm_s": math.nan}]
    with pytest.raises(ValueError, match="x_cm"):
        rows_to_scenario({**START, "x_cm": math.nan}, rows, 0)


def test_rows_to_scenario_accepts_numpy_numbers() -> None:
    """numpy floats and ints (what a DataFrame gives) are converted; pydantic is strict."""
    rows = [
        {"type": "move", "duration_s": np.float64(2.0), "end_speed_cm_s": np.float32(100.0)},
    ]
    start = {k: np.float64(v) for k, v in START.items()}
    scenario = rows_to_scenario(start, rows, np.int64(7))  # type: ignore[arg-type]
    assert scenario.seed == 7
    assert type(scenario.start.x_cm) is float
    assert type(scenario.segments[0].duration_s) is float


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
    """An invalid table is shown as st.error, with no plot and no write."""

    def bad(start: dict[str, float], rows: list[dict[str, Any]], seed: int) -> Scenario:
        raise ValueError("segment 0: end_speed_cm_s is blank")

    monkeypatch.setattr("vl_data.generator.ui.generate_tab.rows_to_scenario", bad)
    at = _run(_small(small_config, tmp_path))
    assert not at.exception
    assert "end_speed_cm_s is blank" in at.error[0].value
    assert len(at.get("plotly_chart")) == 0
    assert list(tmp_path.iterdir()) == []


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
