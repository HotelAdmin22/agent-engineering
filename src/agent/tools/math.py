"""Deterministic arithmetic. The model chooses what to compute, never what the answer is.

Every function here takes evidence ids and column names -- never literal
numbers. That is the whole design. A tool signature like
`compute_delta(current=52270, previous=22320)` would let the model type a digit
into an argument, and a mistyped digit is indistinguishable from a correct one
once it is inside a result. Taking ids means the inputs are whatever the
warehouse actually returned.

Each function writes its own computation evidence, so a report can cite
`{{e9.delta_pct}}` and the verifier can trace that number back to the two
queries it came from.
"""

from typing import Any

from ..evidence import EvidenceRecord, EvidenceStore


def _cell(store: EvidenceStore, evidence_id: str, column: str) -> float:
    """One scalar out of a single-row evidence record."""
    record = store.get(evidence_id)
    if record is None:
        raise KeyError(f"no such evidence: {evidence_id}")
    if record.row_count != 1:
        raise ValueError(
            f"{evidence_id} has {record.row_count} rows; a scalar needs exactly 1"
        )
    if column not in record.columns:
        raise KeyError(f"{evidence_id} has no column {column!r}: {record.columns}")
    return float(record.rows[0][record.columns.index(column)])


def _table(
    store: EvidenceStore, evidence_id: str, *columns: str
) -> list[tuple]:
    record = store.get(evidence_id)
    if record is None:
        raise KeyError(f"no such evidence: {evidence_id}")
    missing = [c for c in columns if c not in record.columns]
    if missing:
        raise KeyError(f"{evidence_id} has no column(s) {missing}: {record.columns}")
    idx = [record.columns.index(c) for c in columns]
    return [tuple(row[i] for i in idx) for row in record.rows]


def compute_delta(
    store: EvidenceStore,
    current_id: str,
    previous_id: str,
    column: str,
) -> EvidenceRecord:
    """Absolute and percentage change between two single-row results."""
    current = _cell(store, current_id, column)
    previous = _cell(store, previous_id, column)
    delta = current - previous
    return store.put_computation(
        fields={
            "current": round(current, 2),
            "previous": round(previous, 2),
            "delta": round(delta, 2),
            "delta_pct": round(delta / previous * 100, 2) if previous else None,
            "direction": "up" if delta > 0 else "down" if delta < 0 else "flat",
            "column": column,
        },
        note=f"delta of {column} between {current_id} and {previous_id}",
    )


def compute_contribution(
    store: EvidenceStore,
    current_id: str,
    previous_id: str,
    label_column: str,
    value_column: str,
) -> EvidenceRecord:
    """How much each segment contributed to the total change.

    Contributions sum to the total change by construction, which is what the
    verifier's reconciliation check later relies on.
    """
    current = dict(_table(store, current_id, label_column, value_column))
    previous = dict(_table(store, previous_id, label_column, value_column))

    rows = []
    for label in sorted(set(current) | set(previous)):
        now = float(current.get(label) or 0)
        before = float(previous.get(label) or 0)
        rows.append({
            "label": label,
            "current": round(now, 2),
            "previous": round(before, 2),
            "delta": round(now - before, 2),
        })

    total_delta = sum(r["delta"] for r in rows)
    for row in rows:
        row["share_of_change_pct"] = (
            round(row["delta"] / total_delta * 100, 2) if total_delta else None
        )

    rows.sort(key=lambda r: abs(r["delta"]), reverse=True)
    return store.put_computation(
        fields={
            "total_delta": round(total_delta, 2),
            "segments": rows,
            "largest_mover": rows[0]["label"] if rows else None,
            "largest_mover_delta": rows[0]["delta"] if rows else None,
            "label_column": label_column,
            "value_column": value_column,
        },
        note=f"contribution by {label_column} between {previous_id} and {current_id}",
    )


def mix_vs_rate(
    store: EvidenceStore,
    current_id: str,
    previous_id: str,
    label_column: str,
    units_column: str,
    revenue_column: str,
) -> EvidenceRecord:
    """Split a revenue change into volume, rate and interaction effects.

    revenue = units x price, so for each segment:

        volume      = (u1 - u0) * p0      sold a different amount at the old price
        rate        = (p1 - p0) * u0      sold the old amount at a different price
        interaction = (u1 - u0) * (p1 - p0)

    The three sum exactly to the revenue change. That identity is the point:
    "revenue fell because volume fell" is a claim that can be checked, unlike
    "revenue fell because demand softened".
    """
    cur = {
        label: (float(units), float(rev))
        for label, units, rev in _table(
            store, current_id, label_column, units_column, revenue_column
        )
    }
    prev = {
        label: (float(units), float(rev))
        for label, units, rev in _table(
            store, previous_id, label_column, units_column, revenue_column
        )
    }

    rows = []
    for label in sorted(set(cur) | set(prev)):
        u1, r1 = cur.get(label, (0.0, 0.0))
        u0, r0 = prev.get(label, (0.0, 0.0))
        p1 = r1 / u1 if u1 else 0.0
        p0 = r0 / u0 if u0 else 0.0

        volume = (u1 - u0) * p0
        rate = (p1 - p0) * u0
        interaction = (u1 - u0) * (p1 - p0)

        rows.append({
            "label": label,
            "revenue_delta": round(r1 - r0, 2),
            "volume_effect": round(volume, 2),
            "rate_effect": round(rate, 2),
            "interaction_effect": round(interaction, 2),
            "units_before": u0,
            "units_after": u1,
            "price_before": round(p0, 2),
            "price_after": round(p1, 2),
            "driver": max(
                (("volume", abs(volume)), ("rate", abs(rate)),
                 ("interaction", abs(interaction))),
                key=lambda pair: pair[1],
            )[0],
        })

    rows.sort(key=lambda r: abs(r["revenue_delta"]), reverse=True)
    totals = {
        "total_revenue_delta": round(sum(r["revenue_delta"] for r in rows), 2),
        "total_volume_effect": round(sum(r["volume_effect"] for r in rows), 2),
        "total_rate_effect": round(sum(r["rate_effect"] for r in rows), 2),
        "total_interaction_effect": round(
            sum(r["interaction_effect"] for r in rows), 2
        ),
    }
    return store.put_computation(
        fields={**totals, "segments": rows,
                "dominant_effect": max(
                    (("volume", abs(totals["total_volume_effect"])),
                     ("rate", abs(totals["total_rate_effect"])),
                     ("interaction", abs(totals["total_interaction_effect"]))),
                    key=lambda pair: pair[1],
                )[0]},
        note=f"mix vs rate by {label_column} between {previous_id} and {current_id}",
    )


def summarise(record: EvidenceRecord) -> dict[str, Any]:
    """What a computation hands back to the model: the id and the fields."""
    return {"evidence_id": record.id, "fields": record.fields}
