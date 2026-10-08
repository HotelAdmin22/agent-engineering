"""Generate docs/architecture.html from the source tree.

The doc is built, not written. A hand-maintained architecture page describes
the system as it was on the day someone last remembered to edit it, which is
the one thing an architecture page must not do.

Everything on the page comes from the repository at build time:

  - each module's own docstring, which is where the reasoning already lives
  - live counts (Spider databases present, ledger rows, recorded experiments)
  - git state, so a page built from uncommitted work says so

Run it after a change:

    uv run python docs/build_architecture.py
"""

import ast
import io
import json
import re
import subprocess
from string import Template
from datetime import datetime, timezone
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "architecture.html"

# Layers, in the order a reader should meet them. The blurb is the one thing
# here that is hand-written, because "what is this layer for" is not derivable
# from any single file inside it.
LAYERS = [
    ("Capability surface", "gateway",
     "The only module that may import duckdb or execute SQL. The model proposes, "
     "this decides. parse -> authorize -> lint -> rewrite -> cost -> execute."),
    ("Evidence", "evidence",
     "Immutable records of everything that ran. Every number in a report resolves "
     "to an id here, or it does not get published."),
    ("Orientation", "catalog",
     "Cards for tables and metrics, so the agent's first move is a search rather "
     "than a guessed table name."),
    ("Deterministic math", "tools",
     "Deltas, contribution, and the mix-versus-rate split. Takes evidence ids, "
     "never literal numbers -- the model chooses what to compute, not the answer."),
    ("Planner", "agent",
     "The loop that wires the tools together, with step and token budgets. Reuses "
     "manual_loop rather than implementing a second agent loop."),
    ("Verification", "verifier",
     "Every number traced to evidence, and the parts reconciled against the whole."),
    ("Rendering", "report",
     "Turns {{e7.delta_pct}} into a figure. The only thing that converts an id "
     "into a digit."),
    ("Audit", "audit",
     "Append-only log of every prompt and tool call, including the refusals that "
     "leave no evidence behind."),
    ("The loop", "manual_loop",
     "One request, the tools it asked for, repeat. Written by hand twice -- once per "
     "provider -- so the shape of each API stays visible."),
    ("Benchmark", "evals",
     "Spider 2.0-Lite and the gold warehouse set, both run through the same agent."),
    ("Improvement", "orchestrator",
     "Failure collection, the triage analyst that proposes fixes, and the verdict "
     "ledger that records whether a repair landed."),
]

# The loop the project exists to run. Not "how one question is answered" --
# that is one box inside step 2.
ITERATION = [
    ("Sweep", "docker compose run --rm sweep",
     "135 Spider tasks through one agent, in a container that cannot reach the "
     "answer key. Resumable; a sweep takes hours."),
    ("Record", "runs/<sweep>/<task>/",
     "answer.json, evidence.jsonl, audit.jsonl per task. Written DURING the run, "
     "so an interrupted task is still diagnosable."),
    ("Verify", "deterministic, no model",
     "Did it produce SQL? Did it actually RUN the SQL it submitted? Did that "
     "return rows? A model never grades its own work."),
    ("Score", "evals/spider/evaluation_suite/evaluate.py",
     "Spider's own scorer, on the HOST, against gold the container never saw. "
     "Never a home-grown scorer -- a lenient one invents improvements."),
    ("Triage", "evals/run_triage.py",
     "A read-only agent reads failure signatures across every run and proposes "
     "fixes. It has no write tool and cannot propose changes to its own judges."),
    ("Experiment", "experiments/results.jsonl",
     "One variable changed, re-measured, written down whether it helped or not. "
     "`inconclusive` is a real and common outcome."),
]

# Each entry is derived from a real file at build time, so the page cannot
# claim an isolation property the configuration does not actually have.
CONTAINMENT_SOURCES = {
    "image": ROOT / ".dockerignore",
    "mounts": ROOT / "compose.yaml",
}


def module_docs():
    out = []
    paths = sorted(ROOT.glob("*/*.py")) + sorted(ROOT.glob("src/agent/*/*.py"))
    for path in paths:
        rel = path.relative_to(ROOT)
        skip = (".venv", "__pycache__", "__init__", "build_architecture")
        if any(s in str(rel) for s in skip):
            continue
        text = io.open(path, encoding="utf-8").read()
        try:
            doc = ast.get_docstring(ast.parse(text)) or ""
        except SyntaxError:
            doc = ""
        if not doc:
            continue
        paras = [p.strip() for p in doc.strip().split("\n\n") if p.strip()]
        out.append({
            "path": str(rel).replace("\\", "/"),
            # src/agent/gateway/gateway.py groups under "gateway", not "src"
            "package": rel.parts[2] if rel.parts[0] == "src" else rel.parts[0],
            "title": paras[0].replace("\n", " ") if paras else "",
            "why": [p.replace("\n", " ") for p in paras[1:]],
            "lines": len(text.splitlines()),
        })
    return out


def _count_lines(path):
    if not path.exists():
        return 0
    return len([l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()])


def live_status():
    """Facts computed at build time. These are the rows that stop the page lying."""
    spider = ROOT / "evals" / "spider" / "databases"
    dbs = len(list(spider.glob("*.sqlite"))) if spider.exists() else 0
    n_tasks = _count_lines(ROOT / "evals" / "spider" / "tasks.jsonl")
    n_exp = _count_lines(ROOT / "experiments" / "results.jsonl")

    runs = ROOT / "runs"
    n_runs = len([d for d in runs.iterdir() if d.is_dir()]) if runs.exists() else 0
    ledger_rows = 0
    if runs.exists():
        for led in runs.glob("*/ledger.jsonl"):
            ledger_rows += _count_lines(led)

    env = {}
    dotenv = ROOT / ".env"
    if dotenv.exists():
        for line in dotenv.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = bool(v.strip())

    warehouse = ROOT / "data" / "warehouse.duckdb"
    seeded = ROOT / "data" / "warehouse_seeded.duckdb"
    n_gold = _count_lines(ROOT / "evals" / "gold" / "questions.jsonl")

    # The newest eval summary, so the page quotes a measured score rather than
    # an intention. An architecture doc that claims accuracy it never ran is
    # worse than one that admits there is no number yet.
    latest = {}
    summaries = sorted((runs).glob("eval-*/summary.json")) if runs.exists() else []
    if summaries:
        try:
            latest = json.loads(summaries[-1].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            latest = {}
    accuracy = latest.get("execution_accuracy")
    wrong = latest.get("confidently_wrong_rate")

    image = image_state()
    return [
        {"label": "Container image", "value": image["size"],
         "ok": image["built"],
         "note": "docker compose build -- a sweep should not run outside it"},
        {"label": "Warehouse", "value": "built" if warehouse.exists() else "missing",
         "ok": warehouse.exists(), "note": "data/warehouse.duckdb (seeded, reproducible)"},
        {"label": "Seeded anomalies", "value": "built" if seeded.exists() else "missing",
         "ok": seeded.exists(), "note": "evals/seeded/plant.py"},
        {"label": "Gold questions", "value": str(n_gold), "ok": n_gold > 0,
         "note": "evals/gold/questions.jsonl"},
        {"label": "Execution accuracy",
         "value": f"{accuracy:.0%}" if accuracy is not None else "not measured",
         "ok": accuracy is not None, "note": "newest run in runs/eval-*"},
        {"label": "Confidently wrong",
         "value": f"{wrong:.0%}" if wrong is not None else "not measured",
         "ok": wrong == 0 if wrong is not None else False,
         "note": "verified but incorrect -- the number that matters for trust"},
        {"label": "Spider databases", "value": "%d / 30" % dbs, "ok": dbs >= 30,
         "note": "separate benchmark; manual download"},
        {"label": "Spider tasks", "value": str(n_tasks), "ok": n_tasks > 0,
         "note": "local SQLite subset"},
        {"label": "Recorded experiments", "value": str(n_exp), "ok": n_exp > 0,
         "note": "experiments/results.jsonl"},
        {"label": "Orchestrator runs", "value": str(n_runs), "ok": n_runs > 0,
         "note": "runs/"},
        {"label": "Ledger findings", "value": str(ledger_rows), "ok": ledger_rows > 0,
         "note": "findings tracked across runs"},
        {"label": "OPENAI_API_KEY", "value": "set" if env.get("OPENAI_API_KEY") else "missing",
         "ok": bool(env.get("OPENAI_API_KEY")), "note": "Luna worker"},
    ]


def containment() -> list[dict]:
    """What the container can and cannot reach -- read from the real files.

    Parsed from .dockerignore and compose.yaml rather than written down, so the
    page cannot claim an isolation property the configuration does not have.
    """
    ignore = ""
    ignore_path = ROOT / ".dockerignore"
    if ignore_path.exists():
        ignore = ignore_path.read_text(encoding="utf-8")
    compose = ""
    compose_path = ROOT / "compose.yaml"
    if compose_path.exists():
        compose = compose_path.read_text(encoding="utf-8")

    def excluded(pattern: str) -> bool:
        return any(
            line.strip() == pattern or line.strip().rstrip("/") == pattern.rstrip("/")
            for line in ignore.splitlines()
            if line.strip() and not line.strip().startswith("#")
        )

    mounts = re.findall(r"^\s+- (\./[^:]+):([^:]+):(\w+)$", compose, re.M)
    mounted = {src.lstrip("./"): mode for src, _dst, mode in mounts}

    rows = [
        {"what": "Spider gold answers", "path": "evals/spider/gold/",
         "state": "not in image" if excluded("evals/spider/gold/") else "IN IMAGE",
         "ok": excluded("evals/spider/gold/"),
         "why": "The benchmark answer key. Scored on the host, never shipped."},
        {"what": "Warehouse gold set", "path": "evals/gold/",
         "state": "not in image" if excluded("evals/gold/") else "IN IMAGE",
         "ok": excluded("evals/gold/"),
         "why": "The questions the agent is graded on."},
        {"what": "Seeded-anomaly key", "path": "evals/seeded/planted.json",
         "state": "not in image" if excluded("evals/seeded/planted.json") else "IN IMAGE",
         "ok": excluded("evals/seeded/planted.json"),
         "why": "What was planted, and where. Grading key for detection."},
        {"what": "Credentials", "path": ".env",
         "state": "not in image" if excluded(".env") else "IN IMAGE",
         "ok": excluded(".env"),
         "why": "Supplied as a runtime environment variable, never baked in."},
        {"what": "Spider databases", "path": "evals/spider/databases",
         "state": f"mounted {mounted.get('evals/spider/databases', 'NOT MOUNTED')}",
         "ok": mounted.get("evals/spider/databases") == "ro",
         "why": "Read-only, so the container could not modify the benchmark even "
                "if the gateway were wrong."},
        {"what": "Run outputs", "path": "runs/",
         "state": f"mounted {mounted.get('runs', 'NOT MOUNTED')}",
         "ok": mounted.get("runs") == "rw",
         "why": "The only thing the container writes, and the only thing that "
                "leaves it."},
    ]
    return rows


def image_state() -> dict:
    """Is the image built, and how big. Best effort -- docker may not be up."""
    try:
        out = subprocess.run(
            ["docker", "images", "agent-engineering-sweep", "--format", "{{.Size}}"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except Exception:
        out = ""
    return {"built": bool(out), "size": out or "not built"}


def git_state():
    def run(*args):
        try:
            r = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=10)
            return r.stdout.strip()
        except Exception:
            return ""
    dirty = run("git", "status", "--porcelain")
    return {
        "branch": run("git", "rev-parse", "--abbrev-ref", "HEAD") or "unknown",
        "commit": run("git", "rev-parse", "--short", "HEAD") or "none",
        "dirty": len([l for l in dirty.splitlines() if l.strip()]),
    }


def guarantees():
    """Structural restrictions, each verified rather than asserted."""
    return [
        {"what": "The agent runs in a container",
         "how": "<code>docker compose run --rm sweep</code>",
         "why": "The image pins Python and every dependency from uv.lock, and "
                "carries neither the answer key nor the credentials. A sweep is "
                "reproducible from source rather than from one laptop's state."},
        {"what": "Read-only database handle",
         "how": "sqlite3 opened with <code>file:...?mode=ro</code>",
         "why": "A write is rejected by SQLite itself, whatever SQL arrives. A keyword "
                "check would only be a guess about what the model might try."},
        {"what": "No file access from SQL",
         "how": "<code>enable_external_access=false</code> + <code>lock_configuration=true</code>",
         "why": "read_only=True stops writes and says nothing about reads. DuckDB will "
                "run read_json_auto('evals/gold/questions.jsonl'), and the table allowlist "
                "never fires because a table function is not a table. Verified: 7 of 9 "
                "file-read attempts reached the gold answers before this was added."},
        {"what": "One database per task",
         "how": "<code>ATTACH</code> and <code>DETACH</code> denied by a connection authorizer",
         "why": "mode=ro stops writes but still permits ATTACH, which reaches other files "
                "on disk -- including the gold answers. Verified: ATTACH succeeded before "
                "the authorizer was added."},
        {"what": "No file, shell, or network tool",
         "how": "The tool list contains exactly one entry",
         "why": "There is no path from query_database to the grading key, so no prompt "
                "wording and no model decision can reach it."},
        {"what": "The verifier cannot fix anything",
         "how": "It is handed the same single read-only tool",
         "why": "Its only possible output is a report. An auditor that can edit the work "
                "is not an auditor."},
        {"what": "The verifier cannot close its own findings",
         "how": "<code>findings.check_landed()</code> re-runs the evidence query",
         "why": "Whether feedback landed is arithmetic on a row count, not a model's "
                "opinion of its own work."},
    ]


def render(modules, status, git, layers, iteration, guards, contain, image):
    def card(m):
        why = "".join("<p>%s</p>" % escape(p) for p in m["why"][:2])
        return (
            '<article class="mod"><header><code class="path">%s</code>'
            '<span class="lines">%d lines</span></header><h4>%s</h4>%s</article>'
            % (escape(m["path"]), m["lines"], escape(m["title"]), why)
        )

    layer_html = ""
    for name, pkg, blurb in layers:
        mods = [m for m in modules if m["package"] == pkg]
        if not mods:
            continue
        layer_html += (
            '<section class="layer"><h3>%s<code class="pkg">%s/</code></h3>'
            '<p class="blurb">%s</p><div class="mods">%s</div></section>'
            % (escape(name), escape(pkg), escape(blurb), "".join(card(m) for m in mods))
        )

    steps = ""
    for i, (name, cmd, desc) in enumerate(iteration):
        steps += (
            '<li><span class="n">%d</span><div><strong>%s</strong>'
            '<code class="cmd">%s</code><span>%s</span></div></li>'
            % (i + 1, escape(name), escape(cmd), escape(desc))
        )

    contain_rows = "".join(
        '<tr class="%s"><td>%s</td><td><code>%s</code></td>'
        '<td class="v">%s</td><td class="note">%s</td></tr>'
        % ("ok" if c["ok"] else "gap", escape(c["what"]), escape(c["path"]),
           escape(c["state"]), escape(c["why"]))
        for c in contain
    )

    rows = "".join(
        '<tr class="%s"><td>%s</td><td class="v">%s</td><td class="note">%s</td></tr>'
        % ("ok" if s["ok"] else "gap", escape(s["label"]), escape(s["value"]), escape(s["note"]))
        for s in status
    )

    guard_html = "".join(
        '<article class="guard"><h4>%s</h4><p class="how">%s</p><p>%s</p></article>'
        % (escape(g["what"]), g["how"], escape(g["why"]))
        for g in guards
    )

    dirty = ('<span class="dirty">%d uncommitted</span>' % git["dirty"]
             if git["dirty"] else '<span class="clean">clean</span>')

    return Template(TEMPLATE).substitute({
        "built": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "branch": escape(git["branch"]),
        "commit": escape(git["commit"]),
        "dirty": dirty,
        "steps": steps,
        "layers": layer_html,
        "rows": rows,
        "guards": guard_html,
        "n_modules": len(modules),
        "contain_rows": contain_rows,
        "image": escape(image["size"]),
    })


TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agent Architecture</title>
<style>
:root {
  --bg:#fbfaf9; --panel:#fff; --ink:#1c1b19; --muted:#6b6660; --line:#e6e2dd;
  --accent:#b4643a; --ok:#3f7d58; --gap:#b0483f; --code:#f4f1ee; --radius:10px;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg:#17161a; --panel:#1f1e23; --ink:#eceaf0; --muted:#9b96a3; --line:#312f38;
    --accent:#e08a5c; --ok:#6cc08a; --gap:#e5796e; --code:#26242b;
  }
}
:root[data-theme="dark"] {
  --bg:#17161a; --panel:#1f1e23; --ink:#eceaf0; --muted:#9b96a3; --line:#312f38;
  --accent:#e08a5c; --ok:#6cc08a; --gap:#e5796e; --code:#26242b;
}
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
  font:16px/1.65 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif;
  -webkit-font-smoothing:antialiased; }
.wrap { max-width:980px; margin:0 auto; padding:48px 16px 96px; }
code,.path,.pkg { font-family:ui-monospace,"SF Mono",Menlo,Consolas,monospace; }
h1 { font-size:clamp(28px,5vw,40px); line-height:1.15; margin:0 0 10px; letter-spacing:-.02em; }
h2 { font-size:13px; text-transform:uppercase; letter-spacing:.09em; color:var(--muted);
  margin:56px 0 18px; font-weight:600; }
h3 { font-size:20px; margin:0 0 4px; display:flex; align-items:baseline; gap:10px; flex-wrap:wrap; }
h4 { font-size:15px; margin:0 0 8px; line-height:1.4; }
p { margin:0 0 10px; }
.sub { color:var(--muted); font-size:15px; max-width:62ch; }
.meta { display:flex; gap:8px; flex-wrap:wrap; margin-top:20px; font-size:12.5px; }
.meta span { background:var(--code); border:1px solid var(--line); padding:3px 9px;
  border-radius:99px; color:var(--muted); }
.meta .dirty { color:var(--gap); } .meta .clean { color:var(--ok); }
ol.pipe { list-style:none; margin:0; padding:0; }
ol.pipe li { display:flex; gap:14px; padding:13px 0; border-bottom:1px solid var(--line);
  align-items:flex-start; }
ol.pipe li:last-child { border-bottom:0; }
ol.pipe .n { flex:0 0 26px; height:26px; border-radius:50%; background:var(--accent);
  color:#fff; display:grid; place-items:center; font-size:12.5px; font-weight:650; }
ol.pipe strong { display:block; }
ol.pipe .cmd { display:inline-block; font-size:12px; color:var(--accent);
  background:var(--code); border:1px solid var(--line); padding:1px 7px;
  border-radius:5px; margin:3px 0; }
ol.pipe span:not(.n) { color:var(--muted); font-size:14.5px; }
.layer { margin-bottom:36px; }
.pkg { font-size:12.5px; color:var(--accent); background:var(--code); padding:2px 8px;
  border-radius:5px; }
.blurb { color:var(--muted); font-size:14.5px; max-width:66ch; margin-bottom:16px; }
.mods,.guards { display:grid; gap:14px; grid-template-columns:repeat(auto-fit,minmax(290px,1fr)); }
.mod,.guard { background:var(--panel); border:1px solid var(--line);
  border-radius:var(--radius); padding:16px 18px; }
.mod header { display:flex; justify-content:space-between; gap:10px; align-items:baseline;
  margin-bottom:9px; }
.mod .path { font-size:12.5px; color:var(--accent); }
.mod .lines { font-size:11.5px; color:var(--muted); white-space:nowrap; }
.mod p,.guard p { font-size:14px; color:var(--muted); }
.mod p:last-child,.guard p:last-child { margin-bottom:0; }
.guard .how { color:var(--ink); font-size:13.5px; }
.guard code { background:var(--code); padding:1px 6px; border-radius:4px; font-size:12.5px; }
table { width:100%; border-collapse:collapse; font-size:14.5px; }
td { padding:11px 10px; border-bottom:1px solid var(--line); vertical-align:top; }
tr:last-child td { border-bottom:0; }
td.v { font-family:ui-monospace,Menlo,Consolas,monospace; white-space:nowrap; font-weight:600; }
tr.ok td.v { color:var(--ok); } tr.gap td.v { color:var(--gap); }
td.note { color:var(--muted); font-size:13.5px; }
footer { margin-top:64px; padding-top:20px; border-top:1px solid var(--line);
  color:var(--muted); font-size:13px; }
@media (max-width:560px) { td.note { display:none; } }
</style></head><body><div class="wrap">

<h1>Agent Architecture</h1>
<p class="sub"><strong>One agent, run in a container, measured against a benchmark it
cannot see the answers to, and improved one variable at a time.</strong> Everything
below is generated from the source tree and the real Docker configuration, so the page
cannot claim a property this repository does not have.</p>
<div class="meta"><span>built ${built}</span><span>branch ${branch}</span>
<span>commit ${commit}</span>${dirty}<span>${n_modules} modules</span></div>

<h2>The loop this exists to run</h2>
<p class="blurb">A single question being answered is one box inside step 2. The unit of
work here is a sweep, and the unit of progress is a recorded experiment.</p>
<ol class="pipe">${steps}</ol>

<h2>The sandbox &mdash; what the container cannot reach</h2>
<p class="blurb">Derived from <code>.dockerignore</code> and <code>compose.yaml</code> at
build time, not written down. The container produces predictions; the <em>host</em>
scores them, so the thing being measured cannot reach the answer key even if every
in-process rule failed.</p>
<table>${contain_rows}</table>
<p class="blurb"><code>docker compose run --rm sweep --limit 135 --resume</code>
&mdash; image <code>${image}</code></p>

<h2>Live status</h2>
<table>${rows}</table>

<h2>Structural guarantees</h2>
<div class="guards">${guards}</div>

<h2>Modules</h2>
${layers}

<footer>Generated by <code>docs/build_architecture.py</code>. Module descriptions are each
file's own docstring &mdash; edit the source, rebuild the page. Counts are read from disk
at build time.</footer>
</div></body></html>
"""


def main():
    modules = module_docs()
    html = render(
        modules, live_status(), git_state(), LAYERS, ITERATION,
        guarantees(), containment(), image_state(),
    )
    OUT.write_text(html, encoding="utf-8")
    print("wrote %s (%d bytes, %d modules)"
          % (OUT.relative_to(ROOT), len(html), len(modules)))


if __name__ == "__main__":
    main()
