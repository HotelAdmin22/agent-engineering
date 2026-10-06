"""Worker -> verifier -> repair -> did-it-land, for one task.

The shape of one cycle:

    1. the worker answers the question                    (Luna, cheap)
    2. a read-only verifier audits the answer             (Claude, stronger)
    3. if it found actionable defects, the worker retries with that feedback
    4. each finding's evidence query is re-run against the NEW answer
    5. everything is written to the ledger, landed or not

Step 4 is the point of the whole file. Without it, step 3 produces a second
answer and an assumption. The assumption is usually wrong in the direction
that flatters the system.

An asymmetric pair on purpose: a verifier no stronger than the worker tends to
ratify the worker's own mistakes, because it shares them.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from manual_loop import loop_openai
from manual_loop.loop import to_jsonable

from .findings import FALSE_POSITIVE, Finding, Ledger, check_landed
from .trace import ConsoleTracer, print_findings, print_landings
from .verifier import verify

WORKER_SYSTEM = """You are answering a question about a SQLite database.

Use the query_database tool to explore the schema and compute the answer. You \
cannot see the database any other way, so start by inspecting it.

When you have the answer, reply with your final SQL in a ```sql fenced block. \
That single query must produce the answer on its own."""

REPAIR_TEMPLATE = """{original}

A read-only auditor reviewed your previous answer:

```sql
{previous_sql}
```

It reported these defects:

{defects}

Produce a corrected query. Address every defect above. Reply with your final \
SQL in a ```sql fenced block."""


@dataclass
class TaskOutcome:
    task_id: str
    first_sql: str | None = None
    repaired_sql: str | None = None
    findings: list[Finding] = field(default_factory=list)
    landings: list[dict] = field(default_factory=list)
    worker_meta: list[dict] = field(default_factory=list)
    verifier_meta: dict = field(default_factory=dict)

    @property
    def actionable(self) -> list[Finding]:
        return [f for f in self.findings if f.is_actionable]

    @property
    def landed_count(self) -> int:
        return sum(1 for l in self.landings if l["status"] == "landed")

    @property
    def false_positive_count(self) -> int:
        return sum(1 for l in self.landings if l["status"] == "false_positive")


def _format_defects(findings: list[Finding]) -> str:
    return "\n".join(
        f"- [{f.check}] {f.recommendation or f.note}" for f in findings
    )


def _run_worker(prompt: str, sandbox, client=None, trace=None):
    from evals.runner import extract_sql

    result = loop_openai.run_loop(
        prompt=prompt,
        tools=sandbox.tools,
        execute=sandbox.execute,
        system=WORKER_SYSTEM,
        client=client,
        trace=trace,
    )
    meta = {
        "stopped_because": result.stopped_because,
        "num_turns": result.num_turns,
        "usage": result.usage_totals,
    }
    return extract_sql(result.final_text), meta, result


def run_task(
    task_id: str,
    question: str,
    sandbox,
    ledger: Ledger,
    run_id: str,
    repair: bool = True,
    out_dir: Path | None = None,
    worker_client=None,
    verifier_client=None,
    verbose: bool = False,
) -> TaskOutcome:
    """One full cycle. Returns what happened; writes it to the ledger."""
    outcome = TaskOutcome(task_id=task_id)
    worker_trace = ConsoleTracer("worker") if verbose else None
    verifier_trace = ConsoleTracer("verify") if verbose else None

    sql, meta, first_result = _run_worker(question, sandbox, worker_client, worker_trace)
    outcome.first_sql = sql
    outcome.worker_meta.append(meta)

    outcome.findings, outcome.verifier_meta = verify(
        question, sql, sandbox, task_id, client=verifier_client, trace=verifier_trace
    )
    if verbose:
        print_findings(outcome.findings)

    actionable = outcome.actionable
    second_result = None

    if actionable and repair:
        repaired, meta2, second_result = _run_worker(
            REPAIR_TEMPLATE.format(
                original=question,
                previous_sql=sql or "(none produced)",
                defects=_format_defects(actionable),
            ),
            sandbox,
            worker_client,
            worker_trace,
        )
        outcome.repaired_sql = repaired
        outcome.worker_meta.append(meta2)

        # The whole point: re-run each finding's own evidence against the new
        # answer. Nothing here asks a model whether it fixed anything.
        #
        # The pre-check matters as much as the post-check. A finding whose
        # evidence already passed against the ORIGINAL answer was never a real
        # defect, and counting it as 'landed' would credit the loop with a
        # repair it did not make -- inflating exactly the number this file
        # exists to keep honest.
        for f in actionable:
            before, before_detail = check_landed(f, sandbox, sql)
            if before == "landed":
                landing = {
                    "finding_id": f.finding_id,
                    "status": FALSE_POSITIVE,
                    "detail": f"evidence already satisfied before repair ({before_detail})",
                }
                outcome.landings.append(landing)
                ledger.record(run_id, task_id, f, landing)
                continue

            status, detail = check_landed(f, sandbox, repaired)
            landing = {"finding_id": f.finding_id, "status": status, "detail": detail}
            outcome.landings.append(landing)
            ledger.record(run_id, task_id, f, landing)

    if verbose and outcome.landings:
        print_landings(outcome.landings)

    # Findings with no repair attempt are still recorded -- an unrepaired FAIL
    # is a real outcome, not a gap in the log.
    recorded = {l["finding_id"] for l in outcome.landings}
    for f in outcome.findings:
        if f.finding_id not in recorded:
            ledger.record(run_id, task_id, f)

    if out_dir:
        d = Path(out_dir) / task_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "cycle.json").write_text(
            json.dumps(
                {
                    "task_id": task_id,
                    "question": question,
                    "first_sql": outcome.first_sql,
                    "repaired_sql": outcome.repaired_sql,
                    "findings": [f.__dict__ for f in outcome.findings],
                    "landings": outcome.landings,
                    "worker": [to_jsonable(r) for r in [first_result, second_result] if r],
                    "verifier_meta": outcome.verifier_meta,
                },
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )

    return outcome


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
