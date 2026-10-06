# Data agent rules

Architecture boundaries. These are not style preferences — each one is load
bearing, and breaking one silently removes a guarantee the tests claim to make.

- **Only `src/agent/gateway/` may import `duckdb` or execute SQL.** The agent is
  never handed a connection object. If another module needs data, it asks the
  gateway.
- **The LLM never computes or types numbers.** Reports use
  `{{evidence_id.field}}` placeholders, rendered from the evidence store by
  `src/agent/report/`. A digit in model prose is a verifier failure.
- **Every executed query writes an immutable evidence record.** There is no
  update method on `EvidenceStore`, deliberately.
- **Tool results to the model: evidence id, schema, row count, top 10 rows.**
  Never the full result set.
- **Deterministic tools take evidence ids, not numbers.** A signature that
  accepts a literal lets the model type a digit into an argument.
- **The gateway connects with `enable_external_access=false` and
  `lock_configuration=true`.** Without it, SQL can read any file on disk —
  including the eval gold set — and no SQL-text check reliably stops it.
- **Every new gateway rule gets a pytest case.** Run `uv run pytest` before
  finishing.

## Layout

```
src/agent/
  catalog/    table + metric cards; search_catalog, describe_table, get_join_path
  gateway/    parse -> authorize -> lint -> rewrite -> cost -> execute (read-only)
  evidence/   immutable records; ids like e7
  tools/      deterministic math: deltas, contribution, mix vs rate
  agent/      planner loop (reuses manual_loop/loop_openai.py)
  verifier/   every number traced to evidence; reconciliation checks
  report/     renders {{e7.delta_pct}} from evidence
  audit/      append-only JSONL of every prompt and tool call
```

## Commands

```bash
uv run python data/build_warehouse.py    # rebuild the DuckDB fixture (seeded)
uv run python -m agent "why did revenue fall in September?"
uv run pytest                            # gateway + verifier rules
uv run python evals/run_evals.py         # accuracy, pass^k, confidently-wrong
```

## The triage boundary

`orchestrator/triage.py` reads failures and proposes fixes. It has three
read-only tools and no write tool. Two lists in that module are load bearing:

- `READABLE_ROOTS` — it cannot read `evals/gold/`, `evals/seeded/`, `.env` or
  `data/`. Paths are resolved before the containment check, so `../` cannot
  walk out.
- `PROTECTED` — a proposal targeting `gateway/`, `verifier/`, `tests/` or the
  eval sets is rejected at parse time.

**If you add a new grader or check, add it to `PROTECTED`.** An agent scored on
"failures went down" can always win by weakening what reports the failure. The
boundary is a path allowlist in a dispatcher, never a sentence in a prompt.

## Reuse before adding

This repo predates the data agent. Prefer what is here:

- `manual_loop/loop_openai.py` — the OpenAI tool-use loop. Do not write a second one.
- `orchestrator/trace.py` — the console tracer, passed as `trace=`.
- `orchestrator/findings.py` — verdict vocabulary (PASS/FAIL/NEEDS) and the ledger.
- `evals/sandbox.py` — the SQLite read-only sandbox used by the Spider harness.

## Deliberate cuts

Single tenant, no credential broker, no durable execution. See the table at the
end of `docs/data-agent.md` for what each would be in production.
