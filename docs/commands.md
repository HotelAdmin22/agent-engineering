# Command sheet

Every command here has been run in this repo. Work from the repo root.

There is no `jq` on this machine, so the inspection commands use Python.

---

## Ask the agent something

```bash
uv run python -m agent "why did revenue fall in September?"
```

| Flag | Effect |
| --- | --- |
| *(none)* | Live trace of every tool call to **stderr** |
| `--quiet` | Answer only |
| `--no-repair` | First answer; skip the verifier's retry pass |
| `--warehouse data/warehouse_seeded.duckdb` | Run against the planted-anomaly copy |

```bash
uv run python -m agent --quiet "what was revenue in September 2026?"
uv run python -m agent "..." 2>/dev/null     # answer only, trace discarded
uv run python -m agent "..." > run.log 2>&1  # keep both
```

Exit code is `0` when verification passed, `1` when it did not — so it scripts.

> The data agent is `python -m agent`. A bare `uv run agent` is the old Phase 0
> script and is not what you want.

---

## Measure

```bash
uv run python evals/run_evals.py                   # 12 gold questions, once
uv run python evals/run_evals.py --k 3             # pass^3 -- the useful one
uv run python evals/run_evals.py --limit 4         # first 4 questions, while iterating
uv run python evals/run_evals.py --tag why         # only the decomposition questions
uv run python evals/run_evals.py --out runs/my-exp # name the output directory
```

Tags in the gold set: `aggregate`, `filter`, `join`, `group`, `delta`, `why`,
`decomposition`, `segment`, `price`, `units`, `trap-status`.

`pass^1` is just accuracy restated. Use `--k 3` or higher or the number tells
you nothing new.

```bash
uv run python evals/score_anomalies.py             # the 3 planted anomalies
uv run python evals/score_anomalies.py --k 3
```

---

## The triage sub-agent

Reads every recorded failure, proposes tooling fixes. Read-only: it has no
write tool and cannot read the eval gold set.

```bash
uv run python evals/run_triage.py
uv run python evals/run_triage.py --quiet
```

Returns nothing when nothing recurs. That is the correct answer, not a failure.

---

## Rebuild fixtures

```bash
uv run python data/build_warehouse.py     # seeded, identical every time
uv run python evals/seeded/plant.py       # the 3 anomalies, into a copy
```

Rebuilding the warehouse does not invalidate the gold set: gold values are
computed from `gold_sql` at eval time, not hardcoded.

---

## Tests and docs

```bash
uv run pytest                             # 79 tests
uv run pytest tests/test_gateway.py -q    # gateway rules only
uv run pytest -k gold                     # the benchmark-integrity tests
uv run pytest -x -q                       # stop at the first failure

uv run python docs/build_architecture.py  # -> docs/architecture.html
uv run python docs/build_failures.py      # -> docs/failures.html
```

Both HTML docs also rebuild automatically whenever a `.py` file is edited
through Claude Code (`.claude/settings.json` hook).

---

## Reading what happened

Every run writes `runs/<timestamp>/`:

| File | Contents |
| --- | --- |
| `answer.json` | question, template, rendered answer, findings, landings |
| `evidence.jsonl` | every query and computation, immutable, with ids |
| `audit.jsonl` | every prompt and tool call, with tracebacks on crashes |
| `ledger.jsonl` | whether each verifier failure was repaired |

```bash
# the latest answer
python -c "import json,glob,os; d=max(glob.glob('runs/*/answer.json'),key=os.path.getmtime); print(d); print(json.load(open(d))['answer'])"

# the latest eval summary
python -c "import json,glob,os; f=max(glob.glob('runs/eval*/summary.json'),key=os.path.getmtime); [print(f'{k:<24}{v}') for k,v in json.load(open(f)).items()]"

# which tools the latest run called
python -c "import json,glob,os; from collections import Counter; f=max(glob.glob('runs/*/audit.jsonl'),key=os.path.getmtime); print(Counter(json.loads(l)['tool'] for l in open(f) if json.loads(l).get('kind')=='tool_call'))"

# every SQL the agent ran, latest run
python -c "import json,glob,os; f=max(glob.glob('runs/*/evidence.jsonl'),key=os.path.getmtime); [print(json.loads(l).get('sql','')[:120]) for l in open(f)]"

# any errors, with their tracebacks
python -c "import json,glob; [print(r['tool'],'|',r.get('error'),chr(10),r.get('traceback') or '') for f in glob.glob('runs/**/audit.jsonl',recursive=True) for r in map(json.loads,open(f)) if r.get('kind')=='tool_result' and r.get('is_error')]"
```

---

## Tuning

| What | Where |
| --- | --- |
| Data agent prompt | `src/agent/agent/planner.py` → `SYSTEM` |
| Triage prompt | `orchestrator/triage.py` → `SYSTEM` |
| Steps / token budget | `src/agent/agent/planner.py` → `MAX_STEPS`, `MAX_TOKENS` |
| Worker model | `manual_loop/loop_openai.py` → `MODEL` |
| Verifier model | `manual_loop/loop.py` → `MODEL` |
| Metric definitions | `src/agent/catalog/catalog.py` → `METRICS` |

**Measure before and after, or do not change it.** One variable at a time.

```bash
uv run python evals/run_evals.py --k 3 --out runs/before
# ... edit one thing ...
uv run python evals/run_evals.py --k 3 --out runs/after
python -c "import json; b=json.load(open('runs/before/summary.json')); a=json.load(open('runs/after/summary.json')); [print(f'{k:<24} {b.get(k)} -> {a.get(k)}') for k in b]"
```

Baseline to beat, from `experiments/results.jsonl`:

```
execution_accuracy       0.972
pass^3                   0.917
confidently_wrong_rate   0.028
mean_tokens_per_run      5448
```

Write the result into `experiments/results.jsonl` whether it helped or not.
`inconclusive` is a real outcome and the most common one.

---

## Setup, if the environment is cold

```bash
uv sync                                   # install dependencies
# .env needs OPENAI_API_KEY (already set)
uv run python data/build_warehouse.py     # if data/warehouse.duckdb is missing
uv run pytest                             # confirm the install
```
