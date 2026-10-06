"""The triage agent's restrictions.

Every test here is an attempt to do something a triage agent must not be able
to do: read the answer key, walk out of the source tree, or propose a change to
whatever grades it.

These are the tests that matter most in this file set. A triage agent that can
edit the verifier does not fix failures, it removes the thing that reports
them -- and the result looks identical from the outside.
"""

import json

import pytest

from orchestrator.failures import collect
from orchestrator.triage import (
    PATCHABLE,
    PROTECTED,
    TriageTools,
    _protected_hit,
    parse_findings,
)


@pytest.fixture
def tools():
    return TriageTools(collect())


# --- reads are allowlisted ----------------------------------------------

@pytest.mark.parametrize("path", [
    "evals/gold/questions.jsonl",
    "evals/seeded/planted.json",
    "evals/run_evals.py",
    ".env",
    "data/warehouse.duckdb",
    "../../../etc/passwd",
    "src/agent/../../evals/gold/questions.jsonl",   # traversal out and back in
    "docs/../evals/gold/questions.jsonl",
])
def test_unreadable_paths_are_refused(tools, path):
    out, is_error = tools.execute("read_source", {"path": path})
    assert is_error, f"LEAK: {path}"
    assert "questions" not in json.loads(out).get("content", "")


@pytest.mark.parametrize("path", [
    "src/agent/tools/math.py",
    "manual_loop/loop_openai.py",
    "orchestrator/failures.py",
])
def test_source_files_are_readable(tools, path):
    out, is_error = tools.execute("read_source", {"path": path})
    assert not is_error
    assert json.loads(out)["content"]


def test_every_result_is_valid_json_even_when_oversized(tools):
    """Slicing serialised JSON cuts a string mid-token; the payload is cut instead."""
    out, _ = tools.execute("read_source", {"path": "orchestrator/triage.py"})
    parsed = json.loads(out)          # must not raise
    assert parsed.get("truncated") or parsed.get("content")


def test_there_is_no_write_tool(tools):
    names = {s["name"] for s in tools.schemas()}
    assert names == {"list_failures", "show_failure", "read_source"}
    out, is_error = tools.execute("write_file", {"path": "x", "content": "y"})
    assert is_error


# --- proposals aimed at the judge are rejected --------------------------

@pytest.mark.parametrize("path", [
    "src/agent/gateway/gateway.py",
    "src/agent/verifier/check.py",
    "evals/gold/questions.jsonl",
    "evals/seeded/plant.py",
    "evals/run_evals.py",
    "evals/score_anomalies.py",
    "tests/test_gateway.py",
])
def test_protected_paths_cannot_be_proposed(path):
    assert _protected_hit([path]), f"{path} should be protected"


@pytest.mark.parametrize("path", PATCHABLE)
def test_patchable_paths_are_allowed(path):
    assert _protected_hit([path + "thing.py"]) is None


def test_a_proposal_touching_the_verifier_is_dropped():
    report = """```json
    {"findings": [{"signature": "s", "occurrences": 9, "class": "crash",
      "diagnosis": "d", "proposed_change": "loosen the check",
      "target_paths": ["src/agent/verifier/check.py"],
      "assertion": {"kind": "signature_absent", "signature": "s"}}]}
    ```"""
    findings = parse_findings(report)
    assert len(findings) == 1
    assert not findings[0].actionable
    assert "protected" in findings[0].rejected


def test_a_proposal_without_an_assertion_is_dropped():
    report = """```json
    {"findings": [{"signature": "s", "occurrences": 9, "class": "crash",
      "diagnosis": "d", "proposed_change": "c",
      "target_paths": ["src/agent/tools/math.py"]}]}
    ```"""
    assert not parse_findings(report)[0].actionable


def test_a_proposal_without_a_target_is_dropped():
    report = """```json
    {"findings": [{"signature": "s", "occurrences": 9, "class": "crash",
      "diagnosis": "d", "proposed_change": "c", "target_paths": [],
      "assertion": {"kind": "signature_absent", "signature": "s"}}]}
    ```"""
    assert not parse_findings(report)[0].actionable


def test_a_well_formed_proposal_survives():
    report = """```json
    {"findings": [{"signature": "compute_delta :: ValueError", "occurrences": 11,
      "class": "crash", "diagnosis": "d", "proposed_change": "c",
      "target_paths": ["src/agent/tools/math.py"], "confidence": "high",
      "assertion": {"kind": "signature_absent", "signature": "compute_delta :: ValueError"}}]}
    ```"""
    findings = parse_findings(report)
    assert findings[0].actionable
    assert findings[0].occurrences == 11


def test_a_malformed_report_yields_nothing_rather_than_crashing():
    assert parse_findings("no json here") == []
    assert parse_findings("```json\n{nope}\n```") == []
    assert parse_findings(None) == []


def test_protected_list_covers_every_judging_surface():
    """A reminder in test form: adding a new grader means adding it here."""
    for required in ("src/agent/gateway/", "src/agent/verifier/",
                     "evals/gold/", "evals/seeded/", "tests/"):
        assert required in PROTECTED
