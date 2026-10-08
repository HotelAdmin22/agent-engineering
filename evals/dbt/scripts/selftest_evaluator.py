"""Evaluator self-test: does the official scorer behave on known inputs?

    uv run python -m evals.dbt.scripts.selftest_evaluator [--tasks a,b] [--workers 4]

For every manifest task, through the real scoring path (scratch copies, the
evaluator container):

  (a) the gold DB submitted as the prediction    -> must pass
  (b) the unmodified start DB as the prediction  -> should fail; a pass means
      the task is trivially passable and is flagged

Afterwards no scoring/ directory may remain and every gold hash must match
checksums.json. Results go to runs/dbt/_selftest/<stamp>.json.
"""

import argparse
import json
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from evals.dbt import config as config_mod
from evals.dbt import records, submission
from evals.dbt.image import content_tag
from evals.dbt.sandbox import image_id
from evals.dbt.scoring import score_submission
from evals.dbt.tasks import ROOT, dir_sha256, example_dir, gold_dir, load_manifest


def one(task, root: Path, image: str, work: Path) -> dict:
    out = {"instance_id": task.instance_id}
    gold_dbs = sorted(gold_dir(root, task.instance_id).glob("*.duckdb"))
    cases = {
        "gold_as_prediction": gold_dbs[0] if gold_dbs else None,
        "start_db_as_prediction": example_dir(root, task.instance_id) / task.start_db,
    }
    for name, src in cases.items():
        case_dir = work / task.instance_id / name
        if case_dir.exists():
            shutil.rmtree(case_dir)
        sub = case_dir / "submission"
        (sub / task.instance_id).mkdir(parents=True)
        if src is None or not src.exists():
            out[name] = {"passed": None, "detail": "no source file"}
            continue
        shutil.copy2(src, sub / task.instance_id / task.start_db)
        submission.write_metadata(sub, task.instance_id, task.start_db)
        s = score_submission(sub, task.instance_id, root, image, case_dir / "scoring")
        out[name] = {"passed": s.passed, "detail": s.detail,
                     "stdout_tail": s.stdout[-300:] if s.passed is None else ""}
        out[name]["scoring_left_behind"] = (case_dir / "scoring").exists()
        shutil.rmtree(case_dir, ignore_errors=True)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", type=Path, default=ROOT / "evals/dbt/configs/baseline.toml")
    ap.add_argument("--tasks", help="comma-separated subset")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    cfg = config_mod.load(args.config)
    root = config_mod.resolve(cfg, "spider2_root")
    _, tasks = load_manifest(config_mod.resolve(cfg, "task_manifest"))
    if args.tasks:
        wanted = set(args.tasks.split(","))
        tasks = [t for t in tasks if t.instance_id in wanted]
    image = content_tag()
    if not image_id(image):
        print(f"image {image} not built; run evals.dbt.scripts.build_image", file=sys.stderr)
        return 2

    stamp = time.strftime("%Y%m%d-%H%M%S")
    work = config_mod.resolve(cfg, "runs_dir") / "_selftest" / stamp
    work.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(lambda t: one(t, root, image, work), tasks))

    gold_ok = all(dir_sha256(gold_dir(root, t.instance_id)) == t.gold_dir_sha256 for t in tasks)
    gold_fail = [r["instance_id"] for r in results if r["gold_as_prediction"]["passed"] is not True]
    trivial = [r["instance_id"] for r in results if r["start_db_as_prediction"]["passed"] is True]
    errors = [r["instance_id"] for r in results
              if None in (r["gold_as_prediction"]["passed"], r["start_db_as_prediction"]["passed"])]
    leftovers = [r["instance_id"] for r in results
                 if any(r[c].get("scoring_left_behind") for c in
                        ("gold_as_prediction", "start_db_as_prediction"))]
    summary = {
        "image": image, "image_id": image_id(image), "tasks": len(results),
        "gold_passes": len(results) - len(gold_fail), "gold_failures": gold_fail,
        "trivially_passable": trivial, "harness_errors": errors,
        "scoring_dirs_left": leftovers, "gold_hashes_unchanged": gold_ok,
        "results": results,
    }
    records.write_json_atomic(work.parent / f"{stamp}.json", summary)
    shutil.rmtree(work, ignore_errors=True)
    print(json.dumps({k: v for k, v in summary.items() if k != "results"}, indent=2))
    print(f"full results: {work.parent / f'{stamp}.json'}")
    ok = not gold_fail and not errors and not leftovers and gold_ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
