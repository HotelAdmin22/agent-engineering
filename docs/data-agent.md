# The data agent

The build plan this was implemented from, plus what actually got built and what
differs. Architecture rules live in [`../CLAUDE.md`](../CLAUDE.md); the live
component map is [`architecture.html`](architecture.html).

## Goal and scope

A single-tenant, local version of the study guide's architecture. DuckDB stands
in for the warehouse and the components are plain Python modules. The aim is an
agent that answers "why did X change?" with cited numbers, plus an eval score
you can quote.

The boundaries hold even at small scale: the model only proposes SQL, the
gateway decides, results land in an evidence store, and numbers render from
evidence.

## Where this deviates from the plan

Four changes, each because the plan assumed something that was not true here.

| Plan said | What was done | Why |
| --- | --- | --- |
| `uv init data-agent` as a new project | Built inside this repo as `src/agent/` | The loop, tracer and verdict vocabulary already existed here |
| Reuse DuckDB files from Spider2-DBT | Generated `data/warehouse.duckdb` | `evals/spider/databases/` is empty — that dataset was never downloaded |
| Write an agent loop (milestone 3) | Reused `manual_loop/loop_openai.py` | It already implements the Responses API loop; a second one is a second bug surface |
| `pass^5` | Harness takes `--k`; `pass^3` measured | Same metric, fewer runs while iterating |

The warehouse is generated rather than borrowed, and it is not noise. Revenue
falls in the final month for two separable reasons — electronics volume
collapses, apparel reprices +35% — so "why did revenue change?" has a true
answer a mix-versus-rate decomposition can recover. It is seeded, so the numbers
are identical on every rebuild and an eval written against them stays valid.

## Build order, and what each milestone proved

1. **Gateway and evidence store.** `run_sql` parses with sqlglot, refuses
   anything but a SELECT, checks tables against an allowlist, bounds the result
   and writes evidence. 24 pytest cases, including the three the plan names: a
   DROP, a cross join with no predicate, an unknown table.
2. **Catalog.** Cards per table and metric from `information_schema`, plus
   keyword search. Metric cards carry their `sql_hint`, including the
   `status = 'completed'` filter that revenue requires.
3. **Agent loop.** OpenAI function calling over those tools, with step and token
   budgets. Observations return an evidence id, schema, row count and the top 10
   rows — never the full result.
4. **Deterministic tools and placeholders.** `compute_delta`,
   `compute_contribution`, `mix_vs_rate`. All take evidence ids, never numbers.
   `report/` renders `{{e7.delta_pct}}`.
5. **Verifier.** Scans the *template*, before rendering — after rendering, a
   cited figure and an invented one look identical. Also reconciles parts
   against the whole.
6. **Evals.** 12 gold questions, 3 planted anomalies, `run_evals.py` reporting
   execution accuracy, pass^k and the confidently-wrong rate.

## Three bugs worth remembering

**The falsy empty store.** `EvidenceStore` defines `__len__`, so an empty one is
falsy, and `self.store = store or EvidenceStore()` silently discarded the
caller's store. Evidence went somewhere nobody read. Fixed with
`store if store is not None else ...`.

**The grader that marked correct answers wrong.** DuckDB returns `DECIMAL`
columns as Python `Decimal`, which is neither `int` nor `float`. The first eval
run reported 67% accuracy and a 33% confidently-wrong rate; the agent had
actually been right every time. Worth stating plainly: the eval was wrong before
the agent was.

**The gold set was readable.** `read_only=True` stops writes and says nothing
about reads, and DuckDB will happily run
`SELECT * FROM read_json_auto('evals/gold/questions.jsonl')`. The table
allowlist never fired, because a table function is not a table. Seven of nine
file-read attempts reached `gold_sql` and the anomaly key.

Fixed with `enable_external_access=false` and `lock_configuration=true` on the
connection — refusing the capability rather than enumerating the spellings of
it, since blocklisting `read_json_auto`, `read_csv_auto`, `read_text`,
`read_blob`, `glob`, `read_parquet` would lose to whatever DuckDB adds next.
Nine regression tests in `tests/test_gateway.py`.

The reported numbers are not contaminated: an audit of all 305 historical tool
calls and 155 evidence records found zero file access. But that was the agent
never trying, not the system preventing it. The guarantee was absent, and a
guarantee nobody tested is a hope.

## What is deliberately not built

Name each of these as a cut, not a gap.

| From the guide | Here | Production |
| --- | --- | --- |
| Tenant cells, KMS keys | Single tenant | Per-tenant isolation |
| Credential broker | `.env` read at startup | Short-lived minted tokens |
| Durable execution | Each run writes `answer.json`, `evidence.jsonl`, `audit.jsonl` | Temporal or similar |
| Snapshot pinning | The warehouse is a file; copy it | Warehouse time travel |
| Human approval | Row bound enforced by the gateway | Durable approval signal |

## Commands

```bash
uv run python data/build_warehouse.py     # rebuild the fixture
uv run python evals/seeded/plant.py       # plant the 3 anomalies
uv run python -m agent "why did revenue fall in September?"
uv run pytest
uv run python evals/run_evals.py --k 3
```
