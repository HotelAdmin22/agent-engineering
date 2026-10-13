"""Run a task's roles through manual_loop/loop_openai.py. Implements no loop.

One TaskContext per task attempt holds what every role shares: the container,
the deadline, the limits, the OpenAI client, the counters and the logs. `main`
runs through the loop here; delegate tools run subagent roles through the same
loop (evals/dbt/delegate.py) and the same container.

Every request is store=False. Each response's usage is accumulated per role
and per model id the API echoed back, and costed from the config's [pricing].
"""

import collections
import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from manual_loop import loop_openai

from . import delegate as delegate_mod
from . import records
from .config import Role
from .docker_exec import ContainerDead
from .tools import dispatch, limits_for, specs_for, specs_hash

REASONING_INCLUDE = ["reasoning.encrypted_content"]


class _RecordingClient:
    """Wraps the OpenAI client so each request can be summarised in the log
    and bounded by the task deadline, without touching the loop."""

    def __init__(self, client, deadline: float, api_timeout: float):
        self._client = client
        self._deadline = deadline
        self._api_timeout = api_timeout
        self.last_request: dict = {}
        self.responses = self

    def create(self, **kwargs):
        self.last_request = kwargs
        remaining = max(self._deadline - time.monotonic(), 5.0)
        return self._client.responses.create(
            **kwargs, timeout=min(self._api_timeout, remaining))


def _usage_numbers(usage) -> dict:
    if usage is None:
        return {}
    u = usage.model_dump() if hasattr(usage, "model_dump") else dict(usage)
    return {
        "input_tokens": u.get("input_tokens") or 0,
        "cached_input_tokens": (u.get("input_tokens_details") or {}).get("cached_tokens") or 0,
        "output_tokens": u.get("output_tokens") or 0,
        "reasoning_tokens": (u.get("output_tokens_details") or {}).get("reasoning_tokens") or 0,
    }


def _output_items(response) -> list:
    out = []
    for item in response.output or []:
        d = item.model_dump() if hasattr(item, "model_dump") else dict(item)
        d.pop("encrypted_content", None)          # opaque, large, useless in a log
        out.append(d)
    return out


@dataclass
class TaskContext:
    cfg: dict
    roles: dict[str, Role]
    container: str
    attempt_dir: Path
    deadline: float
    client: Any
    run_id: str = ""
    instance_id: str = ""
    include_reasoning: bool = True
    limits: dict = field(default_factory=dict)
    usage_by_role: dict = field(default_factory=dict)
    subagent_calls: dict = field(default_factory=lambda: {"explore": 0, "diagnose": 0,
                                                          "errors": 0, "budget_stops": 0})
    counts: dict = field(default_factory=lambda: collections.Counter())
    tool_counts: dict = field(default_factory=lambda: collections.Counter())
    _log_dirs: dict = field(default_factory=dict)
    _pending_calls: dict = field(default_factory=lambda: collections.defaultdict(collections.deque))
    _turns: dict = field(default_factory=dict)
    _sub_seq: int = 0

    def __post_init__(self):
        self.limits = limits_for(self.cfg)
        self._log_dirs["main"] = self.attempt_dir

    # --- accounting ------------------------------------------------------

    def count_tool(self, role: str, name: str, status: str, payload: dict) -> None:
        self.counts["tool_calls"] += 1
        self.tool_counts[name] += 1
        if status != "ok":
            self.counts["tool_errors"] += 1
        if name == "query_duckdb" and payload.get("rejected"):
            self.counts["gateway_rejections"] += 1
        if name == "run_dbt":
            self.counts["dbt_calls"] += 1

    def add_usage(self, role: str, model_requested: str, response) -> dict:
        nums = _usage_numbers(response.usage)
        entry = self.usage_by_role.setdefault(role, {
            "model": model_requested, "models_returned": [], "calls": 0,
            "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0,
            "reasoning_tokens": 0, "cost_usd": 0.0})
        returned = getattr(response, "model", None)
        if returned and returned not in entry["models_returned"]:
            entry["models_returned"].append(returned)
        entry["calls"] += 1
        for k, v in nums.items():
            entry[k] += v
        price = self.cfg["pricing"][model_requested]
        # D5 originally costed cached input at the full input price, as a
        # deliberate upper bound. That bound turned out to be 4.5x the real
        # figure and was being quoted as the actual cost: the cache hit rate
        # on these runs is 92-94%, because each turn resends a long and almost
        # unchanged prefix. Ten full runs billed $62.29 on the old arithmetic
        # and about $13.65 in reality.
        #
        # cached_input is priced explicitly now. cost_usd is what the run
        # should actually cost; cost_usd_upper keeps the old bound for configs
        # whose pricing table predates this.
        cached = min(entry["cached_input_tokens"], entry["input_tokens"])
        fresh = entry["input_tokens"] - cached
        cached_rate = price.get("cached_input", price["input"])
        entry["cost_usd"] = round(fresh / 1e6 * price["input"]
                                  + cached / 1e6 * cached_rate
                                  + entry["output_tokens"] / 1e6 * price["output"], 6)
        entry["cost_usd_upper"] = round(entry["input_tokens"] / 1e6 * price["input"]
                                        + entry["output_tokens"] / 1e6 * price["output"], 6)
        return nums

    @property
    def total_cost(self) -> float:
        return round(sum(r["cost_usd"] for r in self.usage_by_role.values()), 6)

    # --- logging ---------------------------------------------------------

    def log_tool_call(self, role: str, record: dict) -> None:
        records.append_jsonl(self._log_dirs.get(role, self.attempt_dir) / "tool_calls.jsonl",
                             record)

    def next_subagent_dir(self, role: str) -> Path:
        self._sub_seq += 1
        d = self.attempt_dir / "subagents" / f"{self._sub_seq:02d}-{role}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    # --- delegation ------------------------------------------------------

    def delegate(self, name: str, args: dict, turn: int = 0, call_id: str = ""):
        return delegate_mod.run(self, name, args)


def run_role(ctx: TaskContext, role: Role, prompt: str, log_dir: Path, deadline: float,
             text_format: dict | None = None):
    """One role through the one loop. Returns loop_openai.Result."""
    ctx._log_dirs[role.name] = log_dir
    api = ctx.cfg["api"]
    client = _RecordingClient(ctx.client, deadline, float(api["timeout_s"]))
    pending = ctx._pending_calls[role.name]
    pending.clear()
    spec_hash = specs_hash(role.tools)
    prompt_hash = hashlib.sha256(role.system_prompt.encode()).hexdigest()[:16]

    def on_response(index: int, response) -> None:
        ctx._turns[role.name] = index
        nums = ctx.add_usage(role.name, role.model, response)
        req = client.last_request
        inputs = req.get("input") or []
        outputs_fed = [
            {"call_id": i.get("call_id"), "output": i.get("output"),
             "original_chars": len(i.get("output") or "")}
            for i in inputs[-50:] if isinstance(i, dict) and i.get("type") == "function_call_output"
        ]
        records.append_jsonl(log_dir / "transcript.jsonl", {
            "role": role.name, "turn": index,
            "model_requested": role.model, "model_returned": getattr(response, "model", None),
            "request": {"input_items": len(inputs), "prompt_sha256": prompt_hash,
                        "tool_specs_sha256": spec_hash[:16],
                        "settings": {k: v for k, v in req.items()
                                     if k in ("store", "include", "reasoning", "text")}},
            "response_id": getattr(response, "id", None),
            "status": getattr(response, "status", None),
            "output": _output_items(response),
            "function_outputs_sent": outputs_fed if index > 1 else [],
            "usage": nums,
        }, redact=False)

    def trace(event: dict) -> None:
        if event.get("kind") == "tool_call":
            pending.append((event.get("name"), event.get("call_id")))

    def execute(name: str, tool_input: dict):
        call_id = ""
        if pending and pending[0][0] == name:
            call_id = pending.popleft()[1]
        return dispatch(ctx, role.name, role.tools, name, tool_input,
                        turn=ctx._turns.get(role.name, 0), call_id=call_id)

    settings = role.settings_sent()
    return loop_openai.run_loop(
        prompt=prompt,
        tools=specs_for(role.tools),
        execute=execute,
        system=role.system_prompt,
        max_iterations=role.max_turns,
        client=client,
        trace=trace,
        model=role.model,
        strict_tools=True,
        deadline=deadline,
        text_format=text_format,
        store=settings["store"],
        include=REASONING_INCLUDE if ctx.include_reasoning else None,
        reasoning=settings.get("reasoning"),
        on_response=on_response,
    )


@dataclass
class Outcome:
    status: str                    # completed | max_turns | timeout | agent_error | harness_error
    main_turns: int = 0
    error: str | None = None
    failure_category: str | None = None
    final_text: str | None = None


STOP_TO_STATUS = {"end_turn": "completed", "max_iterations": "max_turns", "timeout": "timeout"}


def run_task(instruction: str, ctx: TaskContext) -> Outcome:
    """The instruction goes to main verbatim. Exceptions become statuses."""
    import openai

    main = ctx.roles["main"]
    try:
        result = run_role(ctx, main, instruction, ctx.attempt_dir, ctx.deadline)
    except ContainerDead as exc:
        if exc.oom_killed:
            return Outcome("agent_error", ctx._turns.get("main", 0), f"OOM: {exc}", "execution")
        return Outcome("harness_error", ctx._turns.get("main", 0), str(exc))
    except openai.APIError as exc:
        return Outcome("agent_error", ctx._turns.get("main", 0),
                       f"{type(exc).__name__}: {exc}"[:1000], "agent_error")
    except Exception as exc:  # noqa: BLE001
        return Outcome("harness_error", ctx._turns.get("main", 0),
                       f"{type(exc).__name__}: {exc}"[:1000])

    status = STOP_TO_STATUS.get(result.stopped_because)
    if status is None:          # incomplete: max_output_tokens, content_filter, ...
        return Outcome("agent_error", result.num_turns,
                       f"response incomplete: {result.stopped_because}", "agent_error",
                       result.final_text)
    category = "budget" if status in ("max_turns", "timeout") else None
    return Outcome(status, result.num_turns, None, category, result.final_text)
