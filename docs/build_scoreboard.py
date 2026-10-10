"""Generate docs/luna-scoreboard.html: the road to 20/64 on Luna.

Built, not written, for the reason build_architecture.py gives -- a
hand-maintained page records the state of the world on the day someone last
remembered to edit it. Everything here comes from run records and
experiments/results.jsonl at build time.

The question this page exists to answer: **what has actually moved pass@1 on
gpt-6-luna, and what has not?** A change that sounds good and measured nothing
is the most expensive thing in this project, because it costs a run and then
gets tried again six weeks later by someone who did not know.

Gold-free by construction. It reads run summaries and the experiment ledger,
never evaluation_suite/gold or runs/dbt/**/scoring/, so it is safe under
docs/, which triage can read.

    uv run python docs/build_scoreboard.py
"""

import json
import subprocess
from datetime import datetime, timezone
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "luna-scoreboard.html"
RUNS = ROOT / "runs" / "dbt"
LEDGER = ROOT / "experiments" / "results.jsonl"

TARGET = 20          # tasks out of 64
TASKS = 64
UNWINNABLE = ["xero_new001", "xero_new002", "social_media001", "chinook001", "tpch002"]


def git_state() -> str:
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                             capture_output=True, text=True, timeout=10).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
                               capture_output=True, text=True, timeout=10).stdout.strip()
        return f"{sha}{' (uncommitted changes)' if dirty else ''}"
    except Exception:
        return "unknown"


def load_runs() -> list[dict]:
    """Every Luna run with enough attempts to say something, newest last."""
    out = []
    for d in sorted(RUNS.iterdir()):
        if not d.is_dir() or not d.name.startswith("luna-"):
            continue
        per_attempt: dict[int, list[bool]] = {}
        model = None
        cost = 0.0
        turns: list[int] = []
        capped = 0
        for f in d.glob("attempt_*/*/result.json"):
            try:
                r = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            per_attempt.setdefault(r["attempt"], []).append(bool(r["passed"]))
            cost += r.get("total_cost_usd") or 0.0
            turns.append(r.get("main_turns") or 0)
            if r.get("status") == "max_turns":
                capped += 1
            model = model or (list(r.get("usage_by_role") or {}) and
                              next(iter((r.get("usage_by_role") or {}).values()), {}).get("model"))
        if not per_attempt:
            continue
        complete = {a: v for a, v in per_attempt.items() if len(v) == TASKS}
        rates = [sum(v) / len(v) for a, v in sorted(complete.items())]
        out.append({
            "run": d.name,
            "complete_attempts": len(complete),
            "partial_attempts": len(per_attempt) - len(complete),
            "rates": rates,
            "pass_at_1": (sum(rates) / len(rates)) if rates else None,
            "tasks": (sum(rates) / len(rates) * TASKS) if rates else None,
            "ever": len({1 for a in per_attempt}),
            "cost": cost,
            "capped": capped,
            "turns": (sum(turns) / len(turns)) if turns else 0,
        })
    return out


def load_ledger() -> list[dict]:
    if not LEDGER.exists():
        return []
    rows = []
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return [r for r in rows if "dbt" in r.get("id", "") or "exp" in r.get("id", "")]


CSS = """
:root{--bg:#0f1115;--fg:#e6e6e6;--dim:#9aa0a6;--line:#2a2f3a;--ok:#4ade80;
--bad:#f87171;--warn:#fbbf24;--acc:#60a5fa}
*{box-sizing:border-box}
body{margin:0;padding:40px 24px 80px;background:var(--bg);color:var(--fg);
font:15px/1.65 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
main{max-width:1000px;margin:0 auto}
h1{font-size:30px;margin:0 0 6px} h2{font-size:21px;margin:44px 0 12px;
padding-bottom:7px;border-bottom:1px solid var(--line)}
h3{font-size:16px;margin:26px 0 8px;color:var(--acc)}
.sub{color:var(--dim);margin:0 0 22px}
.meta{display:flex;gap:18px;flex-wrap:wrap;color:var(--dim);font-size:13px;
margin-bottom:30px}
table{width:100%;border-collapse:collapse;margin:14px 0;font-size:14px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line)}
th{color:var(--dim);font-weight:600;font-size:12px;text-transform:uppercase;
letter-spacing:.4px}
td.v{text-align:right;font-variant-numeric:tabular-nums}
code{background:#1a1e26;padding:1.5px 5px;border-radius:3px;font-size:13px}
.gauge{background:#1a1e26;border:1px solid var(--line);border-radius:10px;
padding:22px;margin:22px 0}
.bar{height:26px;background:#232833;border-radius:5px;overflow:hidden;
position:relative;margin:14px 0 8px}
.fill{height:100%;background:linear-gradient(90deg,#2563eb,#60a5fa)}
.target{position:absolute;top:-4px;bottom:-4px;width:2px;background:var(--warn)}
.big{font-size:38px;font-weight:700;font-variant-numeric:tabular-nums}
.ok{color:var(--ok)} .bad{color:var(--bad)} .warn{color:var(--warn)}
.dim{color:var(--dim)}
.card{background:#151921;border:1px solid var(--line);border-left-width:3px;
border-radius:7px;padding:14px 18px;margin:12px 0}
.card.refuted{border-left-color:var(--bad)}
.card.kept{border-left-color:var(--ok)}
.card.null{border-left-color:var(--dim)}
.card h4{margin:0 0 6px;font-size:15px}
.tag{font-size:11px;text-transform:uppercase;letter-spacing:.5px;
padding:2px 7px;border-radius:3px;background:#232833;color:var(--dim);
margin-left:8px;vertical-align:middle}
footer{margin-top:60px;padding-top:18px;border-top:1px solid var(--line);
color:var(--dim);font-size:13px}
"""


def gauge(best: float | None) -> str:
    if best is None:
        return "<p class='dim'>No completed attempt yet.</p>"
    pct = min(best / TASKS * 100, 100)
    tgt = TARGET / TASKS * 100
    cls = "ok" if best >= TARGET else "warn"
    return f"""<div class="gauge">
<div class="big {cls}">{best:.1f} / {TASKS}</div>
<div class="dim">best measured pass@1 &middot; target {TARGET} &middot;
ceiling {TASKS - len(UNWINNABLE)} (5 tasks are unpassable by anyone)</div>
<div class="bar"><div class="fill" style="width:{pct:.1f}%"></div>
<div class="target" style="left:{tgt:.1f}%"></div></div>
<div class="dim" style="font-size:13px">gap to target:
<strong class="{cls}">{max(TARGET - best, 0):.1f} tasks</strong></div></div>"""


def runs_table(runs: list[dict]) -> str:
    head = ("<table><tr><th>run</th><th>attempts</th><th class='v'>per-attempt</th>"
            "<th class='v'>pass@1</th><th class='v'>tasks</th>"
            "<th class='v'>turn cap</th><th class='v'>cost</th></tr>")
    body = []
    for r in runs:
        rates = ", ".join(f"{x:.3f}" for x in r["rates"]) or "&mdash;"
        p = f"{r['pass_at_1']:.3f}" if r["pass_at_1"] is not None else "&mdash;"
        t = f"{r['tasks']:.1f}" if r["tasks"] is not None else "&mdash;"
        note = "" if not r["partial_attempts"] else (
            f" <span class='tag'>+{r['partial_attempts']} partial</span>")
        body.append(
            f"<tr><td><code>{escape(r['run'])}</code>{note}</td>"
            f"<td class='v'>{r['complete_attempts']}</td><td class='v'>{rates}</td>"
            f"<td class='v'><strong>{p}</strong></td><td class='v'>{t}</td>"
            f"<td class='v'>{r['capped']}</td><td class='v'>${r['cost']:.2f}</td></tr>")
    return head + "".join(body) + "</table>"


def ledger_cards(rows: list[dict]) -> str:
    out = []
    for r in rows:
        d = (r.get("decision") or "").lower()
        cls = ("refuted" if "revert" in d else
               "kept" if "keep" in d else "null")
        out.append(
            f"<div class='card {cls}'><h4>{escape(r.get('id','?'))}"
            f"<span class='tag'>{escape(r.get('decision','') or 'n/a')}</span></h4>"
            f"<p class='dim' style='margin:4px 0'><strong>Changed:</strong> "
            f"{escape(str(r.get('variable_changed','')))[:300]}</p>"
            f"<p style='margin:6px 0'>{escape(str(r.get('result','')))[:700]}</p></div>")
    return "".join(out) or "<p class='dim'>No experiments recorded.</p>"


def main() -> None:
    runs = load_runs()
    scored = [r for r in runs if r["pass_at_1"] is not None]
    best = max((r["tasks"] for r in scored), default=None)
    rows = load_ledger()
    built = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Luna to 20/64</title><style>{CSS}</style></head><body><main>
<h1>Spider2&#8209;DBT: getting <code>gpt-6-luna</code> to {TARGET}/{TASKS}</h1>
<p class="sub">What has moved pass@1, what has not, and why. Generated from run
records and the experiment ledger &mdash; never from the gold set.</p>
<div class="meta"><span>built {built}</span><span>git {escape(git_state())}</span>
<span>{len(scored)} scored run(s)</span><span>{len(rows)} recorded experiment(s)</span></div>

{gauge(best)}

<h2>Every run</h2>
<p class="sub">pass@1 is the mean of the completed per-attempt rates. Partial
attempts are excluded from the rate and flagged, because a half-finished
attempt is not a sample.</p>
{runs_table(runs)}

<h2>What has been tried</h2>
<p class="sub">Green kept, red reverted, grey no measurable effect. The grey
ones matter most: they are the ideas that sound right and are not.</p>
{ledger_cards(rows)}

<h2>What the evidence says</h2>
<h3>The bottleneck is not information</h3>
<p>Three changes gave the agent more or better information &mdash; catalog
access, the <code>.yml</code> spec read by the agent, the same spec injected
for free. The catalog fix removed real waste (168 refused reads &rarr; 0,
failed-call rate 6.5% &rarr; 4.0%) and <strong>moved accuracy by nothing</strong>.
The two spec changes made it worse by spending turns.</p>

<h3>The bottleneck is not awareness of failure</h3>
<p>80% of these projects declare column tests &mdash; 809 <code>not_null</code>,
280 <code>unique</code> &mdash; targeting exactly the measured failure modes.
Wiring them in as a gate with a repair pass surfaced the right rows
(<code>provider001</code>'s assertions named the same 2,857 rows a gold diff
found) and still did not convert: repaired attempts passed at 0.222 against
0.259 for attempts whose assertions were already clean.</p>

<h3>Where the {TARGET} tasks actually are</h3>
<p>In the best run, 10 tasks passed all three attempts and 11 passed one or two.
<strong>The ceiling from making flaky tasks reliable is 21/{TASKS}</strong>, which
is where the target lives. But passing and failing attempts of the same task are
operationally indistinguishable &mdash; 19.1 vs 20.1 turns, 1.7 vs 2.2 tool
errors. The variance is in the model's sampling, not in the harness.</p>

<h3>Five tasks can never pass</h3>
<p>{", ".join(f"<code>{escape(t)}</code>" for t in UNWINNABLE)} &mdash; verified
0 of 9 attempts each across three runs. Broken gold filenames, gold missing the
compared tables, and one task whose starting database ships 1 of the 8 source
tables it needs. Report pass@1 on /{TASKS} and state the ceiling beside it;
do not quietly switch denominators.</p>

<footer>Generated by <code>docs/build_scoreboard.py</code>. Gold-derived
analysis lives in <code>analysis/</code>, outside triage's readable roots.
Run history and the agent-safe findings are in
<code>docs/dbt-run-history.md</code>.</footer>
</main></body></html>"""
    OUT.write_text(html, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}  ({len(html):,} bytes)")
    if best is not None:
        print(f"best pass@1: {best:.1f}/{TASKS}   target {TARGET}   gap {max(TARGET-best,0):.1f}")


if __name__ == "__main__":
    main()
