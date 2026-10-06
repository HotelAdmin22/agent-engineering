"""Cards for every table and metric, and keyword search over them.

Why a catalog at all: without one the agent's first move is `SELECT *` against
a guessed table name, and its second is a different guess. The catalog turns
orientation into one cheap call instead of several refused queries.

Why keyword search rather than embeddings: with three tables and six metrics,
a vector index would be machinery with no blocked step behind it. The scoring
below is deliberately unclever, and should be replaced when a question starts
failing to find the table it needs -- not before.

Metrics are defined here rather than inferred, because "revenue" meaning
`SUM(amount) WHERE status = 'completed'` is a business fact. Leaving the model
to rediscover the status filter every time is how a report quietly reports
refunded orders as revenue.
"""

import re
from dataclasses import dataclass, field

from ..gateway import Gateway


@dataclass
class Metric:
    name: str
    expression: str
    table: str
    filters: str
    grain: str
    description: str

    @property
    def sql_hint(self) -> str:
        where = f"WHERE {self.filters}" if self.filters else ""
        return f"SELECT {self.expression} AS {self.name} FROM {self.table} {where}".strip()


METRICS = [
    Metric(
        name="revenue",
        expression="SUM(amount)",
        table="fct_orders",
        filters="status = 'completed'",
        grain="one row per order",
        description=(
            "Completed order value. Cancelled and refunded orders are excluded; "
            "including them overstates revenue."
        ),
    ),
    Metric(
        name="units",
        expression="SUM(quantity)",
        table="fct_orders",
        filters="status = 'completed'",
        grain="one row per order",
        description="Units sold on completed orders.",
    ),
    Metric(
        name="orders",
        expression="COUNT(*)",
        table="fct_orders",
        filters="status = 'completed'",
        grain="one row per order",
        description="Count of completed orders.",
    ),
    Metric(
        name="avg_unit_price",
        expression="SUM(amount) / NULLIF(SUM(quantity), 0)",
        table="fct_orders",
        filters="status = 'completed'",
        grain="one row per order",
        description=(
            "Realised price per unit. Revenue divided by units -- not AVG(unit_price), "
            "which weights a one-unit order the same as a forty-unit one."
        ),
    ),
]

# Foreign keys are declared, not guessed. A join path inferred from matching
# column names is right until two tables share a column name by coincidence,
# and then it is silently wrong.
JOINS = [
    ("fct_orders", "dim_customer", "customer_id"),
    ("fct_orders", "dim_product", "product_id"),
]


@dataclass
class Card:
    name: str
    kind: str                       # "table" | "metric"
    summary: str
    detail: dict = field(default_factory=dict)
    keywords: list[str] = field(default_factory=list)

    def text(self) -> str:
        return " ".join([self.name, self.summary, *self.keywords]).lower()


def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9_]+", text.lower()) if t]


class Catalog:
    """Built once from the gateway's schema, then queried in memory."""

    def __init__(self, gateway: Gateway):
        self.gateway = gateway
        self.cards: list[Card] = []
        self._schema = {t["table"]: t for t in gateway.describe_schema()}
        self._build()

    def _build(self) -> None:
        for table, info in self._schema.items():
            columns = [c["name"] for c in info["columns"]]
            self.cards.append(Card(
                name=table,
                kind="table",
                summary=(
                    f"{'fact' if table.startswith('fct_') else 'dimension'} table, "
                    f"{info['row_count']} rows, columns: {', '.join(columns)}"
                ),
                detail=info,
                keywords=columns + [table.replace("fct_", "").replace("dim_", "")],
            ))

        for metric in METRICS:
            self.cards.append(Card(
                name=metric.name,
                kind="metric",
                summary=metric.description,
                detail={
                    "expression": metric.expression,
                    "table": metric.table,
                    "filters": metric.filters,
                    "grain": metric.grain,
                    "sql_hint": metric.sql_hint,
                },
                keywords=[metric.table, *_tokens(metric.description)],
            ))

    # --- the three tools -------------------------------------------------

    def search(self, query: str, limit: int = 5) -> list[dict]:
        """Rank cards against a question. Returns the top `limit`."""
        wanted = set(_tokens(query))
        scored = []
        for card in self.cards:
            have = set(_tokens(card.text()))
            overlap = len(wanted & have)
            if card.name.lower() in wanted:
                overlap += 3          # an exact name beats incidental overlap
            if overlap:
                scored.append((overlap, card))
        scored.sort(key=lambda pair: (-pair[0], pair[1].name))
        # Metric cards carry their sql_hint: a metric you have to make a second
        # call to use has not actually saved the agent a turn.
        return [
            {
                "name": c.name,
                "kind": c.kind,
                "summary": c.summary,
                "score": s,
                **({"sql_hint": c.detail["sql_hint"]} if c.kind == "metric" else {}),
            }
            for s, c in scored[:limit]
        ]

    def describe_table(self, name: str) -> dict:
        info = self._schema.get(name)
        if info is None:
            return {
                "error": f"no such table: {name}",
                "available": sorted(self._schema),
            }
        return {
            "table": name,
            "row_count": info["row_count"],
            "columns": info["columns"],
            "joins": [
                {"to": b if a == name else a, "on": key}
                for a, b, key in JOINS if name in (a, b)
            ],
        }

    def get_join_path(self, left: str, right: str) -> dict:
        for a, b, key in JOINS:
            if {a, b} == {left, right}:
                return {
                    "from": left,
                    "to": right,
                    "on": key,
                    "sql": f"{left} JOIN {right} USING ({key})",
                }
        return {
            "error": f"no declared join between {left} and {right}",
            "known": [f"{a} <-> {b} on {k}" for a, b, k in JOINS],
        }
