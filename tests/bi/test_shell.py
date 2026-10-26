"""AppTest tests of the BI shell and runner. Units: cm, cm/s, frames, rows, s."""

import shutil
from pathlib import Path

import pyarrow as pa
import pytest
import yaml
from streamlit.testing.v1 import AppTest

import vl_data.bi as bi_package
import vl_data.bi.analyze_tab as analyze_tab
import vl_data.bi.data as bi_data
from vl_data.bi.data import list_lake_drives
from vl_data.core.config import Config
from vl_data.core.lake import Lake
from vl_data.etl.config import EtlConfig
from vl_data.etl.ingest import ingest_drive
from vl_data.generator.models import Scenario
from vl_data.generator.writer import generate_to_disk
from vl_data.sdk.client import VLClient
from vl_data.usecase.config import MotionConfig

from .helpers import SCENARIO, BiDrive

APP_PATH = Path(bi_package.__file__).with_name("app.py")
TIMEOUT_S = 120
BADGE_CHARS = 12
TAB_LABELS = ["Generate", "Clean", "Analyze", "SQL"]


def shell_script(config, cfg, lake_dir):  # noqa: ANN001, ANN201
    from vl_data.bi.shell import render_app

    render_app(config, cfg, lake_dir)


def badge_script(scenario_hash, label):  # noqa: ANN001, ANN201
    from vl_data.bi.shell import hash_badge

    hash_badge(scenario_hash, label)


def _app(config: Config, cfg: MotionConfig, lake_dir: Path, raw_dir: Path) -> AppTest:
    config = config.model_copy(update={"raw_dir": raw_dir})
    return AppTest.from_function(
        shell_script, args=(config, cfg, lake_dir), default_timeout=TIMEOUT_S
    )


def _badges(at: AppTest, index: int) -> list[str]:
    return [m.value for m in at.tabs[index].markdown if "-badge[" in m.value]


def _captions(at: AppTest, index: int) -> list[str]:
    return [c.value for c in at.tabs[index].caption]


def test_four_tabs_and_badge_on_each(
    clean: BiDrive, bi_config: Config, motion_cfg: MotionConfig, tmp_path: Path
) -> None:
    """Tab labels are fixed; each tab shows the 12-hex badge and the full hash in a caption."""
    full = clean.client.lineage(clean.drive_id).to_pylist()[0]["scenario_hash"]
    at = _app(bi_config, motion_cfg, clean.root / "lake", tmp_path / "raw").run()
    assert not at.exception
    assert [t.label for t in at.tabs] == TAB_LABELS
    assert at.sidebar.selectbox(key="bi_drive").value == clean.drive_id
    for index in range(4):
        badges = _badges(at, index)
        assert len(badges) == 1
        assert full[:BADGE_CHARS] in badges[0]
        assert full[: BADGE_CHARS + 1] not in badges[0]
        assert f"scenario_hash: {full}" in _captions(at, index)
    assert "selected drive" in _badges(at, 0)[0]
    assert not at.error


def test_generate_tab_is_unchanged_and_rerun_skips_evaluation(
    clean: BiDrive,
    bi_config: Config,
    motion_cfg: MotionConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Generate form is there; a run and a Generate click never evaluate, Evaluate does."""
    evaluate_calls: list[str] = []
    analyze_calls: list[str] = []
    real_evaluate = bi_data.evaluate_drive
    real_analyze = analyze_tab.analyze_drive

    def evaluate_spy(*args: object, **kwargs: object) -> object:
        evaluate_calls.append("evaluate")
        return real_evaluate(*args, **kwargs)

    def analyze_spy(*args: object, **kwargs: object) -> object:
        analyze_calls.append("analyze")
        return real_analyze(*args, **kwargs)

    monkeypatch.setattr("vl_data.bi.data.evaluate_drive", evaluate_spy)
    monkeypatch.setattr("vl_data.bi.analyze_tab.analyze_drive", analyze_spy)
    raw = tmp_path / "raw"
    shutil.copytree(clean.drive_dir, raw / clean.drive_id)  # the selected drive is evaluable
    at = _app(bi_config, motion_cfg, clean.root / "lake", raw).run()
    assert not at.exception
    assert at.sidebar.selectbox(key="bi_drive").value == clean.drive_id
    assert evaluate_calls == []
    assert analyze_calls == []
    generate = at.tabs[0]
    assert generate.button[0].label == "Generate"
    assert len(generate.selectbox) + len(generate.number_input) + len(generate.text_input) > 0
    assert [p.name for p in raw.iterdir()] == [clean.drive_id]  # nothing written before the click
    generate.button[0].click()
    at.run()
    assert not at.exception
    assert not at.error
    assert evaluate_calls == []
    assert analyze_calls == []
    written = {p.name for p in raw.iterdir()}
    assert len(written) == 2  # the click wrote a different drive next to the selected one
    assert clean.drive_id in written
    # Positive control: the Evaluate button reaches evaluate_drive on this raw folder.
    at.button(key="analyze_evaluate").click()
    at.run()
    assert not at.exception
    assert evaluate_calls == ["evaluate"]
    assert analyze_calls == ["analyze"]


def _assert_hash_on_all_tabs(at: AppTest, full: str) -> None:
    for index in range(4):
        badges = _badges(at, index)
        assert len(badges) == 1
        assert full[:BADGE_CHARS] in badges[0]
        assert _captions(at, index).count(f"scenario_hash: {full}") == 1


def test_sidebar_lists_lake_drives_including_rejected(
    clean: BiDrive, bi_config: Config, motion_cfg: MotionConfig, tmp_path: Path
) -> None:
    """The selector offers every lineage drive; each drive's own hash shows on all four tabs."""
    lake_dir = tmp_path / "lake"
    shutil.copytree(clean.root / "lake", lake_dir)
    other = {**SCENARIO, "seed": 7}
    drive_dir = generate_to_disk(Scenario.model_validate(other), bi_config, tmp_path / "rawsrc")
    with (drive_dir / "frames.parquet").open("ab") as handle:
        handle.write(b"x")  # rejected on ingest
    result = ingest_drive(drive_dir, Lake(lake_dir), EtlConfig())
    assert result.status == "rejected"
    assert drive_dir.name != clean.drive_id
    expected = list_lake_drives(Lake(lake_dir))
    assert set(expected) == {clean.drive_id, drive_dir.name}
    private = VLClient(Lake(lake_dir))
    hash_clean = private.lineage(clean.drive_id).to_pylist()[0]["scenario_hash"]
    hash_rejected = private.lineage(drive_dir.name).to_pylist()[0]["scenario_hash"]
    assert hash_clean is not None
    assert hash_rejected is not None
    assert hash_clean != hash_rejected

    at = _app(bi_config, motion_cfg, lake_dir, tmp_path / "raw").run()
    assert not at.exception
    box = at.sidebar.selectbox(key="bi_drive")
    assert list(box.options) == expected
    selected = box.value
    first, second = (
        (hash_clean, hash_rejected) if selected == clean.drive_id else (hash_rejected, hash_clean)
    )
    other_drive = drive_dir.name if selected == clean.drive_id else clean.drive_id
    _assert_hash_on_all_tabs(at, first)
    box.select(other_drive)
    at.run()
    assert not at.exception
    assert at.sidebar.selectbox(key="bi_drive").value == other_drive
    assert [t.label for t in at.tabs] == TAB_LABELS
    _assert_hash_on_all_tabs(at, second)


def test_missing_lake_shows_info(
    bi_config: Config, motion_cfg: MotionConfig, tmp_path: Path
) -> None:
    """A missing lake folder gives hints on three tabs, Generate still draws, nothing created."""
    lake_dir = tmp_path / "no_such_lake"
    raw = tmp_path / "raw"
    at = _app(bi_config, motion_cfg, lake_dir, raw).run()
    assert not at.exception
    assert not at.error
    assert at.sidebar.selectbox(key="bi_drive").value is None
    assert [t.label for t in at.tabs] == TAB_LABELS
    assert at.tabs[0].button[0].label == "Generate"
    assert _badges(at, 0) == []
    for index in (1, 2, 3):
        assert len(at.tabs[index].info) == 1
        assert "lake" in at.tabs[index].info[0].value
        assert _badges(at, index) == []
    assert not lake_dir.exists()
    assert not raw.exists()


def test_empty_lake_shows_info(bi_config: Config, motion_cfg: MotionConfig, tmp_path: Path) -> None:
    """An existing empty lake folder gives the same hints and stays empty."""
    lake_dir = tmp_path / "lake"
    lake_dir.mkdir()
    at = _app(bi_config, motion_cfg, lake_dir, tmp_path / "raw").run()
    assert not at.exception
    assert len(at.info) == 3
    assert list(lake_dir.iterdir()) == []


@pytest.mark.parametrize(
    "error", [ValueError("fixed message"), OSError("C:/secret/place"), pa.ArrowInvalid("C:/x")]
)
def test_lineage_read_error_shows_one_error_and_none_badge(
    clean: BiDrive,
    bi_config: Config,
    motion_cfg: MotionConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    """A failing lineage read shows one safe error (from the Clean tab), a none badge everywhere."""

    def boom(*args: object, **kwargs: object) -> object:
        raise error

    monkeypatch.setattr("vl_data.sdk.client.VLClient.lineage", boom)
    at = _app(bi_config, motion_cfg, clean.root / "lake", tmp_path / "raw").run()
    assert not at.exception
    shown = [e.value for e in at.error]
    assert len(shown) == 1
    assert len(at.tabs[1].error) == 1  # the Clean tab reports it; the shell stays silent
    assert "secret" not in shown[0]
    assert "C:/x" not in shown[0]
    for index in range(4):
        assert "none" in _badges(at, index)[0]


def test_two_lineage_rows_give_none_badge_without_shell_error(
    clean: BiDrive,
    bi_config: Config,
    motion_cfg: MotionConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lineage table with two rows is not a hash: the none badge, and no error from the shell."""
    real = VLClient.lineage

    def doubled(self: VLClient, drive: str) -> pa.Table:
        table = real(self, drive)
        return pa.concat_tables([table, table])

    monkeypatch.setattr("vl_data.sdk.client.VLClient.lineage", doubled)
    at = _app(bi_config, motion_cfg, clean.root / "lake", tmp_path / "raw").run()
    assert not at.exception
    assert len(at.error) == 1  # one error, from the Clean tab; the shell adds none
    assert len(at.tabs[1].error) == 1
    for index in range(4):
        assert "none" in _badges(at, index)[0]


@pytest.mark.parametrize("error", [OSError("C:/secret"), pa.ArrowInvalid("C:/secret")])
def test_unreadable_lake_folder_shows_one_safe_error(
    clean: BiDrive,
    bi_config: Config,
    motion_cfg: MotionConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    """A lake folder that cannot be listed gives one path-free error; Generate still draws."""

    def boom(*args: object, **kwargs: object) -> object:
        raise error

    monkeypatch.setattr("vl_data.bi.shell.list_lake_drives", boom)
    at = _app(bi_config, motion_cfg, clean.root / "lake", tmp_path / "raw").run()
    assert not at.exception
    assert len(at.error) == 1
    assert at.sidebar.selectbox(key="bi_drive").value is None
    texts = [e.value for e in at.error] + [i.value for i in at.info]
    assert texts
    assert all("secret" not in text for text in texts)
    assert [t.label for t in at.tabs] == TAB_LABELS
    assert at.tabs[0].button[0].label == "Generate"


def test_hash_badge_without_hash() -> None:
    """None gives a gray none badge and a short caption."""
    at = AppTest.from_function(badge_script, args=(None, "scenario hash")).run()
    assert not at.exception
    badges = [m.value for m in at.markdown if "-badge[" in m.value]
    assert badges == [":gray-badge[scenario hash: none]"]
    assert len(at.caption) == 1


def test_hash_badge_with_hash() -> None:
    """A hash gives a blue badge with 12 hex and the full 64 hex in the caption."""
    full = "ab" * 32
    at = AppTest.from_function(badge_script, args=(full, "x")).run()
    badges = [m.value for m in at.markdown if "-badge[" in m.value]
    assert badges == [f":blue-badge[x: {full[:12]}]"]
    assert at.caption[0].value == f"scenario_hash: {full}"


def _boom(error: Exception):  # noqa: ANN202
    def raiser() -> object:
        raise error

    return raiser


def test_runner_script_in_process_for_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run app.py as a plain script under its module name so coverage sees its lines.

    It stays before the AppTest run of the same file (which would make coverage skip it).
    Covers the load branch and the failure branch. Nothing is created on disk.
    """
    lake = tmp_path / "lake"
    lake.mkdir()
    monkeypatch.setenv("VL_LAKE_DIR", str(lake))
    monkeypatch.setenv("VL_RAW_DIR", str(tmp_path / "raw"))
    source = compile(APP_PATH.read_text(encoding="utf-8"), str(APP_PATH), "exec")
    exec(source, {"__name__": "vl_data.bi.app"})
    monkeypatch.setattr("vl_data.core.config.load_config", _boom(OSError("C:/secret")))
    exec(source, {"__name__": "vl_data.bi.app"})
    assert list(lake.iterdir()) == []
    assert not (tmp_path / "raw").exists()


def test_runner_script_runs_with_default_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """app.py renders four tabs with the default config and an empty lake; no file is written."""
    lake = tmp_path / "lake"
    lake.mkdir()
    raw = tmp_path / "raw"
    monkeypatch.setenv("VL_LAKE_DIR", str(lake))
    monkeypatch.setenv("VL_RAW_DIR", str(raw))
    at = AppTest.from_file(str(APP_PATH), default_timeout=TIMEOUT_S).run()
    assert not at.exception
    assert not at.error
    assert [t.label for t in at.tabs] == TAB_LABELS
    assert [t.value for t in at.title] == ["VL data product"]
    assert list(lake.iterdir()) == []
    assert not raw.exists()


@pytest.mark.parametrize("kind", ["os", "value", "yaml", "yaml_file"])
def test_runner_script_broken_config_shows_one_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """A config that cannot load gives one path-free error and no tabs."""
    secret = str(tmp_path / "secret_dir")
    errors = {
        "os": OSError(secret),
        "value": ValueError(secret),
        "yaml": yaml.YAMLError(secret),
    }
    if kind == "yaml_file":
        bad = tmp_path / "secret_dir.yaml"
        bad.write_text("a: [unclosed\n", encoding="utf-8")
        monkeypatch.setattr("vl_data.core.config.DEFAULT_CONFIG_PATH", bad)
    else:
        monkeypatch.setattr("vl_data.core.config.load_config", _boom(errors[kind]))
    at = AppTest.from_file(str(APP_PATH), default_timeout=TIMEOUT_S).run()
    assert not at.exception
    assert len(at.error) == 1
    assert "secret_dir" not in at.error[0].value
    assert len(at.tabs) == 0
