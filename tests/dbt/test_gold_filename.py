"""Repairing a gold filename the eval set itself gets wrong.

Three tasks name a gold file their own gold directory does not contain, so
`evaluate.py` -- which joins the filename literally -- raises, the exception is
swallowed, and the task scores 0 whatever the agent did.

This sits in the scoring path, which is PROTECTED, so the tests below are
mostly about what it must NOT do. It must not choose between candidates, must
not fire when the named file is present, and must not reach outside the staged
copy.
"""

import json

import pytest

from evals.dbt import scoring


@pytest.fixture
def suite(tmp_path, monkeypatch):
    """A minimal spider2 clone with one task whose gold filename is wrong."""
    root = tmp_path / "clone"
    (root / "evaluation_suite" / "gold").mkdir(parents=True)
    (root / "evaluation_suite" / "gold" / "spider2_eval.jsonl").write_text(
        json.dumps({"instance_id": "demo001", "evaluation": {"func": "duckdb_match",
                    "parameters": {"gold": "wanted.duckdb", "condition_tabs": ["t"],
                                   "condition_cols": [[0]], "ignore_orders": [True]}}}) + "\n",
        encoding="utf-8")
    return root


def staged(tmp_path, *filenames):
    d = tmp_path / "staged"
    d.mkdir(exist_ok=True)
    for f in filenames:
        (d / f).write_bytes(b"duckdb-bytes")
    return d


def test_the_one_present_file_becomes_reachable(tmp_path, suite):
    d = staged(tmp_path, "actual.duckdb")
    used = scoring._repair_gold_filename(d, suite, "demo001")
    assert used == "actual.duckdb"
    assert (d / "wanted.duckdb").read_bytes() == b"duckdb-bytes"
    # The original stays: nothing is renamed or removed.
    assert (d / "actual.duckdb").exists()


def test_it_does_nothing_when_the_named_file_is_there(tmp_path, suite):
    d = staged(tmp_path, "wanted.duckdb", "actual.duckdb")
    assert scoring._repair_gold_filename(d, suite, "demo001") is None


def test_it_refuses_to_choose_between_candidates(tmp_path, suite):
    """Two candidates means guessing which is the answer key. Fail honestly."""
    d = staged(tmp_path, "one.duckdb", "two.duckdb")
    assert scoring._repair_gold_filename(d, suite, "demo001") is None
    assert not (d / "wanted.duckdb").exists()


def test_an_empty_gold_directory_is_left_alone(tmp_path, suite):
    d = staged(tmp_path)
    assert scoring._repair_gold_filename(d, suite, "demo001") is None


def test_an_unknown_task_is_not_an_error(tmp_path, suite):
    d = staged(tmp_path, "actual.duckdb")
    assert scoring._repair_gold_filename(d, suite, "nosuch999") is None


def test_it_writes_only_inside_the_staged_directory(tmp_path, suite):
    """The clone's gold must never be modified by scoring."""
    d = staged(tmp_path, "actual.duckdb")
    before = {p.name for p in (suite / "evaluation_suite" / "gold").iterdir()}
    scoring._repair_gold_filename(d, suite, "demo001")
    assert {p.name for p in (suite / "evaluation_suite" / "gold").iterdir()} == before
    assert sorted(p.name for p in d.iterdir()) == ["actual.duckdb", "wanted.duckdb"]


def test_it_is_wired_into_the_staging_path():
    """A repair nothing calls is a comment."""
    import inspect
    assert "_repair_gold_filename(" in inspect.getsource(scoring.score_submission)


# --- the real eval set ---------------------------------------------------

def test_the_three_known_tasks_have_exactly_one_candidate():
    """If a fourth task ever drifts into this state, it should be noticed."""
    from pathlib import Path

    gold = Path("vendor/Spider2/spider2-dbt/evaluation_suite/gold")
    if not gold.is_dir():
        pytest.skip("spider2 clone not present")
    spec = {}
    for line in (gold / "spider2_eval.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            d = json.loads(line)
            spec[d["instance_id"]] = d["evaluation"]["parameters"].get("gold")
    mismatched = []
    for task, wanted in spec.items():
        d = gold / task
        if not d.is_dir() or not isinstance(wanted, str) or (d / wanted).exists():
            continue
        mismatched.append((task, len(list(d.glob("*.duckdb")))))
    # Every known mismatch must be unambiguous, or the repair silently skips it.
    assert all(n == 1 for _, n in mismatched), mismatched
    assert {t for t, _ in mismatched} == {
        "xero_new001", "xero_new002", "social_media001"}, mismatched
