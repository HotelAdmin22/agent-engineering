"""The one vocabulary. Every verdict and status in the project is defined here.

It was defined twice. `orchestrator/findings.py` and `src/agent/verifier/check.py`
each declared PASS/FAIL/NEEDS, and `planner.py` imported one of them as
`LEDGER_FAIL` to dodge the name clash with the other. That alias was the symptom.

## Verdicts -- did a check pass?

Four values, not two. Borrowed from SignalPilot's verifier agent (Apache-2.0),
and the reason is the third one: a check that could not run is NOT a passing
check. A boolean verifier silently converts "I could not gather the evidence"
into "fine", which is the exact failure a verifier exists to prevent.

## Landings -- did a repair actually work?

Five values, and `false_positive` is the one that is easy to omit and expensive
to omit. Without a pre-check, a finding that was never a real defect scores as
a successful repair and the loop takes credit for work it did not do. The first
smoke test of the orchestrator produced exactly that result.
"""

# --- verdicts ---------------------------------------------------------------

PASS = "PASS"      # evidence ran and showed no defect
FAIL = "FAIL"      # evidence ran and showed a defect
NEEDS = "NEEDS"    # evidence could not be gathered -- NOT a pass
NA = "N/A"         # the check does not apply here

VERDICTS = (PASS, FAIL, NEEDS, NA)


# --- what a repaired query must produce -------------------------------------

ZERO_ROWS = "zero_rows"
NONZERO_ROWS = "nonzero_rows"
ONE_ROW = "one_row"

EXPECTATIONS = (ZERO_ROWS, NONZERO_ROWS, ONE_ROW)


# --- landings ---------------------------------------------------------------

LANDED = "landed"                   # evidence failed before the repair, passes after
NOT_LANDED = "not_landed"           # defect still present
REGRESSED = "regressed"             # passed before, fails now
UNVERIFIABLE = "unverifiable"       # the evidence query itself would not run
FALSE_POSITIVE = "false_positive"   # evidence already passed BEFORE any repair

LANDINGS = (LANDED, NOT_LANDED, REGRESSED, UNVERIFIABLE, FALSE_POSITIVE)
