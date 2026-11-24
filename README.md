# bi-tool: semantic layer generator + validator

Generates a BI semantic layer (stars, dimensions, measures, hierarchies) from database
metadata, then validates that queries built from it return correct numbers.

| File | What it does |
|---|---|
| `db.py` | **All database access.** `CsvDatabase` (CSV folder via DuckDB) and `SqlServerDatabase` (pyodbc) with the same methods |
| `semantic.py` | metadata → relationships → table roles → stars → `semantic.json` |
| `validate.py` | checks a `semantic.json` against the data (PASS / WARN / FAIL) |
| `make_sample.py` | example retail warehouse as CSVs |
| `config.py` | reads the config file, opens the source with only the listed tables |
| `main.py` | command line |

## Setup
```
python3 -m venv --without-pip .venv            # Ubuntu without python3-venv; else plain `python3 -m venv .venv`
python3 -m pip --python .venv/bin/python install -r requirements.txt
```

## Config file
Everything is driven by a JSON config: where the data is, and which tables are facts
and dimensions. **Only the listed tables are scanned**; their roles are taken as given.

```json
{
  "source": { "type": "csv", "folder": "data/retail" },
  "facts": ["fact_sales", "fact_returns"],
  "dimensions": ["dim_customer", "dim_product", "dim_category", "dim_date", "dim_store", "order_status"],
  "output": "semantic.json"
}
```
- Names can include a schema (`dbo.FactSales`) or not (`FactSales` matches any schema); case-insensitive.
- A typo is an error with a suggestion; a table in both lists is an error.
- Relative paths are relative to the config file.
- Notes in the output flag: a configured role the data disagrees with, a dimension no fact
  joins to, and a declared FK pointing at a table you did not list (a forgotten dimension?).

SQL Server: see `config.sqlserver.example.json`. The password is read from the env var named
in `password_env` (never stored in the file), or use `"trusted": true`. Needs the Microsoft
ODBC driver (`msodbcsql18`) and SQL Server 2016+.

## Run
```
.venv/bin/python make_sample.py                                 # data/retail (with planted problems)
.venv/bin/python main.py generate config.example.json           # -> semantic.json
.venv/bin/python main.py validate config.example.json -v        # exit 1 if anything FAILs
.venv/bin/python -m pytest -q
```

### Your own CSVs
One `<table>.csv` per table in a folder, plus an optional `constraints.json`
(`primary_keys`, `unique_keys`, `foreign_keys`, `descriptions`; see `make_sample.py`).
