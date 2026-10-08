"""The sandbox image tag is a hash of what goes into the image.

Configs never name an image. The runner derives the tag from the files the
Dockerfile copies, so a run cannot silently use an image built from different
code: change the gateway, the tool host or a pin and the tag changes, and the
runner stops with "run build_image" until the image matching the tree exists.
"""

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
IMAGE_NAME = "spider2-dbt-sandbox"

# Everything the Dockerfile COPYs, plus the lock the requirements come from.
INPUTS = [
    "docker/dbt-sandbox",
    "pyproject.toml",
    "README.md",
    "uv.lock",
    "src",
    "evals/dbt/sandbox_tools",
]
SKIP_PARTS = {"__pycache__", ".pytest_cache"}


def input_files(root: Path = ROOT) -> list[Path]:
    files = []
    for entry in INPUTS:
        path = root / entry
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files += [p for p in path.rglob("*")
                      if p.is_file() and not SKIP_PARTS & set(p.parts)
                      and p.suffix != ".pyc"]
    return sorted(files, key=lambda p: p.relative_to(root).as_posix())


def inputs_hash(root: Path = ROOT) -> str:
    h = hashlib.sha256()
    for path in input_files(root):
        rel = path.relative_to(root).as_posix()
        # Normalise line endings: a CRLF checkout must hash like an LF one, or
        # the same commit gets a different tag per machine.
        data = path.read_bytes().replace(b"\r\n", b"\n")
        h.update(rel.encode() + b"\0" + hashlib.sha256(data).digest())
    return h.hexdigest()


def content_tag(root: Path = ROOT) -> str:
    return f"{IMAGE_NAME}:{inputs_hash(root)[:12]}"
