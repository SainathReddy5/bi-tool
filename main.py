"""
Command line. Everything is driven by a config file (see config.py).

    python main.py generate config.json      # scan the listed tables -> semantic layer
    python main.py validate config.json      # check that layer against the data (-v: show passes)

validate exits 1 if any check FAILs.
"""

from __future__ import annotations

import argparse
import sys

import semantic
import validate
from config import ConfigError, load_config, open_source, roles


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

    if layer.get("notes"):
        print("\nNotes")
        for n in layer["notes"]:
            print(f"  NOTE {n}")


def cmd_generate(args) -> int:
    cfg = load_config(args.config)
    with open_source(cfg) as db:
        layer = semantic.generate(db, roles(cfg, db))
        print(f"Scanned {len(db.tables())} table(s) listed in {args.config}\n")
    semantic.save(layer, cfg["output"])
    print_layer(layer)
    print(f"\nwritten to {cfg['output']}")
    return 0


def cmd_validate(args) -> int:
    cfg = load_config(args.config)
    layer = semantic.load(cfg["output"])
    with open_source(cfg) as db:
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
        sp.add_argument("config", help="config file listing the source, facts and dimensions")
        sp.set_defaults(func=fn)
        if name == "validate":
            sp.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, ValueError, FileNotFoundError) as e:
        print(f"ERROR  {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
