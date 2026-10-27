# Architecture and design

A small end-to-end data product for a vehicle-location (VL) signal: a mock data source, an ETL,
a queryable warehouse (the "lake") and a BI app. The implemented use case is ego motion: velocity
and acceleration of the vehicle, computed from its point cloud alone. All data is synthetic.

## Flow

```
 [1 Generator] --> data/raw/<hash12>/ --> [2 Loader / ETL] --> Lake --> [3 Motion] --> Lake
   scenario          frames.parquet        verify, validate,    keypoints   velocity,    motion
   (YAML)            manifest.json         9 keypoints/frame    quarantine  acceleration
                                                                lineage
                                   VLClient (Python SDK) + SQL (DuckDB) read the lake
                                                      |
                                              [4 BI: Streamlit]
                                       Generate | Clean | Analyze | SQL
```

| Module | Job |
|---|---|
| 1 Generator (data source) | Turns a start position and `move` / `stop` segments into a drive: 16 fps, each frame a 1 m cube of 100 x 100 x 100 = 1,000,000 points (whole cm). Writes `frames.parquet` (one row per frame) and a `manifest.json` with SHA-256 hashes. The drive folder is named by the scenario hash. |
| 2 Loader / ETL | Checks the hashes, validates each frame against six rules (VL-001 to VL-006, see [ERROR_CODES.md](ERROR_CODES.md)), reduces the cloud to 9 keypoints (8 corners and the centroid), and writes three tables. A bad frame goes to quarantine and the rest of the drive continues. |
| 3 Motion (use case) | Reads the centroid keypoints and fits a least-squares quadratic (order 2) over a centred sliding window of 16 frames on the real timestamps. Its first derivative is velocity (cm/s) and its second is acceleration (cm/s^2). Writes the `motion` table. |
| 4 BI | One Streamlit page. Generate writes a drive, Clean shows kept, rejected and missing frames with their codes, Analyze plots position, velocity and acceleration, SQL runs one SELECT. Buttons run ingest and motion from the page. |

## Data model (the warehouse)

Plain Parquet files, one folder per table and drive: `<lake>/<table>/drive=<id>/part-0.parquet`.
The shape is not tied to the use case: a drive and frame key, a timestamp and numeric measures.

| Table | One row per | Content |
|---|---|---|
| keypoints | keypoint per frame | frame_id, timestamp_ns, keypoint, x_cm, y_cm, z_cm |
| quarantine | rejected frame or drive problem | frame_id, row_index, code (VL-001..006 or INTEGRITY), message |
| lineage | drive | scenario hash, schema_version, status, frame counts, keypoint_rows, hash of the frames file |
| motion | frame | frame_id, timestamp_ns, x_cm, velocity_cm_s, acceleration_cm_s2, n_points |

Re-running a load never duplicates data: each table partition is written to a temporary folder and
then swapped in, so a crash keeps the old data. The swap is per table, not one atomic commit.

## Technology choices and alternatives

| Part | Chosen | Why | Alternatives |
|---|---|---|---|
| Raw format | Parquet (PyArrow), zstd, one row per frame with a binary points column | Columnar and compact; a full frame is 12 MB raw. zstd gives 287 MB per 1,000-frame drive (measured) against 4.0 GB with snappy | One row per point, CSV, HDF5 |
| Query | DuckDB over Parquet | SQL with no server | Trino, Athena, Postgres |
| Lake format | Plain Parquet in Hive-style folders behind one `Lake` class | Nothing to run | Apache Iceberg or Delta Lake (atomic commits, time travel) |
| ETL engine | pandas and numpy, chunked reads | One machine; clear code | Polars, Spark |
| Validation | A rule registry with error codes; the same registry builds the error-code document | Explicit, testable | Great Expectations, pandera |
| Config and models | YAML plus pydantic | Readable; fails early with clear errors | Environment variables, dataclasses |
| Estimator | Least-squares sliding window on timestamps | Uses real timestamps, so a dropped frame is a gap; numpy only | Per-frame differences (too noisy), Savitzky-Golay, Kalman smoother |
| API | Python SDK (`VLClient`) plus SQL | Fits a local data-science user | REST service (listed as coming soon) |
| BI | Streamlit and Plotly | Python only, same SDK as the command line, tabs testable without a browser | Dash, Superset, Grafana, Jupyter |
| CLI and tests | Typer; pytest and hypothesis | Typed commands; property tests suit a two-way check | argparse; unittest |
| Not used | Kafka, Spark, Airflow, Docker | No purpose at this scale | See below |

## Storage and scaling

At 1,000,000 points per frame, a frame is 12 MB, 192 MB per second at 16 fps and about 691 GB per
hour (computed). The demo drive (1,000 frames) is 12 GB raw (computed) and 287 MB on disk (measured).
This design fits one machine and one drive at a time. At scale (not built or measured here):

- **Object storage (S3 or similar) with keyframe-plus-delta storage:** 691 GB per hour per vehicle
  does not fit on local disk, and most points repeat from frame to frame.
- **Apache Iceberg or Delta Lake:** plain Parquet has no atomic commit across tables and allows one
  writer. These add atomic commits, many writers, schema evolution and time travel.
- **Spark or Flink, scheduled by Airflow:** one task per drive replaces the one-machine pandas load;
  the scheduler adds retries.
- **Kafka:** only if drives must be queryable while they are still being recorded.
- **Trino and a REST API, with a login:** many users need a shared query service, not an in-process
  DuckDB and an open Streamlit page.

## Limits

- The vehicle moves in a straight line along x and never rotates; a cube's orientation cannot be
  read from its points.
- Plain Parquet has no atomic multi-file commit and no time travel; the lake has one writer.
- A scenario has at most 2,000 frames, and a drive is generated in batches to bound memory.
- The SQL tab runs any single SELECT and is for a local single user (127.0.0.1 only).
