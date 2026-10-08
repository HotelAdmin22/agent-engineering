"""Stage task workspaces and manage the per-attempt task containers.

Not evals/sandbox.py (the Lite harness's, now folded into agent.gateway). This
one is infrastructure, not a SQL policy: what a task attempt can reach is
decided by what is mounted and which flags the container starts with.

Every task container gets exactly one mount (its workspace), no network, no
environment, a read-only root, dropped capabilities and resource caps. Gold is
never mounted, so no SQL, dbt model or file tool can reach it: the path does
not exist inside.
"""

import json
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .docker_exec import container_state, docker
from .tasks import Task, dir_sha256, example_dir

LABEL = "s2dbt"


class StageError(Exception):
    pass


@dataclass
class SandboxConfig:
    image: str
    network: str = "none"
    memory: str = "4g"
    cpus: str = "2"
    pids_limit: int = 256
    user: str = "1000:1000"
    keep_failed_containers: bool = False

    def flags(self) -> list[str]:
        return [
            "--network", self.network,
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--read-only",
            "--tmpfs", "/tmp:rw,size=256m",
            "--memory", self.memory,
            "--cpus", str(self.cpus),
            "--pids-limit", str(self.pids_limit),
            "--user", self.user,
        ]


def container_name(run_id: str, attempt: int, instance_id: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "-", f"s2dbt-{run_id}-a{attempt}-{instance_id}")
    return safe[:120]


# --- dbt deps ------------------------------------------------------------
#
# 7 of the 68 examples declare packages they do not vendor (V2). Resolving them
# per attempt would put the network inside the measurement and let package
# versions drift between attempts. Instead each is resolved ONCE, by a one-shot
# container that has network but mounts only a scratch copy of the example, and
# the result is cached under runs/dbt/_deps/<instance_id>/. Every attempt
# copies the same packages in; the cache hash is recorded in run_manifest.json.

def needs_deps(src: Path) -> bool:
    packages = src / "packages.yml"
    if not packages.exists():
        return False
    text = packages.read_text(encoding="utf-8", errors="replace")
    declared = re.search(r"^\s*-\s*(package|git|local)\s*:", text, re.M)
    vendored = (src / "dbt_packages").is_dir() and any((src / "dbt_packages").iterdir())
    return bool(declared) and not vendored


def deps_cache_dir(cache_root: Path, instance_id: str) -> Path:
    return Path(cache_root) / instance_id


def ensure_deps(task: Task, spider2_root: Path, cache_root: Path, image: str,
                user: str = "1000:1000") -> dict | None:
    """Resolve and cache dbt packages for a task that needs them."""
    src = example_dir(spider2_root, task.instance_id)
    if not needs_deps(src):
        return None
    cache = deps_cache_dir(cache_root, task.instance_id)
    if (cache / "dbt_packages").is_dir() and (cache / "deps.json").exists():
        return json.loads((cache / "deps.json").read_text(encoding="utf-8"))

    with tempfile.TemporaryDirectory(prefix="s2dbt-deps-", dir=Path(cache_root).parent
                                     if Path(cache_root).parent.exists() else None) as tmp:
        work = Path(tmp) / "workspace"
        shutil.copytree(src, work, ignore=shutil.ignore_patterns("*.duckdb"))
        proc = docker(
            "run", "--rm", "--network", "bridge", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--read-only",
            "--tmpfs", "/tmp:rw,size=256m", "--user", user,
            "--mount", f"type=bind,source={work.resolve()},target=/workspace",
            image, "dbt", "--no-use-colors", "deps", "--profiles-dir", "/workspace",
            timeout=600,
        )
        if proc.returncode != 0 or not (work / "dbt_packages").is_dir():
            raise StageError(f"dbt deps failed for {task.instance_id}: "
                             f"{(proc.stdout + proc.stderr)[-800:]}")
        cache.mkdir(parents=True, exist_ok=True)
        if (cache / "dbt_packages").exists():
            shutil.rmtree(cache / "dbt_packages")
        shutil.copytree(work / "dbt_packages", cache / "dbt_packages")
        lock = work / "package-lock.yml"
        if lock.exists():
            shutil.copy2(lock, cache / "package-lock.yml")
    info = {
        "instance_id": task.instance_id,
        "dbt_packages_sha256": dir_sha256(cache / "dbt_packages"),
        "package_lock": (cache / "package-lock.yml").read_text(encoding="utf-8")
        if (cache / "package-lock.yml").exists() else None,
        "resolved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (cache / "deps.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    return info


# --- staging -------------------------------------------------------------

def stage(task: Task, spider2_root: Path, workspace: Path, cache_root: Path) -> None:
    """Fresh copy of the example; the clone is never touched."""
    src = example_dir(spider2_root, task.instance_id)
    actual = dir_sha256(src)
    if actual != task.example_dir_sha256:
        raise StageError(f"{task.instance_id}: example dir hash changed "
                         f"({actual[:12]} != {task.example_dir_sha256[:12]}); aborting")
    if workspace.exists():
        shutil.rmtree(workspace)
    shutil.copytree(src, workspace)
    if needs_deps(src):
        cache = deps_cache_dir(cache_root, task.instance_id)
        if not (cache / "dbt_packages").is_dir():
            raise StageError(f"{task.instance_id} needs dbt deps but {cache} is empty; "
                             "run evals.dbt.scripts.check_tasks_dbt --deps")
        shutil.copytree(cache / "dbt_packages", workspace / "dbt_packages")
        if (cache / "package-lock.yml").exists():
            shutil.copy2(cache / "package-lock.yml", workspace / "package-lock.yml")


# --- containers ----------------------------------------------------------

def start(name: str, workspace: Path, cfg: SandboxConfig, labels: dict[str, str]) -> dict:
    remove(name)
    label_args = []
    for k, v in labels.items():
        label_args += ["--label", f"{LABEL}.{k}={v}"]
    flags = cfg.flags()
    proc = docker(
        "run", "-d", "--name", name, *label_args, *flags,
        "--mount", f"type=bind,source={Path(workspace).resolve()},target=/workspace",
        cfg.image, "sleep", "infinity", timeout=120,
    )
    if proc.returncode != 0:
        raise StageError(f"container start failed: {proc.stderr.strip()[:500]}")
    return {
        "name": name,
        "id": proc.stdout.strip(),
        "image": cfg.image,
        "image_id": image_id(cfg.image),
        "flags": flags,
        "mounts": [f"{Path(workspace).resolve()}:/workspace:rw"],
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def stop_and_remove(name: str, keep: bool = False) -> dict:
    """Record the final state, then remove (unless kept for debugging)."""
    state = container_state(name)
    docker("stop", "-t", "5", name, timeout=60)
    final = container_state(name) or state
    info = {
        "exit_code": final.get("ExitCode"),
        "oom_killed": bool(final.get("OOMKilled") or state.get("OOMKilled")),
        "was_running": bool(state.get("Running")),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if not keep:
        remove(name)
    return info


def remove(name: str) -> None:
    docker("rm", "-f", name, timeout=60)


def cleanup_run(run_id: str) -> int:
    proc = docker("ps", "-aq", "--filter", f"label={LABEL}.run_id={run_id}", timeout=60)
    ids = proc.stdout.split()
    if ids:
        docker("rm", "-f", *ids, timeout=120)
    return len(ids)


def image_id(tag: str) -> str | None:
    proc = docker("image", "inspect", tag, "--format", "{{.Id}}", timeout=60)
    return proc.stdout.strip() if proc.returncode == 0 else None


def versions() -> dict:
    proc = docker("version", "--format", "{{json .}}", timeout=60)
    try:
        data = json.loads(proc.stdout)
        return {"client": data.get("Client", {}).get("Version"),
                "server": (data.get("Server") or {}).get("Version")}
    except (json.JSONDecodeError, AttributeError):
        return {"client": None, "server": None, "error": proc.stderr.strip()[:200]}
