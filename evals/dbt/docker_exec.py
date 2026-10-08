"""Run one tool call inside a task container. The host never executes it.

    docker exec -i <ctr> timeout <s> python -m sandbox_tools.toolhost <tool>

Arguments go in as JSON on stdin and one JSON object comes back on stdout. No
`-e`, no `--env-file`: the exec inherits only the image's own environment.

Three ways a call can fail, kept distinct because they mean different things:

  error            the tool ran and refused or failed -> the model sees it
  timeout          the call overran -> the model sees it
  container_error  the container is gone -> the task cannot continue; raised
                   as ContainerDead so the adapter ends the task
"""

import json
import os
import subprocess
import time
from dataclasses import dataclass

# Host-side backstop beyond the in-container `timeout`, for a wedged daemon.
HOST_GRACE_S = 30


class ContainerDead(Exception):
    def __init__(self, container: str, oom_killed: bool, detail: str = ""):
        super().__init__(f"container {container} is not running ({detail})")
        self.container = container
        self.oom_killed = oom_killed


@dataclass
class ExecResult:
    payload: dict
    status: str            # ok | error | timeout | container_error
    exit_code: int | None
    duration_ms: int


def docker_env() -> dict:
    """The docker CLI's environment, minus the key. Belt and braces: `docker
    exec` does not forward host variables, but nothing needs to see it."""
    env = dict(os.environ)
    env.pop("OPENAI_API_KEY", None)
    return env


def docker(*args: str, input: str | None = None, timeout: float | None = None,
           check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args], input=input, capture_output=True, text=True,
        timeout=timeout, check=check, env=docker_env(), encoding="utf-8", errors="replace",
    )


def container_state(container: str) -> dict:
    proc = docker("inspect", container, "--format", "{{json .State}}", timeout=30)
    if proc.returncode != 0:
        return {}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}


def run_tool(container: str, tool: str, args: dict, limits: dict,
             timeout_s: float) -> ExecResult:
    request = json.dumps({"args": args, "limits": limits})
    started = time.monotonic()
    inner = max(int(timeout_s), 1)
    cmd = ["exec", "-i", container, "timeout", "--signal=KILL", str(inner),
           "python", "-m", "sandbox_tools.toolhost", tool]
    try:
        proc = docker(*cmd, input=request, timeout=inner + HOST_GRACE_S)
    except subprocess.TimeoutExpired:
        return ExecResult({"error": f"{tool} timed out after {inner}s"}, "timeout", None,
                          _ms(started))
    duration = _ms(started)

    if proc.returncode != 0:
        state = container_state(container)
        if not state.get("Running"):
            raise ContainerDead(container, bool(state.get("OOMKilled")),
                                f"exit {proc.returncode}: {proc.stderr.strip()[:200]}")
        if proc.returncode in (124, 137):
            return ExecResult({"error": f"{tool} timed out after {inner}s"}, "timeout",
                              proc.returncode, duration)
        return ExecResult({"error": f"{tool} failed (exit {proc.returncode}): "
                                    f"{proc.stderr.strip()[-500:]}"},
                          "error", proc.returncode, duration)

    try:
        payload = json.loads(proc.stdout)
        if not isinstance(payload, dict):
            raise ValueError("not an object")
    except (json.JSONDecodeError, ValueError):
        return ExecResult({"error": f"{tool} returned unparseable output"}, "error",
                          proc.returncode, duration)

    status = "error" if "error" in payload else "ok"
    return ExecResult(payload, status, proc.returncode, duration)


def _ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)
