# Supporting documentation

Vehicle-location (VL) data product. This file holds the three supporting documents the
exercise asks for: the list of error codes, the architecture and design, and the release
notes. It is built from `docs/ERROR_CODES.md`, `docs/ARCHITECTURE.md` and
`docs/RELEASE_NOTES.md` by `tools/build_supporting_docs.py`. Edit those files, not this one.

## 1. Error codes

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

### Integrity errors (outside the VL codes)

The manifest check runs before any frame is read. It reports a changed file, a missing file,
or an extra file, and the load stops. The same applies to an unreadable manifest, an unsupported
schema version, a folder name that does not match the manifest hash, and a frame number outside
0 to `frame_count - 1`. These are not frame-level errors, so they have no VL code. They are
written to the quarantine table as one drive-level row each with the code `INTEGRITY`, and the
drive is marked `rejected` in the lineage table.

### Generator-side limits

The generator itself rejects a scenario instead of writing a bad drive: a duration that is not
a whole number of frames, or a move whose start and end speed are both 0.

## 2. Architecture and design

Status as of 2026-10-23: the design below is agreed. Module 1 (the generator) has an approved
plan and is not built yet. Module 2 (the loader and ETL) has an approved plan and is not built
either. Modules 3 and 4 are designed in outline and will get their own
plans. Anything marked "planned" has not been run.

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
| 1 Generator | Turn a start position and a motion profile into a hashed drive: point cloud, ground truth, manifest. Check itself in both directions. | Plan approved, not built |
| 2 Loader / ETL | Verify hashes, validate frames (VL-001 to VL-006), reduce each frame to 9 keypoints, write to the lake. | Plan approved, not built |
| 3 Use-case algorithm | Read keypoints, compute velocity and acceleration, write a motion table. Evaluate against ground truth. | Outline |
| 4 BI | One Streamlit app with three tabs: Generate, Clean, Analyze. | Outline |

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
  etl/                       Module 2 (planned): config, keypoints, facts, rules,
                             ingest, errors_doc, cli
  core/lake.py, lake_schemas.py   Module 2 (built): the Lake class and the table schemas
config/default.yaml          fps, output folder, body, tolerances, fit parameters
config/etl.yaml              Module 2 (planned): keypoint list, speed limit, chunk size
warehouse/                   the lake (planned, git-ignored)
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

### Data flow inside the generator

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
- Measured on the 1,000-frame demo at 1,000,000 points per frame (a scratch script with the same 50-frame batching, `.claude/run-proofs/FULL-DENSITY-measure.log`): 75.1 s, peak working set 2,859 MiB, `frames.parquet` 287,134,433 bytes with zstd (4,024,944,648 bytes with snappy). The real `generate` command took 81 s including verification. The 100,000-frame cap was not run.

Units are centimetres, seconds, cm/s and cm/s^2 everywhere.

### Data model

| Table or file | Layer | Content |
|---|---|---|
| `frames.parquet` | raw drive | frame_id, timestamp_ns, points (1,000,000 x 3 int32 = 12,000,000 bytes at the shipped density, one binary column) |
| `ground_truth.parquet` | raw drive | frame_id, timestamp_ns, x_cm, velocity_cm_s, acceleration_cm_s2 |
| `manifest.json` | raw drive | schema version 2, drive hash, scenario, body (`size_cm`, `points_per_axis`), fps, counts, SHA-256 and size of each file |
| keypoints | bronze (planned) | one row per keypoint: frame_id, timestamp_ns, keypoint name, x, y, z (cm, float64); 9 rows per frame |
| quarantine | bronze (planned) | one row per rejected frame or missing frame number: frame_id, row_index, code, message; a drive-level row has no frame_id |
| lineage | bronze (planned) | one row per drive: scenario hash, schema version, status, frame counts, keypoint rows, the frames and ground-truth file hashes |
| motion | silver (planned) | per-frame x, velocity, acceleration |
| evaluation tables | gold (planned) | errors against ground truth |

The warehouse tables use a generic, use-case-independent shape: a drive and frame key, a
timestamp, and numeric measures.

#### Core modules (built)

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

### Module 2: loader and ETL (lake built; the rest planned)

The lake storage (`core/lake.py`, `core/lake_schemas.py`) is built and tested, see the core modules
table above. The ETL settings and the keypoint pick are built and tested too (below). The steps,
rules and the ingest are design and have not been run. No number about them comes from a run.

#### ETL settings and keypoints (built)

| Module | What it holds | Rules worth knowing |
|---|---|---|
| `etl/config.py` | `EtlConfig`, `KeypointSpec`, `LimitsConfig`, `load_etl_config` | Read from `config/etl.yaml`. Unknown keys are rejected. Keypoint names use letters, digits and `_` (up to 64 characters). A keypoint index must be a real integer, so `true` or `9.0` is refused. `max_jump_cm(fps, frame_gap)` is `max_speed * frame_gap / fps + margin`, with the margin added once: 564.5 cm for one frame at 16 fps and 1,127.0 cm for two (checked by a test). Bounds: speed at most 100,000 cm/s, 256 keypoints, 1,024 MiB per chunk. A chunk is sized in MiB of point data (`chunk_mib`, default 128; peak memory is about 2 to 3 times that, because the payload and its decoded copy are both held), not in frames: `chunk_frames(points_per_frame)` gives 11,184 frames at 1,000 points per frame and 11 frames at 1,000,000 (checked by a test). A corner keypoint is written as its bits (x, y, z), each 0 or 1, so the same list works at any density. `VL_LAKE_DIR` overrides the lake folder when it is not empty. |
| `etl/keypoints.py` | `extract_keypoints` | Input is frames x points x 3 integers (cm). Output is frames x keypoints x 3 float64 (cm), in the order of the keypoint list. A corner is picked by lattice index; the centroid is the float64 mean of all points. Float input and out-of-range indices are refused. |
| `config/etl.yaml` | the nine keypoints, speed limit 9,000 cm/s, margin 2 cm, chunk size 256 | The built-in defaults in `etl/config.py` are identical to this file (checked by a test). |

The corner lattice indices are derived from the number of points per axis n as `bx*(n-1)*n*n + by*(n-1)*n + bz*(n-1)`: 0, 9, 90, 99, 900, 909, 990, 999 at n = 10, and 0, 99, 9900, 9999, 990000, 990099, 999900, 999999 at n = 100 (both checked by a test). The ETL gets n from the drive: `etl/body.py` takes it from the manifest's `points_per_frame` (the integer cube root) and the cube size from the core config, until the generator adds body keys to the manifest. A timing check on 2026-10-24 showed `extract_keypoints` takes 0.045 s for 4 frames of 1,000,000 points (48 MB) on the development machine (Windows 11, 15.7 GB RAM).

#### Frame scan and rules (built)

| Module | What it holds | Rules worth knowing |
|---|---|---|
| `etl/facts.py` | `scan_frames` | Streams `frames.parquet` in chunks and returns one small record per row: frame number, timestamp, payload size, payload SHA-256, whether the cloud is valid, centroid. A bad payload never raises; a null frame number or timestamp, or a missing column, raises one `ValueError` (the caller checks the schema first). It never opens the ground-truth file. The size is checked before the payload is decoded. |
| `etl/rules.py` | `RULES`, `RuleContext`, `run_rules` | Six rules with their codes, meaning, detection text and example fault. Drive rules (VL-005) and frame rules (VL-001 to VL-004, VL-006). VL-006 runs only on frames that passed the others. |
| `etl/body.py` | `expected_frame_count` | The number of frames the scenario implies, used by VL-001. |

On 2026-10-24 the ETL package had 151 tests (381 in the whole suite), and line coverage of the ETL package was 99%. Running the rules on the demo drive (1,000 frames on a 125-point test body) gave no violations, and each of the six generator faults produced exactly its own code. The full-density behaviour of the scan is not measured yet.

On 2026-10-23 the settings and keypoint modules had 71 tests (201 in the whole suite), and line coverage of the ETL package was 100%.

What goes in: a drive folder `data/raw/<hash12>/` with the full point cloud (1,000 points per
frame). What comes out: three tables in the lake, `keypoints`, `quarantine` and `lineage`, each
stored as `<table>/drive=<hash12>/part-0.parquet` and queryable with DuckDB.

Steps for one drive:

1. **Integrity gate.** Read the manifest and check every file hash, the schema version and the
   folder name. Any failure rejects the whole drive before a frame is read.
2. **Schema gate.** Compare the frames file's columns with the agreed frames schema (VL-005).
3. **Scan, pass 1.** Read the frames file in chunks. For each frame keep a small record: frame
   number, timestamp, payload size, payload hash, whether the points form the expected cube,
   and the centroid. Memory stays small because only these records are kept.
4. **Rules.** Run VL-001 to VL-004 on the records. Run VL-006 only on frames that passed those.
   VL-006 first picks a trusted starting frame: among the first three clean frames it takes the
   pair that agrees most tightly (distance as a fraction of the allowed jump) and starts from the
   earlier frame of that pair, so a teleported first frame cannot poison the rest. After that it
   compares each centroid with the last accepted frame.
5. **Reduce, pass 2.** Read the file again, keep the accepted frames, and pick the 8 corners by
   index plus the centroid.
6. **Write.** Write each table's new partition aside, then swap it in for the old one.

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
A scenario may have at most 100,000 frames (about 1 h 44 min at 16 fps).

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
| Hashing | SHA-256 over canonical JSON (scenario) and file bytes (files) | Standard, checkable with common tools | Content hashes, UUIDs | Needing stability across library versions |
| Raw file format | Parquet via PyArrow, one row per frame | Columnar, compact, readable by every tool | One row per point, CSV, HDF5 | Per-point SQL |
| Query | DuckDB over Parquet folders | SQL with no server | Trino, Athena | Many users, large data |
| ETL engine | pandas and numpy | Data is small, code is clear | Polars, Spark | Data no longer fits one machine |
| Lake format | Plain Parquet, Hive-style folders, behind one class | Nothing to run | Apache Iceberg, Delta Lake | Many writers, schema changes, time travel |
| Idempotent writes | Write to a temp folder, then swap the drive folder | Re-running never duplicates, a crash never loses old data | Blind append, upsert | Concurrent writers |
| Validation | Rule registry with error codes | Explicit and testable; the same registry builds the error-code document | Great Expectations, pandera | Many datasets and owners |
| Keypoint table layout (planned) | Long: one row per keypoint | Generic; the keypoint list is a YAML change; simple SQL filter by name | Wide: 27 coordinate columns per frame | Heavy column maths on the corners |
| Bad frames (planned) | Quarantine only the failed frame, with a details-only row | One bad frame does not cost the drive; the raw frame stays in `data/raw/` | Reject the whole drive; copy the raw bytes | A consumer needs replay from the lake alone |
| Ingest passes (planned) | Two chunked passes: facts, then keypoints | Bounded memory, and rules see the whole sequence | One pass holding the drive in memory; Spark | Drives no longer fit one machine |
| BI | Streamlit and Plotly | One Python file per view | Superset, Grafana | Non-Python users |
| CLI | Typer | Typed commands | argparse | None |
| Tests | pytest and hypothesis | Property tests suit a two-way check | unittest | None |
| Not used | Kafka, Spark, Airflow, dlt, Docker | No purpose at this scale | See "Scaling" | See "Scaling" |

### How the simulator is checked

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

### Fault injection

`generator/faults.py` builds bad drives on purpose so Module 2 can be tested against known errors.
`inject_faults(drive, faults)` returns a new `Drive` and a dict of counts per error code. Only the
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
`BreakSchema`; `delta_ns == 0`; `dx_cm == 0`; a `delta_ns` that leaves int64 or makes the
timestamp equal to the row before or after; any two rows (a `DuplicateFrame` copy excepted) that
end with the same timestamp; `CorruptPoints` on a frame with under 12 bytes; and a `dx_cm` that
overflows int32. Each fault is intended to trigger its own code; Module 2 behaviour is not yet
verified.

### Error handling

Integrity errors (a changed, missing or extra file, an unreadable manifest, an unsupported schema
version) reject the whole drive, as does VL-005. A rejected drive gets one drive-level quarantine
row, a lineage row with status `rejected`, and no keypoints. Frame errors VL-001 to VL-004 and
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

Status as of 2026-10-23: the project skeleton is built (install, `--version`, help); the generator
and the other modules are not. This draft is filled in as each module lands, and
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
| Generator | Writes a hashed drive: 16 fps point cloud, ground truth, manifest. Checks itself by recovering the scenario from the data. | Plan approved |
| Loader / ETL | Verifies hashes, validates frames with error codes VL-001 to VL-006, keeps 9 keypoints per frame, and writes keypoints, quarantine and lineage tables to a queryable lake. | Plan approved, not built |
| Use-case algorithm | Computes velocity and acceleration and compares them with ground truth. | Planned |
| BI | One Streamlit app: generate, clean, analyze. | Planned |

### Install and first check (run on 2026-10-23, Windows PowerShell, Python 3.12.6)

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -m vl_data --version
```

The last command prints `vl_data 0.1.0` and exits with code 0. Running `python -m vl_data` with no
arguments prints the help text and exits with code 2. Python 3.11 or newer is required; only
3.12 has been tested.

### Usage

Run these with the project's virtual environment active, or call `.venv\Scripts\python.exe` in place of `python`.

Generate the demo drive (1,000 frames, one reversal, two stops). Run on 2026-10-23, it took
about 1.5 s:

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
`ground_truth.parquet` (30,929 bytes) and `manifest.json` (1,454 bytes). The run (2026-10-24, Python 3.12.6, pyarrow 26.0.0, numpy 2.5.4) took 81 s including verification; the 1,000-frame drive holds 12 GB of points before compression. `--out` is optional; without
it the drive goes to `raw_dir` from `config/default.yaml` (or `VL_RAW_DIR`). Generating the same
scenario again replaces the folder with identical files.

Check a drive against its manifest:

```powershell
python -m vl_data verify data/raw/bf394b9bfb42
```

This prints `manifest: verified` and exits with code 0. After one byte of `ground_truth.parquet`
was changed in a copy, it printed `changed: ground_truth.parquet (sha256 mismatch)` on stderr and
exited with code 1. A missing or invalid scenario file gives a one-line `error: ...` message and
exit code 1.

Check that the generator can read its own drive back (the two-way check). Run on 2026-10-24,
it took 1.41 s wall time and writes nothing to disk (log: `.claude/run-proofs/TASK-5-measure.log`, local, git-ignored (A28)):

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

The same log records the exit code (0) and the demo-drive errors against ground truth: maximum centroid
x error 0.498 cm and maximum velocity error 15.0 cm/s (`.claude/run-proofs/TASK-5-measure.py`).

The limits are 6 frames, 8 frames and 40 cm/s (`tolerances` in `config/default.yaml`). If a
check fails, the segment line and one line per problem are printed (problems on stderr) and the
exit code is 1. A missing or invalid scenario file gives a one-line `error: ...` message and
exit code 1.

The check is only tested for segments of 32 frames (2 s) or more (A14). For every segment under
32 frames the command prints one line such as `warning: segment 1 is 16 frames; ...` on stderr
before the result. The demo prints no warning, because its shortest segment is exactly 32 frames.

A scenario file looks like this:

```yaml
seed: 0
start: {x_cm: 0, y_cm: 0, z_cm: 0, speed_cm_s: 0}
segments:
  - {type: stop, duration_s: 2.0}
  - {type: move, duration_s: 6.0, end_speed_cm_s: 600}
  - {type: move, duration_s: 8.0, end_speed_cm_s: -300}
  - {type: stop, duration_s: 4.0}
```

Speeds are cm/s and positive means forward. `move` changes speed in a straight line from the
current speed to `end_speed_cm_s`. Durations must be a whole number of frames (a multiple of
1/16 s).

Load the drive into the lake (planned, not yet run):

```powershell
python -m vl_data ingest data/raw/<hash12>
python -m vl_data ingest --all data/raw
python -m vl_data error-codes --check
```

`ingest` prints a summary (frames expected, kept and quarantined, keypoint rows) and exits 0 when
the drive was loaded and 1 when it was rejected. The lake is queried with DuckDB over
`warehouse/`. The plan expects `SELECT keypoint, count(*) FROM keypoints GROUP BY 1` to return 9
keypoints per frame; that has not been run yet.

Query and dashboard examples for the use case and the app will be added when Modules 3 and 4
exist.

### Known issues

- No rotation. The vehicle never turns or yaws. A real vehicle rotates.
- A drive at full density is large: 12 GB of points per 1,000 frames before compression (about 287 MB on disk with zstd), and generating or checking it takes more than a minute. `generate()` and `write_drive` hold a whole drive in memory; use the `generate` command (it streams).
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
- The loader reads each drive twice, in chunks. It is built for one machine, not for a stream.
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
