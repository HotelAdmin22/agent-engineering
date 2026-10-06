# Spider 2.0-Lite — local (SQLite) subset

135 tasks, 30 databases. The entry point to Spider 2.0: real benchmark tasks,
run locally, no cloud credentials, no per-query cost.

## Already here

- `tasks.jsonl` — the 135 `local*` tasks (`instance_id`, `db`, `question`, `external_knowledge`)
- `gold/spider2lite_eval.jsonl` — the grading key

## You must download (Google Drive blocks scripted download)

1. **Databases** — [this zip](https://drive.usercontent.google.com/download?id=1coEVsCZq-Xvj9p2TnhBFoFTsY-UoYGmG&export=download).
   Unzip and put the `.sqlite` files directly in `databases/`, so the runner
   finds `databases/<db>.sqlite` for each task's `db` field.

2. **External knowledge docs** — 13 tasks cite a markdown file (`RFM.md`,
   `f1_overtake.md`, ...). Copy them from the Spider2 repo's
   `spider2-lite/resource/documents/` into `documents/`. Without them those
   13 tasks are unanswerable, and they are not cheating — they are part of
   the task.

## gold/ is not for the agent

`gold/` holds the grading key, and the upstream repo also ships gold SQL and
gold results. The agent cannot reach any of it: its only tool is
`query_database`, bound to one SQLite file, opened read-only, with ATTACH
denied. There is no file, shell, or search tool. See `evals/sandbox.py`.

Keep it that way. If you ever give the agent a file-reading tool, this
directory becomes a cheating surface.

## Scoring

Use Spider's own `evaluation_suite/evaluate.py`. Do not write your own scorer —
one that is slightly too lenient invents improvements that never happened.
