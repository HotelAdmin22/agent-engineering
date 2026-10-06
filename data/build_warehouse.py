"""Build data/warehouse.duckdb -- a small retail warehouse with a real story in it.

The guide assumes a DuckDB file reused from Spider2-DBT. That dataset was never
downloaded here (evals/spider/databases/ is empty), so this generates one
instead.

It is not random noise. Revenue falls in the final month for two separate and
separable reasons, so "why did revenue change?" has a true answer that a
mix-versus-rate decomposition can actually recover:

  - Electronics: unit volume collapses (a VOLUME effect)
  - Apparel:     unit price rises, volume roughly flat (a RATE effect)

Everything is seeded, so the numbers are identical on every rebuild and an eval
written against them stays valid.

    uv run python data/build_warehouse.py
"""

import random
from datetime import date, timedelta
from pathlib import Path

import duckdb

OUT = Path(__file__).resolve().parent / "warehouse.duckdb"
SEED = 20261006

REGIONS = ["EMEA", "AMER", "APAC"]
SEGMENTS = ["enterprise", "mid-market", "smb"]

PRODUCTS = [
    (1, "Aurora Laptop",    "electronics", 1200.00),
    (2, "Nimbus Monitor",   "electronics",  340.00),
    (3, "Vertex Keyboard",  "electronics",   90.00),
    (4, "Harbour Jacket",   "apparel",      180.00),
    (5, "Meridian Boots",   "apparel",      150.00),
    (6, "Cobalt Scarf",     "apparel",       45.00),
    (7, "Fathom Notebook",  "stationery",     12.00),
    (8, "Quill Pen Set",    "stationery",     28.00),
]

MONTHS = [date(2026, m, 1) for m in range(4, 10)]  # Apr..Sep 2026
LAST = MONTHS[-1]

SCHEMA = """
CREATE TABLE dim_customer (
    customer_id INTEGER PRIMARY KEY,
    customer_name TEXT NOT NULL,
    segment TEXT NOT NULL,
    region TEXT NOT NULL,
    signed_up DATE NOT NULL
);

CREATE TABLE dim_product (
    product_id INTEGER PRIMARY KEY,
    product_name TEXT NOT NULL,
    category TEXT NOT NULL,
    list_price DECIMAL(10,2) NOT NULL
);

CREATE TABLE fct_orders (
    order_id INTEGER PRIMARY KEY,
    order_date DATE NOT NULL,
    customer_id INTEGER NOT NULL,
    product_id INTEGER NOT NULL,
    quantity INTEGER NOT NULL,
    unit_price DECIMAL(10,2) NOT NULL,
    amount DECIMAL(12,2) NOT NULL,
    status TEXT NOT NULL
);
"""


def _volume(category: str, month: date, rng: random.Random) -> int:
    """Orders per category per month. The last month is where the story is."""
    base = {"electronics": 46, "apparel": 38, "stationery": 30}[category]
    if month == LAST and category == "electronics":
        base = int(base * 0.45)          # the volume collapse
    return base + rng.randint(-3, 3)


def _price_factor(category: str, month: date) -> float:
    """Unit price multiplier. Apparel reprices in the last month."""
    if month == LAST and category == "apparel":
        return 1.35                       # the rate effect
    return 1.0


def main() -> None:
    rng = random.Random(SEED)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.unlink(missing_ok=True)

    customers = []
    for cid in range(1, 61):
        customers.append((
            cid,
            f"Customer {cid:03d}",
            SEGMENTS[cid % len(SEGMENTS)],
            REGIONS[cid % len(REGIONS)],
            date(2025, 1, 1) + timedelta(days=rng.randint(0, 500)),
        ))

    orders = []
    oid = 1
    for month in MONTHS:
        for pid, _name, category, list_price in PRODUCTS:
            n = _volume(category, month, rng) // 3  # per product within category
            for _ in range(n):
                day = month + timedelta(days=rng.randint(0, 27))
                qty = rng.randint(1, 4)
                price = round(list_price * _price_factor(category, month), 2)
                # A small share never completes. Analyses that forget to filter
                # on status get a different, wrong answer -- deliberately.
                status = "completed" if rng.random() > 0.12 else rng.choice(
                    ["cancelled", "refunded"]
                )
                orders.append((
                    oid, day, rng.randint(1, 60), pid, qty, price,
                    round(qty * price, 2), status,
                ))
                oid += 1

    con = duckdb.connect(str(OUT))
    con.execute(SCHEMA)
    con.executemany("INSERT INTO dim_customer VALUES (?, ?, ?, ?, ?)", customers)
    con.executemany("INSERT INTO dim_product VALUES (?, ?, ?, ?)", PRODUCTS)
    con.executemany(
        "INSERT INTO fct_orders VALUES (?, ?, ?, ?, ?, ?, ?, ?)", orders
    )

    summary = con.execute("""
        SELECT strftime(o.order_date, '%Y-%m') AS month,
               p.category,
               SUM(o.amount) AS revenue,
               SUM(o.quantity) AS units
        FROM fct_orders o JOIN dim_product p USING (product_id)
        WHERE o.status = 'completed'
        GROUP BY 1, 2 ORDER BY 1, 2
    """).fetchall()
    con.close()

    print(f"wrote {OUT}")
    print(f"  {len(customers)} customers, {len(PRODUCTS)} products, {len(orders)} orders")
    print("\n  month    category      revenue     units")
    for month, cat, rev, units in summary:
        print(f"  {month}  {cat:<12} {rev:>10,.2f} {units:>9}")


if __name__ == "__main__":
    main()
