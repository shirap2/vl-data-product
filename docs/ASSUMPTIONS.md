# Assumptions and decisions log

Every assumption made while planning, who made it, and what changes if it is wrong.
Updated 2026-10-23. "User" means you decided it in the planning conversation.
"Assistant" means I assumed it and you have not confirmed it yet.

## Decisions you made

| # | Decision | Effect |
|---|---|---|
| D1 | The vehicle is a 1 m cube, so there is no yaw. Motion is a straight line along x with a signed speed (positive forward, negative backward, 0 stopped). | No turning, no direction-of-travel maths, no turn events. |
| D2 | The use case is extracting velocity and acceleration only. Trajectory is the base signal they come from. Events and clip candidates are dropped. | Ground truth has no event labels. |
| D3 | The motion profile uses two segment types: `move(duration_s, end_speed_cm_s)` and `stop(duration_s)`. The drive starts at rest unless the start block says otherwise. | One type covers cruise, braking, speeding up and reversing. |
| D4 | The 1,000 points are a 10x10x10 lattice from 0 to 99 cm, in fixed order. | The 8 cube corners are lattice points. |
| D5 | `scenario_hash` is the SHA-256 of the canonical JSON of the validated scenario. | Same scenario, same hash. |
| D6 | Manifest file hashes cover the bytes on disk. | Standard check, but tied to one pyarrow version. |
| D7 | A drive lives in `data/raw/<first 12 hex of the hash>/`. | Re-running a scenario overwrites its own folder. |
| D8 | `frames.parquet` has one row per frame, with the points in one binary column. | About 1,000 rows per drive, no per-point SQL. |
| D9 | The inverse function returns the per-frame track first, then a fitted segment list. | The simulator is checked in both directions. |
| D10 | Segment durations are written in seconds and must be a whole number of frames. | No float rounding in frame counts. |
| D11 | Config is YAML read by a Python loader. The skeleton is part of the Module 1 plan. Noise is out of scope. The UI scope for Module 1 is the Generate tab only. | |
| D12 | The generator lives in its own folder, `src/vl_data/generator/`. | Later modules sit beside it. |
| D13 | Docs are written in my own words and do not quote the assignment PDF. The PDF is never committed. | |

## Assumptions I made (not yet confirmed by you)

| # | Assumption | Why I made it | If wrong |
|---|---|---|---|
| A1 | The (1,2,0) in the assignment's second-frame example is a typo for (1,1,0). | A 1 cm move on x of the point (0,1,0) gives (1,1,0). | Only the explanation changes; the code shifts every point by the same vector either way. |
| A2 | "API" means a Python SDK plus SQL, with no web service. A REST wrapper is listed as coming soon. | Simplest thing that gives users a query interface. | Add a small FastAPI module later. |
| A3 | Your answer about the cube and yaw means straight-line motion along x only (not planar motion without rotation). | You said the cube cannot show yaw and "this is what we are left with". | A planar variant would need course maths again. |
| A4 | `start.x_cm, y_cm, z_cm` is the position of the lattice's left-back-bottom corner (the body's origin point). The centroid is 49.5 cm further on each axis. | The assignment places that corner at the origin. | Positions shift by a constant. |
| A5 | Position is integrated with forward Euler: `x[k+1] = x[k] + v[k] / 16`. Ground-truth velocity is `(x[k+1] - x[k]) * 16` and acceleration is `(v[k+1] - v[k]) * 16`. The last frame repeats the previous value. | The decoder will compute the same differences, so the only error left is rounding. | Ground truth would differ from what a decoder sees by a small integration error. |
| A6 | A segment's end speed is reached on the first frame of the next segment. The last segment never reaches its own end speed exactly. | Follows from a linear ramp over `n` frames. | A one-frame shift at segment ends. |
| A7 | Acceleration is the signed derivative of signed velocity. While driving backward, a negative acceleration means speeding up. | The simplest definition. | Users who expect "negative = braking" must use speed magnitude. |
| A8 | Points are rounded to whole centimetres with round-half-to-even (numpy `rint`) and stored as int32. | The assignment states cubic-centimetre resolution. | Slightly different rounding error. |
| A9 | Timestamps start at 0: `timestamp_ns = frame_id * 62,500,000`. | 16 fps gives exactly 62.5 ms. | None. |
| A10 | `seed` is part of the scenario and the hash, but it has no effect until noise exists. | You asked to hash the whole scenario. | A dead field in the meantime. |
| A11 | The sensor's point order is fixed, so the ETL can pick the 8 corners by index (0, 9, 90, 99, 900, 909, 990, 999). | The lattice is x-major. | A real sensor would need a different reduction. |
| A12 | The ETL keeps 9 keypoints per frame: the 8 corners and the centroid. | Fixed corner spacing doubles as a rigid-shape check. | Changes Module 2 only. |
| A13 | The measured tolerances (position 0.6 cm, velocity 17 cm/s, boundary 6 frames, duration 8 frames, end speed 40 cm/s) come from one demo drive and a throwaway script that is not in the repo. | Planning needed numbers before code exists. | The property tests may force wider limits; they will be recorded, not hidden. |
| A14 | The hypothesis search domain is limited to segments of at least 16 frames and a slope change of at least 50 cm/s^2 between neighbours. | Very short or very similar segments cannot be told apart under rounding noise. | Narrower domain means weaker round-trip evidence. |
| A15 | The fit parameters (noise sigma 12 cm/s, penalty 40, stop speed 30 cm/s, minimum piece 8 frames) are starting values from a prototype. | They found all 7 boundaries on the demo drive. | Re-tune in config. |
| A16 | Python 3.11 or newer. The machine runs 3.12. | `requires-python >= 3.11`. | None. |
| A17 | Parquet files are written with fixed options so the same scenario gives the same bytes on one pyarrow version. | Needed for a stable hash. | Hashes change when pyarrow changes. Stated as a known issue. |
| A18 | A fault count returned by the injector is the number of injected faults per code, not the number of rows the ETL will reject. | One teleport touches two frame-to-frame jumps but is one fault. | The ETL tests must map faults to rejected frames. |
| A19 | The Module 2 session works from the contract sent on 2026-10-23, and any contract change needs a plan amendment. | Two sessions share one design. | Rework in Module 2. |
| A20 | The assignment has no due date. It has to be asked for. | The PDF does not state one. | None until submission. |
| A21 | The model name for the LLM usage doc is "Claude Sonnet 5.5, through Claude Code". | It is what this session reports. | Edit `docs/LLM_USAGE.md`. |
| A22 | The package version lives only in `src/vl_data/__init__.py`. `pyproject.toml` reads it (hatch dynamic version). | One place, so the two cannot drift. | Change both by hand if the build backend changes. |
| A23 | The build backend is hatchling. | The packaging guide documents it and it needs little setup. | Switch to setuptools; only `pyproject.toml` changes. |
| A24 | Dependency lower bounds equal the major.minor versions installed on 2026-10-23 (numpy 2.5, pandas 3.1, pyarrow 26.0, pydantic 2.14, typer 0.27, streamlit 1.66, plotly 7.1, pytest 9.1, pytest-cov 7.1, hypothesis 6.170, ruff 0.17, mypy 2.4), except pyyaml and types-PyYAML, which are only bounded at 6.0. | Those are the versions the checks ran on. | An older library may work but is untested. |
| A25 | Running `python -m vl_data` with no arguments prints help and exits with code 2. | `no_args_is_help=True` on the root app; observed on 2026-10-23 and covered by `test_no_args_prints_help_and_exits_2`. Not in the plan. | The exit code depends on the click version (8.5.0 here); only the bare-invocation behaviour changes. |
| A26 | The skeleton has four tests, two more than the plan's two: `test_module_entry_point_runs` and `test_no_args_prints_help_and_exits_2`. | Without the first, `__main__.py` was 0% covered and the skeleton reached 76.9%, under the 80% bar. The second covers the `no_args_is_help` behaviour of A25. | Remove one only if its behaviour is covered another way. |
| A27 | Python 3.11 is the declared minimum but only 3.12.6 was tested. | Only 3.12 is installed on this machine. | A 3.11-only failure would appear on a 3.11 machine. |
| A28 | Run proofs are run directly with the project venv and recorded by hand in `.claude/run-proofs/` (log, exit code, a note that the record was hand-written). You accepted this on 2026-10-23. | The loop's helper `forgeloopp_run.py` imports `fcntl`, so it cannot run on Windows. | Use the helper if it gets Windows support. |
| A29 | `click>=8.2` is pinned in `pyproject.toml`. | `test_no_args_prints_help_and_exits_2` relies on click 8.2 or newer exiting with code 2. | Older click exits 0 for a bare invocation. |
| A30 | `canonical_json` writes a float with a whole value as an int (600.0 as 600, -0.0 as 0), sorts keys, uses no spaces, escapes non-ASCII, and rejects NaN and infinity. | Python's json module does not make 600 and 600.0 agree by itself, and the plan wants them to. | A scenario hash computed another way would differ; the golden test pins one example. |
| A31 | `scenario_hash` is the SHA-256 of the UTF-8 canonical JSON of `model_dump(mode="json")`. | Your decision D5, made concrete. | Changing it changes every drive folder name. |
| A32 | `load_config()` with no path reads `config/default.yaml` from the repo root (found from the source file's location). If that file is missing it uses built-in defaults. A path you pass that does not exist raises an error. A non-empty `VL_RAW_DIR` overrides `raw_dir`. | The plan did not say how the default file is found. | An installed wheel would silently use the defaults, which equal the file today. |
| A33 | `fps` must divide 1,000,000,000 ns evenly. | Keeps the frame spacing an exact whole number of ns. | 7 fps would be rejected. |
| A34 | The manifest hashes every regular file in the drive folder except `manifest.json`, keyed by file name. A subfolder is an error. | The plan said "every data file" without defining it. | A stray file in a drive folder is reported as extra. |
| A35 | `manifest.json` is written as sorted-key, 2-space-indent, ASCII JSON with a single LF at the end, written as bytes so Windows does not change newlines. | The same scenario should give identical manifest bytes on any platform. | A different layout changes the file bytes, not the hashes of the data files. |
| A36 | `verify_drive` reports a bad manifest, bad or missing listed files, unreadable files and unhashable scenarios as problems instead of raising. A pathological listed name (hundreds of characters) or a folder that cannot be listed can still raise an OSError from the filesystem. It returns ok=False with problem strings: changed (size or hash mismatch, or a file it cannot read), missing, extra, `invalid name` (a listed name that is empty, `.`, `..` or contains a path separator, which it never opens), a scenario that no longer matches its hash or cannot be hashed (for example NaN), or an unreadable manifest. The scenario hash check is not in the plan. | Callers get one result type; the extra check ties the stored scenario to its hash. | A caller that wants an exception must raise on `ok` itself. |
| A37 | `decode_points` returns a writable int32 copy and raises on a length that is not a multiple of 12 bytes. Checking the point count against `points_per_frame` is the caller's job (the ETL's VL-004). `encode_points` rejects floats and values outside int32. | Floats must never be cast silently. | A 999-point frame decodes fine and must be caught by the ETL. |
| A38 | `Manifest.seed` is read from the scenario's `seed` field: 0 if the field is absent, and an error if it is present but not a real integer (a bool or a float is rejected). `write_manifest` rejects NaN and infinity. | `build_manifest` has no seed argument in the frozen signature; a silent fallback would let the seed disagree with the scenario. | A scenario without a seed shows seed 0. |

## Not decided yet

- Whether the SDK-plus-SQL answer to "API" is acceptable (A2).
- Details of Modules 2 to 4 beyond what `docs/architecture_context.txt` says.
