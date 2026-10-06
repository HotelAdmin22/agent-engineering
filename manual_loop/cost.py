"""Turn token counts into dollars, when the prices are configured.

Deliberately not shipping hardcoded prices. A stale rate in the source is
worse than no rate: it reports a confident number that is quietly wrong, and
nobody re-checks a figure that already looks plausible.

Set them in .env, from the provider's own pricing page:

    LUNA_INPUT_USD_PER_MTOK=0.25
    LUNA_OUTPUT_USD_PER_MTOK=2.00

With nothing set, `estimate()` returns None and callers report tokens only --
honest about not knowing rather than guessing.

The unit is USD per million tokens, which is how both providers quote.
"""

import os

PREFIXES = {
    "gpt-5.6-luna": "LUNA",
    "gpt-5.6-sol": "SOL",
    "gpt-5.6-terra": "TERRA",
    "claude-opus-5": "OPUS",
}


def rates(model: str) -> tuple[float, float] | None:
    """(input, output) USD per million tokens, or None if unconfigured."""
    prefix = PREFIXES.get(model)
    if not prefix:
        return None
    try:
        rate_in = os.environ.get(f"{prefix}_INPUT_USD_PER_MTOK")
        rate_out = os.environ.get(f"{prefix}_OUTPUT_USD_PER_MTOK")
        if rate_in is None or rate_out is None:
            return None
        return float(rate_in), float(rate_out)
    except ValueError:
        return None


def estimate(model: str, usage: dict) -> float | None:
    """USD for one run's usage, or None when prices are not configured."""
    priced = rates(model)
    if priced is None:
        return None
    rate_in, rate_out = priced
    # The Responses API reports input_tokens/output_tokens; fall back to the
    # total split as input if a provider only gives a total.
    tokens_in = usage.get("input_tokens")
    tokens_out = usage.get("output_tokens")
    if tokens_in is None and tokens_out is None:
        tokens_in, tokens_out = usage.get("total_tokens", 0), 0
    return round(
        (tokens_in or 0) / 1e6 * rate_in + (tokens_out or 0) / 1e6 * rate_out, 6
    )


def describe(model: str, usage: dict) -> str:
    """A line for a summary: dollars when known, tokens when not."""
    total = usage.get("total_tokens", 0)
    usd = estimate(model, usage)
    if usd is None:
        return f"{total:,} tokens (unpriced -- set {PREFIXES.get(model, 'MODEL')}_*_USD_PER_MTOK)"
    return f"{total:,} tokens, ${usd:.4f}"
