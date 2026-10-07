

import json
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from manual_loop import loop_openai

from ..audit import AuditLog
from ..catalog import Catalog
from ..evidence import EvidenceStore
from ..gateway import Gateway
from ..report import Rendered, render
from ..tools import compute_contribution, compute_delta, mix_vs_rate
from orchestrator.findings import Finding, Ledger

from ..verdicts import FAIL, LANDED, NOT_LANDED, REGRESSED

from ..verifier import VerificationResult, check_report

MAX_STEPS = 12
MAX_TOKENS = 120_000
PREVIEW_ROWS = 10

SYSTEM = """You are a data analyst answering a question about a DuckDB warehouse.

## How to work

1. Call `search_catalog` first. It knows the tables and the metric definitions,
   including which filters a metric requires.
2. Use `describe_table` and `get_join_path` before writing a join.
3. Run SQL with `run_sql`. You never get a database connection; every query is
   authorized first and may be refused. A refusal explains itself -- read it and
   repair the query.
4. For any comparison between two periods, run one query per period and then use
   `compute_delta`, `compute_contribution` or `mix_vs_rate`. Do not subtract
   numbers yourself.

## The one rule that matters

**You must never write a digit that you computed or read off a result.**

Every number in your final answer must be a placeholder of the form
`{{evidence_id.field}}` -- for example `{{e7.delta_pct}}`. The placeholders are
filled in from the evidence store after you finish. A number you type yourself
is rejected by the verifier and the answer is thrown away.

Dates and period names ("2026-09", "Q3") may be written normally.

## Finishing

End with your analysis as prose containing placeholders. Say what changed, and
why, and name the mechanism -- a volume effect and a rate effect are different
answers and the tools can tell them apart."""


def _tool_schemas() -> list[dict]:
    """Anthropic-shaped: manual_loop.loop_openai converts to the Responses API."""
    return [
        {
            "name": "search_catalog",
            "description": (
                "Find relevant tables and metric definitions. Call this first. "
                "Metric results include a sql_hint with the required filters."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
        },
        {
            "name": "describe_table",
            "description": "Columns, types, row count and declared joins for one table.",
            "input_schema": {
                "type": "object",
                "properties": {"table": {"type": "string"}},
                "required": ["table"],
                "additionalProperties": False,
            },
        },
        {
            "name": "get_join_path",
            "description": "The declared join key between two tables.",
            "input_schema": {
                "type": "object",
                "properties": {"left": {"type": "string"}, "right": {"type": "string"}},
                "required": ["left", "right"],
                "additionalProperties": False,
            },
        },
        {
            "name": "run_sql",
            "description": (
                "Run one read-only SELECT. Returns an evidence id, the columns, "
                "the row count and the first rows -- never the whole result. "
                "May be refused; the refusal says why."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"sql": {"type": "string"}},
                "required": ["sql"],
                "additionalProperties": False,
            },
        },
        {
            "name": "compute_delta",
            "description": (
                "Change between two single-row results. Takes evidence ids, not "
                "numbers."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "current_evidence_id": {"type": "string"},
                    "previous_evidence_id": {"type": "string"},
                    "column": {"type": "string"},
                },
                "required": ["current_evidence_id", "previous_evidence_id", "column"],
                "additionalProperties": False,
            },
        },
        {
            "name": "compute_contribution",
            "description": (
                "How much each segment contributed to a total change. Both "
                "evidence records must be grouped by the same label column."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "current_evidence_id": {"type": "string"},
                    "previous_evidence_id": {"type": "string"},
                    "label_column": {"type": "string"},
                    "value_column": {"type": "string"},
                },
                "required": [
                    "current_evidence_id", "previous_evidence_id",
                    "label_column", "value_column",
                ],
                "additionalProperties": False,
            },
        },
        {
            "name": "mix_vs_rate",
            "description": (
                "Split a revenue change into volume, rate and interaction effects. "
                "Both evidence records need a label, a units column and a revenue "
                "column. Use this to answer WHY revenue moved."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "current_evidence_id": {"type": "string"},
                    "previous_evidence_id": {"type": "string"},
                    "label_column": {"type": "string"},
                    "units_column": {"type": "string"},
                    "revenue_column": {"type": "string"},
                },
                "required": [
                    "current_evidence_id", "previous_evidence_id",
                    "label_column", "units_column", "revenue_column",
                ],
                "additionalProperties": False,
            },
        },
    ]


@dataclass
class AnswerRun:
    question: str
    run_id: str
    template: str | None = None
    rendered: Rendered | None = None
    verification: VerificationResult | None = None
    repaired: bool = False
    landings: list[dict] = field(default_factory=list)
    steps: int = 0
    stopped_because: str = ""
    usage: dict = field(default_factory=dict)
    evidence_ids: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(
            self.verification and self.verification.ok
            and self.rendered and self.rendered.ok
        )

    @property
    def answer(self) -> str:
        return self.rendered.text if self.rendered else "(no answer produced)"


class Planner:
    """Wires the catalog, gateway and tools into one tool surface."""

    def __init__(
        self,
        warehouse: Path | str = "data/warehouse.duckdb",
        run_dir: Path | str | None = None,
        run_id: str | None = None,
    ):
        self.run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        self.run_dir = Path(run_dir) if run_dir else Path("runs") / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)

        self.store = EvidenceStore(self.run_dir / "evidence.jsonl")
        self.gateway = Gateway(warehouse, store=self.store)
        self.catalog = Catalog(self.gateway)
        self.audit = AuditLog(self.run_dir / "audit.jsonl", run=self.run_id)
        # Same ledger type the orchestrator uses, so repair outcomes across
        # both loops are queryable the same way.
        self.ledger = Ledger(self.run_dir / "ledger.jsonl")

    # --- tool dispatch ---------------------------------------------------

    def execute(self, name: str, args: dict) -> tuple[str, bool]:
        """Returns (content, is_error). Errors are returned, never raised."""
        self.audit.event("tool_call", tool=name, args=args)
        tb = None
        try:
            payload, is_error = self._dispatch(name, args)
        except Exception as exc:                      # surfaced to the model
            # The model gets one line; the audit log gets the whole traceback.
            # A tool that keeps failing is a tooling bug, and "KeyError: units"
            # without a stack is not enough to find it.
            tb = traceback.format_exc()
            payload, is_error = {"error": f"{type(exc).__name__}: {exc}"}, True
        text = json.dumps(payload, default=str)
        self.audit.event(
            "tool_result",
            tool=name,
            is_error=is_error,
            chars=len(text),
            # Refusals carry their reason; crashes carry their stack.
            error=payload.get("error") or payload.get("reason") if is_error else None,
            stage=payload.get("stage") if is_error else None,
            traceback=tb,
            args=args if is_error else None,
        )
        return text, is_error

    def _dispatch(self, name: str, args: dict) -> tuple[dict, bool]:
        if name == "search_catalog":
            return {"results": self.catalog.search(args["query"])}, False

        if name == "describe_table":
            result = self.catalog.describe_table(args["table"])
            return result, "error" in result

        if name == "get_join_path":
            result = self.catalog.get_join_path(args["left"], args["right"])
            return result, "error" in result

        if name == "run_sql":
            decision, record = self.gateway.run_sql(args["sql"])
            if not decision.allowed:
                return {"refused": True, "stage": decision.stage,
                        "reason": decision.reason}, True
            return record.preview(limit=PREVIEW_ROWS), False

        if name == "compute_delta":
            record = compute_delta(
                self.store, args["current_evidence_id"],
                args["previous_evidence_id"], args["column"],
            )
        elif name == "compute_contribution":
            record = compute_contribution(
                self.store, args["current_evidence_id"],
                args["previous_evidence_id"], args["label_column"],
                args["value_column"],
            )
        elif name == "mix_vs_rate":
            record = mix_vs_rate(
                self.store, args["current_evidence_id"],
                args["previous_evidence_id"], args["label_column"],
                args["units_column"], args["revenue_column"],
            )
        else:
            return {"error": f"unknown tool: {name}"}, True

        return {"evidence_id": record.id, "fields": record.fields}, False

    def _force_answer(self, items: list, client=None) -> str | None:
        """One tool-free turn, to convert gathered evidence into an answer."""
        from openai import OpenAI

        self.audit.event("force_answer", items=len(items))
        client = client or OpenAI()
        try:
            response = client.responses.create(
                model=loop_openai.MODEL,
                instructions=SYSTEM,
                input=list(items) + [{
                    "role": "user",
                    "content": (
                        "Your step budget is spent. Write your final answer now "
                        "from the evidence you already have. Do not call any more "
                        "tools. Every number must still be a "
                        "{{evidence_id.field}} placeholder. If the evidence is "
                        "incomplete, say so and answer what it does support."
                    ),
                }],
            )
        except Exception as exc:                       # a failed rescue is not fatal
            self.audit.event("force_answer_failed", error=str(exc))
            return None
        return response.output_text

    # --- the run ---------------------------------------------------------

    def answer(
        self,
        question: str,
        max_steps: int = MAX_STEPS,
        max_tokens: int = MAX_TOKENS,
        repair: bool = True,
        trace=None,
        client=None,
    ) -> AnswerRun:
        run = AnswerRun(question=question, run_id=self.run_id)
        self.audit.event("question", question=question)

        result = loop_openai.run_loop(
            prompt=question,
            tools=_tool_schemas(),
            execute=self.execute,
            system=SYSTEM,
            max_iterations=max_steps,
            max_total_tokens=max_tokens,
            trace=trace,
            client=client,
        )
        run.template = result.final_text
        run.steps = result.num_turns
        run.stopped_because = result.stopped_because
        run.usage = result.usage_totals

        # A budget that throws away the work it paid for is not a budget, it is
        # a leak. On exhaustion, ask once more with no tools: the evidence is
        # already gathered and an answer from it beats nothing at all.
        if result.stopped_because in ("max_iterations", "token_budget") and not run.template:
            run.template = self._force_answer(result.messages, client)
            run.stopped_because += "+forced_answer"

        run.verification = check_report(run.template or "", self.store)

        # One repair attempt, recorded the way orchestrator/ records them: which
        # checks were failing before, which are failing after, and therefore
        # whether the repair actually landed.
        #
        # The naive version -- "it failed, we retried, it passes now" -- cannot
        # tell a real fix from a check that was never going to fail twice. Same
        # reason the orchestrator pre-checks before crediting a repair.
        if repair and not run.verification.ok:
            before = {f.check for f in run.verification.failures}
            self.audit.event("repair", failing=sorted(before))

            complaints = "\n".join(
                f"- [{f.check}] {f.detail}" for f in run.verification.failures
            )
            repaired = loop_openai.run_loop(
                prompt=(
                    f"{question}\n\nYour previous answer was rejected by the "
                    f"verifier:\n\n{complaints}\n\nRewrite it. Every number must be "
                    f"a {{{{evidence_id.field}}}} placeholder. Reuse the evidence you "
                    f"already gathered; you may run more queries if you need to."
                ),
                tools=_tool_schemas(),
                execute=self.execute,
                system=SYSTEM,
                max_iterations=max_steps,
                max_total_tokens=max_tokens,
                trace=trace,
                client=client,
            )
            run.template = repaired.final_text
            run.steps += repaired.num_turns
            run.repaired = True
            run.verification = check_report(run.template or "", self.store)

            after = {f.check for f in run.verification.failures}
            for check in sorted(before | after):
                if check in before and check not in after:
                    status = LANDED
                elif check in before:
                    status = NOT_LANDED
                else:
                    status = REGRESSED      # repairing one check broke another
                landing = {"check": check, "status": status}
                run.landings.append(landing)
                self.ledger.record(
                    self.run_id,
                    question[:60],
                    Finding(
                        finding_id=f"{self.run_id}:{check}",
                        check=check,
                        verdict=FAIL,
                        recommendation="verifier failure on the first answer",
                        evidence_query="SELECT 1",          # re-checked by check_report,
                        expected_after_fix="one_row",       # not by SQL; see note above
                        note=status,
                    ),
                    landing,
                )
            self.audit.event("repair_outcome", landings=run.landings)

        run.rendered = render(run.template or "", self.store)
        run.evidence_ids = run.rendered.used
        self.audit.event(
            "answer",
            ok=run.ok,
            steps=run.steps,
            evidence=run.evidence_ids,
            verification=run.verification.summary(),
        )
        (self.run_dir / "answer.json").write_text(
            json.dumps(
                {
                    "question": question,
                    "template": run.template,
                    "answer": run.answer,
                    "ok": run.ok,
                    "steps": run.steps,
                    "usage": run.usage,
                    "evidence_ids": run.evidence_ids,
                    "findings": [vars(f) for f in run.verification.findings],
                    "landings": run.landings,
                },
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        return run
