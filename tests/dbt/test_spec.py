"""The injected model spec: what it says, what it costs, and what it must never read.

The last prompt experiment failed on cost rather than on being wrong, so the
size assertions here are not hygiene -- they are the thing that was actually
broken. The leak test is the one that matters most: spider2_eval.jsonl names
the scored tables, and a prompt built from it would be handing over the answer
key.
"""

import json

import pytest

from evals.dbt import spec

PROJECT = """
version: 2
models:
  - name: fct_orders
    description: |
      One row per order that has at least one line item.
      Orders with no lines are excluded.
    columns:
      - name: order_id
        description: Primary key. A long description that should not survive.
        tests: [unique, not_null]
      - name: customer_id
        description: Another long description.
  - name: dim_customer
    description: One row per customer.
    columns:
      - name: customer_id
"""


@pytest.fixture
def workspace(tmp_path):
    models = tmp_path / "models"
    (models / "marts").mkdir(parents=True)
    (models / "marts" / "schema.yml").write_text(PROJECT, encoding="utf-8")
    return tmp_path


def test_model_names_and_grain_reach_the_prompt(workspace):
    block = spec.for_workspace(workspace)
    assert "fct_orders" in block
    assert "dim_customer" in block
    # The grain sentence is the whole point -- these failures turn on whether
    # the model covers every row or only the ones with activity.
    assert "at least one line item" in block
    assert "One row per customer." in block


def test_column_names_are_kept_and_their_prose_is_dropped(workspace):
    block = spec.for_workspace(workspace)
    assert "order_id" in block and "customer_id" in block
    # Per-column descriptions are what make these files 152 KB.
    assert "should not survive" not in block
    assert "Another long description" not in block


def test_dbt_packages_are_excluded(workspace):
    pkg = workspace / "dbt_packages" / "fivetran" / "models"
    pkg.mkdir(parents=True)
    (pkg / "x.yml").write_text(
        "version: 2\nmodels:\n  - name: upstream_vendor_model\n", encoding="utf-8")
    assert "upstream_vendor_model" not in spec.for_workspace(workspace)


def test_the_block_is_bounded(workspace):
    """A cap that is not enforced is a comment."""
    many = ["version: 2", "models:"]
    for i in range(400):
        many.append(f"  - name: model_{i}")
        many.append(f"    description: {'x' * 300}")
    (workspace / "models" / "big.yml").write_text("\n".join(many), encoding="utf-8")
    block = spec.for_workspace(workspace, max_chars=4000)
    assert len(block) <= 4000
    assert "further model(s)" in block


def test_a_project_with_no_yml_adds_nothing(tmp_path):
    (tmp_path / "models").mkdir()
    instruction, stats = spec.augment("build the thing", tmp_path)
    assert instruction == "build the thing"
    assert stats == {"models_declared": 0, "spec_chars": 0}


def test_malformed_yml_does_not_crash_the_run(workspace):
    (workspace / "models" / "broken.yml").write_text(
        "version: 2\nmodels:\n  - name: [unclosed\n", encoding="utf-8")
    block = spec.for_workspace(workspace)
    assert "fct_orders" in block          # the good file still lands


def test_augment_reports_what_it_cost(workspace):
    instruction, stats = spec.augment("build the thing", workspace)
    assert instruction.startswith("build the thing")
    assert stats["models_declared"] == 2
    assert stats["spec_chars"] == len(spec.for_workspace(workspace))


# --- the boundary -------------------------------------------------------
#
# condition_tabs in spider2_eval.jsonl is the marking scheme. Nothing that
# builds a prompt may read it, and a test is the only thing that keeps that
# true once someone is optimising for the pass rate. Asserted on the call
# site rather than on the module text, because the module's docstring names
# the eval set precisely in order to say it must not be used.

def test_the_runner_builds_the_prompt_from_the_workspace_only():
    from pathlib import Path
    source = Path("evals/dbt/runner.py").read_text(encoding="utf-8")
    call = source[source.index("spec.augment("):]
    call = call[:call.index(")") + 1]
    assert "task.instruction" in call
    assert "workspace" in call
    # task.evaluation carries condition_tabs straight from the eval set.
    assert "evaluation" not in call
    assert "gold" not in call


def test_augment_takes_no_evaluation_argument():
    import inspect
    params = set(inspect.signature(spec.augment).parameters)
    assert params == {"instruction", "workspace", "max_chars"}
