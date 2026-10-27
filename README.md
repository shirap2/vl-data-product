# vl-data-product

A small data product for a vehicle-location (VL) signal: a mock data source, an ETL into a
queryable data lake, one implemented use case (ego-motion: velocity and acceleration) and a
Streamlit BI app. All data is synthetic.

```
Generator --> Loader / ETL --> Lake (Parquet, DuckDB SQL) --> Motion use case --> BI app
 (drive)     (6 error codes)   keypoints, quarantine,          VLClient SDK       Generate, Clean,
                                lineage, motion                                    Analyze, SQL
```

## Quick start (Windows PowerShell, Python 3.11+; tested on 3.12)

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -m vl_data generate --scenario src/vl_data/generator/scenarios/demo.yaml --out data/raw
.venv\Scripts\python.exe -m vl_data ingest data/raw/bf394b9bfb42 --lake-dir warehouse
.venv\Scripts\python.exe -m vl_data motion bf394b9bfb42 --lake-dir warehouse
.venv\Scripts\python.exe -m streamlit run src/vl_data/bi/app.py --server.address 127.0.0.1
```

The demo drive has 1,000 frames of 1,000,000 points each (full density). Generating it takes
about 1.5 minutes and about 290 MB on disk. Run the app from the repository root.

## Documentation

| Document | What it holds |
|---|---|
| [docs/USE_CASES.md](docs/USE_CASES.md) | Six use cases considered, and the one implemented |
| [docs/SUPPORTING_DOCUMENTATION.md](docs/SUPPORTING_DOCUMENTATION.md) | Error codes, architecture and design, release notes in one file |
| [docs/ERROR_CODES.md](docs/ERROR_CODES.md), [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/RELEASE_NOTES.md](docs/RELEASE_NOTES.md) | The three parts as separate files (release notes: description, usage, known issues, coming soon) |
| [docs/ENGINEERING_REPORT.pdf](docs/ENGINEERING_REPORT.pdf) | Illustrated report on all four modules |
| [docs/ASSUMPTIONS.md](docs/ASSUMPTIONS.md) | Every design decision and assumption |
| [docs/LLM_USAGE.md](docs/LLM_USAGE.md) | Model, approach and main prompts |

Technology choices and their alternatives are in the design-choices tables of the architecture
document. Timings in the docs were measured on one Windows machine. Some documents cite logs under
`.claude/run-proofs/`; those are local development logs that are not in this repository.

## Tests

```powershell
.venv\Scripts\python.exe -m pytest -q
```
