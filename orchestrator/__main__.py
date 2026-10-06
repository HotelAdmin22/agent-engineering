"""Watch one full cycle, live.

    uv run python -m orchestrator                 # trace to the terminal
    uv run python -m orchestrator --quiet         # transcript only
    uv run python -m orchestrator --no-repair     # first answer, no second pass

Runs against a small built-in SQLite fixture rather than Spider, because
evals/spider/databases/ is still empty. The verifier is stubbed for the same
reason manual_loop/loop.py cannot run yet -- see orchestrator/README.md.

Everything it does is written to runs/<timestamp>/.
"""

import argparse
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

from evals.sandbox import Sandbox

from . import orchestrate
from .findings import FAIL, Finding, Ledger

FIXTURE = """
CREATE TABLE customers(customer_id INT, name TEXT, country TEXT);
CREATE TABLE orders(order_id INT, customer_id INT, status TEXT, amount REAL);
INSERT INTO customers VALUES (1,'Ada','NG'),(2,'Bruno','BR'),(3,'Chen','SG');
INSERT INTO orders VALUES (1,1,'completed',249.0),(2,1,'completed',89.5),
                          (3,2,'refunded',120.0),(4,3,'completed',612.75),
                          (5,3,'pending',310.2),(6,3,'completed',45.0);
"""

QUESTION = "Which country has the highest total completed order value?"


def stub_verify(question, sql, sandbox, task_id, client=None, trace=None):
    """Stands in for the real verifier until ANTHROPIC_API_KEY is available.

    It asserts the one thing the question demands -- a single row -- and does
    so with a re-runnable query, exactly as the real verifier must.
    """
    return [
        Finding(
            finding_id=f"{task_id}:check2:shape",
            check="CHECK 2",
            verdict=FAIL,
            recommendation="The question names one country, so return exactly one row.",
            evidence_query="SELECT * FROM ({candidate})",
            expected_after_fix="one_row",
            note="stubbed verifier",
        )
    ], {"stub": True}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true", help="no live trace")
    ap.add_argument("--no-repair", action="store_true", help="skip the second pass")
    ap.add_argument("--out", type=Path, help="output directory")
    args = ap.parse_args()

    load_dotenv(".env")

    run_id = orchestrate.new_run_id()
    out_dir = args.out or Path("runs") / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    db = out_dir / "fixture.sqlite"
    con = sqlite3.connect(db)
    con.executescript(FIXTURE)
    con.commit()
    con.close()

    orchestrate.verify = stub_verify
    ledger = Ledger(out_dir / "ledger.jsonl")

    print(f"run {run_id} -> {out_dir}\n", file=sys.stderr)
    outcome = orchestrate.run_task(
        task_id="demo001",
        question=QUESTION,
        sandbox=Sandbox(db),
        ledger=ledger,
        run_id=run_id,
        repair=not args.no_repair,
        out_dir=out_dir,
        verbose=not args.quiet,
    )

    print(f"\nfindings {len(outcome.findings)} | landed {outcome.landed_count} "
          f"| false positives {outcome.false_positive_count}", file=sys.stderr)
    print(f"ledger   {ledger.summarise()}", file=sys.stderr)
    print(f"transcript {out_dir / 'demo001' / 'cycle.json'}", file=sys.stderr)


if __name__ == "__main__":
    main()
