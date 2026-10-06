"""Phase 1 — the hand-written Claude tool-use loop.

The loop, and nothing else. It knows how to talk to the Messages API, execute
whichever tools it is handed, and stop. It knows nothing about Spider, SQLite,
or evaluation -- that belongs to the caller.

Built on the low-level `anthropic` SDK rather than claude-agent-sdk, because a
benchmark needs a harness that is fixed, fully owned, and free of the Claude
Code CLI's ~19k tokens of system prompt and its built-in tools.
"""

import json
from dataclasses import dataclass, field
from typing import Any, Callable

import anthropic

MODEL = "claude-opus-5"
MAX_TOKENS = 8000
MAX_ITERATIONS = 15


@dataclass
class Turn:
    """One request/response cycle, kept for the transcript."""

    index: int
    stop_reason: str | None
    text: str
    tool_calls: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)


@dataclass
class Result:
    """Everything one run produced."""

    final_text: str | None
    turns: list[Turn]
    stopped_because: str
    messages: list[dict]

    @property
    def num_turns(self) -> int:
        return len(self.turns)

    @property
    def usage_totals(self) -> dict:
        totals: dict[str, int] = {}
        for turn in self.turns:
            for key, value in turn.usage.items():
                if isinstance(value, int):
                    totals[key] = totals.get(key, 0) + value
        return totals


def run_loop(
    prompt: str,
    tools: list[dict],
    execute: Callable[[str, dict], tuple[str, bool]],
    system: str | None = None,
    max_iterations: int = MAX_ITERATIONS,
    client: Any = None,
    trace: Callable[[dict], None] | None = None,
) -> Result:
    """Run the agent loop until the model stops calling tools.

    `execute(name, input) -> (content, is_error)` is supplied by the caller, so
    this function never decides what a tool may do. That decision -- and
    therefore the sandbox -- lives entirely with whoever calls this.
    """
    emit = trace or (lambda event: None)
    client = client or anthropic.Anthropic()
    messages: list[dict] = [{"role": "user", "content": prompt}]
    turns: list[Turn] = []

    for index in range(1, max_iterations + 1):
        kwargs: dict[str, Any] = {
            "model": MODEL,
            "max_tokens": MAX_TOKENS,
            "thinking": {"type": "adaptive"},
            "tools": tools,
            "messages": messages,
        }
        if system:
            kwargs["system"] = system

        response = client.messages.create(**kwargs)

        text = "".join(b.text for b in response.content if b.type == "text")
        tool_use = [b for b in response.content if b.type == "tool_use"]
        turns.append(
            Turn(
                index=index,
                stop_reason=response.stop_reason,
                text=text,
                tool_calls=[{"name": b.name, "input": b.input} for b in tool_use],
                usage=response.usage.model_dump() if response.usage else {},
            )
        )

        emit({"kind": "text", "turn": index, "text": text})
        for b in tool_use:
            emit({"kind": "tool_call", "turn": index, "name": b.name, "input": b.input})

        # Every stop_reason is handled. An unhandled one is an infinite loop or
        # a silently truncated answer.
        if response.stop_reason == "end_turn":
            emit({"kind": "stop", "turn": index, "reason": "end_turn"})
            return Result(text, turns, "end_turn", messages)
        if response.stop_reason == "refusal":
            emit({"kind": "stop", "turn": index, "reason": "refusal"})
            return Result(None, turns, "refusal", messages)
        if response.stop_reason == "max_tokens":
            emit({"kind": "stop", "turn": index, "reason": "max_tokens"})
            return Result(text or None, turns, "max_tokens", messages)
        if response.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": response.content})
            continue

        # The whole content list, not just the text: it carries the tool_use
        # blocks and the thinking blocks, and thinking must go back unchanged.
        messages.append({"role": "assistant", "content": response.content})

        # Every result in ONE user message. Splitting them across messages
        # silently trains the model out of making parallel tool calls.
        tool_results = []
        for block in tool_use:
            content, is_error = execute(block.name, block.input)
            emit({"kind": "tool_result", "turn": index, "content": content, "is_error": is_error})
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": content,
                    "is_error": is_error,
                }
            )
        messages.append({"role": "user", "content": tool_results})

    emit({"kind": "stop", "reason": "max_iterations"})
    return Result(None, turns, "max_iterations", messages)


def to_jsonable(result: Result) -> dict:
    """Result -> plain dict, for writing a transcript."""
    return {
        "stopped_because": result.stopped_because,
        "num_turns": result.num_turns,
        "usage_totals": result.usage_totals,
        "final_text": result.final_text,
        "turns": [
            {
                "index": t.index,
                "stop_reason": t.stop_reason,
                "text": t.text,
                "tool_calls": json.loads(json.dumps(t.tool_calls, default=str)),
                "usage": t.usage,
            }
            for t in result.turns
        ],
    }
