"""Phase 0 — run an agent and watch it.

Sends one prompt to the Claude Agent SDK and prints every message the agent
produces, in the order it produces them. The point is to see the loop happen:
what the agent says, which tools it reaches for, what comes back, and what the
whole thing cost.

Every run is also written to transcripts/ as JSON, so it can be re-read after
the terminal has scrolled away.

Usage:

    uv run agent "your prompt here"
"""

import asyncio
import dataclasses
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    query,
)

from . import config


def print_block(block) -> None:
    """Print one content block from an assistant message."""
    if isinstance(block, ThinkingBlock):
        print(f"[thinking] {block.thinking}")
    elif isinstance(block, TextBlock):
        print(block.text)
    elif isinstance(block, ToolUseBlock):
        print(f"[tool: {block.name}] {block.input}")
    elif isinstance(block, ToolResultBlock):
        print(f"[tool result] {block.content}")
    else:
        print(f"[{type(block).__name__}] {block}")


def print_result(message: ResultMessage) -> None:
    """Print the run summary — the numbers worth watching."""
    print("\n--- result ---")
    print(f"turns:    {message.num_turns}")
    print(f"duration: {message.duration_ms} ms")
    if message.total_cost_usd is not None:
        print(f"cost:     ${message.total_cost_usd:.4f}")
    if message.usage:
        print(f"usage:    {message.usage}")
    if message.is_error:
        print(f"error:    {message.result}")


def write_transcript(prompt: str, messages: list, session_id: str | None) -> Path:
    """Write one run to transcripts/ as JSON and return the path.

    Stdout scrolls away, and inside Docker `--rm` deletes the container and the
    CLI's own session log with it. This is the copy that survives.
    """
    started = datetime.now(timezone.utc)
    name = f"{started:%Y%m%d-%H%M%S}-{(session_id or 'unknown')[:8]}.json"
    path = config.TRANSCRIPTS_DIR / name

    config.TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "timestamp": started.isoformat(),
                "session_id": session_id,
                "prompt": prompt,
                "model": config.MODEL,
                "tools": config.READ_ONLY_TOOLS,
                "messages": messages,
            },
            indent=2,
            default=str,  # anything not JSON-native gets str()'d rather than raising
        ),
        encoding="utf-8",
    )
    return path


async def run(prompt: str) -> None:
    options = ClaudeAgentOptions(
        system_prompt=config.system_prompt(),
        model=config.MODEL,
        # tools= limits what exists; allowed_tools= stops it prompting for them.
        # Both are needed: see the comment in config.py.
        tools=config.READ_ONLY_TOOLS,
        allowed_tools=config.READ_ONLY_TOOLS,
    )

    messages: list = []
    session_id: str | None = None

    # finally: a run that crashes mid-way is exactly the one worth reading, so
    # write whatever was captured before re-raising.
    try:
        async for message in query(prompt=prompt, options=options):
            messages.append(
                dataclasses.asdict(message)
                if dataclasses.is_dataclass(message)
                else {"type": type(message).__name__, "repr": repr(message)}
            )
            session_id = getattr(message, "session_id", None) or session_id

            if isinstance(message, AssistantMessage):
                for block in message.content:
                    print_block(block)
            elif isinstance(message, ResultMessage):
                print_result(message)
    finally:
        path = write_transcript(prompt, messages, session_id)
        print(f"transcript: {path}")


def main() -> None:
    prompt = " ".join(sys.argv[1:]).strip()
    if not prompt:
        print('usage: agent "your prompt here"', file=sys.stderr)
        raise SystemExit(1)
    asyncio.run(run(prompt))


if __name__ == "__main__":
    main()
