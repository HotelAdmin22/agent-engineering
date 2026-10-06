"""Report rendering and verification.

The property under test: a number cannot reach a report without coming from
evidence. Every test here is an attempt to sneak one past.
"""

import pytest

from agent.evidence import EvidenceStore
from agent.report import render
from agent.verifier import check_report, find_unbacked_numbers


@pytest.fixture
def store():
    st = EvidenceStore()
    st.put_query(
        sql="SELECT SUM(amount) AS revenue FROM fct_orders",
        columns=["revenue"],
        rows=[[37851.75]],
    )                                       # e1
    st.put_computation(fields={
        "total_delta": -24692.25,
        "segments": [
            {"label": "electronics", "delta": -29950.00},
            {"label": "apparel", "delta": 5169.75},
            {"label": "stationery", "delta": 88.00},
        ],
    })                                      # e2
    return st


# --- unbacked numbers ----------------------------------------------------

def test_a_typed_number_is_caught():
    assert find_unbacked_numbers("Revenue was 37851.75 last month") == ["37851.75"]


def test_a_placeholder_is_not_a_typed_number():
    assert find_unbacked_numbers("Revenue was {{e1.revenue}} last month") == []


def test_a_typed_percentage_is_caught():
    assert "39%" in find_unbacked_numbers("down about 39% on the month")


def test_dates_and_quarters_are_allowed():
    """A period name is not a computed quantity."""
    text = "Between 2026-08 and 2026-09, and across Q3 2026, the trend held."
    assert find_unbacked_numbers(text) == []


def test_a_number_hidden_among_dates_is_still_caught():
    found = find_unbacked_numbers("In 2026-09 revenue hit 37851.75")
    assert found == ["37851.75"]


# --- end-to-end checks ---------------------------------------------------

def test_clean_report_passes(store):
    result = check_report(
        "Revenue was {{e1.revenue}} in 2026-09; the change was {{e2.total_delta}}.",
        store,
    )
    assert result.ok, [f.detail for f in result.failures]


def test_report_with_a_typed_number_fails(store):
    result = check_report("Revenue was 37851.75 in 2026-09.", store)
    assert not result.ok
    assert any(f.check == "numbers_backed" for f in result.failures)


def test_report_citing_nothing_is_not_a_pass(store):
    """No numbers and no citations is 'could not verify', not 'verified'."""
    result = check_report("Revenue went down a bit.", store)
    verdicts = {f.check: f.verdict for f in result.findings}
    assert verdicts["placeholders_resolve"] == "NEEDS"


def test_unknown_evidence_id_fails(store):
    result = check_report("Revenue was {{e99.revenue}}.", store)
    assert not result.ok
    assert any(f.check == "placeholders_resolve" for f in result.failures)


def test_unknown_field_fails(store):
    result = check_report("Revenue was {{e1.not_a_field}}.", store)
    assert not result.ok


def test_reconciliation_catches_parts_that_do_not_sum(store):
    store.put_computation(fields={
        "total_delta": -24692.25,
        "segments": [{"label": "electronics", "delta": -1000.00}],   # nowhere near
    })                                      # e3
    result = check_report("Breakdown: {{e3.total_delta}}.", store)
    assert not result.ok
    assert any(f.check == "reconciliation" for f in result.failures)


def test_reconciliation_tolerates_rounding(store):
    store.put_computation(fields={
        "total_delta": 100.00,
        "segments": [{"label": "a", "delta": 33.33},
                     {"label": "b", "delta": 33.33},
                     {"label": "c", "delta": 33.34}],
    })                                      # e3
    result = check_report("Breakdown: {{e3.total_delta}}.", store)
    assert result.ok, [f.detail for f in result.failures]


# --- rendering -----------------------------------------------------------

def test_render_substitutes_from_evidence(store):
    out = render("Revenue was {{e1.revenue}}.", store)
    assert out.ok
    assert "37,851.75" in out.text
    assert out.used == ["e1"]


def test_render_never_silently_empties_a_bad_placeholder(store):
    """An unresolvable placeholder must stay visible, not vanish."""
    out = render("Revenue was {{e9.revenue}}.", store)
    assert not out.ok
    assert "{{e9.revenue}}" in out.text
    assert out.errors


def test_render_leaves_prose_alone(store):
    out = render("No numbers here at all.", store)
    assert out.text == "No numbers here at all."
    assert out.used == []
