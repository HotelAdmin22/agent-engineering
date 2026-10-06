"""Plant known anomalies in a copy of the warehouse, and record what was planted.

An eval built only on gold questions measures whether the agent can compute
what you already told it to compute. It cannot measure whether the agent would
notice something you did not ask about -- which is the whole job of "why did X
change?".

So: copy the warehouse, break three specific things, write down what was broken,
and ask the agent an open question. The answer file is the grading key and the
agent never has a tool that can reach it (the gateway is bound to the seeded
database and nothing else).

    uv run python evals/seeded/plant.py
"""

import json
import shutil
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "data" / "warehouse.duckdb"
SEEDED = ROOT / "data" / "warehouse_seeded.duckdb"
KEY = Path(__file__).resolve().parent / "planted.json"

# Each anomaly names the month it lands in, what it does, and the DISTINCTIVE
# signals an answer must contain to count as having found it.
#
# "Distinctive" is the hard-won part. The first version used generous keywords
# like "price" and "cancel", and scored a run 3/3 that had actually found two:
# "price" matched the phrase "lower-priced products" in an unrelated aside, and
# the month matched "versus July" where July was only a comparison baseline.
# A keyword that can appear by accident measures prose style, not detection.
#
# Each signal here names something that cannot be said by chance: the category,
# the region, or the product that was actually tampered with.
ANOMALIES = [
    {
        "id": "a1",
        "name": "stationery price spike",
        "month": "2026-07",
        "sql": """
            UPDATE fct_orders SET unit_price = unit_price * 3.0,
                                  amount = quantity * unit_price * 3.0
            WHERE product_id IN (7, 8)
              AND strftime(order_date, '%Y-%m') = '2026-07'
        """,
        "signals": ["stationery"],
        "expect_effect": "rate",
    },
    {
        "id": "a2",
        "name": "APAC order collapse",
        "month": "2026-08",
        "sql": """
            UPDATE fct_orders SET status = 'cancelled'
            WHERE strftime(order_date, '%Y-%m') = '2026-08'
              AND customer_id IN (SELECT customer_id FROM dim_customer
                                  WHERE region = 'APAC')
        """,
        "signals": ["apac"],
        "expect_effect": "volume",
    },
    {
        "id": "a3",
        "name": "duplicated June orders",
        "month": "2026-06",
        "sql": """
            INSERT INTO fct_orders
            SELECT order_id + 100000, order_date, customer_id, product_id,
                   quantity, unit_price, amount, status
            FROM fct_orders
            WHERE strftime(order_date, '%Y-%m') = '2026-06' AND product_id = 1
        """,
        "signals": ["duplicat", "aurora"],  # product_id 1 is Aurora Laptop
        "expect_effect": "volume",
    },
]


def main() -> None:
    if not SOURCE.exists():
        raise SystemExit(f"build the warehouse first: {SOURCE}")

    SEEDED.unlink(missing_ok=True)
    shutil.copy(SOURCE, SEEDED)

    con = duckdb.connect(str(SEEDED))        # writable: this is the planting step
    before = con.execute(
        "SELECT SUM(amount) FROM fct_orders WHERE status = 'completed'"
    ).fetchone()[0]
    for anomaly in ANOMALIES:
        con.execute(anomaly["sql"])
    after = con.execute(
        "SELECT SUM(amount) FROM fct_orders WHERE status = 'completed'"
    ).fetchone()[0]
    con.close()

    KEY.write_text(
        json.dumps(
            {
                "source": str(SOURCE.name),
                "seeded": str(SEEDED.name),
                "revenue_before": float(before),
                "revenue_after": float(after),
                "anomalies": [
                    {k: v for k, v in a.items() if k != "sql"} for a in ANOMALIES
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"wrote {SEEDED.name} with {len(ANOMALIES)} anomalies")
    print(f"  completed revenue {before:,.2f} -> {after:,.2f}")
    for a in ANOMALIES:
        print(f"  {a['id']}  {a['month']}  {a['name']}")
    print(f"\ngrading key: {KEY.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
