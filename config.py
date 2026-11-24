"""
The config file: where the data is, and which tables are facts and dimensions.

    {
      "source": {"type": "csv", "folder": "data/retail"},
      "facts":      ["fact_sales"],
      "dimensions": ["dim_customer", "dim_product", "dim_date"],
      "output": "semantic.json"
    }

SQL Server source (the password is read from an environment variable, never the file):

    "source": {"type": "sqlserver", "server": "sql01", "port": 1433, "database": "SalesDW",
               "user": "bi_reader", "password_env": "MSSQL_PASSWORD",
               "trust_server_certificate": false}

    or "trusted": true instead of user / password_env for Windows/Kerberos login.

Only the listed tables are scanned. Names may include a schema ("dbo.FactSales")
or not ("FactSales" matches any schema); matching is case-insensitive.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from db import CsvDatabase, Database, SqlServerDatabase, build_connection_string


class ConfigError(ValueError):
    pass


def load_config(path: str | Path) -> dict:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    try:
        cfg = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path} is not valid JSON: {e}")

    problems = []
    if not isinstance(cfg, dict):
        raise ConfigError("the config must be a JSON object")

    src = cfg.get("source")
    if not isinstance(src, dict) or src.get("type") not in ("csv", "sqlserver"):
        problems.append('"source" must be an object with "type": "csv" or "sqlserver"')
    elif src["type"] == "csv" and not src.get("folder"):
        problems.append('csv source needs "folder"')
    elif src["type"] == "sqlserver":
        for key in ("server", "database"):
            if not src.get(key):
                problems.append(f'sqlserver source needs "{key}"')
        if not src.get("trusted") and not (src.get("user") and src.get("password_env")):
            problems.append('sqlserver source needs "user" and "password_env" (or "trusted": true)')

    for key in ("facts", "dimensions"):
        v = cfg.get(key, [])
        if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
            problems.append(f'"{key}" must be a list of table names')
    if not cfg.get("facts"):
        problems.append('"facts" must list at least one fact table')

    if not problems:
        both = {f.lower() for f in cfg["facts"]} & {d.lower() for d in cfg.get("dimensions", [])}
        if both:
            problems.append(f"listed as both fact and dimension: {', '.join(sorted(both))}")

    if problems:
        raise ConfigError(f"{path}: " + "; ".join(problems))

    cfg.setdefault("dimensions", [])
    cfg.setdefault("output", "semantic.json")
    # relative paths are relative to the config file
    base = path.resolve().parent
    if src["type"] == "csv":
        src["folder"] = str((base / src["folder"]).resolve())
    cfg["output"] = str((base / cfg["output"]).resolve())
    return cfg


def connection_string(src: dict) -> str:
    password = None
    if not src.get("trusted"):
        var = src["password_env"]
        password = os.environ.get(var)
        if not password:
            raise ConfigError(f"environment variable {var} (the SQL Server password) is not set")
    return build_connection_string(
        src["server"], src["database"], src.get("user"), password,
        port=src.get("port"),
        driver=src.get("driver", "ODBC Driver 18 for SQL Server"),
        trusted=bool(src.get("trusted")),
        encrypt=src.get("encrypt", True),
        trust_server_certificate=bool(src.get("trust_server_certificate")),
    )


def open_source(cfg: dict) -> Database:
    """Open the database, limited to the tables listed in the config."""
    tables = cfg["facts"] + cfg["dimensions"]
    src = cfg["source"]
    if src["type"] == "csv":
        return CsvDatabase(src["folder"], tables=tables)
    return SqlServerDatabase(connection_string(src), tables=tables)


def roles(cfg: dict, db: Database) -> dict[str, str]:
    """{real table name: "fact" | "dimension"} using the names the database resolved."""
    from db import resolve_tables

    available = db.tables()
    out = {t: "fact" for t in resolve_tables(cfg["facts"], available)}
    out.update({t: "dimension" for t in resolve_tables(cfg["dimensions"], available)})
    return out
