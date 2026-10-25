# Use-case errors

This file lists the named failures of the Module 3 use case (`motion`, `evaluate`, `VLClient.sql`).
They have no VL code: the VL codes in `docs/ERROR_CODES.md` belong to the loader and describe a
bad frame or drive in the raw data. A use-case error describes a drive that cannot be processed or
evaluated. No new code was added. This file is written by hand, because `docs/ERROR_CODES.md` is
generated from the rule registry by `python -m vl_data error-codes --write` and a test fails if
it differs from that output.

## MotionError (raised by `compute_motion`, exit code 1 of `motion`)

Nothing is written to the lake when it is raised; an old motion table stays as it was.

| When it happens | What to do |
|---|---|
| The drive has no lineage row (it was never ingested, or the id is wrong). | Run `python -m vl_data ingest <drive folder>`; check the id (the 12-hex folder name). |
| The lineage table has more than one row for the drive. | Re-ingest the drive; the ingest replaces the lineage partition. |
| The lineage status is not `ok` (the drive was rejected). | Read the quarantine table for the reason (`docs/ERROR_CODES.md`), fix the raw drive, ingest again. |
| The drive has no rows for the position keypoint (`position_keypoint` in `config/motion.yaml`, default `centroid`). | Check the name against the keypoints in `config/etl.yaml`. |
| The timestamps of the keypoint rows are not strictly increasing. | Re-ingest; a drive whose frames pass the loader should not hit this. |
| An x value is not finite. | Re-ingest the drive. |

## EvaluationError (raised by `evaluate_drive`, exit code 1 of `evaluate`)

`evaluate` reads the ground truth, so it needs the raw drive folder as well as the lake. Nothing
is stored. The message never holds a path or manifest text.

| When it happens | What to do |
|---|---|
| The drive folder fails the manifest check (a changed, missing or extra file, or an unreadable manifest). | Run `python -m vl_data verify <drive folder>` for the problem list; regenerate the drive. |
| The lineage row is missing or duplicated, its status is not `ok`, or its `scenario_hash` or `ground_truth_sha256` differs from the manifest (the lake holds a different drive version). | Ingest the drive again from the folder you are evaluating. |
| The motion table is missing or empty. | Run `python -m vl_data motion <drive id>` first. |
| The ground truth has a different number of rows than the manifest implies ("ground truth has N rows; the manifest implies M"). | Regenerate the drive; the truth file was changed or cut. |
| The motion row count differs from the manifest frame count (a motion table left from an earlier ingest). | Run `motion` again after any ingest. |
| The ground truth or the motion table has other frame ids than the other, or the ground truth has wrong columns. | Regenerate the drive and ingest, then run `motion`. |
| Reading a file fails (the exception type name is shown). | Check the folder and the lake are readable and not in use. |

A drive that is evaluated but misses a tolerance is not an error: `evaluate` prints `FAIL` and exits
1 as well. The two cases differ in the message: `result: FAIL` with the error table, or `error:`
on stderr.

## VLClient.sql (ValueError)

`VLClient.sql(query)` raises `ValueError` when the text cannot be parsed, holds more than one
statement, or is not a SELECT. A drive id that is empty or holds a character other than letters,
digits, `_` and `-` raises `ValueError` from the Lake in `keypoints`, `motion`, `lineage` and
`quarantine`; `motion` on the command line turns it into exit code 2. A SELECT can still call
`read_parquet` on any path the user can read (known issue, assumption M3-A10).

## Command exit codes

The `motion` and `evaluate` commands use the same codes as `ingest`. Neither command prints a
traceback; an unexpected failure prints only the exception type name.

| Code | `motion` | `evaluate` |
|---|---|---|
| 0 | Motion computed and stored. | The drive passed all three tolerances. |
| 1 | `MotionError`, or any other unexpected failure (type name shown). | A tolerance was exceeded, `EvaluationError`, or any other unexpected failure (type name shown). A file read failure inside the comparison is an `EvaluationError`. |
| 2 | Drive id with a character other than letters, digits, `_` and `-`; lake folder missing or not a folder; configuration cannot be loaded. | Drive folder is not a folder, or is or holds a symlink or junction; lake folder missing; configuration cannot be loaded. |
| 3 | A file or Parquet read or write failed (reached through the lake read or write). | Only if `evaluate_drive` itself fails unexpectedly with a file error. |
