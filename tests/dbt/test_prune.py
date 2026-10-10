"""Pruning an attempt, and refusing a run that cannot finish.

Both exist because of one incident: on 2026-10-09 a run filled a 466 GB disk
and died on ENOSPC at attempt 3 of 3, after ~3 hours and $7. The money was
spent and the run could not be scored.
"""

import json

import pytest

from evals.dbt import runner


def make_attempt(tmp_path, attempt=1):
    d = tmp_path / f"attempt_{attempt}" / "demo001"
    (d / "workspace" / "models").mkdir(parents=True)
    (d / "workspace" / "models" / "m.sql").write_text("select 1", encoding="utf-8")
    (d / "submission" / "demo001").mkdir(parents=True)
    (d / "submission" / "demo001" / "demo.duckdb").write_bytes(b"\0" * 2048)
    (d / "result.json").write_text(json.dumps({"passed": True}), encoding="utf-8")
    (d / "tool_calls.jsonl").write_text('{"function":"run_dbt"}\n', encoding="utf-8")
    return d


def test_workspace_goes_and_the_records_stay(tmp_path):
    d = make_attempt(tmp_path)
    runner.prune_attempt(d, {"runs": {"keep_submissions_for_attempt": 1}}, attempt=1)
    assert not (d / "workspace").exists()
    # Everything the summary, triage and the progress reporter read.
    assert (d / "result.json").exists()
    assert (d / "tool_calls.jsonl").exists()


def test_submissions_are_kept_for_the_named_attempt(tmp_path):
    """Gold diffs are the only way to learn what a failure was, and they
    cannot be run after the database is gone."""
    d = make_attempt(tmp_path, attempt=1)
    runner.prune_attempt(d, {"runs": {"keep_submissions_for_attempt": 1}}, attempt=1)
    assert (d / "submission" / "demo001" / "demo.duckdb").exists()


def test_submissions_go_for_every_other_attempt(tmp_path):
    d = make_attempt(tmp_path, attempt=2)
    runner.prune_attempt(d, {"runs": {"keep_submissions_for_attempt": 1}}, attempt=2)
    assert not (d / "submission").exists()
    assert not (d / "workspace").exists()


def test_zero_keeps_no_submissions_at_all(tmp_path):
    d = make_attempt(tmp_path, attempt=1)
    runner.prune_attempt(d, {"runs": {"keep_submissions_for_attempt": 0}}, attempt=1)
    assert not (d / "submission").exists()


def test_the_default_keeps_attempt_one(tmp_path):
    """An absent [runs] section must not silently delete the diffable attempt."""
    d = make_attempt(tmp_path, attempt=1)
    runner.prune_attempt(d, {}, attempt=1)
    assert (d / "submission").exists()
    assert not (d / "workspace").exists()


def test_pruning_an_already_pruned_attempt_is_not_an_error(tmp_path):
    d = make_attempt(tmp_path, attempt=3)
    runner.prune_attempt(d, {}, attempt=3)
    runner.prune_attempt(d, {}, attempt=3)   # resume re-runs this path


def _usage(free_gb: float):
    return type("Usage", (), {"total": 0, "used": 0, "free": int(free_gb * 2**30)})()


def test_a_run_is_refused_when_the_disk_cannot_hold_it(monkeypatch):
    """Fail at minute zero, not at attempt 3 of 3."""
    monkeypatch.setattr(runner.shutil, "disk_usage", lambda _p: _usage(2))
    with pytest.raises(runner.RunRefused, match="2.0 GB free, need"):
        runner.check_free_space(min_gb=8.0)


def test_the_refusal_says_how_to_reclaim_the_space(monkeypatch):
    monkeypatch.setattr(runner.shutil, "disk_usage", lambda _p: _usage(0.5))
    with pytest.raises(runner.RunRefused, match="prune_runs"):
        runner.check_free_space(min_gb=8.0)


def test_enough_space_passes(monkeypatch):
    monkeypatch.setattr(runner.shutil, "disk_usage", lambda _p: _usage(40))
    runner.check_free_space(min_gb=8.0)


def test_validate_checks_free_space():
    """The guard is wired in, not merely defined."""
    import inspect
    assert "check_free_space()" in inspect.getsource(runner.validate)
