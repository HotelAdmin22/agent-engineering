"""Container-side tools, exercised on the host against a temp workspace.

SANDBOX_WORKSPACE points the tools at tmp_path, exactly as /workspace is in
the container. The container is the first boundary; these are the second.
"""

import json
import os
from pathlib import Path

import duckdb
import pytest

from evals.dbt.sandbox_tools import dbt_runner, file_tools, path_guard, toolhost


@pytest.fixture
def ws(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    (root / "models").mkdir(parents=True)
    (root / "models" / "a.sql").write_text("select 1 as x\n-- todo\nselect 2\n", encoding="utf-8")
    (root / "models" / "schema.yml").write_text("version: 2\n", encoding="utf-8")
    con = duckdb.connect(str(root / "proj.duckdb"))
    con.execute("CREATE TABLE t AS SELECT range AS id FROM range(30)")
    con.close()
    (tmp_path / "secret.txt").write_text("gold", encoding="utf-8")
    monkeypatch.setenv("SANDBOX_WORKSPACE", str(root))
    return root


# --- path guard ------------------------------------------------------------

@pytest.mark.parametrize("path", ["../secret.txt", "models/../../secret.txt",
                                  "/etc/passwd", "/workspace/../secret.txt"])
def test_escapes_are_denied(ws, path):
    with pytest.raises(path_guard.PathDenied):
        path_guard.resolve(path)


def test_workspace_absolute_paths_are_rerooted(ws):
    assert path_guard.resolve("/workspace/models/a.sql") == (ws / "models" / "a.sql").resolve()


def test_symlink_escape_is_denied(ws, tmp_path):
    link = ws / "models" / "link.txt"
    try:
        os.symlink(tmp_path / "secret.txt", link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted on this host")
    with pytest.raises(path_guard.PathDenied):
        path_guard.resolve("models/link.txt")
    assert "error" in file_tools.read_file("models/link.txt")


@pytest.mark.parametrize("path", ["proj.duckdb", "models/../proj.duckdb", "x.duckdb.wal"])
def test_duckdb_files_cannot_be_written(ws, path):
    assert "error" in file_tools.write_file(path, "x")
    assert (ws / "proj.duckdb").stat().st_size > 100


# --- file tools ------------------------------------------------------------

def test_read_file_numbers_and_windows(ws):
    out = file_tools.read_file("models/a.sql", 2, 1)
    assert out["content"].strip() == "2  -- todo"
    assert out["total_lines"] == 3


def test_read_file_truncates(ws):
    (ws / "big.sql").write_text("x" * 10000, encoding="utf-8")
    out = file_tools.read_file("big.sql", None, None, max_chars=500)
    assert out["truncated"] and len(out["content"]) == 500


def test_list_and_search(ws):
    files = file_tools.list_files("models/*.sql")["files"]
    assert files == ["models/a.sql"]
    assert "proj.duckdb" in file_tools.list_files(None)["files"]
    hits = file_tools.search_files(r"select \d", None, "*.sql")["matches"]
    assert [h["line"] for h in hits] == [1, 3]


def test_write_then_edit(ws):
    assert file_tools.write_file("models/new/b.sql", "select 1")["created"]
    assert file_tools.edit_file("models/new/b.sql", "select 1", "select 2")["replaced"] == 1
    assert (ws / "models/new/b.sql").read_text() == "select 2"


def test_edit_requires_exactly_one_match(ws):
    assert "not found" in file_tools.edit_file("models/a.sql", "nope", "x")["error"]
    assert "2 times" in file_tools.edit_file("models/a.sql", "select", "x")["error"]


# --- SQL adapter (through agent.gateway) -------------------------------------

def test_query_returns_bounded_rows_and_true_count(ws):
    from evals.dbt.sandbox_tools import sql
    out = sql.query_duckdb("SELECT id FROM t ORDER BY id", max_rows=5)
    assert out["rows"] == [[0], [1], [2], [3], [4]]
    assert out["truncated"] and out["row_count"] == 30
    assert out["types"] == ["int"]


@pytest.mark.parametrize("bad", ["DROP TABLE t", "SELECT 1; SELECT 2",
                                 "ATTACH 'x.duckdb'"])
def test_query_refusals_are_marked(ws, bad):
    from evals.dbt.sandbox_tools import sql
    out = sql.query_duckdb(bad)
    assert out.get("rejected") and "error" in out


def test_query_releases_the_database(ws):
    from evals.dbt.sandbox_tools import sql
    sql.query_duckdb("SELECT COUNT(*) FROM t")
    con = duckdb.connect(str(ws / "proj.duckdb"))      # dbt's read-write open
    con.execute("CREATE TABLE built AS SELECT 1")
    con.close()


# --- dbt runner: refusals happen before dbt is ever started -------------------

@pytest.mark.parametrize("command", ["deps", "seed", "run-operation", "debug", "run; rm -rf /"])
def test_dbt_commands_outside_the_allowlist_are_refused(ws, command):
    out = dbt_runner.run_dbt(command)
    assert out.get("rejected")


@pytest.mark.parametrize("select", ["a;b", "$(whoami)", "a && b", "x`y`", "--vars x",
                                    "a --target prod", "-x"])
def test_dbt_selectors_are_pattern_checked(ws, select):
    out = dbt_runner.run_dbt("run", select)
    assert out.get("rejected"), select


def test_dbt_allowlist_comes_from_limits(ws):
    out = toolhost.handle("run_dbt", {"args": {"command": "build", "select": None},
                                      "limits": {"dbt_allowed_commands": ["run"]}})
    assert out.get("rejected")


def test_toolhost_unknown_tool_and_truncation(ws):
    assert "error" in toolhost.handle("rm", {})
    for i in range(40):
        (ws / "models" / f"model_{i:02d}.sql").write_text("select 1", encoding="utf-8")
    out = toolhost.handle("list_files", {"args": {"pattern": None},
                                         "limits": {"tool_output_max_chars": 300}})
    assert len(json.dumps(out)) <= 300 and out["truncated"]
