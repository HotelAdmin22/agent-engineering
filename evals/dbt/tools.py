"""Tool specs and role-checked dispatch. The host routes; the container executes.

`dispatch` is the one place a model's function call becomes an action:

  1. Is the function in this role's tool list? If not, refused -- a subagent
     cannot write files or run dbt because those names are not in its list,
     not because its prompt asks it not to.
  2. delegate_* -> evals/dbt/delegate.py (a subagent loop on the host).
  3. everything else -> docker_exec, inside the task container.

It always returns a string. Errors are {"error": ...}. A dead container is
the one thing that raises (ContainerDead), because the task cannot go on.

Every spec is strict: every property is required and additionalProperties is
false, so optional parameters are nullable and sent as null.
"""

import hashlib
import json
from typing import Any

from .docker_exec import ExecResult, run_tool

CONTAINER_TOOLS = {"read_file", "list_files", "search_files", "write_file", "edit_file",
                   "query_duckdb", "run_dbt"}
DELEGATE_TOOLS = {"delegate_explore", "delegate_diagnose"}


def _spec(name: str, description: str, properties: dict) -> dict:
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        },
    }


_STR = {"type": "string"}
_NSTR = {"type": ["string", "null"]}
_NINT = {"type": ["integer", "null"]}

TOOL_SPECS: dict[str, dict] = {
    "read_file": _spec(
        "read_file",
        "Read a text file in /workspace with line numbers. offset is the first "
        "line (1-based), limit the number of lines; null reads from the start / "
        "to the end. Long output is truncated: read further with offset.",
        {"path": _STR, "offset": _NINT, "limit": _NINT},
    ),
    "list_files": _spec(
        "list_files",
        "List files in /workspace matching a glob, e.g. 'models/**/*.sql'. null "
        "lists everything. At most 500 entries.",
        {"pattern": _NSTR},
    ),
    "search_files": _spec(
        "search_files",
        "Search text files for a Python regex. path narrows to a file or "
        "directory (null = whole workspace); glob filters file names, e.g. "
        "'*.yml'. At most 200 matches.",
        {"regex": _STR, "path": _NSTR, "glob": _NSTR},
    ),
    "write_file": _spec(
        "write_file",
        "Create or overwrite a text file in /workspace (e.g. a model .sql or a "
        "schema .yml). The .duckdb database cannot be written; change it by "
        "running dbt.",
        {"path": _STR, "content": _STR},
    ),
    "edit_file": _spec(
        "edit_file",
        "Replace one exact, unique occurrence of old with new in a file. Errors "
        "if old matches zero or several times -- include enough context.",
        {"path": _STR, "old": _STR, "new": _STR},
    ),
    "query_duckdb": _spec(
        "query_duckdb",
        "Run ONE read-only SELECT (or WITH ... SELECT) against the project's "
        "DuckDB database. Returns columns, value types, row_count and the first "
        "rows. Tables in dbt custom schemas can be schema-qualified. DDL, DML, "
        "ATTACH, COPY, PRAGMA, SET and file-reading functions are refused.",
        {"sql": _STR},
    ),
    "run_dbt": _spec(
        "run_dbt",
        "Run dbt in /workspace. command is one of run, build, test, compile, "
        "parse. select is a dbt selector (model names, +graph operators, tags) "
        "or null for the whole project. Returns the exit code and the end of "
        "the log.",
        {"command": {"type": "string", "enum": ["run", "build", "test", "compile", "parse"]},
         "select": _NSTR},
    ),
    "delegate_explore": _spec(
        "delegate_explore",
        "Ask a fast read-only helper to explore the project and database and "
        "answer one specific question (e.g. which source tables and columns a "
        "model needs, what an existing macro does). It starts fresh: put "
        "everything it needs in the question. focus_paths are files or "
        "directories to start from (may be empty). Returns a structured answer.",
        {"question": _STR, "focus_paths": {"type": "array", "items": _STR}},
    ),
    "delegate_diagnose": _spec(
        "delegate_diagnose",
        "Ask a fast read-only helper to diagnose a dbt or SQL error. Paste the "
        "error text verbatim; focus_paths are the files most likely involved. "
        "It reads files and queries the database but changes nothing. Returns "
        "the likely cause and a suggested change.",
        {"error_text": _STR, "focus_paths": {"type": "array", "items": _STR}},
    ),
}


def specs_for(tool_names: list[str]) -> list[dict]:
    unknown = [t for t in tool_names if t not in TOOL_SPECS]
    if unknown:
        raise ValueError(f"unknown tools in config: {unknown}")
    return [TOOL_SPECS[t] for t in tool_names]


def specs_hash(tool_names: list[str] | None = None) -> str:
    specs = TOOL_SPECS if tool_names is None else {t: TOOL_SPECS[t] for t in tool_names}
    return hashlib.sha256(json.dumps(specs, sort_keys=True).encode()).hexdigest()


def limits_for(cfg: dict) -> dict:
    """What travels to toolhost with every call."""
    return {
        "tool_output_max_chars": cfg["limits"]["tool_output_max_chars"],
        "max_rows_returned": cfg["gateway"]["max_rows_returned"],
        "query_timeout_s": cfg["gateway"]["query_timeout_s"],
        "dbt_allowed_commands": list(cfg["dbt"]["allowed_commands"]),
        "dbt_tool_timeout_s": cfg["dbt"]["tool_timeout_s"],
        "final_run_timeout_s": cfg["dbt"]["final_run_timeout_s"],
    }


def call_timeout(cfg: dict, name: str) -> float:
    if name == "run_dbt":
        return float(cfg["dbt"]["tool_timeout_s"]) + 15
    if name == "query_duckdb":
        return float(cfg["gateway"]["query_timeout_s"]) + 30
    return 60.0


def dispatch(ctx: Any, role: str, allowed: list[str], name: str, args: dict,
             turn: int = 0, call_id: str = "") -> tuple[str, bool]:
    """(content, is_error) for the loop. Logs one tool_calls.jsonl line."""
    if name not in allowed:
        payload, status, extra = {"error": f"tool {name!r} is not available to the "
                                           f"{role} role"}, "rejected", {}
        exec_info = None
    elif name in DELEGATE_TOOLS:
        payload, status, extra = ctx.delegate(name, args, turn=turn, call_id=call_id)
        exec_info = None
    elif name in CONTAINER_TOOLS:
        exec_info: ExecResult = run_tool(ctx.container, name, args, ctx.limits,
                                         call_timeout(ctx.cfg, name))
        payload, status, extra = exec_info.payload, exec_info.status, {}
        if payload.get("rejected"):
            status = "rejected"
    else:
        payload, status, extra, exec_info = {"error": f"unknown tool: {name}"}, "rejected", {}, None

    ctx.count_tool(role, name, status, payload)
    record = {
        "role": role, "turn": turn, "call_id": call_id, "function": name,
        "arguments": args, "status": status,
        "exit_code": exec_info.exit_code if exec_info else None,
        "duration_ms": exec_info.duration_ms if exec_info else extra.get("duration_ms"),
    }
    if name == "query_duckdb":
        record["sql_hash"] = payload.get("sql_hash")
        record["row_count"] = payload.get("row_count")
    if name == "run_dbt":
        record["dbt_exit_code"] = payload.get("exit_code")
    if "subagent_dir" in extra:
        record["subagent_dir"] = extra["subagent_dir"]
    if "error" in payload:
        record["error"] = payload["error"]
    ctx.log_tool_call(role, record)

    text = json.dumps(payload, default=str)
    return text, "error" in payload
