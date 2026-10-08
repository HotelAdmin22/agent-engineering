"""dbt readiness: every manifest task's project parses and connects in a fresh
task container, and the tasks that need packages have them cached.

    uv run python -m evals.dbt.scripts.check_tasks_dbt [--deps] [--tasks a,b]

--deps resolves missing packages first (one one-shot container per task, with
network, mounting only a scratch copy; see sandbox.ensure_deps).

`dbt debug` exits non-zero in the sandbox because git is not installed; git is
only needed by `dbt deps` for git packages, which never runs in a task
container. Readiness is therefore read from the debug output: profiles.yml and
dbt_project.yml valid, connection ok.
"""

import argparse
import json
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from evals.dbt import config as config_mod
from evals.dbt import docker_exec, records, sandbox
from evals.dbt.image import content_tag
from evals.dbt.tasks import ROOT, dir_sha256, example_dir, load_manifest

READY_MARKERS = ("profiles.yml file [OK found and valid]",
                 "dbt_project.yml file [OK found and valid]",
                 "Connection test: [OK connection ok]")


def check(task, root: Path, cfg: dict, image: str, work: Path, deps_root: Path) -> dict:
    ws = work / task.instance_id / "workspace"
    out = {"instance_id": task.instance_id,
           "needs_deps": sandbox.needs_deps(example_dir(root, task.instance_id))}
    try:
        sandbox.stage(task, root, ws, deps_root)
    except sandbox.StageError as exc:
        return out | {"ready": False, "error": str(exc)}
    sc = cfg["sandbox"]
    scfg = sandbox.SandboxConfig(image=image, memory=sc["memory"], cpus=sc["cpus"],
                                 pids_limit=sc["pids_limit"], user=sc["user"])
    name = sandbox.container_name("check", 0, task.instance_id)
    try:
        sandbox.start(name, ws, scfg, {"run_id": "check", "instance_id": task.instance_id})
        started = time.monotonic()
        r = docker_exec.run_tool(name, "dbt_debug", {}, {"timeout_s": 120}, 150)
        log = r.payload.get("log", "") or r.payload.get("error", "")
        out["debug_seconds"] = round(time.monotonic() - started, 1)
        out["ready"] = all(m in log for m in READY_MARKERS)
        out["profiles_dir"] = "/workspace" if "Using profiles.yml file at /workspace/profiles.yml" in log else "?"
        if not out["ready"]:
            out["log_tail"] = log[-1200:]
    finally:
        sandbox.remove(name)
        shutil.rmtree(work / task.instance_id, ignore_errors=True)
    out["source_unchanged"] = dir_sha256(example_dir(root, task.instance_id)) == task.example_dir_sha256
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", type=Path, default=ROOT / "evals/dbt/configs/baseline.toml")
    ap.add_argument("--tasks")
    ap.add_argument("--deps", action="store_true", help="resolve missing dbt packages first")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    cfg = config_mod.load(args.config)
    root = config_mod.resolve(cfg, "spider2_root")
    runs_dir = config_mod.resolve(cfg, "runs_dir")
    deps_root = runs_dir / "_deps"
    _, tasks = load_manifest(config_mod.resolve(cfg, "task_manifest"))
    if args.tasks:
        wanted = set(args.tasks.split(","))
        tasks = [t for t in tasks if t.instance_id in wanted]
    image = content_tag()
    if not sandbox.image_id(image):
        print(f"image {image} not built; run evals.dbt.scripts.build_image", file=sys.stderr)
        return 2

    deps = {}
    if args.deps:
        for t in tasks:
            if sandbox.needs_deps(example_dir(root, t.instance_id)):
                print(f"dbt deps: {t.instance_id}", flush=True)
                info = sandbox.ensure_deps(t, root, deps_root, image, cfg["sandbox"]["user"])
                deps[t.instance_id] = info["dbt_packages_sha256"] if info else None

    work = runs_dir / "_check" / time.strftime("%Y%m%d-%H%M%S")
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(lambda t: check(t, root, cfg, image, work, deps_root), tasks))
    shutil.rmtree(work, ignore_errors=True)

    ready = [r for r in results if r.get("ready")]
    summary = {
        "image": image, "tasks": len(results), "ready": len(ready),
        "not_ready": [r["instance_id"] for r in results if not r.get("ready")],
        "needs_deps": [r["instance_id"] for r in results if r["needs_deps"]],
        "deps_resolved": deps,
        "source_changed": [r["instance_id"] for r in results if not r.get("source_unchanged", True)],
        "results": results,
    }
    out_path = runs_dir / "_check" / f"{time.strftime('%Y%m%d-%H%M%S')}.json"
    records.write_json_atomic(out_path, summary)
    print(json.dumps({k: v for k, v in summary.items() if k != "results"}, indent=2))
    for r in results:
        if not r.get("ready"):
            print(f"--- {r['instance_id']}: {r.get('error') or r.get('log_tail', '')[-600:]}")
    return 0 if len(ready) == len(results) and not summary["source_changed"] else 1


if __name__ == "__main__":
    sys.exit(main())
