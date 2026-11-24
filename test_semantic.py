"""Tests on the generated retail example.   Run: python -m pytest -q"""

import pytest

import semantic
import validate
from db import CsvDatabase, build_connection_string
from make_sample import generate as make_sample


@pytest.fixture(scope="module")
def retail(tmp_path_factory):
    folder = make_sample(tmp_path_factory.mktemp("retail"), plant_problems=True)
    with CsvDatabase(folder) as db:
        layer = semantic.generate(db)
        yield db, layer


@pytest.fixture(scope="module")
def clean(tmp_path_factory):
    folder = make_sample(tmp_path_factory.mktemp("clean"), plant_problems=False)
    with CsvDatabase(folder) as db:
        yield db, semantic.generate(db)


def star(layer, name):
    return next(s for s in layer["stars"] if s["name"] == name)


def rel(layer, table, col):
    return [r for r in layer["relationships"]
            if r["from_table"] == table and r["from_columns"] == [col]]


# ---- classification ---------------------------------------------------------

def test_table_roles(retail):
    _, layer = retail
    roles = {t: i["role"] for t, i in layer["tables"].items()}
    assert roles == {
        "fact_sales": "fact", "fact_returns": "fact",
        "dim_customer": "dimension", "dim_product": "dimension", "dim_store": "dimension",
        "dim_date": "dimension", "dim_category": "dimension",
        "order_status": "lookup", "stg_orders": "staging",
    }


# ---- relationships ----------------------------------------------------------

@pytest.mark.parametrize("table, col, target", [
    ("fact_sales", "ship_date_key", "dim_date"),
    ("fact_sales", "store_id", "dim_store"),
    ("fact_sales", "status_code", "order_status"),
    ("fact_returns", "customer_id", "dim_customer"),
    ("fact_returns", "store_id", "dim_store"),
])
def test_undeclared_fks_are_inferred(retail, table, col, target):
    _, layer = retail
    [r] = rel(layer, table, col)
    assert r["to_table"] == target and r["origin"] == "inferred" and r["confidence"] >= 0.8


def test_no_false_relationships(retail):
    _, layer = retail
    assert len(layer["relationships"]) == 11          # 6 declared + 5 inferred
    linked = {(r["from_table"], c) for r in layer["relationships"] for c in r["from_columns"]}
    for t, c in [("fact_sales", "quantity"), ("fact_sales", "sales_id"),
                 ("dim_date", "year"), ("fact_returns", "return_qty")]:
        assert (t, c) not in linked


def test_cardinality(retail):
    _, layer = retail
    assert all(r["cardinality"] == "many_to_one" for r in layer["relationships"])


# ---- stars ------------------------------------------------------------------

def test_role_playing_dates(retail):
    _, layer = retail
    sales = star(layer, "Sales")
    names = [d["name"] for d in sales["dimensions"] if d["table"] == "dim_date"]
    assert sorted(names) == ["Order Date", "Ship Date"]
    assert any("role-playing" in n for n in sales["notes"])


def test_measures(retail):
    _, layer = retail
    m = {x["name"]: x["aggregation"] for x in star(layer, "Sales")["measures"]}
    assert m == {"Quantity": "sum", "Unit Price": "avg", "Sales Amount": "sum",
                 "Discount Amount": "sum", "Sales Count": "count"}


def test_snowflake_and_hierarchies(retail):
    _, layer = retail
    dims = {d["name"]: d for d in star(layer, "Sales")["dimensions"]}
    assert [s["to_table"] for s in dims["Category"]["join_path"]] == ["dim_product", "dim_category"]
    hier = {h["name"]: [l["column"] for l in h["levels"]]
            for d in dims.values() for h in d["hierarchies"]}
    assert hier["Category > Product"] == ["category_name", "product_name"]
    assert hier["Calendar"] == ["year", "quarter", "month_name", "full_date"]
    assert hier["Geography"] == ["country", "city"]


def test_grain_and_chasm_note(retail):
    _, layer = retail
    s = star(layer, "Sales")
    assert s["grain"]["columns"] == ["sales_id"]
    assert any("chasm" in n for n in s["notes"])


# ---- validation -------------------------------------------------------------

def test_validator_catches_planted_problems(retail):
    db, layer = retail
    res = validate.validate(db, layer)
    fails = {(r["check"], r["where"]) for r in res if r["status"] == "FAIL"}
    assert fails == {("join_safety", "Sales / Customer"), ("reconcile", "Sales / Customer"),
                     ("join_safety", "Returns / Customer"), ("reconcile", "Returns / Customer")}
    warns = [r for r in res if r["status"] == "WARN"]
    assert len(warns) == 1 and warns[0]["check"] == "orphans" and "1 row(s)" in warns[0]["message"]


def test_clean_data_passes(clean):
    db, layer = clean
    res = validate.validate(db, layer)
    assert [r for r in res if r["status"] != "PASS"] == []


def test_validator_catches_broken_layer(clean):
    db, layer = clean
    import copy
    broken = copy.deepcopy(layer)
    s = star(broken, "Sales")
    s["measures"][0]["column"] = "no_such_col"
    # join product on category_id instead of product_id -> wrong key, fan-out
    prod = next(d for d in s["dimensions"] if d["name"] == "Product")
    prod["join_path"][0]["to_columns"] = ["category_id"]
    res = validate.validate(db, broken)
    msgs = [(r["status"], r["check"]) for r in res]
    assert ("FAIL", "structure") in msgs and ("FAIL", "join_safety") in msgs
    assert ("FAIL", "reconcile") in msgs


# ---- SQL Server connection string (no server needed) ------------------------

def test_connection_string():
    cs = build_connection_string("db1", "SalesDW", "sa", "p;w}d", port=1433)
    assert cs.startswith("Driver={ODBC Driver 18 for SQL Server};Server={tcp:db1,1433};")
    assert "PWD={p;w}}d}" in cs and "Encrypt={yes}" in cs
    trusted = build_connection_string(r"SQL01\PROD", "SalesDW", trusted=True)
    assert "Trusted_Connection={yes}" in trusted and "UID" not in trusted
    with pytest.raises(ValueError):
        build_connection_string("db1", "SalesDW")


# ---- config file: only listed tables, roles from config --------------------

import json

from config import ConfigError, connection_string, load_config, open_source, roles as config_roles
from db import resolve_tables


def run_config(tmp_path, folder, facts, dimensions):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"source": {"type": "csv", "folder": str(folder)},
                                "facts": facts, "dimensions": dimensions,
                                "output": str(tmp_path / "semantic.json")}))
    cfg = load_config(path)
    db = open_source(cfg)
    return db, semantic.generate(db, config_roles(cfg, db))


@pytest.fixture(scope="module")
def retail_folder(tmp_path_factory):
    return make_sample(tmp_path_factory.mktemp("cfg_retail"), plant_problems=True)


@pytest.fixture(scope="module")
def clean_folder(tmp_path_factory):
    return make_sample(tmp_path_factory.mktemp("cfg_clean"), plant_problems=False)


def test_only_listed_tables_are_scanned(tmp_path, retail_folder):
    db, layer = run_config(tmp_path, retail_folder, ["fact_sales"],
                           ["dim_customer", "dim_product", "dim_date", "dim_store"])
    assert db.tables() == ["fact_sales", "dim_customer", "dim_product", "dim_date", "dim_store"]
    assert set(layer["tables"]) == set(db.tables())
    assert [s["name"] for s in layer["stars"]] == ["Sales"]
    assert not any(r["to_table"] == "order_status" for r in layer["relationships"])
    assert layer["config"]["facts"] == ["fact_sales"]


def test_config_roles_win(tmp_path, retail_folder):
    _, layer = run_config(tmp_path, retail_folder, ["fact_sales"], ["order_status", "dim_store"])
    t = layer["tables"]["order_status"]
    assert t["role"] == "dimension" and t["reasons"] == ["set in config"]
    assert t["guessed_role"] == "lookup"
    assert not any("order_status" in n for n in layer["notes"])  # lookup is a kind of dimension


def test_disagreement_is_noted(tmp_path, retail_folder):
    _, layer = run_config(tmp_path, retail_folder, ["fact_sales"],
                          ["fact_returns", "dim_product", "dim_date"])
    assert layer["tables"]["fact_returns"]["role"] == "dimension"
    assert any("fact_returns is configured as dimension but looks like a fact" in n
               for n in layer["notes"])
    assert any("dimension fact_returns is not joined to any fact" in n for n in layer["notes"])


def test_missing_dimension_is_noted(tmp_path, retail_folder):
    _, layer = run_config(tmp_path, retail_folder, ["fact_sales"], ["dim_product", "dim_date"])
    assert any("fact_sales.customer_id references dim_customer, which is not in the config" in n
               for n in layer["notes"])
    assert "Customer" not in [d["name"] for d in layer["stars"][0]["dimensions"]]


def test_unknown_table_suggests_name(retail_folder):
    with pytest.raises(ValueError, match="'fact_sale' not found.*did you mean fact_sales"):
        CsvDatabase(retail_folder, tables=["fact_sale"])


def test_resolve_tables_schema_and_case():
    avail = ["dbo.FactSales", "dbo.DimDate", "stage.DimDate"]
    assert resolve_tables(["factsales", "DBO.DIMDATE"], avail) == ["dbo.FactSales", "dbo.DimDate"]
    with pytest.raises(ValueError, match="ambiguous"):
        resolve_tables(["DimDate"], avail)


@pytest.mark.parametrize("cfg, message", [
    ({"source": {"type": "csv", "folder": "x"}, "facts": []}, "at least one fact"),
    ({"source": {"type": "csv", "folder": "x"}, "facts": ["a"], "dimensions": ["A"]},
     "both fact and dimension"),
    ({"source": {"type": "oracle"}, "facts": ["a"]}, '"source"'),
    ({"source": {"type": "sqlserver", "server": "s", "database": "d"}, "facts": ["a"]},
     "password_env"),
])
def test_bad_config(tmp_path, cfg, message):
    path = tmp_path / "c.json"
    path.write_text(json.dumps(cfg))
    with pytest.raises(ConfigError, match=message):
        load_config(path)


def test_sqlserver_password_from_env(monkeypatch):
    src = {"type": "sqlserver", "server": "sql01", "port": 1433, "database": "DW",
           "user": "bi", "password_env": "TEST_BI_PW"}
    monkeypatch.delenv("TEST_BI_PW", raising=False)
    with pytest.raises(ConfigError, match="TEST_BI_PW"):
        connection_string(src)
    monkeypatch.setenv("TEST_BI_PW", "s3cret")
    cs = connection_string(src)
    assert "Server={tcp:sql01,1433}" in cs and "PWD={s3cret}" in cs


def test_validate_with_config(tmp_path, clean_folder, retail_folder):
    tables = (["fact_sales", "fact_returns"],
              ["dim_customer", "dim_product", "dim_category", "dim_date", "dim_store", "order_status"])
    db, layer = run_config(tmp_path, clean_folder, *tables)
    assert [r for r in validate.validate(db, layer) if r["status"] != "PASS"] == []
    db, layer = run_config(tmp_path, retail_folder, *tables)
    fails = {(r["check"], r["where"]) for r in validate.validate(db, layer) if r["status"] == "FAIL"}
    assert ("reconcile", "Sales / Customer") in fails
