"""Mask likely personal data before it is written to disk.

The evidence store keeps raw result rows so a number can be traced back to the
query that produced it. On a synthetic warehouse that is harmless. On a real
one it means customer records in a JSONL file that nothing treats as sensitive,
sitting in `runs/` next to the transcripts.

Two independent rules, because either alone misses the obvious case:

  BY COLUMN -- a column called `email`, `phone`, `customer_name` is masked
  whatever it contains. Catches the empty-looking and the oddly-formatted.

  BY VALUE -- anything shaped like an email, an SSN, a phone number or a
  Luhn-valid card number is masked whatever the column is called. Catches
  `notes` and `attr_7`.

This is a reduction in exposure, not anonymisation. Re-identification from
quasi-identifiers (a date of birth plus a postcode) is not addressed, and
aggregate `fields` are left alone on purpose -- a SUM is the answer the report
needs, and masking it would make the evidence useless.

The in-memory record is NOT masked: placeholders resolve against it during the
run. Only the persisted copy is redacted.

Two false positives found in a real audit log, both fixed here, both worth
remembering: a redactor that corrupts the data it protects is worse than none.
"""

import re
from typing import Any

SENSITIVE_COLUMN = re.compile(
    r"name|email|e_mail|phone|mobile|address|street|postcode|zip|"
    r"ssn|nino|tax_id|dob|birth|passport|licen[cs]e|card|iban|account_no",
    re.I,
)

VALUE_PATTERNS = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "<email>"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "<ssn>"),
    (re.compile(r"\+\d[\d\s().-]{7,}\d"), "<phone>"),
]

# Card numbers are CHECKED, not just shaped. A plain "13 to 19 digits" pattern
# also matches float precision and row counts, and it silently rewrote real
# numbers in an audit log -- `slope 0.021798456123456` became `slope 0.<card>`,
# and `606*1351329*4090` lost a factor. Luhn removes essentially all of that.
CARD_CANDIDATE = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")

# Keys that are structural, not personal. Without this the audit log masked its
# own `name` field -- the tool name, `query_database` -- because the key regex
# matches the word "name".
SAFE_KEYS = {
    "name", "kind", "tool", "turn", "stage", "reason", "status", "table",
    "column", "label", "check", "verdict", "run_id", "ts", "db", "model",
    "is_error", "chars", "index", "id",
}

MASK = "<redacted>"


def _luhn(digits: str) -> bool:
    total, parity = 0, len(digits) % 2
    for i, char in enumerate(digits):
        n = int(char)
        if i % 2 == parity:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _mask_cards(text: str) -> str:
    def replace(match: re.Match) -> str:
        digits = re.sub(r"\D", "", match.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits):
            return "<card>"
        return match.group(0)

    return CARD_CANDIDATE.sub(replace, text)


def redact_value(value: Any) -> Any:
    """Mask a single value by shape. Numbers pass through untouched."""
    if not isinstance(value, str):
        return value
    out = value
    for pattern, replacement in VALUE_PATTERNS:
        out = pattern.sub(replacement, out)
    return _mask_cards(out)


def redact_rows(columns: list[str], rows: list[list[Any]]) -> list[list[Any]]:
    """A masked copy of the rows. The originals are not modified."""
    sensitive = {i for i, c in enumerate(columns) if SENSITIVE_COLUMN.search(c or "")}
    masked = []
    for row in rows:
        masked.append([
            MASK if i in sensitive and value is not None else redact_value(value)
            for i, value in enumerate(row)
        ])
    return masked


def redact_text(text: str | None) -> str | None:
    """For audit entries: SQL and tool arguments can carry literals."""
    return redact_value(text) if isinstance(text, str) else text


def redact_obj(obj: Any) -> Any:
    """Walk a structure, masking sensitive keys and value shapes."""
    if isinstance(obj, dict):
        return {
            k: (
                redact_obj(v)
                if str(k) in SAFE_KEYS or not SENSITIVE_COLUMN.search(str(k))
                else MASK
            )
            for k, v in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v) for v in obj]
    return redact_value(obj)


def columns_masked(columns: list[str]) -> list[str]:
    return [c for c in columns if SENSITIVE_COLUMN.search(c or "")]
