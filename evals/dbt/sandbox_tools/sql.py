"""query_duckdb: a thin adapter over agent.gateway. Imports no duckdb.

The gateway does every check (one SELECT, read-only, no external access,
locked configuration, injected LIMIT, watchdog timeout). This module only
finds the task's database, shapes the result for the model, and closes the
handle before returning -- the next tool call may be a `dbt run`, which needs
the write lock.
"""

import hashlib

from agent.evidence import EvidenceStore
from agent.gateway import Gateway

from .path_guard import workspace

MAX_CELL_CHARS = 200


def find_database():
    dbs = sorted(workspace().glob("*.duckdb"))
    if len(dbs) != 1:
        return None, f"expected one .duckdb in the workspace root, found {len(dbs)}"
    return dbs[0], ""


def _cell(value):
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    text = str(value)
    return text if len(text) <= MAX_CELL_CHARS else text[:MAX_CELL_CHARS] + "..."


def _types(columns, rows):
    """Python-side types of the first non-null value per column.

    The gateway returns rows, not the engine's column types; this is a hint
    for the model, not a schema. DESCRIBE is not a SELECT and is refused.
    """
    out = []
    for i, _ in enumerate(columns):
        sample = next((r[i] for r in rows if r[i] is not None), None)
        out.append(type(sample).__name__ if sample is not None else "unknown")
    return out


def query_duckdb(sql: str, max_rows: int = 20, timeout_s: float = 30.0) -> dict:
    db, problem = find_database()
    if db is None:
        return {"error": problem}
    sql_hash = hashlib.sha256((sql or "").encode()).hexdigest()[:16]

    gateway = Gateway(db, store=EvidenceStore(), max_rows=max_rows, timeout=timeout_s)
    try:
        decision, record = gateway.run_sql(sql or "")
        if not decision.allowed:
            return {"error": decision.reason, "stage": decision.stage,
                    "rejected": True, "sql_hash": sql_hash}

        total = None
        if record.truncated:
            # The row count past the cap, through the same gateway. Refused or
            # failed counts are left unknown rather than guessed.
            count_decision, count_record = gateway.run_sql(
                f"SELECT COUNT(*) AS n FROM ({sql.strip().rstrip(';')}) AS q"
            )
            if count_decision.allowed and count_record and count_record.rows:
                total = count_record.rows[0][0]
        rows = [[_cell(v) for v in r] for r in record.rows]
        return {
            "columns": record.columns,
            "types": _types(record.columns, record.rows),
            "row_count": record.row_count if not record.truncated else total,
            "rows": rows,
            "truncated": record.truncated,
            "sql_hash": sql_hash,
        }
    finally:
        gateway.close()
