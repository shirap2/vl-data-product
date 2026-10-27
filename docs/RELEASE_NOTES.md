# Release notes: version 0.1.0

Detail, measurements and the full list of known issues:
[extras/RELEASE_NOTES_DETAILED.md](extras/RELEASE_NOTES_DETAILED.md).

## Product description

A data product for a vehicle-location signal. It generates a mock drive (a 1 m cube moving in a
straight line, forwards, backwards and stopped, 16 frames per second, 1,000,000 points per frame),
validates and loads it into a queryable lake, extracts the vehicle's velocity and acceleration from
the points alone, and shows the result in a Streamlit app. Everything is synthetic.

| Part | What it does |
|---|---|
| Generator | Writes a hashed drive from a YAML scenario |
| Loader / ETL | Verifies hashes, applies error codes VL-001 to VL-006, writes keypoints, quarantine and lineage tables |
| Motion | Computes velocity and acceleration into a motion table |
| SDK and BI | `VLClient` (Python) and SQL; a Streamlit app with Generate, Clean, Analyze and SQL tabs |

## Usage

Install (Windows PowerShell, Python 3.11 or newer; tested on 3.12):

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Run the whole pipeline on the demo scenario (1,000 frames, one reversal, two stops):

```powershell
python -m vl_data generate --scenario src/vl_data/generator/scenarios/demo.yaml --out data/raw
python -m vl_data ingest data/raw/bf394b9bfb42 --lake-dir warehouse
python -m vl_data motion bf394b9bfb42 --lake-dir warehouse
python -m streamlit run src/vl_data/bi/app.py --server.address 127.0.0.1
```

Measured on one Windows machine on 2026-10-25 (single runs): generate 81 to 87 s, ingest 44 to
56 s, motion 1 s. A scenario file lists a start and `move` / `stop` segments with durations in
seconds and speeds in cm/s (see `src/vl_data/generator/scenarios/demo.yaml`).

Query the lake from Python:

```python
from vl_data.core.lake import Lake
from vl_data.sdk.client import VLClient

client = VLClient(Lake("warehouse"))
client.sql("SELECT frame_id, round(velocity_cm_s, 1) AS v FROM motion "
           "WHERE frame_id IN (0, 250, 500) ORDER BY frame_id").to_pylist()
```

Other client methods: `drives()`, `keypoints(drive)`, `motion(drive)`, `lineage(drive)`,
`quarantine(drive)`. In the app, open a drive in the sidebar; Clean shows why frames were
rejected, Analyze plots the motion, SQL runs one SELECT. Error codes: [ERROR_CODES.md](ERROR_CODES.md).
Tests: `python -m pytest -q` (966 passed, 5 skipped, measured on 2026-10-27).

## Known issues

- No rotation: the vehicle never turns, and motion is along x only.
- A drive is large: 12 GB of points per 1,000 frames before compression, 287 MB on disk (zstd).
  A scenario is capped at 2,000 frames.
- Values are whole centimetres. Per-frame acceleration is too noisy, so it is fitted over 16
  frames; speed changes and the first and last 8 frames of a clip carry larger errors.
- The lake is single-writer, and a load is not one atomic commit across its three tables. Loading
  the drive again repairs a crash.
- Re-ingesting a drive leaves the old motion table until `motion` (or "Recompute motion") is run.
- Timings vary a lot between runs on the same machine (generate 81 to 238 s); the cause is unknown.
- The SQL tab and `VLClient.sql` run any single SELECT, including `read_parquet` on any readable
  file. Use them locally only; the app listens on 127.0.0.1 and has no login.
- Run the app from the repository root (the default lake and raw folders are relative).
- The app was tested with Streamlit's test runner and one server start, not by hand in a browser.

## Coming soon

Rotation and yaw with a non-symmetric body, planar motion, event detection (hard braking,
reversing) and clip candidates, Apache Iceberg as the table format, a REST API, streaming ingestion
(for example Kafka), parallel loading of many drives, a Spark backend, a locked SQL connection,
and delta-encoded storage.
