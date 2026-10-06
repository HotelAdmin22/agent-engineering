"""Ask one open question against the seeded warehouse; score what it found.

Different from run_evals.py on purpose. Gold questions measure whether the
agent can compute what it was told to compute. This measures whether it would
notice something nobody pointed at -- which is the actual job.

Scoring is strict: an anomaly counts as found only when the answer names its
month AND at least one keyword describing its cause. "August was weak" is not
finding a regional cancellation spike; it is noticing the hole it left.

    uv run python evals/score_anomalies.py
    uv run python evals/score_anomalies.py --k 3
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "src"))

from agent.agent import Planner          # noqa: E402

KEY = _ROOT / "evals" / "seeded" / "planted.json"
SEEDED = _ROOT / "data" / "warehouse_seeded.duckdb"

QUESTION = (
    "Something looks wrong with revenue in this warehouse. Compare the months, "
    "then break the anomalous ones down by every dimension available until you "
    "can name the cause. Report which months are anomalous and why."
)


MONTH_NAMES = {
    "01": "january", "02": "february", "03": "march", "04": "april",
    "05": "may", "06": "june", "07": "july", "08": "august",
    "09": "september", "10": "october", "11": "november", "12": "december",
}


def _month_named(month: str, text: str) -> bool:
    """Accept '2026-07' or 'July'.

    Matching only the ISO form scored a run 0/3 that had in fact named every
    month -- in English. The grader was wrong, not the agent. Second time that
    has happened here, after Decimal in run_evals.py.
    """
    iso = month.lower()
    name = MONTH_NAMES.get(month.split("-")[-1], "")
    return iso in text or (bool(name) and name in text)


def score(answer: str, anomalies: list[dict]) -> list[dict]:
    text = (answer or "").lower()
    out = []
    for a in anomalies:
        month_named = _month_named(a["month"], text)
        hits = [k for k in a.get("signals", a.get("keywords", [])) if k in text]
        out.append({
            "id": a["id"],
            "name": a["name"],
            "month": a["month"],
            "month_named": month_named,
            "signals_hit": hits,
            "found": month_named and bool(hits),
        })
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=1)
    ap.add_argument("--question", default=QUESTION)
    args = ap.parse_args()

    if not KEY.exists() or not SEEDED.exists():
        sys.exit("run evals/seeded/plant.py first")

    load_dotenv(_ROOT / ".env")
    key = json.loads(KEY.read_text(encoding="utf-8"))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_dir = _ROOT / "runs" / f"anomaly-{stamp}"

    totals = []
    for attempt in range(1, args.k + 1):
        planner = Planner(warehouse=SEEDED, run_dir=out_dir / f"run-{attempt}")
        run = planner.answer(args.question)
        rows = score(run.answer, key["anomalies"])
        found = sum(r["found"] for r in rows)
        months = sum(r["month_named"] for r in rows)
        totals.append(found)

        print(f"\nrun {attempt}: found {found}/{len(rows)} "
              f"(months named {months}/{len(rows)}, steps {run.steps}, "
              f"tokens {run.usage.get('total_tokens', 0)})")
        for r in rows:
            mark = "ok " if r["found"] else "XX "
            print(f"  {mark} {r['id']} {r['name']:<24} month={r['month_named']!s:<5} "
                  f"signals={r['signals_hit'] or '[]'}")

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.json").write_text(json.dumps({
        "k": args.k,
        "anomalies": len(key["anomalies"]),
        "found_per_run": totals,
        "best": max(totals),
        "mean": round(sum(totals) / len(totals), 2),
        "target": "at least 2 of 3",
        "meets_target": max(totals) >= 2,
    }, indent=2), encoding="utf-8")

    print(f"\nbest {max(totals)}/{len(key['anomalies'])} | "
          f"mean {sum(totals) / len(totals):.2f} | "
          f"target 2/3 -> {'MET' if max(totals) >= 2 else 'NOT MET'}")
    print(f"written to {out_dir}")


if __name__ == "__main__":
    main()
