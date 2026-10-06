"""Collect every failure across every run, grouped by signature.

Separated from docs/build_failures.py because two things need this: the HTML
report a human reads, and the triage agent that proposes fixes. A collector
living inside a renderer can only ever serve the renderer.
"""

import json
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "runs"


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _tool_of(row: dict) -> str:
    """Audit rows come from two producers with slightly different shapes.

    The Planner logs `tool`; the Spider runner logs loop trace events, which
    name it `name`. A collector that assumes one shape crashes on the other --
    which it did, taking 13 tests with it.
    """
    return str(row.get("tool") or row.get("name") or "unknown")


def _signature(row: dict) -> str:
    """Group key. Specific enough to be actionable, general enough to cluster."""
    tool = _tool_of(row)
    if row.get("traceback"):
        error = (row.get("error") or "").split("\n")[0]
        return f"{tool} :: {error[:90]}"
    stage = row.get("stage") or "?"
    reason = (row.get("error") or row.get("content") or "").split(":")[0][:70]
    return f"{tool} :: [{stage}] {reason}"


def collect() -> dict:
    crashes: dict[str, list[dict]] = defaultdict(list)
    refusals: dict[str, list[dict]] = defaultdict(list)
    incorrect: list[dict] = []
    unfinished: list[dict] = []
    tool_calls = Counter()
    tool_errors = Counter()
    runs_seen = set()

    for audit in sorted(RUNS.glob("**/audit.jsonl")):
        runs_seen.add(audit.parent.name)
        for row in _read_jsonl(audit):
            if row.get("kind") == "tool_call":
                tool_calls[_tool_of(row)] += 1
            if row.get("kind") != "tool_result" or not row.get("is_error"):
                continue
            tool_errors[_tool_of(row)] += 1
            row["_run"] = audit.parent.name
            (crashes if row.get("traceback") else refusals)[_signature(row)].append(row)

    for summary in sorted(RUNS.glob("**/runs.jsonl")):
        for row in _read_jsonl(summary):
            if not row.get("correct"):
                row["_run"] = summary.parent.name
                incorrect.append(row)

    for answer in sorted(RUNS.glob("**/answer.json")):
        try:
            data = json.loads(answer.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not data.get("answer", "").strip() or data.get("ok") is False:
            findings = [
                f for f in data.get("findings", []) if f.get("verdict") == "FAIL"
            ]
            unfinished.append({
                "run": answer.parent.name,
                "question": data.get("question", ""),
                "steps": data.get("steps"),
                "empty": not data.get("answer", "").strip(),
                "findings": findings,
            })

    return {
        "crashes": crashes,
        "refusals": refusals,
        "incorrect": incorrect,
        "unfinished": unfinished,
        "tool_calls": tool_calls,
        "tool_errors": tool_errors,
        "runs": len(runs_seen),
    }


