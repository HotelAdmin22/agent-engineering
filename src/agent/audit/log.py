"""Append-only JSONL log of every prompt and every tool call.

Separate from the evidence store on purpose. Evidence answers "where did this
number come from"; the audit log answers "what did the system do", including
the things that produced no evidence at all -- refused SQL, failed tools, the
exact prompt text. A refusal leaves no evidence record by design, so without
this there is no trace that the agent tried.

Append-only and flushed per line, so a run that crashes still leaves a readable
log up to the moment it died.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class AuditLog:
    def __init__(self, path: Path | str, run_id: str):
        self.path = Path(path)
        self.run_id = run_id
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def event(self, kind: str, **fields: Any) -> None:
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id,
            "kind": kind,
            **fields,
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")

    def rows(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [
            json.loads(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
