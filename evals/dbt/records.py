"""What a DBT run writes to disk. Thin on purpose.

Redaction reuses agent.evidence.redact (the same masking the audit log and the
evidence store apply). What is added here is only what the audit log's schema
does not cover: the guide's transcript/tool-call line shapes, atomic JSON
writes for result.json and run_manifest.json, and scrubbing the API key's
value out of anything persisted.
"""

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.evidence.redact import redact_obj

MAX_LOG_FIELD = 4000
_lock = threading.Lock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _secret_values() -> list[str]:
    vals = []
    for name in ("OPENAI_API_KEY",):
        v = os.environ.get(name)
        if v and len(v) >= 8:
            vals.append(v)
    return vals


def scrub_secrets(obj: Any) -> Any:
    secrets = _secret_values()
    if not secrets:
        return obj
    if isinstance(obj, str):
        for s in secrets:
            obj = obj.replace(s, "<redacted:key>")
        return obj
    if isinstance(obj, dict):
        return {k: scrub_secrets(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [scrub_secrets(v) for v in obj]
    return obj


def trim(obj: Any, limit: int = MAX_LOG_FIELD) -> Any:
    """Truncate long strings, recording the original length."""
    if isinstance(obj, str) and len(obj) > limit:
        return obj[:limit] + f"...[+{len(obj) - limit} chars]"
    if isinstance(obj, dict):
        return {k: trim(v, limit) for k, v in obj.items()}
    if isinstance(obj, list):
        return [trim(v, limit) for v in obj]
    return obj


def append_jsonl(path: Path, row: dict, redact: bool = True) -> None:
    row = {"ts": now_iso(), **row}
    row = scrub_secrets(trim(row))
    if redact:
        row = redact_obj(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row, default=str)
    with _lock, path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def write_json_atomic(path: Path, obj: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(scrub_secrets(obj), indent=2, default=str)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def empty_result(run_id: str, experiment: str, attempt: int, instance_id: str,
                 split: str) -> dict:
    return {
        "run_id": run_id, "experiment": experiment, "attempt": attempt,
        "instance_id": instance_id, "split": split,
        "status": None, "passed": False, "failure_category": None,
        "main_turns": 0, "duration_s": 0.0, "final_dbt_exit_code": None,
        "container": {"id": "", "exit_code": None, "oom_killed": False},
        "usage_by_role": {}, "total_cost_usd": 0.0,
        "subagent_calls": {"explore": 0, "diagnose": 0, "errors": 0, "budget_stops": 0},
        "tool_calls": 0, "tool_errors": 0, "gateway_rejections": 0, "dbt_calls": 0,
        # What the injected .yml spec cost. Declared here so the field exists
        # even on an attempt that dies before staging -- the last prompt change
        # cost 70% more input tokens and nothing recorded it per attempt.
        "models_declared": 0, "spec_chars": 0,
        # Did the project's own assertions run, did they fail, did the agent
        # get a repair pass? Recorded so the gate's effect is measurable
        # separately from its cost.
        "test_repair": {"ran": False, "exit_code": None, "repaired": False},
        "error": None, "started_at": now_iso(), "finished_at": None,
    }
