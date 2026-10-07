# evals

Task sets for measuring the agent.

Empty on purpose. Phase 5.

| File | Purpose |
| --- | --- |
| `gold/questions.jsonl` | Warehouse questions, iterated against freely. |
| `spider/` | Spider 2.0-Lite: 135 tasks, 30 databases, its own scorer. |
| `runner.py` | Runs the agent over a task set. |

## Why two sets

`dev.json` is what you tune against, so you will overfit to it — that is what
it is for. `heldout.json` is the check that the tuning generalized. Looking at
the held-out set after every change destroys the only property that makes it
useful, so run it rarely and deliberately.

`dev.json` and `heldout.json` were placeholders that stayed `[]` for the whole
project and were removed. Spider 2.0-Lite is the real task set: 135 tasks over
30 unfamiliar databases, with an upstream scorer, which is the eval surface a
12-question warehouse set could never provide.
