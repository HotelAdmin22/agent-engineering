# Tools

**Reserved — Phase 4.**

Every tool gets an entry here *before* it is implemented. A tool that cannot be
justified in writing should not exist.

Each entry should record:

- **Why it exists** — the workflow step that is blocked without it
- **Inputs** — parameters and their types
- **Outputs** — what comes back, including the error shape
- **Permissions** — what it can reach, and what it structurally cannot
- **Workflow step** — which of Orient / Plan / Act / Verify / Recover / Finish needs it

On permissions: prefer restricting capability structurally over validating
inputs. A read-only connection cannot write, whatever the agent sends it; a
validator is a guess about what the agent might try.

The tool catalogue below is intentionally unpopulated until Phase 4.

---

## Restricting the tool surface

*Recorded 2026-09-15, Phase 0. This section documents a mechanism, not a tool
design — it belongs here because every future entry's **Permissions** line
depends on getting it right.*

`ClaudeAgentOptions` has two options that look interchangeable and are not.
Only one of them restricts anything.

| Option | What it does | Restriction? |
| --- | --- | --- |
| `tools=` | The entire tool surface offered to the model. Tools absent from it are never presented. Defaults to `None`, meaning **every built-in tool**. | **Yes** — structural |
| `allowed_tools=` | Auto-approves tools so they run without a permission prompt. | No — it only removes friction |

From the SDK's own docstring on `allowed_tools`:

> Tool names that are auto-allowed without prompting for permission.
> **To restrict which tools are available at all, use `tools`.**

Set `allowed_tools` alone and every other built-in stays reachable. The model
still sees them, still calls them, and is stopped by an approval gate — a
validator, and one with nobody to answer it in a non-interactive run.

Set both. `tools=` defines what exists; `allowed_tools=` stops the survivors
prompting.

```python
options = ClaudeAgentOptions(
    tools=READ_ONLY_TOOLS,          # what exists
    allowed_tools=READ_ONLY_TOOLS,  # what runs unprompted
)
```

### What it cost to get this wrong

Identical prompt (query a DuckDB file — a task the read-only agent cannot do),
run once with `allowed_tools` alone and once with `tools=` also set:

| | `allowed_tools` only | Both set |
| --- | --- | --- |
| Tools attempted | Bash, Write, ToolSearch, PowerShell | none outside the list |
| Turns | 46 | **1** |
| Duration | 212 s | **9.9 s** |
| Cost | $0.4828 | **$0.0305** |
| Behaviour | probed for a way around the block | stated its limits and stopped |

In the first run the agent tried to write a file, and tried aliasing a Python
binary to get around the command-name check. Nothing was damaged — the gate
held — but it held by refusing requests, not by making them unthinkable.

In the second the agent opened with *"my available tools are limited to Glob,
Grep, Read, WebFetch, and WebSearch"* and stopped. **16× cheaper on an identical
prompt**, purely from telling the model the truth about its capabilities.

Two lessons worth carrying into Phase 4:

1. A capability the model can *attempt* is not restricted, however reliably the
   attempt is refused. Restriction means the option never appears.
2. An honest tool surface is a cost optimisation, not just a safety measure. A
   model that must discover its limits by trial pays for every probe.

### Read-only is not the same as contained

The current surface is `Read`, `Glob`, `Grep`, `NotebookRead`, `WebSearch`,
`WebFetch`. All six are read-only in that none can modify local state.

`WebSearch` and `WebFetch` are different in kind from the other four: they send
data outward. An agent holding them can transmit anything in its context to an
external service. The other four are confined to the local filesystem.

So **Permissions** entries below should answer two questions, not one:

- What can this tool *change*?
- What can this tool *reach*, inbound and outbound?

---

## Tool catalogue

Intentionally unpopulated. Phase 4.
