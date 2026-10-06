"""Findings that can be checked mechanically, and the ledger that tracks them.

The failure this file exists to prevent: a verifier reports a defect, the
worker runs again, and something -- the worker, the verifier, or the
orchestrator -- declares it fixed because the second run *said* so. Nothing
re-checked it. The loop reports progress it never made.

So a FAIL finding is not prose. It carries a query and the result that query
must produce once the defect is gone. Whether feedback landed is then decided
by running that query, not by asking a model.

Borrowed from SignalPilot's verifier agent (Apache-2.0): four-valued verdicts
rather than pass/fail. NEEDS is the honest outcome when the evidence could not
be gathered, and it is exactly the case a boolean verifier silently converts
into a false PASS.
"""

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


# --- Verdicts ---------------------------------------------------------------

PASS = "PASS"      # evidence ran and showed no defect
FAIL = "FAIL"      # evidence ran and showed a defect
NEEDS = "NEEDS"    # evidence could not be gathered -- NOT a pass
NA = "N/A"         # the check does not apply to this task

VERDICTS = (PASS, FAIL, NEEDS, NA)


# --- What a fixed query must produce ----------------------------------------

ZERO_ROWS = "zero_rows"
NONZERO_ROWS = "nonzero_rows"
ONE_ROW = "one_row"

EXPECTATIONS = (ZERO_ROWS, NONZERO_ROWS, ONE_ROW)


# --- Whether feedback landed ------------------------------------------------

LANDED = "landed"              # re-run evidence now matches expectation
NOT_LANDED = "not_landed"      # defect still present
REGRESSED = "regressed"        # passed before, fails now
UNVERIFIABLE = "unverifiable"  # the evidence query itself failed to run
FALSE_POSITIVE = "false_positive"  # evidence already passed BEFORE any repair


@dataclass
class Finding:
    """One check's outcome on one task, on one run."""

    finding_id: str
    check: str
    verdict: str
    recommendation: str = ""
    evidence_query: str | None = None
    expected_after_fix: str | None = None
    note: str = ""

    def __post_init__(self) -> None:
        if self.verdict not in VERDICTS:
            raise ValueError(f"bad verdict {self.verdict!r}, expected one of {VERDICTS}")
        if self.expected_after_fix and self.expected_after_fix not in EXPECTATIONS:
            raise ValueError(f"bad expectation {self.expected_after_fix!r}")
        # A FAIL without re-checkable evidence cannot be tracked across runs,
        # which is the entire point. Catch it here rather than discovering it
        # three runs later as a permanently 'unverifiable' row.
        if self.verdict == FAIL and not (self.evidence_query and self.expected_after_fix):
            raise ValueError(
                f"FAIL finding {self.finding_id!r} needs both evidence_query and "
                "expected_after_fix, or it can never be confirmed fixed"
            )

    @property
    def is_actionable(self) -> bool:
        return self.verdict == FAIL


def _matches(rows: list, expectation: str) -> bool:
    if expectation == ZERO_ROWS:
        return len(rows) == 0
    if expectation == NONZERO_ROWS:
        return len(rows) > 0
    if expectation == ONE_ROW:
        return len(rows) == 1
    raise ValueError(f"unknown expectation: {expectation}")


def check_landed(finding: Finding, sandbox, candidate_sql: str | None = None) -> tuple[str, str]:
    """Re-run the finding's evidence. Returns (status, detail).

    No model is consulted. The query either produces what a fixed version
    would produce, or it does not.

    An evidence query may contain `{candidate}`, which is replaced by the
    worker's current SQL as a subquery. That is what makes the check test the
    new answer rather than the unchanged source data -- on Spider the worker
    produces a query, not a table, so there is nothing else to inspect.
    """
    if not finding.evidence_query or not finding.expected_after_fix:
        return UNVERIFIABLE, "finding carries no re-checkable evidence"

    sql = finding.evidence_query
    if "{candidate}" in sql:
        if not candidate_sql:
            return UNVERIFIABLE, "evidence needs {candidate} but no worker SQL was supplied"
        sql = sql.replace("{candidate}", candidate_sql.strip().rstrip(";"))

    try:
        con = sandbox._connect()
        try:
            rows = con.execute(sql).fetchall()
        finally:
            con.close()
    except sqlite3.Error as exc:
        return UNVERIFIABLE, f"{type(exc).__name__}: {exc}"

    if _matches(rows, finding.expected_after_fix):
        return LANDED, f"{len(rows)} rows, expectation {finding.expected_after_fix} met"
    return NOT_LANDED, f"{len(rows)} rows, expectation {finding.expected_after_fix} unmet"


class Ledger:
    """Append-only record of findings and whether they were ever resolved.

    Kept as JSONL for the same reason experiments/results.jsonl is: a run that
    is not written down did not happen, and a format you can grep survives
    tooling changes.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, run_id: str, task_id: str, finding: Finding, landing: dict | None = None) -> None:
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "run_id": run_id,
            "task_id": task_id,
            **asdict(finding),
        }
        if landing:
            row["landing"] = landing
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")

    def rows(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(l) for l in self.path.read_text(encoding="utf-8").splitlines() if l.strip()]

    def summarise(self) -> dict:
        """Counts by verdict and by landing status, across every run."""
        rows = self.rows()
        verdicts: dict[str, int] = {}
        landings: dict[str, int] = {}
        for r in rows:
            verdicts[r["verdict"]] = verdicts.get(r["verdict"], 0) + 1
            status = (r.get("landing") or {}).get("status")
            if status:
                landings[status] = landings.get(status, 0) + 1
        return {"total": len(rows), "verdicts": verdicts, "landings": landings}
