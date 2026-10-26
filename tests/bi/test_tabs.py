"""AppTest tests of the Clean and Analyze tabs. Units: cm, cm/s, cm/s^2, frames, rows, s."""

import json
from pathlib import Path

import pyarrow as pa
import pytest
from streamlit.testing.v1 import AppTest

from vl_data.bi import data
from vl_data.bi.data import (
    QUARANTINE_ROWS_SHOWN,
    AnalyzeOutcome,
    ingest_command,
    load_quality,
    motion_command,
)
from vl_data.core.config import Config
from vl_data.sdk.client import VLClient
from vl_data.usecase.config import MotionConfig, MotionTolerances
from vl_data.usecase.evaluate import evaluate_drive

from .helpers import DROPPED_FRAME, BiDrive

UNKNOWN_DRIVE = "0" * 12  # a valid id with no rows in any lake here


def clean_script(client, drive, raw_dir, lake_dir):  # noqa: ANN001, ANN201
    from vl_data.bi.clean_tab import render_clean_tab

    render_clean_tab(client, drive, raw_dir, lake_dir)


def analyze_script(client, config, cfg, lake_dir, drive):  # noqa: ANN001, ANN201
    from vl_data.bi.analyze_tab import render_analyze_tab

    render_analyze_tab(client, config, cfg, lake_dir, drive)


def _lake_dir(d: BiDrive) -> Path:
    return d.root / "lake"


def _clean_app(client: VLClient, drive: str, d: BiDrive) -> AppTest:
    args = (client, drive, d.drive_dir.parent, _lake_dir(d))
    return AppTest.from_function(clean_script, args=args, default_timeout=30)


def _analyze_app(
    d: BiDrive, config: Config, cfg: MotionConfig, drive: str | None = None
) -> AppTest:
    cfg_raw = config.model_copy(update={"raw_dir": d.drive_dir.parent})
    args = (d.client, cfg_raw, cfg, _lake_dir(d), drive or d.drive_id)
    return AppTest.from_function(analyze_script, args=args, default_timeout=30)


def _chart_keys(at: AppTest) -> list[str]:
    return [element.key for element in at.get("plotly_chart")]


def _text(elements: object) -> str:
    return " ".join(str(e.value) for e in elements)  # type: ignore[attr-defined]


@pytest.fixture
def eval_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every call of evaluate_drive made through the data layer."""
    calls: list[str] = []

    def spy(*args: object, **kwargs: object) -> object:
        calls.append("called")
        return evaluate_drive(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("vl_data.bi.data.evaluate_drive", spy)
    return calls


class StubClient:
    """Client double: fixed tables, or an error raised on a chosen call."""

    def __init__(
        self,
        lineage: pa.Table,
        quarantine: pa.Table,
        motion: pa.Table | None = None,
        fail_quarantine_on_call: int | None = None,
        error: Exception | None = None,
    ) -> None:
        self._lineage = lineage
        self._quarantine = quarantine
        self._motion = motion
        self._fail_on = fail_quarantine_on_call
        self._error = error
        self._quarantine_calls = 0

    def lineage(self, drive: str) -> pa.Table:
        if self._error is not None and self._fail_on is None:
            raise self._error
        return self._lineage

    def quarantine(self, drive: str) -> pa.Table:
        self._quarantine_calls += 1
        if self._error is not None and self._fail_on == self._quarantine_calls:
            raise self._error
        return self._quarantine

    def motion(self, drive: str) -> pa.Table:
        if self._error is not None and self._motion is None:
            raise self._error
        assert self._motion is not None
        return self._motion


# --- Clean tab -------------------------------------------------------------------------------


def test_clean_tab_shows_counts_chart_and_table(dropped: BiDrive) -> None:
    quality = load_quality(dropped.client, dropped.drive_id)
    assert quality is not None
    at = _clean_app(dropped.client, dropped.drive_id, dropped).run()
    assert not at.exception
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["expected (frames)"] == str(quality.frames_expected)
    assert metrics["in (frames)"] == str(quality.frames_in)
    assert metrics["kept (frames)"] == str(quality.frames_kept)
    assert metrics["rejected (frames)"] == str(quality.frames_rejected)
    assert metrics["missing (frames)"] == str(quality.frames_missing) == "1"
    assert metrics["keypoint rows (rows)"] == str(quality.keypoint_rows)
    assert _chart_keys(at) == ["clean_quality", "clean_codes"]
    table = at.dataframe[0].value
    expected = dropped.client.quarantine(dropped.drive_id)
    assert len(table) == expected.num_rows
    assert "VL-001" in set(table["code"])
    assert f"showing {expected.num_rows} of {expected.num_rows} quarantine rows" in _text(
        at.caption
    )
    assert DROPPED_FRAME == 5  # the dropped frame is the one VL-001 reports


def test_clean_tab_shows_rejected_drive(rejected: BiDrive) -> None:
    at = _clean_app(rejected.client, rejected.drive_id, rejected).run()
    assert not at.exception
    assert not at.error
    assert len(at.dataframe) == 1
    assert _chart_keys(at) == ["clean_quality", "clean_codes"]


def test_clean_tab_no_lineage_shows_ingest_command(clean: BiDrive) -> None:
    at = _clean_app(clean.client, UNKNOWN_DRIVE, clean).run()
    assert not at.exception
    assert len(at.info) == 1
    command = ingest_command(clean.drive_dir.parent, UNKNOWN_DRIVE, _lake_dir(clean))
    assert [c.value for c in at.code] == [command]
    assert at.code[0].language == "powershell"
    assert not at.get("plotly_chart")
    assert not at.metric


def test_clean_tab_caps_quarantine_table(dropped: BiDrive) -> None:
    one = dropped.client.quarantine(dropped.drive_id)
    big = one.take([0] * 250)
    lineage = dropped.client.lineage(dropped.drive_id)
    stub = StubClient(lineage, big)
    at = _clean_app(stub, dropped.drive_id, dropped).run()  # type: ignore[arg-type]
    assert not at.exception
    assert len(at.dataframe[0].value) == QUARANTINE_ROWS_SHOWN == 200
    assert "showing 200 of 250 quarantine rows" in _text(at.caption)


def test_clean_tab_errors_are_messages(dropped: BiDrive) -> None:
    bad = _clean_app(dropped.client, "../x", dropped).run()
    assert not bad.exception
    assert len(bad.error) == 1
    assert "invalid drive id" in str(bad.error[0].value)
    lineage = dropped.client.lineage(dropped.drive_id)
    quarantine = dropped.client.quarantine(dropped.drive_id)
    for error in (OSError("disk"), pa.ArrowInvalid("bad file")):
        read_fails = StubClient(lineage, quarantine, error=error)
        at = _clean_app(read_fails, dropped.drive_id, dropped).run()  # type: ignore[arg-type]
        assert not at.exception
        assert len(at.error) == 1
        # second quarantine read (the table) fails after load_quality succeeded
        table_fails = StubClient(lineage, quarantine, fail_quarantine_on_call=2, error=error)
        at = _clean_app(table_fails, dropped.drive_id, dropped).run()  # type: ignore[arg-type]
        assert not at.exception
        assert len(at.error) == 1
        assert not at.dataframe


# --- Analyze tab -----------------------------------------------------------------------------


MOTION_KEYS = ["motion_position", "motion_velocity", "motion_acceleration"]
EVAL_KEYS = [
    "eval_timeline_velocity",
    "eval_timeline_acceleration",
    "eval_error_velocity",
    "eval_error_acceleration",
]


def test_analyze_waits_for_click(
    clean: BiDrive, bi_config: Config, motion_cfg: MotionConfig, eval_spy: list[str]
) -> None:
    at = _analyze_app(clean, bi_config, motion_cfg).run()
    assert not at.exception
    assert eval_spy == []
    assert at.button(key="analyze_evaluate") is not None
    assert not at.dataframe  # no metrics table
    assert _chart_keys(at) == MOTION_KEYS  # no evaluation chart


def test_analyze_shows_stored_motion_without_click_or_raw_folder(
    clean: BiDrive, bi_config: Config, motion_cfg: MotionConfig, eval_spy: list[str]
) -> None:
    config = bi_config.model_copy(update={"raw_dir": clean.root / "no-such-raw"})
    args = (clean.client, config, motion_cfg, _lake_dir(clean), clean.drive_id)
    at = AppTest.from_function(analyze_script, args=args, default_timeout=30).run()
    assert not at.exception
    assert not at.error
    assert _chart_keys(at) == MOTION_KEYS
    assert eval_spy == []


def test_analyze_no_motion_shows_command_not_charts(
    unmotioned: BiDrive, bi_config: Config, motion_cfg: MotionConfig, eval_spy: list[str]
) -> None:
    at = _analyze_app(unmotioned, bi_config, motion_cfg).run()
    assert not at.exception
    assert not at.get("plotly_chart")
    command = motion_command(unmotioned.drive_id, _lake_dir(unmotioned))
    assert [c.value for c in at.code] == [command]
    assert len(at.error) == 1
    assert "no motion table" in str(at.error[0].value)
    assert eval_spy == []


def test_analyze_click_draws_report_once(
    clean: BiDrive, bi_config: Config, motion_cfg: MotionConfig, eval_spy: list[str]
) -> None:
    config = bi_config.model_copy(update={"raw_dir": clean.drive_dir.parent})
    report = evaluate_drive(clean.drive_dir, clean.client, config, motion_cfg)
    at = _analyze_app(clean, bi_config, motion_cfg).run()
    at.button(key="analyze_evaluate").click().run()
    assert not at.exception
    assert eval_spy == ["called"]
    assert report.passed
    status = _text(at.success) + _text(at.error)
    assert "PASS" in status
    assert "FAIL" not in status
    assert _chart_keys(at) == MOTION_KEYS + EVAL_KEYS
    metrics_table = at.dataframe[0].value
    assert list(metrics_table["metric"]) == ["position", "velocity", "acceleration"]
    expected = data.metric_rows(report, config, motion_cfg)
    assert list(metrics_table["interior_max"]) == pytest.approx(
        [row["interior_max"] for row in expected]
    )
    assert "ground truth" in _text(at.caption)
    at.run()  # unrelated rerun: the stored outcome is drawn, no new evaluation
    assert not at.exception
    assert eval_spy == ["called"]
    assert _chart_keys(at) == MOTION_KEYS + EVAL_KEYS
    assert "PASS" in _text(at.success)
    assert at.session_state["analyze_outcome"].drive == clean.drive_id


def test_analyze_outcome_of_other_drive_is_not_drawn(
    clean: BiDrive, bi_config: Config, motion_cfg: MotionConfig, eval_spy: list[str]
) -> None:
    at = _analyze_app(clean, bi_config, motion_cfg)
    at.session_state["analyze_outcome"] = AnalyzeOutcome("other-drive", None, "stale", "stale cmd")
    at.run()
    assert not at.exception
    assert not at.error
    assert not at.code
    assert eval_spy == []


def test_analyze_fail_and_problem_paths(
    clean: BiDrive,
    dropped: BiDrive,
    bi_config: Config,
    motion_cfg: MotionConfig,
) -> None:
    tight = motion_cfg.model_copy(update={"tolerances": MotionTolerances(velocity_cm_s=1e-9)})
    config = bi_config.model_copy(update={"raw_dir": clean.drive_dir.parent})
    assert not evaluate_drive(clean.drive_dir, clean.client, config, tight).passed
    at = _analyze_app(clean, bi_config, tight).run()
    at.button(key="analyze_evaluate").click().run()
    assert not at.exception
    assert "FAIL" in _text(at.error)
    assert "PASS" not in _text(at.success)
    assert _chart_keys(at) == MOTION_KEYS + EVAL_KEYS
    assert not at.dataframe[0].value["within"].iloc[1]  # velocity row exceeds 1e-9 cm/s

    # problem without a command: the dropped drive has 288 truth rows but 287 manifest rows
    at = _analyze_app(dropped, bi_config, motion_cfg).run()
    at.button(key="analyze_evaluate").click().run()
    assert not at.exception
    assert len(at.error) == 1
    assert "ground truth has 288 rows; the manifest implies 287" in str(at.error[0].value)
    assert not at.code
    assert _chart_keys(at) == MOTION_KEYS

    # problem with a command: the raw folder is gone, so the ingest command is offered
    config_missing = bi_config.model_copy(update={"raw_dir": clean.root / "no-such-raw"})
    args = (clean.client, config_missing, motion_cfg, _lake_dir(clean), clean.drive_id)
    at = AppTest.from_function(analyze_script, args=args, default_timeout=30).run()
    at.button(key="analyze_evaluate").click().run()
    assert not at.exception
    assert len(at.error) == 1
    assert [c.value for c in at.code] == [
        ingest_command(clean.root / "no-such-raw", clean.drive_id, _lake_dir(clean))
    ]


def test_analyze_read_errors_are_messages(
    clean: BiDrive, bi_config: Config, motion_cfg: MotionConfig
) -> None:
    lineage = clean.client.lineage(clean.drive_id)
    quarantine = clean.client.quarantine(clean.drive_id)
    for error in (OSError("disk"), pa.ArrowInvalid("bad file")):
        stub = StubClient(lineage, quarantine, error=error)
        args = (stub, bi_config, motion_cfg, _lake_dir(clean), clean.drive_id)
        at = AppTest.from_function(analyze_script, args=args, default_timeout=30).run()
        assert not at.exception
        assert len(at.error) == 1
        assert not at.get("plotly_chart")
    bad = _analyze_app(clean, bi_config, motion_cfg, drive="../x").run()
    assert not bad.exception
    assert len(bad.error) == 1
    assert "invalid drive id" in str(bad.error[0].value)


def test_units_in_labels(
    clean: BiDrive, dropped: BiDrive, bi_config: Config, motion_cfg: MotionConfig
) -> None:
    at = _clean_app(dropped.client, dropped.drive_id, dropped).run()
    for label in (m.label for m in at.metric):
        assert "(frames)" in label or "(rows)" in label
    app = _analyze_app(clean, bi_config, motion_cfg).run()
    app.button(key="analyze_evaluate").click().run()
    table = app.dataframe[0].value
    assert list(table["unit"]) == ["cm", "cm/s", "cm/s^2"]
    titles: dict[str, tuple[str, str]] = {}
    for chart in app.get("plotly_chart"):
        layout = json.loads(chart.proto.spec)["layout"]  # JSON text of the Plotly figure
        titles[chart.key] = (layout["xaxis"]["title"]["text"], layout["yaxis"]["title"]["text"])
    assert set(titles) == set(MOTION_KEYS + EVAL_KEYS)
    for key, (x_title, _) in titles.items():
        assert x_title == "time (s)", key
    assert "(cm)" in titles["motion_position"][1]
    assert "(cm/s)" in titles["motion_velocity"][1]
    assert "(cm/s^2)" in titles["motion_acceleration"][1]
    assert "(cm/s)" in titles["eval_timeline_velocity"][1]
    assert "(cm/s^2)" in titles["eval_timeline_acceleration"][1]
    assert "(cm/s)" in titles["eval_error_velocity"][1]
    assert "(cm/s^2)" in titles["eval_error_acceleration"][1]


# --- Repair: no path in messages, tolerances pinned, drawing errors guarded ------------------

SECRET = r"C:\secret\x.parquet"


def _leaky_errors() -> list[Exception]:
    return [OSError(SECRET), pa.ArrowInvalid(f"Failed to open local file '{SECRET}'")]


def _page_text(at: AppTest) -> str:
    return _text(at.error) + " " + _text(at.info)


def test_clean_load_quality_error_hides_path(dropped: BiDrive) -> None:
    lineage = dropped.client.lineage(dropped.drive_id)
    quarantine = dropped.client.quarantine(dropped.drive_id)
    for error in _leaky_errors():
        stub = StubClient(lineage, quarantine, error=error)
        at = _clean_app(stub, dropped.drive_id, dropped).run()  # type: ignore[arg-type]
        assert not at.exception
        page = _page_text(at)
        assert "secret" not in page
        assert SECRET not in page
        assert type(error).__name__ in page


def test_clean_quarantine_error_hides_path(dropped: BiDrive) -> None:
    lineage = dropped.client.lineage(dropped.drive_id)
    quarantine = dropped.client.quarantine(dropped.drive_id)
    for error in _leaky_errors():
        stub = StubClient(lineage, quarantine, fail_quarantine_on_call=2, error=error)
        at = _clean_app(stub, dropped.drive_id, dropped).run()  # type: ignore[arg-type]
        assert not at.exception
        page = _page_text(at)
        assert "secret" not in page
        assert SECRET not in page
        assert type(error).__name__ in page


def test_analyze_motion_error_hides_path(
    clean: BiDrive, bi_config: Config, motion_cfg: MotionConfig
) -> None:
    lineage = clean.client.lineage(clean.drive_id)
    quarantine = clean.client.quarantine(clean.drive_id)
    for error in _leaky_errors():
        stub = StubClient(lineage, quarantine, error=error)
        args = (stub, bi_config, motion_cfg, _lake_dir(clean), clean.drive_id)
        at = AppTest.from_function(analyze_script, args=args, default_timeout=30).run()
        assert not at.exception
        page = _page_text(at)
        assert "secret" not in page
        assert SECRET not in page
        assert type(error).__name__ in page


def test_analyze_error_charts_use_matching_tolerances(
    clean: BiDrive, bi_config: Config, motion_cfg: MotionConfig
) -> None:
    cfg = motion_cfg.model_copy(
        update={
            "tolerances": MotionTolerances(velocity_cm_s=7.5, acceleration_cm_s2=123.0),
        }
    )
    at = _analyze_app(clean, bi_config, cfg).run()
    at.button(key="analyze_evaluate").click().run()
    assert not at.exception
    expected = {"eval_error_velocity": 7.5, "eval_error_acceleration": 123.0}
    seen: dict[str, set[float]] = {}
    for chart in at.get("plotly_chart"):
        if chart.key in expected:
            shapes = json.loads(chart.proto.spec)["layout"]["shapes"]
            seen[chart.key] = {shape["y0"] for shape in shapes}
    assert seen == {key: {value, -value} for key, value in expected.items()}


def test_clean_drawing_error_is_message(dropped: BiDrive, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args: object, **kwargs: object) -> object:
        raise KeyError(SECRET)

    monkeypatch.setattr("vl_data.bi.clean_tab.quality_figure", broken)
    at = _clean_app(dropped.client, dropped.drive_id, dropped).run()
    assert not at.exception
    assert len(at.error) == 1
    page = _page_text(at)
    assert "KeyError" in page
    assert dropped.drive_id in page
    assert "secret" not in page


def test_analyze_motion_drawing_error_is_message(
    clean: BiDrive, bi_config: Config, motion_cfg: MotionConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object, **kwargs: object) -> object:
        raise KeyError(SECRET)

    monkeypatch.setattr("vl_data.bi.analyze_tab.motion_figure", broken)
    at = _analyze_app(clean, bi_config, motion_cfg).run()
    assert not at.exception
    assert len(at.error) == 1
    page = _page_text(at)
    assert "KeyError" in page
    assert clean.drive_id in page
    assert "secret" not in page


def test_analyze_report_drawing_error_is_message(
    clean: BiDrive, bi_config: Config, motion_cfg: MotionConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object, **kwargs: object) -> object:
        raise pa.ArrowInvalid(SECRET)

    monkeypatch.setattr("vl_data.bi.analyze_tab.timeline_figure", broken)
    at = _analyze_app(clean, bi_config, motion_cfg).run()
    at.button(key="analyze_evaluate").click().run()
    assert not at.exception
    page = _page_text(at)
    assert "ArrowInvalid" in page
    assert clean.drive_id in page
    assert "secret" not in page


def test_clean_quarantine_table_drawing_error_is_message(dropped: BiDrive) -> None:
    class BrokenTable:
        num_rows = 1

        def slice(self, *args: object) -> object:
            raise KeyError(SECRET)

    class BrokenQuarantine(StubClient):
        def quarantine(self, drive: str) -> pa.Table:
            table = super().quarantine(drive)
            if self._quarantine_calls == 2:  # the table read, after load_quality
                return BrokenTable()  # type: ignore[return-value]
            return table

    lineage = dropped.client.lineage(dropped.drive_id)
    stub = BrokenQuarantine(lineage, dropped.client.quarantine(dropped.drive_id))
    at = _clean_app(stub, dropped.drive_id, dropped).run()  # type: ignore[arg-type]
    assert not at.exception
    assert len(at.error) == 1
    assert "KeyError" in _page_text(at)
    assert "secret" not in _page_text(at)
    assert not at.dataframe
