# analysis/ — host-only, gold-derived

Everything here was produced by diffing submitted databases against the
Spider2 gold databases. It therefore contains the answer key in prose: exact
reference row counts, which columns differ, what the reference query must have
done.

**This directory is deliberately outside `orchestrator/triage.py`'s
`READABLE_ROOTS`** (`src/agent`, `manual_loop`, `orchestrator`, `docs`). An
agent cannot read it, and it must stay that way. It is also in `PROTECTED`, so
no proposal can target it.

If you want a finding from here to reach an agent, restate it as a general
engineering rule with no task named and no reference value quoted, and put
that in `docs/`. "Check whether an aggregate should cover every row of the
source or only rows with activity" is safe. "Task X's answer has 75,007 rows"
is not.

The agent-safe counterpart is `docs/dbt-run-history.md`.
