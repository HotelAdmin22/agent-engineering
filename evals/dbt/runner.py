"""Run Spider2-DBT: k attempts over the 64-task manifest, one config.

    uv run python -m evals.dbt.runner --config evals/dbt/configs/baseline.toml
    uv run python -m evals.dbt.runner --config evals/dbt/configs/routed.toml \\
        --tasks playbook001,f1001 --attempts 1                      # smoke
    uv run python -m evals.dbt.runner --resume <run_id>

Separate from evals/runner.py (Spider2-Lite) and shares none of its internals;
it mirrors its conventions: announce before running, append-only progress,
resume by skipping what is finished.

Per task attempt (evals/dbt/PROJECT_SETUP.md §6.3):

  stage -> start container -> agent (main [+ subagents]) -> final `dbt run`
  -> stop + remove container -> move the prediction to submission/
  -> score on scratch copies in a fresh evaluator container -> result.json

Resume: a task attempt is done when its result.json exists with a status
other than harness_error. Anything else is reset first -- its container is
force-removed and its whole directory, half-edited workspace included, is
deleted and re-staged from the source.
"""

import argparse
import csv
import logging
import platform
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from . import config as config_mod
from . import records, sandbox, scoring, spec, submission
from .agent_adapter import Outcome, TaskContext, run_role, run_task
from .docker_exec import ContainerDead, run_tool
from .image import content_tag, inputs_hash
from .tasks import ROOT, Task, file_sha256, load_manifest, load_splits
from .tools import specs_hash

# Pruning keeps a run near 1.6 GB; this is headroom for the
# in-flight attempts plus the image's own scratch.
MIN_FREE_GB = 8.0

log = logging.getLogger("evals.dbt")
_print_lock = threading.Lock()


class RunRefused(Exception):
    pass


# --- environment and fingerprint -------------------------------------------

def git(*args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    return proc.stdout.strip()


def git_dirty() -> bool:
    return bool(git("status", "--porcelain"))


def spider2_commit(root: Path) -> str | None:
    proc = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                          capture_output=True, text=True)
    return proc.stdout.strip() or None


def build_run_manifest(cfg: dict, run_id: str, selection: dict, image: str,
                       allow_dirty: bool) -> dict:
    import openai

    roles = config_mod.roles(cfg)
    root = config_mod.resolve(cfg, "spider2_root")
    manifest_path = config_mod.resolve(cfg, "task_manifest")
    mdir = manifest_path.parent
    deps_root = config_mod.resolve(cfg, "runs_dir") / "_deps"
    deps = {p.name: records.read_json(p / "deps.json")["dbt_packages_sha256"]
            for p in sorted(deps_root.glob("*"))
            if p.name in selection["tasks"] and (p / "deps.json").exists()}
    sc = cfg["sandbox"]
    scfg = sandbox.SandboxConfig(image=image, network=sc["network"], memory=sc["memory"],
                                 cpus=sc["cpus"], pids_limit=sc["pids_limit"], user=sc["user"])
    base_digest = next((l.split("@", 1)[1].strip() for l in
                        (ROOT / "docker/dbt-sandbox/Dockerfile").read_text().splitlines()
                        if l.startswith("FROM ") and "@" in l), None)
    return {
        "run_id": run_id,
        "created_at": records.now_iso(),
        "git_sha": git("rev-parse", "HEAD"),
        "git_dirty": git_dirty(),
        "allow_dirty": allow_dirty,
        "config": {k: v for k, v in cfg.items() if not k.startswith("_")}
        | {"spider2_root_resolved": str(root), "config_path": cfg["_path"]},
        "selection": selection,
        "roles": {
            name: {"model": r.model, "settings_sent": r.settings_sent(),
                   "include": ["reasoning.encrypted_content"]
                   if cfg["api"].get("include_reasoning", True) else [],
                   "max_turns": r.max_turns, "tools": r.tools,
                   "output_schema": r.output_schema,
                   "prompt_path": str(r.system_prompt_path.relative_to(ROOT)),
                   "prompt_sha256": file_sha256(r.system_prompt_path),
                   "models_returned": []}
            for name, r in roles.items()
        },
        "sandbox": {
            "docker": sandbox.versions(),
            "image": image,
            "image_id": sandbox.image_id(image),
            "base_digest": base_digest,
            "inputs_sha256": inputs_hash(),
            "container_flags": scfg.flags(),
            "deps_cache": deps,
        },
        "host": {"python": platform.python_version(), "openai": openai.__version__,
                 "os": platform.platform(), "uv_lock_sha256": file_sha256(ROOT / "uv.lock")},
        "benchmark": {
            "spider2_commit": spider2_commit(root),
            "manifest_sha256": file_sha256(manifest_path),
            "excluded_sha256": file_sha256(mdir / "excluded.json"),
            "splits_sha256": file_sha256(config_mod.resolve(cfg, "splits")),
            "checksums_sha256": file_sha256(mdir / "checksums.json"),
        },
        "tool_specs_sha256": specs_hash(),
        "attempts": selection["attempts"],
        "budgets": {"task_timeout_s": cfg["task_timeout_s"],
                    **{f"{n}.max_turns": r.max_turns for n, r in roles.items()},
                    **({"max_subagent_calls": cfg["delegation"]["max_subagent_calls"]}
                       if "delegation" in cfg else {})},
        "pricing": cfg["pricing"],
        "concurrency": cfg["concurrency"],
        "task_count": len(selection["tasks"]),
    }


# Fields that may legitimately differ between the original run and a resume.
VOLATILE = {"created_at", "git_dirty", "allow_dirty", "docker"}


def fingerprint(m: dict) -> dict:
    def strip(obj):
        if isinstance(obj, dict):
            return {k: strip(v) for k, v in obj.items()
                    if k not in VOLATILE and k != "models_returned"}
        return obj
    return strip(m)


def fingerprint_diff(a: dict, b: dict, prefix: str = "") -> list[str]:
    out = []
    for k in sorted(set(a) | set(b)):
        va, vb = a.get(k), b.get(k)
        if isinstance(va, dict) and isinstance(vb, dict):
            out += fingerprint_diff(va, vb, f"{prefix}{k}.")
        elif va != vb:
            out.append(f"{prefix}{k}")
    return out


def check_free_space(min_gb: float = MIN_FREE_GB) -> None:
    """Refuse at minute zero rather than at attempt 3 of 3.

    A run that dies on ENOSPC has already spent its money and cannot be
    scored. That is not hypothetical: it cost ~3 hours and $7 on 2026-10-09.
    """
    free_gb = shutil.disk_usage(ROOT).free / 2**30
    if free_gb < min_gb:
        raise RunRefused(
            f"{free_gb:.1f} GB free, need {min_gb} GB. Reclaim it with "
            "`uv run python -m evals.dbt.scripts.prune_runs --all --dry-run` "
            "and drop --dry-run when the list looks right.")


def validate(cfg: dict, image: str, allow_dirty: bool) -> None:
    import os
    for name, role in cfg["roles"].items():
        if not role.get("model"):
            raise RunRefused(f"roles.{name} has no model")
    if not os.environ.get("OPENAI_API_KEY"):
        raise RunRefused("OPENAI_API_KEY is not set on the host (.env)")
    v = sandbox.versions()
    if not v.get("server"):
        raise RunRefused(f"docker is not available: {v}")
    if not sandbox.image_id(image):
        raise RunRefused(f"image {image} does not exist -- run "
                         "`uv run python -m evals.dbt.scripts.build_image`")
    manifest_path = config_mod.resolve(cfg, "task_manifest")
    load_manifest(manifest_path)
    load_splits(config_mod.resolve(cfg, "splits"), manifest_path)
    for role in config_mod.roles(cfg).values():
        role.system_prompt_path.read_text(encoding="utf-8")
    if git_dirty() and not allow_dirty:
        raise RunRefused("git tree is dirty; commit first, or pass --allow-dirty "
                         "(recorded; the run is then not reportable)")
    check_free_space()


# --- one task attempt ------------------------------------------------------

def failure_category(passed: bool, outcome: Outcome, final_exit: int | None,
                     have_prediction: bool) -> str | None:
    if passed:
        return None
    if outcome.failure_category:
        return outcome.failure_category
    if final_exit not in (0, None) or not have_prediction:
        return "execution"
    return "untriaged"


def reset_attempt(attempt_dir: Path, name: str) -> None:
    sandbox.remove(name)
    if attempt_dir.exists():
        import shutil
        shutil.rmtree(attempt_dir)


def run_attempt(task: Task, attempt: int, split: str, cfg: dict, run_id: str,
                run_dir: Path, image: str, client) -> dict:
    root = config_mod.resolve(cfg, "spider2_root")
    deps_root = config_mod.resolve(cfg, "runs_dir") / "_deps"
    attempt_dir = run_dir / f"attempt_{attempt}" / task.instance_id
    name = sandbox.container_name(run_id, attempt, task.instance_id)
    reset_attempt(attempt_dir, name)
    attempt_dir.mkdir(parents=True)

    result = records.empty_result(run_id, cfg["experiment"], attempt, task.instance_id, split)
    sc = cfg["sandbox"]
    scfg = sandbox.SandboxConfig(image=image, network=sc["network"], memory=sc["memory"],
                                 cpus=sc["cpus"], pids_limit=sc["pids_limit"], user=sc["user"],
                                 keep_failed_containers=sc.get("keep_failed_containers", False))
    started = time.monotonic()
    outcome = Outcome("harness_error")
    container_info: dict = {}
    final_exit = None
    ctx = None
    try:
        sandbox.stage(task, root, attempt_dir / "workspace", deps_root)
        container_info = sandbox.start(name, attempt_dir / "workspace", scfg, {
            "run_id": run_id, "attempt": str(attempt), "instance_id": task.instance_id})
        records.write_json_atomic(attempt_dir / "container.json", container_info)
        result["container"]["id"] = container_info["id"]
        result["container"]["image_id"] = container_info["image_id"]

        ctx = TaskContext(cfg=cfg, roles=config_mod.roles(cfg), container=name,
                          attempt_dir=attempt_dir,
                          deadline=time.monotonic() + float(cfg["task_timeout_s"]),
                          client=client, run_id=run_id, instance_id=task.instance_id,
                          include_reasoning=cfg["api"].get("include_reasoning", True))
        # The project's own .yml spec, read on the host from the staged
        # workspace and handed over for free. Source is models/**/*.yml --
        # never task.evaluation, which names the scored tables.
        # Default OFF: measured harmful (turn cap hit 2 -> 19, +147k input
        # tokens per attempt, 28 passes against 31). A config with no [prompt]
        # section -- baseline.toml, the reportable control -- must not silently
        # inherit it.
        instruction, spec_stats = (
            spec.augment(task.instruction, attempt_dir / "workspace")
            if cfg.get("prompt", {}).get("inject_model_spec", False)
            else (task.instruction, {"models_declared": 0, "spec_chars": 0})
        )
        result.update(spec_stats)
        (attempt_dir / "instruction.md").write_text(instruction, encoding="utf-8")
        outcome = run_task(instruction, ctx)
        outcome, result["test_repair"] = test_gate(name, ctx, outcome, cfg, attempt_dir)

        if outcome.status != "harness_error" and cfg["dbt"].get("final_run", True):
            try:
                r = run_tool(name, "final_dbt_run", {}, ctx.limits,
                             float(cfg["dbt"]["final_run_timeout_s"]) + 30)
                (attempt_dir / "dbt_final.log").write_text(
                    r.payload.get("log") or r.payload.get("error", ""), encoding="utf-8")
                final_exit = r.payload.get("exit_code")
            except ContainerDead as exc:
                if exc.oom_killed:
                    outcome = Outcome("agent_error", outcome.main_turns,
                                      f"OOM in final dbt run: {exc}", "execution")
                else:
                    outcome = Outcome("harness_error", outcome.main_turns, str(exc))
    except sandbox.StageError as exc:
        outcome = Outcome("harness_error", 0, str(exc))
    except Exception as exc:  # noqa: BLE001
        outcome = Outcome("harness_error", outcome.main_turns,
                          f"{type(exc).__name__}: {exc}"[:1000])
    finally:
        keep = scfg.keep_failed_containers and outcome.status != "completed"
        state = sandbox.stop_and_remove(name, keep=keep) if container_info else {}
        if container_info:
            container_info.update(state)
            records.write_json_atomic(attempt_dir / "container.json", container_info)
            result["container"].update({"exit_code": state.get("exit_code"),
                                        "oom_killed": state.get("oom_killed", False)})

    have_prediction = False
    if outcome.status != "harness_error":
        pred = submission.place(attempt_dir, task.instance_id, task.start_db)
        have_prediction = pred is not None
        try:
            score = scoring.score_attempt(attempt_dir, task.instance_id, root, image)
        except Exception as exc:  # noqa: BLE001
            score = scoring.Score(None, "", f"{type(exc).__name__}: {exc}")
        if score.passed is None:
            outcome = Outcome("harness_error", outcome.main_turns,
                              f"scoring: {score.detail}")
        else:
            # §6.7: a timeout or agent_error is a fail even if the database it
            # left behind happens to match. The evaluator's verdict is kept.
            result["scored_pass"] = bool(score.passed)
            result["passed"] = bool(score.passed) and outcome.status not in (
                "timeout", "agent_error")

    result.update({
        "status": outcome.status,
        "failure_category": None if outcome.status == "harness_error" else
        failure_category(result["passed"], outcome, final_exit, have_prediction),
        "main_turns": outcome.main_turns,
        "duration_s": round(time.monotonic() - started, 1),
        "final_dbt_exit_code": final_exit,
        "error": outcome.error,
        "finished_at": records.now_iso(),
    })
    if ctx is not None:
        result["usage_by_role"] = ctx.usage_by_role
        result["total_cost_usd"] = ctx.total_cost
        result["subagent_calls"] = ctx.subagent_calls
        result["tool_calls_by_function"] = dict(ctx.tool_counts)
        for k in ("tool_calls", "tool_errors", "gateway_rejections", "dbt_calls"):
            result[k] = ctx.counts.get(k, 0)
    records.write_json_atomic(attempt_dir / "result.json", result)
    prune_attempt(attempt_dir, cfg, attempt)
    return result


REPAIR = """Your models are built, but `dbt test` fails. These are the project's
own declared assertions about its models -- not my opinion of your work, and
not the scoring. A `not_null` failure means a column you produced is NULL where
the schema says it never is; a `unique` failure means the model has more rows
than its key allows, so a join has duplicated them.

{log}

Fix the models so these pass, then run `run_dbt` with command `test` to confirm.
If an assertion is genuinely wrong about the data, leave it and say which and
why -- do not delete the test, disable the model, or weaken the schema to make
it pass.
"""


def test_gate(container: str, ctx, outcome: Outcome, cfg: dict,
              attempt_dir: Path) -> tuple[Outcome, dict]:
    """Run the project's own assertions, and give one pass to fix failures.

    The agent decides it is finished when `dbt run` exits 0, which says only
    that the SQL compiled. Measured on 192 attempts: 141 of 147 failures had a
    clean dbt exit. Meanwhile 80% of these projects declare column tests --
    809 `not_null` and 280 `unique` -- and the agent ran `test` in 23% of
    attempts. One flaky task shipped 2,777 NULLs in three columns its own
    schema marks `not_null`, passing on the attempts where it happened not to.

    So the signal already exists in the repo and is simply not consulted. This
    consults it. A gate rather than a sentence in the prompt, for the reason
    CLAUDE.md gives about boundaries: an instruction is advice, and the
    measured compliance rate for this particular advice is 23%.

    It deliberately does not fail the attempt when tests fail. Gold sometimes
    deviates from its own schema, so a failing assertion is evidence, not a
    verdict -- the evaluator stays the only thing that decides.
    """
    stats = {"ran": False, "exit_code": None, "repaired": False}
    if outcome.status != "completed" or not cfg["dbt"].get("test_gate", False):
        return outcome, stats
    try:
        r = run_tool(container, "final_dbt_test", {}, ctx.limits,
                     float(cfg["dbt"]["final_run_timeout_s"]) + 30)
    except ContainerDead:
        return outcome, stats
    # NOT `log`: that is the module logger, and shadowing it turned the
    # error path below into an AttributeError.
    test_log = r.payload.get("log") or r.payload.get("error", "")
    (attempt_dir / "dbt_test.log").write_text(test_log, encoding="utf-8")
    stats["ran"] = True
    stats["exit_code"] = r.payload.get("exit_code")
    if stats["exit_code"] in (0, None):
        return outcome, stats

    main = ctx.roles["main"]
    tail = test_log[-int(cfg["limits"].get("tool_output_max_chars", 4000)):]
    try:
        repaired = run_role(ctx, main, REPAIR.format(log=tail), attempt_dir, ctx.deadline)
    except Exception as exc:  # noqa: BLE001 -- a failed repair keeps the first answer
        log.warning("test-gate repair failed: %s", exc)
        return outcome, stats
    stats["repaired"] = True
    return Outcome(outcome.status, outcome.main_turns + repaired.num_turns,
                   outcome.error, outcome.failure_category, repaired.final_text), stats


def prune_attempt(attempt_dir: Path, cfg: dict, attempt: int) -> None:
    """Drop the attempt's bulk now that it is scored.

    A run kept ~9.5 GB -- 64 tasks x 3 attempts x ~28 MB, almost all of it the
    staged workspace and the submitted database. On 2026-10-09 that filled the
    disk and killed a run three hours in, at attempt 3 of 3. Nothing downstream
    reads either directory: the summary, triage and this module all work from
    result.json and tool_calls.jsonl, which are ~1% of the bytes.

    Submissions are kept for one attempt, because diffing a prediction against
    gold is the only way to learn what a failure actually was, and it cannot be
    done after the fact. Attempt 1 by default, which is the one worth diffing.
    """
    import shutil

    keep = cfg.get("runs", {}).get("keep_submissions_for_attempt", 1)
    targets = ["workspace"]
    if keep != attempt:
        targets.append("submission")
    for name in targets:
        target = attempt_dir / name
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)


def is_done(attempt_dir: Path) -> bool:
    p = attempt_dir / "result.json"
    if not p.exists():
        return False
    try:
        return records.read_json(p).get("status") not in (None, "harness_error")
    except ValueError:
        return False


# --- the run -----------------------------------------------------------------

def setup_logging(run_dir: Path) -> None:
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S")
    for h in (logging.FileHandler(run_dir / "runner.log", encoding="utf-8"),
              logging.StreamHandler(sys.stdout)):
        h.setFormatter(fmt)
        log.addHandler(h)


def write_triage_template(run_dir: Path, results: list[dict]) -> None:
    path = run_dir / "triage.csv"
    existing = set()
    if path.exists():
        with path.open(encoding="utf-8") as fh:
            existing = {(r["instance_id"], r["attempt"]) for r in csv.DictReader(fh)}
    fields = ["instance_id", "attempt", "split", "status", "failure_category", "stage",
              "explore_used", "example", "notes"]
    new = path.exists()
    with path.open("a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        if not new:
            w.writeheader()
        for r in results:
            if r["passed"] or (r["instance_id"], str(r["attempt"])) in existing:
                continue
            w.writerow({"instance_id": r["instance_id"], "attempt": r["attempt"],
                        "split": r["split"], "status": r["status"],
                        "failure_category": r["failure_category"], "stage": "",
                        "explore_used": "yes" if r["subagent_calls"].get("explore") else
                        ("no" if r["experiment"] == "routed" else ""),
                        "example": "", "notes": ""})


def execute_run(cfg: dict, run_id: str, run_dir: Path, selection: dict, image: str,
                client=None) -> list[dict]:
    from openai import OpenAI

    _, all_tasks = load_manifest(config_mod.resolve(cfg, "task_manifest"))
    splits = load_splits(config_mod.resolve(cfg, "splits"),
                         config_mod.resolve(cfg, "task_manifest"))
    by_id = {t.instance_id: t for t in all_tasks}
    tasks = [by_id[i] for i in selection["tasks"]]
    client = client or OpenAI(timeout=float(cfg["api"]["timeout_s"]),
                              max_retries=int(cfg["api"]["max_retries"]))
    removed = sandbox.cleanup_run(run_id)
    if removed:
        log.info(f"removed {removed} leftover container(s) for {run_id}")

    results = []
    for attempt in range(1, selection["attempts"] + 1):
        todo = [t for t in tasks
                if not is_done(run_dir / f"attempt_{attempt}" / t.instance_id)]
        log.info(f"attempt {attempt}: {len(todo)} to run, {len(tasks) - len(todo)} done")

        def work(t: Task) -> dict:
            with _print_lock:
                log.info(f"[a{attempt}] {t.instance_id:<32} running...")
            r = run_attempt(t, attempt, splits.get(t.instance_id, "?"), cfg, run_id,
                            run_dir, image, client)
            with _print_lock:
                log.info(f"[a{attempt}] {t.instance_id:<32} {r['status']:<13} "
                         f"passed={'y' if r['passed'] else 'n'} turns={r['main_turns']:<3} "
                         f"${r['total_cost_usd']:.4f} {r['duration_s']}s"
                         + (f" error={r['error'][:120]}" if r.get("error") else ""))
            return r

        with ThreadPoolExecutor(max_workers=int(cfg["concurrency"])) as pool:
            results += list(pool.map(work, todo))

    all_results = [records.read_json(p) for p in sorted(run_dir.glob("attempt_*/*/result.json"))]
    write_triage_template(run_dir, all_results)
    return all_results


def prepare_deps(cfg: dict, selection: dict, image: str) -> None:
    """Resolve cached dbt packages BEFORE the run manifest records their hashes."""
    _, all_tasks = load_manifest(config_mod.resolve(cfg, "task_manifest"))
    root = config_mod.resolve(cfg, "spider2_root")
    deps_root = config_mod.resolve(cfg, "runs_dir") / "_deps"
    for t in all_tasks:
        if t.instance_id in selection["tasks"]:
            sandbox.ensure_deps(t, root, deps_root, image, cfg["sandbox"]["user"])


def main(argv: list[str] | None = None) -> int:
    load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser(description="Run Spider2-DBT")
    ap.add_argument("--config", type=Path)
    ap.add_argument("--resume", metavar="RUN_ID")
    ap.add_argument("--tasks", help="comma-separated instance ids (smoke runs)")
    ap.add_argument("--attempts", type=int)
    ap.add_argument("--allow-dirty", action="store_true")
    ap.add_argument("--no-report", action="store_true")
    args = ap.parse_args(argv)

    if bool(args.config) == bool(args.resume):
        ap.error("pass exactly one of --config or --resume")

    image = content_tag()
    if args.resume:
        run_dir = ROOT / "runs" / "dbt" / args.resume
        if not (run_dir / "run_manifest.json").exists():
            print(f"no run_manifest.json in {run_dir}", file=sys.stderr)
            return 2
        stored = records.read_json(run_dir / "run_manifest.json")
        cfg = config_mod.load(stored["config"]["config_path"])
        run_id, selection = stored["run_id"], stored["selection"]
        try:
            validate(cfg, image, stored.get("allow_dirty", False))
        except RunRefused as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 2
        prepare_deps(cfg, selection, image)
        current = build_run_manifest(cfg, run_id, selection, image, stored.get("allow_dirty"))
        diff = fingerprint_diff(fingerprint(stored), fingerprint(current))
        if diff:
            print(f"refused: the run's inputs changed since it started: {diff[:12]}",
                  file=sys.stderr)
            return 2
    else:
        cfg = config_mod.load(args.config)
        try:
            validate(cfg, image, args.allow_dirty)
        except RunRefused as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 2
        _, tasks = load_manifest(config_mod.resolve(cfg, "task_manifest"))
        ids = [t.instance_id for t in tasks]
        if args.tasks:
            wanted = [t.strip() for t in args.tasks.split(",") if t.strip()]
            unknown = [w for w in wanted if w not in ids]
            if unknown:
                print(f"unknown task ids: {unknown}", file=sys.stderr)
                return 2
            ids = wanted
        selection = {"tasks": ids, "attempts": args.attempts or int(cfg["attempts"]),
                     "subset": bool(args.tasks), "attempts_override": args.attempts is not None}
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        run_id = f"{cfg['experiment']}-{stamp}"
        run_dir = config_mod.resolve(cfg, "runs_dir") / run_id
        run_dir.mkdir(parents=True)
        prepare_deps(cfg, selection, image)
        records.write_json_atomic(run_dir / "run_manifest.json",
                                  build_run_manifest(cfg, run_id, selection, image,
                                                     args.allow_dirty))

    setup_logging(run_dir)
    log.info(f"run {run_id}: {len(selection['tasks'])} task(s) x {selection['attempts']} "
             f"attempt(s), concurrency {cfg['concurrency']}, image {image} -> {run_dir}")
    results = execute_run(cfg, run_id, run_dir, selection, image)
    _record_models_returned(run_dir, results)
    if not args.no_report:
        from . import report
        report.write_summary(run_dir)
        log.info(f"summary: {run_dir / 'summary.md'}")
    harness = sum(1 for r in results if r["status"] == "harness_error")
    return 1 if harness else 0


def _record_models_returned(run_dir: Path, results: list[dict]) -> None:
    m = records.read_json(run_dir / "run_manifest.json")
    for r in results:
        for role, u in (r.get("usage_by_role") or {}).items():
            seen = m["roles"].setdefault(role, {}).setdefault("models_returned", [])
            for mid in u.get("models_returned", []):
                if mid not in seen:
                    seen.append(mid)
    records.write_json_atomic(run_dir / "run_manifest.json", m)


if __name__ == "__main__":
    sys.exit(main())
