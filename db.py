"""
All database access for the BI tool lives in this file.

Two backends expose the same methods, so everything else (semantic.py,
validate.py) never knows which one it is talking to:

    CsvDatabase(folder)          a folder of CSV files, loaded into in-memory DuckDB.
                                 Optional constraints.json declares PKs / FKs.
    SqlServerDatabase(conn_str)  Microsoft SQL Server through pyodbc, metadata
                                 read from the sys.* catalog views.

Metadata methods
    tables()                     -> ["fact_sales", ...]   (SQL Server: "dbo.fact_sales")
                                    only the tables passed as `tables=` (from the config)
    columns(table)               -> [{"name", "type", "family", "nullable"}, ...]
    primary_key(table)           -> ["col", ...]  ([] if none)
    unique_keys(table)           -> [["col", ...], ...]   (includes the primary key)
    foreign_keys()               -> [{"name", "table", "columns", "ref_table", "ref_columns"}]
    foreign_keys_leaving_scope() -> declared FKs pointing at a table that was not scanned
    row_count(table)             -> int
    description(table, column=None) -> str | None

Profiling / validation methods (same SQL for both backends)
    duplicate_key_count(table, cols)            key values that occur more than once
    match_count(child, ccols, parent, pcols)    (non-null child rows, rows found in parent)
    totals(table, cols)                         (row count, [SUM(col), ...])
    joined_totals(fact, cols, joins)            same, after LEFT JOINing a dimension path
    query(sql, params)                          raw access
"""

from __future__ import annotations

import json
from pathlib import Path

# --------------------------------------------------------------------------- #
# Type families: every native type is mapped to one of these.
# --------------------------------------------------------------------------- #

_FAMILY_BY_TYPE = {
    "integer": {"tinyint", "smallint", "int", "integer", "bigint", "hugeint", "utinyint",
                "usmallint", "uinteger", "ubigint", "int1", "int2", "int4", "int8"},
    "decimal": {"decimal", "numeric", "money", "smallmoney", "float", "real", "double",
                "float4", "float8"},
    "string": {"char", "varchar", "text", "nchar", "nvarchar", "ntext", "string"},
    "date": {"date", "datetime", "datetime2", "smalldatetime", "datetimeoffset", "time",
             "timestamp", "timestamp with time zone", "timestamptz"},
    "boolean": {"bit", "boolean", "bool"},
}


def resolve_tables(wanted: list[str], available: list[str]) -> list[str]:
    """Map names from the config to real table names.

    'dbo.FactSales' must match exactly (case-insensitive); 'FactSales' matches
    any schema. Unknown or ambiguous names raise ValueError with suggestions.
    """
    import difflib

    by_full = {a.lower(): a for a in available}
    by_base: dict[str, list[str]] = {}
    for a in available:
        by_base.setdefault(a.split(".")[-1].lower(), []).append(a)

    out, problems = [], []
    for w in wanted:
        key = w.strip().lower()
        if key in by_full:
            hit = by_full[key]
        elif "." not in key and len(by_base.get(key, [])) == 1:
            hit = by_base[key][0]
        elif "." not in key and len(by_base.get(key, [])) > 1:
            problems.append(f"'{w}' is ambiguous, matches: {', '.join(by_base[key])} "
                            f"(add the schema)")
            continue
        else:
            candidates = list(dict.fromkeys(list(by_full) + list(by_base)))
            close = difflib.get_close_matches(key, candidates, n=3, cutoff=0.6)
            hint = f" (did you mean {', '.join(close)}?)" if close else ""
            problems.append(f"table '{w}' not found{hint}")
            continue
        if hit not in out:
            out.append(hit)
    if problems:
        raise ValueError("; ".join(problems))
    return out


def type_family(native_type: str) -> str:
    """'DECIMAL(12,2)' -> 'decimal', 'nvarchar' -> 'string', unknown -> 'other'."""
    t = native_type.lower().split("(")[0].strip()
    for family, names in _FAMILY_BY_TYPE.items():
        if t in names:
            return family
    return "other"


# --------------------------------------------------------------------------- #
# Shared SQL (works on DuckDB and T-SQL; only identifier quoting differs)
# --------------------------------------------------------------------------- #


class Database:
    """Base class: profiling SQL shared by both backends."""

    def quote(self, name: str) -> str:
        return '"' + name.replace('"', '""') + '"'

    def table_ref(self, table: str) -> str:
        return ".".join(self.quote(p) for p in table.split("."))

    def query(self, sql: str, params: tuple | list = ()) -> list[tuple]:
        raise NotImplementedError

    def close(self) -> None:
        pass

    def foreign_keys_leaving_scope(self) -> list[dict]:
        return []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- helpers ---------------------------------------------------------- #

    def _cols(self, cols: list[str], alias: str | None = None) -> str:
        prefix = f"{self.quote(alias)}." if alias else ""
        return ", ".join(prefix + self.quote(c) for c in cols)

    def _not_null(self, cols: list[str], alias: str | None = None) -> str:
        prefix = f"{self.quote(alias)}." if alias else ""
        return " AND ".join(f"{prefix}{self.quote(c)} IS NOT NULL" for c in cols)

    def _on(self, left_alias: str, left_cols: list[str], right_alias: str, right_cols: list[str]) -> str:
        return " AND ".join(
            f"{self.quote(left_alias)}.{self.quote(lc)} = {self.quote(right_alias)}.{self.quote(rc)}"
            for lc, rc in zip(left_cols, right_cols)
        )

    # ---- profiling -------------------------------------------------------- #

    def duplicate_key_count(self, table: str, cols: list[str]) -> int:
        """How many distinct key values occur more than once (0 = unique)."""
        sql = (
            f"SELECT COUNT(*) FROM (SELECT {self._cols(cols)} FROM {self.table_ref(table)} "
            f"WHERE {self._not_null(cols)} GROUP BY {self._cols(cols)} HAVING COUNT(*) > 1) d"
        )
        return int(self.query(sql)[0][0])

    def is_unique(self, table: str, cols: list[str]) -> bool:
        return self.duplicate_key_count(table, cols) == 0

    def match_count(self, child: str, child_cols: list[str],
                    parent: str, parent_cols: list[str]) -> tuple[int, int]:
        """(non-null child rows, child rows whose key exists in parent)."""
        p_distinct = f"(SELECT DISTINCT {self._cols(parent_cols)} FROM {self.table_ref(parent)})"
        first_parent = f"{self.quote('p')}.{self.quote(parent_cols[0])}"
        sql = (
            f"SELECT COUNT(*), COUNT({first_parent}) "
            f"FROM {self.table_ref(child)} {self.quote('c')} "
            f"LEFT JOIN {p_distinct} {self.quote('p')} ON {self._on('c', child_cols, 'p', parent_cols)} "
            f"WHERE {self._not_null(child_cols, 'c')}"
        )
        total, matched = self.query(sql)[0]
        return int(total), int(matched or 0)

    def totals(self, table: str, cols: list[str]) -> tuple[int, list[float | None]]:
        """(row count, [SUM(col) ...]) on a single table."""
        sums = "".join(f", SUM({self.quote(c)})" for c in cols)
        row = self.query(f"SELECT COUNT(*){sums} FROM {self.table_ref(table)}")[0]
        return int(row[0]), [None if v is None else float(v) for v in row[1:]]

    def joined_totals(self, fact: str, cols: list[str], joins: list[dict],
                      fact_alias: str = "f") -> tuple[int, list[float | None]]:
        """(row count, [SUM(fact.col) ...]) after LEFT JOINing `joins`.

        joins: [{"from": alias, "from_columns": [...], "to_table": t,
                 "to": alias, "to_columns": [...]}, ...]
        If a join's key is not unique, rows multiply and the totals grow (fan-out).
        """
        sums = "".join(f", SUM({self.quote(fact_alias)}.{self.quote(c)})" for c in cols)
        sql = f"SELECT COUNT(*){sums} FROM {self.table_ref(fact)} {self.quote(fact_alias)}"
        for j in joins:
            sql += (
                f" LEFT JOIN {self.table_ref(j['to_table'])} {self.quote(j['to'])} "
                f"ON {self._on(j['from'], j['from_columns'], j['to'], j['to_columns'])}"
            )
        row = self.query(sql)[0]
        return int(row[0]), [None if v is None else float(v) for v in row[1:]]


# --------------------------------------------------------------------------- #
# CSV backend (DuckDB)
# --------------------------------------------------------------------------- #


class CsvDatabase(Database):
    """A folder of CSV files acting as a database.

    <folder>/<table>.csv          one table per file (types inferred by DuckDB)
    <folder>/constraints.json     optional:
        {
          "primary_keys": {"dim_customer": ["customer_id"]},
          "unique_keys":  {"dim_product": [["product_code"]]},
          "foreign_keys": [{"table": "fact_sales", "columns": ["customer_id"],
                            "ref_table": "dim_customer", "ref_columns": ["customer_id"]}],
          "descriptions": {"fact_sales": "One row per order line",
                           "fact_sales.sales_amount": "Net amount after discount"}
        }
    """

    def __init__(self, folder: str | Path, tables: list[str] | None = None):
        """tables: only these are loaded (names as in the config); None = every CSV."""
        import duckdb

        self.folder = Path(folder)
        if not self.folder.is_dir():
            raise FileNotFoundError(f"CSV folder not found: {self.folder}")
        files = {p.stem: p for p in sorted(self.folder.glob("*.csv"))}
        names = resolve_tables(tables, list(files)) if tables is not None else list(files)

        self.con = duckdb.connect(":memory:")
        self._tables: list[str] = []
        for name in names:
            self.con.execute(
                f"CREATE TABLE {self.quote(name)} AS SELECT * FROM read_csv(?, header = true)",
                [str(files[name])],
            )
            self._tables.append(name)

        cfile = self.folder / "constraints.json"
        self.constraints = json.loads(cfile.read_text()) if cfile.exists() else {}

    def query(self, sql, params=()):
        return self.con.execute(sql, list(params)).fetchall()

    def close(self):
        self.con.close()

    # ---- metadata --------------------------------------------------------- #

    def tables(self):
        return list(self._tables)

    def columns(self, table):
        pk = set(self.primary_key(table))
        rows = self.query(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = ? ORDER BY ordinal_position",
            [table],
        )
        return [
            {"name": n, "type": t.lower(), "family": type_family(t), "nullable": n not in pk}
            for n, t in rows
        ]

    def primary_key(self, table):
        return list(self.constraints.get("primary_keys", {}).get(table, []))

    def unique_keys(self, table):
        keys = [list(k) for k in self.constraints.get("unique_keys", {}).get(table, [])]
        pk = self.primary_key(table)
        return ([pk] if pk else []) + keys

    def _all_foreign_keys(self):
        return [{
            "name": fk.get("name", f"fk_{fk['table']}_{'_'.join(fk['columns'])}"),
            "table": fk["table"],
            "columns": list(fk["columns"]),
            "ref_table": fk["ref_table"],
            "ref_columns": list(fk["ref_columns"]),
        } for fk in self.constraints.get("foreign_keys", [])]

    def foreign_keys(self):
        """Declared FKs with both ends among the loaded tables."""
        loaded = set(self._tables)
        return [fk for fk in self._all_foreign_keys()
                if fk["table"] in loaded and fk["ref_table"] in loaded]

    def foreign_keys_leaving_scope(self):
        """Declared FKs from a loaded table to a table that was not loaded."""
        loaded = set(self._tables)
        return [fk for fk in self._all_foreign_keys()
                if fk["table"] in loaded and fk["ref_table"] not in loaded]

    def row_count(self, table):
        return int(self.query(f"SELECT COUNT(*) FROM {self.table_ref(table)}")[0][0])

    def description(self, table, column=None):
        key = f"{table}.{column}" if column else table
        return self.constraints.get("descriptions", {}).get(key)


# --------------------------------------------------------------------------- #
# SQL Server backend (pyodbc)
# --------------------------------------------------------------------------- #


def _odbc_value(value: str) -> str:
    """Wrap an ODBC connection-string value in {} so ; = and spaces are safe."""
    return "{" + str(value).replace("}", "}}") + "}"


def build_connection_string(server: str, database: str, user: str | None = None,
                            password: str | None = None, *, port: int | None = None,
                            driver: str = "ODBC Driver 18 for SQL Server",
                            trusted: bool = False, encrypt: bool = True,
                            trust_server_certificate: bool = False) -> str:
    """ODBC connection string for SQL Server.

    trusted=True uses Windows/Kerberos auth (no user/password).
    trust_server_certificate=True only for dev servers with self-signed certs.
    """
    parts = {
        "Driver": driver,
        "Server": f"tcp:{server},{port}" if port else server,
        "Database": database,
        "Encrypt": "yes" if encrypt else "no",
        "TrustServerCertificate": "yes" if trust_server_certificate else "no",
        "ApplicationIntent": "ReadOnly",
        "APP": "bi-tool",
    }
    if trusted:
        parts["Trusted_Connection"] = "yes"
    else:
        if not user or not password:
            raise ValueError("user and password are required unless trusted=True")
        parts["UID"] = user
        parts["PWD"] = password
    return ";".join(f"{k}={_odbc_value(v)}" for k, v in parts.items())


class SqlServerDatabase(Database):
    """Microsoft SQL Server via pyodbc. Table names are 'schema.table'."""

    def __init__(self, connection_string: str, tables: list[str] | None = None,
                 login_timeout: int = 15, query_timeout: int = 300):
        """tables: only these are scanned (names as in the config); None = whole database."""
        import pyodbc

        self.con = pyodbc.connect(connection_string, timeout=login_timeout, autocommit=True)
        self.con.timeout = query_timeout
        self.wanted = tables
        self._meta: dict | None = None

    def quote(self, name):
        return "[" + name.replace("]", "]]") + "]"

    def query(self, sql, params=()):
        return [tuple(r) for r in self.con.cursor().execute(sql, *params).fetchall()]

    def close(self):
        self.con.close()

    # ---- metadata: read the catalog once (scoped tables only), answer from memory #

    def _load(self) -> dict:
        if self._meta is not None:
            return self._meta
        # 1. Object names only (cheap), then keep just the tables from the config.
        objects = {f"{schema}.{name}": (oid, otype.strip()) for oid, schema, name, otype in self.query("""
            SELECT o.object_id, s.name, o.name, o.type
            FROM sys.objects o JOIN sys.schemas s ON s.schema_id = o.schema_id
            WHERE o.type IN ('U', 'V') AND o.is_ms_shipped = 0
            ORDER BY s.name, o.name""")}
        names = resolve_tables(self.wanted, list(objects)) if self.wanted is not None else list(objects)
        tables: dict[str, dict] = {
            n: {"kind": "view" if objects[n][1] == "V" else "table", "columns": [], "pk": [],
                "unique": {}, "rows": None, "desc": None, "col_desc": {}}
            for n in names
        }

        # 2. Every other catalog query is limited to those objects (one parameter,
        #    so no 2100-parameter limit). STRING_SPLIT needs SQL Server 2016+.
        ids = ",".join(str(objects[n][0]) for n in names) or "0"
        in_scope = "IN (SELECT CAST(value AS int) FROM STRING_SPLIT(?, ','))"

        for schema, tname, cname, typ, max_len, prec, scale, nullable in self.query(f"""
            SELECT s.name, o.name, c.name, t.name, c.max_length, c.precision, c.scale, c.is_nullable
            FROM sys.columns c
            JOIN sys.objects o ON o.object_id = c.object_id
            JOIN sys.schemas s ON s.schema_id = o.schema_id
            JOIN sys.types t   ON t.user_type_id = c.user_type_id
            WHERE o.object_id {in_scope}
            ORDER BY s.name, o.name, c.column_id""", [ids]):
            t = tables.get(f"{schema}.{tname}")
            if t is None:
                continue
            if typ in ("varchar", "char", "varbinary", "binary"):
                native = f"{typ}({'max' if max_len == -1 else max_len})"
            elif typ in ("nvarchar", "nchar"):
                native = f"{typ}({'max' if max_len == -1 else max_len // 2})"
            elif typ in ("decimal", "numeric"):
                native = f"{typ}({prec},{scale})"
            else:
                native = typ
            t["columns"].append({"name": cname, "type": native, "family": type_family(typ),
                                 "nullable": bool(nullable)})

        # Unique, enabled, unfiltered indexes (PRIMARY KEY + UNIQUE constraints/indexes).
        for schema, tname, iname, is_pk, cname in self.query(f"""
            SELECT s.name, o.name, i.name, i.is_primary_key, c.name
            FROM sys.indexes i
            JOIN sys.objects o        ON o.object_id = i.object_id
            JOIN sys.schemas s        ON s.schema_id = o.schema_id
            JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
                                     AND ic.is_included_column = 0
            JOIN sys.columns c        ON c.object_id = ic.object_id AND c.column_id = ic.column_id
            WHERE i.is_unique = 1 AND i.is_disabled = 0 AND i.has_filter = 0
              AND o.object_id {in_scope}
            ORDER BY s.name, o.name, i.index_id, ic.key_ordinal""", [ids]):
            t = tables.get(f"{schema}.{tname}")
            if t is None:
                continue
            t["unique"].setdefault(iname, []).append(cname)
            if is_pk:
                t["pk"].append(cname)

        for schema, tname, rows in self.query(f"""
            SELECT s.name, o.name, SUM(p.rows)
            FROM sys.partitions p
            JOIN sys.objects o ON o.object_id = p.object_id
            JOIN sys.schemas s ON s.schema_id = o.schema_id
            WHERE p.index_id IN (0, 1) AND o.type = 'U' AND o.object_id {in_scope}
            GROUP BY s.name, o.name""", [ids]):
            if f"{schema}.{tname}" in tables:
                tables[f"{schema}.{tname}"]["rows"] = int(rows)

        for schema, tname, cname, value in self.query(f"""
            SELECT s.name, o.name, c.name, CAST(ep.value AS nvarchar(4000))
            FROM sys.extended_properties ep
            JOIN sys.objects o ON o.object_id = ep.major_id
            JOIN sys.schemas s ON s.schema_id = o.schema_id
            LEFT JOIN sys.columns c ON c.object_id = ep.major_id AND c.column_id = ep.minor_id
            WHERE ep.class = 1 AND ep.name = 'MS_Description' AND o.object_id {in_scope}""", [ids]):
            t = tables.get(f"{schema}.{tname}")
            if t is None:
                continue
            if cname is None:
                t["desc"] = value
            else:
                t["col_desc"][cname] = value

        fks: dict[str, dict] = {}
        # FKs whose child table is in scope; the parent may be outside it.
        for fk_name, ps, pt, pc, rs, rt, rc in self.query(f"""
            SELECT fk.name, ps.name, pt.name, pc.name, rs.name, rt.name, rc.name
            FROM sys.foreign_keys fk
            JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id
            JOIN sys.objects pt ON pt.object_id = fkc.parent_object_id
            JOIN sys.schemas ps ON ps.schema_id = pt.schema_id
            JOIN sys.columns pc ON pc.object_id = fkc.parent_object_id
                               AND pc.column_id = fkc.parent_column_id
            JOIN sys.objects rt ON rt.object_id = fkc.referenced_object_id
            JOIN sys.schemas rs ON rs.schema_id = rt.schema_id
            JOIN sys.columns rc ON rc.object_id = fkc.referenced_object_id
                               AND rc.column_id = fkc.referenced_column_id
            WHERE fk.is_disabled = 0 AND fkc.parent_object_id {in_scope}
            ORDER BY fk.name, fkc.constraint_column_id""", [ids]):
            key = f"{ps}.{fk_name}"
            fk = fks.setdefault(key, {"name": fk_name, "table": f"{ps}.{pt}", "columns": [],
                                      "ref_table": f"{rs}.{rt}", "ref_columns": []})
            fk["columns"].append(pc)
            fk["ref_columns"].append(rc)

        self._meta = {"tables": tables, "fks": list(fks.values())}
        return self._meta

    def tables(self):
        return list(self._load()["tables"])

    def columns(self, table):
        return list(self._load()["tables"][table]["columns"])

    def primary_key(self, table):
        return list(self._load()["tables"][table]["pk"])

    def unique_keys(self, table):
        return [list(cols) for cols in self._load()["tables"][table]["unique"].values()]

    def foreign_keys(self):
        """Declared FKs with both ends among the scanned tables."""
        meta = self._load()
        return [dict(fk) for fk in meta["fks"] if fk["ref_table"] in meta["tables"]]

    def foreign_keys_leaving_scope(self):
        """Declared FKs from a scanned table to a table that is not scanned."""
        meta = self._load()
        return [dict(fk) for fk in meta["fks"] if fk["ref_table"] not in meta["tables"]]

    def row_count(self, table):
        rows = self._load()["tables"][table]["rows"]
        if rows is None:  # views have no partitions: count them
            rows = int(self.query(f"SELECT COUNT_BIG(*) FROM {self.table_ref(table)}")[0][0])
        return rows

    def description(self, table, column=None):
        t = self._load()["tables"][table]
        return t["col_desc"].get(column) if column else t["desc"]

