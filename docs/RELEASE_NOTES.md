# Release notes: version 0.1.0

Status as of 2026-10-26: Modules 1 (generator), 2 (loader and ETL), 3 (use case) and 4 (the BI
app) are built. Modules 1 to 3 are checked by tests and by a full-density run; Module 4 is checked
by Streamlit's test runner and one live-server start, not by a hand-opened browser. Every command
and number below comes from a real run unless it says it comes from reading the source or the
documentation.

## Product description

A small data product for a vehicle-location signal. It generates a mock drive from a start
position and a motion profile, stores the point cloud, extracts the vehicle's velocity and
acceleration from the points alone, and shows the result next to the known truth. The vehicle
is a 1 m cube that moves in a straight line, forwards, backwards and stopped, at different
speeds. All data is synthetic.

Module 3 adds the use case: `motion` turns the stored centroid position of a drive into velocity (cm/s) and acceleration (cm/s^2) with a sliding least-squares fit over 16 frames, and `evaluate` checks the result against the ground truth and prints PASS or FAIL against tolerances in `config/motion.yaml`. The Python SDK (`VLClient`) and SQL read the lake.

Module 4 adds the BI app: one Streamlit page with four tabs. Generate writes a drive; Clean shows how many frames of the selected drive were kept, rejected or missing and why; Analyze shows the stored velocity and acceleration and, on a click, checks them against the ground truth; SQL runs one SELECT over the lake. Apart from the Generate tab, which writes a drive, it only reads the lake.

## What is in this version

| Part | What it does | Status |
|---|---|---|
| Generator | Writes a hashed drive: 16 fps point cloud, ground truth, manifest. Checks itself by recovering the scenario from the data. | Built |
| Loader / ETL | Verifies hashes, validates frames with error codes VL-001 to VL-006, keeps 9 keypoints per frame, and writes keypoints, quarantine and lineage tables to a queryable lake. | Built |
| Use-case algorithm | Computes velocity and acceleration and compares them with ground truth (`motion`, `evaluate`, `VLClient`). | Built |
| BI | One Streamlit app with four tabs: Generate, Clean, Analyze, SQL. | Built |

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
wrong form or table shows a red error and no plot. A bad cell (blank, text, true/false, not a number, infinite, or a duration
that is not a whole number of frames) names the segment and the field, for example "segment 0 duration_s must be a finite
number"; a bad start field names the field ("start x_cm must be a finite number"); a cleared seed says "seed is blank; enter a
whole number" and a seed that is not a number says "seed must be a whole number"; an empty table or a table over 2,000 frames
gets its own message. If the target folder is a symbolic link or a plain file, the tab does not call it
"already generated"; it shows the writer's error and changes nothing (A76). Two Generate clicks
at the same time for a drive that does not exist yet can still both write (the race is not closed, A79). The tab is tested with Streamlit's test runner
(`tests/generator/test_ui.py`, 49 tests, 48 passed and 1 skipped, in 10.35 s, measured in
`.claude/run-proofs/TASK-7-1-1-iter1-ui-tests.log`; the one skip is the real-symlink test, which needs the Windows symbolic-link right
and has a no-rights stand-in that ran; the whole suite was 332 tests, 331 passed and 1 skipped, 100.00% coverage, measured in `TASK-7-1-1-iter1-suite.log`; both logs are local, git-ignored, A28), which cannot edit the table, so editing a row
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

### Motion and evaluation (Module 3)

Compute the centroid velocity and acceleration of an ingested drive, then compare them with the
ground truth:

```powershell
python -m vl_data motion <hash12> --lake-dir warehouse
python -m vl_data evaluate data/raw/<hash12> --lake-dir warehouse
```

`motion` prints `drive`, `frames_read`, `frames_estimated` and `frames_null` (counts of frames; a
frame is null when its velocity or acceleration has no estimate). `evaluate` prints the scenario
hash, the frames compared, interior and null, and the interior max and RMS error of position
(cm), velocity (cm/s) and acceleration (cm/s^2) with their tolerances and the all-frame
velocity and acceleration errors, then `result: PASS` or `result: FAIL`. Both use the exit codes
of `ingest` (0 done or passed, 1 refused, failed or a tolerance exceeded, 2 usage or
configuration error, 3 a file could not be read or written; inside `evaluate` a read failure during the comparison is reported as a failure, code 1) and the same lake folder rule
(`--lake-dir`, else `VL_LAKE_DIR`, `config/etl.yaml`, `warehouse`). Window, order and
tolerances are in `config/motion.yaml`. The failures are listed in `docs/USE_CASE_ERRORS.md`.

Real run (2026-10-25, full density: demo scenario, 1,000 frames x 1,000,000 points; times are
wall-clock, one machine). All exit codes were 0.

| Command | Time | Main output |
|---|---|---|
| `generate --scenario src/vl_data/generator/scenarios/demo.yaml --out <work>/raw` | 79 s | `frames: 1000`, `points_per_frame: 1000000` |
| `ingest <work>/raw/bf394b9bfb42 --lake-dir <work>/lake` | 56 s | `status: ok`, `frames_kept: 1000`, `keypoint_rows: 9000` |
| `motion bf394b9bfb42 --lake-dir <work>/lake` | 1 s | `frames_read: 1000 frames`, `frames_estimated: 1000 frames`, `frames_null: 0 frames` |
| `motion` again | 2 s | same; the motion partition file hash did not change |
| `evaluate <work>/raw/bf394b9bfb42 --lake-dir <work>/lake` | 2 s | `frames_interior: 880 frames`, position 0.498 cm, velocity 3.520 cm/s, acceleration 4.378 cm/s^2 (interior max), `result: PASS` |
| `pytest tests/ -q` | 66 s | 851 passed, 4 skipped |

The full `evaluate` output is in `docs/ARCHITECTURE.md` (Module 3, Command line).

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
With an empty lake the Generate tab has no badge and the other three tabs show a hint.

- Clean (read-only): the metrics "expected (frames)", "in (frames)", "kept (frames)", "rejected
  (frames)", "missing (frames)" and "keypoint rows (rows)"; a bar chart of the frame counts; a bar
  chart of quarantine rows per error code; and the quarantine table, which shows at most 200 rows
  and states the full count ("showing N of M quarantine rows"). If the drive is not ingested the
  tab prints the command to run, for example
  `python -m vl_data ingest '<raw>\<drive>' --lake-dir '<lake>'`, and does not run it.
- Analyze: "Stored motion" draws centroid x (cm), velocity (cm/s) and acceleration (cm/s^2)
  against time (s) from the lake; it needs no click and no raw folder. The button "Evaluate
  against ground truth" runs the evaluation only when clicked, using the raw folder, and keeps the
  result in the session cache (a copy kept in memory for as long as the browser tab stays open),
  so a click on another widget does not repeat it. The result is PASS or
  FAIL from the evaluation, a metric table, estimate-against-truth charts for velocity and
  acceleration, and error charts with dashed tolerance lines. If the drive was ingested with
  status ok but has no stored motion, the tab prints
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
printed `SERVER_CHECK: PASS`, 6.5 s in all, measured on 2026-10-26; log
`.claude/run-proofs/TASK-6-iter2-server.log`, local, git-ignored). That check proves the app starts and is
served; it cannot click a widget, which is what the test runner covers. The page was not opened in
a browser by hand, and the full-density demo drive was not loaded in the app.

## Known issues

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
- Motion (Module 3): the motion table is not refreshed by a new ingest. After re-ingesting a drive, run `motion` again; until then `evaluate` stops with an error if the frame count changed, and compares stale values if it did not.
- `VLClient.sql` runs any single SELECT, including `read_parquet` on any file the user can read. It is a local single-user tool; do not expose it to untrusted input (assumption M3-A10).
- The 16-frame least-squares fit smooths speed changes and the ends of a clip: frames within 8 frames of a change of acceleration, and the first and last 8 frames, carry larger errors (all-frame acceleration max 55.678 cm/s^2 against 4.378 cm/s^2 on interior frames, measured on the demo drive; assumption M3-A29).
- `tools/motion_sweep.py` is a measuring tool whose logic is not type-checked by mypy.
- `evaluate` hashes every file of the drive before it compares anything (`verify_drive`). The points are 12 GB per 1,000 full-density frames uncompressed; on disk they are compressed, and the measured `evaluate` took 2 s on the full-density demo drive. Noisier data would take longer (assumption M3-A33).
- Single machine, CPU only.
- Module 4, SQL tab: it runs any single SELECT through `VLClient.sql`, which can also read any file path on the machine through `read_parquet` (a DuckDB function that reads a Parquet file from any path); this is a local single-user tool, and production would use a locked connection. The "local single-user" premise depends on the app listening on 127.0.0.1 only (this computer): started on `0.0.0.0` (all network interfaces) or behind a port forward, anyone who can reach the port could use the SQL tab, which has no login. The wider reach below comes from reading the DuckDB defaults and documentation and was NOT run: other file types (`read_csv`, `read_json`, `read_text`, glob patterns), possible network reads through extension autoload (DuckDB loading an add-on such as `httpfs` by itself when a query needs it), Windows UNC paths (a UNC path is a Windows network-share path; opening one can send a login hash to that share), environment variables and secrets through `getenv` (a DuckDB function that returns an environment variable) and `duckdb_secrets()`, and no memory, thread or time limit. `COPY`, `INSERT`, `CREATE`, `ATTACH`, `INSTALL`, `SET` and `PRAGMA` are refused as non-SELECT (by the statement-type check in the SDK, read from the code; the tests cover DROP, COPY and a two-statement query, and check that the lake files are unchanged). No writes were found by reading, not by a run. The 10,000-row cap limits the display only; the whole result is loaded first. You accepted the wider reach on 2026-10-26 on condition that it is documented and a follow-up is written: `module4-TASK-4-1.md`.
- Module 4: a drive written by `write_drive` after a `DropFrame` fault cannot be evaluated: `evaluate_drive` raises `EvaluationError` (for a 288-frame drive, for example: "ground truth has 288 rows; the manifest implies 287"; N and N-1 in general). The Analyze tab shows that problem and no report; the Clean tab still shows VL-001. This is an integration gap between Module 1 and Module 3, found by reading the source and the tests; the AppTest suite (`tests/bi/test_tabs.py`) shows the behaviour. The follow-up is written: `module3-faulted-drive-cannot-be-evaluated.md`.
- Module 4: the stored Evaluate result is keyed by drive id only and can be stale (after a new ingest, a new motion run or a changed raw drive) until the next click on Evaluate. The follow-up is written: `module4-TASK-3-1.md`.
- Module 4: the check that the raw folder is not a link looks one level deep and is not atomic; junction detection needs Python 3.12 or later. The follow-up is written: `module4-TASK-1-1.md` (link check).
- Module 4: the SQL box text is not refreshed when the selected drive changes (pick the example again), and the Run result is shown only in the run after the click, so any other widget action clears it.
- Module 4: the Generate tab badge shows the hash of the drive selected in the sidebar, not the scenario in the form.
- Module 4: run the app from the repository root, because the default lake and raw folders (`warehouse`, `data/raw`) are relative to the current folder.
- Module 4: the live-server check proves startup and serving only; widget behaviour (the Evaluate and Run buttons, the dropdown) is proven by Streamlit's test runner, and the page was not opened in a browser by hand.

## Coming soon

Rotation and yaw with a non-symmetric body, planar motion, optional noise, event detection and
clip candidates, Apache Iceberg as the table format, a REST API, streaming ingestion (for example
Kafka), parallel loading of many drives, a data-quality report function, a Spark backend for many
drives, an object-store profile, scheduled runs, and delta-encoded storage.

Module 3 follow-ups: a stored evaluation table (today the report is printed and not stored), event detection on the motion table, a REST API over the SDK, and a Kalman or RTS smoother as an alternative to the window fit.

Module 4 follow-ups: a locked SQL connection (external access off, extension autoload off, a memory limit), a fingerprint of the lake and raw drive so a stored evaluation is not shown stale, a deeper link check on the raw folder, a fix for evaluating a drive with a dropped frame, and a REST service if one is needed. The written follow-up files (in `.claude/plans/followups/`) are `module4-TASK-4-1.md` (SQL reach), `module4-TASK-3-1.md` (stale result), `module4-TASK-1-1.md` (link check) and `module3-faulted-drive-cannot-be-evaluated.md` (dropped frame).
