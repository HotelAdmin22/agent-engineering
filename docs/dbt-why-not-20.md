# Why `gpt-6-luna` does not reach 20/64, and what would have to change

The target was 20 of 64 tasks on Spider2-DBT. Eight experiments and roughly
1,500 scored attempts later, the established figure is **16.8/64 (95% CI
15.7–18.0)**. This page is the account of why, written so the next person does
not spend another week and ~$14 rediscovering it.

**It does not say 20 is impossible.** It says 20 is 4.3 tasks away, names
exactly where those tasks would have to come from, and shows that every lever
available from the harness has been tried and measured at zero.

---

## 1. The number, and how firm it is

Pooled over the 9 attempts of the current prompt family:

```
pass@1 = 0.257 = 16.4/64      95% CI 15.6 – 17.3 tasks
```

That is the tightest estimate in the project, and the control's 16.0 sits
inside it. **The gap to 20 is 3.6 tasks.**

Across all eight runs, individual attempts of **identical configurations**
have landed anywhere from **12 to 19 tasks**. That seven-task spread is pure
sampling noise, and it is larger than any effect measured in any experiment.

This matters for a specific reason: **a single attempt scoring 19 or 20 is not
evidence of a 19- or 20-task system.** It happened twice here (`19/18/12` and
`18/16/19`) and both runs averaged 16–17. If you have seen a 20/64 reported
for this model, the first question is whether it was pass@1 over k≥3 or a
single attempt. Given this distribution, a one-off 20 is entirely consistent
with a true rate of 16.

---

## 2. Where the 64 tasks actually stand

Every task, across up to 25 attempts each:

| | Tasks | Contributes to pass@1 |
| --- | --- | --- |
| Solved **every** time | 5 | 5.0 |
| Flaky — solved **sometimes** | 27 | 10.7 of a possible 27 |
| **Never solved, in up to 25 attempts** | **32** | 0.0 |
| | **64** | **15.7** |

Two hard facts fall out of this.

**The capability envelope is 32/64.** The model has demonstrated, at least
once, that it can solve exactly half the benchmark. The other half it has
never solved in 25 tries.

**16.3 tasks are locked up in flakiness.** The 27 flaky tasks are worth 27 if
made reliable and currently return 10.7. That headroom is where any realistic
gain lives.

### Of the 32 never-solved, two are unpassable by anyone

**Corrected 2026-10-12.** This page previously said five, and a ceiling of
59/64. That was wrong, and the error is worth keeping visible because it
flattered the result.

Genuinely unpassable &mdash; **2 tasks**:

- `chinook001`: its gold database does not contain `dim_customer`,
  `fct_invoice` or `obt_invoice`, the three tables its own eval spec compares.
  There is nothing to be scored against.
- `tpch002`: ships a starting database with 1 of the 8 source tables its
  project needs, no seeds, and an empty `packages.yml`. It cannot be built.

**Unscoreable, but not unpassable &mdash; 3 tasks.** `xero_new001`,
`xero_new002` and `social_media001` name a gold file their directory does not
contain, so `evaluate.py` raised and they scored 0 whatever the agent did. The
file that *is* there holds every table the spec compares, so this was a typo
in Spider2's eval set. `evals/dbt/scoring.py` now resolves it inside the staged
copy.

That recovered no passes: scored against the correct gold, all three
submissions from three separate runs still score 0. They fail legitimately.
But they now fail for the real reason, so a future change that solved one
would be visible rather than silently discarded.

**The real ceiling is 62/64 = 96.9%**, not 59/64.

---

## 3. Exactly what reaching 20 would require

From the observed per-task rates, reaching 20 means recovering **4.3 tasks**.
There are only two places they can come from:

**(a) Stabilise the flakiest tasks.** Making the five most-variable tasks
perfectly reliable recovers 4.7 — just enough. The candidates, with how often
they currently land:

```
intercom001      4.3%      recharge001     4.3%
recharge002      4.2%      tickit002       4.0%
divvy001         8.7%      jira001         8.7%
```

These are tasks the model solves roughly one time in twenty. "Stabilising"
them means turning a 1-in-20 event into a near-certainty.

**(b) Solve 4 of the 32 never-passed tasks.** These have resisted 25 attempts
each.

For scale: **the best consistency gain measured across all eight experiments
was pass^3 0.156 → 0.188**, which is 10 → 12 tasks passing all three attempts.
Nothing tried has come close to moving 4.3 tasks of reliability.

---

## 4. Why the harness cannot deliver it

The failures are not mechanical. Forty-five failing tasks diffed against gold
on exactly the tables the evaluator compares:

| Category | Tasks | |
| --- | --- | --- |
| Kept rows gold dropped, or dropped rows gold kept | **20 (44%)** | scope choice |
| Same row count, different values | **16 (36%)** | computation choice |
| Never built the model | 5 (11%) | capability |
| Unscoreable (broken gold) | 3 (7%) | not the agent |
| **Duplicated rows / broken join** | **0** | — |

**Zero broken joins. Zero fan-out.** The model writes mechanically correct
SQL. Eighty per cent of failures are it making a different *defensible* choice
from the one gold made.

### The paired example that defines the problem

`tpch001` asks for the purchase status of **"each customer"**. The model
returned all 150,000 customers. Gold returned 75,007 — only those with
purchases. **The model's reading is the more literal one**, and it lost.

`f1001`, the same model, erring in the opposite direction: gold zero-fills 75
rows that the model left NULL.

So one task wants inactive rows excluded, another wants them included as
zeros, and **neither task says which**. There is no rule that satisfies both.

Supporting measurements:

- Of 766 declared models, **2%** state their grain explicitly ("one row per
  X"). **74%** say nothing about it at all.
- `asana001`'s gold **contradicts its own schema file** on column order.
- `f1001` is scored on a top-20 containing a tie at the boundary, with no
  tiebreak specified — unwinnable by construction.

This is the core limitation. **The benchmark requires guessing undocumented
conventions.** A model guesses them from priors built by having seen many dbt
projects. That is a property of the model, not of the scaffolding around it,
and it is the one thing a harness cannot supply.

---

## 5. Everything that was tried, and what killed it

> **Costs corrected 2026-10-13.** Every figure here was previously ~4.5x too
> high. The harness costed cached input at the full input rate as a deliberate
> upper bound (D5) and that bound was then quoted as the actual cost. The cache
> hit rate on these runs is 92-94%, so ten full runs billed $62.29 on the old
> arithmetic and about $13.65 in reality. A Luna k=3 run costs **~$1.30**, not ~$6.

Each of these cost roughly $1.30 and 3.5 hours. **Do not repeat them without a
new reason.**

| Lever | What it measurably did | Accuracy |
| --- | --- | --- |
| **Gateway catalog access** *(kept)* | refused catalog reads 168 → 0; failed-call rate 6.5% → 4.0% | none |
| Agent reads the `.yml` spec | max_turns 5 → 29; cost $1.33 → $2.02 | worse |
| Spec injected free from the host | +147k input tokens/attempt — 180× the block's own size | worse |
| `dbt test` gate + repair pass | named the right rows; repaired attempts 0.222 vs clean 0.259 | none |
| `reasoning_effort = "high"` | reasoning 1,700 → 3,745 tokens; noise band *widened* | none |
| More turns | capped attempts pass at 0.361 vs 0.238; failures stop at median 19 of 40 | ruled out |
| Infer conventions from pre-built models | scored table present at start in only 11/64; agent already queries it in 8 | dropped |
| Don't rewrite working models *(kept on cost)* | turns 19.4 → 17.6; cost $1.31 → $1.26 | +0.8, not established |
| Repair the build failures it caused | execution failures 10, 7 → 3 | none — those tasks fail on content anyway |

Three of these deserve emphasis because of what they rule out:

**Information is not the constraint.** Three separate changes gave the model
more or better information. The catalog fix eliminated 168 refused reads and
moved accuracy by exactly nothing.

**Awareness of failure is not the constraint.** The test gate handed the model
its exact failing rows, by name — `provider001`'s assertions flagged the same
2,857 rows an independent gold diff found — and it still could not fix them.

**Budget is not the constraint.** Failing attempts stop at a median of 19
turns with 21 unused. They stop because the model believes it is finished. The
pass rate *declines* with turns used (0.358 at 0–9 turns, 0.145 at 30–34):
long attempts are long because the model is lost, not because it needs room.

---

## 6. The limitations, stated plainly

1. **Convention inference.** The model cannot reliably guess which of several
   defensible interpretations a task intends. This accounts for ~80% of
   failures and is not addressable from the harness.
2. **Output variance.** On identical inputs, the same task flips between pass
   and fail. Passing and failing attempts of the same task are operationally
   indistinguishable — 19.1 vs 20.1 turns, 1.7 vs 2.2 tool errors. The
   variance lives in model sampling, which the harness does not control.
3. **A hard capability wall on half the set.** 32 of 64 tasks have never been
   solved once in 25 attempts.
4. **False confidence.** The model stops early, believing it is done, with
   most of its budget unspent and its output wrong. It has no internal signal
   that distinguishes a correct result from an incorrect one — demonstrated by
   the test gate, where being told the answer did not help.

---

## 6b. Where cost beliefs distorted the work

Worth recording, because it is the kind of error that hides inside a method
rather than inside a number.

For nine experiments the harness costed cached input at the full input rate --
a deliberate upper bound that was then quoted as the actual cost. The real
cache hit rate is 92-94%, so a run cost about **$1.30, not $6**.

That mattered more than the arithmetic:

- This page says detecting a 2-task change needs **k=6**. Every experiment was
  run at **k=3**, because k=6 was believed to cost $12. It costs $2.60.
- A k=6 control run was **killed partway through** on the reasoning that it
  could not move the number and cost money.
- `reasoning_effort = "high"` was **reverted on a k=3 null**, citing a "+57%
  cost" that was $2.01 against $1.31. Its three attempts were 0.297, 0.281 and
  0.188: the first two are two of the four best attempts out of 26 recorded,
  the third is the worst. That is precisely the shape k=3 cannot resolve.

The rule about k=6 was written here and then not applied to a revert decision.
A standard that only governs the results you like is not a standard.

## 7. What would actually move it

Honestly, and in order of expected effect:

1. **A model with stronger dbt priors.** The gap is convention inference, which
   comes from having seen more dbt projects. `baseline.toml` is the configured
   control at `gpt-6.1-sol` and has never been run at scale here. This is the
   lever the evidence points at and the one that was explicitly out of scope.
2. **Fix or exclude the 5 unpassable tasks** — pre-registered, with evidence,
   applied to all historical runs at once. Worth up to 5 of the gap, and it is
   a benchmark defect rather than a result.
3. **Check what the 20/64 you are comparing against was measured on.** If
   their manifest does not contain our 5 broken tasks, their 20/64 and our
   16.8/64 may describe the same system.

What is *not* worth doing: another prompt variant. Three have been tried, all
measured, none moved accuracy. The two that changed behaviour at all did so by
making the model do more work for worse results.
