"""AppTest tests of the SQL tab. Units: rows, cm, cm/s."""

from pathlib import Path

import pyarrow as pa
import pytest
from streamlit.testing.v1 import AppTest

from vl_data.bi.data import example_queries
from vl_data.sdk.client import VLClient

from .helpers import BiDrive


def sql_script(client, drive):  # noqa: ANN001, ANN201
    from vl_data.bi.sql_tab import render_sql_tab

    render_sql_tab(client, drive)


def _app(client: VLClient, drive: str) -> AppTest:
    return AppTest.from_function(sql_script, args=(client, drive), default_timeout=30)


def _run_text(at: AppTest, query: str) -> AppTest:
    at.text_area(key="sql_text").set_value(query).run()
    at.button(key="sql_run").click().run()
    return at


def _captions(at: AppTest) -> list[str]:
    return [str(c.value) for c in at.caption]


def test_example_fills_box_and_run_shows_table(dropped: BiDrive) -> None:
    examples = example_queries(dropped.drive_id)
    label = "Quarantine rows per code"
    at = _app(dropped.client, dropped.drive_id).run()
    assert not at.exception
    assert at.text_area(key="sql_text").value == ""
    assert not at.dataframe
    at.selectbox(key="sql_example").select(label).run()
    assert at.text_area(key="sql_text").value == examples[label]
    at.button(key="sql_run").click().run()
    assert not at.exception
    assert len(at.dataframe) == 1
    frame = at.dataframe[0].value
    assert frame["code"].tolist() == ["VL-001"]
    assert frame["rows_quarantined"].tolist() == [1]
    assert "1 rows" in _captions(at)
    assert not at.error


def test_sql_tab_messages(dropped: BiDrive, tmp_path: Path) -> None:
    at = _app(dropped.client, dropped.drive_id).run()
    assert any("one SELECT only" in c for c in _captions(at))

    _run_text(at, "DROP VIEW keypoints")
    assert not at.exception
    assert [e.value for e in at.error] == ["only a SELECT statement is allowed"]
    assert not at.dataframe

    _run_text(at, "SELECT no_such_column FROM lineage")
    assert not at.exception
    assert len(at.error) == 1
    assert "no_such_column" in str(at.error[0].value)

    _run_text(at, "SELECT * FROM generate_series(1, 12345) AS t(n)")
    assert not at.exception
    assert not at.error
    assert len(at.dataframe) == 1
    assert len(at.dataframe[0].value) == 10_000
    assert "showing 10,000 of 12,345 rows" in _captions(at)

    missing = tmp_path / "secret_folder_xyz" / "missing.parquet"
    _run_text(at, f"SELECT * FROM read_parquet('{missing.as_posix()}')")
    assert not at.exception
    assert len(at.error) == 1
    text = str(at.error[0].value)
    assert "IOException" in text
    assert "secret_folder_xyz" not in text
    assert "missing.parquet" not in text


def test_sql_tab_bad_drive_id_shows_error_and_box(dropped: BiDrive) -> None:
    at = _app(dropped.client, "bad'id").run()
    assert not at.exception
    assert [e.value for e in at.error] == [
        "invalid drive id: use letters, digits, '_' and '-' only"
    ]
    assert at.text_area(key="sql_text").value == ""
    assert at.selectbox(key="sql_example").options == []
    _run_text(at, "SELECT 1 AS one")
    assert not at.exception
    assert at.dataframe[0].value["one"].tolist() == [1]


def test_duplicate_column_names_are_made_unique(dropped: BiDrive) -> None:
    at = _app(dropped.client, dropped.drive_id).run()
    _run_text(at, "SELECT 1 AS a, 2 AS a")
    assert not at.exception
    assert not at.error
    assert len(at.dataframe) == 1
    frame = at.dataframe[0].value
    assert list(frame.columns) == ["a", "a_1"]
    assert frame["a"].tolist() == [1]
    assert frame["a_1"].tolist() == [2]


def test_unrenderable_result_shows_fixed_error(
    dropped: BiDrive, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise pa.ArrowInvalid("secret detail")

    monkeypatch.setattr("streamlit.dataframe", broken)
    at = _app(dropped.client, dropped.drive_id).run()
    _run_text(at, "SELECT 1 AS one")
    assert not at.exception
    assert [e.value for e in at.error] == ["The result cannot be shown as a table"]
    assert not any("rows" in c for c in _captions(at))


@pytest.mark.parametrize(
    ("count", "caption"),
    [(10_000, "10,000 rows"), (10_001, "showing 10,000 of 10,001 rows"), (0, "0 rows")],
)
def test_row_count_caption_at_the_cap(dropped: BiDrive, count: int, caption: str) -> None:
    at = _app(dropped.client, dropped.drive_id).run()
    _run_text(at, f"SELECT * FROM range({count}) AS t(n)")
    assert not at.exception
    assert not at.error
    assert len(at.dataframe) == 1
    assert len(at.dataframe[0].value) == min(count, 10_000)
    captions = _captions(at)
    assert caption in captions
    assert any("showing" in c for c in captions) == (count > 10_000)
