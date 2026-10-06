"""The same hand-written loop, against Luna's Responses API.

`loop.py` talks to Anthropic's Messages API; this talks to OpenAI's Responses
API. They return the identical `Result`, so `evals/runner.py` and the
orchestrator do not know or care which one ran.

Why a second file rather than a provider flag inside the first: the two APIs
disagree about almost everything that matters here -- content blocks vs output
items, `stop_reason` vs the absence of one, nested vs flat tool schemas. A
single function with branches on every line teaches the shape of neither.

The caller still supplies `tools` in Anthropic's shape, because the sandbox
already speaks it. The conversion happens here.
"""

from typing import Any, Callable

from openai import OpenAI

from .loop import MAX_ITERATIONS, Result, Turn

MODEL = "gpt-5.6-luna"

# A bare create() call loses a whole run to one transient 429. The SDK already
# does exponential backoff with jitter, so configuring it beats hand-rolling a
# retry loop that gets the jitter wrong.
REQUEST_TIMEOUT = 120.0     # seconds per request, not per run
MAX_RETRIES = 5


def to_openai_tools(tools: list[dict]) -> list[dict]:
    """Anthropic tool shape -> the Responses API's flat function shape."""
    return [
        {
            "type": "function",
            "name": t["name"],
            "description": t["description"],
            "parameters": t["input_schema"],
        }
        for t in tools
    ]


def run_loop(
    prompt: str,
    tools: list[dict],
    execute: Callable[[str, dict], tuple[str, bool]],
    system: str | None = None,
    max_iterations: int = MAX_ITERATIONS,
    client: Any = None,
    trace: Callable[[dict], None] | None = None,
    max_total_tokens: int | None = None,
) -> Result:
    """Run until the model stops calling tools. Signature matches loop.run_loop."""
    import json

    emit = trace or (lambda event: None)
    client = client or OpenAI(timeout=REQUEST_TIMEOUT, max_retries=MAX_RETRIES)
    spent = 0
    items: list[Any] = [{"role": "user", "content": prompt}]
    turns: list[Turn] = []

    for index in range(1, max_iterations + 1):
        kwargs: dict[str, Any] = {
            "model": MODEL,
            "input": items,
            "tools": to_openai_tools(tools),
        }
        if system:
            kwargs["instructions"] = system

        response = client.responses.create(**kwargs)

        calls = [i for i in response.output if i.type == "function_call"]
        turns.append(
            Turn(
                index=index,
                # The Responses API has no stop_reason; `status` is the nearest
                # equivalent, and the presence of calls is what drives the loop.
                stop_reason=response.status,
                text=response.output_text or "",
                tool_calls=[{"name": c.name, "input": c.arguments} for c in calls],
                usage=response.usage.model_dump() if response.usage else {},
            )
        )

        emit({"kind": "text", "turn": index, "text": response.output_text or ""})
        for c in calls:
            emit({"kind": "tool_call", "turn": index, "name": c.name, "input": c.arguments})

        if response.status == "incomplete":
            reason = getattr(response.incomplete_details, "reason", "incomplete")
            emit({"kind": "stop", "turn": index, "reason": reason})
            return Result(response.output_text or None, turns, reason, items)

        # Carry the model's own output forward verbatim, calls included.
        items += response.output

        if not calls:
            emit({"kind": "stop", "turn": index, "reason": "end_turn"})
            return Result(response.output_text, turns, "end_turn", items)

        # A step budget alone does not bound cost: one turn can read a very
        # large result. Check tokens too, and stop between turns rather than
        # mid-tool, so the transcript stays coherent.
        spent += (response.usage.total_tokens or 0) if response.usage else 0
        if max_total_tokens is not None and spent >= max_total_tokens:
            emit({"kind": "stop", "turn": index, "reason": "token_budget"})
            return Result(response.output_text or None, turns, "token_budget", items)

        for call in calls:
            try:
                tool_input = json.loads(call.arguments)
            except json.JSONDecodeError as exc:
                content, is_error = f"invalid tool arguments: {exc}", True
            else:
                content, is_error = execute(call.name, tool_input)

            emit({"kind": "tool_result", "turn": index, "content": content, "is_error": is_error})
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": call.call_id,
                    # The Responses API has no is_error flag; the error has to
                    # be legible in the text or the model cannot recover.
                    "output": f"ERROR: {content}" if is_error else content,
                }
            )

    emit({"kind": "stop", "reason": "max_iterations"})
    return Result(None, turns, "max_iterations", items)
