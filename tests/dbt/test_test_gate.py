"""The dbt test gate: run the project's own assertions, allow one repair.

Why a gate and not a prompt line: the agent already had `test` in its allowed
commands and ran it in 23% of attempts. One flaky task shipped 2,777 NULLs in
three columns its own schema marks `not_null`, and passed only on the attempts
where it happened not to. The signal existed and was not consulted.

What the gate must NOT do is decide the outcome. Gold sometimes deviates from
its own schema, so a failing assertion is evidence for the agent, never a
verdict -- the evaluator stays the only thing that scores.
"""

import pytest

from evals.dbt import runner
from evals.dbt.agent_adapter import Outcome


class FakeCtx:
    def __init__(self):
        self.roles = {"main": object()}
        self.limits = {}
        self.deadline = 1e9


def cfg(test_gate=True, max_chars=4000):
    return {"dbt": {"test_gate": test_gate, "final_run_timeout_s": 600},
            "limits": {"tool_output_max_chars": max_chars}}


def tool_result(exit_code, log="FAIL 3 not_null_fct_sales_cat_group"):
    return type("R", (), {"payload": {"exit_code": exit_code, "log": log}})()


@pytest.fixture
def done():
    return Outcome("completed", 12, None, None, "built it")


def test_passing_assertions_change_nothing(monkeypatch, tmp_path, done):
    monkeypatch.setattr(runner, "run_tool", lambda *a, **k: tool_result(0, "PASS=14"))
    out, stats = runner.test_gate("c", FakeCtx(), done, cfg(), tmp_path)
    assert out is done
    assert stats == {"ran": True, "exit_code": 0, "repaired": False}
    assert (tmp_path / "dbt_test.log").read_text(encoding="utf-8") == "PASS=14"


def test_failing_assertions_buy_one_repair_pass(monkeypatch, tmp_path, done):
    seen = {}

    def fake_run_role(ctx, role, prompt, log_dir, deadline):
        seen["prompt"] = prompt
        return type("R", (), {"num_turns": 5, "final_text": "fixed the join"})()

    monkeypatch.setattr(runner, "run_tool", lambda *a, **k: tool_result(1))
    monkeypatch.setattr(runner, "run_role", fake_run_role)
    out, stats = runner.test_gate("c", FakeCtx(), done, cfg(), tmp_path)

    assert stats == {"ran": True, "exit_code": 1, "repaired": True}
    assert out.main_turns == 17            # 12 + 5, so the cost is visible
    assert out.final_text == "fixed the join"
    # The agent must see which assertion failed, not just that one did.
    assert "not_null_fct_sales_cat_group" in seen["prompt"]


def test_the_repair_forbids_weakening_the_schema(monkeypatch, tmp_path, done):
    """An agent scored on failures going down can delete the test instead."""
    seen = {}
    monkeypatch.setattr(runner, "run_tool", lambda *a, **k: tool_result(1))
    monkeypatch.setattr(runner, "run_role",
                        lambda c, r, prompt, d, dl: seen.update(prompt=prompt) or
                        type("R", (), {"num_turns": 1, "final_text": "x"})())
    runner.test_gate("c", FakeCtx(), done, cfg(), tmp_path)
    for phrase in ("do not delete the test", "disable the model", "weaken the schema"):
        assert phrase in seen["prompt"]


def test_the_gate_is_off_by_default(monkeypatch, tmp_path, done):
    called = []
    monkeypatch.setattr(runner, "run_tool", lambda *a, **k: called.append(1))
    out, stats = runner.test_gate("c", FakeCtx(), done, {"dbt": {}, "limits": {}}, tmp_path)
    assert out is done and not called
    assert stats["ran"] is False


def test_an_unfinished_attempt_is_not_gated(monkeypatch, tmp_path):
    """max_turns or an agent error means there is nothing to repair."""
    called = []
    monkeypatch.setattr(runner, "run_tool", lambda *a, **k: called.append(1))
    for status in ("max_turns", "agent_error", "harness_error", "timeout"):
        out, stats = runner.test_gate(
            "c", FakeCtx(), Outcome(status, 40, None, "budget"), cfg(), tmp_path)
        assert stats["ran"] is False
    assert not called


def test_a_failed_repair_keeps_the_original_answer(monkeypatch, tmp_path, done):
    monkeypatch.setattr(runner, "run_tool", lambda *a, **k: tool_result(1))

    def boom(*a, **k):
        raise RuntimeError("API died")

    monkeypatch.setattr(runner, "run_role", boom)
    out, stats = runner.test_gate("c", FakeCtx(), done, cfg(), tmp_path)
    assert out is done                      # the first answer survives
    assert stats["repaired"] is False


def test_a_dead_container_is_not_fatal(monkeypatch, tmp_path, done):
    from evals.dbt.docker_exec import ContainerDead

    def dead(*a, **k):
        raise ContainerDead("c", False, "gone")

    monkeypatch.setattr(runner, "run_tool", dead)
    out, stats = runner.test_gate("c", FakeCtx(), done, cfg(), tmp_path)
    assert out is done and stats["ran"] is False


def test_the_log_handed_back_is_bounded(monkeypatch, tmp_path, done):
    seen = {}
    monkeypatch.setattr(runner, "run_tool", lambda *a, **k: tool_result(1, "Z" * 50_000))
    monkeypatch.setattr(runner, "run_role",
                        lambda c, r, prompt, d, dl: seen.update(prompt=prompt) or
                        type("R", (), {"num_turns": 1, "final_text": "x"})())
    runner.test_gate("c", FakeCtx(), done, cfg(max_chars=500), tmp_path)
    assert seen["prompt"].count("Z") <= 500
