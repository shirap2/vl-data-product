# Release notes: version 0.1.0

Status as of 2026-10-26: Modules 1 (generator), 2 (loader and ETL), 3 (use case) and 4 (the BI
app) are built. Modules 1 to 3 are checked by tests and by a full-density run; Module 4 is checked
by Streamlit's test runner and one live-server start, not by a hand-opened browser. Every command
and number below comes from a real run unless it says it comes from reading the source or the
documentation.

## Product description

A small data product for a vehicle-location signal. It generates a mock drive from a start
position and a motion profile, stores the point cloud, extracts the vehicle's velocity and
acceleration from the points alone, and stores the result as a queryable motion table. The vehicle
is a 1 m cube that moves in a straight line, forwards, backwards and stopped, at different
speeds. All data is synthetic.

Module 3 adds the use case: `motion` turns the stored centroid position of a drive into velocity (cm/s) and acceleration (cm/s^2) with a sliding least-squares fit over 16 frames. The Python SDK (`VLClient`) and SQL read the lake.

Module 4 adds the BI app: one Streamlit page with four tabs. Generate writes a drive; Clean shows how many frames of the selected drive were kept, rejected or missing and why; Analyze shows the stored centroid position, velocity and acceleration; SQL runs one SELECT over the lake. The app reads the lake. It writes in two places: the Generate tab writes a raw drive, the ingest panel writes the lake through `ingest_drive`, on a click, and the Compute motion button on the Analyze tab writes the motion table through `compute_motion`, on a click.

## What is in this version

| Part | What it does | Status |
|---|---|---|
| Generator | Writes a hashed drive: 16 fps point cloud and manifest. Checks itself by recovering the scenario from the data. | Built |
| Loader / ETL | Verifies hashes, validates frames with error codes VL-001 to VL-006, keeps 9 keypoints per frame, and writes keypoints, quarantine and lineage tables to a queryable lake. | Built |
| Use-case algorithm | Computes velocity and acceleration into a motion table (`motion`, `VLClient`). | Built |
| BI | One Streamlit app with four tabs: Generate, Clean, Analyze, SQL. | Built |

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
and has a no-rights stand-in that ran; on the Generator branch at that time the whole suite was 332 tests, 331 passed and 1 skipped, 100.00% coverage, measured in `TASK-7-1-1-iter1-suite.log`, and its fresh-clone run on 2026-10-25 gave 331 passed, 1 skipped in 32.41 s, 100% coverage of 1,037 statements, with ruff and mypy clean (measured, `FINAL-DOCS-iter1-fresh-clone.log`). On the Loader branch after `main` was merged in (2026-10-27, measured, output not saved to `.claude/run-proofs/`), the whole suite gave 939 passed and 5 skipped in 128 s, with 282 of the tests in `tests/etl` and 99% line coverage of the package. The logs are local, git-ignored, A28), which cannot edit the table, so editing a row
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

### Motion (Module 3)

Compute the centroid velocity and acceleration of an ingested drive:

```powershell
python -m vl_data motion <hash12> --lake-dir warehouse
```

`motion` prints `drive`, `frames_read`, `frames_estimated` and `frames_null` (counts of frames; a
frame is null when its velocity or acceleration has no estimate). It uses the exit codes of
`ingest` (0 done, 1 refused or failed, 2 usage or configuration error, 3 a file could not be read
or written) and the same lake folder rule (`--lake-dir`, else `VL_LAKE_DIR`, `config/etl.yaml`,
`warehouse`). Window and order are in `config/motion.yaml`. The failures are listed in
`docs/extras/USE_CASE_ERRORS.md`.

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

### BI app (Module 4)

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
With an empty lake the Generate tab has no badge and the other three tabs show a hint; the ingest panel shows under Generate and in Clean.

- Clean (reads the lake): the metrics "expected (frames)", "in (frames)", "kept (frames)", "rejected
  (frames)", "missing (frames)" and "keypoint rows (rows)"; a bar chart of the frame counts; a bar
  chart of quarantine rows per error code; and the quarantine table, which shows at most 200 rows
  and states the full count ("showing N of M quarantine rows"). If the drive is not ingested the
  tab prints the command to run, for example
  `python -m vl_data ingest '<raw>\<drive>' --lake-dir '<lake>'`, and does not run it.
- Ingest (Generate tab and Clean tab): choose a raw drive folder and click "Ingest this drive".
  One click ingests one drive; the sidebar then lists it. The panel calls the same
  `ingest_drive` as the command, so the `ingest_drive` figures (in-process, not the command-line run) are its ingest cost
  (full-density drive: 44.7 s first ingest, 43.6 s re-ingest, measured 2026-10-25 on one machine, plus one
  unexplained 3,730 s outlier); the panel's own overhead on a full-density drive is not measured.
  The page is blocked while it runs, so a large drive can take minutes. The check script
  `.claude/run-proofs/module4_ingest_check.py` prints the seconds of a small drive. A
  drive already in the lake can be ingested again ("re-ingest"). Command line alternative:
  `python -m vl_data ingest '<raw>\<drive>' --lake-dir '<lake>'` (do not run both at once; the
  lake has a single writer, and the in-process lock of the panel does not cover the command line).
- Analyze: draws the stored motion: centroid x (cm), velocity (cm/s) and acceleration (cm/s^2)
  against time (s), from the lake; it needs no raw folder. If the drive was ingested with status
  ok but has no stored motion, the tab says "Motion is not computed yet" and shows the button
  "Compute motion"; command line alternative:
  `python -m vl_data motion <drive> --lake-dir '<lake>'`. With motion present the tab shows the
  charts and a button "Recompute motion" (use it after a re-ingest). The result (frames read,
  estimated, null) shows once after a click. If the drive is not ingested, or was rejected at
  ingest, there is no button and the tab prints the `ingest` command instead. If the drive was
  rejected after motion was stored, the charts show with a warning that they are from an
  earlier run and no button. If the drive has more than one lineage row, it shows a problem
  text and no command (from reading the code, not run). Do not run the command line `motion`
  and a panel click at the same time; the panels share one in-process lock that the command line
  does not take.
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
missed), and the full suite gave 958 passed, 4 skipped (all measured, 2026-10-26). With the ingest panel, on 2026-10-27 `tests/bi` gave 141 passed, 1 skipped with 100% coverage of `vl_data.bi` (439 statements; measured; log `.claude/run-proofs/M4-INGEST-final-tests-bi.log`, local and git-ignored). The server check
proves the app starts and is served; it cannot click a widget, which is what the test runner
covers. The page was not opened in a browser by hand, and the full-density demo drive was not loaded in the app.

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
- Measured on 2026-10-25 (one machine, demo drive `bf394b9bfb42`, 1,000 frames x 1,000,000 points, row groups of 50 frames): the latest run took 44.7 s for the first ingest into a fresh lake and 43.6 s for a re-ingest, with a peak working set of 1,898 MiB (`.claude/run-proofs/TASK-4-measure-iter2b.log`). An earlier run measured 45.0 s, 42.9 s and 1,718 MiB (`TASK-4-measure.log`). One first run (`TASK-4-measure-iter2.log`) took 3,730 s; the cause is unknown and it was not reproduced, so it is an open outlier (the next full-density run should log free memory and disk).
- A drive folder is treated as hostile: a manifest above 2,000,000 points per frame (`max_points_per_frame`), a Parquet footer claiming more than 2 x expected frames + 1,000 rows, or a malformed `scenario_hash` is rejected with `INTEGRITY` before anything large is allocated, and at most 20 problems (plus one "and K more" row) are stored per drive. The row-group size check trusts the footer, so a compression bomb is a known gap.
- `frames_rejected` counts quarantine rows that name a file row, so a frame that breaks two rules counts twice; `frames_in` is not always `frames_kept + frames_rejected`. The ingest assumes the drive folder does not change while it runs.
- A drive whose frames file cannot be read, whose manifest cannot be read, or whose folder name is not its scenario hash is rejected with the code `INTEGRITY`, not with one of the six VL codes.
- The loader's speed limit (9,000 cm/s) is a Tesla's top speed of 322 km/h rounded up and is set in
  a YAML file. A faster vehicle would be flagged as impossible motion.
- Per-frame acceleration is not checked by any loader rule.
- The point-cloud check (VL-004) allows for the 1 cm rounding wobble of whole-centimetre points. A single point moved by exactly 1 cm is not detected; a move of 2 cm or more is.
- The loader decides which frame is a teleport by comparing positions. If the first three clean frames of a drive are themselves inconsistent, the loader trusts the most tightly agreeing pair; a drive that starts with several bad frames may be judged wrongly.
- Motion (Module 3): the motion table is not refreshed by a new ingest. After re-ingesting a drive, run `motion` again; until then the motion table holds the values of the earlier ingest.
- BI app (Module 4): a re-ingest leaves the old motion table in place until "Recompute motion" is clicked in the Analyze tab (or `motion` is run). The lock that Ingest and Compute motion share is in-process only: it does not cover the command-line `ingest` or `motion`, or a second server process. If the result line of a failed "Recompute motion" click is not drawn when the drive is not ingested ok (for example the lineage changed after the click), the error is not shown; reload the page and check the Clean tab (known issue found in review, not fixed).
- `VLClient.sql` runs any single SELECT, including `read_parquet` on any file the user can read. It is a local single-user tool; do not expose it to untrusted input (assumption M3-A10).
- The 16-frame least-squares fit smooths speed changes and the ends of a clip: frames within 8 frames of a change of acceleration, and the first and last 8 frames, carry larger errors (all-frame acceleration max 55.678 cm/s^2 against 4.378 cm/s^2 on interior frames, a development measurement against the in-memory truth of the demo drive, from `tools/motion_sweep.py`; assumption M3-A29).
- `tools/motion_sweep.py` is a developer measuring tool whose logic is not type-checked by mypy. It compares the estimator with the in-memory truth of generated drives; the product does not.
- Single machine, CPU only.
- Module 4, SQL tab: it runs any single SELECT through `VLClient.sql`, which can also read any file path on the machine through `read_parquet` (a DuckDB function that reads a Parquet file from any path); this is a local single-user tool, and production would use a locked connection. The "local single-user" premise depends on the app listening on 127.0.0.1 only (this computer): started on `0.0.0.0` (all network interfaces) or behind a port forward, anyone who can reach the port could use the SQL tab, which has no login. The wider reach below comes from reading the DuckDB defaults and documentation and was NOT run: other file types (`read_csv`, `read_json`, `read_text`, glob patterns), possible network reads through extension autoload (DuckDB loading an add-on such as `httpfs` by itself when a query needs it), Windows UNC paths (a UNC path is a Windows network-share path; opening one can send a login hash to that share), environment variables and secrets through `getenv` (a DuckDB function that returns an environment variable) and `duckdb_secrets()`, and no memory, thread or time limit. `COPY`, `INSERT`, `CREATE`, `ATTACH`, `INSTALL`, `SET` and `PRAGMA` are refused as non-SELECT (by the statement-type check in the SDK, read from the code; the tests cover DROP, COPY and a two-statement query, and check that the lake files are unchanged). No writes were found by reading, not by a run. The 10,000-row cap limits the display only; the whole result is loaded first. You accepted the wider reach on 2026-10-26 on condition that it is documented and a follow-up is written: `module4-TASK-4-1.md`.
- Module 4: the SQL box text is not refreshed when the selected drive changes (pick the example again), and the Run result is shown only in the run after the click, so any other widget action clears it.
- Module 4: the Generate tab badge shows the hash of the drive selected in the sidebar, not the scenario in the form.
- Module 4: run the app from the repository root, because the default lake and raw folders (`warehouse`, `data/raw`) are relative to the current folder.
- Module 4: the live-server check proves startup and serving only; widget behaviour (the Run button, the dropdown) is proven by Streamlit's test runner, and the page was not opened in a browser by hand.

## Coming soon

Rotation and yaw with a non-symmetric body, planar motion, optional noise, event detection and
clip candidates, Apache Iceberg as the table format, a REST API, streaming ingestion (for example
Kafka), parallel loading of many drives, a data-quality report function, a Spark backend for many
drives, an object-store profile, scheduled runs, and delta-encoded storage.

Module 3 follow-ups: event detection on the motion table, a REST API over the SDK, and a Kalman or RTS smoother as an alternative to the window fit.

Module 4 follow-ups: a locked SQL connection (external access off, extension autoload off, a memory limit), and a REST service if one is needed. The written follow-up file (in `.claude/plans/followups/`) is `module4-TASK-4-1.md` (SQL reach).
