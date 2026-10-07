"""Read an audit log back as something a person can scan.

JSONL is the right storage format and the wrong reading format. This renders a
run as one line per event, with the timings on the left, so the question "where
did the time go" is answerable at a glance rather than by writing a one-off
script every time.

    uv run python evals/show_run.py runs/spider-budget/local021
    uv run python evals/show_run.py runs/spider-budget          # whole sweep
    uv run python evals/show_run.py runs/spider-budget --slow   # slowest calls
"""

import argparse
import json
import sys
from pathlib import Path

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"
BLUE, YELLOW, RED, GREEN = "\033[34m", "\033[33m", "\033[31m", "\033[32m"

# A tool call slower than this is worth noticing. The hang that prompted all of
# this ran for 26 minutes; anything over a few seconds deserves a colour.
SLOW_MS = 5_000


def load(path: Path) -> list[dict]:
    rows = []
    for log in sorted(path.rglob("audit.jsonl")) if path.is_dir() else [path]:
        for line in log.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return rows


def paint(code: str, text: str, color: bool) -> str:
    return f"{code}{text}{RESET}" if color else text


def detail(row: dict) -> str:
    """The one field that matters for this kind of event."""
    for key in ("sql", "question", "input", "args", "content", "text", "reason", "error"):
        if key in row:
            value = row[key]
            if isinstance(value, dict):
                value = value.get("sql") or json.dumps(value)
            return " ".join(str(value).split())
    return ""


def render(rows: list[dict], color: bool = True, width: int = 96) -> None:
    current_task = None
    for row in rows:
        task = row.get("task")
        if task != current_task:
            current_task = task
            label = f"{row.get('run', '?')}" + (f" / {task}" if task else "")
            print(f"\n{paint(BOLD, label, color)}")

        kind = row.get("kind", "?")
        dur = row.get("dur_ms")
        timing = f"{row.get('t_ms', 0)/1000:7.1f}s"
        if dur is not None:
            flag = RED if dur > SLOW_MS else DIM
            timing += paint(flag, f" +{dur/1000:5.1f}s", color)
        else:
            timing += "        "

        mark = {
            "tool_call": paint(BLUE, "->", color),
            "tool_result": paint(RED if row.get("is_error") else DIM, "<-", color),
            "stop": paint(YELLOW, "##", color),
            "task": paint(GREEN, "::", color),
        }.get(kind, "  ")

        tool = row.get("tool", "")
        text = detail(row)[:width]
        print(f"  {timing} {mark} {tool:<16} {paint(DIM, text, color)}")


def slowest(rows: list[dict], limit: int) -> None:
    timed = [r for r in rows if r.get("dur_ms") is not None]
    timed.sort(key=lambda r: -r["dur_ms"])
    print(f"\n  slowest {min(limit, len(timed))} of {len(timed)} timed calls\n")
    for row in timed[:limit]:
        where = f"{row.get('task') or row.get('run')}"
        print(f"  {row['dur_ms']/1000:7.1f}s  {where:<14} {row.get('tool',''):<16} "
              f"{detail(row)[:70]}")


def summarise(rows: list[dict]) -> None:
    tasks = {r.get("task") for r in rows if r.get("task")}
    calls = [r for r in rows if r.get("kind") == "tool_call"]
    timed = [r["dur_ms"] for r in rows if r.get("dur_ms") is not None]
    errors = [r for r in rows if r.get("is_error")]
    print(f"\n  {len(rows)} events | {len(tasks) or 1} task(s) | {len(calls)} tool calls"
          f" | {len(errors)} errors")
    if timed:
        timed.sort()
        print(f"  tool time: median {timed[len(timed)//2]/1000:.2f}s, "
              f"max {timed[-1]/1000:.1f}s, total {sum(timed)/1000:.1f}s")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", type=Path)
    ap.add_argument("--slow", action="store_true", help="slowest calls only")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args()

    if not args.path.exists():
        sys.exit(f"no such path: {args.path}")

    rows = load(args.path)
    if not rows:
        sys.exit(f"no audit.jsonl under {args.path}")

    if args.slow:
        slowest(rows, args.limit)
    else:
        render(rows, color=not args.no_color)
    summarise(rows)


if __name__ == "__main__":
    main()
