"""The package imports, so `pytest tests/dbt` never exits with code 5."""

import importlib

import pytest


@pytest.mark.parametrize("module", [
    "evals.dbt", "evals.dbt.runner", "evals.dbt.tasks", "evals.dbt.sandbox",
    "evals.dbt.agent_adapter", "evals.dbt.tools", "evals.dbt.docker_exec",
    "evals.dbt.delegate", "evals.dbt.submission", "evals.dbt.scoring",
    "evals.dbt.records", "evals.dbt.report", "evals.dbt.image", "evals.dbt.config",
    "evals.dbt.sandbox_tools.toolhost",
])
def test_module_imports(module):
    importlib.import_module(module)


def test_no_dbt_module_imports_anthropic_or_duckdb():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2] / "evals" / "dbt"
    import re
    banned = re.compile(r"^\s*(import|from)\s+(anthropic|duckdb)", re.M)
    for path in root.rglob("*.py"):
        assert not banned.search(path.read_text(encoding="utf-8")), path
