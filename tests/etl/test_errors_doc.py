"""Tests for the generated error-code document. Small body only (tmp_path).

The real faults-to-codes check (each example fault gives its code) is in test_e2e.py, through
the command line.
"""

from dataclasses import replace
from pathlib import Path

import pytest

from vl_data.etl import errors_doc
from vl_data.etl.errors_doc import render_error_codes
from vl_data.etl.rules import RULES

DOC = Path(__file__).resolve().parents[2] / "docs" / "ERROR_CODES.md"

INTEGRITY_BULLETS = [
    "- An unreadable manifest: missing, not valid JSON, invalid, or a schema-1 manifest without a "
    "body.",
    "- A folder name that is not the first 12 hex characters of `scenario_hash`.",
    "- A malformed `scenario_hash` (not 64 lowercase hex characters).",
    "- An unsupported schema version (see `supported_schema_versions` in `config/etl.yaml`).",
    "- A frame rate (fps) that is below 1 or does not divide 1,000,000,000 ns evenly.",
    "- `frames.parquet` not listed in the manifest.",
    "- Points per frame above `max_points_per_frame`.",
    "- An invalid body or scenario in the manifest.",
    "- Hash or size problems found by the manifest check: a changed, missing or extra file.",
    "- A null frame number or timestamp in the frames file.",
    "- A frame number outside 0 to the expected frame count minus 1.",
]


def test_doc_regenerates_without_diff() -> None:
    text = DOC.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert render_error_codes() == text


def test_all_six_codes_present() -> None:
    lines = render_error_codes().splitlines()
    for number, rule in enumerate(RULES, start=1):
        assert rule.code == f"VL-00{number}"
        assert any(line.startswith(f"| {rule.code} | ") for line in lines), rule.code
    assert any(line.startswith("## Integrity errors") for line in lines)


def test_document_shape() -> None:
    text = render_error_codes()
    lines = text.splitlines()
    assert text.endswith("\n")
    assert text.isascii()
    assert sum(line.startswith("# ") for line in lines) == 1
    assert lines[0].startswith("# ")
    assert not any(line.startswith("###") for line in lines)
    assert sum(line.startswith("## ") for line in lines) >= 2
    assert "\r" not in text


def test_fifth_column_comes_from_fault_fields() -> None:
    text = render_error_codes()
    assert "`DropFrame(frame_id)`" in text
    assert "`ShiftTimestamp(frame_id, delta_ns)`" in text
    assert "`BreakSchema()`" in text
    assert "`Teleport(frame_id, dx_cm)`" in text


def test_pipe_in_registry_text_is_escaped(monkeypatch: pytest.MonkeyPatch) -> None:
    piped = [replace(RULES[0], meaning="a | b")]
    monkeypatch.setattr(errors_doc, "RULES", piped)
    text = render_error_codes()
    assert "a \\| b" in text
    assert "a | b" not in text


def test_integrity_section_lists_the_gates() -> None:
    lines = render_error_codes().splitlines()
    for bullet in INTEGRITY_BULLETS:
        assert bullet in lines, bullet
    assert (
        "- An unreadable or implausible frames file: not Parquet, a footer that claims too many "
        "rows, or a" in lines
    )
    assert "  row group above `max_row_group_mib`." in lines
    assert (
        "At most 20 problems are stored per drive; when there are more, one extra row says" in lines
    )
    assert (
        '"and K more problems". A frames file that reads fine but has the wrong columns or '
        "types is" in lines
    )
    assert "VL-005, not INTEGRITY." in lines
