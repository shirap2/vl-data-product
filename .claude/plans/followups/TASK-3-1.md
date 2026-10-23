# Follow-up TASK-3-1: upper limits on the body size in the config

Raised by the TASK-3 security review (deferred finding) and confirmed by the user on 2026-10-23 as
a follow-up, not a change inside TASK-3. Run it with `/forgeloopp:implement-loop` on this file.
The values below are proposals: confirm them with the user before running.

## CONTEXT

`BodyConfig` in `src/vl_data/core/config.py` has `size_cm: int = Field(default=100, gt=0)` and
`points_per_axis: int = Field(default=10, ge=2)` with no upper limit. `cube_lattice` allocates
`points_per_axis ** 3` rows, so a config with `points_per_axis: 10000` would try to allocate 10^12
rows. The value is only reachable through the local config file (the scenario YAML has no body
fields), so the risk is low today, but a config that the Generate tab may expose later should
fail with a clear error.

`src/vl_data/core/config.py` and `tests/core/test_config.py` were committed in TASK-2. The
Module 2 (ETL) session imports `Config` and `BodyConfig` by name: no name or signature may
change. `generator.body.cube_lattice` already rejects `points_per_axis > size_cm` (TASK-3
repair).

## WHAT_TO_DO

1. In `BodyConfig`, add upper limits: `size_cm` at most 10_000 and `points_per_axis` at most 50
   (50 ** 3 = 125,000 points per frame, 1.5 MB per frame). Keep the defaults (100 and 10).
2. Keep the validation messages clear (pydantic's `le` messages are fine).
3. Update `docs/ASSUMPTIONS.md` (a new row for the limits and why) and the core table in
   `docs/ARCHITECTURE.md`; then run `python tools/build_supporting_docs.py`.

## TESTS

- `tests/core/test_config.py::test_body_limits`: `points_per_axis: 50` and `size_cm: 10000` load;
  `points_per_axis: 51`, `points_per_axis: 10000` and `size_cm: 10001` raise a pydantic
  `ValidationError`; the defaults still load from `config/default.yaml`.
- `tests/core/test_config.py::test_defaults_load` (existing, must still pass: the defaults 100 and
  10 stay valid).
- All existing tests in `tests/core` and `tests/generator` still pass unchanged.

## VERIFY

```powershell
.venv\Scripts\python.exe -m pytest tests -q
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m mypy src
.venv\Scripts\python.exe -m pytest --cov=vl_data --cov-report=term-missing --cov-fail-under=80 tests -q
```

## RUN_PROOF

```text
RUN_PROOF (TASK-3-1, body size limits):
  device: cpu
  command: python -m pytest tests/core/test_config.py -v
  cwd: repo root
  timeout: 00:05:00
  pass_criteria:
    - exit code 0
    - output contains "tests/core/test_config.py::test_body_limits PASSED"
    - output contains "tests/core/test_config.py::test_defaults_load PASSED"
    - output contains no "FAILED"
  exercises: the new upper limits and the unchanged defaults
```

## CHECKLIST

- [ ] The user confirmed the limit values
- [ ] RED evidence captured
- [ ] GREEN evidence captured
- [ ] Coverage evidence reported
- [ ] Clean-code pass completed
- [ ] Reviewer gate passed
- [ ] Run-proof gate passed (run directly with the venv, per assumption A28)
- [ ] The Module 2 session told that `BodyConfig` now has limits (no names changed)
- [ ] User approved commit
