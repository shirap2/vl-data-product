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
| VL-001 | Missing frame | A frame number is absent from the sequence. | `frame_id` values are not consecutive. | `DropFrame(frame_id)` |
| VL-002 | Timestamp gap | Two neighbouring frames are not 62,500,000 ns apart. | The timestamp difference is not exactly 62,500,000 ns. | `ShiftTimestamp(frame_id, delta_ns)` |
| VL-003 | Duplicate frame | The same frame number appears more than once. | A repeated `frame_id`. | `DuplicateFrame(frame_id)` |
| VL-004 | Corrupt point cloud | A frame's points are not a valid cube of `points_per_frame` points (1,000,000 at the shipped density). | The byte length is not `points_per_frame` x 3 x 4 (12,000,000 bytes at the shipped density), or the corner spacing is not the cube's. | `CorruptPoints(frame_id)` |
| VL-005 | Schema mismatch | The table does not have the expected columns and types. | The schema differs from the frames schema. | `BreakSchema()` |
| VL-006 | Impossible motion | The speed implied between two frames is beyond what the vehicle can do. | Centroid x change over 1/16 s exceeds the configured maximum speed or acceleration. | `Teleport(frame_id, dx_cm)` |

### Integrity errors (outside the VL codes)

The manifest check runs before any frame is read. It reports a changed file, a missing file,
or an extra file, and the load stops. These are not frame-level errors, so they have no VL
code.

### Generator-side limits

The generator itself rejects a scenario instead of writing a bad drive: a duration that is not
a whole number of frames, or a move whose start and end speed are both 0.

## 2. Architecture and design

Status as of 2026-10-23: the design below is agreed. Module 1 (the generator) has an approved
plan and is not built yet. Modules 2 to 4 are designed in outline and will get their own plans.
Anything marked "planned" has not been run.

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
| 2 Loader / ETL | Verify hashes, validate frames (VL-001 to VL-006), reduce each frame to 9 keypoints, write to the lake. | Contract agreed, plan pending |
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
config/default.yaml          fps, output folder, body, tolerances, fit parameters
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
- Measured on the 1,000-frame demo at 1,000,000 points per frame (a scratch script with the same 50-frame batching, `.claude/run-proofs/FULL-DENSITY-measure.log`): 75.1 s, peak working set 2,859 MiB, `frames.parquet` 287,134,433 bytes with zstd (4,024,944,648 bytes with snappy). The real `generate` command took 81 s including verification. The 2,000-frame cap was not run (about 24 GB raw, about 0.6 GB on disk and about 2.5 minutes, scaled from this run).

Units are centimetres, seconds, cm/s and cm/s^2 everywhere.

### Data model

| Table or file | Layer | Content |
|---|---|---|
| `frames.parquet` | raw drive | frame_id, timestamp_ns, points (1,000,000 x 3 int32 = 12,000,000 bytes at the shipped density, one binary column) |
| `ground_truth.parquet` | raw drive | frame_id, timestamp_ns, x_cm, velocity_cm_s, acceleration_cm_s2 |
| `manifest.json` | raw drive | schema version 2, drive hash, scenario, body (`size_cm`, `points_per_axis`), fps, counts, SHA-256 and size of each file |
| keypoints | bronze (planned) | 9 points per frame: 8 cube corners and the centroid |
| quarantine | bronze (planned) | rejected frames with their error code |
| lineage | bronze (planned) | scenario hash to file hashes to row counts |
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
| Hashing | SHA-256 over canonical JSON (scenario) and file bytes (files) | Standard, checkable with common tools | Content hashes, UUIDs | Needing stability across library versions |
| Raw file format | Parquet via PyArrow, one row per frame | Columnar, compact, readable by every tool | One row per point, CSV, HDF5 | Per-point SQL |
| Query | DuckDB over Parquet folders | SQL with no server | Trino, Athena | Many users, large data |
| ETL engine | pandas and numpy | Data is small, code is clear | Polars, Spark | Data no longer fits one machine |
| Lake format | Plain Parquet, Hive-style folders, behind one class | Nothing to run | Apache Iceberg, Delta Lake | Many writers, schema changes, time travel |
| Idempotent writes | Write to a temp folder, then swap the drive folder | Re-running never duplicates, a crash never loses old data | Blind append, upsert | Concurrent writers |
| Validation | Rule registry with error codes | Explicit and testable | Great Expectations, pandera | Many datasets and owners |
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

Integrity errors (a changed, missing or extra file) stop a load. Frame errors VL-001 to VL-006
send the frame to quarantine. See [ERROR_CODES.md](#1-error-codes).

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
| Loader / ETL | Verifies hashes, validates frames with error codes VL-001 to VL-006, keeps 9 keypoints per frame. | Planned |
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

Generate a drive from the browser (the Generate tab). Run on 2026-10-25 (Windows, Python 3.12.6,
Streamlit 1.66.1); only the server start and an HTTP check were run, the page was not opened in a
browser by hand:

```powershell
python -m streamlit run src/vl_data/generator/ui/generate_app.py --server.headless true --server.address 127.0.0.1 --server.port 8765
```

`--server.address 127.0.0.1` binds the page to this computer only; `--server.port 8765` is the port
that run used. Streamlit printed `Uvicorn server started on 127.0.0.1:8765` and `URL: http://127.0.0.1:8765`;
the page answered with HTTP 200 and `/_stcore/health` answered `ok` (measured, log
`.claude/run-proofs/TASK-7-iter3-server.log`, local, git-ignored, A28). The page has a
start form (`x_cm`, `y_cm`, `z_cm`, `speed_cm_s`, `seed`; this description of the page comes from the code, not from a browser run), a segment table that starts with the
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
wrong table (a blank cell, a duration that is not a whole number of frames, over 2,000 frames)
(also an infinite or true/false cell, or a cleared seed) shows a red error naming the row and no plot. Two Generate clicks
at the same time for a drive that does not exist yet can still both write (the race is not closed, A79). The tab is tested with Streamlit's test runner
(`tests/generator/test_ui.py`, 42 tests and 7.55 s, measured in
`.claude/run-proofs/TASK-7-1-iter2-ui-tests.log`; the whole suite was 305 tests, 100.00% coverage, measured in `TASK-7-1-iter2-suite.log`; both logs are local, git-ignored, A28), which cannot edit the table, so editing a row
by hand is not covered by a test.

Query and dashboard examples will be added when Modules 3 and 4 exist.

### Known issues

- No rotation. The vehicle never turns or yaws. A real vehicle rotates.
- A drive at full density is large: 12 GB of points per 1,000 frames before compression (about 287 MB on disk with zstd), and generating or checking it takes more than a minute. `generate()` and `write_drive` hold a whole drive in memory; use the `generate` command (it streams). A drive over 1 GiB of points (more than 89 frames at full density) is refused by `generate()` and `write_drive`.
- A scenario may have at most 2,000 frames (125 s at 16 fps). At full density that is about 0.6 GB on disk and about 2.5 minutes to generate (scaled from the 1,000-frame run, not run). The cap is a validation limit in `generator.models`.
- Motion is one-dimensional (along x).
- Values are rounded to whole centimetres, which adds small errors. Per-frame acceleration is too
  noisy to use directly, so it is fitted over windows.
- File hashes only match when the same pyarrow version wrote the files.
- Plain Parquet has no atomic multi-file commit, no schema-evolution tracking and no time travel.
  Writing a drive is not safe for concurrent writers.
- Test tolerances come from one demo drive.
- The two-way check (`roundtrip`) is only tested for segments of 32 frames (2 s) or more. Shorter
  segments can merge into one fitted piece or miss the end-speed limit; the command warns.
- Single machine, CPU only.

### Coming soon

Rotation and yaw with a non-symmetric body, planar motion, optional noise, event detection and
clip candidates, Apache Iceberg as the table format, a REST API, streaming ingestion, a Spark
backend for many drives, an object-store profile, scheduled runs, and delta-encoded storage.
