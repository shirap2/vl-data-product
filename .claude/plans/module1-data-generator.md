# Plan: Module 1, data generator (straight-line VL drives, hashed, checked in both directions)

## Goal

Given a start position and a motion profile, write a deterministic, hashed mock drive (full 16 fps point cloud, separate ground truth, manifest), from a CLI or a Generate-tab form, with an inverse function that recovers the motion from the data to validate the simulator.

## User Request Summary

The user wants a data generator whose input is the vehicle's start location and a motion profile along the frames, with a minimal UI. It outputs the full sensor data, so the vehicle's sensor data can be simulated. The simulator is evaluated by running the reverse functions on the data and comparing with the generator inputs (a two-way transformation). Every file gets a hash signature so each file can be tied to the scenario that made it, and the extracted use-case data can later be checked against the ground truth. Configs are YAML files read by a Python loader. Technologies should be simple, modular and follow widespread engineering practice. Everything must answer the assignment PDF. The use case is velocity and acceleration only. The vehicle drives forwards, backwards and stops at different speeds. A cube's orientation cannot be seen in its points, so there is no yaw and no turning.

## Context

- The assignment asks for a mocked data source for a vehicle-location point-cloud signal at 16 frames per second, with a 1 m cube as the example vehicle and a 100^3 point lattice. Saving 1000 frames is allowed ("You don't need to save all the data"). [assignment.pdf, section 1.2 and section 3.2a]
- The assignment's second-frame example contains (1,2,0), which is a typo for (1,1,0). [assignment.pdf section 1.2; architecture_context.txt "ASSIGNMENT COVERAGE"]
- No data is supplied, so the source must be mocked. [assignment.pdf section 3 "Data"]
- The agreed design (straight line along x, signed speed, `move` and `stop` segments, hashed manifest, two-way inverse) and the decisions log are in architecture_context.txt. [architecture_context.txt "DECISIONS", "MODULE 1", "DECISIONS LOG"]
- The repo has no code yet. Only README.md is tracked. The skeleton is part of this plan (user decision).
- Measured on 2026-10-23 with a throwaway script (not in the repo) on a 1-D demo drive of 1000 frames, 10x10x10 integer lattice:

| Quantity | Measured max | Worst-case bound (computed) |
|---|---|---|
| Position error, x (cm) | 0.498 | 0.5 |
| Per-frame velocity error (cm/s) | 15.0 | 16 |
| Per-frame acceleration error (cm/s^2) | 452 | 512 |
| Acceleration error, 16-frame window fit (cm/s^2) | 6.8 | not computed |
| Segment boundary error (frames) | 4 | not computed |
| Segment duration error (frames) | 5 | not computed |
| End-speed error (cm/s) | 24.4 | not computed |

  The acceleration signal in the demo is at most 100 cm/s^2 in size, so per-frame acceleration (error 452) is unusable and the fit must use windows. The boundary, duration and end-speed numbers come from one demo drive; the hypothesis tests extend them (see Risks).

## Reliable Sources

### Authoritative (implementation truth)

| Rank | Name | Location | Covers |
|------|------|----------|--------|
| 1 | Assignment | docs/assignment.pdf | signal definition, 16 fps, cube example, deliverables, mocked source |
| 2 | Architecture context | docs/architecture_context.txt | agreed design, module 1 scope, decisions log |
| 1 | numpy docs | https://numpy.org/doc/ | arrays, rint, int32, linspace |
| 1 | PyArrow docs | https://arrow.apache.org/docs/python/ | tables, schemas, Parquet writing |
| 1 | pydantic docs | https://docs.pydantic.dev/ | models, validators, JSON dump |
| 1 | PyYAML docs | https://pyyaml.org/wiki/PyYAMLDocumentation | safe_load |
| 1 | Typer docs | https://typer.tiangolo.com/ | CLI commands |
| 1 | hypothesis docs | https://hypothesis.readthedocs.io/ | property tests, strategies |
| 1 | Python hashlib docs | https://docs.python.org/3.11/library/hashlib.html | SHA-256 |
| 1 | Streamlit docs | https://docs.streamlit.io/ | form widgets, data_editor, AppTest |
| 1 | pandas docs | https://pandas.pydata.org/docs/ | DataFrame for data_editor only |

### Context-Only (not implementation truth)

| Rank | Name | Location | Note |
|------|------|----------|------|
| 3 | Repo config | .claude/forgeloopp.md | commands, gates, conventions (its rules apply as repo config) |

## Requirements

- REQ-1: A `Scenario` model (YAML file) holds a start (x, y, z in cm and speed in cm/s, default 0), a seed, and a list of segments `move(duration_s, end_speed_cm_s)` and `stop(duration_s)`. Durations are in seconds and must be a whole number of frames. A move whose start and end speed are both 0 is rejected. [user decisions: segments, duration unit, start speed; architecture_context.txt MODULE 1]
- REQ-2: Configuration is YAML read by a Python loader that validates it. An environment variable can override the output folder. [user request; architecture_context.txt DECISIONS]
- REQ-3: Each frame is a 1 m cube, a 10x10x10 lattice of 1,000 points from 0 to 99 cm in fixed order, stored as int32 cm; 16 frames per second; `timestamp_ns = frame_id * 62,500,000`. [user decision: lattice; assignment.pdf 1.2]
- REQ-4: Motion is a straight line along x with signed speed. y and z are constant. There is no rotation and no yaw. Speed changes linearly inside a `move`. The ground truth holds per-frame x, velocity and acceleration, with no event labels. [user decisions: geometry, ground truth, drop events]
- REQ-5: A drive is written to `data/raw/<first 12 hex of scenario_hash>/` as `frames.parquet` (one row per frame, points as one binary column), `ground_truth.parquet` and `manifest.json`. [user decisions: layout, frames layout]
- REQ-6: `scenario_hash` is the SHA-256 of the canonical JSON of the validated scenario. The manifest holds the full 64-hex hash and the SHA-256 of each file's bytes on disk. A verifier detects any changed, missing or extra file. The same scenario gives byte-identical files. [user decisions: hash, file hashes]
- REQ-7: A 1 cm move on x shifts every point by exactly (1, 0, 0). [assignment.pdf 1.2]
- REQ-8: The simulator is checked in both directions. `infer_track` recovers per-frame position and velocity from the frames. `fit_segments` recovers the typed segment list from the track. `infer(generate(s))` matches `s` within the measured tolerances, tested with hypothesis. [user decision: inverse = both; user request]
- REQ-9: A fault-injection helper produces known faults for each ETL error code VL-001 to VL-006 and reports how many were injected per code. [architecture_context.txt MODULE 1; assignment.pdf 3.3a]
- REQ-10: The CLI offers `generate`, `verify` and `roundtrip`. Every command is run as `python -m vl_data <command>`. [forgeloopp.md Conventions]
- REQ-11: A Generate tab (start form, editable segment table, preview plot, Generate button) uses the same code path as the CLI. The plan delivers this tab only. [user decision: UI scope]
- REQ-12: Skeleton and tooling: `pyproject.toml`, src layout, ruff, mypy and pytest clean, coverage at least 80%, Python 3.11 or newer, runs on Windows PowerShell with no `make`. [forgeloopp.md Gates and Conventions]
- REQ-13: The generator reads only its scenario and config. The ground truth is written as a separate file. [architecture_context.txt DECISIONS]
- REQ-14: The generator is the assignment's mocked data source: it saves at least 1000 frames and every unit (cm, s, cm/s, cm/s^2) is stated in docstrings. [assignment.pdf 3.2a; forgeloopp.md Conventions]

## Non-Goals

- Yaw, rotation or turning of any kind; two-dimensional motion.
- Noise (only integer-cm rounding error exists).
- Event labels and clip candidates.
- The ETL, the lake, the use-case algorithm, the Clean and Analyze tabs, the three-tab shell.
- A REST API, streaming, real data, GPU work.

## Constraints

- Python 3.11 or newer (the machine has 3.12); Windows PowerShell; commands run as `python -m <module>` only; no `make`; type hints everywhere.
- Libraries: numpy, pyarrow, pydantic, PyYAML, Typer, Streamlit, Plotly, pandas (Streamlit data_editor only), pytest, pytest-cov, hypothesis, ruff, mypy.
- Coverage threshold 80; forbidden edit paths `warehouse/`, `data/`, `*.parquet`. Tests write only to `tmp_path`.
- Units: integer cm for points, seconds, cm/s, cm/s^2.
- Everything runs on CPU, on one machine.

## Success Criteria

- A demo drive of 1000 frames at 16 fps with timestamp spacing of exactly 62,500,000 ns and 1,000 points per frame.
- A 1 cm x move shifts every point by exactly (1, 0, 0).
- The same scenario run twice gives byte-identical files and the same scenario hash.
- `verify` reports ok on a fresh drive and names the file after a byte is changed, a file is deleted, or an extra file is added.
- The round trip on the demo scenario finds the right segment types and count, with boundary error of at most 6 frames, duration error of at most 8 frames and end-speed error of at most 40 cm/s. Per-frame position error is at most 0.6 cm and per-frame velocity error at most 17 cm/s. Hypothesis finds no case outside these limits in its allowed search domain.
- Each fault type produces exactly the count the helper reports.
- The Generate tab, run by the Streamlit test runner, writes a drive with the same scenario hash and the same file hashes as the CLI.
- ruff and mypy are clean; coverage is at least 80%.

## Explicit User Decisions

| Decision | User Answer | Resolved in Stage |
|----------|-------------|-------------------|
| Skeleton inside this plan? | Inside this plan (Recommended) | 1 |
| Noise in scope? | Out for now (Recommended) | 1 |
| Ground-truth contents | Track plus event labels (Recommended); later replaced by "Drop events (Recommended)" | 1, 3 |
| UI scope | Generate tab only (Recommended) | 1 |
| New sources (pydantic, PyYAML, Typer, hypothesis, hashlib) | Approve all five | 2 |
| Segment description | Typed segments; later replaced by "move + stop (Recommended)" | 3 |
| Point sampling | 10x10x10 lattice | 3 |
| Scenario hash input | Canonical JSON of the validated scenario | 3 |
| Inverse function output | Both, in two steps (Recommended) | 3 |
| File hash scope | The file bytes on disk (Recommended) | 3 |
| Drive folder name | data/raw/<first 12 hex of hash>/ (Recommended) | 3 |
| Event labels | Explicit label on each segment (Recommended); later "Drop events (Recommended)" | 3 |
| Starting direction of travel | Explicit field, default 0 rad (Recommended); later made moot by the one-axis decision, so there is no direction field | 3 |
| Duration unit | Seconds, validated to whole frames (Recommended) | 3 |
| Speed rule and speeding up | "change the scenario and use case we are building the data pipeline for- i only want to extract accerlation and velocity. the vehicle can drive forwards and backwards and stop. it can drive at different speeds" | 3 |
| Geometry | "i think that given that i have a cubic vehicle point clouds i cant simulate theyaw so this is what we are left with" (taken as straight-line motion along x with signed speed, no turning) | 3 |
| Segment types | move + stop (Recommended) | 3 |
| frames.parquet layout | One row per frame, points as one binary column (Recommended) | 3 |
| Use-case scope | Velocity and acceleration only; clip candidates and events dropped | 3 |

## Unresolved Decisions

NONE

## Design

The generator is a pure pipeline from a validated `Scenario` to a `Drive` (two Arrow tables plus the scenario and its hash), then a writer. Each step is a small function, so each can be tested alone.

1. **Scenario** (REQ-1). A pydantic model validates a YAML file. Segment durations must be multiples of 1/16 s. Floats are stored as floats, so `600` and `600.0` give the same hash.
2. **Speed per frame** (REQ-4). A segment of `n` frames that goes from speed `a` to speed `b` gives frame speeds `a + (b - a) * i / n` for `i = 0..n-1`. The next segment starts at `b`. `stop` holds 0. This is forward Euler, so position is `x[k+1] = x[k] + v[k] / 16`. The end speed `b` is reached on the first frame of the next segment (the last segment never reaches its own end speed exactly). The ground truth is defined to match what a decoder sees: `velocity[k] = (x[k+1] - x[k]) * 16` and `acceleration[k] = (v[k+1] - v[k]) * 16`, with the last frame repeating the previous value.
3. **Points** (REQ-3, REQ-7). The lattice axis values are `rint(linspace(0, 99, 10))`, in x-major order. A frame's points are `rint(lattice + [x, y, z])` as int32. y and z are constant. The 8 cube corners are lattice points, so the ETL can pick them by index.
4. **Files** (REQ-5, REQ-6). `frames.parquet` has one row per frame with `frame_id`, `timestamp_ns` and a binary `points` column (1000 x 3 int32, little-endian, row-major). `ground_truth.parquet` has per-frame x, velocity and acceleration. The manifest stores the schema version, scenario hash, seed, fps, frame count, points per frame, the scenario itself, and the SHA-256 and size of each file. Files are written with fixed Parquet options, so the same scenario gives the same bytes on the same pyarrow version.
5. **Hashing** (REQ-6). `canonical_json` sorts keys, uses no spaces, and writes floats with Python's shortest round-trip form. `scenario_hash = sha256(canonical_json(scenario))`. The folder is the first 12 hex characters; the manifest keeps all 64.
6. **Safe writes**. A drive is written to a temporary folder next to the target and then swapped in, so a crash never leaves a half-written drive. Writing is not safe for concurrent writers (stated as a known issue).
7. **Inverse** (REQ-8). `infer_track` takes the centroid x of each frame (mean of the points minus the lattice centre) and the forward difference of it. `fit_segments` runs an optimal-partition (dynamic programming) fit of piecewise-linear lines to the velocity series. A piece whose fitted speed stays under a stop threshold is a `stop`; any other piece is a `move` with the fitted end speed. `infer_scenario` rebuilds a `Scenario` and `compare_scenarios` reports the differences against the tolerances. This was prototyped on the demo drive and found all 7 boundaries within 4 frames.
8. **Faults** (REQ-9). Six small fault classes (one per code) change a `Drive`'s frames table and return counts per code. The manifest is built after injection, so a faulty drive still verifies.
9. **CLI and UI** (REQ-10, REQ-11). Both call `generate_to_disk(scenario, config, out_root)`, which generates, writes, and verifies. The UI renders in a function and a tiny runner file. The three-tab shell is left to the BI module.

Design choices are backed by: REQ-1 to REQ-14 above, the user decisions table, and the sources in the Reliable Sources table (numpy for arrays and `rint`, PyArrow for Parquet, hashlib for SHA-256, pydantic for validation).

### New Code And How It Connects

- `core/config.py`: `Config` and `load_config`, reads `config/default.yaml`, validates with pydantic, applies the `VL_RAW_DIR` override.
- `core/hashing.py`: `canonical_json`, `sha256_hex`, `file_sha256`, `scenario_hash`. No imports from the generator.
- `core/schemas.py`: `FRAMES_SCHEMA`, `GROUND_TRUTH_SCHEMA`, and `encode_points` / `decode_points` for the binary column. The ETL will import these.
- `core/manifest.py`: `Manifest`, `build_manifest`, `write_manifest`, `read_manifest`, `verify_drive`.
- `generator/models.py`: `Scenario`, `Start`, `MoveSegment`, `StopSegment`, `load_scenario`.
- `generator/body.py`: `cube_lattice`.
- `generator/motion.py`: `frame_count`, `speed_profile`, `integrate_position`, `ground_truth_arrays`.
- `generator/drive.py`: `Drive`, `generate`, `drive_dir_name` (builds a drive in memory, no file access).
- `generator/writer.py`: `write_drive`, `generate_to_disk` (writes the drive folder and verifies it).
- `generator/inverse.py`: `Track`, `infer_track`, `fit_segments`, `infer_scenario`, `compare_scenarios`, `RoundTripReport`.
- `generator/faults.py`: six fault classes and `inject_faults`.
- `cli.py` (root): the Typer app, `--version`, and registration of each module's commands (only the generator's for now).
- `generator/cli.py`: the generator's three commands and `register(app)`, which the root `cli.py` calls.
- `generator/ui/generate_tab.py` and `generator/ui/generate_app.py`: `render_generate_tab` and a standalone runner. The BI module will import the tab later.
- Existing project code it talks to: none yet (the repo is empty). The generator depends on `core/` only. `core/` imports nothing from the generator.

### Why This Design Stays Simple

- One direction of dependency: `core` is imported by `generator` and the root `cli`, never the reverse. The generator is one self-contained folder (code, CLI commands, UI tab, demo scenario), so later modules sit beside it.
- Pure functions on numpy arrays for the physics, so the 1 cm shift and the ground truth are unit-testable without files.
- One code path (`generate_to_disk`) for the CLI and the UI, so there is nothing to keep in sync.
- The inverse reuses the same `Scenario` model as the forward direction, so a round trip compares like with like.
- Genuinely new and necessarily new: the piecewise-linear fit (it is the inverse the user asked for) and the fault classes (the ETL needs known faults to test against).
- No noise, no events, no rotation, and no extra UI tabs were added.

## Files / Modules

| Action | Path | Purpose | Change |
|--------|------|---------|--------|
| create | pyproject.toml | build and tool config | src layout, dependencies, dev dependencies, ruff, mypy, pytest and coverage settings, `requires-python >=3.11` |
| create | src/vl_data/__init__.py | package | defines `__version__` |
| create | src/vl_data/__main__.py | `python -m vl_data` entry | calls the Typer app |
| create | src/vl_data/cli.py | CLI | `generate`, `verify`, `roundtrip`, `--version` |
| create | src/vl_data/core/__init__.py | package | empty package marker |
| create | src/vl_data/core/config.py | config loader | `Config` models and `load_config` |
| create | src/vl_data/core/hashing.py | hashing | canonical JSON and SHA-256 helpers |
| create | src/vl_data/core/schemas.py | Arrow schemas | frames and ground-truth schemas, point encoding |
| create | src/vl_data/core/manifest.py | manifest | build, write, read and verify a drive's manifest |
| create | src/vl_data/generator/__init__.py | package | exports public names |
| create | src/vl_data/generator/models.py | scenario model | pydantic scenario, segments, validators, YAML loader |
| create | src/vl_data/generator/body.py | body | cube lattice |
| create | src/vl_data/generator/motion.py | physics | speed profile, integration, ground-truth arrays |
| create | src/vl_data/generator/drive.py | drive builder | `Drive`, `generate`, `drive_dir_name` (in memory) |
| create | src/vl_data/generator/writer.py | drive writer | `write_drive`, `generate_to_disk` |
| create | src/vl_data/generator/cli.py | generator commands | `generate`, `verify`, `roundtrip` and `register(app)` |
| create | src/vl_data/generator/inverse.py | inverse | track, segment fit, scenario inference, comparison |
| create | src/vl_data/generator/faults.py | fault injection | fault classes and `inject_faults` |
| create | config/default.yaml | config | fps, body, output folder, tolerances, fit parameters |
| create | src/vl_data/generator/scenarios/demo.yaml | demo scenario | the 1000-frame demo drive used in the measurements |
| create | src/vl_data/generator/ui/__init__.py | package | empty package marker |
| create | src/vl_data/generator/ui/generate_tab.py | UI | `render_generate_tab`, form-to-scenario helper, preview plot |
| create | src/vl_data/generator/ui/generate_app.py | UI runner | loads config and calls `render_generate_tab` |
| create | tests/core/test_config.py | tests | loader and override |
| create | tests/core/test_hashing.py | tests | canonical JSON, golden hash |
| create | tests/core/test_manifest.py | tests | build, verify, tamper cases |
| create | tests/core/test_schemas.py | tests | point encoding round trip |
| create | tests/generator/test_models.py | tests | scenario validation |
| create | tests/generator/test_body.py | tests | lattice and 1 cm shift |
| create | tests/generator/test_motion.py | tests | speed, position and ground-truth consistency |
| create | tests/generator/test_generator.py | tests | drive files, determinism, folder name |
| create | tests/generator/test_inverse.py | tests | track, fit, hypothesis round trip |
| create | tests/generator/test_faults.py | tests | one test per code |
| create | tests/test_cli.py | tests | root CLI: version and help |
| create | tests/generator/test_cli.py | tests | generator commands |
| create | tests/generator/test_ui.py | tests | Streamlit app test, CLI parity |
| create | tests/conftest.py | fixtures | shared config and demo scenario fixtures |

## Classes / Functions / Signatures

```python
# file: src/vl_data/core/config.py
from pathlib import Path
from pydantic import BaseModel

class BodyConfig(BaseModel):
    size_cm: int = 100                 # cube edge, cm
    points_per_axis: int = 10          # lattice points per axis

class ToleranceConfig(BaseModel):
    position_cm: float = 0.6
    velocity_cm_s: float = 17.0
    boundary_frames: int = 6
    duration_frames: int = 8
    end_speed_cm_s: float = 40.0

class FitConfig(BaseModel):
    min_segment_frames: int = 8
    noise_sigma_cm_s: float = 12.0
    penalty: float = 40.0
    stop_speed_cm_s: float = 30.0

class Config(BaseModel):
    fps: int = 16
    raw_dir: Path = Path("data/raw")
    body: BodyConfig = BodyConfig()
    tolerances: ToleranceConfig = ToleranceConfig()
    fit: FitConfig = FitConfig()

    @property
    def frame_interval_ns(self) -> int:
        """Frame spacing in ns (62,500,000 at 16 fps)."""
        raise NotImplementedError

def load_config(path: Path | None = None) -> Config:
    """Load and validate config YAML; VL_RAW_DIR overrides raw_dir."""
    raise NotImplementedError
```

```python
# file: src/vl_data/core/hashing.py
from pathlib import Path
from typing import Any
from pydantic import BaseModel

def canonical_json(obj: Any) -> str:
    """Sorted-key, no-space JSON with shortest round-trip floats."""
    raise NotImplementedError

def sha256_hex(data: bytes) -> str:
    """SHA-256 of bytes, 64 lowercase hex characters."""
    raise NotImplementedError

def file_sha256(path: Path) -> str:
    """SHA-256 of a file's bytes on disk."""
    raise NotImplementedError

def scenario_hash(scenario: BaseModel) -> str:
    """SHA-256 of the canonical JSON of a validated scenario."""
    raise NotImplementedError
```

```python
# file: src/vl_data/core/schemas.py
import numpy as np
import pyarrow as pa
from numpy.typing import NDArray

SCHEMA_VERSION: int = 1
FRAMES_SCHEMA: pa.Schema          # frame_id int64, timestamp_ns int64, points binary
GROUND_TRUTH_SCHEMA: pa.Schema    # frame_id int64, timestamp_ns int64, x_cm float64,
                                  # velocity_cm_s float64, acceleration_cm_s2 float64

def encode_points(points: NDArray[np.int32]) -> bytes:
    """Encode an (n, 3) int32 array (cm) as little-endian row-major bytes."""
    raise NotImplementedError

def decode_points(buf: bytes) -> NDArray[np.int32]:
    """Decode bytes from encode_points back to an (n, 3) int32 array (cm)."""
    raise NotImplementedError
```

```python
# file: src/vl_data/core/manifest.py
from dataclasses import dataclass
from pathlib import Path
from pydantic import BaseModel

class FileEntry(BaseModel):
    sha256: str
    bytes: int

class Manifest(BaseModel):
    schema_version: int
    scenario_hash: str
    seed: int
    fps: int
    frame_count: int
    points_per_frame: int
    scenario: dict[str, object]
    files: dict[str, FileEntry]

@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    problems: list[str]

def build_manifest(drive_dir: Path, scenario_hash: str, scenario: dict[str, object],
                   fps: int, frame_count: int, points_per_frame: int) -> Manifest:
    """Hash every data file in drive_dir (bytes on disk) and build the manifest."""
    raise NotImplementedError

def write_manifest(manifest: Manifest, drive_dir: Path) -> Path:
    """Write manifest.json (sorted keys) and return its path."""
    raise NotImplementedError

def read_manifest(drive_dir: Path) -> Manifest:
    """Read and validate manifest.json."""
    raise NotImplementedError

def verify_drive(drive_dir: Path) -> VerifyResult:
    """Check every listed file's hash and size; report changed, missing and extra files."""
    raise NotImplementedError
```

```python
# file: src/vl_data/generator/models.py
from pathlib import Path
from typing import Annotated, Literal
from pydantic import BaseModel, Field

class Start(BaseModel):
    x_cm: float = 0.0
    y_cm: float = 0.0
    z_cm: float = 0.0
    speed_cm_s: float = 0.0            # signed, along +x

class MoveSegment(BaseModel):
    type: Literal["move"] = "move"
    duration_s: float                  # seconds, multiple of 1/16
    end_speed_cm_s: float              # signed, cm/s

class StopSegment(BaseModel):
    type: Literal["stop"] = "stop"
    duration_s: float                  # seconds, multiple of 1/16

Segment = Annotated[MoveSegment | StopSegment, Field(discriminator="type")]

class Scenario(BaseModel):
    seed: int = 0                      # reserved for future noise; part of the hash
    start: Start = Start()
    segments: list[Segment]

def load_scenario(path: Path) -> Scenario:
    """Read and validate a scenario YAML file."""
    raise NotImplementedError
```

```python
# file: src/vl_data/generator/body.py
import numpy as np
from numpy.typing import NDArray

def cube_lattice(size_cm: int, points_per_axis: int) -> NDArray[np.int32]:
    """Return the (points_per_axis**3, 3) cube lattice in cm, x-major, from 0 to size_cm - 1."""
    raise NotImplementedError
```

```python
# file: src/vl_data/generator/motion.py
import numpy as np
from numpy.typing import NDArray
from vl_data.generator.models import Scenario

def frame_count(scenario: Scenario, fps: int) -> int:
    """Total frames: the sum of segment durations in frames."""
    raise NotImplementedError

def speed_profile(scenario: Scenario, fps: int) -> NDArray[np.float64]:
    """Signed speed in cm/s for every frame (linear ramp inside each segment)."""
    raise NotImplementedError

def integrate_position(start_x_cm: float, speed_cm_s: NDArray[np.float64], fps: int) -> NDArray[np.float64]:
    """Forward-Euler x position in cm: x[k+1] = x[k] + v[k] / fps."""
    raise NotImplementedError

def ground_truth_arrays(x_cm: NDArray[np.float64], speed_cm_s: NDArray[np.float64], fps: int
                        ) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Return (velocity_cm_s, acceleration_cm_s2) per frame, defined by forward differences."""
    raise NotImplementedError
```

```python
# file: src/vl_data/generator/drive.py
from dataclasses import dataclass
import pyarrow as pa
from vl_data.core.config import Config
from vl_data.generator.models import Scenario

@dataclass(frozen=True)
class Drive:
    scenario: Scenario
    scenario_hash: str
    frames: pa.Table
    ground_truth: pa.Table

def drive_dir_name(scenario_hash: str) -> str:
    """First 12 hex characters of the scenario hash."""
    raise NotImplementedError

def generate(scenario: Scenario, config: Config) -> Drive:
    """Build frames and ground truth for a scenario; no file access."""
    raise NotImplementedError
```

```python
# file: src/vl_data/generator/writer.py
from pathlib import Path
from vl_data.core.config import Config
from vl_data.generator.drive import Drive
from vl_data.generator.models import Scenario

def write_drive(drive: Drive, out_root: Path, config: Config) -> Path:
    """Write frames, ground truth and manifest to <out_root>/<hash12>/ via a temp folder swap."""
    raise NotImplementedError

def generate_to_disk(scenario: Scenario, config: Config, out_root: Path) -> Path:
    """generate + write_drive + verify_drive; raise if verification fails."""
    raise NotImplementedError
```

```python
# file: src/vl_data/generator/inverse.py
from dataclasses import dataclass
import numpy as np
import pyarrow as pa
from numpy.typing import NDArray
from vl_data.core.config import Config, FitConfig, ToleranceConfig
from vl_data.generator.models import Scenario, Segment

@dataclass(frozen=True)
class Track:
    x_cm: NDArray[np.float64]            # centroid x per frame, cm
    velocity_cm_s: NDArray[np.float64]   # forward difference, cm/s, length n - 1

@dataclass(frozen=True)
class RoundTripReport:
    ok: bool
    problems: list[str]
    max_boundary_error_frames: int
    max_duration_error_frames: int
    max_end_speed_error_cm_s: float

def infer_track(frames: pa.Table, config: Config) -> Track:
    """Recover centroid x and velocity from the frames alone (no ground truth)."""
    raise NotImplementedError

def fit_segments(track: Track, fit: FitConfig, fps: int) -> list[Segment]:
    """Fit piecewise-linear speed pieces and return typed move/stop segments."""
    raise NotImplementedError

def infer_scenario(frames: pa.Table, config: Config) -> Scenario:
    """infer_track then fit_segments, rebuilt as a Scenario (seed 0)."""
    raise NotImplementedError

def compare_scenarios(expected: Scenario, inferred: Scenario, tol: ToleranceConfig,
                      fps: int) -> RoundTripReport:
    """Compare segment types, count, boundaries, durations and end speeds to tolerances."""
    raise NotImplementedError
```

```python
# file: src/vl_data/generator/faults.py
from dataclasses import dataclass
from typing import Sequence
from vl_data.generator.drive import Drive

@dataclass(frozen=True)
class DropFrame:            # VL-001 missing frame
    frame_id: int

@dataclass(frozen=True)
class ShiftTimestamp:       # VL-002 timestamp gap
    frame_id: int
    delta_ns: int

@dataclass(frozen=True)
class DuplicateFrame:       # VL-003 duplicate frame
    frame_id: int

@dataclass(frozen=True)
class CorruptPoints:        # VL-004 corrupt point cloud (a point is removed)
    frame_id: int

@dataclass(frozen=True)
class BreakSchema:          # VL-005 schema mismatch (the points column is renamed)
    pass

@dataclass(frozen=True)
class Teleport:             # VL-006 physically impossible motion
    frame_id: int
    dx_cm: int

Fault = DropFrame | ShiftTimestamp | DuplicateFrame | CorruptPoints | BreakSchema | Teleport

def inject_faults(drive: Drive, faults: Sequence[Fault]) -> tuple[Drive, dict[str, int]]:
    """Apply faults to the frames table; return the new drive and injected counts per code."""
    raise NotImplementedError
```

```python
# file: src/vl_data/cli.py   (root app)
import typer

app: typer.Typer                      # --version, and each module's commands via register(app)
```

```python
# file: src/vl_data/generator/cli.py
from pathlib import Path
import typer

def register(app: typer.Typer) -> None:
    """Add the generate, verify and roundtrip commands to the root app."""
    raise NotImplementedError

def generate(scenario: Path, out: Path | None = None) -> None:
    """Generate a drive from a scenario YAML and print its summary."""
    raise NotImplementedError

def verify(drive_dir: Path) -> None:
    """Verify a drive's manifest; exit non-zero on any problem."""
    raise NotImplementedError

def roundtrip(scenario: Path) -> None:
    """Generate in memory, infer the scenario back, compare to tolerances; exit non-zero on failure."""
    raise NotImplementedError
```

```python
# file: src/vl_data/generator/ui/generate_tab.py
from typing import Any
import plotly.graph_objects as go
from vl_data.core.config import Config
from vl_data.generator.models import Scenario

def rows_to_scenario(start: dict[str, float], rows: list[dict[str, Any]], seed: int) -> Scenario:
    """Turn the form's start values and segment-table rows into a validated Scenario."""
    raise NotImplementedError

def preview_figure(scenario: Scenario, config: Config) -> go.Figure:
    """Speed (cm/s) against time (s) for the scenario."""
    raise NotImplementedError

def render_generate_tab(config: Config) -> None:
    """Draw the form, preview and Generate button; Generate calls generate_to_disk."""
    raise NotImplementedError
```

## Task Breakdown

- TASK-1: Project skeleton and tooling
  - Depends on: none
  - Files: pyproject.toml, src/vl_data/__init__.py, src/vl_data/__main__.py, src/vl_data/cli.py, tests/conftest.py, tests/test_cli.py
  - REQs covered: REQ-10, REQ-12
  - Source: Typer docs, architecture_context.txt

- TASK-2: Core: config loader, hashing, schemas, manifest
  - Depends on: TASK-1
  - Files: src/vl_data/core/*, config/default.yaml, tests/core/*
  - REQs covered: REQ-2, REQ-5, REQ-6, REQ-12
  - Source: pydantic docs, PyYAML docs, hashlib docs, PyArrow docs

- TASK-3: Scenario model, body and motion
  - Depends on: TASK-2
  - Files: src/vl_data/generator/models.py, body.py, motion.py, src/vl_data/generator/scenarios/demo.yaml, tests/generator/test_models.py, test_body.py, test_motion.py
  - REQs covered: REQ-1, REQ-3, REQ-4, REQ-7, REQ-14
  - Source: assignment.pdf, numpy docs, pydantic docs

- TASK-4: Drive writer and `generate` and `verify` commands
  - Depends on: TASK-3
  - Files: src/vl_data/generator/drive.py, writer.py, cli.py, __init__.py, src/vl_data/cli.py, tests/generator/test_generator.py, tests/generator/test_cli.py
  - REQs covered: REQ-5, REQ-6, REQ-10, REQ-13, REQ-14
  - Source: PyArrow docs, hashlib docs

- TASK-5: Inverse functions and `roundtrip` command
  - Depends on: TASK-4
  - Files: src/vl_data/generator/inverse.py, src/vl_data/generator/cli.py, tests/generator/test_inverse.py, tests/generator/test_cli.py
  - REQs covered: REQ-8, REQ-10
  - Source: hypothesis docs, numpy docs, architecture_context.txt

- TASK-6: Fault-injection helper
  - Depends on: TASK-4
  - Files: src/vl_data/generator/faults.py, tests/generator/test_faults.py
  - REQs covered: REQ-9
  - Source: architecture_context.txt, PyArrow docs

- TASK-7: Generate tab
  - Depends on: TASK-4
  - Files: src/vl_data/generator/ui/__init__.py, generate_tab.py, generate_app.py, tests/generator/test_ui.py
  - REQs covered: REQ-11
  - Source: Streamlit docs, pandas docs

## Tests Per Task

| Task | Test file | Test name | What it verifies |
|------|-----------|-----------|-----------------|
| TASK-1 | tests/test_cli.py | test_version_prints | `--version` prints `vl_data 0.1.0` |
| TASK-1 | tests/test_cli.py | test_help_runs | `--help` exits 0 |
| TASK-2 | tests/core/test_config.py | test_defaults_load | default.yaml loads; fps 16 gives 62,500,000 ns |
| TASK-2 | tests/core/test_config.py | test_env_override_raw_dir | VL_RAW_DIR changes raw_dir |
| TASK-2 | tests/core/test_config.py | test_bad_config_rejected | an unknown key or a negative fps raises |
| TASK-2 | tests/core/test_hashing.py | test_canonical_json_sorted_compact | sorted keys, no spaces, 600 and 600.0 agree |
| TASK-2 | tests/core/test_hashing.py | test_scenario_hash_golden | a fixed scenario gives a fixed 64-hex hash |
| TASK-2 | tests/core/test_hashing.py | test_scenario_hash_ignores_key_order | key order in the source does not change the hash |
| TASK-2 | tests/core/test_schemas.py | test_points_roundtrip | encode then decode gives the same int32 array |
| TASK-2 | tests/core/test_manifest.py | test_verify_ok | a fresh drive verifies |
| TASK-2 | tests/core/test_manifest.py | test_verify_detects_changed_byte | a changed byte names the file |
| TASK-2 | tests/core/test_manifest.py | test_verify_detects_missing_and_extra | a deleted file and an added file are reported |
| TASK-3 | tests/generator/test_models.py | test_duration_must_be_whole_frames | 1.03 s is rejected, 1.0625 s is accepted |
| TASK-3 | tests/generator/test_models.py | test_move_with_zero_speeds_rejected | move from 0 to 0 is rejected |
| TASK-3 | tests/generator/test_models.py | test_demo_yaml_loads | generator/scenarios/demo.yaml gives 1000 frames |
| TASK-3 | tests/generator/test_body.py | test_lattice_shape_and_corners | 1,000 points, includes the 8 corners, x-major order |
| TASK-3 | tests/generator/test_body.py | test_one_cm_shift_is_exact | a 1 cm x move shifts every point by exactly (1,0,0) |
| TASK-3 | tests/generator/test_motion.py | test_speed_profile_ramp | speeds follow the linear ramp and reversal passes through 0 |
| TASK-3 | tests/generator/test_motion.py | test_ground_truth_matches_forward_difference | velocity and acceleration equal differences of x and v |
| TASK-4 | tests/generator/test_generator.py | test_frame_timestamps_exact | 1000 frames, spacing 62,500,000 ns, 1,000 points each |
| TASK-4 | tests/generator/test_generator.py | test_same_scenario_same_bytes | two runs give identical file hashes |
| TASK-4 | tests/generator/test_generator.py | test_folder_is_hash12 | folder name is the first 12 hex of the manifest hash |
| TASK-4 | tests/generator/test_generator.py | test_rewrite_replaces_folder | writing twice leaves one clean folder |
| TASK-4 | tests/generator/test_generator.py | test_generator_ignores_ground_truth_file | frames do not change if the ground-truth file is deleted |
| TASK-4 | tests/generator/test_cli.py | test_generate_prints_summary | the summary lines are printed and the manifest is verified |
| TASK-4 | tests/generator/test_cli.py | test_verify_fails_on_tamper | verify exits non-zero after a byte is changed |
| TASK-5 | tests/generator/test_inverse.py | test_infer_track_within_tolerance | centroid x error at most 0.6 cm, velocity error at most 17 cm/s |
| TASK-5 | tests/generator/test_inverse.py | test_fit_segments_demo | the demo gives 8 segments with the right types |
| TASK-5 | tests/generator/test_inverse.py | test_roundtrip_demo_within_tolerance | boundary, duration and end-speed errors within tolerance |
| TASK-5 | tests/generator/test_inverse.py | test_roundtrip_property | hypothesis scenarios round-trip within tolerance |
| TASK-5 | tests/generator/test_cli.py | test_roundtrip_command | the command prints "round trip OK" and exits 0 |
| TASK-6 | tests/generator/test_faults.py | test_vl001_drop_frame | one frame is missing and the count is 1 for VL-001 |
| TASK-6 | tests/generator/test_faults.py | test_vl002_shift_timestamp | one timestamp is moved and the count is 1 for VL-002 |
| TASK-6 | tests/generator/test_faults.py | test_vl003_duplicate_frame | a frame_id appears twice and the count is 1 for VL-003 |
| TASK-6 | tests/generator/test_faults.py | test_vl004_corrupt_points | a frame has 999 points and the count is 1 for VL-004 |
| TASK-6 | tests/generator/test_faults.py | test_vl005_break_schema | the points column is renamed and the count is 1 for VL-005 |
| TASK-6 | tests/generator/test_faults.py | test_vl006_teleport | a frame jumps by dx_cm and the count is 1 for VL-006 |
| TASK-6 | tests/generator/test_faults.py | test_faulty_drive_still_verifies | a faulty drive written to disk passes `verify_drive` |
| TASK-7 | tests/generator/test_ui.py | test_app_renders_without_exception | the app loads and shows the form and the preview |
| TASK-7 | tests/generator/test_ui.py | test_rows_to_scenario_validates | invalid rows give a clear error, not a crash |
| TASK-7 | tests/generator/test_ui.py | test_generate_button_matches_cli | the button writes a drive with the same hash as the CLI |

## Verification Commands

```powershell
python -m ruff check .
python -m mypy src
python -m pytest tests -q
python -m pytest --cov=vl_data --cov-report=term-missing --cov-fail-under=80 tests -q
```

## Risks / Residual Uncertainty

| Risk | Likelihood | Mitigation |
|------|-----------|-----------|
| The tolerances come from one demo drive; hypothesis may find a scenario that breaks the segment fit (small slope changes, very short segments) | med | The hypothesis search domain is limited to segments of at least 16 frames and a slope change of at least 50 cm/s^2 between neighbouring segments. If a case fails, widen the tolerance with a new measurement, or tighten the domain, and record it; never loosen silently |
| Parquet bytes can differ between pyarrow versions, so a file hash is only stable on one version | med | Fixed write options; the manifest records only hashes; the known issue is stated in the docs |
| The fit parameters (noise sigma 12, penalty 40, stop speed 30 cm/s) are starting values from the prototype, not tuned on many drives | med | The demo round trip and the hypothesis test must pass; values live in config |
| `scenario_hash` includes `seed`, which has no effect until noise exists | low | Kept on purpose (user decision to hash the whole scenario); documented as reserved |
| Machine has Python 3.12; the project says 3.11 | low | `requires-python >=3.11`, ruff target py311, mypy python_version 3.11 |
| The run proof writes `data/raw/<hash12>/`, a forbidden edit path | low | It is runtime output in an ignored folder, not a source edit; tests use `tmp_path` only |
| Atomic folder swap is not safe for concurrent writers | low | Accepted; stated as a known issue |
| The Streamlit test runner cannot edit the data_editor table | low | The test uses the default demo rows and the button; `rows_to_scenario` has its own unit tests |

## implement-loop Handoff

**Task file**: inline below (this plan). Run one task per implement-loop pass, TASK-1 first.

**Acceptance criteria for implement-loop**:
- TASK-1: `python -m vl_data --version` prints `vl_data 0.1.0`; ruff, mypy and pytest are clean on the empty skeleton.
- TASK-2: config loads and validates; `scenario_hash` is stable (golden hash); `verify_drive` reports changed, missing and extra files; point encoding round-trips.
- TASK-3: the scenario model enforces whole-frame durations and rejects zero-to-zero moves; the lattice has 1,000 points including the 8 corners; a 1 cm x move shifts every point by exactly (1,0,0); ground truth equals forward differences.
- TASK-4: the demo drive has 1000 frames, 62,500,000 ns spacing, 1,000 points per frame; two runs give identical bytes; the folder is the 12-hex prefix; `verify` catches tampering; the generator never reads the ground-truth file.
- TASK-5: the demo round trip passes within the measured tolerances; the hypothesis property test passes in its stated domain.
- TASK-6: each of the six faults produces the reported count; a faulty drive still verifies.
- TASK-7: the Generate tab renders and its button writes the same drive as the CLI.
- All tasks: ruff and mypy clean; coverage at least 80%; no files written outside `tmp_path` by tests; units in every docstring.
- Docs touched by each task (amendment agreed 2026-10-23). A task edits only its own sections; shared files are reconciled at merge. Every number and command added to a doc must come from a real run in that task.
  - TASK-1: ARCHITECTURE folder structure; RELEASE_NOTES `--version` usage line.
  - TASK-2: ARCHITECTURE data model (manifest, core modules); ASSUMPTIONS rows for hashing and manifest.
  - TASK-3: ARCHITECTURE motion model; ASSUMPTIONS A4 to A9 confirmed or changed.
  - TASK-4: ARCHITECTURE data flow and files on disk; RELEASE_NOTES `generate` and `verify` usage with real output.
  - TASK-5: ARCHITECTURE two-way check, with the measured limits replaced by the ones the tests produce; ASSUMPTIONS A13 to A15.
  - TASK-6: ERROR_CODES example-fault column checked against the real fault classes; ARCHITECTURE fault section.
  - TASK-7: RELEASE_NOTES Generate-tab usage.
  - A final docs pass, outside this plan, runs every command in the docs from a fresh clone and checks every number against real output.

**Tests to write (TDD)**: every row of "Tests Per Task". Each test must fail first (RED), then pass (GREEN). Sources: assignment.pdf for the shift and 16 fps tests, architecture_context.txt for the contracts, library docs for API use.

**Verification commands**:
```powershell
python -m ruff check .
python -m mypy src
python -m pytest tests -q
python -m pytest --cov=vl_data --cov-report=term-missing --cov-fail-under=80 tests -q
```

**Run proof** (see `../implement-loop/run-proof.md`):
```text
RUN_PROOF (TASK-1, Project skeleton and tooling):
  device: cpu
  command: python -m vl_data --version
  cwd: repo root
  timeout: 00:02:00
  pass_criteria:
    - exit code 0
    - output contains the line "vl_data 0.1.0"
  exercises: package install, src layout, Typer entry point

RUN_PROOF (TASK-2, Core: config, hashing, schemas, manifest):
  device: cpu
  command: python -m pytest tests/core -v
  cwd: repo root
  timeout: 00:05:00
  pass_criteria:
    - exit code 0
    - output contains "tests/core/test_hashing.py::test_scenario_hash_golden PASSED"
    - output contains "tests/core/test_manifest.py::test_verify_detects_changed_byte PASSED"
    - output contains no "FAILED"
  exercises: stable hash, tamper detection, config loading

RUN_PROOF (TASK-3, Scenario model, body and motion):
  device: cpu
  command: python -m pytest tests/generator/test_models.py tests/generator/test_body.py tests/generator/test_motion.py -v
  cwd: repo root
  timeout: 00:05:00
  pass_criteria:
    - exit code 0
    - output contains "test_one_cm_shift_is_exact PASSED"
    - output contains "test_ground_truth_matches_forward_difference PASSED"
    - output contains "test_demo_yaml_loads PASSED"
  exercises: lattice, exact shift, ground-truth definition, demo scenario

RUN_PROOF (TASK-4, Drive writer, generate and verify):
  device: cpu
  command: python -m vl_data generate --scenario src/vl_data/generator/scenarios/demo.yaml --out data/raw
  cwd: repo root
  timeout: 00:05:00
  pass_criteria:
    - exit code 0
    - output contains "frames: 1000"
    - output contains "timestamp_spacing_ns: 62500000"
    - output contains "points_per_frame: 1000"
    - output contains a line "scenario_hash: " followed by 64 lowercase hex characters
    - output contains "manifest: verified"
    - the folder data/raw/<first 12 hex of that hash>/ contains frames.parquet, ground_truth.parquet and manifest.json
  exercises: generation, file layout, hashes, manifest verification

RUN_PROOF (TASK-5, Inverse functions and roundtrip):
  device: cpu
  command: python -m vl_data roundtrip --scenario src/vl_data/generator/scenarios/demo.yaml
  cwd: repo root
  timeout: 00:10:00
  pass_criteria:
    - exit code 0
    - output contains "round trip OK"
    - output contains "segments: expected 8, found 8"
    - output contains "max_boundary_error_frames:" with a value of 6 or less
    - output contains "max_duration_error_frames:" with a value of 8 or less
    - output contains "max_end_speed_error_cm_s:" with a value of 40 or less
  exercises: infer_track, fit_segments, compare_scenarios on the demo drive

RUN_PROOF (TASK-6, Fault-injection helper):
  device: cpu
  command: python -m pytest tests/generator/test_faults.py -v
  cwd: repo root
  timeout: 00:05:00
  pass_criteria:
    - exit code 0
    - output contains PASSED for test_vl001_drop_frame, test_vl002_shift_timestamp, test_vl003_duplicate_frame, test_vl004_corrupt_points, test_vl005_break_schema and test_vl006_teleport
    - output contains "test_faulty_drive_still_verifies PASSED"
  exercises: all six fault types and their counts

RUN_PROOF (TASK-7, Generate tab):
  device: cpu
  command: python -m pytest tests/generator/test_ui.py -v
  cwd: repo root
  timeout: 00:05:00
  pass_criteria:
    - exit code 0
    - output contains "test_app_renders_without_exception PASSED"
    - output contains "test_generate_button_matches_cli PASSED"
  exercises: the Streamlit form, the button, and parity with the CLI
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
