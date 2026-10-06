# evals

Task sets for measuring the agent.

Empty on purpose. Phase 5.

| File | Purpose |
| --- | --- |
| `dev.json` | Development tasks. Iterated against freely. |
| `heldout.json` | Held-out tasks. Run rarely. |
| `runner.py` | Runs the agent over a task set and scores it. |

## Why two sets

`dev.json` is what you tune against, so you will overfit to it — that is what
it is for. `heldout.json` is the check that the tuning generalized. Looking at
the held-out set after every change destroys the only property that makes it
useful, so run it rarely and deliberately.

Both files start as `[]`. Tasks get added when there is an agent to run them
against, drawn from real failures observed in Phases 0–4 rather than invented
up front.
