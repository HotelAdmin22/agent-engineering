# orchestrator

Worker → verifier → repair → **did it actually land**.

Phase 6. The loop that checks whether feedback was implemented, rather than
assuming it was.

## The failure this exists to prevent

A verifier reports a defect. The worker runs again. Something declares it
fixed — because the second run said so, or because a second verification pass
did not happen to re-raise it. Nothing re-checked the original claim. The loop
then reports progress it never made, and the error compounds silently across
runs.

## How it is prevented

A `FAIL` finding is not prose. It carries a query and the result that query
must produce once the defect is gone:

```json
{"check": "CHECK 3", "verdict": "FAIL",
 "recommendation": "aggregate; the question asks for a total",
 "evidence_query": "SELECT * FROM ({candidate})",
 "expected_after_fix": "one_row"}
```

`{candidate}` is substituted with whatever SQL the worker produces next. The
query is then run and the row count compared. **No model is asked whether its
own feedback landed.**

A `FAIL` that arrives without both fields is demoted to `NEEDS` at parse time.
A finding nobody can re-run is a finding nobody can confirm was fixed, and
allowing it into the ledger would quietly create a permanently untrackable row.

## Four verdicts, not two

From SignalPilot's verifier agent (Apache-2.0):

| Verdict | Meaning |
| --- | --- |
| `PASS` | Evidence ran, no defect |
| `FAIL` | Evidence ran, defect shown |
| `NEEDS` | Evidence could not be gathered — **not** a pass |
| `N/A` | Check structurally inapplicable |

`NEEDS` is the case a boolean verifier silently converts into a false `PASS`.

## Five landing statuses

| Status | Meaning |
| --- | --- |
| `landed` | Evidence failed before the repair, passes after |
| `not_landed` | Defect still present |
| `regressed` | Passed before, fails now |
| `unverifiable` | The evidence query itself would not run |
| `false_positive` | Evidence **already passed before any repair** |

`false_positive` is the one that is easy to omit and expensive to omit. Without
a pre-check, a finding that was never a real defect is scored as a successful
repair, and the loop takes credit for work it did not do. The first smoke test
of this module produced exactly that result before the pre-check was added.

## Structural guarantees

Both agents run inside `evals/sandbox.py` — one database, read-only, `ATTACH`
denied. The verifier therefore:

1. **Cannot fix anything.** It has `query_database` and nothing else, so its
   only possible output is a report.
2. **Cannot reach the gold answers.** Verification is no more of a cheating
   surface than the task.
3. **Cannot close its own findings.** The landing check is arithmetic on a row
   count, performed by `findings.check_landed`.

## Model split

Luna (`gpt-5.6-luna`) works, Claude verifies. Asymmetric on purpose: a verifier
no stronger than the worker tends to ratify the worker's own mistakes, because
it shares them. Cheap worker, expensive judge, and the judge runs once per task
rather than once per turn.

## Files

| File | Purpose |
| --- | --- |
| `findings.py` | `Finding`, verdicts, `check_landed`, `Ledger` |
| `verifier.py` | The read-only auditor and its report parser |
| `orchestrate.py` | One full cycle per task |

`manual_loop/loop_openai.py` is the Luna worker — same `Result` as
`manual_loop/loop.py`, so callers do not know which provider ran.

## Not yet done

- **No eval attached.** `evals/spider/databases/` is empty (0 of 30) and
  `experiments/results.jsonl` has no rows. Until the baseline runs, this loop
  has no number to move, and "the verifier helps" is an untested belief.
- **The verifier has never run live.** `manual_loop/loop.py` needs
  `ANTHROPIC_API_KEY`; only `CLAUDE_CODE_OAUTH_TOKEN` is set, which the
  low-level SDK does not accept. Both smoke tests stubbed the verifier and ran
  the worker for real.
