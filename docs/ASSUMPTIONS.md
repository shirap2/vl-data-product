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
| M2-D1 | Module 2 quarantines only the frame that fails a rule. The rest of the drive loads. | One bad frame does not cost the drive. |
| M2-D2 | A whole-drive failure (changed, missing or extra file, unreadable manifest, unsupported schema version, a renamed points column) writes one drive-level quarantine row, a lineage row with status `rejected`, and no keypoints. | The Clean tab can show the failure, and lineage records the attempt. |
| M2-D3 | The lake keeps 9 keypoints per frame (8 corners and the centroid) in a long table, one row per keypoint. | Generic shape, and the keypoint list is a YAML change. |
| M2-D4 | A quarantine row holds the drive, frame number, code and a short message. It does not copy the frame. | The raw frame stays in `data/raw/` and is hash-verified. |
| M2-D5 | Duplicate frames (VL-003): if the copies are identical, keep the first and quarantine the repeats. If they differ, quarantine all copies. | None of differing copies can be trusted. |
| M2-D6 | VL-001 writes one quarantine row per missing frame number. VL-002 compares each timestamp with `frame_id x 62,500,000 ns`. VL-004 checks all 1,000 points against the lattice. | One fault gives one row, matching the fault helper. |
| M2-D7 | The ETL is reached as `python -m vl_data ingest` and as the Python function `ingest_drive`. Both use the same code path. | Same pattern as the generator. |
| M2-D8 | The Lake class and the keypoints, quarantine and lineage schemas are built in the Module 2 plan, not in the Module 1 plan. | Module 1 has no lake code. |
| M2-D9 | The ETL reads frames in chunks and checks `schema_version` in the manifest. | Bounded memory, and a contract change is told apart from corrupt data. |
| M2-D10 | Every task updates its own doc sections during the task. Every number or command in a doc comes from a real run in that task. | Docs cannot drift from the code. |
| M2-D11 | Users query the lake through a Python SDK (`VLClient`) plus SQL. The Module 3 plan builds `VLClient`, starting with reads from the Lake class. | No plan before Module 3 needs it. No web service. |

## Assumptions I made (not yet confirmed by you)

| # | Assumption | Why I made it | If wrong |
|---|---|---|---|
| A1 | The (1,2,0) in the assignment's second-frame example is a typo for (1,1,0). | A 1 cm move on x of the point (0,1,0) gives (1,1,0). | Only the explanation changes; the code shifts every point by the same vector either way. |
| A2 | "API" means a Python SDK plus SQL, with no web service. A REST wrapper is listed as coming soon. Confirmed by you on 2026-10-23. | Simplest thing that gives users a query interface. | Add a small FastAPI module later. |
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
| A20 | The assignment has no due date. It has to be asked for. You told me on 2026-10-23 that it is due one week from that day, which is 2026-10-30. | The PDF does not state one. | None until submission. |
| A21 | The model name for the LLM usage doc is "Claude Sonnet 5.5, through Claude Code". | It is what this session reports. | Edit `docs/LLM_USAGE.md`. |
| M2-A1 | The VL-006 limit is 9,000 cm/s (about 324 km/h), which is 564.5 cm per frame including a 2 cm rounding margin. | You asked for the maximum a Tesla could do. You confirmed the top speed as 322 km/h (8,944 cm/s) on 2026-10-23, and I rounded it up to 9,000 cm/s. | Change `max_speed_cm_s` in `config/etl.yaml`. A higher value misses small teleports. |
| M2-A2 | The generator's `Teleport` fault moves only its own frame. The generator session confirmed this on 2026-10-23. One teleport makes two bad jumps (into the frame and out of it), but the rule compares with the last accepted frame, so only the teleported frame is flagged. | The fault is described as "a frame jumps by dx_cm". | If it shifts every later frame, the "compare with the last accepted frame" rule would quarantine all of them. The rule must change. |
| M2-A3 | VL-006 compares a frame's centroid with the last accepted frame, scaled by the frame number gap, using the straight-line distance in x, y and z. | A dropped or rejected frame must not make its neighbour look like a jump. | Neighbours of quarantined frames could be flagged wrongly. |
| M2-A4 | No ETL rule tests acceleration per frame. | Per-frame acceleration error (452 cm/s^2) is larger than the signal. | Good drives would be flagged. |
| M2-A5 | Whole-drive failures that are not VL codes (hash, manifest, schema version, folder name, a frame number outside 0 to frame_count-1) use the code `INTEGRITY`. | The six VL codes describe frames. | Another name for the same rows; only the document changes. |
| M2-A6 | The `ingest` command exits 0 when a drive was ingested, even if some frames were quarantined, and exits 1 when the drive was rejected. | Quarantine is normal operation. A rejected drive is a failure. | Scripts that expect non-zero on any quarantine need a flag. |
| M2-A7 | The lineage table holds no wall-clock time. | A re-ingest then leaves the lake byte-identical. | Adding `ingested_at` loses the byte-identical check. |
| M2-A8 | Keypoint x, y and z are stored as float64 centimetres. | The centroid has .5 values; corners are whole numbers and fit exactly. | Mixed types per keypoint would complicate the table. |
| M2-A9 | ETL settings live in `config/etl.yaml`, loaded by `etl/config.py`. The cube size and fps come from the core config and the manifest. | It avoids editing the Module 1 config file. | Merge the settings into `core/config.py` later. |
| M2-A10 | The ETL imports `generator.body.cube_lattice` to know the expected lattice. | One source of truth for the lattice. Imports still point one way. | A copy in the ETL could drift from the generator. |
| M2-A11 | A re-ingest swaps the keypoints, quarantine and lineage partitions one after another, not as one atomic step. | Plain Parquet folders have no multi-table commit. | A crash between swaps leaves mixed tables until the drive is ingested again. |
| M2-A12 | Two frames with the same frame number are identical copies only if their timestamp and points hash both match. | A different timestamp means a different record. | A looser rule would keep a repeat with a bad clock. |
| M2-A13 | Frames in the file may be out of order. The ETL orders them by frame number before VL-006. | A duplicate may be appended at the end. | A sorted-file assumption would give false jumps. |
| M2-A14 | The ETL never opens `ground_truth.parquet`. The lineage row stores the file's hash from the manifest so evaluation can tie to it. | Only the evaluation step may read ground truth. | Leakage into the ETL would make the checks meaningless. |

## Not decided yet

- Details of Modules 3 and 4 beyond what `docs/architecture_context.txt` says. Module 2 has a written plan (`.claude/plans/module2-loader-etl.md`) that is waiting for your approval.
