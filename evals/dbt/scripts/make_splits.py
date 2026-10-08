"""Make evals/dbt/manifests/splits.json once: 13 held-out tasks, seed 42.

    uv run python -m evals.dbt.scripts.make_splits

Tune only on dev; report held_out at milestones only. The file is committed
and never regenerated: this script refuses to overwrite it.
"""

import argparse
import json
import random
import sys
from pathlib import Path

from evals.dbt.tasks import ROOT, file_sha256, load_manifest

MANIFESTS = ROOT / "evals" / "dbt" / "manifests"
SEED = 42
HELD_OUT = 13


def make(manifest_path: Path, seed: int = SEED, held_out: int = HELD_OUT) -> dict:
    _, tasks = load_manifest(manifest_path)
    ids = [t.instance_id for t in tasks]
    held = random.Random(seed).sample(ids, held_out)
    return {
        "seed": seed,
        "held_out": sorted(held),
        "dev": [i for i in ids if i not in held],
        "manifest_sha256": file_sha256(manifest_path),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--manifest", type=Path, default=MANIFESTS / "tasks_64.json")
    ap.add_argument("--out", type=Path, default=MANIFESTS / "splits.json")
    args = ap.parse_args()
    if args.out.exists():
        print(f"{args.out} exists; splits are never regenerated", file=sys.stderr)
        return 1
    splits = make(args.manifest)
    args.out.write_bytes((json.dumps(splits, indent=2) + "\n").encode("utf-8"))
    print(f"held_out={len(splits['held_out'])} dev={len(splits['dev'])} -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
