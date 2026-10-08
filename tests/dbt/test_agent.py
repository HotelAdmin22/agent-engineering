"""The loop extension, tool specs, role-scoped dispatch, and delegation.

No API and no Docker: a scripted fake client stands in for the Responses API,
and container tools are swapped for a recorder. Live versions of these checks
are in test_container.py (-m docker) and the smoke runs.
"""

import json
import time

import pytest

from evals.dbt import agent_adapter, config, delegate, tools
from manual_loop import loop_openai

from .fakes import FakeClient, api_error

ROOT = config.ROOT


# --- the loop: new keywords pass through; defaults unchanged -----------------

def test_loop_defaults_send_what_they_always_sent():
    client = FakeClient({loop_openai.MODEL: ["hi"]})
    loop_openai.run_loop("q", [], lambda n, a: ("", False), client=client)
    req = client.requests[0]
    assert req["model"] == loop_openai.MODEL
    assert not {"store", "include", "reasoning", "text"} & set(req)


def test_loop_role_keywords_pass_through():
    seen = []
    client = FakeClient({"m": [[("t", {"x": 1})], "done"]})
    spec = {"name": "t", "description": "d", "input_schema": {"type": "object"}}
    result = loop_openai.run_loop(
        "q", [spec], lambda n, a: ("ok", False), client=client, model="m",
        strict_tools=True, store=False, include=["reasoning.encrypted_content"],
        text_format={"type": "json_schema", "name": "x", "schema": {}, "strict": True},
        reasoning={"effort": "low"}, on_response=lambda i, r: seen.append((i, r.model)))
    req = client.requests[0]
    assert req["model"] == "m" and req["store"] is False and req["tools"][0]["strict"]
    assert req["include"] == ["reasoning.encrypted_content"]
    assert req["text"]["format"]["name"] == "x" and req["reasoning"] == {"effort": "low"}
    assert seen == [(1, "m-echo"), (2, "m-echo")]
    assert result.stopped_because == "end_turn"


def test_loop_deadline_stops_with_timeout():
    client = FakeClient({"m": [[("t", {})]] * 5})
    spec = {"name": "t", "description": "d", "input_schema": {"type": "object"}}
    result = loop_openai.run_loop("q", [spec], lambda n, a: (time.sleep(0.05) or "ok", False),
                                  client=client, model="m", deadline=time.monotonic() + 0.01)
    assert result.stopped_because == "timeout"
    # the call that was made got its output before stopping
    assert result.messages[-1]["type"] == "function_call_output"


# --- tool specs --------------------------------------------------------------

@pytest.mark.parametrize("name", list(tools.TOOL_SPECS))
def test_every_spec_is_strict_compatible(name):
    schema = tools.TOOL_SPECS[name]["input_schema"]
    assert schema["additionalProperties"] is False
    assert sorted(schema["required"]) == sorted(schema["properties"])


def test_main_role_has_exactly_the_seven_tools():
    cfg = config.load(ROOT / "evals/dbt/configs/baseline.toml")
    assert [s["name"] for s in tools.specs_for(cfg["roles"]["main"]["tools"])] == [
        "read_file", "list_files", "search_files", "write_file", "edit_file",
        "query_duckdb", "run_dbt"]


def test_subagent_roles_have_no_write_dbt_or_delegate_tools():
    cfg = config.load(ROOT / "evals/dbt/configs/routed.toml")
    for role in ("explore", "diagnose"):
        assert not set(cfg["roles"][role]["tools"]) & {
            "write_file", "edit_file", "run_dbt", "delegate_explore", "delegate_diagnose"}


# --- a context with the container swapped for a recorder ----------------------

class Ctx(agent_adapter.TaskContext):
    pass


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    cfg = config.load(ROOT / "evals/dbt/configs/routed.toml")
    executed = []

    def fake_run_tool(container, name, args, limits, timeout):
        from evals.dbt.docker_exec import ExecResult
        executed.append(name)
        return ExecResult({"ok": True, "tool": name}, "ok", 0, 1)

    monkeypatch.setattr(tools, "run_tool", fake_run_tool)
    c = agent_adapter.TaskContext(cfg=cfg, roles=config.roles(cfg), container="none",
                                  attempt_dir=tmp_path, deadline=time.monotonic() + 60,
                                  client=None)
    c.executed = executed
    return c


def test_dispatch_rejects_tools_outside_the_role(ctx):
    for name in ("write_file", "edit_file", "run_dbt", "delegate_explore"):
        out, is_error = tools.dispatch(ctx, "explore", ctx.roles["explore"].tools, name, {})
        assert is_error and "not available" in json.loads(out)["error"]
    assert ctx.executed == []
    rows = [json.loads(l) for l in (ctx.attempt_dir / "tool_calls.jsonl").read_text().splitlines()]
    assert {r["status"] for r in rows} == {"rejected"}


def test_dispatch_routes_container_tools(ctx):
    out, is_error = tools.dispatch(ctx, "main", ctx.roles["main"].tools, "read_file",
                                   {"path": "x", "offset": None, "limit": None})
    assert not is_error and ctx.executed == ["read_file"]


# --- delegation ---------------------------------------------------------------

EXPLORE_OK = json.dumps({"answer": "a", "relevant_files": ["models/x.sql"],
                         "relevant_tables": [{"table": "t", "columns": ["id"]}],
                         "evidence": ["read models/x.sql"], "open_questions": []})


def test_delegate_brief_has_no_main_history(ctx):
    ctx.client = FakeClient({"gpt-6-luna": [EXPLORE_OK]})
    payload, status, extra = delegate.run(ctx, "delegate_explore",
                                          {"question": "which columns?", "focus_paths": []})
    assert status == "ok" and payload["answer"] == "a"
    brief = json.loads((ctx.attempt_dir / extra["subagent_dir"] / "brief.json").read_text())
    assert set(brief) == {"goal", "inputs", "output_schema", "done_condition", "budget"}
    req = ctx.client.requests[0]
    assert len(req["input"]) == 1 and json.loads(req["input"][0]["content"]) == brief
    assert req["text"]["format"]["strict"] is True
    assert ctx.usage_by_role["explore"]["calls"] == 1


def test_subagent_cannot_reach_write_or_dbt(ctx):
    ctx.client = FakeClient({"gpt-6-luna": [[("write_file", {"path": "x", "content": "y"}),
                                              ("run_dbt", {"command": "run", "select": None})],
                                             EXPLORE_OK]})
    payload, status, _ = delegate.run(ctx, "delegate_explore",
                                      {"question": "q", "focus_paths": []})
    assert status == "ok" and ctx.executed == []


def test_the_eleventh_delegate_call_is_a_budget_stop(ctx):
    ctx.client = FakeClient({"gpt-6-luna": [EXPLORE_OK] * 20})
    statuses = [delegate.run(ctx, "delegate_explore", {"question": "q", "focus_paths": []})[1]
                for _ in range(11)]
    assert statuses[:10] == ["ok"] * 10
    out = delegate.run(ctx, "delegate_explore", {"question": "q", "focus_paths": []})
    assert statuses[10] == "budget" and out[0] == {"error": "budget", "detail": out[0]["detail"]}


def test_invalid_subagent_output_is_an_error_not_a_crash(ctx):
    ctx.client = FakeClient({"gpt-6-luna": ['{"answer": 3}']})
    payload, status, _ = delegate.run(ctx, "delegate_explore", {"question": "q", "focus_paths": []})
    assert status == "error" and "validation" in payload["error"]
    assert ctx.subagent_calls["errors"] == 1


def test_subagent_timeout_returns_budget_to_main(ctx):
    ctx.cfg["delegation"]["subagent_timeout_s"] = 0
    ctx.client = FakeClient({"gpt-6-luna": [[("read_file", {"path": "x"})]] * 5})
    payload, status, _ = delegate.run(ctx, "delegate_explore", {"question": "q", "focus_paths": []})
    assert status == "budget" and payload["error"] == "budget"


def test_subagent_api_error_does_not_end_the_task(ctx):
    ctx.client = FakeClient({}, raise_for={"gpt-6-luna": api_error()})
    payload, status, _ = delegate.run(ctx, "delegate_diagnose",
                                      {"error_text": "boom", "focus_paths": []})
    assert status == "error" and "API error" in payload["error"]


def test_results_are_truncated_to_the_cap(ctx):
    big = json.dumps({"answer": "x" * 5000, "relevant_files": [], "relevant_tables": [],
                      "evidence": ["e" * 300] * 10, "open_questions": []})
    ctx.client = FakeClient({"gpt-6-luna": [big]})
    payload, status, _ = delegate.run(ctx, "delegate_explore", {"question": "q", "focus_paths": []})
    assert status == "ok" and payload["_truncated"]
    assert len(json.dumps(payload)) <= ctx.cfg["delegation"]["result_max_chars"] + 30


# --- main role through the adapter ---------------------------------------------

def test_main_role_usage_and_status(ctx):
    ctx.client = FakeClient({"gpt-6.1-sol": [[("read_file", {"path": "a", "offset": None,
                                                             "limit": None})], "done"]})
    outcome = agent_adapter.run_task("build the models", ctx)
    assert outcome.status == "completed" and outcome.main_turns == 2
    u = ctx.usage_by_role["main"]
    assert u["calls"] == 2 and u["input_tokens"] == 2000 and u["models_returned"] == ["gpt-6.1-sol-echo"]
    assert u["cost_usd"] == pytest.approx(2000 / 1e6 * 2 + 200 / 1e6 * 10)
    req = ctx.client.requests[0]
    assert req["input"][0]["content"] == "build the models"     # verbatim
    assert req["store"] is False and all(t["strict"] for t in req["tools"])
    transcript = (ctx.attempt_dir / "transcript.jsonl").read_text().splitlines()
    assert len(transcript) == 2


def test_bad_model_name_is_an_agent_error(ctx):
    ctx.client = FakeClient({}, raise_for={"gpt-6.1-sol": api_error("model not found")})
    outcome = agent_adapter.run_task("x", ctx)
    assert outcome.status == "agent_error" and outcome.failure_category == "agent_error"


def test_max_turns_is_a_budget_failure(ctx):
    ctx.roles["main"].max_turns = 2
    ctx.client = FakeClient({"gpt-6.1-sol": [[("list_files", {"pattern": None})]] * 5})
    outcome = agent_adapter.run_task("x", ctx)
    assert outcome.status == "max_turns" and outcome.failure_category == "budget"
