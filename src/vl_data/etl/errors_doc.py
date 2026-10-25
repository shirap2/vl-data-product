"""Builds docs/ERROR_CODES.md from the rule registry (ASCII only, LF line ends)."""

import dataclasses

from vl_data.etl.rules import RULES
from vl_data.generator import faults

_INTRO = """\
# Error codes

This file is generated from the rule registry in `src/vl_data/etl/rules.py` by
`python -m vl_data error-codes --write`. Do not edit it by hand: a test fails if it drifts from
the code.

A frame that fails a frame rule is moved to the quarantine table with its code, and valid frames
continue through the pipeline. A drive-level problem rejects the whole drive. Timestamps and
frame counts in the rules come from the drive itself (its manifest), so the wording below holds
at any point density and frame rate.
"""

_INTEGRITY = """\
## Integrity errors (outside the VL codes)

The manifest check runs before any frame is read. These problems are not frame-level errors, so
they have no VL code. Each is written to the quarantine table as a drive-level row (no frame_id,
no row_index) with the code `INTEGRITY`, and the drive is marked `rejected` in the lineage table.
The drive's keypoints are removed. The causes are:

- An unreadable manifest: missing, not valid JSON, invalid, or a schema-1 manifest without a body.
- A folder name that is not the first 12 hex characters of `scenario_hash`.
- A malformed `scenario_hash` (not 64 lowercase hex characters).
- An unsupported schema version (see `supported_schema_versions` in `config/etl.yaml`).
- A frame rate (fps) that is below 1 or does not divide 1,000,000,000 ns evenly.
- `frames.parquet` not listed in the manifest.
- Points per frame above `max_points_per_frame`.
- An invalid body or scenario in the manifest.
- Hash or size problems found by the manifest check: a changed, missing or extra file.
- An unreadable or implausible frames file: not Parquet, a footer that claims too many rows, or a
  row group above `max_row_group_mib`.
- A null frame number or timestamp in the frames file.
- A frame number outside 0 to the expected frame count minus 1.

At most 20 problems are stored per drive; when there are more, one extra row says
"and K more problems". A frames file that reads fine but has the wrong columns or types is
VL-005, not INTEGRITY.
"""

_GENERATOR = """\
## Generator-side limits

The generator itself rejects a scenario instead of writing a bad drive: a duration that is not
a whole number of frames, or a move whose start and end speed are both 0.
"""


def _cell(text: str) -> str:
    """Text safe inside a Markdown table cell: pipes escaped, one line."""
    return " ".join(text.split()).replace("|", "\\|")


def _example(class_name: str) -> str:
    """The example fault as `Name(field, ...)` from the fields of its dataclass."""
    fault_class = getattr(faults, class_name)
    names = ", ".join(field.name for field in dataclasses.fields(fault_class))
    return f"`{class_name}({names})`"


def render_error_codes() -> str:
    """Markdown for docs/ERROR_CODES.md, built from RULES plus the INTEGRITY note.

    One `# ` title and `##` sections only; ASCII; ends with a newline.
    """
    lines = [
        "## The six rule codes",
        "",
        "| Code | Name | What it means | How it is detected | Example fault used in tests |",
        "|---|---|---|---|---|",
    ]
    for rule in RULES:
        scope = "Rejects the whole drive. " if rule.scope == "drive" else ""
        lines.append(
            f"| {rule.code} | {_cell(rule.name)} | {_cell(rule.meaning)} | "
            f"{_cell(scope + rule.detection)} | {_example(rule.example_fault)} |"
        )
    return "\n".join([_INTRO, *lines, "", _INTEGRITY, _GENERATOR])
