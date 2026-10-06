# agent-engineering

A learning repository for understanding agent engineering by progressively
building — and measuring — a Claude-based agent.

The goal is not to end up with a finished product. The goal is to understand
why each piece exists, by adding it only when something is actually blocked
without it, and then checking whether it helped.

## Current Phase

**Phase 6 — Scope Expansion**

A local data agent answers "why did X change?" against a DuckDB warehouse, with
every number traced to evidence and an eval score behind it. See
[`CLAUDE.md`](CLAUDE.md) for the architecture rules,
[`docs/commands.md`](docs/commands.md) for how to run it, and
[`docs/architecture.html`](docs/architecture.html) for the live component map.

```bash
uv run python -m agent "why did revenue fall in September?"
uv run python evals/run_evals.py --k 3
```

Measured baseline (`experiments/results.jsonl`): execution accuracy 0.972,
pass^3 0.917, confidently-wrong rate 0.028.

| Phase | State |
| --- | --- |
| 0 Observe | done — removed; see git history before `d41821a` |
| 1 Agent Loop | done — `manual_loop/`, by hand, twice (Anthropic and OpenAI) |
| 2 Workflow | done — orient, plan, act, verify, recover |
| 3 Prompting | done — environment facts live in the catalog, not the prompt |
| 4 Tools and MCP | tools done; `mcp_server/` still a placeholder |
| 5 Evaluation | done — gold set, seeded anomalies, pass^k |
| 6 Scope Expansion | in progress — orchestrator, triage sub-agent |
| 7 Explain the Design | `docs/architecture.html` is generated from the source |

## Setup

Docker is the primary path. The container is what makes a run reproducible,
and reproducibility is what the later phases measure against.

1. **Install Docker** — [Docker Desktop](https://docs.docker.com/get-started/get-docker/).
   On Windows this requires hardware virtualization enabled in BIOS/UEFI
   (**SVM Mode** on AMD, **VT-x** on Intel). Docker Desktop runs containers
   inside a WSL2 Linux VM and cannot start without it.

2. **Install `uv`** — required outside Docker, useful on the host either way:

   ```bash
   # macOS / Linux
   curl -LsSf https://astral.sh/uv/install.sh | sh
   # Windows
   powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```

3. **Configure authentication.** Copy `.env.example` to `.env` and set
   `ANTHROPIC_API_KEY`, or — running outside Docker only — run `claude login`
   to use a Claude subscription instead. Never commit `.env`.

4. **Build the container:**

   ```bash
   docker compose build
   ```

5. **Run the agent:**

   ```bash
   docker compose run --rm agent "List the files here and tell me what this project is."
   ```

   Or, outside Docker:

   ```bash
   uv run agent "List the files here and tell me what this project is."
   ```

## Project Philosophy

- **Start small and expand.** Build the next thing, not the eventual thing.
- **Understand the agent loop before abstracting it.** Write it by hand once.
- **Add tools only when the workflow requires them.** A tool with no blocked step behind it is a guess.
- **Prefer structural capability restrictions over validators.** A read-only connection cannot write; a validator only hopes.
- **Keep environment knowledge separate from generalized instructions.** What is true of *this* database does not belong in a prompt about how to think.
- **Change one variable at a time during experiments.** Two changes, one result, no conclusion.
- **Evaluate improvements instead of relying on intuition.** "It feels better" is not a measurement.

## Roadmap

| Phase | Focus |
| --- | --- |
| 0 | **Observe the Agent** — run it and watch what it does |
| 1 | **Agent Loop** — implement the tool-use loop by hand |
| 2 | **Workflow Design** — Orient → Plan → Act → Verify → Recover → Finish |
| 3 | **Prompting and Generalization** — separate environment facts from instructions |
| 4 | **Tools and MCP** — expose tools deliberately, one justification at a time |
| 5 | **Evaluation** — dev and held-out task sets, measured experiments |
| 6 | **Scope Expansion** — widen what the agent is trusted to do |
| 7 | **Explain the Design** — be able to defend every piece |

Each phase has a directory waiting for it. Those directories are placeholders,
and their contents say so.
