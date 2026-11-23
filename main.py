"""
Command line.

    python main.py generate data/retail                  # -> semantic.json
    python main.py validate data/retail                  # validate semantic.json against the data
    python main.py generate sqlserver                    # same, on SQL Server (MSSQL_* env vars)

Options: -o/--layer PATH (default semantic.json). validate exits 1 if any check FAILs.
"""

from __future__ import annotations

import argparse
import sys

import semantic
import validate
from db import open_database


def print_layer(layer: dict) -> None:
    print("Tables")
    for t, info in layer["tables"].items():
        why = "; ".join(info["reasons"])
        print(f"  {t:<16} {info['role']:<10} {info['row_count']:>6,} rows   {why}")

    print("\nRelationships")
    for r in layer["relationships"]:
        src = f"{r['from_table']}({', '.join(r['from_columns'])})"
        dst = f"{r['to_table']}({', '.join(r['to_columns'])})"
        print(f"  {src:<36} -> {dst:<28} {r['cardinality']:<12} {r['origin']:<9} "
              f"conf={r['confidence']:.2f}")

    for s in layer["stars"]:
        g = s["grain"]
        print(f"\nStar '{s['name']}'  fact={s['fact_table']}  grain=({', '.join(g['columns'])})")
        print("  Measures")
        for m in s["measures"]:
            col = m["column"] or "*"
            print(f"    {m['name']:<20} {m['aggregation'].upper()}({col})  {m['additivity']}")
        print("  Dimensions")
        for d in s["dimensions"]:
            path = " -> ".join(f"{st['to_table']}" for st in d["join_path"])
            print(f"    {d['name']:<16} via {path}")
            for h in d["hierarchies"]:
                print(f"      hierarchy {h['name']}: " + " > ".join(l["column"] for l in h["levels"]))
        for n in s["notes"]:
            print(f"  NOTE {n}")


def cmd_generate(args) -> int:
    with open_database(args.source) as db:
        layer = semantic.generate(db)
    semantic.save(layer, args.layer)
    print_layer(layer)
    print(f"\nwritten to {args.layer}")
    return 0


def cmd_validate(args) -> int:
    layer = semantic.load(args.layer)
    with open_database(args.source) as db:
        results = validate.validate(db, layer)
    order = {"FAIL": 0, "WARN": 1, "PASS": 2}
    for r in sorted(results, key=lambda r: order[r["status"]]):
        if r["status"] == "PASS" and not args.verbose:
            continue
        print(f"{r['status']:<5} {r['check']:<12} {r['where']:<28} {r['message']}")
    counts = validate.summary(results)
    print(f"\n{counts.get('PASS', 0)} passed, {counts.get('WARN', 0)} warnings, "
          f"{counts.get('FAIL', 0)} failed" + ("" if args.verbose else "   (-v to show passes)"))
    return 1 if counts.get("FAIL") else 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Semantic layer generator / validator")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn in (("generate", cmd_generate), ("validate", cmd_validate)):
        sp = sub.add_parser(name)
        sp.add_argument("source", help="CSV folder, or 'sqlserver' to use MSSQL_* env vars")
        sp.add_argument("-o", "--layer", default="semantic.json")
        sp.set_defaults(func=fn)
        if name == "validate":
            sp.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
