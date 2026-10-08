"""read / list / search / write / edit, bounded and confined to /workspace.

Each function returns a JSON-able dict. Errors are {"error": ...}; nothing
raises out of here except a bug, which toolhost turns into an error too.
"""

import fnmatch
import re
from pathlib import Path

from .path_guard import PathDenied, relative, resolve, workspace

MAX_LIST = 500
MAX_MATCHES = 200
MAX_LINE_CHARS = 300

# Never worth reading as text, and the .duckdb files are large.
BINARY_SUFFIXES = {".duckdb", ".wal", ".zip", ".gz", ".parquet", ".pyc", ".png", ".jpg"}
SKIP_DIRS = {".git", "__pycache__", "logs"}


def _is_texty(path: Path) -> bool:
    return path.suffix.lower() not in BINARY_SUFFIXES


def _walk(base: Path):
    root = workspace()
    for path in sorted(base.rglob("*")):
        rel_parts = path.relative_to(root).parts if path != root else ()
        if any(part in SKIP_DIRS for part in rel_parts):
            continue
        yield path


def read_file(path: str, offset: int | None = None, limit: int | None = None,
              max_chars: int = 4000) -> dict:
    try:
        target = resolve(path)
    except PathDenied as exc:
        return {"error": str(exc)}
    if not target.is_file():
        return {"error": f"no such file: {path}"}
    if not _is_texty(target):
        return {"error": f"{target.name} is binary; query it with query_duckdb"}

    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(int(offset or 1), 1)
    count = int(limit) if limit else len(lines)
    chunk = lines[start - 1: start - 1 + count]
    numbered = "\n".join(f"{start + i:>5}  {line}" for i, line in enumerate(chunk))
    truncated = len(numbered) > max_chars
    if truncated:
        numbered = numbered[:max_chars]
    return {
        "path": relative(target),
        "total_lines": len(lines),
        "from_line": start,
        "content": numbered,
        "truncated": truncated,
        "note": "pass offset/limit to read further" if truncated else "",
    }


def list_files(pattern: str | None = None) -> dict:
    root = workspace()
    pattern = (pattern or "**/*").strip() or "**/*"
    if pattern.startswith("/workspace/"):
        pattern = pattern[len("/workspace/"):]
    if ".." in Path(pattern).parts:
        return {"error": "pattern may not contain .."}
    out = []
    for path in _walk(root):
        if not path.is_file():
            continue
        rel = relative(path)
        if fnmatch.fnmatch(rel, pattern) or (pattern.startswith("**/")
                                              and fnmatch.fnmatch(rel, pattern[3:])):
            out.append(rel)
            if len(out) > MAX_LIST:
                break
    truncated = len(out) > MAX_LIST
    return {"pattern": pattern, "files": out[:MAX_LIST], "count": min(len(out), MAX_LIST),
            "truncated": truncated}


def search_files(regex: str, path: str | None = None, glob: str | None = None) -> dict:
    try:
        rx = re.compile(regex)
    except re.error as exc:
        return {"error": f"invalid regex: {exc}"}
    try:
        base = resolve(path or ".")
    except PathDenied as exc:
        return {"error": str(exc)}

    files = [base] if base.is_file() else [p for p in _walk(base) if p.is_file()]
    matches = []
    for file in files:
        if not _is_texty(file):
            continue
        rel = relative(file)
        if glob and not (fnmatch.fnmatch(rel, glob) or fnmatch.fnmatch(file.name, glob)):
            continue
        try:
            text = file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                matches.append({"path": rel, "line": number, "text": line[:MAX_LINE_CHARS]})
                if len(matches) > MAX_MATCHES:
                    break
        if len(matches) > MAX_MATCHES:
            break
    return {"matches": matches[:MAX_MATCHES], "count": min(len(matches), MAX_MATCHES),
            "truncated": len(matches) > MAX_MATCHES}


def write_file(path: str, content: str) -> dict:
    try:
        target = resolve(path, write=True)
    except PathDenied as exc:
        return {"error": str(exc)}
    target.parent.mkdir(parents=True, exist_ok=True)
    existed = target.exists()
    target.write_text(content, encoding="utf-8", newline="\n")
    return {"path": relative(target), "created": not existed, "chars": len(content)}


def edit_file(path: str, old: str, new: str) -> dict:
    try:
        target = resolve(path, write=True)
    except PathDenied as exc:
        return {"error": str(exc)}
    if not target.is_file():
        return {"error": f"no such file: {path}"}
    if not old:
        return {"error": "old must be non-empty; use write_file to create a file"}
    text = target.read_text(encoding="utf-8", errors="replace")
    count = text.count(old)
    if count == 0:
        return {"error": "old text not found; read the file and copy it exactly"}
    if count > 1:
        return {"error": f"old text matches {count} times; include more context so it is unique"}
    target.write_text(text.replace(old, new, 1), encoding="utf-8", newline="\n")
    return {"path": relative(target), "replaced": 1}
