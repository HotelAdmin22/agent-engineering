"""Generate docs/failures.html -- every failure across every run, grouped.

Built for the question "what should I fix next", which a per-run transcript
cannot answer. One run shows you a KeyError; forty runs show you that the same
KeyError fires on compute_delta eleven times and nowhere else, which is a
tooling bug with an address.

Four classes, deliberately kept apart because they call for different fixes:

  CRASH      an unhandled exception inside a tool. Always a bug in our code.
             Carries the traceback.
  REFUSAL    the gateway said no. Expected, and usually the model repairing
             itself -- but a refusal that repeats is a missing affordance.
  INCORRECT  the eval graded the answer wrong. Split out by whether the
             verifier caught it.
  UNFINISHED the run hit a step or token budget without answering.

    uv run python docs/build_failures.py
"""

import json
from datetime import datetime, timezone
from html import escape
import sys
from pathlib import Path
from string import Template

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from orchestrator.failures import collect  # noqa: E402

OUT = ROOT / "docs" / "failures.html"

MAX_EXAMPLES = 3


def _group_block(title: str, groups: dict, kind: str) -> str:
    if not groups:
        return (f'<section><h2>{escape(title)}</h2>'
                f'<p class="none">none recorded</p></section>')

    ordered = sorted(groups.items(), key=lambda kv: -len(kv[1]))
    cards = ""
    for signature, rows in ordered:
        example = rows[0]
        args = json.dumps(example.get("args") or {}, indent=2)[:1200]
        tb = example.get("traceback")
        runs = sorted({r["_run"] for r in rows})
        detail = (
            f'<details><summary>traceback &amp; arguments</summary>'
            f'<pre class="tb">{escape(tb)}</pre>' if tb else
            '<details><summary>arguments</summary>'
        )
        cards += (
            f'<article class="fail {kind}">'
            f'<header><code>{escape(signature)}</code>'
            f'<span class="count">{len(rows)}x</span></header>'
            f'<p class="msg">{escape((example.get("error") or "")[:300])}</p>'
            f'{detail}<pre class="args">{escape(args)}</pre>'
            f'<p class="runs">seen in {len(runs)} run(s): '
            f'{escape(", ".join(runs[:6]))}{"..." if len(runs) > 6 else ""}</p>'
            f'</details></article>'
        )
    return f'<section><h2>{escape(title)}</h2><div class="fails">{cards}</div></section>'


def render(data: dict) -> str:
    counts = {
        "crashes": sum(len(v) for v in data["crashes"].values()),
        "refusals": sum(len(v) for v in data["refusals"].values()),
        "incorrect": len(data["incorrect"]),
        "unfinished": len(data["unfinished"]),
    }
    total_calls = sum(data["tool_calls"].values())

    tiles = "".join(
        f'<div class="tile {"bad" if n and k == "crashes" else ""}">'
        f'<span class="n">{n}</span><span class="k">{k}</span></div>'
        for k, n in counts.items()
    )

    rows = ""
    for tool, calls in data["tool_calls"].most_common():
        errs = data["tool_errors"].get(tool, 0)
        rate = errs / calls if calls else 0
        rows += (
            f'<tr><td><code>{escape(tool)}</code></td><td class="v">{calls}</td>'
            f'<td class="v">{errs}</td>'
            f'<td class="v {"gap" if rate > 0.2 else "ok"}">{rate:.0%}</td></tr>'
        )

    wrong = ""
    for row in data["incorrect"]:
        badge = ("confidently wrong" if row.get("confidently_wrong")
                 else "caught by verifier")
        cls = "bad" if row.get("confidently_wrong") else ""
        wrong += (
            f'<tr class="{cls}"><td><code>{escape(str(row.get("id")))}</code></td>'
            f'<td>{escape(badge)}</td><td class="v">{row.get("steps", "")}</td>'
            f'<td class="note">{escape(str(row.get("detail", ""))[:110])}</td></tr>'
        )
    wrong_block = (
        f'<table><tr><th>question</th><th>outcome</th><th>steps</th><th>detail</th></tr>'
        f'{wrong}</table>' if wrong else '<p class="none">none recorded</p>'
    )

    unfinished = ""
    for row in data["unfinished"]:
        reason = "no answer produced" if row["empty"] else "verifier failed"
        notes = "; ".join(f.get("detail", "")[:70] for f in row["findings"][:2])
        unfinished += (
            f'<tr><td><code>{escape(row["run"])}</code></td>'
            f'<td>{escape(reason)}</td><td class="v">{row.get("steps", "")}</td>'
            f'<td class="note">{escape(notes)}</td></tr>'
        )
    unfinished_block = (
        f'<table><tr><th>run</th><th>reason</th><th>steps</th><th>findings</th></tr>'
        f'{unfinished}</table>' if unfinished else '<p class="none">none recorded</p>'
    )

    return Template(TEMPLATE).substitute(
        built=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        runs=data["runs"],
        calls=total_calls,
        tiles=tiles,
        tool_rows=rows,
        crashes=_group_block("Crashes — bugs in our code", data["crashes"], "crash"),
        refusals=_group_block("Gateway refusals — the boundary working", data["refusals"], "refusal"),
        wrong=wrong_block,
        unfinished=unfinished_block,
    )


TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agent Failures</title>
<style>
:root {
  --bg:#fbfaf9; --panel:#fff; --ink:#1c1b19; --muted:#6b6660; --line:#e6e2dd;
  --accent:#b4643a; --ok:#3f7d58; --gap:#b0483f; --code:#f4f1ee; --radius:10px;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg:#17161a; --panel:#1f1e23; --ink:#eceaf0; --muted:#9b96a3; --line:#312f38;
  --accent:#e08a5c; --ok:#6cc08a; --gap:#e5796e; --code:#26242b;
} }
:root[data-theme="dark"] {
  --bg:#17161a; --panel:#1f1e23; --ink:#eceaf0; --muted:#9b96a3; --line:#312f38;
  --accent:#e08a5c; --ok:#6cc08a; --gap:#e5796e; --code:#26242b;
}
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
  font:16px/1.6 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif; }
.wrap { max-width:1040px; margin:0 auto; padding:48px 16px 96px; }
code,pre { font-family:ui-monospace,"SF Mono",Menlo,Consolas,monospace; }
h1 { font-size:clamp(28px,5vw,38px); margin:0 0 8px; letter-spacing:-.02em; }
h2 { font-size:13px; text-transform:uppercase; letter-spacing:.09em;
  color:var(--muted); margin:52px 0 16px; font-weight:600; }
p { margin:0 0 10px; }
.sub { color:var(--muted); font-size:15px; max-width:64ch; }
.meta { display:flex; gap:8px; flex-wrap:wrap; margin-top:18px; font-size:12.5px; }
.meta span { background:var(--code); border:1px solid var(--line); padding:3px 9px;
  border-radius:99px; color:var(--muted); }
.tiles { display:grid; gap:12px; grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
  margin-top:28px; }
.tile { background:var(--panel); border:1px solid var(--line); border-radius:var(--radius);
  padding:16px 18px; display:flex; flex-direction:column; gap:3px; }
.tile .n { font-size:30px; font-weight:650; letter-spacing:-.02em; }
.tile .k { font-size:12.5px; color:var(--muted); text-transform:uppercase;
  letter-spacing:.07em; }
.tile.bad .n { color:var(--gap); }
.fails { display:grid; gap:12px; }
.fail { background:var(--panel); border:1px solid var(--line); border-radius:var(--radius);
  padding:15px 17px; border-left:3px solid var(--muted); }
.fail.crash { border-left-color:var(--gap); }
.fail.refusal { border-left-color:var(--accent); }
.fail header { display:flex; justify-content:space-between; gap:12px;
  align-items:baseline; margin-bottom:7px; }
.fail header code { font-size:13px; color:var(--accent); word-break:break-word; }
.count { font-size:12px; color:var(--muted); background:var(--code);
  border-radius:99px; padding:2px 9px; white-space:nowrap; }
.msg { font-size:13.5px; color:var(--muted); margin-bottom:8px; }
details { margin-top:6px; }
summary { cursor:pointer; font-size:12.5px; color:var(--muted); }
pre { background:var(--code); border:1px solid var(--line); border-radius:7px;
  padding:11px 13px; overflow-x:auto; font-size:12px; line-height:1.5; margin:8px 0 0; }
pre.tb { color:var(--gap); }
.runs { font-size:12px; color:var(--muted); margin:8px 0 0; }
.none { color:var(--muted); font-size:14px; font-style:italic; }
table { width:100%; border-collapse:collapse; font-size:14px; }
th { text-align:left; font-size:11.5px; text-transform:uppercase; letter-spacing:.06em;
  color:var(--muted); padding:0 10px 8px; font-weight:600; }
td { padding:10px; border-top:1px solid var(--line); vertical-align:top; }
td.v { font-family:ui-monospace,Menlo,Consolas,monospace; white-space:nowrap; }
td.v.gap { color:var(--gap); } td.v.ok { color:var(--ok); }
td.note { color:var(--muted); font-size:13px; }
tr.bad td { background:color-mix(in srgb, var(--gap) 8%, transparent); }
footer { margin-top:60px; padding-top:18px; border-top:1px solid var(--line);
  color:var(--muted); font-size:13px; }
</style></head><body><div class="wrap">

<h1>Agent Failures</h1>
<p class="sub">Every failure across every run, grouped by signature. A single
transcript shows you one error; this shows you which error keeps happening, which
is the one worth fixing.</p>
<div class="meta"><span>built $built</span><span>$runs runs scanned</span>
<span>$calls tool calls</span></div>

<div class="tiles">$tiles</div>

<h2>Error rate by tool</h2>
<table><tr><th>tool</th><th>calls</th><th>errors</th><th>rate</th></tr>$tool_rows</table>

$crashes

$refusals

<h2>Incorrect answers</h2>
$wrong

<h2>Unfinished or unverified runs</h2>
$unfinished

<footer>Generated by <code>docs/build_failures.py</code> from
<code>runs/**/audit.jsonl</code>, <code>runs/**/runs.jsonl</code> and
<code>runs/**/answer.json</code>. Refusals are the gateway working as designed;
crashes are not.</footer>
</div></body></html>
"""


def main() -> None:
    data = collect()
    OUT.write_text(render(data), encoding="utf-8")
    crashes = sum(len(v) for v in data["crashes"].values())
    refusals = sum(len(v) for v in data["refusals"].values())
    print(f"wrote {OUT.relative_to(ROOT)}")
    print(f"  {data['runs']} runs | {sum(data['tool_calls'].values())} tool calls")
    print(f"  {crashes} crashes ({len(data['crashes'])} distinct)")
    print(f"  {refusals} refusals ({len(data['refusals'])} distinct)")
    print(f"  {len(data['incorrect'])} incorrect | {len(data['unfinished'])} unfinished")


if __name__ == "__main__":
    main()
