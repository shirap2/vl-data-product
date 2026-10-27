# Release notes: version 0.1.0 (draft)

Status as of 2026-10-25: the project skeleton, the generator (Module 1, with its Generate tab) and
the loader (Module 2) are built and tested; the use-case algorithm (Module 3) and the Clean and
Analyze tabs (Module 4) are not. This draft is filled in as each module lands, and
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
| Loader / ETL | Verifies hashes, validates frames with error codes VL-001 to VL-006, keeps 9 keypoints per frame, and writes keypoints, quarantine and lineage tables to a queryable lake. | Built and tested (Module 2) |
| Use-case algorithm | Computes velocity and acceleration and compares them with ground truth. | Planned |
| BI | One Streamlit app: generate, clean, analyze. | Generate tab built (Module 1); Clean and Analyze planned |

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

## Known issues

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

## Coming soon

Rotation and yaw with a non-symmetric body, planar motion, optional noise, event detection and
clip candidates, Apache Iceberg as the table format, a REST API, streaming ingestion (for example
Kafka), parallel loading of many drives, a data-quality report function, a Spark backend for many
drives, an object-store profile, scheduled runs, and delta-encoded storage.
