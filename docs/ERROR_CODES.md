# Error codes

Draft. The six codes below are agreed. Module 2 (the loader and ETL) owns the rules, and this
file will be regenerated from its rule registry so it cannot drift from the code. Until then
the "How it is detected" column describes the intended rule, not shipped code.

A frame that fails a rule is moved to the quarantine table with its code. Valid frames
continue through the pipeline.

| Code | Name | What it means | How it is detected | Example fault used in tests |
|---|---|---|---|---|
| VL-001 | Missing frame | A frame number is absent from the sequence. | `frame_id` values are not consecutive. | `DropFrame(frame_id)` |
| VL-002 | Timestamp gap | Two neighbouring frames are not 62,500,000 ns apart. | The timestamp difference is not exactly 62,500,000 ns. | `ShiftTimestamp(frame_id, delta_ns)` |
| VL-003 | Duplicate frame | The same frame number appears more than once. | A repeated `frame_id`. | `DuplicateFrame(frame_id)` |
| VL-004 | Corrupt point cloud | A frame's points are not a valid cube of `points_per_frame` points (1,000,000 at the shipped density). | The byte length is not `points_per_frame` x 3 x 4 (12,000,000 bytes at the shipped density), or the corner spacing is not the cube's. | `CorruptPoints(frame_id)` |
| VL-005 | Schema mismatch | The table does not have the expected columns and types. | The schema differs from the frames schema. | `BreakSchema()` |
| VL-006 | Impossible motion | The speed implied between two frames is beyond what the vehicle can do. | Centroid x change over 1/16 s exceeds the configured maximum speed or acceleration. | `Teleport(frame_id, dx_cm)` |

## Integrity errors (outside the VL codes)

The manifest check runs before any frame is read. It reports a changed file, a missing file,
or an extra file, and the load stops. These are not frame-level errors, so they have no VL
code.

## Generator-side limits

The generator itself rejects a scenario instead of writing a bad drive: a duration that is not
a whole number of frames, or a move whose start and end speed are both 0.
