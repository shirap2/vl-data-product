# Use-case errors

This file lists the named failures of the Module 3 use case (`motion`, `VLClient.sql`).
They have no VL code: the VL codes in `docs/ERROR_CODES.md` belong to the loader and describe a
bad frame or drive in the raw data. A use-case error describes a drive that cannot be processed.
No new code was added. This file is written by hand, because `docs/ERROR_CODES.md` is
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

## VLClient.sql (ValueError)

`VLClient.sql(query)` raises `ValueError` when the text cannot be parsed, holds more than one
statement, or is not a SELECT. A drive id that is empty or holds a character other than letters,
digits, `_` and `-` raises `ValueError` from the Lake in `keypoints`, `motion`, `lineage` and
`quarantine`; `motion` on the command line turns it into exit code 2. A SELECT can still call
`read_parquet` on any path the user can read (known issue, assumption M3-A10).

## Command exit codes

The `motion` command uses the same codes as `ingest`. It prints no traceback; an unexpected
failure prints only the exception type name.

| Code | `motion` |
|---|---|
| 0 | Motion computed and stored. |
| 1 | `MotionError`, or any other unexpected failure (type name shown). |
| 2 | Drive id with a character other than letters, digits, `_` and `-`; lake folder missing or not a folder; configuration cannot be loaded. |
| 3 | A file or Parquet read or write failed (reached through the lake read or write). |
