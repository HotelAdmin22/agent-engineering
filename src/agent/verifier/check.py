"""Does every number in this report come from evidence, and does it add up?

Two different failures, two different checks:

  UNBACKED NUMBER -- the model typed a digit instead of a placeholder. Caught
  by scanning the TEMPLATE, before rendering. After rendering every number
  looks identical, so a verifier that runs on the output can never tell a cited
  figure from an invented one. This is the check that has to run early.

  RECONCILIATION -- the parts do not sum to the whole. A contribution breakdown
  whose segments miss the total by 8% is arithmetically impossible, so either
  the query was wrong or the segments are not a partition.

Results use the shared verdict vocabulary in src/agent/verdicts.py: FAIL when
evidence shows a defect, NEEDS when the check could not run. A check that could
not run is not a pass.

Named CheckResult, not Finding. Three different things in this project were
called Finding -- a verification outcome, a defect with a re-runnable
assertion, and a proposed tooling change -- and planner.py had to alias one of
them to import the others.
"""

import re
from dataclasses import dataclass, field

from ..evidence import EvidenceStore
from ..report.render import PLACEHOLDER, placeholders

from ..verdicts import FAIL, NEEDS, PASS  # noqa: F401

# Tokens that are legitimately bare in prose. Dates and ISO months name a
# period, they are not a computed quantity -- the agent should not have to run
# a query to write "September 2026".
ALLOWED = re.compile(
    r"""
    \b\d{4}-\d{2}(-\d{2})?\b        # 2026-09, 2026-09-30
  | \bQ[1-4]\b                      # Q3
  | \b(19|20)\d{2}\b                # a bare year
    """,
    re.VERBOSE,
)
NUMERIC = re.compile(r"\d[\d,]*(\.\d+)?%?")

# Components are stored rounded to 2dp, so summing several of them can differ
# from the rounded total by up to half a cent each. Three components -> 0.015.
# 0.05 absorbs that without being wide enough to hide a real arithmetic error,
# which is always orders of magnitude larger than a cent.
TOLERANCE = 0.05


@dataclass
class CheckResult:
    check: str
    verdict: str
    detail: str
    evidence_id: str | None = None


@dataclass
class VerificationResult:
    findings: list[CheckResult] = field(default_factory=list)

    @property
    def failures(self) -> list[CheckResult]:
        return [f for f in self.findings if f.verdict == FAIL]

    @property
    def ok(self) -> bool:
        return not self.failures

    def summary(self) -> str:
        counts: dict[str, int] = {}
        for f in self.findings:
            counts[f.verdict] = counts.get(f.verdict, 0) + 1
        return " ".join(f"{v}={n}" for v, n in sorted(counts.items())) or "no checks ran"


def _strip_placeholders(template: str) -> str:
    return PLACEHOLDER.sub(" ", template)


def find_unbacked_numbers(template: str) -> list[str]:
    """Numeric literals the model typed outside a placeholder."""
    prose = ALLOWED.sub(" ", _strip_placeholders(template))
    return [m.group(0) for m in NUMERIC.finditer(prose)]


def check_report(template: str, store: EvidenceStore) -> VerificationResult:
    result = VerificationResult()

    # 1. every number traces to evidence
    unbacked = find_unbacked_numbers(template)
    result.findings.append(CheckResult(
        check="numbers_backed",
        verdict=FAIL if unbacked else PASS,
        detail=(
            f"{len(unbacked)} number(s) typed directly into the report: "
            f"{', '.join(unbacked[:8])}. Use {{{{evidence_id.field}}}} instead."
            if unbacked else "every number is a placeholder"
        ),
    ))

    # 2. every placeholder resolves
    cited = placeholders(template)
    unresolved = []
    for evidence_id, name in cited:
        try:
            store.resolve(evidence_id, name)
        except KeyError as exc:
            unresolved.append(f"{evidence_id}.{name} ({exc.args[0]})")
    if cited:
        result.findings.append(CheckResult(
            check="placeholders_resolve",
            verdict=FAIL if unresolved else PASS,
            detail="; ".join(unresolved) if unresolved
                   else f"{len(cited)} placeholder(s) resolved",
        ))
    else:
        result.findings.append(CheckResult(
            check="placeholders_resolve",
            verdict=NEEDS,
            detail="the report cites no evidence at all",
        ))

    # 3. reconciliation on every computation the report cites
    for evidence_id in {eid for eid, _ in cited}:
        record = store.get(evidence_id)
        if record is None or record.kind != "computation":
            continue
        result.findings.append(_reconcile(record))

    return result


def _reconcile(record) -> CheckResult:
    """Parts must sum to the whole, within rounding."""
    f = record.fields
    segments = f.get("segments")

    if segments and "total_delta" in f:
        total = sum(s["delta"] for s in segments)
        gap = abs(total - f["total_delta"])
        return CheckResult(
            check="reconciliation",
            verdict=PASS if gap <= TOLERANCE else FAIL,
            detail=(f"segments sum to {total:,.2f} vs total {f['total_delta']:,.2f} "
                    f"(gap {gap:,.2f})"),
            evidence_id=record.id,
        )

    if "total_revenue_delta" in f:
        parts = (f["total_volume_effect"] + f["total_rate_effect"]
                 + f["total_interaction_effect"])
        gap = abs(parts - f["total_revenue_delta"])
        return CheckResult(
            check="reconciliation",
            verdict=PASS if gap <= TOLERANCE else FAIL,
            detail=(f"volume + rate + interaction = {parts:,.2f} vs revenue delta "
                    f"{f['total_revenue_delta']:,.2f} (gap {gap:,.2f})"),
            evidence_id=record.id,
        )

    return CheckResult(
        check="reconciliation",
        verdict=NEEDS,
        detail="no reconcilable totals on this record",
        evidence_id=record.id,
    )
