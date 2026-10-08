# PROJECT_SETUP.md — Spider2-DBT evaluation environment (added alongside the existing repo)

**Primary source:** *AI data agent system design — study guide* (rev 47).
**Secondary sources:**

- The existing *Spider2-DBT Runner Spec*.
- The repo review of `C:\Users\l\agent-engineering` (`master`, clean tree, 2026-10-07).
- The repo's own `CLAUDE.md`, which **takes precedence over this guide wherever they conflict**.

**Facts checked 2026-10-07 (US Eastern):** benchmark facts against `xlang-ai/Spider2` (main); OpenAI model IDs and prices against [developers.openai.com/api/docs/models](https://developers.openai.com/api/docs/models). Items marked **V#** must still be confirmed against the real files.

> **Scope.** The repo already has an OpenAI-based agent (`src/agent/`), one OpenAI loop (`manual_loop/loop_openai.py`), and a working Spider2-**Lite** runner (`evals/runner.py`, run via `Dockerfile` and `compose.yaml`). This guide **adds** a Spider2-DBT harness in its own namespace, `evals/dbt/`. It **reuses** the existing loop and SQL gateway, and **does not modify** the Lite runner, its Docker or compose setup, or `evals/sandbox.py`.
>
> **Models.** Main role: **`gpt-6.1-sol`**, which does the planning and writes the dbt models. Subagents: **`gpt-6-luna`**. This follows the source's rule: "Small model for reranking, SQL repair, summaries; frontier model for planning, synthesis, verification." OpenAI Responses API only. No Claude or Anthropic code is added.
>
> **Sandbox.** Each DBT task attempt runs in its own Docker container: no network, no API key, no gold files. The DBT harness runs on the host.

This document is the implementation guide. It does not implement anything.

---

## 1. Project overview and baseline objective

**Objective.** A reproducible evaluation environment on a fixed 64-task subset of Spider2-DBT, with k = 3 attempts per task, scored by the benchmark's own `evaluate.py`. Two configurations:

| Config | Setup | Role |
| --- | --- | --- |
| `evals/dbt/configs/baseline.toml` | Main role only: one loop, no subagents | **Control.** The source says to "start with one loop." |
| `evals/dbt/configs/routed.toml` | Main role plus `gpt-6-luna` subagents (§6.6) | **Target.** Measured against the control. |

Both share the manifest, splits, tools, sandbox image, evaluator and main model. Only the delegate tools and the subagent roles differ. The comparison is the source's "one before-and-after … and its measured effect."

**Numbers to produce for each config.** These come from the source's Spider2-DBT section:

- pass@1 and pass^3.
- Failure breakdown by stage (retrieval, generation, execution, interpretation), with one example each.
- Tokens, cost and time per task, by role.

**Design rules.** From the source, plus the repo's `CLAUDE.md`:

- **One loop, one gateway.** Every role runs through `manual_loop/loop_openai.py`. All SQL goes through `src/agent/gateway/`, the only module allowed to import `duckdb` (per CLAUDE.md). The gateway also runs inside the sandbox image. Third-party code (dbt-duckdb, Spider2's `evaluate.py`) is outside this rule.
- **The model proposes, code decides.** Tool calls execute in the sandbox: workspace-only file tools, the gateway (read-only, `enable_external_access=false`, `lock_configuration=true`), and allowlisted dbt commands.
- **Isolation in infrastructure, not prompts.** The source's tenant-cell idea is applied per task:
  - one container per task attempt
  - only that task's workspace mounted
  - `--network none`
  - non-root user, dropped capabilities, read-only root filesystem, resource caps
- **Key rule.** `OPENAI_API_KEY` may exist in the host process, and in the Lite harness container as `compose.yaml` does today. It must **never** enter a DBT task container, an evaluator container or an image.
- **Graders and eval sets are protected.** New DBT graders and gold paths are added to `orchestrator/triage.py`'s `PROTECTED` and `READABLE_ROOTS` controls (Step 10).
- **Deterministic checks and repeat runs.** A pass means the official evaluator matched the output tables to gold. Report pass^k.
- **Pin and record everything.** Models per role, prompts, tool specs, packages, image ID, dataset and manifest.

**Not part of this setup:** credential broker, durable workflow engine, cost approvals, catalog index, and the separate verifier (§8 optional).

### 1.1 Open items

| ID | Type | Item | Blocks |
| --- | --- | --- | --- |
| **B1** | Resolved | Model provider: OpenAI (Responses API), already used by the repo. Key in the host `.env` only. | — |
| **B2** | **Blocker** | **The 64-task subset is undefined.** Both Spider2 JSONL files list 68 tasks; the README says 69. Which 4 are excluded, and why, isn't documented. The owner supplies `evals/dbt/manifests/excluded.json`. | Steps 7–17 |
| **B3** | Resolved | Main: `gpt-6.1-sol`. Subagents: `gpt-6-luna` (owner, 2026-10-07). No dated snapshots are listed, so the model ID the API echoes back is recorded on every call. | — |
| **B4** | Resolved | DBT sandbox: Docker (owner, 2026-10-07). | — |
| **D1** | Provisional | **Main-role tools.** The seven functions in §6.5. No shell. | Step 12 |
| **D2** | Provisional | **Budgets:**<br>• main `max_turns = 40`<br>• explore 15, diagnose 10<br>• `max_subagent_calls = 10` per task<br>• `task_timeout_s = 900`, covering all roles | Full run |
| **D3** | Provisional | **Repeats and split:** k = 3; 13 held-out tasks, seed 42. | Step 8 |
| **D4** | Decision | **dbt versions:** whatever uv resolves at Step 3. Baked into the image; never upgraded within a run series. | Steps 3, 6 |
| **D5** | Prefilled | **Pricing (USD per 1M tokens, input / output), listed 2026-10-07:**<br>• `gpt-6-luna`: 0.10 / 0.50<br>• `gpt-6.1-sol`: 2 / 10<br>Cached-input prices aren't listed, so cached tokens are costed at the input price (an upper bound). | Full run |
| **D6** | Provisional | **Subagents:** two read-only Luna roles, `explore` (the source's catalog explorer) and `diagnose` (the source's SQL repair). | Step 14 |
| **D7** | Provisional | **Container limits:** `--memory 4g --cpus 2 --pids-limit 256`. | Step 11 |
| **D8** | Provisional | **Python 3.12**, matching `requires-python = ">=3.12"` and the existing `python:3.12-slim`. The Spider2 README's 3.11 is only an optional conda suggestion. Owner to confirm. | Step 3 |
| **D9** | Provisional | **Spider2-Lite runner, root `Dockerfile` and `compose.yaml` stay as they are.** The DBT harness runs on the host. Running it in compose would require mounting the Docker socket, which gives root on the host. Owner to confirm. | Step 1 |
| **D10** | Provisional | **Concurrency.** Worst case at `concurrency = 1`: 64 × 3 × 15 min ≈ **48 h per config, about 96 h for both**. Provisional plan: `concurrency = 1` for smoke runs, then `concurrency = 4` for full runs (≈ 12 h worst case per config). Compared runs must use the same value. | Full run |
| **O1** | Owner question | **Anthropic remnants.** `anthropic>=1.6.0` remains a dependency, and `docs/build_architecture.py` checks `ANTHROPIC_API_KEY`. This guide leaves both alone, and new DBT code must not import `anthropic`. | — |
| **O2** | Owner question | **Missing Lite sandbox.** CLAUDE.md names `evals/sandbox.py` as the Lite harness's SQLite read-only sandbox, but the file is missing. That's a Lite issue, out of scope here; DBT uses `evals/dbt/sandbox.py`. | — |
| **V1** | Verify | **`evaluate.py`.** Confirm the CLI (`--result_dir`, `--gold_dir`; asserts `results_metadata.jsonl` and `spider2_eval.jsonl`), the final accuracy line format, and whether `duckdb_match` opens DBs read-write. Scoring uses scratch copies either way (§6.3). | Step 9 |
| **V2** | Verify | Per-task `profiles.yml` location, and any `dbt deps` (network) needs. | Step 11 |
| **V3** | Verify | `evaluate.py`'s third-party imports (tqdm observed) → the `dbt-sandbox` dependency group. | Step 4 |
| **V4** | Verify | **Repo state** per the 2026-10-07 review: §1.2. | Step 0 |
| **V5** | Verify | Windows path length (> 260 characters) under `runs/dbt/` on the host side of the bind mount. | Step 11 |
| **V6** | Verify | Both models work through the loop: function calling, reasoning items with `store=False`, and Luna honoring the structured-output schema. | Steps 13–14 |
| **V7** | Verify | Docker Desktop bind-mount speed from `C:\`. Time one `dbt run`; if it's slow, move the repo into the WSL2 filesystem. | Step 11 |
| **V8** | Verify | **Interfaces before extending.** Read the current signatures of:<br>• `manual_loop/loop_openai.py`<br>• `src/agent/gateway/`<br>• `src/agent/audit/` and `src/agent/evidence/`<br>• `orchestrator/triage.py` (`PROTECTED`, `READABLE_ROOTS`)<br>• `evals/runner.py` (resume and audit conventions)<br>This guide states what's *required* of them, not their current APIs. | Step 0 |
| **V9** | Verify | Where the Lite setup already keeps a Spider2 clone and its run outputs. Reuse that clone, and use `runs/dbt/` or the repo's equivalent convention. | Step 0 |

Record each resolution in `docs/DECISIONS.md` (create it or extend it): ID, decision, date, who decided.

### 1.2 Repo state this guide builds on (V4, from the 2026-10-07 review)

| Area | State |
| --- | --- |
| History | 15+ commits on `master`. Latest: "Experiment 1: give sql mode its own tool surface." |
| Agent | `src/agent/` (catalog, gateway, evidence, verifier, report, audit), on the OpenAI Responses API. There's no root `agent.py` or `config.py`. |
| Loop | `manual_loop/loop_openai.py`. CLAUDE.md: "do not write a second one." |
| Lite harness | `evals/runner.py`: a working Spider2-Lite runner with resume, audit logs and the scorer. `evals/sandbox.py` is reserved by CLAUDE.md for Lite but missing (O2). |
| Docker | Root `Dockerfile` (`python:3.12-slim`) and `compose.yaml` run the **Lite harness** in a container, with the key passed in. |
| Python | `requires-python = ">=3.12"` |
| Triage | `orchestrator/triage.py` protects graders and eval sets through `PROTECTED` and `READABLE_ROOTS`. |
| Dependencies | `claude-agent-sdk` already removed; `anthropic` still present (O1). |

---

## 2. Directory structure

Only paths marked NEW are created. Paths marked EXTEND get additive changes. Everything else stays untouched.

```text
agent-engineering/
├── CLAUDE.md                              # EXTEND · add a short "Spider2-DBT harness" section (Step 1)
├── src/agent/
│   ├── gateway/                           # EXTEND only if needed · DuckDB-file target with the CLAUDE.md flags (Step 5)
│   ├── audit/  evidence/                  # REUSE · JSONL writing, redaction, hashing where they fit
│   └── catalog/ verifier/ report/         # untouched
├── manual_loop/loop_openai.py             # EXTEND only if needed · role parameters (Step 13); no second loop
├── orchestrator/triage.py                 # EXTEND · add DBT graders + gold to PROTECTED; keep gold out of READABLE_ROOTS
├── evals/
│   ├── runner.py                          # Lite runner · untouched
│   └── dbt/                               # NEW · everything Spider2-DBT
│       ├── __init__.py
│       ├── runner.py                      # run orchestration CLI (mirrors evals/runner.py conventions)
│       ├── tasks.py                       # manifest + splits loading
│       ├── sandbox.py                     # stage workspaces; start/stop/clean task containers
│       ├── agent_adapter.py               # builds role configs; calls manual_loop/loop_openai.py
│       ├── tools.py                       # TOOL_SPECS + role-checked dispatch (host side)
│       ├── docker_exec.py                 # runs one tool call in the task container
│       ├── delegate.py                    # delegate_explore / delegate_diagnose → subagent loops
│       ├── submission.py                  # results_metadata.jsonl + prediction DB placement
│       ├── scoring.py                     # evaluator container on scratch copies; re-score CLI
│       ├── records.py                     # thin layer over src/agent/audit; run_manifest, result.json
│       ├── report.py                      # summary + --compare
│       ├── image.py                       # content-hash image tag (shared by build script and runner)
│       ├── sandbox_tools/                 # CONTAINER side · copied into the image
│       │   ├── __init__.py
│       │   ├── toolhost.py                # `python -m sandbox_tools.toolhost <tool>`, JSON in/out
│       │   ├── path_guard.py              # only /workspace
│       │   ├── file_tools.py              # read/list/search/write/edit
│       │   ├── sql.py                     # thin adapter → agent.gateway (imports no duckdb)
│       │   └── dbt_runner.py              # run_dbt + final_dbt_run
│       ├── prompts/                       # main.md, sub_explore.md, sub_diagnose.md
│       ├── configs/                       # baseline.toml, routed.toml (no image tag inside)
│       ├── manifests/                     # excluded.json, tasks_64.json, splits.json, checksums.json
│       └── scripts/                       # check_env, build_image, build_manifest, make_splits,
│                                          #   selftest_evaluator, check_tasks_dbt
├── docker/dbt-sandbox/                    # NEW · separate from the Lite root Dockerfile
│   ├── Dockerfile
│   ├── Dockerfile.dockerignore            # allowlist; applies to this Dockerfile only
│   └── requirements.txt                   # generated by uv export; never hand-edited
├── tests/dbt/                             # NEW · unit tests + `-m docker` integration
├── runs/dbt/<run_id>/                     # NEW outputs (gitignored; V9)
├── docs/DECISIONS.md, docs/results/dbt/   # NEW or EXTEND
├── Dockerfile, compose.yaml               # Lite · untouched (D9)
├── pyproject.toml, uv.lock                # EXTEND · `dbt-sandbox` dependency group
└── PROJECT_SETUP.md
```

Run DBT modules as `uv run python -m evals.dbt.<module>` and scripts as `uv run python -m evals.dbt.scripts.<name>`.

---

## 3. Directory and file responsibilities

| Path | Responsibility | Must not |
| --- | --- | --- |
| `CLAUDE.md` (new section) | State the DBT namespace, that the gateway also runs in the sandbox image, the key rule, and the DBT protected paths. **Owner reviews the diff.** | Weaken existing rules |
| `src/agent/gateway/` | The only `duckdb` importer. Requirements for a DuckDB-file target:<br>• `read_only=True`, `enable_external_access=false`, `lock_configuration=true`<br>• one SELECT/UNION/WITH statement<br>• injected LIMIT and a `COUNT(*)`<br>• timeout via `interrupt()`<br>• scrubbed errors | Break the Lite gateway tests |
| `manual_loop/loop_openai.py` | The one loop. Every DBT role needs it to accept, per call:<br>• model, instructions, tool specs<br>• `max_turns`, a deadline<br>• a dispatch callback<br>• an optional strict `text.format` schema<br>• `store=False`, and the reasoning `include` option<br>• a per-turn log hook<br>• per-call usage and the echoed model ID<br>Missing ones are added as keyword arguments whose defaults keep current behavior. | Change Lite behavior |
| `orchestrator/triage.py` | `PROTECTED` gains the DBT graders and eval sets. `READABLE_ROOTS` must not cover Spider2 gold or `runs/dbt/**/scoring/`. | Lose existing entries |
| `evals/dbt/agent_adapter.py` | Build `Role` objects from config and run `main` through the loop. Its subagent hook calls the same loop. | Implement a loop |
| `evals/dbt/tools.py` | `TOOL_SPECS` (`strict: true`). `dispatch()` checks the role's tool list and routes to `docker_exec` or `delegate`. Always returns a string; errors are `{"error": ...}`. | Execute file, SQL or dbt work on the host |
| `evals/dbt/docker_exec.py` | `docker exec -i <ctr> timeout <s> python -m sandbox_tools.toolhost <tool>`, JSON args on stdin, one JSON result on stdout. Host timeout as a backstop. | Pass environment variables |
| `evals/dbt/delegate.py` | Build the brief, run the subagent role in the **same** container, validate and truncate its JSON, enforce `max_subagent_calls` | Pass main's history; give subagents write, dbt or delegate tools |
| `evals/dbt/sandbox_tools/*` | Execute calls inside `/workspace`. `sql.py` delegates to `agent.gateway`. `path_guard` rejects outside paths, `..` escapes, symlink escapes, and writes to `*.duckdb`. | Import `duckdb`, `openai` or `anthropic`; read env secrets |
| `evals/dbt/sandbox.py` | Verify the source checksum, stage the workspace, start the container (§6.4), stop and remove it, clean up by label | Mount anything but the workspace; touch `evals/sandbox.py` |
| `evals/dbt/scoring.py` | Score in a one-shot evaluator container on scratch copies (§6.3), then delete the scratch dir | Score against the original gold or submission files |
| `evals/dbt/records.py` | Reuse `src/agent/audit` for JSONL and redaction where it fits. Add only what's missing: `run_manifest.json`, `result.json`, atomic writes. | Duplicate audit or redaction logic |
| `evals/dbt/image.py` | `content_tag()`: sha256 over `docker/dbt-sandbox/*`, `pyproject.toml`, `src/**` and `evals/dbt/sandbox_tools/**` → `spider2-dbt-sandbox:<12 hex>` | Read tags from committed configs |
| `evals/dbt/runner.py` | Create or resume runs, loop attempts and tasks (`concurrency` workers), write results. Mirror `evals/runner.py` CLI and resume conventions. | Import from `evals/runner.py` internals unless they're designed for reuse |

---

## 4. Required tooling and dependencies

| Component | Where | Version policy | Basis |
| --- | --- | --- | --- |
| Docker Desktop / Engine | Host | Record versions per run | B4 |
| `python:3.12-slim` | DBT image | **Pinned by digest** | D8; matches the repo |
| Python 3.12, uv, git | Host | Existing | Repo |
| `openai` | Host (existing dependency) | Existing lock | Repo |
| `duckdb`, `sqlglot` | Existing dependencies used by the gateway; added to `dbt-sandbox` only if they're missing | Locked | CLAUDE.md, source |
| `dbt-core`, `dbt-duckdb` | `dbt-sandbox` group | Locked at Step 3 (D4) | Benchmark |
| `evaluate.py` imports | `dbt-sandbox` group | Locked (V3) | Spider2 |
| `gdown` | `dbt-sandbox` group (host use only) | Locked | Spider2 README |
| `pytest` | Existing dev group | Existing | Repo |

The host drives Docker through the `docker` CLI (`subprocess`). No Docker SDK dependency is added.

### 4.1 `docker/dbt-sandbox/Dockerfile` and its ignore file

```dockerfile
FROM python:3.12-slim@sha256:<digest>          # pin; record in DECISIONS.md
RUN useradd --create-home --uid 1000 agent
COPY docker/dbt-sandbox/requirements.txt /opt/sandbox/requirements.txt
RUN pip install --no-cache-dir -r /opt/sandbox/requirements.txt
COPY pyproject.toml /opt/project/pyproject.toml
COPY src/ /opt/project/src/                     # provides agent.gateway (plus any file the build backend needs, e.g. README)
RUN pip install --no-cache-dir --no-deps /opt/project
COPY evals/dbt/sandbox_tools/ /opt/sandbox/sandbox_tools/
ENV HOME=/tmp PYTHONPATH=/opt/sandbox PYTHONDONTWRITEBYTECODE=1 SANDBOX_WORKSPACE=/workspace
USER agent
WORKDIR /workspace
```

`docker/dbt-sandbox/Dockerfile.dockerignore` is an allowlist, so `.env` can never enter the build context. It doesn't affect the Lite build.

```text
*
!pyproject.toml
!src/
!evals/dbt/sandbox_tools/
!docker/dbt-sandbox/requirements.txt
```

`requirements.txt` = `uv export --no-dev --group dbt-sandbox --no-hashes --no-emit-project --format requirements-txt`. That's the main dependencies plus the group. The SDKs inside the image are inert, because no key is ever present.

### 4.2 `evals/dbt/configs/baseline.toml` (control)

```toml
experiment = "baseline"
attempts = 3                        # D3
task_manifest = "evals/dbt/manifests/tasks_64.json"
splits = "evals/dbt/manifests/splits.json"
spider2_root = "<existing clone>/spider2-dbt"   # V9: reuse the Lite clone if present
runs_dir = "runs/dbt"               # V9
concurrency = 1                     # D10: smoke = 1; full runs = 4 (compared runs must match)
task_timeout_s = 900                # D2; covers every role in the task

[sandbox]
image = "auto"                      # runner derives spider2-dbt-sandbox:<content hash>; no tag stored here
network = "none"
memory = "4g"                       # D7
cpus = "2"                          # D7
pids_limit = 256                    # D7
user = "1000:1000"                  # Linux host: host uid:gid (V7)
keep_failed_containers = false      # debugging only; never for reportable runs

[api]
timeout_s = 120
max_retries = 2

[roles.main]
model = "gpt-6.1-sol"
system_prompt = "evals/dbt/prompts/main.md"
tools = ["read_file", "list_files", "search_files", "write_file", "edit_file",
         "query_duckdb", "run_dbt"]  # D1
max_turns = 40                      # D2
# reasoning_effort = ""             # optional; sent only if set; recorded

[limits]
tool_output_max_chars = 4000

[gateway]
max_rows_returned = 20
query_timeout_s = 30

[dbt]
allowed_commands = ["run", "build", "test", "compile", "parse"]
tool_timeout_s = 300
final_run = true
final_run_timeout_s = 600

[pricing]                           # D5
"gpt-6-luna"  = { input = 0.10, output = 0.50 }
"gpt-6.1-sol" = { input = 2.00, output = 10.00 }
```

### 4.3 `evals/dbt/configs/routed.toml` (target)

The same as baseline except for the lines below. `--compare` asserts every other key matches.

```toml
experiment = "routed"

[roles.main]
tools = ["read_file", "list_files", "search_files", "write_file", "edit_file",
         "query_duckdb", "run_dbt", "delegate_explore", "delegate_diagnose"]

[roles.explore]
model = "gpt-6-luna"
system_prompt = "evals/dbt/prompts/sub_explore.md"
tools = ["read_file", "list_files", "search_files", "query_duckdb"]
max_turns = 15
output_schema = "explore_result"

[roles.diagnose]
model = "gpt-6-luna"
system_prompt = "evals/dbt/prompts/sub_diagnose.md"
tools = ["read_file", "search_files", "query_duckdb"]
max_turns = 10
output_schema = "diagnose_result"

[delegation]
max_subagent_calls = 10
result_max_chars = 2000
```

**Main prompt** (`evals/dbt/prompts/main.md`):

- If the system prompt `loop_openai.py` uses today is task-agnostic, copy it.
- Otherwise write a minimal role prompt: the role, the workspace, "use only the provided tools", and "done = the dbt project builds the requested models."
- No task-specific hints. Record which option you took.

---

## 5. Spider2-DBT 64-task benchmark integration

### 5.1 Source layout (observed; confirm at Step 4)

```text
<spider2_root>/                     # = <Spider2 clone>/spider2-dbt
├── examples/spider2-dbt.jsonl      # 68 lines; keys: instance_id, instruction, type
├── examples/<instance_id>/         # dbt project + start .duckdb (placed by setup.py)
├── evaluation_suite/evaluate.py    # --result_dir (required), --gold_dir (default ./gold)
├── evaluation_suite/gold/spider2_eval.jsonl        # 68 lines; instance_id + evaluation{func, parameters}
├── evaluation_suite/gold/<instance_id>/<gold>.duckdb
└── setup.py                        # DBT_start_db.zip → examples/, dbt_gold.zip → evaluation_suite/gold/
```

The eval spec, e.g. `duckdb_match` with `gold`, `condition_tabs`, `condition_cols` and `ignore_orders`. Spider2's own harness (`methods/spider-agent-dbt`) is not used.

### 5.2 Subset definition (B2)

- `evals/dbt/manifests/excluded.json`: `{"source_task_count": 68, "excluded": [{"instance_id": "...", "reason": "..."}]}` (owner-supplied).
- `build_manifest` writes `tasks_64.json` with, per task:
  - `instance_id`, `instruction` (verbatim), `type`
  - `start_db` (the single `*.duckdb` in the example dir)
  - `evaluation` (verbatim)
  - `example_dir_sha256`, `gold_dir_sha256`
- The file header records `spider2_commit`, `source_task_count`, `task_count` and `excluded_sha256`.

The script fails loudly instead of guessing:

- Both JSONL files have the same 68 IDs.
- Every excluded ID exists, and `len(excluded) == 4`.
- Each task has one `.duckdb` and its gold file.
- Every `func == "duckdb_match"`; any other function stops the build for owner review.
- Task order follows `spider2-dbt.jsonl`.
- Directory hash = sha256 over sorted `(relative_path, file_sha256)` pairs.

### 5.3 Splits

`splits.json` = `{"seed": 42, "held_out": [13], "dev": [51], "manifest_sha256": "..."}` from `random.Random(42).sample`.

Tune only on `dev`. Report `held_out` at milestones only. Never regenerate the file.

### 5.4 Integrity and gold isolation

- `checksums.json` stores the zip hashes and every example and gold directory hash.
- The example hash is re-verified before each staging; on a mismatch the run aborts. The gold hash is re-verified after every evaluator self-test and at the end of each run.
- Gold is **never** mounted into a task container. That also blocks agent-written SQL or dbt models from `ATTACH`ing or `read_*`ing gold: no such path exists inside.
- Task containers have no network.
- Gold reaches only the evaluator container, as a **scratch copy**. That container starts after the task container is removed, and the scratch copy is deleted after scoring.
- The triage agent can't read gold or `scoring/` dirs, and can't modify graders or eval sets (Step 10).

---

## 6. Execution and evaluation workflow

### 6.1 Commands

```bash
uv run python -m evals.dbt.scripts.build_image                                 # build spider2-dbt-sandbox:<content hash>
uv run python -m evals.dbt.runner --config evals/dbt/configs/baseline.toml
uv run python -m evals.dbt.runner --config evals/dbt/configs/routed.toml
uv run python -m evals.dbt.runner --config evals/dbt/configs/routed.toml --tasks playbook001,f1001 --attempts 1   # smoke
uv run python -m evals.dbt.runner --resume <run_id>
uv run python -m evals.dbt.scoring --run runs/dbt/<run_id>
uv run python -m evals.dbt.report --run runs/dbt/<run_id>
uv run python -m evals.dbt.report --compare runs/dbt/<baseline_id> runs/dbt/<routed_id>
```

`run_id` = `<experiment>-<YYYYMMDD-HHMMSS>`. Keep it short because of V5.

### 6.2 Run loop

1. **Validate before starting:**
   - Every role has a model, and the key is present on the host.
   - `docker version` succeeds.
   - The image `spider2-dbt-sandbox:<content_tag()>` exists. If it doesn't, stop with "run build_image." Record its image ID.
   - The manifest, splits and prompt hashes are readable.
   - The git tree is clean (`--allow-dirty` is recorded and makes the run non-reportable).
2. Remove containers labeled with this `run_id`.
3. Write `run_manifest.json` (§7.2).
4. Run attempt-major: for each attempt, run every task through §6.3, with up to `concurrency` tasks at a time.
5. **Resume rule.**
   - A task-attempt is done if its `result.json` exists with `status != "harness_error"`.
   - Anything else (no `result.json`, or `harness_error`) is reset first: force-remove its container by name, **delete its whole task-attempt directory, including the half-edited `workspace/`**, then re-stage from the source.
6. Run `report.py`.

### 6.3 Per task-attempt

1. **Stage.**
   - Verify the example hash, then copy the example dir to `runs/dbt/<run_id>/attempt_<n>/<id>/workspace/`.
   - If V2 shows the task needs `dbt deps`: run one one-shot container **with** network, mounting only the workspace. Record the package lock, then remove the container.
2. **Start the task container** (§6.4) and write `container.json`.
3. **Agent.** `agent_adapter.run_task(instruction_verbatim, ctx)`. Every tool call from main or a subagent executes in this container. `task_timeout_s` covers everything.
4. **Final build.** `final_dbt_run` in the container → `dbt_final.log` and its exit code.
5. **Stop and remove** the container. Record the exit state and `OOMKilled`.
6. **Submission.** Move `workspace/<start_db>` to `submission/<id>/<start_db>`. Write the one-line `submission/results_metadata.jsonl`: `{"instance_id": id, "answer_type": "file", "answer_or_path": start_db}`.
7. **Score on scratch copies:**
   1. Create `scoring/`.
   2. Copy `gold/<id>/` and `spider2_eval.jsonl` into `scoring/gold/`, and `submission/` into `scoring/submission/`.
   3. Run:

      ```bash
      docker run --rm --network none --read-only --tmpfs /tmp --user 1000:1000 \
        -v <abs evaluation_suite>:/eval:ro -v <abs scoring>:/score:rw -w /eval \
        <image> python evaluate.py --result_dir /score/submission --gold_dir /score/gold
      ```

   4. Save the output to `evaluate_stdout.txt`.
   5. **Delete `scoring/`.**
   6. `passed` = parsed 1/1. Unparseable output is `harness_error`.

   Scratch copies work whether or not `duckdb_match` opens files read-write (V1), and the stored submission stays pristine for re-scoring.
8. **Record.** Write `result.json` atomically.

### 6.4 Task container

```bash
docker run -d --name s2dbt-<run_id>-a<n>-<instance_id> \
  --label s2dbt.run_id=<run_id> --label s2dbt.attempt=<n> --label s2dbt.instance_id=<instance_id> \
  --network none --cap-drop ALL --security-opt no-new-privileges \
  --read-only --tmpfs /tmp:rw,size=256m \
  --memory 4g --cpus 2 --pids-limit 256 --user 1000:1000 \
  -v "<abs host workspace>:/workspace:rw" \
  <image> sleep infinity
```

Exactly one mount. No `-e` or `--env-file`. One container per task attempt, shared by main and its subagents.

### 6.5 Loop use and tool execution

- **Loop.** `agent_adapter` builds a `Role` (model, prompt, tool specs, `max_turns`, optional output schema, optional `reasoning_effort`) and calls `manual_loop/loop_openai.py` with:
  - the deadline
  - `dispatch` as the function-call handler
  - `store=False`
  - `include=["reasoning.encrypted_content"]` when the model emits reasoning items (V6)
  - a per-turn log hook

  Usage is accumulated per role and per echoed model ID, and cost comes from `[pricing]`. An API error that survives the client's retries ends the task as `agent_error`. In a subagent, it becomes `{"error": ...}` returned to main.
- **Tool path.**
  1. `dispatch` checks the role.
  2. `docker_exec` runs `toolhost` in the container.
  3. A non-zero exit, a timeout or bad JSON comes back as `{"error": ...}`.
  4. If the container dies mid-call: when `OOMKilled` is true, the task ends as `agent_error` with failure category `execution`; otherwise it ends as `harness_error`.
- **No hosted OpenAI tools, and OpenAI's built-in multi-agent beta isn't used.** The explicit delegate tools keep each role's model, budget and logs visible to the harness.

**Main-role tools** (both configs; they execute in the container):

| Function | Purpose | Limits |
| --- | --- | --- |
| `read_file(path, offset, limit)` | Source's "Read" | `/workspace` only; ≤ `tool_output_max_chars` |
| `list_files(pattern)` | Source's "Glob" | ≤ 500 entries |
| `search_files(regex, path, glob)` | Source's "Grep" | ≤ 200 matches |
| `write_file(path, content)` | Create or overwrite (D1) | Deny `*.duckdb` |
| `edit_file(path, old, new)` | One exact, unique replacement (D1) | Deny `*.duckdb`; error on 0 or >1 matches |
| `query_duckdb(sql)` | `sandbox_tools/sql.py` → `agent.gateway` | The gateway rules in §3; returns columns and types, `row_count`, the first N rows, `truncated` |
| `run_dbt(command, select)` | dbt in `/workspace` (D1) | `command` ∈ allowlist; `select` empty or matching `^[\w.+*:@,/ -]+$`; `--profiles-dir` per V2; timeout; last 4,000 characters, scrubbed |

Strict schemas make every parameter required, so optional parameters are sent as empty strings or null.

### 6.6 Subagents (routed only)

| Main-role tool | Role, model | Subagent tools | Returns (strict schema) |
| --- | --- | --- | --- |
| `delegate_explore(question, focus_paths)` | `explore`, `gpt-6-luna` | read, list, search, `query_duckdb` | `{answer, relevant_files[], relevant_tables[{table, columns[]}], evidence[], open_questions[]}` |
| `delegate_diagnose(error_text, focus_paths)` | `diagnose`, `gpt-6-luna` | read, search, `query_duckdb` | `{likely_cause, file, location_hint, suggested_change, confidence: low/medium/high}` |

Rules, from the source's "Tenants and subagents" and "delegation brief":

1. **Fresh context.** The subagent gets its role prompt plus a brief: goal, inputs, schema, done condition, budget. It never sees main's history.
2. **Same container, narrower tools.** No write, edit, dbt or delegate tools.
3. **Shared budget.** Subagents count against their role's `max_turns`, `max_subagent_calls` and `task_timeout_s`. When a cap is hit, the delegate returns `{"error": "budget"}`.
4. **Small, validated results.** The JSON is validated and truncated to `result_max_chars`. An invalid result returns `{"error": ...}`. When stuck, a subagent returns partial results with `open_questions`.
5. **Discarded, but logged** under `subagents/`.

### 6.7 Scoring rules

- The denominator is always 64. Missing DB, timeout and `agent_error` count as fails.
- Any `harness_error` makes a run non-reportable until it's rerun.
- `evals.dbt.scoring --run` re-scores from stored submissions, using fresh scratch copies.

### 6.8 Metrics (`summary.json` and `summary.md`)

| Metric | Definition |
| --- | --- |
| Per-attempt pass rate | passed / 64 for each attempt. Min and max form the noise range. |
| pass@1 | Mean of the per-attempt pass rates |
| pass^3 | Tasks passed in all 3 attempts / 64 |
| Dev / held-out | The metrics above per split |
| Failure breakdown | Count per `failure_category`, plus one example per stage |
| By role | Mean and median tokens and `cost_usd` per task for main, explore and diagnose; `duration_s`; main turns |
| Delegation | Calls per task by role; error and budget-stop rates |
| Tool use | Calls per function, gateway rejections, tool errors, OOMs |
| **Compare** | Baseline vs routed: pass@1, pass^3, the noise range, cost per task, cost per passed task, time, and main-model tokens. Also tasks flipping pass↔fail in a majority of attempts. A difference within the noise range is reported as "no clear change." |

---

## 7. Logging and reproducibility requirements

### 7.1 Run directory

```text
runs/dbt/<run_id>/
├── run_manifest.json   runner.log   triage.csv   summary.json   summary.md
└── attempt_<n>/<instance_id>/
    ├── workspace/               # bind-mounted at /workspace
    ├── container.json           # name, ID, image ID, flags, times, exit code, OOMKilled
    ├── submission/results_metadata.jsonl, submission/<id>/<start_db>
    ├── transcript.jsonl         # main role, one line per turn
    ├── tool_calls.jsonl         # main role, one line per call
    ├── subagents/<seq>-<role>/  # brief.json, transcript.jsonl, tool_calls.jsonl, result.json
    ├── dbt_final.log   evaluate_stdout.txt   result.json
    └── (scoring/)               # exists only while scoring; deleted afterwards
```

### 7.2 `run_manifest.json`

It records:

- `run_id`, `created_at`, `git_sha`, `git_dirty`
- The full config
- **Per role:** model, settings sent, `max_turns`, prompt sha256, tool list, and the model IDs echoed by the API
- **Sandbox:** Docker client and server versions, image tag, **image ID**, base digest, `content_tag()` inputs hash, the container flags
- Host `openai` and Python versions, the `uv.lock` sha256, the OS
- `spider2_commit`, and the hashes of the manifest, excluded list, splits and checksums
- The `TOOL_SPECS` and schema hash
- k, budgets, pricing, `concurrency`, task count

`--resume` refuses if any of these changed.

### 7.3 `result.json`

```json
{ "run_id": "", "experiment": "baseline|routed", "attempt": 1, "instance_id": "", "split": "dev|held_out",
  "status": "completed|max_turns|timeout|agent_error|harness_error",
  "passed": false, "failure_category": null,
  "main_turns": 0, "duration_s": 0.0, "final_dbt_exit_code": null,
  "container": { "id": "", "exit_code": null, "oom_killed": false },
  "usage_by_role": { "main": { "model": "", "calls": 0, "input_tokens": 0, "cached_input_tokens": 0,
                               "output_tokens": 0, "reasoning_tokens": 0, "cost_usd": 0.0 } },
  "total_cost_usd": 0.0,
  "subagent_calls": { "explore": 0, "diagnose": 0, "errors": 0, "budget_stops": 0 },
  "tool_calls": 0, "tool_errors": 0, "gateway_rejections": 0, "dbt_calls": 0,
  "error": null, "started_at": "", "finished_at": "" }
```

`usage_by_role` includes `explore` and `diagnose` in routed runs.

### 7.4 JSONL logs

Use `src/agent/audit`'s writer and redaction where they fit (V8).

- **`transcript.jsonl`.** One line per turn:
  - `ts`, `role`, `turn`, `model_requested`, `model_returned`
  - Request summary (number of input items, prompt and tool-spec hashes)
  - Response `id` and output items
  - Truncated function outputs, with their original lengths
  - `usage`
- **`tool_calls.jsonl`.** One line per call:
  - `ts`, `role`, `turn`, `call_id`, `function`, `arguments`
  - `status` (`ok|rejected|error|timeout|budget|container_error`), the `docker exec` exit code, `duration_ms`
  - SQL hash and row count; dbt exit code; for delegates, `subagent_dir`
  - Scrubbed and raw errors (raw kept local only)

### 7.5 Status and failure taxonomy

- **`status`** reflects how main stopped. `harness_error` covers a missing image, a container start failure, a dead container that wasn't OOM-killed, and unparseable evaluator output. Rerun it before reporting.
- **`failure_category`** is set automatically where possible:
  - `budget`
  - `agent_error`
  - `execution` (final `dbt run` non-zero, or OOM)
  - otherwise `untriaged`
- **Manual triage** in `triage.csv` assigns the source's four stages:
  - **retrieval** (in routed runs, note whether `explore` was used)
  - **generation**
  - **execution**
  - **interpretation**

### 7.6 Rules

1. Never mutate the Spider2 clone. Each task-attempt gets a fresh workspace and a fresh container.
2. Never write the key anywhere, and never pass it to a DBT container. Redact its value in all logs. `store=False` on every call.
3. **Reportable run:**
   - clean tree
   - every role has a model
   - one image ID
   - `keep_failed_containers = false`
   - zero `harness_error`
   - the same `concurrency` as any run it's compared with
4. Manifest, splits, prompts and the image are fixed within a run series. A change gets a new file, image and config.
5. `--compare` refuses runs that differ in manifest, splits, pricing, main model or settings, image ID, or concurrency.
6. Commit finished summaries to `docs/results/dbt/<run_id>/`. `runs/` stays gitignored.

---

## 8. Implementation checklist

In order. Each step ends with its verification. ⛔ marks a step that needs a resolved blocker. **Every step must leave the existing test suite green.**

**Required**

- [ ] **Step 0: Read the repo (V4, V8, V9).**
  - Read `CLAUDE.md`, `manual_loop/loop_openai.py`, `src/agent/gateway/`, `src/agent/audit/`, `src/agent/evidence/`, `orchestrator/triage.py`, `evals/runner.py`, `Dockerfile`, `compose.yaml` and `pyproject.toml`.
  - Note the current interfaces, the Spider2 clone location and the runs convention in `docs/DECISIONS.md`.
  - Run the full existing test suite and record the command and result as the **regression baseline**.
  - *Verify:* §1.2 matches the repo, with differences noted; the suite passes.
- [ ] **Step 1: Decisions and CLAUDE.md (D9).**
  - Record every §1.1 item in `docs/DECISIONS.md`.
  - Draft the CLAUDE.md section (namespace, gateway-in-image, key rule, DBT protected paths) and **get owner approval** before committing it.
  - *Verify:* every ID has a status; the CLAUDE.md diff is approved and only adds lines.
- [ ] **Step 2: Scaffold `evals/dbt/`.**
  - Create the NEW paths in §2 as stubs, including `evals/dbt/scripts/check_env.py` (implemented) and `tests/dbt/test_import.py` (imports `evals.dbt`, so pytest never exits with code 5).
  - Add `runs/dbt/` to `.gitignore` (V9).
  - *Verify:* `uv run pytest tests/dbt` passes; the full suite still passes; `git status` shows only new paths plus `.gitignore`.
- [ ] **Step 3: Environment (D4, D8).**
  - Check Docker: `docker version` and `docker run --rm hello-world`.
  - `uv add --group dbt-sandbox dbt-core dbt-duckdb gdown`. Add `duckdb` or `sqlglot` only if they aren't dependencies already.
  - `uv sync --all-groups`.
  - Python stays 3.12. Record the versions.
  - *Verify:* `uv run python -m evals.dbt.scripts.check_env` reports:
    - the Docker versions
    - successful imports
    - the key present (never printed)
    - one minimal `responses.create` call succeeding for each of `gpt-6.1-sol` and `gpt-6-luna`
    - `dbt --version` listing the duckdb adapter
- [ ] **Step 4: Benchmark data (V3, V9).**
  - Reuse the existing Spider2 clone if there is one; otherwise `git clone https://github.com/xlang-ai/Spider2.git` to a gitignored path.
  - In `<clone>/spider2-dbt`:
    - `uv run --project <repo root> gdown 1N3f7BSWC4foj-V-1C9n8M2XmgV7FOcqL`
    - `uv run --project <repo root> gdown 1s0USV_iQLo4oe05QqAMnhGGp5jeejCzp`
    - `uv run --project <repo root> python setup.py`
  - Add `evaluate.py`'s third-party imports to `dbt-sandbox`.
  - *Verify:* both JSONL files have 68 lines; each example has one `.duckdb`; the clone commit is recorded; the Lite runner's data is unaffected, and the existing test suite still matches the Step 0 baseline.
- [ ] **Step 5: Gateway DuckDB-file target.**
  - If `src/agent/gateway/` lacks it, add a target satisfying §3, inside the gateway module, with tests in its existing test location.
  - Write `evals/dbt/sandbox_tools/sql.py` as a thin adapter with no `duckdb` import.
  - *Verify:*
    - The existing gateway tests pass.
    - The new tests reject DROP, INSERT, ATTACH, COPY, PRAGMA, SET, `read_csv` and multi-statement input; prove `lock_configuration` blocks `SET enable_external_access=true`; and check the injected LIMIT and that no handle stays open.
    - `git grep -n "import duckdb" -- ':!src/agent/gateway'` finds no first-party code.
- [ ] **Step 6: Sandbox image (content-hash tag).**
  - Pin the base image digest in the Dockerfile.
  - Implement `evals/dbt/image.py` and `scripts/build_image.py`. The script exports `requirements.txt`, builds `spider2-dbt-sandbox:<content_tag()>`, and prints the image ID. **Nothing is written to configs.**
  - *Verify:*
    - Pins match `uv.lock`.
    - In the image: `python -c "import agent.gateway, sandbox_tools"` works with no environment variables.
    - `dbt --version` works in the image.
    - `ls -a /opt /workspace` shows no `.env`, and `docker history --no-trunc` has no key.
    - A rebuild with no changes yields the same tag, and the tree stays clean after building.
- [ ] **Step 7 ⛔ B2: Manifest.**
  - Implement and run `build_manifest` (§5.2), plus `tests/dbt/test_manifest.py`.
  - *Verify:* 64 unique IDs; 4 excluded, each with a reason; all assertions pass; a rerun is byte-identical.
- [ ] **Step 8: Splits** (§5.3).
  - *Verify:* 13 + 51 = 64, disjoint; the manifest hash matches; the file is committed.
- [ ] **Step 9: Evaluator self-test (V1).**
  - Implement `submission.py`, `scoring.py` (scratch copies, §6.3 step 7) and `selftest_evaluator.py`. For every task, the self-test scores:
    - (a) the gold DB as the prediction
    - (b) the unmodified start DB
  - Fix the stdout parser regex to the observed output.
  - *Verify:*
    - (a) passes 64/64, with any exception logged in DECISIONS.
    - (b) Any start-DB pass is flagged as trivially passable.
    - Whether `duckdb_match` opens files read-write is recorded.
    - No `scoring/` dirs remain.
    - Gold hashes are unchanged.
- [ ] **Step 10: Protect graders and eval sets.**
  - Add to `orchestrator/triage.py` `PROTECTED`:
    - `evals/dbt/scoring.py`, `submission.py`, `scripts/selftest_evaluator.py`, `scripts/build_manifest.py`, `scripts/make_splits.py`
    - `evals/dbt/manifests/**`, `docker/dbt-sandbox/**`
    - the Spider2 `spider2-dbt/evaluation_suite/**`
  - Ensure `READABLE_ROOTS` excludes Spider2 gold and `runs/dbt/**/scoring/`.
  - *Verify:* a test (in the triage module's existing test location) shows that triage can't read a gold file or write any protected path, and that existing triage tests pass.
- [ ] **Step 11: Task containers and dbt readiness (D7, V2, V5, V7).**
  - Implement `evals/dbt/sandbox.py` and `check_tasks_dbt`. The check runs `dbt debug` in a fresh container per task, plus a one-shot `deps` container where needed.
  - *Verify:*
    - 64/64 `dbt debug` passes, with the profile location recorded.
    - `deps` tasks are listed and handled.
    - Source hashes are unchanged, with no path-length errors.
    - One `dbt run` is timed (V7).
    - `tests/dbt/test_container.py` (`-m docker`) shows, inside a task container:
      - no network
      - no `OPENAI_API_KEY` in `env`
      - the root filesystem is read-only and `/workspace` is writable
      - no gold path exists
      - the user isn't root
      - no labeled containers remain after a simulated crash
- [ ] **Step 12 (D1): Sandbox tools and host routing.**
  - Implement `sandbox_tools/*`, `evals/dbt/tools.py` and `docker_exec.py`.
  - *Verify:*
    - Host unit tests, with `SANDBOX_WORKSPACE` set to a temp dir: the path guard denies `..`, absolute paths outside the workspace, symlink escapes and `*.duckdb` writes; `edit_file` errors on 0 or >1 matches; `run_dbt` enforces the allowlist.
    - `-m docker` tests: each tool works through a live container; a timeout returns an error; killing the container gives `container_error`; `dispatch` rejects tools outside the role's list; the specs validate as strict.
- [ ] **Step 13: Loop extension and main role (V6).**
  - Add any missing §3 loop parameters to `manual_loop/loop_openai.py` as keyword arguments, defaults unchanged.
  - Implement `agent_adapter.py`.
  - *Verify:*
    - Lite tests are unchanged and green.
    - `baseline.toml`: one task runs end to end with `gpt-6.1-sol`, all tools executing in the container.
    - The first request has exactly the seven tools and no hosted tools.
    - `usage_by_role.main` is filled in.
    - A bad model name yields `agent_error`.
    - `git grep -n "anthropic" -- evals/dbt docker/dbt-sandbox` is empty.
- [ ] **Step 14 (D6): Subagents.**
  - Implement `delegate.py`, both schemas, and role-scoped dispatch.
  - *Verify:*
    - Each delegate returns schema-valid JSON from `gpt-6-luna`.
    - `brief.json` has no main history.
    - Subagent calls to write, dbt or delegate tools are rejected.
    - The 11th delegate call returns `{"error": "budget"}`.
    - A subagent timeout doesn't end the task.
    - `routed.toml`: one task runs end to end and writes `subagents/` folders.
- [ ] **Step 15: Runner and records (D10).**
  - Implement `records.py` (reusing `src/agent/audit`) and `runner.py`, with `concurrency` workers.
  - *Verify:*
    - The smoke run succeeds for both configs at `concurrency = 1`, and again at 2.
    - The §7.1 layout is complete.
    - `--resume` on a finished run is a no-op.
    - Killing mid-task then resuming removes the container, **deletes and re-stages that task-attempt**, and completes.
    - Resuming after an image rebuild is refused.
    - A deliberately wrong prediction fails.
    - Searching `runs/dbt/` for the key finds nothing.
- [ ] **Step 16: Report and compare.**
  - *Verify:*
    - Fixture tests (3 tasks × 3 attempts) check pass@1, pass^3, splits and per-role cost sums.
    - `--compare` renders the comparison.
    - It refuses mismatched manifest, splits, pricing, main model, image ID or concurrency.
- [ ] **Step 17: Readiness review.** Confirm D1–D3, D5–D10 and O1–O2 with the owner, and commit.
  - *Verify:* §9 *Ready*.

**Optional future enhancements.** Each one is measured against the baseline:

- A separate verifier on the main model, in a fresh context. The source puts verification on the frontier model.
- Using the existing `orchestrator/triage.py` to *draft* fix proposals from triaged DBT failures, for owner review only. Never auto-applied.
- Per-role `reasoning_effort` tuning, on `dev` only.
- Gateway lint stage for fan-out joins.
- A dbt-manifest catalog tool.
- Gold-schema-context replay for failure attribution.
- Token and cost enforcement, and prompt caching.
- Concurrency above 4.

---

## 9. Verification and completion criteria

**Ready:**

- [ ] B2 resolved. D1–D10 confirmed or accepted. O1 and O2 answered. CLAUDE.md section approved.
- [ ] The existing test suite matches the Step 0 regression baseline. Lite runner, root `Dockerfile`, `compose.yaml` and `evals/sandbox.py` are unchanged (`git diff master --stat`).
- [ ] No first-party `duckdb` import outside `src/agent/gateway/`. No second loop. No `anthropic` import in DBT code.
- [ ] Image `spider2-dbt-sandbox:<content hash>` built from a pinned base; no key or `.env` inside; building leaves the tree clean.
- [ ] Manifest, splits and checksums committed. Evaluator self-test passes 64/64 on gold. Gold hashes unchanged.
- [ ] Triage protections tested. Container isolation tests pass. `dbt debug` passes 64/64.
- [ ] `uv run pytest` and `uv run pytest -m docker` pass. Smoke runs and resume work for both configs.

**Baseline complete (control):**

- [ ] 64 × 3 `result.json` files, zero `harness_error`, one image ID, recorded concurrency.
- [ ] `summary.md` reports the per-attempt range, pass@1, pass^3, the dev and held-out split, tokens, cost and time, and the failure breakdown.
- [ ] Every failure triaged, with one example per stage. Re-scoring is identical. The summary is committed to `docs/results/dbt/<run_id>/`.

**Routed run complete (target):**

- [ ] The same criteria with `routed.toml`, the same image ID and the same concurrency.
- [ ] `--compare` output committed, reporting pass@1, pass^3, cost and cost per passed task, with "no clear change" applied within the noise range.
