"""Score a sweep with Spider's own evaluator, and write the number down.

The agent produces `runs/<sweep>/<task>/result.csv`. Spider's evaluate.py wants
a flat directory of `<instance_id>.csv`. This stages one into the other, runs
their scorer unmodified, and records the result.

Their scorer, not ours, deliberately. A home-grown comparison that is slightly
too lenient invents improvements that never happened -- and in this project six
separate measurement bugs have already produced numbers that were simply wrong.
The one thing worth not writing ourselves is the thing that decides whether we
are right.

Runs on the HOST, never in the container: the gold answers are deliberately
absent from the image, so the thing being measured cannot reach them.

    uv run python evals/score.py runs/spider-one-agent
"""

import argparse
import csv
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUITE = ROOT / "evals" / "spider" / "evaluation_suite"
GOLD = ROOT / "evals" / "spider" / "gold"
DATABASES = ROOT / "evals" / "spider" / "databases"

FINAL = re.compile(r"Final score:\s*([0-9.]+),\s*Correct examples:\s*(\d+),\s*Total examples:\s*(\d+)")
PER_TASK = re.compile(r"\{'local[^}]*\}")


def backfill(run_dir: Path) -> int:
    """Rebuild a missing result.csv from the stored evidence.

    A sweep run before the sql_was_executed matcher was fixed wrote no
    predictions, because the matcher found nothing and there was nothing to
    write. The evidence is still on disk and the matcher works now, so the
    predictions can be recovered without spending the tokens again.
    """
    sys.path.insert(0, str(ROOT / "src"))
    sys.path.insert(0, str(ROOT))
    from agent.agent.modes import verify_sql_answer           # noqa: E402
    from agent.evidence import EvidenceRecord, EvidenceStore   # noqa: E402

    rebuilt = 0
    for task_dir in sorted(run_dir.iterdir()):
        if not task_dir.is_dir() or (task_dir / "result.csv").exists():
            continue
        answer = task_dir / "answer.json"
        evidence = task_dir / "evidence.jsonl"
        if not (answer.exists() and evidence.exists()):
            continue

        store = EvidenceStore()
        for line in evidence.read_text(encoding="utf-8").splitlines():
            if line.strip():
                record = EvidenceRecord(**json.loads(line))
                store._records[record.id] = record

        data = json.loads(answer.read_text(encoding="utf-8"))
        result = verify_sql_answer(data.get("template"), store, "sqlite")
        matched = next(
            (f.evidence_id for f in result.findings
             if f.check == "sql_was_executed" and f.evidence_id), None
        )
        if matched is None:
            continue
        record = store.get(matched)
        with (task_dir / "result.csv").open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            if record.columns:
                writer.writerow(record.columns)
            writer.writerows(record.rows)
        rebuilt += 1
    return rebuilt


def stage(run_dir: Path, staging: Path) -> int:
    """Flatten runs/<sweep>/<task>/result.csv -> staging/<task>.csv."""
    staging.mkdir(parents=True, exist_ok=True)
    n = 0
    for task_dir in sorted(run_dir.iterdir()):
        prediction = task_dir / "result.csv"
        if task_dir.is_dir() and prediction.exists():
            shutil.copy2(prediction, staging / f"{task_dir.name}.csv")
            n += 1
    return n


def run_scorer(staging: Path) -> tuple[str, int]:
    """Invoke Spider's evaluate.py unmodified. Returns (output, returncode)."""
    proc = subprocess.run(
        [sys.executable, str(ROOT / "evals" / "_run_scorer.py"),
         "--mode", "exec_result",
         "--result_dir", str(staging),
         "--gold_dir", str(GOLD)],
        cwd=SUITE,
        capture_output=True,
        text=True,
        timeout=1800,
    )
    return (proc.stdout + proc.stderr), proc.returncode


def parse(output: str) -> dict:
    per_task: dict[str, int] = {}
    block = PER_TASK.search(output)
    if block:
        try:
            per_task = json.loads(block.group(0).replace("'", '"'))
        except json.JSONDecodeError:
            per_task = {}

    final = FINAL.search(output)
    if not final:
        return {"parsed": False, "per_task": per_task}
    return {
        "parsed": True,
        "accuracy": float(final.group(1)),
        "correct": int(final.group(2)),
        "total": int(final.group(3)),
        "per_task": per_task,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir", type=Path, help="a sweep directory under runs/")
    ap.add_argument("--keep-staging", action="store_true")
    args = ap.parse_args()

    run_dir = args.run_dir if args.run_dir.is_absolute() else ROOT / args.run_dir
    if not run_dir.is_dir():
        sys.exit(f"no such run directory: {run_dir}")
    if not (SUITE / "evaluate.py").exists():
        sys.exit(f"scorer missing: run evals/spider/fetch.py")
    if not DATABASES.glob("*.sqlite"):
        sys.exit("Spider databases missing; see evals/spider/README.md")

    recovered = backfill(run_dir)
    if recovered:
        print(f"rebuilt {recovered} prediction(s) from stored evidence")

    staging = run_dir / "_predictions"
    n = stage(run_dir, staging)
    if not n:
        sys.exit(f"no result.csv files under {run_dir}")
    print(f"staged {n} prediction(s) -> {staging.relative_to(ROOT)}")
    print("running Spider's evaluate.py ...\n")

    output, code = run_scorer(staging)
    result = parse(output)

    if not result["parsed"]:
        print(output[-2500:])
        sys.exit(f"could not parse a score from evaluate.py (exit {code})")

    # Scored against the tasks actually attempted, not Spider's full 547 -- the
    # scorer also prints a "Real score" over 547, which is only meaningful for
    # a complete submission and would read as a catastrophic number here.
    summary = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "run": run_dir.name,
        "attempted": n,
        "correct": result["correct"],
        "accuracy_on_attempted": round(result["correct"] / n, 4),
        "scorer": "spider2-lite evaluation_suite/evaluate.py, exec_result mode",
        "per_task": result["per_task"],
    }
    (run_dir / "score.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(f"  correct   {result['correct']}/{n}")
    print(f"  accuracy  {summary['accuracy_on_attempted']:.1%}")
    if result["per_task"]:
        wrong = [k for k, v in sorted(result["per_task"].items()) if not v]
        print(f"  wrong     {', '.join(wrong) if wrong else 'none'}")
    print(f"\nwritten to {(run_dir / 'score.json').relative_to(ROOT)}")

    if not args.keep_staging:
        shutil.rmtree(staging, ignore_errors=True)


if __name__ == "__main__":
    main()
