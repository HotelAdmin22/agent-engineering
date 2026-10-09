"""The project's own model specification, condensed for the task prompt.

Experiment 2 told the agent to go and read `models/*.yml` itself. The
hypothesis was never actually tested: reading them cost turns (max_turns 5 ->
29, mean input 280k -> 479k tokens) and the run regressed on budget before it
could say anything about accuracy.

This delivers the same information for free. The runner reads the staged
workspace on the host and splices a condensed form into the task instruction,
so the agent starts holding the spec instead of spending its budget
discovering it.

Two rules, both load bearing:

  1. **The source is the task's own workspace, never the eval spec.**
     `spider2_eval.jsonl` names the tables that get scored -- it is the answer
     key. Building a prompt from `condition_tabs` would hand the agent the
     marking scheme. Everything here comes from `models/**/*.yml`, which the
     agent could already open with `read_file`.
  2. **`dbt_packages/` is excluded.** Those are vendored upstream models, they
     are the bulk of the bytes, and they are not what the task asks anyone to
     build.

Column *descriptions* are dropped and only column *names* kept. The grain --
"one row per customer with purchases" -- lives in the model description, which
is what the failures actually turn on; the per-column prose is what makes the
file 152 KB.
"""

from pathlib import Path

MAX_CHARS = 6000          # whole block; ~1.5k tokens
MAX_DESCRIPTION = 240     # per model
MAX_COLUMNS = 40          # per model


def _flatten(text: str) -> str:
    return " ".join(str(text).split())


def _models_from(doc) -> list[dict]:
    if not isinstance(doc, dict):
        return []
    out = []
    for entry in doc.get("models") or []:
        if isinstance(entry, dict) and entry.get("name"):
            out.append(entry)
    return out


def read_models(workspace: Path) -> list[dict]:
    """Every model declared under models/, in file then declaration order."""
    import yaml

    models_dir = Path(workspace) / "models"
    if not models_dir.is_dir():
        return []
    found: list[dict] = []
    seen: set[str] = set()
    for path in sorted(models_dir.rglob("*.yml")) + sorted(models_dir.rglob("*.yaml")):
        try:
            docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8", errors="replace")))
        except Exception:
            continue  # a malformed yml is the agent's problem, not a crash here
        for doc in docs:
            for model in _models_from(doc):
                name = str(model["name"])
                if name in seen:
                    continue
                seen.add(name)
                columns = [
                    str(c["name"]) for c in (model.get("columns") or [])
                    if isinstance(c, dict) and c.get("name")
                ]
                found.append({
                    "name": name,
                    "description": _flatten(model.get("description") or ""),
                    "columns": columns,
                })
    return found


def render(models: list[dict], max_chars: int = MAX_CHARS) -> str:
    """The block appended to the task instruction. Empty when there is nothing."""
    if not models:
        return ""
    lines = [
        "## The models this project declares",
        "",
        "Read from the `.yml` files already in your workspace, so you do not have",
        "to spend turns opening them.",
        "",
        "**Every model listed here must exist when you finish, with the columns",
        "named.** A model the task sentence never mentions is still required if it",
        "is declared here -- the sentence is a summary, this is the list.",
        "",
        "Where a description states the grain, honour it exactly: check `COUNT(*)`",
        "against `COUNT(DISTINCT <key>)`. Most descriptions do not state it, so do",
        "not read a grain into one that is silent -- decide those from the data.",
        "",
    ]
    body: list[str] = []
    truncated = 0
    budget = max_chars - sum(len(line) + 1 for line in lines)
    for model in models:
        description = model["description"][:MAX_DESCRIPTION]
        columns = model["columns"][:MAX_COLUMNS]
        more = len(model["columns"]) - len(columns)
        entry = f"- **{model['name']}**"
        if description:
            entry += f" -- {description}"
        if columns:
            entry += "\n  columns: " + ", ".join(columns)
            if more > 0:
                entry += f", (+{more} more)"
        if budget - (len(entry) + 1) < 0:
            truncated = len(models) - len(body)
            break
        budget -= len(entry) + 1
        body.append(entry)
    if not body:
        return ""
    if truncated:
        body.append(f"- (+{truncated} further model(s); read their .yml if you need them)")
    return "\n".join(lines + body)


def for_workspace(workspace: Path, max_chars: int = MAX_CHARS) -> str:
    return render(read_models(workspace), max_chars)


def augment(instruction: str, workspace: Path, max_chars: int = MAX_CHARS) -> tuple[str, dict]:
    """(instruction, stats). Returns the instruction unchanged when empty.

    The stats land in the attempt record so the cost of this block is
    measurable rather than assumed -- the last prompt change cost 70% more
    input tokens and nobody noticed until the run finished.
    """
    models = read_models(workspace)
    block = render(models, max_chars)
    stats = {"models_declared": len(models), "spec_chars": len(block)}
    if not block:
        return instruction, stats
    return f"{instruction.rstrip()}\n\n{block}\n", stats
