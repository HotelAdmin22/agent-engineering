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
- `src/agent/gateway/` — the only module that opens a database. One gateway,
  two backends (DuckDB and SQLite), inferred from the file extension. The Lite
  harness's old `evals/sandbox.py` was folded into it; do not reintroduce a
  second read-only SQL surface.

## Spider2-DBT harness

Everything for Spider2-DBT lives in `evals/dbt/` (plus `docker/dbt-sandbox/`
and `tests/dbt/`). The Lite runner, the root `Dockerfile` and `compose.yaml`
are separate and untouched by it. See `PROJECT_SETUP.md` and
`docs/DECISIONS.md`.

- **The gateway also runs inside the sandbox image.** `evals/dbt/sandbox_tools/sql.py`
  is a thin adapter over `agent.gateway`; it imports no `duckdb`. The image
  installs `src/` so the same rules apply in the container.
- **The API key never enters a DBT container or image.** It lives in the host
  `.env` only. Task and evaluator containers start with no `-e`/`--env-file`,
  `--network none`, a read-only root and only their workspace mounted. The
  image's build context is an allowlist (`docker/dbt-sandbox/Dockerfile.dockerignore`).
- **One loop.** Every DBT role (main, explore, diagnose) runs through
  `manual_loop/loop_openai.py` via its keyword-only role arguments.
- **DBT graders and eval sets are in `PROTECTED`:** `evals/dbt/scoring.py`,
  `submission.py`, `scripts/selftest_evaluator.py`, `scripts/build_manifest.py`,
  `scripts/make_splits.py`, `evals/dbt/manifests/`, `docker/dbt-sandbox/` and
  the clone's `vendor/Spider2/spider2-dbt/evaluation_suite/`. Triage can read
  neither the gold DBs nor `runs/dbt/**/scoring/`.
- **The image tag is a content hash.** After changing `src/`, `sandbox_tools/`,
  `pyproject.toml`/`uv.lock` or `docker/dbt-sandbox/`, run
  `uv run python -m evals.dbt.scripts.build_image` before a DBT run.

```bash
uv run python -m evals.dbt.scripts.build_image
uv run python -m evals.dbt.runner --config evals/dbt/configs/baseline.toml
uv run pytest -m docker                  # container isolation + one full attempt
```

## Deliberate cuts

Single tenant, no credential broker, no durable execution. See the table at the
end of `docs/data-agent.md` for what each would be in production.
