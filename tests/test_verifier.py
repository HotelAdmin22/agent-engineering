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


# --- path placeholders ---------------------------------------------------
#
# Added after a real run failed with "6 numbers typed directly into the report:
# 17, 0, 17, 0, 17, 1". None of them were typed. The model had written
# {{e17.segments[0].delta}} to cite one segment of a contribution breakdown;
# the grammar only allowed {{eN.field}}, so the regex did not match, the
# placeholder was not stripped, and the indices were read as digits.

@pytest.fixture
def nested():
    st = EvidenceStore()
    st.put_computation(fields={
        "total_delta": -24692.25,
        "segments": [
            {"label": "enterprise", "delta": -18000.50, "share_of_change_pct": 72.9},
            {"label": "smb", "delta": -6691.75, "share_of_change_pct": 27.1},
        ],
    })                                      # e1
    return st


def test_indexed_placeholder_is_not_a_typed_number():
    text = "enterprise fell {{e17.segments[0].delta}} ({{e17.segments[0].share_of_change_pct}}%)"
    assert find_unbacked_numbers(text) == []


def test_indexed_placeholder_resolves(nested):
    out = render("fell by {{e1.segments[0].delta}} of {{e1.total_delta}}", nested)
    assert out.ok, out.errors
    assert "-18,000.5" in out.text
    assert "-24,692.25" in out.text


def test_nested_label_resolves(nested):
    assert "smb" in render("{{e1.segments[1].label}}", nested).text


def test_plain_fields_still_work(nested):
    assert render("{{e1.total_delta}}", nested).ok


def test_index_out_of_range_is_an_error_not_a_blank(nested):
    out = render("{{e1.segments[9].delta}}", nested)
    assert not out.ok
    assert "index out of range" in out.errors[0]
    assert "{{e1.segments[9].delta}}" in out.text      # stays visible


def test_missing_nested_field_names_what_is_available(nested):
    out = render("{{e1.segments[0].nope}}", nested)
    assert not out.ok
    assert "delta" in out.errors[0]


def test_walking_into_a_scalar_is_an_error(nested):
    assert not render("{{e1.total_delta.x}}", nested).ok


def test_a_report_citing_segments_verifies_clean(nested):
    result = check_report(
        "Enterprise fell {{e1.segments[0].delta}}, "
        "{{e1.segments[0].share_of_change_pct}}% of {{e1.total_delta}}.",
        nested,
    )
    assert result.ok, [f.detail for f in result.failures]


# --- redaction -----------------------------------------------------------
#
# runs/ is not a place to leave customer rows. The in-memory record stays
# intact because placeholders resolve against it; only the persisted copy is
# masked.

from agent.evidence.redact import columns_masked, redact_obj, redact_rows


def test_sensitive_columns_are_masked_by_name():
    cols = ["customer_id", "customer_name", "email", "amount"]
    rows = [[1, "Ada Okafor", "ada@example.com", 249.0]]
    out = redact_rows(cols, rows)[0]
    assert out == [1, "<redacted>", "<redacted>", 249.0]


def test_values_are_masked_by_shape_whatever_the_column_is_called():
    out = redact_rows(["attr_7"], [["reach me at ada@example.com"]])[0]
    assert "<email>" in out[0]
    assert "ada@example.com" not in out[0]


@pytest.mark.parametrize("value,expected", [
    ("card 4111 1111 1111 1111", "<card>"),
    ("ssn 123-45-6789", "<ssn>"),
    ("call +44 7700 900123", "<phone>"),
])
def test_value_shapes(value, expected):
    assert expected in redact_rows(["notes"], [[value]])[0][0]


def test_numbers_are_never_masked():
    """Aggregates are the answer. Masking them makes evidence useless."""
    assert redact_rows(["revenue", "units"], [[37851.75, 180]])[0] == [37851.75, 180]


def test_audit_fields_are_redacted():
    out = redact_obj({"sql": "SELECT * FROM c WHERE email='ada@example.com'"})
    assert "ada@example.com" not in out["sql"]


def test_memory_is_untouched_but_disk_is_masked(tmp_path):
    store = EvidenceStore(tmp_path / "e.jsonl")
    record = store.put_query(
        "SELECT ...", ["customer_name", "amount"], [["Ada Okafor", 249.0]]
    )
    assert record.rows[0][0] == "Ada Okafor"          # placeholders need this
    import json
    persisted = json.loads((tmp_path / "e.jsonl").read_text(encoding="utf-8"))
    assert persisted["rows"][0][0] == "<redacted>"
    assert "customer_name" in persisted["note"]


def test_redaction_can_be_turned_off(tmp_path):
    store = EvidenceStore(tmp_path / "e.jsonl", redact=False)
    store.put_query("SELECT ...", ["customer_name"], [["Ada Okafor"]])
    import json
    assert json.loads((tmp_path / "e.jsonl").read_text())["rows"][0][0] == "Ada Okafor"


def test_columns_masked_names_what_was_hidden():
    assert columns_masked(["email", "amount", "phone"]) == ["email", "phone"]
