"""Append-only JSONL log of everything an agent did, with timings.

Separate from the evidence store on purpose. Evidence answers "where did this
number come from"; the audit log answers "what did the system do", including
the things that produced no evidence at all -- refused SQL, failed tools, the
exact prompt text.

## The schema

One line per event. Short keys, no nulls, no empty strings -- a log you have to
scroll past is a log nobody reads.

    ts      ISO timestamp
    run     the sweep or session id       <- ties every task together
    task    the unit within it            <- omitted for single runs
    seq     monotonic event number        <- orders events within a task
    t_ms    milliseconds since run start  <- where the time went
    kind    task | turn | tool_call | tool_result | note | stop
    tool    tool name, on call and result alike
    dur_ms  how long the tool took        <- only on tool_result
    ok      false only when it failed

Three of those exist because of specific failures:

  `dur_ms` -- a query hung for 26 minutes and nothing recorded how long any
  tool call took, so the hang was invisible until someone checked the process
  table.

  `run` + `task` -- the Spider log set run_id to the TASK id, so a 15-task
  sweep produced 15 logs with no field tying them together.

  dropping empties -- the old format wrote `"text": ""` on every turn and
  `"error": null, "stage": null, "traceback": null` on every result.

Append-only and flushed per line, so a run that crashes still leaves a readable
log up to the moment it died.
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..evidence.redact import redact_obj

# Beyond this, a payload is truncated and its full length recorded. A result
# set pasted into a log makes the file unreadable and leaks more than needed.
MAX_FIELD_CHARS = 400


def _trim(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_FIELD_CHARS:
        return value[:MAX_FIELD_CHARS] + f"...[+{len(value) - MAX_FIELD_CHARS}]"
    return value


def _meaningful(value: Any) -> bool:
    """Drop None, empty strings and empty containers. Keep 0 and False."""
    if value is None:
        return False
    if isinstance(value, (str, list, dict, tuple)) and len(value) == 0:
        return False
    return True


class AuditLog:
    def __init__(
        self,
        path: Path | str,
        run: str,
        task: str | None = None,
        redact: bool = True,
    ):
        self.path = Path(path)
        self.run = run
        self.task = task
        self.redact = redact
        self._seq = 0
        self._started = time.monotonic()
        self._pending: dict[str, float] = {}   # tool -> call time, for dur_ms
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # --- writing ---------------------------------------------------------

    def event(self, kind: str, **fields: Any) -> None:
        self._seq += 1

        # A tool_result is timed against the matching tool_call. Pair on
        # call_id, not tool name: a turn can fire three query_database calls at
        # once, and keying on the name means two of the three lose their timing
        # and the third gets the wrong one.
        tool = fields.get("tool") or fields.get("name")
        key = str(fields.get("call_id") or tool or "")
        if kind == "tool_call" and key:
            self._pending[key] = time.monotonic()
        duration = None
        if kind == "tool_result" and key in self._pending:
            duration = round((time.monotonic() - self._pending.pop(key)) * 1000)

        payload = {k: _trim(v) for k, v in fields.items() if _meaningful(v)}
        payload.pop("name", None)          # normalised to `tool`

        row: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "run": self.run,
            "seq": self._seq,
            "t_ms": round((time.monotonic() - self._started) * 1000),
            "kind": kind,
        }
        if self.task:
            row["task"] = self.task
        if tool:
            row["tool"] = str(tool)
        if duration is not None:
            row["dur_ms"] = duration

        # SQL literals and tool arguments carry warehouse values.
        row.update(redact_obj(payload) if self.redact else payload)

        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")

    # --- reading ---------------------------------------------------------

    def rows(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [
            json.loads(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def slowest(self, limit: int = 5) -> list[dict]:
        """The tool calls that took longest. The first thing to look at."""
        timed = [r for r in self.rows() if r.get("dur_ms") is not None]
        return sorted(timed, key=lambda r: -r["dur_ms"])[:limit]
