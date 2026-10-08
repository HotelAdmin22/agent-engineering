"""The task manifest, the splits, and the directory hashes both rest on.

`tasks_64.json` is generated once by scripts/build_manifest.py from the Spider2
clone and the owner's excluded list, and never edited by hand. Everything that
runs a task loads it through here, so a task's identity is always the same
instance_id, instruction and source hash.
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def dir_sha256(path: Path) -> str:
    """sha256 over sorted (relative_path, file_sha256) pairs."""
    h = hashlib.sha256()
    files = sorted((p for p in path.rglob("*") if p.is_file()),
                   key=lambda p: p.relative_to(path).as_posix())
    for f in files:
        h.update(f.relative_to(path).as_posix().encode() + b"\0")
        h.update(file_sha256(f).encode() + b"\n")
    return h.hexdigest()


def json_sha256(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class Task:
    instance_id: str
    instruction: str
    type: str
    start_db: str
    evaluation: dict
    example_dir_sha256: str
    gold_dir_sha256: str
    gold_file_present: bool = True

    @property
    def gold_file(self) -> str:
        return self.evaluation["parameters"]["gold"]


def load_manifest(path: Path) -> tuple[dict, list[Task]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    tasks = [Task(**t) for t in data["tasks"]]
    if len(tasks) != data["task_count"] or len({t.instance_id for t in tasks}) != len(tasks):
        raise ValueError(f"{path}: task_count does not match its tasks")
    return data, tasks


def load_splits(path: Path, manifest_path: Path) -> dict[str, str]:
    """instance_id -> 'dev' | 'held_out', after checking the manifest hash."""
    splits = json.loads(Path(path).read_text(encoding="utf-8"))
    actual = file_sha256(Path(manifest_path))
    if splits["manifest_sha256"] != actual:
        raise ValueError(f"{path} was made for a different manifest "
                         f"({splits['manifest_sha256'][:12]} != {actual[:12]})")
    out = {i: "held_out" for i in splits["held_out"]}
    out.update({i: "dev" for i in splits["dev"]})
    return out


def example_dir(spider2_root: Path, instance_id: str) -> Path:
    return Path(spider2_root) / "examples" / instance_id


def gold_dir(spider2_root: Path, instance_id: str) -> Path:
    return Path(spider2_root) / "evaluation_suite" / "gold" / instance_id


def eval_suite(spider2_root: Path) -> Path:
    return Path(spider2_root) / "evaluation_suite"
