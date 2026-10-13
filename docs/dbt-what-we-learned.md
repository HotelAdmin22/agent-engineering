# Spider2-DBT on Luna: seven experiments, and what the failures actually are

Written for whoever picks this up next, including me. The short version: the
agent's pass rate has not moved outside its own noise band in seven
experiments, and the measurements now say fairly clearly why. The useful
content here is the list of things that do **not** work and the evidence that
rules them out, because each one costs about $6 and 3.5 hours to re-learn.

**Why this does not reach 20/64 is documented separately and in full:**
`docs/dbt-why-not-20.md`. The short form: 32 of the 64 tasks have never
been solved once in up to 25 attempts, 2 of those are unpassable by anyone,
and 80% of the remaining failures are defensible readings of tasks that
never say which reading they want.

Companion pages: `docs/luna-scoreboard.html` (generated, the live numbers),
`docs/dbt-run-history.md` (the run ledger). Gold-derived detail lives in
`analysis/`, outside triage's readable roots.

---

## The number

`gpt-6-luna`, 64 tasks, 3 attempts each.

| Run | Change | pass@1 | Tasks |
| --- | --- | --- | --- |
| `luna-20261008-182025` | baseline | 0.234 | 15 |
| `luna-20261009-030506` | prompt: "read the .yml files" | 0.224 | 14 |
| `luna-20261009-143212` | gateway: catalog access | **0.250** | **16** |
| `luna-20261010-165339` | dbt test gate + repair pass | 0.234 | 15 |
| `luna-20261010-211448` | reasoning_effort = high | 0.255 | 16.3 |

Everything sits in **0.224–0.255**. Individual attempts range 0.188–0.297, so
a difference of one or two tasks is not a result. Only `pass^3` has moved at
all: 0.156 → 0.172.

**Two of the 64 tasks cannot be passed by anyone** — `xero_new001`,
`xero_new002`, `social_media001`, `chinook001`, `tpch002` — verified 0 of 9
attempts each across three runs. Broken gold filenames, gold missing the
tables its own spec compares, and one task whose starting database ships 1 of
the 8 source tables it needs. The ceiling is **62/64 = 96.9%**. Report pass@1
on /64 and state the ceiling beside it; do not quietly change denominators.

---

## What the failures are

45 failing tasks, diffed against gold on exactly the tables the evaluator
compares:

| | Tasks | |
| --- | --- | --- |
| Kept rows gold dropped, or dropped rows gold kept | **20 (44%)** | scope choice |
| Same row count, different values | **16 (36%)** | computation choice |
| Never built the model | 5 (11%) | capability |
| Unscoreable (broken gold) | 3 (7%) | not the agent |
| Compared tables all match | 1 (2%) | |
| **Duplicated rows / broken join** | **0** | — |

**Zero broken joins. Zero fan-out.** The agent writes mechanically correct
SQL. Eighty per cent of failures are it making a different *defensible* choice
from the one gold made.

### The constraint is that the tasks are underdetermined

`tpch001` asks for "the purchase status of **each customer**". The agent
returned all 150,000 customers. Gold returned 75,007 — only those with
purchases. The agent's reading is the more literal one, and it lost.

`f1001`, same agent, opposite direction: gold zero-fills 75 rows the agent
left NULL. So in one task gold excludes inactive rows and in another it
includes them as zeros, and **neither task says which**.

Supporting measurements:

- Of 766 declared models, **2%** state their grain explicitly ("one row per
  X"); **74%** say nothing about it.
- `asana001`'s gold **contradicts its own schema file** on column order.
- `f1001` is scored on a top-20 with a tie at the boundary and no tiebreak
  specified.

This explains every null result below. You cannot reason, re-read or
self-check your way to an undocumented convention.

---

## What does not work, with the evidence

### Giving the agent more information — three attempts, all failed

**Catalog access** (`29f7a39`, kept). The gateway refused
`information_schema`, so the agent could not discover what dbt had built and
guessed schema qualification wrongly. Fixing it took refused catalog reads
**168 → 0**, qualification errors 95 → 11, failed-call rate 6.5% → 4.0%.
**Accuracy moved by nothing.** Worth keeping for the cost and the removed
confound; not an accuracy lever.

**Telling the agent to read `models/*.yml`.** max_turns 5 → 29, budget
failures 4 → 23, input tokens 280k → 479k, cost $1.33 → $2.02, accuracy
inside noise. Reverted.

**Injecting the same spec for free from the host.** The block is ~800 tokens
and added **147,000 input tokens per attempt** — 180× its own size — because
it made the agent go build models no task asked for. Turn cap hit 2 → 19, 28
passes against 31. Reverted. The delivery mechanism was never the problem.

### Telling the agent exactly what is wrong — failed

**The dbt test gate.** 80% of these projects declare column tests (809
`not_null`, 280 `unique`) targeting precisely the measured failure modes, and
the agent ran `test` in 23% of attempts. Wiring them in with a repair pass
worked mechanically: `provider001`'s assertions flagged **the same 2,857 rows
an independent gold diff found**. It converted nothing — repaired attempts
passed at **0.222** against **0.259** for attempts whose assertions were
already clean. One task passed, was repaired, and then failed. Cost +16%,
turns +19%. Reverted.

This is the most informative null. The agent was handed its exact failing
rows, by name, and could not use them.

### Making the agent think harder — failed, and made it less consistent

**`reasoning_effort = "high"`.** Reasoning tokens 1,700 → 3,745, so the lever
pulled. pass@1 0.250 → 0.255, which is +0.3 tasks. The **noise band widened**,
0.219–0.266 → 0.188–0.297. Attempts 1 and 2 came in at 0.297 and 0.281, above
the control's entire range; attempt 3 came in at 0.188 on *identical* inputs
(23.0 turns, 3,726 reasoning tokens against 22.7/3,778 and 22.9/3,733).
Cost +57%. Reverted.

Worth recording: this was called a success after two attempts and the third
erased it. A per-attempt rate is one sample.

### More turns — ruled out without spending a run

768 pooled attempts:

```
hit the 40-turn cap  : n= 36   pass rate 0.361
finished on their own: n=732   pass rate 0.238
```

Capped attempts do **better**. Only 4.7% reach the cap. Failing attempts stop
at a median of **19** turns with 21 left unused — they stop because the agent
believes it is finished. And the pass rate *declines* with turns used: 0.358
at 0–9 turns, 0.229 at 15–19, 0.145 at 30–34. Long attempts are long because
the agent is lost. Raising the budget would extend confusion.

### Inferring conventions from pre-built models — tested, dropped without a run

The idea: the workspace database often holds already-built models that
demonstrate the convention the spec omits. Measured before building anything:

- The scored table is present at start in only **11 of 64** tasks.
- The agent **already queries it in 8 of those 11**.
- Doing so does not correlate with passing: 4 pass / 4 fail when it looked,
  1 pass / 2 fail when it did not.

Dead on arrival. Recorded because it is an attractive idea and someone will
have it again.

---

## The one live lead

Chasing the above surfaced a genuine self-inflicted failure in the F1 family.

`f1001`'s `finishes_by_driver` sits in the starting database with 860 rows,
zeros and no NULLs — **matching gold exactly**. The agent rewrote it and
introduced 75 NULLs across six columns, which is the exact failure the gold
diff found. It was never asked to touch that model.

```
f1001  rewrote all 4 of its scored models, all already built correctly
f1002  rewrote all 3
f1003  rewrote all 4
```

All three fail. All three touched 15–18 `.sql` files for a task needing 3–4.
`dbt run` then overwrites the working table with the rewrite.

The ceiling on fixing this is small and should be stated plainly: the median
number of pre-built models rewritten is **0 for both passing and failing
attempts** across all 64 tasks. It is an F1-project pathology, not a general
one. Best case 16 → 19.

---

## If you pick this up

1. **Do not re-run anything in the "what does not work" list** without a new
   reason. Each costs ~$1.30 and ~3.5 hours.
2. **Judge nothing on a single attempt.** The per-attempt range is
   0.188–0.297. pass@1 averages three for exactly this reason, and even three
   leaves a ±3-point band. If you need to detect a 2-task change, you need
   k=6.
3. **The measurement layer has been wrong more often than the agent.** Seven
   separate measurement bugs were found and fixed during this work; in every
   case the agent was right and the thing grading it was wrong. Verify a
   surprising number before acting on it.
4. **The remaining gap is interpretive, not mechanical.** Eighty per cent of
   failures are defensible readings of underspecified tasks. That is a
   model-priors problem, and the harness levers are exhausted.
