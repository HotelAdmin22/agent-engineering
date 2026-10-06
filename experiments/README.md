# experiments

A log of measured changes to the agent.

Empty on purpose. There is nothing to measure until there is an agent worth
measuring and an eval to measure it with (Phase 5).

## The rule

**Change one variable at a time.** An experiment that changes the prompt *and*
the tool surface *and* the model teaches nothing, because no result can be
attributed to a cause.

## What each entry should eventually record

`results.jsonl` holds one JSON object per experiment:

| Field | Meaning |
| --- | --- |
| `id` | Experiment identifier |
| `hypothesis` | What you expected to happen, written *before* the run |
| `variable_changed` | The single thing that differs from the baseline |
| `accuracy` | Score on the eval set |
| `turns` | Agent turns taken |
| `tokens` | Tokens consumed |
| `cost` | Dollar cost of the run |
| `result` | What actually happened |
| `decision` | `keep` / `revert` / `inconclusive` |

`inconclusive` is a real, common, and honest outcome. Record it rather than
talking yourself into `keep`.

Write entries by hand. Automation comes later, if ever — the discipline matters
more than the tooling.
