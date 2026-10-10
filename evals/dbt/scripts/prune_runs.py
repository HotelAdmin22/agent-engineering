"""Delete the bulk of a finished DBT run, keep everything the analysis uses.

A full run is ~9.5 GB. Of each ~50 MB attempt, almost all of it is two
directories copied per attempt:

    workspace/    the staged dbt project, its dbt_packages and its .duckdb
    submission/   the built database handed to the evaluator

Neither is readable by any summary, triage or scoring path after the run has
been scored. What every analysis actually reads is the loose files beside
them -- result.json, tool_calls.jsonl, transcript.jsonl -- which are ~0.6 MB
per attempt, about 1% of the total.

**What you lose.** `submission/*.duckdb` is the only way to diff a prediction
against gold after the fact. Capture those findings in `analysis/` before
pruning a run you have not finished looking at; `--keep-submissions` prunes
only the workspaces if you are not ready.

Usage:

    uv run python -m evals.dbt.scripts.prune_runs --dry-run
    uv run python -m evals.dbt.scripts.prune_runs --all
    uv run python -m evals.dbt.scripts.prune_runs luna-20261008-182025
    uv run python -m evals.dbt.scripts.prune_runs --all --keep-submissions
"""

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RUNS = ROOT / "runs" / "dbt"

# Pruned. Everything else in an attempt directory is kept.
BULK = ("workspace", "submission")

# A run with no summary never finished. Pruning it is still fine -- the
# records survive -- but say so, because its workspaces are the only state a
# resume could ever use.
def size_of(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def prune_run(run: Path, bulk: tuple[str, ...], dry_run: bool) -> tuple[int, int]:
    freed = removed = 0
    for attempt in sorted(run.glob("attempt_*")):
        for task in sorted(p for p in attempt.iterdir() if p.is_dir()):
            for name in bulk:
                target = task / name
                if not target.is_dir():
                    continue
                freed += size_of(target)
                removed += 1
                if not dry_run:
                    shutil.rmtree(target, ignore_errors=True)
    return freed, removed


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("runs", nargs="*", help="run ids; default is none unless --all")
    ap.add_argument("--all", action="store_true", help="every run directory")
    ap.add_argument("--dry-run", action="store_true", help="report, change nothing")
    ap.add_argument("--keep-submissions", action="store_true",
                    help="prune workspaces only; keep the databases gold is diffed against")
    args = ap.parse_args(argv)

    bulk = ("workspace",) if args.keep_submissions else BULK
    if args.all:
        targets = sorted(p for p in RUNS.iterdir()
                         if p.is_dir() and not p.name.startswith("_"))
    else:
        targets = [RUNS / r for r in args.runs]
    if not targets:
        ap.error("name a run, or pass --all")

    total = 0
    for run in targets:
        if not run.is_dir():
            print(f"  {run.name}: no such run")
            continue
        unfinished = "" if (run / "summary.json").exists() else "  [never finished]"
        freed, removed = prune_run(run, bulk, args.dry_run)
        total += freed
        verb = "would free" if args.dry_run else "freed"
        print(f"  {run.name:28} {verb} {freed/2**30:6.2f} GB "
              f"({removed} dir(s)){unfinished}")
    print(f"\n{'would free' if args.dry_run else 'freed'} {total/2**30:.2f} GB"
          f" ({'workspaces only' if args.keep_submissions else 'workspaces + submissions'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
