"""Run the data agent over Spider 2.0-Lite's 135 local (SQLite) tasks.

One agent. This used to be a second one: its own loop, its own single-tool
sandbox, its own prompt, and no evidence store -- a parallel stack that shared
nothing with the warehouse agent but the HTTP call. Spider tasks now go through
the same Planner, the same gateway, and the same deterministic verifier, in
`sql` mode rather than `prose` mode.

What that buys beyond less code: the `sql_was_executed` check. A model that
explores with five queries and then writes a sixth, clean, never-run one in its
final answer used to have that answer scored as if it were tested. Now it is
rejected before it reaches the scorer.

Scoring is NOT done here. Predictions are written for Spider's own
evaluation_suite/evaluate.py; use their scorer, not one of your own. A
home-grown scorer that is slightly too lenient invents improvements that do not
exist.

    uv run python -m evals.runner --limit 5
    uv run python -m evals.runner --resume --max-tokens 400000
"""

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent.agent import Planner          # noqa: E402
from manual_loop import cost as cost_model  # noqa: E402
from orchestrator.trace import ConsoleTracer  # noqa: E402

SPIDER_DIR = Path(__file__).resolve().parent / "spider"
TASKS = SPIDER_DIR / "tasks.jsonl"
DATABASES = SPIDER_DIR / "databases"
DOCUMENTS = SPIDER_DIR / "documents"

# Spider schemas are unfamiliar, so orientation costs turns that a warehouse
# question does not. 15 was the old runner's ceiling and local015 hit it.
MAX_STEPS = 20


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
        parts.append(
            f"\n--- Reference document: {doc_name} ---\n"
            + (doc.read_text(encoding="utf-8") if doc.exists()
               else f"(Reference document {doc_name} is missing.)")
        )
    return "\n".join(parts)


def write_prediction(run, planner, inst_dir: Path) -> int | None:
    """Spider compares executed rows, so write the rows the agent actually got.

    Taken from the evidence record the verifier matched, not re-executed: the
    point of `sql_was_executed` is that this query already ran, and running it
    again would be a second chance to get a different answer.
    """
    evidence_id = next(
        (f.evidence_id for f in run.verification.findings
         if f.check == "sql_was_executed" and f.evidence_id),
        None,
    )
    if evidence_id is None:
        return None
    record = planner.store.get(evidence_id)
    if record is None:
        return None
    with (inst_dir / "result.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if record.columns:
            writer.writerow(record.columns)
        writer.writerows(record.rows)
    return record.row_count


def run_task(task: dict, out_dir: Path, run_id: str, tracer=None,
             max_steps: int = MAX_STEPS, timeout: float | None = None) -> dict:
    instance = task["instance_id"]
    db_path = DATABASES / f"{task['db']}.sqlite"
    row: dict = {"instance_id": instance, "db": task["db"]}

    if not db_path.exists():
        return row | {"status": "missing_db", "num_turns": 0, "has_sql": False}

    planner = Planner(
        warehouse=db_path,
        gateway_timeout=timeout,
        run_dir=out_dir / instance,
        run_id=run_id,
        mode="sql",
    )
    try:
        run = planner.answer(build_prompt(task), max_steps=max_steps, trace=tracer)
    finally:
        planner.gateway.close()

    row |= {
        "status": run.stopped_because,
        "num_turns": run.steps,
        "has_sql": bool(run.final_sql),
        "verified": run.ok,
        "usage": run.usage,
        "findings": {f.check: f.verdict for f in run.verification.findings},
    }
    rows = write_prediction(run, planner, out_dir / instance)
    if rows is not None:
        row["rows"] = rows
    return row


def main() -> None:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")

    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, help="run only the first N tasks")
    ap.add_argument("--out", type=Path, help="output directory")
    ap.add_argument("--max-steps", type=int, default=MAX_STEPS)
    ap.add_argument("--statement-timeout", type=float, default=30.0,
                    help="seconds per query; 0 disables")
    ap.add_argument("--max-usd", type=float, help="stop the sweep past this spend")
    ap.add_argument("--max-tokens", type=int, help="stop the sweep past this many tokens")
    ap.add_argument("--trace", action="store_true", help="print every tool call")
    ap.add_argument("--resume", action="store_true",
                    help="skip tasks already completed in --out")
    args = ap.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_dir = args.out or Path("runs") / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    tracer = ConsoleTracer("spider") if args.trace else None

    tasks = load_tasks()
    if args.limit:
        tasks = tasks[: args.limit]
    print(f"running {len(tasks)} tasks -> {out_dir}\n", flush=True)

    summary_path = out_dir / "summary.jsonl"
    spent_tokens, spent_usd = 0, 0.0
    from manual_loop import loop_openai
    model = loop_openai.MODEL
    done_count = 0

    for i, task in enumerate(tasks, 1):
        if args.resume and (out_dir / task["instance_id"] / "answer.json").exists():
            print(f"[{i}/{len(tasks)}] {task['instance_id']:<10} skipped (already done)",
                  flush=True)
            continue

        # Announce BEFORE running: printing only on completion means a hang is
        # indistinguishable from work.
        print(f"[{i}/{len(tasks)}] {task['instance_id']:<10} {task['db']:<28} "
              f"running...", flush=True)
        started = time.monotonic()
        row = run_task(task, out_dir, out_dir.name, tracer, args.max_steps,
                       args.statement_timeout or None)
        row["seconds"] = round(time.monotonic() - started, 1)
        done_count += 1

        with summary_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")

        usage = row.get("usage") or {}
        spent_tokens += usage.get("total_tokens", 0) or 0
        spent_usd += cost_model.estimate(model, usage) or 0.0

        print(f"[{i}/{len(tasks)}] {row['instance_id']:<10} {row['status']:<14} "
              f"turns={row.get('num_turns', 0):<3} "
              f"sql={'y' if row.get('has_sql') else 'n'} "
              f"verified={'y' if row.get('verified') else 'n'} "
              f"{row['seconds']}s", flush=True)

        if (args.max_tokens and spent_tokens >= args.max_tokens) or (
            args.max_usd and spent_usd >= args.max_usd
        ):
            print(f"\n  budget reached after {i} task(s): "
                  f"{spent_tokens:,} tokens, ${spent_usd:.4f}", flush=True)
            break

    rows = [json.loads(l) for l in summary_path.read_text(encoding="utf-8").splitlines()
            if l.strip()] if summary_path.exists() else []
    sql = sum(1 for r in rows if r.get("has_sql"))
    verified = sum(1 for r in rows if r.get("verified"))
    print(f"\nproduced SQL for {sql}/{len(rows)} tasks; {verified} passed verification")
    print(f"spend: {cost_model.describe(model, {'total_tokens': spent_tokens})}")
    print(f"\nNo score yet -- run Spider's evaluate.py over {out_dir}")


if __name__ == "__main__":
    main()
