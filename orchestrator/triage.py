"""The triage sub-agent: reads failures, proposes fixes, changes nothing.

It is handed grouped failure signatures across every run -- not one transcript
-- because the useful question is "which error keeps happening", and a single
run cannot answer it.

Three structural restrictions, none of them requested in a prompt:

  1. NO WRITE TOOL. It has three read-only tools and no fourth. The output is a
     list of proposals; applying one is a separate, gated job.

  2. READS ARE ALLOWLISTED. `read_source` resolves the path and refuses
     anything outside the code directories. evals/gold/, evals/seeded/, .env
     and data/ are unreachable, so a triage agent cannot "diagnose" a failure
     by reading the answer key.

  3. PROPOSALS AIMED AT THE JUDGE ARE REJECTED IN CODE. A fix targeting
     gateway/, verifier/, tests/ or the eval sets is dropped at parse time.

That third one is the whole game. An agent scored on "failures went down" has
two ways to win: fix the tool, or weaken whatever reports the failure. In this
project three separate graders have already been wrong -- two too strict, one
too permissive, and the permissive one scored a run 3/3 that was really 2/3.
A fixer optimising against that grader would have found "make the grader more
generous" first, and it would have looked exactly like success.

So the boundary is a path allowlist in a dispatcher, not a sentence in a system
prompt.
"""

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from manual_loop import loop_openai

from .failures import collect

ROOT = Path(__file__).resolve().parent.parent

# Readable: the implementation. Everything else is invisible.
READABLE_ROOTS = [
    ROOT / "src" / "agent",
    ROOT / "manual_loop",
    ROOT / "orchestrator",
    ROOT / "docs",
]
READABLE_SUFFIXES = {".py", ".md"}

# Writable by a later patch stage -- and therefore the only legitimate targets
# for a proposal.
PATCHABLE = ["src/agent/tools/", "src/agent/catalog/", "src/agent/agent/"]

# Never proposable. These decide whether the agent succeeded; a fixer that can
# edit them is grading its own homework.
PROTECTED = [
    "src/agent/gateway/",      # enforces the capability boundary
    "src/agent/verifier/",     # decides whether numbers are backed
    "evals/gold/",             # the answer key
    "evals/seeded/",           # the anomaly key
    "evals/run_evals.py",      # the grader
    "evals/score_anomalies.py",
    "tests/",                  # the regression suite
]

MAX_STEPS = 10
MAX_RESULT_CHARS = 12000

SYSTEM = """You are a triage analyst for an AI data agent. You read failures and
propose fixes. You cannot change anything, and you must not try.

## What you are looking at

Failures grouped by signature across many runs, with counts. A signature seen
once is noise. A signature seen repeatedly is a missing affordance or a bug.

Four classes, which need different fixes:
- CRASH: an unhandled exception in a tool. Always our bug. Has a traceback.
- REFUSAL: the gateway refused the SQL. Usually correct and the model repairs
  itself. A refusal that REPEATS means a tool or a catalog entry is missing.
- INCORRECT: the eval graded the answer wrong.
- UNFINISHED: a run hit a budget without answering.

## What you may propose

Changes to the tools, the catalog, or the planner's prompt and budgets:
  src/agent/tools/   src/agent/catalog/   src/agent/agent/

You may NOT propose changes to the gateway, the verifier, the tests, or the
eval sets. Those decide whether the agent is correct. A proposal that touches
them is rejected automatically and wastes the slot. If a failure looks like it
needs one, say so in the diagnosis and propose nothing.

## Every proposal needs an assertion

An assertion is how someone later checks the fix worked, mechanically, without
asking a model. One of:
  {"kind": "signature_absent", "signature": "<the exact signature>"}
  {"kind": "eval_accuracy_at_least", "value": 0.97}
  {"kind": "error_rate_below", "tool": "run_sql", "value": 0.02}

A proposal whose success cannot be checked is not a proposal.

## Output

End with a single fenced ```json block and nothing after it:

```json
{"findings": [
  {"signature": "compute_delta :: KeyError: no such evidence",
   "occurrences": 11,
   "class": "crash",
   "diagnosis": "compute_delta is called with an evidence id from a previous run...",
   "proposed_change": "Validate the id in _dispatch and return a listing of valid ids...",
   "target_paths": ["src/agent/tools/math.py"],
   "assertion": {"kind": "signature_absent", "signature": "compute_delta :: KeyError: no such evidence"},
   "confidence": "high"}
]}
```

Propose at most three, ordered by occurrences. If nothing recurs, return an
empty list and say so -- inventing work is worse than reporting none."""

JSON_BLOCK = re.compile(r"```json\s*(.*?)```", re.S | re.I)


@dataclass
class TriageFinding:
    signature: str
    occurrences: int
    cls: str
    diagnosis: str
    proposed_change: str
    target_paths: list[str] = field(default_factory=list)
    assertion: dict = field(default_factory=dict)
    confidence: str = "unknown"
    rejected: str = ""          # why this proposal was dropped, if it was

    @property
    def actionable(self) -> bool:
        return not self.rejected


def _protected_hit(paths: list[str]) -> str | None:
    for path in paths:
        norm = str(path).replace("\\", "/").lstrip("./")
        for guard in PROTECTED:
            if norm.startswith(guard) or norm == guard.rstrip("/"):
                return guard
    return None


class TriageTools:
    """Three read-only tools. There is no fourth."""

    def __init__(self, data: dict):
        self.data = data

    def schemas(self) -> list[dict]:
        return [
            {
                "name": "list_failures",
                "description": (
                    "Every failure signature with its count and class, newest "
                    "data. Start here."
                ),
                "input_schema": {"type": "object", "properties": {}, "required": [],
                                 "additionalProperties": False},
            },
            {
                "name": "show_failure",
                "description": "Full detail for one signature: message, traceback, arguments, runs.",
                "input_schema": {
                    "type": "object",
                    "properties": {"signature": {"type": "string"}},
                    "required": ["signature"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "read_source",
                "description": (
                    "Read a source file. Only src/agent, manual_loop, orchestrator "
                    "and docs are readable. The eval sets and credentials are not."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
        ]

    def execute(self, name: str, args: dict) -> tuple[str, bool]:
        try:
            payload, is_error = self._dispatch(name, args)
        except Exception as exc:
            payload, is_error = {"error": f"{type(exc).__name__}: {exc}"}, True
        text = json.dumps(payload, default=str)
        if len(text) > MAX_RESULT_CHARS:
            # Slicing the serialised JSON cuts a string mid-token and hands the
            # model something unparseable. Truncate the payload, then serialise.
            payload = {
                "truncated": True,
                "note": f"result exceeded {MAX_RESULT_CHARS} chars; read a narrower path",
                "head": {k: str(v)[:2000] for k, v in payload.items()},
            }
            text = json.dumps(payload, default=str)
        return text, is_error

    def _dispatch(self, name: str, args: dict) -> tuple[dict, bool]:
        if name == "list_failures":
            rows = []
            for cls in ("crashes", "refusals"):
                for signature, items in self.data[cls].items():
                    rows.append({
                        "signature": signature,
                        "class": cls.rstrip("es") if cls == "crashes" else "refusal",
                        "occurrences": len(items),
                        "runs": len({i["_run"] for i in items}),
                    })
            rows.sort(key=lambda r: -r["occurrences"])
            return {
                "signatures": rows,
                "incorrect_answers": len(self.data["incorrect"]),
                "unfinished_runs": len(self.data["unfinished"]),
                "tool_calls": dict(self.data["tool_calls"]),
                "tool_errors": dict(self.data["tool_errors"]),
            }, False

        if name == "show_failure":
            wanted = args["signature"]
            for cls in ("crashes", "refusals"):
                if wanted in self.data[cls]:
                    items = self.data[cls][wanted]
                    first = items[0]
                    return {
                        "signature": wanted,
                        "occurrences": len(items),
                        "error": first.get("error"),
                        "stage": first.get("stage"),
                        "traceback": first.get("traceback"),
                        "example_args": first.get("args"),
                        "runs": sorted({i["_run"] for i in items})[:10],
                    }, False
            return {"error": f"no such signature: {wanted}"}, True

        if name == "read_source":
            return self._read(args["path"])

        return {"error": f"unknown tool: {name}"}, True

    def _read(self, path: str) -> tuple[dict, bool]:
        try:
            resolved = (ROOT / path).resolve()
        except OSError as exc:
            return {"error": str(exc)}, True

        # Resolve first, THEN check containment: otherwise ../ walks out.
        if not any(
            resolved == root or root in resolved.parents for root in READABLE_ROOTS
        ):
            return {
                "error": f"not readable: {path}",
                "readable_roots": [str(r.relative_to(ROOT)) for r in READABLE_ROOTS],
                "note": "the eval sets and credentials are deliberately unreachable",
            }, True
        if resolved.suffix not in READABLE_SUFFIXES:
            return {"error": f"only {sorted(READABLE_SUFFIXES)} are readable"}, True
        if not resolved.is_file():
            return {"error": f"no such file: {path}"}, True

        return {"path": path, "content": resolved.read_text(encoding="utf-8")[:20000]}, False


def parse_findings(text: str | None) -> list[TriageFinding]:
    if not text:
        return []
    blocks = JSON_BLOCK.findall(text)
    if not blocks:
        return []
    try:
        payload = json.loads(blocks[-1])
    except json.JSONDecodeError:
        return []

    out = []
    for raw in payload.get("findings", []):
        paths = [str(p) for p in (raw.get("target_paths") or [])]
        finding = TriageFinding(
            signature=str(raw.get("signature", "")),
            occurrences=int(raw.get("occurrences") or 0),
            cls=str(raw.get("class", "")),
            diagnosis=str(raw.get("diagnosis", "")),
            proposed_change=str(raw.get("proposed_change", "")),
            target_paths=paths,
            assertion=raw.get("assertion") or {},
            confidence=str(raw.get("confidence", "unknown")),
        )
        # Enforced here, not trusted to the prompt.
        guard = _protected_hit(paths)
        if guard:
            finding.rejected = f"targets protected path {guard}"
        elif not finding.assertion.get("kind"):
            finding.rejected = "no machine-checkable assertion"
        elif not paths:
            finding.rejected = "no target path"
        out.append(finding)
    return out


def triage(trace=None, client=None, max_steps: int = MAX_STEPS) -> dict:
    """Run one triage pass. Returns findings plus the run's own metadata."""
    data = collect()
    tools = TriageTools(data)

    result = loop_openai.run_loop(
        prompt=(
            "Triage the failures in this agent. Start with list_failures, then "
            "read the detail and the source for anything that recurs. Propose at "
            "most three fixes."
        ),
        tools=tools.schemas(),
        execute=tools.execute,
        system=SYSTEM,
        max_iterations=max_steps,
        trace=trace,
        client=client,
    )
    findings = parse_findings(result.final_text)
    return {
        "generated": datetime.now(timezone.utc).isoformat(),
        "runs_scanned": data["runs"],
        "tool_calls_scanned": sum(data["tool_calls"].values()),
        "steps": result.num_turns,
        "stopped_because": result.stopped_because,
        "usage": result.usage_totals,
        "report": result.final_text,
        "findings": [asdict(f) for f in findings],
        "actionable": [asdict(f) for f in findings if f.actionable],
        "rejected": [asdict(f) for f in findings if not f.actionable],
    }
