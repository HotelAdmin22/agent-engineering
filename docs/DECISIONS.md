# Decisions — Spider2-DBT harness

Resolutions of the open items in `PROJECT_SETUP.md` §1.1, plus what was found
while implementing it. **Status** is one of: *Resolved* (owner decided),
*Implemented — owner to confirm* (Claude Code chose, from evidence, and it can
be reversed), *Blocked*, *Verified*, *Open*.

Implemented on branch `spider2-dbt-harness`, 2026-10-07, by Claude Code
(Opus 5.5) for luciano.lanati@travelandleisure.com. Nothing is committed yet.

## Step 0 — repo state and regression baseline (V4, V8, V9)

- **Regression baseline:** `uv run pytest` → **125 passed** (14 s) on `master`
  at `2c18e1d` before any change. After the work: 253 passed, 11 Docker tests
  skipped by default; `uv run pytest -m docker` → 11 passed.
- §1.2 matched the repo, with one difference: `evals/sandbox.py` does not exist
  because the SQLite sandbox was folded into `src/agent/gateway/backends.py`
  ("One gateway covers DuckDB and SQLite", gateway.py docstring). That answers
  **O2**: CLAUDE.md's "Reuse before adding" line about `evals/sandbox.py` is
  stale. Not edited (the CLAUDE.md change is additive only); owner to fix.
- **Interfaces read (V8):**
  - `loop_openai.run_loop(prompt, tools, execute, system, max_iterations, client, trace, max_total_tokens)`
    with a module-level `MODEL`; tools in Anthropic shape; returns `loop.Result`.
  - `Gateway(db_path, store, allowlist, max_rows, max_joins, timeout, backend)`;
    `run_sql()` → `(Decision, EvidenceRecord|None)`. DuckDB backend already
    connects `read_only=True` with `enable_external_access=false`,
    `lock_configuration=true`, watchdog `interrupt()`.
  - `AuditLog(path, run, task)`; `agent.evidence.redact.redact_obj`.
  - `orchestrator/triage.py`: `READABLE_ROOTS` = src/agent, manual_loop,
    orchestrator, docs; `PROTECTED` is a path-prefix list checked at parse time.
  - `evals/runner.py`: `--resume` skips a task whose `answer.json` exists.
- **V9:** there was no Spider2 clone (the Lite setup fetches pieces into
  `evals/spider/`). See X1.

## §1.1 items

| ID | Status | Decision |
| --- | --- | --- |
| B1 | Resolved | OpenAI Responses API; key in host `.env` only. |
| **B2** | **Implemented — owner to confirm** | `manifests/excluded.json` excludes the 4 tasks that have **no gold database at all**: `airbnb002`, `biketheft001`, `google_ads001`, `gitcoin001` (gitcoin001 also has no start DB). Neither zip contains them, so `evaluate.py` cannot score them. 68 − 4 = 64. The file is marked PROPOSED. If the owner picks a different 4, rerun `build_manifest`, delete and regenerate `splits.json` (the splits are bound to the manifest hash). |
| B3 | Resolved | main `gpt-6.1-sol`, subagents `gpt-6-luna`. Echoed model ids are recorded per call (`usage_by_role.*.models_returned`, run_manifest `roles.*.models_returned`). |
| B4 | Resolved | Docker. Engine 29.8.0 (client and server). |
| D1 | Implemented | The seven main-role tools of §6.5, strict schemas. One addition: `run_dbt` rejects any selector token starting with `-` (`--vars`, `--target` …), found by a test — the selector is split into argv. |
| D2 | Implemented | main 40, explore 15, diagnose 10, `max_subagent_calls = 10`, `task_timeout_s = 900`. Added `[delegation] subagent_timeout_s = 300` so one subagent call cannot use the whole task budget; a subagent timeout returns `{"error": "budget"}` to main and never ends the task. |
| D3 | Implemented | k = 3; `splits.json`: 13 held-out / 51 dev, `random.Random(42).sample`. |
| **D4** | **Implemented — owner to confirm** | **dbt-core 1.9.x / dbt-duckdb 1.9.x**, not "whatever uv resolves". uv resolved dbt-core 1.12.5, which refuses projects whose `dbt_packages/` holds packages not declared in `packages.yml` ("dbt expects 0 package(s)… found 1") — that is most Spider2-DBT examples (e.g. activity001). Spider2's reference image installs an unpinned `dbt-duckdb` built around 2024-12 (the data zips are dated 2024-12-19), i.e. the 1.9 line. Pinned `dbt-core>=1.9,<1.10`, `dbt-duckdb>=1.9,<1.10` → locked 1.9.11 / 1.9.6. duckdb 1.5.6 (shared with the gateway). |
| D5 | **Revised 2026-10-13** | Prices as listed. Cached input was costed at the full input price as an upper bound; with a 92-94% cache hit rate that bound ran 4.5x the real figure and was being quoted as the actual cost ($62.29 billed across ten runs against ~$13.65 real). `cached_input` is now priced explicitly per model; `cost_usd` is the real figure and `cost_usd_upper` keeps the old bound. |
| D6 | Implemented | Two read-only Luna roles, strict output schemas `explore_result`, `diagnose_result`. |
| D7 | Implemented | `--memory 4g --cpus 2 --pids-limit 256`. |
| D8 | Implemented — owner to confirm | Python 3.12 (`python:3.12-slim@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f`, pulled 2026-10-07). |
| D9 | Implemented — owner to confirm | Lite runner, root `Dockerfile`, `compose.yaml` untouched (`git diff master --stat` empty for them). DBT harness runs on the host. |
| D10 | Open | `concurrency = 1` in both configs; the runner supports N workers. Note: `divvy001` scoring alone takes ~6 min per evaluation (large tables in pandas). |
| O1 | Open | `anthropic` still a dependency; it is therefore in the image's requirements (inert: no key, no network). No DBT code imports it. |
| O2 | Answered | See Step 0. |
| V1 | Verified | CLI `--result_dir` (required) / `--gold_dir`; asserts exactly one `results_metadata.jsonl` and one `spider2_eval.jsonl`. Final line `"<acc> <passed> <total>"` (parsed by `scoring.FINAL_LINE`). `duckdb_match` opens both DBs `read_only=True`. **Any exception inside `duckdb_match` is swallowed and scored 0** — see V10/V11. Scoring writes only this task's eval line, so the denominator is always 1. |
| V2 | Verified | Each example ships `profiles.yml` in its root → `--profiles-dir /workspace`. 5 tasks need `dbt deps`: netflix001, scd001, zuora001, shopify_holistic_reporting001, retail001 (tpch001/002 have empty `packages.yml`). See X2. |
| V3 | Verified | `eval_utils.py` imports pandas, duckdb, tqdm and **`google.cloud.bigquery`** at module level → `google-cloud-bigquery`, pandas, tqdm added to the `dbt-sandbox` group. |
| V4 | Verified | See Step 0. |
| V5 | Verified | Longest staged path ≈ 233 chars (`nba001/.../_dlt_pipeline_state/...jsonl` under a `routed-<stamp>/attempt_3` dir) — under 260 with 27 to spare. Keep the repo path and run ids short. |
| V6 | Verified (2026-10-08) | After the key was replaced, `check_env` passed for both models: strict function call, `store=False` + `include=["reasoning.encrypted_content"]` across a turn, strict structured output; the API echoes `gpt-6.1-sol` / `gpt-6-luna` exactly. Neither returned reasoning items on the minimal call. Luna honored both subagent schemas in live runs. |
| V7 | Verified | Bind mount from `C:\` is fine: a `dbt run`/`dbt debug` in the sandbox takes ~3 s; staging a workspace is sub-second. No need to move to WSL2. |
| V8 | Verified | See Step 0. |
| V9 | Verified | See X1. Runs under `runs/dbt/` (already gitignored via `runs/`). |

## Benchmark data findings (new — owner decisions needed)

From the evaluator self-test (`scripts/selftest_evaluator`, results in
`runs/dbt/_selftest/20261007-221405.json`): **59/64 pass gold-as-prediction**.
The 5 that do not:

> **RESOLVED 2026-10-12 (V10).** Option (c) is implemented:
> `scoring._repair_gold_filename` places the single present gold file under
> the spec's name **inside the staged scratch copy only**, and fires only when
> the named file is absent and exactly one candidate exists. Re-running the
> self-test on those tasks: **3/3 now pass gold-as-prediction**, gold hashes
> unchanged, no scoring dirs left. The self-test figure becomes 62/64.
>
> It recovered no agent passes -- all three still score 0 against the correct
> gold across three runs' submissions -- but they now fail for the real
> reason. V12 (`chinook001`) and the `tpch002` source-data gap remain genuinely
> unpassable, so the ceiling is **62/64**.

| ID | Item | Task(s) | Effect |
| --- | --- | --- | --- |
| **V10** | Gold filename mismatch | `xero_new001`, `xero_new002` (spec names `xero.duckdb`, file is `xero_new.duckdb`); `social_media001` (spec names `social_media_reporting__rollup_report.duckdb`, file is `social_media.duckdb`) | The evaluator cannot open the gold file → always 0. Unpassable as shipped. Options: (a) leave as permanent fails, (b) swap them into the excluded 4, (c) in the *scratch copy only*, also place the gold under the spec's name. The manifest records `gold_file_present: false` for them. |
| **V12** | Gold equals start DB | `chinook001` | Gold `chinook.duckdb` is **byte-identical to the start DB** and has none of the three scored tables → always 0. Unpassable. |
| **V11** | Nondeterministic gold | `f1001` | `most_fastest_laps` is a VIEW `… ORDER BY fastest_laps DESC LIMIT 20` with ties at the cutoff; DuckDB returns a different tied set run to run. Gold-vs-gold scored 1 and 0 on consecutive runs. Any prediction passes this task by chance. |
| **V13** | Trivially passable | `playbook001`, `mrr001`, `mrr002`, `quickbooks003`, `salesforce001`, `divvy001`, `superstore001`, `f1003`, `retail001` | The **unmodified start DB passes**. An agent that does nothing scores these 9. Confirmed in the smoke run: `playbook001` scored a pass after an agent_error. |

So of 64 tasks, 4 cannot be passed, 1 is a coin flip, and 9 are free. The
owner should decide before any number is reported; the harness reports the
raw evaluator verdict regardless.

Gold hashes were unchanged after the self-test; no `scoring/` dirs remained.

## Deviations and additions (X)

| ID | What | Why |
| --- | --- | --- |
| X1 | Spider2 cloned to **`vendor/Spider2`** (sparse: `spider2-dbt/` plus `methods/spider-agent-dbt` to check the reference dbt version), commit `cafb867313aab4e674652054198f383cf4018943`. `vendor/` added to `.gitignore` **and the root `.dockerignore`**. | Writing outside the repo was not permitted. The root `.dockerignore` line is the one change to the Lite build: without it, the Lite image's `COPY . .` would pick up the DBT gold. Zip sha256: `DBT_start_db.zip` 143d0d4f…4375, `dbt_gold.zip` 0e351c52…3fdc (full values in `manifests/checksums.json`). |
| X2 | `dbt deps` runs **once per task**, not per attempt: a one-shot container with network, mounting only a scratch copy, writes `runs/dbt/_deps/<id>/` (packages + `package-lock.yml` + hash). Staging copies it in; hashes go into `run_manifest.json`, so `--resume` refuses if they change. | Keeps the network out of every measured attempt and pins package versions across attempts and configs. |
| X3 | Gateway: DuckDB allowlist covers **every user schema** of the file, not just `main`. Tests added in `tests/test_gateway.py`. | dbt `+schema:` configs build into `main_staging` etc.; the agent could not query models it had just built. |
| X4 | `pyproject.toml` gains `[tool.pytest.ini_options]`: `testpaths = ["tests"]`, `norecursedirs` incl. `vendor`, and the `docker` marker. | A bare `pytest` walked into the dbt packages' own test suites in `vendor/`. |
| X5 | `loop_openai.run_loop` gains keyword-only `model, strict_tools, deadline, text_format, store, include, reasoning, on_response`; defaults reproduce the old requests exactly (tested). Stop reason `"timeout"` added. | §3 requirements; one loop for every role. |
| X6 | Added `evals/dbt/config.py` (not in §2's tree). | Config loading/validation and the compare-surface diff live in one place. |
| X7 | `query_duckdb` "types" are Python-side types of returned values; `row_count` beyond the cap comes from a second gateway `COUNT(*)`. | The gateway returns rows, not engine types; `DESCRIBE` is refused as non-SELECT. Gateway left unchanged here. |
| X8 | Main prompt: the loop has no task-agnostic system prompt (the planner's prompts are data-agent specific), so **`prompts/main.md` is a minimal role prompt** (role, workspace, tools only, done condition), per §4.3's fallback. | — |
| X9 | `timeout` and `agent_error` attempts are scored but recorded `passed: false`; the evaluator verdict is kept as `scored_pass`. | §6.7. |
| X10 | `dbt debug` exits 1 in the sandbox (no `git` in the image); readiness is read from its output (profiles/project valid, connection ok). | `git` is only for git packages via `dbt deps`, which never runs in a task container. |

## Step results

| Step | Result |
| --- | --- |
| 1 | This file; CLAUDE.md section drafted (additive only). **Awaiting owner approval.** |
| 2 | `evals/dbt/` and `tests/dbt/` created. |
| 3 | Dependencies added; `check_env` passes in full (the live API calls passed on 2026-10-08, after the key was replaced). |
| 4 | 68 tasks in both JSONL files; every kept task has one start `.duckdb`. |
| 5 | Gateway already had the DuckDB target; `sandbox_tools/sql.py` adapter + X3 + tests. |
| 6 | Image built (`spider2-dbt-sandbox:599ffc238143`, id `sha256:06ed87b7…`); no `.env`/key in image or history; same tag on rebuild; tree clean after build. |
| 7 | `tasks_64.json` built from the proposed B2 list; rebuild byte-identical. |
| 8 | `splits.json` 13 + 51, disjoint, bound to the manifest hash. |
| 9 | 59/64 gold passes — see V10–V13. |
| 10 | Triage `PROTECTED` extended; tests show gold and `scoring/` unreadable and DBT graders unproposable. |
| 11 | **64/64 ready**; 5 deps tasks cached; source hashes unchanged; container isolation tests pass. |
| 12 | Tools + routing: unit tests and live-container tests pass. |
| 13–15 | Fake-model tests as above, plus **live smoke runs (2026-10-08, $1.39 total)**: baseline `activity001` pass (11 turns, $0.13, 108 s; first request exactly the 7 tools, no hosted tools); routed `activity001` pass (16 main turns, 2 `explore` delegations, $0.32, of which Luna $0.012); both configs at concurrency 2 on `asana001` + `atp_tour001` (1 pass / 3 fail, all `completed`, attempts overlapped, no containers left). The first baseline run died when Docker Desktop stopped responding (restarted with `docker desktop start`); `--resume` then completed it. No key in `runs/`. |
| 16 | Report + `--compare` with fixture tests (pass@1, pass^3, splits, cost sums, refusals). |
| 17 | Not done: needs owner decisions on B2, D4, D8–D10, V10–V13, the CLAUDE.md section, and a commit. |
