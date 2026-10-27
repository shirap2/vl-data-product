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

The deliverables are short and in `docs/`:

| Document | What it holds |
|---|---|
| [docs/USE_CASES.md](docs/USE_CASES.md) | Six use cases considered, and the one implemented |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Flow, data model, technology choices and alternatives |
| [docs/ERROR_CODES.md](docs/ERROR_CODES.md) | The possible error codes |
| [docs/RELEASE_NOTES.md](docs/RELEASE_NOTES.md) | Product description, usage, known issues, coming soon |
| [docs/LLM_USAGE.md](docs/LLM_USAGE.md) | Model, approach and main prompts |

More detail is in [docs/extras/](docs/extras/): the detailed architecture and release notes, every
design assumption, and an illustrated engineering report
([PDF](docs/extras/ENGINEERING_REPORT.pdf)). Timings were measured on one Windows machine. The
extras cite logs under `.claude/run-proofs/`; those are local development logs that are not in
this repository.

## Tests

```powershell
.venv\Scripts\python.exe -m pytest -q
```
