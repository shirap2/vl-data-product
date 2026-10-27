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
- An unreadable or implausible frames file: not Parquet, a footer that claims too many rows, or a
  row group above `max_row_group_mib`.
- A null frame number or timestamp in the frames file.
- A frame number outside 0 to the expected frame count minus 1.

At most 20 problems are stored per drive; when there are more, one extra row says
"and K more problems". A frames file that reads fine but has the wrong columns or types is
VL-005, not INTEGRITY.

### Generator-side limits

The generator itself rejects a scenario instead of writing a bad drive: a duration that is not
a whole number of frames, or a move whose start and end speed are both 0.

## 2. Architecture and design

Status as of 2026-10-26: Modules 1 (generator), 2 (loader and ETL) and 3 (use case) are built and
checked by tests and by a full-density run. Module 4 (the BI app) is built and checked by
Streamlit's test runner and by one live-server start; it has not been opened in a browser by hand.
Anything marked "planned" has not been run.

### What the product does

It is a small end-to-end data product for a vehicle-location signal: a mocked data source, an
ETL, a queryable warehouse, and a BI view. All data is synthetic. The vehicle is a 1 m cube
that moves in a straight line along x. The use case is extracting its velocity and
acceleration from the point cloud alone and storing the result as a queryable motion table.

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
| 1 Generator | Turn a start position and a motion profile into a hashed drive: point cloud and manifest. Check itself in both directions. | Built |
| 2 Loader / ETL | Verify hashes, validate frames (VL-001 to VL-006), reduce each frame to 9 keypoints, write to the lake. | Built |
| 3 Use-case algorithm | Read keypoints, compute velocity and acceleration, write a motion table. | Built |
| 4 BI | One Streamlit app with four tabs: Generate, Clean, Analyze, SQL. | Built |

Shared code lives in `core/`: config loader, Arrow schemas, hashing, the manifest, and later the
`Lake` class. Imports only point down: entry points, then modules, then core. Core never imports a
module.

### Folder structure

```
src/vl_data/
  __main__.py, cli.py        entry point and root command app
  core/                      config, hashing, schemas, manifest
  generator/                 Module 1: models, body, motion, drive, writer,
                             inverse, faults, cli, scenarios/, ui/
  etl/                       Module 2 (built): config, keypoints, facts, rules,
                             ingest, errors_doc, cli
  bi/                        Module 4 (built): data, figures, clean_tab,
                             analyze_tab, sql_tab, shell, app
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
tab (`generator/ui/`) is built.

### Data flow inside the generator

1. Load and validate the scenario YAML (start values and a list of `move` and `stop` segments).
2. Hash it: SHA-256 of the canonical JSON of the body (`size_cm`, `points_per_axis`) and the scenario gives the drive hash.
3. Build the speed for every frame (a straight-line ramp inside each segment), integrate the
   position.
4. Build each frame's points: a 100x100x100 lattice (1,000,000 points) shifted by the rounded position.
5. Write the frames to a temporary folder, build the manifest from the file hash, swap the folder into place, and verify it.

`generate` in `drive.py` does steps 2 to 4 in memory and returns a `Drive` (scenario, hash, frames
table) by collecting `iter_frame_batches`, which yields record batches of 50 frames; at full density this is only for small drives. `write_drive` in `writer.py` does step 5 for such a drive. `generate_to_disk` streams the batches straight into the Parquet file (memory near one 600 MB batch), writes the manifest, and then runs `verify_drive`; the command line uses `generate_to_disk`.

How a drive is written:

- The temporary folder is `.tmp-*` inside the output folder and holds only `frames.parquet`
  while the manifest is built. `manifest.json` is written last.
- Parquet options are fixed so the same scenario gives the same bytes on the same pyarrow version:
  format version 2.6, zstd compression, no dictionary, statistics on, data page version 2.0 and
  row groups of 50 rows (the file is written one row group at a time; both write paths give the same bytes).
- An existing folder for the same hash is moved into a `.old-*` folder next to it, the new folder
  is renamed into place, and then the `.old-*` folder is deleted. If the last rename fails, the
  old folder is moved back; if that also fails, the `.old-*` folder is kept (it holds the only
  copy of the old drive). The temporary folder is removed on any error.
- A drive written by an older version may hold a `ground_truth.parquet` listed in its manifest. It
  still verifies, because the manifest lists every file that is present, and writing the same
  scenario again replaces the whole folder.
- Measured on the 1,000-frame demo at 1,000,000 points per frame (a scratch script with the same
  50-frame batching, `.claude/run-proofs/FULL-DENSITY-measure.log`): 75.1 s, peak working set
  2,859 MiB, `frames.parquet` 287,134,433 bytes with zstd (4,024,944,648 bytes with snappy). The
  real `generate` command took 81 s including verification on 2026-10-24; its time in the
  fresh-clone run of 2026-10-25 is in RELEASE_NOTES, Usage (measured, local, git-ignored, A28). The
  2,000-frame cap was not run (about 24 GB raw, about 0.6 GB on disk and about 2.5 minutes, scaled
  from this run).

Units are centimetres, seconds, cm/s and cm/s^2 everywhere.

### Data model

| Table or file | Layer | Content |
|---|---|---|
| `frames.parquet` | raw drive | frame_id, timestamp_ns, points (1,000,000 x 3 int32 = 12,000,000 bytes at the shipped density, one binary column) |
| `manifest.json` | raw drive | schema version 2, drive hash, scenario, body (`size_cm`, `points_per_axis`), fps, counts, SHA-256 and size of each file |
| keypoints | bronze (built) | one row per keypoint: frame_id, timestamp_ns, keypoint name, x, y, z (cm, float64); 9 rows per frame |
| quarantine | bronze (built) | one row per rejected frame or missing frame number: frame_id, row_index, code, message; a drive-level row has no frame_id |
| lineage | bronze (built) | one row per drive: scenario hash, schema version, status, frame counts, keypoint rows, the frames file hash |
| motion | silver (built) | per-frame x, velocity, acceleration |

The warehouse tables use a generic, use-case-independent shape: a drive and frame key, a
timestamp, and numeric measures.

#### Core modules (built)

| Module | What it holds | Rules worth knowing |
|---|---|---|
| `core/config.py` | `Config` (fps, raw_dir, body, tolerances, fit) and `load_config` | Unknown keys and non-positive values are rejected, and so is a `points_per_axis` above `size_cm` (checked when the config loads; ASSUMPTIONS A65). `fps` must divide 1,000,000,000 ns evenly (16 fps gives 62,500,000 ns). The environment variable `VL_RAW_DIR` overrides `raw_dir`. |
| `core/hashing.py` | `canonical_json`, `sha256_hex`, `file_sha256`, `scenario_hash` | Keys are sorted, there are no spaces, and a float with a whole value is written as an int, so 600 and 600.0 hash the same. NaN and infinity are rejected. |
| `core/schemas.py` | `FRAMES_SCHEMA`, `encode_points`, `decode_points` | A frame's points are `points_per_frame` x 3 int32 values in cm (1,000,000 at the shipped density), little-endian, row-major, in one binary column. |
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

### Module 2: loader and ETL (ingest, command line and error-code file built; full-density command-line run is the user's proof)

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

Measured (2026-10-25, one Windows machine, `.claude/run-proofs/TASK-5-iter2-a.log`): `generate` of the 1,000-frame demo at 1,000,000 points per frame into `data/raw_proof`, then `ingest --all data/raw_proof --lake-dir warehouse`, took about 120 s of wall-clock time in total (start and finish times in the log). The ingest printed `status: ok`, `frames_expected: 1000`, `frames_kept: 1000`, `frames_quarantined: 0`, `keypoint_rows: 9000`, `points_per_frame_in: 1000000`, `keypoints_per_frame_out: 9` and `elapsed_s: 43.7`, and exited 0. One run, so it is a single measurement.

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
so it is an open outlier: the next full-density run should log free memory and disk. The frame scan
alone took 91 s and 1.59 GiB before the speed-up; after it, a 200-frame benchmark (100 points per
axis) measured about 39 s per 1,000 frames (2.4 times faster). These are single measurements, not
a benchmark suite.

A drive folder is treated as hostile input. Before anything large is allocated: the manifest's points
per frame must not exceed `max_points_per_frame` (2,000,000 points); the lattice is built only after
`verify_drive` passes; the Parquet footer may claim at most 2 x expected frames + 1,000 rows (in total,
per row group, and as the number of row groups); the scan stops if it streams more rows than the
footer claimed; `scenario_hash` must be 64 lowercase hex characters; and stored quarantine text is
cleaned of control characters, cut to 300 characters, and limited to 20 problems plus one summary row.
Known limits: the row-group byte cap trusts the footer's size, which a compression bomb can lie about;
the folder is assumed not to change during an ingest; `manifest.json` and linked data files are read by
core code with no size cap (a follow-up for the generator session). `ground_truth.parquet` is hashed in
full by `verify_drive` but never opened or parsed by the ingest.

#### ETL settings and keypoints (built)

| Module | What it holds | Rules worth knowing |
|---|---|---|
| `etl/config.py` | `EtlConfig`, `KeypointSpec`, `LimitsConfig`, `load_etl_config` | Read from `config/etl.yaml`. Unknown keys are rejected. Keypoint names use letters, digits and `_` (up to 64 characters). A keypoint index must be a real integer, so `true` or `9.0` is refused. `max_jump_cm(fps, frame_gap)` is `max_speed * frame_gap / fps + margin`, with the margin added once: 564.5 cm for one frame at 16 fps and 1,127.0 cm for two (checked by a test). Bounds: speed at most 100,000 cm/s, 256 keypoints, 1,024 MiB per chunk. A chunk is sized in MiB of point data (`chunk_mib`, default 128), not in frames: `chunk_frames(points_per_frame)` gives 11,184 frames at 1,000 points per frame and 11 frames at 1,000,000 (checked by a test). The chunk is the batch size asked of Parquet; it does not bound memory, because pyarrow reads a whole row group. `max_row_group_mib` (default 2,048) is the largest uncompressed row group the ingest will read; a larger one rejects the drive. A corner keypoint is written as its bits (x, y, z), each 0 or 1, so the same list works at any density. `VL_LAKE_DIR` overrides the lake folder when it is not empty. |
| `etl/keypoints.py` | `extract_keypoints` | Input is frames x points x 3 integers (cm). Output is frames x keypoints x 3 float64 (cm), in the order of the keypoint list. A corner is picked by lattice index; the centroid is the float64 mean of all points. Float input and out-of-range indices are refused. |
| `config/etl.yaml` | the nine keypoints, speed limit 9,000 cm/s, margin 2 cm, chunk size 128 MiB, row-group cap 2,048 MiB | The built-in defaults in `etl/config.py` are identical to this file (checked by a test). |

The corner lattice indices are derived from the number of points per axis n as `bx*(n-1)*n*n + by*(n-1)*n + bz*(n-1)`: 0, 9, 90, 99, 900, 909, 990, 999 at n = 10, and 0, 99, 9900, 9999, 990000, 990099, 999900, 999999 at n = 100 (both checked by a test). The ETL gets n from the drive: `etl/body.py` takes it from the manifest's `points_per_frame` (the integer cube root) and the cube size from the core config, until the generator adds body keys to the manifest. A timing check on 2026-10-24 showed `extract_keypoints` takes 0.045 s for 4 frames of 1,000,000 points (48 MB) on the development machine (Windows 11, 15.7 GB RAM).

#### Frame scan and rules (built)

| Module | What it holds | Rules worth knowing |
|---|---|---|
| `etl/facts.py` | `scan_frames` | Streams `frames.parquet` in chunks and returns two things: one small record per row (frame number, timestamp, payload size, payload SHA-256, whether the cloud is valid, centroid) and the keypoints of every valid frame as a rows x keypoints x 3 array (NaN for an invalid frame). A bad payload never raises; a null frame number or timestamp, or a missing column, raises one `ValueError` (the caller checks the schema first). It never opens the ground-truth file. The size is checked before the payload is decoded. The deviation from the lattice is computed in int32 when that cannot overflow (int64 otherwise), the centroid is an exact integer sum divided by the point count, and the SHA-256 runs in a second thread; on a 200-frame drive of 100 points per axis this took 39 ms per frame against 94 to 100 ms for the earlier plain version (2.4 to 2.5 times faster; reading and decompressing the Parquet is about 25 ms of the 39). |
| `etl/ingest.py` | `ingest_drive`, `IngestResult` | The entry point. Runs the gates, the scan, the rules and the three table swaps (next section). |
| `etl/rules.py` | `RULES`, `RuleContext`, `run_rules` | Six rules with their codes, meaning, detection text and example fault. Drive rules (VL-005) and frame rules (VL-001 to VL-004, VL-006). VL-006 runs only on frames that passed the others. |
| `etl/body.py` | `expected_frame_count` | The number of frames the scenario implies, used by VL-001. |

On 2026-10-25 (ingest added) the ETL tests numbered 214 (481 in the whole suite, after the hostile-folder repair pass), and line coverage of the ETL package was 99% (`ingest.py` and `facts.py` 100%; the two uncovered lines are in `rules.py`). Ingesting the demo drive on a 1,000-point test body kept 1,000 of 1,000 frames and wrote 9,000 keypoint rows (checked by a test); each of the five frame faults gave exactly one quarantine row; a changed byte, a missing manifest, a schema-1 manifest, a renamed drive folder and a renamed points column each rejected the whole drive; ingesting twice left all three table files byte-identical.

On 2026-10-24 the ETL package had 151 tests (381 in the whole suite), and line coverage of the ETL package was 99%. Running the rules on the demo drive (1,000 frames on a 125-point test body) gave no violations, and each of the six generator faults produced exactly its own code. The full-density behaviour of the scan is not measured yet.

On 2026-10-23 the settings and keypoint modules had 71 tests (201 in the whole suite), and line coverage of the ETL package was 100%.

What goes in: a drive folder `data/raw/<hash12>/` with the full point cloud (1,000 points per
frame). What comes out: three tables in the lake, `keypoints`, `quarantine` and `lineage`, each
stored as `<table>/drive=<hash12>/part-0.parquet` and queryable with DuckDB.

Steps for one drive:

1. **Gates, cheapest first.** The folder name must be a valid drive id (otherwise `ingest_drive`
   raises `ValueError` and writes nothing). Then: the manifest reads; the folder name equals the
   first 12 hex of the scenario hash; the schema version is in the supported list; the fps divides
   a second; the body and scenario are valid; `frames.parquet` is listed; and only then every file
   hash is checked (`verify_drive`, the slow step). Any failure rejects the whole drive before a
   frame is read. The frame rate and frame spacing come from the manifest, not from a core config.
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
   version, the counts and the manifest hashes of the frames and ground-truth files (taken from
   the manifest after the files were verified; the ground-truth file is never opened). A keypoint
   spec that does not fit the body is a configuration error: `ValueError`, nothing written.

| Code | Rule in short |
|---|---|
| VL-001 | A frame number from 0 to the expected count minus 1 is absent. One row per missing number. The expected count comes from the scenario stored in the manifest (its hash is verified), not from `manifest.frame_count`, which counts the rows of the file. |
| VL-002 | The timestamp is not `frame_id x 62,500,000 ns`. |
| VL-003 | A frame number appears twice. Identical copies keep the first. Differing copies are all quarantined. |
| VL-004 | The payload is not a multiple of 12 bytes, does not hold the drive's points per frame, or a point is more than rounding away from the expected lattice (per axis, the offsets from the lattice may spread by at most 1 cm). A single point moved by exactly 1 cm is not caught. |
| VL-005 | The frames file's columns differ from the agreed schema. Rejects the drive. |
| VL-006 | The centroid is farther from the last accepted frame than 9,000 cm/s allows (about 564.5 cm per frame, with a 2 cm margin). |

The speed limit tests position only. Per-frame acceleration is too noisy (the generator plan
measured an error of 452 cm/s^2) to test.

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
| Body | 1 m cube, 100 x 100 x 100 = 1,000,000-point lattice | The target density: 100 points per axis, 1,000,000 points per frame | A 10 x 10 x 10 sample (the earlier choice), a car-sized box | Storage or time limits |
| Source | Generator writes a drive to disk | Simplest valid source | A service or a stream | Many consumers or live data |
| Config | YAML plus a validating Python loader | Readable, fails early on bad values | Environment variables only, JSON | Many deployment environments |
| Scenario validation | pydantic | Typed models with clear errors | dataclasses plus hand checks | Never needed so far |
| Hashing | SHA-256 over canonical JSON (scenario) and file bytes (files) | Standard, checkable with common tools | Content hashes, UUIDs | Needing stability across library versions |
| Raw file format | Parquet via PyArrow, one row per frame | Columnar, compact, readable by every tool | One row per point, CSV, HDF5 | Per-point SQL |
| Query | DuckDB over Parquet folders | SQL with no server | Trino, Athena | Many users, large data |
| ETL engine | pandas and numpy | Data is small, code is clear | Polars, Spark | Data no longer fits one machine |
| Lake format | Plain Parquet, Hive-style folders, behind one class | Nothing to run | Apache Iceberg, Delta Lake | Many writers, schema changes, time travel |
| Idempotent writes | Write to a temp folder, then swap the drive folder | Re-running never duplicates, a crash never loses old data | Blind append, upsert | Concurrent writers |
| Validation | Rule registry with error codes | Explicit and testable; the same registry builds the error-code document | Great Expectations, pandera | Many datasets and owners |
| Keypoint table layout (planned) | Long: one row per keypoint | Generic; the keypoint list is a YAML change; simple SQL filter by name | Wide: 27 coordinate columns per frame | Heavy column maths on the corners |
| Bad frames (planned) | Quarantine only the failed frame, with a details-only row | One bad frame does not cost the drive; the raw frame stays in `data/raw/` | Reject the whole drive; copy the raw bytes | A consumer needs replay from the lake alone |
| Ingest passes | One chunked pass: facts and keypoints together (user decision 2026-10-25) | Bounded memory, rules see the whole sequence from the facts, and the frames file is decoded once instead of twice | Two passes: facts, then keypoints | A rule needs cloud data that the stored facts do not hold |
| BI | Streamlit and Plotly | Python only, one function per tab that AppTest can run, no server code | Dash, Jupyter, Superset, Grafana; Altair, matplotlib | Shared dashboards for non-Python users |
| CLI | Typer | Typed commands | argparse | None |
| Tests | pytest and hypothesis | Property tests suit a two-way check | unittest | None |
| Not used | Kafka, Spark, Airflow, dlt, Docker | No purpose at this scale | See "Scaling" | See "Scaling" |

### How the simulator is checked

The generator is tested in both directions. Forward: scenario to frames. Back: `infer_track`
recovers per-frame position and velocity from the frames, `fit_segments` fits move and stop
pieces to them, and `compare_scenarios` checks the result against the original within stated
limits. The back direction reads frames only.

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
counts per error code. Only the frames table changes; the scenario and the
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
its own code; Module 2 behaviour is not yet verified.

### Generate tab

`generator/ui/generate_tab.py` draws the tab and `generate_app.py` is a short runner (two imports
and one call; `streamlit run src/vl_data/generator/ui/generate_app.py`); the three-tab shell belongs to the BI
module. `rows_to_scenario` turns the form values and the table rows into a `Scenario` (`float()` on
every number, stop rows lose `end_speed_cm_s`, a blank, NaN, infinite or true/false
cell, an empty table raises a clear `ValueError`; the seed is not an input, the `Scenario`
default 0 is used; ASSUMPTIONS A74, A80, A81). `preview_figure` plots speed (cm/s) against
time (s) from `motion.speed_profile`. A wrong form or table shows `st.error` and no plot.

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

### Module 3: motion use case (built)

`vl_data.sdk.VLClient(lake: Lake)` is the read-only way to the lake. `drives() -> list[str]`
lists the drives that have keypoints. `keypoints(drive, keypoint=None)`, `motion(drive)`,
`lineage(drive)` and `quarantine(drive)` return Arrow tables with exactly the table schema
(no `drive` column); keypoints are ordered by `frame_id`, then keypoint name. A valid unknown
drive gives an empty table; an invalid drive id (for example `a/b` or empty) raises
`ValueError` from the Lake. `sql(query) -> pa.Table` runs one SELECT over the table views (which do have a `drive`
column) and closes its DuckDB connection after every query. The SDK reads only the lake.

The `motion` table (`MOTION_SCHEMA`) has one row per frame: `frame_id` int64, `timestamp_ns`
int64, `x_cm` float64, `velocity_cm_s` float64 (null where there is no estimate),
`acceleration_cm_s2` float64 (null likewise) and `n_points` int32 (samples in the estimator
window).

#### Estimator defaults, measured (sweep)

The estimator fits a least-squares polynomial of order 2 or 3 to the centroid x of the frames
inside a window centred on each frame, on the real timestamps, and reads velocity (cm/s) and
acceleration (cm/s^2) from the fitted coefficients (`vl_data.usecase.estimator`). The window
length, order, minimum points and gap limit are set in `config/motion.yaml`. The values there come
from one command, run on 2026-10-25 (measured run time 23.9 s):

```
python tools/motion_sweep.py --write-yaml
```

The sweep generates two drives in memory with a 100 cm cube of 10 points per axis: the demo
scenario (1,000 frames) and a harder drive of 624 frames (stop 4 s; ramp 0 to 600 cm/s over 3 s,
200 cm/s^2; cruise 6 s; reversal 600 to -600 cm/s over 3 s, 400 cm/s^2; cruise 5 s; slow to 0 over
3 s; stop 15 s). For each frame it takes the mean x of the points (the centroid, cm) and the true
velocity and acceleration that the tool computes from the scenario itself (`true_motion` in
`tools/motion_sweep.py`: the speed profile, forward-Euler position and forward differences; no file
and no lake is read). These are development measurements against that in-memory truth, not a check
that the pipeline runs. Errors are estimate minus truth. "int" columns use interior frames (no
change of true acceleration within half a window of the frame, the last frame excluded; the
function `interior_mask` in the same tool); "all" columns use every frame with an estimate. The baseline row uses
central differences of the centroid x (forward and backward at the ends) and a one-frame half
window. For each cell `min_points` is `window // 2 + 1`, so the one-sided window at the clip ends
still fits, and `max_gap_frames` is the config default, 4. Real output (measured):

```
Selection table: worst case over drives demo, hard, interior frames
row                      window order min_pts interior  int vel max (cm/s)  int acc max (cm/s^2)
window 8, order 2             8     2       5    91.2%              13.467                23.712
window 8, order 3             8     3       5    91.2%              14.599                23.712
window 12, order 2           12     2       7    87.3%              13.143                 8.282
window 12, order 3           12     3       7    87.3%              13.486                 8.282
window 16, order 2           16     2       9    83.5%              12.902                 4.378
window 16, order 3           16     3       9    83.5%              13.515                 4.378
window 24, order 2           24     2      13    75.8%              12.692                 1.568
window 24, order 3           24     3      13    75.8%              12.979                 1.568
window 32, order 2           32     2      17    68.1%              12.559                 0.653
window 32, order 3           32     3      17    68.1%              12.784                 0.653
window 48, order 2           48     2      25    53.2%               3.168                 0.286
window 48, order 3           48     3      25    53.2%               3.232                 0.286
per-frame differences         -     -       -    97.0%              19.000               452.000

Drive demo: velocity in cm/s, acceleration in cm/s^2
row                      frames int vel max int vel rms int acc max int acc rms all vel max all vel rms all acc max all acc rms
window 8, order 2           936       4.533       1.525      23.712       5.799       6.400       1.595      65.091       7.564
window 8, order 3           936       6.713       1.734      23.712       5.799       6.713       1.781      65.091       7.564
window 12, order 2          908       3.701       1.485       8.282       2.047       8.967       1.673      57.031       5.927
window 12, order 3          908       5.410       1.542       8.282       2.047       5.410       1.600      57.031       5.927
window 16, order 2          880       3.520       1.460       4.378       1.000      11.412       1.836      55.678       6.240
window 16, order 3          880       4.152       1.488       4.378       1.000       5.377       1.585      55.678       6.240
window 24, order 2          824       3.278       1.418       1.568       0.350      16.271       2.404      54.568       7.299
window 24, order 3          824       3.765       1.424       1.568       0.350       7.275       1.619      54.568       7.299
window 32, order 2          768       3.235       1.372       0.653       0.142      20.877       3.220      52.905       8.319
window 32, order 3          768       3.400       1.375       0.653       0.142       9.584       1.732      52.905       8.319
window 48, order 2          656       3.168       1.244       0.286       0.047      30.263       5.365      51.928      10.086
window 48, order 3          656       3.232       1.249       0.286       0.047      13.521       2.165      51.928      10.059
per-frame differences       978       9.500       2.777     452.000     172.956       9.500       2.779     452.000     173.660

Drive hard: velocity in cm/s, acceleration in cm/s^2
row                      frames int vel max int vel rms int acc max int acc rms all vel max all vel rms all acc max all acc rms
window 8, order 2           569      13.467       4.011      15.550       4.228      27.200       4.527     246.026      22.021
window 8, order 3           569      14.599       4.056      15.550       4.228      14.599       4.313     246.026      22.021
window 12, order 2          545      13.143       3.881       5.235       1.373      36.484       5.118     229.147      23.914
window 12, order 3          545      13.486       3.889       5.235       1.373      18.302       4.322     229.147      23.914
window 16, order 2          521      12.902       3.735       2.394       0.624      45.941       6.087     222.481      26.400
window 16, order 3          521      13.515       3.741       2.394       0.624      22.348       4.420     222.481      26.400
window 24, order 2          473      12.692       3.375       1.256       0.241      64.720       8.965     215.069      31.143
window 24, order 3          473      12.979       3.378       1.256       0.241      30.394       4.819     215.069      31.143
window 32, order 2          425      12.559       2.875       0.547       0.105      83.495      12.771     211.434      35.404
window 32, order 3          425      12.784       2.875       0.547       0.105      38.252       5.511     211.434      35.404
window 48, order 2          332       0.000       0.000       0.033       0.016     121.057      22.296     207.743      42.816
window 48, order 3          332       0.000       0.000       0.033       0.016      53.951       7.708     207.743      42.816
per-frame differences       605      19.000       4.424     400.000     162.934      19.000       4.517     656.000     169.026

Chosen: window 16 frames, order 2, min_points 9, max_gap_frames 4
Without the interior-fraction requirement (80%) the plain rule would choose window 48, order 2 (53.2% of frames interior in the worst drive)
Measured worst-case interior max error, sweep drives: velocity 12.902 cm/s, acceleration 4.378 cm/s^2
Measured worst-case interior max error, 1502 random and pinned scenarios (seed 0): velocity 8.848 cm/s, acceleration 21.212 cm/s^2
```

Selection rule. Among the window x order cells, take the lowest worst-case (larger of the two
drives) interior acceleration max error; ties (errors equal to 6 decimals) go to the lower
worst-case interior velocity max error, then the smaller window, then the lower order. A cell is
eligible only if at least 80% of the frames of every drive are interior. This interior-fraction
condition decides the result: the plain rule picks window 48, order 2 (measured 0.286 cm/s^2), but
a 48-frame window leaves only 53.2% of the frames interior, so its small interior error hides
large errors near every speed change (measured: all-frame acceleration max 207.7 cm/s^2 and
velocity max 121.1 cm/s on the hard drive). Other shares give (measured, same table): 0.65 gives
window 32, 0.7 and 0.75 give window 24, 0.8 gives window 16, 0.85 gives window 12. The 80% is a
judgement. At window 48 the order 3 acceleration error is 0.28574749048425 and order 2 is
0.28574749048971 (unrounded, measured): a difference of 5e-12 cm/s^2 is float round-off, so it
is a tie, and the lower velocity error (3.168 against 3.232 cm/s) gives order 2. At the chosen
window 16 the two orders also tie on acceleration (4.378 cm/s^2) and order 2 wins on velocity
(12.902 against 13.515 cm/s). Without the tie rule the plain rule printed order 3 by round-off.

The per-frame-difference baseline has an interior acceleration max error of 452.0 cm/s^2 (demo)
and 400.0 cm/s^2 (hard), against 4.378 and 2.394 cm/s^2 for the chosen cell (measured):
differencing integer-cm positions at 16 frames/s multiplies the 0.5 cm rounding by 2 x 16^2. The
chosen cell has lower interior velocity and acceleration errors (max and RMS) and lower all-frame
acceleration errors than the baseline on both drives (tests). It does not have a lower all-frame
velocity max error: 11.4 against 9.5 cm/s (demo) and 45.9 against 19.0 cm/s (hard), because the
16-frame fit smooths across speed changes and the clip ends (measured).

Random-scenario search. The sweep drives start and end at rest, so they never test slow motion
(where the integer-cm rounding of the centroid is a staircase) or a clip that ends while moving
(the first and last 8 frames use a one-sided window). The tool therefore also measures the chosen
cell on a seeded random-scenario search (`--random-search N`, default 1,500, `--seed`, default 0).
It draws N scenarios with numpy's RNG (up to 5 legs of 48 to 96 frames, end speeds up to 400 cm/s)
and always adds two pinned scenarios, the counterexamples the Hypothesis test found: one 48-frame
move from 0 to 2 cm/s (interior acceleration max error 12.909 cm/s^2) and five 96-frame moves
towards -68 cm/s (19.530 cm/s^2); the sweep drives alone would give only 4.378 cm/s^2. Measured
over the 1,502 scenarios of the run above (seed 0): worst interior velocity 8.848 cm/s and
acceleration 21.212 cm/s^2. Other seeds, 1,500 scenarios each (measured on 2026-10-25): seed 1 gives
9.806 cm/s and 25.364 cm/s^2, seed 2 gives 10.669 and 19.818, seed 3 gives 12.381 and 19.896. The
velocity error of about 12.5 cm/s on the hard drive is the forward-difference truth against the
centred fit, a/32 for a = 400 cm/s^2 (assumption M3-A17). The pipeline does not use these numbers
as limits; `tests/usecase/test_estimator.py` keeps them as regression bounds (velocity 19.4 cm/s,
acceleration 31.9 cm/s^2, 1.5 times the largest measured value, rounded up to 0.1), so a change to
the estimator that makes it worse fails a test.

Full-density check (measured on 2026-10-27, 300 frames, the same command run again after the
evaluation was removed; the block is an excerpt of the command output):

```
python tools/motion_sweep.py --full-density-check 300
Full-density check: 300 frames at 100 points per axis, 248 interior, 0 null
                       int vel max int vel rms int acc max int acc rms
full density                 6.549       2.990       4.681       1.081
10 points per axis           6.549       2.990       4.681       1.081
Largest centroid x difference, full vs small lattice: 0.000000 cm
Interior position error (full density): 0.4844 cm max
Units: velocity cm/s, acceleration cm/s^2.
```

The 300 frames were generated at 100 points per axis with `generate_to_disk`, ingested into a temp
lake, run through `compute_motion`, the stored motion table compared with the in-memory truth, and
the temp folders deleted. In this
one run the interior errors match the small-lattice run of the same scenario to every printed
digit and the centroid x values are identical (measured once on 300 frames). A unit test checks
the identity behind it, centroid x = 49.5 + rint(x) for 2, 10 and 100 points per axis, on single
frames. The whole command (sweep, random search and this check) took 99 s (measured on
2026-10-27). The tool limits N to 600 frames, prints the estimated
size (12 MB per frame) and refuses to start when free disk space is under twice that estimate.

#### Data flow

```
raw drive folder --ingest--> lake: keypoints, quarantine, lineage
lake keypoints (centroid rows) --motion--> lake: motion
VLClient.sql / keypoints / motion  <-- read the lake
```

`motion <drive_id>` reads the drive's lineage row (it must exist, be the only one, and have status
`ok`) and its centroid keypoint rows through `VLClient`, fits the sliding window on the real
timestamps, and replaces the drive's motion partition in one swap. It never opens the raw drive.

#### Command line (built)

```powershell
python -m vl_data motion <drive_id> [--lake-dir DIR]
```

The lake folder is `--lake-dir`, else `VL_LAKE_DIR`, else `lake_dir` in `config/etl.yaml`, else
`warehouse` (the same resolution as `ingest`, through `load_etl_config()`). The window and order
come from `config/motion.yaml`. Exit codes follow `ingest`: 0 done, 1 refused or failed
(`MotionError`, or any other unexpected failure), 2 usage or
configuration error (including a lake folder that does not exist), 3 a file could not be read or
written. The named failures are in `docs/USE_CASE_ERRORS.md`.

Real output of the full-density run (measured on 2026-10-25: the demo scenario, 1,000 frames x
1,000,000 points, `generate` 79 s, `ingest` 56 s wall-clock):

```
> python -m vl_data motion bf394b9bfb42 --lake-dir <work>/lake        (exit 0, 1 s)
drive: bf394b9bfb42
frames_read: 1000 frames
frames_estimated: 1000 frames
frames_null: 0 frames (velocity or acceleration is null)
```

A second `motion` run left the motion partition file byte-identical (sha256
`fdab85419d40c367dc6486db5c5b4951a68d5e2cb6d12f3f29a2b9ce96b796e0` both times, measured). The full
suite on 2026-10-27, after the evaluation and the ground truth were removed and origin/main was merged: 798 passed, 4 skipped in 77 s (measured).
The run-proof log is local and git-ignored: `.claude/run-proofs/M3-TASK-7-iter1.log`.

#### Technology reasons and alternatives

- Estimator: a least-squares sliding window on the real timestamps. Alternatives considered:
  per-frame differences (built and measured as the baseline above: interior acceleration max
  error 452.0 and 400.0 cm/s^2); Savitzky-Golay (needs evenly spaced samples, so dropped frames
  would need resampling, and adds the scipy dependency); a Kalman or RTS smoother (more complex,
  needs a motion model and noise tuning; considered for a later version). The window fit needs
  numpy only and uses the timestamps, so a dropped frame is a gap, not a shifted sample.
- Storage and queries: Parquet files in the lake, read through DuckDB. Alternatives: SQLite or
  Postgres (a server or a schema migration to run, and no file hash or folder swap per drive);
  the SDK also returns Arrow tables. Pandas DataFrames were the alternative to Arrow tables: they
  have no exact column types and no real nulls (a missing integer becomes a float NaN).

### Module 4: BI app (built)

One Streamlit app (Streamlit turns a Python script into a web page) with four tabs: Generate,
Clean, Analyze and SQL. The first three are in the
design; the SQL tab is a user decision of 2026-10-26, added so a user can ask the lake their own
question. A sidebar selector, "Drive", picks a drive from the lake, and every tab shows the
scenario hash of that drive as a badge. The package is `vl_data.bi`. It uses `VLClient` and
`render_generate_tab` as they are, writes nothing to the lake and edits no
Module 1, 2 or 3 file. Module 4 adds no error codes: the Clean tab shows the existing VL-001 to
VL-006 and the drive-level code `INTEGRITY` from `docs/ERROR_CODES.md`.

#### Data flow

```
Lake --> VLClient (keypoints, motion, lineage, quarantine, sql) --> bi/data.py
                                                                       |
                                                                  bi/figures.py
                                                                       |
        clean_tab.py   analyze_tab.py   sql_tab.py   render_generate_tab (Module 1)
                                   \         |          /
                                          shell.py  (sidebar, four tabs, badges)
                                             |
                                          app.py  (page setup, configuration)
```

`bi/data.py` has no Streamlit code: it lists the drives, reads the quality of a drive, builds the
`ingest` and `motion` commands, gives the hint for a drive with no stored motion (`motion_hint`),
runs a query and builds the example queries. `bi/figures.py` makes the Plotly figures (Plotly is a Python charting library with hover and zoom). Each tab is one
function that draws into the page and never raises; a failure is shown with `st.error` or
`st.info`, not as a traceback. `shell.py` draws the sidebar and the tabs, and `app.py` is the file
that `streamlit run` starts.

#### Tabs

- Generate: the existing `render_generate_tab(config)`, unchanged. Its badge shows the scenario
  hash of the drive selected in the sidebar, not the hash of the form the user is editing.
- Clean (read-only): for the selected drive, the counts expected, in, kept, rejected and missing
  (frames) and the keypoint rows (rows); a bar chart of frames kept, rejected and missing; a bar
  chart of quarantine rows per error code; and the quarantine table, which shows at most 200 rows
  and states the full count. A drive with no lineage row is not ingested: the tab prints the
  exact `ingest` command for the raw folder and the lake, and does not run it.
- Analyze: only the stored motion: three charts (centroid x in cm, velocity in cm/s,
  acceleration in cm/s^2, against time in s), drawn with no click and no raw folder. The
  centroid x is the raw centroid, not corrected. When the drive has no stored motion the tab
  shows a message and the exact command: an ingested drive with status ok gets the `motion`
  command; a drive that is not ingested, or was rejected, gets the `ingest` command; a drive with
  more than one lineage row gets a problem text and no command (from reading `motion_hint` in
  `bi/data.py`, not run). The tab never reads ground truth.
- SQL: a dropdown of five example queries fills a text box; Run sends the text to
  `VLClient.sql` (it runs the text with DuckDB, an embedded SQL engine), which accepts one SELECT over the views `keypoints`, `quarantine`, `lineage` and
  `motion`. The result table shows at most 10,000 rows and states the total. A non-SELECT, a
  second statement or a bad query shows a one-line message.

#### Command line

```powershell
python -m streamlit run src/vl_data/bi/app.py --server.address 127.0.0.1 --server.headless true --browser.gatherUsageStats false
```

`--server.address 127.0.0.1` makes the app listen on this computer only. Without it, a server may
listen on all network interfaces, which means that any computer that can reach yours over the
network could open the page, including the SQL tab, which has no login. Do not start the app with
`--server.address 0.0.0.0` (all interfaces) or behind a port forward (a router or tunnel setting
that passes outside traffic to your computer).

Run it from the repository root. The lake folder comes from `VL_LAKE_DIR`, else `config/etl.yaml`,
else `warehouse`; the raw folder comes from `VL_RAW_DIR`, else `raw_dir` in
`config/default.yaml` (`data/raw`). The defaults are relative to the current folder, so start the
app from the folder that holds `warehouse` and `data/raw`. The configuration files themselves are
found by file path, not by the current folder.

#### Technology reasons and alternatives

These are reasons from design, not measurements.

- Streamlit: Python only, so the app uses the same typed SDK and the same functions as the
  command line. A tab is a function, which Streamlit's test runner (AppTest) can run without a
  browser. There is no server code to write. Alternative: Dash, which needs callbacks and a layout
  tree for the same screens.
- Grafana and Superset: they need a server, a database connection and configuration, and they
  suit shared dashboards for non-Python users. They would not call the SDK
  directly, so the stored motion would need a separate service. Jupyter is a good place to analyse, but a notebook is not a shipped interface.
- Plotly: hover and zoom come with `st.plotly_chart`. Altair is declarative but
  gives less control over gaps; matplotlib is static.
- No web service: the app reads the lake in-process through the SDK. A REST API is listed as
  coming soon.

#### What changed from the first plan

`ingest_command` and `render_clean_tab` take the lake folder as an argument. The Analyze tab uses
`config.raw_dir` only to print the `ingest` command. `read_problem` is a public helper in `bi/data.py` that the
other files share (it turns an exception into a short message without a file path).

The ground-truth evaluation (an Evaluate button, PASS or FAIL, metrics, estimate-against-truth
charts) was part of the first Module 4 build and was removed on 2026-10-26 by user decision; the
exercise does not require it. `render_analyze_tab(client, config, lake_dir, drive)` and
`render_app(config, lake_dir)` lost the motion-config argument, and the app no longer loads
`config/motion.yaml`.

#### What is proven by what

The tabs are tested with AppTest on a small drive (a 100 cm cube of 10 points per axis); on
2026-10-26 the 100 tests in `tests/bi` passed with 100% statement coverage of `vl_data.bi` (319
statements, 0 missed), and the full suite gave 958 passed, 4 skipped (all measured). A live
Streamlit server was started by a script (`.claude/run-proofs/module4_server_check.py`, local,
git-ignored) after the evaluation was removed: the health endpoint, the page and the server's own
run of `app.py` answered, and the server log had no exception (`SERVER_CHECK: PASS`, 8.4 s
measured on 2026-10-26; log `.claude/run-proofs/M4RM-server.log`). That proves the app starts and is served, not what a browser
draws and not any click.

#### Limits of Module 4

These come from reading the source and the tests, not from a measurement, unless a number says
measured. The full list for users is in the release notes under Known issues.

- The SQL tab runs any single SELECT through `VLClient.sql`, which can also read any file path
  on the machine through `read_parquet` (a DuckDB function that reads a Parquet file from any
  path). The app is a local single-user tool, and that premise depends on the address 127.0.0.1
  (this computer only): the app must not be started with `--server.address 0.0.0.0` or behind a
  port forward, because the SQL tab has no login. Production would use a locked connection
  (external access off, extension autoload off, a memory limit; autoload means DuckDB loads an
  add-on such as `httpfs` by itself when a query needs it). A query could also read environment
  variables with `getenv` (a DuckDB function) or open a Windows UNC path (a network-share path),
  which can send a login hash to that share; this was read from the DuckDB defaults and was not
  run. A follow-up is written: `module4-TASK-4-1.md` in `.claude/plans/followups/`.
- Display caps: 10,000 rows in the SQL tab (the whole result is loaded first, so the cap limits
  what is shown, not memory) and 200 quarantine rows in the Clean tab.
- The Generate-tab badge shows the hash of the drive selected in the sidebar, not the scenario in
  the form.
- A Plotly trace holds one point per frame, up to the 2,000-frame cap of a scenario; larger
  drives were not tried.

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
- The measured errors come from two sweep drives and a random-scenario search (see the sweep section)
  and may widen when other scenarios are tried; the sweep drives start and end at rest.
- SQL tab (Module 4): it runs any single SELECT through `VLClient.sql`, which can also read any
  file path on the machine through `read_parquet`. This is a local single-user tool, and that
  premise depends on the address 127.0.0.1: do not start the app with `--server.address 0.0.0.0`
  or behind a port forward, because the SQL tab has no login. The production
  fix is a locked connection (external access off, extension autoload off, a memory limit). Other
  limits of Module 4 are in "Limits of Module 4" above.

The full assumption list is in [ASSUMPTIONS.md](ASSUMPTIONS.md).

## 3. Release notes

Status as of 2026-10-26: Modules 1 (generator), 2 (loader and ETL), 3 (use case) and 4 (the BI
app) are built. Modules 1 to 3 are checked by tests and by a full-density run; Module 4 is checked
by Streamlit's test runner and one live-server start, not by a hand-opened browser. Every command
and number below comes from a real run unless it says it comes from reading the source or the
documentation.

### Product description

A small data product for a vehicle-location signal. It generates a mock drive from a start
position and a motion profile, stores the point cloud, extracts the vehicle's velocity and
acceleration from the points alone, and stores the result as a queryable motion table. The vehicle
is a 1 m cube that moves in a straight line, forwards, backwards and stopped, at different
speeds. All data is synthetic.

Module 3 adds the use case: `motion` turns the stored centroid position of a drive into velocity (cm/s) and acceleration (cm/s^2) with a sliding least-squares fit over 16 frames. The Python SDK (`VLClient`) and SQL read the lake.

Module 4 adds the BI app: one Streamlit page with four tabs. Generate writes a drive; Clean shows how many frames of the selected drive were kept, rejected or missing and why; Analyze shows the stored centroid position, velocity and acceleration; SQL runs one SELECT over the lake. Apart from the Generate tab, which writes a drive, it only reads the lake.

### What is in this version

| Part | What it does | Status |
|---|---|---|
| Generator | Writes a hashed drive: 16 fps point cloud and manifest. Checks itself by recovering the scenario from the data. | Built |
| Loader / ETL | Verifies hashes, validates frames with error codes VL-001 to VL-006, keeps 9 keypoints per frame, and writes keypoints, quarantine and lineage tables to a queryable lake. | Built |
| Use-case algorithm | Computes velocity and acceleration into a motion table (`motion`, `VLClient`). | Built |
| BI | One Streamlit app with four tabs: Generate, Clean, Analyze, SQL. | Built |

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

Generate the demo drive (1,000 frames, one reversal, two stops). The fresh-clone run on 2026-10-25
took 86.6 s including verification (measured, same log; the 2026-10-24 run on this machine took 81 s).
This Usage section owns the measured run figures; other documents point here. The fresh-clone check
was run twice. Run 1 (`FINAL-DOCS-iter1-fresh-clone-run1.log` and `FINAL-DOCS-iter1-fresh-clone.log`
in `.claude/run-proofs/`, local, git-ignored, A28) gives every time of the 2026-10-25 fresh-clone check quoted in these notes (the 10.35 s of the Generate-tab UI tests comes from `TASK-7-1-1-iter1-ui-tests.log`);
the 81 s of 2026-10-24 comes from `FULL-DENSITY-iter2.log` (`duration_s=81`). Run 2
(`FINAL-DOCS-iter2-fresh-clone.log`) also passed all steps, but its
timings were unreliable and
are not used:

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
and `manifest.json` (1,312 bytes). The sizes are from a run on 2026-10-27 (measured); earlier versions also wrote a `ground_truth.parquet` of 30,929 bytes. The 1,000-frame drive holds 12 GB
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

This prints `manifest: verified` and exits with code 0. After one byte of `frames.parquet`
was changed in a copy of the demo drive (byte 1,000 flipped), it printed
`changed: frames.parquet (sha256 mismatch)` on stderr and exited with code 1 (measured on 2026-10-27). A missing scenario file (`generate` and `roundtrip`) or an
invalid one (`generate`) gives a one-line `error: ...` message and exit code 1 (`generator/cli.py` writes it to stderr; the tests mostly check the exit code and the message text, not the stream: tests
`test_generate_missing_scenario`, `test_generate_invalid_scenario` and
`test_roundtrip_missing_scenario_exits_1` in `tests/generator/test_cli.py`; not run in the
fresh-clone check).

Check that the generator can read its own drive back (the two-way check). Run on 2026-10-25,
it took 54.5 s wall time at full density (1,000,000 points per frame; measured in the fresh-clone run on
2026-10-25, same log) and writes nothing to disk:

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
figures (2026-10-25). 
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
start form (`x_cm`, `y_cm`, `z_cm`, `speed_cm_s`; there is no seed box). The seed is hidden and is 0 for drives made in the tab (a scenario file can still set it). This description of the page comes from the code, not from a browser run. The page also has a segment table that starts with the
demo rows (`type`, `duration_s` in steps of 0.0625 s, `end_speed_cm_s`; rows can be added and
deleted), a speed-against-time plot, and a Generate button. Click Generate and the tab first works out the target folder (the first 12 characters of the scenario hash under `raw_dir`). If
that folder already holds a drive that passes the full `verify_drive` check (every file is hashed again, shown in a
"Checking existing drive..." status block) and whose manifest has the same hash, the tab shows "already generated" with
the folder, hash and frame count and writes nothing; a folder that fails the check is written again and replaced. Next a
disk guard compares the size estimate below (frames x bytes per frame, uncompressed, an upper bound; nothing is credited
back for an existing folder) with the free bytes on the `raw_dir` disk (or its nearest existing parent folder if `raw_dir`
does not exist yet). If the estimate is larger, the tab shows a red error with both numbers and writes nothing. Otherwise
it shows a warning such as "This drive has 1000 frames, about 12,000,000,000 bytes (uncompressed point
data)" (1,000 frames x 12,000,000 bytes at full density, computed; about 12,000 bytes per frame, 12,000,000 bytes for 1,000 frames, at the
10-points-per-axis test body, computed). It then writes the drive under `raw_dir` with the same function
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
and has a no-rights stand-in that ran; the whole suite was 332 tests, 331 passed and 1 skipped, 100.00% coverage, measured in `TASK-7-1-1-iter1-suite.log`; the fresh-clone run on 2026-10-25 gave the same: 331 passed, 1 skipped in 32.41 s, 100% coverage of 1,037 statements, with ruff and mypy clean (measured, `FINAL-DOCS-iter1-fresh-clone.log`); the logs are local, git-ignored, A28), which cannot edit the table, so editing a row
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
per frame, 1,000 frames; `tests/etl/test_cli.py`, `test_errors_doc.py`, `test_e2e.py`, 36 tests),
which ingest through the root app and query the lake with DuckDB (9 keypoints per frame; each of
the six faults gives its code in the quarantine view); `python -m vl_data error-codes --write`
followed by `--check` (exit 0). The full-density command-line run was also done (`generate` to `data/raw_proof`,
then `ingest --all data/raw_proof --lake-dir warehouse`, `.claude/run-proofs/TASK-5-iter2-a.log`):
exit 0, `status: ok`, `frames_kept: 1000`, `frames_quarantined: 0`, `keypoint_rows: 9000`,
`points_per_frame_in: 1000000`, `keypoints_per_frame_out: 9`, `elapsed_s: 43.7`; about 120 s of
wall-clock time for generate plus ingest (one run, measured). Three tests need an OS or account
that can create symlinks or odd folder names and were skipped on this machine.

#### Motion (Module 3)

Compute the centroid velocity and acceleration of an ingested drive:

```powershell
python -m vl_data motion <hash12> --lake-dir warehouse
```

`motion` prints `drive`, `frames_read`, `frames_estimated` and `frames_null` (counts of frames; a
frame is null when its velocity or acceleration has no estimate). It uses the exit codes of
`ingest` (0 done, 1 refused or failed, 2 usage or configuration error, 3 a file could not be read
or written) and the same lake folder rule (`--lake-dir`, else `VL_LAKE_DIR`, `config/etl.yaml`,
`warehouse`). Window and order are in `config/motion.yaml`. The failures are listed in
`docs/USE_CASE_ERRORS.md`.

Real run (2026-10-25, full density: demo scenario, 1,000 frames x 1,000,000 points; times are
wall-clock, one machine). All exit codes were 0.

| Command | Time | Main output |
|---|---|---|
| `generate --scenario src/vl_data/generator/scenarios/demo.yaml --out <work>/raw` | 79 s | `frames: 1000`, `points_per_frame: 1000000` |
| `ingest <work>/raw/bf394b9bfb42 --lake-dir <work>/lake` | 56 s | `status: ok`, `frames_kept: 1000`, `keypoint_rows: 9000` |
| `motion bf394b9bfb42 --lake-dir <work>/lake` | 1 s | `frames_read: 1000 frames`, `frames_estimated: 1000 frames`, `frames_null: 0 frames` |
| `motion` again | 2 s | same; the motion partition file hash did not change |
| `pytest tests/ -q` | 66 s | 851 passed, 4 skipped (before the evaluation was removed; on 2026-10-27, with the ground truth removed from every module, the suite gives 798 passed, 4 skipped in 77 s) |

Query the motion table from Python. This example was run on 2026-10-25 (measured) with the small body
(100 cm cube, 10 points per axis) and the demo scenario, in a scratch lake. It opens your own lake
folder, so replace the placeholder path with yours. The exact query text and output are in
`.claude/run-proofs/M3-TASK-7-sql-example.log`:

```python
from vl_data.core.lake import Lake
from vl_data.sdk.client import VLClient

client = VLClient(Lake("<your lake folder>"))
client.sql("SELECT count(*) AS n FROM motion").column("n")[0].as_py()  # -> 1000
client.sql(
    "SELECT frame_id, round(velocity_cm_s, 1) AS velocity_cm_s FROM motion "
    "WHERE frame_id IN (0, 250, 500, 750, 999) ORDER BY frame_id"
).to_pylist()
```

`VLClient.sql` returns a pyarrow Table. The second query gave (measured):

```
[{'frame_id': 0, 'velocity_cm_s': 0.0}, {'frame_id': 250, 'velocity_cm_s': 600.0}, {'frame_id': 500, 'velocity_cm_s': -300.1}, {'frame_id': 750, 'velocity_cm_s': -0.0}, {'frame_id': 999, 'velocity_cm_s': 0.0}]
```

#### BI app (Module 4)

The app is built with Streamlit (a Python library that turns a script into a web page) and draws
its charts with Plotly (a Python charting library with hover and zoom). Start it from the
repository root, in PowerShell, with the project's virtual environment active. Set `VL_LAKE_DIR`
and `VL_RAW_DIR` only if your lake and raw folders are not the defaults (`warehouse` and
`data/raw`, both relative to the current folder); the two lines below are comments that show the
defaults, so remove the `#` and change the value only if you need to:

```powershell
# $env:VL_LAKE_DIR = 'warehouse'
# $env:VL_RAW_DIR = 'data/raw'
python -m streamlit run src/vl_data/bi/app.py --server.address 127.0.0.1 --server.headless true --browser.gatherUsageStats false
```

`--server.address 127.0.0.1` makes the app listen on this computer only. Without it, a server may
listen on all network interfaces, which means that any other computer that can reach yours over
the network could open the page. The SQL tab has no login, so do not start the app with
`--server.address 0.0.0.0` (all interfaces) and do not put it behind a port forward (a router or tunnel setting that
passes outside traffic to your computer).

To have a drive to look at, use the commands from the sections above (the demo drive, then load it
and compute its motion):

```powershell
python -m vl_data generate --scenario src/vl_data/generator/scenarios/demo.yaml --out data/raw
python -m vl_data ingest data/raw/bf394b9bfb42 --lake-dir warehouse
python -m vl_data motion bf394b9bfb42 --lake-dir warehouse
```

The page has four tabs: Generate, Clean, Analyze and SQL. The sidebar selector "Drive" lists the
drives in the lake. Each tab shows a badge "scenario hash: <first 12 hex>" for the selected drive
and a caption with all 64 characters. (On the Generate tab the badge is labelled "selected
drive" and shows the hash of the drive chosen in the sidebar, not the scenario in the form.)
With an empty lake the Generate tab has no badge and the other three tabs show a hint.

- Clean (read-only): the metrics "expected (frames)", "in (frames)", "kept (frames)", "rejected
  (frames)", "missing (frames)" and "keypoint rows (rows)"; a bar chart of the frame counts; a bar
  chart of quarantine rows per error code; and the quarantine table, which shows at most 200 rows
  and states the full count ("showing N of M quarantine rows"). If the drive is not ingested the
  tab prints the command to run, for example
  `python -m vl_data ingest '<raw>\<drive>' --lake-dir '<lake>'`, and does not run it.
- Analyze: draws only the stored motion: centroid x (cm), velocity (cm/s) and acceleration
  (cm/s^2) against time (s), from the lake; it needs no click and no raw folder. If the drive was
  ingested with status ok but has no stored motion, the tab prints
  `python -m vl_data motion <drive> --lake-dir '<lake>'`. If the drive is not ingested, or was
  rejected at ingest, it prints the `ingest` command instead. If the drive has more than one
  lineage row, it shows a problem text and no command (from reading the code, not run).
- SQL: a dropdown "Example query" with five examples (Quarantine rows per code; Peak speed
  (cm/s); Kept vs rejected frames; First 20 centroid positions (cm); All drives with status)
  fills the box "SQL (one SELECT only)". Run sends it to `VLClient.sql`, which runs it with DuckDB
  (an embedded SQL engine that reads the lake's Parquet files directly). The result table shows at
  most 10,000 rows and states the total ("showing 10,000 of N rows", otherwise "N rows"). A
  statement that is not a single SELECT, or a bad query, shows a one-line message.

What was run (2026-10-26, one machine, CPU): the tabs were tested with Streamlit's test runner on
a small drive (100 cm cube, 10 points per axis), and one real Streamlit server was started on
that drive by `.claude/run-proofs/module4_server_check.py` (local, git-ignored): the health
endpoint answered `ok`, the page answered HTTP 200, the server's own run of `app.py` answered
`ok`, the server log had no Traceback or Exception, and the port was free afterwards (the script
printed `SERVER_CHECK: PASS`, 8.4 s in all, measured on 2026-10-26 with a Python timer; log
`.claude/run-proofs/M4RM-server.log`, local, git-ignored). After the evaluation was removed, the
100 tests in `tests/bi` passed with 100% statement coverage of `vl_data.bi` (319 statements, 0
missed), and the full suite gave 958 passed, 4 skipped (all measured, 2026-10-26). The server check
proves the app starts and is served; it cannot click a widget, which is what the test runner
covers. The page was not opened in a browser by hand, and the full-density demo drive was not loaded in the app.

### Known issues

- No rotation. The vehicle never turns or yaws. A real vehicle rotates.
- A drive at full density is large: 12 GB (12,000,000,000 bytes) of points per 1,000 frames before compression (computed; 287 MB on disk with zstd, measured), and generating and round-tripping it take about 87 s (generate) and 55 s (round trip), measured in the fresh-clone run 1 on 2026-10-25. `generate()` and `write_drive` hold a whole drive in memory; use the `generate` command (it streams). A drive over 1 GiB (2^30 = 1,073,741,824 bytes) of points (more than 89 frames at full density) is refused by `generate()` and `write_drive`.
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
- The loader reads the frames file from disk twice: `verify_drive` hashes it once, then the scan reads it once more, in chunks (the ground-truth file is hashed once and never parsed). It is built for one machine, not for a stream. Measured peak memory was about 1.7 to 1.9 GiB at full density, while one Parquet row group is about 600 MB uncompressed (50 frames); the chunk size does not bound it. A drive with a row group above 2,048 MiB (`max_row_group_mib`) is rejected.
- Measured on 2026-10-25 (one machine, demo drive `bf394b9bfb42`, 1,000 frames x 1,000,000 points, row groups of 50 frames): the latest run took 44.7 s for the first ingest into a fresh lake and 43.6 s for a re-ingest, with a peak working set of 1,898 MiB (`.claude/run-proofs/TASK-4-measure-iter2b.log`). An earlier run measured 45.0 s, 42.9 s and 1,718 MiB (`TASK-4-measure.log`). One first run (`TASK-4-measure-iter2.log`) took 3,730 s while its CPU time was only about 54 s; the cause is unknown and it was not reproduced, so it is an open outlier (the next full-density run should log free memory and disk). The frame scan alone took 91 s and 1.59 GiB before the speed-up, and about 39 s per 1,000 frames (2.4 times faster) on a 200-frame benchmark after it.
- A drive folder is treated as hostile: a manifest above 2,000,000 points per frame (`max_points_per_frame`), a Parquet footer claiming more than 2 x expected frames + 1,000 rows, or a malformed `scenario_hash` is rejected with `INTEGRITY` before anything large is allocated, and at most 20 problems (plus one "and K more" row) are stored per drive. The row-group size check trusts the footer, so a compression bomb is a known gap.
- `frames_rejected` counts quarantine rows that name a file row, so a frame that breaks two rules counts twice; `frames_in` is not always `frames_kept + frames_rejected`. The ingest assumes the drive folder does not change while it runs.
- A drive whose frames file cannot be read, whose manifest cannot be read, or whose folder name is not its scenario hash is rejected with the code `INTEGRITY`, not with one of the six VL codes.
- The loader's speed limit (9,000 cm/s) is a Tesla's top speed of 322 km/h rounded up and is set in
  a YAML file. A faster vehicle would be flagged as impossible motion.
- Per-frame acceleration is not checked by any loader rule.
- The point-cloud check (VL-004) allows for the 1 cm rounding wobble of whole-centimetre points. A single point moved by exactly 1 cm is not detected; a move of 2 cm or more is.
- The loader decides which frame is a teleport by comparing positions. If the first three clean frames of a drive are themselves inconsistent, the loader trusts the most tightly agreeing pair; a drive that starts with several bad frames may be judged wrongly.
- Motion (Module 3): the motion table is not refreshed by a new ingest. After re-ingesting a drive, run `motion` again; until then the motion table holds the values of the earlier ingest.
- `VLClient.sql` runs any single SELECT, including `read_parquet` on any file the user can read. It is a local single-user tool; do not expose it to untrusted input (assumption M3-A10).
- The 16-frame least-squares fit smooths speed changes and the ends of a clip: frames within 8 frames of a change of acceleration, and the first and last 8 frames, carry larger errors (all-frame acceleration max 55.678 cm/s^2 against 4.378 cm/s^2 on interior frames, a development measurement against the in-memory truth of the demo drive, from `tools/motion_sweep.py`; assumption M3-A29).
- `tools/motion_sweep.py` is a developer measuring tool whose logic is not type-checked by mypy. It compares the estimator with the in-memory truth of generated drives; the product does not.
- Single machine, CPU only.
- Module 4, SQL tab: it runs any single SELECT through `VLClient.sql`, which can also read any file path on the machine through `read_parquet` (a DuckDB function that reads a Parquet file from any path); this is a local single-user tool, and production would use a locked connection. The "local single-user" premise depends on the app listening on 127.0.0.1 only (this computer): started on `0.0.0.0` (all network interfaces) or behind a port forward, anyone who can reach the port could use the SQL tab, which has no login. The wider reach below comes from reading the DuckDB defaults and documentation and was NOT run: other file types (`read_csv`, `read_json`, `read_text`, glob patterns), possible network reads through extension autoload (DuckDB loading an add-on such as `httpfs` by itself when a query needs it), Windows UNC paths (a UNC path is a Windows network-share path; opening one can send a login hash to that share), environment variables and secrets through `getenv` (a DuckDB function that returns an environment variable) and `duckdb_secrets()`, and no memory, thread or time limit. `COPY`, `INSERT`, `CREATE`, `ATTACH`, `INSTALL`, `SET` and `PRAGMA` are refused as non-SELECT (by the statement-type check in the SDK, read from the code; the tests cover DROP, COPY and a two-statement query, and check that the lake files are unchanged). No writes were found by reading, not by a run. The 10,000-row cap limits the display only; the whole result is loaded first. You accepted the wider reach on 2026-10-26 on condition that it is documented and a follow-up is written: `module4-TASK-4-1.md`.
- Module 4: the SQL box text is not refreshed when the selected drive changes (pick the example again), and the Run result is shown only in the run after the click, so any other widget action clears it.
- Module 4: the Generate tab badge shows the hash of the drive selected in the sidebar, not the scenario in the form.
- Module 4: run the app from the repository root, because the default lake and raw folders (`warehouse`, `data/raw`) are relative to the current folder.
- Module 4: the live-server check proves startup and serving only; widget behaviour (the Run button, the dropdown) is proven by Streamlit's test runner, and the page was not opened in a browser by hand.

### Coming soon

Rotation and yaw with a non-symmetric body, planar motion, optional noise, event detection and
clip candidates, Apache Iceberg as the table format, a REST API, streaming ingestion (for example
Kafka), parallel loading of many drives, a data-quality report function, a Spark backend for many
drives, an object-store profile, scheduled runs, and delta-encoded storage.

Module 3 follow-ups: event detection on the motion table, a REST API over the SDK, and a Kalman or RTS smoother as an alternative to the window fit.

Module 4 follow-ups: a locked SQL connection (external access off, extension autoload off, a memory limit), and a REST service if one is needed. The written follow-up file (in `.claude/plans/followups/`) is `module4-TASK-4-1.md` (SQL reach).
