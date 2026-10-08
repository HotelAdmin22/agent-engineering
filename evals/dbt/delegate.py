"""delegate_explore / delegate_diagnose: a subagent role, run through the same
loop, in the same container, with a narrower tool list and a fresh context.

The rules, each enforced here rather than requested in a prompt:

  1. Fresh context. The subagent receives its role prompt and a brief (goal,
     inputs, output schema, done condition, budget). Main's history is never
     passed -- the brief is built only from the delegate call's arguments.
  2. Narrower tools. The role's tool list in the config has no write, edit,
     dbt or delegate tool, and dispatch() refuses anything not in it.
  3. Shared budget. Calls per task are capped (max_subagent_calls); each call
     is bounded by the role's max_turns, by subagent_timeout_s, and by the
     task deadline. A cap returns {"error": "budget"} to main.
  4. Small, validated results. Strict structured output, validated again
     here, truncated to result_max_chars. Anything else is {"error": ...}.
  5. Discarded but logged under subagents/<seq>-<role>/.

A subagent failure never ends the task: it becomes an error main can read.
Only a dead container propagates (ContainerDead), because nothing can go on.
"""

import json
import time

from . import records
from .docker_exec import ContainerDead

ROLE_FOR_TOOL = {"delegate_explore": "explore", "delegate_diagnose": "diagnose"}

_S = {"type": "string"}
_SA = {"type": "array", "items": {"type": "string"}}

SCHEMAS = {
    "explore_result": {
        "type": "object",
        "properties": {
            "answer": _S,
            "relevant_files": _SA,
            "relevant_tables": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"table": _S, "columns": _SA},
                    "required": ["table", "columns"],
                    "additionalProperties": False,
                },
            },
            "evidence": _SA,
            "open_questions": _SA,
        },
        "required": ["answer", "relevant_files", "relevant_tables", "evidence",
                     "open_questions"],
        "additionalProperties": False,
    },
    "diagnose_result": {
        "type": "object",
        "properties": {
            "likely_cause": _S,
            "file": _S,
            "location_hint": _S,
            "suggested_change": _S,
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        },
        "required": ["likely_cause", "file", "location_hint", "suggested_change",
                     "confidence"],
        "additionalProperties": False,
    },
}

DONE = {
    "explore": "Done when you can answer the question from files you read and queries "
               "you ran. If you cannot fully answer within budget, return what you found "
               "and list what is unresolved in open_questions.",
    "diagnose": "Done when you can name the most likely cause and a concrete change. If "
                "unsure, say so through confidence rather than guessing.",
}


def text_format(schema_name: str) -> dict:
    return {"type": "json_schema", "name": schema_name, "schema": SCHEMAS[schema_name],
            "strict": True}


# --- validation ----------------------------------------------------------

def validate(value, schema: dict, path: str = "$") -> list[str]:
    """Enough of JSON Schema for the two result schemas above."""
    errors = []
    t = schema.get("type")
    if t == "object":
        if not isinstance(value, dict):
            return [f"{path}: expected object"]
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}.{key}: missing")
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in schema.get("properties", {}):
                    errors.append(f"{path}.{key}: unexpected")
        for key, sub in schema.get("properties", {}).items():
            if key in value:
                errors += validate(value[key], sub, f"{path}.{key}")
    elif t == "array":
        if not isinstance(value, list):
            return [f"{path}: expected array"]
        for i, item in enumerate(value):
            errors += validate(item, schema.get("items", {}), f"{path}[{i}]")
    elif t == "string":
        if not isinstance(value, str):
            return [f"{path}: expected string"]
        if "enum" in schema and value not in schema["enum"]:
            errors.append(f"{path}: not one of {schema['enum']}")
    return errors


def shrink(result: dict, max_chars: int) -> tuple[dict, bool]:
    """Truncate the longest strings / lists until the JSON fits."""
    out = json.loads(json.dumps(result))
    shrunk = False
    for _ in range(200):
        if len(json.dumps(out)) <= max_chars:
            break
        shrunk = True
        key = max(out, key=lambda k: len(json.dumps(out[k])))
        v = out[key]
        if isinstance(v, str):
            out[key] = v[: max(len(v) * 2 // 3, 0)]
        elif isinstance(v, list) and v:
            out[key] = v[: len(v) - 1] if len(v) > 1 else [
                (x[: len(x) // 2] if isinstance(x, str) else x) for x in v]
        else:
            break
    return out, shrunk


# --- the call ------------------------------------------------------------

def brief_for(role: str, tool: str, args: dict, max_turns: int, timeout_s: float,
              schema_name: str) -> dict:
    """Built from the delegate call's own arguments, and nothing else."""
    if tool == "delegate_explore":
        goal = "Answer this question about the dbt project and its DuckDB database."
        inputs = {"question": args.get("question") or "",
                  "focus_paths": list(args.get("focus_paths") or [])}
    else:
        goal = "Diagnose this dbt or SQL error and propose a concrete change."
        inputs = {"error_text": args.get("error_text") or "",
                  "focus_paths": list(args.get("focus_paths") or [])}
    return {
        "goal": goal,
        "inputs": inputs,
        "output_schema": schema_name,
        "done_condition": DONE[role],
        "budget": {"max_turns": max_turns, "timeout_s": timeout_s},
    }


def run(ctx, tool: str, args: dict):
    """(payload, status, extra) for dispatch."""
    from .agent_adapter import run_role   # circular at import time otherwise
    import openai

    started = time.monotonic()
    role_name = ROLE_FOR_TOOL[tool]
    delegation = ctx.cfg["delegation"]
    total_calls = ctx.subagent_calls["explore"] + ctx.subagent_calls["diagnose"]

    if total_calls >= int(delegation["max_subagent_calls"]):
        ctx.subagent_calls["budget_stops"] += 1
        return ({"error": "budget", "detail": f"max_subagent_calls "
                 f"({delegation['max_subagent_calls']}) reached for this task"},
                "budget", {"duration_ms": 0})
    remaining = ctx.deadline - time.monotonic()
    if remaining <= 5:
        ctx.subagent_calls["budget_stops"] += 1
        return {"error": "budget", "detail": "task time is spent"}, "budget", {"duration_ms": 0}

    role = ctx.roles[role_name]
    ctx.subagent_calls[role_name] += 1
    sub_timeout = float(delegation.get("subagent_timeout_s", 300))
    deadline = min(ctx.deadline, time.monotonic() + sub_timeout)
    schema_name = role.output_schema or f"{role_name}_result"

    d = ctx.next_subagent_dir(role_name)
    brief = brief_for(role_name, tool, args, role.max_turns, sub_timeout, schema_name)
    records.write_json_atomic(d / "brief.json", brief)
    rel_dir = d.relative_to(ctx.attempt_dir).as_posix()

    def finish(payload: dict, status: str) -> tuple:
        if status != "ok":
            if status == "budget":
                ctx.subagent_calls["budget_stops"] += 1
            else:
                ctx.subagent_calls["errors"] += 1
        records.write_json_atomic(d / "result.json", {"status": status, "payload": payload,
                                                      "duration_s": round(time.monotonic() - started, 2)})
        return payload, status, {"subagent_dir": rel_dir,
                                 "duration_ms": round((time.monotonic() - started) * 1000)}

    try:
        result = run_role(ctx, role, json.dumps(brief, indent=2), d, deadline,
                          text_format=text_format(schema_name))
    except ContainerDead:
        raise
    except openai.APIError as exc:
        return finish({"error": f"{role_name} subagent API error: {type(exc).__name__}: "
                                f"{str(exc)[:300]}"}, "error")
    except Exception as exc:  # noqa: BLE001 -- never end the task from here
        return finish({"error": f"{role_name} subagent failed: {type(exc).__name__}: "
                                f"{str(exc)[:300]}"}, "error")

    if result.stopped_because in ("max_iterations", "timeout"):
        return finish({"error": "budget", "detail": f"{role_name} subagent stopped: "
                                                    f"{result.stopped_because}"}, "budget")
    if result.stopped_because != "end_turn" or not result.final_text:
        return finish({"error": f"{role_name} subagent returned no result "
                                f"({result.stopped_because})"}, "error")
    try:
        parsed = json.loads(result.final_text)
    except json.JSONDecodeError:
        return finish({"error": f"{role_name} subagent returned invalid JSON"}, "error")
    problems = validate(parsed, SCHEMAS[schema_name])
    if problems:
        return finish({"error": f"{role_name} subagent result failed validation: "
                                f"{problems[:5]}"}, "error")
    payload, shrunk = shrink(parsed, int(delegation["result_max_chars"]))
    if shrunk:
        payload["_truncated"] = True
    return finish(payload, "ok")
