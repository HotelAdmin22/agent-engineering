"""Build evals/dbt/manifests/tasks_64.json and checksums.json from the clone.

    uv run python -m evals.dbt.scripts.build_manifest

Inputs: the Spider2 clone (spider2_root in the config) and the owner's
manifests/excluded.json. Fails loudly instead of guessing:

  - both JSONL files list the same instance ids
  - every excluded id exists, and exactly source_task_count - 64 are excluded
  - each kept task has exactly one start .duckdb and its gold file
  - every evaluation func is duckdb_match (anything else stops for review)
  - task order follows spider2-dbt.jsonl

A rerun with the same inputs is byte-identical.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

from evals.dbt import config as config_mod
from evals.dbt.tasks import (ROOT, dir_sha256, example_dir, file_sha256, gold_dir,
                             json_sha256)

MANIFESTS = ROOT / "evals" / "dbt" / "manifests"
TARGET_COUNT = 64


class ManifestError(Exception):
    pass


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def spider2_commit(spider2_root: Path) -> str | None:
    proc = subprocess.run(["git", "-C", str(spider2_root), "rev-parse", "HEAD"],
                          capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else None


def build(spider2_root: Path, excluded: dict, target: int = TARGET_COUNT,
          commit: str | None = None) -> tuple[dict, dict]:
    examples = _jsonl(spider2_root / "examples" / "spider2-dbt.jsonl")
    evals = _jsonl(spider2_root / "evaluation_suite" / "gold" / "spider2_eval.jsonl")
    ex_ids = [e["instance_id"] for e in examples]
    ev_by_id = {e["instance_id"]: e for e in evals}

    if len(set(ex_ids)) != len(ex_ids):
        raise ManifestError("duplicate instance ids in spider2-dbt.jsonl")
    if set(ex_ids) != set(ev_by_id):
        raise ManifestError(f"JSONL id sets differ: {sorted(set(ex_ids) ^ set(ev_by_id))}")
    if excluded.get("source_task_count") != len(ex_ids):
        raise ManifestError(f"excluded.json says source_task_count="
                            f"{excluded.get('source_task_count')}, clone has {len(ex_ids)}")

    excl_ids = [e["instance_id"] for e in excluded["excluded"]]
    if len(set(excl_ids)) != len(excl_ids):
        raise ManifestError(f"duplicate ids in excluded.json: {excl_ids}")
    missing_reason = [e["instance_id"] for e in excluded["excluded"] if not e.get("reason")]
    if missing_reason:
        raise ManifestError(f"excluded entries without a reason: {missing_reason}")
    unknown = sorted(set(excl_ids) - set(ex_ids))
    if unknown:
        raise ManifestError(f"excluded ids not in the benchmark: {unknown}")
    if len(set(excl_ids)) != len(ex_ids) - target:
        raise ManifestError(f"need exactly {len(ex_ids) - target} exclusions, "
                            f"got {len(set(excl_ids))}")

    tasks, checksums = [], {"examples": {}, "gold": {}}
    for item in examples:
        iid = item["instance_id"]
        if iid in excl_ids:
            continue
        evaluation = ev_by_id[iid]["evaluation"]
        if not isinstance(evaluation, dict) or evaluation.get("func") != "duckdb_match":
            raise ManifestError(f"{iid}: evaluation is not a single duckdb_match "
                                f"({evaluation!r:.120}); stop for owner review")
        ex_dir, g_dir = example_dir(spider2_root, iid), gold_dir(spider2_root, iid)
        dbs = sorted(p.name for p in ex_dir.glob("*.duckdb"))
        if len(dbs) != 1:
            raise ManifestError(f"{iid}: expected one start .duckdb, found {dbs}")
        gold_name = evaluation["parameters"]["gold"]
        if not g_dir.is_dir() or not any(g_dir.glob("*.duckdb")):
            raise ManifestError(f"{iid}: gold directory has no .duckdb")
        # A gold *name* mismatch (the spec names a file the directory does not
        # contain) is recorded, not fatal: the evaluator self-test decides
        # whether the task is passable, and the owner decides what to do.
        ex_hash, g_hash = dir_sha256(ex_dir), dir_sha256(g_dir)
        checksums["examples"][iid] = ex_hash
        checksums["gold"][iid] = g_hash
        tasks.append({
            "instance_id": iid,
            "instruction": item["instruction"],
            "type": item["type"],
            "start_db": dbs[0],
            "evaluation": evaluation,
            "example_dir_sha256": ex_hash,
            "gold_dir_sha256": g_hash,
            "gold_file_present": (g_dir / gold_name).exists(),
        })

    manifest = {
        "spider2_commit": commit,
        "source_task_count": len(ex_ids),
        "task_count": len(tasks),
        "excluded_sha256": json_sha256(excluded),
        "tasks": tasks,
    }
    for zname in ("DBT_start_db.zip", "dbt_gold.zip"):
        z = spider2_root / zname
        checksums.setdefault("zips", {})[zname] = file_sha256(z) if z.exists() else None
    return manifest, checksums


def dump(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", type=Path, default=ROOT / "evals/dbt/configs/baseline.toml")
    ap.add_argument("--excluded", type=Path, default=MANIFESTS / "excluded.json")
    ap.add_argument("--out", type=Path, default=MANIFESTS / "tasks_64.json")
    ap.add_argument("--checksums", type=Path, default=MANIFESTS / "checksums.json")
    args = ap.parse_args()

    cfg = config_mod.load(args.config)
    root = config_mod.resolve(cfg, "spider2_root")
    if not args.excluded.exists():
        print(f"missing {args.excluded}: the owner supplies the excluded list (B2)",
              file=sys.stderr)
        return 2
    excluded = json.loads(args.excluded.read_text(encoding="utf-8"))
    try:
        manifest, checksums = build(root, excluded, commit=spider2_commit(root))
    except ManifestError as exc:
        print(f"manifest build failed: {exc}", file=sys.stderr)
        return 1
    # Written with "\n" endings regardless of platform: the manifest's hash is
    # recorded in splits.json and every run manifest.
    args.out.write_bytes(dump(manifest).encode("utf-8"))
    args.checksums.write_bytes(dump(checksums).encode("utf-8"))
    flagged = [t["instance_id"] for t in manifest["tasks"] if not t["gold_file_present"]]
    print(f"wrote {args.out.relative_to(ROOT)}: {manifest['task_count']} tasks "
          f"(sha256 {file_sha256(args.out)[:12]})")
    if flagged:
        print(f"WARNING: evaluation spec names a gold file that is not present for: "
              f"{flagged} -- see DECISIONS.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
