"""PostToolUse hook: rebuild the generated docs when a .py file changes.

Reads the hook payload on stdin, and does nothing unless the tool touched a
Python file -- rebuilding on every Write would fire on the generated HTML
itself and on every markdown edit.

Kept as a script rather than a one-liner inside settings.json because a
command buried in JSON cannot be tested, and an untested hook that silently
does nothing is worse than no hook.

Wired up in .claude/settings.json as a PostToolUse hook on Write|Edit.

Test it by hand:

    echo '{"tool_input":{"file_path":"x.py"}}' | python docs/rebuild_hook.py
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GENERATORS = [
    ROOT / "docs" / "build_architecture.py",
    ROOT / "docs" / "build_failures.py",
]


def changed_path(payload: dict) -> str:
    return str(
        (payload.get("tool_response") or {}).get("filePath")
        or (payload.get("tool_input") or {}).get("file_path")
        or ""
    )


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return  # malformed payload: stay silent rather than break the turn

    if not changed_path(payload).endswith(".py"):
        return

    # sys.executable, not `uv run`: the generator imports only the standard
    # library, so the interpreter already running this hook is sufficient and
    # about twice as fast.
    for generator in GENERATORS:
        subprocess.run(
            [sys.executable, str(generator)],
            cwd=ROOT,
            capture_output=True,
            timeout=30,
        )


if __name__ == "__main__":
    main()
