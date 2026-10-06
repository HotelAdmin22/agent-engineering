# Spider 2.0-Lite — local (SQLite) subset

135 tasks, 30 databases. The entry point to Spider 2.0: real benchmark tasks,
run locally, no cloud credentials, no per-query cost.

## Already here

- `tasks.jsonl` — the 135 `local*` tasks (`instance_id`, `db`, `question`, `external_knowledge`)
- `gold/spider2lite_eval.jsonl` — the grading key

## Fetched automatically

```bash
uv run python evals/spider/fetch.py
```

Pulls everything that lives in the public xlang-ai/Spider2 repo:

- `documents/` — the 8 markdown files our tasks cite (13 of the 135 reference
  one; they are part of the task, not cheating)
- `evaluation_suite/` — Spider's own `evaluate.py` and `evaluate_utils.py`
- `gold/exec_result/` — 328 gold CSVs, the local tasks only

`--schema` additionally pulls per-database DDL and column metadata.

## You must download (Google Drive blocks scripted download)

**The databases.** [This zip](https://drive.usercontent.google.com/download?id=1coEVsCZq-Xvj9p2TnhBFoFTsY-UoYGmG&export=download).
Unzip so the files land directly in `databases/` as `<db>.sqlite`, one per
task's `db` field. The upstream repo ships schema metadata for these databases
but not the databases themselves, so this is the one manual step.

Until then the runner reports `missing_db` per task and scores nothing.

## gold/ is not for the agent

`gold/` holds the grading key, and the upstream repo also ships gold SQL and
gold results. The agent cannot reach any of it: its only tool is
`query_database`, bound to one SQLite file, opened read-only, with ATTACH
denied. There is no file, shell, or search tool. See `evals/sandbox.py`.

Keep it that way. If you ever give the agent a file-reading tool, this
directory becomes a cheating surface.

## Scoring

Use Spider's own `evaluation_suite/evaluate.py`, now vendored by `fetch.py`. Do not write your own scorer —
one that is slightly too lenient invents improvements that never happened.
