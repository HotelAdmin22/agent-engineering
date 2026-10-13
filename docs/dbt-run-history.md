# Spider2-DBT: what every run so far has measured

> **Why the target was not reached:** `docs/dbt-why-not-20.md`.
> 32 of 64 tasks never solved in 25 attempts; 5 unpassable by anyone;
> 80% of failures are interpretive, not mechanical.

The run directories these numbers came from have been deleted; they were
~28 MB per attempt and filled a 466 GB disk. What survives is each attempt's
`result.json` and `tool_calls.jsonl`, plus this page. See
`evals/dbt/scripts/prune_runs.py`.

**This page is deliberately free of anything derived from the gold
databases.** `docs/` is inside `orchestrator/triage.py`'s `READABLE_ROOTS`, so
an agent can read it. Gold-derived findings — which rows differ, what the
reference query did — live in `analysis/`, which is outside those roots and
must stay there.

---

## The ledger

| Run | Changed | pass@1 | Tasks | pass^3 | Cost |
| --- | --- | --- | --- | --- | --- |
| `luna-20261008-182025` | baseline | 0.234 | 15/64 | 0.156 | $5.79 |
| `luna-20261009-030506` | prompt: "read the .yml files" | 0.224 | 14/64 | 0.125 | $9.76 |
| `luna-20261009-143212` | gateway: catalog access | 0.250 | 16/64 | 0.156 | $5.76 |
| `luna-20261009-213400` | prompt: injected model spec | — | — | — | crashed, disk full |

Experiment 4 crashed before it could be scored, but its two complete attempts
are directly comparable against the same two attempts of the run before it,
and they are decisive enough to have reverted the change:

| 128 attempts each | gateway | + injected spec |
| --- | --- | --- |
| Passed | **31** | 28 |
| Mean turns | 19.3 | **23.6** |
| Hit the 40-turn cap | 2 | **19** |
| Mean input tokens | 274,035 | **421,548** |
| Cost | $3.78 | $5.72 |

The block is ~800 tokens, and it raised input by **147,000 tokens per
attempt** — 180× its own size. It is not expensive to send; it is expensive
because of what it makes the agent do. Naming a mean of 11.1 models and saying
each must exist sent the agent off building models the task never asked for.

**That is experiment 2's failure reproduced through a cheap delivery
mechanism, which means the delivery mechanism was never the problem.** Reverted
to `inject_model_spec = false`; the code stays, tested and flag-controlled.

Per-attempt rates, which is what the noise estimate rests on:

| Run | Attempt rates | Noise range |
| --- | --- | --- |
| baseline | 0.250, 0.219, 0.234 | 0.219 – 0.250 |
| .yml prompt | 0.234, 0.250, 0.188 | 0.188 – 0.250 |
| gateway | 0.219, 0.266, 0.266 | 0.219 – 0.266 |

**Nothing has moved outside noise in three completed runs.** Every band
overlaps every other band. Treat any single-task difference as noise unless
pass^3 moves with it, and it has not.

---

## What is actually established

### 1. It is not the harness

Of 147 failed attempts in the baseline, **141 exited dbt cleanly**. Median
turns, tool errors, gateway rejections and dbt calls are indistinguishable
between passing and failing attempts. The agent builds models that compile and
run and produce the wrong rows.

### 2. The gateway was withholding the catalog, and that is fixed

The largest single source of wasted calls was the gateway refusing
`information_schema`, because `table_names()` excluded it from the allowlist
and the DBT sandbox has no describe tool. The agent had no way to learn what
`dbt run` had just built, so it guessed schema qualification and was wrong
repeatedly.

| | Baseline | After |
| --- | --- | --- |
| Catalog reads refused | 168 | **0** |
| Schema qualification errors | 95 | 11 |
| Failed-call rate | 6.5% | 4.0% |

Fixed in `29f7a39`. **Accuracy did not move** — these errors cost turns, not
correctness. That was predicted in writing before the run.

### 3. Telling the agent to read files costs more than the files are worth

Experiment 2 added a prompt section telling the agent to read every
`models/*.yml` first. Result: `max_turns` 5 → 29, budget failures 4 → 23, mean
input tokens 280k → 479k, cost $5.79 → $9.76, and accuracy inside noise.
Reverted.

The follow-up (`7c11098`) delivers the same content from the host at no turn
cost — median 2.6 KB per task. That run has not completed.

### 4. The `.yml` files mostly do not say what the failures need

Measured across **766 declared models** in the task set:

| | Share |
| --- | --- |
| State grain explicitly ("one row per X") | **2%** |
| Only imply it ("each X") | 24% |
| Say nothing about grain | **74%** |

So experiment 2 was wrong about *where the information lives*, not only about
how to deliver it. Injecting the spec is best justified by the models it names
as required, not by grain.

### 5. Five tasks cannot be passed by anyone

Verified **0 for 9 attempts each**, across all three completed runs.

| Task | Why |
| --- | --- |
| `xero_new001` | eval spec names a gold file the gold directory does not contain |
| `xero_new002` | same |
| `social_media001` | same |
| `chinook001` | gold database does not contain the tables the spec compares |
| `tpch002` | starting database ships 1 of the 8 source tables the project needs; no seeds, empty `packages.yml` |

`evaluate.py` joins the gold filename literally with no globbing, so a missing
file raises and scores 0 silently.

**The ceiling is 59/64 = 92.2%.** Report pass@1 on /64 and state the ceiling
beside it. Do not quietly switch denominators — shrinking the denominator
after seeing a result is the move the `PROTECTED` list exists to prevent.

---

## Operational lessons

- **A run used to need ~9.5 GB** — 192 attempts × ~28 MB of `workspace/` and
  `submission/` copies, of which ~1% is ever read again. Four of them filled a
  466 GB disk and killed a run on ENOSPC at attempt 3 of 3, after ~3 hours and
  $7. Now each attempt is pruned as soon as it is scored, keeping submissions
  for attempt 1 only (gold diffs cannot be done after the fact), so a run sits
  near 1.6 GB. `validate()` also refuses to start below 8 GB free.
- **`--resume <RUN_ID>` skips finished attempts**, so a crash costs the
  attempts in flight rather than the whole run.
- **`dbt run` exiting 0 means nothing.** One task reached exit 0 by disabling
  two model directories and stubbing its sources to empty — it is one of the
  five unwinnable tasks, so this is an integrity concern rather than a
  measurable one.
- **Host-side prompt changes do not change the image tag**, so they compare
  against the previous run with the container held byte-identical.
