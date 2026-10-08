"""Summary metrics and --compare, on a 3-task x 3-attempt fixture."""

import json

import pytest

from evals.dbt import records, report, runner, scoring

TASKS = ["a", "b", "c"]
# passed per (task, attempt): a always, b in 2 of 3, c never
PASS = {("a", 1): True, ("a", 2): True, ("a", 3): True,
        ("b", 1): True, ("b", 2): False, ("b", 3): True,
        ("c", 1): False, ("c", 2): False, ("c", 3): False}


def make_run(tmp_path, name, experiment="baseline", image="sha256:img", concurrency=1,
             pass_map=PASS, main_model="gpt-6.1-sol", pricing=None, harness=()):
    run_dir = tmp_path / name
    cfg = {"experiment": experiment, "sandbox": {"keep_failed_containers": False},
           "roles": {"main": {"model": main_model}}, "concurrency": concurrency}
    manifest = {
        "run_id": name, "git_dirty": False, "config": cfg,
        "selection": {"tasks": TASKS, "attempts": 3, "subset": False, "attempts_override": False},
        "sandbox": {"image_id": image}, "concurrency": concurrency,
        "pricing": pricing or {"gpt-6.1-sol": {"input": 2, "output": 10}},
        "benchmark": {"manifest_sha256": "m", "splits_sha256": "s"},
        "roles": {"main": {"model": main_model, "settings_sent": {"store": False},
                           "max_turns": 40, "prompt_sha256": "p"}},
    }
    records.write_json_atomic(run_dir / "run_manifest.json", manifest)
    for (task, attempt), passed in pass_map.items():
        r = records.empty_result(name, experiment, attempt, task,
                                 "held_out" if task == "a" else "dev")
        r.update({"status": "harness_error" if (task, attempt) in harness else "completed",
                  "passed": passed, "failure_category": None if passed else "untriaged",
                  "duration_s": 10.0, "main_turns": 5, "total_cost_usd": 0.5,
                  "usage_by_role": {"main": {"model": main_model, "models_returned": ["x"],
                                             "calls": 5, "input_tokens": 1000,
                                             "cached_input_tokens": 0, "output_tokens": 100,
                                             "reasoning_tokens": 0, "cost_usd": 0.5}}})
        records.write_json_atomic(run_dir / f"attempt_{attempt}" / task / "result.json", r)
    return run_dir


def test_pass_metrics(tmp_path):
    s = report.summarize(make_run(tmp_path, "r1"))
    o = s["overall"]
    assert o["per_attempt_pass_rate"] == [round(2 / 3, 4), round(1 / 3, 4), round(2 / 3, 4)]
    assert o["pass@1"] == pytest.approx((2 / 3 + 1 / 3 + 2 / 3) / 3, abs=1e-4)
    assert o["pass^3"] == round(1 / 3, 4)
    assert o["noise_range"] == [round(1 / 3, 4), round(2 / 3, 4)]
    assert s["splits"]["held_out"]["pass^3"] == 1.0
    assert s["splits"]["dev"]["tasks"] == 2
    assert s["by_role"]["main"]["cost_usd_total"] == pytest.approx(4.5)
    assert s["cost_per_passed_task_attempt"] == pytest.approx(4.5 / 5)
    assert s["reportable"]


def test_harness_error_makes_a_run_non_reportable(tmp_path):
    s = report.summarize(make_run(tmp_path, "r1", harness={("c", 2)}))
    assert not s["reportable"]
    assert any("harness_error" in r for r in s["not_reportable_because"])


def test_markdown_renders(tmp_path):
    md = report.to_markdown(report.write_summary(make_run(tmp_path, "r1")))
    assert "pass^3" in md and "| all | 3 |" in md


def test_compare_reports_flips_and_verdict(tmp_path):
    better = dict(PASS) | {("c", 1): True, ("c", 2): True}
    a = make_run(tmp_path, "base")
    b = make_run(tmp_path, "routed", experiment="routed", pass_map=better)
    c = report.compare(a, b)
    assert c["majority_flips_fail_to_pass"] == ["c"]
    assert c["pass@1"][1] > c["pass@1"][0]
    assert "no clear change" in report.compare_markdown(report.compare(a, a)) \
        or report.compare(a, a)["verdict"] == "no clear change"


@pytest.mark.parametrize("kwargs", [
    {"image": "sha256:other"}, {"concurrency": 4}, {"main_model": "gpt-6-luna"},
    {"pricing": {"gpt-6.1-sol": {"input": 3, "output": 10}}},
])
def test_compare_refuses_mismatched_runs(tmp_path, kwargs):
    a = make_run(tmp_path, "base")
    b = make_run(tmp_path, "other", experiment="routed", **kwargs)
    with pytest.raises(report.CompareRefused):
        report.compare(a, b)


# --- small pieces of the runner and scorer ----------------------------------------

def test_evaluator_final_line_parser():
    out = "1\n{'instance_id': 'x', ...}\nx\n1.0 1 1\n"
    assert scoring.parse(out) == (1, 1)
    assert scoring.parse("1\n0.0 0 1") == (0, 1)
    assert scoring.parse("Traceback ...\nKeyError: 'x'") is None


def test_failure_categories():
    from evals.dbt.agent_adapter import Outcome
    assert runner.failure_category(True, Outcome("completed"), 0, True) is None
    assert runner.failure_category(False, Outcome("max_turns", failure_category="budget"), 0, True) == "budget"
    assert runner.failure_category(False, Outcome("completed"), 2, True) == "execution"
    assert runner.failure_category(False, Outcome("completed"), 0, False) == "execution"
    assert runner.failure_category(False, Outcome("completed"), 0, True) == "untriaged"


def test_resume_fingerprint_ignores_volatile_fields_only():
    a = {"run_id": "r", "created_at": "t1", "git_dirty": False, "sandbox": {
        "docker": {"client": "1"}, "image_id": "sha256:a"}, "roles": {"main": {"models_returned": []}}}
    b = json.loads(json.dumps(a)) | {"created_at": "t2"}
    b["sandbox"]["docker"] = {"client": "2"}
    b["roles"]["main"]["models_returned"] = ["m"]
    assert runner.fingerprint_diff(runner.fingerprint(a), runner.fingerprint(b)) == []
    b["sandbox"]["image_id"] = "sha256:b"
    assert runner.fingerprint_diff(runner.fingerprint(a), runner.fingerprint(b)) == ["sandbox.image_id"]


def test_is_done(tmp_path):
    d = tmp_path / "a"
    assert not runner.is_done(d)
    records.write_json_atomic(d / "result.json", {"status": "harness_error"})
    assert not runner.is_done(d)
    records.write_json_atomic(d / "result.json", {"status": "timeout"})
    assert runner.is_done(d)


def test_records_scrub_the_key(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-1234567890abcdef")
    p = tmp_path / "log.jsonl"
    records.append_jsonl(p, {"error": "bad key sk-test-1234567890abcdef"})
    records.write_json_atomic(tmp_path / "r.json", {"x": ["sk-test-1234567890abcdef"]})
    assert "sk-test" not in p.read_text() and "sk-test" not in (tmp_path / "r.json").read_text()


def test_content_tag_is_stable_and_tracks_inputs(tmp_path):
    from evals.dbt import image
    root = tmp_path
    (root / "src").mkdir()
    (root / "src" / "a.py").write_bytes(b"x = 1\n")
    t1 = image.content_tag(root)
    assert t1 == image.content_tag(root)
    (root / "src" / "a.py").write_bytes(b"x = 1\r\n")       # a CRLF checkout
    assert image.content_tag(root) == t1
    (root / "src" / "a.py").write_bytes(b"x = 2\n")
    assert image.content_tag(root) != t1
