# Error codes

This file is generated from the rule registry in `src/vl_data/etl/rules.py` by
`python -m vl_data error-codes --write`. Do not edit it by hand: a test fails if it drifts from
the code.

A frame that fails a frame rule is moved to the quarantine table with its code, and valid frames
continue through the pipeline. A drive-level problem rejects the whole drive. Timestamps and
frame counts in the rules come from the drive itself (its manifest), so the wording below holds
at any point density and frame rate.

## The six rule codes

| Code | Name | What it means | How it is detected | Example fault used in tests |
|---|---|---|---|---|
| VL-001 | Missing frame | A frame_id between 0 and frame_count - 1 has no row in the frames file. | Every id in range(frame_count) that is absent from the frame_id column. | `DropFrame(frame_id)` |
| VL-002 | Timestamp gap | A frame's timestamp is not frame_id times the frame interval. | timestamp_ns differs from frame_id * frame_interval_ns, checked per row. | `ShiftTimestamp(frame_id, delta_ns)` |
| VL-003 | Duplicate frame | More than one row carries the same frame_id. | Rows are grouped by frame_id. If all rows of a group have the same timestamp and payload hash, the first is kept and the repeats are flagged; otherwise every row of the group is flagged. | `DuplicateFrame(frame_id)` |
| VL-004 | Corrupt point cloud | A frame's points are not the expected body: wrong length, count or shape. | The payload must be a multiple of 12 bytes, decode to points_per_frame points, and each axis of (points - expected lattice) must spread at most 1 cm. | `CorruptPoints(frame_id)` |
| VL-005 | Schema mismatch | The frames file does not have the columns and types of the frames schema. | Rejects the whole drive. The file schema (metadata ignored) is compared with the frames schema. | `BreakSchema()` |
| VL-006 | Impossible motion | The body moved farther between frames than the speed limit allows. | Among frames with no other violation, in frame_id order, a centroid farther from the last accepted centroid than max_speed * frame_gap / fps + margin is flagged and never becomes the baseline. | `Teleport(frame_id, dx_cm)` |

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
- A drive that cannot be verified: an OSError, ValueError, RecursionError or MemoryError while
  the files are hashed ("cannot verify the drive").
- A body that cannot be turned into the lattice ("cannot build the lattice").
- An unreadable or implausible frames file: not Parquet, a footer that claims too many rows, or a
  row group above `max_row_group_mib`.
- A footer that lists too many row groups.
- A null frame number or timestamp in the frames file.
- A frame number outside 0 to the expected frame count minus 1.

A MemoryError while the rules run or while the frames file is scanned is also an INTEGRITY
rejection of the whole drive.

At most 20 problems are stored per drive; when there are more, one extra row says
"and K more problems". A frames file that reads fine but has the wrong columns or types is
VL-005, not INTEGRITY.

## Generator-side limits

The generator itself rejects a scenario instead of writing a bad drive: a duration that is not
a whole number of frames, a move whose start and end speed are both 0, a scenario of more than
2,000 frames (`MAX_FRAMES` in `generator.models`), or a stop segment while the vehicle is not at
rest (a stop needs speed 0; ASSUMPTIONS A40).
