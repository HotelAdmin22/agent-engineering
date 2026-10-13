"""Costing, after discovering the reported figure was 4.5x the real one.

D5 originally priced cached input at the full input rate as a deliberate
upper bound. The bound was then quoted as the actual cost in commits, docs and
to the person paying the bill. On these runs the cache hit rate is 92-94% --
every turn resends a long, almost unchanged prefix -- so ten full runs billed
$62.29 on that arithmetic and about $13.65 in reality.
"""

import pytest

from evals.dbt.agent_adapter import TaskContext


def FakeUsage(inp, cached, out, reasoning=0):
    """A plain dict: _usage_numbers accepts anything dict() can consume."""
    return {"input_tokens": inp, "output_tokens": out,
            "input_tokens_details": {"cached_tokens": cached},
            "output_tokens_details": {"reasoning_tokens": reasoning}}


class FakeResponse:
    def __init__(self, usage, model="gpt-6-luna"):
        self.usage = usage
        self.model = model


def ctx(cached_input=0.01):
    price = {"input": 0.10, "output": 0.50}
    if cached_input is not None:
        price["cached_input"] = cached_input
    c = TaskContext.__new__(TaskContext)
    c.cfg = {"pricing": {"gpt-6-luna": price}}
    c.usage_by_role = {}
    return c


def test_cached_input_is_billed_at_the_cached_rate():
    c = ctx()
    c.add_usage("main", "gpt-6-luna", FakeResponse(FakeUsage(1_000_000, 900_000, 100_000)))
    e = c.usage_by_role["main"]
    #   100k fresh @ $0.10/M + 900k cached @ $0.01/M + 100k out @ $0.50/M
    assert e["cost_usd"] == pytest.approx(0.01 + 0.009 + 0.05)
    # the old arithmetic, kept so the bound is still visible
    assert e["cost_usd_upper"] == pytest.approx(0.10 + 0.05)


def test_the_discount_is_the_bulk_of_the_bill_at_realistic_cache_rates():
    """92% cached is what these runs actually see."""
    c = ctx()
    c.add_usage("main", "gpt-6-luna", FakeResponse(FakeUsage(260_000, 239_200, 4_200)))
    e = c.usage_by_role["main"]
    assert e["cost_usd"] < e["cost_usd_upper"] / 3


def test_a_pricing_table_without_cached_input_keeps_the_old_bound():
    """Older configs must not silently change their numbers."""
    c = ctx(cached_input=None)
    c.add_usage("main", "gpt-6-luna", FakeResponse(FakeUsage(1_000_000, 900_000, 0)))
    e = c.usage_by_role["main"]
    assert e["cost_usd"] == e["cost_usd_upper"] == pytest.approx(0.10)


def test_cached_never_exceeds_total_input():
    """A provider reporting more cached than input must not produce a credit."""
    c = ctx()
    c.add_usage("main", "gpt-6-luna", FakeResponse(FakeUsage(1000, 5000, 0)))
    assert c.usage_by_role["main"]["cost_usd"] >= 0


def test_costs_accumulate_across_calls():
    c = ctx()
    for _ in range(3):
        c.add_usage("main", "gpt-6-luna", FakeResponse(FakeUsage(1_000_000, 900_000, 100_000)))
    e = c.usage_by_role["main"]
    assert e["calls"] == 3
    assert e["cost_usd"] == pytest.approx(3 * (0.01 + 0.009 + 0.05))


def test_every_shipped_config_prices_cached_input():
    """Otherwise a run silently reports the 4.5x figure again."""
    from pathlib import Path

    from evals.dbt import config as config_mod

    for path in sorted(Path("evals/dbt/configs").glob("*.toml")):
        cfg = config_mod.load(path)
        for model, price in cfg["pricing"].items():
            assert "cached_input" in price, f"{path.name}: {model} has no cached_input"
