"""Every file path a tool touches goes through here. Only /workspace exists.

The container already mounts nothing but the workspace, so this is the second
line, not the first: it turns an escape attempt into a legible refusal the
model can act on, instead of a permission error from a read-only root.

Resolve first, then check containment -- the same order as triage's
`read_source` -- so `..` and symlinks cannot walk out.
"""

import os
from pathlib import Path

# A prediction is the .duckdb file dbt builds into. The agent changes it only
# by running dbt; writing it directly would bypass the project entirely.
DENIED_WRITE_SUFFIXES = (".duckdb", ".duckdb.wal")


class PathDenied(Exception):
    pass


def workspace() -> Path:
    return Path(os.environ.get("SANDBOX_WORKSPACE", "/workspace")).resolve()


def resolve(path: str, write: bool = False) -> Path:
    """The real path inside the workspace, or PathDenied."""
    root = workspace()
    raw = (path or ".").strip()
    if raw in ("", "."):
        candidate = root
    else:
        p = Path(raw)
        if p.is_absolute() or raw.startswith("/"):
            # Accept "/workspace/models/x.sql" as written by a model that read
            # the system prompt, by re-rooting it under the real workspace.
            try:
                p = Path(raw).relative_to("/workspace")
            except ValueError:
                raise PathDenied(f"outside the workspace: {path}")
        candidate = root / p

    resolved = candidate.resolve()
    if resolved != root and root not in resolved.parents:
        raise PathDenied(f"outside the workspace: {path}")

    if write:
        if resolved.name.lower().endswith(DENIED_WRITE_SUFFIXES):
            raise PathDenied(
                f"{resolved.name} is the database; change it by running dbt, "
                "not by writing the file"
            )
        if resolved == root or resolved.is_dir():
            raise PathDenied(f"not a file: {path}")
    return resolved


def relative(path: Path) -> str:
    """How a path is shown back to the model."""
    root = workspace()
    try:
        rel = path.resolve().relative_to(root)
    except ValueError:
        return str(path)
    return rel.as_posix() or "."
