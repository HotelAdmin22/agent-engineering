"""Immutable evidence records. Every number the system reports comes from here.

The rule this enforces: a model may propose SQL and may describe what it means,
but it may never be the source of a digit. Each executed query and each
deterministic computation writes a record, gets an id (`e1`, `e2`, ...), and
the report refers to that id rather than restating the value.

Records are append-only. There is no update method, deliberately -- an evidence
store that can be edited after the fact proves nothing about what was run.

Named scalars live in `fields`, which is what `{{e7.delta_pct}}` resolves
against. Query records populate it with the single cell when the result is 1x1,
because "what was revenue last month" is overwhelmingly the shape asked for.
"""

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

# "segments[0].delta" -> ["segments", "0", "delta"]
_PATH_STEP = re.compile(r"[a-zA-Z_]\w*|\d+")


class EvidenceRecord(BaseModel):
    """One executed query or one deterministic computation."""

    id: str
    kind: str                      # "query" | "computation"
    created_at: str
    sql: str | None = None
    sql_hash: str | None = None
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    fields: dict[str, Any] = Field(default_factory=dict)
    note: str = ""

    def preview(self, limit: int = 10) -> dict:
        """What the model is allowed to see: never the full result set.

        A model handed every row will summarise it in prose and that prose
        becomes an un-cited number. It gets the shape, the size, and enough
        rows to reason about -- then it must cite the id.
        """
        return {
            "evidence_id": self.id,
            "columns": self.columns,
            "row_count": self.row_count,
            "rows": self.rows[:limit],
            "rows_shown": min(limit, len(self.rows)),
            "truncated": self.truncated or len(self.rows) > limit,
            "fields": self.fields,
        }


class EvidenceStore:
    """Append-only store. Hands out ids, never changes a record."""

    def __init__(self, path: Path | None = None):
        self._records: dict[str, EvidenceRecord] = {}
        self._next = 1
        self.path = Path(path) if path else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def _new_id(self) -> str:
        eid = f"e{self._next}"
        self._next += 1
        return eid

    def put_query(
        self,
        sql: str,
        columns: list[str],
        rows: list[list[Any]],
        truncated: bool = False,
        note: str = "",
    ) -> EvidenceRecord:
        fields: dict[str, Any] = {}
        # A 1x1 result is a scalar answer; expose it so a report can cite it
        # without a second computation step.
        if len(rows) == 1 and len(columns) == 1:
            fields["value"] = rows[0][0]
        if len(rows) == 1:
            fields.update({c: v for c, v in zip(columns, rows[0])})

        record = EvidenceRecord(
            id=self._new_id(),
            kind="query",
            created_at=datetime.now(timezone.utc).isoformat(),
            sql=sql,
            sql_hash=hashlib.sha256(sql.encode("utf-8")).hexdigest()[:16],
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            fields=fields,
            note=note,
        )
        return self._append(record)

    def put_computation(self, fields: dict[str, Any], note: str = "") -> EvidenceRecord:
        record = EvidenceRecord(
            id=self._new_id(),
            kind="computation",
            created_at=datetime.now(timezone.utc).isoformat(),
            fields=fields,
            note=note,
        )
        return self._append(record)

    def _append(self, record: EvidenceRecord) -> EvidenceRecord:
        self._records[record.id] = record
        if self.path:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(record.model_dump_json() + "\n")
        return record

    def get(self, evidence_id: str) -> EvidenceRecord | None:
        return self._records.get(evidence_id)

    def resolve(self, evidence_id: str, field: str) -> Any:
        """Look up one value by path. Raises if it is not there.

        `field` may be a plain name (`delta_pct`) or a path into the structure
        the tool returned (`segments[0].delta`). Contribution and mix-vs-rate
        records hold a list of segments, and a report that can only cite the
        totals cannot say which segment moved.

        Raising rather than returning None is the point: a placeholder that
        silently renders as empty is how an uncited number reaches a report.
        """
        record = self.get(evidence_id)
        if record is None:
            raise KeyError(f"no such evidence: {evidence_id}")

        current: Any = record.fields
        walked = evidence_id
        for step in _PATH_STEP.findall(field):
            walked += f".{step}" if not step.isdigit() else f"[{step}]"
            if step.isdigit():
                index = int(step)
                if not isinstance(current, (list, tuple)):
                    raise KeyError(f"{walked}: not a list")
                if index >= len(current):
                    raise KeyError(
                        f"{walked}: index out of range ({len(current)} items)"
                    )
                current = current[index]
            else:
                if not isinstance(current, dict) or step not in current:
                    available = (
                        sorted(current) if isinstance(current, dict) else type(current).__name__
                    )
                    raise KeyError(f"{walked}: no such field (has: {available})")
                current = current[step]
        return current

    def ids(self) -> list[str]:
        return list(self._records)

    def __len__(self) -> int:
        return len(self._records)
