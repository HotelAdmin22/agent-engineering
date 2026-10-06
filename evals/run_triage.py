"""Run one triage pass over the recorded failures and write the proposals.

    uv run python evals/run_triage.py
    uv run python evals/run_triage.py --quiet
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

from orchestrator.triage import triage          # noqa: E402
from orchestrator.trace import ConsoleTracer    # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    load_dotenv(_ROOT / ".env")
    out_dir = _ROOT / "runs" / f"triage-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}"
    out_dir.mkdir(parents=True, exist_ok=True)

    result = triage(trace=None if args.quiet else ConsoleTracer("triage"))
    (out_dir / "triage.json").write_text(
        json.dumps(result, indent=2, default=str), encoding="utf-8"
    )

    print(f"\nscanned {result['runs_scanned']} runs / "
          f"{result['tool_calls_scanned']} tool calls in {result['steps']} steps "
          f"({result['usage'].get('total_tokens', '?')} tokens)")
    print(f"actionable proposals: {len(result['actionable'])}  "
          f"rejected: {len(result['rejected'])}\n")

    for f in result["actionable"]:
        print(f"  [{f['confidence']}] {f['signature'][:70]}  x{f['occurrences']}")
        print(f"      diagnosis : {f['diagnosis'][:100]}")
        print(f"      change    : {f['proposed_change'][:100]}")
        print(f"      target    : {', '.join(f['target_paths'])}")
        print(f"      assertion : {json.dumps(f['assertion'])[:90]}\n")

    for f in result["rejected"]:
        print(f"  REJECTED  {f['signature'][:60]}  -- {f['rejected']}")

    print(f"written to {out_dir}")


if __name__ == "__main__":
    main()
