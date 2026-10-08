"""Live container checks. `uv run pytest -m docker` (needs the current image).

Isolation inside a real task container, every tool through docker exec, the
failure modes the runner distinguishes, and one full task attempt -- staging,
a scripted agent, the final dbt run, the move to submission/ and scoring with
Spider's own evaluate.py -- on a synthetic task.
"""

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import duckdb
import pytest

from evals.dbt import config, docker_exec, records, runner, sandbox, tools
from evals.dbt.image import content_tag
from evals.dbt.tasks import ROOT, Task, dir_sha256

from .fakes import FakeClient

pytestmark = pytest.mark.docker

IMAGE = content_tag()
SPIDER_EVAL = ROOT / "vendor/Spider2/spider2-dbt/evaluation_suite"


@pytest.fixture(scope="module", autouse=True)
def need_image():
    if not sandbox.image_id(IMAGE):
        pytest.skip(f"{IMAGE} not built: uv run python -m evals.dbt.scripts.build_image")


def make_project(path: Path, built: bool = False) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "dbt_project.yml").write_text(
        "name: proj\nversion: '1.0'\nprofile: proj\nmodel-paths: ['models']\n", encoding="utf-8")
    (path / "profiles.yml").write_text(
        "proj:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n"
        "      path: ./proj.duckdb\n      schema: main\n", encoding="utf-8")
    (path / "models").mkdir(exist_ok=True)
    con = duckdb.connect(str(path / "proj.duckdb"))
    con.execute("CREATE TABLE src AS SELECT range AS id, range * 3 AS v FROM range(10)")
    if built:
        con.execute("CREATE TABLE out AS SELECT id, v * 2 AS v2 FROM src")
    con.close()


@pytest.fixture
def box(tmp_path):
    ws = tmp_path / "workspace"
    make_project(ws)
    name = f"s2dbt-test-{os.getpid()}-{int(time.time() * 1000) % 100000}"
    cfg = sandbox.SandboxConfig(image=IMAGE)
    sandbox.start(name, ws, cfg, {"run_id": "pytest", "instance_id": "t"})
    yield name, ws
    sandbox.remove(name)


def sh(name: str, cmd: str) -> subprocess.CompletedProcess:
    return docker_exec.docker("exec", name, "sh", "-c", cmd, timeout=60)


# --- isolation ------------------------------------------------------------------

def test_no_network(box):
    name, _ = box
    r = sh(name, "python -c \"import socket; socket.create_connection(('1.1.1.1', 53), 3)\"")
    assert r.returncode != 0
    assert "eth0" not in sh(name, "cat /proc/net/dev").stdout


def test_no_key_even_when_the_host_has_one(box, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-never-arrive")
    name, _ = box
    assert "OPENAI" not in sh(name, "env").stdout
    out = docker_exec.run_tool(name, "read_file", {"path": "/proc/self/environ"}, {}, 30)
    assert "OPENAI" not in json.dumps(out.payload)


def test_read_only_root_and_writable_workspace(box):
    name, ws = box
    assert sh(name, "touch /opt/x").returncode != 0
    assert sh(name, "touch /workspace/x && echo ok").stdout.strip() == "ok"
    assert (ws / "x").exists()


def test_non_root_and_only_the_workspace_mounted(box):
    name, _ = box
    assert sh(name, "id -u").stdout.strip() != "0"
    mounts = sh(name, "cat /proc/mounts").stdout
    assert " /workspace " in mounts
    assert "gold" not in mounts and "vendor" not in mounts
    assert sh(name, "ls /vendor /gold /score /eval 2>&1; true").stdout.count("No such file") == 4


# --- tools through docker exec --------------------------------------------------

def test_every_tool_runs_in_the_container(box):
    name, ws = box
    limits = {"tool_output_max_chars": 4000, "max_rows_returned": 5, "query_timeout_s": 30,
              "dbt_allowed_commands": ["run", "build", "test", "compile", "parse"],
              "dbt_tool_timeout_s": 300}
    run = lambda tool, args: docker_exec.run_tool(name, tool, args, limits, 330)
    assert run("write_file", {"path": "models/out.sql", "content": "select id, v from src"}).status == "ok"
    assert run("edit_file", {"path": "models/out.sql", "old": "v from", "new": "v * 2 as v2 from"}).status == "ok"
    assert "models/out.sql" in run("list_files", {"pattern": "models/*"}).payload["files"]
    assert run("search_files", {"regex": "v2", "path": None, "glob": None}).payload["count"] == 1
    assert "v * 2" in run("read_file", {"path": "models/out.sql"}).payload["content"]
    dbt = run("run_dbt", {"command": "run", "select": "out"}).payload
    assert dbt["exit_code"] == 0, dbt["output_tail"]
    q = run("query_duckdb", {"sql": "SELECT SUM(v2) AS s FROM out"}).payload
    assert q["rows"] == [[270]]
    assert run("query_duckdb", {"sql": "DROP TABLE src"}).payload.get("rejected")
    assert run("run_dbt", {"command": "deps", "select": None}).payload.get("rejected")


def test_a_call_that_overruns_is_a_timeout(box):
    name, _ = box
    r = docker_exec.run_tool(name, "query_duckdb", {
        "sql": "SELECT COUNT(*) FROM range(100000000000) a WHERE hash(a.range) % 7 = 3"},
        {"query_timeout_s": 60}, 2)
    assert r.status == "timeout"


def test_a_dead_container_raises(box):
    name, _ = box
    docker_exec.docker("kill", name, timeout=60)
    with pytest.raises(docker_exec.ContainerDead) as exc:
        docker_exec.run_tool(name, "list_files", {}, {}, 30)
    assert exc.value.oom_killed is False


def test_cleanup_removes_labeled_containers(tmp_path):
    ws = tmp_path / "w"
    make_project(ws)
    cfg = sandbox.SandboxConfig(image=IMAGE)
    for i in range(2):
        sandbox.start(f"s2dbt-cleanup-{os.getpid()}-{i}", ws, cfg, {"run_id": f"crash{os.getpid()}"})
    assert sandbox.cleanup_run(f"crash{os.getpid()}") == 2
    assert sandbox.cleanup_run(f"crash{os.getpid()}") == 0


# --- one full task attempt on a synthetic task ----------------------------------

@pytest.fixture
def synthetic(tmp_path):
    """A one-task Spider2-DBT layout using the real evaluation_suite code."""
    if not SPIDER_EVAL.exists():
        pytest.skip("Spider2 clone not present")
    root = tmp_path / "spider2-dbt"
    make_project(root / "examples" / "syn001")
    suite = root / "evaluation_suite"
    shutil.copytree(SPIDER_EVAL, suite, ignore=shutil.ignore_patterns("gold"))
    make_project(suite / "gold" / "syn001", built=True)
    for f in ("dbt_project.yml", "profiles.yml"):
        (suite / "gold" / "syn001" / f).unlink()
    shutil.rmtree(suite / "gold" / "syn001" / "models")
    evaluation = {"func": "duckdb_match", "parameters": {
        "gold": "proj.duckdb", "condition_tabs": ["out"], "condition_cols": [[0, 1]],
        "ignore_orders": [True]}}
    (suite / "gold" / "spider2_eval.jsonl").write_text(
        json.dumps({"instance_id": "syn001", "evaluation": evaluation}) + "\n", encoding="utf-8")
    task = Task("syn001", "Build model out = id and v doubled as v2.", "DBT", "proj.duckdb",
                evaluation, dir_sha256(root / "examples" / "syn001"),
                dir_sha256(suite / "gold" / "syn001"))
    cfg = config.load(ROOT / "evals/dbt/configs/baseline.toml")
    cfg["spider2_root"] = str(root)
    cfg["runs_dir"] = str(tmp_path / "runs")
    cfg["task_timeout_s"] = 300
    return cfg, task, root


def agent_script(sql: str):
    return {"gpt-6.1-sol": [
        [("write_file", {"path": "models/out.sql", "content": sql})],
        [("run_dbt", {"command": "run", "select": None})],
        "Built models/out.sql.",
    ]}


def test_full_attempt_passes_with_a_correct_model(synthetic, tmp_path):
    cfg, task, root = synthetic
    run_dir = tmp_path / "runs" / "pytest-run"
    r = runner.run_attempt(task, 1, "dev", cfg, "pytest-run", run_dir, IMAGE,
                           FakeClient(agent_script("select id, v * 2 as v2 from src")))
    a = run_dir / "attempt_1" / "syn001"
    assert r["status"] == "completed" and r["passed"] is True, r
    assert r["final_dbt_exit_code"] == 0 and r["main_turns"] == 3
    for f in ("container.json", "transcript.jsonl", "tool_calls.jsonl", "dbt_final.log",
              "evaluate_stdout.txt", "result.json", "submission/results_metadata.jsonl",
              "submission/syn001/proj.duckdb"):
        assert (a / f).exists(), f
    assert not (a / "scoring").exists()
    assert not (a / "workspace" / "proj.duckdb").exists()        # moved, not copied
    c = records.read_json(a / "container.json")
    assert c["image_id"].startswith("sha256:") and "--network" in c["flags"]
    assert sandbox.container_state(c["name"]) == {}               # removed
    assert dir_sha256(root / "examples" / "syn001") == task.example_dir_sha256


def test_full_attempt_fails_with_a_wrong_model(synthetic, tmp_path):
    cfg, task, _ = synthetic
    r = runner.run_attempt(task, 1, "dev", cfg, "pytest-wrong", tmp_path / "runs" / "w",
                           IMAGE, FakeClient(agent_script("select id, v * 3 as v2 from src")))
    assert r["status"] == "completed" and r["passed"] is False
    assert r["failure_category"] == "untriaged"


def test_reset_deletes_a_half_finished_attempt(synthetic, tmp_path):
    cfg, task, _ = synthetic
    run_dir = tmp_path / "runs" / "resume"
    a = run_dir / "attempt_1" / "syn001"
    (a / "workspace" / "models").mkdir(parents=True)
    (a / "workspace" / "models" / "half.sql").write_text("select broken", encoding="utf-8")
    name = sandbox.container_name("resume", 1, "syn001")
    sandbox.start(name, a / "workspace", sandbox.SandboxConfig(image=IMAGE), {"run_id": "resume"})
    assert not runner.is_done(a)
    r = runner.run_attempt(task, 1, "dev", cfg, "resume", run_dir, IMAGE,
                           FakeClient(agent_script("select id, v * 2 as v2 from src")))
    assert r["passed"] and not (a / "workspace" / "models" / "half.sql").exists()
    assert runner.is_done(a)
