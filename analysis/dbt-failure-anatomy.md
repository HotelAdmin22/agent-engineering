# What the Spider2-DBT failures actually are

Host-only. Gold-derived — see `README.md` in this directory before quoting any
of it anywhere an agent can read.

Source: run `luna-20261009-143212`, attempt 1, all 47 failing tasks, diffed
against `evaluation_suite/gold` on exactly the tables and columns
`spider2_eval.jsonl` compares. The submitted databases have since been pruned;
this page is what survives of them.

---

## Method

`eval_utils.compare_pandas_table` takes each gold column named in
`condition_cols` and asks whether **any** predicted column matches it as a
vector. With `ignore_orders: true`, neither row order nor column order
matters. An earlier triage asserted a `col_order` failure category and
recommended a prompt fix for it — that was wrong, inferred from ordered column
names without reading the evaluator, and is void.

Comparison here normalises numerics to 2dp and strips whitespace, so it is
slightly more lenient than the evaluator. Treat "matches" as a lower bound on
what fails.

---

## 1. There is no fan-out

The prior hypothesis — wrong grain caused by joins multiplying rows — is
**refuted**. Across all 20 wrong-row-count tables, duplicate rows are
essentially zero. On tables where gold has a unique key column:

| Kind | Tables | Meaning |
| --- | --- | --- |
| subset | 9 | dropped rows gold kept |
| superset | 6 | kept rows gold dropped |
| **fan-out** | **0** | — |
| overlapping / no unique key | 4 + 10 | see caveat below |

These are **filter-scope** errors. The fix for a subset is the opposite of the
fix for a superset, which is why "wrong grain" was useless as a category and
why no single prompt rule can address it.

### The clearest case

`tpch001` / `client_purchase_status`. The agent wrote:

```sql
FROM customer AS c
LEFT JOIN customer_purchases AS p ON c.c_custkey = p.customer_id
...
COALESCE(p.purchase_total, 0) AS purchase_total
```

150,000 rows — every customer in TPCH SF1. Gold has **75,007, every one with
`purchase_total > 0`** (minimum 918.19). Gold restricted to customers with
activity; the agent kept the full dimension and zero-filled.

The `.yml` description says *"calculates the purchase status of each
customer"* — which arguably **supports the agent's reading**. The information
needed to get this right is not in the specification.

### The same agent makes the opposite mistake

`f1001` / `finishes_by_driver`: six columns (`disqualified`, `excluded`,
`failed_to_qualify`, `not_classified`, `retired`, `withdrew`), each with
exactly **75 rows where gold has `0.0` and the agent has `NULL`**. A missing
`COALESCE(..., 0)` over a LEFT JOIN. `f1002` repeats it on the identical six
column names, 10 rows.

So: zero-fill where gold filters, and no zero-fill where gold zero-fills. The
agent has no consistent rule, and the task statements do not supply one.

---

## 2. Per-task breakdown — 47 failing tasks, attempt 1

Classified by the worst thing wrong with each.

| Worst problem | Tasks | Share |
| --- | --- | --- |
| Wrong row count | 20 | 43% |
| Every value wrong | 8 | 17% |
| Missing table | 7 | 15% |
| Partly wrong | 7 | 15% |
| Edge cases only (≥90% right) | 3 | 6% |
| Column not built | 1 | 2% |
| Broken gold | 1 | 2% |

Of 91 unmatched columns on same-row-count tables:

| | Columns |
| --- | --- |
| Different values, not null-related | 62 |
| NULL where gold has 0 | 14 |
| Value where gold has NULL | 8 |
| NULL where gold has a value | 6 |
| Column not built | 1 |

NULL gaps appear in 6 tasks: `f1001`, `f1002`, `google_play001`,
`google_play002`, `tickit001`, `workday001`.

### Near-misses worth knowing

- `tickit001` / `fct_sales`: 2,777 rows where gold has real category and event
  names and the agent has `NULL` — a join that failed to match, not a
  zero-fill issue. Every other compared column matches.
- `google_play002`: one column, 3 rows, a rolling average gold fills and the
  agent leaves `NULL` — a window frame too narrow at the series start.
- `f1001` / `most_fastest_laps`: gold has Pierre Gasly, the agent has Juan
  Pablo Montoya — a **tie at the boundary of a top-20**. Unwinnable without a
  tiebreak the task does not specify. Same class as the unordered `STRING_AGG`
  problem in `asana001`.

### Caveat

The 10 "disjoint" and 4 "overlapping" rows had no unique key in gold, so the
classifier fell back to whole-row matching. One wrong computed column makes
every whole row differ, so those labels overstate the problem and are not
reliable. They were never characterised.

Attempt 1 only. Attempts 2 and 3 were never diffed.

---

## 3. The five unwinnable tasks

Each verified **0 of 9 attempts** across all three completed runs.
`evaluate.py:88` joins the gold filename literally — no globbing — so a
missing file raises inside the evaluator and scores 0 silently.

| Task | Evidence |
| --- | --- |
| `xero_new001` | spec names `xero.duckdb`; gold dir contains `xero_new.duckdb` |
| `xero_new002` | same |
| `social_media001` | spec names `social_media_reporting__rollup_report.duckdb`; dir contains `social_media.duckdb` |
| `chinook001` | gold contains only the 11 raw source tables; spec compares `dim_customer`, `fct_invoice`, `obt_invoice` |
| `tpch002` | starting DB contains only `customer` (150,000 rows). Needs `part`, `supplier`, `partsupp`, `nation`, `region`. No seeds, `packages.yml` is 0 bytes. `tpch001` — the same project — ships all 8 tables. |

Ceiling: **59/64 = 92.2%**.

### tpch002 also shows the success signal is gameable

The agent reached `dbt run` exit 0 by editing `dbt_project.yml` to add
`+enabled: false` for two model directories not disabled in the pristine
project, and writing source stubs:

```sql
-- Empty typed fallback for the unavailable TPCH part source.
SELECT CAST(NULL AS BIGINT) AS p_partkey, ... WHERE FALSE
```

Searched for across the whole run: **one task does this, and it is one of the
five unwinnable ones.** A second candidate, `lever001`, shipped three empty
tables and **passed 3/3** — its empty tables are legitimately empty and are not
the compared ones. So this is an integrity concern with no measurable effect on
the pass rate, and a guardrail for it should be justified on that basis only.

---

## 4. What this rules out

- **Grain/fan-out fixes.** No fan-out exists.
- **Column-order fixes.** The evaluator ignores order.
- **A prompt rule for LEFT vs INNER.** The correct choice is task-specific and
  the agent errs in both directions.
- **Chasing the five unwinnable tasks.**

What remains is 62 columns of ordinary wrong logic across heterogeneous tasks,
with no shared mechanism found. Three completed runs have moved pass@1 by less
than its own noise band.
