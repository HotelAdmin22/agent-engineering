"""A worked example of the Claude tool-use loop. READ THIS, THEN WRITE loop.py.

This is a reference, not the Phase 1 deliverable. The point of Phase 1 is to
build the loop yourself; copying this file skips the understanding it exists to
give you. Read it, close it, write your own, then compare.

Requires two dependencies that Phase 0 deliberately does not have:

    uv add anthropic          # the low-level SDK -- no harness, no built-in tools
    uv add --dev duckdb       # only so this example has something to query

And an API key. Note that CLAUDE_CODE_OAUTH_TOKEN does NOT work here: that
authenticates the Claude Code CLI which claude-agent-sdk spawns. The raw SDK
wants ANTHROPIC_API_KEY.

    uv run python -m manual_loop.example_loop "How many customers are there?"
"""

import os
import sys

import anthropic
import duckdb

MODEL = "claude-opus-5"
MAX_TOKENS = 16000

# A `while True` against a paid API, talking to a model that can always justify
# one more tool call, is how you wake up to a surprising bill. Cap it.
MAX_ITERATIONS = 10

# Cap rows so one careless SELECT * cannot fill the context window. Truncation
# has to be VISIBLE to the model, or it will reason over partial data believing
# it is complete -- see the note in query_database().
MAX_ROWS = 50

DB_PATH = os.getenv("TPCH_DB", "tpch.duckdb")


# --------------------------------------------------------------------------
# 1. The tool
#
# A "tool" is three things on the wire: a name, a description, and a JSON
# Schema. That is the whole concept. The description is not documentation --
# it is the only thing the model reads to decide whether to call this, so it
# is a prompt and should be written like one.
# --------------------------------------------------------------------------

QUERY_DATABASE = {
    "name": "query_database",
    "description": (
        "Run a read-only SQL query against the TPC-H DuckDB database and get "
        "the rows back. Use this to answer any question about the data. "
        "Prefer one precise query over several broad ones."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "sql": {
                "type": "string",
                "description": "A single SELECT statement in DuckDB SQL dialect.",
            }
        },
        "required": ["sql"],
        "additionalProperties": False,
    },
}

TOOLS = [QUERY_DATABASE]


def query_database(sql: str) -> str:
    """Execute SQL and return rows as text.

    read_only=True is the structural restriction: this connection is incapable
    of writing, whatever SQL the model sends. Compare that to inspecting the
    string for the word DROP, which is a guess about what the model might try.
    """
    con = duckdb.connect(DB_PATH, read_only=True)
    try:
        result = con.execute(sql)
        columns = [d[0] for d in result.description]
        rows = result.fetchmany(MAX_ROWS)
        more = result.fetchone() is not None
    finally:
        con.close()

    lines = [" | ".join(columns)]
    lines += [" | ".join("NULL" if v is None else str(v) for v in row) for row in rows]
    if more:
        # Tell the model it is seeing a truncated result. Silent truncation is
        # worse than an error: the model confidently answers from partial data.
        lines.append(f"... truncated at {MAX_ROWS} rows. Add LIMIT or aggregate.")
    return "\n".join(lines)


def execute_tool(name: str, tool_input: dict) -> tuple[str, bool]:
    """Dispatch to a tool. Returns (content, is_error).

    Errors are RETURNED, never raised. A failed tool still needs a tool_result
    or the conversation structure breaks -- and for text-to-SQL the error text
    is the most useful thing the model gets, because that is how it repairs a
    bad query on the next turn. Recovery is where the accuracy lives.
    """
    try:
        if name == "query_database":
            return query_database(tool_input["sql"]), False
        return f"No such tool: {name}", True
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}", True


# --------------------------------------------------------------------------
# 2. The loop
# --------------------------------------------------------------------------


def run(user_input: str) -> str | None:
    client = anthropic.Anthropic()
    messages: list[dict] = [{"role": "user", "content": user_input}]
    totals = {"input": 0, "output": 0}

    for iteration in range(1, MAX_ITERATIONS + 1):
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            thinking={"type": "adaptive"},
            tools=TOOLS,
            messages=messages,
        )

        totals["input"] += response.usage.input_tokens
        totals["output"] += response.usage.output_tokens
        print(f"\n--- turn {iteration} (stop_reason={response.stop_reason}) ---")

        # Handle EVERY stop_reason. An unhandled one is an infinite loop or a
        # silently truncated answer.
        if response.stop_reason == "refusal":
            print("model declined the request")
            return None
        if response.stop_reason == "max_tokens":
            print("hit max_tokens -- answer is truncated, raise MAX_TOKENS")
            return None
        if response.stop_reason == "pause_turn":
            # Server-side tool paused mid-turn. Echo it back to resume.
            messages.append({"role": "assistant", "content": response.content})
            continue
        if response.stop_reason == "end_turn":
            for block in response.content:
                if block.type == "text":
                    print(block.text)
            print(f"\ntokens: {totals}")
            return next(
                (b.text for b in response.content if b.type == "text"), None
            )

        # stop_reason == "tool_use" from here down.

        # Append the WHOLE content list, not just the text. It carries the
        # tool_use blocks and the thinking blocks, and thinking blocks must go
        # back unchanged or the next turn is rejected.
        messages.append({"role": "assistant", "content": response.content})

        # One turn can contain SEVERAL tool_use blocks. Every result must come
        # back in a SINGLE user message -- splitting them across messages
        # silently teaches the model to stop making parallel calls. No error,
        # just quietly worse behaviour.
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            print(f"[tool: {block.name}] {block.input}")
            content, is_error = execute_tool(block.name, block.input)
            print(f"[result] {content[:200]}")
            tool_results.append(
                {
                    "type": "tool_result",
                    # Must match the tool_use block's id exactly.
                    "tool_use_id": block.id,
                    "content": content,
                    "is_error": is_error,
                }
            )

        messages.append({"role": "user", "content": tool_results})

    print(f"stopped: hit MAX_ITERATIONS={MAX_ITERATIONS}")
    return None


def main() -> None:
    prompt = " ".join(sys.argv[1:]).strip()
    if not prompt:
        print('usage: python -m manual_loop.example_loop "your question"', file=sys.stderr)
        raise SystemExit(1)
    run(prompt)


if __name__ == "__main__":
    main()
