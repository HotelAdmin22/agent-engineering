"""Answer one question against the local warehouse.

    uv run python -m agent "why did revenue fall in September?"
    uv run python -m agent --quiet "what was revenue last month?"
"""

import argparse
import sys

from dotenv import load_dotenv

from .agent import Planner

DEFAULT_QUESTION = "Why did revenue fall in September 2026 compared with August?"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("question", nargs="?", default=DEFAULT_QUESTION)
    ap.add_argument("--quiet", action="store_true", help="no live trace")
    ap.add_argument("--no-repair", action="store_true")
    ap.add_argument("--warehouse", default="data/warehouse.duckdb")
    args = ap.parse_args()

    load_dotenv(".env")

    trace = None
    if not args.quiet:
        from orchestrator.trace import ConsoleTracer
        trace = ConsoleTracer("agent")

    planner = Planner(warehouse=args.warehouse)
    print(f"run {planner.run_id} -> {planner.run_dir}\n", file=sys.stderr)

    run = planner.answer(args.question, repair=not args.no_repair, trace=trace)

    print("\n" + "=" * 72)
    print(run.answer)
    print("=" * 72)
    print(f"\nverification : {run.verification.summary()}", file=sys.stderr)
    for f in run.verification.findings:
        print(f"  {f.verdict:<5} {f.check:<22} {f.detail[:90]}", file=sys.stderr)
    print(f"evidence     : {', '.join(run.evidence_ids) or 'none'}", file=sys.stderr)
    print(f"steps        : {run.steps} ({run.stopped_because})"
          f"{' after repair' if run.repaired else ''}", file=sys.stderr)
    print(f"tokens       : {run.usage.get('total_tokens', '?')}", file=sys.stderr)
    print(f"transcript   : {planner.run_dir}", file=sys.stderr)

    sys.exit(0 if run.ok else 1)


if __name__ == "__main__":
    main()
