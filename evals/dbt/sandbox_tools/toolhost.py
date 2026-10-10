"""`python -m sandbox_tools.toolhost <tool>`: one JSON request in, one JSON out.

The host runs this through `docker exec -i`. The request arrives on stdin as

    {"args": {...}, "limits": {...}}

and exactly one JSON object is printed to stdout. Limits travel with the
request because the container is started with no environment: nothing about
the run's configuration is baked into the image or passed as variables.

Exit code is 0 whenever a JSON result was printed, including tool errors. A
non-zero exit means the host could not trust stdout and reports an error.
"""

import json
import sys
import traceback

from . import dbt_runner, file_tools


def _sql(args, limits):
    # Imported lazily: the gateway pulls in duckdb, which file tools never need.
    from . import sql
    return sql.query_duckdb(
        args.get("sql") or "",
        max_rows=int(limits.get("max_rows_returned", 20)),
        timeout_s=float(limits.get("query_timeout_s", 30)),
    )


def _truncate(payload: dict, max_chars: int) -> dict:
    """Bound the serialised size without cutting JSON mid-token."""
    text = json.dumps(payload, default=str)
    if len(text) <= max_chars:
        return payload
    out = dict(payload)
    for key in ("content", "output_tail", "rows", "matches", "files"):
        if key in out and isinstance(out[key], (str, list)):
            while len(json.dumps(out, default=str)) > max_chars and len(out[key]) > 0:
                if isinstance(out[key], str):
                    out[key] = out[key][: max(len(out[key]) - 500, 0)]
                else:
                    out[key] = out[key][: max(len(out[key]) // 2, 0)]
            out["truncated"] = True
    return out


def handle(tool: str, request: dict) -> dict:
    args = request.get("args") or {}
    limits = request.get("limits") or {}
    max_chars = int(limits.get("tool_output_max_chars", 4000))

    if tool == "read_file":
        result = file_tools.read_file(args.get("path") or "", args.get("offset"),
                                      args.get("limit"), max_chars=max_chars)
    elif tool == "list_files":
        result = file_tools.list_files(args.get("pattern"))
    elif tool == "search_files":
        result = file_tools.search_files(args.get("regex") or "", args.get("path"),
                                         args.get("glob"))
    elif tool == "write_file":
        result = file_tools.write_file(args.get("path") or "", args.get("content") or "")
    elif tool == "edit_file":
        result = file_tools.edit_file(args.get("path") or "", args.get("old") or "",
                                      args.get("new") if args.get("new") is not None else "")
    elif tool == "query_duckdb":
        result = _sql(args, limits)
    elif tool == "run_dbt":
        result = dbt_runner.run_dbt(
            args.get("command") or "", args.get("select"),
            timeout_s=float(limits.get("dbt_tool_timeout_s", 300)),
            allowed=tuple(limits.get("dbt_allowed_commands") or dbt_runner.DEFAULT_ALLOWED),
            max_chars=max_chars,
        )
        return result                      # already tail-bounded
    elif tool == "final_dbt_run":
        return dbt_runner.final_dbt_run(float(limits.get("final_run_timeout_s", 600)))
    elif tool == "final_dbt_test":
        return dbt_runner.final_dbt_test(float(limits.get("final_run_timeout_s", 600)))
    elif tool == "dbt_debug":
        return dbt_runner.dbt_debug(float(limits.get("timeout_s", 120)))
    else:
        return {"error": f"unknown tool: {tool}"}
    return _truncate(result, max_chars)


def main() -> int:
    tool = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        raw = sys.stdin.read()
        request = json.loads(raw) if raw.strip() else {}
        result = handle(tool, request)
    except Exception as exc:  # noqa: BLE001 -- a tool bug becomes a tool error
        result = {"error": f"{type(exc).__name__}: {exc}",
                  "traceback": traceback.format_exc(limit=3)[-1500:]}
    sys.stdout.write(json.dumps(result, default=str))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
