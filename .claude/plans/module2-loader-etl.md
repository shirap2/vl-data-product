# Plan: Module 2, loader / ETL (verify, validate, reduce to keypoints, write to a queryable lake)

## Goal

Module 2 verifies a drive's hashes, validates every frame (VL-001..006), reduces each full point cloud to 9 keypoints, and writes keypoints, quarantine and lineage tables to the queryable Parquet lake, with a safe re-ingest.

## User Request Summary

"open a new branch and worktree and start the plan loop for loader/ETL- follow the architecture context txt file"

"you are getting the full point cloud and then we should etl it to only the essential data needed for my use case which is infering velocity and acceleration. then it will be saved to the disk in a datalake that can be queried. make sure that this matches the instructions in the assignment pdf file"

## Context

- The assignment asks for an ETL "for converting the data per your use case" and a data warehouse holding "the converted data for user query (more generic data structure will be better)". It also says the data source is mocked and that not all data needs saving. [assignment.pdf, section 3 items 2a to 2c]
- The assignment asks for a list of the possible error codes. This plan owns that list (VL-001 to VL-006) and generates it from the rule registry. [assignment.pdf, section 3 item 3a; architecture_context.txt "ASSIGNMENT COVERAGE"]
- AMENDMENT 2026-10-24 (user decision in the generator session, then confirmed here): the generator will move to the real density, 100 x 100 x 100 = 1,000,000 points per frame (about 12 MB per frame, about 12 GB per 1,000-frame drive). The generator plan follows after its TASK-6; its manifest will describe the body (key names not decided yet). This plan therefore never hard-codes the 10 x 10 x 10 sample: points per frame, the lattice, the payload size and the corner indices come from the drive's own body settings, and chunks are sized by bytes. Tests use small bodies; the final proof uses full density.
- The signal is a point cloud at 16 frames per second. The generator writes a 1 m cube as a lattice of `points_per_axis` cubed points (today 10 x 10 x 10 = 1,000; planned 100 x 100 x 100), one row per frame, in `data/raw/<hash12>/`. [assignment.pdf 1.2; architecture_context.txt "MODULE 1"; module1-data-generator.md "Design" items 3 and 4]
- The use case is the ego vehicle's velocity and acceleration. Position along x is the base signal. The centroid of a frame gives the position. [architecture_context.txt "DECISIONS", "MODULE 3"]
- The input contract is the user-approved Module 1 plan: the `frames.parquet` schema, the manifest keys, `verify_drive`, `decode_points`, the fault classes and `inject_faults`. Nothing of it is built or merged yet. [module1-data-generator.md "Design", "Classes / Functions / Signatures"; message from the generator session, 2026-10-23]
- Measured by the Module 1 plan on a throwaway script: position error up to 0.498 cm, per-frame velocity error up to 15 cm/s, per-frame acceleration error up to 452 cm/s^2. Acceleration is therefore unusable per frame, so no ETL rule tests acceleration. [module1-data-generator.md "Context"]
- The repo has no code on this branch. The branch `worktree-module2-loader-etl` starts from the initial commit. Module 1 must be merged into it before any task here starts.

## Reliable Sources

### Authoritative (implementation truth)

| Rank | Name | Location | Covers |
|------|------|----------|--------|
| 1 | Assignment | docs/assignment.pdf, section 1.2 and section 3 | signal definition, ETL and warehouse deliverables, error-code list |
| 2 | Architecture context | docs/architecture_context.txt | Module 2 scope, lake tables, decisions |
| 2 | Module 1 plan (user-approved) | .claude/plans/module1-data-generator.md in the main checkout | input contract: schemas, manifest, faults, core/ paths |
| 1 | DuckDB docs | https://duckdb.org/docs/ | read_parquet, hive partitioning, views |
| 1 | PyArrow docs | https://arrow.apache.org/docs/python/ | ParquetFile, iter_batches, ParquetWriter, schemas |
| 1 | pandas docs | https://pandas.pydata.org/docs/ | frame-facts DataFrame |
| 1 | numpy docs | https://numpy.org/doc/ | frombuffer, array checks |
| 1 | pydantic docs | https://docs.pydantic.dev/ | config models |
| 1 | PyYAML docs | https://pyyaml.org/wiki/PyYAMLDocumentation | safe_load |
| 1 | Typer docs | https://typer.tiangolo.com/ | CLI commands |
| 1 | hypothesis docs | https://hypothesis.readthedocs.io/ | property tests |
| 1 | Python hashlib docs | https://docs.python.org/3.11/library/hashlib.html | SHA-256 of payloads |

### Context-Only (not implementation truth)

| Rank | Name | Location | Note |
|------|------|----------|------|
| 3 | Repo config | .claude/forgeloopp.md | commands, gates, conventions (its rules apply as repo config) |
| 3 | Generated docs | docs/ARCHITECTURE.md, ASSUMPTIONS.md, ERROR_CODES.md, RELEASE_NOTES.md (main checkout) | drafts this plan updates; ERROR_CODES.md text for VL-002 and VL-006 is superseded by the user decisions below |

## Requirements

- REQ-1: Before reading any frame, the ETL reads the manifest and runs `verify_drive`. A changed, missing or extra file, an unreadable manifest, an unsupported `schema_version`, or a folder name that is not the first 12 hex of the manifest hash rejects the whole drive. A rejected drive writes one quarantine row per problem (code `INTEGRITY`), a lineage row with status `rejected`, and removes any keypoints of that drive. The CLI exits non-zero. [user decisions: hash failure, whole-drive faults; Module 1 plan REQ-6]
- REQ-2: A frames table whose schema differs from the agreed frames schema is VL-005. It rejects the whole drive in the same way, with code VL-005. [user decision: whole-drive faults; architecture_context.txt "MODULE 2"]
- REQ-3: Each frame is checked by the rule registry. VL-001: a frame_id in 0..frame_count-1 is absent (one quarantine row per missing id). VL-002: a timestamp is not `frame_id * 62,500,000` ns. VL-003: a frame_id appears more than once (identical copies: keep the first and quarantine the repeats; copies that differ: quarantine all). VL-004: the payload is not `points_per_frame` x 3 int32 (the drive's own points per frame, `points_per_axis` cubed) or the points are not the expected lattice for the drive's body. VL-006: the centroid is farther from the last accepted frame than the speed limit allows. Only the failed frame goes to quarantine. [user decisions: quarantine granularity, VL-001, VL-002, VL-003, VL-004; architecture_context.txt "MODULE 2"]
- REQ-4: VL-006 compares the centroid of a frame with the last accepted frame. The allowed distance is `max_speed / fps * (frame_id gap) + rounding margin`, with the maximum speed 9,000 cm/s and the margin 2 cm, both in YAML. No rule tests acceleration. [user decision: VL-006 limit; Module 1 plan "Context" measurements]
- REQ-5: Each accepted frame is reduced to the keypoints listed in YAML: the 8 cube corners and the centroid. A corner is named by its bits (x, y, z), each 0 or 1, and its lattice index is derived from the drive's `points_per_axis` n: `bx*(n-1)*n*n + by*(n-1)*n + bz*(n-1)` (n = 10 gives 0, 9, 90, 99, 900, 909, 990, 999; n = 100 gives 0, 99, 9900, 9999, 990000, 990099, 999900, 999999). The keypoints table has one row per keypoint (long layout). [user decision: 9 keypoints, long layout; architecture_context.txt "MODULE 2"]
- REQ-6: The lake has three tables, `keypoints`, `quarantine`, `lineage`, as Parquet folders partitioned by drive (`<table>/drive=<hash12>/`), behind one `Lake` class with partition swap, read, list and a DuckDB connection with a view per table. [architecture_context.txt "DECISIONS"; assignment.pdf 3.2c]
- REQ-7: Re-ingesting a drive replaces its partitions and leaves the lake identical. A failed write keeps the old partition. The swap works on Windows. The lineage row holds no wall-clock time. [architecture_context.txt "MODULE 2"; user decision: risks]
- REQ-8: The ETL reads `frames.parquet` in chunks of a configured number of frames, never the whole drive at once. [user decision: chunked ingest]
- REQ-9: The ETL never opens `ground_truth.parquet`. The lineage row stores its manifest hash so evaluation can tie it back. [architecture_context.txt "DECISIONS"; forgeloopp.md Conventions]
- REQ-10: `python -m vl_data ingest <drive_dir>` and `python -m vl_data ingest --all <raw_dir>` call `ingest_drive`, print a summary, and exit 0 when the drive was ingested (even if frames were quarantined) and 1 when it was rejected. The summary prints status, frames_expected, frames_in, frames_kept, frames_quarantined (the number of quarantine rows), keypoint_rows, points_per_frame_in and keypoints_per_frame_out. [user decision: entry points; Module 1 plan REQ-10]
- REQ-11: The error-code list `docs/ERROR_CODES.md` is generated from the rule registry (`python -m vl_data error-codes --write`, `--check`). A test proves each example fault in the registry exists in `faults.py` and produces exactly its code. [user decision: docs rule; assignment.pdf 3.3a]
- REQ-12: Each task updates its own sections of docs/ARCHITECTURE.md, docs/ASSUMPTIONS.md and docs/RELEASE_NOTES.md. Every number or command written in a doc comes from a real run in that task. Each assumption becomes a row in ASSUMPTIONS.md with its source and what changes if it is wrong. [user decision: docs rule, via the generator session]
- REQ-13: Skeleton and tooling follow Module 1: ruff, mypy and pytest clean, coverage at least 80%, Python 3.11 or newer, Windows PowerShell, `python -m` only. [forgeloopp.md Gates and Conventions]
- REQ-14: The reduction is reported: the summary prints points per frame in and keypoints per frame out. [architecture_context.txt "MODULE 2"; assignment.pdf 3.2b]
- REQ-15: The ETL schemas carry a `schema_version`. The ETL accepts the versions listed in YAML (1 for now). [user decision: schema version]

## Non-Goals

- The generator, the use-case algorithm (velocity, acceleration), the evaluation step, BI, the SDK (VLClient).
- Noise handling, yaw, direction of travel, acceleration checks per frame.
- Real streaming (Kafka) and parallel ingest. Chunked reading stands in; streaming goes in "coming soon".
- A web service and a quality-report function (SQL over the quarantine table covers it).
- The docs track for the 3 use cases, the design rationale and the final docs pass (separate plan). This plan only writes its own doc sections and the error-code file.

## Constraints

- Python 3.11 or newer; Windows PowerShell; commands run as `python -m <module>` only; type hints everywhere; units in every docstring (cm, seconds, ns).
- Libraries: pandas, numpy, PyArrow, DuckDB, pydantic, PyYAML, Typer, pytest, pytest-cov, hypothesis, ruff, mypy.
- Coverage threshold 80; forbidden edit paths `warehouse/`, `data/`, `*.parquet`. Tests write only to `tmp_path`.
- Everything runs on CPU on one machine.
- Import direction: `core` <- `generator` <- `etl`. The ETL imports `core` and `generator.body`, never the reverse.
- Module 1 must be merged first. Each task lists the Module 1 task it needs.

## Success Criteria

- A clean demo drive (1,000 frames, at the density the generator produces: 1,000 points per frame today, 1,000,000 planned) ingests with 1,000 frames kept, 0 quarantined, and 9,000 keypoint rows (9 per frame). The real run reports its wall time and peak memory.
- Each of the five frame faults (VL-001, VL-002, VL-003, VL-004, VL-006), injected one at a time into the demo drive, gives exactly one quarantine row with its own code, and the other frames stay in keypoints (999 frames kept; for VL-001 the file has 999 frames and all 999 are kept).
- VL-005 (renamed points column) rejects the whole drive: one VL-005 row, no keypoints, status `rejected`.
- A changed byte in `frames.parquet` rejects the drive: no keypoints, one `INTEGRITY` row, status `rejected`, exit code 1.
- Ingesting the same drive twice leaves every table file byte-identical.
- The lineage row ties the scenario hash to the frames file hash, the ground-truth file hash and the row counts.
- DuckDB can query the lake with SQL, for example `SELECT keypoint, count(*) FROM keypoints GROUP BY 1`.
- `docs/ERROR_CODES.md` regenerates with no diff, and its example faults are real.
- ruff and mypy are clean; coverage is at least 80%.

## Explicit User Decisions

| Decision | User Answer | Resolved in Stage |
|----------|-------------|-------------------|
| Quarantine granularity | "Failed frame only (Recommended)" | 1 |
| Hash failure | "Abort the ingest (Recommended)"; later superseded for the record by "Accept all four" (drive-level quarantine row for whole-file faults and hash failures) | 1, 3 |
| What the lake stores per frame | "9 keypoints (Recommended)" | 1 |
| VL-006 limits | "YAML config, I set them (Recommended)"; then "what is the maximum jump a tesla could do in a real drive? this should be the limit". Recorded as 9,000 cm/s (about 324 km/h), which is 564.5 cm per frame including the 2 cm margin; the user confirmed the fastest Tesla top speed as 322 km/h (8,944 cm/s), rounded up to 9,000 cm/s | 1, 3 |
| core/ owner | "Generator plan builds it (Recommended)" | 1 |
| Improvements | "Chunked ingest, Docs track, Schema version" | 1 |
| Run proof | "Yes, CPU only (Recommended)" | 1 |
| Known risks | "Scale gap, Windows paths and locks, Fixed point order" | 1 |
| Sources | "Approve all (Recommended)" | 2 |
| Who builds the Lake class and table schemas | "Module 2 plan builds them (Recommended)" | 2 |
| Keypoints table layout | "Accept all four": long (one row per keypoint) | 3 |
| Whole-file faults (VL-005) and hash failures | "Accept all four": one drive-level quarantine row, keypoints stay empty | 3 |
| Quarantine row content | "Accept all four": details only (the raw frame stays in data/raw) | 3 |
| Duplicate frames (VL-003) | "Accept all four": keep the first if the copies are identical, else quarantine all | 3 |
| Docs rule | "Yes, apply it" | 3 |
| Full-density change (2026-10-24) | "Yes, amend and build parametric (Recommended)": amend the plan so TASK-3 to TASK-5 work at any density derived from the manifest, with small bodies in tests; the generator's own move to 1,000,000 points per frame was decided by the user in the generator session | amendment |
| VL-004 check | "All 1,000 points vs the lattice (Recommended)" | 3 |
| VL-002 meaning | "Compare to frame_id x 62.5 ms (Recommended)" | 3 |
| Missing frames (VL-001) | "One quarantine row per missing frame_id (Recommended)" | 3 |
| Entry points | "CLI ingest plus a Python function (Recommended)" | 3 |

## Unresolved Decisions

NONE

## Design

The ETL is a pipeline of small, separately testable steps with one entry point, `ingest_drive`.

1. **Gate on integrity** (REQ-1, REQ-15). Read the manifest, call Module 1's `verify_drive`, check `schema_version`, and check that the folder name equals the first 12 hex of the manifest hash. Any failure rejects the drive before any frame is read.
2. **Gate on schema** (REQ-2). Open `frames.parquet` with `pq.ParquetFile` and compare its schema with `FRAMES_SCHEMA`. A mismatch is VL-005 and rejects the drive. Only metadata is read.
3. **Scan, pass 1** (REQ-3, REQ-8, REQ-9). Stream the file in chunks (`iter_batches`). For each row, record one small "fact": row index, frame_id, timestamp, payload byte length, payload SHA-256, whether the payload is a valid cloud, and the centroid. A cloud is valid when it decodes to `points_per_frame` x 3 int32 and every point minus the first point equals the expected lattice offsets (this also proves the fixed point order the keypoint pick depends on). The facts table has one row per frame, so memory stays small.
4. **Rules** (REQ-3, REQ-4). A registry of six `Rule` objects, each with its code, name, meaning, detection text and example fault. VL-001 to VL-004 run on the facts table. VL-006 runs only on frames that passed the other rules and compares each centroid with the last accepted one, scaled by the frame_id gap, so one bad frame never poisons its neighbours. The registry also drives the generated error-code document.
5. **Reduce, pass 2** (REQ-5, REQ-8). Stream the file again, keep the accepted rows, and pick the keypoints by index from the decoded array. The centroid is the mean of all the frame's points. Batches go straight into the lake writer.
6. **Write** (REQ-6, REQ-7). The `Lake` writes each table's new partition into a temporary folder, then moves the old folder aside, moves the new one in, and deletes the old one. A failure while writing leaves the old partition untouched. Rerunning fixes a crash between the moves. The lineage row carries counts and hashes but no clock time, so a re-ingest is byte-identical.
7. **Docs** (REQ-11, REQ-12). `render_error_codes()` builds `docs/ERROR_CODES.md` from the registry. Each task also edits its own doc sections.

Design choices are backed by REQ-1 to REQ-15, the user decisions table, and the sources above: PyArrow for chunked Parquet, pandas for the facts table, DuckDB for SQL, hashlib for payload hashes.

### New Code And How It Connects

- `core/lake_schemas.py`: Arrow schemas for `keypoints`, `quarantine` and `lineage`. Used by the `Lake` class and the ETL.
- `core/lake.py`: `Lake`. Swaps, reads and lists partitions and gives a DuckDB connection with one view per table.
- `etl/config.py`: `EtlConfig`, `KeypointSpec`, `LimitsConfig`, `load_etl_config`. Reads `config/etl.yaml`; `VL_LAKE_DIR` overrides the lake folder.
- `etl/keypoints.py`: `extract_keypoints`. Picks corners by index and computes the centroid.
- `etl/body.py`: `drive_body`. Gives the ETL the drive's own body (size and points per axis) from its manifest, so no density is hard-coded.
- `etl/facts.py`: `scan_frames`. Pass 1: one fact row per frame, streamed.
- `etl/rules.py`: `Rule`, `RuleContext`, `RULES`, `run_rules`. The six rules and the registry.
- `etl/ingest.py`: `ingest_drive`, `IngestResult`. Runs the gates, scan, rules, reduction and writes.
- `etl/errors_doc.py`: `render_error_codes`. Builds the error-code Markdown from the registry.
- `etl/cli.py`: `register(app)` adds `ingest` and `error-codes`.
- Existing project code it talks to:
  - `core.manifest` (`read_manifest`, `verify_drive`): checks the files against the manifest.
  - `core.schemas` (`FRAMES_SCHEMA`, `decode_points`): the frames contract and the payload decoder.
  - `core.config` (`Config`, `BodyConfig`): fps and cube size.
  - `generator.body.cube_lattice`: the expected lattice offsets, one source of truth.
  - `generator.faults`: used only by tests and by the error-code check.
  - Root `cli.py`: the Typer app that `etl.cli.register` extends.

### Why This Design Stays Simple

- It reuses Module 1's manifest check, schemas, payload decoder, lattice and fault helper. Nothing is redefined.
- It reuses the Module 1 CLI pattern (`register(app)`) and the temp-folder swap idea.
- Rules are plain functions over one small DataFrame, so each is testable alone and the same registry serves the code and the docs.
- Genuinely new, and necessarily so: the `Lake` class (the architecture names it but no plan builds it), the rule registry (the assignment asks for an error-code list), and the two-pass scan (it is what keeps memory bounded while rules still see the whole sequence).
- No quality-report function, SDK, streaming, noise handling or acceleration check was added.

## Files / Modules

| Action | Path | Purpose | Change |
|--------|------|---------|--------|
| modify | pyproject.toml | dependencies | add `duckdb>=2.0` to the runtime dependencies (added during TASK-1; the plan had omitted it) |
| create | src/vl_data/core/lake_schemas.py | lake table schemas | Arrow schemas for keypoints, quarantine, lineage |
| create | src/vl_data/core/lake.py | lake storage | `Lake` with swap, drop, read, list and DuckDB connection |
| create | src/vl_data/etl/__init__.py | package | exports `ingest_drive`, `IngestResult` |
| create | src/vl_data/etl/config.py | ETL config | pydantic models and YAML loader with `VL_LAKE_DIR` override |
| create | src/vl_data/etl/keypoints.py | reduction | `extract_keypoints` |
| create | src/vl_data/etl/body.py | drive body | `drive_body`: the body (size_cm, points_per_axis) a drive was generated with, read from its manifest (amendment) |
| create | tests/etl/test_body.py | tests | drive_body from a manifest with and without body keys (amendment) |
| create | src/vl_data/etl/facts.py | scan pass 1 | `scan_frames`, streamed |
| create | src/vl_data/etl/rules.py | rules | registry and six rules |
| create | src/vl_data/etl/ingest.py | orchestration | gates, scan, rules, reduction, writes |
| create | src/vl_data/etl/errors_doc.py | error-code doc | Markdown rendered from the registry |
| create | src/vl_data/etl/cli.py | commands | `ingest`, `error-codes`, `register(app)` |
| modify | src/vl_data/cli.py | root CLI | call `etl.cli.register(app)` |
| create | config/etl.yaml | ETL config | keypoint list, limits, chunk size, supported schema versions, lake folder |
| modify | docs/ERROR_CODES.md | error-code list | regenerated from the registry |
| modify | docs/ARCHITECTURE.md | design doc | Module 2 and lake sections, filled per task from real runs |
| modify | docs/ASSUMPTIONS.md | assumptions log | new rows M2-A1 and later (decisions M2-D1 and later), one per assumption in this plan; the M2- prefix avoids clashes with the generator's rows |
| modify | docs/RELEASE_NOTES.md | release notes | ETL usage, real output, known issues, coming soon |
| create | tests/core/test_lake.py | tests | Lake behaviour |
| create | tests/etl/test_config.py | tests | ETL config and override |
| create | tests/etl/test_keypoints.py | tests | keypoint extraction |
| create | tests/etl/test_facts.py | tests | scan pass, chunking |
| create | tests/etl/test_rules.py | tests | one test per rule and per edge |
| create | tests/etl/test_ingest.py | tests | gates, quarantine, lineage, idempotency |
| create | tests/etl/test_errors_doc.py | tests | doc generation and fault check |
| create | tests/etl/test_cli.py | tests | ingest and error-codes commands |
| create | tests/etl/test_e2e.py | tests | generate, ingest, fault drives, SQL |
| modify | tests/conftest.py | fixtures | demo drive and lake fixtures |

## Classes / Functions / Signatures

```python
# file: src/vl_data/core/lake_schemas.py
import pyarrow as pa

KEYPOINTS_SCHEMA: pa.Schema   # frame_id int64, timestamp_ns int64, keypoint string,
                              # x_cm float64, y_cm float64, z_cm float64
QUARANTINE_SCHEMA: pa.Schema  # frame_id int64 (nullable), row_index int64 (nullable),
                              # code string, message string
LINEAGE_SCHEMA: pa.Schema     # scenario_hash string (nullable), schema_version int32 (nullable),
                              # status string, frames_expected int64, frames_in int64,
                              # frames_kept int64, frames_rejected int64, frames_missing int64,
                              # keypoint_rows int64, frames_sha256 string (nullable),
                              # ground_truth_sha256 string (nullable)
TABLE_SCHEMAS: dict[str, pa.Schema]   # "keypoints", "quarantine", "lineage"
```

```python
# file: src/vl_data/core/lake.py
from collections.abc import Iterable
from pathlib import Path
import duckdb
import pyarrow as pa

class Lake:
    def __init__(self, root: Path) -> None:
        """A lake of Hive-style Parquet folders: <root>/<table>/drive=<hash12>/part-0.parquet."""
        raise NotImplementedError

    def swap_partition(self, table: str, drive: str, batches: Iterable[pa.RecordBatch]) -> Path:
        """Write batches to a temp folder, then replace the drive's partition; keep the old one on failure."""
        raise NotImplementedError

    def drop_partition(self, table: str, drive: str) -> None:
        """Remove a drive's partition if it exists."""
        raise NotImplementedError

    def list_drives(self, table: str) -> list[str]:
        """Drive ids that have a partition in the table, sorted."""
        raise NotImplementedError

    def read(self, table: str, drive: str | None = None) -> pa.Table:
        """Read one drive or all drives; the result has the table schema plus a string column drive."""
        raise NotImplementedError

    def connect(self) -> duckdb.DuckDBPyConnection:
        """In-memory DuckDB connection with a view per table (empty typed view when a table has no partition)."""
        raise NotImplementedError
```

```python
# file: src/vl_data/etl/config.py
from pathlib import Path
from typing import Literal
from pydantic import BaseModel

class KeypointSpec(BaseModel):
    name: str
    kind: Literal["index", "corner", "centroid"] = "index"
    index: int | None = None          # explicit lattice index, required when kind == "index"
    bits: tuple[int, int, int] | None = None   # corner (x, y, z), each 0 or 1, required when kind == "corner"

class LimitsConfig(BaseModel):
    max_speed_cm_s: float = 9000.0    # cm/s, fastest speed the ETL accepts
    rounding_margin_cm: float = 2.0   # cm, added to the allowed jump

class EtlConfig(BaseModel):
    supported_schema_versions: list[int] = [1]
    chunk_mib: int = 128              # MiB of raw point data read per chunk (replaces chunk_frames, amended 2026-10-24)
    lake_dir: Path = Path("warehouse")
    limits: LimitsConfig = LimitsConfig()
    keypoints: list[KeypointSpec]     # default: the nine built-in keypoints (corner_000 .. corner_111, centroid); same as config/etl.yaml

    def chunk_frames(self, points_per_frame: int) -> int:
        """Frames per chunk so that a chunk holds about chunk_mib MiB of int32 xyz points (at least 1)."""
        raise NotImplementedError

    def max_jump_cm(self, fps: int, frame_gap: int) -> float:
        """Largest centroid distance in cm allowed across frame_gap frames."""
        raise NotImplementedError

def load_etl_config(path: Path | None = None) -> EtlConfig:
    """Load and validate config/etl.yaml; VL_LAKE_DIR overrides lake_dir."""
    raise NotImplementedError
```

```python
# file: src/vl_data/etl/keypoints.py
import numpy as np
from numpy.typing import NDArray
from vl_data.etl.config import KeypointSpec

def extract_keypoints(points: NDArray[np.int32], specs: list[KeypointSpec]) -> NDArray[np.float64]:
    """From (frames, n, 3) int32 cm points return (frames, len(specs), 3) float64 cm; centroid is the mean."""
    raise NotImplementedError
```

```python
# file: src/vl_data/etl/body.py
from vl_data.core.config import BodyConfig
from vl_data.core.manifest import Manifest

def drive_body(manifest: Manifest, fallback: BodyConfig) -> BodyConfig:
    """Body of the drive: the manifest's body keys when present (names to be agreed with the generator);
    otherwise points_per_axis = round(cbrt(points_per_frame)) checked by cube, and size_cm from fallback.
    Raises ValueError if points_per_frame is not a perfect cube."""
    raise NotImplementedError
```

```python
# file: src/vl_data/etl/facts.py
from pathlib import Path
import numpy as np
import pandas as pd
from numpy.typing import NDArray

FACT_COLUMNS: list[str]         # row_index, frame_id, timestamp_ns, n_bytes, payload_sha256,
                                # cloud_ok, cx_cm, cy_cm, cz_cm

def scan_frames(frames_path: Path, offsets: NDArray[np.int32], chunk_frames: int) -> pd.DataFrame:
    """Stream frames.parquet in chunks and return one fact row per row of the file (cm, ns)."""
    raise NotImplementedError
```

```python
# file: src/vl_data/etl/rules.py
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal
import pandas as pd
import pyarrow as pa
from vl_data.etl.config import LimitsConfig

VIOLATION_COLUMNS: list[str]         # row_index (Int64, nullable), frame_id (Int64, nullable), code, message

@dataclass(frozen=True)
class RuleContext:
    facts: pd.DataFrame               # output of scan_frames (empty for drive rules)
    schema: pa.Schema                 # schema of frames.parquet
    frame_count: int                  # from the manifest
    fps: int
    frame_interval_ns: int            # 62,500,000 at 16 fps
    limits: LimitsConfig

@dataclass(frozen=True)
class Rule:
    code: str                         # "VL-001" .. "VL-006"
    name: str
    meaning: str
    detection: str
    example_fault: str                # class name in vl_data.generator.faults
    scope: Literal["drive", "frame"]
    needs_clean_frames: bool          # True for VL-006
    check: Callable[[RuleContext], pd.DataFrame]

RULES: list[Rule]                 # VL-001 to VL-006, in order

def run_rules(ctx: RuleContext, scope: Literal["drive", "frame"]) -> pd.DataFrame:
    """Run the rules of a scope; VL-006 sees only frames that passed the other frame rules."""
    raise NotImplementedError
```

```python
# file: src/vl_data/etl/ingest.py
from dataclasses import dataclass
from pathlib import Path
from vl_data.core.config import Config
from vl_data.core.lake import Lake
from vl_data.etl.config import EtlConfig

@dataclass(frozen=True)
class IngestResult:
    drive: str                        # first 12 hex of the scenario hash (folder name)
    status: str                       # "ok" or "rejected"
    frames_expected: int
    frames_in: int
    frames_kept: int
    frames_rejected: int
    frames_missing: int
    keypoint_rows: int
    quarantine_by_code: dict[str, int]

def ingest_drive(drive_dir: Path, lake: Lake, config: Config, etl: EtlConfig) -> IngestResult:
    """Verify, validate and reduce one drive and swap its partitions into the lake; never reads ground truth."""
    raise NotImplementedError
```

```python
# file: src/vl_data/etl/errors_doc.py
def render_error_codes() -> str:
    """Markdown for docs/ERROR_CODES.md, built from RULES plus the INTEGRITY note."""
    raise NotImplementedError
```

```python
# file: src/vl_data/etl/cli.py
from pathlib import Path
import typer

def register(app: typer.Typer) -> None:
    """Add the ingest and error-codes commands to the root app."""
    raise NotImplementedError

def ingest(drive_dir: Path | None = None, all_in: Path | None = None, lake_dir: Path | None = None) -> None:
    """Ingest one drive folder, or every drive folder under --all; exit 1 if any drive is rejected."""
    raise NotImplementedError

def error_codes(write: bool = False, check: bool = False) -> None:
    """Print the generated error-code Markdown; --write saves it, --check exits 1 if the file differs."""
    raise NotImplementedError
```

## Task Breakdown

- TASK-1: Lake and lake schemas
  - Depends on: Module 1 TASK-2 merged (core package, schemas, manifest, pyproject)
  - Files: pyproject.toml, src/vl_data/core/lake_schemas.py, src/vl_data/core/lake.py, tests/core/test_lake.py, docs/ARCHITECTURE.md (lake section), docs/ASSUMPTIONS.md
  - REQs covered: REQ-6, REQ-7, REQ-12, REQ-13
  - Source: DuckDB docs, PyArrow docs

- TASK-2: ETL config and keypoint extraction
  - Depends on: TASK-1, Module 1 TASK-3 merged (cube_lattice)
  - Files: src/vl_data/etl/__init__.py, config.py, keypoints.py, config/etl.yaml, tests/etl/test_config.py, tests/etl/test_keypoints.py, docs/ASSUMPTIONS.md
  - REQs covered: REQ-4, REQ-5, REQ-12, REQ-13
  - Source: pydantic docs, PyYAML docs, numpy docs, architecture_context.txt

- TASK-2b (amendment 2026-10-24): Density-independent keypoints, byte-sized chunks and the drive-body adapter
  - Depends on: TASK-2
  - Files: src/vl_data/etl/config.py, keypoints.py, body.py, config/etl.yaml, tests/etl/test_config.py, tests/etl/test_keypoints.py, tests/etl/test_body.py, docs/ARCHITECTURE.md, docs/ASSUMPTIONS.md
  - REQs covered: REQ-4, REQ-5, REQ-8, REQ-12, REQ-13
  - Source: pydantic docs, numpy docs, architecture_context.txt, the user's full-density decision

- TASK-3: Frame scan and rule registry (VL-001 to VL-006)
  - Depends on: TASK-2b, Module 1 TASK-4 and TASK-6 merged (writer, fault injection)
  - Files: src/vl_data/etl/facts.py, rules.py, tests/etl/test_facts.py, tests/etl/test_rules.py, tests/conftest.py, docs/ARCHITECTURE.md (rules section), docs/ASSUMPTIONS.md
  - REQs covered: REQ-2, REQ-3, REQ-4, REQ-8, REQ-9, REQ-12, REQ-13
  - Source: PyArrow docs, pandas docs, hashlib docs, assignment.pdf

- TASK-4: Ingest orchestration, quarantine, lineage and idempotent writes
  - Depends on: TASK-1, TASK-3
  - Files: src/vl_data/etl/ingest.py, __init__.py, tests/etl/test_ingest.py, docs/ARCHITECTURE.md (data flow, error handling), docs/ASSUMPTIONS.md
  - REQs covered: REQ-1, REQ-2, REQ-5, REQ-6, REQ-7, REQ-8, REQ-9, REQ-14, REQ-15
  - Source: PyArrow docs, DuckDB docs, architecture_context.txt

- TASK-5: CLI, error-code document, end-to-end proof
  - Depends on: TASK-4
  - Files: src/vl_data/etl/cli.py, errors_doc.py, src/vl_data/cli.py, docs/ERROR_CODES.md, docs/RELEASE_NOTES.md, docs/ARCHITECTURE.md, docs/ASSUMPTIONS.md, tests/etl/test_cli.py, test_errors_doc.py, test_e2e.py
  - REQs covered: REQ-10, REQ-11, REQ-12, REQ-13, REQ-14
  - Source: Typer docs, assignment.pdf, DuckDB docs

## Tests Per Task

| Task | Test file | Test name | What it verifies |
|------|-----------|-----------|-----------------|
| TASK-1 | tests/core/test_lake.py | test_swap_creates_hive_partition | a swap creates `<table>/drive=<id>/part-0.parquet` with the table schema |
| TASK-1 | tests/core/test_lake.py | test_swap_replaces_and_is_idempotent | swapping the same data twice leaves byte-identical files and one partition |
| TASK-1 | tests/core/test_lake.py | test_failed_write_keeps_old_partition | an exception inside the batch iterator leaves the old data and no temp folder |
| TASK-1 | tests/core/test_lake.py | test_swap_after_connection_closed | a partition read by DuckDB and then closed can be swapped (renamed from test_swap_over_open_reader_on_windows, because the test closes the connection first) |
| TASK-1 | tests/core/test_lake.py | test_wrong_schema_rejected | batches with a different schema raise ValueError and write nothing |
| TASK-1 | tests/core/test_lake.py | test_list_and_read | list_drives is sorted; read adds a string drive column; read of one drive filters |
| TASK-1 | tests/core/test_lake.py | test_duckdb_views_query | SQL group by over the keypoints view returns expected counts; a 12-digit numeric drive id stays a string |
| TASK-1 | tests/core/test_lake.py | test_empty_table_view | a table with no partition gives an empty typed view |
| TASK-1 | tests/core/test_lake.py | test_drop_partition | the partition disappears and a missing one is a no-op |
| TASK-1 | tests/core/test_lake.py | test_table_schemas_match_plan | field names, types, nullability of the three schemas and the TABLE_SCHEMAS keys (added during TASK-1) |
| TASK-1 | tests/core/test_lake.py | test_zero_batches_writes_valid_empty_file | a swap with no batches writes a readable zero-row file (the docs do not state it; added after the source check) |
| TASK-1 | tests/core/test_lake.py | test_view_over_zero_row_partition_mixed_with_data | SQL over one zero-row drive and one drive with rows returns only the rows (added after the source check) |
| TASK-1 | tests/core/test_lake.py | test_invalid_table_and_drive_rejected | unknown table, empty id, path separators, trailing newline and non-ASCII digits raise ValueError |
| TASK-1 | tests/core/test_lake.py | test_temp_folders_not_listed | swap folders under `_swap` never show in list_drives or in a view |
| TASK-1 | tests/core/test_lake.py | test_foreign_folder_with_part_file_is_invisible | a foreign folder holding a part file does not change list_drives or the view (added after review) |
| TASK-1 | tests/core/test_lake.py | test_special_characters_in_root_path | a lake root containing `[ ] { }` works through the view (`*` and `?` cannot be tested on Windows) (added after review) |
| TASK-1 | tests/core/test_lake.py | test_invalid_drive_folder_names_not_listed | folders named `drive=a b` or `drive=` are not listed and do not break read (added after review) |
| TASK-1 | tests/core/test_lake.py | test_drop_partition_propagates_errors | a delete error is raised and the partition stays (added after review) |
| TASK-1 | tests/core/test_lake.py | test_interrupt_in_batches_keeps_old_partition | RuntimeError, KeyboardInterrupt and SystemExit from the batch iterator keep the old partition (added after review) |
| TASK-1 | tests/core/test_lake.py | test_restore_failure_keeps_original_exception_with_note | a failed restore re-raises the original error with a note naming the old folder (added after review) |
| TASK-1 | tests/core/test_lake.py | test_swap_sweeps_leftover_swap_folders | only stale (over one hour) `_swap/new-*` folders are removed by the next swap; fresh `new-*` and all `old-*` stay (added after review) |
| TASK-1 | tests/core/test_lake.py | test_second_swap_does_not_destroy_unrestored_old_partition | after a failed restore, later swaps leave the only copy in `_swap/old-*` intact (added after review) |
| TASK-1 | tests/core/test_lake.py | test_view_with_hive_like_segments_in_root | a root containing `env=prod` and `drive=x` segments adds no extra column to the view (added after review) |
| TASK-1 | tests/core/test_lake.py | test_many_drives_in_one_view | 500 drives in one table return 500 rows through the view (added after review) |
| TASK-1 | tests/core/test_lake.py | test_swap_with_open_connection_idle | on Windows a swap succeeds while an idle DuckDB connection is open (observed on this machine) (added after review) |
| TASK-2 | tests/etl/test_config.py | test_defaults_load | config/etl.yaml loads with 9 keypoints and the 9,000 cm/s limit |
| TASK-2 | tests/etl/test_config.py | test_env_override_lake_dir | VL_LAKE_DIR changes lake_dir |
| TASK-2 | tests/etl/test_config.py | test_bad_keypoint_spec_rejected | an index spec with no index, or a negative index, raises |
| TASK-2 | tests/etl/test_config.py | test_max_jump_scales_with_gap | at 16 fps the jump for gap 1 is 564.5 cm and for gap 2 is 1,127 cm |
| TASK-2 | tests/etl/test_keypoints.py | test_corners_by_index | corner names map to lattice indices 0, 9, 90, 99, 900, 909, 990, 999 |
| TASK-2 | tests/etl/test_keypoints.py | test_centroid_is_mean | the centroid of a shifted lattice equals the shift plus 49.5 cm |
| TASK-2 | tests/etl/test_keypoints.py | test_shift_moves_all_keypoints | a 1 cm x shift moves every keypoint by exactly (1, 0, 0) |
| TASK-2b | tests/etl/test_keypoints.py | test_corner_kind_derives_index_from_density | kind "corner" with bits picks lattice indices 0, 9, 90, 99, 900, 909, 990, 999 at 10 points per axis and 0, 99, 9900, 9999, 990000, 990099, 999900, 999999 at 100 points per axis |
| TASK-2b | tests/etl/test_keypoints.py | test_corner_kind_rejects_non_cube_point_count | a point count that is not a cube raises ValueError |
| TASK-2b | tests/etl/test_config.py | test_chunk_frames_from_chunk_mib | chunk_frames(points_per_frame) is at least 1 and scales inversely with the points per frame (for example 11,184 frames at 1,000 points and 11 frames at 1,000,000 points for 128 MiB) |
| TASK-2b | tests/etl/test_config.py | test_corner_spec_validation | kind "corner" needs bits of 0 or 1; "index" and "centroid" take no bits |
| TASK-2b | tests/etl/test_config.py | test_defaults_use_corner_kind | the built-in keypoints and config/etl.yaml use kind "corner" for the eight corners and are identical |
| TASK-2b | tests/etl/test_body.py | test_body_from_points_per_frame | 1,000 points per frame gives 10 per axis and 1,000,000 gives 100, size_cm from the fallback |
| TASK-2b | tests/etl/test_body.py | test_body_rejects_non_cube | 999 or 1,001 points per frame raises ValueError |
| TASK-3 | tests/etl/test_facts.py | test_one_fact_per_row | a 1,000-frame drive gives 1,000 fact rows with matching ids and hashes |
| TASK-3 | tests/etl/test_facts.py | test_chunking_gives_same_facts | chunk sizes 1, 7 and 256 give identical facts |
| TASK-3 | tests/etl/test_facts.py | test_corrupt_payload_not_cloud_ok | a 999-point payload and a moved point are cloud_ok False with no crash |
| TASK-3 | tests/etl/test_facts.py | test_ground_truth_never_opened | scanning works with ground_truth.parquet deleted |
| TASK-3 | tests/etl/test_rules.py | test_vl001_missing_frame | DropFrame gives one VL-001 row for that frame_id |
| TASK-3 | tests/etl/test_rules.py | test_vl001_two_missing_two_rows | two dropped ids give two rows |
| TASK-3 | tests/etl/test_rules.py | test_vl002_shifted_timestamp | ShiftTimestamp flags only that frame |
| TASK-3 | tests/etl/test_rules.py | test_vl003_identical_duplicate_keeps_first | an identical repeat gives one VL-003 row on the repeat |
| TASK-3 | tests/etl/test_rules.py | test_vl003_differing_duplicates_quarantine_all | differing copies give VL-003 rows for every copy |
| TASK-3 | tests/etl/test_rules.py | test_vl004_corrupt_points | CorruptPoints gives one VL-004 row |
| TASK-3 | tests/etl/test_rules.py | test_vl005_schema_mismatch | BreakSchema gives one drive-level VL-005 row with null frame_id |
| TASK-3 | tests/etl/test_rules.py | test_vl006_teleport_flags_one_frame | Teleport of 1,000 cm flags only that frame, not its neighbours |
| TASK-3 | tests/etl/test_rules.py | test_vl006_after_dropped_frame_uses_gap | a dropped frame does not trigger VL-006 on the next frame |
| TASK-3 | tests/etl/test_rules.py | test_vl006_just_inside_limit_passes | a jump of 560 cm passes and 570 cm fails at gap 1 |
| TASK-3 | tests/etl/test_rules.py | test_vl006_skips_other_failed_frames | a VL-004 frame is not used as the comparison baseline |
| TASK-3 | tests/etl/test_rules.py | test_clean_demo_has_no_violations | the unmodified demo drive gives zero violations |
| TASK-3 | tests/etl/test_rules.py | test_property_clean_drives_never_flagged | hypothesis: generated drives at speeds under the limit give no violations |
| TASK-4 | tests/etl/test_ingest.py | test_clean_drive_ingested | 1,000 kept, 0 quarantined, 9,000 keypoint rows, lineage counts match the manifest |
| TASK-4 | tests/etl/test_ingest.py | test_each_frame_fault_one_quarantine_row | each of VL-001, VL-002, VL-003, VL-004, VL-006 gives one quarantine row of its code and 999 kept frames (VL-003: file has 1,001 rows, 1,000 kept) |
| TASK-4 | tests/etl/test_ingest.py | test_tampered_file_rejects_drive | a changed byte gives one INTEGRITY row, status rejected, no keypoints |
| TASK-4 | tests/etl/test_ingest.py | test_unreadable_manifest_rejects_drive | a missing manifest rejects the drive and keeps the folder name as the drive id |
| TASK-4 | tests/etl/test_ingest.py | test_unsupported_schema_version_rejects | schema_version 2 rejects the drive |
| TASK-4 | tests/etl/test_ingest.py | test_folder_name_mismatch_rejects | a renamed drive folder rejects the drive |
| TASK-4 | tests/etl/test_ingest.py | test_frame_id_out_of_range_rejects | an id outside 0..frame_count-1 rejects the drive |
| TASK-4 | tests/etl/test_ingest.py | test_schema_mismatch_rejects_drive | BreakSchema gives one VL-005 row, status rejected |
| TASK-4 | tests/etl/test_ingest.py | test_rejected_after_ok_removes_keypoints | an ok drive later tampered has its keypoints removed |
| TASK-4 | tests/etl/test_ingest.py | test_reingest_is_byte_identical | ingesting twice leaves every lake file byte-identical |
| TASK-4 | tests/etl/test_ingest.py | test_reingest_after_fix_replaces_quarantine | a repaired drive replaces its old quarantine rows |
| TASK-4 | tests/etl/test_ingest.py | test_lineage_row_content | lineage has the scenario hash, both file hashes and the counts; no clock column |
| TASK-4 | tests/etl/test_ingest.py | test_ground_truth_not_read | with the Parquet readers patched to raise on any path named ground_truth.parquet, a clean ingest still succeeds |
| TASK-4 | tests/etl/test_ingest.py | test_keypoint_values_match_generator | centroid x of each kept frame is within 0.6 cm of the generator's ground truth x plus 49.5 cm (the test, not the ETL, reads ground truth) |
| TASK-4 | tests/etl/test_ingest.py | test_chunk_size_does_not_change_output | chunk sizes 1 and 256 give identical tables |
| TASK-5 | tests/etl/test_errors_doc.py | test_doc_regenerates_without_diff | `render_error_codes()` equals docs/ERROR_CODES.md |
| TASK-5 | tests/etl/test_errors_doc.py | test_example_faults_are_real | each registry example_fault exists in faults.py and, injected, reports exactly its code |
| TASK-5 | tests/etl/test_errors_doc.py | test_all_six_codes_present | the document lists VL-001 to VL-006 and the INTEGRITY note |
| TASK-5 | tests/etl/test_cli.py | test_ingest_one_drive_summary | the summary shows frames kept, quarantined, keypoints per frame and exit 0 |
| TASK-5 | tests/etl/test_cli.py | test_ingest_all | `--all` ingests every drive folder under a root |
| TASK-5 | tests/etl/test_cli.py | test_rejected_drive_exits_one | a tampered drive exits 1 |
| TASK-5 | tests/etl/test_cli.py | test_ingest_needs_one_target | giving neither a folder nor `--all`, or both, exits with a usage error |
| TASK-5 | tests/etl/test_cli.py | test_error_codes_check | `--check` exits 1 when the file differs and 0 when it matches |
| TASK-5 | tests/etl/test_e2e.py | test_generate_ingest_query | generate the demo drive, ingest, and DuckDB returns 9 keypoints per frame and a matching lineage row |
| TASK-5 | tests/etl/test_e2e.py | test_faulty_drives_end_to_end | each of the six faults written to disk and ingested gives its code in the quarantine view |

## Verification Commands

```powershell
python -m ruff check .
python -m mypy src
python -m pytest tests -q
python -m pytest --cov=vl_data --cov-report=term-missing --cov-fail-under=80 tests -q
python -m vl_data error-codes --check
```

## Risks / Residual Uncertainty

| Risk | Likelihood | Mitigation |
|------|-----------|-----------|
| The generator contract changes after this plan (schemas, manifest keys, fault behaviour) | med | Contract copied from the approved Module 1 plan; any change needs a plan amendment from the generator session. The ETL imports `FRAMES_SCHEMA`, `decode_points`, `verify_drive` and the fault classes, so renamed items fail at import time, not silently |
| Nothing from Module 1 is built or merged, so this branch cannot start TASK-1 | high | Tasks name the Module 1 task each needs; merge Module 1 into this branch first |
| `Teleport` is assumed to move only its own frame. If it shifts all later frames, the "last accepted frame" rule would quarantine every frame after it | med | The assumption is logged; `test_vl006_teleport_flags_one_frame` and the generator's own test show the real behaviour. If it differs, amend the rule, never loosen the limit |
| Fixed point order is assumed. A changed order passes the count check but fails the lattice check, so the whole drive's frames are quarantined as VL-004 | low | The lattice check covers all 1,000 points, so the failure is loud |
| Full density (planned by the generator): about 12 MB per frame and about 12 GB per 1,000-frame drive. The facts scan hashes, decodes and checks every payload, in two passes, so an ingest may take minutes. This machine has 15.7 GB of RAM and 414 GB of free disk | high | Chunks are sized in MiB (`chunk_mib`), not frames; row groups are about 50 frames; the real time and peak memory are measured in the TASK-5 run and written to the docs; tests use small bodies; if the full-density run is too slow, report it before the deadline instead of weakening the checks |
| The generator's manifest does not yet say what body (size and points per axis) a drive has; the key names are not decided | med | `etl/body.py` reads the drive's body from the manifest when present and otherwise derives `points_per_axis` from `points_per_frame` and takes `size_cm` from the core config; the generator will send the exact keys (and bump `schema_version` to 2 if the layout changes) before it edits core |
| Windows locks: DuckDB or Parquet readers holding files can block the folder swap | med | `connect()` returns an in-memory connection that reads files lazily; the tests `test_swap_after_connection_closed` and `test_swap_with_open_connection_idle` cover it (an idle open connection did not block a swap on this Windows machine); the old folder is renamed aside, not overwritten |
| A crash between moving the old folder aside and moving the new one in leaves no partition | low | A rerun recreates it; the three tables are swapped one after another, not atomically; stated as a known issue |
| DuckDB infers a 12-digit hex folder like `123456789012` as a number | low | The view reads with an explicit VARCHAR type for `drive`; `test_duckdb_views_query` uses a numeric-looking id |
| The VL-006 limit of 9,000 cm/s is a Tesla's 322 km/h (user-confirmed) rounded up, so a faster vehicle would be flagged | low | It lives in YAML and is logged as an assumption; change the value if a faster vehicle is in scope |
| A frame_id outside 0..frame_count-1 is not covered by VL-001 to VL-006 | low | The drive is rejected with code INTEGRITY and the message names the id; logged as an assumption |
| Parquet bytes differ across pyarrow versions, so a re-ingest is only byte-identical on one version | med | Fixed write options; stated as a known issue |
| ERROR_CODES.md text for VL-002 and VL-006 in main contradicts these decisions | low | TASK-5 regenerates the file from the registry |
| The docs files (ARCHITECTURE.md, ASSUMPTIONS.md, ERROR_CODES.md, RELEASE_NOTES.md) exist only uncommitted in the main checkout, not on this branch | med | Merge the docs branch from the generator session into this branch before TASK-1; each task edits only its own sections so the merge stays small |

## implement-loop Handoff

**Task file**: inline below (this plan). Run one task per implement-loop pass, TASK-1 first, after Module 1 TASK-2 is merged into `worktree-module2-loader-etl`.

**Acceptance criteria for implement-loop**:
- TASK-1: the lake swaps, reads, lists and drops partitions; a failed write keeps the old partition; DuckDB views query the tables, including an empty table; re-swap is byte-identical.
- TASK-2: `config/etl.yaml` loads with 9 keypoints and the 9,000 cm/s limit; `VL_LAKE_DIR` overrides the lake folder; keypoints are picked by index and the centroid is the mean; a 1 cm x shift moves all keypoints by (1, 0, 0).
- TASK-2b: the nine keypoints are derived from the drive's `points_per_axis` (corner indices at 10 and at 100 points per axis are exactly the ones listed in REQ-5); chunk size is set in MiB and converted to frames from the points per frame; `drive_body` returns the body from a manifest; `config/etl.yaml` and the built-in defaults still agree.
- TASK-3: the facts scan streams in chunks with identical results for any chunk size; each rule flags exactly its fault and nothing else; VL-006 respects the gap and the baseline rule; a clean drive gives zero violations; ground truth is never opened.
- TASK-4: a clean drive gives 1,000 kept and 9,000 keypoint rows; each fault gives one quarantine row; every whole-drive failure writes a drive-level row, a rejected lineage row and no keypoints; re-ingest is byte-identical; lineage ties the scenario hash to both file hashes and the counts.
- TASK-5: the CLI prints the summary and exits 0 or 1 as stated; `error-codes --check` passes on the regenerated document; the example faults are real; the end-to-end test queries the lake with SQL.
- All tasks: ruff and mypy clean; coverage at least 80%; tests write only to `tmp_path`; units in every docstring; each task updates its own doc sections with numbers from its own real run, and adds its assumptions to ASSUMPTIONS.md.

**Tests to write (TDD)**: every row of "Tests Per Task". Each test must fail first (RED), then pass (GREEN). Sources: the Module 1 plan for the contract, the library docs for API use, architecture_context.txt for the rules.

**Verification commands**:
```powershell
python -m ruff check .
python -m mypy src
python -m pytest tests -q
python -m pytest --cov=vl_data --cov-report=term-missing --cov-fail-under=80 tests -q
python -m vl_data error-codes --check
```

**Run proof** (see `../implement-loop/run-proof.md`):
```text
RUN_PROOF (TASK-1, Lake and lake schemas):
  device: cpu
  command: python -m pytest tests/core/test_lake.py -v
  cwd: repo root
  timeout: 00:05:00
  pass_criteria:
    - exit code 0
    - output contains "test_swap_replaces_and_is_idempotent PASSED"
    - output contains "test_failed_write_keeps_old_partition PASSED"
    - output contains "test_duckdb_views_query PASSED"
    - output contains no "FAILED"
  exercises: partition swap, failure safety, DuckDB views over the Hive layout

RUN_PROOF (TASK-2, ETL config and keypoints):
  device: cpu
  command: python -m pytest tests/etl/test_config.py tests/etl/test_keypoints.py -v
  cwd: repo root
  timeout: 00:05:00
  pass_criteria:
    - exit code 0
    - output contains "test_defaults_load PASSED"
    - output contains "test_max_jump_scales_with_gap PASSED"
    - output contains "test_shift_moves_all_keypoints PASSED"
  exercises: YAML config, the speed limit, index-based keypoint pick, the centroid

RUN_PROOF (TASK-2b, Density-independent keypoints, chunks and drive body):
  device: cpu
  command: python -m pytest tests/etl/test_config.py tests/etl/test_keypoints.py tests/etl/test_body.py -v
  cwd: repo root
  timeout: 00:10:00
  pass_criteria:
    - exit code 0
    - output contains "test_corner_kind_derives_index_from_density PASSED"
    - output contains "test_chunk_frames_from_chunk_mib PASSED"
    - output contains "test_body_from_points_per_frame PASSED"
    - output contains no "FAILED"
  exercises: corner indices at 10 and 100 points per axis, byte-sized chunks, the drive-body adapter

RUN_PROOF (TASK-3, Frame scan and rules):
  device: cpu
  command: python -m pytest tests/etl/test_facts.py tests/etl/test_rules.py -v
  cwd: repo root
  timeout: 00:10:00
  pass_criteria:
    - exit code 0
    - output contains PASSED for test_vl001_missing_frame, test_vl002_shifted_timestamp, test_vl003_identical_duplicate_keeps_first, test_vl004_corrupt_points, test_vl005_schema_mismatch and test_vl006_teleport_flags_one_frame
    - output contains "test_chunking_gives_same_facts PASSED"
    - output contains "test_clean_demo_has_no_violations PASSED"
  exercises: all six rules against the real fault helper, chunk independence

RUN_PROOF (TASK-4, Ingest orchestration):
  device: cpu
  command: python -m pytest tests/etl/test_ingest.py -v
  cwd: repo root
  timeout: 00:10:00
  pass_criteria:
    - exit code 0
    - output contains "test_clean_drive_ingested PASSED"
    - output contains "test_each_frame_fault_one_quarantine_row PASSED"
    - output contains "test_tampered_file_rejects_drive PASSED"
    - output contains "test_reingest_is_byte_identical PASSED"
    - output contains "test_keypoint_values_match_generator PASSED"
  exercises: gates, quarantine, lineage, idempotent swap, agreement with the generator's truth

RUN_PROOF (TASK-5, CLI, error-code document, end to end):
  device: cpu
  command: python -m vl_data generate --scenario src/vl_data/generator/scenarios/demo.yaml --out data/raw; if ($?) { python -m vl_data ingest --all data/raw --lake-dir warehouse }
  cwd: repo root
  timeout: 01:00:00
  pass_criteria:
    - exit code 0
    - output contains "frames_expected: 1000"
    - output contains "frames_kept: 1000"
    - output contains "frames_quarantined: 0"
    - output contains "keypoint_rows: 9000"
    - output contains "points_per_frame_in:" with the same value as the `points_per_frame:` line printed by the generate step (1000 today, 1000000 at full density), and "keypoints_per_frame_out: 9"
    - output contains "status: ok"
    - the run reports elapsed seconds (recorded in the run-proof record and the docs; no pass threshold, it is a measurement)
  exercises: the generator-to-lake path from the command line, the summary, the exit code

RUN_PROOF (TASK-5, error-code document and faulty drives):
  device: cpu
  command: python -m pytest tests/etl/test_errors_doc.py tests/etl/test_e2e.py tests/etl/test_cli.py -v; if ($?) { python -m vl_data error-codes --check }
  cwd: repo root
  timeout: 00:15:00
  pass_criteria:
    - exit code 0
    - output contains "test_example_faults_are_real PASSED"
    - output contains "test_faulty_drives_end_to_end PASSED"
    - output contains "test_generate_ingest_query PASSED"
    - output contains no "FAILED"
  exercises: generated error codes match real faults, six faulty drives through the CLI path and DuckDB
```

**Checklist**:
- [ ] Source check completed using sources listed in this plan
- [ ] RED evidence captured
- [ ] GREEN evidence captured
- [ ] Coverage evidence reported
- [ ] Clean-code pass completed (tests untouched, suite green)
- [ ] Reviewer gate passed
- [ ] Run-proof gate passed on the declared CPU/GPU target
- [ ] Plan/ADR/docs updated to match what was actually built, if this change invalidated any (including docs/architecture_context.txt)
- [ ] User approved commit
