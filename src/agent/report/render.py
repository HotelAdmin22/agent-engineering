"""Render {{evidence_id.field}} placeholders from the evidence store.

The model writes prose containing `{{e7.delta_pct}}`. It never writes the
digit. This module is the only thing that turns an id into a number, which is
what makes "no digit in the report came from model text" a checkable property
rather than an intention.

An unresolvable placeholder is an error, not an empty string. Rendering
`{{e9.delta}}` as "" would produce a sentence that reads fine and says nothing
true -- the worst available failure.
"""

import re
from dataclasses import dataclass, field

from ..evidence import EvidenceStore

PLACEHOLDER = re.compile(r"\{\{\s*(e\d+)\.([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


@dataclass
class Rendered:
    text: str
    used: list[str] = field(default_factory=list)      # evidence ids cited
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _format(value: object) -> str:
    if isinstance(value, bool) or value is None:
        return str(value)
    if isinstance(value, float):
        # Money and percentages both read better with thousands separators and
        # two decimals; trailing ".0" on a whole number does not.
        return f"{value:,.2f}".rstrip("0").rstrip(".") if value % 1 else f"{value:,.0f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def render(template: str, store: EvidenceStore) -> Rendered:
    used: list[str] = []
    errors: list[str] = []

    def substitute(match: re.Match) -> str:
        evidence_id, name = match.group(1), match.group(2)
        try:
            value = store.resolve(evidence_id, name)
        except KeyError as exc:
            errors.append(str(exc).strip('"'))
            return match.group(0)      # leave it visible; do not render empty
        if evidence_id not in used:
            used.append(evidence_id)
        return _format(value)

    return Rendered(text=PLACEHOLDER.sub(substitute, template), used=used, errors=errors)


def placeholders(template: str) -> list[tuple[str, str]]:
    """Every (evidence_id, field) the template refers to."""
    return [(m.group(1), m.group(2)) for m in PLACEHOLDER.finditer(template)]
