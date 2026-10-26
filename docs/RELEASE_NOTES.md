# Release notes: version 0.1.0 (draft)

Status as of 2026-10-25: the project skeleton and the generator (Module 1) are built and tested; the
other modules are not. This draft is filled in as each module lands, and
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
| Generator | Writes a hashed drive: 16 fps point cloud, ground truth, manifest. Checks itself by recovering the scenario from the data. | Built and tested (Module 1) |
| Loader / ETL | Verifies hashes, validates frames with error codes VL-001 to VL-006, keeps 9 keypoints per frame. | Planned |
| Use-case algorithm | Computes velocity and acceleration and compares them with ground truth. | Planned |
| BI | One Streamlit app: generate, clean, analyze. | Planned |

## Install and first check (run on 2026-10-25, Windows PowerShell, Python 3.12.6, fresh clone and new virtual environment)

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

## Usage

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
`ground_truth.parquet` (30,929 bytes) and `manifest.json` (1,454 bytes). The sizes are from the same fresh-clone run (measured). The 1,000-frame drive holds 12 GB
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

This prints `manifest: verified` and exits with code 0. After one byte of `ground_truth.parquet`
was changed in a copy, it printed `changed: ground_truth.parquet (sha256 mismatch)` on stderr and
exited with code 1 (measured in run 1, which shows the message in its stderr section; the step that changed the copy is recorded only in the run 2 log). A missing scenario file (`generate` and `roundtrip`) or an
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
figures (2026-10-25). The demo-drive errors against ground truth (maximum centroid x error 0.498 cm,
maximum velocity error 15.0 cm/s) are a separate, older measurement: 2026-10-24, with
`.claude/run-proofs/TASK-5-measure.py` (log `TASK-5-measure.log`) on the old 10x10x10 body; they
were not re-measured at full density.

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

Query and dashboard examples will be added when Modules 3 and 4 exist.

## Known issues

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
- Single machine, CPU only.

## Coming soon

Rotation and yaw with a non-symmetric body, planar motion, optional noise, event detection and
clip candidates, Apache Iceberg as the table format, a REST API, streaming ingestion, a Spark
backend for many drives, an object-store profile, scheduled runs, and delta-encoded storage.
