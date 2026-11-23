"""
Validate a semantic layer against the data.

    results = validate(db, layer)     # list of {"status", "check", "where", "message"}

status is PASS, WARN or FAIL. FAIL means a query built from this layer can
return wrong numbers; WARN means it works but someone should look.

Checks
  structure      tables / columns referenced by the layer exist, join types match
  relationships  inferred relationships with low confidence need review
  join_safety    every join target key is really unique (else rows multiply)
  orphans        fact keys with no matching dimension row
  reconcile      SUM(measure) after joining each dimension == SUM on the raw fact
  naming         dimension / measure names and aliases unique within a star
  measures       sum/avg only on numeric columns
  grain          the fact has a known, unique grain
"""

from __future__ import annotations

from collections import Counter

LOW_CONFIDENCE = 0.8
TOLERANCE = 1e-6


def _result(status, check, where, message):
    return {"status": status, "check": check, "where": where, "message": message}


def validate(db, layer: dict) -> list[dict]:
    out: list[dict] = []
    columns = {t: {c.lower(): c for c in _names(db, t)} for t in db.tables()}
    families = {t: {c["name"].lower(): c["family"] for c in db.columns(t)} for t in db.tables()}

    # ---- relationships ----------------------------------------------------- #
    for r in layer["relationships"]:
        where = r["name"]
        if r["origin"] == "inferred" and r["confidence"] < LOW_CONFIDENCE:
            out.append(_result("WARN", "relationships", where,
                               f"inferred with low confidence {r['confidence']}; please confirm"))

    for star in layer["stars"]:
        fact, s = star["fact_table"], star["name"]

        # ---- structure: fact + measures ----------------------------------- #
        if fact not in columns:
            out.append(_result("FAIL", "structure", s, f"fact table {fact} not found"))
            continue
        for m in star["measures"]:
            if m["column"] and m["column"].lower() not in columns[fact]:
                out.append(_result("FAIL", "structure", f"{s} / {m['name']}",
                                   f"measure column {fact}.{m['column']} not found"))

        # ---- measures: numeric -------------------------------------------- #
        for m in star["measures"]:
            if m["column"] and m["aggregation"] in ("sum", "avg"):
                fam = families[fact].get(m["column"].lower())
                if fam not in ("integer", "decimal"):
                    out.append(_result("FAIL", "measures", f"{s} / {m['name']}",
                                       f"{m['aggregation']} on non-numeric column ({fam})"))

        # ---- naming ------------------------------------------------------- #
        for kind, names in (("dimension", [d["name"] for d in star["dimensions"]]),
                            ("measure", [m["name"] for m in star["measures"]]),
                            ("alias", [star["fact_alias"]] + [st["to"] for d in star["dimensions"]
                                                              for st in d["join_path"][-1:]])):
            for name, n in Counter(x.lower() for x in names).items():
                if n > 1:
                    out.append(_result("FAIL", "naming", s, f"{kind} name '{name}' used {n} times"))

        # ---- grain -------------------------------------------------------- #
        g = star.get("grain") or {}
        if not g.get("columns"):
            out.append(_result("WARN", "grain", s, "no unique grain found for the fact table"))
        elif db.is_unique(fact, g["columns"]):
            out.append(_result("PASS", "grain", s, f"one row per {', '.join(g['columns'])}"))
        else:
            out.append(_result("FAIL", "grain", s,
                               f"declared grain ({', '.join(g['columns'])}) is not unique"))

        # ---- per dimension ------------------------------------------------ #
        # only measures that passed the structure/type checks above
        sum_measures = [m["column"] for m in star["measures"]
                        if m["aggregation"] == "sum" and m["column"]
                        and families[fact].get(m["column"].lower()) in ("integer", "decimal")]
        base_count, base_sums = db.totals(fact, sum_measures)
        alias_table = {star["fact_alias"]: fact}
        checked_steps: set[tuple] = set()

        for d in star["dimensions"]:
            where = f"{s} / {d['name']}"
            ok = True

            # structure: every join column exists, types match
            for st in d["join_path"]:
                alias_table[st["to"]] = st["to_table"]
                left_t = alias_table.get(st["from"])
                right_t = st["to_table"]
                if right_t not in columns or left_t not in columns:
                    out.append(_result("FAIL", "structure", where, f"table {right_t} not found"))
                    ok = False
                    continue
                for lc, rc in zip(st["from_columns"], st["to_columns"]):
                    if lc.lower() not in columns[left_t] or rc.lower() not in columns[right_t]:
                        out.append(_result("FAIL", "structure", where,
                                           f"join column {left_t}.{lc} or {right_t}.{rc} not found"))
                        ok = False
                    elif families[left_t][lc.lower()] != families[right_t][rc.lower()]:
                        out.append(_result("WARN", "structure", where,
                                           f"type mismatch {left_t}.{lc} "
                                           f"({families[left_t][lc.lower()]}) vs {right_t}.{rc} "
                                           f"({families[right_t][rc.lower()]})"))
            for a in d["attributes"]:
                if a["column"].lower() not in columns.get(d["table"], {}):
                    out.append(_result("FAIL", "structure", where,
                                       f"attribute column {d['table']}.{a['column']} not found"))
            for h in d["hierarchies"]:
                for lvl in h["levels"]:
                    if lvl["column"].lower() not in columns.get(lvl["table"], {}):
                        out.append(_result("FAIL", "structure", where,
                                           f"hierarchy '{h['name']}' level column "
                                           f"{lvl['table']}.{lvl['column']} not found"))
            if not ok:
                continue

            # join safety + orphans, once per distinct join step
            for st in d["join_path"]:
                key = (alias_table[st["from"]], tuple(st["from_columns"]),
                       st["to_table"], tuple(st["to_columns"]))
                if key in checked_steps:
                    continue
                checked_steps.add(key)
                left_t = alias_table[st["from"]]
                dups = db.duplicate_key_count(st["to_table"], st["to_columns"])
                if dups:
                    out.append(_result("FAIL", "join_safety", where,
                                       f"{st['to_table']}({', '.join(st['to_columns'])}) has {dups} "
                                       f"duplicated key value(s): joining multiplies rows"))
                total, matched = db.match_count(left_t, st["from_columns"],
                                                st["to_table"], st["to_columns"])
                if total - matched:
                    out.append(_result("WARN", "orphans", where,
                                       f"{total - matched} row(s) in {left_t} have "
                                       f"{', '.join(st['from_columns'])} with no match in "
                                       f"{st['to_table']} (shown as an empty member)"))

            # reconcile: totals through the join path == raw fact totals
            count, sums = db.joined_totals(fact, sum_measures, d["join_path"], star["fact_alias"])
            diffs = []
            if count != base_count:
                diffs.append(f"rows {base_count:,} -> {count:,}")
            for col, a, b in zip(sum_measures, base_sums, sums):
                if a is not None and b is not None and abs(a - b) > TOLERANCE * max(1.0, abs(a)):
                    diffs.append(f"SUM({col}) {a:,.2f} -> {b:,.2f} ({(b - a) / a:+.2%})")
            if diffs:
                out.append(_result("FAIL", "reconcile", where,
                                   "totals change when this dimension is joined: " + "; ".join(diffs)))
            else:
                out.append(_result("PASS", "reconcile", where,
                                   f"{count:,} rows and {len(sum_measures)} measure total(s) match"))

    return out


def _names(db, table):
    return [c["name"] for c in db.columns(table)]


def summary(results: list[dict]) -> dict:
    return dict(Counter(r["status"] for r in results))
