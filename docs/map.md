# Where everything is

Four agents, three tool surfaces, two loops. Line numbers verified against the
source; if they drift, `grep -n "^SYSTEM" <file>` finds the prompt.

## The four agents

An "agent" here is a system prompt plus a tool list, handed to a loop. There is
nothing else to one.

| Agent | File | Prompt at | Tools |
| --- | --- | --- | --- |
| **Data agent** — answers warehouse questions | `src/agent/agent/planner.py` | `:41` | 7 |
| **Triage analyst** — reads failures, proposes fixes | `orchestrator/triage.py` | `:71` | 3, read-only |
| **Spider verifier** — audits an answer | `orchestrator/verifier.py` | `:28` | 1 |
| **Spider worker** — text-to-SQL benchmark | `evals/runner.py` | `:38` | 1 |

## What each one can actually do

The tool list *is* the capability boundary. Nothing an agent can do is absent
from these lists.

**Data agent** — 7 tools, declared in `planner._tool_schemas()`, dispatched in
`planner._dispatch()`:

| Tool | Implemented in |
| --- | --- |
| `search_catalog`, `describe_table`, `get_join_path` | `src/agent/catalog/catalog.py` |
| `run_sql` | `src/agent/gateway/gateway.py` |
| `compute_delta`, `compute_contribution`, `mix_vs_rate` | `src/agent/tools/math.py` |

**Spider worker and verifier** — 1 tool, `query_database`, in
`evals/sandbox.py`. SQLite, read-only, `ATTACH` denied.

**Triage** — 3 tools, `list_failures`, `show_failure`, `read_source`, all in
`orchestrator/triage.py`. No write tool exists.

## The two loops

Every agent runs on one of these. Neither is a framework; both are a `for`
loop over one API.

| File | Model | Lines |
| --- | --- | --- |
| `manual_loop/loop_openai.py` | `gpt-5.6-luna` | 125 |
| `manual_loop/loop.py` | `claude-opus-5` | 162 |

They return the same `Result`, so callers do not know which provider ran.

## Reading order, if you want to follow one request end to end

1. `src/agent/__main__.py` — the CLI entry. 90 lines.
2. `src/agent/agent/planner.py` — the prompt, the tool list, the dispatcher,
   the repair pass. **This is the file to read first.**
3. `manual_loop/loop_openai.py` — the loop it calls. Read `run_loop` top to
   bottom; it is one function.
4. `src/agent/gateway/gateway.py` — what happens to the SQL the model proposes.
   `authorize()` is the whole policy.
5. `src/agent/verifier/check.py` — how an answer is judged.

Everything else is supporting cast.

## Things that are not agents

Easy to mistake for one:

| | |
| --- | --- |
| `src/agent/evidence/store.py` | A record of what ran. No model. |
| `src/agent/report/render.py` | Turns `{{e7.delta}}` into a number. No model. |
| `src/agent/audit/log.py` | Append-only JSONL. No model. |
| `orchestrator/findings.py` | Verdicts, ledger, landing checks. **No model, deliberately** — this is what decides whether a repair worked. |
| `evals/run_evals.py` | The grader. No model. |
| `docs/build_*.py` | Documentation generators. No model. |

The split matters: anything that judges the agent has no model in it, so it
cannot be talked around.

## Where output lands

```
runs/<timestamp>/
  answer.json      the question, the template, the rendered answer, findings
  evidence.jsonl   every query and computation, immutable, with ids
  audit.jsonl      every prompt and tool call, tracebacks on crashes
  ledger.jsonl     whether each verifier failure was repaired
```

## Two benchmarks, measuring different things

| | Gold set | Spider 2.0-Lite |
| --- | --- | --- |
| Where | `evals/gold/questions.jsonl` | `evals/spider/tasks.jsonl` |
| Size | 12 questions, 1 warehouse | 135 tasks, 30 databases |
| Engine | DuckDB | SQLite |
| Agent | the full data agent | the 1-tool Spider worker |
| Measures | cited-evidence pipeline | raw text-to-SQL across unfamiliar schemas |
| Run | `uv run python evals/run_evals.py --k 3` | `uv run python -m evals.runner` |

Spider does **not** exercise the catalog, evidence store, placeholders or
verifier. It is a different agent on a different engine, kept separate on
purpose.
