"""The verifier sub-agent: a read-only auditor of one worker answer.

Three properties, each structural rather than requested:

  1. It cannot fix anything. It gets the same `query_database` tool the worker
     had and nothing else, so its only output is a report.
  2. It cannot reach the gold answers. It runs inside the same Sandbox as the
     worker -- one database, read-only, ATTACH denied -- so verification is no
     more of a cheating surface than the task itself.
  3. It cannot close its own findings. Every FAIL must ship a query and the
     result a fixed version would produce; orchestrator.findings re-runs that
     query later. The verifier never decides whether its feedback landed.

The prompt style -- four-valued verdicts, and a `because` on every rule -- is
taken from SignalPilot's verifier agent (Apache-2.0).
"""

import json
import re

from manual_loop import loop as claude_loop
from manual_loop import loop_openai as luna_loop
from .findings import EXPECTATIONS, Finding

# Runs on OpenAI for now: the Anthropic key the Claude loop needs is not set,
# and a verifier that has never executed is not a verifier. The asymmetric
# worker/judge split is still the goal -- point this at a stronger model than
# the worker once there is a key for one.
WORKERS = {"luna": luna_loop, "claude": claude_loop}
DEFAULT_WORKER = "luna"

JSON_BLOCK = re.compile(r"```json\s*(.*?)```", re.S | re.I)

SYSTEM = """You are a read-only structure auditor. Return a report. Fix nothing.

You are given a question about a SQLite database and the SQL another agent \
produced as its answer. Use `query_database` to gather evidence, then report.

## Verdicts

Use PASS only when the evidence you needed actually ran, because missing \
evidence cannot confirm correctness. Use NEEDS when evidence was unavailable, \
because an unverified check is not a passing one. Use N/A only when a check is \
structurally inapplicable to this task.

## Checks

CHECK 1 - Executability. Run the candidate SQL. FAIL if it errors, because an \
answer that does not run is not an answer.

CHECK 2 - Output shape. Infer the expected cardinality from the question \
("how many" means one row and one column; "for each X" means one row per X; \
"top N" means at most N). FAIL a measurable mismatch, because a shape the \
question did not ask for is the wrong answer even when the numbers are right.

CHECK 3 - Grain and fan-out. If the SQL joins, compare COUNT(*) against \
COUNT(DISTINCT <grain key>) on the joined set. FAIL inflation, because a join \
that duplicates rows silently multiplies every aggregate over them.

CHECK 4 - Filter completeness. Check every condition the question implies is \
present in the SQL. FAIL a missing filter, because an unfiltered population \
answers a different question.

CHECK 5 - NULL handling. If a filtered or joined column contains NULLs, \
determine whether they silently drop rows. FAIL silent loss, because NULL \
elimination is invisible in the result.

## Output

Report your findings as a single fenced ```json block, and nothing after it:

```json
{"findings": [
  {"check": "CHECK 3",
   "verdict": "FAIL",
   "recommendation": "join on (customer_id, order_date), not customer_id alone",
   "evidence_query": "SELECT customer_id, COUNT(*) FROM ... GROUP BY 1 HAVING COUNT(*) > 1",
   "expected_after_fix": "zero_rows",
   "note": "6 joined rows against 4 distinct orders"}
]}
```

Write `evidence_query` against `{candidate}` wherever you mean the worker's \
query, for example `SELECT a, COUNT(*) FROM ({candidate}) GROUP BY 1 HAVING \
COUNT(*) > 1`. It is substituted with whatever SQL the worker produces next, \
because a check that only inspects source data cannot tell whether the answer \
changed.

Every FAIL must carry `evidence_query` and `expected_after_fix`, because a \
finding nobody can re-run is a finding nobody can confirm was fixed. \
`expected_after_fix` is one of: zero_rows, nonzero_rows, one_row -- it \
describes what that query returns once the defect is gone. PASS, NEEDS and \
N/A findings may omit both."""

PROMPT = """Question:
{question}

Candidate SQL from the worker:
```sql
{sql}
```

Audit it. Gather evidence with query_database, then return your JSON report."""


def parse_findings(text: str | None, task_id: str) -> list[Finding]:
    """Pull findings out of the verifier's report.

    A malformed report is dropped with a NEEDS rather than crashing the run:
    the orchestrator's job is to keep going and record that verification did
    not happen, not to fail the whole eval on one bad block.
    """
    if not text:
        return [Finding(f"{task_id}:verifier:noreport", "VERIFIER", "NEEDS",
                        note="verifier produced no text")]

    blocks = JSON_BLOCK.findall(text)
    if not blocks:
        return [Finding(f"{task_id}:verifier:noblock", "VERIFIER", "NEEDS",
                        note="no ```json block in verifier report")]

    try:
        payload = json.loads(blocks[-1])
    except json.JSONDecodeError as exc:
        return [Finding(f"{task_id}:verifier:badjson", "VERIFIER", "NEEDS",
                        note=f"unparseable report: {exc}")]

    out: list[Finding] = []
    for i, raw in enumerate(payload.get("findings", [])):
        check = str(raw.get("check", f"CHECK?{i}"))
        slug = check.lower().replace(" ", "")
        try:
            out.append(
                Finding(
                    finding_id=f"{task_id}:{slug}",
                    check=check,
                    verdict=str(raw.get("verdict", "NEEDS")).upper(),
                    recommendation=str(raw.get("recommendation", "")),
                    evidence_query=raw.get("evidence_query"),
                    expected_after_fix=raw.get("expected_after_fix"),
                    note=str(raw.get("note", "")),
                )
            )
        except ValueError as exc:
            # A FAIL the verifier did not make re-checkable is demoted, not
            # dropped: the observation survives, but it cannot be tracked, and
            # the ledger will say so.
            out.append(
                Finding(
                    finding_id=f"{task_id}:{slug}:demoted",
                    check=check,
                    verdict="NEEDS",
                    recommendation=str(raw.get("recommendation", "")),
                    note=f"demoted from FAIL: {exc}",
                )
            )
    return out


def verify(question: str, sql: str | None, sandbox, task_id: str, client=None,
           trace=None, worker: str = DEFAULT_WORKER) -> tuple[list[Finding], dict]:
    """Audit one answer. Returns (findings, run metadata)."""
    if not sql:
        return [Finding(f"{task_id}:check1:executability", "CHECK 1", "FAIL",
                        recommendation="produce SQL in a ```sql block",
                        evidence_query="SELECT 1", expected_after_fix="one_row",
                        note="worker produced no SQL")], {"skipped": True}

    result = WORKERS[worker].run_loop(
        prompt=PROMPT.format(question=question, sql=sql),
        tools=sandbox.tools,
        execute=sandbox.execute,
        system=SYSTEM,
        client=client,
        trace=trace,
    )
    meta = {
        "stopped_because": result.stopped_because,
        "num_turns": result.num_turns,
        "worker": worker,
        "usage": result.usage_totals,
    }
    return parse_findings(result.final_text, task_id), meta
