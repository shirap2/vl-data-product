# ForgeLoopP Repo Configuration

## Sources
- authoritative: docs/assignment.pdf (exercise requirements and signal description)
- authoritative: https://duckdb.org/docs/ (DuckDB)
- authoritative: https://pandas.pydata.org/docs/ (pandas)
- authoritative: https://numpy.org/doc/ (numpy)
- authoritative: https://arrow.apache.org/docs/python/ (PyArrow)
- authoritative: https://docs.streamlit.io/ (Streamlit)
- authoritative: docs/architecture_context.txt (agreed design and decisions log; read it in full)
- authoritative: https://docs.pydantic.dev/ (pydantic)
- authoritative: https://pyyaml.org/wiki/PyYAMLDocumentation (PyYAML)
- authoritative: https://typer.tiangolo.com/ (Typer)
- authoritative: https://hypothesis.readthedocs.io/ (hypothesis)
- authoritative: https://docs.python.org/3.11/library/hashlib.html (hashlib)
- context_only: other generated docs under docs/

## Commands
- full_suite: `python -m pytest tests/ -q`
- targeted_test: `python -m pytest {test_path} -q`
- coverage: `python -m pytest --cov={module} --cov-report=term-missing {test_path}`

## Gates
- coverage_threshold: 80
- test_file_patterns: `tests/`, `test_*.py`, `*_test.py`, `conftest.py`
- forbidden_paths: `warehouse/`, `data/`, `*.parquet`

## Conventions
- I am on Windows PowerShell. Use `python -m ...` commands only. No `make`, no
  bash-only commands in run proofs, no `python3`.
- Python 3.11, type hints everywhere, ruff and mypy clean.
- Small modules with clear interfaces. No code I cannot explain in an interview.
- Units: integer centimeters, seconds, radians. Say the unit in every docstring.
- The pipeline never reads the ground truth. Only the evaluation step does.
