# Spider2-DBT baseline and failure triage

> **CORRECTION (2026-10-08).** The `col_order` category below is wrong, and so
> is the "8 free wins" recommendation built on it. The evaluator does **not**
> compare columns by position. `eval_utils.compare_pandas_table` takes each
> required gold column and asks whether *any* predicted column matches it as a
> vector:
>
> ```python
> gold_cols = gold.iloc[:, condition_cols]   # WHICH gold columns matter
> pred_cols = pred                            # ALL predicted columns
> for gold in t_gold_list:
>     if not any(vectors_match(gold, pred) for pred in t_pred_list):
>         score = 0
> ```
>
> Column order is irrelevant. My categoriser inferred `col_order` from ordered
> column *names* and never read the evaluator. Verifying `asana001` is what
> caught it — and that task turns out to fail on a single genuinely different
> value, an unordered `STRING_AGG` whose list contains the same set of projects
> in a different order.
>
> Two further findings from that check:
>
> - The agent produced `asana__team` with its columns in exactly the order
>   `models/asana.yml` declares. **Gold deviates from its own schema file**
>   (`number_of_active_projects` and `active_projects` are swapped). The agent
>   followed the documented contract; gold did not.
> - The real categories are therefore: wrong row count, wrong values, missing
>   table — plus a distinct **list-order** mode where an aggregated string holds
>   the right set in a nondeterministic order.
>
> The baseline number (15/64, 23.4%) and everything in "It is not the harness"
> are unaffected — those came from run records, not from this diff. Sections
> below marked with a strikethrough heading are superseded.


Run `luna-20261008-182025`. The first **reportable** measurement in this
project: clean git tree, full task set, three attempts.

## The number

**15 of 64 tasks — pass@1 = 23.4%**

| | |
| --- | --- |
| Tasks | 64 (68 minus the 4 with no gold database) |
| Attempts | 3 per task, 192 total |
| Per-attempt pass rate | 0.2500, 0.2188, 0.2344 |
| Noise range | 0.2188 – 0.2500 |
| Model | `gpt-6-luna` |
| Cost | **$5.79** total, $0.0301 per attempt |
| Wall clock | 18:20 → 21:41, 6.7 h compute at concurrency 2 |

Three independent attempts landing within 3 points of each other means
23–25% is a real figure, not a lucky draw. This is the first number here with
its own variance estimate.

### Dev and held-out agree

| Split | Tasks | pass@1 |
| --- | --- | --- |
| dev | 51 | 23.5% |
| held_out | 13 | 23.1% |

A 0.4-point gap. **Nothing is overfitted** — the agent is equally mediocre on
tasks it was never tuned against, which is what a baseline should look like.

### Consistency

| | Tasks |
| --- | --- |
| Passed all 3 attempts | 10 |
| Flaky (1–2 of 3) | 10 |
| Failed all 3 | **44** |

The failures are systematic, not noise. That is good news for fixing them.

---

## It is not the harness

Of 147 failed attempts, **141 had `final_dbt_exit_code: 0`** — dbt built
cleanly and the evaluator simply scored zero. Every operational metric is
indistinguishable between passing and failing attempts:

| Median | Failing | Passing |
| --- | --- | --- |
| main turns | 17 | 18 |
| tool errors | 2 | 2 |
| gateway rejections | 1 | 1 |
| dbt calls | 3 | 3 |
| tool calls | 34 | 38 |

Only 4 of 192 attempts hit `max_turns`. Only 6 had a non-zero dbt exit.

So: not crashes, not the sandbox, not the gateway, not the tool surface, not
the budget. The agent builds models that compile and run and produce the wrong
rows.

### Why "139 untriaged" overstated the mystery

The automatic categoriser only fires on `execution` and `budget`. Anything that
*completes and scores zero* falls through to `untriaged` by default. One bucket
wearing a label that implies forty.

---

## ~~What the failures actually are~~ (superseded — see the correction above)

Diffed the submitted DuckDB against gold for every failing task on attempt 1,
on exactly the tables and columns `spider2_eval.jsonl` compares.

**45 failing tasks, 77 compared tables:**

| Category | Tables | Meaning | Fix |
| --- | --- | --- | --- |
| `row_count` | 32 | Wrong grain — aggregated at the wrong level | Capability |
| `col_order` | 21 | Right rows, right count, columns in a different order | **Prompt** |
| `values` | 13 | Right shape, wrong numbers | Capability |
| `missing_table` | 10 | Never built the required model | Capability |

### The column-order finding

The evaluator selects columns **by position**:

```json
"condition_tabs": ["dim_listings_hosts", "mom_agg_reviews"],
"condition_cols": [[2,3,4,5,6,7,8], [0,1,3]]
```

So on `airbnb001`:

```
submitted:  MINIMUM_NIGHTS, ROOM_TYPE, PRICE, HOST_ID, HOST_NAME, IS_SUPERHOST, CREATED_AT
gold:       LISTING_ID, LISTING_NAME, ROOM_TYPE, MINIMUM_NIGHTS, PRICE, HOST_ID, HOST_NAME
```

**Row counts identical: 17,499 both sides.** Same eleven columns. A different
`SELECT` order scores zero.

Nothing in the task statement says column order matters. dbt does not care.
The grader does, and the agent was never told.

**8 tasks fail on nothing else:** `asana001`, `google_play001`,
`google_play002`, `inzight001`, `jira001`, `nba001`, `quickbooks001`,
`recharge002`.

### The grain finding

Same task, the other table:

```
mom_agg_reviews   submitted 11,358 rows     gold 3 rows
```

A month-over-month aggregate with three rows in gold. The agent produced
11,358 — it aggregated per-listing, or not at all. That is a real comprehension
failure, and it sits in the same task as a purely cosmetic one.

---

## ~~What to do next, in order~~ (superseded — see the correction above)

1. **Verify the 8 near-misses.** The `col_order` label fires when row counts
   match and the ordered column names differ. That is a strong hint, not proof
   — the values could also be wrong. Value-diff `asana001` on aligned columns
   before counting them as free wins.
2. **If they hold, change one thing**: tell the agent that column order is
   compared by position and must match the requested order. Re-run, re-measure.
   Ceiling if it fixed only those 8: **15/64 → 23/64, 23.4% → 35.9%**. No new
   capability required, which makes it the cheapest experiment available.
3. **Then the 10 `missing_table` tasks** — the agent did not build a required
   model at all. Likely the clearest capability signal after the cheap win.
4. **`row_count` (32) is the real work** and should be attacked last, with the
   first two removed from the noise.

## Caveats

- One run, k=3. Good for pass@1, not enough to attribute a small change.
- The category diff is attempt 1 only. The other two attempts were not diffed.
- `col_order` is inferred from names and row counts, not from comparing values.
- This analysis runs on the **host**. `CLAUDE.md` keeps gold databases and
  `runs/dbt/**/scoring/` out of triage's reach deliberately — the agent must
  never see any of it.
