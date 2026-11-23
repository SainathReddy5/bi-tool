"""
Generate the example retail warehouse as CSV files (deterministic).

    python make_sample.py                 -> data/retail       (with planted problems)
    python make_sample.py data/clean --clean                    (no planted problems)

Planted problems (so validation has something to catch):
  * dim_customer has customer_id 7 twice      -> fan-out (inflated totals)
  * fact_sales has a row for customer_id 999  -> orphan (no such customer)

Foreign keys deliberately NOT declared in constraints.json (must be inferred):
  fact_sales.ship_date_key -> dim_date.date_key      (role-playing date)
  fact_sales.store_id      -> dim_store.store_id
  fact_sales.status_code   -> order_status.status_code
  fact_returns.customer_id -> dim_customer.customer_id
  fact_returns.store_id    -> dim_store.store_id
"""

from __future__ import annotations

import csv
import json
import random
import sys
from datetime import date, timedelta
from pathlib import Path


def _write(folder: Path, name: str, header: list[str], rows: list[list]) -> None:
    with open(folder / f"{name}.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def generate(folder: str | Path, plant_problems: bool = True) -> Path:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("*.csv"):
        old.unlink()
    rnd = random.Random(42)

    # ---- dim_date: 2024 calendar ---------------------------------------- #
    dates = []
    d = date(2024, 1, 1)
    while d.year == 2024:
        dates.append([int(d.strftime("%Y%m%d")), d.isoformat(), d.year, (d.month - 1) // 3 + 1,
                      d.month, d.strftime("%B"), d.day])
        d += timedelta(days=1)
    _write(folder, "dim_date",
           ["date_key", "full_date", "year", "quarter", "month", "month_name", "day"], dates)
    date_keys = [r[0] for r in dates]

    # ---- dim_category / dim_product (snowflake) ------------------------- #
    categories = [[1, "Electronics"], [2, "Furniture"], [3, "Clothing"], [4, "Groceries"]]
    _write(folder, "dim_category", ["category_id", "category_name"], categories)

    products = []
    for pid in range(1, 21):
        cat = (pid - 1) % 4 + 1
        products.append([pid, f"Product {pid:02d}", cat, round(rnd.uniform(5, 500), 2)])
    _write(folder, "dim_product", ["product_id", "product_name", "category_id", "list_price"], products)

    # ---- dim_customer (with a duplicate key when planting problems) ----- #
    geo = [("USA", "New York"), ("USA", "Chicago"), ("Germany", "Berlin"),
           ("France", "Paris"), ("India", "Hyderabad"), ("Japan", "Tokyo")]
    segments = ["Consumer", "Corporate", "Small Business"]
    customers = []
    for cid in range(1, 51):
        country, city = geo[cid % len(geo)]
        customers.append([cid, f"Customer {cid:02d}", city, country, segments[cid % 3]])
    if plant_problems:
        customers.append([7, "Customer 07 (dup)", "Paris", "France", "Consumer"])
    _write(folder, "dim_customer",
           ["customer_id", "customer_name", "city", "country", "segment"], customers)

    # ---- dim_store / order_status --------------------------------------- #
    stores = [[1, "Downtown", "East"], [2, "Mall", "East"], [3, "Airport", "West"],
              [4, "Outlet", "West"], [5, "Online", "Web"]]
    _write(folder, "dim_store", ["store_id", "store_name", "region"], stores)

    statuses = [["N", "New"], ["S", "Shipped"], ["D", "Delivered"], ["C", "Cancelled"]]
    _write(folder, "order_status", ["status_code", "status_desc"], statuses)

    # ---- fact_sales ----------------------------------------------------- #
    sales = []
    for sid in range(1, 2001):
        oi = rnd.randrange(len(date_keys) - 10)
        status = rnd.choice("NSDDDC")
        ship = "" if status in "NC" else date_keys[oi + rnd.randint(1, 7)]  # NULL when not shipped
        qty = rnd.randint(1, 5)
        price = products[rnd.randrange(20)][3]
        amount = round(qty * price, 2)
        discount = round(amount * rnd.choice([0, 0, 0.05, 0.1]), 2)
        sales.append([sid, date_keys[oi], ship, rnd.randint(1, 50), rnd.randint(1, 20),
                      rnd.randint(1, 5), status, qty, price, amount, discount])
    if plant_problems:
        sales.append([2001, date_keys[100], date_keys[102], 999, 1, 1, "D", 1, 10.0, 10.0, 0.0])
    _write(folder, "fact_sales",
           ["sales_id", "order_date_key", "ship_date_key", "customer_id", "product_id", "store_id",
            "status_code", "quantity", "unit_price", "sales_amount", "discount_amount"], sales)

    # ---- fact_returns (second fact sharing dimensions) ------------------ #
    returns = []
    for rid in range(1, 201):
        returns.append([rid, date_keys[rnd.randrange(len(date_keys))], rnd.randint(1, 50),
                        rnd.randint(1, 20), rnd.randint(1, 5), rnd.randint(1, 3),
                        round(rnd.uniform(5, 300), 2)])
    _write(folder, "fact_returns",
           ["return_id", "return_date_key", "customer_id", "product_id", "store_id",
            "return_qty", "refund_amount"], returns)

    # ---- stg_orders: raw landing table, no keys ------------------------- #
    stg = [[f"ORD-{i:05d}", f"Customer {rnd.randint(1, 50):02d}", f"{rnd.uniform(5, 900):.2f}",
            f"2024-12-{rnd.randint(1, 28):02d} 10:00:00"] for i in range(1, 51)]
    _write(folder, "stg_orders", ["order_ref", "cust_name", "amount_text", "loaded_at"], stg)

    # ---- constraints.json: what a DBA declared (incomplete on purpose) -- #
    constraints = {
        "primary_keys": {
            "dim_date": ["date_key"],
            "dim_category": ["category_id"],
            "dim_product": ["product_id"],
            "dim_customer": ["customer_id"],
            "dim_store": ["store_id"],
            "order_status": ["status_code"],
            "fact_sales": ["sales_id"],
            "fact_returns": ["return_id"],
        },
        "foreign_keys": [
            {"table": "fact_sales", "columns": ["order_date_key"],
             "ref_table": "dim_date", "ref_columns": ["date_key"]},
            {"table": "fact_sales", "columns": ["customer_id"],
             "ref_table": "dim_customer", "ref_columns": ["customer_id"]},
            {"table": "fact_sales", "columns": ["product_id"],
             "ref_table": "dim_product", "ref_columns": ["product_id"]},
            {"table": "dim_product", "columns": ["category_id"],
             "ref_table": "dim_category", "ref_columns": ["category_id"]},
            {"table": "fact_returns", "columns": ["product_id"],
             "ref_table": "dim_product", "ref_columns": ["product_id"]},
            {"table": "fact_returns", "columns": ["return_date_key"],
             "ref_table": "dim_date", "ref_columns": ["date_key"]},
        ],
        "descriptions": {
            "fact_sales": "One row per order line",
            "fact_sales.sales_amount": "Gross line amount (quantity x unit price)",
        },
    }
    (folder / "constraints.json").write_text(json.dumps(constraints, indent=2))
    return folder


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else "data/retail"
    path = generate(out, plant_problems="--clean" not in sys.argv)
    print(f"sample data written to {path}/")
