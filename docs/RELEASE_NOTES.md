# Release notes: version 0.1.0 (draft)

Status as of 2026-10-23: the project skeleton is built (install, `--version`, help); the generator
and the other modules are not. This draft is filled in as each module lands, and
every command and number in it must come from a real run before submission. Items marked
"planned" have not been run.

## Product description

A small data product for a vehicle-location signal. It generates a mock drive from a start
position and a motion profile, stores the point cloud, extracts the vehicle's velocity and
acceleration from the points alone, and shows the result next to the known truth. The vehicle
is a 1 m cube that moves in a straight line, forwards, backwards and stopped, at different
speeds. All data is synthetic.

## What is in this version

| Part | What it does | Status |
|---|---|---|
| Generator | Writes a hashed drive: 16 fps point cloud, ground truth, manifest. Checks itself by recovering the scenario from the data. | Plan approved |
| Loader / ETL | Verifies hashes, validates frames with error codes VL-001 to VL-006, keeps 9 keypoints per frame. | Planned |
| Use-case algorithm | Computes velocity and acceleration and compares them with ground truth. | Planned |
| BI | One Streamlit app: generate, clean, analyze. | Planned |

## Install and first check (run on 2026-10-23, Windows PowerShell, Python 3.12.6)

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -m vl_data --version
```

The last command prints `vl_data 0.1.0` and exits with code 0. Running `python -m vl_data` with no
arguments prints the help text and exits with code 2. Python 3.11 or newer is required; only
3.12 has been tested.

## Usage

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
points_per_frame: 1000
scenario_hash: c8f597af59a680681e1c36d760a325a699f20fed9e2a653de78a3b80d5bca5db
drive_dir: data\raw\c8f597af59a6
manifest: verified
```

The folder `data/raw/c8f597af59a6/` then holds `frames.parquet` (3,196,692 bytes),
`ground_truth.parquet` (18,133 bytes) and `manifest.json` (1,385 bytes). `--out` is optional; without
it the drive goes to `raw_dir` from `config/default.yaml` (or `VL_RAW_DIR`). Generating the same
scenario again replaces the folder with identical files.

Check a drive against its manifest:

```powershell
python -m vl_data verify data/raw/c8f597af59a6
```

This prints `manifest: verified` and exits with code 0. After one byte of `ground_truth.parquet`
was changed in a copy, it printed `changed: ground_truth.parquet (sha256 mismatch)` on stderr and
exited with code 1. A missing or invalid scenario file gives a one-line `error: ...` message and
exit code 1.

Planned, not yet run (arrives with the inverse functions):

```powershell
python -m vl_data roundtrip --scenario src/vl_data/generator/scenarios/demo.yaml
```

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

Query and dashboard examples will be added when Modules 3 and 4 exist.

## Known issues

- No rotation. The vehicle never turns or yaws. A real vehicle rotates.
- The point cloud is a 1,000-point sample, not the full 1,000,000 points.
- Motion is one-dimensional (along x).
- Values are rounded to whole centimetres, which adds small errors. Per-frame acceleration is too
  noisy to use directly, so it is fitted over windows.
- File hashes only match when the same pyarrow version wrote the files.
- Plain Parquet has no atomic multi-file commit, no schema-evolution tracking and no time travel.
  Writing a drive is not safe for concurrent writers.
- Test tolerances come from one demo drive.
- Single machine, CPU only.

## Coming soon

Rotation and yaw with a non-symmetric body, planar motion, optional noise, event detection and
clip candidates, Apache Iceberg as the table format, a REST API, streaming ingestion, a Spark
backend for many drives, an object-store profile, scheduled runs, and delta-encoded storage.
