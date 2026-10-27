# Supporting documentation

Vehicle-location (VL) data product. This file holds the three supporting documents the
exercise asks for: the list of error codes, the architecture and design, and the release
notes. It is built from `docs/ERROR_CODES.md`, `docs/ARCHITECTURE.md` and
`docs/RELEASE_NOTES.md` by `tools/build_supporting_docs.py`. Edit those files, not this one.

## 1. Error codes

This file is generated from the rule registry in `src/vl_data/etl/rules.py` by
`python -m vl_data error-codes --write`. Do not edit it by hand: a test fails if it drifts from
the code.

A frame that fails a frame rule is moved to the quarantine table with its code, and valid frames
continue through the pipeline. A drive-level problem rejects the whole drive. Timestamps and
frame counts in the rules come from the drive itself (its manifest), so the wording below holds
at any point density and frame rate.

### The six rule codes

| Code | Name | What it means | How it is detected | Example fault used in tests |
|---|---|---|---|---|
| VL-001 | Missing frame | A frame_id between 0 and frame_count - 1 has no row in the frames file. | Every id in range(frame_count) that is absent from the frame_id column. | `DropFrame(frame_id)` |
| VL-002 | Timestamp gap | A frame's timestamp is not frame_id times the frame interval. | timestamp_ns differs from frame_id * frame_interval_ns, checked per row. | `ShiftTimestamp(frame_id, delta_ns)` |
| VL-003 | Duplicate frame | More than one row carries the same frame_id. | Rows are grouped by frame_id. If all rows of a group have the same timestamp and payload hash, the first is kept and the repeats are flagged; otherwise every row of the group is flagged. | `DuplicateFrame(frame_id)` |
| VL-004 | Corrupt point cloud | A frame's points are not the expected body: wrong length, count or shape. | The payload must be a multiple of 12 bytes, decode to points_per_frame points, and each axis of (points - expected lattice) must spread at most 1 cm. | `CorruptPoints(frame_id)` |
| VL-005 | Schema mismatch | The frames file does not have the columns and types of the frames schema. | Rejects the whole drive. The file schema (metadata ignored) is compared with the frames schema. | `BreakSchema()` |
| VL-006 | Impossible motion | The body moved farther between frames than the speed limit allows. | Among frames with no other violation, in frame_id order, a centroid farther from the last accepted centroid than max_speed * frame_gap / fps + margin is flagged and never becomes the baseline. | `Teleport(frame_id, dx_cm)` |

### Integrity errors (outside the VL codes)

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

### Generator-side limits

The generator itself rejects a scenario instead of writing a bad drive: a duration that is not
a whole number of frames, a move whose start and end speed are both 0, a scenario of more than
2,000 frames (`MAX_FRAMES` in `generator.models`), or a stop segment while the vehicle is not at
rest (a stop needs speed 0; ASSUMPTIONS A40).

## 2. Architecture and design

Status as of 2026-10-25: the design below is agreed. Module 1 (the generator, with its Generate
tab) and Module 2 (the loader and ETL: lake, rules, ingest and the command line) are built and
tested. Modules 3 and 4 are designed in outline and will get their own plans. Anything marked
"planned" has not been run.

### What the product does

It is a small end-to-end data product for a vehicle-location signal: a mocked data source, an
ETL, a queryable warehouse, and a BI view. All data is synthetic. The vehicle is a 1 m cube
that moves in a straight line along x. The use case is extracting its velocity and
acceleration from the point cloud alone and showing that the result matches the known truth.

### Modules

```
 [1 Generator] -> data/raw/<hash12>/ -> [2 ETL] -> lake -> [3 Use case] -> lake
                                                    |                      |
                                                    +---- SDK + SQL <------+
                                                              |
                                                       [4 BI: Streamlit]
```

| Module | Job | Status |
|---|---|---|
| 1 Generator | Turn a start position and a motion profile into a hashed drive: point cloud, ground truth, manifest. Check itself in both directions. | Built and tested |
| 2 Loader / ETL | Verify hashes, validate frames (VL-001 to VL-006), reduce each frame to 9 keypoints, write to the lake. | Built and tested |
| 3 Use-case algorithm | Read keypoints, compute velocity and acceleration, write a motion table. Evaluate against ground truth. | Outline |
| 4 BI | One Streamlit app with three tabs: Generate, Clean, Analyze. | Outline |

Shared code lives in `core/`: config loader, Arrow schemas, hashing, the manifest, the `Lake`
class and the lake table schemas. Imports only point down: entry points, then modules, then core. Core never imports a
module.

### Folder structure

```
src/vl_data/
  __main__.py, cli.py        entry point and root command app
  core/                      config, hashing, schemas, manifest
  generator/                 Module 1: models, body, motion, drive, writer,
                             inverse, faults, cli, scenarios/, ui/
  etl/                       Module 2 (built): config, body, keypoints, facts, rules,
                             ingest, errors_doc, cli
  core/lake.py, lake_schemas.py   Module 2 (built): the Lake class and the table schemas
config/default.yaml          fps, output folder, body, tolerances, fit parameters
config/etl.yaml              Module 2 (built): keypoint list, speed limit, chunk size
warehouse/                   the lake (built, git-ignored)
tests/                       mirrors the source folders
docs/                        this file and the other documents
data/raw/<hash12>/           generated drives (git-ignored)
pyproject.toml               packaging (hatchling), dependencies, ruff, mypy, pytest, coverage
.venv/                       project virtual environment (git-ignored)
```

Built so far: the project skeleton (`pyproject.toml`, `src/vl_data/__init__.py`, `__main__.py`,
`cli.py` with `--version`, `tests/conftest.py`, `tests/test_cli.py`), the shared core
(`src/vl_data/core/` with `config.py`, `hashing.py`, `schemas.py`, `manifest.py`, plus
`config/default.yaml` and `tests/core/`), and the first three generator modules
(`src/vl_data/generator/models.py`, `body.py`, `motion.py`, the demo scenario in
`generator/scenarios/demo.yaml`, and `tests/generator/`), then the drive builder and writer
(`drive.py`, `writer.py`) and the generator's `generate` and `verify` commands
(`generator/cli.py`, registered by the root `cli.py`). The back direction (`inverse.py`) and the
`roundtrip` command are built, and so is the fault-injection helper (`faults.py`). The Generate
tab (`generator/ui/`) is built. Module 2 is built too: the lake (`core/lake.py`,
`core/lake_schemas.py`), the ETL settings, the keypoint pick, the frame scan, the six rules, the
ingest, the `ingest` and `error-codes` commands, and the generated `docs/ERROR_CODES.md`.
Not built yet: Module 3 (the use-case algorithm), and the Clean and Analyze tabs of Module 4.

### Data flow inside the generator

1. Load and validate the scenario YAML (start values and a list of `move` and `stop` segments).
2. Hash it: SHA-256 of the canonical JSON of the body (`size_cm`, `points_per_axis`) and the scenario gives the drive hash.
3. Build the speed for every frame (a straight-line ramp inside each segment), integrate the
   position, and derive ground-truth velocity and acceleration by differences.
4. Build each frame's points: a 100x100x100 lattice (1,000,000 points) shifted by the rounded position.
5. Write the frames and ground truth to a temporary folder, build the manifest from the file
   hashes, swap the folder into place, and verify it.

`generate` in `drive.py` does steps 2 to 4 in memory and returns a `Drive` (scenario, hash, frames
table, ground-truth table) by collecting `iter_frame_batches`, which yields record batches of 50 frames; at full density this is only for small drives. `write_drive` in `writer.py` does step 5 for such a drive. `generate_to_disk` streams the batches straight into the Parquet file (one batch is 600 MB of points, and the measured peak working set was 2,859 MiB, so the peak is several times one batch; its parts were not measured), writes the ground truth and manifest, and then runs `verify_drive`; the command line uses `generate_to_disk`.

How a drive is written:

- The temporary folder is `.tmp-*` inside the output folder and holds only the two Parquet files
  while the manifest is built. `manifest.json` is written last.
- Parquet options are fixed so the same scenario gives the same bytes on the same pyarrow version:
  format version 2.6, zstd compression, no dictionary, statistics on, data page version 2.0 and
  row groups of 50 rows (the file is written one row group at a time; both write paths give the same bytes).
- An existing folder for the same hash is moved into a `.old-*` folder next to it, the new folder
  is renamed into place, and then the `.old-*` folder is deleted. If the last rename fails, the
  old folder is moved back; if that also fails, the `.old-*` folder is kept (it holds the only
  copy of the old drive). The temporary folder is removed on any error.
- The frames are never computed from `ground_truth.parquet`. Only manifest hashing and verify
  read its bytes; the generator itself only writes it.
- Measured on the 1,000-frame demo at 1,000,000 points per frame (a scratch script with the same
  50-frame batching, `.claude/run-proofs/FULL-DENSITY-measure.log`): 75.1 s, peak working set
  2,859 MiB, `frames.parquet` 287,134,433 bytes with zstd (4,024,944,648 bytes with snappy). The
  real `generate` command took 81 s to 237.7 s including verification in single runs on one
  machine (81 s on 2026-10-24; 237.7 s in the fresh-clone run of 2026-10-25, measured in
  `FINAL-DOCS-M2-fresh-clone.log`); the spread is unexplained, see RELEASE_NOTES, Usage. The
  2,000-frame cap was not run (about 24 GB raw, about 0.6 GB on disk and about 2.5 minutes, scaled
  from this run).

Units are centimetres, seconds, cm/s and cm/s^2 everywhere.

### Data model

| Table or file | Layer | Content |
|---|---|---|
| `frames.parquet` | raw drive | frame_id, timestamp_ns, points (1,000,000 x 3 int32 = 12,000,000 bytes at the shipped density, one binary column) |
| `ground_truth.parquet` | raw drive | frame_id, timestamp_ns, x_cm, velocity_cm_s, acceleration_cm_s2 |
| `manifest.json` | raw drive | schema version 2, drive hash, scenario, body (`size_cm`, `points_per_axis`), fps, counts, SHA-256 and size of each file |
| keypoints | bronze (built) | one row per keypoint: frame_id, timestamp_ns, keypoint name, x, y, z (cm, float64); 9 rows per frame |
| quarantine | bronze (built) | one row per rejected frame or missing frame number: frame_id, row_index, code, message; a drive-level row has no frame_id |
| lineage | bronze (built) | one row per drive: scenario hash, schema version, status, frame counts, keypoint rows, the frames file hash |
| motion | silver (planned) | per-frame x, velocity, acceleration |
| evaluation tables | gold (planned) | errors against ground truth |

The warehouse tables use a generic, use-case-independent shape: a drive and frame key, a
timestamp, and numeric measures.

#### Core modules (built)

| Module | What it holds | Rules worth knowing |
|---|---|---|
| `core/config.py` | `Config` (fps, raw_dir, body, tolerances, fit) and `load_config` | Unknown keys and non-positive values are rejected, and so is a `points_per_axis` above `size_cm` (checked when the config loads; ASSUMPTIONS A65). `fps` must divide 1,000,000,000 ns evenly (16 fps gives 62,500,000 ns). The environment variable `VL_RAW_DIR` overrides `raw_dir`. |
| `core/hashing.py` | `canonical_json`, `sha256_hex`, `file_sha256`, `scenario_hash` | Keys are sorted, there are no spaces, and a float with a whole value is written as an int, so 600 and 600.0 hash the same. NaN and infinity are rejected. |
| `core/schemas.py` | `FRAMES_SCHEMA`, `GROUND_TRUTH_SCHEMA`, `encode_points`, `decode_points` | A frame's points are `points_per_frame` x 3 int32 values in cm (1,000,000 at the shipped density), little-endian, row-major, in one binary column. |
| `core/manifest.py` | `Manifest`, `FileEntry`, `VerifyResult`, build, write, read, `verify_drive` | The manifest lists every file in the drive folder except itself. `verify_drive` reports changed, missing, extra and invalidly named files and a scenario that no longer matches its hash. A bad manifest, an unreadable file or a scenario that cannot be hashed is a reported problem, not an error. A pathological listed name (hundreds of characters) or a folder that cannot be listed can still raise an OSError. |
| `core/lake_schemas.py` | `KEYPOINTS_SCHEMA`, `QUARANTINE_SCHEMA`, `LINEAGE_SCHEMA`, `TABLE_SCHEMAS` | One plain dict of table name to Arrow schema. A new table (for example `motion`) is one new entry; `Lake` accepts only names in this dict. |
| `core/lake.py` | `Lake`: `swap_partition`, `drop_partition`, `list_drives`, `read`, `connect` | Layout `<root>/<table>/drive=<id>/part-0.parquet`. A swap writes a new folder under `<root>/_swap`, moves the old partition aside, renames the new one in, then deletes the old one; a failed write or rename keeps the old partition. The Parquet write options are pinned, so the same input gives the same bytes on one pyarrow version and the same batch boundaries (checked by a test). `connect()` returns an in-memory DuckDB connection with one view per table; the drive column is a VARCHAR, so a numeric-looking id is never read as a number. |

The lake class was built and run in the Module 2 plan's first task. On 2026-10-23 its 33 tests passed (83 passed in the whole suite), line coverage was 98% for `lake.py` and 100% for `lake_schemas.py` (99.37% total), and ruff and mypy strict were clean.

What the Lake does not do:

- It is single-writer. Two swaps on the same root at the same time are not supported.
- A `_swap/old-*` folder is never deleted automatically after a failed restore, because it can be the only copy of a partition. Recover or remove it by hand. Only `new-*` folders older than an hour are swept.
- A view reads the files that exist when `connect()` runs. Close the connection before a swap, because an open connection may hold the files on Windows. On the test machine (Windows 11, DuckDB 2.0.0) a swap succeeded while an idle connection was open.
- Drive ids that differ only by letter case collide on Windows.
- The glob characters `*` and `?` in the lake path could only be tested on non-Windows systems, because Windows does not allow them in file names.
- Symlinks or junctions planted inside the lake folder are not detected.

### Module 2: loader and ETL (ingest, command line and error-code file built; full-density command-line run done on 2026-10-25)

The lake storage (`core/lake.py`, `core/lake_schemas.py`), the ETL settings, the keypoint pick, the
frame scan, the rules, the ingest (`etl/ingest.py`), the commands (`etl/cli.py`) and the
error-code document generator (`etl/errors_doc.py`) are built and tested on the small body.

#### Command line (built)

`etl/cli.py` registers two commands on the root app (`python -m vl_data`). Data flow: `ingest`
loads the ETL config (`load_etl_config`, never the core config), opens the lake, calls
`ingest_drive` for each drive folder and prints a `key: value` summary per drive (drive, status,
frames_expected, frames_in, frames_kept, frames_quarantined, frames_missing, keypoint_rows,
points_per_frame_in, keypoints_per_frame_out, one `quarantine_<code>` line per code, elapsed_s).
It prints codes and counts only, never quarantine message text.

Measured (2026-10-25, one Windows machine, `.claude/run-proofs/TASK-5-iter2-a.log`): `generate` of the 1,000-frame demo at 1,000,000 points per frame into `data/raw_proof`, then `ingest --all data/raw_proof --lake-dir warehouse`, took about 120 s of wall-clock time in total (start and finish times in the log). The ingest printed `status: ok`, `frames_expected: 1000`, `frames_kept: 1000`, `frames_quarantined: 0`, `keypoint_rows: 9000`, `points_per_frame_in: 1000000`, `keypoints_per_frame_out: 9` and `elapsed_s: 43.7`, and exited 0. One run, so it is a single measurement. In the fresh-clone run of
the same day (`.claude/run-proofs/FINAL-DOCS-M2-fresh-clone.log`) `ingest --all data/raw --lake-dir
warehouse` printed `elapsed_s: 49.1` (wall 50.9 s) and a single-folder `ingest` printed
`elapsed_s: 45.8` (wall 47.4 s), both exit 0 with the same counts; `generate` took 237.7 s there.

- `ingest <drive_dir>` or `ingest --all <raw_dir>` (exactly one of the two). `--all` takes only
  direct subfolders named by 12 lowercase hex characters, sorted; anything else prints
  `skipped: <name>`.
- The lake root is `--lake-dir`, else `VL_LAKE_DIR`, else `lake_dir` in `config/etl.yaml`, else
  `warehouse`.
- Exit codes: 0 = every drive ingested (quarantined frames are normal); 1 = at least one drive
  rejected (`--all` still ingests every drive first); 2 = usage or configuration error (both or
  neither target, a path that is not a folder, a single folder not named by 12 lowercase hex
  characters, no drive folders under `--all`, a keypoint spec that does not fit the body, an
  unreadable ETL config); 3 = at least one drive could not be read or written (an `OSError`;
  with `--all` the other drives are still ingested and the exit code is the highest of 3, 1, 0).
  A usage error found before any drive is ingested writes nothing. With `--all`, a keypoint-spec
  error (or another `ValueError` from `ingest_drive`) aborts at the drive where it is found, and the
  drives ingested before it stay written.
- `--all` never follows a symlink or junction (`skipped: <name> (link)`). Every name or text from
  outside that is printed passes the filter `_safe` (only 0x20 to 0x7e, cut to 64 characters).
- `error-codes` prints `render_error_codes()`; `--write` saves `docs/ERROR_CODES.md` (UTF-8, LF);
  `--check` exits 1 if the file differs (compared after CRLF to LF) or is missing; both flags exit
  2; `--path` picks another file, and is required outside a source checkout. `docs/ERROR_CODES.md`
  is built from the rule registry plus static text; a test fails if it drifts from the code.

ingest_drive measured earlier (not the command-line run). All measured on 2026-10-25 on one machine,
demo drive `bf394b9bfb42`, 1,000 frames of 1,000,000 points, row groups of 50 frames: the latest run took 44.7 s for the
first ingest into a fresh lake and 43.6 s for a re-ingest, with a peak working set of 1,898 MiB
(log `.claude/run-proofs/TASK-4-measure-iter2b.log`). An earlier run measured 45.0 s, 42.9 s and
1,718 MiB (`.claude/run-proofs/TASK-4-measure.log`). One first run in
`.claude/run-proofs/TASK-4-measure-iter2.log` took 3,730 s (its re-ingest took 44.5 s); CPU time
was only about 54 s while it ran. The cause is unknown and it was not reproduced in two later runs,
so it is an open outlier: the next full-density run should log free memory and disk. A frame scan
of 91 s and 1.59 GiB before a speed-up was measured once in a session; the output was not saved to
a log. These are single measurements, not a benchmark suite.

A drive folder is treated as hostile input. Before anything large is allocated: the manifest's points
per frame must not exceed `max_points_per_frame` (2,000,000 points); the lattice is built only after
`verify_drive` passes; the Parquet footer may claim at most 2 x expected frames + 1,000 rows (in total,
per row group, and as the number of row groups); the scan stops if it streams more rows than the
footer claimed; `scenario_hash` must be 64 lowercase hex characters; and stored quarantine text is
cleaned of control characters, cut to 300 characters, and limited to 20 problems plus one summary row.
Known limits: the row-group byte cap trusts the footer's size, which a compression bomb can lie about;
the folder is assumed not to change during an ingest; `manifest.json` and linked data files are read by
core code with no size cap (a follow-up for the generator session). The ingest opens only
`frames.parquet`; any other file listed in the manifest is hashed in full by `verify_drive` and never opened.

#### ETL settings and keypoints (built)

| Module | What it holds | Rules worth knowing |
|---|---|---|
| `etl/config.py` | `EtlConfig`, `KeypointSpec`, `LimitsConfig`, `load_etl_config` | Read from `config/etl.yaml`. Unknown keys are rejected. Keypoint names use letters, digits and `_` (up to 64 characters). A keypoint index must be a real integer, so `true` or `9.0` is refused. `max_jump_cm(fps, frame_gap)` is `max_speed * frame_gap / fps + margin`, with the margin added once: 564.5 cm for one frame at 16 fps and 1,127.0 cm for two (checked by a test). Bounds: speed at most 100,000 cm/s, 256 keypoints, 1,024 MiB per chunk. A chunk is sized in MiB of point data (`chunk_mib`, default 128), not in frames: `chunk_frames(points_per_frame)` gives 11,184 frames at 1,000 points per frame and 11 frames at 1,000,000 (checked by a test). The chunk is the batch size asked of Parquet; it does not bound memory, because pyarrow reads a whole row group. `max_row_group_mib` (default 2,048) is the largest uncompressed row group the ingest will read; a larger one rejects the drive. A corner keypoint is written as its bits (x, y, z), each 0 or 1, so the same list works at any density. `VL_LAKE_DIR` overrides the lake folder when it is not empty. |
| `etl/keypoints.py` | `extract_keypoints` | Input is frames x points x 3 integers (cm). Output is frames x keypoints x 3 float64 (cm), in the order of the keypoint list. A corner is picked by lattice index; the centroid is the float64 mean of all points. Float input and out-of-range indices are refused. |
| `config/etl.yaml` | the nine keypoints, speed limit 9,000 cm/s, margin 2 cm, chunk size 128 MiB, row-group cap 2,048 MiB | The built-in defaults in `etl/config.py` are identical to this file (checked by a test). |

The corner lattice indices are derived from the number of points per axis n as `bx*(n-1)*n*n + by*(n-1)*n + bz*(n-1)`: 0, 9, 90, 99, 900, 909, 990, 999 at n = 10, and 0, 99, 9900, 9999, 990000, 990099, 999900, 999999 at n = 100 (both checked by a test). The ETL gets n from the drive: `drive_body(manifest)` in `etl/body.py` reads the body (`size_cm`, `points_per_axis`) from `manifest.body` (schema version 2); it does not use the core config. A timing check on 2026-10-24 showed `extract_keypoints` takes 0.045 s for 4 frames of 1,000,000 points (48 MB) on the development machine (Windows 11, 15.7 GB RAM).

#### Frame scan and rules (built)

| Module | What it holds | Rules worth knowing |
|---|---|---|
| `etl/facts.py` | `scan_frames` | Streams `frames.parquet` in chunks and returns two things: one small record per row (frame number, timestamp, payload size, payload SHA-256, whether the cloud is valid, centroid) and the keypoints of every valid frame as a rows x keypoints x 3 array (NaN for an invalid frame). A bad payload never raises; a null frame number or timestamp, or a missing column, raises one `ValueError` (the caller checks the schema first). The size is checked before the payload is decoded. The deviation from the lattice is computed in int32 when that cannot overflow (int64 otherwise), the centroid is an exact integer sum divided by the point count, and the SHA-256 runs in a second thread (the speed-up was measured once in a session, not saved to a log). |
| `etl/ingest.py` | `ingest_drive`, `IngestResult` | The entry point. Runs the gates, the scan, the rules and the three table swaps (next section). |
| `etl/rules.py` | `RULES`, `RuleContext`, `run_rules` | Six rules with their codes, meaning, detection text and example fault. Drive rules (VL-005) and frame rules (VL-001 to VL-004, VL-006). VL-006 runs only on frames that passed the others. |
| `etl/body.py` | `drive_body`, `expected_frame_count` | `drive_body(manifest)` returns the body from `manifest.body`; `expected_frame_count(manifest)` is the number of frames the scenario implies, used by VL-001. |

On 2026-10-25 (final state of this branch) the ETL tests numbered 284 and the whole suite 649 (645 passed, 4 skipped in 64 s, rerun on 2026-10-26 for the final docs pass; the 99% coverage figure is the Loader session's and was not re-measured then). At the earlier ingest milestone the same day the ETL tests numbered 214 (481 in the whole suite, after the hostile-folder repair pass), and line coverage of the ETL package was 99% (`ingest.py` and `facts.py` 100%; the two uncovered lines are in `rules.py`). Ingesting the demo drive on a 1,000-point test body kept 1,000 of 1,000 frames and wrote 9,000 keypoint rows (checked by a test); each of the five frame faults gave exactly one quarantine row; a changed byte, a missing manifest, a schema-1 manifest, a renamed drive folder and a renamed points column each rejected the whole drive; ingesting twice left all three table files byte-identical.

On 2026-10-24 the ETL package had 151 tests (381 in the whole suite), and line coverage of the ETL package was 99%. Running the rules on the demo drive (1,000 frames on a 125-point test body) gave no violations, and each of the six generator faults produced exactly its own code. The full-density behaviour of the scan is not measured yet.

On 2026-10-23 the settings and keypoint modules had 71 tests (201 in the whole suite), and line coverage of the ETL package was 100%.

What goes in: a drive folder `data/raw/<hash12>/` with the full point cloud (1,000,000 points per
frame at the shipped density). What comes out: three tables in the lake, `keypoints`, `quarantine` and `lineage`, each
stored as `<table>/drive=<hash12>/part-0.parquet` and queryable with DuckDB.

Steps for one drive:

1. **Gates, cheapest first.** The folder name must be a valid drive id (otherwise `ingest_drive`
   raises `ValueError` and writes nothing). Then: the manifest reads; the folder name equals the
   first 12 hex of the scenario hash (after a check that the hash is 64 lowercase hex characters);
   the schema version is in the supported list; the fps divides a second; the points per frame are
   within `max_points_per_frame`; the body and scenario are valid; `frames.parquet` is listed; the
   keypoint specs fit the drive's point count (a spec that does not is a configuration error and
   raises `ValueError`, ASSUMPTIONS M2-A34); and only then every file hash is checked
   (`verify_drive`, the slow step), followed by the lattice build. Any failure rejects the whole
   drive before a frame is read. The frame rate and frame spacing come from the manifest, not from a core config.
2. **Schema gate.** Open the frames file (an unreadable file rejects the drive), refuse any row
   group above `max_row_group_mib` (uncompressed), and compare the columns with the agreed frames
   schema (VL-005).
3. **One scan.** Read the frames file once, in chunks. For each frame keep a small record: frame
   number, timestamp, payload size, payload hash, whether the points form the expected cube, and
   the centroid. For each valid frame the scan also picks the keypoints (8 corners by index and
   the centroid) into a small array, so the file is not read a second time. A frame number outside
   0 to expected count minus 1 rejects the drive. A scan that raises (null frame number,
   mistyped column, unreadable Parquet) rejects the drive with one `INTEGRITY` row.
4. **Rules.** Run VL-001 to VL-004 on the records. Run VL-006 only on frames that passed those.
   VL-006 first picks a trusted starting frame: among the first three clean frames it takes the
   pair that agrees most tightly (distance as a fraction of the allowed jump) and starts from the
   earlier frame of that pair, so a teleported first frame cannot poison the rest. After that it
   compares each centroid with the last accepted frame.
5. **Reduce.** Keep the keypoint rows of the accepted frames (taken by file row, so a repeated
   frame number cannot mix them up), sorted by frame number and then by the order in
   `config/etl.yaml`, and write them as one batch. The file bytes therefore do not depend on the
   chunk size.
6. **Write.** Write each table's new partition aside, then swap it in for the old one, in this
   order: keypoints, quarantine, lineage. Lineage goes last and acts as the commit marker. A
   rejected drive drops its keypoints first, then writes its quarantine rows and a lineage row
   with status `rejected` and zero counts. The lineage row holds the scenario hash, the schema
   version, the counts and the manifest hash of the frames file (taken from
   the manifest after the files were verified). A keypoint
   spec that does not fit the body is a configuration error: `ValueError`, nothing written.

| Code | Rule in short |
|---|---|
| VL-001 | A frame number from 0 to the expected count minus 1 is absent. One row per missing number. The expected count comes from the scenario stored in the manifest (its hash is verified), not from `manifest.frame_count`, which counts the rows of the file. |
| VL-002 | The timestamp is not `frame_id x 62,500,000 ns`. |
| VL-003 | A frame number appears twice. Identical copies keep the first. Differing copies are all quarantined. |
| VL-004 | The payload is not a multiple of 12 bytes, does not hold the drive's points per frame, or a point is more than rounding away from the expected lattice (per axis, the offsets from the lattice may spread by at most 1 cm). A single point moved by exactly 1 cm is not caught. |
| VL-005 | The frames file's columns differ from the agreed schema. Rejects the drive. |
| VL-006 | The centroid is farther from the last accepted frame than 9,000 cm/s allows (about 564.5 cm per frame, with a 2 cm margin). |

The speed limit tests position only. Per-frame acceleration is too noisy to test (a per-frame
error of 452 cm/s^2 was reported by a throwaway run; that figure is not logged and is not evidence).

#### What "not atomic" means for the three tables

A re-ingest replaces the drive's partition in `keypoints`, in `quarantine` and in `lineage`. Each
replacement is safe on its own: the new data is written aside, the old folder is renamed out of
the way, the new folder is renamed in, and the old one is deleted. If the new data fails while
being written, the old partition stays. But the three tables are replaced one after another,
and plain Parquet has no commit that covers all three. A crash after the keypoints swap and
before the lineage swap leaves new keypoints next to the old lineage row. Ingesting the drive
again fixes it, because the ingest is idempotent.

#### Motion model (built)

A scenario holds a start state (position in cm and speed in cm/s) and a list of segments. A
`move` changes the speed in a straight line to an end speed (a negative speed means backward).
A `stop` holds speed 0 and is allowed only when the vehicle is already at rest, so speed never
jumps. Durations are in seconds and must be a whole number of frames (a multiple of 1/16 s).
A scenario may have at most 2,000 frames (125 s at 16 fps).

From a scenario, `motion.py` builds, frame by frame:

| Step | Rule |
|---|---|
| Speed | A move of n frames from speed a to speed b gives `a + i * (b - a) / n` at frame i, so the end speed is reached on the first frame of the next segment. A stop gives 0. |
| Position | Forward Euler along x: `x[0]` is the start, `x[k+1] = x[k] + speed[k] / 16`. |
| Ground-truth velocity | `(x[k+1] - x[k]) * 16`; the last frame repeats the previous value. |
| Ground-truth acceleration | `(speed[k+1] - speed[k]) * 16` from the speed profile; the last frame repeats the previous value. |
| Points | `place_body` returns `rint(lattice + [x, y, z])` as int32 cm. The lattice has 100 x 100 x 100 points on 0, 1, ... 99 cm, x-major, so the 8 corners are at indices 0, 99, 9,900, 9,999, 990,000, 990,099, 999,900, 999,999. |

Real values for the demo scenario (run on 2026-10-24 at 1,000,000 points per frame): 1,000 frames in 8 segments; speeds from
-300 to 600 cm/s; acceleration between -100 and 100 cm/s^2; final position 6,450.0 cm (run on 2026-10-23); drive
hash `bf394b9bfb421fda860ab97fbab54d5c36c995a661e770969cbffd9a2afa95c1`, so its drive folder
is `bf394b9bfb42`. A 1 cm move on x shifts every point by exactly (1, 0, 0) when positions are
whole centimetres.

### Design choices and alternatives

| Decision | Choice | Why | Alternatives | When I would change it |
|---|---|---|---|---|
| Motion model | Straight line along x, signed speed, no rotation | A cube's orientation cannot be read from its points | Planar motion, full rotation with a non-symmetric body | The real signal rotates |
| Position estimate | Centroid of a frame's points | Exact for pure translation, needs no matching between frames | Tracking one reference point | Rotation or a changing point set |
| Body | 1 m cube, 100 x 100 x 100 = 1,000,000-point lattice | Matches the assignment's example density; the PDF allows fewer frames, not thinner ones | A 10 x 10 x 10 sample (the earlier choice), a car-sized box | Storage or time limits |
| Source | Generator writes a drive to disk | Simplest valid source | A service or a stream | Many consumers or live data |
| Config | YAML plus a validating Python loader | Readable, fails early on bad values | Environment variables only, JSON | Many deployment environments |
| Scenario validation | pydantic | Typed models with clear errors | dataclasses plus hand checks | Never needed so far |
| Hashing | SHA-256 over canonical JSON (body and scenario) and file bytes (files) | Standard, checkable with common tools | Content hashes, UUIDs | Needing stability across library versions |
| Raw file format | Parquet via PyArrow, one row per frame | Columnar, compact, readable by every tool | One row per point, CSV, HDF5 | Per-point SQL |
| Query | DuckDB over Parquet folders | SQL with no server | Trino, Athena | Many users, large data |
| ETL engine | pandas and numpy | Data is small, code is clear | Polars, Spark | Data no longer fits one machine |
| Lake format | Plain Parquet, Hive-style folders, behind one class | Nothing to run | Apache Iceberg, Delta Lake | Many writers, schema changes, time travel |
| Idempotent writes | Write to a temp folder, then swap the drive folder | Re-running never duplicates, a crash never loses old data | Blind append, upsert | Concurrent writers |
| Validation | Rule registry with error codes | Explicit and testable; the same registry builds the error-code document | Great Expectations, pandera | Many datasets and owners |
| Keypoint table layout | Long: one row per keypoint | Generic; the keypoint list is a YAML change; simple SQL filter by name | Wide: 27 coordinate columns per frame | Heavy column maths on the corners |
| Bad frames | Quarantine only the failed frame, with a details-only row | One bad frame does not cost the drive; the raw frame stays in `data/raw/` | Reject the whole drive; copy the raw bytes | A consumer needs replay from the lake alone |
| Ingest passes | One chunked pass: facts and keypoints together (user decision 2026-10-25) | Bounded memory, rules see the whole sequence from the facts, and the frames file is decoded once instead of twice | Two passes: facts, then keypoints | A rule needs cloud data that the stored facts do not hold |
| BI | Streamlit and Plotly | One Python file per view | Superset, Grafana | Non-Python users |
| CLI | Typer | Typed commands | argparse | None |
| Tests | pytest and hypothesis | Property tests suit a two-way check | unittest | None |
| Not used | Kafka, Spark, Airflow, dlt, Docker | No purpose at this scale | See "Scaling" | See "Scaling" |

### How the simulator is checked

The generator is tested in both directions. Forward: scenario to frames. Back: `infer_track`
recovers per-frame position and velocity from the frames, `fit_segments` fits move and stop
pieces to them, and `compare_scenarios` checks the result against the original within stated
limits. The back direction reads frames only, never ground truth.

Limits measured on the demo drive, from two different runs, and the limits set in
`config/default.yaml`.

(a) 2026-10-24, on the old 10x10x10 body (run log `.claude/run-proofs/TASK-5-measure.log`, local,
git-ignored (A28), from `.claude/run-proofs/TASK-5-measure.py`). These two were not re-measured at
full density:

| Quantity | Measured max | Test limit |
|---|---|---|
| Position error (x), centroid | 0.498 cm | 0.6 cm |
| Velocity error per frame | 15.0 cm/s | 17 cm/s |

(b) 2026-10-25, at full density (1,000,000 points per frame), printed by `python -m vl_data
roundtrip` in the fresh-clone run 1 (`.claude/run-proofs/FINAL-DOCS-iter1-fresh-clone.log`, local,
git-ignored, A28):

| Quantity | Measured max | Test limit |
|---|---|---|
| Segment boundary error | 4 frames | 6 frames |
| Segment duration error | 5 frames | 8 frames |
| End-speed error | 24.4 cm/s | 40 cm/s |

The earlier figures in `architecture_context.txt` (0.66 cm and 17.6 cm/s; not logged, not evidence) came from a planar
variant that is not in the repo; the 1-D numbers above come from the logged run (0.498 cm,
15.0 cm/s). The developer's throwaway run also reported a per-frame acceleration error of
452 cm/s^2, larger than the demo's own signal (up to 100 cm/s^2), and 6.8 cm/s^2 when fitted over
16-frame windows; those two figures are not logged and are not evidence. The rounding of the
points to whole centimetres is the source of the logged errors.

How the back direction works (`generator/inverse.py`): `infer_track` takes the mean x of each
frame's points minus the lattice centre; velocity is the forward difference times 16. `fit_segments`
splits the velocity series into linear pieces by penalised optimal partitioning (noise sigma 12 cm/s,
penalty 40, pieces of at least 8 frames), extrapolates each line to its piece boundary for the end
speed, and calls a piece a stop when its speed stays under 30 cm/s. A snap rule then makes the
result a valid scenario (stops at 0 cm/s, the move before a stop ends at 0, a 0 -> 0 move is merged
into the stops; ASSUMPTIONS A53). The demo gives 8 segments of the right types.

Search domain of the property test (ASSUMPTIONS A14; see `_scenarios()` in
`tests/generator/test_inverse.py`): up to 7 draws that each add a segment or are skipped, at most
1,600 frames, 32 to 160 frames per segment, move end speeds on a 10 cm/s grid within +-600 cm/s,
a nonzero end speed of at least 100 cm/s in size, stops only at rest, and a slope change of at
least 50 cm/s^2 from the previous segment. The running slope starts at 0, so a first move also
needs a slope of at least 50 cm/s^2 in size. One exception: when every draw is skipped, the
strategy falls back to a single 16-frame stop, below the 32-frame minimum.
Logged runs outside the domain (local, git-ignored, A28): 16-frame segments 0 -> 460 -> -180 -> 0
gave an end-speed error of 42.4 cm/s, 24-frame segments 0 -> -460 -> 490 -> 0 gave 40.8 cm/s
(limit 40), and 0 -> 100 -> 110 cm/s at 16 frames each (slope change 90 cm/s^2) was fitted as one
piece, so shorter segments are not covered. A 3,000-example stress run was reported but is not
logged; only the committed 40-example derandomized test is evidence.
Decision (2026-10-24, user): keep this 32-frame domain (ASSUMPTIONS A14). `roundtrip` therefore
prints a `warning:` line on stderr for every segment under 32 frames
(`MIN_RELIABLE_SEGMENT_FRAMES`).

### Fault injection

`generator/faults.py` builds bad drives on purpose so Module 2 can be tested against known errors.
`inject_faults(drive, faults, frame_interval_ns=62_500_000)` returns a new `Drive` and a dict of
counts per error code. Only the frames table changes; the ground truth, the scenario and the
scenario hash are copied unchanged, and the input drive is not modified. A faulty drive can be
written with `write_drive` and still passes `verify_drive`, because the manifest is built from the
faulty file.

| Fault | Code | What it does to the frames table |
|-------|------|----------------------------------|
| `DropFrame(frame_id)` | VL-001 | Deletes that row. The other frame ids are not renumbered. |
| `ShiftTimestamp(frame_id, delta_ns)` | VL-002 | Adds `delta_ns` (ns) to that frame's `timestamp_ns` only. |
| `DuplicateFrame(frame_id)` | VL-003 | Inserts a copy of the row right after the original (same frame id and timestamp). |
| `CorruptPoints(frame_id)` | VL-004 | Removes the last point (12 bytes) from that frame, so its length is (n - 1) x 12 bytes. |
| `BreakSchema()` | VL-005 | Renames the column `points` to `pts`. |
| `Teleport(frame_id, dx_cm)` | VL-006 | Adds `dx_cm` (cm) to the x coordinate of every point of that frame only. |

Rules (ASSUMPTIONS A59 to A62): a fault finds its row by the `frame_id` value in the original drive,
so an earlier drop or duplicate never moves the target. The returned dict holds only the codes that
were injected, and each count is the number of fault objects with that code. These inputs raise
`ValueError` instead of being applied: repeated `frame_id` values in the original table; an unknown
`frame_id`; two faults on the same `frame_id` (including the same fault twice); a second
`BreakSchema`; a `delta_ns` that is not an int, is 0, leaves int64 or is not smaller than one frame
interval (`frame_interval_ns`, ns) in size; `dx_cm == 0`; an input table whose own timestamps are
equal or not increasing; final timestamps of the surviving rows that are not strictly increasing in
original row order (dropped rows and `DuplicateFrame` copies are left out); `CorruptPoints` on a
frame with under 12 bytes; and a `dx_cm` that overflows int32. Each fault is intended to trigger
its own code. Module 2 is checked against all six: each of the six faults is covered by
`tests/etl/test_e2e.py` and `tests/etl/test_ingest.py`, which ingest a faulty drive and expect its code.

### Generate tab

`generator/ui/generate_tab.py` draws the tab and `generate_app.py` is a short runner (two imports
and one call; `streamlit run src/vl_data/generator/ui/generate_app.py`); the three-tab shell belongs to the BI
module. `rows_to_scenario` turns the form values and the table rows into a `Scenario` (`float()` on
every number, stop rows lose `end_speed_cm_s`, a blank, NaN, infinite or true/false
cell, an empty table raises a clear `ValueError`; the seed is not an input, the `Scenario`
default 0 is used; ASSUMPTIONS A74, A80, A81). `preview_figure` plots speed (cm/s) against time (s) from `motion.speed_profile`. A wrong form or table shows
`st.error` and no plot.

The Generate button runs these steps in order (ASSUMPTIONS A70 to A80):

1. Work out the drive hash and the target folder (`raw_dir/<hash12>`). One `try` block runs
   `check_batch_fits`, `scenario_hash` and `frame_count`; a `ValueError` from any of them (a body
   whose 50-frame batch cannot fit one Arrow binary array, a fractional body in the hash, or the
   frame count) is shown with `st.error` as "Cannot generate: ...".
2. Skip an existing drive: `_verified_manifest` accepts the folder only if it is a real folder (not a
   symlink), `verify_drive` passes in full (every file is hashed again, in an `st.status` block) and
   its manifest has the same hash. The tab then says "already generated", shows the folder, hash and
   frame count, and writes nothing. A folder that fails the check is written again.
3. Disk guard: `_free_bytes` reads the free bytes of the `raw_dir` disk (or its nearest existing
   parent). If the number of frames x the bytes per frame that `check_batch_fits(config)` returns
   (an uncompressed upper bound, nothing credited back for an existing folder) is larger, the tab
   shows `st.error` and writes nothing.
4. Otherwise it shows a warning with the frame count and that size, then calls
   `generate_to_disk(scenario, config, config.raw_dir)` inside an `st.status` block. This is the
   function the CLI calls, so both give the same drive (tested by comparing the scenario hash and
   every file hash). The writer refuses a symlink or a plain file at the target path and replaces
   nothing; the tab shows its error ("Cannot write drive: ..."). The disk guard (step 3) runs
   before the writer, so with a full disk the user sees the disk error first.

The 2,000-frame cap stays in `Scenario`. Two clicks at once for a drive that does not exist yet can
both write (ASSUMPTIONS A79). Streamlit's test runner cannot edit the table, so only the default demo
rows are tested (`tests/generator/test_ui.py`, ASSUMPTIONS A73, A75).

### Error handling

Integrity errors reject the whole drive, as does VL-005. They are: a changed, missing or extra
file (one `INTEGRITY` row per problem), an unreadable manifest or a schema-1 manifest without a
body, an unsupported schema version, a folder name that is not the first 12 hex of the scenario
hash, an fps that does not divide a second, an invalid body or scenario, a frames file that is not
listed or cannot be read, a Parquet row group above `max_row_group_mib`, and a frame number outside
0 to expected count minus 1. A rejected drive gets its drive-level quarantine rows, a lineage row
with status `rejected`, and no keypoints. A folder name that is not a valid drive id and a keypoint
spec that does not fit the body raise `ValueError` and write nothing. Frame errors VL-001 to VL-004 and
VL-006 send only the failed frame to quarantine. See [ERROR_CODES.md](#1-error-codes).

### Storage math

A full-density frame is 100 x 100 x 100 = 1,000,000 points. At 3 coordinates of 4 bytes each
that is 12 MB per frame, 192 MB per second at 16 fps, and about 691 GB per hour (computed). The
1,000-frame demo is therefore 12 GB before compression (computed) and 287,134,433 bytes on disk with zstd (measured), about 42 times smaller (computed). A production design would store a keyframe and the ego-motion change
per frame instead of every point.

### Scaling to production

Apache Iceberg or Delta on object storage with a managed catalog, Spark or Flink for large
drive counts, keyframe-plus-delta storage, schema contracts between producer and consumers,
partitioning and compaction, and scheduled runs with an orchestrator.

### Limits of this design

- No rotation. A real vehicle rotates, and reading its orientation needs a non-symmetric body
  or tracked points.
- Plain Parquet has no atomic commit across files, no schema-evolution tracking and no time
  travel. The folder swap protects one drive at a time and is not safe for concurrent writers.
- File hashes depend on the pyarrow version that wrote the files.
- A re-ingest swaps three tables one after another, not as one atomic commit (see the section on
  what "not atomic" means above). Re-ingesting repairs a crash.
- The speed limit of 9,000 cm/s is a Tesla's top speed (322 km/h, 8,944 cm/s) rounded up. A
  faster vehicle would be flagged as impossible motion.
- Tolerances come from one demo drive and may widen when the property tests run.

The full assumption list is in [ASSUMPTIONS.md](ASSUMPTIONS.md).

## 3. Release notes

Status as of 2026-10-25: the project skeleton, the generator (Module 1, with its Generate tab) and
the loader (Module 2) are built and tested; the use-case algorithm (Module 3) and the Clean and
Analyze tabs (Module 4) are not. This draft is filled in as each module lands, and
every command and number in it must come from a real run before submission. Items marked
"planned" have not been run.

### Product description

A small data product for a vehicle-location signal. It generates a mock drive from a start
position and a motion profile, stores the point cloud, extracts the vehicle's velocity and
acceleration from the points alone, and shows the result next to the known truth. The vehicle
is a 1 m cube that moves in a straight line, forwards, backwards and stopped, at different
speeds. All data is synthetic.

### What is in this version

| Part | What it does | Status |
|---|---|---|
| Generator | Writes a hashed drive: 16 fps point cloud, ground truth, manifest. Checks itself by recovering the scenario from the data. | Built and tested (Module 1) |
| Loader / ETL | Verifies hashes, validates frames with error codes VL-001 to VL-006, keeps 9 keypoints per frame, and writes keypoints, quarantine and lineage tables to a queryable lake. | Built and tested (Module 2) |
| Use-case algorithm | Computes velocity and acceleration and compares them with ground truth. | Planned |
| BI | One Streamlit app: generate, clean, analyze. | Generate tab built (Module 1); Clean and Analyze planned |

### Install and first check (run on 2026-10-25, Windows PowerShell, Python 3.12.6, fresh clone and new virtual environment)

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -m vl_data --version
```

The last command prints `vl_data 0.1.0` and exits with code 0. The install step took 222 s (measured;
pip took most packages from its cache and downloaded three (hypothesis, starlette, certifi; the log has "Using cached" and "Downloading" lines), so this is not a cold download time)
and the run resolved pyarrow 26.0.0, numpy 2.5.4, pandas 3.1.0, pydantic 2.14.1, plotly 7.1.0 and
streamlit 1.66.1 (measured, `pip freeze` in `.claude/run-proofs/FINAL-DOCS-iter1-fresh-clone.log`,
local, git-ignored, A28; the pins in `pyproject.toml` are minimums, so a later install may resolve
newer versions). `.[dev]` adds the test tools (pytest 9.1.1, pytest-cov 7.1.0, hypothesis 6.170.9,
ruff 0.17.2, mypy 2.4.0; measured, same `pip freeze`); streamlit, plotly and pandas are main
dependencies. Running `python -m vl_data` with no
arguments prints the help text and exits with code 2. Python 3.11 or newer is required; only
3.12 has been tested.

### Usage

Run these with the project's virtual environment active, or call `.venv\Scripts\python.exe` in place of `python`.

Generate the demo drive (1,000 frames, one reversal, two stops). The times of this command vary a
lot on this machine: the full-density `generate` (with verification) took 237.7 s in the
fresh-clone run of 2026-10-25 on the Loader branch (`FINAL-DOCS-M2-fresh-clone.log`, read here;
other programs were running), and 81 s in an earlier run (the 81 s is
`FULL-DENSITY-iter2.log`, `duration_s=81`; that log is not in this checkout; the 75.1 s in
`FULL-DENSITY-measure.log` is a scratch-script write without verification, not this command). The
spread is unexplained, so treat 81 to 238 s as the range seen, from single runs. This Usage section owns the
measured run figures; other documents point here. The Generator branch's own fresh-clone check
(`FINAL-DOCS-iter1-fresh-clone-run1.log` and `FINAL-DOCS-iter1-fresh-clone.log`, 86.6 s for the same
command; the 10.35 s of the Generate-tab UI tests comes from `TASK-7-1-1-iter1-ui-tests.log`) is
quoted below where noted. These logs are in `.claude/run-proofs/` (local, git-ignored, A28; the
logs of the Generator branch may exist only in its own checkout). A second Generator-branch run
(`FINAL-DOCS-iter2-fresh-clone.log`) passed all steps, but its timings were unreliable and are not
used:

```powershell
python -m vl_data generate --scenario src/vl_data/generator/scenarios/demo.yaml --out data/raw
```

It prints:

```text
frames: 1000
timestamp_spacing_ns: 62500000
points_per_frame: 1000000
scenario_hash: bf394b9bfb421fda860ab97fbab54d5c36c995a661e770969cbffd9a2afa95c1
drive_dir: data\raw\bf394b9bfb42
manifest: verified
```

The folder `data/raw/bf394b9bfb42/` then holds `frames.parquet` (287,134,433 bytes, zstd),
`ground_truth.parquet` (30,929 bytes) and `manifest.json` (1,454 bytes). The sizes are from the Loader-branch fresh-clone run (measured, `FINAL-DOCS-M2-fresh-clone.log`). The 1,000-frame drive holds 12 GB
(here 12,000,000,000 bytes) of
points before compression (computed: 1,000 frames x 12,000,000 bytes per frame). `--out` is optional;
without it the drive goes to `raw_dir` from `config/default.yaml` (or `VL_RAW_DIR`). Generating the same
scenario again replaces the folder with one clean folder that passes `verify` (test
`test_rewrite_replaces_folder` in `tests/generator/test_generator.py`; the files have the same bytes
on the same pyarrow version, from the code and A63, not run in the fresh-clone check).

Check a drive against its manifest:

```powershell
python -m vl_data verify data/raw/bf394b9bfb42
```

This prints `manifest: verified` and exits with code 0 (2.3 s in the Loader-branch fresh-clone
run, measured). After one byte of `ground_truth.parquet`
was changed in a copy, it printed `changed: ground_truth.parquet (sha256 mismatch)` on stderr and
exited with code 1 (measured in the same log, `FINAL-DOCS-M2-fresh-clone.log`; its first attempt
changed nothing because of a path bug in the check script, and the log says so). A missing scenario file (`generate` and `roundtrip`) or an
invalid one (`generate`) gives a one-line `error: ...` message and exit code 1 (`generator/cli.py` writes it to stderr; the tests mostly check the exit code and the message text, not the stream: tests
`test_generate_missing_scenario`, `test_generate_invalid_scenario` and
`test_roundtrip_missing_scenario_exits_1` in `tests/generator/test_cli.py`; not run in the
fresh-clone check).

Check that the generator can read its own drive back (the two-way check). Run on 2026-10-25,
it took 56.1 s (and 54 s in an earlier run on the same clone) at full density (1,000,000 points
per frame; measured, `FINAL-DOCS-M2-fresh-clone.log`; the full-density run of 2026-10-24 took 70.0 s, ASSUMPTIONS A15, so one machine gave 54 to 70 s
in single runs; a time of 1.41 s was measured at the old 10-points-per-axis density and does not
apply) and writes nothing to disk:

```powershell
python -m vl_data roundtrip --scenario src/vl_data/generator/scenarios/demo.yaml
```

It prints:

```text
segments: expected 8, found 8
max_boundary_error_frames: 4
max_duration_error_frames: 5
max_end_speed_error_cm_s: 24.4
round trip OK
```

The fresh-clone log records the exit code (0) and these five printed lines. They are the full-density
figures (2026-10-25). The demo-drive errors against ground truth (maximum centroid x error 0.498 cm,
maximum velocity error 15.0 cm/s) are a separate, older measurement, measured at the old
10-points-per-axis density: 2026-10-24, with `.claude/run-proofs/TASK-5-measure.py` (log
`TASK-5-measure.log`) on the old 10x10x10 body; they were not re-measured at full density.

The limits are 6 frames, 8 frames and 40 cm/s (`tolerances` in `config/default.yaml`). If a
check fails, the segment line and one line per problem are printed (problems on stderr according to `generator/cli.py`) and the
exit code is 1 (test `test_roundtrip_failed_comparison_exits_1` in `tests/generator/test_cli.py`,
which uses a stand-in failed comparison; not run in the fresh-clone check).

The check is only tested for segments of 32 frames (2 s) or more (A14). For every segment under
32 frames the command prints one line such as `warning: segment 1 is 16 frames; ...` on stderr
before the result. The demo prints no warning, because its shortest segment is exactly 32 frames.

A scenario file looks like this:

```yaml
start: {x_cm: 0, y_cm: 0, z_cm: 0, speed_cm_s: 0}
segments:
  - {type: stop, duration_s: 2.0}
  - {type: move, duration_s: 6.0, end_speed_cm_s: 600}
  - {type: move, duration_s: 8.0, end_speed_cm_s: -300}
  - {type: stop, duration_s: 4.0}
```

The optional `seed` key (a whole number, default 0) is still accepted in a scenario file.

Speeds are cm/s and positive means forward. `move` changes speed in a straight line from the
current speed to `end_speed_cm_s`. Durations must be a whole number of frames (a multiple of
1/16 s).

Generate a drive from the browser (the Generate tab). Run on 2026-10-25 (Windows, Python 3.12.6,
Streamlit 1.66.1); only the server start and an HTTP check were run: the server answers, the page was
not rendered or opened in a browser by hand:

```powershell
python -m streamlit run src/vl_data/generator/ui/generate_app.py --server.headless true --server.address 127.0.0.1 --server.port 8765
```

`--server.address 127.0.0.1` binds the page to this computer only; `--server.port 8765` is the port
that run used. Streamlit printed `Uvicorn server started on 127.0.0.1:8765` and `URL: http://127.0.0.1:8765`;
the server answered `GET /` with HTTP 200 and `/_stcore/health` with `ok` (measured, in the fresh-clone
log; an earlier run: `.claude/run-proofs/TASK-7-iter3-server.log`, local, git-ignored, A28). The page has a
start form (`x_cm`, `y_cm`, `z_cm`, `speed_cm_s`; there is no seed box, the seed is hidden and is 0 for drives made in the tab (a scenario file can still set it); this description of the page comes from the code, not from a browser run), a segment table that starts with the
demo rows (`type`, `duration_s` in steps of 0.0625 s, `end_speed_cm_s`; rows can be added and
deleted), a speed-against-time plot, and a Generate button. Click Generate and the tab first works out the target folder (the first 12 characters of the scenario hash under `raw_dir`). If
that folder already holds a drive that passes the full `verify_drive` check (every file is hashed again, shown in a
"Checking existing drive..." status block) and whose manifest has the same hash, the tab shows "already generated" with
the folder, hash and frame count and writes nothing; a folder that fails the check is written again and replaced. Next a
disk guard compares the size estimate below (frames x bytes per frame, uncompressed, an upper bound; nothing is credited
back for an existing folder) with the free bytes on the `raw_dir` disk (or its nearest existing parent folder if `raw_dir`
does not exist yet). If the estimate is larger, the tab shows a red error with both numbers and writes nothing. Otherwise
it shows a warning such as "This drive has 1000 frames, about 12,000,000,000 bytes (uncompressed point
data)" (1,000 frames x 12,000,000 bytes at full density, computed; the unit tests use a smaller body, so
their text shows smaller numbers). It then writes the drive under `raw_dir` with the same function
the `generate` command uses, and shows the folder, the scenario hash and the frame count. A
wrong form or table shows a red error and no plot. A bad cell (blank, text, true/false, not a number or infinite) names the segment and the field, for example "segment 0 duration_s must be a finite
number"; a bad start field names the field ("start x_cm must be a finite number"); a duration that
is not a whole number of frames gets the scenario model's own message after "Invalid scenario:" (for example
"duration_s 1.03 s is not a whole number of frames", from `generator/models.py`, worded differently from the cell
messages); an empty table or a table over 2,000 frames
gets its own message. If the target folder is a symbolic link or a plain file, the tab does not call it
"already generated"; it shows the writer's error and changes nothing (A76). The disk guard runs
before the writer, so with a full disk the user sees the disk error first. Two Generate clicks
at the same time for a drive that does not exist yet can still both write (the race is not closed, A79). The tab is tested with Streamlit's test runner
(`tests/generator/test_ui.py`, 49 tests, 48 passed and 1 skipped, in 10.35 s, measured in
`.claude/run-proofs/TASK-7-1-1-iter1-ui-tests.log`; the one skip is the real-symlink test, which needs the Windows symbolic-link right
and has a no-rights stand-in that ran; on the Generator branch at that time the whole suite was 332 tests, 331 passed and 1 skipped, 100.00% coverage, measured in `TASK-7-1-1-iter1-suite.log`, and its fresh-clone run on 2026-10-25 gave 331 passed, 1 skipped in 32.41 s, 100% coverage of 1,037 statements, with ruff and mypy clean (measured, `FINAL-DOCS-iter1-fresh-clone.log`). On this Loader branch, which also holds the ETL tests, the whole suite is 649 tests: 645 passed and 4 skipped in 64 s (measured again on 2026-10-26, output not saved to `.claude/run-proofs/`), with 284 of them in `tests/etl` (the Loader session's count) and 99% coverage (the Loader session's figure, not re-measured). The logs are local, git-ignored, A28), which cannot edit the table, so editing a row
by hand is not covered by a test.

Load the drive into the lake:

```powershell
python -m vl_data ingest data/raw/<hash12>
python -m vl_data ingest --all data/raw --lake-dir warehouse
python -m vl_data error-codes --check
python -m vl_data error-codes --write
```

`ingest` prints one `key: value` block per drive (drive, status, frames_expected, frames_in,
frames_kept, frames_quarantined, frames_missing, keypoint_rows, points_per_frame_in,
keypoints_per_frame_out, one `quarantine_<code>` line per code, elapsed_s). It exits 0 when every
drive was loaded (quarantined frames are normal), 1 when at least one drive was rejected, 2 on a
usage or configuration error (with `--all`, drives loaded before the error stay written), and 3
when at least one drive could not be read or written. `--all` takes only folders named by 12 hex
characters and prints `skipped: <name>` for anything else (`(link)` for a symlink, which is never
followed); a single folder must be named the same way. `error-codes --write` and `--check` need
`--path` outside a source checkout. The lake folder is `--lake-dir`, else
`VL_LAKE_DIR`, else `lake_dir` in `config/etl.yaml`, else `warehouse`. `error-codes --check` exits
0 when `docs/ERROR_CODES.md` matches the rule registry; `--write` regenerates it.

What was run (2026-10-25): the command-line and end-to-end tests on the small body (125 points
per frame, 1,000 frames; `tests/etl/test_cli.py`, `test_errors_doc.py`, `test_e2e.py`, 55 tests: 41 + 6 + 8),
which ingest through the root app and query the lake with DuckDB (9 keypoints per frame; each of
the six faults gives its code in the quarantine view); `python -m vl_data error-codes --write`
followed by `--check` (exit 0). The full-density command-line run was also done, twice (single runs, measured).
First (`generate` to `data/raw_proof`, then `ingest --all data/raw_proof --lake-dir warehouse`,
`.claude/run-proofs/TASK-5-iter2-a.log`): exit 0, `status: ok`, `frames_kept: 1000`,
`frames_quarantined: 0`, `keypoint_rows: 9000`, `points_per_frame_in: 1000000`,
`keypoints_per_frame_out: 9`, `elapsed_s: 43.7`; about 120 s of wall-clock time for generate plus
ingest. Second, from a fresh clone (`FINAL-DOCS-M2-fresh-clone.log`): `ingest --all data/raw
--lake-dir warehouse` gave the same counts with `elapsed_s: 49.1` (wall 50.9 s), and
`ingest data/raw/bf394b9bfb42 --lake-dir warehouse` gave `elapsed_s: 45.8` (wall 47.4 s); `generate`
in that run took 237.7 s. The Generate tab started in that run too: HTTP 200 on `/` and `ok` on
`/_stcore/health` at 127.0.0.1:8765. Three tests need an OS or account
that can create symlinks or odd folder names and were skipped on this machine.

Query and dashboard examples for the use case and the app will be added when Modules 3 and 4
exist.

### Known issues

- No rotation. The vehicle never turns or yaws. A real vehicle rotates.
- A drive at full density is large: 12 GB (12,000,000,000 bytes) of points per 1,000 frames before compression (computed; 287 MB on disk with zstd, measured), and generating and round-tripping it take 65 to 238 s (generate) and 54 to 70 s (round trip) in single runs on one machine, with an unexplained spread (see Usage). `generate()` and `write_drive` hold a whole drive in memory; use the `generate` command (it streams). A drive over 1 GiB (2^30 = 1,073,741,824 bytes) of points (more than 89 frames at full density) is refused by `generate()` and `write_drive`.
- A scenario may have at most 2,000 frames (125 s at 16 fps). At full density that is about 0.6 GB on disk and about 2.5 minutes to generate (computed: twice the 75.1 s of the 1,000-frame write in `FULL-DENSITY-measure.log`, not run). The cap is a validation limit in `generator.models`.
- Motion is one-dimensional (along x).
- Values are rounded to whole centimetres, which adds small errors. Per-frame acceleration is too
  noisy to use directly, so it is fitted over windows.
- File hashes only match when the same pyarrow version wrote the files.
- Plain Parquet has no atomic multi-file commit, no schema-evolution tracking and no time travel.
  Writing a drive is not safe for concurrent writers.
- Test tolerances come from one demo drive.
- The two-way check (`roundtrip`) is only tested for segments of 32 frames (2 s) or more. Shorter
  segments can merge into one fitted piece or miss the end-speed limit; the command warns.
- Loading a drive replaces its keypoints, quarantine and lineage partitions one after another, not
  as one atomic step. A crash in between is repaired by loading the drive again.
- The lake is single-writer: load one drive at a time. A failed partition restore leaves the old
  data in `_swap/old-<id>` for you to recover by hand; it is never deleted automatically.
- The loader reads the frames file from disk twice: `verify_drive` hashes it once, then the scan reads it once more, in chunks (any other file in the manifest is hashed once and never parsed). It is built for one machine, not for a stream. Measured peak memory was about 1.7 to 1.9 GiB at full density, while one Parquet row group is about 600 MB uncompressed (50 frames); the chunk size does not bound it. A drive with a row group above 2,048 MiB (`max_row_group_mib`) is rejected.
- Measured on 2026-10-25 (one machine, demo drive `bf394b9bfb42`, 1,000 frames x 1,000,000 points, row groups of 50 frames): the latest run took 44.7 s for the first ingest into a fresh lake and 43.6 s for a re-ingest, with a peak working set of 1,898 MiB (`.claude/run-proofs/TASK-4-measure-iter2b.log`). An earlier run measured 45.0 s, 42.9 s and 1,718 MiB (`TASK-4-measure.log`). One first run (`TASK-4-measure-iter2.log`) took 3,730 s while its CPU time was only about 54 s; the cause is unknown and it was not reproduced, so it is an open outlier (the next full-density run should log free memory and disk). A frame scan of 91 s and 1.59 GiB before a speed-up was measured once in a session; the output was not saved to a log.
- A drive folder is treated as hostile: a manifest above 2,000,000 points per frame (`max_points_per_frame`), a Parquet footer claiming more than 2 x expected frames + 1,000 rows, or a malformed `scenario_hash` is rejected with `INTEGRITY` before anything large is allocated, and at most 20 problems (plus one "and K more" row) are stored per drive. The row-group size check trusts the footer, so a compression bomb is a known gap.
- `frames_rejected` counts quarantine rows that name a file row, so a frame that breaks two rules counts twice; `frames_in` is not always `frames_kept + frames_rejected`. The ingest assumes the drive folder does not change while it runs.
- A drive whose frames file cannot be read, whose manifest cannot be read, or whose folder name is not its scenario hash is rejected with the code `INTEGRITY`, not with one of the six VL codes.
- The loader's speed limit (9,000 cm/s) is a Tesla's top speed of 322 km/h rounded up and is set in
  a YAML file. A faster vehicle would be flagged as impossible motion.
- Per-frame acceleration is not checked by any loader rule.
- The point-cloud check (VL-004) allows for the 1 cm rounding wobble of whole-centimetre points. A single point moved by exactly 1 cm is not detected; a move of 2 cm or more is.
- The loader decides which frame is a teleport by comparing positions. If the first three clean frames of a drive are themselves inconsistent, the loader trusts the most tightly agreeing pair; a drive that starts with several bad frames may be judged wrongly.
- Single machine, CPU only.

### Coming soon

Rotation and yaw with a non-symmetric body, planar motion, optional noise, event detection and
clip candidates, Apache Iceberg as the table format, a REST API, streaming ingestion (for example
Kafka), parallel loading of many drives, a data-quality report function, a Spark backend for many
drives, an object-store profile, scheduled runs, and delta-encoded storage.
