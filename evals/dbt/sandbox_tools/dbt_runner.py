"""run_dbt and final_dbt_run. Only allowlisted subcommands, inside /workspace.

The model chooses the subcommand from a fixed list and an optional selector
matched against a strict pattern; it never supplies argv. `deps`, `seed
--full-refresh`, `run-operation` and friends are simply not reachable.
"""

import re
import subprocess

from .path_guard import workspace

DEFAULT_ALLOWED = ("run", "build", "test", "compile", "parse")
SELECT_PATTERN = re.compile(r"^[\w.+*:@,/ -]+$")
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def scrub(text: str) -> str:
    return ANSI.sub("", text or "")


def _argv(command: str, select: str | None) -> list[str]:
    argv = ["dbt", "--no-use-colors", command, "--profiles-dir", str(workspace()),
            "--project-dir", str(workspace())]
    if select:
        argv += ["--select", *select.split()]
    return argv


def _run(argv: list[str], timeout_s: float) -> tuple[int | None, str, bool]:
    try:
        proc = subprocess.run(
            argv, cwd=workspace(), capture_output=True, text=True,
            timeout=timeout_s, stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or b"")
        out = out.decode(errors="replace") if isinstance(out, bytes) else out
        return None, scrub(out), True
    return proc.returncode, scrub((proc.stdout or "") + (proc.stderr or "")), False


def run_dbt(command: str, select: str | None = None, timeout_s: float = 300.0,
            allowed=DEFAULT_ALLOWED, max_chars: int = 4000) -> dict:
    command = (command or "").strip()
    if command not in allowed:
        return {"error": f"command {command!r} is not allowed; use one of {list(allowed)}",
                "rejected": True}
    select = (select or "").strip() or None
    # The selector is split on spaces into argv, so a token starting with "-"
    # would be read by dbt as a flag (--vars, --target, --profiles-dir ...).
    if select and (not SELECT_PATTERN.match(select)
                   or any(tok.startswith("-") for tok in select.split())):
        return {"error": "select may contain only model names, paths, tags, "
                         "graph operators (+ @ *) and commas", "rejected": True}

    code, output, timed_out = _run(_argv(command, select), timeout_s)
    tail = output[-max_chars:]
    return {
        "command": command,
        "select": select or "",
        "exit_code": code,
        "timed_out": timed_out,
        "output_tail": tail,
        "output_truncated": len(output) > len(tail),
    }


def final_dbt_run(timeout_s: float = 600.0) -> dict:
    """The harness's own `dbt run` after the agent stops. Full log returned."""
    code, output, timed_out = _run(_argv("run", None), timeout_s)
    return {"exit_code": code, "timed_out": timed_out, "log": output}


def dbt_debug(timeout_s: float = 120.0) -> dict:
    """Readiness check (scripts/check_tasks_dbt). Not an agent tool."""
    code, output, timed_out = _run(_argv("debug", None), timeout_s)
    return {"exit_code": code, "timed_out": timed_out, "log": output}
