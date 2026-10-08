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

import time
from typing import Any, Callable

from openai import OpenAI

from .loop import MAX_ITERATIONS, Result, Turn

MODEL = "gpt-5.6-luna"

# A bare create() call loses a whole run to one transient 429. The SDK already
# does exponential backoff with jitter, so configuring it beats hand-rolling a
# retry loop that gets the jitter wrong.
REQUEST_TIMEOUT = 120.0     # seconds per request, not per run
MAX_RETRIES = 5


def to_openai_tools(tools: list[dict], strict: bool = False) -> list[dict]:
    """Anthropic tool shape -> the Responses API's flat function shape.

    `strict` asks the API to enforce the schema on the arguments it generates.
    It requires every property to be listed in `required` and
    additionalProperties=false, so it is opt-in: the existing tool schemas were
    not written for it.
    """
    out = []
    for t in tools:
        spec = {
            "type": "function",
            "name": t["name"],
            "description": t["description"],
            "parameters": t["input_schema"],
        }
        if strict:
            spec["strict"] = True
        out.append(spec)
    return out


def run_loop(
    prompt: str,
    tools: list[dict],
    execute: Callable[[str, dict], tuple[str, bool]],
    system: str | None = None,
    max_iterations: int = MAX_ITERATIONS,
    client: Any = None,
    trace: Callable[[dict], None] | None = None,
    max_total_tokens: int | None = None,
    *,
    model: str | None = None,
    strict_tools: bool = False,
    deadline: float | None = None,
    text_format: dict | None = None,
    store: bool | None = None,
    include: list[str] | None = None,
    reasoning: dict | None = None,
    on_response: Callable[[int, Any], None] | None = None,
) -> Result:
    """Run until the model stops calling tools. Signature matches loop.run_loop.

    The keyword-only arguments exist for callers that run several roles through
    this one loop (evals/dbt). Every default reproduces the original behaviour:

      model        -- per call, instead of the module-level MODEL
      strict_tools -- send tools with strict=True
      deadline     -- a time.monotonic() value; checked before every request
                      and after every tool call, stops with "timeout"
      text_format  -- the Responses API `text.format`, e.g. a strict json_schema
      store, include, reasoning -- passed through only when set
      on_response  -- called with (turn index, raw response) for every response,
                      so a caller can log usage and the model id the API echoed
    """
    import json

    emit = trace or (lambda event: None)
    client = client or OpenAI(timeout=REQUEST_TIMEOUT, max_retries=MAX_RETRIES)
    spent = 0
    items: list[Any] = [{"role": "user", "content": prompt}]
    turns: list[Turn] = []

    def out_of_time() -> bool:
        return deadline is not None and time.monotonic() >= deadline

    for index in range(1, max_iterations + 1):
        if out_of_time():
            emit({"kind": "stop", "turn": index, "reason": "timeout"})
            return Result(None, turns, "timeout", items)

        kwargs: dict[str, Any] = {
            "model": model or MODEL,
            "input": items,
            "tools": to_openai_tools(tools, strict=strict_tools),
        }
        if system:
            kwargs["instructions"] = system
        if text_format is not None:
            kwargs["text"] = {"format": text_format}
        if store is not None:
            kwargs["store"] = store
        if include:
            kwargs["include"] = list(include)
        if reasoning:
            kwargs["reasoning"] = dict(reasoning)

        response = client.responses.create(**kwargs)
        if on_response is not None:
            on_response(index, response)

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
            emit({"kind": "tool_call", "turn": index, "name": c.name,
                  "call_id": c.call_id, "input": c.arguments})

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

            emit({"kind": "tool_result", "turn": index, "name": call.name,
                  "call_id": call.call_id, "content": content,
                  "is_error": is_error})
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": call.call_id,
                    # The Responses API has no is_error flag; the error has to
                    # be legible in the text or the model cannot recover.
                    "output": f"ERROR: {content}" if is_error else content,
                }
            )

        # Checked between turns, after every call has its output, so the
        # transcript stays coherent rather than ending on an unanswered call.
        if out_of_time():
            emit({"kind": "stop", "turn": index, "reason": "timeout"})
            return Result(None, turns, "timeout", items)

    emit({"kind": "stop", "reason": "max_iterations"})
    return Result(None, turns, "max_iterations", items)
