# bi-tool: semantic layer generator + validator

Generates a BI semantic layer (stars, dimensions, measures, hierarchies) from database
metadata, then validates that queries built from it return correct numbers.

| File | What it does |
|---|---|
| `db.py` | **All database access.** `CsvDatabase` (CSV folder via DuckDB) and `SqlServerDatabase` (pyodbc) with the same methods |
| `semantic.py` | metadata → relationships → table roles → stars → `semantic.json` |
| `validate.py` | checks a `semantic.json` against the data (PASS / WARN / FAIL) |
| `make_sample.py` | example retail warehouse as CSVs |
| `main.py` | command line |

## Setup
```
python3 -m venv --without-pip .venv            # Ubuntu without python3-venv; else plain `python3 -m venv .venv`
python3 -m pip --python .venv/bin/python install -r requirements.txt
```

## Run
```
.venv/bin/python make_sample.py                        # data/retail (with planted problems)
.venv/bin/python main.py generate data/retail          # -> semantic.json
.venv/bin/python main.py validate data/retail -v       # exit 1 if anything FAILs
.venv/bin/python -m pytest -q
```

### Your own CSVs
One `<table>.csv` per table in a folder, plus an optional `constraints.json`
(`primary_keys`, `unique_keys`, `foreign_keys`, `descriptions`; see `make_sample.py`).

### SQL Server (later)
Needs the Microsoft ODBC driver (`msodbcsql18`). Then:
```
export MSSQL_SERVER=host MSSQL_DATABASE=SalesDW MSSQL_USER=... MSSQL_PASSWORD=...
.venv/bin/python main.py generate sqlserver
.venv/bin/python main.py validate sqlserver
```
