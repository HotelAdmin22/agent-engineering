"""Phase 5 — run the agent over Spider 2.0-Lite's 135 local (SQLite) tasks.

Deliberately naive. This is the BASELINE: one read-only query tool, no schema
pre-loading, no retry policy, no workflow. Every later experiment is measured
against whatever this scores, so it must stay simple. Making it clever now
spends your improvements before you have anything to compare them against.

    uv run python -m evals.runner --limit 5
    uv run python -m evals.runner --out runs/baseline-1

Scoring is NOT done here. Predictions are written for Spider's own
evaluation_suite/evaluate.py; use their scorer, not one of your own. A
home-grown scorer that is slightly too lenient invents improvements that do
not exist.
"""

import argparse
import csv
import time
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from agent.audit import AuditLog

from manual_loop import cost as cost_model
from manual_loop import loop as claude_loop
from manual_loop import loop_openai as luna_loop
from manual_loop.loop import to_jsonable

from orchestrator.trace import ConsoleTracer

from .sandbox import Sandbox

SPIDER_DIR = Path(__file__).resolve().parent / "spider"
TASKS = SPIDER_DIR / "tasks.jsonl"
DATABASES = SPIDER_DIR / "databases"
DOCUMENTS = SPIDER_DIR / "documents"

SYSTEM = """You are answering a question about a SQLite database.

Use the query_database tool to explore the schema and compute the answer. You \
cannot see the database any other way, so start by inspecting it.

When you have the answer, reply with your final SQL in a ```sql fenced block. \
That single query must produce the answer on its own."""

SQL_BLOCK = re.compile(r"```sql\s*(.*?)```", re.S | re.I)


def load_tasks() -> list[dict]:
    if not TASKS.exists():
        sys.exit(f"missing {TASKS} -- see evals/spider/README.md")
    return [
        json.loads(l) for l in TASKS.read_text(encoding="utf-8").splitlines() if l.strip()
    ]


def build_prompt(task: dict) -> str:
    """Question, plus the external knowledge document when the task cites one.

    The document is part of the task -- 13 of the 135 reference one and cannot
    be answered without it. Gold SQL and gold results are NOT here, and are not
    reachable by any tool the agent has.
    """
    parts = [task["question"]]
    doc_name = task.get("external_knowledge")
    if doc_name:
        doc = DOCUMENTS / doc_name
        if doc.exists():
            parts.append(
                f"\n--- Reference document: {doc_name} ---\n"
                f"{doc.read_text(encoding='utf-8')}"
            )
        else:
            parts.append(f"\n(Reference document {doc_name} is missing.)")
    return "\n".join(parts)


def extract_sql(text: str | None) -> str | None:
    if not text:
        return None
    blocks = SQL_BLOCK.findall(text)
    return blocks[-1].strip() if blocks else None


WORKERS = {"luna": luna_loop, "claude": claude_loop}

# Cap on what a single event contributes to the log. A full result set in an
# audit line makes the file unreadable and leaks more than it needs to.
AUDIT_EVENT_CHARS = 600


def _recorder(audit: AuditLog, tracer=None):
    """Feed loop events to the audit log, and optionally to the console.

    The loop emits tool_call BEFORE the tool runs and tool_result after, so a
    query that hangs leaves a tool_call with no matching result -- which names
    the exact SQL that died. The previous version wrote nothing until the task
    finished, so a 26-minute hang left an empty directory.
    """

    def on_event(event: dict) -> None:
        kind = event.get("kind", "event")
        fields = {
            k: (v[:AUDIT_EVENT_CHARS] if isinstance(v, str) else v)
            for k, v in event.items()
            if k != "kind"
        }
        audit.event(kind, **fields)
        if tracer:
            tracer(event)

    return on_event




def run_task(task: dict, out_dir: Path, worker: str = "luna", tracer=None) -> dict:
    """Run one task. Returns a summary row; writes transcript + result csv."""
    db_path = DATABASES / f"{task['db']}.sqlite"
    instance = task["instance_id"]
    row: dict = {"instance_id": instance, "db": task["db"]}

    if not db_path.exists():
        row |= {"status": "missing_db", "num_turns": 0, "has_sql": False}
        return row

    sandbox = Sandbox(db_path)

    # Written per event, before the task finishes, so an interrupted task is
    # still diagnosable.
    inst_dir = out_dir / instance
    inst_dir.mkdir(parents=True, exist_ok=True)
    audit = AuditLog(inst_dir / "audit.jsonl", instance)
    audit.event("task", db=task["db"], question=task["question"][:600])

    result = WORKERS[worker].run_loop(
        prompt=build_prompt(task),
        tools=sandbox.tools,
        execute=sandbox.execute,
        system=SYSTEM,
        trace=_recorder(audit, tracer),
    )

    sql = extract_sql(result.final_text)
    row |= {
        "status": result.stopped_because,
        "num_turns": result.num_turns,
        "has_sql": bool(sql),
        "usage": result.usage_totals,
    }

    (inst_dir / "transcript.json").write_text(
        json.dumps(
            {"task": task, "run": to_jsonable(result), "sql": sql}, indent=2, default=str
        ),
        encoding="utf-8",
    )

    # Execute the predicted SQL through the same read-only sandbox and save the
    # rows: Spider compares executed results, not SQL text.
    if sql:
        try:
            con = sandbox._connect()
            cur = con.execute(sql)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchall()
            con.close()
            with (inst_dir / "result.csv").open("w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                if cols:
                    writer.writerow(cols)
                writer.writerows(rows)
            row["rows"] = len(rows)
        except Exception as exc:
            row |= {"status": "sql_failed", "error": f"{type(exc).__name__}: {exc}"}

    return row


def main() -> None:
    # This module is run as `-m evals.runner`, so nothing else has loaded the
    # credentials for it.
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")

    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, help="run only the first N tasks")
    ap.add_argument("--out", type=Path, help="output directory")
    # Default luna: it is what authenticates here, and it is cheap enough to
    # run 135 tasks against without thinking about it.
    ap.add_argument("--worker", choices=sorted(WORKERS), default="luna")
    # A 135-task sweep is where cost actually accrues. Bound the sweep, not
    # the single run -- the token budget already bounds that.
    ap.add_argument("--max-usd", type=float, help="stop the sweep past this spend")
    ap.add_argument("--max-tokens", type=int, help="stop the sweep past this many tokens")
    ap.add_argument("--trace", action="store_true",
                    help="print every tool call as it happens")
    ap.add_argument("--resume", action="store_true",
                    help="skip tasks already completed in --out")
    args = ap.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_dir = args.out or Path("runs") / stamp
    out_dir.mkdir(parents=True, exist_ok=True)

    tracer = ConsoleTracer('spider') if args.trace else None

    tasks = load_tasks()
    if args.limit:
        tasks = tasks[: args.limit]
    print(f"running {len(tasks)} tasks -> {out_dir}\n")

    summary = []
    spent_tokens = 0
    spent_usd = 0.0
    model = WORKERS[args.worker].MODEL

    # Append per task, not once at the end. A sweep of 135 tasks takes hours;
    # an interrupted one that loses its completed work is worse than useless,
    # because the tokens were already spent. Verified the hard way.
    summary_path = out_dir / "summary.jsonl"

    for i, task in enumerate(tasks, 1):
        done = out_dir / task["instance_id"] / "transcript.json"
        if args.resume and done.exists():
            print(f"[{i}/{len(tasks)}] {task['instance_id']:<10} skipped (already done)")
            continue

        # Announce BEFORE running. Printing only on completion means a hang
        # is indistinguishable from work -- one task sat for 26 minutes and
        # the console showed nothing at all.
        print(f"[{i}/{len(tasks)}] {task['instance_id']:<10} {task['db']:<28} "
              f"running...", flush=True)
        started = time.monotonic()
        row = run_task(task, out_dir, args.worker, tracer)
        row["seconds"] = round(time.monotonic() - started, 1)
        summary.append(row)
        with summary_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + '\n')

        usage = row.get("usage") or {}
        spent_tokens += usage.get("total_tokens", 0) or 0
        spent_usd += cost_model.estimate(model, usage) or 0.0
        row["usd"] = cost_model.estimate(model, usage)

        over_tokens = args.max_tokens and spent_tokens >= args.max_tokens
        over_usd = args.max_usd and spent_usd >= args.max_usd
        if over_tokens or over_usd:
            print(f"\n  budget reached after {i} task(s): "
                  f"{spent_tokens:,} tokens, ${spent_usd:.4f}")
            break
        print(
            f"[{i}/{len(tasks)}] {row['instance_id']:<10} "
            f"{row['status']:<14} turns={row.get('num_turns', 0):<3} "
            f"sql={'y' if row.get('has_sql') else 'n'} "
            f"{row['seconds']}s",
            flush=True,
        )

    (out_dir / "summary.jsonl").write_text(
        "\n".join(json.dumps(r, default=str) for r in summary) + "\n", encoding="utf-8"
    )

    done = sum(1 for r in summary if r.get("has_sql"))
    turns = [r.get("num_turns", 0) for r in summary]
    print(f"\nproduced SQL for {done}/{len(summary)} tasks")
    if turns:
        print(f"mean turns: {sum(turns) / len(turns):.1f}")
    print(f"\nNo score yet -- run Spider's evaluate.py over {out_dir}")


if __name__ == "__main__":
    main()
