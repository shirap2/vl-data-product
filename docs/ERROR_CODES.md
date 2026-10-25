# Error codes

Draft. The six codes below are agreed. Module 2 (the loader and ETL) owns the rules, and this
file will be regenerated from its rule registry so it cannot drift from the code. Until then
the "How it is detected" column describes the intended rule, not shipped code.

A frame that fails a rule is moved to the quarantine table with its code. Valid frames
continue through the pipeline.

| Code | Name | What it means | How it is detected | Example fault used in tests |
|---|---|---|---|---|
| VL-001 | Missing frame | A frame number is absent from the sequence. | A number from 0 to the expected frame count minus 1 has no row. The expected count comes from the scenario in the manifest, not from `manifest.frame_count`. One quarantine row per missing number. | `DropFrame(frame_id)` |
| VL-002 | Timestamp gap | A frame's timestamp is not what its frame number implies. | The timestamp is not `frame_id x 62,500,000 ns`. | `ShiftTimestamp(frame_id, delta_ns)` |
| VL-003 | Duplicate frame | The same frame number appears more than once. | A repeated `frame_id`. Identical copies keep the first and quarantine the repeats; differing copies are all quarantined. | `DuplicateFrame(frame_id)` |
| VL-004 | Corrupt point cloud | A frame's points are not a valid 1,000-point cube. | The payload is not a multiple of 12 bytes, does not hold the drive's points per frame (12,000,000 bytes at the shipped density), or a point is more than rounding away from the expected lattice (per axis the offsets may spread by at most 1 cm). | `CorruptPoints(frame_id)` |
| VL-005 | Schema mismatch | The table does not have the expected columns and types. | The schema differs from the frames schema. Rejects the whole drive. | `BreakSchema()` |
| VL-006 | Impossible motion | The distance moved between two frames is beyond what the vehicle can do. | The centroid is farther from the last accepted frame than the configured maximum speed allows (scaled by the frame gap, plus a rounding margin). The starting frame is the earlier frame of the most tightly agreeing pair among the first three clean frames. Acceleration is not tested. | `Teleport(frame_id, dx_cm)` |

## Integrity errors (outside the VL codes)

The manifest check runs before any frame is read. It reports a changed file, a missing file,
or an extra file, and the load stops. The same applies to an unreadable manifest, an unsupported
schema version, a folder name that does not match the manifest hash, and a frame number outside
0 to `frame_count - 1`. These are not frame-level errors, so they have no VL code. They are
written to the quarantine table as one drive-level row each with the code `INTEGRITY`, and the
drive is marked `rejected` in the lineage table.

## Generator-side limits

The generator itself rejects a scenario instead of writing a bad drive: a duration that is not
a whole number of frames, or a move whose start and end speed are both 0.
