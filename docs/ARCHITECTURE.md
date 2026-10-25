# Architecture and design

Status as of 2026-10-25: Modules 1 (generator), 2 (loader and ETL) and 3 (use case) are built and
checked by tests and by a full-density run. Module 4 (the BI app) is planned. Anything marked
"planned" has not been run.

## What the product does

It is a small end-to-end data product for a vehicle-location signal: a mocked data source, an
ETL, a queryable warehouse, and a BI view. All data is synthetic. The vehicle is a 1 m cube
that moves in a straight line along x. The use case is extracting its velocity and
acceleration from the point cloud alone and showing that the result matches the known truth.

## Modules

```
 [1 Generator] -> data/raw/<hash12>/ -> [2 ETL] -> lake -> [3 Use case] -> lake
                                                    |                      |
                                                    +---- SDK + SQL <------+
                                                              |
                                                       [4 BI: Streamlit]
```

| Module | Job | Status |
|---|---|---|
| 1 Generator | Turn a start position and a motion profile into a hashed drive: point cloud, ground truth, manifest. Check itself in both directions. | Built |
| 2 Loader / ETL | Verify hashes, validate frames (VL-001 to VL-006), reduce each frame to 9 keypoints, write to the lake. | Built |
| 3 Use-case algorithm | Read keypoints, compute velocity and acceleration, write a motion table. Evaluate against ground truth. | Built |
| 4 BI | One Streamlit app with three tabs: Generate, Clean, Analyze. | Planned |

Shared code lives in `core/`: config loader, Arrow schemas, hashing, the manifest, and later the
`Lake` class. Imports only point down: entry points, then modules, then core. Core never imports a
module.

## Folder structure

```
src/vl_data/
  __main__.py, cli.py        entry point and root command app
  core/                      config, hashing, schemas, manifest
  generator/                 Module 1: models, body, motion, drive, writer,
                             inverse, faults, cli, scenarios/, ui/
  etl/                       Module 2 (built): config, keypoints, facts, rules,
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
(`drive.py`, `writer.py`) and the generator's `generate` and `verify` commands (`generator/cli.py`,
registered by the root `cli.py`). The back direction (`inverse.py`) and the `roundtrip` command are built, and so is the fault-injection helper (`faults.py`). Not built yet: the Generate tab.

## Data flow inside the generator

1. Load and validate the scenario YAML (start values and a list of `move` and `stop` segments).
2. Hash it: SHA-256 of the canonical JSON of the body (`size_cm`, `points_per_axis`) and the scenario gives the drive hash.
3. Build the speed for every frame (a straight-line ramp inside each segment), integrate the
   position, and derive ground-truth velocity and acceleration by differences.
4. Build each frame's points: a 100x100x100 lattice (1,000,000 points) shifted by the rounded position.
5. Write the frames and ground truth to a temporary folder, build the manifest from the file
   hashes, swap the folder into place, and verify it.

`generate` in `drive.py` does steps 2 to 4 in memory and returns a `Drive` (scenario, hash, frames
table, ground-truth table) by collecting `iter_frame_batches`, which yields record batches of 50 frames; at full density this is only for small drives. `write_drive` in `writer.py` does step 5 for such a drive. `generate_to_disk` streams the batches straight into the Parquet file (memory near one 600 MB batch), writes the ground truth and manifest, and then runs `verify_drive`; the command line uses `generate_to_disk`.

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
- Measured on the 1,000-frame demo at 1,000,000 points per frame (a scratch script with the same 50-frame batching, `.claude/run-proofs/FULL-DENSITY-measure.log`): 75.1 s, peak working set 2,859 MiB, `frames.parquet` 287,134,433 bytes with zstd (4,024,944,648 bytes with snappy). The real `generate` command took 81 s including verification. The 2,000-frame cap was not run (about 24 GB raw, about 0.6 GB on disk and about 2.5 minutes, scaled from this run).

Units are centimetres, seconds, cm/s and cm/s^2 everywhere.

## Data model

| Table or file | Layer | Content |
|---|---|---|
| `frames.parquet` | raw drive | frame_id, timestamp_ns, points (1,000,000 x 3 int32 = 12,000,000 bytes at the shipped density, one binary column) |
| `ground_truth.parquet` | raw drive | frame_id, timestamp_ns, x_cm, velocity_cm_s, acceleration_cm_s2 |
| `manifest.json` | raw drive | schema version 2, drive hash, scenario, body (`size_cm`, `points_per_axis`), fps, counts, SHA-256 and size of each file |
| keypoints | bronze (built) | one row per keypoint: frame_id, timestamp_ns, keypoint name, x, y, z (cm, float64); 9 rows per frame |
| quarantine | bronze (built) | one row per rejected frame or missing frame number: frame_id, row_index, code, message; a drive-level row has no frame_id |
| lineage | bronze (built) | one row per drive: scenario hash, schema version, status, frame counts, keypoint rows, the frames and ground-truth file hashes |
| motion | silver (built) | per-frame x, velocity, acceleration |
| evaluation tables | gold (planned) | errors against ground truth |

The warehouse tables use a generic, use-case-independent shape: a drive and frame key, a
timestamp, and numeric measures.

### Core modules (built)

| Module | What it holds | Rules worth knowing |
|---|---|---|
| `core/config.py` | `Config` (fps, raw_dir, body, tolerances, fit) and `load_config` | Unknown keys and non-positive values are rejected. `fps` must divide 1,000,000,000 ns evenly (16 fps gives 62,500,000 ns). The environment variable `VL_RAW_DIR` overrides `raw_dir`. |
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

## Module 2: loader and ETL (ingest, command line and error-code file built; full-density command-line run is the user's proof)

The lake storage (`core/lake.py`, `core/lake_schemas.py`), the ETL settings, the keypoint pick, the
frame scan, the rules, the ingest (`etl/ingest.py`), the commands (`etl/cli.py`) and the
error-code document generator (`etl/errors_doc.py`) are built and tested on the small body.

### Command line (built)

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

### ETL settings and keypoints (built)

| Module | What it holds | Rules worth knowing |
|---|---|---|
| `etl/config.py` | `EtlConfig`, `KeypointSpec`, `LimitsConfig`, `load_etl_config` | Read from `config/etl.yaml`. Unknown keys are rejected. Keypoint names use letters, digits and `_` (up to 64 characters). A keypoint index must be a real integer, so `true` or `9.0` is refused. `max_jump_cm(fps, frame_gap)` is `max_speed * frame_gap / fps + margin`, with the margin added once: 564.5 cm for one frame at 16 fps and 1,127.0 cm for two (checked by a test). Bounds: speed at most 100,000 cm/s, 256 keypoints, 1,024 MiB per chunk. A chunk is sized in MiB of point data (`chunk_mib`, default 128), not in frames: `chunk_frames(points_per_frame)` gives 11,184 frames at 1,000 points per frame and 11 frames at 1,000,000 (checked by a test). The chunk is the batch size asked of Parquet; it does not bound memory, because pyarrow reads a whole row group. `max_row_group_mib` (default 2,048) is the largest uncompressed row group the ingest will read; a larger one rejects the drive. A corner keypoint is written as its bits (x, y, z), each 0 or 1, so the same list works at any density. `VL_LAKE_DIR` overrides the lake folder when it is not empty. |
| `etl/keypoints.py` | `extract_keypoints` | Input is frames x points x 3 integers (cm). Output is frames x keypoints x 3 float64 (cm), in the order of the keypoint list. A corner is picked by lattice index; the centroid is the float64 mean of all points. Float input and out-of-range indices are refused. |
| `config/etl.yaml` | the nine keypoints, speed limit 9,000 cm/s, margin 2 cm, chunk size 128 MiB, row-group cap 2,048 MiB | The built-in defaults in `etl/config.py` are identical to this file (checked by a test). |

The corner lattice indices are derived from the number of points per axis n as `bx*(n-1)*n*n + by*(n-1)*n + bz*(n-1)`: 0, 9, 90, 99, 900, 909, 990, 999 at n = 10, and 0, 99, 9900, 9999, 990000, 990099, 999900, 999999 at n = 100 (both checked by a test). The ETL gets n from the drive: `etl/body.py` takes it from the manifest's `points_per_frame` (the integer cube root) and the cube size from the core config, until the generator adds body keys to the manifest. A timing check on 2026-10-24 showed `extract_keypoints` takes 0.045 s for 4 frames of 1,000,000 points (48 MB) on the development machine (Windows 11, 15.7 GB RAM).

### Frame scan and rules (built)

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

### What "not atomic" means for the three tables

A re-ingest replaces the drive's partition in `keypoints`, in `quarantine` and in `lineage`. Each
replacement is safe on its own: the new data is written aside, the old folder is renamed out of
the way, the new folder is renamed in, and the old one is deleted. If the new data fails while
being written, the old partition stays. But the three tables are replaced one after another,
and plain Parquet has no commit that covers all three. A crash after the keypoints swap and
before the lineage swap leaves new keypoints next to the old lineage row. Ingesting the drive
again fixes it, because the ingest is idempotent.

### Motion model (built)

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

## Design choices and alternatives

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
| BI | Streamlit and Plotly | One Python file per view | Superset, Grafana | Non-Python users |
| CLI | Typer | Typed commands | argparse | None |
| Tests | pytest and hypothesis | Property tests suit a two-way check | unittest | None |
| Not used | Kafka, Spark, Airflow, dlt, Docker | No purpose at this scale | See "Scaling" | See "Scaling" |

## How the simulator is checked

The generator is tested in both directions. Forward: scenario to frames. Back: `infer_track`
recovers per-frame position and velocity from the frames, `fit_segments` fits move and stop
pieces to them, and `compare_scenarios` checks the result against the original within stated
limits. The back direction reads frames only, never ground truth.

Limits measured on the demo drive (2026-10-24; run log `.claude/run-proofs/TASK-5-measure.log`, local, git-ignored (A28), from `python -m vl_data roundtrip`
and `.claude/run-proofs/TASK-5-measure.py`), and the limit set in `config/default.yaml`:

| Quantity | Measured max | Test limit |
|---|---|---|
| Position error (x) | 0.498 cm | 0.6 cm |
| Velocity error per frame | 15.0 cm/s | 17 cm/s |
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

## Fault injection

`generator/faults.py` builds bad drives on purpose so Module 2 can be tested against known errors.
`inject_faults(drive, faults, frame_interval_ns=62_500_000)` returns a new `Drive` and a dict of counts per error code. Only the
frames table changes; the ground truth, the scenario and the scenario hash are copied unchanged, and
the input drive is not modified. A faulty drive can be written with `write_drive` and still passes
`verify_drive`, because the manifest is built from the faulty file.

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
`BreakSchema`; a `delta_ns` that is not an int, is 0, leaves int64 or is not smaller than one frame interval
(`frame_interval_ns`, ns) in size; `dx_cm == 0`; an input table whose own timestamps are equal or
not increasing; final timestamps of the surviving rows that are not strictly increasing in original
row order (dropped rows and `DuplicateFrame` copies are left out); `CorruptPoints` on a frame with under 12 bytes; and a `dx_cm` that
overflows int32. Each fault is intended to trigger its own code; Module 2 behaviour is not yet
verified.

## Error handling

Integrity errors reject the whole drive, as does VL-005. They are: a changed, missing or extra
file (one `INTEGRITY` row per problem), an unreadable manifest or a schema-1 manifest without a
body, an unsupported schema version, a folder name that is not the first 12 hex of the scenario
hash, an fps that does not divide a second, an invalid body or scenario, a frames file that is not
listed or cannot be read, a Parquet row group above `max_row_group_mib`, and a frame number outside
0 to expected count minus 1. A rejected drive gets its drive-level quarantine rows, a lineage row
with status `rejected`, and no keypoints. A folder name that is not a valid drive id and a keypoint
spec that does not fit the body raise `ValueError` and write nothing. Frame errors VL-001 to VL-004 and
VL-006 send only the failed frame to quarantine. See [ERROR_CODES.md](ERROR_CODES.md).

## Module 3: motion use case (built)

`vl_data.sdk.VLClient(lake: Lake)` is the read-only way to the lake. `drives() -> list[str]`
lists the drives that have keypoints. `keypoints(drive, keypoint=None)`, `motion(drive)`,
`lineage(drive)` and `quarantine(drive)` return Arrow tables with exactly the table schema
(no `drive` column); keypoints are ordered by `frame_id`, then keypoint name. A valid unknown
drive gives an empty table; an invalid drive id (for example `a/b` or empty) raises
`ValueError` from the Lake. `sql(query) -> pa.Table` runs one SELECT over the table views (which do have a `drive`
column) and closes its DuckDB connection after every query. The SDK never opens ground truth.

The `motion` table (`MOTION_SCHEMA`) has one row per frame: `frame_id` int64, `timestamp_ns`
int64, `x_cm` float64, `velocity_cm_s` float64 (null where there is no estimate),
`acceleration_cm_s2` float64 (null likewise) and `n_points` int32 (samples in the estimator
window).

### Estimator defaults, measured (sweep)

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
velocity and acceleration from the in-memory ground truth (no file and no lake is read). Errors are
estimate minus truth. "int" columns use interior frames (no change of true acceleration within half
a window of the frame, the last frame excluded; the same function `evaluate.interior_mask` that
`evaluate_drive` uses); "all" columns use every frame with an estimate. The baseline row uses
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
Tolerances (x1.5 of the larger, rounded up to 0.1): velocity 19.4 cm/s, acceleration 31.9 cm/s^2
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

Tolerances are 1.5 times the largest measured interior max error, rounded up to 0.1 (never below
the measured value, never zero). The measured value is the largest of the chosen cell on the two
sweep drives and on a seeded random-scenario search. The sweep drives start and end at rest, so
they never test slow motion (where the integer-cm rounding of the centroid is a staircase) or a
clip that ends while moving (the first and last 8 frames use a one-sided window). The search
(`--random-search N`, default 1,500, `--seed`, default 0) draws N scenarios with numpy's RNG (up to
5 legs of 48 to 96 frames, end speeds up to 400 cm/s) and always adds two pinned scenarios, the
counterexamples the Hypothesis test found: one 48-frame move from 0 to 2 cm/s (interior
acceleration max error 12.909 cm/s^2) and five 96-frame moves towards -68 cm/s (19.530 cm/s^2);
the sweep drives alone would give only 4.378 cm/s^2 x 1.5 = 6.6 cm/s^2. Measured over the 1,502
scenarios of the run above (seed 0): worst interior velocity 8.848 cm/s and acceleration
21.212 cm/s^2. The tolerances are therefore velocity 12.902 x 1.5 = 19.4 cm/s (sweep drives) and
acceleration 21.212 x 1.5 = 31.9 cm/s^2. Other seeds, 1,500 scenarios each (measured, not used
for the YAML): seed 1 gives 9.806 cm/s and 25.364 cm/s^2, seed 2 gives 10.669 and 19.818, seed 3
gives 12.381 and 19.896, all below the tolerances. The velocity error of about 12.5 cm/s on the
hard drive is the forward-difference truth against the centred fit, a/32 for a = 400 cm/s^2
(assumption M3-A17).

Full-density check (measured once, 2026-10-25, 300 frames; the block is an excerpt of the command
output, the first lines of the report):

```
python tools/motion_sweep.py --full-density-check 300
Full-density check: 300 frames at 100 points per axis, 248 interior, 0 null; passed=True
                       int vel max int vel rms int acc max int acc rms
full density                 6.549       2.990       4.681       1.081
10 points per axis           6.549       2.990       4.681       1.081
Largest centroid x difference, full vs small lattice: 0.000000 cm
Interior position error (full density): 0.4844 cm max
Units: velocity cm/s, acceleration cm/s^2.
```

The 300 frames were generated at 100 points per axis with `generate_to_disk`, ingested into a temp
lake, run through `compute_motion` and `evaluate_drive`, and the temp folders deleted. In this
one run the interior errors match the small-lattice run of the same scenario to every printed
digit and the centroid x values are identical (measured once on 300 frames). A unit test checks
the identity behind it, centroid x = 49.5 + rint(x) for 2, 10 and 100 points per axis, on single
frames. The check took 37.8 s in total. The tool limits N to 600 frames, prints the estimated
size (12 MB per frame) and refuses to start when free disk space is under twice that estimate.

### Data flow

```
raw drive folder --ingest--> lake: keypoints, quarantine, lineage
lake keypoints (centroid rows) --motion--> lake: motion
motion + raw drive ground truth --evaluate--> printed report (not stored)
VLClient.sql / keypoints / motion  <-- read the lake
```

`motion <drive_id>` reads the drive's lineage row (it must exist, be the only one, and have status
`ok`) and its centroid keypoint rows through `VLClient`, fits the sliding window on the real
timestamps, and replaces the drive's motion partition in one swap. It never opens the raw drive
or the ground truth. `evaluate <drive_dir>` is the only code that reads the ground truth
(`vl_data.usecase.evaluate`; a test greps the rest of `vl_data.usecase` and `vl_data.sdk` for it).
It checks the drive files and hashes first, then the lineage hashes, then compares the stored
motion with the truth frame by frame and returns the report; nothing is stored.

### Command line (built)

```powershell
python -m vl_data motion <drive_id> [--lake-dir DIR]
python -m vl_data evaluate <drive_dir> [--lake-dir DIR]
```

The lake folder is `--lake-dir`, else `VL_LAKE_DIR`, else `lake_dir` in `config/etl.yaml`, else
`warehouse` (the same resolution as `ingest`, through `load_etl_config()`). The window, order and
tolerances come from `config/motion.yaml`. Exit codes follow `ingest`: 0 done (evaluate: passed),
1 refused or failed (`MotionError`, `EvaluationError`, or a tolerance exceeded), 2 usage or
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

> python -m vl_data evaluate <work>/raw/bf394b9bfb42 --lake-dir <work>/lake   (exit 0, 2 s)
scenario_hash: bf394b9bfb421fda860ab97fbab54d5c36c995a661e770969cbffd9a2afa95c1
frames_compared: 1000 frames
frames_interior: 880 frames
frames_null: 0 frames
position (cm): interior max 0.498 rms 0.217 | tolerance 0.6
velocity (cm/s): interior max 3.520 rms 1.460 | tolerance 19.4 | all frames max 11.412 rms 1.836
acceleration (cm/s^2): interior max 4.378 rms 1.000 | tolerance 31.9 | all frames max 55.678 rms 6.240
result: PASS
```

A second `motion` run left the motion partition file byte-identical (sha256
`fdab85419d40c367dc6486db5c5b4951a68d5e2cb6d12f3f29a2b9ce96b796e0` both times, measured). The full
suite after the run: 851 passed, 4 skipped in 66 s (measured). The log is local and git-ignored:
`.claude/run-proofs/M3-TASK-7-iter1.log`.

### Evaluation

A frame is interior when no change of true acceleration lies within half a window (8 frames at
the default 16) of it; the last frame never is. The pass test uses interior frames only: position
error at most `tolerances.position_cm` in `config/default.yaml`, velocity error at most
`tolerances.velocity_cm_s` and acceleration error at most `tolerances.acceleration_cm_s2` in
`config/motion.yaml`. The all-frame errors are printed for information. In the run above the
all-frame acceleration maximum (55.678 cm/s^2) is above the tolerance because frames next to a
change of acceleration are included; the interior maximum (4.378 cm/s^2) is the gated number.

### Technology reasons and alternatives

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

## Storage math

A full-density frame is 100 x 100 x 100 = 1,000,000 points. At 3 coordinates of 4 bytes each
that is 12 MB per frame, 192 MB per second at 16 fps, and about 691 GB per hour (computed). The
1,000-frame demo is therefore 12 GB before compression (computed) and 287,134,433 bytes on disk with zstd (measured), about 42 times smaller (computed). A production design would store a keyframe and the ego-motion change
per frame instead of every point.

## Scaling to production

Apache Iceberg or Delta on object storage with a managed catalog, Spark or Flink for large
drive counts, keyframe-plus-delta storage, schema contracts between producer and consumers,
partitioning and compaction, and scheduled runs with an orchestrator.

## Limits of this design

- No rotation. A real vehicle rotates, and reading its orientation needs a non-symmetric body
  or tracked points.
- Plain Parquet has no atomic commit across files, no schema-evolution tracking and no time
  travel. The folder swap protects one drive at a time and is not safe for concurrent writers.
- File hashes depend on the pyarrow version that wrote the files.
- A re-ingest swaps three tables one after another, not as one atomic commit (see the section on
  what "not atomic" means above). Re-ingesting repairs a crash.
- The speed limit of 9,000 cm/s is a Tesla's top speed (322 km/h, 8,944 cm/s) rounded up. A
  faster vehicle would be flagged as impossible motion.
- Tolerances come from two sweep drives and a random-scenario search (see the sweep section) and
  may widen when other scenarios are tried; the sweep drives start and end at rest.

The full assumption list is in [ASSUMPTIONS.md](ASSUMPTIONS.md).
