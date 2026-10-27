"""Tests of the ingest panel: raw folder scan, run_ingest and the panel inside the app shell.

Units: frames and rows are counts; times are seconds (file modification times).
"""

import os
import shutil
from pathlib import Path

import pyarrow as pa
import pytest
import yaml
from streamlit.testing.v1 import AppTest

import vl_data.bi.ingest_panel as panel_module
from vl_data.bi.ingest_panel import pending_raw_drives, run_ingest
from vl_data.core.config import Config
from vl_data.core.lake import Lake
from vl_data.etl.ingest import IngestResult
from vl_data.generator.models import Scenario
from vl_data.generator.writer import generate_to_disk

from .helpers import SCENARIO

TIMEOUT_S = 120
HEX_A = "a" * 12
HEX_B = "b" * 12
HEX_C = "c" * 12
BUTTON = "Ingest this drive"


def _folder(raw: Path, name: str, mtime: float | None = None) -> Path:
    path = raw / name
    path.mkdir(parents=True)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


# ---------------------------------------------------------------- pending_raw_drives


def test_missing_raw_folder_is_not_an_error(tmp_path: Path) -> None:
    found = pending_raw_drives(tmp_path / "nope", [])
    assert found.missing
    assert found.problem is None
    assert found.pending == [] and found.ingested == []


def test_empty_raw_folder(tmp_path: Path) -> None:
    found = pending_raw_drives(tmp_path, [])
    assert not found.missing
    assert found.problem is None
    assert found.pending == [] and found.ingested == []


def test_raw_path_is_a_file_gives_problem_without_path(tmp_path: Path) -> None:
    target = tmp_path / "secret_file"
    target.write_text("x", encoding="utf-8")
    found = pending_raw_drives(target, [])
    assert found.problem is not None
    assert "secret_file" not in found.problem
    assert found.pending == []


def test_unreadable_raw_folder_gives_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(self: Path) -> object:
        raise PermissionError(str(tmp_path / "secret"))

    monkeypatch.setattr(Path, "iterdir", boom)
    found = pending_raw_drives(tmp_path, [])
    assert found.problem is not None
    assert "secret" not in found.problem


def test_only_twelve_lowercase_hex_folders_count(tmp_path: Path) -> None:
    _folder(tmp_path, HEX_A)
    for name in ["abc", "d" * 11, "g" * 12, "a" * 13, ".tmp-" + HEX_B, ".old-" + HEX_B, "zz"]:
        _folder(tmp_path, name)
    _folder(tmp_path, "F" * 12)  # upper case is not a drive folder (Windows ignores case, so no A)
    (tmp_path / HEX_C).write_text("a file, not a folder", encoding="utf-8")
    assert pending_raw_drives(tmp_path, []).pending == [HEX_A]


def test_symlink_is_skipped(tmp_path: Path) -> None:
    target = _folder(tmp_path, "somewhere")
    try:
        os.symlink(target, tmp_path / HEX_A, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("no privilege to create a symlink")
    assert pending_raw_drives(tmp_path, []).pending == []


def test_junction_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _folder(tmp_path, HEX_A)
    _folder(tmp_path, HEX_B)
    real = getattr(Path, "is_junction", lambda self: False)
    monkeypatch.setattr(
        Path, "is_junction", lambda self: self.name == HEX_A or real(self), raising=False
    )
    assert pending_raw_drives(tmp_path, []).pending == [HEX_B]


def test_entry_that_cannot_be_examined_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _folder(tmp_path, HEX_A)
    _folder(tmp_path, HEX_B)
    real = Path.is_symlink

    def flaky(self: Path) -> bool:
        if self.name == HEX_A:
            raise OSError("denied")
        return real(self)

    monkeypatch.setattr(Path, "is_symlink", flaky)
    assert pending_raw_drives(tmp_path, []).pending == [HEX_B]


def test_stat_failure_skips_the_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _folder(tmp_path, HEX_A)
    _folder(tmp_path, HEX_B)
    real = Path.stat

    def flaky(self: Path, **kwargs: object) -> os.stat_result:
        if self.name == HEX_A:
            raise OSError("denied")
        return real(self, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "stat", flaky)
    assert pending_raw_drives(tmp_path, []).pending == [HEX_B]


def test_ingested_drive_goes_to_its_own_group(tmp_path: Path) -> None:
    _folder(tmp_path, HEX_A)
    _folder(tmp_path, HEX_B)
    found = pending_raw_drives(tmp_path, [HEX_A, "not_on_disk"])
    assert found.pending == [HEX_B]
    assert found.ingested == [HEX_A]


def test_pending_is_newest_folder_first(tmp_path: Path) -> None:
    _folder(tmp_path, HEX_A, mtime=1_000_000)
    _folder(tmp_path, HEX_B, mtime=3_000_000)
    _folder(tmp_path, HEX_C, mtime=2_000_000)
    assert pending_raw_drives(tmp_path, []).pending == [HEX_B, HEX_C, HEX_A]


def test_ingested_drive_with_newest_folder_is_not_pending_and_older_one_is(tmp_path: Path) -> None:
    """Re-generating an ingested drive touches its folder; it still is not a pending drive."""
    _folder(tmp_path, HEX_A, mtime=9_000_000)  # ingested, newest folder
    _folder(tmp_path, HEX_B, mtime=1_000_000)  # pending, older
    found = pending_raw_drives(tmp_path, [HEX_A])
    assert found.pending == [HEX_B]
    assert found.ingested == [HEX_A]


# ---------------------------------------------------------------- run_ingest


@pytest.fixture(scope="module")
def small_drive(tmp_path_factory: pytest.TempPathFactory, bi_config: Config) -> Path:
    root = tmp_path_factory.mktemp("panel_src")
    return generate_to_disk(Scenario.model_validate(SCENARIO), bi_config, root)


def _raw_with(small_drive: Path, tmp_path: Path, *, corrupt: bool = False) -> Path:
    raw = tmp_path / "raw"
    shutil.copytree(small_drive, raw / small_drive.name)
    if corrupt:
        with (raw / small_drive.name / "frames.parquet").open("ab") as handle:
            handle.write(b"x")
    return raw


def test_run_ingest_good_drive(small_drive: Path, tmp_path: Path) -> None:
    raw = _raw_with(small_drive, tmp_path)
    lake_dir = tmp_path / "lake"
    result = run_ingest(raw, small_drive.name, lake_dir)
    assert isinstance(result, IngestResult)
    assert result.status == "ok"
    assert result.frames_kept > 0
    assert Lake(lake_dir).read("lineage", small_drive.name).num_rows == 1


def test_run_ingest_corrupted_frames_is_a_rejected_result(
    small_drive: Path, tmp_path: Path
) -> None:
    raw = _raw_with(small_drive, tmp_path, corrupt=True)
    result = run_ingest(raw, small_drive.name, tmp_path / "lake")
    assert isinstance(result, IngestResult)
    assert result.status == "rejected"


def test_run_ingest_bad_drive_id_gives_problem_without_path(tmp_path: Path) -> None:
    raw = tmp_path / "raw_secret"
    (raw / "bad.name").mkdir(parents=True)
    result = run_ingest(raw, "bad.name", tmp_path / "lake_secret")
    assert isinstance(result, str)
    assert "secret" not in result
    assert "bad.name" not in result


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            ValueError("C:/secret ValidationError value=42"),
            "The drive id or the ETL settings are not valid.",
        ),
        (OSError("C:/secret"), "Cannot read or write a file (OSError)."),
        (yaml.YAMLError("C:/secret"), "The ETL config file is not valid YAML."),
        (pa.ArrowInvalid("C:/secret"), "Cannot read a table (ArrowInvalid)."),
    ],
)
def test_run_ingest_config_failure_gives_safe_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception, expected: str
) -> None:
    def boom(*args: object, **kwargs: object) -> object:
        raise error

    monkeypatch.setattr("vl_data.bi.ingest_panel.load_etl_config", boom)
    _folder(tmp_path, HEX_A)
    result = run_ingest(tmp_path, HEX_A, tmp_path / "lake")
    assert result == expected
    assert "secret" not in result
    assert "42" not in result


def test_run_ingest_write_failure_gives_safe_problem(
    small_drive: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: object, **kwargs: object) -> object:
        raise OSError("C:/secret")

    monkeypatch.setattr("vl_data.bi.ingest_panel.ingest_drive", boom)
    result = run_ingest(_raw_with(small_drive, tmp_path), small_drive.name, tmp_path / "lake")
    assert isinstance(result, str)
    assert "secret" not in result


# ---------------------------------------------------------------- the panel in the app


def shell_script(config, lake_dir):  # noqa: ANN001, ANN201
    from vl_data.bi.shell import render_app

    render_app(config, lake_dir)


def panel_script(config, lake_dir, drives, prefix):  # noqa: ANN001, ANN201
    from vl_data.bi.ingest_panel import render_ingest_panel

    render_ingest_panel(config, lake_dir, drives, prefix)


def _app(config: Config, lake_dir: Path, raw_dir: Path) -> AppTest:
    config = config.model_copy(update={"raw_dir": raw_dir})
    return AppTest.from_function(shell_script, args=(config, lake_dir), default_timeout=TIMEOUT_S)


def _panel(config: Config, raw_dir: Path, lake_dir: Path, drives: list[str]) -> AppTest:
    config = config.model_copy(update={"raw_dir": raw_dir})
    args = (config, lake_dir, drives, "p")
    return AppTest.from_function(panel_script, args=args, default_timeout=TIMEOUT_S)


def _ingest_buttons(tab) -> list:  # noqa: ANN001
    return [b for b in tab.button if b.label == BUTTON]


def test_empty_lake_with_pending_drive_shows_panel_in_clean_and_generate(
    small_drive: Path, bi_config: Config, tmp_path: Path
) -> None:
    raw = _raw_with(small_drive, tmp_path)
    lake_dir = tmp_path / "lake"
    lake_dir.mkdir()
    at = _app(bi_config, lake_dir, raw).run()
    assert not at.exception
    assert not at.error
    assert len(_ingest_buttons(at.tabs[0])) == 1
    assert len(_ingest_buttons(at.tabs[1])) == 1
    assert _ingest_buttons(at.tabs[2]) == []
    assert _ingest_buttons(at.tabs[3]) == []
    for index in (2, 3):  # Analyze and SQL keep the hint
        assert len(at.tabs[index].info) == 1
        assert "Ingest this drive" in at.tabs[index].info[0].value
        assert "python -m vl_data ingest" in at.tabs[index].info[0].value
    assert at.tabs[1].selectbox(key="clean_ingest_drive").value == small_drive.name
    assert at.tabs[0].selectbox(key="generate_ingest_drive").value == small_drive.name


def test_click_ingests_and_drive_appears_in_sidebar(
    small_drive: Path, bi_config: Config, tmp_path: Path
) -> None:
    raw = _raw_with(small_drive, tmp_path)
    lake_dir = tmp_path / "lake"
    at = _app(bi_config, lake_dir, raw).run()
    _ingest_buttons(at.tabs[1])[0].click()
    at.run()
    assert not at.exception
    assert not at.tabs[0].error  # Generate and Clean report no error; Analyze may say 'no motion'
    assert not at.tabs[1].error
    assert at.sidebar.selectbox(key="bi_drive").value == small_drive.name
    lineage = Lake(lake_dir).read("lineage", small_drive.name)
    assert lineage.num_rows == 1
    assert lineage.column("status").to_pylist() == ["ok"]
    shown = [s.value for s in at.tabs[1].success]  # the result line, drawn once
    assert len(shown) == 1
    assert shown[0].startswith(f"Drive {small_drive.name}: status ok, ")
    metrics = {m.label: m.value for m in at.tabs[1].metric}
    assert int(metrics["kept (frames)"]) > 0
    # the drive is ingested now: the selector offers it as a re-ingest
    box = at.tabs[1].selectbox(key="clean_ingest_drive")
    assert list(box.options) == [f"{small_drive.name} (re-ingest)"]


def test_second_click_is_safe(small_drive: Path, bi_config: Config, tmp_path: Path) -> None:
    raw = _raw_with(small_drive, tmp_path)
    lake_dir = tmp_path / "lake"
    at = _app(bi_config, lake_dir, raw).run()
    _ingest_buttons(at.tabs[1])[0].click()
    at.run()
    _ingest_buttons(at.tabs[1])[0].click()
    at.run()
    assert not at.exception
    assert not at.tabs[0].error  # Generate and Clean report no error; Analyze may say 'no motion'
    assert not at.tabs[1].error
    assert Lake(lake_dir).read("lineage").num_rows == 1


def test_rejected_drive_is_a_result_not_an_error(
    small_drive: Path, bi_config: Config, tmp_path: Path
) -> None:
    raw = _raw_with(small_drive, tmp_path, corrupt=True)
    lake_dir = tmp_path / "lake"
    at = _app(bi_config, lake_dir, raw).run()
    _ingest_buttons(at.tabs[1])[0].click()
    at.run()
    assert not at.exception
    assert not at.tabs[0].error  # Generate and Clean report no error; Analyze may say 'no motion'
    assert not at.tabs[1].error
    warnings = [w.value for w in at.tabs[1].warning]
    assert len(warnings) == 1
    assert "rejected" in warnings[0]
    assert "Clean" in warnings[0]
    assert at.sidebar.selectbox(key="bi_drive").value == small_drive.name


def test_no_raw_folder_shows_info_and_no_selector(bi_config: Config, tmp_path: Path) -> None:
    at = _app(bi_config, tmp_path / "lake", tmp_path / "no_raw").run()
    assert not at.exception
    assert not at.error
    for tab in (at.tabs[0], at.tabs[1]):
        assert _ingest_buttons(tab) == []
        assert any("raw" in i.value.lower() for i in tab.info)
    assert not (tmp_path / "no_raw").exists()


def test_panel_raw_folder_without_drives_shows_info(bi_config: Config, tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    at = _panel(bi_config, raw, tmp_path / "lake", []).run()
    assert not at.exception
    assert len(at.info) == 1
    assert at.button == []


def test_panel_unreadable_raw_folder_shows_safe_error(bi_config: Config, tmp_path: Path) -> None:
    target = tmp_path / "secret_file"
    target.write_text("x", encoding="utf-8")
    at = _panel(bi_config, target, tmp_path / "lake", []).run()
    assert not at.exception
    assert len(at.error) == 1
    assert "secret" not in at.error[0].value


def test_panel_problem_text_is_an_error_and_status_note_mentions_minutes(
    small_drive: Path, bi_config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("vl_data.bi.ingest_panel.run_ingest", lambda *a, **k: "fixed problem text")
    raw = _raw_with(small_drive, tmp_path)
    at = _panel(bi_config, raw, tmp_path / "lake", []).run()
    assert "minutes" in " ".join(c.value for c in at.caption)
    at.button[0].click()
    at.run()
    assert not at.exception
    assert [e.value for e in at.error] == ["fixed problem text"]


def test_panel_pre_selects_newest_pending_and_lists_ingested_last(
    bi_config: Config, tmp_path: Path
) -> None:
    raw = tmp_path / "raw"
    _folder(raw, HEX_A, mtime=1_000_000)
    _folder(raw, HEX_B, mtime=3_000_000)
    _folder(raw, HEX_C, mtime=9_000_000)
    at = _panel(bi_config, raw, tmp_path / "lake", [HEX_C]).run()
    box = at.selectbox(key="p_ingest_drive")
    assert list(box.options) == [HEX_B, HEX_A, f"{HEX_C} (re-ingest)"]
    assert box.value == HEX_B


def test_two_panels_use_distinct_widget_keys(
    small_drive: Path, bi_config: Config, tmp_path: Path
) -> None:
    raw = _raw_with(small_drive, tmp_path)
    at = _app(bi_config, tmp_path / "lake", raw).run()
    keys = [b.key for b in at.button if b.label == BUTTON]
    assert sorted(keys) == ["clean_ingest_button", "generate_ingest_button"]


def test_ingest_selects_the_new_drive_among_existing_ones(
    small_drive: Path, bi_config: Config, tmp_path: Path
) -> None:
    """With another drive already in the lake, the ingested drive becomes the selected one."""
    other = {**SCENARIO, "seed": 3}
    first = generate_to_disk(Scenario.model_validate(other), bi_config, tmp_path / "raw")
    assert isinstance(run_ingest(tmp_path / "raw", first.name, tmp_path / "lake"), IngestResult)
    shutil.copytree(small_drive, tmp_path / "raw" / small_drive.name)
    at = _app(bi_config, tmp_path / "lake", tmp_path / "raw").run()
    assert at.sidebar.selectbox(key="bi_drive").value == first.name
    box = at.tabs[1].selectbox(key="clean_ingest_drive")
    assert box.value == small_drive.name  # the only pending drive comes first
    _ingest_buttons(at.tabs[1])[0].click()
    at.run()
    assert not at.exception
    assert at.sidebar.selectbox(key="bi_drive").value == small_drive.name


def test_stale_selection_is_replaced_by_the_first_drive(
    small_drive: Path, bi_config: Config, tmp_path: Path
) -> None:
    """A selector state that names no lake drive falls back to the first drive."""
    raw = _raw_with(small_drive, tmp_path)
    assert isinstance(run_ingest(raw, small_drive.name, tmp_path / "lake"), IngestResult)
    at = _app(bi_config, tmp_path / "lake", raw)
    at.session_state["bi_drive"] = "gone"
    at.run()
    assert not at.exception
    assert at.sidebar.selectbox(key="bi_drive").value == small_drive.name


# ---------------------------------------------------------------- repair pass


def _boom(error: Exception):  # noqa: ANN202
    def raiser(*args: object, **kwargs: object) -> object:
        raise error

    return raiser


def test_run_ingest_rechecks_name_and_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[str] = []
    monkeypatch.setattr("vl_data.bi.ingest_panel.ingest_drive", lambda *a, **k: called.append("x"))
    _folder(tmp_path, HEX_A)
    (tmp_path / HEX_B).write_text("a file", encoding="utf-8")
    for name in ["bad.name", "A" * 12, HEX_B, HEX_C]:  # bad name, upper case, file, missing
        assert (
            run_ingest(tmp_path, name, tmp_path / "lake") == "That drive folder is not available."
        )
    monkeypatch.setattr(Path, "is_junction", lambda self: self.name == HEX_A, raising=False)
    assert run_ingest(tmp_path, HEX_A, tmp_path / "lake") == "That drive folder is not available."
    assert called == []


def test_run_ingest_unexaminable_folder_is_not_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _folder(tmp_path, HEX_A)
    monkeypatch.setattr(Path, "is_symlink", _boom(OSError("C:/secret")))
    assert run_ingest(tmp_path, HEX_A, tmp_path / "lake") == "That drive folder is not available."


def test_run_ingest_unexpected_error_gives_fixed_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _folder(tmp_path, HEX_A)
    monkeypatch.setattr("vl_data.bi.ingest_panel.ingest_drive", _boom(RuntimeError("C:/secret")))
    assert run_ingest(tmp_path, HEX_A, tmp_path / "lake") == "Ingest failed unexpectedly."
    assert not panel_module._LOCK.locked()


def test_run_ingest_value_error_from_ingest_drive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _folder(tmp_path, HEX_A)
    monkeypatch.setattr("vl_data.bi.ingest_panel.ingest_drive", _boom(ValueError("C:/secret")))
    result = run_ingest(tmp_path, HEX_A, tmp_path / "lake")
    assert isinstance(result, str)
    assert "secret" not in result
    assert not panel_module._LOCK.locked()


def test_busy_lock_blocks_ingest_and_is_not_released_by_the_loser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[str] = []
    monkeypatch.setattr("vl_data.bi.ingest_panel.ingest_drive", lambda *a, **k: called.append("x"))
    _folder(tmp_path, HEX_A)
    assert panel_module._LOCK.acquire(blocking=False)
    try:
        result = run_ingest(tmp_path, HEX_A, tmp_path / "lake")
        assert result == "Another ingest is running. Try again when it has finished."
        assert called == []
        assert panel_module._LOCK.locked()  # still held by the first ingest
    finally:
        panel_module._LOCK.release()


def test_lock_is_released_after_success_and_after_rejection(
    small_drive: Path, tmp_path: Path
) -> None:
    raw = _raw_with(small_drive, tmp_path)
    assert isinstance(run_ingest(raw, small_drive.name, tmp_path / "lake"), IngestResult)
    assert not panel_module._LOCK.locked()
    with (raw / small_drive.name / "frames.parquet").open("ab") as handle:
        handle.write(b"x")
    result = run_ingest(raw, small_drive.name, tmp_path / "lake")
    assert isinstance(result, IngestResult)
    assert result.status == "rejected"
    assert not panel_module._LOCK.locked()


def test_selected_second_drive_is_kept_across_reruns(bi_config: Config, tmp_path: Path) -> None:
    """The shell keeps a valid selection other than the first drive, also after another rerun."""
    lake = Lake(tmp_path / "lake")
    for seed in (1, 2):
        scenario = Scenario.model_validate({**SCENARIO, "seed": seed})
        folder = generate_to_disk(scenario, bi_config, tmp_path / "src")
        assert isinstance(run_ingest(tmp_path / "src", folder.name, lake.root), IngestResult)
    drives = lake.list_drives("lineage")
    assert len(drives) == 2
    at = _app(bi_config, lake.root, tmp_path / "raw")
    at.session_state["bi_drive"] = drives[1]
    at.run()
    assert at.sidebar.selectbox(key="bi_drive").value == drives[1]
    at.tabs[3].selectbox[0].select_index(1)  # a different widget (SQL example), not an ingest
    at.run()
    assert not at.exception
    assert at.sidebar.selectbox(key="bi_drive").value == drives[1]


def test_selector_state_for_a_removed_folder_is_dropped(
    small_drive: Path, bi_config: Config, tmp_path: Path
) -> None:
    raw = tmp_path / "raw"
    _folder(raw, HEX_A, mtime=2_000_000)
    _folder(raw, HEX_B, mtime=1_000_000)
    at = _panel(bi_config, raw, tmp_path / "lake", []).run()
    at.selectbox(key="p_ingest_drive").select(HEX_B)
    at.run()
    assert at.selectbox(key="p_ingest_drive").value == HEX_B
    shutil.rmtree(raw / HEX_B)
    at.run()
    assert not at.exception
    assert at.selectbox(key="p_ingest_drive").value == HEX_A


def test_unreadable_lake_listing_hides_the_ingest_panel(
    small_drive: Path, bi_config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("vl_data.bi.shell.list_lake_drives", _boom(OSError("C:/secret")))
    raw = _raw_with(small_drive, tmp_path)
    at = _app(bi_config, tmp_path / "lake", raw).run()
    assert not at.exception
    assert len(at.error) == 1
    for index in range(4):
        assert _ingest_buttons(at.tabs[index]) == []
    assert not (tmp_path / "lake").exists()


def test_panel_busy_text_is_drawn_as_an_error(
    small_drive: Path, bi_config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("vl_data.bi.ingest_panel.run_ingest", lambda *a, **k: panel_module._BUSY)
    at = _panel(bi_config, _raw_with(small_drive, tmp_path), tmp_path / "lake", []).run()
    at.button[0].click()
    at.run()
    assert [e.value for e in at.error] == [
        "Another ingest is running. Try again when it has finished."
    ]
