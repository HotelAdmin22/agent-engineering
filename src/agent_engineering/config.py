"""Configuration for the Phase 0 agent.

Minimal on purpose. This grows when a phase needs it to, and not before.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

# Where run transcripts are written. The default resolves to /app/transcripts in
# the container, which is the bind mount, so runs survive `--rm`. Override with
# TRANSCRIPTS_DIR if the project is ever installed non-editable.
TRANSCRIPTS_DIR = Path(os.getenv("TRANSCRIPTS_DIR") or PROJECT_ROOT / "transcripts")

# Left unset by default so the Claude Code CLI picks its own model. Set
# ANTHROPIC_MODEL in .env to pin one.
MODEL = os.getenv("ANTHROPIC_MODEL") or None

# Every read-only built-in tool, and nothing else.
#
# This list is passed to BOTH `tools` and `allowed_tools`, because they do
# different jobs and only one of them is a real restriction:
#
#   tools=          the entire tool surface offered to the model. Tools absent
#                   from it are never presented, so the model cannot call them.
#                   THIS is the structural restriction. Default None = all tools.
#   allowed_tools=  merely auto-approves tools so they run without a permission
#                   prompt. Setting only this leaves every other built-in tool
#                   reachable; the model still tries them and gets blocked by an
#                   approval step that nobody answers in a non-interactive run.
#
# Getting that backwards was a real bug here: with allowed_tools alone, the
# agent attempted Bash, Write, and even aliasing a python binary to evade the
# block, burning 46 turns before giving up. See the SDK docstring on
# ClaudeAgentOptions.allowed_tools: "To restrict which tools are available at
# all, use `tools`."
#
# Excluded, because none of these are read-only:
#   Bash, BashOutput, KillShell  -- arbitrary command execution
#   Write, Edit, MultiEdit       -- file mutation
#   NotebookEdit                 -- file mutation
#   Task                         -- spawns subagents that get their own tools
#   TodoWrite                    -- scratch state only, but excluding it keeps
#                                   the invariant "nothing here can write"
READ_ONLY_TOOLS = [
    "Read",
    "Glob",
    "Grep",
    "NotebookRead",
    "WebSearch",
    "WebFetch",
]


def system_prompt() -> str:
    """Read the system prompt from prompts/system.md."""
    return (PROMPTS_DIR / "system.md").read_text(encoding="utf-8")
