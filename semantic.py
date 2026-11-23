"""
Generate a semantic layer from database metadata.

    layer = generate(db)        # db: any backend from db.py
    save(layer, "semantic.json")

Steps
  1. read metadata          tables, columns, keys, declared FKs, row counts
  2. relationships          declared FKs + inferred ones (name, type, data containment)
  3. classify tables        fact / dimension / lookup / staging / unknown (scored, with reasons)
  4. stars                  each fact + the dimensions reachable through to-one joins
  5. measures               numeric non-key fact columns
  6. dimensions             attributes + hierarchies (date, geography, snowflake)

This module contains no SQL: every database question goes through db.py.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

NUMERIC = {"integer", "decimal"}
INFER_MIN_CONTAINMENT = 0.95      # share of child values that must exist in the parent
FACT_PREFIXES = ("fact_", "fct_", "f_")
DIM_PREFIXES = ("dim_", "d_")
LOOKUP_PREFIXES = ("lkp_", "lu_", "ref_")
STAGING_PREFIXES = ("stg_", "staging_", "tmp_", "temp_", "raw_", "landing_")
LOOKUP_WORDS = ("status", "type", "code", "lookup", "reason", "flag")
NON_ADDITIVE_WORDS = ("price", "rate", "pct", "percent", "ratio", "avg", "average")


# --------------------------------------------------------------------------- #
# Naming helpers
# --------------------------------------------------------------------------- #

def base_name(table: str) -> str:
    """'dbo.fact_sales' -> 'fact_sales'"""
    return table.split(".")[-1].lower()


def entity(table: str) -> str:
    """'dbo.fact_sales' -> 'sales' (prefix removed, plural kept)"""
    b = base_name(table)
    for p in FACT_PREFIXES + DIM_PREFIXES + LOOKUP_PREFIXES + STAGING_PREFIXES:
        if b.startswith(p):
            return b[len(p):]
    return b


def stem(table: str) -> str:
    """'dbo.dim_customers' -> 'customer', 'order_status' stays 'order_status'"""
    b = entity(table)
    if b.endswith("ies") and len(b) > 4:
        b = b[:-3] + "y"
    elif b.endswith("s") and not b.endswith(("ss", "us", "is")) and len(b) > 3:
        b = b[:-1]
    return b


def label(name: str) -> str:
    """'customer_name' -> 'Customer Name', 'CustomerID' -> 'Customer Id'"""
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    return " ".join(w.capitalize() for w in re.split(r"[_\s]+", name) if w)


def snake(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


# --------------------------------------------------------------------------- #
# 1. Metadata
# --------------------------------------------------------------------------- #

def read_metadata(db) -> dict:
    tables = {}
    for t in db.tables():
        tables[t] = {
            "columns": db.columns(t),
            "primary_key": db.primary_key(t),
            "unique_keys": db.unique_keys(t),
            "row_count": db.row_count(t),
            "description": db.description(t),
        }
    return {"tables": tables, "foreign_keys": db.foreign_keys()}


def _col(meta: dict, table: str, column: str) -> dict | None:
    for c in meta["tables"][table]["columns"]:
        if c["name"].lower() == column.lower():
            return c
    return None


# --------------------------------------------------------------------------- #
# 2. Relationships
# --------------------------------------------------------------------------- #

def _relationship(db, child, child_cols, parent, parent_cols, origin, confidence, reasons, name=None):
    unique_child = db.is_unique(child, child_cols)
    total, matched = db.match_count(child, child_cols, parent, parent_cols)
    return {
        "name": name or f"{base_name(child)}.{'_'.join(child_cols)}->{base_name(parent)}",
        "from_table": child,
        "from_columns": list(child_cols),
        "to_table": parent,
        "to_columns": list(parent_cols),
        # to-one from the child side; read from the parent it is one-to-many
        "cardinality": "one_to_one" if unique_child else "many_to_one",
        "origin": origin,
        "confidence": round(confidence, 2),
        "containment": round(matched / total, 4) if total else None,
        "reasons": reasons,
    }


def find_relationships(db, meta: dict) -> list[dict]:
    rels = []
    declared_cols: set[tuple[str, str]] = set()

    # ---- declared --------------------------------------------------------- #
    for fk in meta["foreign_keys"]:
        rels.append(_relationship(db, fk["table"], fk["columns"], fk["ref_table"], fk["ref_columns"],
                                  "declared", 1.0, ["declared foreign key"], name=fk["name"]))
        for c in fk["columns"]:
            declared_cols.add((fk["table"], c.lower()))

    # ---- candidate targets: single-column keys of every table ------------- #
    targets = []   # (table, key column, is declared PK)
    for t, info in meta["tables"].items():
        for key in info["unique_keys"]:
            if len(key) == 1:
                targets.append((t, key[0], key == info["primary_key"]))

    # ---- inferred --------------------------------------------------------- #
    for child, info in meta["tables"].items():
        own_pk = [c.lower() for c in info["primary_key"]]
        for col in info["columns"]:
            cname = col["name"].lower()
            if (child, cname) in declared_cols:
                continue
            if own_pk == [cname]:
                continue  # a table's own single-column PK is not a foreign key
            best = None
            for parent, key, is_pk in targets:
                if parent == child:
                    continue
                k = key.lower()
                s = stem(parent)
                if cname == k:
                    name_score, why = 0.5, f"same name as {base_name(parent)}.{key}"
                elif cname in (f"{s}_id", f"{s}id", f"{s}_key", f"{s}key") and k in ("id", "key"):
                    name_score, why = 0.5, f"'{col['name']}' = table name + key"
                elif cname.endswith("_" + k) and k not in ("id", "key", "code"):
                    name_score, why = 0.4, f"ends with '_{key}' (role-playing?)"
                else:
                    continue
                pcol = _col(meta, parent, key)
                if pcol is None or pcol["family"] != col["family"]:
                    continue
                total, matched = db.match_count(child, [col["name"]], parent, [key])
                if total == 0:
                    continue
                share = matched / total
                if share < INFER_MIN_CONTAINMENT:
                    continue
                score = name_score + (0.3 if share >= 0.99 else 0.15) + (0.2 if is_pk else 0.1)
                reasons = [why, f"types match ({col['family']})",
                           f"{share:.1%} of values found in target",
                           "target is primary key" if is_pk else "target is unique key"]
                if best is None or score > best[0]:
                    best = (score, parent, key, reasons)
            if best:
                score, parent, key, reasons = best
                rels.append(_relationship(db, child, [col["name"]], parent, [key],
                                          "inferred", min(score, 0.99), reasons))
    return rels


# --------------------------------------------------------------------------- #
# 3. Table classification
# --------------------------------------------------------------------------- #

def _fk_columns(rels: list[dict], table: str) -> set[str]:
    return {c.lower() for r in rels if r["from_table"] == table for c in r["from_columns"]}


def classify_tables(meta: dict, rels: list[dict]) -> dict:
    max_rows = max((i["row_count"] for i in meta["tables"].values()), default=0) or 1
    out = {}
    for t, info in meta["tables"].items():
        b = base_name(t)
        cols = info["columns"]
        outgoing = [r for r in rels if r["from_table"] == t]
        incoming = [r for r in rels if r["to_table"] == t]
        fk_cols = _fk_columns(rels, t)
        pk = {c.lower() for c in info["primary_key"]}
        measures = [c for c in cols if c["family"] in NUMERIC
                    and c["name"].lower() not in fk_cols | pk]
        strings = [c for c in cols if c["family"] == "string"]
        rows = info["row_count"]

        score = Counter()
        reasons: dict[str, list[str]] = {k: [] for k in
                                         ("fact", "dimension", "lookup", "bridge", "staging")}

        def add(kind, pts, why):
            score[kind] += pts
            reasons[kind].append(why)

        # fact
        if b.startswith(FACT_PREFIXES):
            add("fact", 3, "name prefix")
        if len(outgoing) >= 2:
            add("fact", 2, f"{len(outgoing)} outgoing relationships")
        if measures and outgoing:
            add("fact", 2, f"{len(measures)} numeric non-key column(s)")
        if outgoing and not incoming:
            add("fact", 1, "not referenced by other tables")
        if rows >= max_rows / 10 and outgoing:
            add("fact", 1, f"large ({rows:,} rows)")

        # dimension
        if b.startswith(DIM_PREFIXES):
            add("dimension", 3, "name prefix")
        if incoming:
            add("dimension", 2, f"referenced by {len(incoming)} relationship(s)")
        if pk:
            add("dimension", 1, "has primary key")
        if strings and incoming:
            add("dimension", 1, "descriptive text columns")

        # lookup: small code/description table
        if b.startswith(LOOKUP_PREFIXES):
            add("lookup", 3, "name prefix")
        if len(cols) <= 3 and incoming:
            add("lookup", 2, f"only {len(cols)} columns")
        if any(w in b for w in LOOKUP_WORDS):
            add("lookup", 2, "code/status style name")
        if incoming:
            add("lookup", 1, "referenced")
        if rows <= 100 and incoming:
            add("lookup", 1, f"small ({rows} rows)")

        # bridge: key made only of foreign keys, almost nothing else
        if len(outgoing) >= 2 and pk and pk <= fk_cols and len(cols) - len(pk) <= 2:
            add("bridge", 6, "primary key is made of foreign keys only")

        # staging
        if b.startswith(STAGING_PREFIXES):
            add("staging", 4, "name prefix")
        if not pk and not outgoing and not incoming:
            add("staging", 2, "no keys and no relationships")

        best, pts = score.most_common(1)[0] if score else ("unknown", 0)
        role = best if pts >= 3 else "unknown"
        out[t] = {
            "role": role,
            "scores": dict(score),
            "reasons": reasons.get(role, []),
            "row_count": rows,
        }
    return out


# --------------------------------------------------------------------------- #
# 4-6. Stars, measures, dimensions
# --------------------------------------------------------------------------- #

def _name_column(meta: dict, table: str) -> str:
    """Best 'display' column of a table: *name / *desc / *title, else first text column."""
    cols = meta["tables"][table]["columns"]
    for c in cols:
        if c["family"] == "string" and re.search(r"name|desc|title|label", c["name"], re.I):
            return c["name"]
    for c in cols:
        if c["family"] == "string":
            return c["name"]
    pk = meta["tables"][table]["primary_key"]
    return pk[0] if pk else cols[0]["name"]


def _measures(meta: dict, fact: str, rels: list[dict]) -> list[dict]:
    info = meta["tables"][fact]
    skip = _fk_columns(rels, fact) | {c.lower() for c in info["primary_key"]}
    out = []
    for c in info["columns"]:
        if c["family"] not in NUMERIC or c["name"].lower() in skip:
            continue
        non_additive = any(w in c["name"].lower() for w in NON_ADDITIVE_WORDS)
        out.append({
            "name": label(c["name"]),
            "column": c["name"],
            "aggregation": "avg" if non_additive else "sum",
            "additivity": "non_additive" if non_additive else "additive",
            "type": c["family"],
        })
    out.append({"name": f"{label(entity(fact))} Count", "column": None, "aggregation": "count",
                "additivity": "additive", "type": "integer"})
    return out


_DATE_LEVELS = [("Year", ("year", "calendar_year")),
                ("Quarter", ("quarter", "calendar_quarter")),
                ("Month", ("month_name", "month", "calendar_month")),
                ("Day", ("full_date", "date", "day"))]
_GEO_LEVELS = [("Country", ("country",)), ("Region", ("region",)),
               ("State", ("state", "province")), ("City", ("city",))]


def _level_hierarchy(meta: dict, table: str, name: str, spec) -> dict | None:
    names = {c["name"].lower(): c["name"] for c in meta["tables"][table]["columns"]}
    levels = []
    for lvl, candidates in spec:
        for cand in candidates:
            if cand in names:
                levels.append({"name": lvl, "table": table, "column": names[cand]})
                break
    return {"name": name, "levels": levels} if len(levels) >= 2 else None


def build_stars(meta: dict, rels: list[dict], roles: dict) -> list[dict]:
    to_one = [r for r in rels if r["cardinality"] in ("many_to_one", "one_to_one")]
    dim_like = {"dimension", "lookup"}
    stars = []

    for fact, cls in roles.items():
        if cls["role"] != "fact":
            continue
        dims: list[dict] = []
        used_aliases = {"f"}
        notes = []

        direct = [r for r in to_one if r["from_table"] == fact
                  and roles[r["to_table"]]["role"] in dim_like]
        per_target = Counter(r["to_table"] for r in direct)

        def make_alias(text):
            a = snake(text) or "d"
            base, i = a, 2
            while a in used_aliases:
                a, i = f"{base}_{i}", i + 1
            used_aliases.add(a)
            return a

        def attributes(table, key_cols):
            skip = {k.lower() for k in key_cols} | _fk_columns(rels, table)
            return [{"name": label(c["name"]), "column": c["name"], "type": c["family"]}
                    for c in meta["tables"][table]["columns"] if c["name"].lower() not in skip]

        for r in sorted(direct, key=lambda r: r["from_columns"]):
            parent = r["to_table"]
            if per_target[parent] > 1:
                # role-playing: name after the FK column (ship_date_key -> "Ship Date")
                role = r["from_columns"][0].lower()
                for k in r["to_columns"]:
                    role = role.removesuffix("_" + k.lower())
                dim_name = label(f"{role}_{stem(parent)}")
            else:
                dim_name = label(stem(parent))
            alias = make_alias(dim_name)
            step = {"from": "f", "from_columns": r["from_columns"], "to_table": parent,
                    "to": alias, "to_columns": r["to_columns"], "relationship": r["name"]}
            dim = {
                "name": dim_name,
                "table": parent,
                "role": roles[parent]["role"],
                "key": r["to_columns"],
                "join_path": [step],
                "attributes": attributes(parent, r["to_columns"]),
                "hierarchies": [],
            }
            for h in (_level_hierarchy(meta, parent, "Calendar", _DATE_LEVELS),
                      _level_hierarchy(meta, parent, "Geography", _GEO_LEVELS)):
                if h:
                    dim["hierarchies"].append(h)
            dims.append(dim)

            # snowflake: follow to-one joins from this dimension to other dimension tables
            path, current, current_alias, visited = [step], parent, alias, {fact, parent}
            chain = [(parent, alias)]
            while True:
                nxt = [x for x in to_one if x["from_table"] == current
                       and roles[x["to_table"]]["role"] in dim_like and x["to_table"] not in visited]
                if len(nxt) != 1:
                    if len(nxt) > 1:
                        notes.append(f"{dim_name}: {len(nxt)} outward joins from {current}; "
                                     f"only single chains are followed")
                    break
                x = nxt[0]
                outer = x["to_table"]
                outer_name = label(stem(outer)) if per_target[parent] == 1 else \
                    f"{dim_name} {label(stem(outer))}"
                outer_alias = make_alias(outer_name)
                step2 = {"from": current_alias, "from_columns": x["from_columns"], "to_table": outer,
                         "to": outer_alias, "to_columns": x["to_columns"], "relationship": x["name"]}
                path = path + [step2]
                dims.append({
                    "name": outer_name, "table": outer, "role": roles[outer]["role"],
                    "key": x["to_columns"], "join_path": path,
                    "attributes": attributes(outer, x["to_columns"]), "hierarchies": [],
                })
                chain.append((outer, outer_alias))
                visited.add(outer)
                current, current_alias = outer, outer_alias

            if len(chain) > 1:  # Category > Product style hierarchy on the base dimension
                levels = [{"name": label(stem(t)), "table": t, "column": _name_column(meta, t)}
                          for t, _ in reversed(chain)]
                dim["hierarchies"].append({"name": " > ".join(l["name"] for l in levels),
                                           "levels": levels})

        for parent, n in per_target.items():
            if n > 1:
                notes.append(f"role-playing: {base_name(parent)} is joined {n} times "
                             f"({', '.join(d['name'] for d in dims if d['table'] == parent)})")

        stars.append({
            "name": label(entity(fact)),
            "fact_table": fact,
            "fact_alias": "f",
            "grain": None,
            "measures": _measures(meta, fact, rels),
            "dimensions": dims,
            "notes": notes,
        })
    return stars


def _grain(db, meta: dict, fact: str, rels: list[dict]) -> dict:
    pk = meta["tables"][fact]["primary_key"]
    if pk:
        return {"columns": pk, "source": "primary key"}
    fk_cols = [c for r in rels if r["from_table"] == fact for c in r["from_columns"]]
    from itertools import combinations
    for n in range(1, min(4, len(fk_cols)) + 1):
        for combo in combinations(fk_cols, n):
            if db.is_unique(fact, list(combo)):
                return {"columns": list(combo), "source": "unique combination of foreign keys"}
    return {"columns": [], "source": "unknown: no unique key found"}


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #

def generate(db) -> dict:
    meta = read_metadata(db)
    rels = find_relationships(db, meta)
    roles = classify_tables(meta, rels)
    stars = build_stars(meta, rels, roles)

    shared = Counter(d["table"] for s in stars for d in {x["table"]: x for x in s["dimensions"]}.values())
    for s in stars:
        s["grain"] = _grain(db, meta, s["fact_table"], rels)
        conformed = sorted({base_name(d["table"]) for d in s["dimensions"] if shared[d["table"]] > 1})
        if conformed:
            s["notes"].append(
                "shares dimensions with another fact (" + ", ".join(conformed) + "): "
                "aggregate each fact separately before combining (chasm trap)")

    for t, info in meta["tables"].items():
        roles[t]["description"] = info["description"]
        for c in info["columns"]:
            desc = db.description(t, c["name"])
            if desc:
                c["description"] = desc

    return {
        "version": 1,
        "tables": {t: {**roles[t], "columns": meta["tables"][t]["columns"],
                       "primary_key": meta["tables"][t]["primary_key"]} for t in meta["tables"]},
        "relationships": rels,
        "stars": stars,
    }


def save(layer: dict, path: str | Path) -> None:
    Path(path).write_text(json.dumps(layer, indent=2, default=str))


def load(path: str | Path) -> dict:
    return json.loads(Path(path).read_text())
