"""Score the data agent: execution accuracy, pass^k, confidently-wrong rate.

Three numbers, because one is not enough to make a decision with:

  EXECUTION ACCURACY -- fraction of runs that got the right answer. The headline.

  PASS^k -- fraction of questions right in ALL k runs. A model that is right 80%
  of the time on every question scores 0.8 accuracy and 0.33 pass^3. The gap is
  the part you cannot ship, and averaging hides it.

  CONFIDENTLY-WRONG RATE -- fraction of runs where the verifier passed and the
  answer was still wrong. This is the only number that matters for trust. A
  wrong answer that announces itself is an inconvenience; a wrong answer wearing
  a verified badge is a liability.

Gold values are computed by running `gold_sql` through the same gateway at eval
time rather than being pasted into the file. A hardcoded expected number silently
stops matching the moment the warehouse is rebuilt, and then the eval measures
nothing.

    uv run python evals/run_evals.py --k 3
    uv run python evals/run_evals.py --limit 3 --tag why
"""

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

# Both the repo root (for manual_loop) and src/ (for agent) -- this script is
# run as a file, not a module, so neither is on the path by default.
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "src"))

from agent.agent import Planner          # noqa: E402
from agent.evidence import EvidenceStore  # noqa: E402
from agent.gateway import Gateway        # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
GOLD = ROOT / "evals" / "gold" / "questions.jsonl"
WAREHOUSE = ROOT / "data" / "warehouse.duckdb"

REL_TOLERANCE = 0.005     # 0.5%: survives rounding, not a wrong aggregate
ABS_TOLERANCE = 0.01


def load_gold(tag: str | None = None, limit: int | None = None) -> list[dict]:
    rows = [
        json.loads(line)
        for line in GOLD.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if tag:
        rows = [r for r in rows if tag in r.get("tags", [])]
    return rows[:limit] if limit else rows


def gold_value(task: dict, warehouse: Path) -> object:
    """Compute the truth now, through the same gateway the agent uses."""
    if task.get("expect", {}).get("value") is not None:
        return task["expect"]["value"]
    if not task.get("gold_sql"):
        return None
    gateway = Gateway(warehouse, store=EvidenceStore())
    decision, record = gateway.run_sql(task["gold_sql"])
    if not decision.allowed or record is None or not record.rows:
        raise RuntimeError(f"gold_sql failed for {task['id']}: {decision.reason}")
    return record.rows[0][0]


def _close(a: float, b: float) -> bool:
    if b == 0:
        return abs(a) <= ABS_TOLERANCE
    return abs(a - b) <= max(ABS_TOLERANCE, abs(b) * REL_TOLERANCE)


def _numbers_in(value) -> list[float]:
    """Every number reachable inside an evidence field, however nested."""
    out: list[float] = []
    if isinstance(value, bool):
        return out
    if isinstance(value, dict):
        for v in value.values():
            out += _numbers_in(v)
        return out
    if isinstance(value, (list, tuple)):
        for v in value:
            out += _numbers_in(v)
        return out
    # Decimal, int, float, and numeric strings all land here. DuckDB returns
    # DECIMAL columns as Decimal, which is neither int nor float -- treating
    # only those two as numeric silently graded correct answers as wrong.
    try:
        return [float(value)]
    except (TypeError, ValueError):
        return out
    return out


def grade(run, task: dict, truth, store) -> tuple[bool, str]:
    """Did the agent actually answer this correctly?

    Graded against the evidence the answer CITED, not against its prose. A
    report can name the right category in passing while every number in it came
    from a different query.
    """
    kind = task["expect"]["kind"]
    answer = (run.answer or "").lower()
    cited = [store.get(eid) for eid in run.evidence_ids]
    cited = [r for r in cited if r is not None]

    if kind == "label":
        expected = str(truth).lower()
        return expected in answer, f"expected {expected!r} in answer"

    if kind == "driver":
        expected = str(truth).lower()
        effects = [
            str(r.fields["dominant_effect"]).lower()
            for r in cited if "dominant_effect" in r.fields
        ]
        # Both must agree: the prose names it AND the cited decomposition says
        # it. Either alone is a coin flip dressed as an answer.
        agreed = bool(effects) and expected in effects
        return (expected in answer and agreed), (
            f"expected {expected!r}; cited effects {effects or 'none'}"
        )

    target = float(truth)
    for record in cited:
        if any(_close(n, target) for n in _numbers_in(record.fields)):
            return True, f"{target} found in cited {record.id}"
    return False, f"{target} not in cited evidence {run.evidence_ids or '[]'}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=1, help="runs per question")
    ap.add_argument("--limit", type=int, help="first N questions only")
    ap.add_argument("--tag", help="only questions carrying this tag")
    ap.add_argument("--warehouse", type=Path, default=WAREHOUSE)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    tasks = load_gold(args.tag, args.limit)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_dir = args.out or ROOT / "runs" / f"eval-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"{len(tasks)} questions x {args.k} run(s) -> {out_dir}\n")

    rows = []
    per_question: dict[str, list[bool]] = defaultdict(list)

    for task in tasks:
        try:
            truth = gold_value(task, args.warehouse)
        except RuntimeError as exc:
            print(f"  {task['id']}  SKIP  {exc}")
            continue

        for attempt in range(1, args.k + 1):
            planner = Planner(
                warehouse=args.warehouse,
                run_dir=out_dir / f"{task['id']}-{attempt}",
            )
            run = planner.answer(task["question"], trace=None)
            correct, why = grade(run, task, truth, planner.store)
            verified = run.ok
            per_question[task["id"]].append(correct)

            rows.append({
                "id": task["id"],
                "attempt": attempt,
                "tags": task.get("tags", []),
                "correct": correct,
                "verified": verified,
                "confidently_wrong": verified and not correct,
                "steps": run.steps,
                "tokens": run.usage.get("total_tokens", 0),
                "stopped_because": run.stopped_because,
                "repaired": run.repaired,
                "detail": why,
            })
            flag = "ok " if correct else "XX "
            badge = "verified" if verified else "unverified"
            print(
                f"  {task['id']}-{attempt}  {flag} {badge:<10} "
                f"steps={run.steps:<2} tok={run.usage.get('total_tokens', 0):<6} {why[:58]}"
            )

    if not rows:
        sys.exit("no runs completed")

    n = len(rows)
    accuracy = sum(r["correct"] for r in rows) / n
    confidently_wrong = sum(r["confidently_wrong"] for r in rows) / n
    pass_k = sum(1 for v in per_question.values() if all(v)) / len(per_question)
    tokens = sum(r["tokens"] for r in rows)

    summary = {
        "generated": stamp,
        "questions": len(per_question),
        "k": args.k,
        "runs": n,
        "execution_accuracy": round(accuracy, 3),
        f"pass^{args.k}": round(pass_k, 3),
        "confidently_wrong_rate": round(confidently_wrong, 3),
        "mean_steps": round(sum(r["steps"] for r in rows) / n, 2),
        "total_tokens": tokens,
        "mean_tokens_per_run": round(tokens / n),
    }

    (out_dir / "runs.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )
    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )

    print("\n" + "-" * 58)
    for key, value in summary.items():
        print(f"  {key:<24} {value}")
    print("-" * 58)
    print(f"\nwritten to {out_dir}")


if __name__ == "__main__":
    main()
