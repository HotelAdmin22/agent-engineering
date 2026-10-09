"""Where a DBT run has got to, from the run directory alone.

The runner writes its summary only at the end, so a run in flight has no
single file to read. This counts result.json files instead -- which is what
the runner has already committed to disk -- and so it is safe to call at any
point, including while the run is still writing.

    uv run python -m evals.dbt.scripts.progress              # newest run
    uv run python -m evals.dbt.scripts.progress luna-2026...  # a named one

Deliberately read-only: it opens no database, imports no gateway, and never
touches runs/dbt/**/scoring/.
"""

import json
import sys
from collections import Counter
from pathlib import Path

RUNS = Path("runs/dbt")


def newest_run() -> Path:
    candidates = [p for p in RUNS.iterdir() if p.is_dir() and not p.name.startswith("_")]
    if not candidates:
        raise SystemExit(f"no runs under {RUNS}")
    return max(candidates, key=lambda p: p.stat().st_mtime)


def main(argv: list[str]) -> int:
    run = RUNS / argv[0] if argv else newest_run()
    if not run.is_dir():
        raise SystemExit(f"no such run: {run}")

    attempts: dict[str, list[dict]] = {}
    for result in sorted(run.glob("attempt_*/*/result.json")):
        try:
            data = json.loads(result.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue  # being written right now; it will be there next time
        attempts.setdefault(result.parent.parent.name, []).append(data)

    if not attempts:
        print(f"{run.name}: no finished attempts yet")
        return 0

    print(f"{run.name}")
    passed_by_task: dict[str, int] = {}
    total_cost = 0.0

    for name in sorted(attempts):
        rows = attempts[name]
        passed = sum(1 for r in rows if r.get("passed"))
        cost = sum(r.get("total_cost_usd") or 0.0 for r in rows)
        total_cost += cost
        status = Counter(r.get("status") for r in rows)
        odd = ", ".join(f"{k} {v}" for k, v in status.items() if k != "completed")
        print(
            f"  {name}: {passed:3d}/{len(rows):3d} passed  ({passed / len(rows):5.1%})"
            f"  ${cost:.2f}" + (f"  [{odd}]" if odd else "")
        )
        for r in rows:
            if r.get("passed"):
                task = r.get("instance_id") or "?"
                passed_by_task[task] = passed_by_task.get(task, 0) + 1

    done = sum(len(v) for v in attempts.values())
    print(f"  {done} attempt(s) scored, ${total_cost:.2f} so far")

    if len(attempts) > 1:
        # NOT pass^k: the later attempts are still being written, so a task
        # counted here has passed every attempt that has finished, not every
        # attempt there will be. The real figure comes from summary.json.
        every = sum(1 for n in passed_by_task.values() if n == len(attempts))
        print(
            f"  {len(passed_by_task)} task(s) passed at least once;"
            f" {every} in all {len(attempts)} attempts started so far"
        )

    # The leading indicator for the gateway change: these should be near zero.
    errors: Counter = Counter()
    for calls in run.glob("attempt_*/*/tool_calls.jsonl"):
        try:
            lines = calls.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                call = json.loads(line)
            except json.JSONDecodeError:
                continue
            errors["calls"] += 1
            if call.get("status") == "ok":
                continue
            errors["failed"] += 1
            reason = (call.get("error") or "").strip()
            sql = ((call.get("arguments") or {}).get("sql") or "").lower()
            if "information_schema" in sql or "sqlite_master" in sql:
                errors["catalog reads refused"] += 1
            elif "does not exist" in reason and "Did you mean" in reason:
                errors["schema qualification"] += 1

    if errors["calls"]:
        rate = errors["failed"] / errors["calls"]
        print(
            f"  {errors['calls']} tool calls, {errors['failed']} failed ({rate:.1%})"
            f" -- catalog refused {errors['catalog reads refused']},"
            f" qualification {errors['schema qualification']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
