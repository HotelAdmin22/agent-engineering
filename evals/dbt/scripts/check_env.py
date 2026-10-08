"""Is this machine ready to run the Spider2-DBT harness?

    uv run python -m evals.dbt.scripts.check_env [--no-api]

Reports Docker versions, imports, the key's presence (never its value), dbt
with the duckdb adapter, and one minimal Responses call per configured model:
function calling with a strict tool, store=False with encrypted reasoning
carried across a turn, and strict structured output (V6).
"""

import argparse
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

from evals.dbt.image import ROOT

PING_TOOL = {
    "type": "function", "name": "ping", "strict": True,
    "description": "Reply to a ping. Always call this exactly once.",
    "parameters": {"type": "object", "properties": {"word": {"type": "string"}},
                   "required": ["word"], "additionalProperties": False},
}
SCHEMA = {"type": "json_schema", "name": "pong", "strict": True,
          "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}},
                     "required": ["ok"], "additionalProperties": False}}


def line(ok: bool | None, what: str, detail: str = "") -> bool:
    mark = {True: "ok  ", False: "FAIL", None: "skip"}[ok]
    print(f"[{mark}] {what}{(' -- ' + detail) if detail else ''}")
    return ok is not False


def check_model(client, model: str) -> tuple[bool, str]:
    """Two turns: a strict function call, its output fed back, then a
    strict-schema answer. store=False with reasoning.encrypted_content."""
    common = {"model": model, "store": False, "include": ["reasoning.encrypted_content"]}
    r1 = client.responses.create(
        **common, input=[{"role": "user", "content": "Call ping with word='hi'."}],
        tools=[PING_TOOL], tool_choice="required")
    calls = [i for i in r1.output if i.type == "function_call"]
    if not calls:
        return False, "no function call"
    items = [{"role": "user", "content": "Call ping with word='hi'."}, *r1.output,
             {"type": "function_call_output", "call_id": calls[0].call_id, "output": "pong"}]
    r2 = client.responses.create(**common, input=items, tools=[PING_TOOL],
                                 text={"format": SCHEMA})
    parsed = json.loads(r2.output_text)
    reasoning = any(i.type == "reasoning" for i in r1.output)
    return parsed.get("ok") in (True, False), (
        f"echoed model {r1.model}; reasoning items: {reasoning}; "
        f"structured output: {parsed}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--no-api", action="store_true", help="skip the paid API calls")
    ap.add_argument("--models", default="gpt-6.1-sol,gpt-6-luna")
    args = ap.parse_args()
    load_dotenv(ROOT / ".env")
    ok = True

    proc = subprocess.run(["docker", "version", "--format",
                           "{{.Client.Version}} / {{.Server.Version}}"],
                          capture_output=True, text=True)
    ok &= line(proc.returncode == 0, "docker client / server", proc.stdout.strip()
               or proc.stderr.strip()[:200])

    for mod in ("openai", "duckdb", "sqlglot", "dbt.version", "dbt.adapters.duckdb",
                "agent.gateway", "manual_loop.loop_openai"):
        try:
            importlib.import_module(mod)
            ok &= line(True, f"import {mod}")
        except Exception as exc:  # noqa: BLE001
            ok &= line(False, f"import {mod}", str(exc)[:200])

    proc = subprocess.run([sys.executable, "-m", "dbt.cli.main", "--version"],
                          capture_output=True, text=True)
    out = proc.stdout + proc.stderr
    ok &= line("duckdb" in out, "dbt --version lists the duckdb adapter",
               " ".join(l.strip() for l in out.splitlines() if "installed" in l or "duckdb:" in l))

    key = os.environ.get("OPENAI_API_KEY")
    ok &= line(bool(key), "OPENAI_API_KEY present on the host (value not shown)")

    if args.no_api or not key:
        line(None, "Responses API calls", "skipped")
    else:
        from openai import OpenAI
        client = OpenAI(timeout=120, max_retries=2)
        for model in args.models.split(","):
            try:
                good, detail = check_model(client, model)
                ok &= line(good, f"responses.create {model}", detail)
            except Exception as exc:  # noqa: BLE001
                ok &= line(False, f"responses.create {model}", f"{type(exc).__name__}: {exc}"[:300])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
